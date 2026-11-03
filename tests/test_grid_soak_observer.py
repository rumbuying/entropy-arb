import asyncio
import json
import os
import sqlite3

import pytest

from entropy_arb.grid.soak_observer import Window, snapshot, main
from entropy_arb.grid.runtime import GridRuntime
from entropy_arb.grid.watchdog import process_birth, write_private
from test_grid_service_coverage import configured


def sample(seconds, **changes):
    return {'wall_ms':int((100000+seconds)*1000),'monotonic':seconds,
            'identity':{'grid_id':'test','scope':'ordinary'},'issues':[],
            'runtime_instance':'first','epoch':1,**changes}


def test_twenty_four_hours_requires_elapsed_clocks_not_sample_count():
    window=Window(required_seconds=1)
    for i in range(10000):
        window.add(sample(i/100000))
    assert not window.report()['completed_without_observed_issues']
    assert window.required==86400
    window=Window()
    for seconds in range(0,86401,10):
        result=window.add(sample(seconds))
    assert result['completed_without_observed_issues']
    assert result['release_qualified_by_observer'] is False
    assert result['live_dispatch_enabled_by_observer'] is False


@pytest.mark.parametrize('change,issue',[
    ({'monotonic':90000},'OBSERVATION_GAP'),
    ({'monotonic':11,'wall_ms':100100000},'CLOCK_CHANGED'),
    ({'identity':{'grid_id':'other'}},'SCOPE_CHANGED'),
    ({'runtime_instance':'replacement'},'RUNTIME_INSTANCE_CHANGED'),
    ({'epoch':2},'GRID_EPOCH_CHANGED'),
    ({'issues':['NATIVE_PROTECTION_EVIDENCE_UNAVAILABLE']},'NATIVE_PROTECTION_EVIDENCE_UNAVAILABLE'),
])
def test_gap_clock_scope_and_protection_failures_cannot_be_hidden(change,issue):
    window=Window();window.add(sample(0));window.add(sample(10,**change))
    for seconds in range(20,86431,10):
        window.add(sample(seconds))
    assert window.report()['issues'][issue]>=1
    assert not window.report()['completed_without_observed_issues']


def test_missing_database_is_not_created(tmp_path):
    path=tmp_path/'missing.db'
    with pytest.raises(sqlite3.OperationalError):
        snapshot(db=path,grid_id='missing',scope='ordinary',heartbeat_file=tmp_path/'hb')
    assert not path.exists()


def test_snapshot_reads_real_schema_and_distinguishes_private_scope(tmp_path,monkeypatch):
    from entropy_arb.grid import soak_observer as observer
    monkeypatch.setenv('GRID_REAL_DISPATCH','1')
    monkeypatch.setattr(observer.time,'time_ns',lambda:35000000000)
    monkeypatch.setattr(observer.time,'monotonic',lambda:100)
    async def run():
        rt=GridRuntime.open(str(tmp_path/'prod.db'),execution_mode='production')
        try:
            gid,cfg,venue,stop,qty,proof=await configured(rt)
            row=rt.lifecycle._grid(gid)
            context={'account':cfg.account_ref,'market':cfg.market_ref,'config_hash':cfg.config_hash,'credential_revision':'test-only'}
            lease={'account':cfg.account_ref,'market':cfg.market_ref,'config_hash':cfg.config_hash,'epoch':row['epoch'],'confirmed_from_ms':30000,'deadline_ms':40000}
            await rt.store.write(lambda c:c.executemany('INSERT OR REPLACE INTO runtime_state(key,value) VALUES(?,?)',[
                ('operator-trial:'+gid,json.dumps(context)),('native-protection-lease:'+gid,json.dumps(lease))]))
            hb=tmp_path/'run/grid-runtime-heartbeat.json'
            heartbeat={'schema_version':1,'uid':os.getuid(),'pid':os.getpid(),'birth':process_birth(os.getpid()),'db':str(tmp_path/'prod.db'),'phase':'running','instance':'a'*32,'monotonic':100}
            write_private(hb,heartbeat)
            write_private(hb.with_name('grid-watchdog-state.json'),{'status':'healthy','pid':os.getpid(),'instance':'a'*32,'checked_monotonic':100})
            kwargs=dict(db=rt.store.path,grid_id=gid,heartbeat_file=hb)
            result=snapshot(**kwargs,scope='operator-trial')
            assert result['issues']==[] and result['scope_is_ordinary'] is False
            with pytest.raises(ValueError,match='SCOPE_UNAVAILABLE'):
                snapshot(**kwargs,scope='ordinary')
            lease['deadline_ms']=35000
            await rt.store.write(lambda c:c.execute('UPDATE runtime_state SET value=? WHERE key=?',(json.dumps(lease),'native-protection-lease:'+gid)))
            result=snapshot(**kwargs,scope='operator-trial')
            assert 'NATIVE_PROTECTION_EVIDENCE_UNAVAILABLE' in result['issues']
        finally:rt.close()
    asyncio.run(run())


def test_cli_once_never_counts_unavailable_sources_as_completed(tmp_path,capsys):
    output=tmp_path/'private/report.json'
    result=main(['--db',str(tmp_path/'missing.db'),'--grid-id','missing','--scope','ordinary',
        '--heartbeat-file',str(tmp_path/'hb'),'--output',str(output),'--once'])
    assert result==1 and not (tmp_path/'missing.db').exists()
    report=json.loads(output.read_text())
    assert report['read_only'] and not report['completed_without_observed_issues']
    assert report['issues']=={'OBSERVATION_SOURCE_UNAVAILABLE':1}
    previous=output.read_bytes()
    assert main(['--db','unused','--grid-id','missing','--scope','ordinary',
        '--heartbeat-file','unused','--output',str(output),'--once'])==2
    assert output.read_bytes()==previous
