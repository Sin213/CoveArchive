"""Pure scheduling and saved Browse-rule eligibility."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import subprocess
import sys
import pytest
from covearchive.auto_archive import Schedule, eligible_items, failure_text
from test_scan_worker import release, asset, catalog


@pytest.fixture
def clock(): return [10000.0]


def test_startup_sync_restart_interval_change_and_ties(clock):
    s=Schedule(clock=lambda:clock[0]); assert s.sync({'b':1,'a':1})==()
    assert s.due() is None and s.status('a').last_check is None
    assert s.status('a').next_check==13600 and s.status('a').state=='waiting'
    clock[0]+=3600; assert s.due()=='a'
    s.started('a'); assert s.due() is None and s.status('a').next_check is None
    assert s.status('a').state=='checking'
    s.succeeded('a','No new assets'); assert s.due()=='b'
    assert s.status('a').last_check==clock[0] and s.status('a').message=='No new assets'
    s.sync({'a':6,'b':1}); assert s.status('a').next_check==clock[0]+21600
    due=s.status('b').next_check; clock[0]+=5; s.sync({'a':6,'b':1})
    assert s.status('b').next_check==due
    restarted=Schedule(clock=lambda:clock[0]); restarted.sync({'b':1})
    assert restarted.due() is None and restarted.status('b').next_check==clock[0]+3600
    s.started('b'); assert s.sync({'a':6})==('b',) and s.status('b') is None
    with pytest.raises(FrozenInstanceError): s.status('a').state='bad'


@pytest.mark.parametrize('code',['network','timeout','server_error','incomplete_scan','process','protocol',
    'queue_unavailable','database_busy','database_unavailable'])
def test_transient_ladder_fourth_regular_and_success_resets(clock,code):
    s=Schedule(clock=lambda:clock[0]); s.sync({'a':1})
    for delay in (300,900,1800,3600,300):
        s.started('a'); s.failed('a',code)
        status=s.status('a'); assert status.last_check==clock[0] and status.next_check==clock[0]+delay
        assert status.state==('error' if delay==3600 else 'retrying')
        clock[0]+=delay-1; assert s.due() is None
        clock[0]+=1; assert s.due()=='a'
    s.started('a'); s.succeeded('a','Done'); s.failed('a',code)
    assert s.status('a').next_check==clock[0]+300


@pytest.mark.parametrize('code',['not_found','forbidden','unauthorized','needs_rules','needs_root','internal_error','unknown'])
def test_permanent_errors_wait_regular_interval(clock,code):
    s=Schedule(clock=lambda:clock[0]); s.sync({'a':6}); s.started('a'); s.failed('a',code)
    assert s.status('a').state=='error' and s.status('a').next_check==clock[0]+21600
    assert s.status('a').message==failure_text(code)


@pytest.mark.parametrize('hint,delay',[(None,60),(-1,60),(0,60),(120,120),(9999,3600),(10**400,3600),
    (float('nan'),60),(float('inf'),60),('remote',60),(True,60)])
def test_rate_limit_clamps_and_blocks_other_watches(clock,hint,delay):
    s=Schedule(clock=lambda:clock[0]); s.sync({'a':1,'b':1}); clock[0]+=3600
    s.started('a')
    try: s.failed('a','rate_limited',retry_after=hint)
    except OverflowError: pytest.fail('rate-limit hint must be bounded without overflow')
    assert s.blocked_until==clock[0]+delay and s.status('a').next_check==clock[0]+delay
    assert s.due() is None
    clock[0]+=delay; assert s.due()=='b'


def test_rate_limits_share_failure_ladder_and_backwards_clock(clock):
    s=Schedule(clock=lambda:clock[0]); s.sync({'a':1})
    s.failed('a','rate_limited',retry_after=60); s.failed('a','network')
    assert s.status('a').next_check==clock[0]+900
    s.failed('a','timeout'); s.failed('a','network')
    assert s.status('a').next_check==clock[0]+3600
    clock[0]-=10000
    assert s.status('a').next_check==clock[0]+3600 and s.due() is None
    s.failed('a','network'); assert s.status('a').next_check==clock[0]+300


def test_eligible_matches_browse_and_deduplicates():
    assets=[asset(1,'APP-WIN.zip',size=1),asset(1,'duplicate.zip',size=1),
        asset(2,'app-symbols.zip',size=1),asset(3,'app.tar.gz',size=1),
        asset(4,'app.txt',size=1),asset(5,'app.zip',size=1,state='new')]
    c=catalog(release(1,assets=assets),release(1),release(2,draft=True),
        release(3,prerelease=True,assets=[asset(7,size=1)]))
    rule=dict(include=['APP*'],exclude=['*symbols*'],extensions=['.zip'],prereleases=False)
    assert [(r.id,a.id) for r,a in eligible_items(c,rule)]==[(1,1)]
    rule['prereleases']=True
    assert [(r.id,a.id) for r,a in eligible_items(c,rule)]==[(1,1),(3,7)]
    rule.update(include=[],exclude=[],extensions=[]); assert eligible_items(c,rule)==()


@pytest.mark.parametrize('change',[dict(name='x'*1025+'.zip'),dict(size=2**63),dict(size=-1),dict(size=True),dict(name='')])
def test_hostile_asset_cannot_block_good_neighbors(change):
    c=catalog(release(assets=[asset(1,size=1),asset(2,size=1),asset(3,size=1)]))
    r=c.releases[0]; bad=replace(r.assets[1],**change)
    c=replace(c,releases=(replace(r,assets=(r.assets[0],bad,r.assets[2])),))
    rule=dict(include=[],exclude=[],extensions=['.zip'],prereleases=False)
    assert [a.id for _,a in eligible_items(c,rule)]==[1,3]


@pytest.mark.parametrize('tag',['','x'*1025])
def test_hostile_tag_and_valid_boundaries(tag):
    c=catalog(release(1,assets=[asset(size=0)]),release(2,assets=[asset(size=2**63-1)]))
    c=replace(c,releases=(replace(c.releases[0],tag_name=tag),replace(c.releases[1],tag_name='x'*1024)))
    assert [(r.id,a.size) for r,a in eligible_items(c,dict(include=[],exclude=[],extensions=['.zip'],prereleases=False))]==[(2,2**63-1)]


def test_actionable_messages_and_qt_free_import():
    assert failure_text('network')=='No connection to GitHub; will retry automatically'
    assert 'URL' in failure_text('not_found') and 'off' in failure_text('not_found')
    assert 'Save rules for this repository' in failure_text('needs_rules')
    assert 'archive folder' in failure_text('needs_root')
    assert 'resume' in failure_text('rate_limited',retry_at=10000)
    assert 'SECRET' not in failure_text('SECRET')
    result=subprocess.run([sys.executable,'-c',"import sys; sys.path.insert(0,sys.argv[1]); import covearchive.auto_archive; assert not any(k.startswith(('PySide6','covearchive.ui','covearchive.queue')) for k in sys.modules)",str(Path(__file__).resolve().parents[1]/'src')],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('bad',[True,0,-1,2**63,1.0])
@pytest.mark.parametrize('target',['release','asset'])
def test_hostile_ids_do_not_block_other_items(bad,target):
    c=catalog(release(1,assets=[asset(1,size=1)]),release(2,assets=[asset(2,size=1)]))
    first=c.releases[0]
    first=replace(first,id=bad) if target=='release' else replace(first,assets=(replace(first.assets[0],id=bad),))
    c=replace(c,releases=(first,c.releases[1]))
    assert [(r.id,a.id) for r,a in eligible_items(c,dict(include=[],exclude=[],extensions=['.zip'],prereleases=False))]==[(2,2)]


def test_rate_limit_clock_rollback_cannot_strand_all_watches():
    now=[100000.]; s=Schedule(clock=lambda:now[0]); s.sync({'a':1}); s.failed('a','rate_limited',retry_after=3600)
    now[0]-=86400; assert s.due() is None
    now[0]+=3600; assert s.due()=='a'


def test_release_preserves_schedule_history_and_retry_counter():
    now=[10000.]; s=Schedule(clock=lambda:now[0]); s.sync({'a':1}); s.failed('a','network')
    before=s.status('a'); s.started('a'); assert s.checking=='a'; s.release('a')
    assert s.checking is None and s.status('a')==replace(before,state='waiting')
    s.failed('a','network'); assert s.status('a').next_check==now[0]+900
    with pytest.raises(AttributeError): s.checking='a'
