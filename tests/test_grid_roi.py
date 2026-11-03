import asyncio
from decimal import Decimal,localcontext
import pytest
from engine_harness import make_runtime,create_ready
from entropy_arb.grid import ledger,models
from entropy_arb.grid.operations import GridOperations


def test_cumulative_roi_keeps_releases_and_losses_out_of_denominator():
    result=ledger.cumulative_allocation_roi('-4',['30','40','35','45'],['10','5'])
    assert result['roi_denominator']=='65'  # 30 + 10 + 10 sizing, +15 buffer.
    with localcontext() as context:
        context.prec=50
        assert Decimal(result['roi_percent'])==Decimal('-4')/65*100
    assert ledger.cumulative_allocation_roi('2',['40'],[])['roi_percent']=='5'
    for versions,buffers in [([],[]),(['0'],[]),(['10'],['-1'])]:
        with pytest.raises(models.GridModelError):ledger.cumulative_allocation_roi('0',versions,buffers)


def test_buffer_replay_and_close_do_not_reset_roi_capital(tmp_path):
    async def run():
        rt,venue=make_runtime(tmp_path)
        try:
            gid,cfg,plan=await create_ready(rt,venue)
            operations=GridOperations(rt,rt.lifecycle)
            await operations.add_buffer(gid,amount='10',request_id='buffer-once')
            await operations.add_buffer(gid,amount='10',request_id='buffer-once')
            assert rt.performance(gid)['roi_denominator']=='90'
            await rt.start_grid(gid)
            await rt.step_market(gid,'104')
            opened=rt.performance(gid)
            assert opened['roi_status']=='estimated'
            with localcontext() as context:
                context.prec=50
                assert Decimal(opened['roi_percent'])==Decimal(opened['net_pnl'])/90*100
            await rt.lifecycle.stop_flat(gid,actor=rt.actors[gid],request_id='roi-close')
            closed=rt.performance(gid)
            assert closed['roi_denominator']=='90' and closed['roi_status']=='booked'
            assert rt.grid_snapshot(gid)['budget']['reserved_amount']=='0'
        finally:rt.close()
    asyncio.run(run())
