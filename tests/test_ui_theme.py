"""Offscreen behavior of the system theme and shared window controls."""
import sys

import pytest
from PySide6.QtCore import Qt, qInstallMessageHandler
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest

from test_ui_window import APP, ui, isolated_data
from test_scan_process import until


@pytest.fixture(autouse=True)
def restore_scheme():
    previous = APP.styleHints().colorScheme()
    yield
    APP.styleHints().setColorScheme(previous)
    APP.processEvents()


def scheme(value):
    APP.styleHints().setColorScheme(value)
    # The offscreen plugin has no platform theme and ignores this setter.
    # Deliver the same public notification a native platform would report.
    if APP.styleHints().colorScheme() != value:
        APP.styleHints().colorSchemeChanged.emit(value)
    APP.processEvents()


def pixel(image,x,y=None):
    if y is None: x,y=x.x(),x.y()
    ratio=image.devicePixelRatio()
    return image.pixelColor(min(image.width()-1,max(0,int(x*ratio))),
                            min(image.height()-1,max(0,int(y*ratio))))


def assert_button_fits(button,window):
    assert button.width()>=button.sizeHint().width(),button.text()
    assert button.height()>=button.sizeHint().height(),button.text()
    assert button.parentWidget().rect().contains(button.geometry()),button.text()
    assert window.rect().contains(button.mapTo(window,button.rect().topLeft())),button.text()
    assert window.rect().contains(button.mapTo(window,button.rect().bottomRight())),button.text()


def brightness(window):
    image = window.grab().toImage()
    samples = [image.pixelColor(x, y).lightnessF()
               for x in range(10, image.width(), 31)
               for y in range(10, image.height(), 31)]
    return sum(samples) / len(samples)


def test_rendered_scheme_live_flip(ui):
    window, controller = ui
    scheme(Qt.ColorScheme.Light)
    light = brightness(window)
    scheme(Qt.ColorScheme.Dark)
    dark = brightness(window)
    assert light - dark > .35
    scheme(Qt.ColorScheme.Light)
    assert abs(brightness(window) - light) < .03
    assert not controller.calls


def test_stylesheet_parses_without_qt_warnings(ui):
    messages = []
    previous = qInstallMessageHandler(lambda kind, context, text: messages.append(text))
    try:
        for value in (Qt.ColorScheme.Light, Qt.ColorScheme.Dark):
            scheme(value)
            ui[0].grab()
    finally:
        qInstallMessageHandler(previous)
    assert not [m for m in messages if 'stylesheet' in m.lower()]


@pytest.fixture
def owned_window():
    from shiboken6 import isValid
    from PySide6.QtCore import QCoreApplication, QEvent
    from test_ui_window import isolated_window, FakeController, ROOT
    window=isolated_window(ROOT/"run_covearchive.py",FakeController())
    window.show(); APP.processEvents()
    yield window
    if isValid(window):
        window.close(); until(lambda:window._window_closed,10)
        window.deleteLater(); QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete); APP.processEvents()


def test_close_disconnects_scheme_callback(owned_window,monkeypatch):
    from shiboken6 import isValid
    from PySide6.QtCore import QCoreApplication, QEvent
    window=owned_window; tracker=window._theme; destroyed=[]; errors=[]; warnings=[]
    window.destroyed.connect(lambda *_:destroyed.append('window'))
    tracker.destroyed.connect(lambda *_:destroyed.append('tracker'))
    monkeypatch.setattr(sys,'excepthook',lambda *args:errors.append(args))
    previous=qInstallMessageHandler(lambda kind,context,text:warnings.append(text))
    try:
        scheme(Qt.ColorScheme.Light); window.close(); until(lambda:window._window_closed,10)
        before=window.styleSheet(); scheme(Qt.ColorScheme.Dark)
        assert window.styleSheet()==before
        window.deleteLater(); QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete); APP.processEvents()
        assert not isValid(window) and not isValid(tracker)
        assert set(destroyed)=={'window','tracker'}
        scheme(Qt.ColorScheme.Light); scheme(Qt.ColorScheme.Dark)
        assert not errors
        assert not [w for w in warnings if 'deleted' in w.lower() or 'stylesheet' in w.lower()]
    finally: qInstallMessageHandler(previous)


def test_ignored_close_keeps_theme_tracking(owned_window,monkeypatch):
    window=owned_window; shutdown=window.controller.shutdown
    monkeypatch.setattr(window.controller,'shutdown',lambda *args:False)
    scheme(Qt.ColorScheme.Light); window.close(); APP.processEvents()
    assert window.isVisible() and not window._window_closed
    before=window.styleSheet(); scheme(Qt.ColorScheme.Dark)
    assert window.styleSheet()!=before
    monkeypatch.setattr(window.controller,'shutdown',shutdown)


def test_window_navigation_shortcuts(ui):
    window, controller = ui
    window.activateWindow()
    for key, tab in ((Qt.Key_2, 1), (Qt.Key_3, 2), (Qt.Key_4, 3), (Qt.Key_1, 0)):
        QTest.keyClick(APP.focusWidget() or window, key, Qt.ControlModifier)
        APP.processEvents()
        assert window.tabs.currentIndex() == tab
    window.tabs.setCurrentIndex(2)
    QTest.keyClick(window.library_panel.search, Qt.Key_L, Qt.ControlModifier)
    APP.processEvents()
    assert window.tabs.currentIndex() == 0 and window.url.hasFocus()
    assert not controller.calls


def luminance(color):
    channels = [getattr(color, c)() for c in ('redF', 'greenF', 'blueF')]
    linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
    return sum(c * w for c, w in zip(linear, (.2126, .7152, .0722)))


@pytest.mark.parametrize('value', [Qt.ColorScheme.Light, Qt.ColorScheme.Dark])
def test_theme_text_contrast(ui, value):
    scheme(value)
    # Inspect the effective colors actually supplied to Qt's widget delegates.
    palette = ui[0].palette()
    for foreground, background in ((QPalette.WindowText, QPalette.Window),
            (QPalette.Text, QPalette.Base), (QPalette.ButtonText, QPalette.Button),
            (QPalette.HighlightedText, QPalette.Highlight), (QPalette.PlaceholderText, QPalette.Base)):
        a, b = sorted((luminance(palette.color(foreground)), luminance(palette.color(background))))
        assert (b + .05) / (a + .05) >= 4.5


def test_status_tone_changes_and_reverts(ui):
    window, _ = ui
    for value in (Qt.ColorScheme.Light, Qt.ColorScheme.Dark):
        scheme(value)
        window._status('Same status', False)
        normal = window.status.palette().color(QPalette.WindowText)
        window._status('Same status', True)
        error = window.status.palette().color(QPalette.WindowText)
        assert error != normal
        assert window.status.property('tone') == 'error'
        window._status('Same status', False)
        assert window.status.palette().color(QPalette.WindowText) == normal
        assert window.status.property('tone') == 'normal'


def test_all_token_text_pairs_and_unknown_fallback():
    from PySide6.QtGui import QColor
    from covearchive.ui.theme import TOKENS, resolve_scheme
    for name, tokens in TOKENS.items():
        for foreground, background in [
                *[(fg, bg) for fg in ('text', 'muted', 'accent', 'danger', 'warning', 'success')
                  for bg in ('surface', 'card')],
                ('accent_text', 'accent'), ('selection_text', 'selection')]:
            a, b = sorted(luminance(QColor(tokens[k])) for k in (foreground, background))
            assert (b + .05) / (a + .05) >= 4.5, (name, foreground, background)
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(tokens['surface']))
        assert resolve_scheme(Qt.ColorScheme.Unknown, palette) == name


def test_browse_empty_and_provider_are_passive(ui):
    from PySide6.QtWidgets import QLabel
    from test_ui_window import succeed, release
    window, controller = ui
    empty = [label for label in window.tabs.widget(0).findChildren(QLabel)
             if 'nothing is scanned automatically' in label.text().lower()]
    assert len(empty) == 1 and empty[0].isVisible()
    assert window.tree.viewport().isAncestorOf(empty[0])
    for url, provider in (('https://github.com/Owner/Repo', 'GitHub'),
            ('https://codeberg.org/Owner/Repo', 'Codeberg'),
            ('https://gitlab.com/group/project', 'GitLab'), ('not a URL', 'Unsupported')):
        window.url.setText(url)
        assert any(label.isVisible() and label.text() == provider
                   for label in window.toolbar.findChildren(QLabel))
    assert not controller.calls
    succeed(window, controller, release())
    assert not empty[0].isVisible()
    window._clear()
    assert empty[0].isVisible()


@pytest.mark.parametrize('kind,expected', [
    ('eligible', 'Eligible'), ('filtered', 'Filtered out'), ('unsupported', 'Unsupported format'),
    ('new', 'Not uploaded'), ('external_link', 'External link, not archived'),
    ('unverifiable', 'Not verifiable: needs exactly one package file with size and SHA-256'),
    ('excluded', 'Excluded by filename rules'), ('unmatched', 'Not matched by include rules'),
    ('duplicate', 'Duplicate asset')])
def test_asset_status_explanations(ui, kind, expected):
    from PySide6.QtGui import QColor
    from covearchive.ui.theme import TOKENS
    from covearchive.ui.window import ReleaseModel
    from covearchive.build_filters import BuildRules
    from covearchive.github.select import select_releases
    from test_ui_window import catalog, release, asset
    assets = [asset(name='<b>file</b>.exe' if kind == 'filtered' else
                    '<b>file</b>.txt' if kind == 'unsupported' else '<b>file</b>.zip',
                    state=kind if kind in ('new', 'external_link', 'unverifiable') else 'uploaded')]
    if kind == 'duplicate': assets *= 2
    selection = select_releases(catalog(release(assets=assets)), enabled_extensions=('.zip',))
    rules = BuildRules((), ('*',)) if kind == 'excluded' else BuildRules(('never',), ()) if kind == 'unmatched' else BuildRules()
    model = ReleaseModel(selection, ui[0].tree, rules=rules)
    index = model.index(1 if kind == 'duplicate' else 0, 5, model.index(0, 0))
    assert model.data(index) == expected
    tooltip = model.data(index, Qt.ToolTipRole)
    assert tooltip and expected in tooltip and len(tooltip) > len(expected)
    assert '<' not in tooltip and '>' not in tooltip
    for value in (Qt.ColorScheme.Light, Qt.ColorScheme.Dark):
        scheme(value)
        color = model.data(index, Qt.ForegroundRole)
        if kind != 'eligible':
            assert color is not None
            name = 'dark' if value == Qt.ColorScheme.Dark else 'light'
            assert color.color() == QColor(TOKENS[name]['muted'])


@pytest.mark.parametrize('scale', [1, 1.5])
@pytest.mark.parametrize('state', ['empty', 'catalog', 'scanning'])
def test_browse_compact_layout(ui, scale, state):
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QPushButton
    window, _ = ui
    from test_ui_window import succeed, release, asset, URL
    if state == 'catalog':
        succeed(window, ui[1], release(name='Long repository title ' * 100, assets=[asset()]))
        window.tree.setCurrentIndex(window.tree.model().index(0, 0))
        window.history_status.setText('Archiving Git history...')
    elif state == 'scanning':
        window.url.setText(URL); window.scan.click()
    font = QFont(window.font()); font.setPointSizeF(font.pointSizeF() * scale)
    window.setFont(font); window.resize(820, 520); APP.processEvents()
    assert window.width() == 820 and window.height() == 520
    assert window.tree.height() >= 140
    buttons = [b for b in window.tabs.widget(0).findChildren(QPushButton) if b.isVisible()]
    for button in buttons:
        assert_button_fits(button,window)
        assert window.rect().contains(button.mapTo(window, button.rect().bottomRight())), button.text()
        for other in buttons:
            if other is not button and other.parent() is button.parent():
                assert not button.geometry().intersects(other.geometry()), (button.text(), other.text())


def test_browse_provider_updates_without_scan(ui):
    from PySide6.QtWidgets import QLabel
    window, controller = ui
    for url, expected in (('https://github.com/a/b', 'GitHub'),
            ('https://codeberg.org/a/b', 'Codeberg'), ('https://gitlab.com/a/b', 'GitLab'),
            ('<b>https://evil.example/?token=secret</b>', 'Unsupported')):
        window.url.setText(url)
        assert any(label.text() == expected and label.isVisible()
                   for label in window.toolbar.findChildren(QLabel))
    assert not controller.calls


@pytest.fixture
def queue_ui(ui):
    from test_ui_queue import FakeBridge
    from covearchive.ui.queue_panel import QueuePanel
    window, _ = ui
    bridge = FakeBridge(); panel = QueuePanel(bridge, window)
    window.tabs.removeTab(1); window.tabs.insertTab(1, panel, 'Queue'); window.tabs.setCurrentIndex(1)
    APP.processEvents()
    return window, panel, bridge


def test_queue_empty_notice_and_partition_identity(queue_ui):
    from PySide6.QtWidgets import QLabel
    from test_ui_queue import job, view, S
    window, panel, bridge = queue_ui
    panel._view(view(100, ()))
    empty = [label for label in panel.findChildren(QLabel)
             if 'nothing starts automatically' in label.text().lower()]
    assert len(empty) == 1 and empty[0].isVisible()
    assert 'Browse' in empty[0].text() and 'Archive Selected' in empty[0].text()
    panel._view(view(101, (), 'Queue database is unavailable'))
    assert panel.banner.text() == 'Queue database is unavailable' and panel.banner.isVisible()
    assert panel.banner.property('tone') == 'error'
    assert not bridge.calls
    panel._view(view(102, (job(S.QUEUED),)))
    assert not empty[0].isVisible()
    assert panel.start.isEnabled()


def test_queue_partition_controls_and_refresh(queue_ui):
    from dataclasses import replace
    from test_ui_queue import job, view, S, ControlResult
    _, panel, bridge = queue_ui
    jobs = tuple(job(state, job_id=str(i)) for i, state in enumerate(
        (S.COMPLETED, S.QUEUED, S.FAILED, S.RUNNING, S.CANCELLED, S.RETRY_WAIT, S.PAUSED)))
    panel._view(view(100, jobs))
    def ids(): return [panel.table.item(row, 0).data(Qt.UserRole) for row in range(panel.table.rowCount())]
    assert ids() == ['1', '3', '5', '6', '0', '2', '4']
    assert panel.table.rowCount() == len(jobs)
    cue = panel.table.verticalHeaderItem(4)
    assert cue is not None and 'Finished' in cue.text() and panel.table.verticalHeader().isVisible()
    panel.table.selectRow(0)
    panel._view(view(101, tuple(reversed(jobs))))
    assert ids() == ['6', '5', '3', '1', '4', '2', '0']
    assert panel.selected_ids() == ('1',) and panel.buttons['pause'].isEnabled()
    panel.buttons['pause'].click()
    assert bridge.calls[-1] == (panel.token, 'pause', ('1',))
    assert panel.waiting == {'1'} and not panel.buttons['cancel'].isEnabled()
    panel._result(ControlResult(panel.token, 'pause', '1', True, None, S.PAUSED, None))
    assert not panel.waiting and panel.ack == {'1': (S.PAUSED, None)}
    changed = tuple(replace(j, state=S.COMPLETED) if j.job_id == '1' else j for j in jobs)
    panel._view(view(102, changed))
    assert ids() == ['3', '5', '6', '0', '1', '2', '4']
    assert panel.selected_ids() == ('1',) and not panel.ack and not panel.message.text()
    assert not any(b.isEnabled() for b in panel.buttons.values())
    panel._view(view(101, ()))
    assert ids() == ['3', '5', '6', '0', '1', '2', '4'] and panel.selected_ids() == ('1',)


def test_queue_hostile_text_remains_plain(queue_ui):
    from test_ui_queue import job, view, S
    from covearchive.ui.queue_panel import _safe, _HISTORY
    _, panel, _ = queue_ui
    hostile = '<b>file</b>\n\u202e.zip'
    panel._view(view(100, (job(S.COMPLETED, asset_name=hostile, error_code='https://evil/?token=SECRET'),), hostile))
    assert panel.banner.textFormat() == Qt.PlainText and panel.banner.text() == _safe(hostile)
    assert panel.table.item(0, 0).text() == _safe(hostile)
    assert _HISTORY in panel.table.item(0, 0).toolTip()
    for column in range(6):
        cell = panel.table.item(0, column)
        assert '<' not in cell.toolTip() and 'SECRET' not in cell.text() + cell.toolTip()
    panel._view(view(101, (job(asset_name='https://evil/?token=SECRET'),), 'https://evil/?token=SECRET'))
    assert panel.table.item(0, 0).text() == panel.banner.text() == '[redacted]'


@pytest.mark.parametrize('scale', [1, 1.5])
def test_queue_compact_layout_and_live_state_colors(queue_ui, scale):
    from PySide6.QtGui import QFont, QColor
    from test_ui_queue import job, view, S
    from covearchive.ui.theme import TOKENS
    window, panel, _ = queue_ui
    font = QFont(window.font()); font.setPointSizeF(font.pointSizeF() * scale)
    window.setFont(font); window.resize(820, 520)
    jobs = tuple(job(state, job_id=str(i)) for i, state in enumerate(
        (S.RUNNING, S.RETRY_WAIT, S.PAUSED, S.FAILED, S.COMPLETED, S.CANCELLED)))
    panel._view(view(100, jobs, 'Queue database is unavailable')); APP.processEvents()
    assert window.size().width() == 820 and window.size().height() == 520
    assert panel.table.height() >= 140
    buttons = [panel.start, *panel.buttons.values()]
    for button in buttons:
        assert_button_fits(button,window)
        assert window.rect().contains(button.mapTo(window, button.rect().bottomRight()))
        for other in buttons:
            if button is not other: assert not button.geometry().intersects(other.geometry())
    roles = {S.RUNNING:'accent', S.RETRY_WAIT:'warning', S.PAUSED:'warning',
             S.FAILED:'danger', S.COMPLETED:'success', S.CANCELLED:'muted'}
    for value, name in ((Qt.ColorScheme.Light, 'light'), (Qt.ColorScheme.Dark, 'dark')):
        scheme(value)
        for row in range(panel.table.rowCount()):
            identity = panel.table.item(row, 0).data(Qt.UserRole)
            state = next(j.state for j in jobs if j.job_id == identity)
            assert panel.table.item(row, 2).foreground().color() == QColor(TOKENS[name][roles[state]])


def test_library_headings_empty_states_and_tones(ui):
    from PySide6.QtWidgets import QLabel, QFrame
    window, controller = ui; window.tabs.setCurrentIndex(2); panel = window.library_panel
    labels = panel.findChildren(QLabel)
    assert {'COLLECTIONS', 'REPOSITORIES'} <= {label.text() for label in labels}
    empty = [label for label in labels if 'Import' in label.text() and 'URL' in label.text()]
    assert len(empty) == 1 and empty[0].isVisible()
    placeholder = [label for label in labels if 'Select a repository' in label.text()]
    assert len(placeholder) == 1 and placeholder[0].isVisible()
    assert panel.auto_detail.parent().frameShape() != QFrame.NoFrame
    panel._add(text='https://github.com/a/b')
    assert not empty[0].isVisible() and panel.status.property('tone') == 'success'
    panel.search.setText('no-match')
    assert empty[0].isVisible() and 'no repositories match the search' in empty[0].text().lower()
    panel.search.clear(); assert not empty[0].isVisible()
    panel._add(text='invalid')
    assert panel.status.text().startswith('Added 0') and panel.status.property('tone') == 'error'
    panel.search.setText('no-match')
    panel.store.writable = False; panel.store.problem = 'Library is read-only: test problem'; panel._lock()
    assert panel.status.text() == panel.store.problem and panel.status.property('tone') == 'error'
    assert empty[0].isVisible() and panel.store.problem in empty[0].text()
    assert not controller.calls


def test_library_inactive_muting_and_selection_render(ui):
    from PySide6.QtGui import QColor
    from covearchive.library import model as m
    from covearchive.ui.theme import TOKENS
    window, _ = ui; window.tabs.setCurrentIndex(2); panel = window.library_panel
    panel.library = m.Library(repositories=(m.Repository('github', 'https://github.com/a/active'),
        m.Repository('gitlab', 'https://gitlab.com/a/inactive'))); panel.refresh()
    for value, name in ((Qt.ColorScheme.Light, 'light'), (Qt.ColorScheme.Dark, 'dark')):
        scheme(value)
        inactive = next(i for i, repo in enumerate(panel.model.rows) if not repo.active)
        active = 1 - inactive
        assert panel.model.data(panel.model.index(inactive, 3)) == 'Inactive: gitlab'
        brush = panel.model.data(panel.model.index(inactive, 0), Qt.ForegroundRole)
        assert brush is not None and brush.color() == QColor(TOKENS[name]['muted'])
        assert panel.model.data(panel.model.index(active, 0), Qt.ForegroundRole) is None
        panel.table.clearSelection(); panel.search.setFocus(); APP.processEvents()
        rect = panel.table.visualRect(panel.model.index(active, 0)); point = rect.topRight()
        point.setX(point.x() - 8); point.setY(rect.center().y())
        before = panel.table.viewport().grab().toImage().pixelColor(point)
        panel.table.selectRow(active); panel.search.setFocus(); APP.processEvents()
        after = panel.table.viewport().grab().toImage().pixelColor(point)
        assert sum(abs(a-b) for a,b in zip(before.getRgb()[:3],after.getRgb()[:3])) > 25


def test_library_selection_pixels_both_schemes(ui):
    from covearchive.library import model as m
    window, _ = ui; window.tabs.setCurrentIndex(2); panel = window.library_panel
    panel.library = m.Library(repositories=(m.Repository('github', 'https://github.com/a/one'),
        m.Repository('github', 'https://github.com/a/two'))); panel.refresh()
    for value in (Qt.ColorScheme.Light, Qt.ColorScheme.Dark):
        scheme(value); panel.table.clearSelection(); panel.search.setFocus(); APP.processEvents()
        rect=panel.table.visualRect(panel.model.index(0,0)); point=rect.topRight()
        point.setX(point.x()-8); point.setY(rect.center().y())
        before=panel.table.viewport().grab().toImage().pixelColor(point)
        panel.table.selectRow(0); panel.search.setFocus(); APP.processEvents()
        after=panel.table.viewport().grab().toImage().pixelColor(point)
        assert sum(abs(a-b) for a,b in zip(before.getRgb()[:3],after.getRgb()[:3])) > 25


@pytest.mark.parametrize('scale', [1, 1.5])
def test_library_compact_layout_and_action_locking(ui, scale):
    from PySide6.QtGui import QFont
    from covearchive.library import model as m
    window, _ = ui; panel = window.library_panel; window.tabs.setCurrentIndex(2)
    panel.library = m.Library(repositories=(m.Repository('github', 'https://github.com/a/repo', name='<b>' + 'LongName' * 10 + '</b>'),))
    panel.refresh(); font = QFont(window.font()); font.setPointSizeF(font.pointSizeF() * scale)
    window.setFont(font); window.resize(820,520); APP.processEvents()
    assert window.size().width() == 820 and window.size().height() == 520
    assert panel.table.height() >= 140 and panel.tree.height() >= 140
    assert panel.model.data(panel.model.index(0,0)).startswith('<b>')
    tooltip = panel.model.data(panel.model.index(0,0), Qt.ToolTipRole)
    assert tooltip and 'LongName' * 10 in tooltip and '<' not in tooltip
    buttons = [panel.add,panel.bulk,panel.import_button,panel.export_button,panel.new,
               panel.rename_collection,panel.delete,panel.move,panel.rename,panel.remove,panel.auto_button]
    for button in buttons:
        assert_button_fits(button,window)
        assert window.rect().contains(button.mapTo(window,button.rect().bottomRight()))
        for other in buttons:
            if button is not other and button.parent() is other.parent():
                assert not button.geometry().intersects(other.geometry())
    panel.table.selectRow(0)
    assert all(b.isEnabled() for b in (panel.add,panel.bulk,panel.import_button,panel.export_button,panel.new,panel.move,panel.rename,panel.remove,panel.auto_button))
    assert not panel.rename_collection.isEnabled() and not panel.delete.isEnabled()
    panel.busy=True; panel._lock(); assert all(not b.isEnabled() for b in buttons)
    panel.busy=False; panel._lock(); panel.store.writable=False; panel._lock()
    assert all(not b.isEnabled() for b in buttons)


@pytest.mark.parametrize('tab', [0, 1, 2])
def test_polish_header_has_no_nested_frame(ui, tab):
    from PySide6.QtGui import QColor
    from covearchive.ui.theme import TOKENS
    window, _ = ui; window.tabs.setCurrentIndex(tab)
    view = (window.tree, window.queue_panel.table, window.library_panel.table)[tab]
    header = view.header() if tab == 0 else view.horizontalHeader()
    for value, name in ((Qt.ColorScheme.Light, 'light'), (Qt.ColorScheme.Dark, 'dark')):
        scheme(value)
        assert header.frameWidth() == 0
        assert header.viewport().geometry().topLeft().x() == 0
        image = header.grab().toImage()
        assert pixel(image,20,0) == QColor(TOKENS[name]['surface'])
        assert pixel(image,20,header.height()-.5) == QColor(TOKENS[name]['border'])


@pytest.mark.parametrize('control', ['url', 'browse', 'prereleases', 'tree', 'tabs'])
def test_polish_focus_is_visible_without_geometry_shift(ui, control):
    window, _ = ui
    if control=='prereleases': window.filter_toggle.click(); APP.processEvents()
    widget = window.nav if control == 'tabs' else getattr(window, control)
    for value in (Qt.ColorScheme.Light, Qt.ColorScheme.Dark):
        scheme(value)
        other = window.browse if control == 'url' else window.url
        other.setFocus(); APP.processEvents()
        geometry = widget.geometry(); hint = widget.sizeHint(); before = widget.grab().toImage()
        widget.setFocus(); APP.processEvents(); after = widget.grab().toImage()
        assert widget.hasFocus()
        assert widget.geometry() == geometry and widget.sizeHint() == hint
        changes = sum(pixel(before,x,y) != pixel(after,x,y)
                      for x in range(4, min(widget.width()-4, 100)) for y in (0,1))
        assert changes > 10, control


@pytest.mark.parametrize('control', ['url', 'browse', 'tree'])
def test_polish_resting_border_is_one_pixel(ui, control):
    from PySide6.QtGui import QColor
    from covearchive.ui.theme import TOKENS
    window, _ = ui; widget = getattr(window, control)
    (window.url if control != 'url' else window.browse).setFocus(); APP.processEvents()
    for value, name in ((Qt.ColorScheme.Light, 'light'), (Qt.ColorScheme.Dark, 'dark')):
        scheme(value)
        image=widget.grab().toImage()
        assert pixel(image,1.75,widget.height()/2) == QColor(TOKENS[name]['card'])


def test_polish_provider_badge(ui):
    from PySide6.QtGui import QColor
    from covearchive.ui.theme import TOKENS
    window, _ = ui; window.url.setText('https://github.com/a/b'); window.resize(820,520)
    for value, name in ((Qt.ColorScheme.Light, 'light'), (Qt.ColorScheme.Dark, 'dark')):
        scheme(value)
        badge=window.provider; image=badge.grab().toImage()
        assert pixel(image,2,badge.height()/2) == QColor(TOKENS[name]['selection'])
        assert badge.text() == 'GitHub' and not badge.wordWrap()
        assert abs(badge.mapTo(window,badge.rect().center()).y()-window.scan.mapTo(window,window.scan.rect().center()).y()) <= 1
        assert badge.height() <= window.scan.height()+2 and window.width() == 820


def test_polish_queue_every_row_has_group_marker(ui):
    from test_ui_queue import job, view, S
    window, _ = ui; window.tabs.setCurrentIndex(1)
    panel=window.queue_panel
    panel._view(view(100,tuple(job(state,job_id=str(i)) for i,state in enumerate(
        (S.COMPLETED,S.QUEUED,S.FAILED,S.RUNNING,S.CANCELLED,S.RETRY_WAIT,S.PAUSED)))))
    assert [panel.table.verticalHeaderItem(row).text() for row in range(7)] == ['Pending']*4+['Finished']*3
    assert panel.table.verticalHeader().width() <= panel.table.fontMetrics().horizontalAdvance('Finished')+20


def test_repair_r1_library_six_rows_fit(ui):
    from covearchive.library import model as m
    window,_=ui; panel=window.library_panel; window.tabs.setCurrentIndex(2)
    panel.library=m.Library(repositories=tuple(m.Repository(provider,url,name='Long repository name '+str(i)*60)
        for i,(provider,url) in enumerate([('github','https://github.com/Fictional/Alpha'),
        ('github','https://github.com/Fictional/Beta'),('github','https://github.com/Owner/Tool'),
        ('gitlab','https://gitlab.com/group/subgroup/repo'),('other','https://git.example.org/team/repo'),
        ('gitea','https://codeberg.org/user/Repo')])))
    panel.refresh(); window.resize(820,520); APP.processEvents()
    assert not panel.table.horizontalScrollBar().isVisible()
    assert all(panel.table.columnWidth(c)>=panel.table.horizontalHeader().minimumSectionSize() for c in (0,1))


def test_repair_r2_provider_empty_and_invalid(ui):
    window,controller=ui
    for text in ('','   '):
        window.url.setText(text); assert not window.provider.isVisible()
    window.url.setText('invalid'); assert window.provider.isVisible()
    assert window.provider.text()=='Unsupported' and window.provider.property('tone') in ('muted','warning')
    invalid=window.provider.palette().color(QPalette.Window)
    window.url.setText('https://github.com/a/b')
    assert window.provider.text()=='GitHub' and window.provider.palette().color(QPalette.Window)!=invalid
    assert not controller.calls


def test_repair_r3_scan_overlay(ui):
    from test_ui_window import URL
    window,controller=ui; window.url.setText(URL); window.scan.click()
    assert not window.empty.isVisible() or 'choose a repository' not in window.empty.text().lower()
    window.cancel.click()
    assert not window.empty.isVisible() or 'choose a repository' not in window.empty.text().lower()
    controller.finish(None,'CANCELLED'); assert window.empty.text()=='No repository scanned'


def test_repair_r4_empty_queue_summary(queue_ui):
    from dataclasses import replace
    from test_ui_queue import view
    panel=queue_ui[1]; panel._view(replace(view(100,()),counts=(),bytes_total=0))
    assert panel.summary.text()=='0 B / 0 B'


def test_repair_r5_nontext_contrast():
    from PySide6.QtGui import QColor
    from covearchive.ui.theme import TOKENS
    for name,tokens in TOKENS.items():
        for fg in ('border','focus'):
            for bg in ('card','surface'):
                a,b=sorted(luminance(QColor(tokens[k])) for k in (fg,bg))
                assert (b+.05)/(a+.05)>=3,(name,fg,bg)


@pytest.mark.parametrize('problem,expected',[('Path C:/bad\u202e\nfolder','Path C:/bad\\u202e\nfolder'),
    ('Cannot read C:/data?token=SECRET','[redacted]'),('Cannot read https://host/SECRET','[redacted]')])
def test_repair_r6_hostile_library_diagnostics(ui,problem,expected):
    panel=ui[0].library_panel; panel.store.writable=False; panel.store.problem=problem; panel._lock()
    assert panel.status.text()==expected and panel.empty.text()==expected
    assert panel.status.textFormat()==panel.empty.textFormat()==Qt.PlainText


def test_repair_r7_queue_message_tooltip(queue_ui):
    from test_ui_queue import job,view,S,ControlResult
    from covearchive.ui.archive_bridge import RunEvent
    panel=queue_ui[1]; panel._view(view(100,(job(S.QUEUED),))); panel.table.selectAll()
    panel.buttons['pause'].click(); panel._result(ControlResult(panel.token,'pause','one',True,None,S.PAUSED,None))
    assert panel.message.toolTip()==panel.message.text()
    panel._run(RunEvent('started')); assert panel.message.toolTip()==panel.message.text()
    panel._view(view(101,(job(S.PAUSED),))); panel.buttons['resume'].click()
    assert panel.message.toolTip()==panel.message.text()


def test_r2_multiline_app_messages(ui):
    window,_=ui; panel=window.library_panel
    panel._add(text='https://github.com/o/r\nbad\nalso bad')
    assert panel.status.text()=='Added 1, skipped 0 duplicate(s), failed 2\nLine 2: Invalid repository URL\nLine 3: Invalid repository URL'
    panel.table.selectRow(0); panel._show_auto_detail()
    assert panel.auto_detail.text().split('\n')==['Off','Last - / Next -']


def test_r2_auto_detail_literal(ui):
    panel=ui[0].library_panel; panel._add(text='https://github.com/o/r'); panel.table.selectRow(0)
    assert panel.auto_detail.text().split('\n')==['Off','Last - / Next -']


def test_r2_multiline_error_tooltip(ui):
    window,_=ui; panel=window.library_panel
    panel._add(text='https://github.com/o/r')
    panel._transaction(lambda lib:lib.set_auto_archive('github:o/r',1))
    window.auto.schedule.failed('o/r','not_found'); window.auto.changed.emit()
    assert panel.model.data(panel.model.index(0,4),Qt.ToolTipRole)=='Error\nRepository not found; check the URL or turn Auto-Archive off'


@pytest.mark.parametrize('text,expected',[('a\nb','a\nb'),('https://h/x?token=\nSECRET','[redacted]'),
    ('a\rb','a\\u000db'),('a\u202eb','a\\u202eb'),('a\u2028b','a\\u2028b'),
    ('<release folder>','\\u003crelease folder\\u003e'),('a\u2029b','a\\u2029b')])
def test_r2_safe_lines_literals(text,expected):
    from covearchive.ui import queue_panel as module
    sanitize=getattr(module,'_safe_lines',module._safe)
    assert sanitize(text)==expected


def test_r2_provider_slot_rejected_inputs(ui,monkeypatch):
    window,controller=ui; errors=[]; warnings=[]
    monkeypatch.setattr(sys,'excepthook',lambda *args:errors.append(args))
    previous=qInstallMessageHandler(lambda kind,context,text:warnings.append(text))
    try:
        for text,expected in (('','Unsupported'),('  ','Unsupported'),('x'*10000,'Unsupported'),
                     ('\u202e','Unsupported'),('a\nb','Unsupported'),('Ã©','Unsupported'),
                     ('https://github.com/o/r/releases/tag/v1','Unsupported'),
                     ('https://user:pw@github.com/o/r','Unsupported'),
                     ('https://github.com/o/r?token=SECRET','GitHub')):
            window.url.setText(text); APP.processEvents()
            if text.strip(): assert window.provider.text()==expected
        assert not errors and not warnings and not controller.calls
    finally: qInstallMessageHandler(previous)


def test_r2_shutdown_and_clear_tooltips(ui):
    from covearchive.ui.archive_bridge import ShutdownStatus
    window,_=ui; window.queue_panel._message('old')
    window._shutdown_status(ShutdownStatus('timeout_waiting',('one',),False))
    expected='Still active: 1 job(s) remain; the window will close when they finish'
    assert window.queue_panel.message.text()==expected and window.queue_panel.message.toolTip()==expected
    window.destination.setText('folder'); window.destination.clear()
    assert window.destination.text()==window.destination.toolTip()==''


def test_r2_elided_label_height(ui):
    window,_=ui; window.resize(820,520)
    for label in (window.status,window.summary,window.detail):
        label.setText('Long text '*100); APP.processEvents()
        assert label.height()==label.sizeHint().height()
    window.destination.setText('Long path '*100); APP.processEvents()
    assert window.destination.wordWrap() and window.destination.hasHeightForWidth()
    assert window.destination.toolTip()==window.destination.text()


def test_r2_elided_clear_tooltip(ui):
    label=ui[0].destination; label.setText('folder'); label.clear()
    assert label.text()==label.toolTip()==''


@pytest.mark.parametrize('kind,expected', [
    ('no_releases', 'No releases found'),
    ('prerelease', '1 prerelease hidden'),
    ('no_formats', 'No formats selected'),
    ('no_matches', 'No matching assets')])
def test_r3_browse_result_overlay(ui,kind,expected):
    from test_ui_window import succeed, release, URL
    from covearchive.scan_worker import ScanFailure
    window,controller=ui
    if kind=='no_formats':
        for box in window.formats.values(): box.setChecked(False)
    if kind=='no_matches':
        window.include.setText('nothing-*'); window._rules_changed()
    succeed(window,controller,*(() if kind=='no_releases' else (release(prerelease=kind=='prerelease'),)))
    APP.processEvents()
    assert window.status.text()==expected
    if kind in ('no_formats','no_matches'):
        assert window.tree.model().rowCount()>0 and not window.empty.isVisible() and window.release_meta.text()==expected
        assert window.preview.text()==('Rules match 0 of 0 eligible assets' if kind=='no_formats' else 'Rules match 0 of 1 eligible assets')
    else:
        assert window.tree.model().rowCount()==0
        assert window.empty.isVisible() and window.empty.text()==expected
        assert window.tree.viewport().isAncestorOf(window.empty)
        assert window.tree.viewport().rect().contains(window.empty.mapTo(window.tree.viewport(),window.empty.rect().center()))
    assert window.empty.textFormat()==Qt.PlainText and window.empty.alignment()==Qt.AlignCenter
    window.prereleases.setChecked(True); window.formats['.zip'].setChecked(True)
    window.include.clear(); window._rules_changed()
    succeed(window,controller,release()); assert window.tree.model().rowCount()>0
    assert not window.empty.isVisible()
    for state,result in (('FAILED',ScanFailure('a'*32,'network')),('CANCELLED',None)):
        window.url.setText(URL); window.scan.click()
        assert window.empty.isVisible() and window.empty.text()=='Scanning releases'
        window.cancel.click(); assert window.empty.isVisible()
        controller.finish(result,state); APP.processEvents()
        assert window.empty.isVisible()
        assert window.empty.text()==(window.status.text() if state=='FAILED' else 'No repository scanned')


def test_r3_scrollbar_token_contrast():
    from PySide6.QtGui import QColor
    from covearchive.ui.theme import TOKENS
    for tokens in TOKENS.values():
        a,b=sorted(luminance(QColor(tokens[k])) for k in ('border','surface'))
        assert (b+.05)/(a+.05)>=3


@pytest.mark.parametrize('value,name',[(Qt.ColorScheme.Light,'light'),(Qt.ColorScheme.Dark,'dark')])
@pytest.mark.parametrize('orientation',[Qt.Vertical,Qt.Horizontal])
def test_r3_scrollbar_rendering_and_scrolling(ui,value,name,orientation):
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QTableWidget,QTableWidgetItem,QStyle,QStyleOptionSlider
    from covearchive.ui.theme import TOKENS
    window,_=ui; table=QTableWidget(100,3,window); table.setGeometry(20,60,440,200)
    for column in range(3): table.setColumnWidth(column,250)
    for row in range(100): table.setItem(row,0,QTableWidgetItem('Row '+str(row)))
    table.show(); scheme(value)
    bar=table.verticalScrollBar() if orientation==Qt.Vertical else table.horizontalScrollBar()
    assert bar.isVisible() and bar.maximum()>0
    option=QStyleOptionSlider(); option.initFrom(bar); option.rect=bar.rect()
    option.orientation=orientation; option.minimum=bar.minimum(); option.maximum=bar.maximum()
    option.sliderPosition=bar.value(); option.sliderValue=bar.value(); option.pageStep=bar.pageStep()
    option.upsideDown=False
    slider=bar.style().subControlRect(QStyle.CC_ScrollBar,option,QStyle.SC_ScrollBarSlider,bar)
    image=bar.grab().toImage(); tokens=TOKENS[name]
    assert pixel(image,slider.center())==QColor(tokens['border'])
    trackpoint=bar.rect().center()
    if orientation==Qt.Vertical: trackpoint.setY(bar.height()-3)
    else: trackpoint.setX(bar.width()-3)
    assert pixel(image,trackpoint)==QColor(tokens['surface'])
    assert (slider.height() if orientation==Qt.Vertical else slider.width())>=24
    for control in (QStyle.SC_ScrollBarAddLine,QStyle.SC_ScrollBarSubLine):
        assert bar.style().subControlRect(QStyle.CC_ScrollBar,option,control,bar).isEmpty()
    bar.setValue(bar.maximum()//2); assert 0<bar.value()<bar.maximum()
    bar.setValue(0); table.setFocus(); QTest.keyClick(table,Qt.Key_End,Qt.ControlModifier)
    APP.processEvents(); assert table.verticalScrollBar().value()>0
    assert table.horizontalScrollBar().value()>0


@pytest.mark.parametrize('problem,usable', [('Queue database is unavailable',False),
    ('Queue database is unavailable',True),(None,False)])
def test_r4_queue_unavailable_empty_state(queue_ui,problem,usable):
    from dataclasses import replace
    from test_ui_queue import view,job,S
    _,panel,bridge=queue_ui
    bridge.view_changed.emit(view(100,())); APP.processEvents()
    assert panel.empty.isVisible()
    assert panel.empty.text()=='Select assets in Browse and choose Archive Selected.\nNothing starts automatically.'
    bridge.view_changed.emit(replace(view(101,(),problem),usable=usable)); APP.processEvents()
    assert panel.empty.isVisible() and panel.empty.text()=='Queue unavailable.'
    assert panel.empty.textFormat()==Qt.PlainText
    bridge.view_changed.emit(replace(view(102,(job(S.QUEUED),),problem),usable=usable)); APP.processEvents()
    assert not panel.empty.isVisible()
    bridge.view_changed.emit(view(103,())); APP.processEvents()
    assert panel.empty.isVisible()
    assert panel.empty.text()=='Select assets in Browse and choose Archive Selected.\nNothing starts automatically.'


def test_pf1_other_overlays_never_cover_rows(queue_ui):
    from test_ui_queue import view,job,S
    window,panel,bridge=queue_ui
    for seq,problem in ((100,None),(101,'Queue database is unavailable')):
        bridge.view_changed.emit(view(seq,(job(S.QUEUED),),problem)); APP.processEvents()
        assert panel.table.rowCount()>0 and not panel.empty.isVisible()
    window.tabs.setCurrentIndex(2); library=window.library_panel
    library._add(text='https://github.com/o/r'); APP.processEvents()
    assert library.model.rowCount()>0 and not library.empty.isVisible()
    library.store.writable=False; library.store.problem='Library is read-only'; library._lock()
    assert library.model.rowCount()>0 and not library.empty.isVisible()


def test_pf2_effective_contrast_matrix(ui):
    import re
    from PySide6.QtGui import QColor
    from covearchive.ui.theme import set_role
    from covearchive.ui.window import ReleaseModel
    from covearchive.github.select import select_releases
    from covearchive.library import model as lm
    from test_ui_window import catalog,release,asset
    from test_ui_queue import job,view,S
    window,_=ui; failures=[]; checked=[]
    for scheme_value in (Qt.ColorScheme.Light,Qt.ColorScheme.Dark):
        scheme(scheme_value); name=window.property('themeName'); sheet=window.styleSheet()
        rules=[]
        for selectors,body in re.findall(r'([^{}]+)\{([^{}]*)\}',sheet):
            properties=dict(re.findall(r'([\w-]+)\s*:\s*([^;]+)',body))
            for selector in selectors.split(','): rules.append((selector.strip(),properties))
        def style(*selectors):
            result={}
            matched=[(selector,props,i) for i,(selector,props) in enumerate(rules) if selector in selectors]
            for selector,props,i in sorted(matched,key=lambda r:(r[0].count('#'),r[0].count('[')+len(re.findall(r'(?<!:):(?!:)',r[0])),r[2])):
                result.update(props)
            return result
        def color(value):
            match=re.search(r'#[0-9a-fA-F]{6}',value); assert match,value
            return QColor(match.group())
        def check(label,fg,bg,minimum=4.5):
            fg=fg if isinstance(fg,QColor) else color(fg); bg=bg if isinstance(bg,QColor) else color(bg)
            a,b=sorted((luminance(fg),luminance(bg))); ratio=(b+.05)/(a+.05)
            checked.append((name,label))
            if ratio<minimum: failures.append(f'{name} {label}: {fg.name()} / {bg.name()} = {ratio:.3f} < {minimum}')
        root=style('QWidget'); card=style('QFrame[section="card"]')['background']
        field=style('QWidget','QLineEdit'); selected=style('QAbstractItemView::item:selected')
        backgrounds=[root['background'],card,selected['background']]
        for group in (QPalette.Active,QPalette.Inactive,QPalette.Disabled):
            palette=window.palette()
            for fg,bg in ((QPalette.WindowText,QPalette.Window),(QPalette.Text,QPalette.Base),
                          (QPalette.ButtonText,QPalette.Button),(QPalette.PlaceholderText,QPalette.Base),
                          (QPalette.HighlightedText,QPalette.Highlight),(QPalette.ToolTipText,QPalette.ToolTipBase)):
                check(f'palette {group.name} {fg.name}/{bg.name}',palette.color(group,fg),palette.color(group,bg))
        for role in ('secondary','primary','danger'):
            for state in ('',':hover',':pressed',':focus',':disabled'):
                paint=style('QWidget','QPushButton','QPushButton'+state,f'QPushButton[role="{role}"]',
                            f'QPushButton[role="{role}"]'+state,'QWidget:disabled' if state==':disabled' else '')
                check(f'{role} button {state}',paint['color'],paint['background'])
        disabled=style('QWidget','QLineEdit','QWidget:disabled','QLineEdit:disabled')
        check('disabled edit',disabled['color'],disabled['background'])
        check('disabled placeholder',disabled['placeholder-text-color'],disabled['background'])
        for selector in ('QTabBar::tab','QTabBar::tab:selected','QHeaderView::section','QToolTip'):
            paint=style('QWidget','QTabBar::tab' if selector.endswith(':selected') else '',selector)
            check(selector,paint['color'],paint['background'])
        notice=style('QLabel[notice="true"]')['background']
        for tone in ('normal','error','warning','success','muted'):
            fg=style(f'QLabel[tone="{tone}"]')['color']
            for i,bg in enumerate([*backgrounds,notice]): check(f'tone {tone} background {i}',fg,bg)
        for tone in ('normal','muted'):
            paint=style('QWidget','QLabel#providerBadge',f'QLabel#providerBadge[tone="{tone}"]')
            check('provider '+tone,paint['color'],paint['background'])
        check('field placeholder',field['placeholder-text-color'],field['background'])
        check('selection text',selected['color'],selected['background'])
        for selector,key in (('QLineEdit','border'),('QLineEdit:focus','border'),
                             ('QScrollBar::handle','background'),('QTabBar::tab:selected','border-bottom')):
            foreground=style(selector)[key]
            targets=[style('QScrollBar')['background']] if 'ScrollBar' in selector else [root['background'],card]
            for bg in targets: check(selector+' boundary',foreground,bg,3)
        check('selection indicator',style('QTableView#repositoryTable::item:selected')['border-bottom'],selected['background'],3)
        check('disabled boundary',disabled['border-color'],disabled['background'],3)
        progress=style('QWidget','QProgressBar')
        for bg in (progress['background'],style('QProgressBar::chunk')['background']): check('progress text',progress['color'],bg)
        window.queue_panel._view(view(100,tuple(job(state,job_id=str(i)) for i,state in enumerate(S))))
        table=window.queue_panel.table
        for row in range(table.rowCount()):
            for column in range(table.columnCount()):
                cell=table.item(row,column)
                for bg in (cell.background().color(),color(selected['background'])):
                    check('queue '+str(cell.data(Qt.UserRole+1))+' column '+str(column),cell.foreground().color(),bg)
        model=ReleaseModel(select_releases(catalog(release(assets=[asset(name='file.txt')]))),window.tree)
        muted=model.data(model.index(0,0,model.index(0,0)),Qt.ForegroundRole).color()
        library=window.library_panel; library.library=lm.Library(repositories=(lm.Repository('gitlab','https://gitlab.com/o/r'),)); library.refresh()
        inactive=library.model.data(library.model.index(0,0),Qt.ForegroundRole).color()
        for label,fg in (('release muted',muted),('repository inactive',inactive)):
            for bg in backgrounds: check(label,fg,bg)
        window.scan.setEnabled(False); APP.processEvents()
        assert window.scan.palette().color(QPalette.Disabled,QPalette.Button)!=window.scan.palette().color(QPalette.Active,QPalette.Button)
        if window.scan.font().bold(): failures.append(name+' disabled primary remains bold')
        window.scan.setEnabled(True)
        window.url.setEnabled(False); APP.processEvents()
        palette=window.url.palette()
        for role in (QPalette.Text,QPalette.PlaceholderText):
            check('effective disabled edit '+role.name,palette.color(QPalette.Disabled,role),palette.color(QPalette.Disabled,QPalette.Base))
        window.url.setEnabled(True)
    assert len(checked)>200
    assert not failures,'\n'.join(failures)
