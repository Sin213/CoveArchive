"""Offline Health dashboard, no queue or archive writes."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from dataclasses import replace
from uuid import uuid4

import pytest
from PySide6 import QtWidgets as W
from PySide6.QtCore import QObject, Signal, Qt
from covearchive.archive import availability as a
from covearchive.library.model import Library
from covearchive.scan_worker import ScanFailure
from covearchive.ui.library_store import LibraryStore
from covearchive.ui.library_panel import LibraryPanel
from covearchive.ui.health_panel import ReviewDialog
from test_archive_availability import current, upstream
from test_archive_health import REPO, archive, asset_path, snapshot
from test_scan_process import APP


class Worker(QObject):
    state_changed = Signal(str, str)
    terminal = Signal(str, str, object)
    def __init__(self): super().__init__(); self.calls = []; self.job = None; self.cancels = 0
    def start(self, repo, root):
        assert self.job is None
        self.job = uuid4().hex; self.calls.append((repo, root)); self.state_changed.emit(self.job, 'RUNNING'); return True
    def finish(self, status, state='COMPLETED'):
        rid = self.job; self.job = None; self.terminal.emit(rid, state, status)
    def cancel(self): self.cancels += 1
    def shutdown(self, *a, **kw):
        if self.job: self.finish(None, 'CANCELLED')
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'LOCALAPPDATA'):
        monkeypatch.setenv(key, str(tmp_path / key))
    root, project, records = archive(tmp_path)
    store = LibraryStore(tmp_path / 'library.json')
    lib = Library().add_repository('https://github.com/Owner/Repo')
    assert store.save(lib) is None
    worker = Worker()
    panel = LibraryPanel(store, health_controller=worker, archive_root=lambda: str(root))
    panel.resize(820, 520); panel.show(); panel.view_selector.setCurrentIndex(1); APP.processEvents()
    yield panel, panel.health, worker, root, project, records
    panel.close(); APP.processEvents()


def checked(root, scan=None):
    return a.check_repository(str(root), REPO, scan=lambda _: scan or upstream(current()))


def test_no_auto_check_empty_and_library_integration(env):
    panel, h, worker, *_ = env
    assert not worker.calls and h.table.rowCount() == 1
    assert h.table.item(0, 5).text() == 'Not checked'
    assert panel.view_selector.count() == 2
    panel.library = Library(); panel.refresh(); APP.processEvents()
    assert h.table.rowCount() == 0 and not h.check_all.isEnabled()
    assert 'No repositories' in h.progress.text()


def test_one_repo_verified_details_and_no_mutation(env):
    panel, h, worker, root, *_ = env
    before = snapshot(root); library_bytes = panel.store.path.read_bytes()
    h.table.selectRow(0); h.check_selected.click()
    assert len(worker.calls) == 1 and h.cancel_button.isEnabled()
    worker.finish(checked(root)); APP.processEvents()
    assert h.results['github:owner/repo'].status.headline == 'up_to_date'
    assert h.table.item(0, 5).text() == 'Up to date'
    assert 'Verified assets: 1' in h.details.toPlainText()
    assert 'Local verification complete: Yes' in h.details.toPlainText()
    assert snapshot(root) == before and panel.store.path.read_bytes() == library_bytes


def test_check_all_sequential_snapshot_progress_rate_limit(env):
    panel, h, worker, root, *_ = env
    panel.library = panel.library.add_repository('https://github.com/o/second').add_repository('https://github.com/o/third'); panel.refresh()
    h.check_all.click(); assert len(worker.calls) == 1
    worker.finish(checked(root)); APP.processEvents(); assert len(worker.calls) == 2
    second = worker.calls[-1][0]
    from covearchive.providers import parse_repository_url
    limited = a.assess(parse_repository_url(second.url), ScanFailure('', 'rate_limited'), None)
    worker.finish(limited); APP.processEvents()
    assert len(worker.calls) == 2 and not h.running
    assert '1 completed' in h.progress.text() and '1 inconclusive' in h.progress.text()
    assert 'rate limit' in h.progress.text().lower()
    assert h.table.item(2, 5).text() == 'Not checked'


def test_cancel_keeps_results_and_drops_late_events(env):
    panel, h, worker, root, *_ = env
    panel.library = panel.library.add_repository('https://github.com/o/second'); panel.refresh()
    h.check_all.click(); worker.finish(checked(root)); APP.processEvents()
    rid = worker.job; h.cancel_button.click(); assert worker.cancels == 1
    worker.finish(None, 'CANCELLED'); APP.processEvents()
    worker.terminal.emit(rid, 'COMPLETED', checked(root)); APP.processEvents()
    assert len(h.results) == 1 and not h.running
    assert h.table.item(1, 5).text() == 'Not checked'


def test_removed_readded_and_foreign_identity_results_ignored(env):
    panel, h, worker, root, *_ = env
    h.check_all.click(); rid = worker.job
    worker.terminal.emit('foreign', 'COMPLETED', checked(root)); assert not h.results
    panel.library = Library(); panel.refresh()
    panel.library = panel.library.add_repository('https://github.com/Owner/Repo'); panel.refresh()
    worker.finish(checked(root)); APP.processEvents(); assert not h.results
    h.check_all.click()
    bad = replace(checked(root), repository=replace(REPO, owner='Other'))
    worker.finish(bad); APP.processEvents()
    assert h.results['github:owner/repo'].status.headline != 'up_to_date'


@pytest.mark.parametrize('kind,headline,detail', [
    ('new', 'Update available', 'New upstream release'),
    ('damaged', 'Needs attention', 'Damaged assets: 1'),
    ('missing', 'Needs attention', 'Missing assets: 1'),
    ('unavailable', 'Unavailable or inaccessible', 'private, renamed'),
    ('network', 'Inconclusive', 'network failure'),
    ('incomplete', 'Inconclusive', 'Local verification complete: No'),
])
def test_status_and_plain_details(env, kind, headline, detail):
    _, h, worker, root, project, records = env
    scan = upstream(current())
    if kind == 'new': scan = upstream(current(id=2, tag_name='v2'))
    if kind == 'damaged': asset_path(project, records).write_bytes(b'bad')
    if kind == 'missing': asset_path(project, records).unlink()
    if kind in ('unavailable', 'network'): scan = ScanFailure('', 'not_found' if kind == 'unavailable' else 'network')
    if kind == 'incomplete':
        result = a.check_repository(str(root), REPO, scan=lambda _: scan,
                                    limits=a.AuditLimits(max_asset_bytes=1))
    else: result = checked(root, scan)
    h.table.selectRow(0); h.check_all.click(); worker.finish(result); APP.processEvents()
    assert h.table.item(0, 5).text() == headline
    assert detail in h.details.toPlainText()


def test_review_selection_warnings_browse_and_no_batch_queue(env):
    panel, h, worker, root, *_ = env
    h.check_all.click(); worker.finish(checked(root, upstream(current(id=2, tag_name='v2')))); APP.processEvents()
    dialog = ReviewDialog(h); opened = []; panel.open_requested.connect(opened.append)
    dialog.show(); APP.processEvents()
    assert dialog.table.rowCount() == 1
    assert not dialog.open_button.isEnabled()
    dialog.table.selectRow(0); APP.processEvents(); assert dialog.open_button.isEnabled()
    assert 'batch queue' in dialog.notice.text().lower()
    dialog.open_button.click(); assert opened == ['https://github.com/Owner/Repo']
    dialog.close()
    for state in ('tag_reused', 'changed_upstream'):
        status = checked(root)
        status = replace(status, stable=replace(status.stable, comparison=a.Comparison(state, a._COMPARISON_MESSAGES[state])))
        h.check_all.click(); worker.finish(status); APP.processEvents()
        dialog = ReviewDialog(h)
        assert 'Warning' in dialog.table.item(0, 2).text()
        assert 'replace' in dialog.table.item(0, 2).text()
        from covearchive.ui.health_panel import COMPARISONS
        assert COMPARISONS[state] in dialog.table.item(0, 2).text()
        dialog.close()


def test_stable_prerelease_separate_and_missing_prerequisites(env):
    panel, h, worker, root, *_ = env
    scan = upstream(current(), current(id=3, tag_name='v3-rc1', prerelease=True,
                                       published_at='2026-11-01T00:00:00Z'))
    h.check_all.click(); worker.finish(checked(root, scan)); APP.processEvents()
    assert h.table.item(0, 3).text() == 'v1'
    assert h.table.item(0, 4).text() == 'v3-rc1'
    assert h.table.item(0, 5).text() == 'Up to date'
    from covearchive.ui.health_panel import attention, review_reason
    assert attention(h.results['github:owner/repo'].status)
    assert 'Prerelease: New upstream release' in review_reason(h.results['github:owner/repo'].status)
    repo = panel.library.repositories[0]
    assert 'No saved rules' in h.prerequisites(repo) and 'No saved archive destination' in h.prerequisites(repo)


def test_missing_root_is_not_an_invented_missing_asset_count(env):
    _, h, worker, root, *_ = env
    status = a.check_repository(str(root / 'absent'), REPO, scan=lambda _: upstream(current()))
    h.table.selectRow(0); h.check_all.click(); worker.finish(status); APP.processEvents()
    assert 'Missing assets: Unknown' in h.details.toPlainText()
    assert 'Audit missing observations: 1' in h.details.toPlainText()


def test_review_revalidates_removed_repository_and_evidence(env):
    panel, h, worker, root, *_ = env
    h.check_all.click(); worker.finish(checked(root, upstream(current(id=2, tag_name='v2')))); APP.processEvents()
    dialog = ReviewDialog(h); dialog.table.selectRow(0); opened = []; h.open_requested.connect(opened.append)
    panel.library = Library(); panel.refresh(); dialog.open_button.click()
    assert not opened and not dialog.open_button.isEnabled()
    assert 'changed' in dialog.explanation.text()
    dialog.close()


def test_check_selected_snapshot_rename_and_add_during_check(env):
    panel, h, worker, root, *_ = env
    h.table.selectRow(0); h.check_selected.click()
    panel.library = panel.library.rename_repository('github:owner/repo', 'Renamed').add_repository('https://github.com/o/new')
    panel.refresh(); worker.finish(checked(root)); APP.processEvents()
    assert len(worker.calls) == 1 and h.results['github:owner/repo'].status.headline == 'up_to_date'
    assert h.table.item(0, 0).text() == 'Renamed' and h.table.item(1, 5).text() == 'Not checked'


@pytest.mark.parametrize('change', ['remove', 'add', 'import', 'replace'])
def test_library_changes_while_health_hidden_keep_identity_and_finish(env, change):
    from covearchive.providers import parse_repository_url
    from test_scan_process import until
    panel, h, worker, root, *_ = env
    panel.library = panel.library.add_repository('https://github.com/o/second'); panel.refresh()
    h.table.selectRow(1); h.check_selected.click()
    second = panel.library.repositories[1]
    panel.view_selector.setCurrentIndex(0); APP.processEvents()
    if change == 'remove': panel.library = panel.library.remove_repositories(['github:owner/repo'])
    elif change == 'add': panel.library = panel.library.add_repository('https://github.com/o/third')
    elif change == 'import':
        panel.library = Library().add_repository(second.url).add_repository('https://github.com/o/imported')
    else: panel.library = Library().add_repository('https://github.com/o/replacement')
    panel.refresh()
    result = a.assess(parse_repository_url(second.url), ScanFailure('', 'not_found'), None)
    worker.finish(result); until(lambda: not h.running)
    assert h._active is None and not h._pending
    panel.view_selector.setCurrentIndex(1); APP.processEvents()
    assert [h.table.item(i, 0).text() for i in range(h.table.rowCount())] == [r.display_name for r in panel.library.repositories]
    assert [r.identity for r in h._selected()] == ([] if change == 'replace' else [second.identity])
    assert set(h.results) == (set() if change == 'replace' else {second.identity})
    assert h.check_all.isEnabled()


def test_no_queue_path_or_auto_archive_changes(env, monkeypatch):
    from covearchive.ui.archive_bridge import ArchiveBridge
    panel, h, worker, root, *_ = env
    monkeypatch.setattr(ArchiveBridge, 'enqueue', lambda *a: pytest.fail('Health queued work'))
    before = panel.store.path.read_bytes()
    h.check_all.click(); worker.finish(checked(root, upstream(current(id=2, tag_name='v2')))); APP.processEvents()
    h.update_all.click(); APP.processEvents()
    assert h._review_dialog.isVisible() and panel.store.path.read_bytes() == before
    assert all(repo.auto_archive is None for repo in panel.library.repositories)
    assert not h._review_dialog.queue_button.isEnabled()
    h._review_dialog.close()


def test_main_window_four_tabs_no_scan_and_worker_shutdown(tmp_path, monkeypatch):
    from covearchive.ui.window import MainWindow
    from covearchive.ui.archive_bridge import ArchiveBridge
    from covearchive.ui.preferences import PreferenceStore
    from test_ui_window import FakeController
    from test_scan_process import ROOT, command, until, gone
    for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'LOCALAPPDATA'):
        monkeypatch.setenv(key, str(tmp_path / key))
    store = LibraryStore(tmp_path / 'library.json')
    store.save(Library().add_repository('https://github.com/Owner/Repo'))
    manual = FakeController(); bridge = ArchiveBridge(tmp_path / 'queue.db')
    window = MainWindow(ROOT / 'run_covearchive.py', manual, bridge=bridge,
                        preferences=PreferenceStore(tmp_path / 'prefs.json'), library=store)
    window.show(); APP.processEvents()
    h = window.library_panel.health
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ['Browse', 'Queue', 'Library', 'Discover']
    assert not manual.calls and h.controller.job is None
    window.tabs.setCurrentIndex(2); window.library_panel.view_selector.setCurrentIndex(1)
    h.controller.command_factory = command('import time;time.sleep(60)')
    h.check_all.click(); until(lambda: h.controller.job.process.processId() > 0)
    pid = h.controller.job.process.processId()
    window.close(); until(lambda: window._window_closed, seconds=5)
    assert gone(pid) and h.controller.job is None and not h.running


@pytest.mark.parametrize('phase', ['scan', 'audit'])
def test_main_window_shutdown_reaps_unconfirmed_batch(tmp_path, monkeypatch, phase):
    import sqlite3
    from covearchive.ui.window import MainWindow
    from covearchive.ui.archive_bridge import ArchiveBridge
    from covearchive.ui.preferences import PreferenceStore
    from covearchive.ui.health_panel import Checked
    from test_ui_window import FakeController
    from test_scan_process import ROOT, command, until, gone
    for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'LOCALAPPDATA'):
        monkeypatch.setenv(key, str(tmp_path / key))
    root, _, _ = archive(tmp_path); before = snapshot(root)
    store = LibraryStore(tmp_path / 'library.json'); store.save(Library().add_repository('https://github.com/Owner/Repo'))
    library_before = store.path.read_bytes(); prefs = PreferenceStore(tmp_path / 'prefs.json')
    settings, _, _ = prefs.load()
    settings['archive_root'] = str(root)
    settings['repositories'] = {'owner/repo': dict(include=[], exclude=[], extensions=['.zip'], prereleases=False)}
    prefs.save(settings)
    bridge = ArchiveBridge(tmp_path / 'queue.db')
    window = MainWindow(ROOT / 'run_covearchive.py', FakeController(), bridge=bridge, preferences=prefs, library=store)
    window.show(); until(lambda: bridge.is_open)
    h = window.library_panel.health; repo = h._repos[0]
    status = checked(root, upstream(current(id=2, tag_name='v2')))
    h.results[repo.identity] = evidence = Checked(status, 'now', str(root))
    blocked = 'import sys,time;sys.stdin.buffer.readline();time.sleep(60)'
    catalog_reply = (f'import sys,json,os;sys.path[:0]=[{str(ROOT / "src")!r},{str(ROOT / "tests")!r}];'
        'from covearchive.scan_worker import encode_success;from test_archive_availability import current,upstream;'
        'request=json.loads(sys.stdin.buffer.readline());'
        'sys.stdout.buffer.write(encode_success(request["request_id"],upstream(current(id=2,tag_name="v2")).catalog));'
        'sys.stdout.buffer.flush();os._exit(0)')
    h.updates.scan.command_factory = command(blocked if phase == 'scan' else catalog_reply)
    h.updates.check.command_factory = command(blocked)
    assert h.updates.scan.auth is None and h.updates.check.auth is None
    h.updates.prepare([(repo.identity, evidence)], str(root))
    active = h.updates.scan if phase == 'scan' else h.updates.check
    until(lambda: active.job and active.job.process.processId())
    pid = active.job.process.processId(); window.close(); until(lambda: window._window_closed, seconds=5)
    assert gone(pid) and h.updates.scan.job is None and h.updates.check.job is None
    assert not h.updates.running and snapshot(root) == before and store.path.read_bytes() == library_before
    with sqlite3.connect(tmp_path / 'queue.db') as db: assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


def test_accepted_review_deleted_before_shutdown(env):
    from PySide6.QtCore import QCoreApplication, QEvent
    _, h, worker, root, *_ = env
    h.check_all.click(); worker.finish(checked(root, upstream(current(id=2, tag_name='v2')))); APP.processEvents()
    h.review(); dialog = h._review_dialog; dialog.accept()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete); APP.processEvents()
    assert h._review_dialog is None
    assert h.shutdown()


def test_keep_open_does_not_disable_health(env, tmp_path):
    from covearchive.ui.window import MainWindow
    from covearchive.ui.preferences import PreferenceStore
    from test_ui_window import WindowBridge, FakeController
    from test_scan_process import ROOT
    panel, *_ = env
    bridge = WindowBridge(); bridge.run_active = True
    window = MainWindow(ROOT / 'run_covearchive.py', FakeController(), bridge=bridge,
                        library=panel.store, preferences=PreferenceStore(tmp_path / 'prefs.json'))
    window._confirm_close = lambda: 'Keep Open'; window.show(); APP.processEvents()
    window.close(); APP.processEvents()
    assert window.isVisible() and not window.library_panel.health._stopped
    bridge.closed = True; window.close()


@pytest.mark.parametrize('size', [(820, 520), (1180, 720)])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_health_geometry_accessibility(env, size, theme):
    from covearchive.ui.theme import ThemeTracker
    panel, h, *_ = env
    tracker = ThemeTracker(panel, theme); panel.resize(*size); APP.processEvents()
    assert panel.minimumSizeHint().width() <= 820 and panel.minimumSizeHint().height() <= 520
    for button in (h.check_all, h.check_selected, h.cancel_button, h.review_button):
        assert button.accessibleName() and h.rect().contains(button.mapTo(h, button.rect().center()))
    tracker.disconnect()
