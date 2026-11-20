"""Offline, isolated Library panel interaction."""
import os
import sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QTimer, QItemSelectionModel
from PySide6.QtGui import QContextMenuEvent
from threading import Event, current_thread
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


def test_open_in_browse_selection_and_accessibility(panel):
    assert not panel.open_button.isEnabled()
    panel.library = t.build_plan(encoded(git_sync_document(2)), panel.library).apply(panel.library); panel.refresh()
    assert not panel.open_button.isEnabled()
    select(panel, panel.model.rows[0].identity); assert panel.open_button.isEnabled()
    assert panel.open_button.text() == panel.open_button.accessibleName() == panel.open_button.accessibleDescription() == "Open in Browse"
    assert panel.open_button not in panel.mutating and panel.auto_button.nextInFocusChain() is panel.open_button
    panel.table.selectAll(); assert not panel.open_button.isEnabled()
    emitted = []; panel.open_requested.connect(emitted.append)
    panel.open_button.click(); QTest.keyClick(panel.table, Qt.Key_Return)
    assert emitted == []
    panel.table.clearSelection(); assert not panel.open_button.isEnabled()
    QTest.keyClick(panel.table, Qt.Key_Enter); assert emitted == []


@pytest.mark.parametrize("readonly", [False, True])
@pytest.mark.parametrize("provider,url", [("github", "https://github.com/Owner/Repo"),
    ("gitlab", "https://gitlab.com/Group/Sub/Repo"), ("gitea", "https://codeberg.org/Owner/Repo"),
    ("gitea", "https://forge.example/Owner/Repo")])
def test_open_in_browse_emits_repository_url(panel, readonly, provider, url):
    panel.library = m.Library(repositories=(m.Repository(provider, url),)); panel.refresh()
    panel.store.writable = not readonly; panel._lock()
    select(panel, panel.model.rows[0].identity)
    assert panel.open_button.isEnabled() and panel.model.rows[0].active == (provider == "github")
    emitted = []; panel.open_requested.connect(emitted.append); panel.open_button.click()
    assert emitted == [panel.model.rows[0].url] == [url]
    panel.table.clearSelection(); assert not panel.open_button.isEnabled()


@pytest.mark.parametrize("activation", ["double_click", Qt.Key_Return, Qt.Key_Enter])
def test_open_in_browse_table_activation(panel, activation):
    panel.library = t.build_plan(encoded(git_sync_document(2)), panel.library).apply(panel.library); panel.refresh()
    repo = next(r for r in panel.model.rows if not r.active); select(panel, repo.identity)
    emitted = []; panel.open_requested.connect(emitted.append)
    panel.table.setFocus(); APP.processEvents()
    if activation == "double_click":
        index = panel.model.index(next(i for i, r in enumerate(panel.model.rows) if r.identity == repo.identity), 0)
        point = panel.table.visualRect(index).center()
        QTest.mouseClick(panel.table.viewport(), Qt.LeftButton, pos=point)
        QTest.mouseDClick(panel.table.viewport(), Qt.LeftButton, pos=point)
    else: QTest.keyClick(panel.table, activation)
    assert emitted == [repo.url]


@pytest.fixture
def menu_panel(panel, monkeypatch):
    panel.library = m.Library(repositories=tuple(m.Repository('github', f'https://github.com/Owner/Repo{i}') for i in range(3)))
    panel.refresh(); APP.processEvents()
    monkeypatch.setattr(p.W.QMenu, 'popup', lambda menu, pos: None)
    return panel


def menu_at(panel, row):
    return panel._context_menu(panel.table.visualRect(panel.model.index(row, 0)).center())


@pytest.mark.parametrize('readonly', [False, True])
@pytest.mark.parametrize('count', [0, 1, 3])
def test_repository_menu_order_and_button_states(menu_panel, readonly, count):
    v = menu_panel; v.store.writable = not readonly
    if count == 1: v.table.selectRow(0)
    elif count: v.table.selectAll()
    menu = v._build_menu(); actions = menu.actions()
    assert [a.text() for a in actions] == ['Open in Browse', 'Open on website', 'Copy URL', '',
        'Move to...', 'Rename...', 'Auto-Archive...', 'Remove from list...']
    assert [a.isSeparator() for a in actions] == [False, False, False, True, False, False, False, False]
    assert [a.isEnabled() for a in actions[:3]] == [v.open_button.isEnabled()] * 3 == [count == 1] * 3
    assert [a.isEnabled() for a in actions[4:]] == [b.isEnabled() for b in (v.move, v.rename, v.auto_button, v.remove)]
    assert menu.parent() is v and menu.accessibleName() and menu.accessibleDescription()
    if readonly: assert not any(a.isEnabled() for a in actions[4:])


def test_repository_menu_empty_area_and_row_selection(menu_panel):
    v = menu_panel
    point = v.table.viewport().rect().bottomRight()
    assert not v.table.indexAt(point).isValid()
    assert v._context_menu(point) is None
    assert not v.table.selectionModel().selectedRows()
    v.table.selectRow(0)
    v.table.selectionModel().select(v.model.index(1, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
    assert len(v.table.selectionModel().selectedRows()) == 2
    menu = menu_at(v, 1)
    assert len(v.table.selectionModel().selectedRows()) == 2 and not menu.actions()[0].isEnabled()
    menu.deleteLater()
    menu = menu_at(v, 2)
    assert [i.row() for i in v.table.selectionModel().selectedRows()] == [2]
    assert v.table.currentIndex().row() == 2 and menu.actions()[0].isEnabled()
    menu.deleteLater()


def test_repository_menu_mouse_context_event(menu_panel, monkeypatch):
    v = menu_panel; seen = []
    monkeypatch.setattr(p.W.QMenu, 'popup', lambda menu, pos: seen.append((menu, pos)))
    assert v.table.contextMenuPolicy() == Qt.CustomContextMenu
    point = v.table.visualRect(v.model.index(1, 0)).center()
    QTest.mouseClick(v.table.viewport(), Qt.RightButton, pos=point)
    APP.sendEvent(v.table.viewport(), QContextMenuEvent(QContextMenuEvent.Mouse, point, v.table.viewport().mapToGlobal(point)))
    assert seen and seen[-1][1] == v.table.viewport().mapToGlobal(point)
    assert [i.row() for i in v.table.selectionModel().selectedRows()] == [1]
    v.table.selectAll(); QTest.mouseClick(v.table.viewport(), Qt.RightButton, pos=point)
    APP.sendEvent(v.table.viewport(), QContextMenuEvent(QContextMenuEvent.Mouse, point, v.table.viewport().mapToGlobal(point)))
    assert len(v.table.selectionModel().selectedRows()) == 3


@pytest.mark.parametrize('activation', ['menu', 'shift_f10', 'context_event'])
def test_repository_menu_keyboard_at_current_row(menu_panel, monkeypatch, activation):
    v = menu_panel; seen = []; v.table.selectRow(1); v.table.setFocus()
    monkeypatch.setattr(p.W.QMenu, 'popup', lambda menu, pos: seen.append((menu, pos)))
    if activation == 'context_event':
        point = v.table.rect().bottomRight()
        APP.sendEvent(v.table, QContextMenuEvent(QContextMenuEvent.Keyboard, point, v.table.mapToGlobal(point)))
    else: QTest.keyClick(v.table, Qt.Key_Menu if activation == 'menu' else Qt.Key_F10,
        Qt.NoModifier if activation == 'menu' else Qt.ShiftModifier)
    assert len(seen) == 1
    assert seen[0][1] == v.table.viewport().mapToGlobal(v.table.visualRect(v.model.index(1, 0)).center())
    assert [i.row() for i in v.table.selectionModel().selectedRows()] == [1]
    v.table.clearSelection(); v.table.setCurrentIndex(v.model.index(-1, -1)); seen.clear()
    QTest.keyClick(v.table, Qt.Key_Menu); assert not seen


@pytest.mark.parametrize('readonly', [False, True])
def test_repository_menu_copy_and_open_in_browse(menu_panel, readonly):
    v = menu_panel; v.store.writable = not readonly; emitted = []; v.open_requested.connect(emitted.append)
    actions = menu_at(v, 1).actions(); url = v.model.rows[1].url
    actions[0].trigger(); assert emitted == [url]
    APP.clipboard().setText('before'); actions[2].trigger()
    assert APP.clipboard().text() == url and v.status.text() == 'Copied URL'
    assert v.status.property('tone') == 'success'


@pytest.mark.parametrize('success', [False, True])
@pytest.mark.parametrize('readonly', [False, True])
def test_repository_menu_open_website(menu_panel, monkeypatch, success, readonly):
    v = menu_panel; v.store.writable = not readonly; calls = []
    monkeypatch.setattr(p.QDesktopServices, 'openUrl', lambda url: calls.append(url) or success)
    action = menu_at(v, 0).actions()[1]; assert action.isEnabled(); action.trigger()
    assert len(calls) == 1 and isinstance(calls[0], p.QUrl) and calls[0].toString() == v.model.rows[0].url
    if not success:
        assert v.status.text() == 'Could not open the browser' and v.status.property('tone') == 'error'


def test_repository_menu_http_disables_only_website(menu_panel, monkeypatch):
    v = menu_panel; url = 'http://forge.example/Owner/Repo'
    v.library = m.Library(repositories=(m.Repository('gitea', url),)); v.refresh()
    monkeypatch.setattr(p.QDesktopServices, 'openUrl', lambda *_: pytest.fail('disabled URL opened'))
    actions = menu_at(v, 0).actions()
    assert [a.isEnabled() for a in actions[:3]] == [True, False, True]
    actions[1].trigger(); actions[2].trigger(); assert APP.clipboard().text() == url


def test_repository_menu_existing_mutating_slots(automatic_panel, monkeypatch):
    v = automatic_panel.panel; calls = []
    for name in ('_move', '_rename', '_auto_archive', '_remove'):
        monkeypatch.setattr(v, name, lambda name=name: calls.append(name))
    v.table.selectRow(0); actions = v._build_menu().actions()
    assert all(a.isEnabled() for a in actions[4:])
    for action in actions[4:]: action.trigger()
    assert calls == ['_move', '_rename', '_auto_archive', '_remove']


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
    gui_thread = current_thread(); threads = {}; gate = Event(); entered = Event()
    read, build = p.t.read_import_file, p.t.build_plan
    def worker_read(path):
        threads["read"] = current_thread()
        assert current_thread() is not gui_thread
        entered.set()
        assert gate.wait(10), "Import worker gate timed out"
        return read(path)
    def worker_build(*args):
        threads["build"] = current_thread()
        assert current_thread() is not gui_thread
        return build(*args)
    monkeypatch.setattr(p.t, "read_import_file", worker_read)
    monkeypatch.setattr(p.t, "build_plan", worker_build)
    ticks = []; timer = QTimer(); timer.setTimerType(Qt.PreciseTimer)
    timer.timeout.connect(lambda: ticks.append(perf_counter()))
    start = perf_counter(); timer.start(5)
    try:
        panel._import()
        deadline = start + 5
        while (len(ticks) < 6 or not entered.is_set()) and perf_counter() < deadline:
            APP.processEvents(); QTest.qWait(1)
        assert entered.is_set() and len(ticks) >= 6, "GUI heartbeat timed out while import worker was held"
        assert panel.busy and not panel._future.done()
        assert all(not w.isEnabled() for w in [*panel.mutating, panel.export_button])
        gate.set(); idle(panel)
        ticks.append(perf_counter())
        gap = max(b - a for a, b in zip([start, *ticks], ticks)) * 1000
        print(f"5000-entry import whole span: heartbeats={len(ticks) - 1}, max gap={gap:.3f} ms")
        assert gap < 250
    finally:
        timer.stop(); gate.set()
    assert set(threads) == {"read", "build"} and all(thread is not gui_thread for thread in threads.values())
    assert panel.export_button.isEnabled()
    monkeypatch.setattr(p.t, "read_import_file", read)
    monkeypatch.setattr(p.t, "build_plan", build)
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
    assert (v.tabs.count(), v.windowIcon().isNull()) == (4, False)
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
@pytest.fixture
def automatic_panel(tmp_path):
    from types import SimpleNamespace as NS
    from covearchive.ui.auto_archive import AutoArchive
    from test_ui_auto_archive import Bridge, Scan
    clock=[10000.]; scan=Scan(); bridge=Bridge(); store=LibraryStore(tmp_path/'auto-library.json')
    assert store.save(m.Library().add_repository('https://github.com/o/r')) is None
    settings=dict(archive_root=str(tmp_path/'archive'),repositories={'o/r':dict(include=[],exclude=[],extensions=['.zip'],prereleases=False)})
    auto=AutoArchive(scan,bridge,lambda:value.library,lambda:settings,clock=lambda:clock[0],tick_ms=100000)
    value=LibraryPanel(store,auto=auto); value.changed.connect(auto.sync); value.resize(1000,600); value.show(); auto.start()
    yield NS(panel=value,auto=auto,clock=clock,scan=scan,bridge=bridge,settings=settings)
    auto.shutdown(); value.close()


def auto_dialog(monkeypatch,choice='Every 6 hours',answer=True):
    calls=[]
    monkeypatch.setattr(p.W.QInputDialog,'getItem',lambda *a:(calls.append(a) or choice,True))
    monkeypatch.setattr(p.W.QMessageBox,'question',lambda *a:p.W.QMessageBox.Yes if answer else p.W.QMessageBox.No)
    return calls


def test_auto_columns_exist_before_opt_in(panel):
    assert panel.model.columnCount()==6
    assert panel.model.headerData(4,Qt.Horizontal)=='Auto-Archive'
    panel._add(text='https://github.com/o/r'); select(panel,'github:o/r'); assert not panel.auto_button.isEnabled()


def test_auto_enable_persists_arms_and_disables_without_queue_changes(automatic_panel,monkeypatch,tmp_path):
    e=automatic_panel; v=e.panel; assert not v.auto_button.isEnabled(); select(v,'github:o/r')
    assert v.auto_button.isEnabled(); calls=auto_dialog(monkeypatch); v.auto_button.click()
    assert calls[0][3]==['Off','Every 1 hour','Every 6 hours','Every 12 hours','Every 24 hours'] and calls[0][4]==0
    assert m.load_library(v.store.path.read_bytes()).repositories[0].auto_archive==6
    assert e.auto.status('o/r').next_check==e.clock[0]+21600 and not e.scan.calls and not e.bridge.calls
    archive=tmp_path/'old.zip'; archive.write_bytes(b'old archive')
    select(v,'github:o/r'); calls=auto_dialog(monkeypatch,'Off')
    monkeypatch.setattr(p.W.QMessageBox,'question',lambda *a:pytest.fail('disabling must not ask for consent'))
    v.auto_button.click(); assert calls[0][4]==2 and archive.read_bytes()==b'old archive'
    assert v.library.repositories[0].auto_archive is None and not e.bridge.calls
    e.clock[0]+=100000; e.auto.tick(); assert not e.scan.calls
    assert 'archives and queued downloads are unchanged' in v.status.text()
    for widget in (v.auto_button,v.auto_detail): assert widget.accessibleName() and widget.accessibleDescription()
    assert v.auto_detail.textFormat()==Qt.PlainText and v.auto_detail.wordWrap()


@pytest.mark.parametrize('missing',['rules','root','no'])
def test_auto_refusal_is_atomic(automatic_panel,monkeypatch,missing):
    e=automatic_panel; v=e.panel; select(v,'github:o/r'); before=v.store.path.read_bytes()
    if missing=='rules': e.settings['repositories'].clear()
    elif missing=='root': e.settings['archive_root']=''
    auto_dialog(monkeypatch,answer=missing!='no'); v.auto_button.click()
    assert v.store.path.read_bytes()==before and v.library.repositories[0].auto_archive is None
    if missing!='no': assert e.auto.problem(v.library.repositories[0]) in v.status.text()


def test_auto_inactive_readonly_refresh_selection_detail_and_message(automatic_panel,monkeypatch):
    e=automatic_panel; v=e.panel; assert v._transaction(lambda lib:lib.add_repository('https://host/g/r',provider='gitlab'))
    select(v,'gitlab:host/g/r'); auto_dialog(monkeypatch); v.auto_button.click()
    assert all(r.auto_archive is None for r in v.library.repositories) and 'active' in v.status.text().lower()
    v.table.selectAll(); v.auto_button.click(); assert v.library.repositories[0].auto_archive==6
    assert v.library.repositories[1].auto_archive is None and 'skipped' in v.status.text().lower()
    select(v,'github:o/r'); selected=v._selected(); resets=[]; v.model.modelReset.connect(lambda:resets.append(1))
    v.status.setText('User action'); e.auto.schedule.failed('o/r','not_found'); e.auto.changed.emit()
    assert v._selected()==selected and not resets and v.status.text()=='User action'
    assert 'URL' in v.auto_detail.text() and v.model.data(v.model.index(0,4))=='Error'
    v.table.selectAll(); assert not v.auto_detail.text()
    v.store.writable=False; v._lock(); assert not v.auto_button.isEnabled()


def test_auto_add_bulk_import_and_readd_stay_off(automatic_panel,monkeypatch,tmp_path):
    e=automatic_panel; v=e.panel; select(v,'github:o/r'); auto_dialog(monkeypatch); v.auto_button.click()
    select(v,'github:o/r'); v.remove.click(); v._add(text='https://github.com/o/r')
    monkeypatch.setattr(p.W.QInputDialog,'getMultiLineText',lambda *a:('https://github.com/o/s',True)); v.bulk.click()
    path=tmp_path/'import.json'; path.write_bytes(encoded(git_sync_document(2)))
    monkeypatch.setattr(p.W.QFileDialog,'getOpenFileName',lambda *a:(str(path),''))
    monkeypatch.setattr(p.W.QMessageBox,'exec',lambda box:next(b for b in box.buttons() if b.text()=='Import').click() or 0)
    v.import_button.click(); idle(v)
    assert len(v.library.repositories)==4 and all(r.auto_archive is None for r in v.library.repositories)
    assert not e.scan.calls and not e.bridge.calls


@pytest.mark.parametrize('count',[1,2])
def test_auto_status_pluralization(automatic_panel,monkeypatch,count):
    e=automatic_panel; v=e.panel; noun='repository' if count==1 else 'repositories'
    for i in range(1,count):
        v._add(text=f'https://github.com/o/r{i}'); e.settings['repositories'][f'o/r{i}']=e.settings['repositories']['o/r'].copy()
    auto_dialog(monkeypatch); v.table.selectAll(); v.auto_button.click()
    assert v.status.text()==f'Auto-Archive on for {count} {noun} every 6 hours; first check in 6 hours'
    auto_dialog(monkeypatch,'Off'); v.table.selectAll(); v.auto_button.click()
    assert v.status.text()==f'Auto-Archive off for {count} {noun}; archives and queued downloads are unchanged'
    for i in range(count): assert v._transaction(lambda lib,i=i:lib.add_repository(f'https://host/g/r{i}',provider='gitlab'))
    auto_dialog(monkeypatch); v.table.selectAll(); v.auto_button.click()
    assert v.status.text().endswith(f'; {count} inactive {noun} skipped')
    v.search.setText('host/g'); v.table.selectAll(); v.auto_button.click()
    assert v.status.text()==f'No active GitHub {noun} selected; {count} inactive {noun} skipped'


@pytest.mark.parametrize('size',[(1180,720),(820,520)])
def test_auto_columns_fit_and_full_tooltips(automatic_panel,size):
    e=automatic_panel; v=e.panel; assert v._transaction(lambda lib:lib.set_auto_archive('github:o/r',6))
    e.auto.schedule.succeeded('o/r',''); e.auto.changed.emit(); v.resize(*size); APP.processEvents()
    for column in (4,5):
        index=v.model.index(0,column); text=v.model.data(index)
        assert v.table.columnWidth(column)>=v.table.fontMetrics().horizontalAdvance(text)
        assert v.model.data(index,Qt.ToolTipRole)==text
    select(v,'github:o/r'); e.auto.schedule.failed('o/r','not_found'); e.auto.changed.emit()
    assert 'URL' in v.model.data(v.model.index(0,4),Qt.ToolTipRole)
    assert v.auto_detail.text(); v.search.setText('absent'); assert not v.auto_detail.text()


@pytest.mark.parametrize('missing',['rules','root'])
def test_auto_refusal_names_repository_and_batch_count(automatic_panel,monkeypatch,missing):
    e=automatic_panel; v=e.panel; v._add(text='https://github.com/o/s')
    if missing=='root':
        e.settings['repositories']['o/s']=e.settings['repositories']['o/r'].copy(); e.settings['archive_root']=''
    v.table.selectAll(); auto_dialog(monkeypatch); before=v.store.path.read_bytes(); v.auto_button.click()
    expected='o/s' if missing=='rules' else 'o/r'
    assert expected in v.status.text() and ('1 of 2' if missing=='rules' else '2 of 2') in v.status.text()
    assert v.store.path.read_bytes()==before


def test_auto_consent_saved_folder_interval_and_singular_hours(automatic_panel,monkeypatch):
    e=automatic_panel; v=e.panel; select(v,'github:o/r'); auto_dialog(monkeypatch,'Every 1 hour'); questions=[]
    monkeypatch.setattr(p.W.QMessageBox,'question',lambda *a:questions.append(a) or p.W.QMessageBox.Yes)
    v.auto_button.click(); text=questions[0][2]
    for part in ('1 repository','1 hour',e.settings['archive_root'],'first check','automatically','disk space','bandwidth','never deleted'):
        assert part in text
    assert questions[0][-1]==p.W.QMessageBox.No
    assert 'every 1 hour; first check in 1 hour' in v.status.text()
    assert e.auto.summary(v.library.repositories[0])=='Every 1 hour'


def test_existing_status_column_and_auto_columns_size_to_contents(panel):
    header=panel.table.horizontalHeader()
    assert [header.sectionResizeMode(i) for i in range(6)]==[
        p.W.QHeaderView.Stretch,p.W.QHeaderView.Stretch,p.W.QHeaderView.Stretch,
        p.W.QHeaderView.ResizeToContents,p.W.QHeaderView.ResizeToContents,p.W.QHeaderView.ResizeToContents]


@pytest.mark.parametrize('target', ['panel', 'table'])
def test_identifying_column_floor_tracks_font_changes(panel, target):
    panel._add(text='https://github.com/Fictional/Tool'); APP.processEvents()
    assert panel.table.horizontalHeader().minimumSectionSize() >= panel.table.sizeHintForColumn(0)
    widget = panel if target == 'panel' else panel.table
    font = widget.font(); font.setPointSizeF(font.pointSizeF() * 1.5)
    widget.setFont(font); APP.processEvents()
    floor = panel.table.horizontalHeader().minimumSectionSize()
    assert floor >= max(84, panel.table.fontMetrics().horizontalAdvance('Fictional/Tool') + 6)
    assert all(floor >= panel.table.sizeHintForColumn(column) for column in (0, 1))
    assert all(panel.table.columnWidth(column) >= floor for column in range(3))
