"""GridRuntime: one service owning many grids (spec §3).

Ownership rules:

- The runtime process is the ONLY writer of its store; each grid runs an
  actor; accounts share one AccountExecutor per signer scope.
- ``execution_mode`` comes from the SERVER START CONFIG ONLY (never from
  a web request): ``simulation`` binds fixture accounts/adapters and a
  dedicated store path; ``production`` accepts only observed plans with
  valid qualification and re-checks at the final send gate. The default
  real-dispatch switch is OFF.
- Creation is one transaction: preview validation/consumption + account
  registration + claim rows + grid/version/levels + budget reservation.
  Exchange-side actions happen after the commit, never inside it.
"""
from __future__ import annotations

import logging
import uuid
import asyncio
import json
import os
from decimal import Decimal, localcontext
from typing import Any, Dict, List, Optional

from . import ledger, models, planner, store
from .actor import GridActor
from .adapters.base import AdapterError, GridVenue
from .adapters.fixture import FixtureVenue
from .executor import AccountExecutor
from .inventory import Inventory
from .inventory_layout import owned_lots, rebuild_candidates, owned_risk_context, portfolio_risk_context
from .funding import FundingSynchronizer, funding_scope
from .journal import OrderJournal
from .lifecycle import GridLifecycle
from .recovery import RecoveryBarrier, recovery_read, audit_report, audit_rebuild_error
from .store import Store, StoreError

log = logging.getLogger("grid.runtime")

PREVIEW_TTL_SEC = 30.0
DISPATCH_BATCH = 32
INITIAL_POSITION_RECONCILIATION_TIMEOUT_SEC = 10
INITIAL_POSITION_RECONCILIATION_POLL_SEC = .25


class RuntimeError_(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


class GridRuntime:
    def __init__(self, *, st: Store, execution_mode: str,
                 scope_owner: Optional[str] = None) -> None:
        if execution_mode not in ("simulation", "production"):
            raise RuntimeError_("BAD_MODE", execution_mode)
        self.store = st
        self.execution_mode = execution_mode
        self.journal = OrderJournal(st)
        self.venues: Dict[str, GridVenue] = {}       # account_id -> venue
        self.funding = FundingSynchronizer(st, self.venues)
        self.executors: Dict[str, AccountExecutor] = {}
        self.actors: Dict[str, GridActor] = {}
        self.lifecycle = GridLifecycle(st=st, journal=self.journal,
                                       executors=self.executors)
        self.lifecycle.runtime = self
        def identity(c):
            row = c.execute("SELECT value FROM meta WHERE key='runtime_id'").fetchone()
            if row:
                return row[0]
            value = scope_owner or f"runtime-{uuid.uuid4().hex[:8]}"
            c.execute("INSERT INTO meta VALUES('runtime_id',?)", (value,))
            return value
        self.runtime_id = self.store.write_sync(identity)
        self._pump_lock = asyncio.Lock()
        self.account_control = asyncio.Lock()
        self._recovery_locks = {}
        self._dispatch_seq = 0
        from .live_risk import LiveRiskGuard
        self.live_risk = LiveRiskGuard(self)
        from .protection import ProtectionManager
        self.protection = ProtectionManager(self)
        from .service_coverage import ServiceCoverageManager
        self.service_coverage = ServiceCoverageManager(self)
        from .protection_watch import ProtectionLease
        self.protection_lease = ProtectionLease(self)
        self.heartbeat_file = None
        from .profit_protection import ProfitProtectionManager
        self.profit_protection = ProfitProtectionManager(self)
        self.operator_trial = None
        self.live_release = None

    # ------------------------------------------------------------ factory
    @classmethod
    def open(cls, db_path: str, *, execution_mode: str) -> "GridRuntime":
        """Open (or create) the runtime store. Simulation and production
        MUST use different paths — enforced here."""
        if execution_mode == "production" and "sim" in db_path:
            raise RuntimeError_(
                "MODE_STORAGE_CONFLICT",
                "production runtime refuses a simulation store path")
        import fcntl
        from pathlib import Path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        lock = open(db_path + ".lock", "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise RuntimeError_("RUNTIME_IN_USE", "another runtime owns this store", 409)
        try:
            st = Store.open(db_path)
        except Exception:
            lock.close()
            raise
        def mode(c):
            previous = c.execute("SELECT value FROM meta WHERE key='execution_mode'").fetchone()
            if previous and previous[0] != execution_mode:
                raise StoreError("MODE_STORAGE_CONFLICT", "store execution mode differs")
            c.execute("INSERT OR IGNORE INTO meta VALUES('execution_mode',?)", (execution_mode,))
        try:
            st.write_sync(mode)
        except Exception:
            st.close()
            lock.close()
            raise
        runtime = cls(st=st, execution_mode=execution_mode)
        runtime._process_lock = lock
        return runtime

    def close(self) -> None:
        self.store.close()
        if getattr(self, "_process_lock", None):
            self._process_lock.close()
            self._process_lock = None

    def cached_request(self, scope, request_id, request_hash):
        row = self.store.read(lambda c:c.execute("SELECT request_hash,result_json FROM request_dedup WHERE owner_scope=? AND request_id=?",(scope,request_id)).fetchone())
        if not row:
            return None
        if row["request_hash"] != request_hash:
            raise StoreError("IDEMPOTENCY_CONFLICT", "request ID already belongs to another payload")
        return _loads(row["result_json"])

    def venue_for(self, account, market):
        root = self.venues.get(account)
        if root is None:
            raise RuntimeError_("ACCOUNT_UNRESOLVED", "account is not registered", 422)
        executor = self.executors.get(account)
        return executor.market_venue(market) if executor else (root.market_venue(market) if hasattr(root, "market_venue") else root)

    async def account_available(self, account, asset, *, grid_id=None, config=None):
        venue=self.venues.get(account)
        if not venue:
            raise RuntimeError_("ACCOUNT_UNRESOLVED", "account is not registered", 422)
        cfg = config or (self.config(grid_id) if grid_id is not None else None)
        if cfg is not None:
            venue = self.venue_for(account, cfg.market_ref)
        snap=await venue.account_snapshot()
        if snap.account != account:
            raise RuntimeError_("ACCOUNT_SCOPE_MISMATCH", "account snapshot belongs to another account", 422)
        value=next((a[1] for a in snap.assets if a[0]==asset),None)
        if value is None:
            raise RuntimeError_("CAPITAL_UNAVAILABLE", "available balance is unknown", 422)
        if cfg is not None and self.execution_mode == "production" and hasattr(self.venues[account], "market_venue"):
            context = portfolio_risk_context(self.store, config=cfg, grid_id=grid_id)
            return Decimal(self.venues[account].portfolio_capacity(cfg, snap, context))
        if grid_id is not None and self.execution_mode == "production" and hasattr(venue, "owned_margin_capacity") and Decimal(snap.position_qty_base or "0") != 0:
            cfg = self.config(grid_id)
            if cfg.account_ref != account or cfg.capital.asset != asset:
                raise RuntimeError_("ACCOUNT_SCOPE_MISMATCH", "budget scope differs from the grid", 422)
            market = await venue.market_snapshot()
            context = owned_risk_context(self.store, grid_id=grid_id, config=cfg, mark=market.mark,
                                        funding_status=self.funding.status(account, cfg.market_ref),
                                        scoped_funding=self.scoped_recovery_current(account))
            return Decimal(venue.owned_margin_capacity(cfg, snap, context))
        return Decimal(value)

    def check_budget(self, c, account, asset, total, available, exclude=None):
        # Historical fixed-stop uncertainty belongs to the shared account,
        # even if its originating grid was already closed. Existing budgets
        # may be reduced while investigating; new or larger reservations may
        # not consume capacity whose ownership has not been reconciled.
        if exclude is None:
            increases = True
        else:
            old = c.execute('SELECT b.reserved_amount FROM budget_reservations b JOIN grids g ON g.grid_id=b.grid_id '
                            'WHERE b.grid_id=? AND b.asset=? AND g.account_id=?', (exclude, asset, account)).fetchone()
            increases = old is None or Decimal(total) > Decimal(old[0])
        if increases and self.journal.pending_account_fixed_terminals(c, account):
            raise StoreError('PROTECTION_CUMULATIVE_UNVERIFIED', 'account has unresolved protection executions', 422)
        if exclude is not None and callable(getattr(self.live_release, 'validate_reservation', None)):
            self.live_release.validate_reservation(c, exclude, total)
        if exclude is not None and self.operator_trial is not None:
            self.operator_trial.validate_reservation(c, exclude, total)
        rows=c.execute("SELECT b.grid_id,b.reserved_amount FROM budget_reservations b JOIN grids g ON g.grid_id=b.grid_id "
                       "WHERE g.account_id=? AND b.asset=?",(account,asset)).fetchall()
        used=sum((Decimal(r["reserved_amount"]) for r in rows if r["grid_id"]!=exclude),Decimal(0))
        if Decimal(total) < 0 or used+Decimal(total)>available:
            raise StoreError("BUDGET_EXCEEDED", "account cannot cover all grid reservations", 422)

    # -------------------------------------------------------- sim venue
    def add_fixture_venue(self, venue: FixtureVenue) -> None:
        """Simulation-only wiring: a fixture account/adapter. Production
        runtimes refuse fixture venues at this seam."""
        if self.execution_mode != "simulation":
            raise RuntimeError_("FIXTURE_IN_PRODUCTION",
                                "fixture venues are simulation-only",
                                status=403)
        account_id = venue._account.account_id
        self.venues[account_id] = venue
        self.executors[account_id] = AccountExecutor(
            st=self.store, venue=venue, journal=self.journal,
            account=venue._account, execution_mode=self.execution_mode)

    async def add_venue(self, venue: GridVenue) -> None:
        """Register observed real data; registration never grants trading."""
        if self.execution_mode != "production":
            raise RuntimeError_("REAL_VENUE_IN_SIMULATION", "real adapters require production mode", 403)
        account = await venue.resolve_account()
        if account.fixture or isinstance(venue, FixtureVenue):
            raise RuntimeError_("FIXTURE_IN_PRODUCTION", "fixture venues are simulation-only", 403)
        if account.account_id in self.venues:
            raise RuntimeError_("ACCOUNT_ALREADY_REGISTERED", "account adapter already registered")
        market = getattr(venue, "market", None)
        if market is None and not hasattr(venue, "market_venue"):
            raise RuntimeError_("MARKET_UNRESOLVED", "adapter has no explicit market", 422)
        if market is not None and getattr(venue, "rules", None) is None:
            await venue.load_instrument(market.market_key)
        def register(c):
            row = c.execute("SELECT venue,deployment,owner,fixture FROM accounts WHERE account_id=?", (account.account_id,)).fetchone()
            if row and (row["venue"], row["deployment"], row["owner"], row["fixture"]) != (account.venue, account.deployment, account.owner, 0):
                raise StoreError("ACCOUNT_IDENTITY_CONFLICT", "economic account identity changed")
            c.execute("INSERT OR IGNORE INTO accounts(account_id,venue,deployment,owner,fixture,created_ts) VALUES(?,?,?,?,0,?)",
                      (account.account_id, account.venue, account.deployment, account.owner, models.utc_now_iso()))
            c.execute("UPDATE accounts SET credential_revision=? WHERE account_id=?", (account.credential_revision, account.account_id))
        await self.store.write(register)
        self.venues[account.account_id] = venue
        # Read-only account rechecks replace the root adapter. Keep market
        # qualification attached; the authority still checks the current
        # credential revision for every grant.
        if hasattr(venue, "market_venue") and callable(getattr(self.live_release, "grant", None)):
            venue.live_release = self.live_release
        self.executors[account.account_id] = AccountExecutor(st=self.store, venue=venue,
            journal=self.journal, account=account, execution_mode=self.execution_mode)
        self.executors[account.account_id].trial_authority = lambda: self.operator_trial
        self.executors[account.account_id].live_authority = lambda: self.live_release
        if hasattr(venue, "risk_model_for") or hasattr(venue, "market_venue"):
            executor = self.executors[account.account_id]
            executor.risk_prepare = self.live_risk.prepare
            executor.risk_fence = self.live_risk.fence
            executor.risk_refuse = self.live_risk.refuse
            executor.risk_finished = lambda intent_id: self.live_risk.permits.pop(intent_id, None)

    # ------------------------------------------------------------ preview
    async def preview(self, config: models.GridConfig, *, purpose: str =
                      "create", fixed_qty_base=None, grid_id=None, target_revision=None, reservation_delta="0",
                      persist=True, validate_resting=True) -> Dict[str, Any]:
        params = dict(purpose=purpose, fixed_qty_base=fixed_qty_base, grid_id=grid_id,
                      target_revision=target_revision, reservation_delta=reservation_delta,
                      persist=persist, validate_resting=validate_resting)
        venue = self.venue_for(config.account_ref, config.market_ref)
        transport = getattr(venue, 'transport', None)
        if hasattr(transport, 'read_sequence'):
            sequence = getattr(transport, 'public_read_sequence', transport.read_sequence) if grid_id is None else transport.read_sequence
            async with sequence():
                return await self._preview(config, **params)
        return await self._preview(config, **params)

    async def _preview(self, config: models.GridConfig, *, purpose: str =
                       "create", fixed_qty_base=None, grid_id=None, target_revision=None, reservation_delta="0",
                       persist=True, validate_resting=True) -> Dict[str, Any]:
        """Build a Plan against the venue's current rules/market. In
        simulation the plan_kind is 'fixture'. Native models may return a
        VALID observed size while live_eligible remains False. The private
        grid context is derived from durable ownership for resume/adjust;
        RPC create previews cannot supply it or borrow an existing position.
        """
        account_id = config.account_ref
        original_grid = self.lifecycle._grid(grid_id) if grid_id is not None else None
        venue = self.venue_for(account_id, config.market_ref)
        if venue is None:
            raise RuntimeError_("ACCOUNT_UNRESOLVED",
                                f"no venue for account {account_id}",
                                status=422)
        if self.execution_mode == "production" and config.credential_ref != getattr(venue, "credential_ref", None):
            raise RuntimeError_("CREDENTIAL_REVISION_CONFLICT", "账户凭证已变更，请重新选择账户并预览", 409)
        rules = await venue.load_instrument(config.market_ref)
        if hasattr(venue, "grid_rules"):
            rules = venue.grid_rules(config.lower_price, config.upper_price)
        from .hl_rate import critical_market_snapshot
        quote = critical_market_snapshot(venue) if grid_id is not None else venue.market_snapshot()
        snap, acct_snap = await asyncio.gather(quote, venue.account_snapshot())
        self.protection.validate_config(config, venue, rules, snap,allow_service_preview=True)
        observation = {"market_ts": snap.ts, "account_ts": acct_snap.ts, "mark": snap.mark}
        if acct_snap.account != account_id or snap.market != config.market_ref:
            raise RuntimeError_("ACCOUNT_SCOPE_MISMATCH", "risk snapshot belongs to another account or market", 422)
        inventory_context = None
        portfolio_context = None
        if self.execution_mode == "production" and hasattr(self.venues[account_id], "market_venue"):
            portfolio_context = portfolio_risk_context(self.store, config=config, grid_id=grid_id,
                scoped_funding=self.scoped_recovery_current(account_id))
        if grid_id is not None and self.execution_mode == "production":
            inventory_context = owned_risk_context(self.store, grid_id=grid_id, config=config, mark=snap.mark,
                funding_status=self.funding.status(account_id, config.market_ref), target_revision=target_revision,
                reservation_delta=reservation_delta, scoped_funding=self.scoped_recovery_current(account_id))
            if inventory_context["epoch"] != original_grid["epoch"] or inventory_context["base_revision"] != (original_grid["active_revision"] or 1):
                raise RuntimeError_("OPERATION_INTERRUPTED", "grid changed during inventory risk observation")
        # Native models may produce an observed size preview without live
        # execution qualification. Fixtures retain their test-only demand table.
        sizing_out = None
        risk_model = getattr(venue, "risk_model", None)
        max_qty = getattr(venue, "max_qty_base", "0.01")
        try:
            lines = planner.price_lines(config.lower_price,
                                        config.upper_price,
                                        config.grid_count, config.spacing,
                                        rules.tick)
        except models.GridModelError as e:
            raise RuntimeError_("VALIDATION_ERROR", e.message, 400) from e
        if hasattr(venue, "risk_model_for"):
            risk_kwargs = {}
            if inventory_context:
                risk_kwargs["owned_context"] = inventory_context
                if getattr(venue, 'venue', None) == 'bulk':
                    # An existing quantity is revalidated against the committed
                    # capital: the adverse envelope already covers the owned
                    # inventory's downside, so the unrealized loss must not
                    # shrink the pool a second time (that refused every resume
                    # after a mark-down with BUDGET_EXCEEDED).
                    risk_kwargs["revalidate"] = fixed_qty_base is not None
            if portfolio_context is not None:
                risk_kwargs["portfolio_context"] = portfolio_context
            risk_model, cap = venue.risk_model_for(config, lines, snap, acct_snap, **risk_kwargs)
            max_qty = cap if cap is not None else max_qty
        if risk_model is not None:
            from . import sizing as _sizing
            account_remaining = next(
                (a[1] for a in acct_snap.assets
                 if a[0] == rules.settlement_asset), "0")
            account_remaining = getattr(risk_model, "account_remaining", None) or account_remaining
            total_budget = getattr(risk_model, "total_budget", config.capital.total)
            service_policy=config.protection.service_coverage if config.protection else None
            if service_policy is not None:
                if not hasattr(risk_model,'with_service_coverage'):
                    raise RuntimeError_('SERVICE_COVERAGE_MODEL_UNAVAILABLE','当前风险模型尚无保护期敞口计算',422)
                risk_model=risk_model.with_service_coverage()
            sizing_out = _sizing.solve_qty(
                risk_model=risk_model, qty_step_base=rules.qty_step,
                min_qty_base=rules.min_qty or "0",
                max_qty_base=max_qty,
                used_prices=lines["levels"],
                min_notional=rules.min_notional or "0",
                total=total_budget,
                reserve=config.capital.reserve,
                account_remaining=account_remaining, fixed_qty_base=fixed_qty_base,
                service_coverage_limit=service_policy.max_open_notional if service_policy else None,
                selection_ordinary_utilization=(getattr(risk_model, 'ordinary_utilization', '1')
                    if self.execution_mode == 'production' else '1'))
            if service_policy is not None and sizing_out.get('service_coverage'):
                sizing_out['service_coverage'].update(max_seconds=service_policy.max_seconds,
                    guardian=self.service_guardian_status(),qualification='pending')
            if inventory_context is not None:
                sizing_out["inventory_risk"] = {"net_qty_base": inventory_context["qty_base"],
                    "net_pnl": inventory_context["net_pnl"], "capital_limit": inventory_context["capital_limit"],
                    "effective_total": total_budget, "account_capacity": account_remaining,
                    "base_revision": inventory_context["base_revision"]}
            if sizing_out.get("validation_status") != "VALID":
                return {"plan": None, "sizing": sizing_out,
                        "preview_id": None, "observation": observation}
            # Non-blocking budget envelope: the runtime risk watch re-solves
            # the plan against the live account every tick, and every fill
            # consumes margin.  A preview that only says VALID hides the
            # price at which that re-validation starts refusing and the grid
            # auto-pauses; estimate it here and let the operator see it
            # before opening.  Never blocks or fails the plan.
            if (self.execution_mode == "production" and fixed_qty_base is None
                    and hasattr(risk_model, "leverage")
                    and getattr(risk_model, "account_remaining", None)
                    and sizing_out.get("adverse_required")):
                try:
                    envelope = _sizing.budget_envelope(
                        account_remaining=risk_model.account_remaining,
                        adverse_required=sizing_out["adverse_required"],
                        qty_base=sizing_out["qty_base"],
                        levels=lines["levels"], mark=snap.mark,
                        leverage=risk_model.leverage,
                        taker_fee=getattr(risk_model, "taker_fee", "0"),
                        grid_count=config.grid_count)
                    if envelope is not None:
                        sizing_out["budget_envelope"] = envelope
                except Exception:
                    pass  # an estimate must never fail the preview
            if self.execution_mode == "production":
                if hasattr(venue, 'preview_account_setup'):
                    sizing_out['account_setup'] = venue.preview_account_setup(config, acct_snap)
                elif acct_snap.evidence:
                    actual = acct_snap.evidence["leverage"]
                    sizing_out["account_setup"] = {"actual": actual, "requested": {"type": config.margin_mode, "value": config.leverage},
                        "requires_change": actual != {"type": config.margin_mode, "value": config.leverage},
                        "confirmed": actual == {"type": config.margin_mode, "value": config.leverage}}
        risk_snapshot = None
        capability_revision = "fixture"
        if risk_model is not None and self.execution_mode == "production":
            risk_snapshot = acct_snap.to_risk_snapshot("pending")
            risk_id = models.sha256_hex({"snapshot": risk_snapshot.to_dict(), "evidence": acct_snap.evidence,
                                        "inventory": inventory_context})
            from dataclasses import replace
            risk_snapshot = replace(risk_snapshot, snapshot_id=risk_id)
            capability_revision = f"{risk_model.model_id}:UNQUALIFIED"
        plan = planner.build_fixture_plan(
            config=config, rules=rules, market_snapshot=snap,
            risk_snapshot=risk_snapshot, sizing=sizing_out,
            capability_revision=capability_revision,
            plan_kind="fixture" if self.execution_mode == "simulation"
            else "observed")
        if (self.execution_mode == 'production' and self.live_release is not None
                and plan.validation_status == 'VALID' and self.live_release.permits_config(config)):
            from dataclasses import replace
            grant = self.live_release.grant(config.account_ref, config.market_ref, config=config)
            plan = planner._with_plan_hash(replace(plan, live_eligible=True,
                capability_revision='live-release:'+grant['grant_id']))
        elif (self.execution_mode == 'production' and plan.validation_status == 'VALID'
                and self.operator_trial is not None
                and callable(getattr(self.operator_trial, 'website_authorizes', None))
                and self.operator_trial.website_authorizes(config)):
            # Website-path operator trial (Bulk schema 2): mark the plan for
            # this scope only. Creation re-validates every scope inside the
            # store transaction; expiry or a disarmed venue refuses there.
            from dataclasses import replace
            plan = planner._with_plan_hash(replace(plan, live_eligible=True,
                capability_revision='operator-trial:'+self.operator_trial.policy_hash))
        if plan.validation_status != "VALID":
            return {"plan": plan, "preview_id": None, "sizing": sizing_out, "observation": observation}
        if validate_resting and inventory_context is not None and hasattr(venue, "validate_resting_candidates"):
            candidates = rebuild_candidates([order.to_dict() for order in plan.candidate_orders if order.status == "SEND_CANDIDATE"],
                inventory_context["lots"], qty_per_grid=plan.qty_per_grid,
                revision=inventory_context["revision"], direction=config.direction)
            try:
                venue.validate_resting_candidates(candidates, epoch=inventory_context["epoch"], policy=config.execution_policy)
            except Exception as error:
                raise RuntimeError_("RESTING_PLAN_INVALID", "存量退出订单或新挂单不满足当前交易所精度、最小金额规则；暂不能重建", 422) from error
        if not persist:
            return {"plan": plan, "sizing": sizing_out, "preview_id": None, "observation": observation}
        import time as _time
        preview_id = f"gpv-{uuid.uuid4().hex[:16]}"
        expires = _time.time() + PREVIEW_TTL_SEC

        def w(c):
            if inventory_context is not None:
                current = c.execute("SELECT epoch FROM grids WHERE grid_id=?", (grid_id,)).fetchone()
                if current[0] != inventory_context["epoch"]:
                    raise RuntimeError_("OPERATION_INTERRUPTED", "grid changed before saving risk preview")
            c.execute(
                "INSERT INTO previews(preview_id, purpose, config_hash, "
                "account, market, rules_revision, capability_revision, "
                "risk_snapshot_id, quote_snapshot_id, plan_json, "
                "expires_ts, created_ts, config_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (preview_id, purpose, plan.config_hash, plan.account,
                 plan.market, plan.rules_revision, plan.capability_revision,
                 plan.risk_snapshot_id, plan.quote_snapshot_id,
                 _dumps(plan.to_dict()), expires, _time.time(), _dumps(config.to_dict())))
        await self.store.write(w)
        return {"plan": plan, "sizing": sizing_out, "preview_id": preview_id,
                "expires_ts": expires}

    # ------------------------------------------------------------- create
    async def create_grid(self, *, config: models.GridConfig,
                          preview_id: str, request_id: str,
                          confirm_market: Optional[str] = None, _operator_trial=None) \
            -> Dict[str, Any]:
        async with self.account_control:
            return await self._create_grid(config=config, preview_id=preview_id,
                request_id=request_id, confirm_market=confirm_market, _operator_trial=_operator_trial)

    async def _create_grid(self, *, config, preview_id, request_id, confirm_market=None, _operator_trial=None):
        """The creation transaction (spec §6.2). Idempotent per
        request_id; a consumed/expired/foreign preview is refused."""
        import hashlib
        import time as _time
        config_hash = config.config_hash
        request_hash = models.sha256_hex({
            "config_hash": config_hash, "preview_id": preview_id,
            "confirm_market": confirm_market})
        cached=self.cached_request("grid-create",request_id,request_hash)
        if cached:
            return cached
        # Expiry bounds the user's confirmation. Native balance collection
        # can wait for the shared read budget after a timely confirmation;
        # the final transaction still rechecks every scope and fresh budget.
        confirmed_at = _time.time()
        expiry = self.store.read(lambda c: c.execute(
            "SELECT expires_ts FROM previews WHERE preview_id=?", (preview_id,)).fetchone())
        if expiry is not None and confirmed_at > expiry[0]:
            raise StoreError("PREVIEW_EXPIRED", "preview expired", 409)
        venue = self.venue_for(config.account_ref, config.market_ref)
        if self.execution_mode == "production" and (not venue or config.credential_ref != venue.credential_ref):
            raise RuntimeError_("CREDENTIAL_REVISION_CONFLICT", "账户凭证已变更，请重新预览", 409)
        if not venue:
            raise RuntimeError_("ACCOUNT_UNRESOLVED", "account is not registered", 422)
        account_snapshot = await venue.account_snapshot()
        if account_snapshot.account != config.account_ref:
            raise RuntimeError_("ACCOUNT_SCOPE_MISMATCH", "account snapshot belongs to another account", 422)
        account_identity = await venue.resolve_account()
        if account_identity.account_id != config.account_ref or self.execution_mode == "simulation" and not account_identity.fixture:
            raise RuntimeError_("ACCOUNT_SCOPE_MISMATCH", "account identity is not valid for this runtime", 422)
        value = next((a[1] for a in account_snapshot.assets if a[0] == config.capital.asset), None)
        if value is None:
            raise RuntimeError_("CAPITAL_UNAVAILABLE", "available balance is unknown", 422)
        available = Decimal(value)
        if self.execution_mode == "production" and hasattr(self.venues[config.account_ref], "market_venue"):
            context = portfolio_risk_context(self.store, config=config,
                scoped_funding=self.scoped_recovery_current(config.account_ref))
            available = Decimal(self.venues[config.account_ref].portfolio_capacity(config, account_snapshot, context))
        history_start = venue.history_cursor_from_snapshot(account_snapshot) if hasattr(venue, "history_cursor_from_snapshot") else None
        grid_id = f"g-{uuid.uuid4().hex[:16]}"

        def w(c):
            res, dup = store.register_request(
                c, owner_scope="grid-create", request_id=request_id,
                request_hash=request_hash, result={"grid_id": grid_id})
            if dup:
                return res
            row = c.execute(
                "SELECT * FROM previews WHERE preview_id = ?",
                (preview_id,)).fetchone()
            if row is None:
                raise StoreError("PREVIEW_NOT_FOUND",
                                 f"preview {preview_id} missing", 404)
            if row["purpose"] != "create":
                raise StoreError("PREVIEW_PURPOSE", "preview belongs to another operation")
            if confirmed_at > row["expires_ts"]:
                raise StoreError("PREVIEW_EXPIRED", "preview expired", 409)
            if row["config_hash"] != config_hash:
                raise StoreError("PREVIEW_STALE",
                                 "config changed since preview", 409)
            if row["consumed_by"] is not None:
                raise StoreError("PREVIEW_CONSUMED",
                                 f"preview already used by "
                                 f"{row['consumed_by']}", 409)
            plan = models.Plan.from_dict(_loads(row["plan_json"]))
            nonlocal _operator_trial
            if (_operator_trial is None and self.execution_mode == 'production'
                    and self.operator_trial is not None and plan.live_eligible
                    and callable(getattr(self.operator_trial, 'website_context', None))):
                # Website-path trial (Bulk schema 2): build the context from
                # the active policy inside the transaction. None means the
                # request is out of scope and the public path takes over.
                _operator_trial = self.operator_trial.website_context(
                    c, config, plan, request_id, venue)
            trial = (_operator_trial is not None and self.operator_trial is not None
                     and _operator_trial['policy_hash'] == self.operator_trial.policy_hash
                     and _operator_trial['config_hash'] == config_hash and self.operator_trial.active()
                     and self.operator_trial.request_authorized(request_id))
            if trial:
                self.operator_trial.validate_creation(c, _operator_trial, config, plan)
            public_context = None
            if self.execution_mode == 'production' and plan.live_eligible and not trial:
                if self.live_release is None:
                    raise StoreError('CAPABILITY_UNVERIFIED', 'live approval is unavailable', 422)
                public_context = self.live_release.validate_creation(c, config, plan)
            if self.execution_mode == "production" and not plan.live_eligible and not trial:
                raise StoreError("CAPABILITY_UNVERIFIED", "observed plans cannot authorize real grids", 422)
            self.check_budget(c,config.account_ref,config.capital.asset,config.capital.total,available)
            if plan.validation_status != "VALID":
                raise StoreError("PLAN_NOT_VALID",
                                 f"plan is {plan.validation_status}", 422)
            if self.execution_mode == "simulation":
                acct_row = c.execute(
                    "SELECT fixture FROM accounts WHERE account_id = ?",
                    (config.account_ref,)).fetchone()
                if acct_row is None:
                    c.execute(
                        "INSERT INTO accounts(account_id, venue, "
                        "deployment, owner, fixture, created_ts) "
                        "VALUES(?,?,?,?,1,?)",
                        (config.account_ref, account_identity.venue, account_identity.deployment, account_identity.owner,
                         models.utc_now_iso()))
            # economic claims: the ACCOUNT claim is re-entrant for the
            # same runtime (several grids on one account, different
            # markets); the MARKET claim is exclusive — even for us
            # (spec §11.1: one grid per account+market incl. residuals)
            held = c.execute(
                "SELECT owner_id, state FROM scope_claims WHERE "
                "scope_key = ?", (f"acct:{config.account_ref}",)).fetchone()
            if held is not None and held["state"] == "held" \
                    and held["owner_id"] != self.runtime_id:
                raise StoreError("ACCOUNT_IN_USE",
                                 f"account claimed by {held['owner_id']}",
                                 409)
            held = c.execute(
                "SELECT owner_id, state FROM scope_claims WHERE "
                "scope_key = ?", (f"market:{config.account_ref}:{config.market_ref}",)).fetchone()
            if held is not None and held["state"] == "held":
                raise StoreError("ACCOUNT_IN_USE",
                                 f"market already claimed by "
                                 f"{held['owner_id']}", 409)
            for key in (f"acct:{config.account_ref}",
                        f"market:{config.account_ref}:{config.market_ref}"):
                c.execute(
                    "INSERT INTO scope_claims(scope_key, owner_type, "
                    "owner_id, state, acquired_ts) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(scope_key) DO UPDATE SET state='held', "
                    "owner_id=excluded.owner_id, acquired_ts="
                    "excluded.acquired_ts, released_ts = NULL",
                    (key, "grid", self.runtime_id, "held",
                     models.utc_now_iso()))
            now = models.utc_now_iso()
            c.execute(
                "INSERT INTO grids(grid_id, account_id, market_key, name, "
                "status, desired_mode, epoch, active_revision, created_ts,"
                " updated_ts) VALUES(?,?,?,?, 'READY', 'ACTIVE', 0, NULL,"
                " ?, ?)",
                (grid_id, config.account_ref, config.market_ref,
                 f"{config.direction}-grid", now, now))
            if trial:
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',
                          ('operator-trial:' + grid_id, _dumps({**_operator_trial, 'grid_id': grid_id})))
            if public_context is not None:
                c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',
                          ('live-grid:'+grid_id, _dumps({**public_context,'grid_id':grid_id})))
            c.execute(
                "INSERT INTO grid_versions(grid_id, revision, config_hash,"
                " plan_hash, config_json, plan_json, created_ts) "
                "VALUES(?, 1, ?, ?, ?, ?, ?)",
                (grid_id, config_hash, plan.plan_hash, _dumps(config.to_dict()),
                 _dumps(plan.to_dict()), now))
            for lvl in plan.levels:
                c.execute(
                    "INSERT INTO grid_levels(grid_id, revision, "
                    "level_index, price) VALUES(?,1,?,?)",
                    (grid_id, lvl.level_index, lvl.price))
            c.execute(
                "INSERT INTO budget_reservations(grid_id, reserved_amount,"
                " asset, updated_ts) VALUES(?,?,?,?)",
                (grid_id, config.capital.total, config.capital.asset, now))
            c.execute("UPDATE previews SET consumed_by = ? WHERE "
                      "preview_id = ?", (grid_id, preview_id))
            if history_start is not None:
                c.execute("INSERT INTO runtime_state(key,value) VALUES(?,?)", (f"history-start:{grid_id}", history_start))
                c.execute("INSERT OR IGNORE INTO runtime_state(key,value) VALUES(?,?)",
                    (f"funding-start:{funding_scope(config.account_ref, config.market_ref)}", history_start))
            return {"grid_id": grid_id}
        out = await self.store.write(w)
        # wire the actor post-commit
        await self._load_actor(out["grid_id"])
        return out

    # -------------------------------------------------------------- actor
    async def _load_actor(self, grid_id: str, *,
                          revision: Optional[int] = None, expected_epoch=None) -> GridActor:
        """Wire the actor for the ACTIVE (or explicit) version. Hot
        modification swaps the actor onto the new revision; the old
        version's rows stay untouched for audit."""
        def q(c):
            g = c.execute("SELECT * FROM grids WHERE grid_id = ?",
                          (grid_id,)).fetchone()
            if g is None:
                raise StoreError("NOT_FOUND", f"grid {grid_id}", 404)
            if expected_epoch is not None and g["epoch"] != expected_epoch:
                raise RuntimeError_("OPERATION_INTERRUPTED", "grid changed before loading actor")
            rev = revision if revision is not None \
                else (g["active_revision"] or 1)
            v = c.execute(
                "SELECT * FROM grid_versions WHERE grid_id = ? AND "
                "revision = ?", (grid_id, rev)).fetchone()
            if v is None:
                raise StoreError("NOT_FOUND",
                                 f"grid {grid_id} revision {rev}", 404)
            levels = [r["price"] for r in c.execute(
                "SELECT price FROM grid_levels WHERE grid_id = ? AND "
                "revision = ? ORDER BY level_index", (grid_id, rev)) \
                .fetchall()]
            cfg = _loads(v["config_json"])
            return {"grid": dict(g), "config": cfg, "levels": levels,
                    "revision": rev}
        info = self.store.read(q)
        account_id = info["grid"]["account_id"]
        venue = self.venue_for(account_id, info["grid"]["market_key"])
        if venue is None:
            raise RuntimeError_("ACCOUNT_UNRESOLVED", account_id, 422)
        closed = info['grid']['status'] == 'CLOSED'
        rules = getattr(venue, 'rules', None) if closed else None
        if rules is None:
            from .hl_rate import critical_instrument_rules
            rules = await critical_instrument_rules(venue, info["grid"]["market_key"])
        if hasattr(venue, "grid_rules"):
            cfg = models.GridConfig.from_dict(info["config"])
            rules = venue.grid_rules(cfg.lower_price, cfg.upper_price)
        account = await venue.resolve_account()
        cfg = models.GridConfig.from_dict(info["config"])
        actor = GridActor(
            st=self.store, journal=self.journal, grid_id=grid_id,
            account=account, market_key=info["grid"]["market_key"],
            rules=rules, direction=cfg.direction, levels=info["levels"],
            epoch=int(info["grid"]["epoch"]), revision=info["revision"],
            execution_policy=cfg.execution_policy)
        # plan candidates from the stored plan
        def q2(c):
            row = c.execute(
                "SELECT plan_json FROM grid_versions WHERE grid_id = ? "
                "AND revision = ?", (grid_id, info["revision"])).fetchone()
            return _loads(row["plan_json"])
        plan_dict = self.store.read(q2)
        plan = models.Plan.from_dict(plan_dict)
        actor.candidates = [c.to_dict() for c in plan.candidate_orders]
        actor.drain_pump = self._make_pump(grid_id)
        if closed:
            # CLOSED has no inventory to mark. Its historical reference is
            # sufficient to reconstruct the ledger, without spending live
            # API capacity for every previously closed grid at startup.
            actor.mark = plan.reference_price or '0'
        else:
            from .hl_rate import critical_market_snapshot
            snap = await critical_market_snapshot(venue)
            actor.mark = snap.mark or "0"
        if expected_epoch is not None and self.lifecycle._grid(grid_id)["epoch"] != expected_epoch:
            raise RuntimeError_("OPERATION_INTERRUPTED", "grid changed while loading actor")
        self.actors[grid_id] = actor
        return actor

    def _make_pump(self, grid_id: str):
        async def pump() -> None:
            await self.pump_events(grid_id)
        return pump

    # ------------------------------------------------------------ control
    async def start_grid(self, grid_id: str, *,
                         request_id: Optional[str] = None) -> Dict[str, Any]:
        op = await self.lifecycle._begin_operation(grid_id, "start", request_id)
        if op.get("replayed"):
            return op
        activation_epoch = None
        try:
            grid=self.lifecycle._grid(grid_id)
            if grid["status"] not in ("READY", "WAITING_RANGE"):
                raise RuntimeError_("BAD_STATE", "grid cannot start from current state")
            activation_epoch = await self.lifecycle._bump_epoch(grid_id, status="STARTING", desired="ACTIVE", expected_epoch=grid["epoch"], allowed_states=("READY","WAITING_RANGE"))
            if self.execution_mode == "production":
                await self.lifecycle._step(op["operation_id"], "reconcile")
                report = await self.recover(grid_id, restore=False, expected_epoch=activation_epoch)
                if not report["ok"]:
                    raise RuntimeError_("RECOVERY_BLOCKED", "orders/history/position not reconciled", 422)
            await self.lifecycle._step(op["operation_id"], "initialize")
            result = await self.activate(grid_id, initialize=True, expected_epoch=activation_epoch)
            await self.lifecycle._finish_operation(op["operation_id"], "succeeded", result=result)
            return {"operation_id": op["operation_id"], "status": "succeeded", **result}
        except Exception as e:
            current = self.lifecycle._grid(grid_id)
            protective = self.store.read(lambda c: c.execute('SELECT value FROM runtime_state WHERE key=?',
                ('protection-exit:'+grid_id,)).fetchone())
            closeout = _loads(protective[0]) if protective else None
            if (activation_epoch is not None and closeout
                    and closeout.get('trigger_epoch') == activation_epoch + 1
                    and current['epoch'] > activation_epoch and current['desired_mode']=='STOPPED'):
                result = {'code':'PROTECTION_EXIT_TRIGGERED', 'protective_exit':True,
                          'trigger':closeout['reason'], 'grid_status':current['status'],
                          'message':'启动期间触发保护，已转入自动退出流程；请查看网格详情与实际仓位。'}
                await self.lifecycle._finish_operation(op['operation_id'],'failed',result=result,error=result['message'])
                return {'operation_id':op['operation_id'],'status':'failed',**result}
            if activation_epoch is not None:
                await self.lifecycle._set_status(grid_id, "NEEDS_ATTENTION", desired="PAUSED",
                    expected_epoch=activation_epoch, allowed_states=("STARTING", "NEEDS_ATTENTION"))
            await self.lifecycle._finish_operation(op["operation_id"], "failed", error=str(e))
            if isinstance(e, RuntimeError_) and e.code == "ORDER_NOT_CONFIRMED":
                return {"operation_id": op["operation_id"], "status": "failed", "code": e.code,
                        "message": e.message, "grid_status": self.lifecycle._grid(grid_id)['status']}
            raise

    def config(self, grid_id: str) -> models.GridConfig:
        raw = self.store.read(lambda c: c.execute(
            "SELECT v.config_json FROM grid_versions v JOIN grids g ON g.grid_id=v.grid_id "
            "WHERE v.grid_id=? AND v.revision=COALESCE(g.active_revision,1)", (grid_id,)).fetchone())
        if not raw:
            raise RuntimeError_("NOT_FOUND", grid_id, 404)
        return models.GridConfig.from_dict(json.loads(raw[0]))

    async def activate(self, grid_id: str, *, initialize=False, expected_epoch=None) -> Dict[str, Any]:
        original = self.lifecycle._grid(grid_id)
        if expected_epoch is not None and original["epoch"] != expected_epoch:
            raise RuntimeError_("OPERATION_INTERRUPTED", "a newer operation owns activation")
        expected_epoch = original["epoch"] if expected_epoch is None else expected_epoch
        if original["status"] != "STARTING" or original["desired_mode"] != "ACTIVE":
            raise RuntimeError_("OPERATION_INTERRUPTED", "grid is no longer starting")
        stored_plan = self.store.read(lambda c: _loads(c.execute(
            "SELECT plan_json FROM grid_versions WHERE grid_id=? AND revision=?",
            (grid_id, original["active_revision"] or 1)).fetchone()[0]))
        cfg = self.config(grid_id)
        venue = self.venue_for(cfg.account_ref, cfg.market_ref)
        fresh = await self.preview(cfg, purpose="activate", fixed_qty_base=stored_plan["qty_per_grid"], grid_id=grid_id)
        plan = fresh.get("plan")
        if not plan or plan.validation_status != "VALID":
            sizing = fresh.get("sizing") or {}
            log.warning('GRID_ACTIVATE_PLAN_BLOCKED plan=%s sizing_status=%s sizing_code=%s',
                getattr(plan, 'validation_status', None), sizing.get('validation_status'), sizing.get('code'))
            raise RuntimeError_("PLAN_NOT_VALID", "current risk or price plan is blocked", 422)
        if (self.execution_mode=='production' and getattr(getattr(venue,'account_parent',None),'live_release',None) is not None
                and not (self.live_release is not None and self.live_release.permits_grid(grid_id)
                         or self.operator_trial is not None and self.operator_trial.permits_grid(grid_id))):
            raise RuntimeError_('CAPABILITY_UNVERIFIED','此网格的实盘授权已失效，仍可撤单和平仓',422)
        if self.execution_mode == "production" and hasattr(venue, "configure_leverage"):
            async def gate():
                current = self.lifecycle._grid(grid_id)
                return (current["epoch"] == expected_epoch and current["status"] == "STARTING"
                        and current["desired_mode"] == "ACTIVE"
                        and ((os.getenv("GRID_REAL_DISPATCH", "0") == "1" and venue.live_qualified and venue.write_enabled)
                             or self.operator_trial is not None and self.operator_trial.permits_grid(grid_id)
                             or self.live_release is not None and self.live_release.permits_grid(grid_id))
                        and cfg.credential_ref == venue.credential_ref)
            control = await venue.configure_leverage(leverage=cfg.leverage, margin_mode=cfg.margin_mode,
                request_id=f"leverage:{grid_id}:e{expected_epoch}", gate=gate)
            if control["state"] != "CONFIRMED":
                raise RuntimeError_(control.get("code") or "LEVERAGE_UNCONFIRMED", "账户杠杆设置尚未确认，网格未发单", 422)
            # A change (or an adapter without an explicit no-effect proof)
            # needs a new margin snapshot. Re-reading an already matching
            # setting need not duplicate the full activation preview; normal
            # per-order risk observations and final fences still run.
            if not (control.get('unchanged') is True
                    and (fresh.get('sizing') or {}).get('account_setup',{}).get('confirmed') is True):
                fresh = await self.preview(cfg, purpose="activate", fixed_qty_base=stored_plan["qty_per_grid"], grid_id=grid_id)
                plan = fresh.get("plan")
        current = self.lifecycle._grid(grid_id)
        if current["epoch"] != expected_epoch or current["status"] != "STARTING" or current["desired_mode"] != "ACTIVE":
            raise RuntimeError_("OPERATION_INTERRUPTED", "grid changed during risk validation")
        if not plan or plan.validation_status != "VALID":
            raise RuntimeError_("PLAN_NOT_VALID", "current risk or price plan is blocked", 422)
        actor = await self._load_actor(grid_id, expected_epoch=expected_epoch)
        actor.candidates = [c.to_dict() for c in plan.candidate_orders]
        if (plan.initialization_status == 'WAITING_RANGE'
                and self.store.read(actor.inv.net_position) == 0
                and await self.lifecycle._open_grid_order_count(grid_id) == 0):
            if not await self.lifecycle._set_status(grid_id, 'WAITING_RANGE', desired='ACTIVE',
                expected_epoch=expected_epoch, allowed_states=('STARTING',)):
                raise RuntimeError_('OPERATION_INTERRUPTED', 'grid changed while waiting for price')
            return {'placed': 0, 'intents': 0, 'waiting_for_range': True}
        executor = self.executors[actor.account.account_id]
        await self.protection.prepare(grid_id, actor, expected_epoch=expected_epoch)
        if not initialize and cfg.initial_position == 'immediate':
            # An early start failure can precede the initial IOC (for
            # example, a native stop's query index becoming visible late).
            # Resume honors the saved immediate-entry choice only when no
            # initial intent or owned execution has ever existed. Accepted,
            # unknown and rejected initial attempts are never replayed here.
            initialize = self.store.read(lambda c: not c.execute(
                "SELECT 1 FROM order_intents WHERE grid_id=? AND cause_key LIKE 'initial-position:%' LIMIT 1",
                (grid_id,)).fetchone() and not c.execute(
                'SELECT 1 FROM execution_owners WHERE grid_id=? LIMIT 1', (grid_id,)).fetchone())
        if initialize and Decimal(plan.initial_position_qty) > 0:
            side = "buy" if cfg.direction == "long" else "sell"
            venue = self.venue_for(actor.account.account_id, actor.market_key)
            if self.execution_mode == 'production':
                from .hl_rate import critical_market_snapshot
                initial_quote = await critical_market_snapshot(venue)
                if initial_quote.market != actor.market_key or initial_quote.mark is None:
                    raise RuntimeError_('INITIAL_POSITION_PRICE_UNAVAILABLE', '无法取得建仓市场的最新价格')
                age = venue.clock_ms()-int(models.parse_iso_ts(initial_quote.ts, 'initial_quote').timestamp()*1000)
                if not -1000 <= age < 5000:
                    raise RuntimeError_('INITIAL_POSITION_PRICE_STALE', '建仓价格已过期')
                actor.mark = initial_quote.mark
            mark = Decimal(actor.mark)
            price = mark * (Decimal("1.003") if side == "buy" else Decimal("0.997"))
            tick = Decimal(actor.rules.tick)
            if hasattr(venue, "protected_ioc_price"):
                price = venue.protected_ioc_price(price, side)
            else:
                price = (price / tick).to_integral_value(rounding="ROUND_FLOOR" if side == "buy" else "ROUND_CEILING") * tick
            layout = planner.initial_layout([line.price for line in plan.levels], plan.reference_price,
                'mark', None, None, plan.qty_per_grid, cfg.direction, cfg.initial_position)
            slots = [{'level_index': cycle + (1 if side == 'buy' else 0), 'qty_base': plan.qty_per_grid}
                     for cycle in layout['initial_inventory_cycles']]
            intent = await self.journal.create_intent(
                grid_id=grid_id, cause_key=f"initial-position:{grid_id}:e{expected_epoch}",
                revision=actor.revision, level_index=0, side=side,
                qty_base=plan.initial_position_qty, price=str(price), policy="ioc",
                reduce_only=False, epoch=expected_epoch, expected_epoch=expected_epoch,
                initial_position_layout=slots)
            result = await executor.submit_intent(intent["intent_id"], epoch=expected_epoch)
            await self.pump_events(grid_id)
            if result["outcome"] != "ACCEPTED":
                raise RuntimeError_("INITIAL_POSITION_UNRESOLVED", "initial position was not confirmed")
            if self.execution_mode == 'production':
                # An ACK is not a booked fill. Query the owned order, history
                # and actual position before sizing exits; WS can arrive later.
                await self._reconcile_initial_position(grid_id, intent['intent_id'], expected_epoch=expected_epoch)
        candidates = self.resting_candidates(actor, plan)
        def clear_demands(c):
            row = c.execute("SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?", (grid_id,)).fetchone()
            if row["epoch"] != expected_epoch or row["status"] != "STARTING" or row["desired_mode"] != "ACTIVE":
                raise RuntimeError_("OPERATION_INTERRUPTED", "grid changed before rebuilding orders")
            c.execute("UPDATE order_intents SET state='EXPIRED_EPOCH' WHERE grid_id=? AND epoch<? "
                      "AND state IN ('PLANNED','BLOCKED_SLOT')", (grid_id, expected_epoch))
            c.execute("DELETE FROM refill_demands WHERE grid_id=?", (grid_id,))
        await self.store.write(clear_demands)
        intents = await actor.place_initial(candidates, epoch=expected_epoch)
        placed, pending = 0, 0
        for intent in sorted(intents, key=lambda item: not item['reduce_only']):
            unknown_rounds = 0
            for _ in range(4):
                # A previous initial order can fill before the rest of the
                # batch is ready. Book it before checking the next order,
                # without letting this drain dispatch the remaining batch.
                await self.pump_events(grid_id, dispatch=False)
                if self.operator_trial is not None and self.operator_trial.permits_grid(grid_id) and hasattr(venue.transport, 'prepare_read_capacity'):
                    await venue.transport.prepare_read_capacity()
                result = await executor.submit_intent(intent["intent_id"], epoch=expected_epoch)
                if result.get('outcome') == 'DEFERRED' and result.get('reject_code') in ('POST_ONLY_WAIT', 'POST_ONLY_BOOK_WAIT', 'ENTRY_PRICE_WAIT'):
                    break
                if result.get('outcome') == 'UNKNOWN' and result.get('reject_code') in (
                        'RISK_SNAPSHOT_EXPIRED', 'RISK_OBSERVATION_CHANGED', 'RISK_NATIVE_SNAPSHOT_STALE',
                        'RISK_COLLECTION_TIMEOUT', 'RISK_EVENTS_PENDING', 'RISK_API_BUDGET_WAIT',
                        'RISK_FUNDING_CHANGED_WAIT'):
                    # The venue's prepare-phase fence refused before any I/O
                    # (stage preparation_not_sent): no bulk original exists, so
                    # the intent is provably unsent. Return it to PLANNED and
                    # retry with a fresh observation instead of failing the
                    # whole activation on machine-load timing.
                    await self.store.write(lambda c: c.execute(
                        "UPDATE order_intents SET state='PLANNED' WHERE intent_id=? AND state='UNKNOWN'",
                        (intent["intent_id"],)))
                    continue
                if result.get('outcome') != 'DEFERRED':
                    break
            if result.get("resolved") == "ABSENT_FINAL_PLANNED":
                result = await executor.submit_intent(intent["intent_id"], epoch=expected_epoch)
            if result.get("resolved") in ("OPEN", "FILLED"):
                result["outcome"] = "ACCEPTED"
            if result["outcome"] == "ACCEPTED":
                placed += 1
            elif result['outcome'] == 'DEFERRED':
                pending += 1
            elif result['outcome'] == 'UNKNOWN':
                # One unproven acknowledgment must not discard a long
                # paced activation: the venue's read visibility can lag
                # the ack by minutes (the bulk openOrders quirk), and
                # the lookup path never resends. Give it three bounded
                # reconciliation rounds before failing the start.
                # 2026-11-06 00:53, g-8d266770: a 17-minute activation
                # aborted on one unproven ACK after the entries were
                # already placed.
                resolved = result.get("resolved")
                while (unknown_rounds < 3 and resolved not in
                       ("OPEN", "PARTIAL", "FILLED", "ABSENT_FINAL_PLANNED",
                        "EXPIRED_EPOCH", "REJECTED", "CANCELED")):
                    unknown_rounds += 1
                    await asyncio.sleep(5)
                    await self.pump_events(grid_id, dispatch=False)
                    resolved = await executor.resolve_unknown(intent["intent_id"])
                if resolved in ("OPEN", "PARTIAL", "FILLED"):
                    placed += 1
                elif resolved in ("ABSENT_FINAL_PLANNED", "EXPIRED_EPOCH",
                                  "REJECTED", "CANCELED"):
                    pending += 1
                else:
                    # The venue's lookup lag can outlast the three bounded
                    # rounds (the bulk openOrders/history quirk lags a fresh
                    # submit's visibility by minutes). The scoped capture
                    # running alongside this rebuild declares such a submit
                    # absent — an unacknowledged send that is not resting
                    # natively, with the counter folds proving no unbooked
                    # economics. Retire the intent exactly as the recovery
                    # barrier does instead of aborting the whole activation.
                    # 2026-11-06 10:44, g-2f850fa0: the resume aborted at
                    # rebuild_orders while its own capture had already
                    # declared the submit absent, stranding the grid in
                    # STARTING — resume refuses STARTING, recover does not
                    # activate, so no driver could ever finish it.
                    cid = intent.get('client_id')
                    retired = False
                    parent = getattr(venue, 'account_parent', None)
                    scope = getattr(getattr(parent, 'journal', None), 'scope', None)
                    if scope and cid:
                        absent = self.store.read(lambda c: c.execute(
                            'SELECT value FROM runtime_state WHERE key=?',
                            ('bulk-declared-absence:' + scope + ':' + cid,)).fetchone())
                        cert = None
                        if absent is not None:
                            try: cert = json.loads(absent[0])
                            except (ValueError, TypeError): cert = None
                        if (isinstance(cert, dict)
                                and cert.get('schema') == 'bulk-declared-absence-v1'
                                and cert.get('client_id') == cid
                                and cert.get('account') == actor.account.account_id
                                and cert.get('evidence') == 'MAY_HAVE_SENT'
                                and cert.get('proof_state') == 'NOT_FOUND_TRANSIENT'):
                            await self.journal.transition(intent['intent_id'], 'PLANNED')
                            await self.journal.transition(intent['intent_id'], 'EXPIRED_EPOCH')
                            pending += 1; retired = True
                            log.warning('GRID_INITIAL_ORDER_ABSENT_RETIRED grid=%s intent=%s cid=%s',
                                        grid_id, intent['intent_id'], cid)
                    if not retired:
                        log.error('GRID_INITIAL_ORDER_UNCONFIRMED grid=%s intent=%s outcome=%s resolved=%s',
                                  grid_id, intent["intent_id"], result.get('outcome'), resolved)
                        raise RuntimeError_("ORDER_NOT_CONFIRMED", "initial/restored grid orders require reconciliation")
            else:
                # The outcome alone cannot be triaged; carry the full
                # result and the intent for the operator and the monitor.
                log.error('GRID_INITIAL_ORDER_UNCONFIRMED grid=%s intent=%s outcome=%s reject_code=%s resolved=%s',
                          grid_id, intent["intent_id"], result.get('outcome'),
                          result.get('reject_code'), result.get('resolved'))
                raise RuntimeError_("ORDER_NOT_CONFIRMED", "initial/restored grid orders require reconciliation")
        await self.pump_events(grid_id)
        updated = await self.store.write(lambda c: c.execute(
            "UPDATE grids SET status='RUNNING', active_revision=COALESCE(active_revision,1), updated_ts=? "
            "WHERE grid_id=? AND epoch=? AND status='STARTING' AND desired_mode='ACTIVE'",
            (models.utc_now_iso(), grid_id, expected_epoch)).rowcount)
        if not updated:
            raise RuntimeError_("OPERATION_INTERRUPTED", "grid was paused or stopped during activation")
        return {"placed": placed, "intents": len(intents), 'pending_orders': pending}

    def resting_candidates(self, actor: GridActor, plan: models.Plan) -> List[Dict[str, Any]]:
        """Rebuild from actual surviving inventory, retaining original exit prices."""
        candidates = [dict(c) for c in actor.candidates
                      if c['status'] == 'SEND_CANDIDATE' or actor.execution_policy == 'post_only']
        if actor.direction in ('long', 'short'):
            # Keep every unoccupied cycle as a durable price demand. A later
            # price return must not silently lose deferred directional cycles.
            side = 'buy' if actor.direction == 'long' else 'sell'
            indices = range(len(actor.levels)-1) if side == 'buy' else range(1, len(actor.levels))
            present = {(c['level_index'], c['side']) for c in candidates}
            for index in indices:
                if (index, side) not in present:
                    candidates.append({'status': 'SEND_CANDIDATE', 'side': side, 'level_index': index,
                        'qty_base': plan.qty_per_grid, 'price': actor.levels[index], 'purpose': 'pending_entry', 'reduce_only': False})
        try:
            lots = self.store.read(lambda c: owned_lots(c, self.store, actor.grid_id))
            return rebuild_candidates(candidates, lots, qty_per_grid=plan.qty_per_grid,
                                      revision=actor.revision, direction=actor.direction)
        except models.GridModelError as error:
            raise RuntimeError_(error.reason, error.message, 422) from error

    async def _reconcile_initial_position(self, grid_id, intent_id, *, expected_epoch):
        """Allow bounded publication lag for ONE already acknowledged IOC.

        Reads/drain only: never submit again, invent a fill or permit new grid
        orders before the complete barrier succeeds. Native protection and
        the enclosing startup deadline keep their original limits.
        """
        initial = self.journal.get_intent(intent_id)
        if (not initial or initial['grid_id'] != grid_id or initial['epoch'] != expected_epoch
                or not initial['cause_key'].startswith('initial-position:' + grid_id + ':')
                or not initial['client_id'] or initial['reduce_only'] or initial['policy'] != 'ioc'):
            raise RuntimeError_('INITIAL_POSITION_UNRESOLVED', 'initial order identity is unverified')
        venue = self.venue_for(self.lifecycle._account_id(grid_id), self.config(grid_id).market_ref)
        generation = getattr(venue, 'stream_generation', None)
        def fence():
            grid = self.lifecycle._grid(grid_id)
            current = self.journal.get_intent(intent_id)
            if (grid['epoch'] != expected_epoch or grid['desired_mode'] != 'ACTIVE'
                    or grid['status'] not in ('STARTING', 'NEEDS_ATTENTION')
                    or current is None
                    or any(current[k] != initial[k] for k in ('client_id','epoch','qty_base','side','grid_id'))
                    or generation is not None and (venue.stream_generation != generation or not venue.stream_connected)):
                raise RuntimeError_('OPERATION_INTERRUPTED', 'initial reconciliation scope changed')
        loop = asyncio.get_running_loop()
        deadline = loop.time() + INITIAL_POSITION_RECONCILIATION_TIMEOUT_SEC
        while True:
            fence()
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError_('INITIAL_POSITION_UNRESOLVED', 'initial position history is not reconciled')
            try:
                report = await asyncio.wait_for(self.recover(grid_id, restore=False,
                    expected_epoch=expected_epoch), remaining)
            except TimeoutError as error:
                # A canceled read may have entered RECOVERING. Leave no
                # active startup behind; a newer epoch/mode always wins.
                await self.lifecycle._set_status(grid_id, 'NEEDS_ATTENTION', desired='PAUSED',
                    expected_epoch=expected_epoch, allowed_states=('STARTING','RECOVERING','NEEDS_ATTENTION'))
                raise RuntimeError_('INITIAL_POSITION_UNRESOLVED', 'initial position history is not reconciled') from error
            fence()
            if report['ok']:
                if not await self.lifecycle._set_status(grid_id, 'STARTING', expected_epoch=expected_epoch,
                        allowed_states=('STARTING','NEEDS_ATTENTION')):
                    raise RuntimeError_('OPERATION_INTERRUPTED', 'initial reconciliation scope changed')
                return report
            transient = (bool(report['drift'] or report['gaps'])
                and all(r.get('code') == 'POSITION_MISMATCH' for r in report['drift'])
                and all(r.get('code') in ('OWNED_ORDER_UNRESOLVED','FILL_CUMULATIVE_MISMATCH')
                        and r.get('client_id') == initial['client_id'] for r in report['gaps']))
            if not transient:
                raise RuntimeError_('INITIAL_POSITION_UNRESOLVED', 'initial position history is not reconciled')
            await asyncio.sleep(min(INITIAL_POSITION_RECONCILIATION_POLL_SEC, max(0, deadline-loop.time())))
            await self.pump_events(grid_id, dispatch=False)

    async def dispatch_pending(self, grid_id: str) -> int:
        """Send every PLANNED intent of the CURRENT epoch (refills land
        here after the fill transaction commits)."""
        actor = self.actors.get(grid_id)
        if actor is None:
            return 0
        g = self.lifecycle._grid(grid_id)
        actor.epoch = g["epoch"]
        # Activation owns its initial intents until every initial outcome is
        # reconciled. A background tick must not compete with that sequence.
        if g["desired_mode"] != "ACTIVE" or g["status"] != "RUNNING":
            return 0
        await actor.release_demands()
        planned = self.journal.intents_for_grid(grid_id,
                                                states=["PLANNED"])
        sent = 0
        for it in planned[:DISPATCH_BATCH]:
            res = await self.executors[actor.account.account_id] \
                .submit_intent(it["intent_id"], epoch=actor.epoch)
            if res.get('outcome') == 'DEFERRED':
                if res.get('reject_code') in ('POST_ONLY_WAIT', 'ENTRY_PRICE_WAIT'):
                    continue  # one waiting entry must not block an owned exit
                break
            if res.get("outcome") in ("ACCEPTED", "REJECTED"):
                sent += 1
            elif res.get("outcome") == "UNKNOWN":
                sent += 1
            unresolved = res.get("outcome") == "UNKNOWN" and res.get("resolved") == "STILL_UNKNOWN"
            if res.get("outcome") == "REJECTED" or unresolved:
                from .lifecycle import LifecycleError
                try:
                    await self.lifecycle._bump_epoch(grid_id,status="NEEDS_ATTENTION",desired="PAUSED",
                        expected_epoch=it["epoch"],allowed_states=("RUNNING","STARTING"))
                except LifecycleError:
                    # A newer pause/exit already owns cleanup; do not replace it.
                    break
                await self.journal.expire_epoch(grid_id,self.lifecycle._grid(grid_id)["epoch"])
                await self.store.write(lambda c:store.register_event(c,event_key=f"dispatch-blocked:{it['intent_id']}",
                    scope=grid_id,event_type="dispatch_blocked",payload=res))
                await self.lifecycle._cancel_open_grid_orders(grid_id,self.lifecycle._grid(grid_id)["epoch"])
                break
        return sent

    async def pump_events(self, grid_id: str, *, dispatch=True) -> int:
        """Drain the venue's private events into the fill transaction."""
        actor = self.actors.get(grid_id)
        if actor is None:
            return 0
        async with self._pump_lock:
            venue = self.venues[actor.account.account_id]
            applied = 0
            triggered = set()
            funding_error = None
            for ev in venue.poll():
                if ev.execution is None:
                    continue
                fill = ev.execution
                owner = self.store.read(lambda c: c.execute(
                    "SELECT i.grid_id FROM exchange_orders o JOIN order_intents i ON i.intent_id=o.intent_id "
                    "WHERE o.account=? AND (o.client_id=? OR o.exchange_order_id=?)",
                    (fill.account, fill.order_client_id, fill.exchange_order_id)).fetchone())
                target = self.actors.get(owner[0]) if owner else None
                if target is None or target.market_key != fill.market:
                    await self.lifecycle._set_status(grid_id, "NEEDS_ATTENTION", desired="PAUSED")
                    await self.store.write(lambda c: store.register_event(c,
                        event_key=f"foreign:{fill.execution_key}", scope=grid_id,
                        event_type="foreign_execution", payload=fill.to_dict()))
                    continue
                res = await target.apply_fill(fill)
                if res.get('protection_triggered'):
                    triggered.add(target.grid_id)
                applied += int(res["outcome"] == "applied")
            if self.lifecycle._grid(grid_id)["status"] != "RECOVERING":
                from .adapters.base import AdapterError
                scoped_event = None
                try:
                    funding = await self.sync_funding(grid_id, force=False)
                    if not funding["ok"] and self.lifecycle._grid(grid_id)["status"] in ("RUNNING", "STARTING", "READY"):
                        scoped_event = funding.get("code")
                except Exception as error:
                    code = str(error) if isinstance(error, AdapterError) else type(error).__name__
                    # Only retryable transport/read failures are "pending".
                    # Data-quality codes (a certified gap, a floor gap, a
                    # cursor conflict) still go through the scoped-basis
                    # judgment below.
                    transient = isinstance(error, (TimeoutError, OSError)) or (
                        isinstance(error, AdapterError) and (
                            code.startswith('BULK_READ_')
                            or code in ('BULK_HISTORY_HTTP_FAILED', 'BULK_HISTORY_BUSY',
                                        'BULK_HISTORY_STORAGE_ERROR', 'BULK_HISTORY_STATE_NOT_READY',
                                        'BULK_CURSOR_EXPIRED', 'BULK_HISTORY_PAGE_BUDGET_EXHAUSTED',
                                        'FUNDING_HISTORY_UNAVAILABLE', 'HL_INFO_BUDGET_WAIT')))
                    if transient:
                        # A background funding reconciliation read failed. It
                        # is a pending read, not a state change: the next tick
                        # resyncs and the recovery barrier re-folds the
                        # economics. Raising here fails the pump, and the pump
                        # scheduler pauses the whole grid silently (2026-11-04
                        # 21:03: one BULK_READ_HTTP_FAILED paused a RUNNING
                        # grid with no epoch bump and no event).
                        await self.store.write(lambda c: store.register_event(c,
                            event_key=f"funding-scan-transient:{grid_id}:{code}",
                            scope=grid_id, event_type='funding_scan_transient',
                            payload={'code': code}))
                        scoped_event = None   # a pending read, not a state change
                    else:
                        # Bulk's retained-history funding scan raises instead
                        # of returning ok=False. Judge it against the
                        # account-scoped basis exactly like recovery does.
                        if not (self.execution_mode == 'production'
                                and isinstance(error, AdapterError)
                                and str(error) in ("FUNDING_HISTORY_INCOMPLETE", "FUNDING_HISTORY_SCAN_PENDING")):
                            raise
                        scoped_event = str(error)
                        funding = {"ok": False, "code": scoped_event}
                if scoped_event is not None and self.lifecycle._grid(grid_id)["status"] in ("RUNNING", "STARTING", "READY"):
                    if not self.scoped_recovery_current(actor.account.account_id):
                        # A private-stream reconnect starts a new era and
                        # invalidates the stored basis; refresh it here so the
                        # scoped funding judgment stays live.
                        parent = self.venues.get(actor.account.account_id)
                        capture = getattr(parent, 'capture_scoped_recovery_basis', None)
                        if callable(capture):
                            try:
                                await capture()
                            except Exception:
                                pass
                    if self.scoped_recovery_current(actor.account.account_id):
                        # The live account-scoped basis folded funding
                        # economics bit-exactly; the retained-history scan
                        # staying pending is not a drift signal. The next
                        # recovery re-folds and refuses on any change.
                        from . import store as _store
                        await self.store.write(lambda c: _store.register_event(c,
                            event_key=f"funding-scan-pending:{grid_id}:{scoped_event}",
                            scope=grid_id, event_type='funding_scan_pending_scoped',
                            payload={'code': scoped_event, 'global_coverage_advanced': False}))
                    else:
                        funding_error = AdapterError(scoped_event)
        for target_id in triggered:
            # The fill transaction already advanced epoch and disabled risk.
            # Release the pump lock before cancellation can drain more fills.
            await self.lifecycle._cancel_open_grid_orders(target_id, self.lifecycle._grid(target_id)['epoch'])
        if funding_error is not None:
            raise funding_error
        profit_grid = self.lifecycle._grid(grid_id)
        profit_eligible = (profit_grid['desired_mode']=='ACTIVE' and profit_grid['status'] in ('STARTING','RUNNING')) or (
            profit_grid['desired_mode']=='PAUSED' and profit_grid['status']=='PAUSED')
        if self.execution_mode == 'simulation' and profit_eligible and self.config(grid_id).profit_protection is not None:
            quote = await self.venue_for(actor.account.account_id, actor.market_key).market_snapshot()
            checked = self.profit_protection.evaluate(grid_id, {'mark': quote.mark, 'market_ts': quote.ts})
            if checked and checked['code'] in ('PROFIT_TAKE_REACHED', 'PROFIT_LOSS_REACHED'):
                await self.profit_protection.request_exit(grid_id, epoch=self.lifecycle._grid(grid_id)['epoch'], code=checked['code'])
        if dispatch:
            await self.dispatch_pending(grid_id)
        return applied

    async def step_market(self, grid_id: str, price: str, *,
                          fill_fraction: str = "1") -> int:
        """SIMULATION driver: move the fixture mark, then drain."""
        if self.execution_mode != "simulation":
            raise RuntimeError_("SIM_ONLY", "step_market is simulation",
                                status=403)
        actor = self.actors.get(grid_id)
        if actor is None:
            raise RuntimeError_("NOT_FOUND", grid_id, 404)
        venue = self.venue_for(actor.account.account_id, actor.market_key)
        venue.step(price, fill_fraction=fill_fraction)
        actor.mark = price
        return await self.pump_events(grid_id)

    # ----------------------------------------------------------- recovery
    async def on_stream_loss(self, account_id, *, cancel_orders=True):
        """Freeze all new risk before canceling owned orders; repeated gaps
        do not repeatedly bump epochs. Paused/closed grids remain paused/closed.
        """
        service_frozen=[]
        service_rows=self.store.read(lambda c:[dict(r) for r in c.execute(
            "SELECT g.grid_id,g.epoch,s.value FROM grids g JOIN runtime_state s ON s.key='service-coverage:'||g.grid_id "
            "WHERE g.account_id=? AND g.desired_mode='ACTIVE'",(account_id,))])
        for row in service_rows:
            if _loads(row['value'])['phase']=='rearming':
                if await self.protection.request_exit(row['grid_id'],expected_epoch=row['epoch'],
                        reason='PROTECTION_SERVICE_STREAM_LOSS') is not None:
                    service_frozen.append(row['grid_id'])
        def freeze(c):
            rows = c.execute("SELECT grid_id,epoch,status FROM grids WHERE account_id=? "
                "AND status IN ('RUNNING','STARTING') AND desired_mode='ACTIVE'", (account_id,)).fetchall()
            frozen = []
            for row in rows:
                starting = c.execute("SELECT 1 FROM operations WHERE grid_id=? AND op_type='start' "
                    "AND status='running' AND step IN ('','reconcile')", (row['grid_id'],)).fetchone()
                if row['status'] == 'STARTING' and starting:
                    # The first barrier is expected to begin with a gap. It
                    # proves history and stream generation before initialization;
                    # treating that initial gap as a new loss interrupts itself.
                    continue
                lock = self._recovery_locks.get(row['grid_id'])
                if lock is not None and lock.locked():
                    # An in-flight recovery owns this grid's epoch and is
                    # already reconciling the gap. Freezing it here would bump
                    # the epoch under its own activation, fail the rebuild with
                    # OPERATION_INTERRUPTED, and leave the gap flag (which only
                    # a SUCCESSFUL recovery clears) permanently set: the
                    # scheduler would then re-freeze on every tick forever
                    # (2026-11-04 r17c livelock: rebuild interrupted 9x, epoch
                    # climbing by 2 per attempt).
                    continue
                c.execute("UPDATE grids SET epoch=epoch+1,status='RECOVERING',updated_ts=? WHERE grid_id=?",
                          (models.utc_now_iso(), row["grid_id"]))
                # A failed proof must never roll back the account's stream
                # fence, including freezes for other markets in this batch.
                c.execute('SAVEPOINT stream_protection_carry')
                try:
                    config=self.service_coverage.transaction_config(c,row['grid_id'])
                    if config.protection and (config.protection.service_coverage
                            or self.protection_lease.backend(row['grid_id'],config) is not None):
                        venue=self.venue_for(config.account_ref,config.market_ref)
                        self.protection_lease.carry_recovery_epoch(c,row['grid_id'],previous_epoch=row['epoch'],
                            epoch=row['epoch']+1,now_ms=venue.clock_ms())
                except Exception:
                    c.execute('ROLLBACK TO stream_protection_carry')
                    store.register_event(c, event_key=f"stream-protection-carry-failed:{row['grid_id']}:{row['epoch']+1}",
                        scope=row['grid_id'], event_type='stream_protection_carry_failed',
                        payload={'epoch': row['epoch']+1})
                finally:
                    c.execute('RELEASE stream_protection_carry')
                store.register_event(c, event_key=f"stream-gap:{row['grid_id']}:{row['epoch']+1}", scope=row["grid_id"],
                    event_type="stream_recovery_required", payload={"account": account_id, "epoch": row["epoch"] + 1})
                frozen.append({'grid_id': row['grid_id'], 'epoch': row['epoch'] + 1})
            return frozen
        affected = await self.store.write(freeze)
        for frozen in affected:
            gid, epoch = frozen["grid_id"], frozen["epoch"]
            if self.lifecycle._grid(gid)["epoch"] != epoch:
                continue
            actor = self.actors.get(gid)
            if actor:
                actor.epoch = epoch
            await self.journal.expire_epoch(gid, epoch)
            if cancel_orders:
                await self.lifecycle._cancel_open_grid_orders(gid, epoch)
        return service_frozen+[r["grid_id"] for r in affected]

    async def recover(self, grid_id: str, *, restore=True, expected_epoch=None, rebuild=False,
                      clear_stream_gap=True) -> Dict[str, Any]:
        async with self._recovery_locks.setdefault(grid_id, asyncio.Lock()):
            grid = self.lifecycle._grid(grid_id)
            venue = self.venue_for(grid['account_id'], grid['market_key'])
            transport = getattr(venue, 'transport', None)
            if hasattr(transport, 'read_sequence'):
                # Recovery reconciles fills, orders, funding and position;
                # it does not consume an opening-margin snapshot on HL.
                weight = (200 if grid['desired_mode']=='STOPPED' and venue.venue=='hl'
                          and hasattr(venue,'position_snapshot') else 350)
                async with transport.read_sequence(weight):
                    return await self._recover(grid_id, restore=restore, expected_epoch=expected_epoch,
                                               rebuild=rebuild, clear_stream_gap=clear_stream_gap)
            return await self._recover(grid_id, restore=restore, expected_epoch=expected_epoch,
                                       rebuild=rebuild, clear_stream_gap=clear_stream_gap)

    async def _recover(self, grid_id: str, *, restore, expected_epoch, rebuild, clear_stream_gap) -> Dict[str, Any]:
        actor = self.actors.get(grid_id) or await self._load_actor(grid_id)
        def enter(c):
            row=dict(c.execute("SELECT status,epoch,desired_mode FROM grids WHERE grid_id=?",(grid_id,)).fetchone())
            if expected_epoch is not None and row["epoch"] != expected_epoch:
                raise RuntimeError_("OPERATION_INTERRUPTED","grid changed before reconciliation")
            c.execute("UPDATE grids SET status='RECOVERING',updated_ts=? WHERE grid_id=?",(models.utc_now_iso(),grid_id))
            return row
        before=await self.store.write(enter)
        previous=before["status"]
        venue = self.venue_for(actor.account.account_id, actor.market_key)
        stream_generation = getattr(venue, "stream_generation", None)
        # No replacement orders until the previous epoch's owned orders are
        # canceled and their late executions have been reconciled.
        if rebuild and previous in ("RECOVERING", "NEEDS_ATTENTION"):
            await self.lifecycle._cancel_open_grid_orders(grid_id, before["epoch"])
        async def reconcile():
            barrier = RecoveryBarrier(st=self.store, venue=venue,
                                      journal=self.journal,
                                      executor=self.executors[
                                          actor.account.account_id],
                                      actor=actor)
            if before['desired_mode']=='STOPPED' and venue.venue=='hl' and hasattr(venue,'position_snapshot'):
                report = await barrier.run(position_only=True)
            else:
                report = await barrier.run()
            if report["ok"] and stream_generation is not None:
                if not venue.stream_connected or venue.stream_generation != stream_generation:
                    report["ok"] = False
                    report["gaps"].append({"code": "STREAM_CHANGED_DURING_RECOVERY",
                        "detail": f"gen {stream_generation} -> {venue.stream_generation} "
                                  f"connected={venue.stream_connected} decoder={getattr(getattr(venue, '_stream', None), 'decoder', None) is not None}"})
            if report["ok"] and rebuild and await self.lifecycle._open_grid_order_count(grid_id):
                report["ok"] = False
                report["gaps"].append({"code": "OLD_ORDERS_NOT_TERMINAL"})
            try:
                # A callable, not an awaitable: recovery_read's 3-attempt
                # retry only engages for callables — an already-created
                # awaitable silently skipped every retry.
                await recovery_read(lambda: self.sync_funding(grid_id))
            except Exception as error:
                scoped_funded = (report.get("scoped_basis") is not None
                    and isinstance(error, AdapterError)
                    and str(error) in ("FUNDING_HISTORY_INCOMPLETE", "FUNDING_HISTORY_SCAN_PENDING")
                    and self.scoped_recovery_current(actor.account.account_id))
                if scoped_funded:
                    # The account-scoped basis folded booked funding economics
                    # bit-exactly into the native counters; the retained-
                    # history scan staying pending is not a drift signal. The
                    # next recovery re-folds and refuses on any change.
                    report["scoped_funding"] = True
                    # The refreshed basis is bound to the CURRENT stream era,
                    # so a mid-recovery reconnect it re-proved after no
                    # longer fails the recovery — remove exactly that gap
                    # and recompute ok from what remains.
                    report["gaps"] = [g for g in report["gaps"]
                                      if g.get("code") != "STREAM_CHANGED_DURING_RECOVERY"]
                    report["ok"] = not report["gaps"] and not report["drift"]
                else:
                    report["ok"] = False
                    report["gaps"].append({"code": "FUNDING_HISTORY_UNAVAILABLE", "detail": str(error)})
            return report
        import time
        started = time.monotonic()
        report = await reconcile()
        log.info('GRID_STREAM_RECOVERY_PHASE phase=reconcile elapsed_ms=%s outcome=%s',
                 int(max(0, time.monotonic()-started)*1000), 'verified' if report['ok'] else 'unconfirmed')

        def w(c):
            row = c.execute(
                "SELECT status, desired_mode,epoch FROM grids WHERE grid_id = ?",
                (grid_id,)).fetchone()
            # The proof may have changed while awaiting account/funding I/O.
            # Recheck in the same transaction that restores strategy state.
            if report['ok'] and self.journal.pending_fixed_terminals(c,grid_id,actor.account.account_id):
                report['ok'] = False
                report['gaps'].append({'code':'PROTECTION_CUMULATIVE_UNVERIFIED'})
            store.register_event(c, event_key='recovery-report:' + uuid.uuid4().hex,
                scope=grid_id, event_type='recovery_checked', payload={
                    'epoch': before['epoch'], 'previous_status': previous,
                    'epoch_matches': row['epoch'] == before['epoch'],
                    **audit_report(report)})
            if row["epoch"] != before["epoch"]:
                return False
            desired = row["desired_mode"]
            if report["ok"]:
                # recovery restores the DESIRED mode, never a blanket
                # RUNNING (a paused grid stays paused, spec §9.3 step 8)
                nxt = "RUNNING" if desired == "ACTIVE" else \
                    ("PAUSED" if desired == "PAUSED" else
                     ("STOPPED_WITH_POSITION" if desired == "STOPPED"
                      else row["status"]))
                if not restore or previous in ("READY", "CLOSED", "STOPPING", "STARTING"):
                    nxt = previous
                elif rebuild and previous in ('RECOVERING','NEEDS_ATTENTION') and desired=='ACTIVE':
                    # Do not expose RUNNING while fresh native proof and the
                    # rebuilt activation epoch are still being prepared.
                    nxt = 'RECOVERING'
                if row["status"] == "RECOVERING":
                    c.execute(
                        "UPDATE grids SET status = ?, updated_ts = ? WHERE "
                        "grid_id = ?", (nxt, models.utc_now_iso(), grid_id))
            else:
                c.execute(
                    "UPDATE grids SET status = 'NEEDS_ATTENTION', "
                    "updated_ts = ? WHERE grid_id = ?",
                    (models.utc_now_iso(), grid_id))
            return True
        applied = await self.store.write(w)
        if not applied:
            report["ok"] = False
            report["gaps"].append({"code": "RECOVERY_INTERRUPTED"})
        if applied and report["ok"] and stream_generation is not None and clear_stream_gap:
            # No await between this last connection check and clearing the
            # flag; adapter pre-send gates check it again after signing.
            if venue.stream_connected and venue.stream_generation == stream_generation:
                venue.stream_gap = False
            else:
                report["ok"] = False
                report["gaps"].append({"code": "STREAM_CHANGED_DURING_RECOVERY"})
                await self.lifecycle._set_status(grid_id, "NEEDS_ATTENTION", desired="PAUSED",
                    expected_epoch=before["epoch"], allowed_states=(previous, "RECOVERING"))
        if report["ok"] and rebuild and restore and previous in ("RECOVERING", "NEEDS_ATTENTION"):
            current = self.lifecycle._grid(grid_id)
            if current["desired_mode"] == "ACTIVE" and current["status"] == "RECOVERING":
                activation_epoch = None
                rebuild_started = time.monotonic()
                try:
                    native_protection = await self.protection_lease.resume_proofs(grid_id,
                        expected_epoch=before['epoch'],allowed_states=('RECOVERING',))
                    activation_epoch = await self.lifecycle._bump_epoch(grid_id, status="STARTING", desired="ACTIVE",
                        expected_epoch=before["epoch"], allowed_states=("RECOVERING",),native_protection=native_protection)
                    report["rebuild"] = await self.activate(grid_id, expected_epoch=activation_epoch)
                except Exception as error:
                    report["ok"] = False
                    report["gaps"].append({"code": "RECOVERY_REBUILD_UNCONFIRMED"})
                    report['rebuild_error'] = audit_rebuild_error(error)
                    report['rebuild_epoch'] = activation_epoch
                    if activation_epoch is not None:
                        await self.lifecycle._set_status(grid_id, "NEEDS_ATTENTION", desired="PAUSED",
                            expected_epoch=activation_epoch, allowed_states=("STARTING",))
                    def record_failure(c):
                        current = c.execute('SELECT epoch,status,desired_mode FROM grids WHERE grid_id=?',
                                            (grid_id,)).fetchone()
                        store.register_event(c, event_key='recovery-rebuild-failed:' + uuid.uuid4().hex,
                            scope=grid_id, event_type='recovery_rebuild_failed', payload={
                                'recovery_epoch': before['epoch'], 'activation_epoch': activation_epoch,
                                'observed_epoch': current['epoch'], 'status': current['status'],
                                'desired_mode': current['desired_mode'], **report['rebuild_error']})
                    await self.store.write(record_failure)
                finally:
                    log.info('GRID_STREAM_RECOVERY_PHASE phase=rebuild elapsed_ms=%s outcome=%s',
                             int(max(0, time.monotonic()-rebuild_started)*1000),
                             'verified' if report['ok'] else 'unconfirmed')
        return report

    async def sync_funding(self, grid_id: str, *, force=True):
        actor = self.actors[grid_id]
        try:
            return await self.funding.sync(account=actor.account.account_id, market=actor.market_key, force=force)
        except Exception as error:
            from .adapters.base import AdapterError
            venue=self.venue_for(actor.account.account_id,actor.market_key)
            if (isinstance(error,AdapterError) and str(error)=='FUNDING_HISTORY_INCOMPLETE'
                    and getattr(venue,'venue',None)=='bulk' and self.store.read(actor.inv.net_position)==0
                    and self.store.read(lambda c:c.execute('SELECT 1 FROM execution_owners WHERE grid_id=? LIMIT 1',(grid_id,)).fetchone())):
                try:
                    await self.funding.sync_applied_net(account=actor.account.account_id,market=actor.market_key,
                        grid_id=grid_id,force=False)
                except StoreError:raise
                except Exception:pass  # partial net evidence cannot clear the original history failure
            raise

    async def sync_funding_net(self, grid_id: str):
        """Publish independently matched lifecycle nets, not funding payments."""
        grid=self.lifecycle._grid(grid_id)
        return await self.funding.sync_applied_net(account=grid['account_id'],market=grid['market_key'],grid_id=grid_id)

    def scoped_recovery_current(self, account_id: str) -> bool:
        """The account's bulk scoped recovery basis is still live evidence.

        Bound to the same connected private-stream era, credential revision
        and baseline fingerprint it was proven under; anything else refuses."""
        venue = self.venues.get(account_id)
        check = getattr(venue, 'scoped_recovery_current', None)
        return bool(callable(check) and check())


    async def startup(self) -> None:
        await self.protection_lease.close_interrupted()
        rows = self.store.read(lambda c: [dict(r) for r in c.execute("SELECT * FROM grids")])
        deferred, failed_accounts = [], set()
        for row in rows:
            gid = row["grid_id"]
            try:
                await self._load_actor(gid)
                window=self.service_coverage.snapshot(gid)
                if (window and window['phase']=='rearming' and row['desired_mode']=='ACTIVE'
                        and row['status']!='CLOSED'):
                    if (row['status'] in ('STARTING','RUNNING','RECOVERING')
                            and self.protection_lease.carried_fixed_proof(gid)):
                        # Same carried-proof resume as close_interrupted: the
                        # window is still rearming, but the fixed stop was
                        # verified resting moments before the interruption.
                        await self.lifecycle._set_status(gid,'RECOVERING')
                        log.info('carried fixed-stop lease still valid; restart resumes into recovery: %s',gid)
                    else:
                        # A process interruption cannot turn an unprotected
                        # window into a fresh activation with a renewed budget.
                        await self.protection.request_exit(gid,expected_epoch=row['epoch'],
                            reason='PROTECTION_SERVICE_RESTART')
                        continue
                if row["status"] != "CLOSED":
                    pending = self.store.read(lambda c: c.execute(
                        "SELECT op_type FROM operations WHERE grid_id=? AND status IN ('running','queued')", (gid,)).fetchone())
                    if pending:
                        queued = self.store.read(lambda c: c.execute('SELECT value FROM runtime_state WHERE key=?',
                            ('protection-exit:' + gid,)).fetchone())
                        queued = _loads(queued[0]) if queued else {}
                        protective_closeout = (queued.get('phase') in ('pending', 'running', 'reconcile')
                                               and queued.get('expected_epoch') == row['epoch']) or pending[0] == 'stop_flat'
                        if pending[0] == 'stop_flat' and queued.get('phase') not in ('pending', 'running', 'reconcile'):
                            from .protection_exit import enqueue_exit
                            await self.store.write(lambda c: enqueue_exit(c, gid, row['epoch'], 'interrupted_stop_flat'))
                        await self.lifecycle._set_status(gid, "NEEDS_ATTENTION", desired="STOPPED" if protective_closeout else "PAUSED")
                        await self.store.write(lambda c: c.execute(
                            "UPDATE operations SET status='failed',error='interrupted; reconciliation required',ended_ts=? "
                            "WHERE grid_id=? AND status IN ('running','queued')", (models.utc_now_iso(), gid)))
                        if pending[0] == 'stop_flat':
                            await self.store.write(lambda c: c.execute(
                                "UPDATE order_intents SET state='EXPIRED_EPOCH' WHERE grid_id=? "
                                "AND cause_key LIKE 'exit-flat:%' AND state IN ('PLANNED','BLOCKED_SLOT')",
                                (gid,)))
                    multi = hasattr(self.venues[row["account_id"]], "market_venue")
                    if multi and row["status"] == "RECOVERING" and row["desired_mode"] == "ACTIVE":
                        await self.lifecycle._cancel_open_grid_orders(gid, row["epoch"])
                    report = await self.recover(gid, restore=not multi, clear_stream_gap=not multi)
                    if multi:
                        if report["ok"]:
                            deferred.append((row, pending))
                        else:
                            failed_accounts.add(row["account_id"])
                        continue
                    if report["ok"] and not pending and row["status"] == "RECOVERING" and row["desired_mode"] == "ACTIVE":
                        # A graceful shutdown canceled all orders. Rebuild only
                        # after history and the independent venue snapshot agree.
                        await self.lifecycle._bump_epoch(gid, status="STARTING", desired="ACTIVE")
                        await self.activate(gid)
            except Exception:
                log.exception("startup grid reconciliation failed: %s", gid)
                failed_accounts.add(row["account_id"])
                if row["status"] != "CLOSED":
                    await self.lifecycle._set_status(gid, "NEEDS_ATTENTION", desired="PAUSED")
        # A shared account must finish every market's ledger backfill before
        # any restarted grid sizes against the shared margin or places orders.
        for row, pending in deferred:
            gid = row["grid_id"]
            if row["account_id"] in failed_accounts:
                await self.lifecycle._set_status(gid, "NEEDS_ATTENTION", desired="PAUSED")
                continue
            try:
                report = await self.recover(gid)
                if report["ok"] and not pending and row["status"] == "RECOVERING" and row["desired_mode"] == "ACTIVE":
                    await self.lifecycle._bump_epoch(gid, status="STARTING", desired="ACTIVE")
                    await self.activate(gid)
            except Exception:
                log.exception("startup grid reconciliation failed: %s", gid)
                await self.lifecycle._set_status(gid, "NEEDS_ATTENTION", desired="PAUSED")

    # --------------------------------------------------------------- view
    def service_guardian_status(self):
        from .watchdog import guardian_health
        return guardian_health(self.heartbeat_file,self.store.path)

    def performance(self, grid_id: str, *, mark: Optional[str] = None) \
            -> Dict[str, Any]:
        """Unified ledger view (spec §12) from durable rows."""
        actor = self.actors.get(grid_id)
        if actor is None:
            raise RuntimeError_("NOT_FOUND", grid_id, 404)
        if mark is None:
            mark = actor.mark

        def q(c):
            own_transaction=not c.in_transaction
            if own_transaction:c.execute('BEGIN')
            try:
                execs = [dict(r) for r in c.execute(
                "SELECT e.* FROM executions e JOIN execution_owners o ON o.execution_key=e.execution_key "
                "WHERE o.grid_id=? ORDER BY e.exchange_ts,e.execution_key",
                (grid_id,)).fetchall()]
                allocs = [dict(r) for r in c.execute(
                "SELECT * FROM pair_allocations WHERE grid_id = ?",
                (grid_id,)).fetchall()]
                payments = [dict(r) for r in c.execute(
                    "SELECT p.* FROM funding_payments p JOIN funding_owners o ON o.payment_key=p.payment_key WHERE o.grid_id=?",(grid_id,))]
                from .funding_net import funding_view
                funding,net_metadata=funding_view(c,grid_id=grid_id,account=actor.account.account_id,market=actor.market_key,payments=payments)
                return execs,allocs,funding,net_metadata
            finally:
                if own_transaction:c.rollback()
        execs, allocs, funding, net_metadata = self.store.read(q)
        by_key = {e["execution_key"]: e for e in execs}
        if execs and all(e["source_seq"] and e["source_seq"].isdigit() for e in execs):
            execs.sort(key=lambda e: int(e["source_seq"]))
        perf = ledger.compute_performance(execs, mark=Decimal(mark), funding_net=funding)
        if net_metadata is not None:perf['funding_accounting']=net_metadata
        pairs = ledger.pair_net_from_allocations(allocs, by_key)
        perf.update(pairs)
        origins = self.store.read(lambda c: {r['execution_key']: r['cause_key'] for r in c.execute(
            'SELECT e.execution_key,i.cause_key FROM executions e JOIN execution_owners own ON own.execution_key=e.execution_key '
            'JOIN exchange_orders o ON o.account=e.account AND (o.client_id=e.order_client_id OR o.exchange_order_id=e.exchange_order_id) '
            'JOIN order_intents i ON i.intent_id=o.intent_id WHERE own.grid_id=? AND i.grid_id=?', (grid_id, grid_id))})
        grid_pairs = [allocation for allocation in allocs if all(
            origins.get(allocation[key], '').startswith(('initial:', 'refill:', 'initial-position:'))
            for key in ('buy_execution_key', 'sell_execution_key'))]
        grid_profit = ledger.pair_net_from_allocations(grid_pairs, by_key)
        perf.update(grid_pair_qty=grid_profit['pair_qty'], grid_pair_net=grid_profit['pair_net'])
        perf["reconciliation_status"] = "needs_attention" if self.lifecycle._grid(grid_id)["status"] in ("NEEDS_ATTENTION", "RECOVERING") else "booked"
        if self.funding.status(actor.account.account_id, actor.market_key).get("ok") is False:
            perf["reconciliation_status"] = "needs_attention"
        if net_metadata and net_metadata['ambiguous_payment_keys']:
            perf['reconciliation_status']='needs_attention'
        def roi_inputs(c):
            versions = [_loads(r[0])['capital']['total'] for r in c.execute(
                'SELECT config_json FROM grid_versions WHERE grid_id=? ORDER BY revision', (grid_id,))]
            additions = []
            for row in c.execute("SELECT payload_json FROM events WHERE scope=? AND event_type='capital_movement' ORDER BY seq", (grid_id,)):
                movement = _loads(row[0])
                if movement.get('kind') == 'add_buffer':
                    additions.append(movement['amount'])
            return versions, additions
        versions, additions = self.store.read(roi_inputs)
        perf.update(ledger.cumulative_allocation_roi(perf['net_pnl'], versions, additions))
        perf['roi_status'] = ('needs_attention' if perf['reconciliation_status']=='needs_attention'
                              else 'estimated' if Decimal(perf['net_qty_base']) != 0 else 'booked')
        return perf

    def metrics(self) -> Dict[str, Any]:
        """Health/metrics snapshot (spec §15). Amounts are per-asset; no
        cross-asset totals are invented."""
        def q(c):
            unknown = c.execute(
                "SELECT COUNT(*) AS n, MIN(created_ts) AS oldest FROM "
                "order_intents WHERE state = 'UNKNOWN'").fetchone()
            open_n = c.execute(
                "SELECT COUNT(*) AS n FROM order_intents WHERE state IN "
                "('PLANNED','DISPATCHING','ACCEPTED','OPEN','PARTIAL',"
                "'CANCEL_REQUESTED','CANCEL_PENDING')").fetchone()
            execs = c.execute(
                "SELECT COUNT(*) AS n, MAX(source_seq) AS last_seq FROM "
                "executions").fetchone()
            events = c.execute(
                "SELECT COUNT(*) AS n FROM events").fetchone()
            statuses = c.execute(
                "SELECT status, COUNT(*) AS n FROM grids GROUP BY status") \
                .fetchall()
            budgets = c.execute(
                "SELECT g.account_id,b.asset,b.reserved_amount FROM "
                "budget_reservations b JOIN grids g ON g.grid_id=b.grid_id").fetchall()
            totals, account_totals = {}, {}
            with localcontext() as context:
                context.prec = 50
                for row in budgets:
                    asset = row['asset']
                    totals[asset] = totals.get(asset, Decimal(0)) + Decimal(row['reserved_amount'])
                    key = (row['account_id'], asset)
                    account_totals[key] = account_totals.get(key, Decimal(0)) + Decimal(row['reserved_amount'])
            return {"unknown_intents": int(unknown["n"]),
                    "unknown_oldest_ts": unknown["oldest"],
                    "open_intents": int(open_n["n"]),
                    "executions": int(execs["n"]),
                    "last_fill_source_seq": execs["last_seq"],
                    "events": int(events["n"]),
                    "grids_by_status": {r["status"]: r["n"]
                                        for r in statuses},
                    "budget_reserved": [{"asset": asset, "total": models.canonical_decimal_text(total)}
                                        for asset, total in sorted(totals.items())],
                    "account_budget_reserved": [{"account_id": account, "asset": asset,
                                                "total": models.canonical_decimal_text(total)}
                                               for (account, asset), total in sorted(account_totals.items())],
                    "execution_mode": self.execution_mode,
                    "real_dispatch": (self.execution_mode == 'production'
                                      and os.getenv('GRID_REAL_DISPATCH', '0') == '1'
                                      and ((self.live_release is not None
                                            and callable(getattr(self.live_release, 'summary', None))
                                            and any(self.live_release.summary(account) for account in self.venues))
                                           or (self.operator_trial is not None
                                               and callable(getattr(self.operator_trial, 'armed', None))
                                               and self.operator_trial.active()
                                               and self.operator_trial.armed()))),
                    "ts": models.utc_now_iso()}
        return self.store.read(q)

    def grid_snapshot(self, grid_id: str) -> Dict[str, Any]:
        actor = self.actors.get(grid_id)
        if actor is None:
            raise RuntimeError_("NOT_FOUND", grid_id, 404)
        snap = self.store.read(actor.snapshot)
        row = self.store.read(lambda c: dict(
            c.execute("SELECT status, desired_mode, epoch, "
                      "active_revision FROM grids WHERE grid_id = ?",
                      (grid_id,)).fetchone()))
        snap.update(row)
        snap["config"] = self.config(grid_id).to_dict()
        snap["mark"] = actor.mark
        snap["budget"] = self.store.read(lambda c: dict(c.execute(
            "SELECT reserved_amount,asset FROM budget_reservations WHERE grid_id=?", (grid_id,)).fetchone()))
        snap["plan"] = self.store.read(lambda c: _loads(c.execute(
            "SELECT plan_json FROM grid_versions WHERE grid_id=? AND revision=COALESCE(?,1)",
            (grid_id, row["active_revision"])).fetchone()[0]))
        risk = self.store.read(lambda c: c.execute("SELECT value FROM runtime_state WHERE key=?", (f"risk-watch:{grid_id}",)).fetchone())
        snap["risk_watch"] = _loads(risk[0]) if risk else None
        first = self.store.read(lambda c: c.execute(
            'SELECT config_json FROM grid_versions WHERE grid_id=? ORDER BY revision LIMIT 1', (grid_id,)).fetchone())
        snap['profit_protection_denominator'] = _loads(first[0])['capital']['total'] if first else None
        range_watch = self.store.read(lambda c:c.execute('SELECT value FROM runtime_state WHERE key=?',
            ('range-watch:'+grid_id,)).fetchone())
        snap['range_watch'] = _loads(range_watch[0]) if range_watch else None
        protection_exit = self.store.read(lambda c: c.execute(
            'SELECT value FROM runtime_state WHERE key=?', ('protection-exit:'+grid_id,)).fetchone())
        exit_record = _loads(protection_exit[0]) if protection_exit else None
        snap['protection_exit'] = ({key: exit_record.get(key) for key in
            ('reason', 'phase', 'expected_epoch', 'code')} if exit_record else None)
        snap['service_coverage'] = self.service_coverage.snapshot(grid_id)
        return snap


def _dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _loads(s: str) -> Any:
    import json
    return json.loads(s)
