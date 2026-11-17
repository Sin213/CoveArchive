"""Foundations: real settings, theme persistence, and toolbar state."""
import json
from dataclasses import replace
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel
from PySide6.QtTest import QTest
from test_ui_window import ui, APP, URL, isolated_data
from test_ui_queue import job, view, S


def test_toolbar_shell_and_scan(ui):
    w,c=ui
    assert not w.tabs.tabBar().isVisible()
    assert not any('Cove Nexus family' in x.text() for x in w.findChildren(QLabel))
    w.nav.setCurrentIndex(2); assert w.tabs.currentIndex()==2
    w.tabs.setCurrentIndex(0); assert w.nav.currentIndex()==0
    assert not w.cancel.isVisible()
    w.url.setText(URL); w.scan.click(); assert w.cancel.isVisible()
    w.cancel.click(); assert w.cancel.isVisible() and not w.cancel.isEnabled()
    c.finish(None,'CANCELLED'); assert not w.cancel.isVisible()


def test_theme_override_system_and_persistence(ui):
    w,_=ui
    assert 'appearance' not in w.settings
    before=w.property('themeName'); w.theme_toggle.click()
    target='dark' if before=='light' else 'light'
    assert w.property('themeName')==target
    assert json.loads(w.preferences.path.read_text())['appearance']==target
    w._theme.apply(Qt.ColorScheme.Dark if target=='light' else Qt.ColorScheme.Light)
    assert w.property('themeName')==target
    w.set_appearance(None)
    assert 'appearance' not in json.loads(w.preferences.path.read_text())


def test_readonly_theme_is_session_only(ui):
    w,_=ui; w.preferences.writable=False
    before=w.property('themeName'); w.theme_toggle.click()
    assert w.property('themeName')!=before
    assert "won't persist" in w.status.text()


def test_settings_save_discard_and_locations(ui,tmp_path):
    w,_=ui; w.settings_button.click(); APP.processEvents()
    d=w.findChild(QDialog); assert d and d.isModal() and d.isVisible()
    assert d.preferences_path.isReadOnly() and d.queue_path.isReadOnly()
    original=w.root.text(); d.root.setText('relative'); d.save.click()
    assert d.isVisible() and d.feedback.text() and w.root.text()==original
    d.feedback.setProperty('tone',None); d.root.setText(str(tmp_path/'missing')); d.save.click()
    assert d.isVisible() and d.feedback.text()=='Choose an existing folder that you can write to.' and d.feedback.property('tone')=='error'
    d.root.setText(str(tmp_path)); d.save.click(); APP.processEvents()
    assert not d.isVisible() and w.root.text()==str(tmp_path)
    assert w.preferences.load()[0]['archive_root']==str(tmp_path)
    w.settings_button.click(); APP.processEvents(); d=w.settings_dialog
    d.root.setText('discard'); d.reject(); APP.processEvents()
    assert w.root.text()==str(tmp_path) and w.settings_button.hasFocus()


def test_queue_badge_chip_real_views_and_stale(ui):
    w,_=ui; w.resize(1400,800)
    jobs=(job(speed_bps=1024),job(job_id='other',owned=False,speed_bps=999999),job(S.RETRY_WAIT,job_id='wait'))
    w._queue_view(replace(view(100,jobs),bytes_done=21,bytes_total=84)); APP.processEvents()
    assert w.nav.tabText(1)=='Queue (3)'
    assert '1.0 KiB/s' in w.queue_chip.text() and w.queue_chip.toolTip()=='2 running, 1 waiting'
    assert w.queue_progress.value()==25
    w._queue_view(view(99,())); assert w.nav.tabText(1)=='Queue (3)'
    w.queue_chip.click(); assert w.tabs.currentIndex()==1
    w._queue_view(view(101,())); assert not w.queue_chip.isVisible()


def test_settings_shortcut_escape_and_system(ui):
    w,_=ui; w.activateWindow(); w.set_appearance('dark')
    QTest.keyClick(w.url,Qt.Key_Comma,Qt.ControlModifier); APP.processEvents()
    d=w.settings_dialog; assert d.isVisible()
    d.appearance.setCurrentIndex(0); d.save.click(); APP.processEvents()
    assert w._theme.override is None and 'appearance' not in w.preferences.load()[0]
    w._theme.apply(Qt.ColorScheme.Light); assert w.property('themeName')=='light'
    w._theme.apply(Qt.ColorScheme.Dark); assert w.property('themeName')=='dark'
    w.settings_button.click(); APP.processEvents()
    QTest.keyClick(w.settings_dialog,Qt.Key_Escape); APP.processEvents()
    assert not w.settings_dialog.isVisible() and w.settings_button.hasFocus()


def test_polish_settings_content_and_safe_paths(ui):
    w,_=ui; w.preferences.path=w.preferences.path.parent/'<b>prefs</b>.json'
    w.settings_button.click(); APP.processEvents(); d=w.settings_dialog
    labels=d.findChildren(QLabel)
    assert not any('&' in x.text() and x.buddy() is None for x in labels)
    assert {x.text() for x in labels if x.property('inspectorHeading')}=={'APPEARANCE','ARCHIVE LOCATION','DATA LOCATIONS'}
    assert {'System follows your operating system until you choose Light or Dark.','The folder must already exist and be writable.','Read-only; shown for support.'}<={x.text() for x in labels if x.property('tone')=='muted'}
    for edit in (d.preferences_path,d.queue_path):
        assert edit.toolTip() and '<b>' not in edit.toolTip() and edit.accessibleName() and edit.cursorPosition()==0
    assert d.height()<=d.minimumSizeHint().height()+10


def test_polish_hidpi_icons(ui):
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QColor
    from covearchive.ui.icons import chrome_icon
    from covearchive.ui.theme import current_tokens
    color=current_tokens(ui[0])['accent']
    for kind in ('sun','moon','gear'):
        pixmap=chrome_icon(kind,color).pixmap(QSize(24,24),3.0)
        assert pixmap.devicePixelRatio()>=2 and pixmap.deviceIndependentSize().width()==24
        image=pixmap.toImage()
        assert any(image.pixelColor(x,y)==QColor(color) for x in range(image.width()) for y in range(image.height()))


@pytest.mark.parametrize('appearance',['dark','light'])
def test_m1_fresh_persisted_appearance(tmp_path,monkeypatch,appearance):
    from covearchive.ui.preferences import PreferenceStore
    from test_ui_window import isolated_window,FakeController,ROOT,until
    store=PreferenceStore(tmp_path/'prefs.json'); prefs=store.load()[0]; prefs['appearance']=appearance; assert store.save(prefs) is None
    monkeypatch.setattr(APP.styleHints(),'colorScheme',lambda:Qt.ColorScheme.Light if appearance=='dark' else Qt.ColorScheme.Dark)
    w=isolated_window(ROOT/'run_covearchive.py',FakeController())
    try: assert w.property('themeName')==appearance and not w.controller.calls
    finally: w.close(); until(lambda:w.bridge.closed); APP.processEvents()


def test_m1_failed_settings_save(ui,tmp_path,monkeypatch):
    w,_=ui; old=tmp_path/'old'; new=tmp_path/'new'; old.mkdir(); new.mkdir(); w.settings['archive_root']=str(old); w.root.setText(str(old)); assert w.preferences.save(w.settings) is None
    before=w.preferences.path.read_bytes(); target='light' if w.property('themeName')=='dark' else 'dark'; w.settings_button.click(); d=w.settings_dialog; d.root.setText(str(new)); d.appearance.setCurrentIndex(1 if target=='light' else 2)
    monkeypatch.setattr(w.preferences,'save',lambda _: 'Disk unavailable'); d.save.click()
    assert d.isVisible() and w.root.text()==str(old) and w.preferences.path.read_bytes()==before
    assert w.property('themeName')==target and "won't persist" in d.feedback.text() and 'Disk unavailable' in d.feedback.text()


@pytest.mark.parametrize('case',['clear','configured-only','prefilled','spaces','leading','trailing','relative','parent','gone','file','browse','unwritable','first','empty-spaces'])
def test_settings_root_validation_paths(ui,tmp_path,monkeypatch,case):
    w,_=ui; old=tmp_path/'old'; old.mkdir(); other=tmp_path/'other'; other.mkdir(); file=tmp_path/'file'; file.write_text('x'); initial='' if case in ('first','configured-only','empty-spaces') else str(old)
    w.settings['archive_root']=None if case in ('first','prefilled','empty-spaces') else str(old); w.root.setText(initial); assert w.preferences.save(w.settings) is None
    before=w.preferences.path.read_bytes(); settings=dict(w.settings); w.settings_button.click(); APP.processEvents(); d=w.settings_dialog; d.appearance.setCurrentIndex(2)
    text={'empty-spaces':'   ','spaces':'   ','leading':' '+str(other),'trailing':str(other)+' ','relative':'other','parent':str(other)+'/../old','gone':str(other),'file':str(file),'unwritable':str(other)}.get(case,'')
    d.root.setText(text)
    if case=='gone': other.rmdir()
    if case=='unwritable': monkeypatch.setattr('covearchive.ui.settings_dialog.os.access',lambda *args:False)
    if case=='browse':
        monkeypatch.setattr('covearchive.ui.settings_dialog.QFileDialog.getExistingDirectory',lambda *args:str(file)); d.browse.click(); assert w.settings==settings and w.root.text()==initial and w.preferences.path.read_bytes()==before
    d.save.click(); APP.processEvents()
    if case=='first': assert not d.isVisible() and w.root.text()=='' and w.preferences.load()[0]['archive_root'] is None and w.preferences.load()[0]['appearance']=='dark'
    else:
        assert d.isVisible() and d.feedback.text()=='Choose an existing folder that you can write to.' and d.feedback.property('tone')=='error' and d.root.hasFocus()
        assert w.root.text()==initial and w.settings==settings and w.preferences.path.read_bytes()==before
