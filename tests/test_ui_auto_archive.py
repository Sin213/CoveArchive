"""Offline automatic checks, isolated from Browse and queue ownership."""
from dataclasses import replace
from types import SimpleNamespace as NS
import pytest
from PySide6.QtCore import QObject, Signal
from covearchive import scan_worker as wire
from covearchive.library.model import Library
from covearchive.queue import JobState as S
from covearchive.ui.archive_bridge import EnqueueRequest, EnqueueResult, EnqueueItem
from covearchive.ui.auto_archive import AutoArchive
from test_ui_window import FakeController, APP, RID
from test_scan_worker import catalog, release, asset
from test_ui_archive import rig, until, submit
from test_scan_process import until as until_closed


class Bridge(QObject):
    enqueued=Signal(object)
    def __init__(self):
        super().__init__(); self.is_open=True; self.shutting_down=False; self.closed=False; self.calls=[]; self.accept=True
    def enqueue(self,request): self.calls.append(request); return self.accept
    def start_queue(self,*a): pytest.fail('auto called start_queue')
    def control(self,*a): pytest.fail('auto called control')
    def begin_shutdown(self,*a): pytest.fail('auto called begin_shutdown')
    @property
    def _store(self): pytest.fail('auto accessed store')
    @property
    def _runner(self): pytest.fail('auto accessed runner')


class Scan(FakeController):
    def __init__(self): super().__init__(); self.cancels=0; self.accept=True
    def cancel(self): self.cancels+=1; super().cancel()
    def start(self,url): return super().start(url) if self.accept else False


@pytest.fixture
def env(tmp_path,monkeypatch):
    for key in ('LOCALAPPDATA','XDG_CONFIG_HOME','XDG_STATE_HOME'): monkeypatch.setenv(key,str(tmp_path/key))
    clock=[10000.]; lib=[Library().add_repository('https://github.com/Owner/Repo').set_auto_archive('github:owner/repo',1)]
    settings=dict(archive_root=str(tmp_path/'archive'),repositories={'owner/repo':dict(include=[],exclude=[],extensions=['.zip'],prereleases=False)})
    scan=Scan(); bridge=Bridge(); auto=AutoArchive(scan,bridge,lambda:lib[0],lambda:settings,clock=lambda:clock[0],tick_ms=100000)
    value=NS(auto=auto,scan=scan,bridge=bridge,clock=clock,lib=lib,settings=settings,repo=lib[0].repositories[0])
    yield value
    auto.shutdown(); APP.processEvents()


def due(e): e.clock[0]+=3600; e.auto.tick()
def success(scan,c=None,*,rejected=False):
    c=c if c is not None else catalog(release(assets=[asset(size=1)]))
    scan.finish(wire.decode_response(wire.encode_success(RID,c,token_rejected=rejected),RID))
def result(req,**kw):
    return EnqueueResult(req.token,kw.get('created',1),0,(),(),kw.get('started',False),kw.get('pending',0),kw.get('error'),kw.get('startup_error'))


def test_startup_restart_single_scan_and_manual_independence(env):
    e=env; manual=Scan(); manual.start('https://github.com/manual/repo'); e.auto.start()
    assert not e.scan.calls and not e.bridge.calls and e.auto.summary(e.repo)=='Every 1 hour'
    e.clock[0]+=3599; e.auto.tick(); assert not e.scan.calls
    e.clock[0]+=1; e.auto.tick(); e.auto.tick()
    assert len(e.scan.calls)==1 and e.auto.summary(e.repo)=='Checking now...'
    assert manual.job==RID and manual.cancels==0
    e.auto.shutdown(); newer=AutoArchive(Scan(),e.bridge,lambda:e.lib[0],lambda:e.settings,clock=lambda:e.clock[0])
    try:
        newer.start(); newer.tick(); assert not newer.scan.calls
        assert newer.status('owner/repo').next_check==e.clock[0]+3600
    finally: newer.shutdown(); manual.shutdown()


@pytest.mark.parametrize('rejected',[False,True])
def test_foreign_results_queued_delivery_and_notes(env,rejected):
    e=env; changes=[]; e.auto.changed.connect(lambda:changes.append(1)); e.auto.start(); due(e)
    e.scan.terminal.emit('foreign','COMPLETED',None); assert not e.bridge.calls
    success(e.scan,rejected=rejected); req=e.bridge.calls[-1]
    assert req.token<0 and req.requeue_completed is False
    e.bridge.enqueued.emit(replace(result(req),token=1)); APP.processEvents()
    assert e.auto.status('owner/repo').state=='checking'
    e.bridge.enqueued.emit(result(req,pending=2,startup_error='database_busy'))
    assert e.auto.status('owner/repo').state=='checking'
    APP.processEvents(); status=e.auto.status('owner/repo')
    assert status.state=='waiting' and 'Start Queue' in status.message and '2' in status.message
    assert 'Last ' in e.auto.schedule_text(e.repo) and 'Next ' in e.auto.schedule_text(e.repo)
    assert '\n' in e.auto.detail(e.repo) and changes


@pytest.mark.parametrize('change',['disable','remove','shutdown'])
def test_stale_scan_and_enqueue_results_ignored(env,change):
    e=env; e.auto.start(); due(e)
    if change=='disable': e.lib[0]=e.lib[0].set_auto_archive(e.repo.identity,None); e.auto.sync()
    elif change=='remove': e.lib[0]=Library(); e.auto.sync()
    else: assert e.auto.shutdown() and e.auto.shutdown()
    assert e.scan.cancels==(0 if change=='shutdown' else 1)
    success(e.scan); e.auto.tick(); assert not e.bridge.calls
    if change!='shutdown': assert e.auto.status('owner/repo') is None
    assert not e.auto.timer.isActive() if change=='shutdown' else True


def test_sync_only_cancels_removed_active_key_and_late_enqueue(env):
    e=env; e.auto.start(); due(e)
    e.lib[0]=e.lib[0].add_repository('https://github.com/o/other').set_auto_archive(e.repo.identity,6)
    e.auto.sync(); assert not e.scan.cancels
    success(e.scan); req=e.bridge.calls[-1]
    e.lib[0]=e.lib[0].set_auto_archive(e.repo.identity,None); e.auto.sync()
    e.bridge.enqueued.emit(result(req)); APP.processEvents(); assert e.auto.status('owner/repo') is None


@pytest.mark.parametrize('missing,code',[('rules','needs_rules'),('root','needs_root')])
def test_actionable_prerequisites_no_scan(env,missing,code):
    e=env
    if missing=='rules': e.settings['repositories'].clear()
    else: e.settings['archive_root']=''
    assert e.auto.problem(e.repo)
    e.auto.start(); due(e); assert not e.scan.calls and not e.bridge.calls
    assert e.auto.status('owner/repo').state=='error'
    assert e.auto.problem(e.repo) in e.auto.detail(e.repo)


@pytest.mark.parametrize('flag',['is_open','shutting_down','closed'])
def test_unavailable_bridge_never_scans(env,flag):
    e=env; setattr(e.bridge,flag,flag!='is_open'); e.auto.start(); due(e)
    assert not e.scan.calls and not e.bridge.calls
    assert e.auto.status('owner/repo').state=='retrying'


@pytest.mark.parametrize('detail,delay',[({'retry_after_seconds':120,'retry_at':1},120),({'retry_at':13780},180),({'reset_at':13840},240),({},60)])
def test_failure_hint_precedence_and_timeout(env,detail,delay):
    e=env; e.auto.start(); due(e)
    e.scan.finish(wire.ScanFailure(RID,'rate_limited',detail=detail),'FAILED')
    assert e.auto.status('owner/repo').next_check==e.clock[0]+delay
    assert 'resume' in e.auto.detail(e.repo)
    e.clock[0]+=delay; e.auto.tick(); e.scan.finish(None,'TIMED_OUT')
    assert e.auto.status('owner/repo').next_check==e.clock[0]+900


def test_empty_catalog_rejected_start_enqueue_failure_and_shutdown(env):
    e=env; e.auto.start(); e.scan.accept=False; due(e)
    assert e.auto.status('owner/repo').state!='checking' and not e.bridge.calls
    e.scan.accept=True; due(e); success(e.scan,catalog())
    assert e.auto.status('owner/repo').message=='No new assets' and not e.bridge.calls
    due(e); e.bridge.accept=False; success(e.scan)
    assert e.auto.status('owner/repo').state=='retrying'
    e.auto.shutdown(); before=e.auto.status('owner/repo'); e.bridge.enqueued.emit(result(e.bridge.calls[-1])); APP.processEvents()
    e.auto.tick(); assert e.auto.status('owner/repo')==before


def test_real_bridge_dedupe_all_states_and_archive_bytes(env,rig,tmp_path):
    e=env; e.auto.shutdown(); bridge,sink=rig(); scan=Scan()
    auto=AutoArchive(scan,bridge,lambda:e.lib[0],lambda:e.settings,clock=lambda:e.clock[0]); root=tmp_path/'archive'; root.mkdir()
    (root/'old.zip').write_bytes(b'old archive'); (root/'release.json').write_bytes(b'old manifest')
    before={p.name:p.read_bytes() for p in root.iterdir()}
    c=catalog(release(assets=[asset(i,size=1) for i in range(1,6)]))
    try:
        auto.start(); e.clock[0]+=3600; auto.tick(); success(scan,c)
        until(lambda:sink.enqueues and auto.status('owner/repo').state=='waiting')
        assert sink.enqueues[-1].created==5
        store=bridge._store; jobs=store.list_jobs(); store.claim_next('test'); store.finish(jobs[0].job_id,'test',S.COMPLETED,result=NS(status='ARCHIVED'))
        store.claim_next('test'); store.finish(jobs[1].job_id,'test',S.FAILED,code='network')
        store.transition(jobs[2].job_id,S.CANCELLED); store.claim_next('test'); saved=store.list_jobs()
        e.clock[0]+=3600; auto.tick(); success(scan,c)
        until(lambda:len(sink.enqueues)==2 and auto.status('owner/repo').state=='waiting')
        assert sink.enqueues[-1].created==sink.enqueues[-1].requeued==0 and store.list_jobs()==saved
        e.clock[0]+=3600; auto.tick(); success(scan,catalog()); assert store.list_jobs()==saved
        assert {p.name:p.read_bytes() for p in root.iterdir()}==before
        req=EnqueueRequest(99,c.repository,str(root),(EnqueueItem(c.releases[0],c.releases[0].assets[0]),))
        assert submit(bridge,sink,req).requeued==1
    finally: auto.shutdown()


@pytest.mark.parametrize('error',['database_busy','database_unavailable','internal_error'])
def test_enqueue_errors_and_foreign_negative_token(env,error):
    e=env; e.auto.start(); due(e); success(e.scan); req=e.bridge.calls[-1]
    e.bridge.enqueued.emit(replace(result(req),token=req.token-1)); APP.processEvents()
    assert e.auto.status('owner/repo').state=='checking'
    e.bridge.enqueued.emit(result(req,error=error)); APP.processEvents()
    assert e.auto.status('owner/repo').state==('error' if error=='internal_error' else 'retrying')
    assert not e.bridge.calls[0].requeue_completed


def test_other_watch_removal_does_not_cancel_and_started_note(env):
    e=env; e.lib[0]=e.lib[0].add_repository('https://github.com/o/other').set_auto_archive('github:o/other',6)
    e.auto.start(); due(e); e.lib[0]=e.lib[0].remove_repositories('github:o/other'); e.auto.sync()
    assert e.scan.cancels==0
    success(e.scan); e.bridge.enqueued.emit(result(e.bridge.calls[-1],started=True)); APP.processEvents()
    assert 'downloads started' in e.auto.detail(e.repo)
    e.lib[0]=e.lib[0].set_auto_archive(e.repo.identity,None); e.auto.sync()
    assert e.auto.summary(e.lib[0].repositories[0])=='Off'


def test_busy_reaping_scan_does_not_fail_other_due_watch(env):
    e=env; e.lib[0]=e.lib[0].add_repository('https://github.com/z/other').set_auto_archive('github:z/other',1)
    e.settings['repositories']['z/other']=e.settings['repositories']['owner/repo'].copy()
    e.auto.start(); due(e)
    e.lib[0]=e.lib[0].set_auto_archive(e.repo.identity,None); e.auto.sync()
    assert e.scan.cancels==1
    before=e.auto.status('z/other'); changes=[]; e.auto.changed.connect(lambda:changes.append(1))
    e.auto.tick()
    assert e.auto.status('z/other')==before and not changes
    e.scan.job=None; e.auto.tick()
    assert e.scan.calls[-1]=='https://github.com/z/other' and e.auto.status('z/other').state=='checking'


def test_live_cancel_releases_scheduler_and_other_watches(env):
    e=env; e.lib[0]=e.lib[0].add_repository('https://github.com/z/other').set_auto_archive('github:z/other',1)
    e.settings['repositories']['z/other']=e.settings['repositories']['owner/repo'].copy()
    e.auto.start(); due(e); changes=[]; e.auto.changed.connect(lambda:changes.append(1))
    balanced(e.auto); e.scan.finish(None,'CANCELLED'); balanced(e.auto)
    assert e.auto.status('owner/repo').state=='retrying'
    assert e.auto.status('owner/repo').next_check==e.clock[0]+300 and changes
    e.auto.tick(); balanced(e.auto); assert e.scan.calls[-1]=='https://github.com/z/other'
    success(e.scan,catalog()); balanced(e.auto); e.clock[0]+=300; e.auto.tick(); balanced(e.auto)
    assert e.scan.calls[-1]==e.repo.url


def window_for(e,tmp_path,bridge,auto_scan):
    from covearchive.ui.window import MainWindow
    from covearchive.ui.library_store import LibraryStore
    from covearchive.ui.preferences import PreferenceStore
    store=LibraryStore(tmp_path/'library.json'); assert store.save(e.lib[0]) is None
    prefs=PreferenceStore(tmp_path/'prefs.json'); settings,_,_=prefs.load(); settings.update(e.settings)
    assert prefs.save(settings) is None
    manual=Scan(); window=MainWindow(controller=manual,bridge=bridge,library=store,preferences=prefs,
        auto_scan=auto_scan,auto_clock=lambda:e.clock[0])
    window._confirm_close=lambda:'Pause and Close'; window.show()
    return window,manual


def test_window_auto_startup_delay_and_independent_browse(env,rig,tmp_path):
    e=env; bridge,sink=rig(); automatic=Scan(); window,manual=window_for(e,tmp_path,bridge,automatic)
    try:
        assert not automatic.calls and not manual.calls and not sink.enqueues
        e.clock[0]+=3599; window.auto.tick(); assert not automatic.calls
        e.clock[0]+=1; window.auto.tick(); assert len(automatic.calls)==1 and not manual.calls
        window.url.setText('https://github.com/manual/repo'); window.scan.click()
        assert manual.calls==['https://github.com/manual/repo'] and automatic.job==RID
        success(manual); assert window.show_catalog and window.catalog is not None
        assert automatic.cancels==0 and not sink.enqueues
    finally:
        window.close(); until_closed(lambda:bridge.closed and not window.isVisible(),10)
        assert not window.auto.timer.isActive()


def test_window_hung_auto_tick_prompt_close_kills_and_reaps(env,rig,tmp_path):
    import time
    from covearchive.ui.scan_process import ScanController
    from test_scan_process import command, gone
    e=env; bridge,sink=rig(); automatic=ScanController(command_factory=command('import time;time.sleep(30)'),deadline_ms=60000)
    window,manual=window_for(e,tmp_path,bridge,automatic)
    try:
        e.clock[0]+=3600; started=time.monotonic(); window.auto.tick()
        assert time.monotonic()-started<1
        until(lambda:automatic.job is not None and automatic.job.process.processId()>0)
        pid=automatic.job.process.processId()
        window.url.setText('https://github.com/manual/repo'); window.scan.click(); success(manual)
        assert window.show_catalog and automatic.job is not None
        window.close(); until_closed(lambda:automatic.job is None and gone(pid) and bridge.closed and not window.isVisible(),10)
        assert not sink.enqueues and not window.auto.timer.isActive()
    finally:
        automatic.shutdown(); window.close(); until_closed(lambda:bridge.closed and not window.isVisible(),10)


@pytest.mark.parametrize('state,result_kind',[('FAILED','failure'),('COMPLETED','invalid'),('COMPLETED','rules')])
def test_other_terminal_exits_release_the_scheduler(env,state,result_kind):
    e=env; e.auto.start(); due(e)
    if result_kind=='rules':
        e.settings['repositories']['owner/repo']['include']=True
        success(e.scan)
    else: e.scan.finish(wire.ScanFailure(RID,'network') if result_kind=='failure' else None,state)
    assert e.auto.status('owner/repo').state!='checking'
    e.clock[0]+=3600; e.auto.tick(); assert len(e.scan.calls)==2


@pytest.mark.parametrize('active',[False,True])
def test_window_close_stops_both_scans_before_queue(env,rig,tmp_path,monkeypatch,active):
    from covearchive.ui.window import MainWindow
    from covearchive.ui.library_store import LibraryStore
    from covearchive.ui.preferences import PreferenceStore
    bridge,sink=rig(); manual=Scan(); window=MainWindow(controller=manual,bridge=bridge,
        library=LibraryStore(tmp_path/'empty.json'),preferences=PreferenceStore(tmp_path/'empty-prefs.json'))
    window.show(); calls=[]; window._confirm_close=lambda:'Pause and Close'
    if active: window.last_view=NS(seq=10**9,jobs=(NS(job_id='job-1',owned=True,state=S.RUNNING),))
    try:
        with monkeypatch.context() as patch:
            if not hasattr(window,'auto'): patch.setattr(window,'auto',NS(),raising=False)
            patch.setattr(manual,'shutdown',lambda:calls.append('browse') or True)
            patch.setattr(window.auto,'shutdown',lambda:calls.append('auto') or True,raising=False)
            patch.setattr(bridge,'begin_shutdown',lambda *a:calls.append('bridge'))
            window.close(); assert calls==['browse','auto','bridge']
    finally: window.close(); until_closed(lambda:bridge.closed and not window.isVisible(),10)


def test_keep_open_preserves_auto_and_next_due_check(env,rig,tmp_path):
    e=env; bridge,sink=rig(); automatic=Scan(); window,manual=window_for(e,tmp_path,bridge,automatic)
    window.last_view=NS(seq=10**9,jobs=(NS(job_id='job-1',owned=True,state=S.RUNNING),)); window._confirm_close=lambda:'Keep Open'
    try:
        window.close()
        assert window.isVisible() and window.auto.timer.isActive() and not automatic.closed
        e.clock[0]+=3600; window.auto.tick(); assert len(automatic.calls)==1 and not sink.enqueues
    finally:
        window._confirm_close=lambda:'Pause and Close'; window.close(); until_closed(lambda:bridge.closed and not window.isVisible(),10)


def test_about_to_quit_and_repeated_closed_window_shutdown(env,rig,tmp_path):
    e=env; bridge,sink=rig(); automatic=Scan(); window,manual=window_for(e,tmp_path,bridge,automatic)
    try:
        e.clock[0]+=3600; window.auto.tick(); assert automatic.job is not None
        APP.aboutToQuit.emit()
        assert automatic.closed and manual.closed and not window.auto.timer.isActive()
        window.auto.tick(); assert not sink.enqueues
        window.close(); window.close(); until_closed(lambda:bridge.closed and not window.isVisible(),10)
        window.close(); assert window.auto.shutdown()
    finally: window.close(); until_closed(lambda:bridge.closed and not window.isVisible(),10)


def balanced(auto):
    assert (auto._active is None)==(auto.schedule.checking is None)


@pytest.mark.parametrize('state',['FAILED','CANCELLED'])
def test_synchronous_start_terminal_never_rearms_checking(env,monkeypatch,state):
    e=env; original=e.scan.start
    def start(url):
        accepted=original(url); e.scan.finish(wire.ScanFailure(RID,'process'),state); return accepted
    monkeypatch.setattr(e.scan,'start',start); e.auto.start(); balanced(e.auto); due(e); balanced(e.auto)
    assert e.auto.status('owner/repo').state=='retrying'
    monkeypatch.setattr(e.scan,'start',original); e.clock[0]+=300; e.auto.tick(); balanced(e.auto)
    assert len(e.scan.calls)==2


def test_release_on_shutdown_false_and_busy_start(env,monkeypatch):
    e=env; e.auto.start(); e.scan.accept=False; before=e.auto.status('owner/repo'); due(e); balanced(e.auto)
    assert e.auto.status('owner/repo')==before
    e.scan.accept=True; e.auto.tick(); balanced(e.auto); assert len(e.scan.calls)==1
    monkeypatch.setattr(e.scan,'shutdown',lambda:False)
    assert not e.auto.shutdown(); balanced(e.auto); assert e.auto.status('owner/repo').state=='waiting'


def test_enqueue_watchdog_releases_and_other_watch_runs(env):
    e=env; e.lib[0]=e.lib[0].add_repository('https://github.com/z/other').set_auto_archive('github:z/other',1)
    e.settings['repositories']['z/other']=e.settings['repositories']['owner/repo'].copy()
    e.auto.start(); due(e); balanced(e.auto); success(e.scan); balanced(e.auto); old=e.bridge.calls[-1]
    for _ in range(19): e.auto.tick(); balanced(e.auto)
    assert e.auto.status('owner/repo').state=='checking'
    e.auto.tick(); balanced(e.auto); assert e.auto.status('owner/repo').state=='retrying'
    e.auto.tick(); balanced(e.auto); assert e.scan.calls[-1]=='https://github.com/z/other'
    e.bridge.enqueued.emit(result(old)); APP.processEvents(); balanced(e.auto)
    e.scan.finish(None,'CANCELLED'); balanced(e.auto)


@pytest.mark.parametrize('phase',['scan','enqueue'])
def test_disable_reenable_ignores_old_scan_and_enqueue_signals(env,monkeypatch,phase):
    e=env
    def start(url):
        if e.scan.job: return False
        e.scan.calls.append(url); e.scan.job=f'{len(e.scan.calls):032x}'; e.scan.state_changed.emit(e.scan.job,'RUNNING'); return True
    monkeypatch.setattr(e.scan,'start',start); e.auto.start(); due(e); balanced(e.auto); old=e.scan.job
    c=catalog(release(assets=[asset(size=1)])); response=wire.decode_response(wire.encode_success(old,c),old)
    if phase=='enqueue': e.scan.finish(response,rid=old); balanced(e.auto)
    req=e.bridge.calls[-1] if phase=='enqueue' else None
    e.lib[0]=e.lib[0].set_auto_archive(e.repo.identity,None); e.auto.sync(); balanced(e.auto)
    e.lib[0]=e.lib[0].set_auto_archive(e.repo.identity,1); e.auto.sync(); balanced(e.auto)
    e.scan.job=None; e.clock[0]+=3600; e.auto.tick(); balanced(e.auto); current=e.auto._active
    e.scan.state_changed.emit(old,'RUNNING'); e.scan.terminal.emit(old,'COMPLETED',response)
    if req: e.bridge.enqueued.emit(result(req)); APP.processEvents()
    assert e.auto._active is current; balanced(e.auto)
    e.scan.finish(None,'CANCELLED',rid=e.scan.job); balanced(e.auto)
    e.clock[0]+=300; e.auto.tick(); balanced(e.auto); assert len(e.scan.calls)==3


def test_real_failed_to_start_releases_and_later_tick_retries(env,tmp_path):
    from covearchive.ui.scan_process import ScanController
    e=env; scan=ScanController(command_factory=lambda _: [str(tmp_path/'missing.exe')])
    auto=AutoArchive(scan,e.bridge,lambda:e.lib[0],lambda:e.settings,clock=lambda:e.clock[0]); events=[]
    scan.terminal.connect(lambda *args:events.append(args))
    try:
        auto.start(); e.clock[0]+=3600; auto.tick(); until(lambda:len(events)==1); balanced(auto)
        assert auto.status('owner/repo').state=='retrying'
        e.clock[0]+=300; auto.tick(); until(lambda:len(events)==2); balanced(auto)
    finally: auto.shutdown(); balanced(auto)


def test_window_close_retry_when_scan_shutdown_is_not_finished(env,rig,tmp_path,monkeypatch):
    e=env; bridge,sink=rig(); automatic=Scan(); window,manual=window_for(e,tmp_path,bridge,automatic)
    original=automatic.shutdown
    try:
        e.clock[0]+=3600; window.auto.tick(); balanced(window.auto)
        monkeypatch.setattr(automatic,'shutdown',lambda:False); window.close()
        assert window.isVisible() and not bridge.shutting_down; balanced(window.auto)
        monkeypatch.setattr(automatic,'shutdown',original); window.close(); until_closed(lambda:bridge.closed and not window.isVisible(),10)
        balanced(window.auto); assert not sink.enqueues
    finally:
        monkeypatch.setattr(automatic,'shutdown',original); window.close(); until_closed(lambda:bridge.closed,10)


def test_real_runner_two_checks_never_redownload_completed_assets(env,rig,tmp_path):
    from covearchive.archive.download import ArchiveResult, ArchiveStatus
    e=env; calls=[]; root=tmp_path/'archive'; root.mkdir()
    c=catalog(release(assets=[asset(i,size=1) for i in (1,2)]))
    def archive(repo,release,asset,archive_root,**kw):
        calls.append(asset.id); (root/asset.name.replace('.zip',f'-{asset.id}.zip')).write_bytes(bytes([asset.id]))
        return ArchiveResult(ArchiveStatus.ARCHIVED,None,asset.id,release.id)
    bridge,sink=rig(run=True,catalog=lambda _:c,archive=archive); scan=Scan()
    auto=AutoArchive(scan,bridge,lambda:e.lib[0],lambda:e.settings,clock=lambda:e.clock[0])
    try:
        auto.start(); e.clock[0]+=3600; auto.tick(); balanced(auto); success(scan,c)
        until(lambda:len(bridge._store.list_jobs())==2 and all(j.state==S.COMPLETED for j in bridge._store.list_jobs()) and not bridge.run_active and auto.status('owner/repo').state=='waiting')
        balanced(auto); before={p.name:p.read_bytes() for p in root.iterdir()}
        e.clock[0]+=3600; auto.tick(); balanced(auto); success(scan,c)
        until(lambda:len(sink.enqueues)==2 and auto.status('owner/repo').state=='waiting'); balanced(auto)
        assert sorted(calls)==[1,2] and {p.name:p.read_bytes() for p in root.iterdir()}==before
    finally: auto.shutdown(); balanced(auto)


def test_tick_self_repairs_orphaned_schedule_checking(env):
    e=env; e.auto.start(); balanced(e.auto); e.clock[0]+=3600
    e.auto.schedule.started('owner/repo'); assert e.auto._active is None and e.auto.schedule.checking=='owner/repo'
    e.auto.tick(); balanced(e.auto); assert len(e.scan.calls)==1
    e.scan.finish(None,'CANCELLED'); balanced(e.auto); e.clock[0]+=300; e.auto.tick(); balanced(e.auto)
    assert len(e.scan.calls)==2


@pytest.mark.parametrize('failure',['save','readonly'])
@pytest.mark.parametrize('missing',['rules','root'])
def test_window_unsaved_rules_or_folder_never_enable_auto(env,rig,tmp_path,monkeypatch,failure,missing):
    from test_ui_library import auto_dialog, select
    e=env; e.lib[0]=e.lib[0].set_auto_archive(e.repo.identity,None)
    if missing=='rules': e.settings['repositories'].clear()
    else: e.settings['archive_root']=None
    bridge,sink=rig(); automatic=Scan(); window,manual=window_for(e,tmp_path,bridge,automatic)
    manual_requests=[]
    try:
        if failure=='save': monkeypatch.setattr(window.preferences,'save',lambda _: 'Save failed')
        else: window.preferences.writable=False
        window.url.setText(e.repo.url); window.scan.click(); success(manual)
        if missing=='rules': window.save_rules.click(); assert 'owner/repo' in window.settings['repositories']
        else:
            window.root.setText(str(tmp_path/'new-archive')); window._select(False)
            monkeypatch.setattr(bridge,'enqueue',lambda req:manual_requests.append(req) or True)
            window.archive_selected.click(); assert window.settings['archive_root']==str(tmp_path/'new-archive')
        panel=window.library_panel; before=panel.store.path.read_bytes(); select(panel,e.repo.identity)
        auto_dialog(monkeypatch); panel.auto_button.click()
        assert panel.store.path.read_bytes()==before and panel.library.repositories[0].auto_archive is None
        assert 'saved' in panel.status.text().lower()
        e.clock[0]+=86400; window.auto.tick(); assert not automatic.calls and not sink.enqueues
        assert len(manual_requests)==(missing=='root')
    finally:
        window.enqueue_pending=False; window.close(); until_closed(lambda:bridge.closed and not window.isVisible(),10)


@pytest.mark.parametrize('size',[(1180,720),(820,520)])
def test_window_auto_column_font_measurements(env,rig,tmp_path,size):
    from PySide6.QtCore import Qt
    e=env; bridge,sink=rig(); window,manual=window_for(e,tmp_path,bridge,Scan())
    try:
        window.auto.schedule.succeeded('owner/repo',''); window.auto.changed.emit()
        window.tabs.setCurrentIndex(2); window.resize(*size); APP.processEvents(); panel=window.library_panel
        for column in (4,5):
            index=panel.model.index(0,column); text=panel.model.data(index); width=panel.table.columnWidth(column)
            required=panel.table.fontMetrics().horizontalAdvance(text)
            print(f'window {window.width()}x{window.height()}: column {column}, width={width}, text={required}')
            assert window.width()==size[0] and width>=required and panel.model.data(index,Qt.ToolTipRole)==text
    finally: window.close(); until_closed(lambda:bridge.closed and not window.isVisible(),10)


@pytest.mark.parametrize('size',[(820,520),(1180,720)])
def test_library_identifying_columns_scroll_instead_of_collapsing(env,rig,tmp_path,size):
    from PySide6.QtCore import Qt
    e=env; e.lib[0]=Library().add_repository('https://github.com/Fictional/Tool').set_auto_archive('github:fictional/tool',1)
    e.settings['repositories']={'fictional/tool':e.settings['repositories']['owner/repo'].copy()}
    bridge,sink=rig(); window,manual=window_for(e,tmp_path,bridge,Scan())
    try:
        window.auto.schedule.succeeded('fictional/tool','')
        window.auto._time=lambda value:'2027-01-15 01:00' if value==e.clock[0] else '2027-01-15 02:00'
        window.auto.changed.emit(); window.tabs.setCurrentIndex(2); window.resize(*size); APP.processEvents()
        panel=window.library_panel; table=panel.table; metrics=table.fontMetrics()
        widths=[table.columnWidth(i) for i in range(6)]; cells=[panel.model.data(panel.model.index(0,i)) for i in range(6)]
        assert cells==['Fictional/Tool','Fictional/Tool','Unsorted','Active','Every 1 hour','Last 2027-01-15 01:00 / Next 2027-01-15 02:00']
        print(f'window {window.width()}x{window.height()}: widths={widths}, text={[metrics.horizontalAdvance(t) for t in cells]}, horizontal scrollbar={table.horizontalScrollBar().isVisible()}')
        floor=table.horizontalHeader().minimumSectionSize()
        assert window.width()==size[0] and floor>=metrics.horizontalAdvance('Fictional/Tool')+6
        assert all(width>=floor for width in widths[:3])
        for column in (4,5):
            assert widths[column]>=metrics.horizontalAdvance(cells[column])
            assert panel.model.data(panel.model.index(0,column),Qt.ToolTipRole)==cells[column]
        assert table.horizontalScrollBar().isVisible()==(size[0]==820)
        assert all(width>=metrics.horizontalAdvance(text)+6 for width,text in zip(widths,cells))
        assert all(widths[i]>=metrics.horizontalAdvance(panel.model.headerData(i,Qt.Horizontal))+6 for i in range(6))
        if size[0]==820:
            table.horizontalScrollBar().setValue(table.horizontalScrollBar().maximum()); APP.processEvents()
            assert table.columnViewportPosition(5)+widths[5]<=table.viewport().width()
    finally: window.close(); until_closed(lambda:bridge.closed and not window.isVisible(),10)


@pytest.mark.parametrize('paginated',[False,True])
def test_worker_rate_limit_outcomes_block_all_watches_and_share_ladder(env,paginated):
    from covearchive.github.client import IncompleteScanError, RateLimitError, NetworkError
    e=env; e.lib[0]=e.lib[0].add_repository('https://github.com/z/other').set_auto_archive('github:z/other',1)
    e.settings['repositories']['z/other']=e.settings['repositories']['owner/repo'].copy()
    e.auto.start(); due(e); balanced(e.auto)
    cause=RateLimitError(kind='primary',status=403,retry_after_seconds=120,retry_at=int(e.clock[0]+120),reset_at=None,remaining=0)
    failure=wire.decode_response(wire.encode_error(RID,IncompleteScanError(2,'error',cause) if paginated else cause),RID)
    delay=60 if paginated else 120
    if paginated: assert failure.detail==dict(pages_completed=2,reason='error',cause_code='rate_limited')
    e.scan.finish(failure,'FAILED'); balanced(e.auto); status=e.auto.status('owner/repo')
    assert status.state=='retrying' and 'GitHub rate limit reached; checks resume' in status.message
    assert e.auto.schedule.blocked_until==e.clock[0]+delay and status.next_check==e.clock[0]+delay
    e.auto.tick(); balanced(e.auto); assert len(e.scan.calls)==1
    e.clock[0]+=delay-1; e.auto.tick(); balanced(e.auto); assert len(e.scan.calls)==1
    e.clock[0]+=1; e.auto.tick(); balanced(e.auto); assert e.scan.calls[-1]=='https://github.com/z/other'
    e.scan.finish(None,'CANCELLED'); balanced(e.auto); e.auto.tick(); balanced(e.auto)
    assert e.scan.calls[-1]==e.repo.url
    e.scan.finish(wire.decode_response(wire.encode_error(RID,NetworkError()),RID),'FAILED'); balanced(e.auto)
    assert e.auto.status('owner/repo').next_check==e.clock[0]+900


@pytest.mark.parametrize('cause_code',['network','timeout','server_error',None,True,0,['rate_limited'],{'code':'rate_limited'}])
def test_incomplete_non_rate_causes_leave_other_watches_unblocked(env,cause_code):
    from covearchive.github.client import IncompleteScanError, NetworkError, RequestTimeoutError, TransientServerError
    causes={'network':NetworkError(),'timeout':RequestTimeoutError(),'server_error':TransientServerError(status=503)}
    valid=type(cause_code) is str and cause_code in causes
    error=IncompleteScanError(2,'error',causes[cause_code] if valid else None)
    failure=wire.decode_response(wire.encode_error(RID,error),RID)
    if not valid and cause_code is not None: failure=replace(failure,detail={**failure.detail,'cause_code':cause_code})
    e=env; e.lib[0]=e.lib[0].add_repository('https://github.com/z/other').set_auto_archive('github:z/other',1)
    e.settings['repositories']['z/other']=e.settings['repositories']['owner/repo'].copy()
    e.auto.start(); due(e); e.scan.finish(failure,'FAILED'); balanced(e.auto)
    assert e.auto.status('owner/repo').next_check==e.clock[0]+300 and e.auto.schedule.blocked_until==0
    assert 'incomplete' in e.auto.status('owner/repo').message and 'rate limit' not in e.auto.status('owner/repo').message
    e.auto.tick(); balanced(e.auto); assert e.scan.calls[-1]=='https://github.com/z/other'


@pytest.mark.parametrize('state',['TIMED_OUT','CANCELLED'])
def test_non_failed_terminal_does_not_promote_nested_rate_payload(env,state):
    from covearchive.github.client import IncompleteScanError, RateLimitError
    e=env; e.auto.start(); due(e)
    cause=RateLimitError(kind='primary',status=403,retry_after_seconds=120,retry_at=int(e.clock[0]+120))
    failure=wire.decode_response(wire.encode_error(RID,IncompleteScanError(2,'error',cause)),RID)
    e.scan.finish(failure,state); balanced(e.auto)
    assert e.auto.schedule.blocked_until==0 and e.auto.status('owner/repo').next_check==e.clock[0]+300
    assert 'rate limit' not in e.auto.status('owner/repo').message


def test_enqueue_rate_error_already_uses_bounded_global_policy(env):
    e=env; e.auto.start(); due(e); success(e.scan); request=e.bridge.calls[-1]
    e.bridge.enqueued.emit(result(request,error='rate_limited')); APP.processEvents(); balanced(e.auto)
    assert e.auto.schedule.blocked_until==e.clock[0]+60
    assert 'GitHub rate limit reached' in e.auto.status('owner/repo').message



@pytest.mark.parametrize('mode',['redirect','first','later'])
def test_later_page_unauthorized_uses_authentication_guidance(env,mode):
    from test_scan_process import later_401_script, command
    from pathlib import Path
    import subprocess,sys,json
    token='ghp_'+'A'*36
    request=wire.encode_request_with_token(RID,'https://github.com/Owner/Repo',token)
    child=subprocess.run([sys.executable,'-I','-B','-c',later_401_script(mode)],input=request,capture_output=True,timeout=3)
    assert child.returncode==0
    failure=wire.decode_response(child.stdout,RID)
    e=env; e.auto.start(); due(e); e.scan.finish(failure,'FAILED')
    assert ('GitHub denied access' if mode=='redirect' else 'GitHub rejected the access token') in e.auto.status('owner/repo').message



@pytest.mark.parametrize('code,flag,text',[('unauthorized',False,'GitHub denied access'),('not_found',True,'Repository not found')])
def test_unproven_unauthorized_is_access_denied(env,code,flag,text):
    e=env; e.auto.start(); due(e)
    e.scan.finish(wire.ScanFailure(RID,code,token_rejected=flag),'FAILED')
    assert text in e.auto.status('owner/repo').message
