"""Offline, isolated Library panel interaction."""
import os
import sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QTimer
from threading import Event
from PySide6.QtTest import QTest
import hashlib
import sqlite3
from time import perf_counter
import pytest
from covearchive.library import model as m, transfer as t
from covearchive.ui import library_panel as p
from covearchive.ui.library_store import LibraryStore
from covearchive.ui.library_panel import LibraryPanel
from library_fixtures import git_sync_document, encoded
from pathlib import Path
from test_ui_window import ui

APP = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    for key, child in [("LOCALAPPDATA", "appdata"), ("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state")]:
        monkeypatch.setenv(key, str(tmp_path / child))
    assert all(Path(os.environ[key]).is_relative_to(tmp_path) for key in ("LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_STATE_HOME"))
    monkeypatch.setattr(p.W.QFileDialog, "getOpenFileName", lambda *a: (str(tmp_path / "import.json"), ""))


@pytest.fixture
def panel(tmp_path, monkeypatch):
    from test_ui_window import FakeController, ArchiveBridge
    def forbidden(*args, **kwargs): pytest.fail("Library attempted scan/enqueue")
    monkeypatch.setattr(FakeController, "start", forbidden); monkeypatch.setattr(ArchiveBridge, "enqueue", forbidden)
    value = LibraryPanel(LibraryStore(tmp_path / "data" / "library.json"), protected=lambda: [tmp_path / "state", tmp_path / "archive", None, ""]); value.resize(820, 520); value.show()
    value.activateWindow(); APP.processEvents()
    yield value
    value.close()


def select(panel, identity):
    row = next(i for i, r in enumerate(panel.model.rows) if r.identity == identity)
    panel.table.selectRow(row)


def scope(panel, id):
    panel.scope = id; panel.refresh()


def idle(panel):
    deadline = perf_counter() + 5
    while getattr(panel, "busy", False) and perf_counter() < deadline: APP.processEvents(); QTest.qWait(5)
    assert not getattr(panel, "busy", False)


def test_add_bulk_real_paste_and_normal_field_paste(panel, monkeypatch):
    panel.url.setText("https://github.com/o/r"); panel.add.click(); panel.url.setText("https://github.com/o/r"); panel.add.click()
    assert "Added 0, skipped 1 duplicate(s), failed 0" == panel.status.text()
    saves = []; save = panel.store.save
    monkeypatch.setattr(panel.store, "save", lambda lib: saves.append(lib) or save(lib))
    monkeypatch.setattr(p.W.QInputDialog, "getMultiLineText", lambda *a: ("https://github.com/o/s\nSECRET_INVALID\nhttps://github.com/o/r", True))
    panel.bulk.click(); assert "Added 1, skipped 1 duplicate(s), failed 1" in panel.status.text()
    assert len(saves) == 1
    assert "Line 2:" in panel.status.text() and "SECRET" not in panel.status.text()
    for text in ["https://github.com/o/t", "https://github.com/o/u\nhttps://github.com/o/v"]:
        APP.clipboard().setText(text); panel.table.setFocus(); APP.processEvents()
        QTest.keyClick(panel.table, Qt.Key_V, Qt.ControlModifier); APP.processEvents()
    assert len(panel.library.repositories) == 5
    APP.clipboard().setText("normal paste"); panel.url.clear(); panel.url.setFocus()
    QTest.keyClick(panel.url, Qt.Key_V, Qt.ControlModifier)
    assert panel.url.text() == "normal paste" and len(panel.library.repositories) == 5
    APP.clipboard().setText("https://github.com/o/field-only"); panel._paste(); QTest.keyClick(panel.url, Qt.Key_V, Qt.ControlModifier)
    assert len(panel.library.repositories) == 5
    panel.table.setFocus(); APP.clipboard().setText("x" * (1024 * 1024 + 1)); panel._paste()
    assert "1 MiB" in panel.status.text()


def test_collections_repositories_recursive_search_and_confirmations(panel, monkeypatch):
    texts = iter(["Parent", "Child", "Renamed", "Friendly / Name", ""])
    monkeypatch.setattr(p.W.QInputDialog, "getText", lambda *a: (next(texts), True))
    messages = []
    monkeypatch.setattr(p.W.QMessageBox, "question", lambda *a: messages.append(a[2]) or p.W.QMessageBox.Yes)
    panel.new.click(); parent = panel.library.collections[0].id; scope(panel, parent); panel.new.click()
    child = panel.library.collections[1].id; scope(panel, child)
    panel.url.setText("https://github.com/o/r"); panel.add.click(); panel.rename_collection.click()
    scope(panel, parent); assert panel.model.rowCount() == 1 and "(1)" in panel.tree.currentItem().text(0)
    panel.search.setText("renamed"); assert panel.model.rowCount() == 1
    panel.search.setText("absent"); assert panel.model.rowCount() == 0
    panel.search.clear(); select(panel, "github:o/r"); panel.rename.click()
    assert panel.library.repositories[0].name == "Friendly / Name"
    select(panel, "github:o/r"); panel.rename.click(); assert panel.library.repositories[0].name is None
    scope(panel, child); panel.delete.click(); assert panel.library.repositories[0].collection == parent
    assert "1 collections" in messages[-1] and "1 repositories" in messages[-1] and "archives are kept" in messages[-1]
    select(panel, "github:o/r"); monkeypatch.setattr(p.W.QInputDialog, "getItem", lambda *a: (a[3][0], True)); panel.move.click()
    scope(panel, m.UNSORTED); select(panel, "github:o/r"); panel.remove.click()
    assert not panel.library.repositories and "saved rules are NOT touched" in messages[-1]


@pytest.mark.parametrize("bad,cancel,fail", [(False, False, False), (True, False, False), (False, True, False), (False, False, True)])
def test_import_preview_commit_duplicate_invalid_cancel(panel, tmp_path, monkeypatch, bad, cancel, fail):
    if fail:
        from test_library_store import failed_save
        panel._add(text="https://github.com/o/local"); failed_save(monkeypatch, "commit")
        before = panel.library; stored = panel.store.load()[0]; raw = panel.store.path.read_bytes()
    path = tmp_path / "import.json"; path.write_bytes(encoded(git_sync_document(10, invalid=bad)))
    previews = []
    def preview(box):
        buttons = [b for b in box.buttons() if b.text() == "Import"]
        previews.append((box.text(), box.detailedText(), len(buttons)))
        if buttons and not cancel and not fail: panel.library = panel.library.add_repository("https://github.com/o/during-preview")
        if buttons and not cancel: buttons[0].click()
        return 0
    monkeypatch.setattr(p.W.QMessageBox, "exec", preview)
    panel.import_button.click(); idle(panel)
    if fail:
        assert panel.library == before and panel.store.load()[0] == stored and panel.store.path.read_bytes() == raw and "commit" in panel.status.text()
        return
    if not bad:
        assert "Import from Git-Sync data\n10 repositories in the file\n" in previews[0][0] and "duplicates (already in your list or repeated in the file; skipped)\n" in previews[0][0] and "CoveArchive ignores this and never deletes archived versions." in previews[0][0]
    assert previews[-1][2] == int(not bad) and "SECRET" not in repr(previews)
    assert bool(panel.library.repositories) == (not bad and not cancel)
    if not bad and not cancel:
        assert len(panel.library.repositories) == 11 and any(r.identity == "github:o/during-preview" for r in panel.library.repositories) and m.load_library(panel.store.path.read_bytes()) == panel.library
        before = m.dump_library(panel.library); panel.import_button.click(); idle(panel)
        assert m.dump_library(panel.library) == before and "Added 0" in panel.status.text()


def test_transaction_failures_readonly_export_and_files_preserved(panel, tmp_path, monkeypatch):
    archive = tmp_path / "archives"; archive.mkdir(); (archive / "old.zip").write_bytes(b"ARCHIVE")
    prefs = tmp_path / "preferences.json"; prefs.write_bytes(b"RULES")
    queue = tmp_path / "queue.db"
    db = sqlite3.connect(queue); db.execute("CREATE TABLE jobs (id INTEGER)"); db.execute("INSERT INTO jobs VALUES (1)"); db.commit(); db.close()
    protected = [archive / "old.zip", prefs, queue]; hashes = [hashlib.sha256(f.read_bytes()).digest() for f in protected]
    panel._add(text="https://github.com/o/r")
    assert panel._transaction(lambda lib: lib.create_collection("Group", id="g").create_collection("Child", "g", id="c"))
    assert panel._transaction(lambda lib: lib.move_repositories(["github:o/r"], "c").rename_collection("g", "Renamed"))
    assert panel._transaction(lambda lib: lib.move_collection("c", None).delete_collection("g").delete_collection("c"))
    plan = t.build_plan(encoded(git_sync_document(20)), panel.library); assert panel._transaction(plan.apply)
    assert panel._transaction(lambda lib: lib.remove_repositories([lib.repositories[-1].identity]))
    original = panel.library
    path = tmp_path / "export.json"; monkeypatch.setattr(p.W.QFileDialog, "getSaveFileName", lambda *a: (str(path), ""))
    panel.export_button.click(); assert m.strict_json_loads(path.read_bytes())["format"] == t.FORMAT
    exported = path.read_bytes(); monkeypatch.setattr(p, "atomic_write", lambda *a: "Export failed")
    panel.export_button.click(); assert path.read_bytes() == exported
    monkeypatch.setattr(panel.store, "save", lambda lib: "Save failed"); panel._add(text="https://github.com/o/new")
    assert panel.library == original and panel.status.text() == "Save failed"
    assert [hashlib.sha256(f.read_bytes()).digest() for f in protected] == hashes
    db = sqlite3.connect(queue); assert db.execute("SELECT * FROM jobs").fetchall() == [(1,)]; db.close()
    blocked = tmp_path / "blocked-export.json"; monkeypatch.setattr(p.W.QFileDialog, "getSaveFileName", lambda *a: (str(blocked), ""))
    for raw in (b"broken", b'{"version":2}', None):
        corrupt = tmp_path / "corrupt.json"; corrupt.write_bytes(raw if raw else m.dump_library(m.Library())); readonly = LibraryPanel(LibraryStore(corrupt))
        if raw is None: corrupt.write_bytes(b"external"); readonly._add(text="https://github.com/o/fresh")
        problem = readonly.store.problem; writes = []; monkeypatch.setattr(p, "atomic_write", lambda *a: writes.append(a))
        assert all(not w.isEnabled() for w in [readonly.url, readonly.export_button, *readonly.mutating]) and problem
        readonly.export_button.click(); readonly._export()
        assert not writes and not blocked.exists() and readonly.status.text() == problem
        readonly.close()


def test_scale_accessibility_and_layout(panel, tmp_path, monkeypatch):
    path = tmp_path / "import.json"; path.write_bytes(encoded(git_sync_document(5000)))
    monkeypatch.setattr(p.W.QMessageBox, "exec", lambda box: next(b for b in box.buttons() if b.text() == "Import").click() or 0)
    ticks = [perf_counter()]; timer = QTimer(); timer.timeout.connect(lambda: ticks.append(perf_counter())); timer.start(5)
    panel._import(); assert getattr(panel, "busy", False) and all(not w.isEnabled() for w in [*panel.mutating, panel.export_button])
    idle(panel); timer.stop(); gap = max(b - a for a, b in zip(ticks, ticks[1:])) * 1000
    print(f"5000-entry import: ticks={len(ticks)}, max gap={gap:.3f} ms"); assert len(ticks) > 2 and gap < 250 and panel.export_button.isEnabled()
    panel.search.setText("repo4999"); assert panel.model.rowCount() == 1
    assert panel.table.indexWidget(panel.model.index(0, 0)) is None and panel.table.verticalHeader().isHidden()
    assert panel.sizeHint().width() <= 820 and panel.minimumSizeHint().height() <= 520
    assert panel.status.textFormat() == Qt.PlainText and all(w.accessibleName() for w in [panel.url, panel.search, panel.tree, panel.table, *panel.mutating])
    gate = Event(); read = p.t.read_import_file; before = panel.library; raw = panel.store.path.read_bytes()
    monkeypatch.setattr(p.t, "read_import_file", lambda path: (gate.wait(2), read(path))[1]); panel._import(); panel.close(); gate.set()
    if not panel._future.cancelled(): panel._future.result(timeout=3)
    APP.processEvents(); assert not panel._poll.isActive() and panel.library is before and panel.store.path.read_bytes() == raw


def test_many_collection_performance(panel):
    panel.library = m.Library(tuple(m.Collection(str(i), str(i)) for i in range(2000)),
        tuple(m.Repository("github", f"https://github.com/o/r{i}", collection=str(i % 2000)) for i in range(5000)))
    times = []
    for op in [lambda: panel.library.browse(search="r4999"), panel.refresh, lambda: t.export_library(panel.library)]:
        start = perf_counter(); op(); times.append((perf_counter() - start) * 1000)
    print("2000 collections/5000 repos search/refresh/export ms:", times)
    assert all(ms <= 150 for ms in times)


@pytest.mark.parametrize("raw,expected", [(encoded({"format": "Git-Sync data", "version": 1, "repos": [{"api": "github", "repository": path}]}), expected) for path, expected in [("SECRET:pw@o/r", "Credentials are not allowed"), ("o/r/releases/tag/v1", "Use a repository URL, not a release tag")]] +
    [(b"[]", "Invalid file structure"), (b'{"format":"Git-Sync data","version":1,"version":1,"repos":[]}', "Duplicate JSON fields are not allowed"), (encoded({"format":"Git-Sync data","version":1,"repos":[{"repository":"SECRET", "api":"bad token"}] * 51}), "Invalid repository URL")], ids=["userinfo", "release_tag", "structure", "duplicate", "many"])
def test_selection_states_and_invalid_preview(panel, tmp_path, monkeypatch, raw, expected):
    assert not any(w.isEnabled() for w in [panel.rename_collection, panel.delete, panel.move, panel.rename, panel.remove])
    panel.add.click(); assert panel.status.text() == "Enter a repository URL"
    panel._add(text="https://github.com/o/r"); assert not panel.url.text()
    saves = []; monkeypatch.setattr(panel.store, "save", lambda lib: saves.append(lib) or "Unexpected save")
    panel._add(text="https://github.com/o/r"); select(panel, "github:o/r"); assert panel.move.isEnabled()
    before = m.dump_library(panel.library); plan = t.build_plan(raw, panel.library); previews = []; path = tmp_path / "import.json"; path.write_bytes(raw)
    def preview(box):
        previews.append((box.text(), box.detailedText(), box.standardButtons(), sum(box.buttonRole(b) != p.W.QMessageBox.ActionRole for b in box.buttons())))
        return 0
    monkeypatch.setattr(p.W.QMessageBox, "exec", preview); panel.import_button.click(); idle(panel)
    text, details, buttons, count = previews[0]; assert text.startswith("Nothing was imported") and (buttons, count) == (p.W.QMessageBox.Cancel, 1)
    assert expected in details and ("File structure:" in details if plan.code else "Fix the file and try again." in text)
    assert "SECRET" not in repr(previews) and not saves and m.dump_library(panel.library) == before
    if len(plan.invalid) == 50: assert "50 or more invalid entries" in text


def test_repository_labels_tooltips_and_zero_retention_preview(panel, tmp_path, monkeypatch):
    panel.library = m.Library(repositories=(m.Repository("github", "https://github.com/Owner/Repo"),
        m.Repository("gitea", "https://forge.example/Group/Repo"))); panel.refresh()
    for row, label in enumerate(["Owner/Repo", "forge.example/Group/Repo"]):
        index = panel.model.index(row, 1)
        assert panel.model.data(index) == label and panel.model.data(index, Qt.ToolTipRole) == panel.model.rows[row].url
    path = tmp_path / "import.json"; path.write_bytes(t.export_library(m.Library()))
    previews = []; monkeypatch.setattr(p.W.QMessageBox, "exec", lambda box: previews.append(box.text()) or 0)
    panel.import_button.click(); idle(panel)
    assert previews[0].startswith("Import from a CoveArchive list\n0 repositories in the file\n") and "drop older versions" not in previews[0]


def test_window_library_icon_and_close(ui, tmp_path, monkeypatch):
    v, controller = ui
    from covearchive.queue.store import default_queue_path
    v.settings["archive_root"] = str(tmp_path / "saved-archive"); v.root.setText(str(tmp_path / "typed-archive"))
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "linux")
        assert getattr(v, "_protected_roots", lambda: [])() == [default_queue_path().parent, v.root.text(), v.settings["archive_root"]]
    assert (v.tabs.count(), v.windowIcon().isNull()) == (3, False)
    assert v.tabs.tabText(2) == "Library" and v.windowIcon().availableSizes()[0].width() == 512
    from covearchive.ui import branding
    asset = Path(branding.__file__).parents[1] / "assets/covearchive-icon.png"; raw = asset.read_bytes()
    assert len(raw) == 72823 and raw.startswith(b"\x89PNG\r\n\x1a\n") and hashlib.sha256(raw).hexdigest() == "28ef206c6ddb4e55f7cc8b78bdebfd4308eb8fadf4d8ef82dca5333ddb6a97fb"
    monkeypatch.setattr(branding, "__file__", str(tmp_path / "missing/ui/branding.py")); assert branding.app_icon().isNull()
    enqueued = []; monkeypatch.setattr(v.bridge, "enqueue", lambda *a: enqueued.append(a))
    assert not v.library_panel.library.repositories; v.library_panel._add(text="https://github.com/o/fresh")
    imported = tmp_path / "import.json"; imported.write_bytes(encoded(git_sync_document(2)))
    monkeypatch.setattr(p.W.QMessageBox, "exec", lambda box: next(b for b in box.buttons() if b.text() == "Import").click() or 0)
    v.library_panel.import_button.click(); idle(v.library_panel); assert len(v.library_panel.library.repositories) == 3
    assert controller.calls == [] and enqueued == []
    v.tabs.setCurrentIndex(2); v.library_panel.table.setFocus(); APP.processEvents(); APP.clipboard().setText("https://github.com/o/pasted")
    QTest.keyClick(v.library_panel.table, Qt.Key_V, Qt.ControlModifier); APP.processEvents()
    assert len(v.library_panel.library.repositories) == 4 and controller.calls == [] and enqueued == []
    v.tabs.setCurrentIndex(2); v.close(); assert v.preferences.load()[0]["window"]["tab"] == "browse"
    for source in asset.parents[2].rglob("*.py"):
        assert not any(marker in source.read_bytes() for marker in (b"C:\\Users", b"/home/sin"))


@pytest.mark.parametrize("kind", ["library.json", "preferences.json", "queue.db", "case", "dotdot", "relative", "nested/library.json", "state/queue.sqlite3", "state/queue.sqlite3-wal", "archive/Owner/Repo/v1/manifest.json", "archive/../archive/Owner/Repo/v1/manifest.json"])
def test_export_refuses_application_folder_aliases(panel, tmp_path, monkeypatch, kind):
    if kind == "case" and sys.platform != "win32": pytest.skip("Case aliases require Windows")
    panel._add(text="https://github.com/o/r"); folder = panel.store.path.parent
    for name in ("preferences.json", "queue.db"): (folder / name).write_bytes(b"protected")
    target = folder / kind
    if kind == "case": target = Path(str(panel.store.path).swapcase())
    if kind == "dotdot": target = folder / ".." / folder.name / "library.json"
    if kind == "relative": target = Path(os.path.relpath(panel.store.path))
    if kind.startswith(("state/", "archive/")):
        target = tmp_path / kind; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(b"protected")
    before = panel.library; files = {f: f.read_bytes() for f in folder.iterdir()}
    files[target] = target.read_bytes() if target.exists() else None
    monkeypatch.setattr(p.W.QFileDialog, "getSaveFileName", lambda *a: (str(target), "")); panel._export()
    assert panel.status.text() == "Choose a location outside CoveArchive's data and archive folders"
    assert panel.library == before and {f: f.read_bytes() if f.exists() else None for f in files} == files
    target = tmp_path / "outside.json"; panel._export(); assert m.strict_json_loads(target.read_bytes())["format"] == t.FORMAT


def test_move_unsorted_choice_cannot_collide(panel, monkeypatch):
    panel._transaction(lambda lib: lib.create_collection("(Unsorted)", id="g").add_repository("https://github.com/o/r", collection="g"))
    panel._add(text="https://github.com/o/r.git.git"); assert "Line 1: Invalid repository URL" in panel.status.text()
    for label, expected in [("first", None), ("(Unsorted)", "g")]:
        select(panel, "github:o/r"); monkeypatch.setattr(p.W.QInputDialog, "getItem", lambda *a: (a[3][0] if label == "first" else label, True))
        panel.move.click(); assert panel.library.repositories[0].collection == expected


def test_hidden_import_drops_prepared_result(tmp_path, monkeypatch):
    panel = LibraryPanel(LibraryStore(tmp_path / "hidden" / "library.json")); path = tmp_path / "import.json"; path.write_bytes(encoded(git_sync_document(1)))
    seen = []
    monkeypatch.setattr(p.W.QMessageBox, "exec", lambda box: seen.append(box.text()) or 0)
    panel._import(); idle(panel)
    assert not seen and not panel.store.path.exists() and not panel.library.repositories and panel.isEnabled()
    panel.close()


@pytest.mark.parametrize("kind,raw,allowed", [("json", b'{"schema_version":1}', False), ("empty", b"", False), ("bom", b'\xef\xbb\xbf{"format":"wrong"}', False),
    ("duplicate", b'{"format":"wrong","format":"covearchive-repository-list"}', False), ("large", b'{}'.ljust(8 * 1024 * 1024 + 1), False),
    ("directory", None, False), ("dangling", None, False), ("link", None, False), ("library", m.dump_library(m.Library()), False), ("ads", None, False), ("missing", None, False),
    ("new", None, True), ("export", t.export_library(m.Library()), True), ("export-bom", b'\xef\xbb\xbf' + t.export_library(m.Library()), True)], ids=["json", "empty", "bom", "duplicate", "large", "directory", "dangling", "link", "library", "ads", "missing", "new", "export", "export-bom"])
def test_export_replaces_only_previous_exports(panel, tmp_path, monkeypatch, kind, raw, allowed):
    panel._add(text="https://github.com/o/exported")
    root = tmp_path / "old-root"; root.mkdir(); target = root / ("file:stream" if kind == "ads" else "archive-index.json")
    if kind == "ads" and sys.platform != "win32": pytest.skip("Windows alternate data streams")
    if kind == "missing": target = root / "missing" / "export.json"
    elif kind == "directory": target.mkdir()
    elif kind in ("dangling", "link"):
        try: target.symlink_to(root / "other")
        except OSError: pytest.skip("Symlinks unavailable")
        if kind == "link": (root / "other").write_bytes(b'{"schema_version":1}')
    elif raw is not None: target.write_bytes(raw)
    before = {f: (f.read_bytes() if f.is_file() else None, f.lstat().st_mtime_ns) for f in root.iterdir()}
    calls = []; write = p.atomic_write; memory = panel.library
    monkeypatch.setattr(p, "atomic_write", lambda *a: calls.append(a) or write(*a))
    monkeypatch.setattr(p.W.QFileDialog, "getSaveFileName", lambda *a: (str(target), "")); panel._export()
    if allowed:
        assert calls and target.read_bytes() == t.export_library(memory) and t.is_export_file(target) and panel.library is memory
    else:
        assert not calls and panel.status.text() == "Choose a new file name; only a previous CoveArchive export can be replaced"
        assert not t.is_export_file(target)
        assert {f: (f.read_bytes() if f.is_file() else None, f.lstat().st_mtime_ns) for f in root.iterdir()} == before and panel.library is memory
