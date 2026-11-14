from dataclasses import replace
from types import SimpleNamespace as NS
import threading
import sqlite3
import pytest
from covearchive.queue import models as m
from covearchive.queue.store import QueueStore, RunnerLock
from covearchive.queue.runner import QueueRunner
from covearchive.archive.download import ArchiveResult, ArchiveStatus
from covearchive.github import client as g
from test_queue_store import item

S=m.JobState
def result(status='ARCHIVED',code=None,**kw): return ArchiveResult(ArchiveStatus(status),code,1,10,**kw)
def catalog(items): return lambda repo:NS(releases=tuple({r.id:r for target,r,a,root in items if target==repo}.values()))
def prepare(tmp_path,items,**kw):
    store=QueueStore(tmp_path/'q.db'); store.enqueue_many(items)
    return store,QueueRunner(store,catalog=catalog(items),**kw)
class Clock:
    def __init__(self): self.now=100; self.sleeps=[]
    def __call__(self): return self.now
    def sleep(self,n): self.sleeps.append(n); self.now=round(self.now+n,9)

def test_Q07_Q08_Q09_Q10_bounded_projects(tmp_path):
    items=[item(1),item(2),item(3,root='C:/Other'),item(4,root='C:/Third')]
    # One catalog release contains all current asset ids.
    r=replace(items[0][1],assets=tuple(i[2] for i in items)); items=[(p,r,a,root) for p,_,a,root in items]
    barrier=threading.Barrier(2); lock=threading.Lock(); active=set(); seen=[]; peak=[0]
    def archive(repo,release,asset,root,**kw):
        with lock:
            assert root not in active; active.add(root); peak[0]=max(peak[0],len(active)); seen.append(asset.id)
        barrier.wait(timeout=5)
        with lock: active.remove(root)
        return result()
    store,runner=prepare(tmp_path,items,archive=archive)
    store.enqueue(*items[0]); summary=runner.run_pending()
    assert peak==[2] and len(seen)==len(set(seen))==4
    assert store.counts()[S.COMPLETED]==4 and summary.completed==4
    assert not any(t.name.startswith('covearchive-queue-') for t in threading.enumerate())

@pytest.mark.parametrize('retry_after,delay',[(None,5.5),(37,37)])
def test_Q17_Q19_retry_exact_and_wait(tmp_path,retry_after,delay):
    clock=Clock(); calls=[]
    def archive(*a,**kw):
        calls.append(clock())
        return result('FAILED','network',retryable=True,retry_after_seconds=retry_after) if len(calls)==1 else result()
    store,runner=prepare(tmp_path,[item()],archive=archive,clock=clock,sleep=clock.sleep,rng=lambda:0.5)
    runner.run_pending(); j=store.list_jobs()[0]
    assert j.state==S.RETRY_WAIT and j.next_attempt_at==100+delay and calls==[100]
    runner.run_pending(wait_for_retries=True)
    assert calls==[100,100+delay] and store.list_jobs()[0].state==S.COMPLETED

def test_Q18_retry_limit(tmp_path):
    clock=Clock(); calls=[]
    def archive(*a,**kw): calls.append(clock()); return result('FAILED','network',retryable=True)
    store,runner=prepare(tmp_path,[item()],archive=archive,settings=m.QueueSettings(max_attempts=3),clock=clock,sleep=clock.sleep,rng=lambda:0)
    runner.run_pending(wait_for_retries=True); j=store.list_jobs()[0]
    assert calls==[100,105,115] and j.retry_count==3 and j.state==S.FAILED and j.last_error_code=='network'

@pytest.mark.parametrize('status,code',[('FAILED','unknown_cdn_host'),('FAILED','disk_full'),('MODIFIED','hash_mismatch'),('CONFLICT','conflict'),('MISMATCH','size_mismatch'),('RECONCILE_REQUIRED','metadata_changed')])
def test_Q20_permanent(tmp_path,status,code):
    store,runner=prepare(tmp_path,[item()],archive=lambda *a,**kw:result(status,code,retryable=True))
    runner.run_pending(wait_for_retries=True); j=store.list_jobs()[0]
    assert j.state==S.FAILED and j.retry_count==0 and j.last_error_code==code

@pytest.mark.parametrize('kind,code',[('release','release_removed'),('asset','asset_unavailable')])
def test_Q30_id_only_and_files_untouched(tmp_path,kind,code):
    marker=tmp_path/'archive.zip'; marker.write_bytes(b'preserve')
    store=QueueStore(tmp_path/'q.db'); args=item(); store.enqueue(*args)
    r=replace(args[1],assets=(replace(args[2],id=99),))
    fetch=[]
    runner=QueueRunner(store,catalog=lambda repo:(fetch.append(repo) or NS(releases=() if kind=='release' else (r,))),archive=lambda *a,**kw:pytest.fail('unexpected archive'))
    runner.run_pending(); assert store.list_jobs()[0].last_error_code==code
    assert marker.read_bytes()==b'preserve' and len(fetch)==1

def test_Q31_cache_rescan_and_internal_error(tmp_path):
    args=[item(1),item(2)]; r=replace(args[0][1],assets=tuple(i[2] for i in args)); fetch=[]; calls=[]
    store=QueueStore(tmp_path/'q.db'); store.enqueue(*args[0])
    def cat(repo): fetch.append(repo); return NS(releases=(r,))
    def archive(repo,r,a,root,**kw):
        calls.append(a.id)
        if a.id==1: raise RuntimeError('secret exception')
        return result()
    runner=QueueRunner(store,catalog=cat,archive=archive)
    runner.run_pending(); store.enqueue_many(args); runner.run_pending()
    assert len(fetch)==2 and calls==[1,2]
    assert [j.state for j in store.list_jobs()]==[S.FAILED,S.COMPLETED]
    assert store.list_jobs()[0].last_error_code=='internal_error'

@pytest.mark.parametrize('error,code,retry',[(g.NetworkError(),'network',True),(g.NotFoundError(),'upstream_removed',False),
    (g.IncompleteScanError(0,'failed',g.RequestTimeoutError()),'timeout',True),
    (g.IncompleteScanError(0,'failed'),'incomplete_scan',False),(ValueError('secret'),'internal_error',False),
    (g.RateLimitError(kind='secondary',status=429,retry_after_seconds=71,retry_at=None),'rate_limited',True)])
def test_Q17_catalog_negative_cache(tmp_path,error,code,retry):
    calls=[]; args=[item(1),item(2,root='C:/Other')]; store=QueueStore(tmp_path/'q.db'); store.enqueue_many(args)
    def cat(repo): calls.append(repo); raise error
    runner=QueueRunner(store,catalog=cat,archive=lambda *a,**kw:pytest.fail('archive'),clock=lambda:100)
    runner.run_pending(); jobs=store.list_jobs()
    assert len(calls)==1 and all(j.last_error_code==code for j in jobs)
    assert all(j.state==(S.RETRY_WAIT if retry else S.FAILED) for j in jobs)
    if isinstance(error,g.RateLimitError): assert all(j.next_attempt_at==171 for j in jobs)

def test_Q07_fifo_and_positive_cache(tmp_path):
    args=[item(i) for i in range(1,4)]; r=replace(args[0][1],assets=tuple(i[2] for i in args)); calls=[]; fetch=[]
    store=QueueStore(tmp_path/'q.db'); store.enqueue_many(args)
    runner=QueueRunner(store,catalog=lambda repo:(fetch.append(repo) or NS(releases=(r,))),archive=lambda repo,r,a,root,**kw:(calls.append(a.id) or result()))
    runner.run_pending(); assert calls==[1,2,3] and len(fetch)==1

def test_Q21_finish_failure_propagates(tmp_path,monkeypatch):
    store,runner=prepare(tmp_path,[item()],archive=lambda *a,**kw:result())
    def fail(*a,**kw): raise m.QueueError('database_unavailable')
    monkeypatch.setattr(store,'finish',fail)
    with pytest.raises(m.QueueError,match='database_unavailable'): runner.run_pending()
    assert store.list_jobs()[0].state==S.RUNNING

def test_Q07_progress_memory_and_arguments(tmp_path):
    clock=Clock()
    def archive(repo,r,a,root,**kw):
        assert kw['policy']=='policy' and kw['session'] is None
        assert kw['clock'] is clock and kw['sleep']==clock.sleep and kw['cancel']() is False
        control=next(iter(runner._active.values()))
        assert control.bytes_done is None and control.speed_bps is None
        kw['progress'](0,400); clock.now+=0.5; kw['progress'](100,400)
        assert control.speed_bps is None
        clock.now+=1; kw['progress'](300,400)
        assert (control.bytes_done,control.bytes_total,control.speed_bps)==(300,400,200)
        assert store.list_jobs()[0].state==S.RUNNING
        return result()
    store,runner=prepare(tmp_path,[item()],archive=archive,policy='policy',monotonic=clock,sleep=clock.sleep)
    runner.run_pending(); assert store.list_jobs()[0].state==S.COMPLETED

@pytest.mark.parametrize('reason,state',[('pause',S.PAUSED),('cancel',S.CANCELLED),('shutdown',S.QUEUED)])
@pytest.mark.parametrize('success',[True,False])
def test_Q16_worker_precedence(tmp_path,reason,state,success):
    def archive(*a,**kw):
        with runner._state: next(iter(runner._active.values())).reason=reason
        assert kw['cancel']()
        return result() if success else result('FAILED','network',retryable=True)
    store,runner=prepare(tmp_path,[item()],archive=archive)
    # Shutdown-to-QUEUED must stop this run instead of reclaiming the same job.
    if reason=='shutdown':
        base=archive
        def archive(*a,**kw):
            value=base(*a,**kw)
            with runner._state: runner._stopping=True
            return value
        runner.archive=archive
    runner.run_pending(); assert store.list_jobs()[0].state==(S.COMPLETED if success else state)

def test_Q15_stop_between_catalog_and_archive(tmp_path):
    args=item()
    def cat(repo):
        with runner._state: next(iter(runner._active.values())).reason='cancel'
        return NS(releases=(args[1],))
    store=QueueStore(tmp_path/'q.db'); store.enqueue(*args)
    runner=QueueRunner(store,catalog=cat,archive=lambda *a,**kw:pytest.fail('stopped before archive'))
    runner.run_pending(); assert store.list_jobs()[0].state==S.CANCELLED

def test_Q09_runner_lock_and_concurrent_call(tmp_path):
    entered=threading.Event(); release=threading.Event(); errors=[]
    def archive(*a,**kw):
        entered.set(); assert release.wait(5); return result()
    store,runner=prepare(tmp_path,[item()],archive=archive)
    def run():
        try: runner.run_pending()
        except Exception as exc: errors.append(exc)
    thread=threading.Thread(target=run)
    try:
        thread.start(); assert entered.wait(5)
        with pytest.raises(m.QueueError,match='already_running'): runner.run_pending()
        other=QueueRunner(store)
        with pytest.raises(m.QueueBusyError): other.run_pending()
    finally:
        release.set(); thread.join(5)
        assert not thread.is_alive()
        if runner._lock is not None: runner._lock.close()
    assert not errors and store.list_jobs()[0].state==S.COMPLETED

def test_Q17_catalog_failure_expires(tmp_path):
    clock=Clock(); args=item(); fetch=[]
    def cat(repo):
        fetch.append(clock())
        if len(fetch)==1: raise g.NetworkError()
        return NS(releases=(args[1],))
    store=QueueStore(tmp_path/'q.db',clock=clock); store.enqueue(*args)
    runner=QueueRunner(store,catalog=cat,archive=lambda *a,**kw:result(),clock=clock,sleep=clock.sleep,rng=lambda:0)
    runner.run_pending(wait_for_retries=True)
    assert fetch==[100,105] and store.list_jobs()[0].state==S.COMPLETED

def test_Q17_many_jobs_share_failure_ttl(tmp_path):
    clock=Clock(); fetch=[]; store=QueueStore(tmp_path/'q.db',clock=clock)
    store.enqueue_many([item(i,root=f'C:/Archive/{i}') for i in range(1,9)])
    def cat(repo): fetch.append(clock()); raise g.NetworkError()
    with QueueRunner(store,catalog=cat,clock=clock,rng=lambda:0) as runner: runner.run_pending()
    assert fetch==[100] and store.counts()[S.RETRY_WAIT]==8

def test_Q20_permanent_catalog_cache_retained(tmp_path):
    clock=Clock(); fetch=[]; store=QueueStore(tmp_path/'q.db',clock=clock)
    store.enqueue_many([item(i) for i in range(1,4)])
    def cat(repo): fetch.append(clock()); clock.now+=100; raise g.NotFoundError()
    with QueueRunner(store,catalog=cat,clock=clock) as runner: runner.run_pending()
    assert fetch==[100] and store.counts()[S.FAILED]==3

@pytest.mark.parametrize('kind',['release','asset'])
def test_Q31_mid_run_enqueue_refresh(tmp_path,kind):
    clock=Clock(); first=item(); second=item(2)
    if kind=='release': second=(second[0],replace(second[1],id=11),second[2],second[3])
    store=QueueStore(tmp_path/'q.db',clock=clock); store.enqueue(*first); fetch=[]; calls=[]
    def cat(repo):
        fetch.append(clock())
        if len(fetch)==1: return NS(releases=(first[1],))
        releases=(first[1],second[1]) if kind=='release' else (replace(first[1],assets=(first[2],second[2])),)
        return NS(releases=releases)
    def archive(repo,r,a,root,**kw):
        calls.append(a.id)
        if a.id==1: clock.now+=1; store.enqueue(*second)
        return result()
    runner=QueueRunner(store,catalog=cat,archive=archive,clock=clock)
    runner.run_pending(); assert calls==[1,2] and fetch==[100,101]
    assert store.counts()[S.COMPLETED]==2

def test_Q19_retry_sleep_slices(tmp_path):
    clock=Clock(); calls=[]
    def archive(*a,**kw):
        calls.append(clock())
        return result('FAILED','network',retryable=True,retry_after_seconds=7) if len(calls)==1 else result()
    store,runner=prepare(tmp_path,[item()],archive=archive,clock=clock,sleep=clock.sleep)
    runner.run_pending(wait_for_retries=True)
    assert calls==[100,107] and len(clock.sleeps)>30 and max(clock.sleeps)<=0.2

def test_Q16_unexpected_cancel(tmp_path):
    store,runner=prepare(tmp_path,[item()],archive=lambda *a,**kw:result('CANCELLED','cancelled'))
    runner.run_pending(); j=store.list_jobs()[0]
    assert (j.state,j.last_error_code)==(S.FAILED,'unexpected_cancel')

def test_Q11_Q13_Q14_pending_controls(tmp_path):
    store,runner=prepare(tmp_path,[item()],archive=lambda *a,**kw:result())
    j=store.list_jobs()[0]
    assert runner.pause(j.job_id).state==S.PAUSED
    assert runner.run_pending().completed==0
    assert runner.resume(j.job_id).state==S.QUEUED
    assert runner.cancel(j.job_id).state==S.CANCELLED
    assert runner.retry(j.job_id).state==S.QUEUED
    runner.run_pending(); assert store.get(j.job_id).state==S.COMPLETED
    with pytest.raises(m.InvalidTransitionError): runner.retry(j.job_id)
    runner.shutdown()

def test_Q13_cooldown_and_retry_reset(tmp_path):
    clock=Clock()
    store,runner=prepare(tmp_path,[item()],archive=lambda *a,**kw:result('FAILED','network',retryable=True),clock=clock,rng=lambda:0)
    runner.run_pending(); j=store.list_jobs()[0]
    runner.pause(j.job_id); runner.resume(j.job_id)
    assert store.get(j.job_id).next_attempt_at==105
    runner.run_pending(); assert store.get(j.job_id).retry_count==1
    runner.cancel(j.job_id); assert runner.retry(j.job_id).retry_count==0
    runner.shutdown()

@pytest.mark.parametrize('method',['pause','resume','cancel','retry'])
def test_Q27_controls_unknown_and_unowned(tmp_path,method):
    store,runner=prepare(tmp_path,[item()],archive=lambda *a,**kw:result())
    with pytest.raises(m.UnknownJobError): getattr(runner,method)('missing')
    j=store.claim_next('other')
    with pytest.raises(m.InvalidTransitionError): getattr(runner,method)(j.job_id)
    assert store.get(j.job_id).claimed_by=='other'
    runner.shutdown()

def _start(runner):
    errors=[]; summaries=[]
    def run():
        try: summaries.append(runner.run_pending())
        except Exception as exc: errors.append(exc)
    thread=threading.Thread(target=run); thread.start()
    return thread,errors,summaries

@pytest.mark.parametrize('action,state',[('pause',S.PAUSED),('cancel',S.CANCELLED),('shutdown',S.QUEUED)])
@pytest.mark.parametrize('completed',[False,True])
def test_Q12_Q15_Q16_Q32_active_controls(tmp_path,action,state,completed):
    entered=threading.Event(); finish=threading.Event(); seen_stop=threading.Event(); calls=[]
    def archive(*a,**kw):
        calls.append(1)
        if len(calls)>1: return result()
        entered.set(); assert finish.wait(5)
        if kw['cancel'](): seen_stop.set()
        return result() if completed else result('CANCELLED','cancelled')
    store,runner=prepare(tmp_path,[item()],archive=archive); j=store.list_jobs()[0]
    thread,errors,summaries=_start(runner)
    try:
        assert entered.wait(5)
        if action=='shutdown': assert runner.shutdown(timeout=0).unresolved==(j.job_id,)
        else: getattr(runner,action)(j.job_id)
        with sqlite3.connect(store.path) as c:
            assert c.execute('select stop_requested from jobs').fetchone()==(None if action=='shutdown' else action,)
    finally: finish.set(); thread.join(5)
    assert not thread.is_alive() and not errors and seen_stop.is_set()
    assert store.get(j.job_id).state==(S.COMPLETED if completed else state)
    with sqlite3.connect(store.path) as c: assert c.execute('select stop_requested from jobs').fetchone()==(None,)
    if action=='pause' and not completed:
        assert summaries[0].paused==1
        runner.run_pending(); assert calls==[1]
        runner.resume(j.job_id); runner.run_pending(); assert calls==[1,1]
    if action=='shutdown' and not completed: assert summaries[0].requeued==1
    assert runner.shutdown().unresolved==() and runner.shutdown().unresolved==()
    with RunnerLock(store.path): pass
    with pytest.raises(m.QueueError,match='shut_down'): runner.run_pending()
    assert not any(t.name.startswith('covearchive-queue-') for t in threading.enumerate())

def test_Q32_unresolved_lock_retained(tmp_path):
    entered=threading.Event(); finish=threading.Event()
    def archive(*a,**kw): entered.set(); assert finish.wait(5); return result('CANCELLED','cancelled')
    store,runner=prepare(tmp_path,[item()],archive=archive); thread,errors,_=_start(runner)
    try:
        assert entered.wait(5)
        assert runner.shutdown(timeout=0).unresolved==(store.list_jobs()[0].job_id,)
        with pytest.raises(m.QueueBusyError): RunnerLock(store.path)
    finally: finish.set(); thread.join(5)
    assert not thread.is_alive() and not errors
    assert runner.shutdown().unresolved==()
    with RunnerLock(store.path): pass

def test_Q32_context_and_retry_wait_shutdown(tmp_path):
    clock=Clock(); sleeps=[]
    def sleep(n):
        sleeps.append(n); clock.sleep(n); runner.shutdown(timeout=0)
    store,runner=prepare(tmp_path,[item()],archive=lambda *a,**kw:result('FAILED','network',retryable=True,retry_after_seconds=86400),clock=clock,sleep=sleep)
    with runner:
        runner.run_pending(wait_for_retries=True)
    assert sleeps==[0.2] and runner.shutdown().unresolved==()
    with RunnerLock(store.path): pass

def test_Q07_snapshot_progress(tmp_path):
    clock=Clock(); entered=threading.Event(); advance=threading.Event(); progressed=threading.Event(); finish=threading.Event()
    def archive(*a,**kw):
        entered.set(); assert advance.wait(5)
        kw['progress'](0,42); clock.now+=2; kw['progress'](20,42); progressed.set()
        assert finish.wait(5); return result()
    store,runner=prepare(tmp_path,[item()],archive=archive,monotonic=clock)
    # Include a completed, pending, failed and cancelled job in the aggregates.
    for i,to in ((2,S.COMPLETED),(3,S.FAILED),(4,S.CANCELLED)):
        j=store.enqueue(*item(i)).job
        with store._write() as c: c.execute('UPDATE jobs SET state=? WHERE job_id=?',(to.value,j.job_id))
    thread,errors,_=_start(runner)
    try:
        assert entered.wait(5); snap=runner.snapshot()
        assert snap.active[0].bytes_done is None and snap.active[0].speed_bps is None
        assert (snap.bytes_done,snap.bytes_total)==(42,84) and snap.counts[S.COMPLETED]==1
        advance.set(); assert progressed.wait(5); snap=runner.snapshot()
        assert (snap.active[0].bytes_done,snap.active[0].bytes_total,snap.active[0].speed_bps)==(20,42,10)
        assert (snap.bytes_done,snap.bytes_total)==(62,84)
    finally: advance.set(); finish.set(); thread.join(5); runner.shutdown()
    assert not thread.is_alive() and not errors

def test_Q25_F1_runner_live_version(tmp_path):
    import sqlite3
    calls=[]; store,runner=prepare(tmp_path,[item()],archive=lambda *a,**k:calls.append(1))
    with sqlite3.connect(store.path) as c: c.execute('PRAGMA user_version=2')
    before=store.path.read_bytes()
    try:
        with pytest.raises(m.QueueVersionError): runner.run_pending()
        assert not calls and store.path.read_bytes()==before
    finally: runner.shutdown()

@pytest.mark.parametrize('first,second,state',[('cancel','shutdown',S.CANCELLED),('pause','shutdown',S.PAUSED),
    ('cancel','pause',S.CANCELLED),('pause','cancel',S.CANCELLED),('shutdown','pause',S.PAUSED)])
def test_Q16_F2_stop_rank(tmp_path,first,second,state):
    entered=threading.Event(); finish=threading.Event()
    def archive(*a,**kw): entered.set(); assert finish.wait(5); return result('CANCELLED','cancelled')
    store,runner=prepare(tmp_path,[item()],archive=archive); job=store.list_jobs()[0]
    thread,errors,_=_start(runner)
    try:
        assert entered.wait(5)
        for action in (first,second):
            if action=='shutdown': runner.shutdown(timeout=0)
            else: getattr(runner,action)(job.job_id)
    finally: finish.set(); thread.join(5); runner.shutdown()
    assert not thread.is_alive() and not errors and store.get(job.job_id).state==state
    with QueueRunner(QueueStore(store.path),catalog=lambda repo:pytest.fail('stopped job fetched')) as reopened:
        assert reopened.run_pending().completed==0

def test_Q19_F3_slow_catalog_ttl(tmp_path):
    clock=Clock(); args=item(); fetch=[]
    def cat(repo):
        fetch.append(clock()); clock.now=160 if len(fetch)==1 else 190
        raise g.RateLimitError(kind='secondary',status=429,retry_after_seconds=30,retry_at=None)
    store=QueueStore(tmp_path/'q.db',clock=clock); store.enqueue(*args)
    with QueueRunner(store,catalog=cat,clock=clock) as runner:
        runner.run_pending(); assert store.list_jobs()[0].next_attempt_at==190
        from covearchive.queue.runner import _Deferred
        clock.now=170; store.enqueue(*item(2))
        with pytest.raises(_Deferred): runner._metadata(args[0])
        assert fetch==[100]
        # Exercise the cached outcome through a worker in the same run's cache.
        assert store.claim_next(runner._owner,now=170) is None
        clock.now=190; runner._metadata(args[0]); assert fetch==[100,190]

@pytest.mark.parametrize('status',[500,501,502,503,504,505])
@pytest.mark.parametrize('wrapped',[False,True])
def test_Q20_F4_catalog_5xx(tmp_path,status,wrapped):
    error=g.TransientServerError(status=status)
    if wrapped: error=g.IncompleteScanError(0,'failed',error)
    def cat(repo): raise error
    store=QueueStore(tmp_path/'q.db'); store.enqueue(*item())
    with QueueRunner(store,catalog=cat) as runner: runner.run_pending()
    assert store.list_jobs()[0].state==(S.RETRY_WAIT if status in (500,502,503,504) else S.FAILED)
    assert store.list_jobs()[0].last_error_code=='server_error'

def test_Q21_F5_runner_recovery_limit(tmp_path):
    store=QueueStore(tmp_path/'q.db'); store.enqueue(*item()); store.claim_next('dead')
    calls=[]
    with QueueRunner(store,settings=m.QueueSettings(max_attempts=1),catalog=lambda repo:calls.append(repo)) as runner:
        runner.run_pending()
    j=store.list_jobs()[0]; assert not calls and (j.state,j.retry_count,j.last_error_code)==(S.FAILED,1,'interrupted')

@pytest.mark.parametrize('action',['pause','cancel','shutdown'])
@pytest.mark.parametrize('real_failure',[False,True])
def test_Q16_F6_stop_error_code(tmp_path,action,real_failure):
    def archive(*a,**kw):
        j=store.list_jobs()[0]
        if action=='shutdown': runner.shutdown(timeout=0)
        else: getattr(runner,action)(j.job_id)
        return result('FAILED','network',retryable=True) if real_failure else result('CANCELLED','cancelled')
    store,runner=prepare(tmp_path,[item()],archive=archive)
    try:
        runner.run_pending(); j=store.list_jobs()[0]
        assert j.last_error_code==('network' if real_failure else None)
        assert j.result_status==('FAILED' if real_failure else 'CANCELLED')
    finally: runner.shutdown()

def _blocked_after_claim_error(tmp_path,monkeypatch):
    entered=threading.Event(); finish=threading.Event(); calls=[]
    def archive(*a,**kw): calls.append(1); entered.set(); assert finish.wait(5); return result('CANCELLED','cancelled')
    store,runner=prepare(tmp_path,[item()],archive=archive); claim=store.claim_next; claims=[]
    def fail_second(*a,**kw):
        claims.append(1)
        if len(claims)==2: raise m.QueueBusyError('database_busy')
        return claim(*a,**kw)
    monkeypatch.setattr(store,'claim_next',fail_second)
    with pytest.raises(m.QueueBusyError): runner.run_pending()
    assert entered.wait(5)
    return store,runner,finish,calls

def test_Q21_F7_keep_live_workers(tmp_path,monkeypatch):
    store,runner,finish,calls=_blocked_after_claim_error(tmp_path,monkeypatch)
    recovered=threading.Event(); observed=[]; recover=store.recover_interrupted
    def watch(*a,**kw):
        value=recover(*a,**kw); observed.append(store.list_jobs()[0]); recovered.set(); return value
    monkeypatch.setattr(store,'recover_interrupted',watch); thread,errors,_=_start(runner)
    try:
        assert recovered.wait(5) and observed[0].state==S.RUNNING and calls==[1]
    finally: finish.set(); thread.join(5); runner.shutdown()
    assert not thread.is_alive() and not errors and calls==[1]

def test_Q32_F7_lock_without_run_loop(tmp_path,monkeypatch):
    store,runner,finish,calls=_blocked_after_claim_error(tmp_path,monkeypatch)
    try:
        report=runner.shutdown(timeout=0.01); assert report.unresolved==(store.list_jobs()[0].job_id,)
        with pytest.raises(m.QueueBusyError): RunnerLock(store.path)
        with QueueRunner(store) as other:
            with pytest.raises(m.QueueBusyError): other.run_pending()
    finally:
        finish.set()
        for control in tuple(runner._active.values()): control.thread.join(5)
        assert runner.shutdown().unresolved==()
    with RunnerLock(store.path): pass

def test_Q32_J2_automatic_release(tmp_path,monkeypatch):
    store,runner,finish,calls=_blocked_after_claim_error(tmp_path,monkeypatch)
    controls=tuple(runner._active.values()); committing=threading.Event(); commit=threading.Event(); original=store.finish
    def blocked_finish(*a,**kw):
        committing.set(); assert commit.wait(5); return original(*a,**kw)
    monkeypatch.setattr(store,'finish',blocked_finish)
    try:
        assert runner.shutdown(timeout=0).unresolved==(store.list_jobs()[0].job_id,)
        with pytest.raises(m.QueueBusyError): RunnerLock(store.path)
        finish.set(); assert committing.wait(5)
        assert all(not c.done for c in controls)
        with pytest.raises(m.QueueBusyError): RunnerLock(store.path)
        commit.set()
        for control in controls: control.thread.join(5); assert not control.thread.is_alive()
        assert not runner._active and runner._lock is None
        with RunnerLock(store.path): pass
        with QueueRunner(store,catalog=catalog([item()]),archive=lambda *a,**kw:result()) as other:
            assert other.run_pending().completed==1
    finally: finish.set(); commit.set(); runner.shutdown()

def test_Q32_F7_shutdown_joins_cooperative(tmp_path):
    entered=threading.Event(); tick=threading.Event()
    def archive(*a,**kw):
        entered.set()
        while not kw['cancel'](): tick.wait(0.05)
        return result('CANCELLED','cancelled')
    store,runner=prepare(tmp_path,[item()],archive=archive); thread,errors,_=_start(runner)
    try:
        assert entered.wait(5)
        assert runner.shutdown(timeout=5).unresolved==()
        assert store.list_jobs()[0].state==S.QUEUED
    finally: runner.shutdown(); thread.join(5)
    assert not thread.is_alive() and not errors

@pytest.mark.parametrize('source',['catalog','cache','archive'])
@pytest.mark.parametrize('action,state',[('pause',S.PAUSED),('cancel',S.CANCELLED),('shutdown',S.QUEUED)])
def test_Q19_FA_stop_cooldown(tmp_path,source,action,state):
    clock=Clock(); entered=threading.Event(); finish=threading.Event(); fetch=[]; calls=[]; args=item()
    def blocked(): entered.set(); assert finish.wait(5)
    def cat(repo):
        fetch.append(1)
        if source=='archive': return NS(releases=(args[1],))
        if source=='catalog': blocked()
        raise g.RateLimitError(kind='secondary',status=429,retry_after_seconds=120,retry_at=None)
    def archive(*a,**kw): blocked(); return result('FAILED','rate_limited',retryable=True,retry_after_seconds=120)
    store=QueueStore(tmp_path/'q.db',clock=clock); j=store.enqueue(*args).job
    runner=QueueRunner(store,catalog=cat,archive=archive,clock=clock); metadata=runner._metadata
    def cached(*a,**kw):
        value=metadata(*a,**kw)
        if source=='cache': value=metadata(*a,**kw); blocked()
        return value
    runner._metadata=cached; thread,errors,_=_start(runner)
    try:
        assert entered.wait(5)
        if action=='shutdown': runner.shutdown(timeout=0)
        else: getattr(runner,action)(j.job_id)
    finally: finish.set(); thread.join(5)
    try:
        stopped=store.get(j.job_id)
        assert not errors and stopped.state==state and stopped.next_attempt_at==220
        assert stopped.retry_count==0 and stopped.last_error_code=='rate_limited' and fetch==[1] and store.cooldown_until()==220
        if action=='pause': runner.resume(j.job_id)
        elif action=='cancel': runner.retry(j.job_id)
    finally: runner.shutdown()
    store.enqueue(replace(args[0],repo='Other'),args[1],args[2],args[3])
    def success(*a,**kw): calls.append(clock()); return result()
    with QueueRunner(QueueStore(store.path),clock=clock,catalog=lambda repo:NS(releases=(args[1],)),archive=success) as fresh:
        for now in (100,219): clock.now=now; fresh.run_pending(); assert not calls
        clock.now=220; fresh.run_pending(); assert calls==[220,220]

@pytest.mark.parametrize('code,maximum,limit',[('manifest_commit_incomplete',5,2),('manifest_commit_incomplete',1,1),('network',4,4)])
def test_Q18_FB_manifest_retry_cap(tmp_path,code,maximum,limit):
    clock=Clock(); calls=[]
    def archive(*a,**kw): calls.append(1); return result('FAILED',code,retryable=True)
    store,runner=prepare(tmp_path,[item()],archive=archive,settings=m.QueueSettings(max_attempts=maximum),clock=clock,sleep=clock.sleep,rng=lambda:0)
    with runner:
        runner.run_pending(); assert store.list_jobs()[0].state==(S.FAILED if limit==1 else S.RETRY_WAIT)
        runner.run_pending(wait_for_retries=True); j=store.list_jobs()[0]
        assert len(calls)==j.retry_count==limit and j.state==S.FAILED and j.last_error_code==code
        assert runner.retry(j.job_id).retry_count==0

@pytest.mark.parametrize('limit',[1,3])
def test_Q08_TL08_deterministic_bound(tmp_path,limit):
    ready=threading.Event(); release=threading.Event(); overflow=threading.Event(); lock=threading.Lock(); started=[]
    def archive(*a,**kw):
        with lock:
            started.append(1)
            if len(started)==limit: ready.set()
            if len(started)>limit: overflow.set()
        assert release.wait(5); return result()
    items=[(replace(p,repo=f'Repo{i}'),r,a,root) for i,(p,r,a,root) in enumerate([item()]*5)]
    store,runner=prepare(tmp_path,items,archive=archive,settings=m.QueueSettings(max_active=limit)); thread,errors,_=_start(runner)
    try:
        assert ready.wait(5) and not overflow.wait(0.5)
        with lock: assert len(started)==limit
        assert sum(t.name.startswith('covearchive-queue-') for t in threading.enumerate())==limit
    finally: release.set(); thread.join(5); runner.shutdown()
    assert not thread.is_alive() and not errors and store.counts()[S.COMPLETED]==5

@pytest.mark.parametrize('source',['catalog','archive'])
def test_Q19_S3_global_limit_restart(tmp_path,source):
    clock=Clock(); args=item(); a=args[2]; r=replace(args[1],assets=tuple(replace(a,id=i) for i in (1,2,3))); fetch=[]; calls=[]
    store=QueueStore(tmp_path/'q.db',clock=clock); j=store.enqueue(*args).job
    done=store.enqueue(*item(3)).job
    with store._write() as c: c.execute("UPDATE jobs SET state='COMPLETED' WHERE job_id=?",(done.job_id,))
    def cat(repo):
        fetch.append(clock())
        if source=='catalog' and clock()==100: raise g.RateLimitError(kind='secondary',status=429,retry_after_seconds=120,retry_at=None)
        return NS(releases=(r,))
    def archive(*a,**kw): calls.append(clock()); return result('FAILED','rate_limited',retryable=True,retry_after_seconds=120) if clock()==100 else result()
    with QueueRunner(store,catalog=cat,archive=archive,clock=clock,settings=m.QueueSettings(max_attempts=1)) as runner:
        runner.run_pending(); assert store.get(j.job_id).state==S.FAILED and store.get(j.job_id).next_attempt_at==220
        runner.retry(j.job_id); store.enqueue(*item(2)); store.enqueue(replace(args[0],repo='Other'),r,replace(a,id=2),args[3]); store.enqueue(*item(3))
        clock.now=150; runner.run_pending(); assert fetch==[100] and runner.snapshot().cooldown_until==220
    with QueueRunner(QueueStore(store.path),catalog=cat,archive=archive,clock=clock) as fresh:
        clock.now=219; fresh.run_pending(); assert fetch==[100]
        clock.now=220; fresh.run_pending(); assert store.counts()[S.COMPLETED]==4 and fresh.snapshot().cooldown_until is None
    assert all(t==220 for t in calls if t!=100)

@pytest.mark.parametrize('stage',['catalog','archive'])
def test_Q19_S4_running_deferral(tmp_path,stage,monkeypatch):
    clock=Clock(); ready=threading.Event(); release=threading.Event(); saved=threading.Event(); requests=[]; args=item(); other=(replace(args[0],repo='Other'),*args[1:])
    store=QueueStore(tmp_path/'q.db',clock=clock); store.enqueue_many((args,other)); finish=store.finish
    def persist(*a,**kw): value=finish(*a,**kw); saved.set(); return value
    monkeypatch.setattr(store,'finish',persist)
    def cat(repo):
        requests.append(repo.repo)
        if repo.repo=='Repo':
            assert ready.wait(5); raise g.RateLimitError(kind='secondary',status=429,retry_after_seconds=120,retry_at=None)
        return NS(releases=(args[1],))
    runner=QueueRunner(store,catalog=cat,archive=lambda *a,**kw:result(),clock=clock,settings=m.QueueSettings(max_attempts=1)); lookup=runner._lookup
    def blocked(repo,job):
        if repo.repo=='Other' and stage=='catalog': ready.set(); assert release.wait(5)
        value=lookup(repo,job)
        if repo.repo=='Other' and stage=='archive': ready.set(); assert release.wait(5)
        return value
    runner._lookup=blocked; thread,errors,_=_start(runner)
    try: assert saved.wait(5)
    finally: release.set(); thread.join(5); runner.shutdown()
    b=store.list_jobs()[1]; assert not errors and (b.state,b.retry_count,b.next_attempt_at)==(S.RETRY_WAIT,0,220)
    assert requests.count('Other')==(stage=='archive')

@pytest.mark.parametrize('invalid',[None,float('nan'),-1,'bad'])
def test_Q19_S3_invalid_cooldown(tmp_path,invalid):
    clock=Clock(); store,runner=prepare(tmp_path,[item()],archive=lambda *a,**kw:result('FAILED','network',retryable=True,retry_after_seconds=invalid),clock=clock,rng=lambda:0)
    with runner: runner.run_pending()
    assert store.list_jobs()[0].next_attempt_at==105 and store.cooldown_until() is None

def test_Q19_S5_wait_no_spin(tmp_path):
    clock=Clock(); calls=[]; store=QueueStore(tmp_path/'q.db',clock=clock); j=store.enqueue(*item()).job; store.claim_next('a')
    store.finish(j.job_id,'a',S.RETRY_WAIT,next_attempt_at=150,cooldown_until=220)
    def sleep(n): assert 0<n<=0.2 and len(clock.sleeps)<1000; clock.sleep(n)
    with QueueRunner(store,catalog=lambda repo:NS(releases=(item()[1],)),archive=lambda *a,**kw:(calls.append(clock()) or result()),clock=clock,sleep=sleep) as runner: runner.run_pending(wait_for_retries=True)
    assert calls==[220] and clock.sleeps

@pytest.mark.parametrize('code,count,maximum,paused,exhausted',[('network',4,3,False,True),('network',4,3,True,True),('network',2,3,False,False),('manifest_commit_incomplete',2,5,False,True),(None,3,3,False,True)])
def test_Q18_H2_persisted_budget(tmp_path,code,count,maximum,paused,exhausted):
    store=QueueStore(tmp_path/'q.db'); args=item(); j=store.enqueue(*args).job; store.claim_next('old')
    store.finish(j.job_id,'old',S.RETRY_WAIT,code=code,retry_count=count)
    if paused: store.transition(j.job_id,S.PAUSED)
    calls=[]
    def cat(repo): calls.append('catalog'); return NS(releases=(args[1],))
    def archive(*a,**kw): calls.append('archive'); return result()
    with QueueRunner(store,catalog=cat,archive=archive,settings=m.QueueSettings(max_attempts=maximum)) as runner:
        if paused: runner.resume(j.job_id)
        runner.run_pending(); current=store.get(j.job_id)
        assert current.state==(S.FAILED if exhausted else S.COMPLETED) and current.retry_count==count
        assert current.last_error_code==((code or 'retry_exhausted') if exhausted else None)
        assert calls==([] if exhausted else ['catalog','archive']) and store.cooldown_until() is None
        if exhausted:
            assert runner.retry(j.job_id).retry_count==0
            runner.run_pending(); assert calls==['catalog','archive'] and store.get(j.job_id).state==S.COMPLETED
