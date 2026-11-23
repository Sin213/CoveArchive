"""Real headless processes + real QueueRunner/archive writes, with fake GitHub I/O."""
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace as NS
import pytest

from test_scheduled import rig,ROOT,state,jobs
from covearchive.queue import JobState as S

BOOT="import sys;sys.path[:0]=sys.argv[1:3];from scheduled_fixtures import parent;raise SystemExit(parent(*sys.argv[3:]))"


def spawn(behavior='fast',ids='1',now=10000,deadline=30):
    options={'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform=='win32' else {}
    return subprocess.Popen([sys.executable,'-I','-B','-c',BOOT,str(ROOT/'src'),str(ROOT/'tests'),
        behavior,ids,str(now),str(deadline)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,**options)


def finish(child,expected):
    try:
        out,err=child.communicate(timeout=15)
        assert child.returncode==expected,(out,err)
        return out
    finally:
        if child.poll() is None: child.kill()
        child.wait(timeout=5)


def events():
    path=Path(os.environ['XDG_STATE_HOME'])/'events.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def until(predicate):
    deadline=time.monotonic()+8
    while not predicate():
        assert time.monotonic()<deadline,'fixture did not become ready'
        time.sleep(0.02)


def dead(pid):
    if sys.platform=='win32':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]; kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]; kernel.WaitForSingleObject.restype=wintypes.DWORD
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x100000,False,pid)
        if not handle: return ctypes.get_last_error()==87
        try: return kernel.WaitForSingleObject(handle,0)==0
        finally: kernel.CloseHandle(handle)
    try: os.kill(pid,0)
    except ProcessLookupError: return True
    status=Path('/proc')/str(pid)/'stat'
    return status.exists() and status.read_text().split(') ',1)[1].startswith('Z')


def no_workers():
    until(lambda:all(dead(e['pid']) for e in events() if e['kind']=='worker'))


def hashes(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


def test_real_headless_incremental_and_zero_historical_deletions(rig):
    historical=rig.root/'historical.zip'; historical.write_bytes(b'old preservation')
    inputs={p:p.read_bytes() for p in rig.config.iterdir()}
    finish(spawn(),0); first=hashes(rig.root)
    assert (rig.root/'Owner'/'Repo'/'release-v1--1'/'app.zip').is_file()
    assert len([e for e in events() if e['kind']=='download'])==1
    finish(spawn(now=13600),0)
    assert hashes(rig.root)==first and len([e for e in events() if e['kind']=='download'])==1
    finish(spawn(ids='1,2',now=17200),0); after=hashes(rig.root)
    assert first.keys()<=after.keys()
    assert all(after[k]==value for k,value in first.items() if not k.endswith('archive-index.json'))
    assert len([e for e in events() if e['kind']=='download'])==2
    assert all(p.read_bytes()==raw for p,raw in inputs.items())
    assert len(jobs())==2 and all(j.state==S.COMPLETED for j in jobs())
    no_workers()


def test_simultaneous_launch_and_sigterm_are_clean(rig):
    child=spawn('slow')
    try:
        until(lambda:any(e['kind']=='download' for e in events()))
        from covearchive.scheduled_state import default_state_path
        before=default_state_path().read_bytes()
        assert b'already_running' in finish(spawn(now=10001),0)
        assert default_state_path().read_bytes()==before
        child.send_signal(signal.CTRL_BREAK_EVENT if sys.platform=='win32' else signal.SIGTERM)
        finish(child,5)
        assert state()['last_outcome']=='interrupted' and state()['repositories']['owner/repo']['last_success'] is None
        assert jobs()[0].state==S.QUEUED
        no_workers()
        finish(spawn(now=13600),0)
        assert len(jobs())==1 and jobs()[0].state==S.COMPLETED
        no_workers()
    finally:
        if child.poll() is None: child.kill()
        child.wait(timeout=5)


def test_parent_crash_eof_and_restart(rig):
    child=spawn('slow')
    try:
        until(lambda:any(e['kind']=='download' for e in events()))
        child.kill(); child.communicate(timeout=5)
        no_workers()
        assert state()['repositories']['owner/repo']['last_success'] is None
        finish(spawn(),0)
        assert len(jobs())==1 and jobs()[0].state==S.COMPLETED
        no_workers()
    finally:
        if child.poll() is None: child.kill()
        child.wait(timeout=5)


def test_hung_download_is_bounded_and_recoverable(rig):
    finish(spawn('hung',deadline=1),7)
    assert state()['last_outcome']=='timeout' and state()['repositories']['owner/repo']['last_success'] is None
    no_workers()
    finish(spawn(now=13600),0)
    assert len(jobs())==1 and jobs()[0].state==S.COMPLETED
    no_workers()


def test_hung_discovery_is_bounded(rig):
    finish(spawn('scan_hang'),3)
    assert state()['repositories']['owner/repo']['last_code']=='timeout'
    assert not (Path(os.environ['LOCALAPPDATA'])/'CoveArchive'/'queue.sqlite3' if sys.platform=='win32' else Path(os.environ['XDG_STATE_HOME'])/'covearchive'/'queue.sqlite3').exists()
    no_workers()


@pytest.mark.parametrize('preexisting',[False,True])
def test_damaged_historical_manifest_is_never_repaired(rig,preexisting):
    from test_archive_health import snapshot
    finish(spawn(),0)
    folder=rig.root/'Owner'/'Repo'/'release-v1--1'
    (folder/'release.json').unlink()
    if preexisting:
        from scheduled_fixtures import fake_catalog
        from covearchive.queue import QueueStore
        cat=fake_catalog('1',extra=True); release=cat.releases[0]
        QueueStore().enqueue(cat.repository,release,release.assets[-1],str(rig.root))
    before=hashes(rig.root); before_stat=snapshot(rig.root)
    downloads=sum(e['kind']=='download' for e in events())
    finish(spawn('fast' if preexisting else 'new_asset',now=13600),4)
    assert hashes(rig.root)==before and not (folder/'release.json').exists()
    assert snapshot(rig.root)==before_stat
    assert sum(e['kind']=='download' for e in events())==downloads
    pending=next(job for job in jobs() if job.asset_id==2)
    assert pending.state==S.QUEUED and pending.retry_count==0
    repo=state()['repositories']['owner/repo']
    assert repo['last_code']=='needs_attention' and repo['last_success']==10000
    no_workers()


@pytest.mark.parametrize('damage',['index','manifest_corrupt','modified','missing_asset','empty_indexed_release'])
def test_damaged_project_defers_new_releases_without_repair(rig,damage):
    from test_archive_health import snapshot
    finish(spawn(),0)
    project=rig.root/'Owner'/'Repo'; folder=project/'release-v1--1'
    if damage=='index': (project/'archive-index.json').unlink()
    elif damage=='manifest_corrupt': (folder/'release.json').write_bytes(b'bad metadata')
    elif damage=='modified': (folder/'app.zip').write_bytes(b'changed preserved bytes')
    else:
        (folder/'app.zip').unlink()
        if damage=='empty_indexed_release': (folder/'release.json').unlink()
    before=snapshot(rig.root); count=sum(e['kind']=='download' for e in events())
    finish(spawn(ids='1,2',now=13600),4)
    assert snapshot(rig.root)==before
    assert sum(e['kind']=='download' for e in events())==count
    pending=next(job for job in jobs() if job.asset_id==2)
    assert pending.state==S.QUEUED and pending.retry_count==0
    repo=state()['repositories']['owner/repo']
    assert repo['last_code']=='needs_attention' and repo['last_success']==10000
    no_workers()


def test_explicit_gui_downloader_still_restores_manifest(rig,monkeypatch):
    from contextlib import contextmanager
    from covearchive.archive import download
    from scheduled_fixtures import fake_catalog,DATA
    finish(spawn(),0)
    folder=rig.root/'Owner'/'Repo'/'release-v1--1'; (folder/'release.json').unlink()
    old=(folder/'app.zip').read_bytes(); cat=fake_catalog('1',extra=True); release=cat.releases[0]
    @contextmanager
    def stream(*args,**kw): yield iter((DATA,))
    monkeypatch.setattr(download,'open_asset_stream',stream)
    result=download.archive_asset(cat.repository,release,release.assets[-1],str(rig.root))
    assert result.status.value=='ARCHIVED' and (folder/'release.json').is_file()
    assert (folder/'app.zip').read_bytes()==old and (folder/'second.zip').read_bytes()==DATA
    no_workers()


@pytest.mark.parametrize('trigger',['eof','watchdog'])
@pytest.mark.parametrize('complete',[False,True])
def test_queue_worker_hard_exit_is_independent_of_shutdown(monkeypatch,trigger,complete):
    from covearchive import scheduled as s
    entered=threading.Event(); shutting=threading.Event(); release=threading.Event()
    eof_release=threading.Event(); exited=threading.Event(); lock=threading.Lock(); threads=[]; results=[]
    class Runner:
        def __init__(self,store): self.store=store
        def run_pending(self,**kw):
            with lock:
                entered.set(); release.wait()
        def shutdown(self,timeout):
            shutting.set()
            with lock: return NS(unresolved=False)
    def read(size):
        (entered if trigger=='eof' else eof_release).wait()
        return b''
    def thread(*args,**kw):
        value=threading.Thread(*args,**kw); threads.append(value); return value
    def timer(*args,**kw):
        value=threading.Timer(*args,**kw); threads.append(value); return value
    stdin=NS(readline=lambda size:b'run\n',read=read); stdout=io.BytesIO()
    monkeypatch.setattr(s,'_stream',lambda name:stdin if name=='stdin' else stdout)
    monkeypatch.setattr(s,'QueueRunner',Runner)
    monkeypatch.setattr(s,'_ScheduledStore',lambda:NS(needs_attention=False))
    monkeypatch.setattr(s,'_hard_exit',exited.set)
    monkeypatch.setattr(s,'GRACE_SECONDS',0.1); monkeypatch.setattr(s,'RUN_SECONDS',0.1)
    monkeypatch.setattr(s,'threading',NS(Thread=thread,Timer=timer,Event=threading.Event,
        current_thread=threading.current_thread,main_thread=threading.main_thread))
    worker=threading.Thread(target=lambda:results.append(s.queue_worker()),daemon=True)
    try:
        worker.start()
        assert entered.wait(1) and shutting.wait(1)
        if complete:
            release.set(); worker.join(1)
            assert results==[0] and not exited.wait(0.3)
        else:
            assert exited.wait(1) and worker.is_alive()
    finally:
        release.set(); entered.set(); eof_release.set(); worker.join(2)
        for value in threads: value.join(2)
    assert not worker.is_alive() and all(not value.is_alive() for value in threads)
