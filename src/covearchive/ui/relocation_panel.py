"""Compact Library location inspector; all I/O runs in the supervised worker."""
from datetime import datetime, timezone
from PySide6 import QtWidgets as W
from PySide6.QtCore import QEvent, QSize, Qt, Signal

from covearchive.relocation import Outcome
from covearchive.ui.relocation_process import RelocationController

NOTICE = ('Existing path-based preservation jobs are not automatically migrated or globally identity-bound. '
          'Archives, rules, intervals and scheduled settings stay at their existing locations. '
          'Saving a new Library entry starts with Auto-Archive off. Legacy archives have no recorded numeric identity '
          'and are not attributed by these observations.')
MESSAGES = {
    Outcome.UNCHANGED: 'Location observed; no verified change from a prior identity observation.',
    Outcome.RENAMED: 'Repository rename verified by numeric GitHub id.',
    Outcome.TRANSFERRED: 'Repository owner transfer verified by numeric GitHub id.',
    Outcome.URL_REUSED: 'Identity mismatch: the saved URL now resolves to a different repository id. '
                        'That occupant is not a verified continuation.',
    Outcome.UNAVAILABLE: 'Repository missing, private or inaccessible. Recorded history is retained.',
    Outcome.INCONCLUSIVE: 'Repository location check inconclusive. Recorded history is retained.',
    Outcome.UNSUPPORTED: 'Relocation detection supports public GitHub only. Codeberg and GitLab moves are unsupported.',
}


class _Scroll(W.QScrollArea):
    """Offers its content's size to the Library layout, then shrinks and scrolls only when space is short."""
    def __init__(self):
        super().__init__()
        W.QApplication.instance().focusChanged.connect(self._reveal_focus)

    def _reveal_focus(self, _previous, current):
        # Tab can enter the panel from a sibling without traversing the scroll area itself.
        if current is not None and self.viewport().isAncestorOf(current):
            self.ensureWidgetVisible(current, 0, 0)

    def minimumSizeHint(self):
        width = self.widget().minimumSizeHint().width() + self.verticalScrollBar().sizeHint().width()
        return QSize(width, max(40, self.fontMetrics().height() + 16))

    def sizeHint(self):
        body = self.widget(); hint = body.sizeHint()
        if body.hasHeightForWidth() and self.viewport().width() > 0: hint.setHeight(body.heightForWidth(self.viewport().width()))
        return hint

    def resizeEvent(self, event):
        super().resizeEvent(event); self.updateGeometry()


    def eventFilter(self, watched, event):
        if event.type() == QEvent.LayoutRequest: self.updateGeometry()
        return super().eventFilter(watched, event)


class RelocationPanel(W.QWidget):
    open_requested = Signal(str)

    def __init__(self, path, save, parent=None, *, controller=None, launcher=None):
        super().__init__(parent)
        self.path, self.save = path, save
        self.controller = controller if controller is not None else RelocationController(launcher, parent=self)
        self.repository = self.pending = self.result = None
        self.stopped = False
        # At the minimum window size the expanded result scrolls instead of overlapping the Library controls.
        outer = W.QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        scroll = _Scroll(); scroll.setWidgetResizable(True); scroll.setFrameShape(W.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); outer.addWidget(scroll)
        body = W.QWidget(); scroll.setWidget(body); body.installEventFilter(scroll)
        layout = W.QVBoxLayout(body); layout.setContentsMargins(0, 0, 0, 0)
        row = W.QHBoxLayout(); layout.addLayout(row)
        self.check = W.QPushButton('Check repository location'); row.addWidget(self.check)
        self.cancel = W.QPushButton('Cancel location check'); row.addWidget(self.cancel); row.addStretch()
        self.detail = W.QLabel('Select one repository to check its location.'); self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.PlainText); self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.detail)
        # A bounded chooser keeps up to 64 durable locations compact.
        self.history = W.QComboBox(); self.history.setAccessibleName('Recorded repository locations')
        self.history.setSizeAdjustPolicy(W.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.history.setMinimumContentsLength(15); layout.addWidget(self.history)
        row = W.QHBoxLayout(); layout.addLayout(row)
        self.open = W.QPushButton('Open verified new location in Browse'); row.addWidget(self.open)
        self.add = W.QPushButton('Save new location to Library'); row.addWidget(self.add)
        row.addStretch(); row = W.QHBoxLayout(); layout.addLayout(row)
        self.open_recorded = W.QPushButton('Open recorded location in Browse'); row.addWidget(self.open_recorded)
        row.addStretch()
        self.notice = W.QLabel(NOTICE); self.notice.setTextFormat(Qt.PlainText); self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        for widget in (self.detail, self.notice):
            widget.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Preferred)
        for button in (self.check, self.cancel, self.open, self.add, self.open_recorded):
            button.setAccessibleName(button.text())
        self.check.clicked.connect(self._check); self.cancel.clicked.connect(self.controller.cancel)
        self.open.clicked.connect(self._open); self.add.clicked.connect(self._save)
        self.open_recorded.clicked.connect(self._open_recorded)
        self.controller.terminal.connect(self._terminal)
        self._details(False)
        self._lock()

    def _details(self, visible):
        for widget in (self.history, self.open, self.add, self.open_recorded, self.notice):
            widget.setVisible(visible)

    def select(self, repository):
        if repository != self.repository:
            self.repository = repository; self.result = None; self.history.clear()
            self._details(False)
            self.detail.setText('Select one repository to check its location.' if repository is None else
                                'Check the selected repository to inspect verified and previous locations.')
        self._lock()

    def _lock(self):
        running = self.pending is not None
        self.check.setEnabled(not self.stopped and not running and self.repository is not None and self.path is not None)
        self.cancel.setEnabled(running)
        verified = bool(self.result and self.result.verified_url and self.repository
                        and self.result.requested_url == self.repository.url
                        and self.result.verified_url.casefold() != self.repository.url.casefold())
        self.open.setEnabled(verified and not running and not self.stopped)
        self.add.setEnabled(self.open.isEnabled())
        self.open_recorded.setEnabled(self.history.count() > 0 and not self.stopped)

    def _check(self):
        if not self.check.isEnabled(): return
        self.result = None; self.pending = self.repository
        self.detail.setText('Checking repository location anonymously…')
        try:
            started = self.controller.start(self.pending, self.path.absolute())
            failed = not started and self.pending is not None
        except Exception:
            failed = True
        if failed:
            self.pending = self.result = None
            self.detail.setText('Location check failed to start; recorded history is retained.')
        self._lock()

    def _terminal(self, request_id, state, result):
        pending = self.pending; self.pending = None
        if self.stopped or pending != self.repository:
            self._lock(); return
        if state != 'COMPLETED':
            self.detail.setText({'CANCELLED': 'Location check cancelled.', 'TIMED_OUT': 'Location check deadline exceeded.'}
                                .get(state, 'Location check failed; recorded history is retained.'))
            self._lock(); return
        self.result = result; self.history.clear()
        self._details(True)
        # Keep the saved path accessible even when it was never a confirmed location.
        self.history.addItem('Saved Library URL: ' + pending.url, pending.url)
        for url, stamp in result.locations:
            try: date = datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
            except (ValueError, OverflowError, OSError): date = str(stamp) + ' epoch seconds'
            self.history.addItem(url + ' — observed ' + date, url)
        text = MESSAGES[result.outcome]
        if result.no_prior_identity:
            text += ' First observation: no prior identity. This does not prove continuity with legacy archives.'
        if result.repository_id: text += f' Recorded GitHub id: {result.repository_id}.'
        if result.observed_id and result.observed_id != result.repository_id:
            text += f' Saved URL currently resolves to id: {result.observed_id}.'
        text += (' Current verified location: ' + result.verified_url) if result.verified_url else ' No current verified location.'
        if result.reason not in ('ok', 'no_prior_identity', 'not_found', 'unsupported'):
            text += ' Reason: ' + result.reason.replace('_', ' ') + '.'
        if result.reason == 'state_invalid':
            text += ' History was not overwritten. Fix or move the invalid history file: ' + str(self.path)
        elif result.reason in ('state_busy', 'conflict'):
            text += ' Another check changed or locked the history; check again.'
        self.detail.setText(text); self._lock()

    def _open(self):
        if self.open.isEnabled(): self.open_requested.emit(self.result.verified_url)

    def _save(self):
        if self.add.isEnabled():
            outcome = self.save(self.result.verified_url)
            self.detail.setText(self.detail.text() + ' Library save: ' + outcome.name.replace('_', ' ').lower() + '.')

    def _open_recorded(self):
        if self.open_recorded.isEnabled(): self.open_requested.emit(self.history.currentData())

    def shutdown(self):
        self.stopped = True; self._lock()
        return self.controller.shutdown()

    def restore(self):
        self.stopped = False; self._lock()
