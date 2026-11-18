"""Foundations: real settings, theme persistence, and toolbar state."""
import json
from dataclasses import replace
import pytest
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QDialog, QLabel, QWidget, QDialogButtonBox
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
    assert {x.text() for x in labels if x.property('inspectorHeading')}=={'APPEARANCE','ARCHIVE LOCATION','GITHUB ACCESS','DATA LOCATIONS'}
    assert {'System follows your operating system until you choose Light or Dark.','The folder must already exist and be writable.','Read-only; shown for support.'}<={x.text() for x in labels if x.property('tone')=='muted'}
    for edit in (d.preferences_path,d.queue_path):
        assert edit.toolTip() and '<b>' not in edit.toolTip() and edit.accessibleName() and edit.cursorPosition()==0
    assert_settings_geometry(d)


def assert_settings_geometry(d):
    APP.processEvents()
    layout=d.layout(); needed=layout.totalHeightForWidth(d.width())
    # Word-wrapped labels need the height at the real width, not minimumSizeHint.
    assert needed>0 and layout.totalMinimumHeightForWidth(d.width())<=d.height()
    assert needed<=d.height()<=needed+10, (d.size(),needed)
    cancel=d.findChild(QDialogButtonBox).button(QDialogButtonBox.Cancel)
    controls=(d.appearance,d.root,d.browse,d.github_token,d.save_token,d.clear_token,
              d.github_link,d.preferences_path,d.queue_path,d.save,cancel)
    for child in controls:
        assert child.isVisible() and child.isEnabled(), child.accessibleName()
        minimum=child.minimumSizeHint()
        assert child.width()>=minimum.width() and child.height()>=minimum.height(), child.accessibleName()
    assert d.preferences_path.isReadOnly() and d.queue_path.isReadOnly()
    leaves=[]
    for child in d.findChildren(QWidget):
        if not child.isVisible(): continue
        rect=QRect(child.mapTo(d,child.rect().topLeft()),child.size())
        assert d.contentsRect().contains(rect), (child.accessibleName(),rect,d.contentsRect())
        if isinstance(child,QLabel) and child.wordWrap():
            assert child.height()>=child.heightForWidth(child.width()), (child.text(),child.size())
        if child in controls or isinstance(child,QLabel): leaves.append((child,rect))
    # Compare labels and controls, excluding containers and their nested children.
    for index,(child,rect) in enumerate(leaves):
        for other,other_rect in leaves[index+1:]:
            if child.isAncestorOf(other) or other.isAncestorOf(child): continue
            assert not rect.intersects(other_rect), (child.accessibleName(),other.accessibleName(),rect,other_rect)


def test_settings_geometry_with_wide_fonts():
    from test_ui_window import isolated_window, FakeController, ROOT, until
    original=QFont(APP.font())
    families=QFontDatabase.families()
    family=next((name for name in ('DejaVu Sans','Verdana') if name in families),original.family())
    try:
        for size in (9,10,11,12,16):
            font=QFont(family,size); font.setStretch(125)
            APP.setFont(font); APP.processEvents()
            w=isolated_window(ROOT/'run_covearchive.py',FakeController())
            try:
                w.show(); APP.processEvents(); w.settings_button.click(); APP.processEvents()
                assert w.settings_dialog.font().pointSize()==size and w.settings_dialog.font().stretch()==125
                assert_settings_geometry(w.settings_dialog)
            finally:
                if w.settings_dialog is not None:
                    w.settings_dialog.reject(); APP.processEvents()
                w.close(); until(lambda:w.bridge.closed,10); APP.processEvents()
    finally:
        APP.setFont(original); APP.processEvents()


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
    finally: w.close(); until(lambda:w.bridge.closed,10); APP.processEvents()


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


def test_github_access_shape_save_clear_and_restart(ui,tmp_path):
    from PySide6.QtWidgets import QLineEdit
    from covearchive.github.token import GitHubAuth, RateLimit
    from test_github_token import Vault, TOKEN, OTHER
    w,_=ui; vault=Vault(); w.auth=GitHubAuth(vault)
    w.settings_button.click(); APP.processEvents(); d=w.settings_dialog
    assert d.github_token.echoMode()==QLineEdit.Password and d.github_token.accessibleName()=='GitHub access token'
    assert not d.github_token.text() and not d.github_token.toolTip() and not d.github_token.accessibleDescription()
    assert 'GITHUB ACCESS' in [x.text() for x in d.findChildren(QLabel)]
    assert d.access_timer.isActive() and d.github_rate.text()=='GitHub API rate limit: not yet known. Appears after the next GitHub request.'
    d.github_token.setText(TOKEN); d.save_token.click()
    assert w.auth.token()==TOKEN and vault.value==TOKEN and not d.github_token.text()
    assert 'Saved securely (Test vault)'==d.github_status.text()
    d.github_token.setText(OTHER); d.github_token.setFocus(); QTest.keyClick(d.github_token,Qt.Key_Return); APP.processEvents()
    assert d.isVisible() and w.auth.token()==OTHER and not d.github_token.text()
    w.auth.reject(OTHER); d._refresh_access()
    assert 'Authentication failed' in d.github_status.text()
    from covearchive.scan_worker import ScanFailure
    assert w._error_text(ScanFailure('a'*32,'unauthorized',token_rejected=True))=='GitHub rejected the access token. Open Settings to replace or clear it.'
    import time
    w.auth.observe(RateLimit(5000,4999,int(time.time())+3600,True,time.time()),w.auth.token()); d._refresh_access()
    assert d.github_rate.text().startswith('GitHub API rate limit: 4999 of 5000 requests remaining') and 'authenticated' in d.github_rate.text()
    w.auth.observe(RateLimit(None,0,int(time.time())+3600,False,time.time()),w.auth.token()); d._refresh_access()
    assert d.github_rate.text().startswith('GitHub API rate limit: 0 requests remaining') and '(anonymous)' in d.github_rate.text()
    w.auth.observe(RateLimit(None,0,1,False,time.time()),w.auth.token()); d._refresh_access(); assert d.github_rate.text()=='GitHub API rate limit: the window has reset; the next request shows the current value.'
    d.clear_token.click(); assert w.auth.token() is None and vault.value is None and d.github_status.text()=='No token'
    d.github_token.setText('invalid'); d.save_token.click()
    assert '20-255' in d.feedback.text() and d.feedback.property('tone')=='error' and not d.github_token.text()
    vault.fail=True; d.github_token.setText(TOKEN); d.save.click()
    assert d.isVisible() and 'Session only (not saved)' in d.github_status.text() and not d.github_token.text()
    assert w.preferences.save(w.settings) is None
    assert TOKEN not in w.preferences.path.read_text()
    d.reject(); assert not d.access_timer.isActive()
    w.settings_button.click(); APP.processEvents(); assert not w.settings_dialog.github_token.text()
    d=w.settings_dialog; d.clear_token.click(); assert w.auth.token() is None and 'Unlock' in d.github_status.text()
    for path in tmp_path.rglob('*'):
        if path.is_file(): assert TOKEN.encode() not in path.read_bytes() and OTHER.encode() not in path.read_bytes()


def test_default_auth_is_session_only_and_wired(ui,monkeypatch):
    import covearchive.ui.window as module
    w,_=ui
    assert w.auth.status().state=='NONE' and w.auth.status().label=='Session only'
    assert w.auto.scan.auth is w.auth
    monkeypatch.setattr(module,'platform_vault',lambda:pytest.fail('default constructor touched storage'))
    from test_ui_window import FakeController, ROOT, until
    other=module.MainWindow(ROOT/'run_covearchive.py',FakeController())
    try:
        assert other.auth.status().label=='Session only'
        assert other.bridge._options['auth'] is other.auth
    finally:
        other.close(); until(lambda:other.bridge.closed,10)


@pytest.mark.parametrize('key', [Qt.Key_Return,Qt.Key_Enter])
@pytest.mark.parametrize('target', ['save_token','clear_token','github_token'])
def test_github_access_focused_enter_uses_correct_action(ui,key,target):
    from covearchive.github.token import GitHubAuth
    from test_github_token import Vault, TOKEN, OTHER
    w,_=ui; w.auth=GitHubAuth(Vault()); w.auth.save(TOKEN)
    w.settings_button.click(); APP.processEvents(); d=w.settings_dialog
    saves=[]; d.save.clicked.connect(lambda:saves.append(True))
    d.github_token.setText(OTHER); widget=getattr(d,target)
    d.activateWindow(); widget.setFocus(); APP.processEvents(); assert widget.hasFocus()
    QTest.keyClick(widget,key); APP.processEvents()
    assert d.isVisible() and not saves and not d.github_token.text()
    assert w.auth.token()==(None if target=='clear_token' else OTHER)


@pytest.mark.parametrize('button', ['save_token','save'])
def test_github_access_trims_pasted_token(ui,button):
    from covearchive.github.token import GitHubAuth, valid_token
    from test_github_token import Vault, TOKEN
    w,_=ui; vault=Vault(); w.auth=GitHubAuth(vault)
    w.settings_button.click(); APP.processEvents(); d=w.settings_dialog
    pasted=' '+TOKEN+'\n'; assert not valid_token(pasted)
    d.github_token.setText(pasted); getattr(d,button).click(); APP.processEvents()
    assert vault.value==TOKEN and w.auth.token()==TOKEN and not d.github_token.text()
    assert d.isVisible()==(button=='save_token')


def test_github_access_refresh_clears_stale_feedback(ui,monkeypatch):
    from covearchive.github.token import GitHubAuth, VaultError
    from test_github_token import Vault, TOKEN, OTHER
    w,_=ui; vault=Vault(); w.auth=GitHubAuth(vault)
    w.settings_button.click(); APP.processEvents(); d=w.settings_dialog
    d.github_token.setText(TOKEN); d.save_token.click()
    assert d.feedback.text()=='GitHub token saved.'
    w.auth.reject(TOKEN); d._refresh_access()
    assert not d.feedback.text() and 'Authentication failed' in d.github_status.text()
    vault.fail=True; w.auth.save(OTHER); d._refresh_access()
    d.feedback.setText('Stale storage feedback')
    def broken(token): raise VaultError('Storage is now unavailable.')
    monkeypatch.setattr(vault,'save',broken); w.auth.save(TOKEN); d._refresh_access()
    assert not d.feedback.text() and 'Storage is now unavailable.' in d.github_status.text()



def test_public_only_token_guidance(ui):
    from pathlib import Path
    w,_=ui; w.settings_button.click(); APP.processEvents()
    assert 'Optional. Anonymous access works without a token. When creating one, choose read-only access to public repositories.' in [label.text() for label in w.settings_dialog.findChildren(QLabel)]
    assert 'CoveArchive uses the token only for repositories GitHub reports public via one authenticated visibility check; otherwise release and asset requests stay anonymous.' in (Path(__file__).resolve().parents[1]/'README.md').read_text(encoding='utf-8')


@pytest.mark.parametrize('action',['clear','replace','same'])
def test_settings_revokes_both_active_token_scans(ui,action):
    from types import SimpleNamespace as NS
    from covearchive.github.token import GitHubAuth
    from test_github_token import Vault,TOKEN,OTHER
    w,_=ui; w.auth=GitHubAuth(Vault()); w.auth.save(TOKEN)
    seen=[]
    w.controller.revoke_token=lambda:seen.append('browse')
    w.auto.scan.revoke_token=lambda:seen.append('auto')
    w.settings_button.click(); APP.processEvents(); d=w.settings_dialog
    if action=='clear': d.clear_token.click()
    else: d.github_token.setText(OTHER if action=='replace' else TOKEN); d.save_token.click()
    assert seen==([] if action=='same' else ['browse','auto'])


def test_later_page_unauthorized_window_message(ui):
    from covearchive.github.token import GitHubAuth
    from covearchive.scan_worker import ScanFailure
    w,_=ui; w.auth=GitHubAuth(None); token='ghp_'+'A'*36; w.auth.save(token); w.auth.reject(token)
    failure=ScanFailure('a'*32,'incomplete_scan',detail={'cause_code':'unauthorized'},token_rejected=True)
    assert w._error_text(failure)=='GitHub rejected the access token. Open Settings to replace or clear it.'


from test_scan_process import rig as process_rig


def test_settings_clear_cancels_and_reaps_both_workers(ui,process_rig):
    from covearchive.github.token import GitHubAuth
    from test_scan_process import command,reply,until,gone,URL
    w,_=ui; auth=GitHubAuth(None); auth.save('ghp_'+'A'*36); w.auth=auth
    browse,browse_events=process_rig(auth=auth); auto,auto_events=process_rig(auth=auth)
    original=(w.controller,w.auto.scan)
    try:
        w.controller,w.auto.scan=browse,auto
        for c in (browse,auto): c.start(URL)
        until(lambda:all(c.job.process.processId()>0 for c in (browse,auto)))
        pids=[c.job.process.processId() for c in (browse,auto)]
        w.settings_button.click(); APP.processEvents(); w.settings_dialog.clear_token.click()
        until(lambda:browse_events and auto_events)
        assert browse_events[0][1]==auto_events[0][1]=='CANCELLED' and all(gone(pid) for pid in pids)
        browse.command_factory=command(reply().replace('r=json.loads(sys.stdin.buffer.readline());',
            "r=json.loads(sys.stdin.buffer.readline());assert 'github_token' not in r;"))
        browse.start(URL); until(lambda:len(browse_events)==2)
        assert browse_events[1][2].code=='not_found'
    finally:
        browse.shutdown(); auto.shutdown(); w.controller,w.auto.scan=original



def test_token_link_rendered_contrast_tracks_reopened_theme(ui):
    from collections import Counter
    from PySide6.QtGui import QColor,QPalette,QImage,QPainter
    from covearchive.ui.theme import TOKENS
    from test_ui_theme import luminance
    w,_=ui; previous=None
    for theme in ('light','dark','light'):
        w.set_appearance(theme); w.settings_button.click(); APP.processEvents(); d=w.settings_dialog
        if previous is not None: assert d is previous
        link=next(label for label in d.findChildren(QLabel) if 'Create a GitHub access token' in label.text())
        source=link.grab().toImage()
        image=QImage(source.size(),QImage.Format_ARGB32)
        image.fill(d.grab().toImage().pixelColor(5,5))
        painter=QPainter(image); painter.drawImage(0,0,source); painter.end()
        colors=Counter(image.pixelColor(x,y).name() for x in range(image.width()) for y in range(image.height()))
        background=QColor(colors.most_common(1)[0][0])
        foreground=QColor(next(color for color,_ in colors.most_common() if color!=background.name()))
        low,high=sorted((luminance(foreground),luminance(background)))
        assert (high+.05)/(low+.05)>=4.5,(theme,foreground.name(),background.name())
        assert foreground==QColor(TOKENS[theme]['accent'])
        assert link.palette().color(QPalette.Link)==QColor(TOKENS[theme]['accent'])
        assert link.openExternalLinks() and 'https://github.com/settings/personal-access-tokens/new' in link.text()
        assert link.accessibleName()=='Create a GitHub access token'
        assert link.textInteractionFlags() & Qt.LinksAccessibleByKeyboard
        link.setFocus(); APP.processEvents(); assert link.hasFocus()
        previous=d; d.reject(); APP.processEvents()


def test_settings_section_notes_wrap_at_minimum_width(ui):
    w,_=ui; w.settings_button.click(); APP.processEvents(); d=w.settings_dialog
    text='Optional. Anonymous access works without a token. When creating one, choose read-only access to public repositories.'
    note=next(label for label in d.findChildren(QLabel) if label.text()==text)
    assert note.wordWrap() and d.minimumWidth()==520 and d.sizeHint().width()<800
    d.resize(520,d.sizeHint().height()); APP.processEvents()
    assert d.width()==520 and note.isVisible() and note.text()==text
    assert note.height()>=note.heightForWidth(note.width())>note.fontMetrics().height()
    assert d.rect().contains(note.mapTo(d,note.rect().bottomRight()))



def test_unproven_unauthorized_never_claims_token_rejection(ui):
    from covearchive.scan_worker import ScanFailure
    from covearchive.github.token import GitHubAuth
    w,_=ui; w.auth=GitHubAuth(None); token='ghp_'+'A'*36; w.auth.save(token); w.auth.reject(token)
    assert 'rejected' not in w._error_text(ScanFailure('a'*32,'unauthorized',401))
    assert w._error_text(ScanFailure('a'*32,'not_found',token_rejected=True))=='Not found: repository not found or inaccessible'


@pytest.mark.parametrize('mode',['redirect','first','later'])
def test_window_wording_uses_real_worker_rejection_provenance(ui,mode):
    import subprocess,sys
    from test_scan_process import later_401_script
    from covearchive import scan_worker as wire
    from covearchive.github.token import GitHubAuth
    from test_github_token import Vault,TOKEN
    w,_=ui; w.auth=GitHubAuth(Vault()); w.auth.save(TOKEN)
    child=subprocess.run([sys.executable,'-I','-B','-c',later_401_script(mode)],input=wire.encode_request_with_token('a'*32,URL,TOKEN),capture_output=True,timeout=3)
    assert child.returncode==0; failure=wire.decode_response(child.stdout,'a'*32)
    if failure.token_rejected: w.auth.reject(TOKEN)
    assert ('rejected' in w._error_text(failure))==(mode!='redirect')



def test_clear_feedback_distinguishes_session_and_failed_vault(ui):
    from covearchive.github.token import GitHubAuth
    from test_github_token import Vault
    w,_=ui; w.auth=GitHubAuth(None); w.settings_button.click(); APP.processEvents(); d=w.settings_dialog
    d.clear_token.click(); assert d.feedback.text()=='GitHub token cleared from this session.'
    vault=Vault(); vault.fail=True; w.auth=GitHubAuth(vault); w.auth.load()
    d.clear_token.click(); assert 'cleared' not in d.feedback.text() and 'Any saved token was not removed' in d.feedback.text()
