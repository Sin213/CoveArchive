from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace as NS
import hashlib
import sqlite3
import threading
import subprocess
import sys
from pathlib import Path
import pytest
from platform_paths import native_root
from covearchive.queue import models as m, store as q
from covearchive.github.models import Release, ReleaseAsset
from covearchive.github.urls import RepositoryTarget

S=m.JobState
EDGES={'QUEUED':{'RUNNING','PAUSED','CANCELLED'},'RETRY_WAIT':{'RUNNING','PAUSED','CANCELLED'},
    'RUNNING':{'COMPLETED','RETRY_WAIT','FAILED','PAUSED','CANCELLED','QUEUED'},
    'PAUSED':{'QUEUED','CANCELLED'},'COMPLETED':{'QUEUED'},'FAILED':{'QUEUED'},'CANCELLED':{'QUEUED'}}

def item(i=1,root=native_root('C:/Archive'),**changes):
    a=ReleaseAsset(i,'app.zip',42,'uploaded','application/zip','https://secret?sig=token',None,None,None,'','','.zip')
    a=replace(a,**changes)
    r=Release(10,'v1',None,False,False,None,'',None,None,(a,),None,None)
    return RepositoryTarget('Owner','Repo'),r,a,root

@pytest.fixture
def db(tmp_path): return q.QueueStore(tmp_path/'jobs.db',clock=lambda:100)

def test_Q01_Q02_Q05_creation_reopen(tmp_path):
    path=tmp_path/'q.db'; db=q.QueueStore(path,clock=lambda:100)
    e=db.enqueue(*item()); assert e.outcome=='created' and e.job.state==S.QUEUED
    assert e.job.created_at==100 and len(e.job.job_id)==32
    assert q.QueueStore(path).get(e.job.job_id)==e.job
    with sqlite3.connect(path) as c:
        assert c.execute('pragma user_version').fetchone()==(1,)
        assert c.execute('pragma journal_mode').fetchone()==('delete',)

@pytest.mark.parametrize('action,state',[('pause',S.PAUSED),('cancel',S.CANCELLED)])
def test_Q21_J1_durable_stop(db,action,state):
    j=db.enqueue(*item()).job; db.claim_next('owner')
    db.finish(j.job_id,'owner',S.RETRY_WAIT,retry_count=3); db.claim_next('owner')
    db.request_stop(j.job_id,'owner',action)
    fresh=q.QueueStore(db.path)
    with sqlite3.connect(db.path) as c: assert c.execute('select stop_requested from jobs').fetchone()==(action,)
    fresh.recover_interrupted(keep=(j.job_id,)); assert fresh.get(j.job_id).state==S.RUNNING
    fresh.recover_interrupted(); stopped=fresh.get(j.job_id)
    assert (stopped.state,stopped.retry_count,stopped.last_error_code,stopped.claimed_by)==(state,3,None,None)
    with sqlite3.connect(db.path) as c: assert c.execute('select stop_requested from jobs').fetchone()==(None,)
    with pytest.raises(m.InvalidTransitionError): db.request_stop(j.job_id,'owner',action)

def test_Q28_J1_stop_ownership_rank_and_clear(db):
    j=db.enqueue(*item()).job
    with pytest.raises(m.UnknownJobError): db.request_stop('missing','owner','pause')
    with pytest.raises(m.InvalidTransitionError): db.request_stop(j.job_id,'owner','pause')
    db.claim_next('owner')
    with pytest.raises(m.InvalidTransitionError): db.request_stop(j.job_id,'other','cancel')
    for action in ('pause','cancel','pause'): db.request_stop(j.job_id,'owner',action)
    with sqlite3.connect(db.path) as c: assert c.execute('select stop_requested from jobs').fetchone()==('cancel',)
    db.finish(j.job_id,'owner',S.COMPLETED,result=NS(status='ARCHIVED'))
    with sqlite3.connect(db.path) as c: assert c.execute('select stop_requested from jobs').fetchone()==(None,)
    db.enqueue(*item())
    with sqlite3.connect(db.path) as c: c.execute("update jobs set stop_requested='pause'")
    db.claim_next('new')
    with sqlite3.connect(db.path) as c: assert c.execute('select stop_requested from jobs').fetchone()==(None,)

def test_Q03_Q04_Q06_fifo_atomicity(db):
    jobs=db.enqueue_many([item(i) for i in range(1,4)])
    assert len(jobs)==3 and db.enqueue(*item()).outcome=='existing'
    with pytest.raises(m.InvalidJobError): db.enqueue_many([item(4),item(0)])
    assert len(db.list_jobs())==3
    for job in jobs:
        claimed=db.claim_next('a'); assert claimed.job_id==job.job.job_id
        assert db.claim_next('b') is None
        db.finish(claimed.job_id,'a',S.FAILED,code='failed')
    assert db.counts()[S.FAILED]==3

def test_Q04_root_and_terminal_dedupe(db):
    e=db.enqueue(*item())
    equivalent='c:\\ARCHIVE\\' if sys.platform == 'win32' else native_root('C:/Archive')
    assert db.enqueue(*item(root=equivalent)).job.job_id==e.job.job_id
    j=db.claim_next('a')
    result=NS(status='ARCHIVED',integrity='LOCAL_HASH_ONLY',transferred=True)
    db.finish(j.job_id,'a',S.COMPLETED,result=result,retry_count=3)
    e=db.enqueue(*item()); assert e.outcome=='requeued' and e.job.retry_count==0
    j=db.claim_next('a'); db.finish(j.job_id,'a',S.FAILED,code='bad')
    assert db.enqueue(*item()).job.state==S.FAILED

@pytest.mark.parametrize('old',list(S))
@pytest.mark.parametrize('new',list(S))
def test_Q28_transition_matrix(db,old,new):
    assert {s.value for s in m.TRANSITIONS[old]}==EDGES[old.value]
    j=db.enqueue(*item()).job
    with sqlite3.connect(db.path) as c: c.execute('update jobs set state=? where job_id=?',(old.value,j.job_id))
    if old==S.RUNNING or new==S.RUNNING or new.value not in EDGES[old.value]:
        with pytest.raises(m.InvalidTransitionError): db.transition(j.job_id,new)
    else: assert db.transition(j.job_id,new).state==new

def test_Q21_recovery_stale_owner_and_cooldown(db):
    j=db.enqueue(*item()).job; db.claim_next('old')
    db.recover_interrupted(keep=(j.job_id,)); assert db.get(j.job_id).state==S.RUNNING
    db.recover_interrupted(); assert db.get(j.job_id).last_error_code=='interrupted'
    with pytest.raises(m.InvalidTransitionError): db.finish(j.job_id,'old',S.FAILED)
    db.claim_next('new')
    with pytest.raises(m.InvalidTransitionError): db.finish(j.job_id,'old',S.FAILED)
    db.finish(j.job_id,'new',S.RETRY_WAIT,code='network',retry_count=1,next_attempt_at=200)
    db.transition(j.job_id,S.PAUSED); db.transition(j.job_id,S.QUEUED)
    assert db.claim_next('a',now=199) is None
    assert db.claim_next('a',now=200).retry_count==1

@pytest.mark.parametrize('new',list(S))
def test_Q28_running_finish_matrix(db,new):
    j=db.enqueue(*item()).job; db.claim_next('owner')
    if new==S.RUNNING:
        with pytest.raises(m.InvalidTransitionError): db.finish(j.job_id,'owner',new)
    else:
        result=NS(status='ARCHIVED',integrity='LOCAL_HASH_ONLY',transferred=True)
        assert db.finish(j.job_id,'owner',new,result=result).state==new

def test_Q28_expected_state(db):
    j=db.enqueue(*item()).job
    with pytest.raises(m.InvalidTransitionError): db.transition(j.job_id,S.PAUSED,expected=S.RETRY_WAIT)
    assert db.get(j.job_id).state==S.QUEUED

@pytest.mark.parametrize('method',['get','transition','finish'])
def test_Q27_unknown(db,method):
    args={'get':('missing',),'transition':('missing',S.PAUSED),'finish':('missing','x',S.FAILED)}
    with pytest.raises(m.UnknownJobError): getattr(db,method)(*args[method])

@pytest.mark.parametrize('kind',['garbage','truncated','foreign','newer'])
def test_Q25_untouched(tmp_path,kind):
    path=tmp_path/'q.db'
    if kind=='garbage': path.write_bytes(b'not sqlite')
    elif kind=='foreign':
        with sqlite3.connect(path) as c: c.execute('create table foreign_table(x)')
    else:
        q.QueueStore(path)
        if kind=='newer':
            with sqlite3.connect(path) as c: c.execute('pragma user_version=2')
        else: path.write_bytes(path.read_bytes()[:150])
    before=hashlib.sha256(path.read_bytes()).digest(); siblings=set(tmp_path.iterdir())
    with pytest.raises(m.QueueVersionError if kind=='newer' else m.QueueCorruptError) as err: q.QueueStore(path)
    assert str(path) in str(err.value)
    assert err.value.path==path and err.value.code==('unsupported_version' if kind=='newer' else 'corrupt')
    assert hashlib.sha256(path.read_bytes()).digest()==before and set(tmp_path.iterdir())==siblings

def test_Q26_thread_claims(tmp_path):
    path=tmp_path/'q.db'; db=q.QueueStore(path)
    db.enqueue_many([item(i,root=native_root(f'C:/Archive/{i}')) for i in range(1,25)])
    stores=[q.QueueStore(path) for _ in range(4)]; barrier=threading.Barrier(4)
    def claim(n):
        barrier.wait(timeout=5); found=[]
        while (j:=stores[n].claim_next(str(n))) is not None: found.append(j.job_id)
        return found
    with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(claim,range(4)))
    ids=sum(results,[]); assert len(ids)==len(set(ids))==24

def test_Q29_privacy_sql_and_success_guard(db):
    hostile="x'); DROP TABLE jobs;--.zip"
    j=db.enqueue(*item(name=hostile)).job; assert j.asset_name==hostile
    db.claim_next('worker')
    with pytest.raises(m.InvalidTransitionError): db.finish(j.job_id,'worker',S.COMPLETED)
    db.finish(j.job_id,'worker',S.FAILED,code='https://secret?token=x')
    assert db.get(j.job_id).last_error_code=='unknown_error'
    raw=db.path.read_bytes().lower()
    for word in (b'http',b'sig=',b'token',b'secret'): assert word not in raw

def test_Q29_hostile_display_redacted(db):
    repo,release,a,root=item(name='https://secret?sig=token.zip')
    release=replace(release,tag_name='https://secret?token=x')
    j=db.enqueue(repo,release,a,root).job
    assert j.asset_name==j.tag_name=='[redacted]'
    for word in (b'http',b'sig=',b'token',b'secret'):
        assert word not in db.path.read_bytes().lower()

@pytest.mark.parametrize('field,value',[('id',True),('id',2**63),('state','new'),
    ('supported_extension',None),('name','x'*1025),('size',-1)])
def test_Q02_invalid_asset(db,field,value):
    with pytest.raises(m.InvalidJobError): db.enqueue(*item(**{field:value}))
    assert not db.list_jobs()

def test_Q02_invalid_release_repository_root(db):
    repo,r,a,root=item()
    bad=[(replace(repo,repo='bad/name'),r,a,root),(repo,replace(r,draft=True),a,root),
        (repo,replace(r,assets=()),a,root),(repo,r,a,'relative'),(repo,replace(r,id=0),a,root)]
    for args in bad:
        with pytest.raises(m.InvalidJobError): db.enqueue(*args)
    assert not db.list_jobs()

def test_Q02_enqueue_no_archive_files(db,tmp_path):
    root=tmp_path/'absent'
    assert not root.exists()
    db.enqueue(*item(root=str(root)))
    assert not root.exists()

def test_Q01_busy_error(tmp_path):
    db=q.QueueStore(tmp_path/'q.db',settings=m.QueueSettings(busy_timeout_seconds=0))
    with sqlite3.connect(db.path,isolation_level=None) as c:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(m.QueueBusyError): db.enqueue(*item())

def test_Q01_default_path(monkeypatch,tmp_path):
    if sys.platform != 'win32':
        monkeypatch.setenv('XDG_STATE_HOME',str(tmp_path))
        assert q.default_queue_path()==tmp_path/'covearchive'/'queue.sqlite3'
        monkeypatch.delenv('XDG_STATE_HOME')
        monkeypatch.setenv('HOME',str(tmp_path))
        assert q.default_queue_path()==tmp_path/'.local'/'state'/'covearchive'/'queue.sqlite3'
        return
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    assert q.default_queue_path()==tmp_path/'CoveArchive'/'queue.sqlite3'
    monkeypatch.delenv('LOCALAPPDATA')
    with pytest.raises(m.QueueError): q.default_queue_path()

def test_Q21_runner_lock(tmp_path):
    path=tmp_path/'q.db'
    with q.RunnerLock(path):
        with pytest.raises(m.QueueBusyError): q.RunnerLock(path)
    lock=q.RunnerLock(path); lock.close(); lock.close()
    with q.RunnerLock(path): pass

@pytest.mark.parametrize('name',['tokenizer-secret.zip','sig=ordinary.zip','app?token=x.zip','custom://private.zip'])
def test_Q29_display_exact_rules(db,name):
    repo,r,a,root=item(name=name); r=replace(r,tag_name=name)
    j=db.enqueue(repo,r,a,root).job
    expected='[redacted]' if '?' in name or '://' in name else name
    assert j.asset_name==j.tag_name==expected

def test_Q25_error_codes_and_directory(tmp_path):
    with pytest.raises(m.QueueError) as e: q.QueueStore(tmp_path)
    assert type(e.value) is m.QueueError and e.value.code=='database_unavailable'
    path=tmp_path/'bad'; path.write_bytes(b'garbage')
    with pytest.raises(m.QueueCorruptError) as e: q.QueueStore(path)
    assert e.value.path==path and e.value.code=='corrupt'

@pytest.mark.parametrize('error,code',[(sqlite3.OperationalError('attempt to write a readonly database'),'database_unavailable'),
    (sqlite3.OperationalError('disk I/O error'),'database_unavailable'),
    (sqlite3.IntegrityError('constraint failed'),'database_error')])
def test_Q25_database_error_mapping(db,monkeypatch,error,code):
    def denied(*a,**kw): raise error
    monkeypatch.setattr(sqlite3,'connect',denied)
    with pytest.raises(m.QueueError) as e: db.list_jobs()
    assert type(e.value) is m.QueueError and e.value.code==code

def test_Q25_unwritable_database(db,monkeypatch):
    path=db.path.parent/'readonly.db'; path.touch()
    connect=sqlite3.connect
    def readonly(*args,**kw):
        return connect(path.resolve().as_uri()+'?mode=ro',uri=True,isolation_level=None)
    monkeypatch.setattr(sqlite3,'connect',readonly)
    with pytest.raises(m.QueueError) as e: q.QueueStore(path)
    assert type(e.value) is m.QueueError and e.value.code=='database_unavailable'

def _children(codes):
    children=[]
    try:
        for code,args in codes:
            children.append(subprocess.Popen([sys.executable,'-I','-B','-c',code,*map(str,args)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        return [(p.communicate(timeout=15),p.returncode) for p in children]
    finally:
        for p in children:
            if p.poll() is None: p.kill()
            p.wait(timeout=5)

def test_Q25_hot_journal(db):
    with sqlite3.connect(db.path) as c:
        c.executemany("INSERT INTO jobs(job_id,identity,state,asset_name) VALUES (?,?,'QUEUED',?)",
            [(str(i),str(i),'x'*500) for i in range(3000)])
    code="import sqlite3,os,sys; c=sqlite3.connect(sys.argv[1],isolation_level=None); c.execute('PRAGMA cache_size=5'); c.execute('BEGIN IMMEDIATE'); c.execute(\"UPDATE jobs SET asset_name=asset_name||'yyyyyyyyyyyyyyyyyyyy'\"); os._exit(0)"
    assert _children([(code,(db.path,))])[0][1]==0
    journal=Path(str(db.path)+'-journal'); assert journal.exists()
    q.QueueStore(db.path)
    with sqlite3.connect(db.path) as c:
        assert c.execute('SELECT DISTINCT length(asset_name) FROM jobs').fetchall()==[(500,)]
    assert not journal.exists()

def test_Q01_fresh_process_open_race(tmp_path):
    path=tmp_path/'fresh.db'
    code="import sys; sys.path.insert(0,sys.argv[1]); from covearchive.queue.store import QueueStore; sys.stdin.readline(); QueueStore(sys.argv[2]); print('OK')"
    # A future instant synchronizes four independent interpreters without parent polling.
    code=code.replace('sys.stdin.readline()','import time; deadline=float(sys.argv[3]); exec(\"while time.time()<deadline: pass\")')
    import time
    result=_children([(code,(Path('src').resolve(),path,time.time()+0.8)) for _ in range(4)])
    assert result==[(('OK\n',''),0)]*4
    with sqlite3.connect(path) as c: assert c.execute("SELECT name FROM sqlite_master WHERE name='jobs'").fetchall()==[('jobs',)]

@pytest.mark.parametrize('operation',['enqueue','claim','recover'])
def test_Q25_F1_live_version_guard(db,operation):
    with sqlite3.connect(db.path) as c: c.execute('PRAGMA user_version=2')
    before=db.path.read_bytes()
    with pytest.raises(m.QueueVersionError):
        {'enqueue':lambda:db.enqueue(*item()),'claim':lambda:db.claim_next('a'),'recover':db.recover_interrupted}[operation]()
    assert db.path.read_bytes()==before

def test_Q25_F1_foreign_empty(tmp_path):
    path=tmp_path/'foreign.db'
    with sqlite3.connect(path) as c: c.execute('PRAGMA application_id=123')
    before=path.read_bytes()
    with pytest.raises(m.QueueCorruptError): q.QueueStore(path)
    assert path.read_bytes()==before

def test_Q21_F5_recovery_attempt_bound(db):
    j=db.enqueue(*item()).job
    for count in range(1,4):
        db.claim_next('dead'); db.recover_interrupted(keep=(j.job_id,))
        assert db.get(j.job_id).retry_count==count-1
        db.recover_interrupted(max_attempts=3); current=db.get(j.job_id)
        assert current.retry_count==count and current.last_error_code=='interrupted'
        assert current.state==(S.FAILED if count==3 else S.QUEUED)
    assert db.claim_next('a') is None

def test_Q25_TN08_live_application_id(db):
    with sqlite3.connect(db.path) as c: c.execute('PRAGMA application_id=123')
    before=db.path.read_bytes()
    with pytest.raises(m.QueueCorruptError): db.enqueue(*item())
    assert db.path.read_bytes()==before

def test_Q19_S2_cooldown_max_and_gate(db):
    db.enqueue_many([item(i,root=native_root(f'C:/A/{i}')) for i in (1,2,3)]); a=db.claim_next('a'); b=db.claim_next('b')
    for j,owner,until in ((a,'a',400),(b,'b',160)): db.finish(j.job_id,owner,S.FAILED,cooldown_until=until)
    assert db.cooldown_until()==400
    other=q.QueueStore(db.path); assert other.claim_next('c',now=399) is None
    assert other.claim_next('c',now=400).asset_id==3
    code="import sys; sys.path.insert(0,sys.argv[1]); from covearchive.queue import QueueStore; print(QueueStore(sys.argv[2]).cooldown_until())"
    assert _children([(code,(Path('src').resolve(),db.path))])[0]==(('400.0\n',''),0)

@pytest.mark.parametrize('damage',['none','old','extra'])
def test_Q25_S1_ordered_schema(db,damage):
    with sqlite3.connect(db.path) as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()==[('cooldowns',),('jobs',)]
        if damage=='old': c.execute('DROP TABLE cooldowns')
        if damage=='extra': c.execute('CREATE TABLE extra(x)')
    failure=None
    try: q.QueueStore(db.path)
    except m.QueueError as exc: failure=exc
    assert (failure is None) if damage=='none' else isinstance(failure,m.QueueCorruptError)

_ROOT_ALIASES=[('D:/','D:\\','D:/.','d:\\.\\'),('D:/Archive','d:\\ARCHIVE\\','D:/Archive/./','D:\\\\Archive')]
@pytest.mark.parametrize('roots',_ROOT_ALIASES)
@pytest.mark.skipif(sys.platform != 'win32', reason='Windows drive, case and separator aliases are not native Linux roots')
def test_Q04_H1_root_aliases(db,roots):
    jobs=[db.enqueue(*item(root=root)).job for root in roots]
    assert len({j.job_id for j in jobs})==len({j.project_key for j in jobs})==1

@pytest.mark.parametrize('roots',_ROOT_ALIASES)
@pytest.mark.skipif(sys.platform != 'win32', reason='Windows drive, case and separator aliases are not native Linux roots')
def test_Q10_H1_alias_project_claim(db,roots):
    db.enqueue(*item(1,root=roots[0])); db.enqueue(*item(2,root=roots[-1]))
    assert db.claim_next('first') is not None and db.claim_next('second') is None
