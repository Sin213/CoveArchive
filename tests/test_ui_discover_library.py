"""Offline Discover saves use the existing Library transaction and lifecycle."""

from dataclasses import replace
from threading import Event

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QContextMenuEvent, QDesktopServices, QTextCursor
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QMenu

from covearchive.library import model as m, transfer as t
from covearchive.ui import library_panel as lp
from covearchive.ui.library_panel import SaveDiscoveredOutcome as Outcome
from covearchive.ui.library_store import LibraryStore
from test_ui_discover import APP, record, rig, search
from test_ui_discover_process import until


@pytest.fixture
def flow(rig, tmp_path, monkeypatch):
    window, panel, controller = rig()
    archive = tmp_path / 'archives'
    archive.mkdir()
    (archive / 'preserved.zip').write_bytes(b'preserved')
    window.settings['archive_root'] = str(archive)
    window.root.setText(str(archive))

    def forbidden(*args, **kwargs):
        pytest.fail('Discover save attempted a scan, history operation or queue job')

    for obj, method in ((window.controller, 'start'), (window.auto.scan, 'start'),
                        (window.history_controller, 'start'), (window.bridge, 'enqueue')):
        monkeypatch.setattr(obj, method, forbidden)
    yield window, panel, controller
    window.auto.tick()
    assert not window.bridge.requests and not window.auto._wanted
    assert not window.history_controller.running
    assert all(repo.auto_archive is None for repo in window.library_panel.library.repositories)
    assert [(path.name, path.read_bytes()) for path in archive.iterdir()] == [('preserved.zip', b'preserved')]


def choose(panel, controller, *records):
    search(panel, controller, *(records or (record(),)))
    panel.results.setCurrentRow(0)


@pytest.mark.parametrize('opens', [True, False])
def test_library_navigation_tracks_current_membership_after_messages(flow, monkeypatch, opens):
    window, panel, controller = flow
    library = window.library_panel
    choose(panel, controller, record(), record(2, 'Unsaved'))
    assert not panel.library_action.isVisible()
    panel.add_button.click()
    assert panel.library_action.isVisible() and panel.library_action.isEnabled()
    copied = []

    class Clipboard:
        def setText(self, value): copied.append(value)

    monkeypatch.setattr(QApplication, 'clipboard', lambda: Clipboard())
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opens)
    panel.copy_action.trigger()
    assert copied == [record().url] and panel.status.text() == 'Copied repository URL'
    assert panel.library_action.isVisible()
    panel.github_action.trigger()
    if not opens: assert panel.status.text() == 'Could not open the browser'
    assert panel.library_action.isVisible()
    panel.library_action.trigger()
    assert window.tabs.currentIndex() == 2
    window._navigate(3)
    panel.results.setCurrentRow(1)
    assert not panel.library_action.isVisible() and not panel.library_action.isEnabled()
    panel.results.setCurrentRow(0)
    assert panel.library_action.isVisible()
    library.busy = True
    library.changed.emit()
    assert 'Library unavailable' in panel.details.toPlainText()
    assert not panel.library_action.isVisible() and not panel.library_action.isEnabled()
    library.busy = False
    library.changed.emit()
    assert panel.library_action.isVisible()
    identity = m.Repository('github', record().url).identity
    library.table.selectRow(next(i for i, repo in enumerate(library.model.rows) if repo.identity == identity))
    monkeypatch.setattr(lp.W.QMessageBox, 'question', lambda *args: lp.W.QMessageBox.Yes)
    library.remove.click()
    assert 'Library: Not saved' in panel.details.toPlainText()
    assert not panel.library_action.isVisible() and not panel.library_action.isEnabled()
    panel.go_to_library()
    assert window.tabs.currentIndex() == 3


def test_unrelated_library_changes_preserve_details_document_selection_and_scroll(flow):
    window, panel, controller = flow
    library = window.library_panel
    choose(panel, controller, record(description='A long repository description. ' * 150))
    panel.add_button.click()
    APP.processEvents()
    cursor = panel.details.textCursor()
    cursor.setPosition(50)
    cursor.setPosition(200, QTextCursor.KeepAnchor)
    panel.details.setTextCursor(cursor)
    bars = [bar for view in (panel.results, panel.details)
            for bar in (view.verticalScrollBar(), view.horizontalScrollBar())]
    assert panel.details.verticalScrollBar().maximum() > 0
    for bar in bars:
        bar.setValue(bar.maximum() // 2)
    scroll = [bar.value() for bar in bars]
    text = panel.details.toPlainText()
    selected = cursor.selectedText()
    changes = QSignalSpy(panel.details.document().contentsChanged)
    calls = list(controller.calls)
    assert library.save_discovered('https://github.com/Unrelated/Repo') is Outcome.ADDED
    assert library._transaction(lambda lib: lib.remove_repositories(['github:unrelated/repo']))
    library.changed.emit()
    APP.processEvents()
    assert changes.count() == 0
    current = panel.details.textCursor()
    assert (current.position(), current.anchor(), current.selectedText()) == (200, 50, selected)
    assert panel.details.toPlainText() == text
    assert [bar.value() for bar in bars] == scroll
    assert controller.calls == calls
    assert library._transaction(lambda lib: lib.remove_repositories(['github:example/maskrecomp']))
    assert changes.count() > 0 and 'Library: Not saved' in panel.details.toPlainText()


def test_save_unsorted_restart_and_canonical_duplicates(flow, rig, monkeypatch):
    window, panel, controller = flow
    library = window.library_panel
    assert library._transaction(lambda lib: lib.create_collection('Selected collection'))
    library.scope = library.library.collections[0].id
    library.refresh()
    library.url.setText('https://github.com/Unrelated/Input')
    choose(panel, controller)
    before = library.library
    saves = []
    save = library.store.save

    def persist(candidate):
        assert library.library is before
        assert panel.status.text() != 'Added to Library (Unsorted)'
        saves.append(candidate)
        return save(candidate)

    monkeypatch.setattr(library.store, 'save', persist)
    panel.add_button.setFocus()
    QTest.keyClick(panel.add_button, Qt.Key_Space)
    assert library.library is not before and len(saves) == 1
    repo, = library.library.repositories
    assert repo.identity == 'github:example/maskrecomp'
    assert repo.collection is repo.auto_archive is None
    assert library.url.text() == 'https://github.com/Unrelated/Input'
    assert panel.status.text() == 'Added to Library (Unsorted)'
    assert panel.status.textFormat() == Qt.PlainText
    assert panel.add_button.hasFocus() and panel.add_button.isEnabled()
    assert panel.library_action.isVisible()
    assert 'In Library' in panel.results.item(0).text()
    assert 'Library: In Library' in panel.details.toPlainText()
    raw = library.store.path.read_bytes()
    panel.add_button.click()
    assert panel.status.text() == 'Already saved' and panel.add_button.hasFocus()
    for url in ('https://github.com/example/maskrecomp/',
                'https://github.com/EXAMPLE/MASKRECOMP.git'):
        assert library.is_saved(url) is True
        assert library.save_discovered(url) is Outcome.ALREADY_SAVED
    choose(panel, controller, record(2), record(3))
    for row in range(2):
        panel.results.setCurrentRow(row)
        panel.add_button.click()
        assert panel.status.text() == 'Already saved'
        assert 'In Library' in panel.results.item(row).text()
    assert len(saves) == 1 and library.store.path.read_bytes() == raw
    restarted, fresh, fresh_controller = rig()
    assert restarted.library_store is not library.store
    assert restarted.library_panel.library == library.library
    choose(fresh, fresh_controller)
    assert 'In Library' in fresh.details.toPlainText()


@pytest.mark.parametrize('state,expected,message', [
    ('busy', Outcome.UNAVAILABLE, 'Library unavailable (busy or importing)...'),
    ('closed', Outcome.UNAVAILABLE, 'Library unavailable (busy or importing)...'),
    ('readonly', Outcome.READ_ONLY, 'Library is read-only...'),
    ('failed', Outcome.SAVE_FAILED, 'Save failed...'),
    ('empty_error', Outcome.SAVE_FAILED, 'Save failed...'),
    ('raised', Outcome.SAVE_FAILED, 'Save failed...'),
])
def test_unavailable_readonly_and_failed_save_are_atomic(flow, monkeypatch, state, expected, message):
    window, panel, controller = flow
    library = window.library_panel
    assert library.save_discovered('https://github.com/Existing/Repo') is Outcome.ADDED
    before, raw = library.library, library.store.path.read_bytes()
    calls = []

    def fail(candidate):
        calls.append(candidate)
        if state == 'raised': raise OSError('SECRET upstream text')
        return '' if state == 'empty_error' else '<b>SECRET upstream text</b>'

    monkeypatch.setattr(library.store, 'save', fail)
    library.busy = state == 'busy'
    library._closed = state == 'closed'
    library.store.writable = state != 'readonly'
    try:
        choose(panel, controller)
        assert library.save_discovered(record().url) is expected
        panel.add_button.click()
        assert panel.status.text() == message
        assert panel.status.textFormat() == Qt.PlainText
        assert not panel.library_action.isVisible()
        assert 'SECRET' not in panel.status.text()
        assert library.library is before and library.store.path.read_bytes() == raw
        assert bool(calls) == (expected is Outcome.SAVE_FAILED)
        if state in ('busy', 'closed'):
            assert library.is_saved(record().url) is None
            assert 'Library unavailable' in panel.results.item(0).text()
            assert 'Library unavailable' in panel.details.toPlainText()
    finally:
        library._closed = library.busy = False


def test_actual_readonly_store_and_disk_change_latch(flow):
    window, panel, controller = flow
    library = window.library_panel
    library.store.path.write_bytes(b'{"version":999}')
    store = LibraryStore(library.store.path)
    readonly = lp.LibraryPanel(store)
    try:
        assert not store.writable
        assert readonly.save_discovered(record().url) is Outcome.READ_ONLY
    finally:
        readonly.close()
    before = library.library
    raw = library.store.path.read_bytes()
    choose(panel, controller)
    panel.add_button.click()
    assert panel.status.text() == 'Save failed...'
    assert library.library is before and library.store.path.read_bytes() == raw
    panel.add_button.click()
    assert panel.status.text() == 'Library is read-only...'
    assert not library.store.writable


def test_provider_qualified_identity_and_invalid_public_input(flow):
    window, panel, controller = flow
    library = window.library_panel
    assert library._transaction(lambda lib: lib.add_repository(
        'https://gitlab.com/Example/MaskRecomp', provider='gitlab'))
    assert library.is_saved(record().url) is False
    before = library.library
    raw = library.store.path.read_bytes()
    for url in (None, '', 'https://evil.invalid/o/r', 'https://user:SECRET@github.com/o/r'):
        assert library.save_discovered(url) is Outcome.INVALID
        assert library.is_saved(url) is False
        assert library.library is before and library.store.path.read_bytes() == raw
    choose(panel, controller)
    panel.add_button.click()
    assert panel.status.text() == 'Added to Library (Unsorted)'
    assert {repo.provider for repo in library.library.repositories} == {'github', 'gitlab'}


def test_add_remove_bulk_and_import_refresh_without_search_or_selection_loss(flow, tmp_path, monkeypatch):
    window, panel, controller = flow
    library = window.library_panel
    records = [record(i + 1, f'Repo{i}') for i in range(60)]
    choose(panel, controller, *records)
    panel.results.setCurrentRow(40)
    panel.results.scrollToItem(panel.results.currentItem())
    APP.processEvents()
    item, scroll = panel.results.currentItem(), panel.results.verticalScrollBar().value()
    selected = item.data(Qt.UserRole)
    metadata = panel.details.toPlainText().split('\n\nLibrary:')[0]
    calls = list(controller.calls)
    library._add(text=selected.url)
    assert panel.results.currentItem() is item and panel.results.currentRow() == 40
    assert panel.results.verticalScrollBar().value() == scroll
    assert 'In Library' in item.text()
    assert panel.details.toPlainText().split('\n\nLibrary:')[0] == metadata
    identity = m.Repository('github', selected.url).identity
    library.table.selectRow(next(i for i, repo in enumerate(library.model.rows) if repo.identity == identity))
    monkeypatch.setattr(lp.W.QMessageBox, 'question', lambda *args: lp.W.QMessageBox.Yes)
    library.remove.click()
    assert library.is_saved(selected.url) is False and 'In Library' not in item.text()
    library._add(text=selected.url + '\n' + records[0].url)
    assert 'In Library' in item.text() and 'In Library' in panel.results.item(0).text()
    imported = tmp_path / 'import.json'
    imported.write_bytes(t.export_library(m.Library().add_repository(records[1].url)))
    monkeypatch.setattr(lp.W.QFileDialog, 'getOpenFileName', lambda *args: (str(imported), ''))

    def accept(box):
        assert library.is_saved(selected.url) is None
        assert library.save_discovered(records[2].url) is Outcome.UNAVAILABLE
        next(button for button in box.buttons() if button.text() == 'Import').click()
        return 0

    monkeypatch.setattr(lp.W.QMessageBox, 'exec', accept)
    window._navigate(2)
    library.import_button.click()
    until(lambda: not library.busy and library.is_saved(records[1].url))
    assert 'In Library' in panel.results.item(1).text()
    assert panel.results.currentItem() is item
    assert panel.results.verticalScrollBar().value() == scroll
    assert controller.calls == calls


def test_busy_import_blocks_discover_save_without_mutation(flow, tmp_path, monkeypatch):
    window, panel, controller = flow
    library = window.library_panel
    imported = tmp_path / 'import.json'
    imported.write_bytes(t.export_library(m.Library().add_repository('https://github.com/Import/Only')))
    monkeypatch.setattr(lp.W.QFileDialog, 'getOpenFileName', lambda *args: (str(imported), ''))
    gate, entered = Event(), Event()
    read = lp.t.read_import_file

    def held_read(path):
        entered.set()
        assert gate.wait(5)
        return read(path)

    monkeypatch.setattr(lp.t, 'read_import_file', held_read)
    monkeypatch.setattr(lp.W.QMessageBox, 'exec', lambda box: 0)
    choose(panel, controller)
    window._navigate(2)
    library._import()
    try:
        until(entered.is_set)
        before = library.library
        window._navigate(3)
        panel.add_button.click()
        assert panel.status.text() == 'Library unavailable (busy or importing)...'
        assert library.library is before and not library.store.path.exists()
        assert 'Library unavailable' in panel.details.toPlainText()
    finally:
        gate.set()
        until(lambda: not library.busy)
    assert 'Library unavailable' not in panel.details.toPlainText()
    assert 'Library: Not saved' in panel.details.toPlainText()


@pytest.mark.parametrize('opens', [True, False])
def test_canonical_browser_and_clipboard_actions(flow, monkeypatch, opens):
    _, panel, controller = flow
    opened, copied = [], []

    class Clipboard:
        def setText(self, value): copied.append(value)

    monkeypatch.setattr(QApplication, 'clipboard', lambda: Clipboard())
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(url) or opens)
    choose(panel, controller)
    panel.copy_action.trigger()
    assert copied == [record().url] and panel.status.text() == 'Copied repository URL'
    panel.github_action.trigger()
    assert len(opened) == 1 and opened[0].toString() == record().url
    assert opened[0].scheme() == 'https' and opened[0].host() == 'github.com'
    if not opens: assert panel.status.text() == 'Could not open the browser'
    assert not panel.library_action.isVisible()


@pytest.mark.parametrize('invalid', [None, 'object',
    replace(record(), owner='Other'), replace(record(), name='Other'),
    replace(record(), url='http://github.com/Example/MaskRecomp'),
    replace(record(), url='https://evil.invalid/Example/MaskRecomp'),
    replace(record(), url='https://github.com/Example/MaskRecomp/'),
    replace(record(), url='https://github.com/Example/MaskRecomp?token=SECRET')])
def test_invalid_result_rejected_for_save_open_and_copy(flow, monkeypatch, invalid):
    window, panel, controller = flow
    choose(panel, controller)
    writes = []
    monkeypatch.setattr(window.library_store, 'save', lambda *args: writes.append('save'))
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda *args: writes.append('browser'))
    monkeypatch.setattr(QApplication, 'clipboard', lambda: pytest.fail('Invalid result copied'))
    panel.results.currentItem().setData(Qt.UserRole, invalid)
    panel.add_button.click()
    assert panel.status.text() == 'Cannot save this result: its GitHub repository URL is invalid.'
    panel.copy_action.trigger()
    panel.github_action.trigger()
    window._open_discovered(invalid)
    assert panel.status.text() == 'Cannot open this result: its GitHub repository URL is invalid.'
    assert not writes and not window.library_panel.library.repositories


def test_keyboard_menu_go_to_library_and_search_cancel(flow, monkeypatch):
    window, panel, controller = flow
    choose(panel, controller)
    panel.results.setFocus()
    QTest.keyClick(panel.results, Qt.Key_Tab)
    assert panel.details.hasFocus()
    QTest.keyClick(panel.details, Qt.Key_Tab)
    assert panel.add_button.hasFocus()
    QTest.keyClick(panel.add_button, Qt.Key_Space)
    QTest.keyClick(panel.add_button, Qt.Key_Space)
    assert panel.add_button.hasFocus() and panel.status.text() == 'Already saved'
    assert panel.add_button.accessibleName() and panel.add_button.accessibleDescription()
    assert panel.more_button.accessibleName() and panel.more_button.accessibleDescription()
    assert panel.more_button in panel.focus_controls
    popups = []
    popup = QMenu.popup
    monkeypatch.setattr(QMenu, 'popup', lambda menu, pos: popups.append(menu))
    panel.results.setFocus()
    for key, modifiers in ((Qt.Key_Menu, Qt.NoModifier), (Qt.Key_F10, Qt.ShiftModifier)):
        QTest.keyClick(panel.results, key, modifiers)
    event = QContextMenuEvent(QContextMenuEvent.Keyboard, QPoint(), QPoint())
    APP.sendEvent(panel.results, event)
    panel.results.customContextMenuRequested.emit(panel.results.visualItemRect(panel.results.currentItem()).center())
    assert popups == [panel.more_menu] * 4
    assert panel.more_menu is panel.more_button.menu()
    assert panel.more_menu.accessibleName() and panel.more_menu.accessibleDescription()
    assert [action.text() for action in panel.more_menu.actions()] == [
        'Open on GitHub', 'Copy Repository URL', 'Go to Library']
    # Exercise the real keyboard menu, including the saved-result navigation action.
    monkeypatch.setattr(QMenu, 'popup', popup)
    panel.more_button.setFocus()
    QTest.keyClick(panel.more_button, Qt.Key_Space)
    APP.processEvents()
    assert panel.more_menu.isVisible()
    for _ in range(3):
        QTest.keyClick(panel.more_menu, Qt.Key_Down)
    assert panel.more_menu.activeAction() is panel.library_action
    QTest.keyClick(panel.more_menu, Qt.Key_Return)
    assert window.tabs.currentIndex() == 2
    window._navigate(3)
    panel.submit()
    assert not panel.add_button.isEnabled() and not panel.more_button.isEnabled()
    assert not panel.github_action.isEnabled() and not panel.copy_action.isEnabled()
    assert not panel.library_action.isVisible()
    count = len(window.library_panel.library.repositories)
    panel.save_selected()
    panel.copy_repository_url()
    panel.open_on_github()
    panel.go_to_library()
    panel.cancel_search()
    controller.finish(state='CANCELLED')
    assert panel.add_button.isEnabled() and panel.more_button.isEnabled()
    assert len(window.library_panel.library.repositories) == count
    assert panel.status.text() == 'Search cancelled. You can retry when ready.'


def test_closed_panel_and_window_handlers_are_noops(flow, monkeypatch):
    window, panel, controller = flow
    choose(panel, controller)
    panel.add_button.click()
    before, raw = window.library_panel.library, window.library_store.path.read_bytes()
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda *args: pytest.fail('Browser after close'))
    monkeypatch.setattr(QApplication, 'clipboard', lambda: pytest.fail('Clipboard after close'))
    window.bridge.closed = True
    window.close()
    assert window._window_closed and panel._closed
    status = panel.status.text()
    tab = window.tabs.currentIndex()
    for callback in (panel.save_selected, panel.open_selected, panel.copy_repository_url,
                     panel.open_on_github, panel.go_to_library, panel.refresh_saved_state):
        callback()
    panel.save_requested.emit(record(2, 'Another'))
    panel.library_requested.emit()
    window._save_discovered(record(2, 'Another'))
    window._open_discovered(record(2, 'Another'))
    window._discover_library()
    window.library_panel.changed.emit()
    panel.show_save_outcome(Outcome.ADDED)
    assert panel.status.text() == status and window.tabs.currentIndex() == tab
    assert window.library_panel.library is before and window.library_store.path.read_bytes() == raw
