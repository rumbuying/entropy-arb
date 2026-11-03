import asyncio

import pytest

from entropy_arb.grid.hl_rate import ReadBudget, info_weight
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.store import Store
from test_grid_hl_execution import controller, allow
from test_grid_hl_owned_risk import seeded_inventory, enable_offline_exchange


def test_matching_native_setting_read_uses_execution_budget_and_sends_no_control(tmp_path):
    async def run():
        st=Store.open(str(tmp_path/'control.db'));ui=None
        try:
            venue,signer,controls=controller(st,value=3)
            await venue.load_instrument(venue.market.market_key)
            budget=ReadBudget(clock=lambda:0);await budget.acquire(550)
            original=venue.transport.post
            async def post(path,payload):
                assert path=='/info' and payload['type']=='activeAssetData'
                await budget.acquire(info_weight(payload['type']))
                return await original(path,payload)
            venue.transport.post=post;venue.transport.read_sequence=budget.read_sequence
            ui=asyncio.create_task(budget.acquire(2))
            result=await asyncio.wait_for(controls.leverage(leverage=3,margin_mode='cross',
                request_id='already-matches',gate=allow),.3)
            assert result['state']=='CONFIRMED' and result['unchanged'] is True
            assert not venue.transport.exchange_calls and not signer.actions
            assert not ui.done() and sum(w for _,w in budget.spent)==570
        finally:
            if ui:ui.cancel();await asyncio.gather(ui,return_exceptions=True)
            st.close()
    asyncio.run(run())


@pytest.mark.parametrize('proof',['confirmed','missing_setup','missing_control'])
def test_activation_only_skips_duplicate_preview_with_explicit_matching_no_effect_proof(tmp_path,monkeypatch,proof):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,transport,signer,_,qty=await seeded_inventory(rt)
            mock=await enable_offline_exchange(rt,venue,transport)
            original=rt.preview;previews=[]
            async def preview(*args,**kwargs):
                result=await original(*args,**kwargs)
                if kwargs.get('purpose')=='activate':
                    previews.append(result)
                    if proof=='missing_setup' and len(previews)==1:
                        result['sizing']['account_setup'].pop('confirmed',None)
                return result
            rt.preview=preview
            if proof=='missing_control':
                original_control=venue.configure_leverage
                async def control(**kwargs):
                    result=await original_control(**kwargs);result.pop('unchanged',None);return result
                venue.configure_leverage=control
            result=await rt.lifecycle.resume(gid,actor=rt.actors[gid],request_id='activation-read-cost')
            assert result['status']=='succeeded'
            assert len(previews)==(1 if proof=='confirmed' else 2)
            assert rt.grid_snapshot(gid)['plan']['qty_per_grid']==qty
            assert not [a for a in mock.exchange_actions if a['type']=='updateLeverage']
        finally:rt.close()
    asyncio.run(run())
