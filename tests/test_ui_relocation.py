"""Isolated offscreen Library workflow using fake provider evidence."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from dataclasses import replace
from itertools import combinations
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QPoint, Qt, Signal, QTimer
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QApplication, QComboBox,
                               QLabel, QLineEdit, QPushButton, QScrollArea, QWidget)

from covearchive.library.model import Library, Repository
from covearchive.ui.library_panel import LibraryPanel
from covearchive.ui.library_store import LibraryStore
from covearchive.ui.preferences import PreferenceStore
from covearchive.ui.window import MainWindow
from covearchive import relocation as r
from test_relocation import A, B, C, Provider, check

APP = QApplication.instance() or QApplication([])


class Worker(QObject):
    terminal = Signal(str, str, object)
    def __init__(self):
        super().__init__(); self.calls = []; self.stopped = False
    def start(self, repo, path): self.calls.append((repo, path)); return True
    def cancel(self): self.terminal.emit('id', 'CANCELLED', None)
    def shutdown(self): self.stopped = True; self.cancel(); return True
    def finish(self, result): self.terminal.emit('id', 'COMPLETED', result)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ('HOME', 'LOCALAPPDATA', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    store = LibraryStore(tmp_path / 'settings' / 'library.json')
    lib = Library(repositories=(replace(A, auto_archive=6),))
    assert store.save(lib) is None
    worker = Worker(); panel = LibraryPanel(store, relocation_controller=worker)
    panel.resize(1180, 720); panel.show(); panel.table.selectRow(0); APP.processEvents()
    yield panel, panel.relocation, worker, tmp_path
    panel.close()


def result(tmp_path):
    p = Provider(); path = tmp_path / 'settings' / 'relocation.json'; check(path, p)
    p.move(B)
    return check(path, p)


def widget_rect(widget, parent):
    return widget.rect().translated(widget.mapTo(parent, QPoint()))


def library_control_rects(window, panel, scroll):
    controls = (QAbstractButton, QAbstractItemView, QComboBox, QLabel, QLineEdit)
    for widget in panel.findChildren(QWidget):
        if not widget.isVisible() or not isinstance(widget, controls) or widget.rect().isEmpty():
            continue
        parent = widget.parentWidget()
        while parent is not panel and not isinstance(parent, (QAbstractItemView, QComboBox)):
            parent = parent.parentWidget()
        if parent is panel:
            rect = widget_rect(widget, window)
            if scroll.viewport().isAncestorOf(widget):
                rect = rect.intersected(widget_rect(scroll.viewport(), window))
            yield widget, rect


def assert_library_button_traversal(window, panel, scroll):
    buttons = {button for button in panel.findChildren(QPushButton) if button.isVisible() and button.isEnabled()}
    for key in (Qt.Key_Tab, Qt.Key_Backtab):
        scroll.verticalScrollBar().setValue(0)
        panel.url.setFocus()
        visited = set()
        while (focused := window.focusWidget()) is not None and focused not in visited:
            visited.add(focused)
            if focused in buttons and scroll.viewport().isAncestorOf(focused):
                assert scroll.viewport().rect().contains(widget_rect(focused, scroll.viewport())), focused.text()
            QTest.keyClick(focused, key)
            APP.processEvents()
        assert buttons <= visited, [button.text() for button in buttons - visited]


@pytest.mark.parametrize('size', [(820, 520), (1180, 720)], ids=['minimum', 'default'])
@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('expanded', [False, True], ids=['collapsed', 'expanded'])
def test_library_layout_at_window_sizes(tmp_path, monkeypatch, size, theme, expanded):
    from test_scan_process import until

    for key in ('HOME', 'USERPROFILE', 'LOCALAPPDATA', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    store = LibraryStore(tmp_path / 'settings' / 'library.json')
    assert store.save(Library(repositories=(replace(A, auto_archive=6),))) is None
    worker = Worker()
    monkeypatch.setattr('covearchive.ui.relocation_panel.RelocationController', lambda launcher, parent: worker)
    window = MainWindow(Path(__file__).resolve().parents[1] / 'run_covearchive.py',
                        preferences=PreferenceStore(tmp_path / 'preferences.json'), library=store)
    try:
        window.setFont(QFont('Noto Sans', 9))
        window.set_appearance(theme, persist=False)
        window.show()
        window._navigate(2)
        window.resize(*size)
        window.activateWindow()
        APP.processEvents()
        panel = window.library_panel
        panel.table.selectRow(0)
        APP.processEvents()
        inspector = panel.relocation
        assert inspector.check.isEnabled()
        if expanded:
            evidence = result(tmp_path)
            inspector.check.click()
            assert worker.calls
            worker.finish(evidence)
            assert inspector.result is evidence
            APP.processEvents()
            APP.processEvents()

        assert (window.width(), window.height()) == size
        assert window.property('themeName') == theme and inspector.history.isVisible() == expanded
        scroll = inspector.findChild(QScrollArea)
        rects = list(library_control_rects(window, panel, scroll))
        overlaps = []
        for (first, first_rect), (second, second_rect) in combinations(rects, 2):
            if first.isAncestorOf(second) or second.isAncestorOf(first):
                continue
            overlap = first_rect.intersected(second_rect)
            if overlap.width() > 2 and overlap.height() > 2:
                overlaps.append((first.accessibleName() or first.objectName(),
                                 second.accessibleName() or second.objectName(), overlap.getRect()))
        assert not overlaps, f'{len(overlaps)} overlaps: {overlaps}'
        bounds = widget_rect(panel, window).intersected(window.rect())
        assert all(bounds.contains(rect) for _, rect in rects if not rect.isEmpty())
        assert panel.tree.viewport().height() >= panel.tree.visualItemRect(panel.tree.topLevelItem(1)).bottom() + 1
        assert panel.table.horizontalHeader().isVisible()
        assert panel.table.viewport().height() >= 2 * panel.table.rowHeight(0)
        # Qt hides a now-unneeded vertical scrollbar on a posted event.
        until(lambda: scroll.widget().width() <= scroll.viewport().width())
        assert scroll.horizontalScrollBar().maximum() == 0
        for button in inspector.findChildren(QPushButton):
            if button.isVisible():
                scroll.ensureWidgetVisible(button, 0, 0)
                assert scroll.viewport().rect().contains(widget_rect(button, scroll.viewport())), button.text()
        assert_library_button_traversal(window, panel, scroll)
        if not expanded:
            inspector.check.click()
            assert inspector.cancel.isEnabled()
            assert_library_button_traversal(window, panel, scroll)
    finally:
        window.close()
        until(lambda: not window.isVisible(), 10)


def test_explicit_browse_and_save_keep_old_url_and_auto_off(env):
    panel, inspector, worker, tmp_path = env
    old = panel.library.repositories[0]
    evidence = result(tmp_path); emitted = []; panel.open_requested.connect(emitted.append)
    before = panel.store.path.read_bytes()
    assert inspector.check.text() == 'Check repository location'
    inspector.check.click(); assert worker.calls == [(old, tmp_path / 'settings' / 'relocation.json')]
    worker.finish(evidence); APP.processEvents()
    assert panel.store.path.read_bytes() == before and emitted == []
    assert inspector.open.isEnabled() and inspector.add.isEnabled()
    assert 'numeric GitHub id' in inspector.detail.text()
    assert 'not automatically migrated or globally identity-bound' in inspector.notice.text()
    assert inspector.history.count() == 3
    inspector.open.click(); assert emitted == [evidence.verified_url]
    inspector.history.setCurrentIndex(0); inspector.open_recorded.click(); assert emitted[-1] == A.url
    inspector.add.click(); APP.processEvents()
    assert len(panel.library.repositories) == 2
    assert next(repo for repo in panel.library.repositories if repo.url == A.url) == old
    new = next(repo for repo in panel.library.repositories if repo.url == evidence.verified_url)
    assert new.auto_archive is None and new.collection is None and new.folder is None
    assert panel.store.load()[0] == panel.library


def test_mismatch_warns_and_only_opens_original_id_verified_location(env):
    panel, inspector, worker, tmp_path = env
    p = Provider(); path = tmp_path / 'r.json'; check(path, p); p.move(B); check(path, p)
    p.by_url[A.ref] = (2, r.RepositoryTarget('Owner', 'Repo')); evidence = check(path, p)
    inspector.check.click(); worker.finish(evidence)
    assert 'Identity mismatch' in inspector.detail.text() and 'not a verified continuation' in inspector.detail.text()
    emitted = []; panel.open_requested.connect(emitted.append); inspector.open.click()
    assert emitted == [evidence.verified_url] and emitted != [A.url]


def test_save_verified_destination_then_reuse_retains_original_identity(env):
    panel, inspector, worker, tmp_path = env
    path = tmp_path / 'settings' / 'relocation.json'; p = Provider(); check(path, p)
    p.move(B); moved = check(path, p)
    inspector.check.click(); worker.finish(moved); inspector.add.click(); APP.processEvents()
    row = next(i for i, repo in enumerate(panel.model.rows) if repo.url == moved.verified_url)
    panel.table.selectRow(row); destination = panel.model.rows[row]
    assert destination.auto_archive is None
    before = path.read_bytes()
    p.move(C); p.by_url[B.repo_key] = (2, B)
    inspector.check.click(); evidence = check(path, p, destination); worker.finish(evidence)
    assert evidence.outcome is r.Outcome.URL_REUSED and not evidence.no_prior_identity
    assert (evidence.repository_id, evidence.observed_id) == (1, 2)
    assert 'Identity mismatch' in inspector.detail.text() and 'not a verified continuation' in inspector.detail.text()
    assert 'First observation' not in inspector.detail.text()
    emitted = []; panel.open_requested.connect(emitted.append); inspector.open.click()
    assert emitted == [r.canonical_url(C)] and destination.url not in emitted
    data = r.RelocationStore(path).load()
    assert next(binding for binding in data['bindings'] if binding['url'] == destination.url)['repository_id'] == 1
    assert path.read_bytes() == before
    assert {record['repository_id'] for record in data['identities']} == {1}


@pytest.mark.parametrize('outcome', [r.Outcome.INCONCLUSIVE, r.Outcome.UNAVAILABLE, r.Outcome.UNSUPPORTED])
def test_no_verified_actions_for_failure_or_unsupported(env, outcome):
    _, inspector, worker, _ = env
    inspector.check.click(); worker.finish(r.Result(outcome, A.url))
    assert not inspector.open.isEnabled() and not inspector.add.isEnabled()


def test_first_observation_and_selection_change_never_claim_archive_identity(env):
    panel, inspector, worker, tmp_path = env
    first = check(tmp_path / 'r.json', Provider())
    inspector.check.click(); worker.finish(first)
    assert 'no prior identity' in inspector.detail.text() and 'does not prove continuity' in inspector.detail.text()
    assert not inspector.open.isEnabled()
    inspector.check.click(); panel.table.clearSelection(); worker.finish(result(tmp_path))
    assert inspector.result is None and not inspector.add.isEnabled()


def test_cancel_shutdown_and_deadline_clear_pending(env):
    _, inspector, worker, _ = env
    inspector.check.click(); inspector.cancel.click()
    assert inspector.pending is None and 'cancelled' in inspector.detail.text()
    inspector.check.click(); worker.terminal.emit('id', 'TIMED_OUT', None)
    assert 'deadline exceeded' in inspector.detail.text() and inspector.check.isEnabled()
    inspector.check.click(); assert inspector.shutdown()
    assert worker.stopped and inspector.pending is None and not inspector.check.isEnabled()


def test_relative_library_path_is_absolute_at_worker_start(tmp_path, monkeypatch):
    from covearchive import relocation_worker as wire

    monkeypatch.chdir(tmp_path)
    store = LibraryStore(Path('settings/library.json'))
    assert store.save(Library(repositories=(A,))) is None
    worker = Worker(); panel = LibraryPanel(store, relocation_controller=worker)
    inspector = panel.relocation; inspector.select(A)
    paths = []

    def start(repo, path):
        _, _, encoded = wire.parse_request(wire.encode_request('a' * 32, repo, path))
        paths.append(Path(encoded))
        return True

    monkeypatch.setattr(worker, 'start', start)
    inspector._check()
    assert paths == [tmp_path / 'settings' / 'relocation.json']
    assert inspector.pending == A and inspector.cancel.isEnabled()
    panel.close()


@pytest.mark.parametrize('emits_terminal', [False, True], ids=['no-terminal', 'synchronous-terminal'])
def test_worker_start_exception_restores_inspector_controls(env, monkeypatch, emits_terminal):
    _, inspector, worker, _ = env

    def start(*_):
        if emits_terminal: worker.cancel()
        raise RuntimeError('could not launch worker')

    with monkeypatch.context() as patch:
        patch.setattr(worker, 'start', start)
        inspector._check()
        assert inspector.pending is None and not inspector.cancel.isEnabled()
        assert inspector.check.isEnabled() and 'failed' in inspector.detail.text()
    inspector.check.click()
    assert worker.calls and inspector.cancel.isEnabled()


@pytest.mark.parametrize('bridge_closed', [False, True], ids=['queue-open', 'queue-closed'])
def test_window_close_resumes_after_relocation_worker_reap(tmp_path, monkeypatch, bridge_closed):
    from covearchive.ui.archive_bridge import ShutdownStatus
    from test_scan_process import until
    from test_ui_window import WindowBridge

    worker = Worker(); reaped = False
    monkeypatch.setattr('covearchive.ui.relocation_panel.RelocationController', lambda launcher, parent: worker)
    monkeypatch.setattr(worker, 'shutdown', lambda: reaped)
    bridge = WindowBridge(); bridge.closed = bridge_closed; bridge.run_active = True
    window = MainWindow(bridge=bridge, preferences=PreferenceStore(tmp_path / 'preferences.json'),
                        library=LibraryStore(tmp_path / 'library.json'))
    confirmations = []
    monkeypatch.setattr(window, '_confirm_close', lambda: confirmations.append(True) or 'Pause and Close')
    window.show()
    try:
        window.close()
        assert window.isVisible() and not bridge.stops
        reaped = True; worker.terminal.emit('id', 'CANCELLED', None)
        if not bridge_closed:
            until(lambda: bridge.stops == [True], seconds=1)
            assert confirmations == [True]
            bridge.closed = True
            bridge.shutdown_status.emit(ShutdownStatus('done', (), False))
        until(lambda: not window.isVisible(), seconds=1)
    finally:
        reaped = bridge.closed = True; window.close()


@pytest.mark.parametrize('initial_ids, resumed_ids, initial_run, resumed_run, ask_again', [
    ((), (), False, True, True),
    (('first',), ('first', 'second'), True, True, True),
    (('first', 'second'), ('second',), True, True, False),
], ids=['new-run', 'new-job-in-active-run', 'remaining-approved-job'])
def test_window_close_confirms_download_started_while_waiting_for_relocation(
        tmp_path, monkeypatch, initial_ids, resumed_ids, initial_run, resumed_run, ask_again):
    from types import SimpleNamespace

    from covearchive.queue.models import JobState
    from test_scan_process import until
    from test_ui_window import WindowBridge

    worker = Worker(); reaped = False
    monkeypatch.setattr('covearchive.ui.relocation_panel.RelocationController', lambda launcher, parent: worker)
    monkeypatch.setattr(worker, 'shutdown', lambda: reaped)
    bridge = WindowBridge()
    window = MainWindow(bridge=bridge, preferences=PreferenceStore(tmp_path / 'preferences.json'),
                        library=LibraryStore(tmp_path / 'library.json'))
    inspector = window.library_panel.relocation
    inspector.select(A)
    confirmations = []

    def set_active(run, ids):
        bridge.run_active = run
        window.last_view = SimpleNamespace(jobs=[
            SimpleNamespace(job_id=job_id, owned=True, state=JobState.RUNNING) for job_id in ids])

    keep_open_at = 1 + initial_run

    def confirm():
        confirmations.append(True)
        return 'Keep Open' if len(confirmations) == keep_open_at else 'Pause and Close'

    monkeypatch.setattr(window, '_confirm_close', confirm)
    window.show()
    try:
        assert inspector.check.isEnabled()
        set_active(initial_run, initial_ids)
        window.close()
        assert window.isVisible() and not bridge.stops and len(confirmations) == initial_run
        assert not inspector.check.isEnabled()
        set_active(resumed_run, resumed_ids)
        reaped = True
        worker.terminal.emit('id', 'CANCELLED', None)
        until(lambda: len(confirmations) > initial_run or bridge.stops, seconds=1)
        if not ask_again:
            assert len(confirmations) == initial_run and bridge.stops == [True]
            return
        assert len(confirmations) == keep_open_at and not bridge.stops and window.isVisible()
        assert inspector.check.isEnabled()
        inspector.check.click()
        assert worker.calls and inspector.cancel.isEnabled()
        worker.terminal.emit('id', 'CANCELLED', None)
        window.close()
        assert len(confirmations) == keep_open_at + 1 and bridge.stops == [True]
    finally:
        reaped = bridge.closed = True; window.close()


@pytest.mark.parametrize('subsystem', ['health', 'auto'])
@pytest.mark.parametrize('bridge_closed', [False, True], ids=['queue-open', 'queue-closed'])
def test_window_close_restores_relocation_when_other_shutdown_waits(
        tmp_path, monkeypatch, subsystem, bridge_closed):
    from test_ui_window import WindowBridge

    worker = Worker()
    monkeypatch.setattr('covearchive.ui.relocation_panel.RelocationController', lambda launcher, parent: worker)
    bridge = WindowBridge(); bridge.closed = bridge_closed; bridge.run_active = True
    window = MainWindow(bridge=bridge, preferences=PreferenceStore(tmp_path / 'preferences.json'),
                        library=LibraryStore(tmp_path / 'library.json'))
    inspector = window.library_panel.relocation
    inspector.select(A)
    confirmations = []
    monkeypatch.setattr(window, '_confirm_close', lambda: confirmations.append(True) or 'Pause and Close')
    waiting = window.auto if subsystem == 'auto' else window.library_panel.health
    window.show()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(waiting, 'shutdown', lambda: False)
            window.close()
            assert window.isVisible() and not bridge.stops
            assert inspector.check.isEnabled()
        window.close()
        if bridge_closed:
            assert not window.isVisible() and not confirmations
        else:
            assert confirmations == [True, True] and bridge.stops == [True]
    finally:
        bridge.closed = True; window.close()


def test_gui_exercise_real_worker_fake_provider_off_main_thread(env):
    from covearchive.ui.relocation_process import RelocationController
    from test_scan_process import ROOT, command, until
    panel, inspector, _, tmp_path = env
    marker = tmp_path / 'worker'
    script = (f'import os,sys;sys.path.insert(0,{str(ROOT / "src")!r});'
              'from covearchive import relocation_worker as w,relocation as r;'
              f'open({str(marker)!r},"w").write(str(os.getpid()));'
              'r.github.GitHubRepositoryClient.repository=lambda *a,**k:(1,r.RepositoryTarget("Owner","Renamed"));'
              'original=r.check_location;'
              'w.r.check_location=lambda repo,path:original(repo,path,clock=lambda:1234);'
              'os._exit(w.main())')
    controller = RelocationController(command_factory=command(script), deadline_ms=3000)
    inspector.controller.terminal.disconnect(inspector._terminal)
    inspector.controller = controller; controller.terminal.connect(inspector._terminal)
    ticks = []; timer = QTimer(); timer.timeout.connect(lambda: ticks.append(1)); timer.start(5)
    try:
        inspector.check.click(); until(lambda: inspector.pending is None, seconds=3)
        assert inspector.result is not None, inspector.detail.text()
        assert inspector.result.no_prior_identity and inspector.result.verified_url.endswith('/Renamed')
        assert int(marker.read_text()) != os.getpid() and ticks
        assert 'First observation: no prior identity' in inspector.detail.text()
        assert panel.library.repositories[0].url == A.url and len(panel.library.repositories) == 1
    finally: timer.stop(); assert controller.shutdown()
