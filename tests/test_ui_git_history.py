"""Manual controller lifecycle and Browse integration; workers never touch widgets."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import threading
import time

import pytest
from PySide6.QtCore import QObject, QThread, Qt, Slot
from PySide6.QtWidgets import QApplication
from covearchive.archive import githistory as gh
from covearchive.ui.git_history import HistoryController, ERRORS, result_text
from test_git_history import REPO
from test_ui_window import ui, isolated_data, succeed, w
from test_scan_worker import release, asset, catalog, RID

APP = QApplication.instance() or QApplication([])


def until(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        APP.processEvents(); time.sleep(.005)
    assert predicate(), "history controller did not complete"


@pytest.mark.parametrize("status", ["ARCHIVED", "UNCHANGED"])
def test_worker_off_gui_thread_queued_delivery_and_restart(tmp_path, monkeypatch, status):
    calls = []; result = gh.HistoryResult(status, tmp_path, 2, 123)
    def work(*args, **kw): calls.append(threading.get_ident()); return result
    monkeypatch.setattr(gh, "archive_history", work)
    controller = HistoryController(); received = []
    class Sink(QObject):
        @Slot(object)
        def done(self, value): received.append((value, QThread.currentThread()))
    sink = Sink(); controller.finished.connect(sink.done, Qt.QueuedConnection)
    try:
        assert not controller.running and not calls
        assert controller.start(REPO, str(tmp_path))
        until(lambda: bool(received) and not controller.running)
        assert calls[0] != threading.get_ident() and received[0] == (result, APP.thread())
        assert controller.start(REPO, str(tmp_path))
        until(lambda: len(received) == 2 and not controller.running)
    finally:
        controller.cancel(); until(controller.shutdown)


@pytest.mark.parametrize("code", ["cancelled", "network", "git_missing", "busy"])
def test_controller_terminal_error_reenables_and_safe_message(tmp_path, monkeypatch, code):
    def fail(*args, **kw): raise gh.HistoryError(code)
    monkeypatch.setattr(gh, "archive_history", fail)
    controller = HistoryController(); failures = []
    controller.failed.connect(failures.append, Qt.QueuedConnection)
    try:
        assert controller.start(REPO, str(tmp_path))
        until(lambda: bool(failures) and not controller.running)
        assert failures[0].code == code and "://" not in str(failures[0])
    finally:
        controller.cancel(); until(controller.shutdown)


def test_controller_single_worker_cancel_and_nonblocking_shutdown(tmp_path, monkeypatch):
    entered = threading.Event(); left = threading.Event()
    def work(*args, cancel=None, **kw):
        entered.set()
        try:
            assert entered.wait(1)
            deadline = time.monotonic() + 2
            while not cancel() and time.monotonic() < deadline: time.sleep(.01)
            assert cancel(); raise gh.HistoryError("cancelled")
        finally: left.set()
    monkeypatch.setattr(gh, "archive_history", work)
    controller = HistoryController()
    try:
        assert controller.start(REPO, str(tmp_path)); until(entered.is_set)
        assert not controller.start(REPO, str(tmp_path))
        started = time.monotonic(); controller.cancel(); controller.shutdown()
        assert time.monotonic() - started < .1
        until(lambda: left.is_set() and not controller.running)
    finally:
        controller.cancel(); until(controller.shutdown)


def test_browse_manual_controls_and_accessibility(ui):
    window, scan = ui
    button = getattr(window, "history_button", None)
    assert button is not None, "Browse needs the manual Archive Git History button"
    assert not button.isEnabled()
    succeed(window, scan, release(assets=[asset()]))
    label = window.history_status
    assert label.textFormat() == Qt.PlainText and label.wordWrap()
    text = " ".join([button.toolTip(), button.accessibleName(), button.accessibleDescription()]).lower()
    assert all(word in text for word in ["public", "branches", "tags", "lfs", "submodule"])
    assert button.focusPolicy() != Qt.NoFocus
    assert not window.history_controller.running
    assert window.history_cancel.accessibleName() == "Cancel Git History"
    assert window.history_cancel.accessibleDescription() == "Stop the running Git history archive"
    assert "lfs" not in window.history_cancel.toolTip().lower()


def test_close_requests_cancel_waits_for_history_finish(ui, monkeypatch):
    window, scan = ui; controller = getattr(window, "history_controller", None)
    assert controller is not None, "window must supervise history before accepting close"
    entered = threading.Event(); finish = threading.Event()
    def work(*args, cancel=None, **kw):
        entered.set()
        try:
            assert finish.wait(2); assert cancel()
            raise gh.HistoryError("cancelled")
        finally: finish.set()
    monkeypatch.setattr(gh, "archive_history", work)
    try:
        assert controller.start(REPO, os.environ["LOCALAPPDATA"]); until(entered.is_set)
        window.close(); APP.processEvents()
        assert window.isVisible() and controller.running
        finish.set(); until(lambda: not controller.running and not window.isVisible())
    finally:
        finish.set(); controller.cancel(); until(controller.shutdown)


@pytest.mark.parametrize("catalog,root,shutting,enabled", [
    (False, True, False, False), (True, False, False, False),
    (True, True, True, False), (True, True, False, True)])
def test_button_prerequisites_never_auto_start(ui, tmp_path, monkeypatch, catalog, root, shutting, enabled):
    window, scan = ui; calls = []
    monkeypatch.setattr(gh, "archive_history", lambda *a, **kw: calls.append((a, kw)))
    button = getattr(window, "history_button", None)
    assert button is not None, "Browse must gate manual history on repository/root/bridge state"
    if catalog: succeed(window, scan, release(assets=[asset()]))
    window.root.setText(str(tmp_path) if root else "relative-root")
    if shutting: window.bridge.begin_shutdown()
    APP.processEvents()
    assert button.isEnabled() is enabled
    assert not calls, "scan, root edits and bridge changes must never start history"


@pytest.mark.parametrize("outcome", ["ARCHIVED", "UNCHANGED", "cancelled", "network"])
def test_manual_job_status_retains_input_and_reenables(ui, tmp_path, monkeypatch, outcome):
    window, scan = ui
    button = getattr(window, "history_button", None)
    assert button is not None, "Browse needs manual history status transitions"
    succeed(window, scan, release(assets=[asset()])); window.root.setText(str(tmp_path))
    entered = threading.Event(); finish = threading.Event()
    def work(*args, **kw):
        entered.set(); assert finish.wait(2)
        if outcome.islower(): raise gh.HistoryError(outcome)
        return gh.HistoryResult(outcome, tmp_path / "snapshot-test", 2, 123)
    monkeypatch.setattr(gh, "archive_history", work)
    original = window.url.text()
    try:
        button.click(); until(entered.is_set)
        assert window.history_controller.running and not button.isEnabled()
        assert "running" in window.history_status.text().lower() or "archiving" in window.history_status.text().lower()
        if outcome == "cancelled":
            window.history_cancel.click(); window._history_enabled()
            assert window.history_status.text() == "Cancelling Git history..." and not window.history_cancel.isEnabled()
        finish.set(); window.history_controller._thread.join(2)
        assert not window.history_controller.running
        window._selection_changed(); assert not button.isEnabled()
        until(lambda: not window.history_controller.running and button.isEnabled())
        text = window.history_status.text().lower()
        expected = {"ARCHIVED": "2", "UNCHANGED": "nothing downloaded", "cancelled": "cancel", "network": "network"}[outcome]
        assert expected in text and window.url.text() == original
        assert window.history_status.textFormat() == Qt.PlainText
    finally:
        finish.set(); window.history_controller.cancel(); until(window.history_controller.shutdown)


def test_keep_open_queue_prompt_restores_history_controls(tmp_path, monkeypatch):
    from covearchive.ui.window import MainWindow
    from covearchive.ui.preferences import PreferenceStore
    from test_ui_window import WindowBridge, FakeController
    bridge = WindowBridge(); bridge.run_active = True; scan = FakeController()
    window = MainWindow(controller=scan, bridge=bridge, preferences=PreferenceStore(tmp_path/"prefs.json"))
    window.show(); succeed(window, scan, release(assets=[asset()])); window.root.setText(str(tmp_path))
    monkeypatch.setattr(window, "_confirm_close", lambda: "Keep Open")
    entered = threading.Event()
    def work(*args, cancel=None, **kw):
        entered.set()
        deadline = time.monotonic()+2
        while not cancel() and time.monotonic()<deadline: time.sleep(.01)
        raise gh.HistoryError("cancelled")
    monkeypatch.setattr(gh, "archive_history", work)
    try:
        assert window.history_controller.start(REPO, str(tmp_path)); until(entered.is_set)
        window.close(); until(lambda: not window.history_controller.running)
        until(lambda: window.history_button.isEnabled())
        assert window.isVisible() and not bridge.stops
    finally:
        window.history_controller.cancel(); until(window.history_controller.shutdown)
        bridge.closed=True; window.close(); APP.processEvents()


@pytest.mark.parametrize("event", ["scan", "catalog"])
def test_rescan_clears_history_status(ui, event):
    window, scan = ui; succeed(window, scan, release())
    window.history_status.setText("Repository moved..."); window.scan.click()
    if event == "catalog":
        window.history_status.setText("Archived Git history...")
        scan.finish(w.decode_response(w.encode_success(RID,catalog(release())),RID))
    assert window.history_status.text() == ""


@pytest.mark.parametrize("code", tuple(ERRORS))
def test_history_failure_identifies_its_workflow(ui, code):
    window, _ = ui; window._history_failed(gh.HistoryError(code))
    text = window.history_status.text()
    assert text.startswith("Git history") and "\n" not in text and "://" not in text


def test_history_publishing_cannot_cancel_message(ui):
    window, _ = ui; window._history_progress("publishing")
    assert window.history_status.text() == "Publishing Git history (cannot be cancelled now)..."


@pytest.mark.parametrize("status", ["ARCHIVED", "UNCHANGED"])
@pytest.mark.parametrize("damaged", [(), ("old-a", "old-b")])
def test_history_result_destination_and_damage_warning(tmp_path, status, damaged):
    text = result_text(gh.HistoryResult(status,tmp_path/"snapshot-test",2,123,damaged))
    assert str(tmp_path) in text and "Git history" in text
    assert ("Warning: 2 earlier snapshot(s) failed verification and were kept untouched" in text) == bool(damaged)
    assert "secret" not in result_text(gh.HistoryResult(status,"https://example/?token=secret",2,123,damaged))


def test_idle_history_controls_share_selection_band(ui):
    window,scan=ui;window.resize(820,520);succeed(window,scan,release(assets=[asset()]));APP.processEvents()
    assert window.history_status.isHidden()
    buttons=(window.select_matching,window.clear_selection,window.archive_selected,window.history_button,window.history_cancel)
    assert all(b.geometry().center().y()==buttons[0].geometry().center().y() for b in buttons)
    assert window.width()==820 and all(b.width()>=b.sizeHint().width() for b in buttons)
    height=window.tree.viewport().height();window.history_button.hide();window.history_cancel.hide();APP.processEvents()
    assert window.tree.viewport().height()<=height


def test_close_waits_for_pending_history_terminal(ui, tmp_path, monkeypatch):
    window,scan=ui;succeed(window,scan,release());window.root.setText(str(tmp_path))
    monkeypatch.setattr(gh,"archive_history",lambda *a,**kw:gh.HistoryResult("ARCHIVED",tmp_path,1,1))
    window.history_button.click();window.history_controller._thread.join(2)
    window.close();assert window.isVisible() and window._history_closing
    until(lambda:not window.isVisible())
