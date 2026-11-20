"""Offline native Discover interactions, identity, handoff and real local workers."""

import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest
from PySide6.QtCore import QObject, QPoint, QProcess, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from covearchive.discover.models import SearchPage, parse_github_repository
from covearchive.discover.worker import DiscoverFailure, DiscoverSuccess, ERROR_CODES
from covearchive.ui.discover_panel import DiscoverPanel, KINDS, SORTS
from covearchive.ui.discover_process import DiscoverController
from covearchive.ui.library_store import LibraryStore
from covearchive.ui.preferences import PreferenceStore
from covearchive.ui.window import MainWindow
from test_ui_window import FakeController as BrowseController, WindowBridge
from test_ui_discover_process import command, gone, until

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


class FakeController(QObject):
    state_changed = Signal(str, str)
    terminal = Signal(str, str, object)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.job = None
        self.serial = 0
        self.reject = False
        self.shutdown_calls = 0
        self.cancel_calls = 0
        self.on_start = None

    @property
    def active_request_id(self):
        return self.job

    def start(self, text, kind='all', sort='relevance', page=1, per_page=30):
        if self.job is not None or self.reject:
            return False
        self.serial += 1
        self.job = f'{self.serial:032x}'
        self.calls.append((text, kind, sort, page, per_page))
        self.state_changed.emit(self.job, 'RUNNING')
        if self.on_start:
            self.on_start()
        return True

    def finish(self, page=None, *, state='COMPLETED', code='network', detail=None):
        rid, self.job = self.job, None
        if state == 'COMPLETED':
            result = DiscoverSuccess(rid, page or result_page())
        elif state == 'FAILED':
            result = DiscoverFailure(rid, code, message='<b>secret upstream</b>', detail=detail)
        else:
            result = None
        self.terminal.emit(rid, state, result)

    def cancel(self):
        self.cancel_calls += 1
        self.state_changed.emit(self.job, 'STOPPING')

    def shutdown(self, timeout_ms=3000):
        self.shutdown_calls += 1
        if self.job:
            self.finish(state='CANCELLED')
        return True


def record(number=1, name='MaskRecomp', **extra):
    data = dict(id=number, owner={'login': 'Example'}, name=name,
                full_name=f'Example/{name}', html_url=f'https://github.com/Example/{name}',
                url=f'https://api.github.com/repos/Example/{name}',
                private=False, visibility='public', disabled=False,
                description='A native port of a game', topics=['recomp'],
                stargazers_count=123, pushed_at='2026-01-02T03:04:05Z')
    data.update(extra)
    return parse_github_repository(data)


def result_page(*records, page=1, has_next=False, **extra):
    values = dict(results=records, page=page, per_page=30,
                  total_count=len(records) + (30 if has_next else 0),
                  retrievable_total=len(records) + (30 if has_next else 0),
                  incomplete_results=False, dropped=0, duplicates=0,
                  has_next_page=has_next)
    values.update(extra)
    return SearchPage(**values)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    windows = []
    original_font = QFont(APP.font())

    def make(controller=None, *, scale=1, browse_controller=None):
        font = QFont(original_font)
        font.setPointSizeF(font.pointSizeF() * scale)
        APP.setFont(font)
        discover = controller if controller is not None else FakeController()
        bridge = WindowBridge()
        browse = browse_controller if browse_controller is not None else BrowseController()
        window = MainWindow(ROOT / 'run_covearchive.py', browse, bridge=bridge,
                            preferences=PreferenceStore(tmp_path / 'prefs.json'),
                            library=LibraryStore(tmp_path / 'library.json'),
                            discover_controller=discover)
        windows.append(window)
        window.show()
        window.activateWindow()
        window._navigate(3)
        APP.processEvents()
        return window, window.discover_panel, discover

    yield make
    for window in windows:
        window.bridge.closed = True
        window.close()
        until(lambda: window._window_closed)
        window.deleteLater()
    APP.processEvents()
    APP.setFont(original_font)


def search(panel, controller, *records, **page_options):
    panel.search.setText("majora's mask")
    panel.search_button.click()
    controller.finish(result_page(*records, **page_options))
    APP.processEvents()


def bounds(widget, ancestor):
    return widget.rect().translated(widget.mapTo(ancestor, QPoint(0, 0)))


def test_four_tabs_and_shortcuts_without_implicit_search(rig):
    window, panel, controller = rig()
    assert [window.tabs.tabText(i) for i in range(4)] == ['Browse', 'Queue', 'Library', 'Discover']
    assert window.nav.count() == window.tabs.count() == 4
    assert controller.parent() is window
    assert len(window.findChildren(DiscoverPanel)) == 1
    assert panel.search.hasFocus() and not controller.calls
    for index, key in enumerate((Qt.Key_1, Qt.Key_2, Qt.Key_3, Qt.Key_4)):
        QTest.keyClick(APP.focusWidget() or window, key, Qt.ControlModifier)
        APP.processEvents()
        assert window.tabs.currentIndex() == window.nav.currentIndex() == index
        assert f'Ctrl+{index + 1}' in window.nav.tabToolTip(index)
    assert panel.search.hasFocus()
    QTest.keyClicks(panel.search, "majora's mask")
    APP.processEvents()
    assert not controller.calls
    for index in (0, 3, 1, 2, 3):
        window.nav.setCurrentIndex(index)
        assert window.tabs.currentIndex() == index
        window.tabs.setCurrentIndex(3 - index)
        assert window.nav.currentIndex() == 3 - index
    window._navigate(3)
    QTest.keyClick(panel.search, Qt.Key_L, Qt.ControlModifier)
    APP.processEvents()
    assert window.tabs.currentIndex() == 0 and window.url.hasFocus()
    assert not controller.calls and not window.controller.calls


def test_keyboard_only_search_selection_and_browse_scan(rig, monkeypatch):
    window, panel, controller = rig()
    writes = []
    monkeypatch.setattr(window.bridge, 'enqueue', lambda *_: writes.append('enqueue'))
    monkeypatch.setattr(window.auto, 'sync', lambda *_: writes.append('auto'))
    monkeypatch.setattr(window.library_store, 'save', lambda *_: writes.append('library'))
    preferences = window.preferences.path
    QTest.keyClick(panel.search, Qt.Key_4, Qt.ControlModifier)
    QTest.keyClicks(panel.search, "majora's mask")
    QTest.keyClick(panel.search, Qt.Key_Return)
    assert controller.calls == [("majora's mask", 'all', 'relevance', 1, 30)]
    assert not window.controller.calls
    controller.finish(result_page(record(), record(2, 'OtherRecomp')))
    APP.processEvents()
    panel.search.setFocus()
    QTest.keyClick(panel.search, Qt.Key_Down)
    assert panel.results.hasFocus() and panel.results.currentRow() == 0
    QTest.keyClick(panel.results, Qt.Key_Down)
    assert panel.results.currentRow() == 1 and 'OtherRecomp' in panel.details.toPlainText()
    QTest.keyClick(panel.results, Qt.Key_Up)
    QTest.keyClick(panel.results, Qt.Key_Return)
    assert window.controller.calls == ['https://github.com/Example/MaskRecomp']
    assert window.active_id is not None and window.tabs.currentIndex() == 0
    assert not writes and not window.bridge.requests and not preferences.exists()
    assert not window.history_controller.running


def test_enter_in_search_never_opens_selected_result(rig):
    window, panel, controller = rig()
    search(panel, controller, record())
    panel.results.setCurrentRow(0)
    panel.search.setFocus()
    QTest.keyClick(panel.search, Qt.Key_Return)
    assert len(controller.calls) == 2 and not window.controller.calls
    assert panel.active_id is not None


@pytest.mark.parametrize('kind_index', range(5))
@pytest.mark.parametrize('sort_index', range(3))
def test_category_sort_mapping_and_explicit_reset(rig, kind_index, sort_index):
    _, panel, controller = rig()
    search(panel, controller, record(), has_next=True)
    panel.next.click()
    controller.finish(result_page(record(), page=2))
    panel.category.setCurrentIndex(kind_index)
    panel.sort.setCurrentIndex(sort_index)
    assert len(controller.calls) == 2
    panel.search_button.click()
    assert controller.calls[-1] == ("majora's mask", KINDS[kind_index][1], SORTS[sort_index][1], 1, 30)


def test_paging_and_retry_use_snapshots_not_live_widgets(rig):
    _, panel, controller = rig()
    panel.category.setCurrentIndex(2)
    panel.sort.setCurrentIndex(1)
    search(panel, controller, record(), has_next=True)
    panel.search.setText('new text')
    panel.category.setCurrentIndex(4)
    panel.sort.setCurrentIndex(2)
    panel.next.click()
    assert controller.calls[-1] == ("majora's mask", 'recomps', 'stars', 2, 30)
    panel.search.setText('edited while running')
    controller.finish(result_page(record(), page=2, has_next=True))
    assert "majora's mask" in panel.query_label.text()
    panel.previous.click()
    assert controller.calls[-1] == ("majora's mask", 'recomps', 'stars', 1, 30)
    controller.finish(state='FAILED')
    panel.search.setFocus()
    QTest.keyClick(panel.search, Qt.Key_F5)
    assert controller.calls[-1] == ("majora's mask", 'recomps', 'stars', 1, 30)
    controller.finish(result_page(record(), page=33, has_next=True,
                                  total_count=1200, retrievable_total=1000))
    assert not panel.next.isEnabled() and '1,000' in panel.status.text()


def test_empty_unknown_and_evidence_are_honest_plain_text(rig):
    _, panel, controller = rig()
    search(panel, controller)
    assert 'No matches' in panel.status.text()
    assert not panel.open_button.isEnabled() and panel.results.count() == 0
    item = record(description='<b>x</b> A native port', fork=True, archived=True)
    search(panel, controller, item)
    panel.results.setCurrentRow(0)
    text = panel.details.toPlainText()
    assert '<b>x</b>' in text
    for value in ('topic — recomp', 'description — native port', 'weak name match — Recomp',
                  'Release availability: Not checked', 'Official status: Not verified',
                  'Original platform: Unknown', 'Supported host OS: Unknown',
                  'Fork: yes', 'Archived: yes', 'Stars do not establish safety'):
        assert value in text
    row = panel.results.item(0).text()
    for value in ('Example/MaskRecomp', 'GitHub', '123 stars', '2026-01-02', 'Fork', 'Archived'):
        assert value in row
    unknown = parse_github_repository(dict(id=2, owner={'login': 'Example'}, name='Plain',
        full_name='Example/Plain', html_url='https://github.com/Example/Plain',
        url='https://api.github.com/repos/Example/Plain', private=False, visibility='public', disabled=False))
    search(panel, controller, unknown)
    panel.results.setCurrentRow(0)
    assert 'Stars unknown' in panel.results.item(0).text()
    assert 'Last activity: unknown' in panel.results.item(0).text()
    assert 'Description: Unknown' in panel.details.toPlainText()
    assert 'Fork: unknown' in panel.details.toPlainText()
    assert 'None recorded' in panel.details.toPlainText()
    assert all(label.textFormat() == Qt.PlainText for label in panel.findChildren(QLabel))


def test_long_untrusted_strings_and_partial_records(rig):
    _, panel, controller = rig()
    panel.search.setText('<b>x</b>')
    panel.submit()
    controller.finish(result_page(record(description='<img src="https://evil.invalid/x">' + ' long' * 100),
        incomplete_results=True, dropped=3, duplicates=2, total_count=4, retrievable_total=4))
    panel.results.setCurrentRow(0)
    assert '<b>x</b>' in panel.query_label.text()
    assert '<img src=' in panel.details.toPlainText()
    assert 'Partial results' in panel.status.text()
    assert '1 invalid record omitted' in panel.status.text()
    assert '2 duplicate records omitted' in panel.status.text()
    assert panel.details.verticalScrollBar().maximum() >= 0


@pytest.mark.parametrize('dropped, duplicates, invalid_message, duplicate_message', [
    (1, 1, None, '1 duplicate record omitted.'),
    (5, 2, '3 invalid records omitted.', '2 duplicate records omitted.'),
    (1, 0, '1 invalid record omitted.', None),
])
def test_omitted_records_distinguish_invalid_from_duplicates(
        rig, dropped, duplicates, invalid_message, duplicate_message):
    _, panel, controller = rig()
    search(panel, controller, record(), dropped=dropped, duplicates=duplicates,
           total_count=1 + dropped, retrievable_total=1 + dropped)
    text = panel.status.text()
    for message, category in ((invalid_message, 'invalid record'), (duplicate_message, 'duplicate record')):
        if message is None:
            assert category not in text
        else:
            assert message in text


@pytest.mark.parametrize('detail, expected', [
    (None, None), ({}, None), ({'retry_after_seconds': 42}, '42 seconds'),
    ({'retry_at': 1800000000}, '2027-01-15'), ({'reset_at': 1800000000}, 'Limit resets at'),
    ({'retry_at': '<b>upstream</b>'}, None), ({'retry_after_seconds': '<b>secret</b>'}, None),
])
def test_rate_limit_only_shows_known_delay(rig, detail, expected):
    _, panel, controller = rig()
    panel.search.setText('mask')
    panel.submit()
    controller.finish(state='FAILED', code='rate_limited', detail=detail)
    text = panel.status.text()
    assert 'rate limit' in text and 'secret' not in text and 'upstream' not in text
    if expected:
        assert expected in text
    else:
        assert text == 'GitHub rate limit reached. Retry later.'
    assert panel.retry.isEnabled() and len(controller.calls) == 1


@pytest.mark.parametrize('code', sorted(ERROR_CODES | {'process', 'protocol', '<b>unknown</b>'}))
def test_fixed_actionable_failures_never_display_upstream_text(rig, code):
    _, panel, controller = rig()
    panel.search.setText('mask')
    panel.submit()
    controller.finish(state='FAILED', code=code, detail={'stderr': '<b>credential</b>'})
    assert 'secret' not in panel.status.text() and '<b>' not in panel.status.text()
    assert 'credential' not in panel.status.text()
    assert panel.retry.isEnabled() and len(controller.calls) == 1


@pytest.mark.parametrize('text', ['', 'OR', 'x' * 101, '\x00mask'])
def test_invalid_query_does_not_spawn_worker(rig, text):
    _, panel, controller = rig()
    panel.search.setText(text)
    panel.submit()
    assert not controller.calls and 'Enter a title' in panel.status.text()
    assert panel.search_button.isEnabled()


def test_cancel_waits_for_terminal_and_retry_is_explicit(rig):
    _, panel, controller = rig()
    panel.search.setText('mask')
    panel.submit()
    assert panel.cancel.isVisible() and panel.cancel.isEnabled() and panel.progress.isVisible()
    for widget in (panel.category, panel.sort, panel.search_button, panel.previous, panel.next, panel.retry):
        assert not widget.isEnabled()
    panel.search.setFocus()
    QTest.keyClick(panel.search, Qt.Key_Escape)
    assert controller.cancel_calls == 1 and 'Stopping' in panel.status.text()
    assert not panel.cancel.isEnabled() and not panel.search_button.isEnabled()
    panel.submit()
    assert len(controller.calls) == 1
    controller.state_changed.emit(controller.job, 'STOP_SLOW')
    assert 'longer than expected' in panel.status.text()
    controller.finish(state='CANCELLED')
    assert 'cancelled' in panel.status.text() and 'Failed' not in panel.status.text()
    assert panel.retry.isEnabled() and panel.search_button.isEnabled() and not panel.cancel.isVisible()
    panel.retry.click()
    assert len(controller.calls) == 2
    controller.finish(state='TIMED_OUT')
    assert 'timed out' in panel.status.text() and panel.retry.isEnabled()


def test_stale_state_and_terminal_cannot_overwrite_new_search(rig):
    _, panel, controller = rig()
    search(panel, controller, record(), has_next=True)
    old = f'{controller.serial:032x}'
    panel.results.setCurrentRow(0)
    panel.search.setText('new search')
    panel.submit()
    current = panel.active_id
    before = (panel.query_label.text(), panel.status.text(), panel.results.currentRow(), panel.page)
    controller.state_changed.emit(old, 'RUNNING')
    controller.state_changed.emit(old, 'STOP_SLOW')
    controller.terminal.emit(old, 'CANCELLED', None)
    controller.terminal.emit(old, 'COMPLETED', DiscoverSuccess(old, result_page()))
    assert panel.active_id == current
    assert before == (panel.query_label.text(), panel.status.text(), panel.results.currentRow(), panel.page)
    controller.finish(result_page(record(2, 'NewRecomp')))
    panel.results.setCurrentRow(0)
    after = (panel.query_label.text(), panel.status.text(), panel.results.currentRow(), panel.page)
    controller.terminal.emit(old, 'FAILED', DiscoverFailure(old, 'network'))
    assert after == (panel.query_label.text(), panel.status.text(), panel.results.currentRow(), panel.page)


def test_synchronous_completion_during_start_is_not_lost(rig):
    _, panel, controller = rig()
    controller.on_start = lambda: controller.finish(result_page(record()))
    panel.search.setText('mask')
    panel.submit()
    assert panel.active_id is None and panel.results.count() == 1
    assert panel.snapshot.text == 'mask' and '1 repositories' in panel.status.text()
    assert panel.search_button.isEnabled() and not panel.cancel.isVisible()


def test_rejected_start_preserves_display_and_does_not_invent_request(rig):
    _, panel, controller = rig()
    search(panel, controller, record(), has_next=True)
    panel.results.setCurrentRow(0)
    before = (panel.page, panel.query_label.text(), panel.results.currentRow(), panel.snapshot)
    controller.reject = True
    panel.search.setText('new')
    panel.submit()
    assert panel.active_id is None and 'could not start' in panel.status.text()
    assert before == (panel.page, panel.query_label.text(), panel.results.currentRow(), panel.snapshot)
    assert panel.search_button.isEnabled() and len(controller.calls) == 1


def test_browse_busy_keeps_url_and_discover_selection(rig):
    window, panel, controller = rig()
    window.url.setText('https://github.com/Existing/Scan')
    window.scan.click()
    window._navigate(3)
    search(panel, controller, record())
    panel.results.setCurrentRow(0)
    panel.open_button.click()
    assert 'Another Browse scan is running' in panel.status.text()
    assert window.tabs.currentIndex() == 3 and panel.results.currentRow() == 0
    assert window.url.text() == 'https://github.com/Existing/Scan'
    assert window.controller.calls == ['https://github.com/Existing/Scan']


def test_browse_handoff_accepts_synchronous_success(rig):
    from covearchive.scan_worker import ScanSuccess
    from test_scan_worker import RID, catalog, release

    class SynchronousBrowseController(BrowseController):
        def start(self, url):
            accepted = super().start(url)
            if accepted:
                self.finish(ScanSuccess(RID, replace(catalog(release()), repository=record().target)))
            return accepted

    browse = SynchronousBrowseController()
    window, panel, controller = rig(browse_controller=browse)
    search(panel, controller, record())
    panel.results.setCurrentRow(0)
    status = panel.status.text()
    panel.open_button.click()
    assert browse.calls == ['https://github.com/Example/MaskRecomp']
    assert window.tabs.currentIndex() == 0 and window.active_id is None and browse.job is None
    assert window.catalog is not None and window.catalog.repository == record().target
    assert panel.status.text() == status and 'could not start' not in panel.status.text()


def test_browse_handoff_reports_rejected_start(rig):
    class RejectingBrowseController(BrowseController):
        def start(self, url):
            self.calls.append(url)
            return False

    browse = RejectingBrowseController()
    window, panel, controller = rig(browse_controller=browse)
    search(panel, controller, record())
    panel.results.setCurrentRow(0)
    panel.open_button.click()
    assert browse.calls == ['https://github.com/Example/MaskRecomp']
    assert window.active_id is None and window.catalog is None
    assert 'Browse could not start' in panel.status.text()


@pytest.mark.parametrize('url', ['http://github.com/Example/MaskRecomp',
    'https://github.com/Other/MaskRecomp', 'https://github.com/Example/MaskRecomp/releases',
    'https://evil.invalid/Example/MaskRecomp', 'https://github.com/Example/MaskRecomp?x=y'])
def test_handoff_revalidates_exact_canonical_url(rig, url):
    window, panel, controller = rig()
    search(panel, controller, replace(record(), url=url))
    panel.results.setCurrentRow(0)
    panel.open_button.click()
    assert 'URL is invalid' in panel.status.text()
    assert not window.controller.calls and window.tabs.currentIndex() == 3


def test_real_worker_failed_to_start_and_crash(rig, tmp_path):
    controller = DiscoverController(command_factory=lambda _: [str(tmp_path / 'missing-worker')])
    _, panel, _ = rig(controller)
    panel.search.setText('mask')
    panel.submit()
    until(lambda: panel.active_id is None)
    assert 'could not start or stopped unexpectedly' in panel.status.text()
    assert panel.retry.isEnabled() and controller.job is None
    controller.command_factory = command('import sys;sys.exit(2)')
    panel.retry.click()
    until(lambda: panel.active_id is None)
    assert 'stopped unexpectedly' in panel.status.text() and panel.retry.isEnabled()


def test_window_close_reaps_active_discover_child_and_disconnects(rig):
    controller = DiscoverController(command_factory=command('import time;time.sleep(60)'))
    window, panel, _ = rig(controller)
    panel.search.setText('mask')
    panel.submit()
    process = controller.job.process
    until(lambda: process.state() == QProcess.Running)
    pid = process.processId()
    window.bridge.closed = True
    window.close()
    until(lambda: window._window_closed)
    assert controller.job is None and gone(pid) and panel._closed
    status = panel.status.text()
    controller.state_changed.emit('0' * 32, 'RUNNING')
    controller.terminal.emit('0' * 32, 'FAILED', DiscoverFailure('0' * 32, 'network'))
    assert panel.status.text() == status
    assert all(not shortcut.isEnabled() for shortcut in panel.shortcuts)


def test_about_to_quit_shutdown_connection_and_final_disconnect(rig):
    window, panel, controller = rig()
    shutdown = window.controller.shutdown
    window.controller.shutdown = lambda *args: False
    window.close()
    assert window.isVisible() and not panel._closed and controller.shutdown_calls == 0
    window.controller.shutdown = shutdown
    window.bridge.closed = True
    window.close()
    assert window._window_closed and panel._closed and controller.shutdown_calls == 1


def test_about_to_quit_reaps_child_in_isolated_application(tmp_path):
    script = '''
import sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from test_ui_discover import APP, FakeController, BrowseController, WindowBridge, command, until, gone
from covearchive.ui.window import MainWindow
from covearchive.ui.preferences import PreferenceStore
from covearchive.ui.discover_process import DiscoverController
from PySide6.QtCore import QProcess
c = DiscoverController(command_factory=command('import time;time.sleep(60)'))
b = WindowBridge()
w = MainWindow(controller=BrowseController(), bridge=b,
    preferences=PreferenceStore(Path(sys.argv[3]) / 'prefs.json'), discover_controller=c)
p = w.discover_panel
p.search.setText('mask')
p.submit()
process = c.job.process
until(lambda: process.state() == QProcess.Running)
pid = process.processId()
APP.aboutToQuit.emit()
assert c.job is None and gone(pid)
b.closed = True
w.close()
assert w._window_closed and p._closed
fake = FakeController()
b2 = WindowBridge()
w2 = MainWindow(controller=BrowseController(), bridge=b2,
    preferences=PreferenceStore(Path(sys.argv[3]) / 'other.json'), discover_controller=fake)
APP.aboutToQuit.emit()
assert fake.shutdown_calls == 1
b2.closed = True
w2.close()
assert fake.shutdown_calls == 2
APP.aboutToQuit.emit()
assert fake.shutdown_calls == 2
print('quit reaped worker; final close disconnected slots')
'''
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / 'config'),
               XDG_STATE_HOME=str(tmp_path / 'state'))
    result = subprocess.run([sys.executable, '-I', '-B', '-c', script,
                             str(ROOT / 'src'), str(ROOT / 'tests'), str(tmp_path)],
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'quit reaped worker' in result.stdout


@pytest.mark.parametrize('scale', [1, 1.4])
@pytest.mark.parametrize('queue_count', [0, 100])
def test_busy_toolbar_fits_minimum_window(rig, scale, queue_count):
    from test_ui_queue import job, view, S

    window, panel, controller = rig(scale=scale)
    window.bridge.view_changed.emit(view(1, tuple(job(S.QUEUED, job_id=str(i)) for i in range(queue_count))))
    APP.processEvents()
    assert window._queue_count == queue_count
    assert window.nav.tabText(1) == ('Queue (100)' if queue_count else 'Queue')
    window.url.setText('https://github.com/Example/Existing')
    window.scan.click()
    window._navigate(3)
    panel.search.setText('mask')
    panel.submit()
    window.resize(820, 520)
    for _ in range(5):
        APP.processEvents()
    assert window.size().toTuple() == (820, 520)
    controls = [window.nav, window.url.parentWidget(), window.scan,
                window.cancel, window.theme_toggle, window.settings_button]
    for tab in range(4):
        window.tabs.setCurrentIndex(tab)
        APP.processEvents()
        assert window.nav.currentIndex() == tab
        assert window.nav.rect().contains(window.nav.tabRect(3))
        for widget in controls:
            assert widget.isVisible() and window.toolbar.rect().contains(bounds(widget, window.toolbar))
        for a, b in zip(controls, controls[1:]):
            assert not bounds(a, window.toolbar).intersects(bounds(b, window.toolbar))
        container = window.url.parentWidget()
        for widget in (window.url, window.provider):
            assert container.rect().contains(bounds(widget, container))
        assert not bounds(window.provider, container).intersects(bounds(window.url, container))
    window._navigate(3)
    assert panel.cancel.isVisible() and window.rect().contains(bounds(panel.cancel, window))
    assert not controller.cancel_calls


@pytest.mark.parametrize('scale', [1, 1.4])
def test_long_query_and_all_partial_messages_never_overlap(rig, scale):
    window, panel, controller = rig(scale=scale)
    window.resize(820, 520)
    query = '.' * 150 + 'x' * 100
    panel.search.setText(query)
    panel.submit()
    controller.finish(result_page(record(description='<b>x</b>' + ' long' * 100),
        incomplete_results=True, dropped=29, duplicates=14, total_count=2000, retrievable_total=1000))
    panel.results.setCurrentRow(0)
    for _ in range(5):
        APP.processEvents()
    assert query in panel.query_label.text()
    for value in ('Partial results', '15 invalid', '14 duplicate', '1,000'):
        assert value in panel.status.text() and value in panel.status.toolTip()
    assert '<b>' not in panel.query_label.toolTip()
    controls = [panel.query_label, panel.results, panel.details, panel.open_button, panel.status]
    for control in controls:
        assert window.rect().contains(bounds(control, window))
    for a, b in zip(controls, controls[1:]):
        assert not bounds(a, window).intersects(bounds(b, window))
    assert panel.query_label.height() == panel.query_label.fontMetrics().height() + 2
    assert panel.status.height() == panel.status.fontMetrics().height() + 2
    assert panel.status.accessibleDescription() == panel.status.text()


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('size', [(820, 520), (1180, 720), (1280, 720)])
@pytest.mark.parametrize('scale', [1, 1.4])
def test_layout_theme_accessibility_and_keyboard_traversal(rig, theme, size, scale):
    window, panel, controller = rig(scale=scale)
    assert panel.search.font().pointSizeF() == pytest.approx(APP.font().pointSizeF())
    window.set_appearance(theme, persist=False)
    window.resize(*size)
    search(panel, controller, record(description='<b>x</b> ' + 'long text ' * 80), has_next=True)
    panel.results.setCurrentRow(0)
    for _ in range(5):
        APP.processEvents()
    assert window.size().width() == size[0] and window.size().height() == size[1]
    assert window.property('themeName') == theme
    visible = [panel.search, panel.search_button, panel.category, panel.sort,
               panel.results, panel.details, panel.open_button, panel.previous, panel.next, panel.retry]
    for widget in visible:
        assert widget.isVisible() and window.rect().contains(bounds(widget, window)), widget.accessibleName()
        assert widget.accessibleName() and widget.accessibleDescription()
        if widget in (panel.search_button, panel.open_button, panel.previous, panel.next, panel.retry):
            assert widget.width() >= widget.sizeHint().width()
            assert widget.height() >= widget.sizeHint().height()
    for a, b in zip(visible, visible[1:]):
        assert not bounds(a, panel).intersects(bounds(b, panel))
    assert panel.results.height() >= 90 and panel.details.height() >= 70
    for label in panel.findChildren(QLabel):
        assert label.isVisible() and label.width() > 0
        assert label.heightForWidth(label.width()) <= label.height()
    assert window.nav.rect().contains(window.nav.tabRect(3))
    assert window.nav.width() <= window.toolbar.width()
    chrome = [window.nav, window.url.parentWidget(), window.scan, window.theme_toggle, window.settings_button]
    for a, b in zip(chrome, chrome[1:]):
        assert window.toolbar.rect().contains(bounds(a, window.toolbar))
        assert not bounds(a, window.toolbar).intersects(bounds(b, window.toolbar))
    panel.results.setFocus()
    QTest.keyClick(panel.results, Qt.Key_Tab)
    assert panel.details.hasFocus()
    QTest.keyClick(panel.details, Qt.Key_Tab)
    assert panel.open_button.hasFocus()


def test_frozen_worker_integration_requires_existing_bundle():
    if not getattr(sys, 'frozen', False):
        pytest.skip('No prebuilt frozen app in this source test environment; no PyInstaller build requested.')
    from covearchive.ui.discover_process import worker_command
    assert worker_command(None) == [str(Path(sys.executable).resolve()), '--discover-worker']
