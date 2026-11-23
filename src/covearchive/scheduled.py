"""One-shot anonymous preservation through the existing scan and archive queue."""
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

from covearchive.auto_archive import eligible_items
from covearchive.archive.naming import validate_root
from covearchive.queue import QueueStore, QueueRunner, QueueError, QueueBusyError, JobState as S
from covearchive.queue.store import default_queue_path
from covearchive.scan_worker import (ScanSuccess,ScanFailure,ERROR_CODES,MAX_RESPONSE_BYTES,
    encode_request,decode_response,ProtocolError)
from covearchive.scheduled_state import State,ScheduleLock,default_state_path,private_directory
from covearchive.ui.library_store import LibraryStore
from covearchive.ui.preferences import PreferenceStore

RUN_SECONDS=900
SCAN_SECONDS=120
GRACE_SECONDS=10
_ENV_KEYS=frozenset(('SYSTEMROOT SYSTEMDRIVE WINDIR TEMP TMP HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY '
    'http_proxy https_proxy all_proxy no_proxy REQUESTS_CA_BUNDLE SSL_CERT_FILE SSL_CERT_DIR '
    'LOCALAPPDATA XDG_CONFIG_HOME XDG_STATE_HOME HOME '
    '_PYI_ARCHIVE_FILE _PYI_APPLICATION_HOME_DIR _PYI_PARENT_PROCESS_LEVEL _PYI_SPLASH_IPC').split())


def command(launcher,mode):
    executable=os.path.abspath(sys.executable)
    if getattr(sys,'frozen',False): return [executable,mode]
    return [executable,'-I','-B',str(Path(launcher or Path(__file__).resolve().parents[2]/'run_covearchive.py').resolve()),mode]


def _stream(name):
    stream=getattr(sys,name)
    if stream is not None: return stream.buffer
    if sys.platform!='win32': return None
    # Windowed frozen executables clear sys.std*, even when a parent supplies pipes.
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetStdHandle.argtypes=[wintypes.DWORD]; kernel.GetStdHandle.restype=wintypes.HANDLE
    handle=kernel.GetStdHandle(-10 if name=='stdin' else -11)
    if handle in (None,0,ctypes.c_void_p(-1).value): return None
    flags=os.O_BINARY|(os.O_RDONLY if name=='stdin' else os.O_WRONLY)
    try: return os.fdopen(msvcrt.open_osfhandle(handle,flags),'rb' if name=='stdin' else 'wb',buffering=0)
    except OSError: return None


def scan_worker():
    from covearchive.scan_worker import main
    stdin,stdout=_stream('stdin'),_stream('stdout')
    if stdin is None or stdout is None: return 2
    return main(stdin,stdout)


@contextmanager
def signals(stop,callback=None):
    previous={}
    def interrupted(*_):
        stop.set()
        if callback is not None: callback()
    if threading.current_thread() is threading.main_thread():
        for name in ('SIGTERM','SIGINT','SIGBREAK'):
            value=getattr(signal,name,None)
            if value is not None: previous[value]=signal.signal(value,interrupted)
    try: yield
    finally:
        for value,handler in previous.items(): signal.signal(value,handler)


def _stop_child(child):
    if child.poll() is None:
        try:
            if sys.platform=='win32': child.send_signal(signal.CTRL_BREAK_EVENT)
            else: child.terminate()
        except OSError: pass
        try: child.wait(timeout=GRACE_SECONDS)
        except subprocess.TimeoutExpired: child.kill()
    child.wait(timeout=5)


def child_run(argv,request,*,stop,deadline,cap):
    """Bound output, runtime and cancellation; keep stdin open for the EOF watchdog."""
    if stop.is_set(): return None,'interrupted'
    if time.monotonic()>=deadline: return None,'timeout'
    env={k:v for k,v in os.environ.items() if k in _ENV_KEYS}
    options=({'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform=='win32' else {'start_new_session':True})
    child=None
    with tempfile.TemporaryFile() as output:
        try:
            child=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=output,stderr=subprocess.DEVNULL,env=env,**options)
            child.stdin.write(request); child.stdin.flush()
            while child.poll() is None:
                if stop.is_set(): return None,'interrupted'
                if time.monotonic()>=deadline: return None,'timeout'
                if os.fstat(output.fileno()).st_size>cap: return None,'protocol'
                stop.wait(0.05)
            if child.returncode!=0: return None,'queue_unavailable' if child.returncode==4 else 'process'
            output.seek(0); data=output.read(cap+1)
            return (data,None) if len(data)<=cap else (None,'protocol')
        except (OSError,ValueError): return None,'process'
        finally:
            if child is not None:
                _stop_child(child)
                try: child.stdin.close()
                except OSError: pass


def scan_repository(url,*,stop,deadline,launcher):
    request_id=uuid4().hex
    raw,error=child_run(command(launcher,'--scheduled-scan-worker'),encode_request(request_id,url),stop=stop,deadline=deadline,cap=MAX_RESPONSE_BYTES)
    if error: return ScanFailure(request_id,error)
    try: return decode_response(raw,request_id)
    except ProtocolError: return ScanFailure(request_id,'protocol')


def run_queue(store,*,stop,deadline,launcher):
    # Only the existing default queue is addressable by the production worker.
    raw,error=child_run(command(launcher,'--scheduled-queue-worker'),b'run\n',stop=stop,deadline=deadline,cap=1024)
    if error: return error
    try:
        result=json.loads(raw)
        if result not in ('succeeded','queue_unavailable','interrupted','needs_attention'): return 'protocol'
        return None if result=='succeeded' else result
    except (ValueError,TypeError): return 'protocol'


def _safe_project(job):
    """Audit without recovery; only absent/healthy projects or new scratch dirs pass."""
    from covearchive.archive.health import audit_project
    from covearchive.archive import naming,store
    from covearchive.archive.fscommon import TEMP_PATTERN
    from covearchive.github.urls import RepositoryTarget
    try:
        repository=RepositoryTarget(job.owner,job.repo,job.provider,job.host,job.project_id)
        report=audit_project(job.archive_root,repository)
        if report.truncated: return False
        if report.complete and report.status=='healthy': return True
        if tuple(f.code for f in report.findings)==('missing_project',): return True
        indexed={r.folder for r in report.indexed or ()}; new=set(); missing=set()
        for finding in report.findings:
            if finding.code=='verified': continue
            if (finding.code not in ('missing_release_manifest','incomplete_state')
                    or not finding.folder or finding.folder in indexed): return False
            new.add(finding.folder)
            if finding.code=='missing_release_manifest': missing.add(finding.folder)
        if not new or new!=missing: return False
        # An interrupted brand-new download may leave an unindexed directory
        # with only owned scratch temps. No preserved bytes or metadata may be
        # present. Never extend this exception to any historically indexed folder.
        remaining=report.limits.max_directory_entries
        with ExitStack() as stack:
            directory=stack.enter_context(store.ArchiveRoot.open(job.archive_root))
            for component in naming.project_components(repository):
                directory=stack.enter_context(directory.open_dir(component,fold_case=True,entry_limit=remaining,wait=False))
                remaining-=directory.lookup_entries
            directory.role='project'
            for folder in new:
                if remaining<=0: return False
                with directory.open_dir(folder,wait=False) as release:
                    release.role='release'; names=release.names(limit=remaining)
                    if len(names)>remaining: return False
                    remaining-=len(names)
                    for name in names:
                        if not TEMP_PATTERN.fullmatch(name): return False
                        # open_temp only inspects/locks an existing orphan here;
                        # no recovery, create, write, rename or delete is called.
                        with release.open_temp(name) as temporary:
                            if temporary.info().links!=1: return False
        return True
    except Exception: return False


class _ScheduledStore(QueueStore):
    needs_attention=False

    def claim_next(self,owner,*,now=None):
        if self.needs_attention: return None
        job=super().claim_next(owner,now=now)
        if job is not None and not _safe_project(job):
            # QueueRunner owns RunnerLock. Return this reservation to the GUI's
            # pending queue BEFORE a worker, metadata request or download starts.
            self.finish(job.job_id,owner,S.QUEUED,code='needs_attention')
            self.needs_attention=True
            return None
        return job


def _hard_exit(): os._exit(5)


def queue_worker():
    """Killable QueueRunner host. No catalogs, URLs or download destinations on stdin."""
    stop=threading.Event(); done=threading.Event(); runner=None
    stdin,stdout=_stream('stdin'),_stream('stdout')
    if stdin is None or stdout is None: return 4
    def shutdown():
        stop.set()
        if runner is not None: runner.shutdown(0)
    def leave():
        timer=threading.Timer(GRACE_SECONDS,lambda:None if done.is_set() else _hard_exit())
        timer.daemon=True; timer.start()
        try:
            shutdown()
            if not done.wait(GRACE_SECONDS): _hard_exit()
        finally: timer.cancel()
    def eof():
        try:
            while stdin.read(1): pass
        finally: leave()
    def watchdog():
        if not done.wait(RUN_SECONDS): leave()
    outcome='queue_unavailable'
    try:
        if stdin.readline(8)!=b'run\n': return 4
        with signals(stop,shutdown):
            threading.Thread(target=eof,daemon=True).start()
            threading.Thread(target=watchdog,daemon=True).start()
            if not stop.is_set():
                runner=QueueRunner(_ScheduledStore())
                if stop.is_set(): runner.shutdown(0)
                else: runner.run_pending(wait_for_retries=False)
                outcome='interrupted' if stop.is_set() else 'needs_attention' if runner.store.needs_attention else 'succeeded'
    except Exception: outcome='queue_unavailable'
    finally:
        if runner is not None:
            report=runner.shutdown(GRACE_SECONDS)
            if report.unresolved: os._exit(5)
        done.set()
    stdout.write((json.dumps(outcome)+'\n').encode()); stdout.flush()
    return 0


def _failure(result,now):
    code=result.code; detail=result.detail or {}; hint=None
    if code=='incomplete_scan' and detail.get('cause_code') in ('rate_limited','unauthorized'): code=detail['cause_code']
    if code=='unauthorized' and not result.token_rejected: code='forbidden'
    for key in ('retry_after_seconds','retry_at','reset_at'):
        value=detail.get(key)
        if type(value) in (int,float): hint=value if key=='retry_after_seconds' else value-now; break
    return code if code in ERROR_CODES|{'process','protocol','interrupted'} else 'internal_error',hint


def _emit(outcome,code):
    if sys.stdout is not None: print(json.dumps(dict(outcome=outcome,exit_code=code)),flush=True)
    return code


def run(launcher=None,*,scan=None,run_queue=None,clock=time.time,stop=None,deadline_seconds=RUN_SECONDS,scan_timeout=SCAN_SECONDS):
    """Injection is import-only for offline tests; production argv has no overrides."""
    stop=stop if stop is not None else threading.Event(); scan=scan or scan_repository
    queue_run=run_queue or globals()['run_queue']; state=None; lock=None
    deadline=time.monotonic()+deadline_seconds
    try:
        path=default_state_path(); private_directory(path.parent)
        try: lock=ScheduleLock(path)
        except QueueBusyError: return _emit('already_running',0)
        state=State.load(path)
        def finish(code,outcome,error=None):
            if error is None:
                unresolved=[r for r in state.data['repositories'].values() if r['last_code'] not in (None,'succeeded','checking')]
                if unresolved: error=max(unresolved,key=lambda r:r['last_attempt'] or 0)['last_code']
            if state.error: code=6; outcome=state.error
            state.data.update(last_finish=clock(),last_outcome=outcome,last_error=state.error or error)
            state.save()
            return _emit(outcome,code)
        state.data.update(last_start=clock(),last_finish=None,last_outcome='running')
        state.save()
        if stop.is_set(): return finish(5,'interrupted','interrupted')
        library,problem,_=LibraryStore().load()
        if problem: return finish(2,'invalid_library','invalid_library')
        settings,problem,_=PreferenceStore().load()
        if problem: return finish(2,'invalid_preferences','invalid_preferences')
        wanted={r.ref:r.auto_archive for r in library.repositories if r.active and r.auto_archive is not None}
        state.sync(wanted,clock())
        if not wanted: return finish(0,'not_opted_in')
        due=[r for r in library.repositories if r.ref in wanted and state.data['repositories'][r.ref]['next_due']<=clock()]
        if not due or state.data['blocked_until']>clock(): return finish(0,'nothing_due')
        pending={}; store=None; exit_code=0; last_error=None; rate_limited=False
        def failed(key,code,exit_value,hint=None):
            nonlocal exit_code,last_error
            state.failed(key,code,clock(),hint); exit_code=max(exit_code,exit_value); last_error=code; state.save()
        for repo in due:
            key=repo.ref
            if stop.is_set() or time.monotonic()>=deadline: break
            if state.data['blocked_until']>clock(): break
            entry=state.data['repositories'][key]; entry.update(last_attempt=clock(),last_code='checking')
            state.save(); rule=settings['repositories'].get(key)
            if rule is None: failed(key,'needs_rules',2); continue
            root=settings.get('archive_root')
            try:
                validate_root(root)
                if not Path(root).is_dir() or not os.access(root,os.W_OK): raise ValueError('needs_root')
            except (ValueError,TypeError,OSError): failed(key,'needs_root',2); continue
            try: result=scan(repo.url,stop=stop,deadline=min(deadline,time.monotonic()+scan_timeout),launcher=launcher)
            except Exception: result=ScanFailure('', 'internal_error')
            if isinstance(result,ScanFailure):
                code,hint=_failure(result,clock())
                if code=='rate_limited': rate_limited=True
                failed(key,code,5 if code=='interrupted' else 3,hint); continue
            if not isinstance(result,ScanSuccess) or result.catalog.repository.repo_key!=key or not result.catalog.complete:
                failed(key,'protocol',3); continue
            items=eligible_items(result.catalog,rule)
            if stop.is_set() or time.monotonic()>=deadline: break
            if not items:
                pending[key]=(); continue
            try:
                if store is None: store=QueueStore()
                rows=store.enqueue_many(((result.catalog.repository,r,a,root) for r,a in items),requeue_completed=False)
                pending[key]=tuple(row.job.job_id for row in rows)
            except QueueError as exc:
                code=exc.code if exc.code in ('database_busy','database_unavailable') else 'queue_unavailable'
                failed(key,code,4)
        interrupted=stop.is_set(); timed_out=time.monotonic()>=deadline
        if pending and not interrupted and not timed_out:
            # The store safely accepts jobs while another process owns RunnerLock.
            # Only the worker attempts ownership and recovery; lock-busy never succeeds.
            try:
                error='rate_limited' if rate_limited else None
                if error is None and store is not None: error=queue_run(store,stop=stop,deadline=deadline,launcher=launcher)
            except Exception: error='queue_unavailable'
            interrupted=stop.is_set() or error=='interrupted'; timed_out=time.monotonic()>=deadline or error=='timeout'
            for key,ids in pending.items():
                if interrupted or timed_out: break
                if error:
                    code=error if error in ('needs_attention','rate_limited') else 'queue_unavailable'
                    failed(key,code,4,state.data['blocked_until']-clock() if code=='rate_limited' else None); continue
                rows=[store.get(job_id) for job_id in ids]
                if all(row.state==S.COMPLETED for row in rows): state.succeeded(key,clock()); state.save()
                else:
                    code=('user_stopped' if any(row.state in (S.PAUSED,S.CANCELLED) for row in rows) else
                        'disk_full' if any(row.last_error_code=='disk_full' for row in rows) else
                        'rate_limited' if any(row.last_error_code=='rate_limited' for row in rows) else 'download_failed')
                    until=store.cooldown_until()
                    failed(key,code,4,until-clock() if until is not None else None)
            if error=='needs_attention':
                for job in store.list_jobs((S.QUEUED,)):
                    if job.provider=='github' and job.last_error_code=='needs_attention' and job.repo_key in wanted and job.repo_key not in pending:
                        failed(job.repo_key,'needs_attention',4)
        if interrupted or timed_out:
            code='interrupted' if interrupted else 'timeout'
            for repo in due:
                entry=state.data['repositories'][repo.ref]
                if repo.ref in pending or entry['last_code']=='checking': state.failed(repo.ref,code,clock())
            return finish(5 if interrupted else 7,code,code)
        return finish(exit_code,'succeeded' if exit_code==0 else 'failed',last_error)
    except (OSError,ValueError,TypeError,QueueError,RecursionError,OverflowError):
        return _emit('state_unavailable',6)
    finally:
        if lock is not None: lock.close()


def main(launcher=None):
    stop=threading.Event()
    with signals(stop): return run(launcher,stop=stop)
