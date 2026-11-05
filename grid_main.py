"""GridRuntime service entry point (G14 entry, spec §15).

    .venv/bin/python grid_main.py --mode simulation --db data/grid-runtime-sim.sqlite3

- ``--mode`` decides execution_mode (server start config ONLY; no web or
  RPC call can change it).
- Simulation mode wires fixture venues without any credentials; production
  mode loads a separate encrypted grid vault and observed HL adapters.
  Credentials do not enable real dispatch: qualification is still pending
  (see LIVE-READINESS.md).
- Never touches the legacy console database; it is the ONLY writer of
  its own store file.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import signal
from pathlib import Path

import aiohttp

from entropy_arb.grid import store
from entropy_arb.grid.adapters.fixture import FixtureVenue
from entropy_arb.grid.rpc import RuntimeRpcServer
from entropy_arb.grid.runtime import GridRuntime, RuntimeError_
from entropy_arb.grid.sizing import DiscreteTableModel
from entropy_arb.grid.stream_recovery import StreamRecoveryScheduler
from entropy_arb.grid.live_risk import LiveRiskScheduler

log = logging.getLogger("grid.main")

# How often the progress sidecar re-publishes the heartbeat while one
# supervision step runs. Interval only; the publisher itself rate-limits
# harder (>=1s between writes).
PROGRESS_HEARTBEAT_S = 2.0


async def _progress_heartbeat(heartbeat, phase):
    """Keep the independent watchdog informed while one bounded step runs.

    The supervision loop publishes between ticks, but a single legitimate
    step can outlast the watchdog's staleness budget — paced bulk reads
    make recovery reconcile passes run tens of seconds — and the watchdog
    then SIGKILL-restarts a live, progressing worker mid-recovery
    (2026-11-05 22:41, g-551c9890: the fresh instance's close_interrupted
    protective-closed the grid). This sidecar re-publishes the same phase
    while the step awaits, so the watchdog measures what it exists for:
    whether the process can still run Python at all.

    It can only run while the event loop is responsive; a wedged loop or a
    synchronous hang blocks it too, so genuine stalls still go stale. The
    invariant this relies on: every awaited operation under the supervision
    loop (and under startup) is bounded by a read/lookup timeout — an
    unbounded await would keep this heartbeat beating forever, so no
    supervision-path await may be added without one.
    """
    try:
        while True:
            await asyncio.sleep(PROGRESS_HEARTBEAT_S)
            try:
                heartbeat.publish(phase)
            except (OSError, ValueError):
                log.warning('runtime heartbeat could not record progress')
    except asyncio.CancelledError:
        pass


async def _with_progress_heartbeat(heartbeat, phase, step):
    keeper = None
    if heartbeat is not None:
        keeper = asyncio.create_task(_progress_heartbeat(heartbeat, phase))
    try:
        return await step
    finally:
        if keeper is not None:
            keeper.cancel()
            await asyncio.gather(keeper, return_exceptions=True)


async def supervision_step(name, step, *, heartbeat=None):
    """One scheduler fault must not end the whole runtime process.

    Any exception escaping a tick used to kill the main loop; the watchdog
    then SIGKILL-restarted the worker and the fresh instance ran
    close_interrupted, which request_exit(PROTECTION_SERVICE_RESTART) every
    protected grid — so a single transient store error protective-closed
    every RUNNING grid at once. The schedulers isolate per-grid failures
    internally; this barrier is the last resort for faults they leak.
    CancelledError still propagates: graceful shutdown depends on it.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        return await _with_progress_heartbeat(heartbeat, 'running', step)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception('supervision step failed: %s', name)
        return None
    finally:
        elapsed = loop.time() - started
        if elapsed >= 1:
            log.warning('supervision step slow: %s elapsed_ms=%.0f', name, elapsed * 1000)

# test-only demand table for the SIM fixture market (never on real
# venues; see sizing.py)
SIM_RISK_ROWS = [
    {"k": k, "ordinary": str(10 * k), "adverse": str(13 * k),
     "account": str(13 * k)} for k in range(1, 11)
]


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="grid_main.py")
    ap.add_argument("--mode", choices=("simulation", "production"),
                    default="simulation")
    from entropy_arb.grid.venue_registry import VENUES
    ap.add_argument('--venue', choices=tuple(VENUES), default='hl',
                    help='production: one isolated venue worker; defaults to the existing HL deployment')
    ap.add_argument("--db", default=None)
    ap.add_argument("--socket", default=None)
    ap.add_argument("--fixture-mark", default="105",
                    help="simulation: initial mark of the fixture market")
    ap.add_argument("--fixture-symbol", default="BTC-USDC-SIM")
    ap.add_argument("--hl-account", help="production: explicit economic account address; read-only, no key loading")
    ap.add_argument("--hl-symbol", default="BTC", help="production read-only main perpetual symbol")
    ap.add_argument("--hl-network", choices=("mainnet", "testnet"), default="mainnet")
    ap.add_argument("--credentials-dir", help="production: private encrypted grid credential directory; default next to grid DB")
    ap.add_argument("--funding-allowance", default="0", help="production: cumulative USDC self-transfer cap from spot to main perpetuals; default disabled")
    ap.add_argument("--commissioning-allowance", default="0", help="production: aggregate mark notional for journaled BTC passive/protected-IOC certification; single order <=15 USDC, default disabled")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument('--grid-trial-policy', help='private operator-scoped native grid commissioning policy; does not qualify public trading')
    ap.add_argument('--grid-live-policy', help='private reviewed per-account/market release policy backed by completed native engine evidence')
    ap.add_argument('--heartbeat-file',help='private managed runtime heartbeat for the independent local watchdog')
    return ap


async def run(args: argparse.Namespace) -> int:
    venue_key = getattr(args, 'venue', 'hl')
    if venue_key != 'hl' and (args.hl_account or getattr(args, 'grid_live_policy', None)
            or getattr(args, 'funding_allowance', '0') != '0'
            or getattr(args, 'commissioning_allowance', '0') != '0'):
        print('refusing: non-HL workers allow observation plus a venue-scoped operator trial; HL policies and financial allowances cannot be reused', file=sys.stderr)
        return 2
    heartbeat=None
    if getattr(args,'heartbeat_file',None):
        from entropy_arb.grid.watchdog import RuntimeHeartbeat
        heartbeat=RuntimeHeartbeat(args.heartbeat_file,args.db)
        heartbeat.publish('starting',force=True)
    session = None
    vault = None
    accounts = None
    if args.mode == "simulation" and args.hl_account:
        print("refusing: real account observation requires production mode", file=sys.stderr)
        return 2
    if args.mode == "production":
        # Real adapters may be observed; audited live qualification is required
        # independently of registration and the global dispatch switch.
        rt = GridRuntime.open(args.db, execution_mode="production")
        rt.heartbeat_file=str(heartbeat.path) if heartbeat else None
        try:
            if heartbeat:await heartbeat.consume(rt,startup=True)
            await rt.protection_lease.close_interrupted()
            from entropy_arb.grid.accounts import GridAccounts
            from entropy_arb.grid.credentials import CredentialVault
            from entropy_arb.grid.adapters.hyperliquid import HyperliquidGridVenue, HyperliquidTransport
            from entropy_arb.grid.adapters.observation import observation_providers
            session = aiohttp.ClientSession()
            vault = CredentialVault(args.credentials_dir or (Path(args.db).parent / "grid-credentials"))
            accounts = GridAccounts(rt, vault, lambda network: HyperliquidTransport(session, network), observe_streams=True,
                                    funding_allowance=getattr(args, "funding_allowance", "0"),
                                    commissioning_allowance=getattr(args, "commissioning_allowance", "0"),
                                    venue_keys=(getattr(args, 'venue', 'hl'),), providers=observation_providers(session))
            failures = await accounts.restore()
            if failures:
                log.warning("%d grid account connections require re-verification", len(failures))
            if args.hl_account:
                venue = HyperliquidGridVenue(HyperliquidTransport(session, args.hl_network),
                    account_address=args.hl_account, symbol=args.hl_symbol, network=args.hl_network)
                if venue._account.account_id not in rt.venues:
                    await rt.add_venue(venue)
            log.info("production accounts loaded; validating market approvals before RPC startup")
        except BaseException:
            if accounts:
                await accounts.close()
            if session:
                await session.close()
            if vault:
                vault.close()
            rt.close()
            raise
    else:
        if "sim" not in args.db:
            print("refusing: simulation mode must use a dedicated "
                  "simulation store path (contains 'sim')",
                  file=sys.stderr)
            return 2
        rt = GridRuntime.open(args.db, execution_mode="simulation")
        try:
            venue = FixtureVenue(native_instrument_id=args.fixture_symbol,
                                 mark=args.fixture_mark,
                                 state_path=args.db + ".venue.json")
            venue.risk_model = DiscreteTableModel(rows=SIM_RISK_ROWS)
            rt.add_fixture_venue(venue)
        except BaseException:
            rt.close()
            raise
    rpc = RuntimeRpcServer(rt, args.socket, accounts=accounts)
    if args.mode == 'production':
        from entropy_arb.grid.live_release import LiveRelease
        rt.live_release = (LiveRelease.from_path(rt, args.grid_live_policy)
                           if getattr(args, 'grid_live_policy', None) else LiveRelease(rt))
        release_failures = await rt.live_release.restore()
        if release_failures:
            log.warning('%d live market approvals are unavailable', len(release_failures))
        log.info('live market approvals ready: %d; real dispatch switch: %s',
                 len(rt.live_release.verified), os.getenv('GRID_REAL_DISPATCH', '0'))
    if getattr(args, 'grid_trial_policy', None):
        from entropy_arb.grid.operator_trial import load_trial_policy
        rt.operator_trial = load_trial_policy(rt, args.grid_trial_policy, venue=venue_key)
        await rt.operator_trial.restore_history()
    elif rt.store.read(lambda c: c.execute("SELECT 1 FROM runtime_state WHERE key LIKE 'operator-trial:%' LIMIT 1").fetchone()):
        # A trial policy file missing at restart still cleans up durably:
        # without a matching policy hash every send is refused and open
        # grids receive exit requests.
        if venue_key == 'bulk':
            from entropy_arb.grid.operator_trial import BulkOperatorTrial
            rt.operator_trial = BulkOperatorTrial(rt, None)
        else:
            from entropy_arb.grid.operator_trial import OperatorTrial
            rt.operator_trial = OperatorTrial(rt, None)
        await rt.operator_trial.restore_history()
    if args.mode == 'production' and venue_key == 'bulk' and rt.operator_trial is not None \
            and getattr(rt.operator_trial, 'policy', None):
        from entropy_arb.grid.operator_trial import BulkOperatorTrial
        if not isinstance(rt.operator_trial, BulkOperatorTrial):
            print('refusing: the bulk worker requires a schema-2 bulk operator trial policy', file=sys.stderr)
            await accounts.close()
            if session:await session.close()
            if vault:vault.close()
            rt.close()
            return 2
        if os.getenv('GRID_REAL_DISPATCH', '0') != '1':
            print('refusing: arming the bulk operator trial requires GRID_REAL_DISPATCH=1', file=sys.stderr)
            await accounts.close()
            if session:await session.close()
            if vault:vault.close()
            rt.close()
            return 2
        try:
            from entropy_arb.grid.adapters.bulk_execution_factory import attach_execution_venue
            venue = await attach_execution_venue(rt, vault, rt.operator_trial, session,
                market_ref=rt.operator_trial.policy['markets'][0])
            accounts.start_stream(venue._account.account_id)
            if getattr(venue, 'risk_feed', None) is not None:
                await venue.risk_feed.start()
            # The shared bulk transport paces public reads to 1/s; a dispatch
            # risk observation (market + account + tiers under concurrent
            # readers) legitimately takes longer than the HL-tuned 5s/10s
            # bounds. Freshness is still enforced, just against this
            # deployment's real read latency.
            rt.live_risk.max_age = 15.0
            rt.live_risk.collection_timeout = 120.0
            # The protection proof deadline must cover the same paced-read
            # pipeline as the risk collection: an HL-tuned 30s window expired
            # mid-activation on bulk and let the watch kill the grid.
            import entropy_arb.grid.adapters.bulk_protection_manager as _bpm
            _bpm.PROOF_MAX_SECONDS = 120
            log.info('bulk execution venue armed under operator trial %s (expires %s)',
                     rt.operator_trial.policy_hash[:12], rt.operator_trial.policy['expires_at'])
        except BaseException:
            await accounts.close()
            if session:await session.close()
            if vault:vault.close()
            rt.close()
            raise
    try:
        # Startup legitimately runs long on paced venue reads (restore,
        # reconcile passes); the watchdog's 'starting' budget must measure a
        # wedged process, not slow honest progress.
        await _with_progress_heartbeat(heartbeat, 'starting', rt.startup())
        await rpc.start()
    except BaseException:
        await rpc.stop()
        if accounts:
            await accounts.close()
        if session:
            await session.close()
        if vault:
            vault.close()
        rt.close()
        raise
    stop = asyncio.Event()
    recovery = StreamRecoveryScheduler(rt)
    risk_watch = LiveRiskScheduler(rt)
    from entropy_arb.grid.protection_exit import ProtectionExitScheduler
    protection_exit = ProtectionExitScheduler(rt)
    from entropy_arb.grid.service_rearm import ServiceRearmScheduler
    service_rearm = ServiceRearmScheduler(rt)
    from entropy_arb.grid.protection_watch import ProtectionWatchScheduler
    protection_watch = ProtectionWatchScheduler(rt)
    from entropy_arb.grid.pump_scheduler import GridPumpScheduler
    pumps = GridPumpScheduler(rt)
    from entropy_arb.grid.range_start import RangeStartScheduler
    range_start = RangeStartScheduler(rt)
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    def request_shutdown():
        stop.set()
        main_task.cancel()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, request_shutdown)
    if heartbeat:heartbeat.publish('running',force=True)
    try:
        while not stop.is_set():
            if heartbeat:await heartbeat.consume(rt)
            if accounts:
                accounts.drain_unused_observations()
            await supervision_step('stream_recovery', recovery.tick(), heartbeat=heartbeat)
            await supervision_step('native_protection', protection_watch.tick(), heartbeat=heartbeat)
            await supervision_step('service_rearm', service_rearm.tick(), heartbeat=heartbeat)
            await supervision_step('risk', risk_watch.tick(), heartbeat=heartbeat)
            if rt.operator_trial is not None:
                await supervision_step('trial_policy', rt.operator_trial.expire(), heartbeat=heartbeat)
            await supervision_step('protection_exit', protection_exit.tick(), heartbeat=heartbeat)
            await supervision_step('event_pumps', pumps.tick(), heartbeat=heartbeat)
            await supervision_step('range_start', range_start.tick(), heartbeat=heartbeat)
            if heartbeat:heartbeat.publish('running')
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except TimeoutError:
                pass
    except asyncio.CancelledError:
        pass
    finally:
        if heartbeat:
            try:heartbeat.publish('stopping',force=True)
            except (OSError,ValueError):log.warning('runtime heartbeat could not record shutdown')
        try:
            await pumps.close()
            await range_start.close()
            await recovery.close()
            await risk_watch.close()
            await protection_watch.close()
            await service_rearm.close()
            await protection_exit.close()
            await rpc.stop()
            for gid in list(rt.actors):
                if rt.lifecycle._grid(gid)["status"] in ("RUNNING", "STARTING"):
                    await rt.lifecycle._set_status(gid, "RECOVERING")
                    await rt.lifecycle._cancel_open_grid_orders(gid, rt.actors[gid].epoch)
                    await rt.pump_events(gid)
        finally:
            if accounts:
                await accounts.close()
            if session:
                await session.close()
            if vault:
                vault.close()
            rt.close()
    return 0


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    worker_root = Path('data/grid-workers') / args.venue
    args.db = args.db or ("data/grid-runtime-sim.sqlite3" if args.mode == "simulation"
                          else "data/grid-runtime.sqlite3" if args.venue == 'hl'
                          else str(worker_root / 'grid-runtime.sqlite3'))
    args.socket = args.socket or ('data/run/grid-runtime.sock' if args.venue == 'hl'
                                  else str(worker_root / 'run/grid-runtime.sock'))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
