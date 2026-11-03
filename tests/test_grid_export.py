import asyncio
import base64
import csv
import hashlib
import io
import json
import tempfile
import threading
import zipfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from entropy_arb.console.grid_web import create_grid_web_app
from entropy_arb.grid import export, store, models
from entropy_arb.grid.adapters.fixture import FixtureVenue
from entropy_arb.grid.rpc import RuntimeRpcServer
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.sizing import DiscreteTableModel
from test_grid_api import CONFIG, RISK_ROWS


async def prepare(path):
    rt = GridRuntime.open(str(path / 'test.db'), execution_mode='simulation')
    venue = FixtureVenue(mark='105')
    venue.risk_model = DiscreteTableModel(rows=RISK_ROWS)
    rt.add_fixture_venue(venue)
    config = models.GridConfig.from_dict(CONFIG)
    preview = await rt.preview(config)
    result = await rt.create_grid(config=config, preview_id=preview['preview_id'], request_id='export-grid')
    gid = result['grid_id']
    def seed(c):
        for n in range(355):
            store.register_event(c, event_key=f'export-{n}', scope=gid,
                                 event_type='export_test', payload={'n': n})
        store.register_event(c, event_key='foreign', scope='other-grid',
                             event_type='foreign', payload={'private': 'other-grid-data'})
        c.execute("UPDATE grids SET name=? WHERE grid_id=?", ('=formula,\n中文', gid))
        c.execute("INSERT INTO executions(execution_key,account,market,trade_id,side,qty_base,price,"
                  "fee_amount,fee_asset,is_maker,exchange_ts,received_ts,source) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  ('test-owned', CONFIG['account_ref'], CONFIG['market_ref'], '900719925474099312345',
                   'buy', '0.000000000000000123', '105.000000000000000001', '-0.00000001',
                   'USDC', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'fixture'))
        c.execute('INSERT INTO execution_owners VALUES(?,?)', ('test-owned', gid))
        c.execute("INSERT INTO funding_payments VALUES(?,?,?,?,?,?,?,?)",
                  ('fund-owned', CONFIG['account_ref'], CONFIG['market_ref'], '-0.0000123',
                   'USDC', '2026-01-01T00:00:00Z', '1', 'fixture'))
        c.execute('INSERT INTO funding_owners VALUES(?,?)', ('fund-owned', gid))
        c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',('range-watch:'+gid,'{"code":"RANGE_OUTSIDE"}'))
        c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',('range-watch:other-grid','other-grid-observation'))
    await rt.store.write(seed)
    return rt, gid


def unpack(data):
    archive = zipfile.ZipFile(io.BytesIO(data))
    manifest = json.loads(archive.read('manifest.json'))
    for name, meta in manifest['files'].items():
        raw = archive.read(name)
        assert hashlib.sha256(raw).hexdigest() == meta['sha256']
        rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
        assert len(rows) == meta['rows']
    return archive, manifest


def test_archive_exports_all_pages_exact_values_and_only_owned_records(tmp_path):
    async def run():
        rt, gid = await prepare(tmp_path)
        manager = export.ExportManager(rt.store.path)
        try:
            result = await manager.begin(gid)
            before = rt.metrics()
            data, offset = bytearray(), 0
            while offset < result['size']:
                chunk = manager.chunk(result['export_id'], offset)
                assert manager.chunk(result['export_id'], offset) == chunk
                data.extend(base64.b64decode(chunk['data'])); offset = chunk['next_offset']
            archive, manifest = unpack(data)
            assert manifest['files']['events.csv']['rows'] >= 355
            assert b'other-grid-data' not in archive.read('events.csv')
            fills = list(csv.DictReader(io.StringIO(archive.read('fills.csv').decode('utf-8-sig'))))
            assert fills[0]['trade_id'] == '900719925474099312345'
            assert fills[0]['qty_base'] == '0.000000000000000123'
            assert fills[0]['price'] == '105.000000000000000001'
            assert fills[0]['fee_amount'] == '-0.00000001'
            assert manifest['files']['funding.csv']['rows'] == 1
            assert b'RANGE_OUTSIDE' in archive.read('observations.csv')
            assert b'other-grid-observation' not in archive.read('observations.csv')
            assert not any('credentials' in name for name in archive.namelist())
            assert {k:v for k,v in rt.metrics().items() if k != 'ts'} == {k:v for k,v in before.items() if k != 'ts'}
            handle = manager.exports[result['export_id']]['file']
            manager.release(result['export_id']); manager.release(result['export_id'])
            assert handle.closed
            with pytest.raises(export.ExportError, match='expired'):
                manager.chunk(result['export_id'], 0)
        finally:
            manager.close(); rt.close()
    asyncio.run(run())


def test_export_keeps_one_snapshot_while_ledger_receives_later_writes(tmp_path, monkeypatch):
    async def run():
        rt, gid = await prepare(tmp_path)
        original = export.csv_cell
        changed = False
        def inject(value):
            nonlocal changed
            if not changed:
                changed = True
                rt.store.write_sync(lambda c: store.register_event(c, event_key='after-export-start',
                    scope=gid, event_type='later', payload={'late': True}))
            return original(value)
        monkeypatch.setattr(export, 'csv_cell', inject)
        manager = export.ExportManager(rt.store.path)
        try:
            result = await manager.begin(gid)
            archive, manifest = unpack(manager.exports[result['export_id']]['file'].read())
            assert b'after-export-start' not in archive.read('events.csv')
            assert rt.metrics()['events'] == manifest['event_high_water'] + 1
        finally: manager.close(); rt.close()
    asyncio.run(run())


def test_expiry_capacity_validation_and_cancelled_worker_cleanup(tmp_path, monkeypatch):
    async def run():
        rt, gid = await prepare(tmp_path)
        now = [0]
        manager = export.ExportManager(rt.store.path, clock=lambda: now[0], ttl=5, capacity=1)
        try:
            result = await manager.begin(gid)
            file = manager.exports[result['export_id']]['file']
            with pytest.raises(export.ExportError) as e: await manager.begin(gid)
            assert e.value.code == 'EXPORT_BUSY'
            for offset in (-1, True, 1.5, result['size'] + 1):
                with pytest.raises(export.ExportError): manager.chunk(result['export_id'], offset)
            now[0] = 6; manager.prune()
            assert file.closed
            with pytest.raises(export.ExportError) as e: await manager.begin('does-not-exist')
            assert e.value.code == 'NOT_FOUND'
            original = export.build_archive
            started, finish = threading.Event(), threading.Event()
            outputs = []
            def slow(*args):
                started.set(); finish.wait(5)
                value = original(*args); outputs.append(value[0]); return value
            monkeypatch.setattr(export, 'build_archive', slow)
            task = asyncio.create_task(manager.begin(gid))
            await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            with pytest.raises(export.ExportError): await manager.begin(gid)
            finish.set()
            await asyncio.gather(*list(manager.workers))
            await asyncio.sleep(0)
            assert outputs[0].closed and manager.building == 0
        finally: manager.close(); rt.close()
    asyncio.run(run())


def test_csv_formula_neutralization_and_decimal_preservation():
    for value in ('=1+2', '+SUM(A1)', '-cmd', '@SUM(A1)', '\t=1', '\r\n+cmd'):
        assert export.csv_cell(value) == "'" + value
    for value in ('-0.000012345', '12345678901234567890', '中文,\n"quoted"', None):
        assert export.csv_cell(value) == ('' if value is None else value)


def test_http_download_uses_header_auth_and_complete_archive_without_trading():
    async def run(path):
        rt, gid = await prepare(path)
        rpc = RuntimeRpcServer(rt, str(path / 'rpc.sock'))
        await rpc.start()
        client = TestClient(TestServer(create_grid_web_app(socket_path=str(path/'rpc.sock'), token='test-token')))
        await client.start_server()
        try:
            before = rt.metrics()
            assert (await client.get(f'/api/grid/{gid}/export?token=test-token')).status == 401
            response = await client.get(f'/api/grid/{gid}/export', headers={'Authorization':'Bearer test-token'})
            assert response.status == 200
            assert response.headers['Cache-Control'] == 'no-store'
            assert 'attachment;' in response.headers['Content-Disposition']
            data = await response.read()
            assert len(data) == int(response.headers['Content-Length'])
            _, manifest = unpack(data)
            assert manifest['grid']['grid_id'] == gid
            assert manifest['files']['events.csv']['rows'] >= 355
            assert {k:v for k,v in rt.metrics().items() if k != 'ts'} == {k:v for k,v in before.items() if k != 'ts'}
            await asyncio.sleep(0.01)
            assert not rpc.exports.exports
            response = await client.get('/api/grid/missing/export', headers={'Authorization':'Bearer test-token'})
            assert response.status == 404
        finally:
            await client.close(); await rpc.stop(); rt.close()
    with tempfile.TemporaryDirectory(prefix='grid-export-', dir='/tmp') as directory:
        asyncio.run(run(Path(directory)))
