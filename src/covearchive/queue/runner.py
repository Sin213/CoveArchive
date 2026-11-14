"""Bounded calling-thread scheduler over the archive pipeline."""
from dataclasses import dataclass
import random
import math
import threading
import time
import uuid
from covearchive.github.urls import RepositoryTarget
from .models import JobState as S, QueueError, InvalidTransitionError, Outcome, classify_result, backoff_seconds, safe_code, LIMITED_RETRY
from .store import RunnerLock

@dataclass(frozen=True)
class RunSummary:
    completed: int=0
    failed: int=0
    cancelled: int=0
    retry_wait: int=0
    paused: int=0
    requeued: int=0

@dataclass(frozen=True)
class JobProgress:
    job_id: str
    bytes_done: int | None
    bytes_total: int | None
    speed_bps: float | None

@dataclass(frozen=True)
class QueueSnapshot:
    counts: dict
    active: tuple[JobProgress,...]
    bytes_done: int
    bytes_total: int
    cooldown_until: float | None=None

class _Deferred(Exception):
    def __init__(self,until): self.until=until

@dataclass(frozen=True)
class ShutdownReport:
    unresolved: tuple[str,...]

@dataclass
class _Control:
    job: object
    thread: object=None
    done: bool=False
    reason: str | None=None
    bytes_done: int | None=None
    bytes_total: int | None=None
    speed_bps: float | None=None
    window_at: float | None=None
    window_bytes: int=0

def _catalog(repository):
    from covearchive.github.client import GitHubReleaseClient
    with GitHubReleaseClient() as client: return client.discover_releases(repository)

def _catalog_error(exc):
    from covearchive.github import client as g
    if isinstance(exc,g.IncompleteScanError):
        return _catalog_error(exc.cause) if exc.cause is not None else Outcome('failed','incomplete_scan')
    if isinstance(exc,g.RateLimitError): return Outcome('retry','rate_limited',exc.retry_after_seconds)
    if isinstance(exc,g.TransientServerError): return Outcome('retry' if exc.status in (500,502,503,504) else 'failed','server_error')
    if isinstance(exc,(g.RequestTimeoutError,g.NetworkError)): return Outcome('retry',safe_code(exc.code))
    if isinstance(exc,g.NotFoundError): return Outcome('failed','upstream_removed')
    return Outcome('failed',safe_code(exc.code) if isinstance(exc,g.GitHubApiError) else 'internal_error')

class QueueRunner:
    def __init__(self,store,*,settings=None,archive=None,catalog=None,policy=None,clock=time.time,
            monotonic=time.monotonic,sleep=time.sleep,rng=random.random):
        self.store=store; self.settings=settings or store.settings
        self.archive=archive; self.catalog=catalog or _catalog; self.policy=policy
        self.clock=clock; self.monotonic=monotonic; self.sleep=sleep; self.rng=rng
        self._owner=uuid.uuid4().hex; self._state=threading.RLock(); self._wake=threading.Event()
        self._active={}; self._cache={}; self._catalog_locks={}; self._lock=None
        self._running=False; self._stopping=False; self._closed=False; self._error=None
        self._serial=0; self._totals={s:0 for s in S}

    def _metadata(self,repository,refresh_before=None):
        with self._state: lock=self._catalog_locks.setdefault(repository.repo_key,threading.Lock())
        with lock:
            self._guard_cooldown()
            value=self._cache.get(repository.repo_key); now=self.clock()
            expired=value is not None and value[1] is not None and value[1].kind=='retry' and now>=value[3]+(value[1].retry_after_seconds or self.settings.retry_base_seconds)
            stale=value is not None and refresh_before is not None and value[2]<refresh_before
            if value is None or expired or stale:
                try: value=(self.catalog(repository),None)
                except Exception as exc: value=(None,_catalog_error(exc))
                self._cache[repository.repo_key]=(*value,now,self.clock())
            return self._cache[repository.repo_key][:3]

    def _lookup(self,repository,job):
        catalog,outcome,fetched_at=self._metadata(repository)
        for attempt in range(2):
            if outcome is not None: return None,None,outcome
            release=next((r for r in catalog.releases if r.id==job.release_id),None)
            asset=next((a for a in release.assets if a.id==job.asset_id),None) if release else None
            if release is not None and asset is not None: return release,asset,None
            if attempt==0 and fetched_at<job.created_at:
                catalog,outcome,fetched_at=self._metadata(repository,refresh_before=job.created_at)
            else: return release,asset,Outcome('failed','release_removed' if release is None else 'asset_unavailable')

    def _cancelled(self,control):
        with self._state: return self._stopping or control.reason is not None

    def _guard_cooldown(self):
        until=self.store.cooldown_until()
        if until is not None and until>self.clock(): raise _Deferred(until)

    def _progress(self,control,received,expected):
        with self._state:
            now=self.monotonic(); control.bytes_done=received; control.bytes_total=expected
            if control.window_at is None: control.window_at=now; control.window_bytes=received
            elif now-control.window_at>=1:
                control.speed_bps=(received-control.window_bytes)/(now-control.window_at)
                control.window_at=now; control.window_bytes=received

    def _worker(self,control):
        job=control.job; result=None; outcome=Outcome('cancelled','cancelled'); until=None; deferred=False
        try:
            if not self._cancelled(control):
                limit=min(self.settings.max_attempts,LIMITED_RETRY.get(job.last_error_code,self.settings.max_attempts))
                if job.retry_count>=limit: outcome=Outcome('failed',job.last_error_code or 'retry_exhausted')
                else:
                    repository=RepositoryTarget(job.owner,job.repo)
                    release,asset,outcome=self._lookup(repository,job)
                    if outcome is None:
                        if not self._cancelled(control):
                            archive=self.archive
                            if archive is None:
                                from covearchive.archive.download import archive_asset
                                archive=archive_asset
                            self._guard_cooldown()
                            result=archive(repository,release,asset,job.archive_root,policy=self.policy,session=None,
                                cancel=lambda:self._cancelled(control),sleep=self.sleep,clock=self.monotonic,
                                progress=lambda received,expected:self._progress(control,received,expected))
                            outcome=classify_result(result)
                        else: outcome=Outcome('cancelled','cancelled')
        except _Deferred as exc: until=exc.until; deferred=True; outcome=Outcome('retry','rate_limited')
        except Exception: outcome=Outcome('failed','internal_error')
        try:
            with self._state:
                reason=control.reason or ('shutdown' if self._stopping else None)
                raw=outcome.retry_after_seconds
                if outcome.kind=='retry' and type(raw) in (int,float) and math.isfinite(raw) and raw>=0: until=self.clock()+raw
                count=job.retry_count; next_at=None; code=outcome.code
                if outcome.kind=='completed': to=S.COMPLETED; code=None
                elif reason is not None:
                    to={'pause':S.PAUSED,'cancel':S.CANCELLED,'shutdown':S.QUEUED}[reason]
                    if outcome.kind=='cancelled': code=None
                    next_at=until
                elif outcome.kind=='retry':
                    if deferred: to=S.RETRY_WAIT
                    else:
                        count+=1; limit=min(self.settings.max_attempts,LIMITED_RETRY.get(code,self.settings.max_attempts))
                        to=S.FAILED if count>=limit else S.RETRY_WAIT
                    if until is not None: next_at=until
                    elif to==S.RETRY_WAIT: next_at=self.clock()+backoff_seconds(count,self.settings,self.rng)
                else:
                    to=S.FAILED
                    if outcome.kind=='cancelled': code='unexpected_cancel'
                self.store.finish(job.job_id,self._owner,to,code=code,next_attempt_at=next_at,retry_count=count,result=result,cooldown_until=until)
                self._totals[to]+=1
        except Exception as exc:
            with self._state:
                if self._error is None: self._error=exc
                self._stopping=True
        finally:
            with self._state:
                control.done=True
                if self._closed and not self._running: self._reap(); self._release_lock()
                self._wake.set()

    def _reap(self):
        for job_id,control in tuple(self._active.items()):
            if control.done or not control.thread.is_alive():
                if control.thread is not threading.current_thread(): control.thread.join(0.01)
                del self._active[job_id]

    def run_pending(self,*,wait_for_retries=False):
        with self._state:
            if self._running: raise QueueError('already_running')
            if self._closed or self._stopping: raise QueueError('shut_down')
            self._running=True
        try:
            with self._state:
                if self._lock is None: self._lock=RunnerLock(self.store.path)
                self._reap(); self.store.recover_interrupted(keep=tuple(self._active),max_attempts=self.settings.max_attempts)
                self._cache.clear(); self._catalog_locks.clear(); self._totals=dict.fromkeys(S,0)
            while True:
                delay=None
                with self._state:
                    self._wake.clear(); self._reap()
                    while not self._stopping and len(self._active)<self.settings.max_active:
                        job=self.store.claim_next(self._owner,now=self.clock())
                        if job is None: break
                        self._serial+=1; control=_Control(job)
                        thread=threading.Thread(target=self._worker,args=(control,),daemon=True,name=f'covearchive-queue-{self._serial}')
                        control.thread=thread; self._active[job.job_id]=control; thread.start()
                    if not self._active:
                        if self._error is not None: raise self._error
                        if wait_for_retries and not self._stopping:
                            now=self.clock(); cooldown=self.store.cooldown_until() or 0
                            times=[max(cooldown,j.next_attempt_at or now) for j in self.store.list_jobs((S.QUEUED,S.RETRY_WAIT)) if cooldown>now or j.next_attempt_at is not None]
                            if times: delay=max(0,min(times)-self.clock())
                        if delay is None: break
                if delay is not None: self.sleep(min(max(delay,0.01),0.2))
                else: self._wake.wait(0.2)
            return RunSummary(*(self._totals[s] for s in (S.COMPLETED,S.FAILED,S.CANCELLED,S.RETRY_WAIT,S.PAUSED,S.QUEUED)))
        finally:
            with self._state:
                self._reap(); self._running=False
                self._release_lock()

    def _change(self,job_id,action):
        with self._state:
            job=self.store.get(job_id)
            if job.state==S.RUNNING and action in ('pause','cancel'):
                control=self._active.get(job_id)
                if control is None or job.claimed_by!=self._owner: raise InvalidTransitionError('ownership')
                job=self.store.request_stop(job_id,self._owner,action)
                self._request_stop(control,action); self._wake.set(); return job
            allowed={'pause':((S.QUEUED,S.RETRY_WAIT),S.PAUSED),'resume':((S.PAUSED,),S.QUEUED),
                'cancel':((S.QUEUED,S.RETRY_WAIT,S.PAUSED),S.CANCELLED),'retry':((S.FAILED,S.CANCELLED),S.QUEUED)}
            states,to=allowed[action]
            if job.state not in states: raise InvalidTransitionError('transition')
            changed=self.store.transition(job_id,to,expected=job.state); self._wake.set(); return changed

    def pause(self,job_id): return self._change(job_id,'pause')
    def resume(self,job_id): return self._change(job_id,'resume')
    def cancel(self,job_id): return self._change(job_id,'cancel')
    def retry(self,job_id): return self._change(job_id,'retry')

    def snapshot(self):
        with self._state:
            jobs=self.store.list_jobs(); counts=dict.fromkeys(S,0); active=[]; done=total=0
            for job in jobs:
                counts[job.state]+=1
                if job.state in (S.FAILED,S.CANCELLED): continue
                total+=job.asset_size
                if job.state==S.COMPLETED: done+=job.asset_size
                elif job.state==S.RUNNING and job.job_id in self._active:
                    control=self._active[job.job_id]; done+=control.bytes_done or 0
                    active.append(JobProgress(job.job_id,control.bytes_done,control.bytes_total,control.speed_bps))
            cooldown=self.store.cooldown_until()
            return QueueSnapshot(counts,tuple(active),done,total,cooldown if cooldown is not None and cooldown>self.clock() else None)

    def _release_lock(self):
        if self._closed and not self._running and not self._active and self._lock is not None:
            self._lock.close(); self._lock=None

    def _request_stop(self,control,reason):
        rank={None:0,'shutdown':1,'pause':2,'cancel':3}
        if rank[reason]>rank[control.reason]: control.reason=reason

    def shutdown(self,timeout=None):
        timeout=self.settings.shutdown_timeout_seconds if timeout is None else timeout
        deadline=time.monotonic()+max(0,timeout)
        with self._state:
            self._closed=True; self._stopping=True
            for control in self._active.values(): self._request_stop(control,'shutdown')
            threads=tuple(control.thread for control in self._active.values()); self._wake.set()
        for thread in threads:
            if thread is not threading.current_thread(): thread.join(max(0,deadline-time.monotonic()))
        with self._state:
            self._reap(); unresolved=tuple(self._active); self._release_lock()
            return ShutdownReport(unresolved)

    def __enter__(self): return self
    def __exit__(self,*args): self.shutdown()

__all__=('QueueRunner','RunSummary','JobProgress','QueueSnapshot','ShutdownReport')
