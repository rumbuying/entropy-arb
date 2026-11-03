"""Recovery barrier (G09, spec §9.3): one flow for startup, private
stream loss, and restart gaps.

Steps, in order; any failure stops risk resumption:

1.  stop new risk (caller does: epoch already bumped / actor detached)
2.  drain pending private events into durable storage (idempotent keys)
3.  backfill fills from the adapter's history FROM THE LAST COVERED
    cursor — pages must be complete; a truncated page leaves a gap and
    blocks resumption
4.  resolve every UNKNOWN intent via lookup (no blind resend)
5.  reconcile open orders: every venue order must map to an owned intent
    (foreign order => drift => NEEDS_ATTENTION)
6.  position check: venue position vs inventory-ledger net
Only when all pass does the caller restore desired_mode.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

from . import models
from .actor import GridActor
from .adapters.base import GridVenue
from .executor import AccountExecutor
from .inventory import Inventory
from .journal import OrderJournal
from .store import Store

log = logging.getLogger("grid.recovery")
RECOVERY_READ_TIMEOUT_SEC = 90


def audit_rebuild_error(error):
    """Persist error symbols without exception messages or transport bodies."""
    trusted = {
        ('entropy_arb.grid.runtime', 'RuntimeError_'),
        ('entropy_arb.grid.models', 'GridModelError'),
        ('entropy_arb.grid.store', 'StoreError'),
        ('entropy_arb.grid.lifecycle', 'LifecycleError'),
        ('entropy_arb.grid.journal', 'JournalError'),
    }
    native_codes = {'HL_MIN_NOTIONAL', 'HL_PRICE_PRECISION_INVALID', 'HL_SIZE_INVALID',
                    'HL_RULES_NOT_LOADED', 'HL_TRANSPORT_UNCERTAIN', 'HL_HTTP_429',
                    'HL_HTTP_502', 'HL_HTTP_503', 'HL_HTTP_504'}
    def symbol(exc):
        cls = type(exc)
        if (cls.__module__, cls.__name__) in trusted:
            code = getattr(exc, 'code', None) or getattr(exc, 'reason', None)
            if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{1,79}', code):
                return code
        if (cls.__module__, cls.__name__) == ('entropy_arb.grid.adapters.base', 'AdapterError'):
            if str(exc) in native_codes:
                return str(exc)
        if isinstance(exc, TimeoutError):
            return 'READ_TIMEOUT'
        return 'UNCLASSIFIED_REBUILD_ERROR'
    result = {'code': symbol(error)}
    if error.__cause__ is not None:
        result['cause_code'] = symbol(error.__cause__)
    return result


def audit_report(report):
    """Keep diagnostic symbols, never transport bodies or credential data."""
    def issues(rows):
        result = []
        for row in rows:
            code = row.get('code', '')
            item = {'code': code if re.fullmatch(r'[A-Z][A-Z0-9_]{1,79}', code)
                    else 'UNCLASSIFIED_RECOVERY_ISSUE'}
            detail = row.get('detail', '')
            match = re.fullmatch(r"(?:[A-Za-z]+Error\(['\"])?([A-Z][A-Z0-9_]{1,76})(?:['\"]\))?", detail)
            if match:
                item['detail_code'] = match.group(1)
            elif detail in ('TimeoutError()', 'TimeoutError'):
                item['detail_code'] = 'READ_TIMEOUT'
            result.append(item)
        return result
    refusal = report.get('scoped_basis_refusal')
    return {'ok': bool(report['ok']), 'gaps': issues(report['gaps']),
            'drift': issues(report['drift']),
            'backfilled': report.get('backfilled', 0),
            'unknown_resolved': report.get('unknown_resolved', 0),
            'scoped_basis': bool(report.get('scoped_basis')),
            'scoped_funding': bool(report.get('scoped_funding')),
            'scoped_basis_refusal': refusal if isinstance(refusal, str)
                and re.fullmatch(r'BULK_[A-Z0-9_]{1,80}', refusal) else None}


async def recovery_read(read):
    """Retry only idempotent reads, inside one shared time bound.

    A callable creates a fresh read for each attempt. Already-created
    awaitables retain the old one-shot contract; financial sends never enter
    this helper. A transport failure is not proof of missing history.
    """
    async def run():
        from .adapters.base import AdapterError
        from .stream_recovery import NETWORK_ERRORS
        for attempt in range(3):
            try:
                return await (read() if callable(read) else read)
            except (AdapterError, TimeoutError) as error:
                retry = isinstance(error, TimeoutError) or str(error) in NETWORK_ERRORS
                if not callable(read) or not retry or attempt == 2:
                    raise
                await asyncio.sleep(.5 * (2 ** attempt))
    return await asyncio.wait_for(run(), RECOVERY_READ_TIMEOUT_SEC)


class RecoveryBarrier:
    def __init__(self, *, st: Store, venue: GridVenue,
                 journal: OrderJournal, executor: AccountExecutor,
                 actor: GridActor) -> None:
        self.store = st
        self.venue = venue
        self.journal = journal
        self.executor = executor
        self.actor = actor

    async def run(self, *, position_only=False) -> Dict[str, Any]:
        report: Dict[str, Any] = {"gaps": [], "backfilled": 0,
                                  "unknown_resolved": 0, "drift": [],
                                  "ok": False}
        if position_only:
            row=self.store.read(lambda c:c.execute('SELECT desired_mode FROM grids WHERE grid_id=?',
                (self.actor.grid_id,)).fetchone())
            if not row or row[0]!='STOPPED':
                report['gaps'].append({'code':'EXIT_MODE_REQUIRED'})
                return report
        # -- 2/3. durable drain + history backfill with coverage proof
        cursor = self._last_fill_source_seq()
        try:
            page = await recovery_read(lambda: self.venue.fills(cursor=cursor))
        except Exception as e:
            report["gaps"].append({"code": "FILL_HISTORY_UNAVAILABLE", "detail": str(e)})
            return report
        if not page.complete or page.truncated:
            if not getattr(page,'scan_pending',False):
                await self._backfill_owned_positives(report)
            scoped = await self._scoped_recovery_basis(report)
            if scoped is None:
                report["gaps"].append(
                    {"code": "FILL_HISTORY_SCAN_PENDING" if getattr(page,'scan_pending',False) else "FILL_HISTORY_TRUNCATED" if page.truncated else "FILL_HISTORY_INCOMPLETE",
                     "detail": "venue history window too small to cover the "
                               "gap; refusing to resume"})
                return report
            # The account-scoped basis supersedes the v42 window backfill's
            # own transitional gap: it proves the same originals without a
            # history window and folds the economics bit-exactly.
            report["gaps"] = [g for g in report["gaps"]
                              if g.get("code") != "OWNED_FILL_RECONCILIATION_UNAVAILABLE"]
            report["scoped_basis"] = scoped.get("fingerprint")
        for ev in page.records:
            if ev.account != self.actor.account.account_id or ev.market != self.actor.market_key:
                continue
            owner = self.store.read(lambda c: c.execute(
                "SELECT i.grid_id FROM exchange_orders o JOIN order_intents i ON i.intent_id=o.intent_id "
                "WHERE o.account=? AND (o.client_id=? OR o.exchange_order_id=?)",
                (ev.account, ev.order_client_id, ev.exchange_order_id)).fetchone())
            if owner and owner[0] != self.actor.grid_id:
                continue
            try:
                res = await self.actor.apply_fill(ev)
            except Exception as e:
                report["drift"].append({"code": "UNOWNED_OR_INVALID_FILL", "detail": str(e)})
                continue
            if res["outcome"] == "applied":
                report["backfilled"] += 1
        # -- 4. UNKNOWN intents
        unknowns = self.journal.intents_for_grid(self.actor.grid_id,
                                                 states=["UNKNOWN", "DISPATCHING"])
        for it in unknowns:
            if it["state"] == "DISPATCHING":
                await self.journal.transition(it["intent_id"], "UNKNOWN")
            resolved = await self.executor.resolve_unknown(it["intent_id"], client_id=it["client_id"])
            if resolved == "STILL_UNKNOWN":
                report["gaps"].append({"code": "ORDER_STILL_UNKNOWN", "intent_id": it["intent_id"]})
            else:
                report["unknown_resolved"] += 1
        # -- 5. open orders vs owned intents
        owned: Dict[str, dict] = {}
        for it in self.journal.intents_for_grid(
                self.actor.grid_id,
                states=["OPEN", "PARTIAL", "ACCEPTED", "CANCEL_REQUESTED",
                        "CANCEL_PENDING"]):
            if it["client_id"]:
                owned[it["client_id"]] = it
        try:
            open_orders = await recovery_read(self.venue.open_orders())
        except Exception as e:  # adapter failure = coverage unknown
            report["gaps"].append({"code": "OPEN_ORDERS_UNAVAILABLE",
                                   "detail": repr(e)})
            return report
        for o in open_orders:
            if o.client_id not in owned:
                known = self.store.read(lambda c: c.execute(
                    "SELECT i.grid_id FROM exchange_orders e JOIN order_intents i ON i.intent_id=e.intent_id "
                    "WHERE e.account=? AND e.client_id=?", (self.actor.account.account_id, o.client_id)).fetchone())
                if known and known[0] != self.actor.grid_id:
                    continue
                report["drift"].append(
                    {"code": "FOREIGN_OPEN_ORDER",
                     "client_id": o.client_id,
                     "exchange_order_id": o.exchange_order_id,
                     "side": o.side, "qty_base": o.qty_base,
                     "price": o.price})
        # Reconcile both directions: absent owned orders cannot silently
        # survive as OPEN; exchange cumulative must equal booked executions.
        for cid, it in owned.items():
            try:
                result = await self.executor.lookup_intent(it)
            except Exception as e:
                report["gaps"].append({"code": "ORDER_LOOKUP_UNAVAILABLE", "detail": str(e)})
                continue
            if result.state not in ("FOUND_OPEN", "FOUND_TERMINAL"):
                report["gaps"].append({"code": "OWNED_ORDER_UNRESOLVED", "client_id": cid})
                continue
            booked = self.store.read(lambda c: c.execute(
                "SELECT cumulative_qty FROM exchange_orders WHERE account=? AND client_id=?",
                (self.actor.account.account_id, cid)).fetchone()[0])
            if not result.cumulative_known:
                if self._parked_fixed_protection(it,result,booked,open_orders):
                    pass  # Positive unchanged conditional book; no claim about future triggered executions.
                elif not it.get('whole_position') or Decimal(booked) > Decimal(it['qty_base']):
                    report['gaps'].append({'code': 'PROTECTION_CUMULATIVE_UNVERIFIED', 'client_id': cid})
                elif result.terminal_state == 'FILLED' and Decimal(booked) == 0:
                    report['gaps'].append({'code': 'PROTECTION_FILL_HISTORY_MISSING', 'client_id': cid})
            elif Decimal(booked) != Decimal(result.cumulative_qty or "0"):
                report["gaps"].append({"code": "FILL_CUMULATIVE_MISMATCH", "client_id": cid})
            if result.state == "FOUND_TERMINAL":
                if result.terminal_state in ("FILLED", "CANCELED"):
                    await self.journal.mark_terminal(account=self.actor.account.account_id,
                                                     client_id=cid, terminal=result.terminal_state)
                else:
                    report["gaps"].append({"code": "TERMINAL_STATE_UNKNOWN", "client_id": cid})
        # A fixed stop can be terminal while its triggered child executions
        # remain unknown.  Revisit durable obligations even after the intent
        # has left the active-state scan, including across process restarts.
        obligations = self.store.read(lambda c: self.journal.pending_fixed_terminals(
            c,self.actor.grid_id,self.actor.account.account_id))
        for obligation in obligations:
            intent = self.journal.get_intent(obligation['intent_id'])
            try:
                proof = await self.executor.lookup_intent(intent)
                booked = self.store.read(lambda c: c.execute(
                    "SELECT cumulative_qty FROM exchange_orders WHERE intent_id=? AND account=?",
                    (obligation['intent_id'], self.actor.account.account_id)).fetchone()[0])
                verified = (intent.get('protection_kind') == 'fixed_quantity'
                    and proof.state == 'FOUND_TERMINAL'
                    and proof.cumulative_known is True
                    and proof.terminal_state in ('FILLED', 'CANCELED')
                    and proof.terminal_state == obligation['terminal_state']
                    and proof.exchange_order_id == obligation['exchange_order_id']
                    and proof.exchange_order_id is not None
                    and Decimal(proof.cumulative_qty) == Decimal(booked)
                    and Decimal(0) <= Decimal(booked) <= Decimal(intent['qty_base']))
                if (not verified and report.get('scoped_basis') is not None
                        and proof.state == 'FOUND_TERMINAL'
                        and proof.terminal_state == obligation['terminal_state']
                        and proof.exchange_order_id == obligation['exchange_order_id']
                        and proof.exchange_order_id is not None
                        and Decimal(proof.cumulative_qty) == Decimal(booked)
                        and Decimal(0) <= Decimal(booked) <= Decimal(intent['qty_base'])
                        and Decimal(0) == Decimal(booked)):
                    # Every binding term matches; the only unprovable part is
                    # the native cumulative_known flag on the venue's zeroed-
                    # size cancel row. The account-scoped basis folds booked
                    # economics bit-exactly: an unbooked child execution of
                    # this stop would have broken the fold and refused the
                    # basis. The basis is the child-execution proof here.
                    verified = True
            except Exception:
                verified = False
            if verified:
                def clear(c):
                    current=c.execute('SELECT terminal_state,cumulative_qty,exchange_order_id FROM exchange_orders '
                        'WHERE intent_id=? AND account=?', (obligation['intent_id'],self.actor.account.account_id)).fetchone()
                    mark=c.execute('SELECT value FROM runtime_state WHERE key=?',
                        ('fixed-stop-terminal-unverified:'+obligation['intent_id'],)).fetchone()
                    if (current is None or current['terminal_state'] != obligation['terminal_state']
                            or current['exchange_order_id'] != obligation['exchange_order_id']
                            or Decimal(current['cumulative_qty']) != Decimal(booked)
                            or (mark[0] if mark else None) != obligation['marker']):
                        return False
                    fingerprint={k:current[k] for k in ('exchange_order_id','terminal_state','cumulative_qty')}
                    c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        ('fixed-stop-terminal-verified:'+obligation['intent_id'],json.dumps(fingerprint,sort_keys=True)))
                    c.execute('DELETE FROM runtime_state WHERE key=?',('fixed-stop-terminal-unverified:'+obligation['intent_id'],))
                    return True
                verified = await self.store.write(clear)
            if not verified:
                report['gaps'].append({'code':'PROTECTION_CUMULATIVE_UNVERIFIED', 'client_id':obligation['client_id']})
        # -- 6. position vs ledger
        try:
            if position_only:
                # Only an already stopped grid may use the exit path.
                # Active/paused recovery retains full collateral checks.
                from .hl_rate import critical_position_snapshot
                snap = await recovery_read(critical_position_snapshot(self.venue))
            else:
                snap = await recovery_read(self.venue.account_snapshot())
        except Exception as e:
            report["gaps"].append({"code": "ACCOUNT_SNAPSHOT_UNAVAILABLE",
                                   "detail": repr(e)})
            return report
        if snap.account != self.actor.account.account_id:
            report['drift'].append({'code':'ACCOUNT_SCOPE_MISMATCH'})
            return report
        if snap.position_qty_base is None:
            report["gaps"].append({"code": "POSITION_UNKNOWN"})
            return report
        venue_pos = Decimal(snap.position_qty_base)
        ledger_pos = self.store.read(
            lambda c: Inventory(self.store, self.actor.grid_id)
            .net_position(c))
        if venue_pos != ledger_pos:
            report["drift"].append(
                {"code": "POSITION_MISMATCH",
                 "venue": models.canonical_decimal_text(venue_pos),
                 "ledger": models.canonical_decimal_text(ledger_pos)})
        report["ok"] = not report["gaps"] and not report["drift"]
        if report["ok"]:
            await self.store.write(lambda c: c.execute(
                "INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value",
                (f"coverage:{self.actor.grid_id}", page.coverage_through or cursor or "")))
        return report

    async def _scoped_recovery_basis(self, report):
        """Account-owned-scope recovery proof for bulk (never global history).

        The account parent proves every post-baseline original, both position
        directions and the fee/funding counter folds. Any refusal leaves the
        global-history gap in the report; nothing here fakes completeness."""
        parent = getattr(self.venue, 'account_parent', None)
        capture = getattr(parent, 'capture_scoped_recovery_basis', None)
        if not callable(capture):
            return None
        from .adapters.base import AdapterError
        import re as _re
        try:
            return await recovery_read(capture())
        except AdapterError as error:
            code = str(error)
            report['scoped_basis_refusal'] = code if _re.fullmatch(
                r'BULK_[A-Z0-9_]{1,80}', code) else 'BULK_SCOPED_BASIS_REFUSED'
            return None
        except Exception:
            report['scoped_basis_refusal'] = 'BULK_SCOPED_BASIS_REFUSED'
            return None

    async def _backfill_owned_positives(self, report):
        reconcile = getattr(self.venue, 'reconcile_owned_fills', None)
        rank = getattr(self.venue, 'owned_execution_rank', None)
        if not callable(reconcile) or not callable(rank):
            return
        from .adapters.base import OrderRequest
        from .journal import is_native_protection
        from fractions import Fraction
        key = 'owned-fill-staging:' + self.actor.grid_id
        try:
            intents = self.journal.intents_for_grid(self.actor.grid_id)
            bound = self.store.read(lambda c: {r['intent_id']: dict(r) for r in c.execute(
                'SELECT e.* FROM exchange_orders e JOIN order_intents i ON i.intent_id=e.intent_id '
                'WHERE i.grid_id=? AND e.account=?', (self.actor.grid_id, self.actor.account.account_id))})
            candidates = sorted((it for it in intents if not is_native_protection(it)
                and it.get('client_id') and it['intent_id'] in bound
                and bound[it['intent_id']]['client_id'] == it['client_id']
                and bound[it['intent_id']]['exchange_order_id']), key=lambda it: it['intent_id'])
            if not candidates:
                return
            expected = {}
            for it in candidates:
                req = OrderRequest(it['intent_id'], it['client_id'], self.actor.account.account_id,
                    self.actor.market_key, it['side'], it['qty_base'], it['price'], it['policy'],
                    bool(it['reduce_only']), it['epoch'], trigger_price=it.get('trigger_price'),
                    whole_position=bool(it.get('whole_position')))
                expected[it['client_id']] = (bound[it['intent_id']]['exchange_order_id'],
                                             models.sha256_hex(req.to_dict()))
            saved = self.store.read(lambda c: c.execute('SELECT value FROM runtime_state WHERE key=?', (key,)).fetchone())
            saved = json.loads(saved[0]) if saved else {'orders': {}, 'records': {}}
            # Terminal native quantities are immutable. Reuse only certificates
            # still bound to exactly the same core request; never cache OPEN.
            orders = {cid: o for cid, o in saved['orders'].items() if cid in expected
                and (o['order_id'], o['request_hash']) == expected[cid]
                and o['state'] == 'FOUND_TERMINAL' and o['terminal_state'] in ('FILLED', 'CANCELED', 'REJECTED')}
            records = {k: v for k, v in saved['records'].items() if v['order_client_id'] in orders}
            control = self.store.read(lambda c: tuple(c.execute(
                'SELECT epoch,status,desired_mode,active_revision FROM grids WHERE grid_id=?',
                (self.actor.grid_id,)).fetchone()))
            def same_scope(c):
                current=c.execute('SELECT epoch,status,desired_mode,active_revision FROM grids WHERE grid_id=?',
                    (self.actor.grid_id,)).fetchone()
                if not current or tuple(current)!=control or current['status']!='RECOVERING':return False
                now={}
                for row in c.execute('SELECT i.*,e.exchange_order_id,e.client_id AS bound_cid FROM order_intents i '
                        'JOIN exchange_orders e ON e.intent_id=i.intent_id WHERE i.grid_id=? AND e.account=?',
                        (self.actor.grid_id,self.actor.account.account_id)):
                    it=OrderJournal.decorate(c,row)
                    if is_native_protection(it) or not it.get('client_id') or not it['exchange_order_id']:continue
                    if it['client_id']!=it['bound_cid']:return False
                    req=OrderRequest(it['intent_id'],it['client_id'],self.actor.account.account_id,self.actor.market_key,
                        it['side'],it['qty_base'],it['price'],it['policy'],bool(it['reduce_only']),it['epoch'],
                        trigger_price=it.get('trigger_price'),whole_position=bool(it.get('whole_position')))
                    now[it['client_id']]=(it['exchange_order_id'],models.sha256_hex(req.to_dict()))
                return now==expected
            unproven = [cid for cid in expected if cid not in orders]
            offset=saved.get('next_index',0)
            if type(offset) is not int or offset<0:raise ValueError('invalid owned scan offset')
            offset=offset%len(unproven) if unproven else 0
            selected=(unproven[offset:]+unproven[:offset])[:4]
            next_index=(offset+len(selected))%len(unproven) if unproven else 0
            observed_orders=dict(orders);observed_records=dict(records)
            def validate_proof(proof,ids):
                if (proof.account != self.actor.account.account_id or proof.market != self.actor.market_key
                        or proof.quantity_matched is not True or len(proof.original_orders)!=len(ids)
                        or {o['client_id']:(o['order_id'],o['request_hash']) for o in proof.original_orders}
                        != {cid:expected[cid] for cid in ids}):
                    raise ValueError('owned execution proof scope differs from core requests')
                if any(o['state'] not in ('FOUND_OPEN','FOUND_TERMINAL')
                        or o['state']=='FOUND_OPEN' and o['terminal_state'] is not None
                        or o['state']=='FOUND_TERMINAL' and o['terminal_state'] not in ('FILLED','CANCELED','REJECTED')
                        for o in proof.original_orders):
                    raise ValueError('positive original quantities required')
            if selected:
                proof = await recovery_read(reconcile(selected))
                validate_proof(proof,selected)
                if any(o['state']=='FOUND_OPEN' for o in proof.original_orders):
                    from .adapters.bulk_batch_lookup import MAX_ORIGINALS
                    if len(unproven)<=MAX_ORIGINALS and set(selected)!=set(unproven):
                        # A single fresh bracketing proof covers all mutable
                        # orders together. Old OPEN observations are never
                        # cached as immutable terminal certificates.
                        selected=unproven
                        proof=await recovery_read(reconcile(selected))
                        validate_proof(proof,selected)
                complete_snapshot=set(selected)==set(unproven)
                accepted={o['client_id']:o for o in proof.original_orders
                    if o['state']=='FOUND_TERMINAL' or complete_snapshot}
                for ev in proof.records:
                    if (ev.account != proof.account or ev.market != proof.market or ev.order_client_id not in selected
                            or ev.exchange_order_id != expected[ev.order_client_id][0]):
                        raise ValueError('owned execution record scope differs from original order')
                    rank(ev)
                    if ev.order_client_id not in accepted:continue
                    old = observed_records.get(ev.execution_key)
                    raw = ev.to_dict()
                    if old is not None and old | {'received_ts': None} != raw | {'received_ts': None}:
                        raise ValueError('staged execution changed')
                    observed_records.setdefault(ev.execution_key,raw)
                observed_orders.update(accepted)
                report['owned_open_orders_proved']=sum(o['state']=='FOUND_OPEN' for o in accepted.values())
            terminal_orders={cid:o for cid,o in observed_orders.items() if o['state']=='FOUND_TERMINAL'}
            terminal_records={k:v for k,v in observed_records.items() if v['order_client_id'] in terminal_orders}
            staged = {'orders':terminal_orders,'records':terminal_records,'next_index':next_index}
            def stage(c):
                if not same_scope(c):return False
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) '
                          'DO UPDATE SET value=excluded.value', (key, json.dumps(staged, sort_keys=True)))
                return True
            if not await self.store.write(stage):
                raise ValueError('recovery mode changed before staging')
            if len(observed_orders) != len(expected):
                report['owned_fill_scan_pending'] = True
                return  # No inventory changes until every original has a matched quantity.
            events = sorted((models.ExecutionEvent.from_dict(v) for v in observed_records.values()), key=rank)
            amounts = {cid: Fraction(0) for cid in observed_orders}
            for ev in events:
                if (ev.order_client_id not in expected or ev.exchange_order_id != expected[ev.order_client_id][0]
                        or ev.account != self.actor.account.account_id or ev.market != self.actor.market_key):
                    raise ValueError('staged execution scope changed')
                amounts[ev.order_client_id] += Fraction(ev.qty_base)
            if any(amounts[cid] != Fraction(o['cumulative_qty']) for cid, o in observed_orders.items()):
                raise ValueError('staged cumulative mismatch')
            existing = self.store.read(lambda c: [dict(r) for r in c.execute(
                'SELECT e.* FROM executions e JOIN execution_owners o ON o.execution_key=e.execution_key '
                'WHERE o.grid_id=?', (self.actor.grid_id,))])
            previous={row['execution_key']:models.ExecutionEvent.from_dict({k:v for k,v in row.items() if k!='event_seq'}) for row in existing}
            for ev in events:
                if ev.execution_key not in previous:continue
                def economics(event):
                    value=event.to_dict()
                    for key in ('received_ts','source'):value.pop(key,None)
                    for key in ('qty_base','price','fee_amount'):
                        value[key]=models.canonical_decimal_text(models.parse_decimal_text(value[key],key))
                    return value
                if economics(previous[ev.execution_key])!=economics(ev):
                    raise ValueError('durable execution economics changed')
            known = {e['execution_key'] for e in existing}
            highwater = max((rank(models.ExecutionEvent.from_dict({k: v for k, v in e.items() if k != 'event_seq'}))
                             for e in existing), default=None)
            if highwater is not None and any(ev.execution_key not in known and rank(ev) < highwater for ev in events):
                raise ValueError('older missing execution requires ordered ledger rebuild')
            for ev in events:
                result = await self.actor.apply_fill(ev, recovery_only=True)
                if result['outcome'] == 'applied':
                    report['backfilled'] += 1
            proof_id = models.sha256_hex(['owned-original-snapshot-replay-v1', self.actor.grid_id,observed_orders,
                {k:v|{'received_ts':None} for k,v in observed_records.items()}])
            def checked(c):
                if not same_scope(c):return False
                from . import store
                store.register_event(c, event_key='owned-fill-proof:' + proof_id,
                    scope=self.actor.grid_id, event_type='owned_fills_reconciled',
                    payload={'proof_id': proof_id, 'orders': len(expected),
                             'records': len(events), 'global_coverage_advanced': False})
                return True
            if not await self.store.write(checked):
                raise ValueError('recovery mode changed before inspection checkpoint')
            report['owned_fill_proof'] = proof_id
        except Exception:
            # Keep private transport bodies and arbitrary adapter messages out
            # of recovery reports. Global recovery is still unconfirmed.
            report['gaps'].append({'code': 'OWNED_FILL_RECONCILIATION_UNAVAILABLE'})

    @staticmethod
    def _parked_fixed_protection(intent,proof,booked,open_orders):
        """Both snapshots must positively identify the untouched fixed stop.

        Unknown cumulative remains unknown. This exception is only about an
        untriggered conditional, not a terminal root or its child executions.
        The normal complete-history and position barriers still apply.
        """
        if (intent.get('protection_kind')!='fixed_quantity' or intent.get('whole_position')
                or not intent['reduce_only'] or intent['side']!='sell' or intent['policy']!='gtc'
                or intent['level_index']!=-1 or proof.state!='FOUND_OPEN'
                or proof.conditional_parked is not True or proof.cumulative_known is not False
                or proof.terminal_state is not None or Decimal(booked)!=0 or Decimal(proof.cumulative_qty)!=0):
            return False
        matches=[o for o in open_orders if o.client_id==intent['client_id']
            and o.exchange_order_id==proof.exchange_order_id]
        if len(matches)!=1:return False
        order=matches[0]
        return (order.conditional_parked is True and order.cumulative_known is False
            and order.whole_position is False and order.reduce_only is True
            and order.source=='rest_query' and order.side==intent['side']
            and Decimal(order.qty_base)==Decimal(intent['qty_base']) and Decimal(order.cumulative_qty)==0
            and order.trigger_price==intent.get('trigger_price') and order.price==intent['price'])

    def _last_fill_source_seq(self) -> Optional[str]:
        """Adapter source_seq of the last APPLIED fill. The 'last event
        received' time is never used as a coverage boundary (spec §9.3)."""
        def q(c):
            row = c.execute("SELECT value FROM runtime_state WHERE key=?",
                            (f"coverage:{self.actor.grid_id}",)).fetchone()
            if row and row[0]:
                return row[0]
            start = c.execute("SELECT value FROM runtime_state WHERE key=?",
                              (f"history-start:{self.actor.grid_id}",)).fetchone()
            return start[0] if start else None
        return self.store.read(q)
