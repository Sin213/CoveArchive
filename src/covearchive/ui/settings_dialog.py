"""Modal settings with explicit saves and non-destructive path edits."""
import os
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QHBoxLayout,
    QComboBox, QLineEdit, QPushButton, QLabel, QFileDialog, QDialogButtonBox)
from covearchive.archive.naming import validate_root
from covearchive.queue.store import default_queue_path, QueueError
from covearchive.ui.queue_panel import display, _safe
from covearchive.ui.theme import set_tone


class SettingsDialog(QDialog):
    def __init__(self, window):
        super().__init__(window); self.window=window
        self.setWindowTitle('Settings'); self.setModal(True); self.setMinimumWidth(520)
        layout=QVBoxLayout(self); form=QFormLayout(); layout.addLayout(form)
        def section(title,hint):
            heading=QLabel(title); heading.setProperty('inspectorHeading',True); form.addRow(heading)
            note=QLabel(hint); note.setTextFormat(Qt.PlainText); set_tone(note,'muted'); form.addRow(note)
        section('APPEARANCE','System follows your operating system until you choose Light or Dark.')
        self.appearance=QComboBox(); self.appearance.addItems(('System','Light','Dark'))
        self.appearance.setCurrentIndex({None:0,'light':1,'dark':2}[window._theme.override])
        self.appearance.setAccessibleName('Appearance'); form.addRow('&Appearance',self.appearance)
        section('ARCHIVE LOCATION','The folder must already exist and be writable.')
        self.root=QLineEdit(window.root.text()); self.root.setAccessibleName('Archive location')
        row=QHBoxLayout(); row.addWidget(self.root,1); self.browse=QPushButton('Browse…'); row.addWidget(self.browse)
        self.browse.setAccessibleName('Browse archive location'); form.addRow('Archive location',row)
        self.browse.clicked.connect(self._browse)
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
        self.adjustSize()

    def _browse(self):
        path=QFileDialog.getExistingDirectory(self,'Archive location',self.root.text())
        if path: self.root.setText(path)

    def _save(self):
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
