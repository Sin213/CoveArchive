"""Mirror actions share the existing history worker and its close/cancel path."""
import threading

import pytest

from covearchive.archive import githistory as gh
from test_ui_git_history import APP, until
from test_ui_window import ui, isolated_data, succeed, w
from test_scan_worker import release, asset
from test_git_history import REPO, local, remove_source


@pytest.mark.parametrize("button,operation", [("mirror_update", "update_mirror"), ("mirror_verify", "verify_mirror")])
def test_manual_mirror_action(ui, tmp_path, monkeypatch, button, operation):
    window, scan = ui; calls = []
    def work(*args, **kwargs):
        calls.append(threading.get_ident())
        return gh.MirrorResult("VERIFIED" if operation == "verify_mirror" else "UPDATED", tmp_path, tmp_path / "capture.json", 2, "2026-01-01T00:00:00.000000Z")
    monkeypatch.setattr(gh, operation, work)
    control = getattr(window, button)
    assert not control.isEnabled() and control.accessibleName() == control.text()
    window.root.setText(str(tmp_path)); succeed(window, scan, release(assets=[asset()]))
    assert not calls
    control.click()
    until(lambda: bool(calls) and not window._history_pending)
    assert calls[0] != threading.get_ident()
    assert "mirror" in window.history_status.text().lower()
    assert "2 refs" in window.history_status.text() and "2026-01-01" in window.history_status.text()
    assert control.isEnabled()


def test_mirror_cancel_and_close(ui, tmp_path, monkeypatch):
    window, scan = ui; entered = threading.Event()
    def work(*args, cancel=None, **kwargs):
        entered.set()
        while not cancel(): threading.Event().wait(.005)
        raise gh.HistoryError("cancelled")
    monkeypatch.setattr(gh, "update_mirror", work)
    window.root.setText(str(tmp_path)); succeed(window, scan, release(assets=[asset()]))
    window.mirror_update.click(); until(entered.is_set)
    assert not window.mirror_verify.isEnabled() and not window.history_button.isEnabled()
    window.history_cancel.click(); until(lambda: not window._history_pending)
    assert window.mirror_verify.isEnabled()
    entered.clear(); window.mirror_update.click(); until(entered.is_set)
    window.close(); until(lambda: not window.history_controller.running and not window.isVisible())


def test_real_git_manual_buttons_and_offline_verify(ui, local):
    window, scan = ui
    window.root.setText(str(local[1])); succeed(window, scan, release(assets=[asset()]))
    assert not list(local[1].rglob("capture-*.json"))
    window.mirror_update.click()
    until(lambda: not window._history_pending, seconds=10)
    assert "Git mirror updated: 2 refs" in window.history_status.text()
    assert len(list(local[1].rglob("capture-*.json"))) == 1
    window.mirror_update.click()
    until(lambda: not window._history_pending, seconds=10)
    assert "unchanged; nothing downloaded" in window.history_status.text()
    remove_source(local[0]); window.mirror_verify.click()
    until(lambda: not window._history_pending, seconds=10)
    assert "Git mirror verified: 2 refs" in window.history_status.text()


def test_repair2_verify_existing_mirror_after_fresh_offline_start(ui, local, monkeypatch):
    window, scan = ui
    gh.update_mirror(REPO, local[1]); remove_source(local[0])
    assert window.catalog is None
    def forbidden(*args, **kwargs): pytest.fail("viewing or verifying must never download history")
    monkeypatch.setattr(gh, "update_mirror", forbidden)
    monkeypatch.setattr(gh, "archive_history", forbidden)
    original = gh._run_git
    def offline(args, **kwargs):
        assert not set(args) & {"fetch", "ls-remote"}
        return original(args, **kwargs)
    monkeypatch.setattr(gh, "_run_git", offline)
    window.root.setText(str(local[1])); window.url.setText("https://github.com/Owner/Repo/releases")
    APP.processEvents()
    assert not scan.calls and window.catalog is None
    assert window.mirror_verify.isEnabled() and not window.history_button.isEnabled()
    window.mirror_verify.click(); until(lambda: not window._history_pending, seconds=10)
    assert "Git mirror verified: 2 refs" in window.history_status.text()
    # A failed scan must not become a prerequisite for this local action either.
    window.scan.click(); scan.finish(w.ScanFailure(scan.job, "network"), "FAILED")
    APP.processEvents()
    assert window.catalog is None and window.mirror_verify.isEnabled()


def test_repair2_offline_verify_tracks_url_validity(ui, tmp_path):
    window, scan = ui; window.root.setText(str(tmp_path))
    window.url.setText("https://github.com/Owner/Repo")
    APP.processEvents(); assert window.mirror_verify.isEnabled()
    window.url.setText("https://user:secret@github.com/Owner/Repo")
    APP.processEvents(); assert not window.mirror_verify.isEnabled()
    assert not scan.calls and not window.history_controller.running
