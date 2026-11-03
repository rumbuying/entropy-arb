"""Native close envelopes and unknown handling with in-memory exchange I/O."""
import asyncio
from decimal import Decimal

import pytest

from entropy_arb.grid.lifecycle import LifecycleError
from entropy_arb.grid.runtime import GridRuntime
from test_grid_hl_owned_risk import seeded_inventory, enable_offline_exchange
from test_grid_hyperliquid import raw_fill, raw_order, OWNER, OID, TID


@pytest.mark.parametrize('entrypoint', ['lifecycle', 'rpc'])
@pytest.mark.parametrize('stream_fill', [True, False])
def test_native_stop_uses_fresh_quote_and_closes_only_after_owned_fill_and_native_zero(tmp_path, monkeypatch, entrypoint, stream_fill):
    monkeypatch.setenv("GRID_REAL_DISPATCH", "1")
    async def run():
        rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
        try:
            gid, cfg, venue, transport, signer, _, qty = await seeded_inventory(rt)
            mock = await enable_offline_exchange(rt, venue, transport)
            actor = rt.actors[gid]
            actor.mark = "1"  # never use the old actor mark for a close
            original = mock.post
            async def post(path, payload):
                if path == "/exchange" and payload["action"]["type"] == "order":
                    action = payload["action"]
                    mock.exchange_actions.append(action)
                    wire = action["orders"][0]
                    assert wire["r"] is True and wire["t"] == {"limit": {"tif": "Ioc"}} and not wire["b"]
                    assert Decimal(wire["p"]) == Decimal("99701")  # fresh 100000.1, 30 bps, rounding keeps the user's floor
                    assert wire["s"] == qty
                    fill = raw_fill(cloid=wire["c"], oid=OID + 500, tid=TID + 1, side="A", sz=qty, px="100000", fee="0.05", time=30000)
                    mock.fill_rows.append(fill)
                    mock.qty = "0"
                    if stream_fill:
                        venue.ingest_message({"channel": "userFills", "data": {"user": OWNER, "fills": [fill]}})
                    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": OID + 500, "totalSz": qty, "avgPx": "100000"}}]}}}
                return await original(path, payload)
            mock.post = post
            previews = rt.store.read(lambda c: c.execute("SELECT COUNT(*) FROM previews").fetchone()[0])
            if entrypoint == 'rpc':
                from entropy_arb.grid.rpc import RuntimeRpcServer
                async def unwanted_reload(*args, **kwargs):
                    pytest.fail('stop waited for an optional actor/UI quote refresh')
                rt._load_actor = unwanted_reload
                result = await RuntimeRpcServer(rt, str(tmp_path / 'unused.sock'))._m_stop_flat(
                    {'grid_id': gid, 'request_id': 'native-flat'})
            else:
                result = await rt.lifecycle.stop_flat(gid, actor=actor, request_id="native-flat")
            assert result["status"] == "succeeded" and result["remaining"] == "0"
            snapshot = rt.grid_snapshot(gid)
            assert snapshot["status"] == "CLOSED" and snapshot["budget"]["reserved_amount"] == "0"
            assert rt.store.read(actor.inv.net_position) == 0 and mock.qty == "0" and not mock.order_rows
            assert len(mock.exchange_actions) == 1
            assert snapshot["risk_watch"] is None and not rt.live_risk.permits
            assert rt.store.read(lambda c: c.execute("SELECT COUNT(*) FROM previews").fetchone()[0]) == previews
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["reject", "unknown", "stale", "price_unavailable"])
def test_native_stop_retains_inventory_claim_and_does_not_repeat_uncertain_or_rejected_ioc(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("GRID_REAL_DISPATCH", "1")
    async def run():
        rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
        try:
            gid, cfg, venue, transport, signer, _, qty = await seeded_inventory(rt)
            mock = await enable_offline_exchange(rt, venue, transport)
            original = mock.post
            async def post(path, payload):
                if payload.get("type") == "l2Book" and failure == "price_unavailable": raise OSError("offline price outage")
                if payload.get("type") == "l2Book" and failure == "stale":
                    data = await original(path, payload)
                    data["time"] = 10000
                    return data
                if path == "/exchange" and payload["action"]["type"] == "order":
                    mock.exchange_actions.append(payload["action"])
                    if failure == "unknown": raise OSError("offline lost acknowledgement")
                    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "offline rule rejection"}]}}}
                return await original(path, payload)
            mock.post = post
            if failure in ("stale", "price_unavailable"):
                with pytest.raises(LifecycleError, match="EXIT_PRICE_STALE" if failure == "stale" else "EXIT_OBSERVATION_UNAVAILABLE"):
                    await rt.lifecycle.stop_flat(gid, actor=rt.actors[gid], request_id="native-stop-failed")
            else:
                result = await rt.lifecycle.stop_flat(gid, actor=rt.actors[gid], request_id="native-stop-partial")
                assert result["status"] == "partial" and Decimal(result["remaining"]) == Decimal(qty)
            assert len(mock.exchange_actions) == (0 if failure in ("stale", "price_unavailable") else 1)
            snapshot = rt.grid_snapshot(gid)
            assert snapshot["status"] == "NEEDS_ATTENTION" and snapshot["desired_mode"] == "STOPPED"
            assert snapshot["budget"]["reserved_amount"] == "80" and Decimal(snapshot["net_position"]) == Decimal(qty)
            assert rt.store.read(lambda c: c.execute("SELECT state FROM scope_claims WHERE scope_key=?", (f"market:{cfg.account_ref}:{cfg.market_ref}",)).fetchone()[0]) != "released"
            assert not rt.store.read(lambda c: c.execute("SELECT 1 FROM operations WHERE grid_id=? AND status IN ('queued','running')", (gid,)).fetchone())
            if failure == "unknown": assert rt.journal.intents_for_grid(gid, states=["UNKNOWN"])
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure', [None, 'small_reject', 'small_trigger', 'small_trigger_history', 'stage_history_gap'])
def test_private_residual_stop_books_native_history_and_never_repeats_the_stage(tmp_path, monkeypatch, failure):
    import json
    from types import SimpleNamespace
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'probe.db'), execution_mode='production')
        try:
            gid, cfg, venue, transport, signer, _, qty = await seeded_inventory(rt)
            mock = await enable_offline_exchange(rt, venue, transport)
            # Offline operator authority; the real policy/account scope is
            # separately exercised in test_grid_operator_trial.
            rt.operator_trial = SimpleNamespace(permits_small_close=lambda g: g == gid,
                permits_intent=lambda i: False, permits_grid=lambda g, **kw: g == gid)
            original = mock.post
            step = Decimal(venue.rules.qty_step)
            suppress_history = failure == 'stage_history_gap'
            reject_small = failure in ('small_reject', 'small_trigger', 'small_trigger_history')
            async def post(path, payload):
                nonlocal reject_small
                if (payload.get('type') in ('userFills', 'userFillsByTime')
                        and suppress_history and len(mock.fill_rows) > 1):
                    rows = await original(path, payload)
                    return [row for row in rows if row['tid'] == TID]
                if path == '/exchange' and payload['action']['type'] == 'order':
                    action = payload['action'];mock.exchange_actions.append(action)
                    wire = action['orders'][0];size = Decimal(wire['s'])
                    if action['grouping'] == 'positionTpsl':
                        assert size == 0 and wire['r'] is True
                        row = raw_order(cloid=wire['c'], oid=OID+900, side='A', sz='0', origSz='0',
                            limitPx=wire['p'], reduceOnly=True)
                        row.update(isPositionTpsl=True,isTrigger=True,orderType='Stop Market',
                            triggerPx=wire['t']['trigger']['triggerPx'])
                        mock.order_rows.append(row)
                        return {'status':'ok','response':{'type':'order','data':{'statuses':['waitingForTrigger']}}}
                    assert wire['r'] and wire['t'] == {'limit': {'tif': 'Ioc'}}
                    if reject_small and size == step:
                        reject_small = False
                        return {'status':'ok','response':{'type':'order','data':{'statuses':[{'error':'Order must have minimum value of $10.'}]}}}
                    n = len(mock.exchange_actions)
                    fill = raw_fill(cloid=wire['c'], oid=OID+500+n, tid=TID+n, side='A',
                        sz=wire['s'], px='100000', fee='0', time=30000)
                    mock.fill_rows.append(fill);mock.qty = str(Decimal(mock.qty)-size)
                    # Deliberately no stream notification: actual history
                    # must book the first stage before sizing the residual.
                    return {'status':'ok','response':{'type':'order','data':{'statuses':[{'filled':
                        {'oid':OID+500+n,'totalSz':wire['s'],'avgPx':'100000'}}]}}}
                return await original(path, payload)
            mock.post = post
            result = await rt.lifecycle.stop_flat(gid, actor=rt.actors[gid], request_id='private-probe', _residual_probe=True)
            stage = rt.store.read(lambda c: json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',
                ('small-close-probe:'+gid,)).fetchone()[0]))
            assert stage['phase'] == 'attempted'
            assert Decimal(mock.exchange_actions[0]['orders'][0]['s']) == Decimal(qty)-step
            if failure is None:
                assert result['status'] == 'succeeded' and mock.qty == '0.00000'
                assert [Decimal(a['orders'][0]['s']) for a in mock.exchange_actions] == [Decimal(qty)-step,step]
            else:
                assert result['status'] == 'partial' and rt.lifecycle._grid(gid)['status'] == 'NEEDS_ATTENTION'
                assert Decimal(mock.qty) == step
                assert len(mock.exchange_actions) == (1 if failure == 'stage_history_gap' else 3)
                if failure in ('small_reject', 'small_trigger', 'small_trigger_history'):
                    assert len(mock.order_rows) == 1 and mock.order_rows[0]['isPositionTpsl']
                    from entropy_arb.grid.protection_exit import ProtectionExitScheduler
                    def queue_record():
                        return rt.store.read(lambda c: json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',
                            ('protection-exit:'+gid,)).fetchone()[0]))
                    before = len(mock.exchange_actions)
                    await ProtectionExitScheduler(rt).run(queue_record())
                    assert len(mock.exchange_actions) == before  # waiting native stop is queried, never duplicated
                suppress_history = False
                if failure in ('small_trigger', 'small_trigger_history'):
                    from entropy_arb.grid.protection_exit import ProtectionExitScheduler
                    order = mock.order_rows[0]
                    fill = raw_fill(cloid=order['cloid'],oid=order['oid'],tid=TID+10,
                        side='A',sz=str(step),px='99999',fee='0',time=30000)
                    mock.fill_rows.append(fill);mock.qty='0';mock.order_rows=[]
                    mock.closed_orders[order['cloid']]=order
                    if failure == 'small_trigger':
                        venue.ingest_message({'channel':'userFills','data':{'user':OWNER,'fills':[fill]}})
                        await rt.pump_events(gid)
                        assert rt.store.read(rt.actors[gid].inv.net_position) == 0
                    scheduler = ProtectionExitScheduler(rt)
                    await scheduler.run(queue_record())
                    if rt.lifecycle._grid(gid)['status'] != 'CLOSED':
                        await scheduler.run(queue_record())
                    assert rt.lifecycle._grid(gid)['status']=='CLOSED' and not mock.order_rows
                else:
                    retried = await rt.lifecycle.stop_flat(gid, actor=rt.actors[gid], request_id='cleanup-after-probe')
                    assert retried['status'] == 'succeeded'
                # The persistent stage is single-use even under a new stop
                # epoch. Only the actual residual is sent during cleanup.
                order_actions = [a for a in mock.exchange_actions if a['type'] == 'order']
                assert Decimal(order_actions[-1]['orders'][0]['s']) == (0 if failure.startswith('small_trigger') else step)
                assert sum(Decimal(a['orders'][0]['s']) > step for a in order_actions) == 1
            assert rt.store.read(rt.actors[gid].inv.net_position) == 0
        finally:
            rt.close()
    asyncio.run(run())


def test_private_residual_stop_requires_policy_before_changing_grid_or_sending(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'disabled-probe.db'), execution_mode='production')
        try:
            gid, cfg, venue, transport, signer, _, qty = await seeded_inventory(rt)
            before = rt.lifecycle._grid(gid)
            with pytest.raises(LifecycleError, match='TRIAL_SMALL_CLOSE_DISABLED'):
                await rt.lifecycle.stop_flat(gid, actor=rt.actors[gid], request_id='not-authorized', _residual_probe=True)
            assert rt.lifecycle._grid(gid) == before and not signer.actions
        finally:
            rt.close()
    asyncio.run(run())
