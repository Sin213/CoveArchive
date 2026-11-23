"""Modal settings with explicit saves and non-destructive path edits."""
import os
import time
import threading
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QEvent, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QHBoxLayout,
    QComboBox, QLineEdit, QPushButton, QLabel, QFileDialog, QDialogButtonBox, QGroupBox)
from covearchive.archive.naming import validate_root
from covearchive.queue.store import default_queue_path, QueueError
from covearchive.ui.queue_panel import display, _safe
from covearchive.ui.theme import set_tone, current_tokens
from covearchive import scheduler
from covearchive.scheduled_state import State, default_state_path
from covearchive.auto_archive import failure_text


class SettingsDialog(QDialog):
    _scheduled_done=Signal(object,object)
    def __init__(self, window):
        super().__init__(window); self.window=window
        self.setWindowTitle('Settings'); self.setModal(True); self.setMinimumWidth(520)
        layout=QVBoxLayout(self); form=QFormLayout(); layout.addLayout(form)
        def section(title,hint):
            heading=QLabel(title); heading.setProperty('inspectorHeading',True); form.addRow(heading)
            note=QLabel(hint); note.setTextFormat(Qt.PlainText); note.setWordWrap(True); set_tone(note,'muted'); form.addRow(note)
        section('APPEARANCE','System follows your operating system until you choose Light or Dark.')
        self.appearance=QComboBox(); self.appearance.addItems(('System','Light','Dark'))
        self.appearance.setCurrentIndex({None:0,'light':1,'dark':2}[window._theme.override])
        self.appearance.setAccessibleName('Appearance'); form.addRow('&Appearance',self.appearance)
        section('ARCHIVE LOCATION','The folder must already exist and be writable.')
        self.root=QLineEdit(window.root.text()); self.root.setAccessibleName('Archive location')
        row=QHBoxLayout(); row.addWidget(self.root,1); self.browse=QPushButton('Browse…'); row.addWidget(self.browse)
        self.browse.setAccessibleName('Browse archive location'); form.addRow('Archive location',row)
        self.browse.clicked.connect(self._browse)
        section('GITHUB ACCESS','Optional. Anonymous access works without a token. When creating one, choose read-only access to public repositories.')
        self.github_token=QLineEdit(); self.github_token.setEchoMode(QLineEdit.Password)
        self.github_token.installEventFilter(self)
        self.github_token.setAccessibleName('GitHub access token'); form.addRow('Access token',self.github_token)
        row=QHBoxLayout(); self.save_token=QPushButton('Save Token'); self.clear_token=QPushButton('Clear Token')
        for button in (self.save_token,self.clear_token):
            button.setAutoDefault(True); row.addWidget(button)
        form.addRow(row)
        self.github_link=QLabel(); self.github_link.setAccessibleName('Create a GitHub access token')
        self.github_link.setTextInteractionFlags(Qt.LinksAccessibleByMouse|Qt.LinksAccessibleByKeyboard)
        self.github_link.setFocusPolicy(Qt.StrongFocus); self.github_link.setOpenExternalLinks(True)
        form.addRow(self.github_link)
        self.github_status=QLabel(); self.github_rate=QLabel()
        for name,label in (('GitHub access status',self.github_status),('GitHub rate limit',self.github_rate)):
            label.setTextFormat(Qt.PlainText); label.setWordWrap(True); label.setAccessibleName(name); form.addRow(label)
        self.save_token.clicked.connect(self._save_token); self.clear_token.clicked.connect(self._clear_token)
        self.github_token.returnPressed.connect(self._save_token)
        self.access_timer=QTimer(self); self.access_timer.setInterval(2000)
        self.access_timer.timeout.connect(self._refresh_access)
        group=QGroupBox('Scheduled preservation'); schedule=QFormLayout(group); form.addRow(group)
        self.schedule_status=QLabel('Not checked'); self.schedule_status.setAccessibleName('Scheduler status')
        row=QHBoxLayout(); row.addWidget(self.schedule_status,1)
        self.schedule_toggle=QPushButton('Enable'); self.schedule_check=QPushButton('Check status')
        for button in (self.schedule_toggle,self.schedule_check): row.addWidget(button)
        schedule.addRow(row)
        self.schedule_last=QLabel(); self.schedule_next=QLabel(); self.schedule_error=QLabel()
        for name,label in (('Last completed run',self.schedule_last),('Next check',self.schedule_next),('Last scheduling error',self.schedule_error)):
            label.setTextFormat(Qt.PlainText); label.setWordWrap(True); label.setAccessibleName(name); schedule.addRow(label)
        self._schedule_enabled=False; self._scheduled_done.connect(self._schedule_result,Qt.QueuedConnection)
        self.schedule_check.clicked.connect(lambda:self._schedule_action('status'))
        self.schedule_toggle.clicked.connect(lambda:self._schedule_action('disable' if self._schedule_enabled else 'enable'))
        self._schedule_state()
        section('DATA LOCATIONS','Read-only; shown for support.')
        self.preferences_path=QLineEdit(display(window.preferences.path or 'Unavailable'))
        try: queue_path=default_queue_path()
        except QueueError: queue_path='Unavailable'
        self.queue_path=QLineEdit(display(queue_path))
        for label,edit in (('Preferences path',self.preferences_path),('Queue path',self.queue_path)):
            edit.setReadOnly(True); edit.setAccessibleName(label); edit.setToolTip(_safe(edit.text())); edit.setCursorPosition(0); form.addRow(label,edit)
        self.feedback=QLabel(); self.feedback.setTextFormat(Qt.PlainText); self.feedback.setWordWrap(True)
        self.feedback.setAccessibleName('Settings feedback'); layout.addWidget(self.feedback)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); layout.addWidget(buttons)
        self.save=buttons.button(QDialogButtonBox.Save); self.save.setAccessibleName('Save settings')
        buttons.accepted.connect(self._save); buttons.rejected.connect(self.reject)
        self._refresh_access(); self._style_link(); self.ensurePolished()
        # adjustSize caps height to the screen and can compress wrapped form rows.
        width=max(self.minimumWidth(),self.sizeHint().width())
        self.resize(width,layout.totalHeightForWidth(width))

    def eventFilter(self,watched,event):
        if watched is self.github_token and event.type()==QEvent.KeyPress and event.key() in (Qt.Key_Return,Qt.Key_Enter):
            self._save_token(); return True
        return super().eventFilter(watched,event)

    def _style_link(self):
        accent=current_tokens(self.window)['accent']
        palette=self.github_link.palette()
        for role in (QPalette.Link,QPalette.LinkVisited): palette.setColor(role,QColor(accent))
        self.github_link.setPalette(palette)
        self.github_link.setText('<a href="https://github.com/settings/personal-access-tokens/new" '
            f'style="color: {accent}">Create a GitHub access token</a>')

    def showEvent(self,event):
        self._style_link()
        self.github_token.clear(); self._refresh_access(); self.access_timer.start()
        super().showEvent(event)

    def hideEvent(self,event):
        self.access_timer.stop(); self.github_token.clear(); super().hideEvent(event)

    def _schedule_state(self,data=None):
        if data is None:
            try:
                saved=State.load(default_state_path()); data=saved.data
                if saved.error: data['last_error']=saved.error
            except (QueueError,OSError): data={}
        def stamp(value):
            if value is None: return 'Unknown'
            try: return time.strftime('%Y-%m-%d %H:%M',time.localtime(value))
            except (ValueError,OverflowError,OSError): return 'Unknown'
        self.schedule_last.setText('Last completed run: '+stamp(data.get('last_finish')))
        due=[r['next_due'] for r in data.get('repositories',{}).values() if r['next_due'] is not None]
        self.schedule_next.setText('Next check: '+stamp(max(min(due),data.get('blocked_until') or 0) if due else None))
        code=data.get('last_error')
        message={'state_invalid':'Schedule state was unreadable; safe defaults were used.',
            'invalid_library':'The saved Library could not be read.',
            'invalid_preferences':'Saved preferences could not be read.',
            'interrupted':'The last run was interrupted.', 'download_failed':'A queued download did not complete.',
            'needs_attention':'Archive Health needs review; scheduled work remains queued for the GUI.',
            'disk_full':'The archive disk is full.', 'user_stopped':'A matching job is paused or cancelled.'}.get(code)
        self.schedule_error.setText('Last error: '+(message or failure_text(code) if code else 'None'))

    def _schedule_action(self,action):
        self.schedule_toggle.setEnabled(False); self.schedule_check.setEnabled(False)
        def work():
            try:
                backend=scheduler.backend(getattr(self.window.controller,'launcher',None))
                result=getattr(backend,action)() if backend is not None else scheduler.Status(None,'unavailable','unsupported_platform')
                saved=State.load(default_state_path()); data=saved.data
                if saved.error: data['last_error']=saved.error
            except Exception: result=scheduler.Status(None,'unavailable','scheduler_unavailable'); data={}
            try: self._scheduled_done.emit(result,data)
            except RuntimeError: pass  # The parent window was closed while the command completed.
        threading.Thread(target=work,daemon=True,name='covearchive-scheduler').start()

    def _schedule_result(self,result,data):
        self._schedule_enabled=result.enabled is True
        self.schedule_status.setText({'on':'On','off':'Off','inactive':'On (timer inactive)'}.get(result.state,'Unavailable'))
        self.schedule_toggle.setText('Disable' if self._schedule_enabled else 'Enable')
        self.schedule_toggle.setEnabled(True); self.schedule_check.setEnabled(True); self._schedule_state(data)
        if result.error: self.schedule_error.setText('Last error: '+result.error.replace('_',' '))

    def _refresh_access(self):
        status=self.window.auth.status()
        state=(status.state,status.problem)
        if getattr(self,'_access_state',None)!=state: self.feedback.clear()
        self._access_state=state
        self.github_token.setPlaceholderText('A token is in use' if self.window.auth.token() else 'No token in use')
        text={'NONE':'No token','SAVED':f'Saved securely ({status.label})',
              'SESSION':'Session only (not saved)',
              'REJECTED':'Authentication failed. Replace or clear the token.'}[status.state]
        if status.problem: text+=' - '+status.problem
        self.github_status.setText(text)
        set_tone(self.github_status,'error' if status.state=='REJECTED' or status.problem else 'normal')
        rate=status.rate
        if rate is None: text='not yet known. Appears after the next GitHub request.'
        elif rate.reset <= time.time():
            text='the window has reset; the next request shows the current value.'
        else:
            remaining=(f'{rate.remaining} of {rate.limit} requests remaining' if rate.limit is not None
                       else f'{rate.remaining} requests remaining')
            try: reset=time.strftime('%H:%M',time.localtime(rate.reset))
            except (ValueError,OverflowError,OSError): reset='unknown'
            text=f'{remaining}, resets {reset} ({"authenticated" if rate.authenticated else "anonymous"}), as last reported by GitHub'
        self.github_rate.setText('GitHub API rate limit: '+text)

    def _revoke_scans(self):
        for controller in (getattr(self.window,'controller',None),getattr(getattr(self.window,'auto',None),'scan',None)):
            revoke=getattr(controller,'revoke_token',None)
            if callable(revoke): revoke()

    def _save_token(self):
        before=self.window.auth.token()
        problem=self.window.auth.save(self.github_token.text().strip())
        if self.window.auth.token()!=before: self._revoke_scans()
        self.github_token.clear(); self._refresh_access()
        self.feedback.setText(problem or 'GitHub token saved.')
        set_tone(self.feedback,'error' if problem else 'normal')
        return problem is None

    def _clear_token(self):
        problem=self.window.auth.clear(); self._revoke_scans()
        self.github_token.clear(); self._refresh_access()
        self.feedback.setText(problem or ('GitHub token cleared from this session.' if self.window.auth.status().label=='Session only' else 'GitHub token cleared.'))
        set_tone(self.feedback,'error' if problem else 'normal')

    def _browse(self):
        path=QFileDialog.getExistingDirectory(self,'Archive location',self.root.text())
        if path: self.root.setText(path)

    def _save(self):
        if self.github_token.text() and not self._save_token(): return
        text=self.root.text()
        try:
            if text or self.window.root.text() or self.window.settings['archive_root'] is not None:
                if text != text.strip(): raise ValueError('Whitespace around archive location.')
                validate_root(text)
                path=Path(text)
                if not path.is_dir() or not os.access(path,os.W_OK):
                    raise ValueError('Choose an existing writable folder.')
        except (ValueError,OSError):
            set_tone(self.feedback,'error'); self.feedback.setText('Choose an existing folder that you can write to.'); self.root.setFocus(); return
        candidate=dict(self.window.settings); candidate['archive_root']=text or None
        appearance=(None,'light','dark')[self.appearance.currentIndex()]
        if appearance is None: candidate.pop('appearance',None)
        else: candidate['appearance']=appearance
        problem=self.window.preferences.save(candidate)
        if problem:
            self.window.set_appearance(appearance, persist=False)
            self.feedback.setText("Appearance applies for this session and won't persist. "+problem); return
        self.window.settings=candidate; self.window.root.setText(text)
        self.window.set_appearance(appearance,persist=False); self.accept()
