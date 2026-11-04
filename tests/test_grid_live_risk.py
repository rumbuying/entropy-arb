"""Native-shaped transport and signer doubles: no network or real secrets."""
import asyncio
import json
from decimal import Decimal

import pytest

from entropy_arb.grid.live_risk import LiveRiskScheduler
from entropy_arb.grid.runtime import GridRuntime
from test_grid_hl_owned_risk import seeded_inventory, enable_offline_exchange
from test_grid_hyperliquid import raw_order, raw_fill, OWNER


async def running(rt):
    gid, cfg, venue, transport, signer, _, qty = await seeded_inventory(rt)
    mock = await enable_offline_exchange(rt, venue, transport)
    await rt.lifecycle.resume(gid, actor=rt.actors[gid], request_id="risk-resume")
    assert rt.lifecycle._grid(gid)["status"] == "RUNNING"
    return gid, cfg, venue, mock, signer, qty


async def refill(rt, gid, qty):
    return await rt.journal.create_intent(grid_id=gid, cause_key="offline-risk-refill", revision=1,
        level_index=2, side="buy", qty_base=qty, price="98000", policy="post_only",
        reduce_only=False, epoch=rt.lifecycle._grid(gid)["epoch"])


def orders_sent(mock):
    return len([action for action in mock.exchange_actions if action["type"] == "order"])


@pytest.mark.parametrize('boundary', ['journal', 'signed'])
@pytest.mark.parametrize('next_unsafe', [False, True])
def test_booked_funding_at_send_boundary_requires_fresh_risk_without_sending(tmp_path, monkeypatch, boundary, next_unsafe):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path/'funding-race.db'), execution_mode='production')
        try:
            gid,cfg,venue,mock,signer,qty = await running(rt)
            intent = await refill(rt,gid,qty)
            original_transition, original_sign = rt.journal.transition, signer.sign
            inserted = False
            async def funding():
                nonlocal inserted
                if inserted:return
                inserted = True
                def write(c):
                    c.execute('INSERT INTO funding_payments VALUES(?,?,?,?,?,?,?,?)',
                        ('funding-at-send',cfg.account_ref,cfg.market_ref,'0.001','USDC',
                         '1970-01-01T00:00:30Z','30','venue_history'))
                    c.execute('INSERT INTO funding_owners VALUES(?,?)',('funding-at-send',gid))
                await rt.store.write(write)
            async def transition(iid,state,**kwargs):
                result = await original_transition(iid,state,**kwargs)
                if iid==intent['intent_id'] and state=='DISPATCHING':await funding()
                return result
            async def sign(action,expiry):
                result = await original_sign(action,expiry)
                await funding()
                return result
            if boundary=='journal':rt.journal.transition=transition
            else:signer.sign=sign
            before=orders_sent(mock);epoch=intent['epoch']
            result=await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'],epoch=epoch)
            assert inserted and result['outcome']=='DEFERRED' and result['known_not_sent']
            assert result['reject_code']=='RISK_FUNDING_CHANGED_WAIT'
            assert orders_sent(mock)==before and not rt.live_risk.permits
            current=rt.journal.get_intent(intent['intent_id']);cid=current['client_id']
            assert current['state']=='PLANNED' and cid
            assert rt.lifecycle._grid(gid)['status']=='RUNNING' and rt.lifecycle._grid(gid)['epoch']==epoch
            rt.journal.transition,signer.sign=original_transition,original_sign
            if next_unsafe:mock.equity='1'
            result=await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'],epoch=epoch)
            assert rt.journal.get_intent(intent['intent_id'])['client_id']==cid
            if next_unsafe:
                assert result['outcome']=='BLOCKED' and orders_sent(mock)==before
                assert rt.lifecycle._grid(gid)['desired_mode']=='PAUSED'
            else:
                assert result['outcome']=='ACCEPTED' and orders_sent(mock)==before+1
        finally:rt.close()
    asyncio.run(run())


def test_routine_risk_observation_leaves_native_stop_read_allowance(tmp_path,monkeypatch):
    from entropy_arb.grid.hl_rate import ReadBudget
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'priority.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,signer,qty=await running(rt)
            budget=ReadBudget(clock=lambda:0);await budget.acquire(500,critical=True)
            mock.read_sequence=lambda weight=350:budget.read_sequence(weight)
            mock.public_read_sequence=lambda weight=160:budget.read_sequence(weight,critical=False)
            mock.read_capacity_available=lambda weight=350:budget.capacity_available(weight)
            mock.public_read_capacity_available=lambda weight=350:budget.capacity_available(weight,critical=False)
            seen=[]
            async def observe(*args,**kwargs):seen.append(True);return {'code':None}
            rt.live_risk._observe=observe
            result=await asyncio.wait_for(rt.live_risk.observe(gid),.2)
            assert result['code']=='RISK_API_BUDGET_WAIT' and not seen
            assert budget.capacity_available(160,critical=True)
            assert not rt.live_risk.permits
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('next_failure',[None,'position','budget'])
def test_owned_fill_during_native_snapshot_defers_without_signing_then_requires_stable_match(tmp_path,monkeypatch,next_failure):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'race.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,signer,qty=await running(rt)
            intent=await refill(rt,gid,qty)
            original=mock.post;changed=False
            target=next(row for row in mock.order_rows if row['side']=='B' and not row['reduceOnly'])
            async def post(path,payload):
                nonlocal changed
                data=await original(path,payload)
                if payload.get('type')=='clearinghouseState' and not changed:
                    changed=True
                    raw=raw_fill(cloid=target['cloid'],oid=target['oid'],tid=9988123,time=30000,
                        sz=target['sz'],px=target['limitPx'],fee='0')
                    assert (await rt.actors[gid].apply_fill(venue.normalize_fill(raw,source='stream')))['outcome']=='applied'
                    mock.qty=str(Decimal(mock.qty)+Decimal(target['sz']))
                    mock.fill_rows.append(raw);mock.order_rows.remove(target)
                    # Return the snapshot captured immediately before that
                    # owned fill, as can happen with a concurrent WS event.
                return data
            mock.post=post
            before,signed=orders_sent(mock),len(signer.actions)
            result=await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'],epoch=intent['epoch'])
            assert changed and result['outcome']=='DEFERRED'
            assert result['reject_code']=='RISK_OBSERVATION_CHANGED_WAIT'
            assert orders_sent(mock)==before and len(signer.actions)==signed
            assert rt.journal.get_intent(intent['intent_id'])['state']=='PLANNED'
            assert rt.lifecycle._grid(gid)['status']=='RUNNING' and not rt.live_risk.permits
            assert rt.grid_snapshot(gid)['risk_watch']['owned_execution_changed_during_collection']
            mock.post=original
            if next_failure=='position':mock.qty=str(Decimal(mock.qty)+Decimal('.00001'))
            if next_failure=='budget':mock.equity='1'
            retry=await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'],epoch=intent['epoch'])
            if next_failure is None:
                assert retry['outcome']=='ACCEPTED' and orders_sent(mock)==before+1
            else:
                assert retry['outcome']=='BLOCKED' and orders_sent(mock)==before
                assert rt.lifecycle._grid(gid)['desired_mode']=='PAUSED'
                expected='RISK_HL_OWNED_POSITION_MISMATCH' if next_failure=='position' else 'RISK_BUDGET_EXCEEDED'
                assert retry['reject_code']==expected
        finally:rt.close()
    asyncio.run(run())


def test_healthy_native_refill_observes_fixed_size_without_creating_preview_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_REAL_DISPATCH", "1")
    async def run():
        rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            count = rt.store.read(lambda c: c.execute("SELECT COUNT(*) FROM previews").fetchone()[0])
            intent = await refill(rt, gid, qty)
            result = await rt.executors[cfg.account_ref].submit_intent(intent["intent_id"], epoch=intent["epoch"])
            assert result["outcome"] == "ACCEPTED"
            assert rt.store.read(lambda c: c.execute("SELECT COUNT(*) FROM previews").fetchone()[0]) == count
            assert rt.grid_snapshot(gid)["risk_watch"]["code"] is None
            assert rt.grid_snapshot(gid)["risk_watch"]["qty_per_grid"] == qty
            assert not rt.live_risk.permits
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["equity", "funding", "foreign_order", "position", "outside_range", "oversize", "stale_quote"])
def test_changed_risk_blocks_refill_and_cancels_owned_orders_before_new_risk(tmp_path, monkeypatch, change):
    monkeypatch.setenv("GRID_REAL_DISPATCH", "1")
    async def run():
        rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            before = orders_sent(mock)
            intent = await refill(rt, gid, str(Decimal(qty) * 20) if change == "oversize" else qty)
            original = mock.post
            if change == "equity": mock.equity = "1"
            if change == "funding":
                mock.funding_rows = [{"time": 29000, "hash": "0xrisk-loss", "delta": {"type": "funding", "coin": "BTC", "usdc": "-10"}}]
                assert (await rt.sync_funding(gid, force=True))["ok"]
            if change == "foreign_order": mock.order_rows.append(raw_order(cloid="0x" + "fe" * 16, oid=123))
            if change == "position": mock.qty = str(Decimal(qty) + Decimal("0.00001"))
            if change in ("outside_range", "stale_quote"):
                async def changed(path, payload):
                    data = await original(path, payload)
                    if payload.get("type") == "metaAndAssetCtxs" and change == "outside_range": data[1][0]["markPx"] = "150000"
                    if payload.get("type") == "l2Book" and change == "stale_quote": data["time"] = 10000
                    return data
                mock.post = changed
            result = await rt.executors[cfg.account_ref].submit_intent(intent["intent_id"], epoch=intent["epoch"])
            assert result["outcome"] == "BLOCKED" and result["risk_blocked"]
            assert orders_sent(mock) == before
            grid = rt.lifecycle._grid(gid)
            assert (grid["status"], grid["desired_mode"]) == ("NEEDS_ATTENTION", "PAUSED")
            assert grid["epoch"] == intent["epoch"] + 1
            assert rt.store.read(rt.actors[gid].inv.net_position) == Decimal(qty)
            assert not [order for order in mock.order_rows if order["oid"] != 123]
            if change == "foreign_order": assert [order["oid"] for order in mock.order_rows] == [123]
            assert rt.store.read(lambda c: c.execute("SELECT COUNT(*) FROM events WHERE scope=? AND event_type='risk_paused'", (gid,)).fetchone()[0]) == 1
            event = json.loads(rt.store.read(lambda c:c.execute(
                "SELECT payload_json FROM events WHERE scope=? AND event_type='risk_paused'", (gid,)).fetchone()[0]))
            assert event['risk_observation']['code'] == result['reject_code']
            assert event['risk_observation']['epoch'] == intent['epoch']
            # Later scheduler projections cannot erase the original refusal.
            await rt.live_risk.record(gid, {'code':'RISK_GRID_CHANGED','epoch':grid['epoch']})
            persisted = json.loads(rt.store.read(lambda c:c.execute(
                "SELECT payload_json FROM events WHERE scope=? AND event_type='risk_paused'", (gid,)).fetchone()[0]))
            assert persisted == event
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["budget", "fill_event", "order_update", "pause", "timeout"])
def test_signing_race_is_rechecked_before_exchange_post(tmp_path, monkeypatch, change):
    monkeypatch.setenv("GRID_REAL_DISPATCH", "1")
    async def run():
        rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            before = orders_sent(mock)
            intent = await refill(rt, gid, qty)
            original_sign = signer.sign
            now = [0.0]
            rt.live_risk.clock = lambda: now[0]
            async def sign(action, expiry):
                payload = await original_sign(action, expiry)
                if action["type"] == "order":
                    if change == "budget":
                        await rt.store.write(lambda c: c.execute("UPDATE budget_reservations SET reserved_amount='1' WHERE grid_id=?", (gid,)))
                    if change == "fill_event":
                        venue.ingest_message({"channel": "userFills", "data": {"user": OWNER, "fills": [mock.fill_rows[0]]}})
                    if change == "order_update":
                        venue.ingest_message({"channel": "orderUpdates", "data": [{"order": raw_order()}]})
                    if change == "pause":
                        await rt.lifecycle._bump_epoch(gid, status="PAUSED", desired="PAUSED")
                    if change == "timeout": now[0] = 6.0
                return payload
            signer.sign = sign
            result = await rt.executors[cfg.account_ref].submit_intent(intent["intent_id"], epoch=intent["epoch"])
            if change in ("fill_event", "order_update", "timeout"):
                assert result["outcome"] == "DEFERRED" and result["known_not_sent"]
                assert orders_sent(mock) == before
                assert rt.journal.get_intent(intent["intent_id"])["state"] == "PLANNED"
                assert rt.lifecycle._grid(gid)["status"] == "RUNNING"
                assert not rt.live_risk.permits
                client_id = rt.journal.get_intent(intent["intent_id"])["client_id"]
                signer.sign = original_sign
                await rt.pump_events(gid, dispatch=False)
                retried = await rt.executors[cfg.account_ref].submit_intent(intent["intent_id"], epoch=intent["epoch"])
                assert retried["outcome"] == "ACCEPTED"
                assert retried["client_id"] == client_id and orders_sent(mock) == before + 1
                return
            assert result["outcome"] == "REJECTED"
            assert orders_sent(mock) == before
            grid = rt.lifecycle._grid(gid)
            assert grid["desired_mode"] == "PAUSED" and grid["epoch"] == intent["epoch"] + 1
            if change == "pause": assert grid["status"] == "PAUSED"
            else: assert grid["status"] == "NEEDS_ATTENTION" and not mock.order_rows
            assert not rt.live_risk.permits
        finally:
            rt.close()
    asyncio.run(run())


def test_queued_fill_defers_without_expiring_or_signing_the_refill(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_REAL_DISPATCH", "1")
    async def run():
        rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            intent = await refill(rt, gid, qty)
            signed = len(signer.actions)
            venue.ingest_message({"channel": "userFills", "data": {"user": OWNER, "fills": [raw_fill()]}})
            result = await rt.executors[cfg.account_ref].submit_intent(intent["intent_id"], epoch=intent["epoch"])
            assert result["outcome"] == "DEFERRED" and len(signer.actions) == signed
            assert rt.journal.get_intent(intent["intent_id"])["state"] == "PLANNED"
            assert rt.lifecycle._grid(gid)["status"] == "RUNNING"
        finally:
            rt.close()
    asyncio.run(run())


def test_periodic_risk_waits_for_read_capacity_without_freezing_or_querying(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'native.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            mock.read_capacity_available = lambda: False
            calls = len(mock.exchange_actions)
            async def unexpected(*args, **kwargs):
                pytest.fail('risk observation started while REST capacity was unavailable')
            mock.post = unexpected
            await LiveRiskScheduler(rt)._observe({'grid_id': gid, 'account_id': cfg.account_ref,
                                                  'epoch': rt.lifecycle._grid(gid)['epoch']})
            assert rt.grid_snapshot(gid)['risk_watch']['code'] == 'RISK_API_BUDGET_WAIT'
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            assert len(mock.exchange_actions) == calls
        finally:
            rt.close()
    asyncio.run(run())


def test_dispatch_reserves_reads_before_starting_the_fresh_risk_clock(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        from entropy_arb.grid.hl_rate import ReadBudget, info_weight
        rt = GridRuntime.open(str(tmp_path / 'native.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            intent = await refill(rt, gid, qty)
            now, waits, reads = [0], [], []
            async def sleep(seconds):
                waits.append(seconds)
                now[0] += seconds
            budget = ReadBudget(clock=lambda: now[0], sleep=sleep)
            await budget.acquire(880, critical=True)
            original = mock.post
            async def paced(path, payload):
                if path == '/info':
                    await budget.acquire(info_weight(payload['type']))
                    reads.append((payload['type'], now[0]))
                return await original(path, payload)
            mock.post = paced
            mock.read_sequence = lambda weight=350: budget.read_sequence(weight)
            mock.read_capacity_available = lambda: budget.capacity_available(350)
            rt.live_risk.clock = lambda: now[0]
            result = await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'], epoch=intent['epoch'])
            assert result['outcome'] == 'ACCEPTED' and waits == [60]
            assert reads and all(stamp == 60 for kind, stamp in reads)
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            assert not rt.live_risk.permits and budget.current_reservation() is None
        finally:
            rt.close()
    asyncio.run(run())


def test_private_order_update_requires_new_observation_and_cannot_hide_foreign_order(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'native.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            intent = await refill(rt, gid, qty)
            before = orders_sent(mock)
            original_sign = signer.sign
            foreign = raw_order(cloid='0x' + 'ef' * 16, oid=8888)
            async def sign(action, expiry):
                payload = await original_sign(action, expiry)
                mock.order_rows.append(foreign)
                venue.ingest_message({'channel': 'orderUpdates', 'data': [{'order': foreign}]})
                return payload
            signer.sign = sign
            result = await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'], epoch=intent['epoch'])
            assert result['outcome'] == 'DEFERRED' and result['known_not_sent']
            assert orders_sent(mock) == before
            signer.sign = original_sign
            result = await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'], epoch=intent['epoch'])
            assert result['outcome'] == 'BLOCKED' and result['risk_blocked']
            assert orders_sent(mock) == before
            assert rt.lifecycle._grid(gid)['status'] == 'NEEDS_ATTENTION'
            assert mock.order_rows == [foreign]
        finally:
            rt.close()
    asyncio.run(run())


def test_cancel_reserves_reads_before_the_native_timeout_and_confirms_terminal_state(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        from entropy_arb.grid.hl_rate import ReadBudget, info_weight
        rt = GridRuntime.open(str(tmp_path / 'native.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            intent = await refill(rt, gid, qty)
            executor = rt.executors[cfg.account_ref]
            assert (await executor.submit_intent(intent['intent_id'], epoch=intent['epoch']))['outcome'] == 'ACCEPTED'
            now, waits = [0], []
            async def sleep(seconds):
                waits.append(seconds)
                now[0] += seconds
            budget = ReadBudget(clock=lambda: now[0], sleep=sleep)
            await budget.acquire(880, critical=True)
            original = mock.post
            async def post(path, payload):
                if path == '/info': await budget.acquire(info_weight(payload['type']))
                return await original(path, payload)
            mock.post = post
            mock.read_sequence = lambda weight=350: budget.read_sequence(weight)
            result = await executor.cancel_intent(intent['intent_id'])
            assert result['outcome'] == 'CANCELED' and waits == [60]
            assert rt.journal.get_intent(intent['intent_id'])['state'] == 'CANCELED'
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('signing_delay', [0, 6])
def test_slow_rule_collection_uses_native_evidence_age_and_still_expires_before_post(tmp_path, monkeypatch, signing_delay):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'native.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            intent = await refill(rt, gid, qty)
            before = orders_sent(mock)
            now = [0.0]
            rt.live_risk.clock = lambda: now[0]
            venue.clock_ms = lambda: 30000 + int(now[0] * 1000)
            original_load, original_post, original_sign = venue.load_instrument, mock.post, signer.sign
            async def load(market):
                now[0] += 6
                return await original_load(market)
            async def post(path, payload):
                result = await original_post(path, payload)
                if path == '/info' and payload['type'] in ('l2Book', 'clearinghouseState'):
                    result['time'] = venue.clock_ms()
                return result
            async def sign(action, expiry):
                result = await original_sign(action, expiry)
                if action['type'] == 'order': now[0] += signing_delay
                return result
            venue.load_instrument, mock.post, signer.sign = load, post, sign
            result = await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'], epoch=intent['epoch'])
            assert result['outcome'] == ('ACCEPTED' if signing_delay == 0 else 'DEFERRED')
            assert orders_sent(mock) == before + int(signing_delay == 0)
            if signing_delay:
                assert result['known_not_sent'] and result['reject_code'] == 'RISK_SNAPSHOT_EXPIRED'
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
        finally:
            rt.close()
    asyncio.run(run())


def test_wall_clock_snapshot_expiry_blocks_post_even_if_monotonic_clock_has_not_advanced(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'native.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            intent = await refill(rt, gid, qty)
            before = orders_sent(mock)
            rt.live_risk.clock = lambda: 0
            original = signer.sign
            async def sign(action, expiry):
                result = await original(action, expiry)
                venue.clock_ms = lambda: 36000
                return result
            signer.sign = sign
            result = await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'], epoch=intent['epoch'])
            assert result['outcome'] == 'DEFERRED' and result['known_not_sent']
            assert result['reject_code'] == 'RISK_SNAPSHOT_EXPIRED' and orders_sent(mock) == before
        finally:
            rt.close()
    asyncio.run(run())


def test_periodic_guard_checks_running_stock_without_a_pending_refill(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_REAL_DISPATCH", "1")
    async def run():
        rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
        scheduler = LiveRiskScheduler(rt)
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            mock.equity = "1"
            before = orders_sent(mock)
            await scheduler.tick()
            await asyncio.gather(*scheduler.tasks.values())
            assert rt.lifecycle._grid(gid)["desired_mode"] == "PAUSED" and not mock.order_rows
            assert orders_sent(mock) == before
            assert rt.grid_snapshot(gid)["risk_watch"]["code"] == "RISK_BUDGET_EXCEEDED"
        finally:
            await scheduler.close()
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['forward', 'backward', 'cleared', 'budget'])
def test_complete_history_progress_does_not_cancel_a_valid_permit(tmp_path, monkeypatch, change):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'native.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            await rt.store.write(lambda c: c.execute('UPDATE runtime_state SET value=? WHERE key=?',
                ('hl-ms:29000', 'coverage:'+gid)))
            intent = await refill(rt, gid, qty)
            before = orders_sent(mock)
            original_sign = signer.sign
            async def sign(action, expiry):
                result = await original_sign(action, expiry)
                value = 'hl-ms:28000' if change == 'backward' else '' if change == 'cleared' else 'hl-ms:30000'
                await rt.store.write(lambda c: c.execute('UPDATE runtime_state SET value=? WHERE key=?',
                    (value, 'coverage:'+gid)))
                if change == 'budget':
                    await rt.store.write(lambda c: c.execute("UPDATE budget_reservations SET reserved_amount='1' WHERE grid_id=?", (gid,)))
                return result
            signer.sign = sign
            result = await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'], epoch=intent['epoch'])
            if change == 'forward':
                assert result['outcome'] == 'ACCEPTED'
                assert orders_sent(mock) == before + 1
                assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            else:
                assert result['outcome'] == 'REJECTED'
                assert orders_sent(mock) == before
                assert rt.lifecycle._grid(gid)['desired_mode'] == 'PAUSED'
        finally:
            rt.close()
    asyncio.run(run())


def test_periodic_observation_cannot_replace_a_new_pause(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_REAL_DISPATCH", "1")
    async def run():
        rt = GridRuntime.open(str(tmp_path / "native.db"), execution_mode="production")
        scheduler = LiveRiskScheduler(rt)
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            original = rt.preview
            async def preview(*args, **kwargs):
                await rt.lifecycle._bump_epoch(gid, status="PAUSED", desired="PAUSED")
                return await original(*args, **kwargs)
            rt.preview = preview
            epoch = rt.lifecycle._grid(gid)["epoch"]
            await scheduler.tick()
            await asyncio.gather(*scheduler.tasks.values())
            assert rt.lifecycle._grid(gid)["status"] == "PAUSED"
            assert rt.lifecycle._grid(gid)["epoch"] == epoch + 1
        finally:
            await scheduler.close()
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('unsafe', [False, True])
def test_collection_change_requires_fresh_check_without_hiding_concrete_budget_failure(tmp_path, monkeypatch, unsafe):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path / 'native.db'), execution_mode='production')
        try:
            gid, cfg, venue, mock, signer, qty = await running(rt)
            intent = await refill(rt, gid, qty)
            before, signed = orders_sent(mock), len(signer.actions)
            original = rt.preview
            async def changed(*args, **kwargs):
                if unsafe:
                    mock.equity = '1'
                result = await original(*args, **kwargs)
                await rt.store.write(lambda c: c.execute('UPDATE budget_reservations SET reserved_amount=? WHERE grid_id=?',
                    ('1' if unsafe else '90', gid)))
                return result
            rt.preview = changed
            result = await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'], epoch=intent['epoch'])
            assert orders_sent(mock) == before
            if unsafe:
                assert all(action['type'] == 'cancelByCloid' for action, expiry in signer.actions[signed:])
                assert result['reject_code'] == 'RISK_BUDGET_EXCEEDED'
                assert rt.lifecycle._grid(gid)['desired_mode'] == 'PAUSED'
            else:
                assert len(signer.actions) == signed
                assert result['outcome'] == 'DEFERRED' and result['reject_code'] == 'RISK_OBSERVATION_CHANGED_WAIT'
                assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
                assert rt.journal.get_intent(intent['intent_id'])['state'] == 'PLANNED'
                rt.preview = original
                retry = await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'], epoch=intent['epoch'])
                assert retry['outcome'] == 'ACCEPTED' and orders_sent(mock) == before + 1
            assert not rt.live_risk.permits
        finally:
            rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('next_failure',[None,'owned_price','foreign_order','position','budget'])
def test_owned_cancel_during_native_order_read_requires_a_fresh_match(tmp_path,monkeypatch,next_failure):
    """A read captured before a booked cancel cannot be compared to later state."""
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'cancel-observation.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,signer,qty=await running(rt)
            intent=await refill(rt,gid,qty)
            target=next(row for row in mock.order_rows if row['side']=='B' and not row['reduceOnly'])
            bound=rt.store.read(lambda c:c.execute('SELECT intent_id FROM exchange_orders WHERE exchange_order_id=?',(str(target['oid']),)).fetchone()[0])
            original=mock.post;changed=False
            async def post(path,payload):
                nonlocal changed
                data=await original(path,payload)
                if payload.get('type')=='frontendOpenOrders' and not changed:
                    changed=True
                    await rt.journal.transition(bound,'CANCELED')
                    mock.order_rows.remove(target)
                    # Return the captured pre-cancel Native response, exactly
                    # as a concurrent confirmed cancellation may overtake it.
                return data
            mock.post=post;before,signed=orders_sent(mock),len(signer.actions)
            result=await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'],epoch=intent['epoch'])
            assert changed and result['outcome']=='DEFERRED',result
            assert result['reject_code']=='RISK_OBSERVATION_CHANGED_WAIT'
            assert rt.grid_snapshot(gid)['risk_watch']['owned_orders_changed_during_collection']
            assert rt.journal.get_intent(intent['intent_id'])['state']=='PLANNED'
            assert orders_sent(mock)==before and len(signer.actions)==signed and not rt.live_risk.permits
            assert rt.lifecycle._grid(gid)['status']=='RUNNING'
            mock.post=original
            if next_failure=='owned_price':
                next(o for o in mock.order_rows if not o['reduceOnly'])['limitPx']='99998'
            if next_failure=='foreign_order':mock.order_rows.append(raw_order(cloid='0x'+'ce'*16,oid=788123))
            if next_failure=='position':mock.qty=str(Decimal(mock.qty)+Decimal('.00001'))
            if next_failure=='budget':mock.equity='1'
            retry=await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'],epoch=intent['epoch'])
            if next_failure is None:
                assert retry['outcome']=='ACCEPTED' and orders_sent(mock)==before+1
            else:
                assert retry['outcome']=='BLOCKED' and orders_sent(mock)==before
                assert rt.lifecycle._grid(gid)['desired_mode']=='PAUSED'
                expected={'owned_price':'RISK_HL_OWNED_ORDER_MISMATCH','foreign_order':'RISK_HL_FOREIGN_OPEN_ORDER','position':'RISK_HL_OWNED_POSITION_MISMATCH','budget':'RISK_BUDGET_EXCEEDED'}[next_failure]
                assert retry['reject_code']==expected
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind',['binding','parameters','missing'])
def test_stable_owned_order_mismatch_keeps_its_safe_diagnostic_in_freeze_event(tmp_path,monkeypatch,kind):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'order-diagnostic.db'),execution_mode='production')
        try:
            gid,cfg,venue,mock,signer,qty=await running(rt)
            intent=await refill(rt,gid,qty)
            target=next(o for o in mock.order_rows if o['side']=='B' and not o['reduceOnly'])
            if kind=='binding':
                iid=rt.store.read(lambda c:c.execute('SELECT intent_id FROM exchange_orders WHERE exchange_order_id=?',(str(target['oid']),)).fetchone()[0])
                await rt.journal.transition(iid,'CANCELED')
            elif kind=='parameters':target['limitPx']='99998'
            else:mock.order_rows.remove(target)
            before=orders_sent(mock)
            result=await rt.executors[cfg.account_ref].submit_intent(intent['intent_id'],epoch=intent['epoch'])
            assert result['outcome']=='BLOCKED' and result['reject_code']=='RISK_HL_OWNED_ORDER_MISMATCH'
            assert orders_sent(mock)==before and not rt.live_risk.permits
            event=json.loads(rt.store.read(lambda c:c.execute("SELECT payload_json FROM events WHERE scope=? AND event_type='risk_paused'",(gid,)).fetchone()[0]))
            assert event['risk_observation']['order_mismatch_kind']=='owned_order.'+kind
            assert not event['risk_observation'].get('owned_orders_changed_during_collection')
        finally:rt.close()
    asyncio.run(run())


def test_transient_observation_failure_is_retried_before_pausing(tmp_path, monkeypatch):
    """One failed collection is blindness, not confirmed danger (2026-11-04:
    a single BULK_READ_HTTP_FAILED paused a RUNNING grid after 1h47m). The
    guard must leave the grid running and let the next tick recollect, and
    only pause after a bounded blind window."""
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path/'blind.db'), execution_mode='production')
        try:
            gid,cfg,venue,mock,signer,qty = await running(rt)
            epoch = rt.lifecycle._grid(gid)['epoch']
            guard = rt.live_risk
            base = guard.clock()
            await guard.freeze(gid, epoch=epoch, code='RISK_OBSERVATION_UNAVAILABLE')
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            # A clean observation clears the blind window.
            await guard.record(gid, {'code': None, 'epoch': epoch})
            await guard.freeze(gid, epoch=epoch, code='RISK_OBSERVATION_UNAVAILABLE')
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            # Sustained blindness past the grace still pauses.
            guard.clock = lambda: base + 121
            await guard.freeze(gid, epoch=epoch, code='RISK_OBSERVATION_UNAVAILABLE')
            assert rt.lifecycle._grid(gid)['status'] == 'NEEDS_ATTENTION'
            assert rt.lifecycle._grid(gid)['desired_mode'] == 'PAUSED'
        finally:
            rt.close()
    asyncio.run(run())


def test_profit_evaluation_failure_is_blindness_not_a_pause(tmp_path, monkeypatch):
    """RISK_PROFIT_QUOTE_STALE / RISK_PROFIT_DATA_UNAVAILABLE used to pause a
    RUNNING grid immediately — one failed funding sync marks reconciliation
    not-booked for its 10s backoff window and a 5s-old quote trips under the
    paced reads. Both are now bounded blindness: tolerated inside the grace,
    paused only when they persist past it. The trigger codes themselves are
    untouched."""
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path/'profit-blind.db'), execution_mode='production')
        try:
            gid,cfg,venue,mock,signer,qty = await running(rt)
            epoch = rt.lifecycle._grid(gid)['epoch']
            guard = rt.live_risk
            base = guard.clock()
            for code in ('RISK_PROFIT_QUOTE_STALE', 'RISK_PROFIT_DATA_UNAVAILABLE'):
                await guard.freeze(gid, epoch=epoch, code=code)
                assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            # Sustained unevaluability past the grace still pauses.
            guard.clock = lambda: base + 121
            await guard.freeze(gid, epoch=epoch, code='RISK_PROFIT_QUOTE_STALE')
            assert rt.lifecycle._grid(gid)['status'] == 'NEEDS_ATTENTION'
            assert rt.lifecycle._grid(gid)['desired_mode'] == 'PAUSED'
        finally:
            rt.close()
    asyncio.run(run())


def test_clean_profit_evaluation_clears_the_blind_window(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path/'profit-clear.db'), execution_mode='production')
        try:
            gid,cfg,venue,mock,signer,qty = await running(rt)
            epoch = rt.lifecycle._grid(gid)['epoch']
            guard = rt.live_risk
            base = guard.clock()
            await guard.freeze(gid, epoch=epoch, code='RISK_PROFIT_DATA_UNAVAILABLE')
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            # A clean profit evaluation clears the window: the next failure
            # starts a fresh grace instead of inheriting the old onset.
            await guard.record(gid, {'code': None, 'epoch': epoch,
                                     'profit_protection': {'code': None}})
            guard.clock = lambda: base + 60
            await guard.freeze(gid, epoch=epoch, code='RISK_PROFIT_QUOTE_STALE')
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            guard.clock = lambda: base + 181
            await guard.freeze(gid, epoch=epoch, code='RISK_PROFIT_QUOTE_STALE')
            assert rt.lifecycle._grid(gid)['status'] == 'NEEDS_ATTENTION' 
        finally:
            rt.close()
    asyncio.run(run())


def test_stream_unreconciled_never_pauses_the_grid_itself(tmp_path, monkeypatch):
    """RISK_STREAM_UNRECONCILED raced the stream-loss path: the risk tick
    paused the grid with an epoch bump while stream recovery owned the same
    transition, interrupting exactly the recovery that clears the gap. The
    code is now a wait on both the freeze skip-list and the scheduler."""
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt = GridRuntime.open(str(tmp_path/'stream-wait.db'), execution_mode='production')
        try:
            gid,cfg,venue,mock,signer,qty = await running(rt)
            epoch = rt.lifecycle._grid(gid)['epoch']
            await rt.live_risk.freeze(gid, epoch=epoch, code='RISK_STREAM_UNRECONCILED')
            assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            assert rt.lifecycle._grid(gid)['epoch'] == epoch
            from entropy_arb.grid.live_risk import LiveRiskScheduler
            scheduler = LiveRiskScheduler(rt)
            try:
                async def observe(gid):
                    return {'code': 'RISK_STREAM_UNRECONCILED', 'epoch': epoch}
                rt.live_risk.observe = observe
                await scheduler._observe({'grid_id': gid, 'account_id': cfg.account_ref, 'epoch': epoch})
                assert rt.lifecycle._grid(gid)['status'] == 'RUNNING'
            finally:
                await scheduler.close()
        finally:
            rt.close()
    asyncio.run(run())
