"""Incremental, serialized funding ingestion with atomic coverage commits.

Throttling never hides an unresolved gap. Recovery forces a query. The
adapter supplies coverage; neither an empty result nor local time does.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from . import models
from .adapters.base import AdapterError
from .store import StoreError


def funding_scope(account, market):
    return models.sha256_hex([account, market])


class FundingSynchronizer:
    def __init__(self, st, venues, *, clock=time.monotonic, interval=60, failure_interval=10):
        self.store, self.venues, self.clock = st, venues, clock
        self.interval, self.failure_interval = interval, failure_interval
        self.locks, self.next_query, self.errors = {}, {}, {}
        self.next_net_query = {}

    async def sync_applied_net(self, *, account, market, grid_id, force=True):
        """Optional net accounting; its success never clears history gaps."""
        if not force and self.clock()<self.next_net_query.get(grid_id,0):return None
        try:
            proof=await self.observe_position_net(account=account,market=market,grid_id=grid_id,apply_to_ledger=True)
        except Exception as error:
            self.next_net_query[grid_id]=self.clock()+self.failure_interval
            code=getattr(error,'code',None) or (str(error) if isinstance(error,AdapterError) and re.fullmatch('[A-Z_0-9]{1,100}',str(error)) else 'FUNDING_NET_UNAVAILABLE')
            await self.store.write(lambda c:c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',('funding-net-status:'+grid_id,
                json.dumps(dict(applied=False,code=code,checked_ts=models.utc_now_iso(),recovery_qualified=False),sort_keys=True))))
            raise
        self.next_net_query[grid_id]=self.clock()+self.interval
        return proof

    def status(self, account, market):
        scope = funding_scope(account, market)
        row = self.store.read(lambda c: c.execute("SELECT value FROM runtime_state WHERE key=?",
                              (f"funding:{scope}",)).fetchone())
        return json.loads(row[0]) if row else {}

    async def _ingest_positive(self,venue,page,*,account,market,cursor,saved,code):
        """Book proved payments atomically with an explicit unresolved gap.

        This never invents absent payments, advances a coverage cursor, turns
        applied position-net into a payment, or grants recovery permission.
        A later complete replay uses the same payment/owner primary keys.
        """
        from .adapters.base import FundingPositiveEvidence,FundingRecord
        from . import store
        evidence=page.positive_evidence
        if (not isinstance(evidence,FundingPositiveEvidence) or evidence.account!=account
                or evidence.market!=market or not evidence.source_revision
                or evidence.source_revision!=page.source_revision
                or tuple(evidence.records)!=tuple(page.records)):
            raise AdapterError('FUNDING_POSITIVE_SCOPE_MISMATCH')
        if saved.get('source_revision') and saved['source_revision']!=evidence.source_revision:
            raise AdapterError('FUNDING_SOURCE_CHANGED')
        rank=venue.funding_cursor_rank
        start,end=rank(evidence.observed_start),rank(evidence.observed_through)
        if (start>end or page.coverage_start!=evidence.observed_start
                or cursor is not None and start>rank(cursor)):
            raise AdapterError('FUNDING_POSITIVE_BOUNDARY_INVALID')
        records=[]
        for p in tuple(evidence.records):
            if (not isinstance(p,FundingRecord) or p.asset!=venue.market.settlement_asset
                    or not isinstance(p.payment_key,str) or not 1<=len(p.payment_key)<=256
                    or p.source_seq is None or not start<=rank(p.source_seq)<=end):
                raise AdapterError('FUNDING_POSITIVE_RECORD_INVALID')
            amount=models.canonical_decimal_text(models.parse_decimal_text(p.amount,'funding_amount'))
            ts=models.format_iso_ts(models.parse_iso_ts(p.ts,'funding_ts'))
            records.append((p.payment_key,amount,p.asset,ts,p.source_seq))
        scope=funding_scope(account,market)
        failed={**saved,'ok':False,'code':code,'attempted_ts':models.utc_now_iso(),
            'source_revision':evidence.source_revision,'positive_observed_count':len(records)}
        def commit(c):
            inserted=owned=unattributed=0
            for key,amount,asset,ts,seq in records:
                expected=(account,market,amount,asset,ts,seq)
                existing=c.execute('SELECT account,market,amount,asset,ts,source_seq FROM funding_payments WHERE payment_key=?',(key,)).fetchone()
                if existing and tuple(existing)!=expected:
                    raise StoreError('DATA_CONFLICT','conflicting positive funding payment')
                owners=c.execute("SELECT g.grid_id FROM grids g LEFT JOIN runtime_state ending "
                    "ON ending.key='closed-at:'||g.grid_id WHERE g.account_id=? AND g.market_key=? "
                    "AND g.created_ts<=? AND (g.status!='CLOSED' OR COALESCE(ending.value,g.updated_ts)>=?) "
                    "ORDER BY g.created_ts DESC LIMIT 2",(account,market,ts,ts)).fetchall()
                if len(owners)>1:raise StoreError('DATA_CONFLICT','ambiguous positive funding ownership')
                owner=owners[0][0] if owners else None
                previous=c.execute('SELECT grid_id FROM funding_owners WHERE payment_key=?',(key,)).fetchone()
                if previous and previous[0]!=owner:
                    raise StoreError('DATA_CONFLICT','positive funding ownership changed')
                metadata=dict(account=account,market=market,payment_key=key,amount=amount,asset=asset,ts=ts,
                    source_seq=seq,source_revision=evidence.source_revision,grid_id=owner,
                    coverage_advanced=False,source='native-positive-payment')
                evidence_key='funding-positive:'+scope+':'+key
                prior=c.execute('SELECT value FROM runtime_state WHERE key=?',(evidence_key,)).fetchone()
                if prior and json.loads(prior[0])!=metadata:
                    raise StoreError('DATA_CONFLICT','positive funding evidence changed')
                c.execute('INSERT OR IGNORE INTO runtime_state(key,value) VALUES(?,?)',
                    (evidence_key,json.dumps(metadata,sort_keys=True)))
                if owner is None:
                    unattributed+=1;continue
                owned+=1
                if not existing:
                    c.execute('INSERT INTO funding_payments VALUES(?,?,?,?,?,?,?,?)',
                        (key,account,market,amount,asset,ts,seq,'venue_history_positive'))
                    inserted+=1
                c.execute('INSERT OR IGNORE INTO funding_owners VALUES(?,?)',(key,owner))
                store.register_event(c,event_key='funding-positive:'+key,scope=owner,
                    event_type='funding_payment_observed',payload=metadata)
            checkpoint={**failed,'positive_inserted_count':inserted,'positive_owned_count':owned,
                'positive_unattributed_count':unattributed}
            c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('funding:'+scope,json.dumps(checkpoint,sort_keys=True)))
            return checkpoint
        return await self.store.write(commit)

    async def observe_position_net(self, *, account, market, grid_id, apply_to_ledger=False):
        """Persist attributed position NET evidence, without booking payments.

        A net lifecycle record is not an hourly payment. Keep it outside the
        native-payment ledger and its coverage cursor so later history cannot
        double-charge the same interval. Explicit ledger publication additionally
        requires the durable execution ledger to reproduce the native proof.
        """
        from dataclasses import asdict
        from .adapters.base import OrderRequest
        from .adapters.bulk_closed_positions import BulkClosedNetProof
        from .journal import OrderJournal, is_native_protection
        from . import store
        scope = funding_scope(account, market)
        async with self.locks.setdefault(scope, asyncio.Lock()):
            venue = self.venues.get(account)
            if venue is None:
                raise AdapterError('FUNDING_NET_SCOPE_MISMATCH')
            if hasattr(venue, 'market_venue'): venue = venue.market_venue(market)
            grid = self.store.read(lambda c: c.execute('SELECT account_id,market_key FROM grids WHERE grid_id=?', (grid_id,)).fetchone())
            if not grid or tuple(grid) != (account, market) or venue._account.account_id != account:
                raise AdapterError('FUNDING_NET_SCOPE_MISMATCH')
            intents = OrderJournal(self.store).intents_for_grid(grid_id)
            bindings = self.store.read(lambda c: {r['intent_id']: dict(r) for r in c.execute(
                'SELECT e.* FROM exchange_orders e JOIN order_intents i ON i.intent_id=e.intent_id '
                'WHERE i.grid_id=? AND e.account=?', (grid_id, account))})
            expected = {}
            for it in intents:
                if is_native_protection(it) or not it.get('client_id') or it['intent_id'] not in bindings:
                    continue
                b = bindings[it['intent_id']]
                if b['client_id'] != it['client_id'] or not b['exchange_order_id']:
                    raise AdapterError('FUNDING_NET_ORIGINAL_UNBOUND')
                req = OrderRequest(it['intent_id'], it['client_id'], account, market, it['side'],
                    it['qty_base'], it['price'], it['policy'], bool(it['reduce_only']), it['epoch'],
                    trigger_price=it.get('trigger_price'), whole_position=bool(it.get('whole_position')))
                expected[it['client_id']] = (b['exchange_order_id'], models.sha256_hex(req.to_dict()))
            if not 1 <= len(expected) <= 16 or not callable(getattr(venue, 'reconcile_closed_position_net', None)):
                raise AdapterError('FUNDING_NET_PROVIDER_UNAVAILABLE')
            # This diagnostic must not monopolize the market's funding lock
            # indefinitely. Only unsigned reads are canceled by this bound.
            proof = await asyncio.wait_for(venue.reconcile_closed_position_net(list(expected)), 90)
            if (not isinstance(proof, BulkClosedNetProof) or not proof.lifecycle_matched
                    or proof.account != account or proof.market != market
                    or any(p.account != account or p.market != market for p in proof.positions)
                    or len(proof.original_orders) != len(expected)
                    or {o['client_id']: (o['order_id'], o['request_hash']) for o in proof.original_orders} != expected):
                raise AdapterError('FUNDING_NET_PROOF_SCOPE_MISMATCH')
            def commit(c):
                current = c.execute('SELECT account_id,market_key FROM grids WHERE grid_id=?', (grid_id,)).fetchone()
                if not current or tuple(current) != (account, market):
                    raise AdapterError('FUNDING_NET_SCOPE_MISMATCH')
                for position in proof.positions:
                    payload = {'grid_id': grid_id, 'source': proof.source, 'position': asdict(position),
                               'ownership_basis': 'original-request-bindings',
                               'funding_payment_history_certified': False}
                    key = 'funding-position-net:' + scope + ':' + position.position_key
                    previous = c.execute('SELECT value FROM runtime_state WHERE key=?', (key,)).fetchone()
                    if previous and json.loads(previous[0]) != payload:
                        raise StoreError('DATA_CONFLICT', 'position net evidence changed')
                    c.execute('INSERT OR IGNORE INTO runtime_state(key,value) VALUES(?,?)',
                              (key, json.dumps(payload, sort_keys=True)))
                store.register_event(c, event_key='funding-position-net-proof:' + proof.proof_id,
                    scope=grid_id, event_type='funding_position_net_observed',
                    payload={'proof_id': proof.proof_id, 'source': proof.source,
                             'position_count': len(proof.positions), 'funding_cost': proof.funding_cost,
                             'payment_coverage_advanced': False})
                if apply_to_ledger:
                    from .funding_net import publish_closed_net
                    publish_closed_net(c,proof=proof,grid_id=grid_id,account=account,market=market,owner=venue.owner)
                    c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        ('funding-net-status:'+grid_id,json.dumps(dict(applied=True,code=None,proof_id=proof.proof_id,
                            position_count=len(proof.positions),checked_ts=models.utc_now_iso(),recovery_qualified=False,
                            funding_payment_history_certified=False),sort_keys=True)))
            await self.store.write(commit)
            return proof

    async def sync(self, *, account, market, force=True):
        scope = funding_scope(account, market)
        async with self.locks.setdefault(scope, asyncio.Lock()):
            if not force and self.clock() < self.next_query.get(scope, 0):
                return {"ok": scope not in self.errors, "skipped": True,
                        "code": self.errors.get(scope)}
            venue = self.venues[account]
            if hasattr(venue, "market_venue"):
                venue = venue.market_venue(market)
            if venue._account.account_id != account or venue.market.market_key != market:
                raise AdapterError("FUNDING_SCOPE_MISMATCH")
            saved = self.status(account, market)
            cursor = saved.get("cursor")
            try:
                if cursor and hasattr(venue, "funding_replay_cursor"):
                    cursor = venue.funding_replay_cursor(cursor)
                if cursor is None:
                    anchor = self.store.read(lambda c: c.execute("SELECT value FROM runtime_state WHERE key=?",
                                             (f"funding-start:{scope}",)).fetchone())
                    cursor = anchor[0] if anchor else None
                page = await venue.funding_history(cursor=cursor)
                if not page.complete or page.truncated:
                    code="FUNDING_HISTORY_SCAN_PENDING" if getattr(page,'scan_pending',False) else "FUNDING_HISTORY_INCOMPLETE"
                    if getattr(page,'positive_evidence',None) is not None:
                        saved=await self._ingest_positive(venue,page,account=account,market=market,
                            cursor=cursor,saved=saved,code=code)
                    raise AdapterError(code)
                rank = venue.funding_cursor_rank
                start, end = rank(page.coverage_start), rank(page.coverage_through)
                if start > end or cursor is not None and start > rank(cursor):
                    raise AdapterError("FUNDING_COVERAGE_GAP")
                if saved.get("cursor") is not None and end < rank(saved["cursor"]):
                    raise AdapterError("FUNDING_COVERAGE_REGRESSED")
                if saved.get("source_revision") and saved["source_revision"] != page.source_revision:
                    raise AdapterError("FUNDING_SOURCE_CHANGED")
                payments = []
                for p in page.records:
                    amount = models.canonical_decimal_text(models.parse_decimal_text(p.amount, "funding_amount"))
                    ts = models.format_iso_ts(models.parse_iso_ts(p.ts, "funding_ts"))
                    if p.asset != venue.market.settlement_asset or not p.payment_key:
                        raise AdapterError("FUNDING_RECORD_INVALID")
                    if p.source_seq is None or not start <= rank(p.source_seq) <= end:
                        raise AdapterError("FUNDING_RECORD_BOUNDARY_INVALID")
                    payments.append((p, amount, ts))
                checkpoint = {"cursor": page.coverage_through, "coverage_start": page.coverage_start,
                              "source_revision": page.source_revision, "checked_ts": models.utc_now_iso(),
                              "ok": True, "code": None}
                def commit(c):
                    for p, amount, ts in payments:
                        existing = c.execute("SELECT account,market,amount,asset,ts,source_seq FROM funding_payments WHERE payment_key=?",
                                             (p.payment_key,)).fetchone()
                        if existing and tuple(existing) != (account, market, amount, p.asset, ts, p.source_seq):
                            raise StoreError("DATA_CONFLICT", "conflicting funding payment")
                        owner = c.execute("SELECT g.grid_id FROM grids g LEFT JOIN runtime_state ending "
                            "ON ending.key='closed-at:'||g.grid_id WHERE g.account_id=? AND g.market_key=? "
                            "AND g.created_ts<=? AND (g.status!='CLOSED' OR COALESCE(ending.value,g.updated_ts)>=?) "
                            "ORDER BY g.created_ts DESC LIMIT 1", (account, market, ts, ts)).fetchone()
                        if not owner:
                            continue
                        previous = c.execute("SELECT grid_id FROM funding_owners WHERE payment_key=?", (p.payment_key,)).fetchone()
                        if previous and previous[0] != owner[0]:
                            raise StoreError("DATA_CONFLICT", "funding ownership changed")
                        c.execute("INSERT OR IGNORE INTO funding_payments VALUES(?,?,?,?,?,?,?,?)",
                            (p.payment_key, account, market, amount, p.asset, ts, p.source_seq, "venue_history"))
                        c.execute("INSERT OR IGNORE INTO funding_owners VALUES(?,?)", (p.payment_key, owner[0]))
                    c.execute("INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (f"funding:{scope}", json.dumps(checkpoint, sort_keys=True)))
                await self.store.write(commit)
            except Exception as error:
                code = getattr(error, "code", None) or (str(error) if isinstance(error, AdapterError) and
                    re.fullmatch(r"[A-Z0-9_:-]+", str(error)) else "HL_TRANSPORT_UNCERTAIN" if isinstance(error, TimeoutError) else "FUNDING_SYNC_FAILED")
                self.errors[scope] = code
                self.next_query[scope] = self.clock() + self.failure_interval
                failed = {**saved, "ok": False, "code": code, "attempted_ts": models.utc_now_iso()}
                await self.store.write(lambda c: c.execute("INSERT INTO runtime_state(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f"funding:{scope}", json.dumps(failed, sort_keys=True))))
                raise
            self.errors.pop(scope, None)
            self.next_query[scope] = self.clock() + (0 if venue._account.fixture else self.interval)
            return {"ok": True, "skipped": False, "cursor": page.coverage_through, "records": len(payments)}
