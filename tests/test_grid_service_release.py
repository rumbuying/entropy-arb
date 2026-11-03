"""Release evidence is local and pinned, and must agree with native facts."""
import copy
import hashlib
import json
import os
import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.adapters.base import AdapterError
from entropy_arb.grid.service_release import artifact, verify
from test_grid_config import base_config


def pin(tmp_path, name, value, lines=False):
    path = tmp_path / name
    raw = ('\n'.join(json.dumps(v) for v in value) if lines else json.dumps(value)).encode()
    path.write_bytes(raw); path.chmod(0o600)
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}


@pytest.fixture
def bundle(tmp_path):
    c = sqlite3.connect(':memory:'); c.row_factory = sqlite3.Row
    c.executescript('''
    CREATE TABLE grids(grid_id,status,account_id,market_key,updated_ts);
    CREATE TABLE runtime_state(key,value);
    CREATE TABLE accounts(account_id,owner);
    CREATE TABLE inventory_lots(grid_id,qty_remaining);
    CREATE TABLE events(seq,event_key,scope,event_type,payload_json);
    CREATE TABLE order_intents(intent_id,grid_id,cause_key,reduce_only,policy,state,side,epoch,qty_base,price,client_id,trigger_price,whole_position);
    CREATE TABLE exchange_orders(intent_id,account,client_id,exchange_order_id);
    CREATE TABLE execution_owners(execution_key,grid_id);
    CREATE TABLE executions(execution_key,account,order_client_id,exchange_order_id,trade_id,side,qty_base,price,fee_amount,is_maker,finality,exchange_ts);
    ''')
    cfg = models.GridConfig.from_dict(base_config(direction='long', protection={
        'stop_loss': {'type': 'price', 'source': 'mark', 'operator': 'lte', 'value': '80'},
        'service_coverage': {'max_open_notional': '150', 'max_seconds': 30}}))
    account, market = cfg.account_ref, cfg.market_ref
    cfgs = {g: cfg for g in ['loop', 'small', 'fault']}
    rt = SimpleNamespace(config=cfgs.__getitem__, journal=SimpleNamespace(decorate=lambda db, row: dict(row)))
    c.execute('INSERT INTO accounts VALUES(?,?)', (account, 'owner'))
    native_audits = {}
    def order(iid, gid, side, qty, price, oid, *, whole=0, epoch=1):
        cid = 'cid-' + iid
        c.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (iid, gid, 'protection:' + iid if whole else 'exit-flat:' + iid, 1, 'reduce_only_ioc', 'CANCELED' if whole else 'FILLED',
             side, epoch, qty, price, cid, '80' if whole else None, whole))
        c.execute('INSERT INTO exchange_orders VALUES(?,?,?,?)', (iid, account, cid, str(oid)))
        return cid
    def execution(gid, tag, side, ms, oid):
        iid = gid + tag; cid = order(iid, gid, side, '.1', '100', oid)
        key = 'hl:mainnet:' + iid
        c.execute('INSERT INTO execution_owners VALUES(?,?)', (key, gid))
        ts = '1970-01-01T00:00:%02d.%03dZ' % (ms // 1000, ms % 1000)
        c.execute('INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                  (key, account, cid, str(oid), str(oid + 100), side, '.1', '100', '.01', 0, 'final', ts))
        return {'oid': oid, 'tid': oid + 100, 'cloid': cid, 'coin': market.split('|')[2], 'side': 'B' if side == 'buy' else 'A',
                'sz': '.1', 'px': '100', 'fee': '.01', 'crossed': True, 'time': ms, 'engine_cause': 'exit-flat:' + iid}
    for index, gid in enumerate(cfgs):
        c.execute('INSERT INTO grids VALUES(?,?,?,?,?)', (gid, 'CLOSED', account, market, '1970-01-01T00:00:25Z'))
        context = {'account': account, 'market': market, 'credential_revision': 'rev', 'config_hash': cfg.config_hash,
                   'history': {'retained_fill_ms': 1, 'clean_funding_anchor_ms': 2}}
        c.execute('INSERT INTO runtime_state VALUES(?,?)', ('operator-trial:' + gid, json.dumps(context)))
        c.execute('INSERT INTO inventory_lots VALUES(?,?)', (gid, '0'))
        raw = [execution(gid, '-buy', 'buy', 5000, 1000 + index * 10), execution(gid, '-sell', 'sell', 20000, 1001 + index * 10)]
        native_audits[gid] = {'grid_id': gid, 'config_hash': cfg.config_hash, 'account_id': account, 'owner': 'owner', 'network': 'mainnet',
                             'matched_fills': raw, 'final_dexes': [{'dex': '', 'orders': [], 'positions': []}], 'native_stops': []}
    old_cid = order('old', 'loop', 'sell', '2', '78.4', 7, whole=1)
    new_cid = order('new', 'loop', 'sell', '2', '78.4', 8, whole=1)
    start = {'generation': 'generation', 'epoch': 1, 'stop_oid': '7', 'stop_intent_id': 'old',
             'started_ms': 10000, 'deadline_ms': 40000, 'open_notional': '20', 'gross_qty': '.2', 'initial_gross_qty': '.2',
             'policy': cfg.protection.service_coverage.to_dict()}
    lease = {'epoch': 1, 'config_hash': cfg.config_hash, 'account': account, 'market': market,
             'confirmed_from_ms': 11000, 'observed_ms': 12000, 'deadline_ms': 41000, 'stops': {'new': '8'}}
    end = {'generation': 'generation', 'epoch': 1, 'confirmed_ms': 12000, 'new_stops': {'new': '8'},
           'confirmed_notional': '20', 'native_lease': lease}
    for seq, kind, data in [(1, 'service_coverage_started', start), (2, 'service_coverage_confirmed', end)]:
        c.execute('INSERT INTO events VALUES(?,?,?,?,?)', (seq, kind, 'loop', kind, json.dumps(data)))
    native_audits['loop']['native_stops'] = [{'intent_id': 'old', 'exchange_order_id': '7',
        'native': {'status': 'order', 'order': {'status': 'reduceOnlyCanceled', 'statusTimestamp': 10000}}}]
    native_order = {'oid': 8, 'cloid': new_cid, 'coin': market.split('|')[2], 'side': 'A', 'reduceOnly': True,
                    'isPositionTpsl': True, 'isTrigger': True, 'orderType': 'Stop Market', 'origSz': '0', 'sz': '0', 'triggerPx': '80', 'limitPx': '78.4'}
    native_audits['loop']['native_stops'][0]['native']['order']['order'] = {**native_order, 'oid': 7, 'cloid': old_cid}
    flight = [{'grid_id': 'loop', 'intent': {'intent_id': 'new'}, 'request_ms': 11000, 'received_ms': 11500,
               'native': {'status': 'order', 'order': {'status': 'open', 'order': native_order}}}]
    fault_cid = order('fault-stop', 'fault', 'sell', '2', '78.4', 9, whole=1)
    fault_order = {**native_order, 'oid': 9, 'cloid': fault_cid}
    recovery = {'grids': ['fault'], 'target_instance': 'instance', 'marker_id': 'marker'}
    c.execute('INSERT INTO events VALUES(?,?,?,?,?)', (3, 'watchdog-recovery:marker', 'runtime', 'watchdog_recovery', json.dumps(recovery)))
    failure = {'grid_id': 'fault', 'schema_version': 1, 'kind': 'position_sigstop', 'passed': True, 'observed_ps_state': 'T',
               'sent_ms': 10000, 'replacement_seconds': 6, 'rpc_recovered_seconds': 10, 'closed_seconds': 15,
               'before': {'loaded': True, 'scope_valid': True, 'pid': 1, 'birth': 'old'},
               'after': {'loaded': True, 'scope_valid': True, 'pid': 2, 'birth': 'new'},
               'heartbeat': {'pid': 1, 'birth': 'old', 'phase': 'running', 'instance': 'instance'},
               'marker': {'pid': 1, 'birth': 'old', 'instance': 'instance', 'marker_id': 'marker'},
               'before_watchdog': {'pid': 3}, 'after_watchdog': {'pid': 3}, 'recovery_event': recovery,
               'owned_qty': '.1', 'native_before': {'state': {'time': 9500, 'assetPositions': [{'position': {
                   'coin': market.split('|')[2], 'szi': '.1', 'positionValue': '10'}}]},
                   'orders': [fault_order], 'stop': {'status': 'order', 'order': {'status': 'open', 'order': fault_order}}}}
    grant = {'account_id': account, 'credential_revision': 'rev', 'market': market, 'loop_grid': 'loop', 'small_close_grid': 'small',
             'service_coverage': cfg.protection.service_coverage.to_dict()}
    def repin():
        grant['service_evidence'] = {'fault_grid': 'fault', 'fault_proof': pin(tmp_path, 'fault.json', failure),
            'native_audits': {g: pin(tmp_path, g + '-audit.json', a) for g, a in native_audits.items()},
            'native_flight': pin(tmp_path, 'flight.jsonl', flight, True)}
    repin()
    yield SimpleNamespace(rt=rt, c=c, grant=grant, start=start, end=end, fault=failure, audits=native_audits,
                          flight=flight, repin=repin, tmp_path=tmp_path)
    c.close()


def test_complete_independent_service_evidence_passes_and_is_hashed(bundle):
    b = bundle; result = verify(b.rt, b.c, b.grant)
    assert result['generations'] == 1 and result['fault_grid'] == 'fault'
    assert result['fill_floor'] == 1 and result['funding_floor'] == 2
    b.fault['replacement_seconds'] = 7; b.repin()
    assert verify(b.rt, b.c, b.grant)['proof_hash'] != result['proof_hash']


@pytest.mark.parametrize('fault_field,value', [('passed', False), ('closed_seconds', 190.233),
    ('replacement_seconds', 31), ('rpc_recovered_seconds', 181), ('owned_qty', '.2'), ('observed_ps_state', 'S')])
def test_fault_claim_cannot_override_actual_bound_or_held_native_position(bundle, fault_field, value):
    b = bundle; b.fault[fault_field] = value; b.repin()
    with pytest.raises(AdapterError, match='SERVICE_UNQUALIFIED'): verify(b.rt, b.c, b.grant)


@pytest.mark.parametrize('scenario', ['native_fee', 'native_missing_fill', 'native_duplicate', 'foreign_owner', 'nonzero_final',
    'late_open', 'wrong_open', 'fixed_stop', 'wrong_cancel_time', 'unconfirmed_generation', 'wrong_scope', 'wall_time_late',
    'watchdog_replaced', 'foreign_order', 'missing_bundle', 'hash_changed', 'permissions'])
def test_service_evidence_rejects_incomplete_changed_or_cross_scope_proof(bundle, scenario):
    b = bundle
    if scenario == 'native_fee': b.audits['loop']['matched_fills'][0]['fee'] = '.02'
    elif scenario == 'native_missing_fill': b.audits['loop']['matched_fills'].pop()
    elif scenario == 'native_duplicate': b.audits['loop']['matched_fills'][1] = b.audits['loop']['matched_fills'][0]
    elif scenario == 'foreign_owner': b.audits['fault']['owner'] = 'foreign'
    elif scenario == 'nonzero_final': b.audits['fault']['final_dexes'][0]['positions'] = [{'position': {'szi': '.1'}}]
    elif scenario == 'late_open': b.flight[0]['received_ms'] = 40000
    elif scenario == 'wrong_open': b.flight[0]['native']['order']['order']['oid'] = 70
    elif scenario == 'fixed_stop': b.flight[0]['native']['order']['order']['origSz'] = '.2'
    elif scenario == 'wrong_cancel_time': b.audits['loop']['native_stops'][0]['native']['order']['statusTimestamp'] = 9900
    elif scenario == 'unconfirmed_generation': b.c.execute("DELETE FROM events WHERE event_type='service_coverage_confirmed'")
    elif scenario == 'wrong_scope': b.c.execute("UPDATE runtime_state SET value='{}' WHERE key='operator-trial:fault'")
    elif scenario == 'wall_time_late': b.c.execute("UPDATE grids SET updated_ts='1970-01-01T00:03:30Z' WHERE grid_id='fault'")
    elif scenario == 'watchdog_replaced': b.fault['after_watchdog']['pid'] = 4
    elif scenario == 'foreign_order': b.fault['native_before']['orders'][0]['cloid'] = 'foreign'
    b.repin()
    if scenario == 'missing_bundle': b.grant.pop('service_evidence')
    if scenario == 'hash_changed': Path(b.grant['service_evidence']['fault_proof']['path']).write_text('{}')
    if scenario == 'permissions': Path(b.grant['service_evidence']['fault_proof']['path']).chmod(0o644)
    with pytest.raises(AdapterError, match='SERVICE_UNQUALIFIED'): verify(b.rt, b.c, b.grant)


def test_artifact_rejects_symlink_and_nonfinite_json(tmp_path):
    ref = pin(tmp_path, 'original', {'passed': True})
    link = tmp_path / 'link'; link.symlink_to(ref['path'])
    with pytest.raises(OSError): artifact({**ref, 'path': str(link)})
    path = tmp_path / 'nan'; path.write_bytes(b'{"value":NaN}'); path.chmod(0o600)
    with pytest.raises(AdapterError): artifact({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})


@pytest.mark.parametrize('change', ['lease_scope', 'lease_stops', 'late_confirmation', 'wrong_epoch', 'excess_cap', 'extended_deadline'])
def test_immutable_window_proof_must_keep_scope_deadline_and_cap(bundle, change):
    b=bundle
    if change=='lease_scope': b.end['native_lease']['config_hash']='other'
    if change=='lease_stops': b.end['native_lease']['stops']={'old':'7'}
    if change=='late_confirmation': b.end['confirmed_ms']=b.start['deadline_ms']
    if change=='wrong_epoch': b.end['epoch']=2
    if change=='excess_cap': b.end['confirmed_notional']='150.01'
    if change=='extended_deadline': b.start['deadline_ms']+=1000
    b.c.execute('UPDATE events SET payload_json=? WHERE event_type=?',(json.dumps(b.start),'service_coverage_started'))
    b.c.execute('UPDATE events SET payload_json=? WHERE event_type=?',(json.dumps(b.end),'service_coverage_confirmed'))
    with pytest.raises(AdapterError,match='SERVICE_UNQUALIFIED'):verify(b.rt,b.c,b.grant)


def working_fault(bundle):
    b=bundle;c=b.c;gid='fault';account=b.grant['account_id'];cid='working-entry'
    c.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ('working','fault','initial:working',0,'gtc','CANCELED','buy',1,'.1','99',cid,None,0))
    c.execute('INSERT INTO exchange_orders VALUES(?,?,?,?)',('working',account,cid,'99'))
    order={'oid':99,'cloid':cid,'coin':b.grant['market'].split('|')[2],'side':'B','reduceOnly':False,
           'origSz':'.1','sz':'.1','limitPx':'99','isPositionTpsl':False}
    native=b.fault['native_before'];native['orders'].append(order)
    manifest={}
    for o in native['orders']:
        row=c.execute('SELECT i.*,o.exchange_order_id FROM order_intents i JOIN exchange_orders o ON o.intent_id=i.intent_id WHERE i.grid_id=? AND o.client_id=?',(gid,o['cloid'])).fetchone()
        manifest[o['cloid']]={k:row[k] for k in ('intent_id','grid_id','side','qty_base','price','reduce_only','client_id','exchange_order_id')}
    b.fault['risk_profile']='owned_working_orders';native['intent_manifest']=manifest;native['exposure_bound']='20'
    return native,order


def test_fault_with_owned_working_entry_qualifies_only_inside_gross_cap(bundle):
    native,order=working_fault(bundle);bundle.repin()
    assert verify(bundle.rt,bundle.c,bundle.grant)['fault_grid']=='fault'


@pytest.mark.parametrize('change',['foreign_oid','wrong_manifest','notional_limit','unclassified_entry'])
def test_held_fault_working_orders_require_native_identity_and_full_gross_bound(bundle,change):
    b=bundle;native,order=working_fault(b)
    if change=='foreign_oid':order['oid']=10000
    if change=='wrong_manifest':native['intent_manifest'][order['cloid']]['grid_id']='other'
    if change=='notional_limit':
        order['limitPx']='1600';native['intent_manifest'][order['cloid']]['price']='1600'
        b.c.execute("UPDATE order_intents SET price='1600' WHERE intent_id='working'")
    if change=='unclassified_entry':b.fault.pop('risk_profile')
    b.repin()
    with pytest.raises(AdapterError,match='SERVICE_UNQUALIFIED'):verify(b.rt,b.c,b.grant)
