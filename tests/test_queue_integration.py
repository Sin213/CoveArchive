"""Real CA-S7 transfers and verified restart/re-enqueue behavior."""
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
import sqlite3
import subprocess
import sys
import json
import threading
import time
import pytest
from covearchive.archive import download as d, manifest as m
from covearchive.github.urls import RepositoryTarget
from covearchive.queue import QueueStore, QueueRunner, RunnerLock, QueueBusyError, JobState as S
from covearchive.transfer.resolve import TransferPolicy
from test_transfer_resolve import fixture_server, asset, TARGET, BODY, SECRET
from test_archive_download import release, project, folder, no_temps
from test_archive_download_e2e import DATA, selected, consistent, CRASH, ROOT

@pytest.fixture
def rig(tmp_path):
    root=tmp_path/'archive'; root.mkdir()
    with fixture_server() as server:
        server.routes['/cdn']=(200,{'Content-Length':str(len(DATA))},DATA)
        yield root,server,QueueStore(tmp_path/'queue.db'),TransferPolicy.for_loopback_test(server.origin)

def runner_for(store,policy,items,**options):
    catalogs={repo.repo_key:r for repo,r,a,root in items}
    return QueueRunner(store,catalog=lambda repo:NS(releases=(catalogs[repo.repo_key],)),policy=policy,**options)

def assert_private(store,server,include_lock=True):
    with sqlite3.connect(store.path) as c:
        text=repr(c.execute('SELECT * FROM jobs').fetchall()).encode()
    raw=store.path.read_bytes()+text
    if include_lock: raw+=Path(str(store.path)+'.lock').read_bytes()
    for marker in (SECRET.encode(),b'sig=',server.origin.encode(),b'http://',b'https://',b'hostile.invalid'):
        assert marker not in raw

def test_Q33_Q23_Q29_real_projects_and_reverify(rig):
    root,server,store,policy=rig; a=selected(True); b=replace(selected(),id=8,name='second.zip')
    c=replace(selected(True),id=9,name='other.zip'); other=RepositoryTarget('Other','Repo')
    ra=release(assets=(a,b)); rb=release(c)
    items=[(TARGET,ra,a,str(root)),(TARGET,ra,b,str(root)),(other,rb,c,str(root))]
    for repo,r,item,path in items:
        server.routes[f'/repos/{repo.owner}/{repo.repo}/releases/assets/{item.id}']=(302,{'Location':f'/cdn{item.id}?'+SECRET},b'')
        server.routes[f'/cdn{item.id}']=(200,{'Content-Length':str(len(DATA))},DATA)
    store.enqueue_many(items)
    with runner_for(store,policy,items) as runner:
        assert runner.run_pending().completed==3
        jobs=store.list_jobs(); assert all(j.state==S.COMPLETED and j.transferred for j in jobs)
        assert [j.integrity for j in jobs]==['PUBLISHER_VERIFIED','LOCAL_HASH_ONLY','PUBLISHER_VERIFIED']
        paths=Counter(path.split('?')[0] for path,headers in server.requests)
        assert len(server.requests)==6
        for repo,r,item,path in items:
            assert paths[f'/repos/{repo.owner}/{repo.repo}/releases/assets/{item.id}']==paths[f'/cdn{item.id}']==1
            target=root/repo.owner/repo.repo/'release-v1--10'/item.name
            assert target.read_bytes()==DATA
        for repo in (TARGET,other):
            base=root/repo.owner/repo.repo
            index=m.loads_index((base/'archive-index.json').read_text())
            manifest=m.loads_release_manifest((base/'release-v1--10'/'release.json').read_text())
            m.check_consistent(index,manifest)
            assert all(a.status=='completed' for a in manifest.assets)
        assert_private(store,server,include_lock=False); server.requests.clear()
        assert all(e.outcome=='requeued' for e in store.enqueue_many(items))
        assert runner.run_pending().completed==3
        assert not server.requests
        assert all(j.result_status=='VERIFIED_EXISTING' and j.transferred is False for j in store.list_jobs())
        assert_private(store,server,include_lock=False)
    assert_private(store,server)
    no_temps(root)

_CLAIM_CHILD=r'''
import sys,time,json
sys.path.insert(0,sys.argv[1])
from covearchive.queue import QueueStore
store=QueueStore(sys.argv[2]); owner=sys.argv[3]
time.sleep(max(0,float(sys.argv[4])-time.time()))
ids=[]
while (job:=store.claim_next(owner)) is not None: ids.append(job.job_id)
print(json.dumps({'owner':owner,'ids':ids}),flush=True)
'''

_RUN_CHILD=r'''
import sys,time
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1])
from covearchive.queue import QueueStore,QueueRunner
def archive(*args,**kwargs):
    print('READY',flush=True)
    while True: time.sleep(0.1)
store=QueueStore(sys.argv[2]); job=store.list_jobs()[0]
release=NS(id=job.release_id,assets=(NS(id=job.asset_id),))
with QueueRunner(store,catalog=lambda repo:NS(releases=(release,)),archive=archive) as runner:
    runner.run_pending()
'''

def _spawn(script,*args):
    return subprocess.Popen([sys.executable,'-I','-B','-c',script,str(ROOT/'src'),*map(str,args)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)

def _stop(children):
    for child in children:
        if child.poll() is None: child.kill()
        child.wait(timeout=5)
        child.stdout.close(); child.stderr.close()

_STOP_CHILD=r'''
import sys,time,threading
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1])
from covearchive.queue import QueueStore,QueueRunner
entered=threading.Event(); block=threading.Event()
store=QueueStore(sys.argv[2]); job=store.list_jobs()[0]
def wait():
    entered.set(); block.wait(30)
def catalog(repo):
    if sys.argv[4]=='catalog': wait()
    return NS(releases=(NS(id=job.release_id,assets=(NS(id=job.asset_id),)),))
def archive(*a,**kw): wait()
runner=QueueRunner(store,catalog=catalog,archive=archive)
threading.Thread(target=runner.run_pending,daemon=True).start()
assert entered.wait(10)
getattr(runner,sys.argv[3])(job.job_id)
print('READY',flush=True)
while True: time.sleep(0.1)
'''

@pytest.mark.parametrize('source',['catalog','archive'])
@pytest.mark.parametrize('action,state',[('pause',S.PAUSED),('cancel',S.CANCELLED)])
def test_Q21_J1_killed_stop_intent(tmp_path,source,action,state):
    store=QueueStore(tmp_path/'queue.db'); a=asset(); r=release(a)
    j=store.enqueue(TARGET,r,a,str(tmp_path)).job; child=_spawn(_STOP_CHILD,store.path,action,source)
    ready=threading.Event(); lines=[]
    def read():
        try: lines.append(child.stdout.readline())
        finally: ready.set()
    reader=threading.Thread(target=read,daemon=True); reader.start()
    try:
        assert ready.wait(15) and lines==['READY\n']
        child.kill(); child.wait(timeout=5); calls=[]
        def archive(*a,**kw): calls.append(1); return d.ArchiveResult(d.ArchiveStatus.ARCHIVED,None,1,10)
        with QueueRunner(QueueStore(store.path),catalog=lambda repo:NS(releases=(r,)),archive=archive) as runner:
            runner.run_pending(); stopped=store.get(j.job_id)
            assert (stopped.state,stopped.retry_count,stopped.last_error_code)==(state,0,None) and not calls
            getattr(runner,'resume' if action=='pause' else 'retry')(j.job_id)
            assert runner.run_pending().completed==1 and calls==[1]
    finally: _stop([child]); reader.join(5); assert not reader.is_alive()

@pytest.mark.parametrize('shared,count,claimed',[(False,12,12),(True,6,1)])
def test_Q26_three_process_claims(tmp_path,shared,count,claimed):
    store=QueueStore(tmp_path/'queue.db'); a=asset(); r=release(a)
    items=[(TARGET if shared else RepositoryTarget('Owner',f'Repo{i}'),r,
        replace(a,id=i+1),str(tmp_path)) for i in range(count)]
    items=[(repo,replace(r,assets=(a,)),a,root) for repo,r,a,root in items]
    jobs=store.enqueue_many(items); children=[]; outputs=[]; start=time.time()+0.8
    try:
        for i in range(3): children.append(_spawn(_CLAIM_CHILD,store.path,f'child_{i}',start))
        for i,child in enumerate(children):
            stdout,stderr=child.communicate(timeout=15)
            assert child.returncode==0,stdout+stderr
            output=json.loads(stdout); assert output['owner']==f'child_{i}'; outputs.append(output)
        ids=[job_id for output in outputs for job_id in output['ids']]
        assert len(ids)==len(set(ids))==claimed
        expected={e.job.job_id for e in jobs}
        assert set(ids)<=expected
        if not shared: assert set(ids)==expected
        owners={job_id:output['owner'] for output in outputs for job_id in output['ids']}
        rows=store.list_jobs(); assert sum(j.state==S.RUNNING for j in rows)==claimed
        for j in rows:
            assert (j.state,j.claimed_by)==((S.RUNNING,owners[j.job_id]) if j.job_id in owners else (S.QUEUED,None))
    finally: _stop(children)

def test_Q21_Q26_killed_runner_lock_and_recovery(tmp_path):
    store=QueueStore(tmp_path/'queue.db'); a=asset(); r=release(a)
    j=store.enqueue(TARGET,r,a,str(tmp_path)).job; child=_spawn(_RUN_CHILD,store.path)
    ready=threading.Event(); lines=[]
    def read_ready():
        try: lines.append(child.stdout.readline())
        finally: ready.set()
    reader=threading.Thread(target=read_ready,daemon=True); reader.start()
    try:
        assert ready.wait(10) and lines==['READY\n']
        running=store.get(j.job_id); assert running.state==S.RUNNING
        with QueueRunner(QueueStore(store.path),catalog=lambda repo:pytest.fail('lock must precede catalog')) as other:
            with pytest.raises(QueueBusyError) as error: other.run_pending()
            assert error.value.code=='runner_active'
        assert store.get(j.job_id)==running
        child.kill(); assert child.wait(timeout=5)!=0
        assert store.get(j.job_id)==running
        calls=[]; recovered=[]; recover=store.recover_interrupted
        def observed_recovery(keep=(),**kw):
            count=recover(keep,**kw); recovered.append((count,store.get(j.job_id))); return count
        store.recover_interrupted=observed_recovery
        def archive(*args,**kwargs):
            calls.append(1); return d.ArchiveResult(d.ArchiveStatus.ARCHIVED,None,a.id,r.id)
        with QueueRunner(store,catalog=lambda repo:NS(releases=(r,)),archive=archive) as runner:
            assert runner.run_pending().completed==1
        assert recovered[0][0]==1 and recovered[0][1].state==S.QUEUED
        assert recovered[0][1].last_error_code=='interrupted' and recovered[0][1].claimed_by is None
        done=QueueStore(store.path).get(j.job_id)
        assert done.state==S.COMPLETED and done.last_error_code is None and done.retry_count==1 and calls==[1]
        with RunnerLock(store.path): pass
    finally:
        _stop([child]); reader.join(timeout=5)
        assert not reader.is_alive()

def test_Q24_modified_same_size_preserved(rig):
    root,server,store,policy=rig; a=selected(); items=[(TARGET,release(a),a,str(root))]
    store.enqueue_many(items)
    with runner_for(store,policy,items) as runner:
        runner.run_pending(); path=folder(root)/a.name; changed=b'x'*len(DATA); path.write_bytes(changed)
        server.requests.clear(); store.enqueue_many(items); runner.run_pending(wait_for_retries=True)
        j=store.list_jobs()[0]
        assert (j.state,j.result_status,j.last_error_code,j.retry_count)==(S.FAILED,'MODIFIED','hash_mismatch',0)
        assert not server.requests and path.read_bytes()==changed
    no_temps(root)

def test_Q22_committed_archive_recovers_queue(rig):
    root,server,store,policy=rig; a=selected(True); r=release(a); items=[(TARGET,r,a,str(root))]
    store.enqueue_many(items); store.claim_next('dead_owner')
    assert d.archive_asset(TARGET,r,a,str(root),policy=policy).status.value=='ARCHIVED'
    server.requests.clear()
    with runner_for(store,policy,items) as runner: runner.run_pending()
    j=store.list_jobs()[0]
    assert (j.state,j.result_status,j.transferred)==(S.COMPLETED,'VERIFIED_EXISTING',False)
    assert not server.requests; consistent(root); assert_private(store,server)

def test_Q22_published_uncommitted_digest_adoption(rig):
    root,server,store,policy=rig; a=selected(True); items=[(TARGET,release(a),a,str(root))]
    store.enqueue_many(items); store.claim_next('dead_owner')
    child=subprocess.Popen([sys.executable,'-I','-B','-c',CRASH,str(ROOT/'src'),str(ROOT/'tests'),
        str(root),server.origin,'after','True'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        stdout,stderr=child.communicate(timeout=20)
        assert child.returncode==71,stdout+stderr
    finally:
        if child.poll() is None: child.kill()
        child.wait(timeout=5)
    path=folder(root)/a.name; assert path.read_bytes()==DATA
    identity=path.stat().st_ino; assert not (project(root)/'archive-index.json').exists()
    server.requests.clear()
    with runner_for(store,policy,items) as runner: runner.run_pending()
    j=store.list_jobs()[0]
    assert (j.state,j.result_status,j.transferred)==(S.COMPLETED,'ALREADY_ARCHIVED',False)
    assert not server.requests and path.stat().st_ino==identity
    consistent(root); assert_private(store,server)

def test_Q12_Q16_real_pause_restarts_from_zero(rig):
    root,server,store,policy=rig; a=selected(); r=release(a); items=[(TARGET,r,a,str(root))]
    j=store.enqueue_many(items)[0].job; callbacks=[]
    def archive(*args,progress,**options):
        def observed(received,total):
            callbacks.append(received); progress(received,total)
            if len(callbacks)==1: runner.pause(j.job_id)
        return d.archive_asset(*args,progress=observed,**options)
    with runner_for(store,policy,items,archive=archive) as runner:
        summary=runner.run_pending()
        assert summary.paused==1 and store.get(j.job_id).state==S.PAUSED
        assert len(callbacks)==1 and 0<callbacks[0]<len(DATA)
        assert not (folder(root)/a.name).exists() and not (project(root)/'archive-index.json').exists()
        assert len(server.requests)==2; no_temps(root)
        runner.resume(j.job_id); assert runner.run_pending().completed==1
        assert store.get(j.job_id).state==S.COMPLETED
        assert 0<callbacks[1]<=65536 and callbacks[-1]==len(DATA)
        # This is a new request from byte zero, never byte-level resume.
        assert sum(path.startswith('/repos/') for path,headers in server.requests)==2
        assert sum(path.startswith('/cdn') for path,headers in server.requests)==2
        assert all(not any(k.lower()=='range' for k in headers) for path,headers in server.requests)
        consistent(root); assert_private(store,server,include_lock=False)
    assert_private(store,server)
