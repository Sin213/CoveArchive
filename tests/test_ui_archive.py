"""Command bridge contracts with real SQLite and a test-owned runner thread."""
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace as NS
import sqlite3
import inspect
import threading
import time
import pytest
from platform_paths import native_root
from PySide6.QtCore import QCoreApplication, QObject, Signal, Slot, Qt, QTimer, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from covearchive.queue import JobState as S, QueueStore, QueueBusyError, QueueError, QueueRunner, RunnerLock, QueueSettings
from covearchive.archive.download import ArchiveResult, ArchiveStatus
from covearchive.ui.archive_bridge import ArchiveBridge, EnqueueRequest, EnqueueItem
from test_queue_store import item


def until(predicate):
    deadline=time.monotonic()+3
    while not predicate() and time.monotonic()<deadline:
        QCoreApplication.processEvents(); time.sleep(0.005)
    assert predicate()


class Sink(QObject):
    def __init__(self): super().__init__(); self.views=[]; self.enqueues=[]; self.controls=[]; self.runs=[]; self.shutdowns=[]
    @Slot(object)
    def view(self, value):
        assert threading.current_thread() is threading.main_thread()
        assert all(j.state==S.RUNNING and j.owned for j in value.jobs if j.stop_pending)
        self.views.append(value)
    @Slot(object)
    def enqueue(self, value):
        assert threading.current_thread() is threading.main_thread()
        self.enqueues.append(value)
    @Slot(object)
    def control(self, value):
        assert threading.current_thread() is threading.main_thread()
        self.controls.append(value)
    @Slot(object)
    def shutdown(self, value): self.shutdowns.append(value)
    @Slot(object)
    def run(self, value):
        assert threading.current_thread() is threading.main_thread()
        self.runs.append(value)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA",str(tmp_path/"appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME",str(tmp_path/"config"))
    monkeypatch.setenv("XDG_STATE_HOME",str(tmp_path/"state"))


@pytest.fixture
def rig(tmp_path):
    app=QApplication.instance() or QApplication([]); bridges=[]
    def make(run=False, **options):
        bridge=ArchiveBridge(tmp_path/"q.db", options, refresh_ms=50, idle_refresh_ms=10000); sink=Sink()
        if not run: bridge._start_run=lambda:False
        bridge.view_changed.connect(sink.view, Qt.QueuedConnection)
        bridge.enqueued.connect(sink.enqueue, Qt.QueuedConnection)
        bridge.control_finished.connect(sink.control, Qt.QueuedConnection)
        bridge.run_event.connect(sink.run, Qt.QueuedConnection)
        bridge.shutdown_status.connect(sink.shutdown, Qt.QueuedConnection)
        bridges.append((bridge,sink)); bridge.open(); until(lambda: sink.views)
        return bridge,sink
    yield make
    for bridge,sink in bridges:
        if bridge.closed: continue
        if hasattr(bridge,"timer"): bridge.timer.stop()
        if bridge._runner is not None: bridge._runner.shutdown(3)
        if getattr(bridge,"_run_thread",None): bridge._run_thread.join(3)
        bridge._commands.put(None); bridge._thread.join(3)
        assert not bridge._thread.is_alive()
    app.processEvents()


def request(token=1, root=native_root("C:/Archive")):
    repo,r,a,_=item()
    return EnqueueRequest(token,repo,root,(EnqueueItem(r,a),))


def submit(bridge,sink,req=None):
    previous=len(sink.enqueues); assert bridge.enqueue(req or request())
    until(lambda:len(sink.enqueues)>previous)
    return sink.enqueues[-1]


def test_snapshots_enqueue_identity_root_and_no_start(rig):
    bridge,sink=rig(catalog=lambda r:pytest.fail("opening/enqueue must not discover"))
    assert bridge.is_open and not bridge.run_active and not bridge.closed
    original=request(); items=list(original.items)
    req=EnqueueRequest(1,original.repository,original.archive_root,items); items.clear()
    result=submit(bridge,sink,req)
    assert result.created==1 and not result.started and result.error is None
    second=submit(bridge,sink,request(2)); assert second.created==0 and len(second.existing)==1
    job=bridge._store.list_jobs()[0]
    assert (job.release_id,job.asset_id,job.archive_root,job.repo_key)==(10,1,native_root("C:/Archive"),"owner/repo")
    until(lambda:sink.views[-1].jobs)
    view=sink.views[-1]; assert view.jobs[0].state==S.QUEUED and not view.jobs[0].owned
    assert "https://" not in repr(view) and "sig=" not in repr(result)
    for obj,field in [(req,"token"),(req.items[0],"asset"),(result,"created"),(view,"seq"),(view.jobs[0],"state")]:
        with pytest.raises(FrozenInstanceError): setattr(obj,field,None)
    assert isinstance(view.counts,tuple) and isinstance(view.jobs,tuple)


def test_duplicate_inflight_and_refresh_coalescing(rig,monkeypatch):
    bridge,sink=rig(); entered=threading.Event(); release=threading.Event(); real=bridge._store.enqueue_many
    def blocked(items,**kw): entered.set(); assert release.wait(3); return real(items,**kw)
    monkeypatch.setattr(bridge._store,"enqueue_many",blocked)
    try:
        assert bridge.enqueue(request()); assert entered.wait(3)
        assert not bridge.enqueue(request())
        before=len(sink.views)
        for _ in range(20): bridge.refresh()
    finally: release.set()
    until(lambda:sink.enqueues and len(sink.views)>before)
    assert len(bridge._store.list_jobs())==1 and len(sink.enqueues)==1
    assert len(sink.views)<=before+2


@pytest.mark.parametrize("action,final",[("pause",S.PAUSED),("cancel",S.CANCELLED),("race",S.COMPLETED)])
def test_running_stop_is_durable_and_real_completion_wins(rig,monkeypatch,action,final):
    entered=threading.Event(); release=threading.Event(); repo,r,a,_=item()
    def archive(*args,**kw):
        kw["progress"](42,42); entered.set(); assert release.wait(3)
        return ArchiveResult(ArchiveStatus.ARCHIVED if action=="race" else ArchiveStatus.CANCELLED,None,a.id,r.id)
    bridge,sink=rig(archive=archive,catalog=lambda repo:NS(releases=(r,)))
    job_id=submit(bridge,sink).job_ids[0]; observed=[]; original=bridge._runner._request_stop
    def stop(control,reason):
        if reason in ("pause","cancel"):
            with sqlite3.connect(bridge._store.path) as db:
                observed.append(db.execute("select stop_requested from jobs where job_id=?",(job_id,)).fetchone()[0])
            assert observed[-1]==reason
        return original(control,reason)
    monkeypatch.setattr(bridge._runner,"_request_stop",stop)
    thread=threading.Thread(target=bridge._runner.run_pending); thread.start()
    try:
        assert entered.wait(3); command="pause" if action=="pause" else "cancel"
        bridge.control(5,command,(job_id,)); until(lambda:sink.controls)
        result=sink.controls[-1]; assert result.ok and result.state_after==S.RUNNING and result.stop_pending==command
        until(lambda:sink.views[-1].jobs[0].stop_pending==command)
        row=sink.views[-1].jobs[0]; assert row.owned and row.state==S.RUNNING and row.bytes_done==42
        assert observed==[command]
    finally: release.set(); thread.join(3)
    assert not thread.is_alive(); bridge.refresh()
    until(lambda:sink.views[-1].jobs[0].state==final)
    assert sink.views[-1].jobs[0].stop_pending is None
    if action=="race":
        assert sink.views[-1].jobs[0].result_status=="ARCHIVED"
        assert submit(bridge,sink,request(9)).requeued==1


def test_controls_only_route_through_runner(rig,monkeypatch):
    bridge,sink=rig(); job_id=submit(bridge,sink).job_ids[0]; calls=[]
    for action in ("pause","resume","cancel","retry"):
        original=getattr(bridge._runner,action)
        def call(identifier, action=action, original=original):
            assert threading.get_ident()==bridge._thread.ident
            calls.append(action); return original(identifier)
        monkeypatch.setattr(bridge._runner,action,call)
    original=bridge._store.transition
    def transition(*args,**kwargs):
        assert inspect.currentframe().f_back.f_code is QueueRunner._change.__code__
        return original(*args,**kwargs)
    monkeypatch.setattr(bridge._store,"transition",transition)
    for token,action in enumerate(("pause","resume","cancel","retry")):
        bridge.control(token,action,(job_id,)); until(lambda:len(sink.controls)>token)
        assert sink.controls[-1].ok
    assert calls==["pause","resume","cancel","retry"]
    bridge._store.claim_next("other-owner")
    before=bridge._store.get(job_id)
    for token,action,identifier,code in ((5,"pause",job_id,"ownership"),
            (6,"cancel","missing","unknown_job"),(7,"arbitrary",job_id,"invalid_action")):
        bridge.control(token,action,(identifier,))
        until(lambda:len(sink.controls)>=token)
        assert not sink.controls[-1].ok and sink.controls[-1].code==code
        assert bridge._store.get(job_id)==before


@pytest.mark.parametrize("kind",["corrupt","newer"])
def test_unavailable_queue_preserves_bytes_and_never_reopens(tmp_path,rig,kind,monkeypatch):
    path=tmp_path/"q.db"
    if kind=="corrupt": path.write_bytes(b"corrupt queue")
    else:
        QueueStore(path)
        with sqlite3.connect(path) as db: db.execute("pragma user_version=2")
    original=path.read_bytes(); bridge,sink=rig()
    assert not bridge.is_open and not sink.views[-1].usable and sink.views[-1].problem and not sink.views[-1].jobs
    assert not bridge.enqueue(request())
    bridge.open(); bridge.refresh(); QTest.qWait(30)
    assert path.read_bytes()==original and not bridge.is_open


@pytest.mark.parametrize("code", ["database_busy", "database_unavailable", "localappdata_unset"])
@pytest.mark.parametrize("retry", ["manual", "timer"])
def test_open_failures_retry_and_recover(rig,monkeypatch,code,retry):
    from covearchive.ui import archive_bridge as module
    original=module.QueueStore; calls=[]
    def create(*args,**kw):
        calls.append(threading.get_ident())
        if len(calls)==1 or code=="localappdata_unset": raise QueueError(code)
        return original(*args,**kw)
    monkeypatch.setattr(module,"QueueStore",create)
    bridge,sink=rig()
    assert len(calls)==1 and calls[0]==bridge._thread.ident and not bridge.is_open
    assert sink.views[-1].problem and not bridge.enqueue(request())
    if retry=="timer": bridge.timer.setInterval(10)
    else: bridge.open(); bridge.refresh()
    if code=="localappdata_unset":
        for _ in range(3):
            bridge.refresh(); QCoreApplication.processEvents(); time.sleep(0.02)
        assert len(calls)==1 and not bridge.is_open
    else:
        until(lambda:bridge.is_open and sink.views[-1].problem is None)
        assert len(calls)==2
    assert all(identifier==bridge._thread.ident for identifier in calls)


def test_post_enqueue_snapshot_failure_has_one_result(rig,monkeypatch):
    bridge,sink=rig()
    def fail(): raise QueueError("database_busy")
    original=bridge._runner.snapshot
    monkeypatch.setattr(bridge._runner,"snapshot",fail)
    result=submit(bridge,sink); until(lambda:sink.views[-1].problem)
    assert result.created==1 and result.error is None
    assert len(sink.enqueues)==1
    assert sink.views[-1].problem and bridge.is_open
    monkeypatch.setattr(bridge._runner,"snapshot",original); bridge.refresh()
    until(lambda:sink.views[-1].problem is None)


@pytest.mark.parametrize("kind",["refresh","enqueue","control"])
@pytest.mark.parametrize("code",["database_busy","database_unavailable","database_error","unexpected"])
def test_transient_commands_preserve_last_good_and_recover(rig,monkeypatch,kind,code):
    bridge,sink=rig(); job_id=submit(bridge,sink).job_ids[0]
    until(lambda:sink.views[-1].jobs); before=sink.views[-1].jobs
    target,name=(bridge._runner,"snapshot") if kind=="refresh" else ((bridge._store,"enqueue_many") if kind=="enqueue" else (bridge._runner,"pause"))
    original=getattr(target,name)
    def fail(*args,**kw):
        if code=="unexpected": raise RuntimeError("https://secret.invalid/?token=SECRET")
        raise QueueError(code)
    monkeypatch.setattr(target,name,fail)
    if kind=="refresh": bridge.refresh()
    elif kind=="enqueue":
        result=submit(bridge,sink,request(55))
        assert result.created==result.requeued==0 and not result.job_ids and result.error
    else: bridge.control(55,"pause",(job_id,)); until(lambda:sink.controls)
    until(lambda:sink.views[-1].problem)
    assert bridge.is_open and sink.views[-1].usable and sink.views[-1].jobs==before and "SECRET" not in sink.views[-1].problem
    if kind=="control":
        result=sink.controls[-1]
        assert not result.ok and result.code==("internal_error" if code=="unexpected" else code)
        assert "SECRET" not in repr(result)
        with pytest.raises(FrozenInstanceError): result.ok=True
    monkeypatch.setattr(target,name,original); bridge.refresh()
    until(lambda:sink.views[-1].problem is None)
    assert bridge.is_open and sink.views[-1].jobs==before





def test_run_loop_single_no_lost_enqueue_at_final_check_and_affinity(rig,monkeypatch):
    repo,r,a,_=item(); gate=threading.Event(); release=threading.Event(); calls=[]; active=[0,0]
    bridge,sink=rig(run=True,catalog=lambda repo:NS(releases=(r,)),archive=lambda *a,**k:ArchiveResult(ArchiveStatus.ARCHIVED,None,1,10))
    original=bridge._runner.run_pending
    def run(**kw):
        calls.append(threading.get_ident()); active[0]+=1; active[1]=max(active)
        try:
            result=original(**kw)
            if len(calls)==1: gate.set(); assert release.wait(3)
            return result
        finally: active[0]-=1
    monkeypatch.setattr(bridge._runner,"run_pending",run)
    bridge._store.enqueue(repo,r,a,native_root("C:/Archive")); bridge.start_queue()
    try:
        until(gate.is_set); assert bridge.run_active
        result=submit(bridge,sink,request(50,native_root("C:/Other"))); assert result.started
        for _ in range(5): bridge.start_queue()
    finally: release.set()
    until(lambda:not bridge.run_active and len(calls)>=2)
    until(lambda:sink.runs and sink.runs[-1].kind=="finished")
    assert active[1]==1 and all(t not in (threading.get_ident(),bridge._thread.ident) for t in calls)
    assert all(j.state==S.COMPLETED for j in bridge._store.list_jobs())


def test_start_rules_defer_restored_jobs_and_resume_batch(rig):
    repo,r,a,_=item(); bridge,sink=rig(run=True,catalog=lambda repo:NS(releases=(r,)),archive=lambda *a,**k:ArchiveResult(ArchiveStatus.ARCHIVED,None,1,10))
    earlier=bridge._store.enqueue(repo,r,a,native_root("C:/Earlier")).job
    result=submit(bridge,sink); assert not result.started and result.deferred_pending==1
    job_id=result.job_ids[0]
    bridge.control(3,"pause",(job_id,)); until(lambda:sink.controls)
    bridge.control(4,"resume",(job_id,)); until(lambda:len(sink.controls)==2)
    assert not sink.controls[-1].started and sink.controls[-1].deferred_pending==1
    assert bridge._store.get(earlier.job_id).state==S.QUEUED
    bridge.start_queue(); until(lambda:bridge._store.get(job_id).state==S.COMPLETED and not bridge.run_active)


def test_other_runner_lock_and_run_exception_leave_commands_usable(rig,monkeypatch):
    bridge,sink=rig(run=True); repo,r,a,_=item(); job=bridge._store.enqueue(repo,r,a,native_root("C:/Archive")).job
    with RunnerLock(bridge._store.path):
        bridge.start_queue(); until(lambda:sink.runs and sink.runs[-1].kind=="blocked")
        assert sink.runs[-1].kind=="blocked" and bridge.is_open
        bridge.refresh(); until(lambda:sink.views[-1].run_state=="blocked")
        assert "owns the queue lock" in sink.views[-1].problem
        bridge.control(5,"pause",(job.job_id,)); until(lambda:sink.controls)
        assert sink.controls[-1].ok
        with pytest.raises(QueueBusyError): RunnerLock(bridge._store.path)
    def fail(**kw): raise RuntimeError("SECRET https://host")
    monkeypatch.setattr(bridge._runner,"run_pending",fail)
    bridge.start_queue(); until(lambda:sink.runs[-1].kind=="error")
    assert sink.runs[-1].code=="internal_error" and bridge.is_open and not bridge.run_active


def test_timer_refresh_and_cooldown_not_bypassed(rig):
    repo,r,a,_=item(); calls=[]
    def archive(*args,**kw): calls.append(time.time()); return ArchiveResult(ArchiveStatus.ARCHIVED,None,1,10)
    bridge,sink=rig(run=True,catalog=lambda repo:NS(releases=(r,)),archive=archive)
    job=bridge._store.enqueue(repo,r,a,native_root("C:/Archive")).job
    bridge._store.claim_next("test"); until_at=time.time()+0.4
    bridge._store.finish(job.job_id,"test",S.RETRY_WAIT,retry_count=2,next_attempt_at=until_at,cooldown_until=until_at)
    bridge.start_queue(); QTest.qWait(70)
    assert not calls and bridge.run_active
    until(lambda:calls and not bridge.run_active)
    until(lambda:sink.views[-1].jobs and sink.views[-1].jobs[0].state==S.COMPLETED)
    assert calls[0]>=until_at and bridge._store.get(job.job_id).retry_count==2
    assert any(v.run_state=="running" for v in sink.views)


def test_enqueue_and_retry_auto_start_without_earlier_pending(rig):
    repo,r,a,_=item(); attempts=[]
    def archive(*args,**kw):
        attempts.append(1)
        return ArchiveResult(ArchiveStatus.FAILED if len(attempts)==1 else ArchiveStatus.ARCHIVED,None,1,10)
    bridge,sink=rig(run=True,catalog=lambda repo:NS(releases=(r,)),archive=archive)
    result=submit(bridge,sink); assert result.started and result.deferred_pending==0
    job_id=result.job_ids[0]; until(lambda:not bridge.run_active and bridge._store.get(job_id).state==S.FAILED)
    bridge.control(8,"retry",(job_id,)); until(lambda:sink.controls)
    assert sink.controls[-1].started and sink.controls[-1].deferred_pending==0
    until(lambda:not bridge.run_active and bridge._store.get(job_id).state==S.COMPLETED)


@pytest.fixture
def blocked_shutdown(rig):
    release=threading.Event(); entered=threading.Event(); mode=[False]; repo,r,a,_=item()
    def archive(*args,**kw):
        mode.append("archive")
        entered.set(); assert release.wait(5)
        return ArchiveResult(ArchiveStatus.ARCHIVED if mode[0] else ArchiveStatus.CANCELLED,None,1,10)
    bridge,sink=rig(run=True,settings=QueueSettings(max_active=1),catalog=lambda repo:NS(releases=(r,)),archive=archive)
    bridge.observation_timeout=0.02; job=submit(bridge,sink).job_ids[0]; until(entered.is_set)
    until(lambda:sink.views[-1].jobs and sink.views[-1].jobs[0].owned)
    yield bridge,sink,job,release,mode
    release.set()
    if bridge.shutting_down: until(lambda:bridge.closed)


def done(bridge,sink): until(lambda:bridge.closed and sink.shutdowns and sink.shutdowns[-1].phase=="done")


def test_shutdown_persists_pause_before_signal_and_keeps_queued(blocked_shutdown,monkeypatch):
    bridge,sink,job,release,mode=blocked_shutdown; observed=[]; original=bridge._runner._request_stop
    repo,r,a,_=item(); queued=bridge._store.enqueue(repo,r,a,native_root("C:/Waiting")).job
    def stop(control,reason):
        if reason=="shutdown": assert observed==["pause"]
        if reason=="pause":
            with sqlite3.connect(bridge._store.path) as db:
                observed.append(db.execute("select stop_requested from jobs where job_id=?",(job,)).fetchone()[0])
            assert threading.get_ident()==bridge._thread.ident and observed[-1]=="pause"
        return original(control,reason)
    monkeypatch.setattr(bridge._runner,"_request_stop",stop)
    bridge.begin_shutdown(); until(lambda:sink.shutdowns and sink.shutdowns[-1].phase=="timeout_waiting")
    assert observed==["pause"] and bridge._store.get(queued.job_id).state==S.QUEUED
    release.set(); done(bridge,sink)
    assert bridge._store.get(job).state==S.PAUSED and bridge._store.get(queued.job_id).state==S.QUEUED


def test_shutdown_returns_without_blocking_and_refreshes(blocked_shutdown):
    bridge,sink,job,release,mode=blocked_shutdown; ticks=[]; timer=QTimer(); timer.timeout.connect(lambda:ticks.append(1)); timer.start(5)
    try:
        start=time.monotonic(); bridge.begin_shutdown(); assert time.monotonic()-start<0.1
        until(lambda:len(ticks)>=3 and any(v.run_state=="stopping" for v in sink.views))
        until(lambda:any(status.phase=="timeout_waiting" for status in sink.shutdowns))
        assert bridge._run_thread.is_alive() and bridge._store.get(job).state==S.RUNNING
        with pytest.raises(QueueBusyError): RunnerLock(bridge._store.path)
        assert not bridge.closed
    finally: timer.stop(); release.set()
    done(bridge,sink)


@pytest.mark.parametrize("pause_active,complete,expected", [
    (True,False,S.PAUSED), (True,True,S.COMPLETED), (False,False,S.QUEUED)])
def test_shutdown_delayed_release_joins_threads_and_no_late_signals(blocked_shutdown,pause_active,complete,expected):
    bridge,sink,job,release,mode=blocked_shutdown
    mode[0]=complete; bridge.begin_shutdown(pause_active=pause_active)
    until(lambda:any(s.phase=="timeout_waiting" for s in sink.shutdowns)); release.set(); done(bridge,sink)
    assert not any(t.is_alive() for t in (bridge._thread,bridge._run_thread,bridge._shutdown_thread))
    assert bridge._store.get(job).state==expected
    assert sink.views[-1].jobs[0].state==expected and sink.views[-1].run_state=="closed"
    with RunnerLock(bridge._store.path): pass
    counts=tuple(map(len,(sink.views,sink.enqueues,sink.controls,sink.runs,sink.shutdowns)))
    bridge.open(); bridge.refresh(); assert not bridge.enqueue(request(90)); bridge.control(91,"pause",(job,)); bridge.start_queue(); bridge.begin_shutdown()
    for _ in range(3): QCoreApplication.processEvents(); time.sleep(0.01)
    assert counts==tuple(map(len,(sink.views,sink.enqueues,sink.controls,sink.runs,sink.shutdowns))) and not bridge.timer.isActive()
    bridge.deleteLater(); QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


def test_shutdown_idempotent_and_shutdown_thread_is_separate(blocked_shutdown,monkeypatch):
    bridge,sink,job,release,mode=blocked_shutdown; calls=[]; original=bridge._runner.shutdown
    def shutdown(timeout=None):
        calls.append(threading.get_ident())
        if bridge.shutting_down:
            assert threading.current_thread() is not threading.main_thread()
            assert calls[-1] != bridge._run_thread.ident
            assert calls[-1] != bridge._thread.ident or timeout==0
        return original(timeout)
    monkeypatch.setattr(bridge._runner,"shutdown",shutdown)
    bridge.begin_shutdown(); thread=bridge._shutdown_thread; bridge.begin_shutdown()
    assert not bridge.enqueue(request(99)); bridge.control(99,"cancel",(job,)); bridge.start_queue(); bridge.open()
    until(lambda:calls); release.set(); done(bridge,sink)
    assert bridge._shutdown_thread is thread and set(calls)=={bridge._thread.ident,thread.ident}
    assert not sink.controls


def test_shutdown_never_opened_or_corrupt_preserves_file(tmp_path,rig):
    app=QCoreApplication.instance(); bridge=ArchiveBridge(tmp_path/"never.db"); sink=Sink()
    bridge.shutdown_status.connect(sink.shutdown,Qt.QueuedConnection); bridge.begin_shutdown(); done(bridge,sink)
    assert not (tmp_path/"never.db").exists()
    path=tmp_path/"q.db"; path.write_bytes(b"corrupt")
    other,other_sink=rig(); other.begin_shutdown(); done(other,other_sink)
    assert path.read_bytes()==b"corrupt"


def test_shutdown_while_waiting_for_retries_is_prompt(rig):
    bridge,sink=rig(run=True); bridge.observation_timeout=0.02; repo,r,a,_=item()
    job=bridge._store.enqueue(repo,r,a,native_root("C:/Archive")).job; bridge._store.claim_next("test")
    bridge._store.finish(job.job_id,"test",S.RETRY_WAIT,next_attempt_at=time.time()+60)
    bridge.start_queue(); until(lambda:bridge.run_active)
    start=time.monotonic(); bridge.begin_shutdown(); done(bridge,sink)
    assert time.monotonic()-start<1 and bridge._store.get(job.job_id).state==S.RETRY_WAIT


@pytest.mark.parametrize("code",["corrupt","database_busy"])
def test_shutdown_failed_intent_is_bounded_and_reported(blocked_shutdown,monkeypatch,code):
    bridge,sink,job,release,mode=blocked_shutdown; original=bridge._runner.pause
    def fail(job): raise QueueError(code)
    monkeypatch.setattr(bridge._runner,"pause",fail)
    try:
        bridge.begin_shutdown(); until(lambda:any(not getattr(status,"intent_saved",True) for status in sink.shutdowns))
        release.set(); done(bridge,sink)
        assert sink.shutdowns[-1].intent_saved is False
        with RunnerLock(bridge._store.path): pass
    finally:
        monkeypatch.setattr(bridge._runner,"pause",original); bridge._fatal=False


def test_C5_real_gui_archive_reverify_and_private_snapshots(tmp_path,caplog):
    from pathlib import Path
    import logging
    from PySide6.QtWidgets import QWidget
    from covearchive import scan_worker as wire
    from covearchive.github.models import ReleaseCatalog
    from covearchive.transfer.resolve import TransferPolicy
    from covearchive.archive import manifest as manifests
    from covearchive.ui.window import MainWindow
    from covearchive.ui.preferences import PreferenceStore
    from test_ui_window import FakeController, ROOT, RID, URL, APP, DATE
    from test_transfer_resolve import fixture_server, TARGET, SECRET
    from test_archive_download import release, folder, project, no_temps
    from test_archive_download_e2e import DATA, selected
    def wait(predicate):
        deadline=time.monotonic()+10
        while not predicate() and time.monotonic()<deadline: APP.processEvents(); time.sleep(0.005)
        assert predicate()
    root=tmp_path/"archive"; root.mkdir(); queue_path=tmp_path/"gui.db"; caplog.set_level(logging.DEBUG)
    a=replace(selected(True),created_at=DATE,updated_at=DATE)
    assets=(a,replace(a,id=8,name="second.zip"),replace(a,id=9,name="symbols.zip"))
    source=release(assets=assets); catalog=ReleaseCatalog(TARGET,(source,),1,1,0,(),"/repos/Owner/Repo/releases",True)
    with fixture_server() as server:
        for asset in assets:
            server.routes[f"/repos/Owner/Repo/releases/assets/{asset.id}"]=(302,{"Location":f"/cdn{asset.id}?"+SECRET},b"")
            server.routes[f"/cdn{asset.id}"]=(200,{"Content-Length":str(len(DATA))},DATA)
        bridge=ArchiveBridge(queue_path,runner_options={"catalog":lambda repo:catalog,"policy":TransferPolicy.for_loopback_test(server.origin)},refresh_ms=20)
        controller=FakeController(); window=MainWindow(ROOT/"run_covearchive.py",controller,bridge=bridge,preferences=PreferenceStore(tmp_path/"prefs.json"))
        window._confirm_close=lambda:"Pause and Close"; window.show()
        try:
            wait(lambda:bridge.is_open); window.url.setText(URL); window.scan.click()
            controller.finish(wire.decode_response(wire.encode_success(RID,catalog),RID)); window.exclude.setText("*symbols*")
            model=window.tree.model(); assert model.setData(model.index(0,0),Qt.Checked,Qt.CheckStateRole)
            assert window.checked=={(10,7),(10,8)}; window.root.setText(str(root)); window.archive_selected.click()
            window.root.setText(str(tmp_path/"later")); window.url.clear()
            wait(lambda:window.last_view and len(window.last_view.jobs)>=2 and all(j.state==S.COMPLETED for j in window.last_view.jobs) and not bridge.run_active)
            assert len(window.last_view.jobs)==2 and all(j.result_status=="ARCHIVED" for j in window.last_view.jobs)
            assert all("Completed (queue record)" in window.queue_panel.table.item(i,2).text() for i in range(2))
            assert {path.split("?")[0] for path,_ in server.requests}=={f"/repos/Owner/Repo/releases/assets/{i}" for i in (7,8)}|{"/cdn7","/cdn8"}
            assert len(server.requests)==4 and all((folder(root)/asset.name).read_bytes()==DATA for asset in assets[:2])
            index=manifests.loads_index((project(root)/"archive-index.json").read_text())
            manifest=manifests.loads_release_manifest((folder(root)/"release.json").read_text()); manifests.check_consistent(index,manifest)
            assert len(manifest.assets)==2 and not (folder(root)/"symbols.zip").exists(); no_temps(root)
            with sqlite3.connect(queue_path) as db: assert db.execute("select archive_root from jobs").fetchall()==[(str(root),)]*2
            server.requests.clear(); window.root.setText(str(root)); window.archive_selected.click()
            wait(lambda:window.last_view and all(j.result_status=="VERIFIED_EXISTING" for j in window.last_view.jobs) and not bridge.run_active)
            assert not server.requests and "re-queued 2" in window.status.text()
            widgets=[window,*window.findChildren(QWidget)]; text=" ".join(w.toolTip() for w in widgets)+" ".join(w.text() for w in widgets if hasattr(w,"text"))
            text+=" ".join(window.queue_panel.table.item(i,c).text()+window.queue_panel.table.item(i,c).toolTip() for i in range(2) for c in range(6))+caplog.text
            raw=queue_path.read_bytes()+text.encode()
            assert all(marker not in raw for marker in (b"http://",b"https://",b"sig=",SECRET.encode(),server.origin.encode()))
        finally:
            window.close(); wait(lambda:bridge.closed and not window.isVisible())
            assert all(not thread.is_alive() for thread in (bridge._thread,bridge._run_thread,bridge._shutdown_thread) if thread is not None)
            with RunnerLock(queue_path): pass


def test_R3_shutdown_waits_for_inflight_open(tmp_path,monkeypatch):
    from covearchive.ui import archive_bridge as module
    entered=threading.Event(); release=threading.Event(); calls=[]; real=module.QueueStore; shutdown=QueueRunner.shutdown
    def create(*a,**k): entered.set(); assert release.wait(3); return real(*a,**k)
    def stop(self,timeout=None): calls.append((threading.current_thread().name,timeout)); return shutdown(self,timeout)
    monkeypatch.setattr(module,"QueueStore",create); monkeypatch.setattr(QueueRunner,"shutdown",stop)
    bridge=ArchiveBridge(tmp_path/"opening.db",observation_timeout=0.01); sink=Sink(); bridge.shutdown_status.connect(sink.shutdown,Qt.QueuedConnection)
    bridge.open(); assert entered.wait(3); bridge.begin_shutdown(); time.sleep(0.06)
    assert not bridge.closed; release.set(); done(bridge,sink)
    assert any(name=="covearchive-shutdown" for name,timeout in calls)
    with RunnerLock(tmp_path/"opening.db"): pass


def test_R3_pause_stops_scheduler_before_command_returns(blocked_shutdown,monkeypatch):
    bridge,sink,job,release,mode=blocked_shutdown; queued=bridge._store.enqueue(*item()[:3],native_root("C:/Waiting")).job; claims=[]; downloads=[]
    original=bridge._store.claim_next; pause=bridge._pause_all; change=bridge._runner.pause
    def claim(*a,**k): claims.append(1); return original(*a,**k)
    def paused(identifier): result=change(identifier); release.set(); time.sleep(0.1); return result
    def delayed(value): pause(value); time.sleep(0.1)
    monkeypatch.setattr(bridge._store,"claim_next",claim); monkeypatch.setattr(bridge._runner,"pause",paused); monkeypatch.setattr(bridge,"_pause_all",delayed)
    monkeypatch.setattr(bridge._runner,"archive",lambda *a,**k: downloads.append(a[2].id))
    bridge.begin_shutdown(); done(bridge,sink)
    assert claims==downloads==[] and bridge._store.get(queued.job_id).state==S.QUEUED


def test_R3_worker_finish_waits_for_atomic_stop_then_resumes(blocked_shutdown,monkeypatch):
    bridge,sink,job,release,mode=blocked_shutdown; runner=bridge._runner; lock=runner._state; waiting=threading.Event(); observed=[]; pause=runner.pause
    class ObservedLock:
        def __enter__(self):
            if release.is_set() and threading.current_thread().name.startswith("covearchive-queue-"): waiting.set()
            return lock.__enter__()
        def __exit__(self,*args): return lock.__exit__(*args)
    def paused(identifier):
        result=pause(identifier); release.set(); assert waiting.wait(1); time.sleep(0.05)
        observed.append(bridge._store.get(job).state); return result
    monkeypatch.setattr(runner,"_state",ObservedLock()); monkeypatch.setattr(runner,"pause",paused)
    bridge.begin_shutdown(); done(bridge,sink)
    assert waiting.is_set() and observed==[S.RUNNING] and bridge._store.get(job).state==S.PAUSED
    assert not bridge._run_thread.is_alive()
    with RunnerLock(bridge._store.path): pass


@pytest.mark.parametrize("held,interrupted", [(False,True),(True,True),(True,False)])
def test_F1_interrupted_rows_require_explicit_start(rig,tmp_path,held,interrupted):
    from covearchive.ui.queue_panel import QueuePanel
    store = QueueStore(tmp_path/"q.db")
    old = store.enqueue(*item()[:3],native_root("C:/Archive")).job
    if interrupted: store.claim_next("crashed-owner")
    calls = []
    def archive(*args,**kwargs):
        calls.append(args[2].id)
        return ArchiveResult(ArchiveStatus.ARCHIVED,None,1,10)
    bridge,sink = rig(run=True,catalog=lambda repo:NS(releases=(item()[1],)),archive=archive)
    panel = QueuePanel(bridge)
    lock = RunnerLock(store.path) if held else None
    try:
        bridge.refresh()
        until(lambda:panel.table.rowCount()==1)
        panel.table.selectRow(0)
        assert not calls and not bridge.run_active and panel.start.isEnabled()
        result = submit(bridge,sink,request(77,native_root("C:/New")))
        assert not result.started and result.deferred_pending==1
        panel.start.click()
        if held:
            until(lambda:panel.view.run_state=="blocked")
            assert panel.banner.text() and not calls and panel.start.isEnabled()
            assert store.get(old.job_id).state==(S.RUNNING if interrupted else S.QUEUED)
            if not interrupted: assert panel.buttons["pause"].isEnabled() and panel.buttons["cancel"].isEnabled()
            lock.close()
            lock = None
            panel.start.click()
        until(lambda:not bridge.run_active and store.get(old.job_id).state==S.COMPLETED)
        assert store.get(old.job_id).retry_count==(1 if interrupted else 0) and len(calls)==2
        bridge.refresh()
        until(lambda:panel.view.problem is None)
    finally:
        if lock: lock.close()
        panel.close()
        panel.deleteLater()


@pytest.mark.parametrize("blocked,stop,restart", [
    (False,None,False), (True,None,False), (False,"pause",False),
    (False,"cancel",False), (False,"pause",True), (False,"cancel",True)])
def test_F2_failed_runner_retires_before_replacement(blocked_shutdown,monkeypatch,rig,blocked,stop,restart):
    bridge,sink,job,release,mode = blocked_shutdown
    mode[0] = True
    old = bridge._runner
    gate = threading.Event()
    shutdown = old.shutdown
    def gated_shutdown(timeout=None):
        assert gate.wait(3)
        return shutdown(timeout)
    if stop: monkeypatch.setattr(old,"shutdown",gated_shutdown)
    if not stop: bridge._store.enqueue(*item()[:3],native_root("C:/Waiting"))
    target = "claim_next" if blocked else "finish"
    original = getattr(bridge._store,target)
    def fail_once(*args,**kwargs):
        monkeypatch.setattr(bridge._store,target,original)
        raise QueueBusyError("database_busy")
    monkeypatch.setattr(bridge._store,target,fail_once)
    if stop:
        bridge.control(99,stop,(job,))
        until(lambda:sink.controls and sink.controls[-1].ok)
    if blocked:
        old.settings = replace(old.settings,max_active=2)
    else:
        release.set()
    until(lambda:any(event.kind=="error" for event in sink.runs))
    if stop:
        try:
            bridge.refresh()
            until(lambda:sink.views[-1].jobs and not sink.views[-1].jobs[0].owned)
            view = sink.views[-1]
            assert bridge._runner is old and view.jobs[0].state==S.RUNNING and view.run_state=="running"
            assert view.jobs[0].stop_pending is None
            with sqlite3.connect(bridge._store.path) as db:
                assert db.execute("select stop_requested from jobs where job_id=?",(job,)).fetchone()[0]==stop
        finally: gate.set()
    if blocked:
        bridge.start_queue()
        QTest.qWait(50)
        assert bridge._runner is old and bridge.run_active
        with pytest.raises(QueueBusyError): RunnerLock(bridge._store.path)
        release.set()
    until(lambda:not bridge.run_active)
    bridge.refresh()
    until(lambda:bridge._runner is not old)
    assert not bridge._run_thread.is_alive()
    with RunnerLock(bridge._store.path): pass
    if restart:
        bridge.begin_shutdown()
        done(bridge,sink)
        bridge,sink = rig(run=True,**bridge._options)
    if stop:
        from covearchive.ui.queue_panel import QueuePanel
        panel = QueuePanel(bridge)
        bridge.refresh()
        until(lambda:panel.view and panel.view.jobs)
        assert not panel.view.jobs[0].owned and panel.view.jobs[0].stop_pending is None and panel.start.isEnabled()
        assert panel.table.item(0,2).text()=="Interrupted or running elsewhere"
        panel.deleteLater()
    bridge.start_queue()
    expected = S.PAUSED if stop=="pause" else S.CANCELLED if stop else S.COMPLETED
    until(lambda:not bridge.run_active and all(j.state==expected for j in bridge._store.list_jobs()))
    assert bridge._store.get(job).retry_count==(0 if blocked or stop else 1)
    if stop: assert mode.count("archive")==1


@pytest.mark.parametrize("action", ["enqueue", "resume", "retry"])
def test_H2_committed_result_survives_startup_read_failure(rig,monkeypatch,action):
    bridge,sink = rig()
    if action!="enqueue":
        job_id = submit(bridge,sink).job_ids[0]
        getattr(bridge._runner,"pause" if action=="resume" else "cancel")(job_id)
    real = bridge._store.list_jobs
    def fail_once():
        monkeypatch.setattr(bridge._store,"list_jobs",real)
        raise QueueBusyError("database_busy")
    monkeypatch.setattr(bridge._store,"list_jobs",fail_once)
    if action=="enqueue":
        result = submit(bridge,sink)
        assert result.created==1 and len(result.job_ids)==1 and result.error is None
    else:
        bridge.control(8,action,(job_id,))
        until(lambda:sink.controls)
        result = sink.controls[-1]
        assert result.ok and result.state_after==S.QUEUED
    assert result.startup_error=="database_busy" and not result.started and bridge.is_open
    again = submit(bridge,sink,request(9))
    assert len(again.existing)==1 and again.created==0 and len(real())==1
def test_auto_archive_bridge_preserves_completed_manual_requeues(rig):
    bridge,sink=rig(); first=submit(bridge,sink); job_id=first.job_ids[0]
    bridge._store.claim_next('owner')
    bridge._store.finish(job_id,'owner',S.COMPLETED,result=NS(status='ARCHIVED'),retry_count=3)
    before=bridge._store.get(job_id)
    req=request(-1)
    req=EnqueueRequest(req.token,req.repository,req.archive_root,req.items,requeue_completed=False)
    automatic=submit(bridge,sink,req)
    assert automatic.error is None and automatic.created==automatic.requeued==0
    assert automatic.existing==((job_id,S.COMPLETED),) and automatic.job_ids==()
    assert bridge._store.get(job_id)==before
    assert len(bridge._store.list_jobs())==1
    assert EnqueueRequest(-2,req.repository,req.archive_root,req.items,requeue_completed=False).requeue_completed is False
    assert request().requeue_completed is True
    manual=submit(bridge,sink,request(2))
    assert manual.requeued==1 and manual.created==0 and bridge._store.get(job_id).state==S.QUEUED
