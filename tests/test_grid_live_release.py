"""Native release qualification: causal evidence, scope, web creation and revocation.

All exchange responses are in-memory and all stores are temporary. Real-world
qualification is separately verified against the actual native engine trials.
"""
import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from entropy_arb.grid import models
from entropy_arb.grid.live_release import LiveRelease
from entropy_arb.grid.operator_trial import OperatorTrial
from entropy_arb.grid.adapters.base import AdapterError
from test_grid_operator_trial import trial, TrialExchange
from test_grid_hyperliquid import raw_fill


async def release_fixture(trial, *, refill_buy_maker=True, resting_buy=False, execution_policy='gtc', extra_cycles=0):
    rt,root,old,cfg=trial
    t=TrialExchange();root.transport=t
    async def sign(action,deadline):return {'action':action,'nonce':30000,'expiresAfter':deadline}
    root.signer=SimpleNamespace(sign=sign);root.control_store=rt.store
    service=OperatorTrial(rt,{**old.policy,'cumulative_limit':'60', 'small_close_probe':True,
        'modes':[{'direction':'long','initial_position':'immediate'}],'execution_policies':[execution_policy]})
    rt.operator_trial=service
    config={**cfg.to_dict(),'direction':'long','initial_position':'immediate','execution_policy':execution_policy,
        'protection':{'stop_loss':cfg.to_dict()['protection']['stop_loss']}}
    config['grid_count']=2
    config['lower_price']='99900';config['upper_price']='100300'
    raw_rows=[];counter=0
    async def fill(gid,cause,side,qty,price,policy=None,maker=True):
        nonlocal counter
        policy = execution_policy if policy is None else policy
        counter+=1
        actor=rt.actors[gid]
        intent=await rt.journal.create_intent(grid_id=gid,cause_key=cause,revision=1,level_index=1,
            side=side,qty_base=qty,price=price,policy=policy,reduce_only=side=='sell',epoch=actor.epoch)
        cid=await rt.executors[config['account_ref']]._client_id(intent['intent_id'])
        oid,tid=70000+counter,80000+counter
        await rt.journal.bind_exchange_order(account=config['account_ref'],client_id=cid,intent_id=intent['intent_id'],exchange_order_id=str(oid))
        raw=raw_fill(cloid=cid,oid=oid,tid=tid,time=30000,side='B' if side=='buy' else 'A',sz=qty,px=price,fee='0',crossed=not maker)
        ev=rt.venue_for(config['account_ref'],config['market_ref']).normalize_fill(raw,source='rest_backfill')
        await actor.apply_fill(ev);raw_rows.append(raw)
        return ev,intent
    async def close_record(gid):
        for typ in ('pause','resume','stop_flat'):
            op=await rt.lifecycle._begin_operation(gid,typ,'historical-'+gid+'-'+typ)
            await rt.lifecycle._finish_operation(op['operation_id'],'succeeded')
        await rt.lifecycle._set_status(gid,'CLOSED',desired='STOPPED')
        await rt.store.write(lambda c:c.execute("UPDATE budget_reservations SET reserved_amount='0' WHERE grid_id=?",(gid,)))
        await rt.store.write(lambda c:c.execute("UPDATE scope_claims SET state='released' WHERE scope_key=?",('market:'+config['account_ref']+':'+config['market_ref'],)))
    loop=(await service.start({'request_id':'trial-one','config':config}))['grid_id']
    qty=rt.grid_snapshot(loop)['plan']['initial_position_qty']
    first,_=await fill(loop,'initial-position:'+loop,'buy',qty,'100000','ioc',False)
    if resting_buy:
        await fill(loop,'initial:resting-buy-proof','buy',qty,'100000')
        await fill(loop,'initial:resting-sell-proof','sell',qty,'100300')
    exit1,_=await fill(loop,'initial:exit:'+loop,'sell',qty,'100300')
    entry2,_=await fill(loop,'refill:'+exit1.execution_key,'buy',qty,'100100',maker=refill_buy_maker)
    last,_=await fill(loop,'refill:'+entry2.execution_key,'sell',qty,'100300')
    for _ in range(extra_cycles):
        entry,_=await fill(loop,'refill:'+last.execution_key,'buy',qty,'100100')
        last,_=await fill(loop,'refill:'+entry.execution_key,'sell',qty,'100300')
    await close_record(loop)
    small=(await service.start({'request_id':'trial-two','config':config}))['grid_id']
    qty=rt.grid_snapshot(small)['plan']['initial_position_qty'];stage_qty=models.canonical_decimal_text(Decimal(qty)-Decimal('0.00001'))
    await fill(small,'initial-position:'+small,'buy',qty,'100000','ioc',False)
    await fill(small,'exit-flat:'+small+':stage','sell',stage_qty,'100000','reduce_only_ioc',False)
    tiny,intent=await fill(small,'exit-flat:'+small+':tiny','sell','0.00001','100000','reduce_only_ioc',False)
    await close_record(small)
    stop=rt.store.read(lambda c:c.execute("SELECT operation_id FROM operations WHERE grid_id=? AND op_type='stop_flat'",(small,)).fetchone()[0])
    probe={'operation_id':stop,'starting_net':qty,'stage_qty':stage_qty,'target_residual':'0.00001','stage_cause':'exit-flat:'+small+':stage'}
    await rt.store.write(lambda c:c.execute('INSERT INTO runtime_state(key,value) VALUES(?,?)',('small-close-probe:'+small,json.dumps(probe))))
    original=t.post
    async def post(path,data):
        if data.get('type')=='orderStatus' and data['oid']==tiny.order_client_id:
            return {'status':'order','order':{'status':'filled','order':{'coin':'BTC','cloid':tiny.order_client_id,
                'oid':int(tiny.exchange_order_id),'side':'A','reduceOnly':True,'tif':'Ioc','origSz':'0.00001','sz':'0'}}}
        if data.get('type')=='userFills':
            return [{**raw,'startPosition':'0.00001'} if str(raw['tid'])==tiny.trade_id else raw for raw in raw_rows]+[{'time':10000}]
        return await original(path,data)
    t.post=post
    grant={'account_id':config['account_ref'],'credential_revision':root._account.credential_revision,'market':config['market_ref'],
        'loop_grid':loop,'small_close_grid':small,'modes':['long:deferred','long:immediate'],'execution_policies':[execution_policy],'capital_limit':'50'}
    release=LiveRelease(rt,{'schema_version':1,'grants':[grant]});rt.live_release=release
    return rt,root,t,release,grant,models.GridConfig.from_dict({**config,'initial_position':'deferred'}),tiny


def test_native_qualification_cannot_authorize_a_service_profile_even_with_cached_proof(trial,monkeypatch):
    from dataclasses import replace
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt,root,t,release,grant,cfg,tiny=await release_fixture(trial)
        assert not await release.restore() and release.permits_config(cfg)
        assert next(iter(release.grants))==models.sha256_hex(grant)
        service=models.ServiceCoveragePolicy('100',10)
        covered=replace(cfg,protection=replace(cfg.protection,service_coverage=service))
        assert release.grant(cfg.account_ref,cfg.market_ref,config=covered) is None
        assert not release.permits_config(covered)
        scoped=LiveRelease(rt,{'schema_version':1,'grants':[{**grant,'service_coverage':service.to_dict()}]})
        candidate=next(iter(scoped.grants.values()))
        # Even a verified-cache entry cannot authorize this profile while
        # the independent guardian is absent. This is an offline store and
        # in-memory exchange only; no real qualification is written.
        scoped.verified[candidate['grant_id']]={}
        rt.service_guardian_status=lambda:{'healthy':False}
        assert not scoped.permits_config(covered)
        assert release.permits_config(cfg)
        rt.service_guardian_status=lambda:{'healthy':True}
        assert scoped.permits_config(covered)
        rt.service_guardian_status=lambda:{'healthy':False}
        assert not scoped.permits_config(covered)
        with pytest.raises(AdapterError,match='LIVE_RELEASE_PROTECTION_SCOPE'):
            rt.store.read(lambda c:scoped.evidence(c,candidate))
        # Relabeling historical configs is still insufficient: the new
        # process-failure and coverage proof has not been established.
        for gid in (grant['loop_grid'],grant['small_close_grid']):
            old=rt.config(gid)
            changed=replace(old,protection=replace(old.protection,service_coverage=service))
            await rt.store.write(lambda c,gid=gid,changed=changed:c.execute(
                'UPDATE grid_versions SET config_json=? WHERE grid_id=?',(json.dumps(changed.to_dict()),gid)))
        with pytest.raises(AdapterError,match='LIVE_RELEASE_SERVICE_UNQUALIFIED'):
            rt.store.read(lambda c:scoped.evidence(c,candidate))
    asyncio.run(run())


def test_native_evidence_qualifies_web_preview_and_atomic_scope_and_revocation_keeps_cleanup(trial,monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt,root,t,release,grant,cfg,tiny=await release_fixture(trial)
        assert not await release.restore()
        assert release.permits_config(cfg)
        preview=await rt.preview(cfg);assert preview['plan'].live_eligible
        made=await rt.create_grid(config=cfg,preview_id=preview['preview_id'],request_id='ordinary-web-create',confirm_market=cfg.market_ref)
        gid=made['grid_id'];assert release.record(gid) and rt.operator_trial.record(gid) is None
        assert release.permits_grid(gid)
        root.stream_connected=True;root.stream_generation=1;root.stream_gap=False
        ex=rt.executors[cfg.account_ref]
        epoch=await rt.lifecycle._bump_epoch(gid,status='STARTING',desired='ACTIVE')
        risk=await rt.journal.create_intent(grid_id=gid,cause_key='initial:public-risk',revision=1,level_index=0,
            side='buy',qty_base='0.00014',price='99800',policy='gtc',reduce_only=False,epoch=epoch)
        scoped=await ex._intent_with_market(risk['intent_id']);assert ex._send_gate(scoped) is None
        release.grants.clear()
        assert not release.permits_grid(gid) and ex._send_gate(scoped)=='CAPABILITY_UNVERIFIED'
        assert (await rt.live_risk.observe(gid))['code']=='RISK_LIVE_APPROVAL_REMOVED'
        await rt.lifecycle._bump_epoch(gid,status='STOPPING',desired='STOPPED')
        close=await rt.journal.create_intent(grid_id=gid,cause_key='exit-flat:public-cleanup',revision=1,level_index=0,
            side='sell',qty_base='0.00001',price='99700',policy='reduce_only_ioc',reduce_only=True,epoch=epoch+1)
        assert ex._send_gate(await ex._intent_with_market(close['intent_id'])) is None
        assert release.permits_small_close(gid)
        monkeypatch.setenv('GRID_REAL_DISPATCH','0')
        assert not release.permits_config(cfg)
        assert ex._send_gate(await ex._intent_with_market(close['intent_id'])) is None
        rt.live_release=LiveRelease(rt)
        assert not await rt.live_release.restore()
        assert rt.live_release.permits_grid(gid,closing=True)
        assert rt.venue_for(cfg.account_ref,cfg.market_ref).funding_retention_floor_ms is not None
    asyncio.run(run())


def test_account_recheck_keeps_release_binding_and_rejects_changed_credentials(trial, monkeypatch):
    from dataclasses import replace
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt, root, transport, release, grant, cfg, tiny = await release_fixture(trial)
        assert not await release.restore()
        aid = cfg.account_ref
        rt.venues.pop(aid)
        rt.executors.pop(aid)
        del root.live_release
        root.views.clear()
        await rt.add_venue(root)
        assert root.live_release is release
        assert not await release.restore()
        assert rt.venue_for(aid, cfg.market_ref).live_qualified
        assert rt.venue_for(aid, cfg.market_ref).retention_floor_ms is not None
        assert rt.venue_for(aid, cfg.market_ref).funding_retention_floor_ms is not None
        assert release.permits_config(cfg)
        root.account_mode_confirmed = False
        assert not release.permits_config(cfg)
        root.account_mode_confirmed = True
        rt.venues.pop(aid)
        rt.executors.pop(aid)
        root._account = replace(root._account, credential_revision='changed')
        del root.live_release
        await rt.add_venue(root)
        assert not rt.venue_for(aid, cfg.market_ref).live_qualified
        assert not release.permits_config(cfg)
    asyncio.run(run())


def test_reservation_growth_obeys_release_cap_even_with_extra_account_cash(trial, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt, root, transport, release, grant, cfg, tiny = await release_fixture(trial)
        assert not await release.restore()
        preview = await rt.preview(cfg)
        made = await rt.create_grid(config=cfg, preview_id=preview['preview_id'], request_id='capital-scope', confirm_market=cfg.market_ref)
        gid = made['grid_id']
        await rt.store.write(lambda c: rt.check_budget(c, cfg.account_ref, 'USDC', '50', Decimal('1000'), exclude=gid))
        with pytest.raises(AdapterError, match='LIVE_RELEASE_CAPITAL_LIMIT'):
            await rt.store.write(lambda c: rt.check_budget(c, cfg.account_ref, 'USDC', '51', Decimal('1000'), exclude=gid))
        monkeypatch.setenv('GRID_REAL_DISPATCH', '0')
        with pytest.raises(AdapterError, match='LIVE_RELEASE_CAPITAL_LIMIT'):
            await rt.store.write(lambda c: rt.check_budget(c, cfg.account_ref, 'USDC', '31', Decimal('1000'), exclude=gid))
        await rt.store.write(lambda c: rt.check_budget(c, cfg.account_ref, 'USDC', '29', Decimal('1000'), exclude=gid))
        assert rt.store.read(lambda c: c.execute('SELECT reserved_amount FROM budget_reservations WHERE grid_id=?', (gid,)).fetchone()[0]) == '30'
    asyncio.run(run())


@pytest.mark.parametrize('change',['scope','not_flat','loop_cause','missing_recovery','native_qty','missing_native_fill','mode'])
def test_release_refuses_incomplete_or_misbound_evidence(trial,monkeypatch,change):
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt,root,t,release,grant,cfg,tiny=await release_fixture(trial)
        if change=='scope':
            await rt.store.write(lambda c:c.execute("UPDATE runtime_state SET value=json_set(value,'$.credential_revision','changed') WHERE key=?",('operator-trial:'+grant['loop_grid'],)))
        elif change=='not_flat':
            await rt.store.write(lambda c:c.execute("UPDATE inventory_lots SET qty_remaining='0.00001' WHERE grid_id=?",(grant['loop_grid'],)))
        elif change=='loop_cause':
            await rt.store.write(lambda c:c.execute("UPDATE order_intents SET cause_key='refill:invented-source' WHERE grid_id=? AND side='buy' AND cause_key LIKE 'refill:%'",(grant['loop_grid'],)))
        elif change=='missing_recovery':
            await rt.store.write(lambda c:c.execute("UPDATE operations SET status='failed' WHERE grid_id=? AND op_type='resume'",(grant['loop_grid'],)))
        elif change=='mode':
            release=LiveRelease(rt,{'schema_version':1,'grants':[{**grant,'modes':['short:deferred']}]})
        else:
            original=t.post
            async def bad_post(path,data):
                result=await original(path,data)
                if change=='native_qty' and data.get('type')=='orderStatus':result['order']['order']['origSz']='0.00002'
                if change=='missing_native_fill' and data.get('type')=='userFills':result=[r for r in result if str(r.get('tid'))!=tiny.trade_id]
                return result
            t.post=bad_post
        failures=await release.restore()
        assert failures and not release.grant(cfg.account_ref,cfg.market_ref)
        assert not release.permits_config(cfg)
        assert not (await rt.preview(cfg))['plan'].live_eligible
    asyncio.run(run())


def test_ordinary_released_grid_starts_and_stops_through_the_normal_rpc_path(trial,monkeypatch,tmp_path):
    from entropy_arb.grid.rpc import RuntimeRpcServer, RuntimeClient
    import tempfile
    import shutil
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt,root,t,release,grant,cfg,tiny=await release_fixture(trial)
        assert not await release.restore()
        root.stream_connected=True;root.stream_generation=1;root.stream_gap=False
        directory=tempfile.mkdtemp(prefix='glr-',dir='/tmp')
        socket=directory+'/public.sock'
        server=RuntimeRpcServer(rt,socket);await server.start()
        client=RuntimeClient(socket)
        try:
            preview=await client.call('preview',{'config':cfg.to_dict()})
            assert preview['plan']['live_eligible']
            created=await client.call_idempotent('create',{'config':cfg.to_dict(),'preview_id':preview['preview_id'],
                'confirm_market':cfg.market_ref},'web-normal-create')
            gid=created['grid_id']
            started=await client.call_idempotent('start',{'grid_id':gid},'web-normal-start')
            assert started['status']=='succeeded' and started['placed']==1 and started['pending_orders']==1
            assert rt.operator_trial.record(gid) is None and release.record(gid)
            assert len(t.native_orders)==2  # native stop and one passive GTC entry
            ended=await client.call_idempotent('stop_flat',{'grid_id':gid},'web-normal-stop')
            assert ended['status']=='succeeded' and not t.native_orders
            assert rt.lifecycle._grid(gid)['status']=='CLOSED'
        finally:
            await client.close();await server.stop();shutil.rmtree(directory)
    asyncio.run(run())


def test_approval_removed_after_preview_cannot_be_consumed_and_feature_scope_is_honest(trial,monkeypatch):
    from entropy_arb.grid.store import StoreError
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt,root,t,release,grant,cfg,tiny=await release_fixture(trial)
        assert not await release.restore()
        preview=await rt.preview(cfg)
        for changed in [{'direction':'short','protection':{'upper_stop_loss':{'type':'price','source':'mark','operator':'gte','value':'101000'}}},{'execution_policy':'post_only'},{'leverage':4}]:
            unsupported=models.GridConfig.from_dict({**cfg.to_dict(),**changed})
            assert not release.permits_config(unsupported)
        release.grants.clear()
        with pytest.raises(AdapterError,match='LIVE_RELEASE_CREATION_UNAUTHORIZED'):
            await rt.create_grid(config=cfg,preview_id=preview['preview_id'],request_id='revoked-before-create',confirm_market=cfg.market_ref)
        assert rt.store.read(lambda c:c.execute('SELECT consumed_by FROM previews WHERE preview_id=?',(preview['preview_id'],)).fetchone()[0]) is None
        assert not rt.store.read(lambda c:c.execute("SELECT 1 FROM runtime_state WHERE key LIKE 'live-grid:%'").fetchone())
    asyncio.run(run())


@pytest.mark.parametrize('resting_buy', [False, True])
def test_gtc_taker_refill_requires_separate_real_resting_buy_evidence(trial, monkeypatch, resting_buy):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt, root, transport, release, grant, cfg, tiny = await release_fixture(
            trial, refill_buy_maker=False, resting_buy=resting_buy)
        failures = await release.restore()
        assert bool(failures) is not resting_buy
        assert bool(release.permits_config(cfg)) is resting_buy
        fills = rt.store.read(lambda c: release.evidence(c, grant)) if resting_buy else None
        if resting_buy:
            assert fills and any(r['side']=='buy' and not r['is_maker'] for r in rt.store.read(lambda c:
                [dict(r) for r in c.execute('SELECT e.side,e.is_maker FROM executions e JOIN execution_owners x ON x.execution_key=e.execution_key WHERE x.grid_id=?', (grant['loop_grid'],))]))
    asyncio.run(run())


def test_post_only_cannot_use_a_taker_refill_as_native_policy_proof(trial, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt, root, transport, release, grant, cfg, tiny = await release_fixture(
            trial, refill_buy_maker=False, resting_buy=True, execution_policy='post_only')
        assert await release.restore()
        assert not release.permits_config(cfg)
    asyncio.run(run())


def test_native_cycle_cannot_qualify_a_fill_outside_its_limit_price(trial, monkeypatch):
    monkeypatch.setenv('GRID_REAL_DISPATCH', '1')
    async def run():
        rt, root, transport, release, grant, cfg, tiny = await release_fixture(trial)
        await rt.store.write(lambda c: c.execute("UPDATE executions SET price='100101' WHERE execution_key IN "
            "(SELECT e.execution_key FROM executions e JOIN execution_owners x ON x.execution_key=e.execution_key "
            "JOIN exchange_orders o ON o.account=e.account AND o.client_id=e.order_client_id JOIN order_intents i ON i.intent_id=o.intent_id "
            "WHERE x.grid_id=? AND i.cause_key LIKE 'refill:%' AND e.side='buy')", (grant['loop_grid'],)))
        assert await release.restore()
        assert not release.permits_config(cfg)
    asyncio.run(run())


def test_same_market_grants_keep_direction_and_policy_evidence_separate(trial,monkeypatch):
    from dataclasses import replace
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt,root,transport,old,long_grant,cfg,tiny=await release_fixture(trial)
        short_grant={**long_grant,'modes':['short:immediate','short:deferred'],
                     'execution_policies':['post_only'],'capital_limit':'30'}
        release=LiveRelease(rt,{'schema_version':1,'grants':[long_grant,short_grant]})
        rt.live_release=release
        failures=await release.restore()
        assert len(failures)==1 # Long evidence cannot qualify short.
        assert release.permits_config(cfg)
        protection=models.GridConfig.from_dict({**cfg.to_dict(),'direction':'short',
            'protection':{'upper_stop_loss':{'type':'price','source':'mark','operator':'gte','value':'101000'}}}).protection
        short=replace(cfg,direction='short',protection=protection,execution_policy='post_only')
        assert not release.permits_config(short)
        assert len(release.summary(cfg.account_ref)[0]['qualifications'])==1
        # Only the selection mechanics below are mocked: real evidence rejection above remains proved.
        short_id=models.sha256_hex(short_grant)
        release.verified[short_id]={'fill_floor':2000,'funding_floor':25000,'proof_hash':'offline-short-selection'}
        assert release.permits_config(short)
        assert not release.permits_config(replace(short,execution_policy='gtc'))
        assert not release.permits_config(replace(cfg,execution_policy='post_only'))
        assert release.context(short)['grant_id']==short_id
        short_preview=await rt.preview(short)
        assert short_preview['plan'].live_eligible
        assert short_preview['plan'].capability_revision=='live-release:'+short_id
        assert release.context(cfg)['grant_id']==models.sha256_hex(long_grant)
        assert release.context(short)['history']['funding_floor']==25000
        summary=release.summary(cfg.account_ref)
        assert len(summary)==1 and len(summary[0]['qualifications'])==2
        # Revoking one feature never routes its old grant id to another feature.
        release.verified.pop(short_id)
        assert not release.grant(cfg.account_ref,cfg.market_ref,grant_id=short_id)
        assert release.permits_config(cfg)
    asyncio.run(run())


def test_overlapping_feature_grants_are_rejected_even_with_different_evidence(trial):
    async def run():
        rt,root,transport,release,grant,cfg,tiny=await release_fixture(trial)
        with pytest.raises(AdapterError,match='LIVE_RELEASE_POLICY_INVALID'):
            LiveRelease(rt,{'schema_version':1,'grants':[grant,{**grant,'capital_limit':'20'}]})
    asyncio.run(run())


def test_verified_same_market_features_preserve_earliest_witnessed_history(trial,monkeypatch):
    async def run():
        rt,root,transport,old,grant,cfg,tiny=await release_fixture(trial)
        base=rt.store.read(lambda c:old.evidence(c,next(iter(old.grants.values()))))
        newer={**grant,'modes':['short:deferred'],'execution_policies':['gtc']}
        release=LiveRelease(rt,{'schema_version':1,'grants':[grant,newer]})
        rt.live_release=release
        new_id=models.sha256_hex(newer)
        # This test isolates merging of independently verified history;
        # direction qualification itself is exercised by the rejection test.
        def verified_history(c,g):
            return {**base,'proof_hash':'offline-new-history','fill_floor':base['fill_floor']+1000,
                    'funding_floor':base['funding_floor']+1000} if g['grant_id']==new_id else base
        monkeypatch.setattr(release,'evidence',verified_history)
        assert not await release.restore()
        view=rt.venue_for(cfg.account_ref,cfg.market_ref)
        assert view.funding_retention_floor_ms==base['funding_floor']
        assert view.retention_floor_ms==max(base['fill_floor'],10000)
        assert release.verified[new_id]['funding_floor']==base['funding_floor']+1000
    asyncio.run(run())


@pytest.mark.parametrize('event_type',['risk_paused','protection_gap_exit_requested'])
@pytest.mark.parametrize('code',['PROTECTION_ORDER_UNVERIFIED','PROTECTION_OBSERVATION_UNAVAILABLE','PROTECTION_REPLACEMENT_UNRESOLVED'])
def test_protection_failure_disqualifies_even_completed_loop_and_cached_native_proof(trial,monkeypatch,code,event_type):
    from entropy_arb.grid import store
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    async def run():
        rt,root,t,release,grant,cfg,tiny=await release_fixture(trial)
        assert not await release.restore() and release.permits_config(cfg)
        await rt.store.write(lambda c:store.register_event(c,event_key='protection-gap-test',scope=grant['loop_grid'],
            event_type=event_type,payload={'code':code,'epoch':4,'position_retained':True}))
        failures=await release.restore()
        assert failures[0]['code']=='LIVE_RELEASE_PROTECTION_GAP'
        assert not release.permits_config(cfg)
        assert not (await rt.preview(cfg))['plan'].live_eligible
    asyncio.run(run())


def test_service_release_requires_new_verifier_even_when_native_cache_exists(trial,monkeypatch):
    from dataclasses import replace
    from entropy_arb.grid import service_release
    async def run():
        rt,root,t,release,grant,cfg,tiny=await release_fixture(trial,extra_cycles=1)
        service=models.ServiceCoveragePolicy('150',30)
        for gid in (grant['loop_grid'],grant['small_close_grid']):
            changed=replace(rt.config(gid),protection=replace(rt.config(gid).protection,service_coverage=service))
            def update(c,gid=gid,changed=changed):
                c.execute('UPDATE grid_versions SET config_json=? WHERE grid_id=?',(json.dumps(changed.to_dict()),gid))
                row=c.execute('SELECT value FROM runtime_state WHERE key=?',('operator-trial:'+gid,)).fetchone()
                ctx=json.loads(row[0]);ctx['config_hash']=changed.config_hash
                c.execute('UPDATE runtime_state SET value=? WHERE key=?',(json.dumps(ctx),'operator-trial:'+gid))
            await rt.store.write(update)
        scoped=LiveRelease(rt,{'schema_version':1,'grants':[{**grant,'service_coverage':service.to_dict(),
            'service_evidence':{'fault_grid':'independent-fault'}}]})
        candidate=next(iter(scoped.grants.values()));calls=[]
        def valid(runtime,c,grant):
            calls.append(grant['grant_id'])
            return {'proof_hash':'new-service-proof','fill_floor':1,'funding_floor':2}
        monkeypatch.setattr(service_release,'verify',valid)
        first=rt.store.read(lambda c:scoped.evidence(c,candidate))
        assert calls==[candidate['grant_id']] and first['fill_floor']==1 and first['funding_floor']==2
        scoped.verified[candidate['grant_id']]=first
        def invalid(*args):raise AdapterError('LIVE_RELEASE_SERVICE_UNQUALIFIED')
        monkeypatch.setattr(service_release,'verify',invalid)
        with pytest.raises(AdapterError,match='SERVICE_UNQUALIFIED'):
            rt.store.read(lambda c:scoped.evidence(c,candidate))
    asyncio.run(run())
