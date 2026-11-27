"""Source-only Browse results use the existing manual Git workflows."""

from dataclasses import replace
import threading
import time

import pytest
from PySide6.QtCore import Qt, QRect
from PySide6.QtTest import QTest

from covearchive.archive import githistory as gh
from covearchive.github.urls import RepositoryTarget
from covearchive.scan_worker import ScanFailure, ScanSuccess, decode_response, encode_success
from covearchive.ui.git_history import ERRORS
from test_git_history import git, local, remove_source, snapshot_bytes
from test_scan_worker import RID, URL, asset, catalog, release
from test_ui_git_history import until
from test_ui_window import APP, isolated_data, succeed, ui


def scan_empty(window, scan, url=URL, repository=None, *, redirects=0):
    window.url.setText(url)
    window.scan.click()
    result = replace(catalog(), redirects=redirects, requests=1 + redirects)
    if repository is not None:
        result = replace(result, repository=repository)
    scan.finish(decode_response(encode_success(RID, result), RID))
    APP.processEvents()


@pytest.mark.parametrize("provider,host,path,suffix,project_id,redirects", [
    ("github", "github.com", "Owner", "/releases", None, 0),
    ("codeberg", "codeberg.org", "Owner", "/releases", None, 0),
    ("codeberg", "codeberg.org", "owner", "/releases", None, 1),
    ("gitlab", "gitlab.com", "Group/Subgroup", "/-/releases", 123, 0),
])
def test_source_only_manual_actions_bind_validated_catalog(ui, tmp_path, monkeypatch,
                                                          provider, host, path, suffix, project_id, redirects):
    window, scan = ui
    calls = []
    monkeypatch.setattr(window.history_controller, "start",
                        lambda repo, root, *, operation: calls.append((repo, root, operation)))
    window.root.setText(str(tmp_path))
    entered = RepositoryTarget(path.lower(), "repo", provider, host)
    target = entered if redirects else RepositoryTarget(path, "Repo", provider, host, project_id)
    scan_empty(window, scan, f"https://{host}/{entered.owner}/{entered.repo}{suffix}", target, redirects=redirects)
    assert window.empty.text() == "Source-only repository"
    assert "no published releases" in window.center_description.text()
    window.formats[".zip"].setChecked(False)
    assert window.empty.text() == "Source-only repository"
    assert not calls
    for button, operation, repository in ((window.source_archive, "snapshot", target),
                                          (window.source_update, "update", target),
                                          (window.mirror_verify, "verify", entered)):
        assert button.isVisible() and button.isEnabled()
        assert button.accessibleName() == button.text() and button.accessibleDescription()
        button.click()
        assert calls[-1] == (repository, str(tmp_path), operation)
    assert len(calls) == 3


def test_github_redirect_requires_a_fresh_canonical_scan(ui, tmp_path, monkeypatch):
    window, scan = ui
    window.root.setText(str(tmp_path))
    scan_empty(window, scan, redirects=1)
    assert window.empty.text() == "Source-only repository"
    assert ERRORS["redirect"] in window.center_description.text()
    monkeypatch.setattr(window.history_controller, "start",
                        lambda *a, **kw: pytest.fail("redirected source history started"))
    for button, operation in ((window.source_archive, "snapshot"), (window.source_update, "update")):
        assert button.isVisible() and not button.isEnabled()
        button.click()
        window._archive_history(operation=operation)
    assert window.mirror_verify.isEnabled()

    relocated = RepositoryTarget("NewOwner", "Repo")
    scan_empty(window, scan, "https://github.com/NewOwner/Repo", relocated)
    assert window.source_archive.isEnabled() and window.source_update.isEnabled()
    assert ERRORS["redirect"] not in window.center_description.text()


@pytest.mark.parametrize("published,heading", [
    (None, "Source-only repository"),
    (release(2, assets=[]), "No matching assets"),
    (release(2, prerelease=True), "1 prerelease hidden"),
])
def test_drafts_do_not_count_as_published_releases(ui, tmp_path, published, heading):
    window, scan = ui
    window.root.setText(str(tmp_path))
    draft = release(draft=True, prerelease=True, published_at=None)
    succeed(window, scan, *((draft,) if published is None else (draft, published)))
    assert window.catalog.releases[0].draft
    assert window.empty.text() == heading
    assert window.hidden_prereleases.text() == ("1 prerelease hidden" if published and published["prerelease"] else "")
    for button in (window.source_archive, window.source_update):
        assert button.isVisible() == (published is None)
        assert button.isEnabled() == (published is None)


@pytest.mark.parametrize("kind", ["prerelease", "format", "include", "exclude",
                                  "incomplete", "network", "forbidden", "not_found", "cancelled"])
def test_other_empty_views_do_not_offer_source_fallback(ui, tmp_path, kind):
    window, scan = ui
    window.root.setText(str(tmp_path))
    if kind == "prerelease":
        succeed(window, scan, release(prerelease=True))
        assert window.empty.text() == "1 prerelease hidden"
    elif kind in ("format", "include", "exclude"):
        if kind == "format":
            window.formats[".zip"].setChecked(False)
        elif kind == "include":
            window.include.setText("missing-*")
        elif kind == "exclude":
            window.exclude.setText("*.zip")
        succeed(window, scan, release(assets=[asset(name="build.zip")]))
        assert not window.eligible and window.release_meta.text() == "No matching assets"
    else:
        window.url.setText(URL)
        window.scan.click()
        if kind == "incomplete":
            result = decode_response(encode_success(RID, replace(catalog(), complete=False)), RID)
            assert isinstance(result, ScanFailure) and result.code == "incomplete_scan"
            scan.finish(result, "FAILED")
        elif kind == "cancelled":
            window.cancel.click()
            scan.finish(None, "CANCELLED")
        else:
            scan.finish(ScanFailure(RID, kind), "FAILED")
        assert not window.history_button.isEnabled() and not window.mirror_update.isEnabled()
    assert window.empty.text() != "Source-only repository"
    assert not window.source_archive.isVisible() and not window.source_update.isVisible()


@pytest.mark.parametrize("mismatch,repository", [
    ("https://github.com/Owner/Other", RepositoryTarget("Owner", "Other")),
    ("https://github.com/Other/Repo", RepositoryTarget("Other", "Repo")),
    ("https://codeberg.org/Owner/Repo", RepositoryTarget("Owner", "Repo", "codeberg", "codeberg.org")),
    ("https://example.org/Owner/Repo", None),
])
def test_mismatched_edit_blocks_downloads_but_verifies_entered_mirror(ui, tmp_path, monkeypatch,
                                                                    mismatch, repository):
    window, scan = ui
    window.root.setText(str(tmp_path))
    scan_empty(window, scan)
    calls = []
    monkeypatch.setattr(window.history_controller, "start",
                        lambda repo, root, *, operation: calls.append((repo, root, operation)))
    window.url.setText(mismatch)
    assert window.empty.text() == "Source-only repository"
    assert "scanned repository" in window.center_description.text()
    for control in (window.source_archive, window.source_update):
        assert not control.isEnabled()
        control.click()
    for operation in ("snapshot", "update"):
        window._archive_history(operation=operation)
    assert not calls
    assert window.mirror_verify.isEnabled() == (repository is not None)
    window.mirror_verify.click()
    assert calls == ([(repository, str(tmp_path), "verify")] if repository is not None else [])
    window.url.setText("github.com/owner/repo")
    assert window.source_archive.isEnabled() and window.source_update.isEnabled()


def test_new_scan_clears_state_and_ignores_older_result(ui, tmp_path):
    window, scan = ui
    window.root.setText(str(tmp_path))
    scan_empty(window, scan)
    stale = ScanSuccess(RID, catalog())
    current = "b" * 32

    def start(url):
        scan.job = current
        scan.state_changed.emit(current, "RUNNING")
        return True

    scan.start = start
    window.scan.click()
    scan.terminal.emit(RID, "COMPLETED", stale)
    assert window.active_id == current
    assert window.empty.text() == "Scanning releases"
    assert not window.source_archive.isVisible() and not window.source_archive.isEnabled()
    assert not window.history_button.isEnabled()
    scan.finish(ScanFailure(current, "network"), "FAILED", rid=current)


@pytest.mark.parametrize("target,reason", [
    (RepositoryTarget("Owner", "Other"), "scan returned a different repository"),
    (RepositoryTarget("bad owner", "Repo"), ERRORS["invalid_repository"]),
])
def test_unbound_catalog_keeps_source_history_disabled(ui, tmp_path, target, reason):
    window, scan = ui
    window.root.setText(str(tmp_path))
    window.url.setText(URL)
    window.scan.click()
    scan.finish(ScanSuccess(RID, replace(catalog(), repository=target)))
    assert window.source_archive.isVisible() and window.source_update.isVisible()
    assert not window.source_archive.isEnabled() and not window.source_update.isEnabled()
    assert reason in window.center_description.text()
    if target.repo == "Other":
        window.url.setText("https://github.com/Owner/Other")
        assert not window.source_archive.isEnabled()


def test_source_only_destination_and_missing_git(ui, tmp_path, monkeypatch):
    window, scan = ui
    window.root.setText("relative")
    scan_empty(window, scan)
    assert not window.source_archive.isEnabled() and not window.source_update.isEnabled()
    assert ERRORS["unsafe_path"] in window.center_description.text()
    window.root.setText(str(tmp_path / "missing"))
    window.source_archive.click()
    until(lambda: ERRORS["unsafe_path"] in window.history_status.text() and not window._history_pending)
    window.root.setText(str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    window.source_update.click()
    until(lambda: ERRORS["git_missing"] in window.history_status.text() and not window._history_pending)
    assert window.source_update.isEnabled()


def test_source_only_cancel_uses_existing_control(ui, tmp_path, monkeypatch):
    window, scan = ui
    entered = threading.Event()

    def work(*args, cancel, **kwargs):
        entered.set()
        deadline = time.monotonic() + 2
        while not cancel() and time.monotonic() < deadline:
            time.sleep(.005)
        assert cancel()
        raise gh.HistoryError("cancelled")

    monkeypatch.setattr(gh, "update_mirror", work)
    window.root.setText(str(tmp_path))
    scan_empty(window, scan)
    window.source_update.click()
    until(entered.is_set)
    assert window.history_cancel.isVisible() and window.history_cancel.isEnabled()
    assert not window.source_archive.isEnabled() and not window.source_update.isEnabled()
    window.history_cancel.click()
    until(lambda: ERRORS["cancelled"] in window.history_status.text() and not window._history_pending)
    assert window.source_update.isEnabled()


def test_source_only_bundle_restores_offline_and_repeat_preserves_it(ui, local, tmp_path):
    window, scan = ui
    source, root, commit, _ = local
    window.root.setText(str(root))
    scan_empty(window, scan)
    assert not list(root.rglob("history.bundle")) and not window._history_pending
    window.source_archive.click()
    until(lambda: not window._history_pending, seconds=10)
    assert "Archived Git history" in window.history_status.text()
    bundle, = root.rglob("history.bundle")
    saved = snapshot_bytes(bundle.parent)
    window.source_archive.click()
    until(lambda: not window._history_pending, seconds=10)
    assert "already up to date" in window.history_status.text()
    assert list(root.rglob("history.bundle")) == [bundle]
    assert snapshot_bytes(bundle.parent) == saved
    remove_source(source)
    restored = tmp_path / "restored"
    git(tmp_path, "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
        "clone", "--ref-format=reftable", str(bundle), str(restored))
    assert git(restored, "rev-parse", "HEAD") == commit
    assert git(restored, "rev-parse", "refs/tags/v1") == commit


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_source_only_controls_fit_and_tab_at_minimum_size(ui, tmp_path, theme):
    window, scan = ui
    window.resize(820, 520)
    window.set_appearance(theme)
    window.root.setText(str(tmp_path))
    scan_empty(window, scan)
    assert (window.width(), window.height()) == (820, 520)
    viewport = window.center_overlay.viewport()
    for button in (window.source_archive, window.source_update):
        assert viewport.rect().contains(QRect(button.mapTo(viewport, button.rect().topLeft()), button.size()))
        assert button.width() >= button.sizeHint().width()
    window.browse.setFocus()
    for button in (window.source_archive, window.source_update, window.mirror_verify):
        QTest.keyClick(window.focusWidget(), Qt.Key_Tab)
        assert window.focusWidget() is button


def test_compact_source_only_collapses_open_releases_pane(ui):
    window, scan = ui
    window.resize(820, 520)
    succeed(window, scan, release())
    APP.processEvents()
    toggle = window.releases_toggle
    pane = window.splitter.widget(0)
    assert toggle.isVisible() and not toggle.isChecked()
    toggle.click()
    APP.processEvents()
    assert pane.isVisible() and window.splitter.sizes()[0] > 0

    target = RepositoryTarget("Owner", "Source")
    scan_empty(window, scan, "https://github.com/Owner/Source", target)
    assert (window.width(), window.height()) == (820, 520)
    assert window.empty.text() == "Source-only repository"
    assert not toggle.isChecked() and pane.isHidden()
    assert window.splitter.sizes()[0] == 0

    succeed(window, scan, release())
    APP.processEvents()
    assert toggle.isVisible() and not toggle.isChecked() and pane.isHidden()
    toggle.setFocus()
    QTest.keyClick(toggle, Qt.Key_Space)
    APP.processEvents()
    assert toggle.isChecked() and pane.isVisible()
    assert window.release_list.isVisible() and window.splitter.sizes()[0] > 0
