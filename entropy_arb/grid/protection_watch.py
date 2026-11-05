"""Small native protection reads and a local deadline independent of venue I/O."""
import asyncio
import json
import logging
import time
from decimal import Decimal

from . import models
from .adapters.base import AdapterError
from .inventory import Inventory

log = logging.getLogger('grid.protection_watch')


# A proof that expires while the account could not be read is pending, not a
# confirmed protection loss: the native stop keeps resting on the venue. The
# bulk backend records when continuous blindness began; the watch tolerates an
# expiry inside this window while it keeps trying to re-prove, and runs the
# exit contract once the window is exhausted (2026-11-04 13:21: a ~6-minute
# exchange read outage expired the proof and closed a RUNNING grid ~90s in).
PROTECTION_BLIND_KEY = 'protection-blind:'
PROTECTION_BLIND_TOLERANCE_MS = 180_000
# Watch-level blindness (the supervisor itself failed to evaluate a grid).
PROTECTION_WATCH_BLIND_KEY = 'protection-watch-blind:'
# Active stream-loss recovery (durable progress marker written by the
# recovery scheduler). A recovery legitimately runs longer than the proof
# window it interrupted — paced multi-market cancels and reconciliation —
# and exiting at the carried deadline bumps the epoch the recovery then can
# never satisfy (2026-11-05 10:57, g-2f8fd555: closed ~2 minutes into every
# stream-gap recovery). The tolerance is bounded per recovery episode and
# only applies while the scheduler keeps refreshing its marker; a stale
# marker (dead scheduler, interrupted process, given-up recovery) enforces
# the deadline unchanged, as does a grid the scheduler left in
# NEEDS_ATTENTION. The native stop keeps resting on the venue throughout.
RECOVERY_WATCH_KEY = 'recovery-watch:'
RECOVERY_WATCH_SCHEMA = 'recovery-watch-v1'
RECOVERY_PROGRESS_FRESH_MS = 120_000
PROTECTION_RECOVERY_TOLERANCE_MS = 600_000


def recovery_in_flight(store,gid,epoch,*,now_mono_ms):
    """Durable progress signal from the stream-recovery scheduler.

    The marker carries the recovery episode's epoch and two monotonic
    timestamps: onset (first scheduler observation of this episode) and
    updated (its latest tick). Callers share the scheduler's time.monotonic
    origin. Tolerance applies only while the marker names the current epoch,
    was refreshed recently, and the episode is still inside the bounded
    recovery tolerance — anything else reads as no active recovery."""
    raw=store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
        (RECOVERY_WATCH_KEY+gid,)).fetchone())
    if raw is None:return False
    try:marker=json.loads(raw[0])
    except (ValueError,TypeError):return False
    return (isinstance(marker,dict) and marker.get('schema')==RECOVERY_WATCH_SCHEMA
        and marker.get('grid_id')==gid and marker.get('epoch')==epoch
        and type(marker.get('onset_mono_ms')) is int and type(marker.get('updated_mono_ms')) is int
        and 0<=marker['onset_mono_ms']<=marker['updated_mono_ms']<=now_mono_ms+1000
        and now_mono_ms-marker['updated_mono_ms']<=RECOVERY_PROGRESS_FRESH_MS
        and now_mono_ms-marker['onset_mono_ms']<=PROTECTION_RECOVERY_TOLERANCE_MS)


class ProtectionLease:
    def __init__(self,runtime):self.rt=runtime

    def backend(self,gid,config=None):
        config=config or self.rt.config(gid)
        if config.account_ref not in self.rt.venues:return None
        venue=self.rt.venue_for(config.account_ref,config.market_ref)
        backend=self.rt.protection.backend(venue)
        return backend if callable(getattr(backend,'check_lease',None)) else None

    def fixed_protection(self,gid):
        """Persisted order kind still fences exposure before adapter/key loading."""
        def read(c):
            for row in c.execute("SELECT s.value FROM runtime_state s JOIN order_intents i "
                    "ON s.key='order-protection:'||i.intent_id WHERE i.grid_id=?",(gid,)):
                try:
                    if json.loads(row[0]).get('kind')=='fixed_quantity':return True
                except (ValueError,TypeError,AttributeError):return True
            return False
        return self.rt.store.read(read)

    def snapshot(self,gid):
        row=self.rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
            ('native-protection-lease:'+gid,)).fetchone())
        return json.loads(row[0]) if row else None

    @staticmethod
    def _mono_now():return time.monotonic_ns()//1_000_000

    @staticmethod
    def _wall_now():return int(time.time()*1000)

    def carried_fixed_proof(self,gid,*,mono_ms=None,now_ms=None):
        """DB-only restart decision for the bulk fixed native stop.

        A lease binds to the session that proved it, so a fresh process's
        check_lease refuses it by design — that fence protects SEND
        authorization and stays untouched. This read re-derives, without the
        session field and without any venue I/O, whether the fixed stop was
        independently verified resting moments before the process
        disappeared; if so a restart may resume the grid into bounded
        recovery (reconcile plus a fresh activation re-prove everything with
        new venue evidence before any send) instead of protective-closing it.
        Closing here made every deploy or watchdog restart protective-close
        every live bulk grid (2026-11-05: eight closes, one per restart;
        g-551c9890's stop had been verified resting 66s before the kill).

        The exact PROOF_MAX_SECONDS arithmetic is deliberately not enforced
        here: the deploy override is venue-specific, close_interrupted runs
        before it, and this decision authorizes no sends. A bounded sanity
        cap keeps a forged deadline from extending the resume window; the
        recovery tolerance bounds it regardless.
        """
        CARRIED_PROOF_SANITY_MS=600_000
        mono_ms=self._mono_now() if mono_ms is None else mono_ms
        now_ms=self._wall_now() if now_ms is None else now_ms
        grid=self.rt.lifecycle._grid(gid)
        # STARTING included: the lease itself proves the activation's fixed
        # stop landed and was verified OPEN (save() runs only with
        # independently queried OPEN proofs) — the activation was past its
        # protection step. Without a valid lease STARTING still exits below,
        # unchanged: an interrupted submit with no proof owns nothing here.
        if (grid['status'] not in ('STARTING','RUNNING','RECOVERING')
                or grid['desired_mode']!='ACTIVE'):return False
        if not self.required(gid):return False
        row=self.rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
            ('native-protection-lease:'+gid,)).fetchone())
        if not row:return False
        try:lease=json.loads(row[0])
        except (ValueError,TypeError):return False
        if (not isinstance(lease,dict) or lease.get('schema')!='bulk-fixed-native-v1'
                or lease.get('whole_position') is not False):return False
        try:
            config=self.rt.store.read(lambda c:self.rt.service_coverage.transaction_config(c,gid))
        except Exception:return False
        if (lease.get('epoch')!=grid['epoch'] or lease.get('config_hash')!=config.config_hash
                or lease.get('account')!=config.account_ref or lease.get('market')!=config.market_ref):
            return False
        stamps=(mono_ms,now_ms,lease.get('confirmed_from_ms'),lease.get('observed_ms'),lease.get('deadline_ms'),
            lease.get('confirmed_from_mono_ms'),lease.get('observed_mono_ms'),lease.get('deadline_mono_ms'))
        if any(type(x) is not int for x in stamps):return False
        if not lease['confirmed_from_ms']<=lease['observed_ms']<=now_ms<lease['deadline_ms']:return False
        if not lease['confirmed_from_mono_ms']<=lease['observed_mono_ms']<=mono_ms<lease['deadline_mono_ms']:return False
        if (lease['deadline_ms']-lease['confirmed_from_ms']>CARRIED_PROOF_SANITY_MS
                or lease['deadline_mono_ms']-lease['confirmed_from_mono_ms']>CARRIED_PROOF_SANITY_MS):return False
        def exposure(c):
            owned=Inventory(self.rt.store,gid).net_position(c)
            pending=c.execute("SELECT side,qty_base FROM order_intents WHERE grid_id=? AND reduce_only=0 "
                "AND state IN ('ACCEPTED','OPEN','PARTIAL','UNKNOWN','DISPATCHING','CANCEL_REQUESTED','CANCEL_PENDING')",
                (gid,)).fetchall()
            return owned,pending
        owned,pending=self.rt.store.read(exposure)
        try:
            bound=Decimal(owned)
            if bound<0:return False
            for order in pending:
                if order['side']!='buy':return False
                bound+=Decimal(order['qty_base'])
            if bound>Decimal(lease['capacity_qty']):return False
        except Exception:return False
        return True

    def save(self,c,gid,*,epoch,proofs,started_ms,now_ms):
        """Called only with independently queried OPEN proofs for every side."""
        config=self.rt.service_coverage.transaction_config(c,gid)
        backend=self.backend(gid,config)
        if backend is not None:
            raise AdapterError('PROTECTION_SERVICE_FIXED_PROOF_REQUIRED')
        policy=config.protection.service_coverage if config.protection else None
        if policy is None:return
        grid=c.execute('SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?',(gid,)).fetchone()
        if grid['epoch']!=epoch or grid['desired_mode']!='ACTIVE' or grid['status'] not in ('STARTING','RUNNING','RECOVERING','NEEDS_ATTENTION'):
            return
        if (type(started_ms) is not int or type(now_ms) is not int or started_ms>now_ms
                or now_ms>=started_ms+policy.max_seconds*1000):
            raise AdapterError('PROTECTION_SERVICE_NATIVE_PROOF_EXPIRED')
        required={s:t for s,t in (('sell',config.protection.lower_stop),('buy',config.protection.upper_stop)) if t is not None}
        owned=abs(Inventory(self.rt.store,gid).net_position(c));seen=set();stops={}
        for iid,proof in proofs:
            row=c.execute('SELECT i.*,o.exchange_order_id FROM order_intents i JOIN exchange_orders o '
                'ON o.intent_id=i.intent_id WHERE i.intent_id=? AND i.grid_id=?',(iid,gid)).fetchone()
            stop=self.rt.journal.decorate(c,row) if row else None
            if (not stop or not stop.get('whole_position') or not stop['reduce_only']
                    or stop['state'] not in ('ACCEPTED','OPEN','PARTIAL') or stop['epoch']>epoch
                    or stop['side'] in seen or stop['side'] not in required
                    or stop['trigger_price']!=required[stop['side']] or Decimal(stop['qty_base'])<owned
                    or proof.state!='FOUND_OPEN' or proof.native_status!='open' or proof.cumulative_known is not False
                    or proof.exchange_order_id!=stop['exchange_order_id']):
                raise AdapterError('PROTECTION_SERVICE_NATIVE_PROOF_UNVERIFIED')
            seen.add(stop['side']);stops[iid]=proof.exchange_order_id
        if not required or seen!=set(required):
            raise AdapterError('PROTECTION_SERVICE_NATIVE_PROOF_UNVERIFIED')
        record={'epoch':epoch,'config_hash':config.config_hash,'account':config.account_ref,
            'market':config.market_ref,'confirmed_from_ms':started_ms,'observed_ms':now_ms,
            'deadline_ms':started_ms+policy.max_seconds*1000,'stops':stops}
        c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            ('native-protection-lease:'+gid,json.dumps(record,sort_keys=True)))

    def carry_recovery_epoch(self,c,gid,*,previous_epoch,epoch,now_ms):
        """Fence old senders without inventing a new native observation.

        Only a still-valid proof for unchanged, owned protection can cross the
        stream-loss fence. All timestamps, including its deadline, stay fixed.
        The independent watcher must keep querying during RECOVERING.
        """
        config=self.rt.service_coverage.transaction_config(c,gid)
        backend=self.backend(gid,config)
        if backend is not None:
            return backend.carry_recovery_epoch(c,gid,previous_epoch=previous_epoch,epoch=epoch,now_ms=now_ms)
        policy=config.protection.service_coverage if config.protection else None
        if policy is None:return False
        grid=c.execute('SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?',(gid,)).fetchone()
        row=c.execute('SELECT value FROM runtime_state WHERE key=?',('native-protection-lease:'+gid,)).fetchone()
        if not row:return False
        lease=json.loads(row[0])
        if not isinstance(lease,dict) or not isinstance(lease.get('stops'),dict):return False
        if (grid['epoch']!=epoch or grid['status']!='RECOVERING' or grid['desired_mode']!='ACTIVE'
                or lease.get('epoch')!=previous_epoch or lease.get('config_hash')!=config.config_hash
                or lease.get('account')!=config.account_ref or lease.get('market')!=config.market_ref
                or any(type(x) is not int for x in (now_ms,lease.get('confirmed_from_ms'),lease.get('observed_ms'),lease.get('deadline_ms')))
                or not lease['confirmed_from_ms']<=lease['observed_ms']<=now_ms<lease['deadline_ms']
                or lease['deadline_ms']!=lease['confirmed_from_ms']+policy.max_seconds*1000):
            return False
        venue=self.rt.venue_for(config.account_ref,config.market_ref)
        specs={s['side']:s for s in self.rt.protection.specs(config,venue)}
        owned=abs(Inventory(self.rt.store,gid).net_position(c));seen=set()
        for iid,oid in lease.get('stops',{}).items():
            row=c.execute('SELECT i.*,o.exchange_order_id FROM order_intents i JOIN exchange_orders o '
                'ON o.intent_id=i.intent_id WHERE i.intent_id=? AND i.grid_id=?',(iid,gid)).fetchone()
            stop=self.rt.journal.decorate(c,row) if row else None
            if (not stop or not stop.get('whole_position') or not stop['reduce_only']
                    or stop['state'] not in ('ACCEPTED','OPEN','PARTIAL') or stop['epoch']>previous_epoch
                    or stop['side'] not in specs or stop['side'] in seen or Decimal(stop['qty_base'])<owned
                    or stop['exchange_order_id']!=oid or stop['trigger_price']!=specs[stop['side']]['trigger_price']
                    or stop['price']!=specs[stop['side']]['price']):
                return False
            seen.add(stop['side'])
        if not specs or seen!=set(specs):return False
        lease['epoch']=epoch
        c.execute('UPDATE runtime_state SET value=? WHERE key=?',
            (json.dumps(lease,sort_keys=True),'native-protection-lease:'+gid))
        return True

    async def refresh(self,gid,*,epoch,proofs,started_ms):
        config=self.rt.config(gid);venue=self.rt.venue_for(config.account_ref,config.market_ref)
        now_ms=venue.clock_ms()
        await self.rt.store.write(lambda c:self.save(c,gid,epoch=epoch,proofs=proofs,started_ms=started_ms,now_ms=now_ms))

    async def resume_proofs(self,gid,*,expected_epoch,allowed_states=('PAUSED','READY','WAITING_RANGE')):
        """Query retained native stops before the activation epoch is committed.

        The lifecycle transaction binds these proofs to its new epoch without
        renewing their original deadline. No old lease is carried forward.
        """
        if self.rt.service_coverage.policy(gid) is None or not self.required(gid):
            return None
        config=self.rt.config(gid)
        venue=self.rt.venue_for(config.account_ref,config.market_ref)
        started_ms=venue.clock_ms();proofs=[]
        for spec in self.rt.protection.specs(config,venue):
            current=self.rt.lifecycle._grid(gid)
            if current['epoch']!=expected_epoch or current['status'] not in allowed_states:
                raise AdapterError('PROTECTION_OPERATION_INTERRUPTED')
            active=self.rt.journal.intents_for_grid(gid,states=['ACCEPTED','OPEN','PARTIAL'])
            matches=[i for i in active if i.get('whole_position') and i['side']==spec['side']
                and i['trigger_price']==spec['trigger_price'] and i['price']==spec['price']]
            if not matches:
                raise AdapterError('PROTECTION_SERVICE_NATIVE_PROOF_UNVERIFIED')
            stop=matches[-1]
            proof=await self.rt.executors[config.account_ref].lookup_intent(stop)
            proofs.append((stop['intent_id'],proof))
        return {'proofs':proofs,'started_ms':started_ms,'config_hash':config.config_hash}

    def required(self,gid):
        # Once entry orders have reached the venue, keep observing for the
        # active grid's lifetime: a terminal order can precede fill booking.
        def read(c):
            if Inventory(self.rt.store,gid).net_position(c)!=0:return True
            if c.execute('SELECT 1 FROM execution_owners WHERE grid_id=? LIMIT 1',(gid,)).fetchone():return True
            return c.execute("SELECT 1 FROM order_intents WHERE grid_id=? AND reduce_only=0 AND client_id IS NOT NULL AND state IN "
                "('ACCEPTED','OPEN','PARTIAL','UNKNOWN','DISPATCHING','CANCEL_REQUESTED','CANCEL_PENDING','FILLED','CANCELED') LIMIT 1",(gid,)).fetchone() is not None
        return self.rt.store.read(read)

    def check(self,gid,*,now_ms,pending=None):
        backend=self.backend(gid)
        if backend is not None:return backend.check_lease(gid,now_ms=now_ms,pending=pending)
        if self.fixed_protection(gid) and (pending is not None or self.required(gid)):
            return 'PROTECTION_SERVICE_FIXED_PROOF_REQUIRED'
        policy=self.rt.service_coverage.policy(gid)
        if policy is None:return None
        grid=self.rt.lifecycle._grid(gid)
        window=self.rt.service_coverage.snapshot(gid)
        if window and window['epoch']==grid['epoch'] and window['phase']=='rearming':
            if window['policy']!=policy.to_dict():return 'PROTECTION_SERVICE_POLICY_CHANGED'
            if type(now_ms) is not int or now_ms<window['started_ms'] or now_ms>=window['deadline_ms']:
                return 'PROTECTION_SERVICE_TIMEOUT'
            return None  # The confirmed native cancellation's original deadline owns this phase.
        if pending is None and not self.required(gid):return None
        config=self.rt.config(gid);lease=self.snapshot(gid)
        if (not lease or lease['epoch']!=grid['epoch'] or lease['config_hash']!=config.config_hash
                or lease['account']!=config.account_ref or lease['market']!=config.market_ref):
            return 'PROTECTION_SERVICE_NATIVE_PROOF_REQUIRED'
        if type(now_ms) is not int or now_ms<lease['confirmed_from_ms'] or now_ms>=lease['deadline_ms']:
            return 'PROTECTION_SERVICE_NATIVE_PROOF_EXPIRED'
        return None

    def retain_orders_during_gap(self, gid):
        """A live HL whole-position stop may protect already-resting orders.

        This does not authorize a send or renew proof. Native proof expiry,
        guardian failure, a rearming window or a newer user operation wins.
        Other protocols require their own fixed-quantity coverage model.
        """
        grid = self.rt.lifecycle._grid(gid)
        if grid['status'] != 'RECOVERING' or grid['desired_mode'] != 'ACTIVE':
            return False
        config = self.rt.config(gid)
        venue = self.rt.venue_for(config.account_ref, config.market_ref)
        backend=self.backend(gid,config)
        if backend is not None:return backend.retain_orders_during_gap(gid)
        if venue.venue != 'hl' or self.rt.service_coverage.policy(gid) is None:
            return False
        guardian = self.rt.service_guardian_status()
        if not isinstance(guardian, dict) or guardian.get('healthy') is not True:
            return False
        window = self.rt.service_coverage.snapshot(gid)
        if window and window.get('epoch') == grid['epoch'] and window.get('phase') == 'rearming':
            return False
        lease = self.snapshot(gid)
        if not lease or self.check(gid, now_ms=venue.clock_ms()) is not None:
            return False
        def valid(c):
            for iid, oid in lease.get('stops', {}).items():
                row = c.execute('SELECT i.*,o.exchange_order_id FROM order_intents i JOIN exchange_orders o '
                                'ON o.intent_id=i.intent_id WHERE i.intent_id=? AND i.grid_id=?', (iid,gid)).fetchone()
                stop = self.rt.journal.decorate(c,row) if row else None
                if (not stop or not stop.get('whole_position') or not stop['reduce_only']
                        or stop['state'] not in ('ACCEPTED','OPEN','PARTIAL')
                        or stop['exchange_order_id'] != oid):
                    return False
            return bool(lease.get('stops'))
        return self.rt.store.read(valid)

    async def close_interrupted(self):
        """Persist exit before startup can wait on any account/venue request."""
        rows=self.rt.store.read(lambda c:[dict(r) for r in c.execute("SELECT grid_id,epoch,status FROM grids "
            "WHERE desired_mode='ACTIVE' AND status IN ('STARTING','RUNNING','RECOVERING','NEEDS_ATTENTION')")])
        failures=[]
        for row in rows:
            gid=row['grid_id']
            try:
                backend=self.backend(gid)
                if (self.rt.service_coverage.policy(gid) is None and not self.fixed_protection(gid)
                        and not (backend is not None and backend.watches(gid))):continue
                if row['status'] in ('STARTING','RUNNING','RECOVERING') and self.carried_fixed_proof(gid):
                    # The fixed stop was verified resting moments before the
                    # interruption; bounded recovery and the fresh activation
                    # re-prove it with new venue evidence before any send.
                    await self.rt.lifecycle._set_status(gid,'RECOVERING')
                    log.info('carried fixed-stop lease still valid; restart resumes into recovery: %s',gid)
                    continue
                window=self.rt.service_coverage.snapshot(gid)
                required=self.required(gid) or bool(window and window['phase']=='rearming')
            except Exception:
                # Unknown adapter/ledger/window state is not proof of safety.
                log.exception('startup protection check failed for %s',gid)
                required=True
            if required:
                try:
                    await self.rt.protection.request_exit(gid,expected_epoch=row['epoch'],reason='PROTECTION_SERVICE_RESTART')
                except Exception:
                    # Attempt every other grid before reporting persistence failure.
                    log.exception('startup protection exit could not be persisted for %s',gid)
                    failures.append(gid)
        if failures:
            raise RuntimeError('PROTECTION_STARTUP_EXIT_PERSISTENCE_FAILED')


class ProtectionWatchScheduler:
    def __init__(self,runtime,*,clock=None,concurrency=4):
        import time
        self.rt=runtime;self.clock=clock or time.monotonic;self.concurrency=concurrency
        self.tasks={};self.next_attempt={};self.closed=False
        self._cancel_requested=set()

    async def _isolate_failure(self,row):
        gid=row['grid_id']
        log.exception('protection watch failed for %s',gid)
        running=self.tasks.get(gid)
        if running:
            from .task_retirement import cancel_once
            await cancel_once(running[1],self._cancel_requested)
        try:
            required=self.rt.protection_lease.required(gid)
        except Exception:
            # An unreadable ledger is not proof of an empty position.
            required=True
        if not required:
            return
        try:
            lease=self.rt.store.read(lambda c:c.execute('SELECT 1 FROM runtime_state WHERE key=?',
                ('native-protection-lease:'+gid,)).fetchone())
        except Exception:
            lease=None
        if not lease:
            # Without a proven native lease there is no stop known to rest on
            # the venue: engine blindness IS protection absence. Exit
            # immediately, unchanged.
            try:
                await self.rt.protection.request_exit(gid,expected_epoch=row['epoch'],
                    reason='PROTECTION_SERVICE_OBSERVATION_UNAVAILABLE')
            except Exception:
                # Other grids still need their own deadlines checked.
                log.exception('could not persist protection exit for %s',gid)
            return
        now_ms=int(self.clock()*1000)
        first=await self._note_watch_blind(gid,now_ms)
        if now_ms-first<PROTECTION_BLIND_TOLERANCE_MS:
            # A supervisor fault (failed eligibility read, venue temporarily
            # missing, store hiccup) is blindness, not confirmed danger: the
            # native stop keeps resting on the venue and the send gate still
            # refuses new risk while blind. One faulting tick used to
            # request_exit a fully protected grid immediately. Retry on the
            # next tick; sustained failure past the window still runs the
            # exit contract below.
            log.warning('PROTECTION_WATCH_BLIND_TOLERATED grid=%s blind_ms=%s',gid,now_ms-first)
            return
        try:
            await self.rt.protection.request_exit(gid,expected_epoch=row['epoch'],
                reason='PROTECTION_SERVICE_OBSERVATION_UNAVAILABLE')
        except Exception:
            # Other grids still need their own deadlines checked.
            log.exception('could not persist protection exit for %s',gid)

    async def _note_watch_blind(self,gid,now_ms):
        """Remember when continuous supervisor blindness began (watch clock).

        Pure durable write. The onset is written once and only cleared by a
        healthy tick — repeated faults never refresh it, so sustained
        blindness always exhausts the tolerance window and runs the exit
        contract. A store that is too broken to record the marker cannot
        record an exit either; blindness then simply restarts once the
        store recovers.
        """
        key=PROTECTION_WATCH_BLIND_KEY+gid
        def write(c):
            row=c.execute('SELECT value FROM runtime_state WHERE key=?',(key,)).fetchone()
            first=None
            if row is not None:
                try:first=int(json.loads(row[0]))
                except (TypeError,ValueError):first=None
            if first is None or now_ms<first:
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?) '
                          'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (key,json.dumps(now_ms)))
                return now_ms
            return first
        try:
            return await self.rt.store.write(write)
        except Exception:
            return now_ms

    async def _clear_watch_blind(self,gid):
        try:
            await self.rt.store.write(lambda c:c.execute('DELETE FROM runtime_state WHERE key=?',
                (PROTECTION_WATCH_BLIND_KEY+gid,)))
        except Exception:
            pass  # A failed cleanup must not fail the tick that just succeeded.

    async def tick(self):
        if self.closed:return
        # `status` must be selected: the RECOVERING exemption below reads it
        # from this row, and a row missing the column silently disabled the
        # exemption — a grid whose recovery cancel was still in flight was
        # exited on its stale lease (2026-11-05 10:57, g-2f8fd555: closed
        # ~2 minutes into every stream-gap recovery because the guard never
        # matched).
        rows=self.rt.store.read(lambda c:[dict(r) for r in c.execute(
            "SELECT grid_id,account_id,market_key,epoch,status FROM grids WHERE desired_mode='ACTIVE' AND status IN ('STARTING','RUNNING','RECOVERING','NEEDS_ATTENTION')")])
        eligible={}
        for row in rows:
            gid=row['grid_id']
            try:
                backend=self.rt.protection_lease.backend(gid)
                if (self.rt.service_coverage.policy(gid) is not None or self.rt.protection_lease.fixed_protection(gid)
                        or backend is not None and backend.watches(gid)):
                    eligible[gid]=row
            except Exception:
                await self._isolate_failure(row)
        for gid,(epoch,task) in list(self.tasks.items()):
            row=eligible.get(gid)
            if not task.done() and (not row or row['epoch']!=epoch):
                from .task_retirement import cancel_once
                await cancel_once(task,self._cancel_requested)
            if task.done():
                await asyncio.gather(task,return_exceptions=True);del self.tasks[gid]
                self._cancel_requested.discard(task)
        for row in sorted(eligible.values(),key=lambda r:self.next_attempt.get(r['grid_id'],0)):
            try:
                await self._tick_grid(row)
                await self._clear_watch_blind(row['grid_id'])
            except Exception:
                await self._isolate_failure(row)

    async def _tick_grid(self,row):
        gid=row['grid_id']
        if row['account_id'] not in self.rt.venues:
            raise RuntimeError('PROTECTION_VENUE_UNAVAILABLE')
        venue=self.rt.venue_for(row['account_id'],row['market_key'])
        code=self.rt.protection_lease.check(gid,now_ms=venue.clock_ms())
        lease_row=self.rt.store.read(lambda c: c.execute(
            'SELECT value FROM runtime_state WHERE key=?',
            ('native-protection-lease:'+gid,)).fetchone())
        lease_epoch=json.loads(lease_row[0]).get('epoch') if lease_row else None
        if code and lease_epoch is not None and lease_epoch != row['epoch']:
            # Stale-epoch lease: the grid epoch moved (pause bumps it) and the
            # activation/resume is re-establishing the proof — its own prepare
            # fences own this window. The stop stays resting on the venue the
            # whole time, so protection is never absent. A lease matching the
            # CURRENT epoch that expires is still enforced below without I/O.
            return
        if code and lease_row is None and (
                row['status'] in ('STARTING', 'RECOVERING') or not self.rt.store.read(
                lambda c: c.execute("SELECT 1 FROM order_intents WHERE grid_id=? AND state IN ('DISPATCHING','ACCEPTED','OPEN','PARTIAL')",
                                    (gid,)).fetchone())):
            # No lease has ever been written and no stop is mid-dispatch:
            # activation owns this window.
            return
        if code and row.get('status') == 'RECOVERING' and self._recovery_in_flight(row):
            # A stream-loss recovery is actively re-proving control (fresh
            # durable marker from the recovery scheduler): its own bounded
            # retry budget and fences own the window, and the recovery
            # tolerance above bounds how long a carried proof may stay
            # un-renewed. STARTING keeps the immediate local exit: the
            # activation owns its own fences and the independent deadline
            # must hold there.
            return
        if code:
            if not self._blind_tolerated(row,code):
                await self.rt.protection.request_exit(gid,expected_epoch=row['epoch'],reason=code)
                running=self.tasks.get(gid)
                if running:
                    from .task_retirement import cancel_once
                    await cancel_once(running[1],self._cancel_requested)
                    if running[1].done():
                        await asyncio.gather(running[1],return_exceptions=True)
                        self._cancel_requested.discard(running[1])
                        self.tasks.pop(gid,None)
                return
            # Pending blindness: fall through and schedule another observation
            # so the proof is renewed the moment account reads recover.
        window=self.rt.service_coverage.snapshot(gid)
        if window and window['epoch']==row['epoch'] and window['phase']=='rearming':return
        if gid in self.tasks or self.clock()<self.next_attempt.get(gid,0) or len(self.tasks)>=self.concurrency:return
        policy=self.rt.service_coverage.policy(gid)
        self.next_attempt[gid]=self.clock()+(min(2,policy.max_seconds/3) if policy is not None else 2)
        self.tasks[gid]=(row['epoch'],asyncio.create_task(self.observe(row),name='grid-native-protection:'+gid))

    def _recovery_in_flight(self,row):
        """Durable progress signal from the stream-recovery scheduler.

        See module-level recovery_in_flight; the watch reads it on its own
        monotonic clock."""
        return recovery_in_flight(self.rt.store,row['grid_id'],row['epoch'],
            now_mono_ms=int(self.clock()*1000))

    def _blind_tolerated(self,row,code):
        """Tolerate a proof expiry or an unreadable proof during blindness.

        Pure durable read: no venue I/O, so a stalled account read cannot hold
        the supervisor. The backend marker is written after a failed verify
        and cleared by any successful one; the watch marker is written by the
        fault-isolation path. Either one inside the tolerance window makes an
        expiry (or a proof the lease check could not evaluate) pending, not a
        confirmed protection loss — a confirmed bad stop still exits
        immediately and sustained blindness still runs the exit contract.
        """
        if not (isinstance(code,str) and (code.endswith('_PROOF_EXPIRED')
                or code=='PROTECTION_SERVICE_FIXED_PROOF_REQUIRED')):
            return False
        saved=self.rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
            (PROTECTION_BLIND_KEY+row['grid_id'],)).fetchone())
        if saved is not None:
            try:
                first=int(json.loads(saved[0]))
                now_ms=self.rt.venue_for(row['account_id'],row['market_key']).clock_ms()
            except Exception:
                now_ms=None
            if now_ms is not None and 0<=now_ms-first<=PROTECTION_BLIND_TOLERANCE_MS:
                log.warning('PROTECTION_BLIND_WAIT code=%s blind_ms=%s',code,now_ms-first)
                return True
        saved=self.rt.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
            (PROTECTION_WATCH_BLIND_KEY+row['grid_id'],)).fetchone())
        if saved is not None:
            try:
                first=int(json.loads(saved[0]))
                now_ms=int(self.clock()*1000)
            except (TypeError,ValueError):
                return False
            if 0<=now_ms-first<=PROTECTION_BLIND_TOLERANCE_MS:
                log.warning('PROTECTION_BLIND_WAIT code=%s watch_blind_ms=%s',code,now_ms-first)
                return True
        return False

    async def observe(self,row):
        gid=row['grid_id'];config=self.rt.config(gid);venue=self.rt.venue_for(row['account_id'],row['market_key'])
        backend=self.rt.protection_lease.backend(gid,config)
        if backend is not None:
            try:await backend.observe(row)
            except asyncio.CancelledError:raise
            except Exception:pass  # An uncertain read never renews the independent deadline.
            return
        specs=self.rt.protection.specs(config,venue)
        active=self.rt.journal.intents_for_grid(gid,states=['ACCEPTED','OPEN','PARTIAL'])
        if not active and not self.rt.protection_lease.required(gid):return
        try:
            started_ms=venue.clock_ms();proofs=[]
            for spec in specs:
                matches=[i for i in active if i.get('whole_position') and i['side']==spec['side']
                    and i['trigger_price']==spec['trigger_price'] and i['price']==spec['price']]
                if not matches:break
                stop=matches[-1];proof=await self.rt.executors[row['account_id']].lookup_intent(stop)
                if proof.state!='FOUND_OPEN' or proof.native_status!='open' or proof.cumulative_known:break
                proofs.append((stop['intent_id'],proof))
            else:
                await self.rt.protection_lease.refresh(gid,epoch=row['epoch'],proofs=proofs,started_ms=started_ms)
                return
            code=await self.rt.protection.verify(gid)
            if code and code not in ('PROTECTION_SERVICE_PENDING','PROTECTION_OPERATION_INTERRUPTED'):
                reason=code if code.startswith('PROTECTION_SERVICE_') or code=='PROTECTION_PRICE_REACHED' else 'PROTECTION_SERVICE_OBSERVATION_UNAVAILABLE'
                await self.rt.protection.request_exit(gid,expected_epoch=row['epoch'],reason=reason)
        except asyncio.CancelledError:raise
        except AdapterError as error:
            code=str(error)
            if code.startswith('PROTECTION_SERVICE_'):
                await self.rt.protection.request_exit(gid,expected_epoch=row['epoch'],reason=code)
            return
        except Exception:
            # No renewal on uncertain I/O. The supervisor checks the last
            # actual confirmation's deadline even while this read is stalled.
            return

    async def close(self):
        self.closed=True
        from .task_retirement import cancel_once
        for _,task in self.tasks.values():await cancel_once(task,self._cancel_requested)
        await asyncio.gather(*(task for _,task in self.tasks.values()),return_exceptions=True)
        self.tasks.clear()
        self._cancel_requested.clear()
