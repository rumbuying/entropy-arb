"""Offline closeout tests: a terminal stop is not proof of its executions."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from entropy_arb.grid.inventory import Inventory
from test_grid_bulk_protection_manager import setup


async def seed_claims(rt, gid, cfg):
    def write(c):
        c.execute("UPDATE grids SET status='RUNNING' WHERE grid_id=?", (gid,))
        c.execute("INSERT INTO scope_claims VALUES(?,?,?,'held','test',NULL)",
                  (f'market:{cfg.account_ref}:{cfg.market_ref}', 'grid', gid))
        c.execute("INSERT INTO budget_reservations VALUES(?,?,?,'test')", (gid, '100', 'USDC'))
    await rt.store.write(write)


def test_flat_canceled_fixed_stop_keeps_claim_and_budget_until_executions_proven(tmp_path, monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            iid=(await rt.protection.prepare(gid,actor,expected_epoch=1))[0]
            await seed_claims(rt,gid,cfg)
            actor.account=parent._account
            actor.inv=Inventory(rt.store,gid)
            actor.drain_pump=AsyncMock()
            rt.recover=AsyncMock(return_value={'ok':True})
            rt.lifecycle._flatten_position=AsyncMock(return_value=0)
            monkeypatch.setattr('entropy_arb.grid.hl_rate.critical_position_snapshot',
                AsyncMock(return_value=SimpleNamespace(account=cfg.account_ref,position_qty_base='0')))
            monkeypatch.setattr('entropy_arb.grid.hl_rate.critical_open_orders',AsyncMock(return_value=[]))
            result=await rt.lifecycle.stop_flat(gid,actor=actor,request_id='offline-close')
            assert result['status']=='partial' and result['code']=='PROTECTION_CUMULATIVE_UNVERIFIED'
            assert rt.lifecycle._grid(gid)['status']=='NEEDS_ATTENTION'
            assert rt.journal.get_intent(iid)['state']=='CANCELED'
            assert len(sender.calls)==2
            def state(c):
                return (c.execute('SELECT state FROM scope_claims').fetchone()[0],
                        c.execute('SELECT reserved_amount FROM budget_reservations').fetchone()[0],
                        c.execute('SELECT status FROM operations WHERE operation_id=?',(result['operation_id'],)).fetchone()[0])
            assert rt.store.read(state)==('held','100','partial')
            replay=await rt.lifecycle.stop_flat(gid,actor=actor,request_id='offline-close')
            assert replay['replayed'] and replay['status']=='partial' and len(sender.calls)==2
        finally:rt.close()
    asyncio.run(run())


def test_finalization_does_not_release_after_newer_epoch(tmp_path,monkeypatch):
    from entropy_arb.grid.lifecycle import LifecycleError
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            await seed_claims(rt,gid,cfg)
            op=await rt.lifecycle._begin_operation(gid,'stop_flat','offline-old')
            epoch=await rt.lifecycle._bump_epoch(gid,status='STOPPING',desired='STOPPED')
            await rt.store.write(lambda c:c.execute('UPDATE grids SET epoch=epoch+1 WHERE grid_id=?',(gid,)))
            with pytest.raises(LifecycleError,match='OPERATION_INTERRUPTED'):
                await rt.lifecycle._finalize_flat(gid,epoch,op['operation_id'])
            assert rt.store.read(lambda c:c.execute('SELECT state FROM scope_claims').fetchone()[0])=='held'
            assert rt.store.read(lambda c:c.execute('SELECT reserved_amount FROM budget_reservations').fetchone()[0])=='100'
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('pending', [False, True])
def test_finalization_check_and_release_share_transaction(tmp_path,monkeypatch,pending):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            if pending:
                iid=(await rt.protection.prepare(gid,actor,expected_epoch=1))[0]
                await rt.lifecycle._cancel_open_grid_orders(gid,1,include_protection=True)
            await seed_claims(rt,gid,cfg)
            op=await rt.lifecycle._begin_operation(gid,'stop_flat','offline-final')
            epoch=await rt.lifecycle._bump_epoch(gid,status='STOPPING',desired='STOPPED')
            result=await rt.lifecycle._finalize_flat(gid,epoch,op['operation_id'])
            def state(c):
                return (c.execute('SELECT state FROM scope_claims').fetchone()[0],
                        c.execute('SELECT reserved_amount FROM budget_reservations').fetchone()[0],
                        c.execute('SELECT status FROM operations WHERE operation_id=?',(op['operation_id'],)).fetchone()[0])
            assert result['status']==('partial' if pending else 'succeeded')
            assert rt.store.read(state)==(('held','100','partial') if pending else ('released','0','succeeded'))
        finally:rt.close()
    asyncio.run(run())
