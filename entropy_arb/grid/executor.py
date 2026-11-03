"""AccountExecutor: the ONLY path from intents to venue calls (spec §7,
§3.1).

Discipline implemented here:

- Client ids are engine-assigned from a persistent per-account counter
  and bound to the intent BEFORE dispatch; adapters may not replace them.
- Every dispatch writes an append-only attempt row with the request hash
  first; a crash between write and send leaves UNKNOWN evidence, never a
  silent retry.
- Outcome handling follows the retry table (spec §7.2): rule rejections
  stop the intent; UNKNOWN keeps the budget and forces a lookup loop;
  post-only cross keeps the price demand; certified ABSENT_FINAL returns
  the intent to PLANNED for a fresh (contract-legal) resubmit.
- Serial per signer scope: one coroutine owns submissions for an account;
  cancels have priority in the runtime's drain order.
- The final send gate re-checks epoch + execution mode + fixture
  quarantine right before the network call (defense in depth).
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, Dict, List, Optional

from . import models, store
from .adapters.base import (AdapterError, CancelResult, GridVenue,
                            LookupResult, OrderRequest, SubmitResult)
from .journal import OrderJournal
from .store import Store

log = logging.getLogger("grid.executor")

# bounded lookup loop for UNKNOWN outcomes
LOOKUP_MAX_TRIES = 5
LOOKUP_BACKOFF_SEC = 0.05
VENUE_TIMEOUT_SEC = 10


class ExecutorError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.code = code
        self.message = message


class AccountExecutor:
    """One instance per (venue deployment, signer scope). Serial: callers
    await submit/cancel; internal dispatch is a single asyncio queue so
    request order is deterministic."""

    def __init__(self, *, st: Store, venue: GridVenue,
                 journal: OrderJournal, account: models.AccountIdentity,
                 execution_mode: str) -> None:
        self.store = st
        self.venue = venue
        self.journal = journal
        self.account = account
        self.execution_mode = execution_mode  # simulation | production
        self._lock = asyncio.Lock()
        self.risk_prepare = self.risk_fence = self.risk_refuse = self.risk_finished = None
        self.trial_authority = lambda: None
        self.live_authority = lambda: None
        self._bind_dispatch_gate(venue)

    def _authorities(self):
        return [a for a in (self.trial_authority(), self.live_authority()) if a is not None]

    def _bind_dispatch_gate(self, venue):
        if hasattr(venue, "dispatch_gate"):
            async def gate(order):
                current = await self._intent_with_market(order.intent_id)
                if (current and current.get('whole_position') and current['cause_key'].startswith(
                        f"exit-flat:{current['grid_id']}:native-residual:")
                        and not await self._small_close_scope(current, venue)):
                    return 'HL_SMALL_CLOSE_UNVERIFIED'
                return self._send_gate(current, check_risk=True) if current else "INTENT_NOT_FOUND"
            venue.dispatch_gate = gate
            async def trial_submit(order):
                current = await self._intent_with_market(order.intent_id)
                return bool(current and any(a.permits_intent(current) for a in self._authorities()))
            async def trial_cancel(client_id):
                row = self.store.read(lambda c: c.execute('SELECT i.intent_id FROM exchange_orders o JOIN order_intents i ON i.intent_id=o.intent_id '
                    'JOIN grids g ON g.grid_id=i.grid_id WHERE o.account=? AND o.client_id=? AND g.market_key=?',
                    (self.account.account_id, client_id, venue.market.market_key)).fetchone())
                current = await self._intent_with_market(row[0]) if row else None
                return bool(current and any(a.permits_grid(current['grid_id'], closing=True) for a in self._authorities()))
            async def trial_control(request_id):
                parts = request_id.split(':')
                return bool(len(parts) == 3 and parts[0] == 'leverage' and any(a.permits_grid(parts[1]) for a in self._authorities()))
            venue.trial_submit_gate, venue.trial_cancel_gate, venue.trial_control_gate = trial_submit, trial_cancel, trial_control
            async def small_close(order):
                current = await self._intent_with_market(order.intent_id)
                if (not current or not any(a.permits_small_close(current['grid_id']) for a in self._authorities())
                        or not current['cause_key'].startswith('exit-flat:') or not order.reduce_only
                        or order.whole_position and not current['cause_key'].startswith(
                            f"exit-flat:{current['grid_id']}:native-residual:")
                        or order.policy != 'reduce_only_ioc' or self._send_gate(current)
                        or any(getattr(order, key) != current[key] for key in ('client_id','side','qty_base','price','epoch'))):
                    return False
                return await self._small_close_scope(current, venue)
            venue.small_close_gate = small_close

    async def _small_close_scope(self, current, venue):
        """Exact owned full-position evidence for private residual cleanup."""
        from .inventory import Inventory
        if not any(a.permits_small_close(current['grid_id']) for a in self._authorities()) or self._send_gate(current):
            return False
        owned = self.store.read(lambda c: Inventory(self.store, current['grid_id']).net_position(c))
        signed = Decimal(current['qty_base']) * (-1 if current['side'] == 'sell' else 1)
        if owned == 0 or owned != -signed:
            return False
        try:
            snapshot = await venue.account_snapshot()
            age = venue.clock_ms() - int(models.parse_iso_ts(snapshot.ts, 'small_close_ts').timestamp()*1000)
            from .hl_unified import supported_margin_evidence
            return (snapshot.account == self.account.account_id and snapshot.position_qty_base is not None
                    and Decimal(snapshot.position_qty_base) == owned and -1000 <= age < 5000
                    and snapshot.evidence and supported_margin_evidence(snapshot.evidence)
                    and self._owned_account_positions(snapshot.evidence.get('positions', []), venue)
                    and self.store.read(lambda c: Inventory(self.store, current['grid_id']).net_position(c)) == owned
                    and not self._send_gate(await self._intent_with_market(current['intent_id'])))
        except Exception:
            return False

    def _owned_account_positions(self, positions, venue):
        """Sibling markets are allowed only with exact owned inventory.

        Re-check the entire ledger after the awaited native snapshot. A
        foreign position or an unsupported market/credential scope fails
        closed, while another owned grid does not strand a small close.
        """
        from .inventory import Inventory
        observed = {}
        for p in positions:
            if p['coin'] in observed:
                return False
            qty = Decimal(p['szi'])
            if qty and p.get('leverage', {}).get('type') != 'cross':
                return False
            observed[p['coin']] = qty
        def read(c):
            expected = {}
            for grid in c.execute('SELECT grid_id,market_key FROM grids WHERE account_id=?', (self.account.account_id,)):
                qty = Inventory(self.store, grid['grid_id']).net_position(c)
                if not qty:
                    continue
                parts = grid['market_key'].split('|')
                if (len(parts)!=5 or parts[0]!=venue.deployment or parts[1]!='' or parts[3:]!=['linear_perp','USDC']
                        or not any(a.permits_grid(grid['grid_id'], closing=True) for a in self._authorities())):
                    return False
                expected[parts[2]] = expected.get(parts[2], Decimal(0)) + qty
            return {k:v for k,v in observed.items() if v} == {k:v for k,v in expected.items() if v}
        return self.store.read(read)

    def market_venue(self, market):
        venue = self.venue.market_venue(market) if hasattr(self.venue, "market_venue") else self.venue
        self._bind_dispatch_gate(venue)
        return venue

    # ----------------------------------------------------------- client id
    async def _client_id(self, intent_id: str) -> str:
        """Persistent client id binding (spec §7.1): the engine's counter
        drives BOTH shapes — plain numeric for narrow-integer venues
        (declared ``client_id_kind = "numeric"``), prefixed otherwise.
        The binding is stored on the intent and reused across retries;
        adapters never rewrite ids themselves."""
        def allocate(c):
            n = store.bump_counter(
                c, f"client_id:{self.account.account_id}")
            ceiling = getattr(self.venue, 'client_id_max', None)
            if ceiling is not None and (type(ceiling) is not int or ceiling < 1 or n > ceiling):
                # Counter and binding remain unchanged on exhaustion. Never
                # truncate/hash/wrap a narrow native identifier or reuse one.
                raise ExecutorError('CLIENT_ID_EXHAUSTED', 'native client id space exhausted')
            if getattr(self.venue, "client_id_kind", "string") == "numeric":
                width = getattr(self.venue, 'client_id_width', 12)
                if type(width) is not int or not 0 <= width <= 32:
                    raise ExecutorError('CLIENT_ID_FORMAT_INVALID', 'invalid native client id format')
                return f"{n:0{width}d}"
            if getattr(self.venue, "client_id_kind", "string") == "hex128":
                import hashlib
                row = c.execute("SELECT value FROM meta WHERE key='runtime_id'").fetchone()
                namespace = hashlib.sha256(f"{self.account.account_id}:{row[0] if row else 'standalone'}".encode()).hexdigest()[:16]
                if n >= 2**64:
                    raise ExecutorError("CLIENT_ID_EXHAUSTED", "client id counter exhausted")
                return f"0x{namespace}{n:016x}"
            return f"g{n:012d}"
        def bind(c):
            row = c.execute('SELECT i.client_id,g.account_id FROM order_intents i '
                'JOIN grids g ON g.grid_id=i.grid_id WHERE i.intent_id=?', (intent_id,)).fetchone()
            if row is None:
                raise ExecutorError('INTENT_NOT_FOUND', intent_id)
            if row['account_id'] != self.account.account_id:
                raise ExecutorError('CLIENT_ID_SCOPE_MISMATCH', 'intent belongs to another account')
            cid = row['client_id'] or allocate(c)
            existing = c.execute('SELECT intent_id FROM exchange_orders WHERE account=? AND client_id=?',
                                 (self.account.account_id, cid)).fetchone()
            if existing and existing['intent_id'] != intent_id:
                raise ExecutorError('CLIENT_ID_CONFLICT', 'native client id is already bound')
            c.execute('UPDATE order_intents SET client_id=? WHERE intent_id=?', (cid, intent_id))
            c.execute("INSERT INTO exchange_orders(account,client_id,exchange_order_id,intent_id,cumulative_qty,terminal_state,updated_ts) "
                "VALUES(?,?,NULL,?,'0',NULL,?) ON CONFLICT(account,client_id) DO NOTHING",
                (self.account.account_id, cid, intent_id, models.utc_now_iso()))
            return cid
        # Allocation, immutable intent binding and the exchange lookup index
        # commit together. Concurrent allocation or a crash cannot leave an
        # unindexed original ID or allocate two IDs for the same intent.
        return await self.store.write(bind)

    # -------------------------------------------------------------- gates
    def _send_gate(self, intent: Dict[str, Any], *, check_risk=False) -> Optional[str]:
        """Final pre-send re-check. Returns a refusal reason or None."""
        if self.account.fixture and self.execution_mode != "simulation":
            return "FIXTURE_IN_PRODUCTION"
        if self.execution_mode not in ("simulation", "production"):
            return "BAD_EXECUTION_MODE"
        trial = any(a.permits_intent(intent) for a in self._authorities())
        if (self.execution_mode=='production' and not trial
                and getattr(getattr(self.market_venue(intent.get('market')), 'account_parent', None), 'live_release', None) is not None):
            return 'REAL_DISPATCH_DISABLED' if os.getenv('GRID_REAL_DISPATCH','0')!='1' else 'CAPABILITY_UNVERIFIED'
        if self.execution_mode == "production" and os.getenv("GRID_REAL_DISPATCH", "0") != "1" and not trial:
            return "REAL_DISPATCH_DISABLED"
        if hasattr(self.venue, "market_venue") and not intent.get("market"):
            return "MARKET_SCOPE_REQUIRED"
        venue = self.market_venue(intent.get("market"))
        if self.execution_mode == "production" and not getattr(venue, "live_qualified", False) and not trial:
            return "CAPABILITY_UNVERIFIED"
        if intent["epoch"] != intent["grid_epoch"]:
            return "EXPIRED_EPOCH"
        closing = intent["cause_key"].startswith("exit-flat:") and bool(intent["reduce_only"])
        if self.execution_mode == "production" and not closing and hasattr(venue, "stream_gap"):
            if venue.stream_gap or not venue.stream_connected:
                return "STREAM_RECOVERY_REQUIRED"
        if closing:
            if intent["grid_status"] != "STOPPING":
                return "GRID_NOT_STOPPING"
        elif intent["desired_mode"] != "ACTIVE" or intent["grid_status"] not in ("STARTING", "RUNNING"):
            return "GRID_NOT_ACTIVE"
        if not intent['reduce_only']:
            # A fixed-stop terminal can appear after a recovery commit. Do not
            # add exposure until its execution obligation has been reconciled.
            try:
                pending=self.store.read(lambda c:self.journal.pending_account_fixed_terminals(
                    c,self.account.account_id))
            except Exception:
                return 'PROTECTION_CUMULATIVE_UNVERIFIED'
            if pending:
                return 'PROTECTION_CUMULATIVE_UNVERIFIED'
        if check_risk and self.risk_fence:
            return self.risk_fence(intent)
        return None

    # ------------------------------------------------------------- submit
    async def _defer_coverage_entry(self,intent_id, *, code='PROTECTION_SERVICE_PENDING'):
        """Known not sent: preserve demand, unless a newer operation owns it."""
        def write(c):
            row=c.execute('SELECT i.state,i.epoch,g.epoch current_epoch,g.status,g.desired_mode '
                'FROM order_intents i JOIN grids g ON g.grid_id=i.grid_id WHERE i.intent_id=?',(intent_id,)).fetchone()
            if not row or row['state']!='DISPATCHING':return False
            current=(row['epoch']==row['current_epoch'] and row['desired_mode']=='ACTIVE'
                     and row['status'] in ('STARTING','RUNNING'))
            c.execute('UPDATE order_intents SET state=? WHERE intent_id=?',
                ('PLANNED' if current else 'EXPIRED_EPOCH',intent_id))
            return current
        deferred=await self.store.write(write)
        return {'intent_id':intent_id,'outcome':'DEFERRED' if deferred else 'BLOCKED',
            'reject_code':code if deferred else 'EXPIRED_EPOCH','known_not_sent':True}

    async def _intent_with_market(self, intent_id: str) \
            -> Optional[Dict[str, Any]]:
        def q(c):
            row = c.execute(
                "SELECT i.*, g.market_key AS market, g.epoch AS grid_epoch, "
                "g.status AS grid_status, g.desired_mode FROM order_intents i "
                "JOIN grids g ON g.grid_id = i.grid_id "
                "WHERE i.intent_id = ?", (intent_id,)).fetchone()
            return self.journal.decorate(c, row) if row else None
        return self.store.read(q)

    async def submit_intent(self, intent_id: str, *, epoch: int) \
            -> Dict[str, Any]:
        """Dispatch one PLANNED intent through the full journal path."""
        loop=asyncio.get_running_loop()
        progress={'phase':'read_capacity','started':loop.time(),'native_stop':False}
        try:
            # A budget wait must not hold the shared signer lock: a small
            # protection submission or cancellation may already have capacity.
            async with self._submission_reads(intent_id, progress=progress):
                progress.update(phase='account_lock',started=loop.time())
                async with self._lock:
                    progress.update(phase='submission',started=loop.time())
                    result = await self._submit_scoped(intent_id, epoch=epoch)
            # Cancellation needs the same account lock; release it first.
            # The freeze checks the intent's epoch before changing any state.
            if result.get("risk_blocked") and self.risk_refuse:
                intent = await self._intent_with_market(intent_id)
                await self.risk_refuse(intent, result["reject_code"])
            return result
        except (asyncio.CancelledError,TimeoutError) as error:
            if progress['native_stop']:
                # Static stage and exception type only: no account, client
                # id, request body, peer message or credential data.
                log.warning('GRID_NATIVE_STOP_PREPARATION_INTERRUPTED phase=%s elapsed_ms=%s exception_type=%s',
                    progress['phase'],int(max(0,loop.time()-progress['started'])*1000),type(error).__name__)
            raise
        finally:
            if self.risk_finished:
                self.risk_finished(intent_id)

    @asynccontextmanager
    async def _submission_reads(self, intent_id, *, progress=None):
        intent = await self._intent_with_market(intent_id)
        if intent is None:
            raise ExecutorError('INTENT_NOT_FOUND', intent_id)
        if intent['state'] == 'PLANNED':
            venue = self.market_venue(intent['market'])
            transport = getattr(venue, 'transport', None)
            if hasattr(transport, 'read_sequence'):
                # A protective whole-position trigger cannot open exposure.
                # Its fresh delegation check costs 140 native read weight;
                # unlike an entry it collects no portfolio risk snapshot.
                protection_only = (venue.venue == 'hl' and intent.get('whole_position')
                    and intent['reduce_only'] and intent.get('trigger_price') is not None
                    and intent['cause_key'].startswith('protection:'+intent['grid_id']+':'))
                if progress is not None:progress['native_stop']=bool(protection_only)
                emergency_close = (intent['reduce_only']
                    and intent.get('cause_key','').startswith('exit-flat:'))
                if protection_only:
                    sequence = transport.read_sequence(160)
                elif venue.venue=='hl' and not emergency_close and hasattr(transport,'execution_read_sequence'):
                    sequence = transport.execution_read_sequence(350)
                elif venue.venue=='hl' and not emergency_close and hasattr(transport,'public_read_sequence'):
                    # Grid entries and ordinary grid exits must leave the
                    # critical allowance available for a native stop or an
                    # emergency close. Wait before taking the signer lock.
                    sequence = transport.public_read_sequence(350)
                else:
                    sequence = transport.read_sequence()
                async with sequence:
                    # _submit_scoped re-reads durable epoch/mode and intent
                    # after both the capacity wait and the signer-lock wait.
                    yield
                    return
        yield

    async def _submit_scoped(self, intent_id: str, *, epoch: int) \
            -> Dict[str, Any]:
        intent = await self._intent_with_market(intent_id)
        if intent is None:
            raise ExecutorError("INTENT_NOT_FOUND", intent_id)
        if intent["epoch"] != epoch:
            await self.journal.transition(intent_id, "EXPIRED_EPOCH")
            return {"intent_id": intent_id, "outcome": "EXPIRED_EPOCH"}
        state = intent["state"]
        if state in ("FILLED", "REJECTED", "CANCELED", "EXPIRED_EPOCH"):
            return {"intent_id": intent_id, "outcome": state}
        if state != "PLANNED":
            return {"intent_id": intent_id, "outcome": state}
        condition = intent.get('entry_price_condition')
        if condition and not condition['armed']:
            venue = self.market_venue(intent['market'])
            quote = await venue.market_snapshot()
            if hasattr(venue, 'clock_ms'):
                age = venue.clock_ms() - int(models.parse_iso_ts(quote.ts, 'entry_quote_ts').timestamp()*1000)
                if not -1000 <= age < 5000:
                    return {'intent_id': intent_id, 'outcome': 'DEFERRED', 'reject_code': 'ENTRY_PRICE_WAIT', 'known_not_sent': True}
            reached = quote.mark is not None and (Decimal(quote.mark) >= Decimal(intent['price'])
                if intent['side'] == 'buy' else Decimal(quote.mark) <= Decimal(intent['price']))
            if not reached:
                return {'intent_id': intent_id, 'outcome': 'DEFERRED', 'reject_code': 'ENTRY_PRICE_WAIT', 'known_not_sent': True}
            def arm(c):
                current = c.execute('SELECT i.state,i.epoch,g.epoch current_epoch,g.status,g.desired_mode FROM order_intents i '
                    'JOIN grids g ON g.grid_id=i.grid_id WHERE i.intent_id=?', (intent_id,)).fetchone()
                if current['state'] != 'PLANNED' or current['epoch'] != current['current_epoch'] or current['epoch'] != epoch \
                        or current['desired_mode'] != 'ACTIVE' or current['status'] not in ('STARTING','RUNNING'):
                    return False
                c.execute('UPDATE runtime_state SET value=? WHERE key=?',
                          ('{"armed":true}', 'entry-price-wait:' + intent_id))
                return True
            if not await self.store.write(arm):
                return {'intent_id': intent_id, 'outcome': 'BLOCKED', 'reject_code': 'GRID_NOT_ACTIVE'}
        if intent['policy'] == 'post_only':
            quote = await self.market_venue(intent['market']).market_snapshot()
            price = Decimal(intent['price'])
            cross = (quote.ask is None or price >= Decimal(quote.ask)) if intent['side'] == 'buy' else (
                quote.bid is None or price <= Decimal(quote.bid))
            if cross:
                await self.journal.record_attempt(intent_id, request_hash='post-only-price', result='NOT_SENT', error_code='POST_ONLY_WAIT')
                return {'intent_id': intent_id, 'outcome': 'DEFERRED', 'reject_code': 'POST_ONLY_WAIT', 'known_not_sent': True}
        client_id = await self._client_id(intent_id)
        intent = await self._intent_with_market(intent_id)
        refuse = self._send_gate(intent)
        if refuse:
            await self.journal.record_attempt(
                intent_id, request_hash="gate", result="NOT_SENT" if refuse == 'PROTECTION_CUMULATIVE_UNVERIFIED' else "REJECTED",
                error_code=refuse)
            if refuse == 'PROTECTION_CUMULATIVE_UNVERIFIED':
                # This is before DISPATCHING or any adapter I/O. Keep the
                # original PLANNED identity for a later reconciled attempt.
                current = await self._intent_with_market(intent_id)
                still_planned = bool(current and current['state'] == 'PLANNED'
                    and current['epoch'] == current['grid_epoch'] == epoch
                    and current['desired_mode'] == 'ACTIVE'
                    and current['grid_status'] in ('STARTING', 'RUNNING'))
                return {"intent_id": intent_id, "outcome": "DEFERRED" if still_planned else "BLOCKED",
                        "reject_code": refuse if still_planned else 'EXPIRED_EPOCH', "known_not_sent": True}
            if refuse == "EXPIRED_EPOCH":
                await self.journal.transition(intent_id, "EXPIRED_EPOCH")
            return {"intent_id": intent_id, "outcome": "BLOCKED",
                    "reject_code": refuse}
        if self.risk_prepare:
            refuse = await self.risk_prepare(intent)
            if refuse:
                await self.journal.record_attempt(intent_id, request_hash="risk-gate", result="NOT_SENT", error_code=refuse)
                # Collection-side timeouts/staleness carry no risk information:
                # the send never happened, the fence re-checks freshness on the
                # retry, and freezing a grid for machine load parks it safely
                # but pointlessly. Concrete unsafe observations still freeze.
                deferred = refuse in ('RISK_EVENTS_PENDING', 'RISK_API_BUDGET_WAIT', 'RISK_OBSERVATION_CHANGED_WAIT',
                                      'PROTECTION_SERVICE_PENDING', 'RISK_COLLECTION_TIMEOUT',
                                      'RISK_SNAPSHOT_EXPIRED', 'RISK_NATIVE_SNAPSHOT_STALE',
                                      'RISK_OBSERVATION_CHANGED')
                return {"intent_id": intent_id, "outcome": "DEFERRED" if deferred else "BLOCKED",
                    "reject_code": refuse, "risk_blocked": not deferred}
        await self.journal.transition(intent_id, "DISPATCHING",
                                      client_id=client_id)
        req = OrderRequest(
            intent_id=intent_id, client_id=client_id,
            account=self.account.account_id, market=intent["market"],
            side=intent["side"], qty_base=intent["qty_base"],
            price=intent["price"], policy=intent["policy"],
            reduce_only=bool(intent["reduce_only"]), epoch=intent["epoch"],
            trigger_price=intent.get('trigger_price'), whole_position=bool(intent.get('whole_position')))
        request_hash = models.sha256_hex(req.to_dict())
        await self.journal.record_attempt(intent_id, request_hash=request_hash,
                                          result="SENT")
        # No network write until the durable current mode has been checked
        # again after all awaited preparation writes. In-flight requests
        # remain DISPATCHING and are queried/canceled, never expired as unsent.
        current = await self._intent_with_market(intent_id)
        refuse = self._send_gate(current, check_risk=True)
        if refuse:
            if refuse in ('PROTECTION_SERVICE_PENDING', 'RISK_FUNDING_CHANGED_WAIT', 'PROTECTION_CUMULATIVE_UNVERIFIED',
                          'RISK_SNAPSHOT_EXPIRED', 'RISK_OBSERVATION_CHANGED', 'RISK_NATIVE_SNAPSHOT_STALE',
                          'RISK_COLLECTION_TIMEOUT', 'RISK_EVENTS_PENDING', 'RISK_API_BUDGET_WAIT'):
                # Collection-side refusals before I/O carry no risk information:
                # the intent returns to PLANNED and the caller retries with a
                # fresh observation. Concrete unsafe observations still freeze.
                await self.journal.record_attempt(intent_id,request_hash=request_hash,result='NOT_SENT',error_code=refuse)
                return await self._defer_coverage_entry(intent_id, code=refuse)
            await self.journal.transition(intent_id, "UNKNOWN")
            await self.journal.record_attempt(intent_id, request_hash=request_hash,
                                              result="NOT_SENT", error_code=refuse)
            await self.store.write(lambda c: c.execute(
                "UPDATE order_intents SET state='EXPIRED_EPOCH' WHERE intent_id=? "
                "AND state='UNKNOWN'", (intent_id,)))
            return {"intent_id": intent_id, "outcome": "BLOCKED", "reject_code": refuse,
                    "risk_blocked": refuse.startswith(("RISK_",'PROTECTION_'))}
        try:
            result = await asyncio.wait_for(self.market_venue(req.market).submit(req), VENUE_TIMEOUT_SEC)
        except (AdapterError, TimeoutError, OSError) as e:
            result = SubmitResult(state="UNKNOWN", stage="adapter_error",
                                  detail=repr(e))
        await self.journal.record_attempt(
            intent_id, request_hash=request_hash, result=result.state,
            error_code=result.reject_code,
            detail={"stage": result.stage, "detail": result.detail})
        if result.state == "ACCEPTED":
            await self.journal.transition(intent_id, "ACCEPTED",
                                          client_id=client_id,
                                          exchange_order_id=result.exchange_order_id)
            await self.journal.bind_exchange_order(
                account=self.account.account_id, client_id=client_id,
                intent_id=intent_id,
                exchange_order_id=result.exchange_order_id)
            return {"intent_id": intent_id, "outcome": "ACCEPTED",
                    "client_id": client_id}
        if result.state == "REJECTED":
            # Venue-specific transient waits may defer only after the adapter
            # verifies its original durable record proves no request I/O.
            # Unknown results and native rejections never enter this branch.
            retry_check=getattr(self.market_venue(req.market),'retryable_not_sent',None)
            atomic_defer=getattr(self.market_venue(req.market),'defer_not_sent',None)
            if retry_check is not None and retry_check(result,request=req):
                if atomic_defer is None:
                    return await self._defer_coverage_entry(intent_id,code=result.reject_code)
                deferred=await atomic_defer(result,request=req)
                if deferred is not None:
                    return {'intent_id':intent_id,'outcome':'DEFERRED' if deferred else 'BLOCKED',
                        'reject_code':result.reject_code if deferred else 'EXPIRED_EPOCH','known_not_sent':True}
            # A fixed stop positively refused before transport may be retired
            # only with an atomic venue tombstone that prevents later sends.
            retire=getattr(self.market_venue(req.market),'reject_fixed_not_sent',None)
            if retire is not None and await retire(result,request=req):
                return {'intent_id':intent_id,'outcome':'REJECTED','reject_code':result.reject_code,
                        'known_not_sent':True}
            if atomic_defer is not None and result.stage in ('preparation_not_sent','final_gate_not_sent'):
                await self.journal.transition(intent_id,'UNKNOWN')
                return {'intent_id':intent_id,'outcome':'UNKNOWN','reject_code':result.reject_code}
            waiting_fills = bool(getattr(self.venue, 'pending_account_events', False)
                                 or getattr(self.market_venue(req.market), '_events', ()))
            deferred_gate = result.reject_code in ('RISK_SNAPSHOT_EXPIRED', 'RISK_EVENTS_PENDING', 'RISK_API_BUDGET_WAIT', 'RISK_FUNDING_CHANGED_WAIT', 'POST_ONLY_WAIT', 'POST_ONLY_BOOK_WAIT', 'PROTECTION_SERVICE_PENDING', 'RISK_OBSERVATION_CHANGED') or (
                waiting_fills and result.reject_code == 'RISK_OBSERVATION_CHANGED')
            if result.stage == 'final_gate_not_sent' and deferred_gate:
                if result.reject_code in ('PROTECTION_SERVICE_PENDING', 'RISK_FUNDING_CHANGED_WAIT'):
                    return await self._defer_coverage_entry(intent_id, code=result.reject_code)
                # The adapter explicitly stopped BEFORE HTTP. Reuse the
                # still-unsent identity after draining events/fresh risk;
                # never apply this path to an uncertain/native rejection.
                changed = await self.store.write(lambda c: c.execute(
                    "UPDATE order_intents SET state='PLANNED' WHERE intent_id=? AND state='DISPATCHING' "
                    "AND epoch=(SELECT epoch FROM grids WHERE grid_id=order_intents.grid_id)", (intent_id,)).rowcount)
                return {'intent_id': intent_id, 'outcome': 'DEFERRED' if changed else 'BLOCKED',
                        'reject_code': result.reject_code, 'known_not_sent': True}
            await self.journal.transition(intent_id, "REJECTED")
            if result.reject_code == 'POST_ONLY_CROSS' and intent['policy'] == 'post_only':
                def retain_price_demand(c):
                    grid = c.execute('SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?', (intent['grid_id'],)).fetchone()
                    if grid['epoch'] != epoch or grid['desired_mode'] != 'ACTIVE' or grid['status'] not in ('STARTING', 'RUNNING'):
                        return False
                    _, deduped = store.register_event(c, event_key='post-only-retry:' + intent_id,
                        scope=intent['grid_id'], event_type='post_only_wait', payload={'rejected_intent_id': intent_id})
                    if deduped:
                        return True
                    prior = c.execute('SELECT * FROM refill_demands WHERE grid_id=? AND revision=? AND level_index=? AND side=?',
                        (intent['grid_id'], intent['revision'], intent['level_index'], intent['side'])).fetchone()
                    if prior and (prior['price'] != intent['price'] or prior['reduce_only'] != intent['reduce_only']):
                        raise StoreError('DATA_CONFLICT', 'post-only demand conflicts with existing slot')
                    qty = Decimal(intent['qty_base']) + (Decimal(prior['qty']) if prior else Decimal(0))
                    c.execute('INSERT INTO refill_demands VALUES(?,?,?,?,?,?,?) ON CONFLICT(grid_id,revision,level_index,side) '
                              'DO UPDATE SET qty=excluded.qty',
                        (intent['grid_id'], intent['revision'], intent['level_index'], intent['side'], intent['price'],
                         int(intent['reduce_only']), models.canonical_decimal_text(qty)))
                    return True
                if await self.store.write(retain_price_demand):
                    return {'intent_id': intent_id, 'outcome': 'DEFERRED', 'reject_code': 'POST_ONLY_WAIT',
                            'replacement_pending': True, 'rejected_client_id': client_id}
            return {"intent_id": intent_id, "outcome": "REJECTED",
                    "reject_code": result.reject_code,
                    "risk_blocked": bool(result.reject_code and result.reject_code.startswith(("RISK_",'PROTECTION_')))}
        # UNKNOWN: budget stays reserved; record and resolve by lookup
        await self.journal.transition(intent_id, "UNKNOWN")
        resolved = await self.resolve_unknown(intent_id, client_id=client_id)
        return {"intent_id": intent_id, "outcome": "UNKNOWN",
                "resolved": resolved}

    # ------------------------------------------------------------- lookup
    def bind_protection(self, intent, venue):
        if intent.get('whole_position') and intent.get('client_id'):
            if not hasattr(venue, 'register_position_stop'):
                raise AdapterError('POSITION_STOP_UNSUPPORTED')
            venue.register_position_stop(intent['client_id'], side=intent['side'],
                qty_bound=intent['qty_base'], price=intent['price'], trigger_price=intent['trigger_price'])

    async def lookup_intent(self, intent):
        scoped = await self._intent_with_market(intent['intent_id'])
        venue = self.market_venue(scoped['market'])
        self.bind_protection(scoped, venue)
        return await self._lookup_order(venue, scoped['client_id'])

    async def _lookup_order(self, venue, client_id):
        """Native order reconciliation shares execution's reserved read budget."""
        async def query():
            transport = getattr(venue, 'transport', None)
            if venue.venue == 'hl' and hasattr(transport, 'read_sequence'):
                async with transport.read_sequence(2):
                    return await venue.lookup(client_id)
            return await venue.lookup(client_id)
        return await asyncio.wait_for(query(), VENUE_TIMEOUT_SEC)

    async def resolve_unknown(self, intent_id: str, *,
                              client_id: Optional[str] = None) -> str:
        """Query-first UNKNOWN resolution (spec §7.2). Never blindly
        resends; certified ABSENT_FINAL returns the intent to PLANNED."""
        scoped = await self._intent_with_market(intent_id)
        if scoped is None:
            raise ExecutorError("INTENT_NOT_FOUND", intent_id)
        venue = self.market_venue(scoped["market"])
        self.bind_protection(scoped, venue)
        if client_id is None:
            row = self.journal.get_intent(intent_id)
            client_id = row["client_id"] if row else None
        # A bulk original whose journal row was never created provably never
        # entered the signed-send pipeline: no nonce, no payload, nothing to
        # query. The venue lookup below would only repeat
        # NOT_FOUND_TRANSIENT. Wait-free proof, no send authorization.
        provably_unsent = getattr(venue, 'provably_unsent', None)
        if callable(provably_unsent) and client_id and await provably_unsent(client_id):
            # UNKNOWN → PLANNED → EXPIRED_EPOCH (the transition table has no
            # direct UNKNOWN → EXPIRED_EPOCH edge; both hops are guarded and
            # the provable never-sent proof makes neither a resend).
            await self.journal.transition(intent_id, "PLANNED")
            await self.journal.transition(intent_id, "EXPIRED_EPOCH")
            return "EXPIRED_EPOCH"
        for _ in range(LOOKUP_MAX_TRIES):
            if client_id is None:
                break
            try:
                res: LookupResult = await self._lookup_order(venue, client_id)
            except (AdapterError, OSError, TimeoutError):
                res = LookupResult(state="UNKNOWN", detail="lookup error")
            if res.state == "FOUND_OPEN":
                await self.journal.transition(
                    intent_id, "PARTIAL" if Decimal(res.cumulative_qty or "0") > 0 else "OPEN",
                    exchange_order_id=res.exchange_order_id)
                await self.journal.bind_exchange_order(
                    account=self.account.account_id, client_id=client_id,
                    intent_id=intent_id,
                    exchange_order_id=res.exchange_order_id)
                return "OPEN"
            if res.state == "FOUND_TERMINAL":
                terminal = res.terminal_state
                if terminal not in ("FILLED", "CANCELED"):
                    continue
                await self.journal.mark_terminal(
                    account=self.account.account_id, client_id=client_id,
                    terminal=terminal)
                return terminal
            if res.state == "ABSENT_FINAL":
                # certified never-accepted: legal resubmit per contract
                await self.journal.transition(intent_id, "PLANNED")
                return "ABSENT_FINAL_PLANNED"
            await asyncio.sleep(LOOKUP_BACKOFF_SEC)
        await self.journal.record_attempt(
            intent_id, request_hash="lookup", result="STILL_UNKNOWN")
        return "STILL_UNKNOWN"

    # ------------------------------------------------------------- cancel
    async def cancel_intents(self, intent_ids):
        """Batch only confirmed ordinary orders of one owned market/epoch.

        Uncertain prior sends remain on the original query-first path. Each
        member is marked before the one native send and proved separately.
        Cancellation/crash leaves CANCEL_REQUESTED for lookup, never a blind
        replay. Newer epochs and retained native stops cannot enter a batch.
        """
        ids = list(dict.fromkeys(intent_ids))
        intents = [await self._intent_with_market(iid) for iid in ids]
        ordinary = [i for i in intents if i and not i.get('whole_position')
                    and i['state'] in ('ACCEPTED', 'OPEN', 'PARTIAL') and i.get('client_id')]
        groups = {}
        for i in ordinary:
            venue = self.market_venue(i['market'])
            if (getattr(venue, 'venue', None) == 'hl' and hasattr(venue, 'cancel_batch')
                    and getattr(venue, 'write_enabled', False)
                    and hasattr(getattr(venue, 'transport', None), 'read_sequence')):
                groups.setdefault((i['grid_id'], i['market'], i['epoch']), []).append(i)
        results = {}
        for group in groups.values():
            for offset in range(0, len(group), 20):
                chunk = group[offset:offset + 20]
                if len(chunk) < 2:
                    continue
                venue = self.market_venue(chunk[0]['market'])
                transport = getattr(venue, 'transport', None)
                # One fresh named-agent check (140) plus 2 per order query.
                # Existing critical ceiling/cooldown still applies.
                async with transport.read_sequence(140 + 2 * len(chunk)):
                    async with self._lock:
                        current = [await self._intent_with_market(i['intent_id']) for i in chunk]
                        eligible = [i for i in current if i and not i.get('whole_position')
                                    and i['state'] in ('ACCEPTED', 'OPEN', 'PARTIAL')
                                    and (i['grid_id'], i['market'], i['epoch']) == (
                                        chunk[0]['grid_id'], chunk[0]['market'], chunk[0]['epoch'])]
                        bound = {}
                        for i in eligible:
                            row = self.store.read(lambda c, iid=i['intent_id']: c.execute(
                                'SELECT exchange_order_id,client_id FROM exchange_orders WHERE intent_id=? AND account=?',
                                (iid, self.account.account_id)).fetchone())
                            if row and row[0] and row[1] == i['client_id']:
                                bound[i['intent_id']] = row[0]
                        eligible = [i for i in eligible if i['intent_id'] in bound]
                        if len(eligible) < 2:
                            continue
                        for i in eligible:
                            await self.journal.transition(i['intent_id'], 'CANCEL_REQUESTED', client_id=i['client_id'])
                        try:
                            native = await asyncio.wait_for(venue.cancel_batch([
                                (i['client_id'], bound[i['intent_id']])
                                for i in eligible]), VENUE_TIMEOUT_SEC)
                        except (AdapterError, OSError, TimeoutError):
                            native = {}
                        for i in eligible:
                            proof = native.get(i['client_id'])
                            if proof and proof.state == 'TERMINAL' and proof.final_state in ('FILLED', 'CANCELED'):
                                await self.journal.mark_terminal(account=self.account.account_id,
                                    client_id=i['client_id'], terminal=proof.final_state)
                                results[i['intent_id']] = {'intent_id': i['intent_id'], 'outcome': proof.final_state}
                            else:
                                await self.journal.transition(i['intent_id'], 'CANCEL_PENDING')
                                results[i['intent_id']] = {'intent_id': i['intent_id'], 'outcome': 'CANCEL_PENDING'}
        for iid in ids:
            if iid not in results:
                results[iid] = await self.cancel_intent(iid)
        return list(results.values())

    async def cancel_intent(self, intent_id: str) -> Dict[str, Any]:
        intent = await self._intent_with_market(intent_id)
        if intent and intent['state'] not in ('FILLED', 'REJECTED', 'CANCELED', 'EXPIRED_EPOCH', 'PLANNED', 'BLOCKED_SLOT'):
            transport = getattr(self.market_venue(intent['market']), 'transport', None)
            if hasattr(transport, 'read_sequence'):
                async with transport.read_sequence(200):
                    return await self._cancel_locked(intent_id)
        return await self._cancel_locked(intent_id)

    async def _cancel_locked(self, intent_id: str) -> Dict[str, Any]:
        async with self._lock:
            intent = self.journal.get_intent(intent_id)
            if intent is None:
                raise ExecutorError("INTENT_NOT_FOUND", intent_id)
            state = intent["state"]
            if state in ("FILLED", "REJECTED", "CANCELED", "EXPIRED_EPOCH"):
                return {"intent_id": intent_id, "outcome": state}
            if state in ("PLANNED", "BLOCKED_SLOT"):
                # never confirmed sent: epoch/stop invalidation applies
                await self.journal.transition(intent_id, "EXPIRED_EPOCH")
                return {"intent_id": intent_id, "outcome": "EXPIRED_EPOCH"}
            if state == "DISPATCHING":
                await self.journal.transition(intent_id, "UNKNOWN")
                state = "UNKNOWN"
            if state in ("UNKNOWN", "CANCEL_PENDING", "CANCEL_REQUESTED"):
                resolved = await self.resolve_unknown(intent_id)
                if resolved not in ("OPEN", "PARTIAL"):
                    if resolved == "ABSENT_FINAL_PLANNED":
                        await self.journal.transition(intent_id, "EXPIRED_EPOCH")
                        resolved = "EXPIRED_EPOCH"
                    return {"intent_id": intent_id, "outcome": resolved}
            client_id = intent["client_id"]
            await self.journal.transition(intent_id, "CANCEL_REQUESTED",
                                          client_id=client_id)
            try:
                scoped = await self._intent_with_market(intent_id)
                self.bind_protection(scoped, self.market_venue(scoped['market']))
                cres: CancelResult = await asyncio.wait_for(self.market_venue(scoped["market"]).cancel(client_id), VENUE_TIMEOUT_SEC)
            except (AdapterError, OSError, TimeoutError):
                cres = CancelResult(state="UNKNOWN", detail="adapter error")
            if cres.state == "TERMINAL":
                final = cres.final_state or "CANCELED"
                await self.journal.mark_terminal(
                    account=self.account.account_id, client_id=client_id,
                    terminal=final)
                return {"intent_id": intent_id, "outcome": final}
            await self.journal.transition(intent_id, "CANCEL_PENDING")
            # resolve by lookup (cancel lost in flight)
            resolved = await self.resolve_unknown(intent_id)
            return {"intent_id": intent_id, "outcome": resolved}
