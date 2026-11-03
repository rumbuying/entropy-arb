"""Stop-and-keep must retain ownership until fixed-stop terminal proof exists."""
import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from entropy_arb.grid.inventory import Inventory
from test_grid_bulk_flat_finality import seed_claims
from test_grid_bulk_protection_manager import setup


def state(rt, gid, account):
    def read(c):
        return (c.execute('SELECT status FROM grids WHERE grid_id=?', (gid,)).fetchone()[0],
                c.execute('SELECT state FROM scope_claims').fetchone()[0],
                c.execute('SELECT reserved_amount FROM budget_reservations').fetchone()[0],
                len(rt.journal.pending_fixed_terminals(c, gid, account)))
    return rt.store.read(read)


def test_keep_flat_stop_cancellation_does_not_release_unverified_account(tmp_path, monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            iid=(await rt.protection.prepare(gid,actor,expected_epoch=1))[0]
            await seed_claims(rt,gid,cfg)
            rt.recover=AsyncMock(return_value={'ok':True})  # pre-cancellation reconciliation
            rt.pump_events=AsyncMock()
            result=await rt.lifecycle.stop_keep(gid,request_id='offline-keep-unverified')
            assert result['status']=='partial' and result['code']=='PROTECTION_CUMULATIVE_UNVERIFIED'
            assert state(rt,gid,cfg.account_ref)==('NEEDS_ATTENTION','held','100',1)
            assert rt.journal.get_intent(iid)['state']=='CANCELED' and len(sender.calls)==2
            op=rt.store.read(lambda c:c.execute('SELECT status FROM operations WHERE operation_id=?',
                (result['operation_id'],)).fetchone()[0])
            assert op=='partial'
            replay=await rt.lifecycle.stop_keep(gid,request_id='offline-keep-unverified')
            assert replay['replayed'] and len(sender.calls)==2
        finally:rt.close()
    asyncio.run(run())


def test_keep_flat_without_pending_protection_keeps_original_success_contract(tmp_path, monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            await seed_claims(rt,gid,cfg)
            rt.recover=AsyncMock(return_value={'ok':True});rt.pump_events=AsyncMock()
            result=await rt.lifecycle.stop_keep(gid,request_id='offline-keep-clear')
            assert result['status']=='succeeded' and result['grid_status']=='CLOSED'
            assert state(rt,gid,cfg.account_ref)==('CLOSED','released','0',0)
            saved=rt.store.read(lambda c:c.execute('SELECT result_json FROM operations WHERE operation_id=?',
                (result['operation_id'],)).fetchone()[0])
            assert json.loads(saved)=={'status':'CLOSED','position':'0'}
        finally:rt.close()
    asyncio.run(run())


def test_keep_nonzero_preserves_protection_and_claim(tmp_path, monkeypatch):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            iid=(await rt.protection.prepare(gid,actor,expected_epoch=1))[0]
            await seed_claims(rt,gid,cfg)
            def seed_lot(c):
                c.execute("INSERT INTO executions(execution_key,account,market,side,qty_base,price,exchange_ts,received_ts,source) "
                    "VALUES('offline',?,?,'buy','0.1','2100','test','test','offline')", (cfg.account_ref,cfg.market_ref))
                Inventory(rt.store,gid).open_lot(c,side='buy',qty=Decimal('0.1'),
                    price=Decimal('2100'),exec_key='offline',ts='test')
            await rt.store.write(seed_lot)
            rt.recover=AsyncMock(return_value={'ok':True});rt.pump_events=AsyncMock()
            result=await rt.lifecycle.stop_keep(gid,request_id='offline-keep-position')
            assert result['status']=='succeeded' and result['grid_status']=='STOPPED_WITH_POSITION'
            assert state(rt,gid,cfg.account_ref)==('STOPPED_WITH_POSITION','held','100',0)
            assert rt.journal.get_intent(iid)['state'] in ('OPEN','ACCEPTED') and len(sender.calls)==1
        finally:rt.close()
    asyncio.run(run())


@pytest.mark.parametrize('wrong_at', [1, 2])
def test_keep_flat_refuses_snapshot_for_another_account(tmp_path, monkeypatch, wrong_at):
    async def run():
        rt,gid,cfg,parent,view,native,p,sender,actor=await setup(tmp_path,monkeypatch)
        try:
            await seed_claims(rt,gid,cfg)
            rt.recover=AsyncMock(return_value={'ok':True});rt.pump_events=AsyncMock()
            count=[0]
            async def snapshot():
                count[0]+=1
                return SimpleNamespace(account='foreign' if count[0]==wrong_at else cfg.account_ref,
                                       position_qty_base='0')
            view.account_snapshot=snapshot
            result=await rt.lifecycle.stop_keep(gid,request_id='offline-keep-foreign')
            assert result['status']=='partial'
            assert state(rt,gid,cfg.account_ref)==('NEEDS_ATTENTION','held','100',0)
        finally:rt.close()
    asyncio.run(run())
