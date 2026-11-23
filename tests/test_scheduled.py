"""Closed-window preservation: isolated inputs, real queue, no network or Qt."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
from covearchive import scheduled as s
from covearchive.scheduled_state import State, default_state_path, MAX_BYTES
from covearchive.library.model import Library, Repository, dump_library
from covearchive.queue import QueueStore, QueueRunner, RunnerLock, JobState as S
from covearchive.archive.download import ArchiveResult, ArchiveStatus
from covearchive.scan_worker import ScanSuccess, ScanFailure
from test_scan_worker import catalog, release, asset

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    for key in ('HOME','XDG_CONFIG_HOME','XDG_STATE_HOME','LOCALAPPDATA'):
        monkeypatch.setenv(key,str(tmp_path/key))
    root=tmp_path/'archive'; root.mkdir()
    config=tmp_path/'LOCALAPPDATA'/'CoveArchive' if sys.platform=='win32' else tmp_path/'XDG_CONFIG_HOME'/'covearchive'; config.mkdir(parents=True)
    lib=Library(repositories=(Repository('github','https://github.com/Owner/Repo',auto_archive=1),))
    prefs=dict(version=1,archive_root=str(root),window=dict(width=1180,height=720,tab='browse'),
        repositories={'owner/repo':dict(include=[],exclude=['*debug*'],extensions=['.zip'],prereleases=False)})
    env=SimpleNamespace(lib=lib,prefs=prefs)
    def save():
        (config/'library.json').write_bytes(dump_library(env.lib))
        (config/'preferences.json').write_text(json.dumps(env.prefs))
    save(); calls=[]; now=[10000.0]
    cat=[catalog(release(assets=[asset(size=3)]))]
    def scan(url,**kw): calls.append(('scan',url)); return ScanSuccess('a'*32,cat[0])
    def archive(repo,rel,item,root,**kw):
        calls.append(('download',item.id))
        return ArchiveResult(ArchiveStatus.ARCHIVED,None,item.id,rel.id)
    def run_queue(store,**kw):
        with QueueRunner(store,catalog=lambda _:cat[0],archive=archive) as runner:
            runner.run_pending()
        return None
    def run(**kw): return s.run(scan=scan,run_queue=run_queue,clock=lambda:now[0],**kw)
    env.__dict__.update(root=root,config=config,save=save,calls=calls,
        now=now,cat=cat,scan=scan,run=run,run_queue=run_queue)
    return env


def state(): return State.load(default_state_path()).data
def jobs(): return QueueStore().list_jobs()


def test_due_incremental_new_release_and_history_preserved(rig):
    old=rig.root/'historical.zip'; old.write_bytes(b'keep me')
    before=(old.read_bytes(),old.stat().st_ino)
    assert rig.run()==0
    assert rig.calls==[('scan','https://github.com/Owner/Repo'),('download',1)]
    assert jobs()[0].state==S.COMPLETED
    repo=state()['repositories']['owner/repo']
    assert repo['last_success']==10000 and repo['next_due']==13600
    assert rig.run()==0 and len(rig.calls)==2
    rig.now[0]=13600
    assert rig.run()==0 and rig.calls[-1][0]=='scan' and len(rig.calls)==3
    rig.cat[0]=catalog(*[release(i,assets=[asset(i,size=3)]) for i in (1,2)])
    rig.now[0]=17200
    assert rig.run()==0 and rig.calls[-1]==('download',2)
    assert len(jobs())==2 and all(j.state==S.COMPLETED for j in jobs())
    assert (old.read_bytes(),old.stat().st_ino)==before


def test_not_opted_in_and_nothing_due_do_not_open_queue(rig,monkeypatch):
    rig.lib=Library(); (rig.config/'library.json').write_bytes(dump_library(rig.lib))
    monkeypatch.setattr(s,'QueueStore',lambda *a,**kw:pytest.fail('queue opened'))
    assert rig.run()==0 and not rig.calls
    assert state()['last_outcome']=='not_opted_in'


@pytest.mark.parametrize('problem',['needs_rules','needs_root'])
def test_missing_opt_in_prerequisites_are_not_success(rig,problem):
    if problem=='needs_rules': rig.prefs['repositories']={}
    else: rig.prefs['archive_root']=None
    rig.save()
    assert rig.run()==2 and not rig.calls
    repo=state()['repositories']['owner/repo']
    assert repo['last_code']==problem and repo['last_success'] is None


@pytest.mark.parametrize('name',['library.json','preferences.json'])
def test_invalid_inputs_are_preserved(rig,name):
    path=rig.config/name; path.write_bytes(b'{bad secret')
    assert rig.run()==2 and not rig.calls and path.read_bytes()==b'{bad secret'
    assert 'secret' not in json.dumps(state())


def test_multiple_intervals(rig):
    second=Repository('github','https://github.com/Other/Repo',auto_archive=6)
    rig.lib=Library(repositories=(*rig.lib.repositories,second)); rig.save()
    rig.prefs['repositories']['other/repo']=rig.prefs['repositories']['owner/repo']; rig.save()
    def scan(url,**kw):
        rig.calls.append(('scan',url))
        from covearchive.github.urls import RepositoryTarget
        return ScanSuccess('a'*32,replace(catalog(),repository=RepositoryTarget('Other' if 'Other' in url else 'Owner','Repo')))
    assert s.run(scan=scan,run_queue=rig.run_queue,clock=lambda:rig.now[0])==0
    rig.now[0]+=3600
    assert s.run(scan=scan,run_queue=rig.run_queue,clock=lambda:rig.now[0])==0
    assert len(rig.calls)==3
    assert state()['repositories']['other/repo']['next_due']==31600


@pytest.mark.parametrize('code',['network','rate_limited','incomplete_scan'])
def test_scan_failure_and_backoff(rig,code):
    def failed(*a,**kw): return ScanFailure('a'*32,code,detail={'retry_after_seconds':120})
    for delay in ((120,120) if code=='rate_limited' else (300,900)):
        assert s.run(scan=failed,run_queue=rig.run_queue,clock=lambda:rig.now[0])==3
        repo=state()['repositories']['owner/repo']
        assert repo['last_success'] is None and repo['next_due']==rig.now[0]+delay
        rig.now[0]+=delay
    assert not rig.calls and not s.default_queue_path().exists()


@pytest.mark.parametrize('job_state',[S.PAUSED,S.CANCELLED,S.FAILED])
def test_user_stopped_and_failed_jobs_are_never_restarted(rig,job_state):
    c=rig.cat[0]; store=QueueStore(); j=store.enqueue(c.repository,c.releases[0],c.releases[0].assets[0],str(rig.root)).job
    if job_state==S.FAILED:
        store.claim_next('prior'); store.finish(j.job_id,'prior',S.FAILED,code='disk_full')
    else: store.transition(j.job_id,job_state)
    assert rig.run()==4
    assert store.get(j.job_id).state==job_state and not any(c[0]=='download' for c in rig.calls)
    assert state()['repositories']['owner/repo']['last_success'] is None


def test_queue_owned_by_gui_is_deferred_without_success(rig):
    store=QueueStore()
    def run_queue(store,**kw): return s.run_queue(store,**kw)
    with RunnerLock(store.path):
        assert s.run(scan=rig.scan,run_queue=run_queue,clock=lambda:rig.now[0])==4
    assert jobs()[0].state==S.QUEUED
    assert state()['repositories']['owner/repo']['last_success'] is None


def test_overlapping_scheduled_run_changes_nothing(rig):
    assert rig.run()==0
    path=default_state_path(); before=path.read_bytes(); rig.calls.clear()
    with RunnerLock(path): assert rig.run()==0
    assert path.read_bytes()==before and not rig.calls


@pytest.mark.parametrize('raw',[b'bad',b'{"version":NaN}',b'{"version":1,"version":1}',
    b'{"extra":1}',b'['*100+b']'*100,pytest.param(b'x'*(MAX_BYTES+1),id='oversized')])
def test_bad_state_is_safe_and_reported(rig,raw):
    path=default_state_path(); path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(raw)
    assert rig.run()==6
    assert state()['last_error']=='state_invalid'
    assert rig.calls[-1]==('download',1)


def test_clock_rollback_and_future_state(rig):
    assert rig.run()==0
    path=default_state_path(); data=state(); data['repositories']['owner/repo']['next_due']=10**20
    path.write_text(json.dumps(data)); rig.now[0]=100
    assert rig.run()==6
    assert state()['repositories']['owner/repo']['next_due']<=3700


def test_no_qt_headless_launcher(rig):
    (rig.config/'library.json').write_bytes(dump_library(Library()))
    script="""import sys,runpy
class BlockQt:
 def find_spec(self,fullname,*args):
  if fullname.startswith(('PySide6','PyQt')): raise AssertionError(fullname)
sys.meta_path.insert(0,BlockQt())
sys.argv=[sys.argv[1],'--scheduled-run']
runpy.run_path(sys.argv[0],run_name='__main__')
"""
    result=subprocess.run([sys.executable,'-I','-B','-c',script,str(ROOT/'run_covearchive.py')],capture_output=True,timeout=10)
    assert result.returncode==0,(result.stdout,result.stderr)
    assert not s.default_queue_path().exists()


def test_interrupted_before_work(rig):
    stop=threading.Event(); stop.set()
    assert rig.run(stop=stop)==5 and not rig.calls
    assert state()['last_outcome']=='interrupted'


def test_state_atomic_permissions_and_failed_replace(rig,monkeypatch):
    assert rig.run()==0
    path=default_state_path(); before=path.read_bytes()
    if sys.platform!='win32': assert path.stat().st_mode&0o777==0o600
    from covearchive import scheduled_state
    monkeypatch.setattr(scheduled_state.os,'replace',lambda *a:(_ for _ in ()).throw(OSError('secret')))
    rig.now[0]+=3600
    assert rig.run()==6 and path.read_bytes()==before
    assert list(path.parent.glob('*.tmp'))==[]


def test_blocked_limit_stops_other_repositories(rig):
    rig.lib=Library(repositories=(*rig.lib.repositories,Repository('github','https://github.com/Other/Repo',auto_archive=1)))
    rig.prefs['repositories']['other/repo']=rig.prefs['repositories']['owner/repo']; rig.save()
    calls=[]
    def scan(url,**kw): calls.append(url); return ScanFailure('a'*32,'rate_limited',detail={'retry_after_seconds':3600})
    assert s.run(scan=scan,clock=lambda:rig.now[0])==3 and len(calls)==1
    assert s.run(scan=scan,clock=lambda:rig.now[0]+1)==0 and len(calls)==1


def test_crash_after_enqueue_recovers_without_duplicate(rig):
    def crash(store,**kw):
        store.claim_next('dead_process')
        raise SystemExit(91)
    with pytest.raises(SystemExit): s.run(scan=rig.scan,run_queue=crash,clock=lambda:rig.now[0])
    assert jobs()[0].state==S.RUNNING
    assert rig.run()==0
    assert len(jobs())==1 and jobs()[0].state==S.COMPLETED and jobs()[0].retry_count==1


def test_download_failure_not_success(rig):
    def run_queue(store,**kw):
        def archive(repo,r,a,*args,**kw): return ArchiveResult(ArchiveStatus.FAILED,'disk_full',a.id,r.id)
        with QueueRunner(store,catalog=lambda _:rig.cat[0],archive=archive) as runner: runner.run_pending()
    assert s.run(scan=rig.scan,run_queue=run_queue,clock=lambda:rig.now[0])==4
    repo=state()['repositories']['owner/repo']
    assert repo['last_code']=='disk_full' and repo['last_success'] is None


def test_cancellation_after_discovery_does_not_enqueue(rig):
    stop=threading.Event()
    def scan(*a,**kw): stop.set(); return ScanSuccess('a'*32,rig.cat[0])
    assert s.run(scan=scan,stop=stop,clock=lambda:rig.now[0])==5
    assert not s.default_queue_path().exists()


@pytest.mark.parametrize('entry_change',[{'next_due':True},{'failures':4},{'hours':2},{'last_code':'secret?token=x'}, {'extra':None}])
def test_strict_per_repository_state(rig,entry_change):
    assert rig.run()==0
    path=default_state_path(); data=state(); data['repositories']['owner/repo'].update(entry_change); path.write_text(json.dumps(data))
    loaded=State.load(path)
    assert loaded.error=='state_invalid' and loaded.data['repositories']=={}


def test_timeout_is_distinct_and_does_not_scan(rig):
    assert rig.run(deadline_seconds=0)==7
    assert state()['last_outcome']=='timeout' and not rig.calls


def test_anonymous_child_environment_drops_token_sources(rig,monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN','super-secret-token')
    argv=[sys.executable,'-I','-B','-c',"import os; print('GITHUB_TOKEN' in os.environ)"]
    import time
    raw,error=s.child_run(argv,b'',stop=threading.Event(),deadline=time.monotonic()+5,cap=1024)
    assert error is None and raw.strip()==b'False'


def test_unsafe_schedule_lock_is_a_failure(rig):
    path=default_state_path(); path.parent.mkdir(parents=True,exist_ok=True)
    target=rig.root/'keep'; target.write_bytes(b'untouched')
    os.link(target,Path(str(path)+'.lock'))
    assert rig.run()==6
    assert target.read_bytes()==b'untouched' and not path.exists()


@pytest.mark.parametrize('frozen',[False,True])
def test_default_worker_interpreter_preserves_symlink(tmp_path,monkeypatch,frozen):
    link=tmp_path/'.venv'/'bin'/'python'; link.parent.mkdir(parents=True)
    if os.name=='nt':
        resolve=Path.resolve; real=Path(sys.executable)
        monkeypatch.setattr(Path,'resolve',lambda path,*a,**kw:real if path==link else resolve(path,*a,**kw))
    else: link.symlink_to(sys.executable)
    monkeypatch.setattr(sys,'executable',str(link))
    monkeypatch.setattr(sys,'frozen',frozen,raising=False)
    assert s.command(tmp_path/'run_covearchive.py','--scheduled-queue-worker')[0]==os.path.abspath(link)


def test_discovery_rate_limit_defers_already_enqueued_work(rig):
    rig.lib=Library(repositories=(*rig.lib.repositories,Repository('github','https://github.com/Other/Repo',auto_archive=1)))
    rig.prefs['repositories']['other/repo']=rig.prefs['repositories']['owner/repo']; rig.save()
    called=[]
    def scan(url,**kw):
        return rig.scan(url,**kw) if 'Owner' in url else ScanFailure('a'*32,'rate_limited',detail={'retry_after_seconds':120})
    def queue(store,**kw): called.append('queue'); return rig.run_queue(store,**kw)
    assert s.run(scan=scan,run_queue=queue,clock=lambda:rig.now[0])==4
    assert called==[] and jobs()[0].state==S.QUEUED and not any(c[0]=='download' for c in rig.calls)
    first=state()['repositories']['owner/repo']
    assert first['last_code']=='rate_limited' and first['last_success'] is None
    assert state()['blocked_until']==rig.now[0]+120


@pytest.mark.parametrize('due',[False,True])
def test_pending_attention_preserves_success_even_with_empty_discovery(rig,due):
    assert rig.run()==0
    cat=rig.cat[0]; release=cat.releases[0]; new=replace(release.assets[0],id=2)
    store=QueueStore(); queued=store.enqueue(cat.repository,replace(release,assets=(new,)),new,str(rig.root)).job
    rig.lib=Library(repositories=(*rig.lib.repositories,Repository('github','https://github.com/Other/Repo',auto_archive=1)))
    rig.prefs['repositories']['other/repo']=rig.prefs['repositories']['owner/repo']; rig.save()
    rig.now[0]=13600 if due else 10001
    def scan(url,**kw):
        from covearchive.github.urls import RepositoryTarget
        result=catalog() if 'Owner' in url else replace(cat,repository=RepositoryTarget('Other','Repo'))
        return ScanSuccess('a'*32,result)
    def queue(store,**kw):
        reserved=store.claim_next('guard')
        assert reserved.job_id==queued.job_id
        store.finish(reserved.job_id,'guard',S.QUEUED,code='needs_attention')
        return 'needs_attention'
    assert s.run(scan=scan,run_queue=queue,clock=lambda:rig.now[0])==4
    repo=state()['repositories']['owner/repo']
    assert repo['last_success']==10000 and repo['last_code']=='needs_attention'
    assert store.get(queued.job_id).state==S.QUEUED
