"""Operating metrics reflect exact stored money and reviewed live scope."""
import asyncio

from engine_harness import make_runtime, create_ready
from test_grid_operator_trial import trial
from test_grid_live_release import release_fixture
from entropy_arb.grid.rpc import RuntimeRpcServer


def test_budget_metrics_keep_decimal_text_and_account_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt, venue = make_runtime(tmp_path)
        try:
            gid, cfg, plan = await create_ready(rt, venue)
            amount = '100000000000000000000.123456789'
            await rt.store.write(lambda c: c.execute('UPDATE budget_reservations SET reserved_amount=? WHERE grid_id=?', (amount, gid)))
            intent = await rt.journal.create_intent(grid_id=gid, cause_key='metrics-accepted', revision=1, level_index=0,
                side='buy', qty_base='0.001', price='100', policy='gtc', reduce_only=False, epoch=0)
            await rt.store.write(lambda c: c.execute("UPDATE order_intents SET state='ACCEPTED' WHERE intent_id=?", (intent['intent_id'],)))
            metrics = rt.metrics()
            assert metrics['budget_reserved'] == [{'asset': 'USDC', 'total': amount}]
            assert metrics['account_budget_reserved'] == [{'account_id': cfg.account_ref, 'asset': 'USDC', 'total': amount}]
            assert metrics['open_intents'] == 1
            assert metrics['real_dispatch'] is False  # Simulation cannot trade with the env flag.
            state = await RuntimeRpcServer(rt, 'unused.sock')._m_status({})
            assert state['healthy'] and state['health_scope'] == 'runtime_liveness'
        finally:
            rt.close()
    asyncio.run(run())


def test_metrics_require_current_approved_market_and_global_switch(trial, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt, root, transport, release, grant, cfg, tiny = await release_fixture(trial)
        assert rt.metrics()['real_dispatch'] is False
        assert not await release.restore()
        assert rt.metrics()['real_dispatch'] is True
        monkeypatch.setenv('GRID_REAL_DISPATCH', '0')
        assert rt.metrics()['real_dispatch'] is False
        monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
        root.account_mode_confirmed = False
        assert rt.metrics()['real_dispatch'] is False
        root.account_mode_confirmed = True
        release.grants.clear()
        assert rt.metrics()['real_dispatch'] is False
    asyncio.run(run())
