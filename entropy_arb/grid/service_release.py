"""Verify pinned private native evidence for a service-assisted live release.

Evidence references are accepted only in the local startup policy. Reading or
verifying a bundle never enables dispatch; LiveRelease still checks identity,
closed causal loops, tiny closeout and the current independent watchdog.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
from decimal import Decimal

from . import models
from .adapters.base import AdapterError


CODE = 'LIVE_RELEASE_SERVICE_UNQUALIFIED'


def require(condition):
    if not condition:
        raise AdapterError(CODE)


def artifact(reference, *, lines=False):
    require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'})
    require(isinstance(reference['path'], str) and Path(reference['path']).is_absolute())
    require(isinstance(reference['sha256'], str) and len(reference['sha256']) == 64)
    fd = os.open(reference['path'], os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as file:
        info = os.fstat(file.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077)
        raw = file.read(8 * 1024 * 1024 + 1)
    require(len(raw) <= 8 * 1024 * 1024 and hashlib.sha256(raw).hexdigest() == reference['sha256'])
    def invalid(_):
        raise AdapterError(CODE)
    parse = lambda data: json.loads(data, parse_constant=invalid)
    return [parse(line) for line in raw.splitlines() if line.strip()] if lines else parse(raw)


def fills(c, gid):
    return [dict(r) for r in c.execute(
        'SELECT e.*,i.cause_key,i.reduce_only,i.policy FROM executions e '
        'JOIN execution_owners x ON x.execution_key=e.execution_key '
        'JOIN exchange_orders o ON o.account=e.account AND o.client_id=e.order_client_id '
        'JOIN order_intents i ON i.intent_id=o.intent_id WHERE x.grid_id=? '
        'ORDER BY e.exchange_ts,e.execution_key', (gid,))]


def timestamp(value):
    return int(models.parse_iso_ts(value, 'evidence.timestamp').timestamp() * 1000)


def closed(rt, c, gid, grant):
    row = c.execute('SELECT * FROM grids WHERE grid_id=?', (gid,)).fetchone()
    context = c.execute('SELECT value FROM runtime_state WHERE key=?', ('operator-trial:' + gid,)).fetchone()
    require(row is not None and row['status'] == 'CLOSED' and context is not None)
    cfg = rt.config(gid)
    require((row['account_id'], row['market_key']) == (grant['account_id'], grant['market']))
    require(cfg.protection and cfg.protection.service_coverage
            and cfg.protection.service_coverage.to_dict() == grant['service_coverage'])
    context = json.loads(context[0])
    require((context['account'], context['market'], context['credential_revision'], context['config_hash']) ==
            (grant['account_id'], grant['market'], grant['credential_revision'], cfg.config_hash))
    require(not c.execute("SELECT 1 FROM order_intents WHERE grid_id=? AND state NOT IN "
        "('FILLED','CANCELED','REJECTED','EXPIRED_EPOCH')", (gid,)).fetchone())
    require(all(Decimal(r[0]) == 0 for r in c.execute('SELECT qty_remaining FROM inventory_lots WHERE grid_id=?', (gid,))))
    return cfg, dict(row)


def native_audit(rt, c, gid, grant, reference):
    audit = artifact(reference)
    cfg, _ = closed(rt, c, gid, grant)
    account = c.execute('SELECT owner FROM accounts WHERE account_id=?', (grant['account_id'],)).fetchone()
    require(audit['grid_id'] == gid and audit['config_hash'] == cfg.config_hash
            and audit['account_id'] == grant['account_id'] and audit['owner'] == account[0]
            and audit['network'] == 'mainnet')
    rows = fills(c, gid)
    native = audit['matched_fills']
    require(isinstance(native, list) and len(native) == len(rows) and rows)
    require(len({(str(n['oid']), str(n['tid'])) for n in native}) == len(native))
    for row in rows:
        hits = [n for n in native if (str(n['oid']), str(n['tid'])) == (row['exchange_order_id'], row['trade_id'])]
        require(len(hits) == 1)
        n = hits[0]
        require(n['cloid'] == row['order_client_id'] and n['coin'] == grant['market'].split('|')[2]
                and n['side'] == ('B' if row['side'] == 'buy' else 'A')
                and n['crossed'] is (not bool(row['is_maker']))
                and n['engine_cause'] == row['cause_key'] and row['finality'] == 'final'
                and row['execution_key'].startswith('hl:mainnet:')
                and type(n['time']) is int and abs(n['time'] - timestamp(row['exchange_ts'])) <= 1)
        require(all(Decimal(n[a]) == Decimal(row[b]) for a, b in
                    [('sz', 'qty_base'), ('px', 'price'), ('fee', 'fee_amount')]))
    dexes = audit['final_dexes']
    require(isinstance(dexes, list) and dexes and len({d['dex'] for d in dexes}) == len(dexes))
    require(all(not d['orders'] and all(Decimal(p['position']['szi']) == 0 for p in d['positions']) for d in dexes))
    return audit


def coverage_windows(rt, c, gid, grant, audit, flight_ref):
    cfg = rt.config(gid)
    flight = artifact(flight_ref, lines=True)
    starts, confirms = {}, {}
    events = [dict(r) for r in c.execute("SELECT * FROM events WHERE scope=? AND event_type IN "
        "('service_coverage_started','service_coverage_confirmed') ORDER BY seq", (gid,))]
    for e in events:
        p = json.loads(e['payload_json']); target = starts if e['event_type'] == 'service_coverage_started' else confirms
        require(p['generation'] not in target)
        target[p['generation']] = (e, p)
    require(starts and set(starts) == set(confirms))
    required_sides = {s for s, v in [('sell', cfg.protection.lower_stop), ('buy', cfg.protection.upper_stop)] if v is not None}
    verified = []
    for generation, (se, start) in starts.items():
        ce, end = confirms[generation]
        require(se['seq'] < ce['seq'] and start['epoch'] == end['epoch'] and start['policy'] == grant['service_coverage'])
        require(type(start['started_ms']) is int and type(end['confirmed_ms']) is int
                and start['deadline_ms'] == start['started_ms'] + grant['service_coverage']['max_seconds'] * 1000
                and start['started_ms'] <= end['confirmed_ms'] < start['deadline_ms'])
        cap = Decimal(grant['service_coverage']['max_open_notional'])
        require(0 <= Decimal(start['open_notional']) <= cap and 0 <= Decimal(end['confirmed_notional']) <= cap)
        old = [s for s in audit['native_stops'] if s['intent_id'] == start['stop_intent_id']]
        require(len(old) == 1 and old[0]['exchange_order_id'] == start['stop_oid'])
        status = old[0]['native']
        require(status['status'] == 'order' and status['order']['status'] == 'reduceOnlyCanceled'
                and status['order']['statusTimestamp'] == start['started_ms'])
        old_row = c.execute('SELECT i.*,o.exchange_order_id FROM order_intents i JOIN exchange_orders o '
            'ON o.intent_id=i.intent_id WHERE i.intent_id=? AND i.grid_id=?', (start['stop_intent_id'],gid)).fetchone()
        require(old_row is not None)
        old_stop = rt.journal.decorate(c,old_row); old_native = status['order']['order']
        require(str(old_native['oid']) == start['stop_oid'] == old_stop['exchange_order_id']
                and old_native['cloid'] == old_stop['client_id'] and old_native['reduceOnly'] is True
                and old_native['isPositionTpsl'] is True and old_native['isTrigger'] is True
                and old_native['coin'] == grant['market'].split('|')[2]
                and old_native['side'] == ('A' if old_stop['side'] == 'sell' else 'B')
                and Decimal(old_native['origSz']) == 0 and old_stop['whole_position'] and old_stop['reduce_only']
                and Decimal(old_native['triggerPx']) == Decimal(old_stop['trigger_price']))
        lease = end['native_lease']
        require(lease['config_hash'] == cfg.config_hash and lease['account'] == cfg.account_ref
                and lease['market'] == cfg.market_ref and lease['epoch'] == start['epoch']
                and lease['stops'] == end['new_stops']
                and start['started_ms'] <= lease['confirmed_from_ms'] <= lease['observed_ms'] <= end['confirmed_ms']
                and lease['deadline_ms'] == lease['confirmed_from_ms'] + grant['service_coverage']['max_seconds'] * 1000)
        seen = set()
        for iid, oid in end['new_stops'].items():
            row = c.execute('SELECT i.*,o.exchange_order_id FROM order_intents i JOIN exchange_orders o '
                'ON o.intent_id=i.intent_id WHERE i.intent_id=? AND i.grid_id=?', (iid, gid)).fetchone()
            require(row is not None)
            stop = rt.journal.decorate(c, row)
            side = stop['side']; trigger = cfg.protection.lower_stop if side == 'sell' else cfg.protection.upper_stop
            require(side in required_sides and side not in seen and stop['reduce_only'] and stop['whole_position']
                    and stop['epoch'] == start['epoch'] and stop['exchange_order_id'] == oid
                    and iid != start['stop_intent_id'] and oid != start['stop_oid'] and stop['trigger_price'] == trigger)
            seen.add(side)
            matches = [f for f in flight if f['grid_id'] == gid and f['intent']['intent_id'] == iid
                       and type(f['request_ms']) is int and type(f['received_ms']) is int
                       and start['started_ms'] <= f['request_ms'] <= f['received_ms'] < start['deadline_ms']
                       and f['native'].get('status') == 'order' and f['native']['order']['status'] == 'open']
            require(matches)
            valid = False
            for f in matches:
                n = f['native']['order']['order']
                valid |= (str(n['oid']) == oid and n['cloid'] == stop['client_id']
                          and n['coin'] == grant['market'].split('|')[2] and n['side'] == ('A' if side == 'sell' else 'B')
                          and n['reduceOnly'] is True and n['isPositionTpsl'] is True and n['isTrigger'] is True
                          and n['orderType'] == 'Stop Market' and Decimal(n['origSz']) == 0 and Decimal(n['sz']) == 0
                          and Decimal(n['triggerPx']) == Decimal(trigger) and Decimal(n['limitPx']) == Decimal(stop['price']))
            require(valid)
        require(seen == required_sides)
        verified.append({'start': start, 'confirmation': end})
    return verified


def fault(rt, c, gid, grant, reference):
    proof = artifact(reference)
    cfg, row = closed(rt, c, gid, grant)
    require(proof['grid_id'] == gid and proof['kind'] == 'position_sigstop' and proof['schema_version'] == 1
            and proof['passed'] is True and not proof.get('failure') and not proof.get('fallback_sigcont')
            and 'T' in proof['observed_ps_state'])
    sent = proof['sent_ms']; require(type(sent) is int)
    require(all(type(proof[k]) in (int, float) for k in ('replacement_seconds','rpc_recovered_seconds','closed_seconds')))
    require(0 < proof['replacement_seconds'] <= 30 and proof['replacement_seconds'] <= proof['rpc_recovered_seconds'] <= proof['closed_seconds']
            and 0 < proof['closed_seconds'] <= 180 and 0 <= timestamp(row['updated_ts']) - sent <= 180000)
    before, after, hb, marker = (proof[k] for k in ('before', 'after', 'heartbeat', 'marker'))
    require(before['loaded'] is True and after['loaded'] is True and before['scope_valid'] is True and after['scope_valid'] is True
            and type(before['pid']) is int and type(after['pid']) is int and before['pid'] > 0 and after['pid'] > 0
            and before['pid'] != after['pid'] and before['birth'] != after['birth'])
    require((hb['pid'], hb['birth'], hb['phase']) == (before['pid'], before['birth'], 'running')
            and (marker['pid'], marker['birth'], marker['instance']) == (before['pid'], before['birth'], hb['instance'])
            and proof['before_watchdog']['pid'] == proof['after_watchdog']['pid'])
    recovery = c.execute('SELECT payload_json FROM events WHERE event_key=?', ('watchdog-recovery:' + marker['marker_id'],)).fetchone()
    require(recovery is not None)
    recovery = json.loads(recovery[0])
    require(recovery == proof['recovery_event'] and gid in recovery['grids'] and recovery['target_instance'] == hb['instance'])
    native = proof['native_before']; qty = Decimal(proof['owned_qty'])
    require(qty != 0 and 0 <= sent - native['state']['time'] < 5000)
    positions = [r['position'] for r in native['state']['assetPositions'] if Decimal(r['position']['szi'])]
    require(len(positions) == 1 and positions[0]['coin'] == grant['market'].split('|')[2]
            and Decimal(positions[0]['szi']) == qty
            and abs(Decimal(positions[0]['positionValue'])) <= Decimal(grant['service_coverage']['max_open_notional']))
    rows = fills(c, gid)
    held = sum((Decimal(r['qty_base']) * (1 if r['side'] == 'buy' else -1)
                for r in rows if timestamp(r['exchange_ts']) <= sent), Decimal(0))
    require(held == qty and any(r['cause_key'].startswith('exit-flat:') and r['reduce_only']
            and sent <= timestamp(r['exchange_ts']) <= sent + 180000 for r in rows))
    owned = {r[0] for r in c.execute('SELECT client_id FROM exchange_orders WHERE account=?', (cfg.account_ref,))}
    require(native['orders'] and all(o['cloid'] in owned for o in native['orders']))
    if proof.get('risk_profile')=='owned_working_orders':
        manifest=native['intent_manifest']
        require(set(manifest)=={o['cloid'] for o in native['orders']}
                and len(manifest)==len(native['orders']))
        mark=abs(Decimal(positions[0]['positionValue'])/qty)
        gross=abs(Decimal(positions[0]['positionValue']))
        for order in native['orders']:
            bound=c.execute('SELECT i.*,o.exchange_order_id FROM order_intents i JOIN exchange_orders o '
                'ON o.intent_id=i.intent_id WHERE i.grid_id=? AND o.client_id=? AND o.exchange_order_id=?',
                (gid,order['cloid'],str(order['oid']))).fetchone()
            require(bound is not None)
            item=rt.journal.decorate(c,bound)
            require(manifest[order['cloid']]=={k:item[k] for k in
                ('intent_id','grid_id','side','qty_base','price','reduce_only','client_id','exchange_order_id')})
            expected_qty=Decimal(0) if item.get('whole_position') else Decimal(item['qty_base'])
            require(order['coin']==grant['market'].split('|')[2] and order['side']==('A' if item['side']=='sell' else 'B')
                    and order['reduceOnly'] is bool(item['reduce_only']) and Decimal(order['origSz'])==expected_qty
                    and Decimal(order['limitPx'])==Decimal(item['price']))
            if not order['reduceOnly']:
                require(expected_qty>0 and not item.get('whole_position'))
                gross+=expected_qty*max(mark,Decimal(order['limitPx']))
        require(gross<=Decimal(grant['service_coverage']['max_open_notional'])
                and abs(Decimal(positions[0]['positionValue']))<=Decimal(native['exposure_bound'])
                <=Decimal(grant['service_coverage']['max_open_notional']))
    else:
        require(all(o['reduceOnly'] is True for o in native['orders']))
    stop_status = native['stop']; require(stop_status['status'] == 'order' and stop_status['order']['status'] == 'open')
    n = stop_status['order']['order']; side = 'sell' if qty > 0 else 'buy'
    trigger = cfg.protection.lower_stop if qty > 0 else cfg.protection.upper_stop
    stop = c.execute('SELECT i.* FROM order_intents i JOIN exchange_orders o ON o.intent_id=i.intent_id '
        'WHERE i.grid_id=? AND o.client_id=? AND o.exchange_order_id=?', (gid,n['cloid'],str(n['oid']))).fetchone()
    require(stop is not None)
    stop = rt.journal.decorate(c,stop)
    require(n in native['orders'] and n['isPositionTpsl'] is True and n['isTrigger'] is True
            and n['side'] == ('A' if side == 'sell' else 'B') and Decimal(n['origSz']) == 0
            and Decimal(n['triggerPx']) == Decimal(trigger) and Decimal(n['limitPx']) == Decimal(stop['price'])
            and stop['whole_position'] and stop['reduce_only'] and stop['side'] == side and stop['trigger_price'] == trigger)
    return proof


def verify(rt, c, grant):
    """No native-only evidence or cached success can bypass this verifier."""
    try:
        evidence = grant.get('service_evidence')
        require(isinstance(evidence, dict) and set(evidence) == {'fault_grid', 'fault_proof', 'native_audits', 'native_flight'})
        fault_gid = evidence['fault_grid']
        require(isinstance(fault_gid, str) and fault_gid not in (grant['loop_grid'], grant['small_close_grid']))
        gids = {grant['loop_grid'], grant['small_close_grid'], fault_gid}
        require(set(evidence['native_audits']) == gids)
        audits = {gid: native_audit(rt, c, gid, grant, evidence['native_audits'][gid]) for gid in sorted(gids)}
        loop_cfg = rt.config(grant['loop_grid']); fault_cfg = rt.config(fault_gid)
        require((loop_cfg.direction, loop_cfg.execution_policy, loop_cfg.margin_mode, loop_cfg.leverage) ==
                (fault_cfg.direction, fault_cfg.execution_policy, fault_cfg.margin_mode, fault_cfg.leverage))
        windows = coverage_windows(rt, c, grant['loop_grid'], grant, audits[grant['loop_grid']], evidence['native_flight'])
        failure = fault(rt, c, fault_gid, grant, evidence['fault_proof'])
        context = json.loads(c.execute('SELECT value FROM runtime_state WHERE key=?',
            ('operator-trial:' + fault_gid,)).fetchone()[0])
        return {'proof_hash': models.sha256_hex({'audits': audits, 'windows': windows, 'fault': failure}),
                'generations': len(windows), 'fault_grid': fault_gid,
                'fill_floor': context['history']['retained_fill_ms'],
                'funding_floor': context['history']['clean_funding_anchor_ms']}
    except AdapterError:
        raise
    except (KeyError, TypeError, ValueError, OSError, ArithmeticError):
        raise AdapterError(CODE) from None
