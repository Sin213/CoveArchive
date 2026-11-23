"""User-initiated Library health and explicitly confirmed archive updates."""
from dataclasses import dataclass
from datetime import datetime

from PySide6 import QtWidgets as W
from PySide6.QtCore import Qt, QTimer, Signal, QItemSelectionModel

from covearchive.archive import availability as a
from covearchive.archive.naming import validate_root
from covearchive.providers import parse_repository_url
from covearchive.scan_worker import ScanFailure
from covearchive.ui.queue_panel import _safe, _safe_lines
from covearchive.ui.health_process import HealthController
from covearchive.ui.scan_process import ScanController
from covearchive.ui.health_updates import HealthUpdates

HEADLINES = dict(up_to_date='Up to date', update_available='Update available',
    attention='Needs attention', not_checked='Not checked', unavailable='Unavailable or inaccessible',
    inconclusive='Inconclusive', no_releases='No published releases', unsupported='Unsupported')
COMPARISONS = dict(verified='Verified local archive', not_archived='New upstream release',
    partial='Partially archived', damaged='Damaged local files', missing_files='Missing local files',
    changed_upstream='Changed upstream metadata', tag_reused='Reused tag',
    unverified='Incomplete / unverified', inconclusive='Inconclusive',
    archive_unavailable='Archive unavailable', no_archivable_assets='No archivable assets',
    unsupported='Unsupported')


def attention(status):
    archive = status.archive
    return (status.headline in ('update_available', 'attention', 'unavailable', 'inconclusive')
            or bool(archive and (not archive.complete or archive.counts.missing or archive.counts.damaged
                                 or archive.counts.unsafe or archive.counts.untrusted))
            or (status.prerelease.release is not None and status.prerelease.comparison.state
                not in ('verified', 'no_archivable_assets')))


def review_reason(status):
    comparisons = [line.comparison.state for line in (status.stable, status.prerelease)
                   if line.release is not None]
    warnings = [COMPARISONS[state] for state in dict.fromkeys(comparisons)
                if state in ('tag_reused', 'changed_upstream')]
    if warnings:
        return 'Warning: ' + '; '.join(warnings) + '. Never replace archived data; inspect in Browse.'
    reasons = [label + ': ' + COMPARISONS[line.comparison.state]
               for label, line in (('Stable', status.stable), ('Prerelease', status.prerelease))
               if line.release is not None and line.comparison.state not in ('verified', 'no_archivable_assets')]
    if status.archive and (not status.archive.complete or status.archive.status != 'healthy'):
        reasons.append('Local audit needs attention; inspect the evidence before queueing.')
    return '; '.join(reasons) or COMPARISONS[status.stable.comparison.state]


def _accessible(widget, name):
    widget.setAccessibleName(name); widget.setAccessibleDescription(name)


def _table(headers):
    table = W.QTableWidget(0, len(headers)); table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(W.QAbstractItemView.SelectRows)
    table.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
    table.setTextElideMode(Qt.ElideRight); table.setWordWrap(False)
    table.verticalHeader().hide(); table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    table.setMinimumSize(0, 90)
    return table


def _item(text):
    item = W.QTableWidgetItem(_safe(text)); item.setToolTip(_safe_lines(text)); return item


@dataclass(frozen=True)
class Checked:
    status: a.RepositoryStatus
    time: str
    root: str
    note: str = ''


class HealthPanel(W.QWidget):
    open_requested = Signal(str)

    def __init__(self, library, controller, archive_root, settings, parent=None, *,
                 update_check=None, update_scan=None, bridge=None, launcher=None):
        super().__init__(parent)
        self.library, self.controller = library, controller
        self.archive_root, self.settings = archive_root, settings
        self.results = {}; self.known = {}; self._repos = (); self._members = set()
        self._pending = []; self._active = None; self.running = False; self._stopped = False
        self._batch = 0; self._cancelled = False
        self.processed = self.completed = self.failed = self.inconclusive = self.total = 0
        self._summary = ''; self._row_by_id = {}; self._rendered_ids = (); self._repo_by_id = {}
        self._dirty = True; self._review_dialog = None
        layout = W.QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(5)
        actions = W.QHBoxLayout(); actions.setSpacing(4); layout.addLayout(actions)
        def button(label, callback):
            value = W.QPushButton(label); _accessible(value, label); value.clicked.connect(callback)
            actions.addWidget(value); return value
        self.check_selected = button('Check Selected', self._check_selected)
        self.check_all = button('Check All', lambda: self.start(self._repos))
        self.cancel_button = button('Cancel', self.cancel)
        self.review_button = button('Review Updates', self.review)
        self.update_all = button('Update All…', self.review)
        self.update_all.setToolTip('Review eligible updates, verify current evidence, then confirm queueing.')
        actions.addStretch()
        self.progress = W.QLabel(''); self.progress.setTextFormat(Qt.PlainText); self.progress.setWordWrap(True)
        self.progress.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Preferred)
        _accessible(self.progress, 'Health check progress'); layout.addWidget(self.progress)
        self.table = _table(('Name', 'Provider', 'Availability', 'Latest stable', 'Latest prerelease',
                             'Health', 'Local comparison', 'Last check (session)', 'Needs attention'))
        _accessible(self.table, 'Repository health'); layout.addWidget(self.table, 1)
        for column, width in enumerate((155, 85, 120, 100, 115, 165, 160, 145, 110)):
            self.table.setColumnWidth(column, width)
        self.details = W.QPlainTextEdit(); self.details.setReadOnly(True)
        self.details.setMinimumHeight(65); self.details.setMaximumHeight(130)
        _accessible(self.details, 'Health evidence and explanation'); layout.addWidget(self.details)
        self.open_button = W.QPushButton('Open in Browse'); _accessible(self.open_button, 'Open health repository in Browse')
        self.open_button.clicked.connect(self.open_selected); layout.addWidget(self.open_button, 0, Qt.AlignLeft)
        self.table.itemSelectionChanged.connect(self._selection)
        controller.state_changed.connect(self._state); controller.terminal.connect(self._terminal)
        self.updates = HealthUpdates(
            update_check if update_check is not None else HealthController(launcher, parent=self),
            update_scan if update_scan is not None else ScanController(launcher, parent=self),
            bridge, library, lambda: self.results, settings, parent=self)
        self.updates.changed.connect(self._buttons)
        self.sync()

    def _selected(self):
        identities = [self._rendered_ids[index.row()] for index in self.table.selectionModel().selectedRows()
                      if index.row() < len(self._rendered_ids)]
        return [self._repo_by_id[key] for key in identities if key in self._repo_by_id]

    def sync(self):
        repos = tuple(self.library().repositories)
        members = {r.identity for r in repos}; removed = self._members - members
        for identity in removed:
            self.results.pop(identity, None); self.known.pop(identity, None)
        self._pending = [r for r in self._pending if r.identity not in removed]
        if self._active is not None and self._active['repo'].identity in removed:
            self._active['valid'] = False
        root = self.archive_root()
        expired = any(value.root != root for value in self.results.values())
        self.results = {key: value for key, value in self.results.items() if value.root == root}
        changed = repos != self._repos
        self._repos, self._members = repos, members
        self._repo_by_id = {r.identity: r for r in repos}
        self._dirty |= changed or expired
        self.updates.sync()
        # Collections remains responsive during large imports: build Health rows
        # only when this view is visible, not for every Library refresh.
        if self.isVisible() and self._dirty: self._render()
        self._buttons()

    def showEvent(self, event):
        self.sync(); self._render(); super().showEvent(event)

    def _render(self):
        selected = {r.identity for r in self._selected()}
        self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False); self.table.setRowCount(len(self._repos))
        self._rendered_ids = tuple(r.identity for r in self._repos)
        self._row_by_id = {r.identity: i for i, r in enumerate(self._repos)}
        for repo in self._repos: self._update_row(repo)
        self.table.clearSelection()
        for identity in selected & self._row_by_id.keys():
            self.table.selectionModel().select(self.table.model().index(self._row_by_id[identity], 0),
                QItemSelectionModel.Select | QItemSelectionModel.Rows)
        self._dirty = False
        self.table.setUpdatesEnabled(True); self.table.blockSignals(False); self._selection()

    def _update_row(self, repo):
        if not self.isVisible(): self._dirty = True; return
        row = self._row_by_id.get(repo.identity)
        if row is None or row >= len(self._rendered_ids) or self._rendered_ids[row] != repo.identity:
            self._dirty = True; return
        repo = self._repo_by_id.get(repo.identity)
        if repo is None: self._dirty = True; return
        checked = self.results.get(repo.identity); status = checked.status if checked else None
        known = self.known.get(repo.identity, (None, None))
        labels = []
        for index, line in enumerate((status.stable, status.prerelease) if status else (None, None)):
            release = line.release if line else None
            if release: labels.append(release.tag)
            elif known[index]: labels.append(known[index].tag + ' (last known)')
            elif line and line.state == 'none': labels.append('None published')
            else: labels.append('Unknown')
        texts = (repo.display_name, repo.provider, status.availability.state.replace('_', ' ').capitalize() if status else 'Not checked',
                 *labels, HEADLINES[status.headline] if status else 'Not checked',
                 COMPARISONS[status.stable.comparison.state] if status else 'Unknown',
                 checked.time if checked else 'Not checked', 'Yes' if status and attention(status) else 'No' if status and status.headline in ('up_to_date', 'no_releases') else 'Unknown')
        for column, text in enumerate(texts): self.table.setItem(row, column, _item(text))

    def _buttons(self):
        selected = self._selected()
        busy = self.running or self.updates.running
        self.check_selected.setEnabled(not busy and not self._stopped and bool(selected))
        self.check_all.setEnabled(not busy and not self._stopped and bool(self._repos))
        self.cancel_button.setEnabled((self.running and not self._cancelled) or
                                      (self.updates.running and self.updates.phase != 'stopping'))
        has_review = any(attention(c.status) for c in self.results.values())
        for button in (self.review_button, self.update_all): button.setEnabled(not busy and not self._stopped and has_review)
        self.open_button.setEnabled(len(selected) == 1)
        if not self._repos: self.progress.setText('No repositories. Add repositories in the Collections view.')
        elif not self.running and self.updates.progress: self.progress.setText(_safe_lines(self.updates.progress))
        elif not self.running: self.progress.setText(self._summary or 'Not checked. Checks run only when requested; no downloads or repairs.')

    def _check_selected(self): self.start(self._selected())

    def start(self, repositories):
        if self.running or self.updates.running or self._stopped: return
        self.sync(); repositories = [r for r in repositories if r.identity in self._members]
        if not repositories: return
        self._batch += 1; self.running = True; self._cancelled = False; self._summary = ''
        self.updates.progress = ''
        self._pending = list(repositories); self._batch_root = self.archive_root()
        self.total = len(self._pending); self.processed = self.completed = self.failed = self.inconclusive = 0
        for repo in repositories:
            self.results.pop(repo.identity, None); self._update_row(repo)
        self._buttons(); self._next(self._batch)

    def _progress(self, suffix):
        return (f'{self.processed}/{self.total} checked · {self.completed} completed · '
                f'{self.failed} failed · {self.inconclusive} inconclusive. {suffix}')

    def _next(self, batch):
        if batch != self._batch or not self.running or self._stopped: return
        if self._cancelled: self._end('Cancelled; completed results kept.'); return
        self.sync()
        if self.archive_root() != self._batch_root:
            self._end('Archive destination changed; run a new check.'); return
        if not self._pending: self._end('Finished.'); return
        repo = self._pending.pop(0)
        self._active = dict(repo=repo, request_id=None, valid=True)
        self.progress.setText(self._progress('Checking ' + _safe(repo.label)))
        try: started = self.controller.start(repo, self._batch_root)
        except (ValueError, TypeError, OSError): started = False
        if not started:
            self._active = None; self.failed += 1; self.processed += 1
            self._end('Health worker could not start. Unchecked repositories remain Not checked.')

    def _state(self, request_id, state):
        if self._active is not None and state == 'RUNNING' and self._active['request_id'] is None:
            self._active['request_id'] = request_id

    def _terminal(self, request_id, state, result):
        active = self._active
        if not self.running or self._stopped or active is None or request_id != active['request_id']: return
        self._active = None
        if self._cancelled or state == 'CANCELLED': self._end('Cancelled; completed results kept.'); return
        repo = active['repo']; self.sync()
        valid = active['valid'] and repo.identity in self._members and self.archive_root() == self._batch_root
        rate_limited = isinstance(result, a.RepositoryStatus) and (
            result.availability.state == 'rate_limited' or result.availability.reason == 'rate_limited')
        if valid:
            note = ''
            if isinstance(result, a.RepositoryStatus):
                identity = (result.repository.provider + ':' + result.repository.repo_key) if result.repository else None
                valid_status = identity == repo.identity if repo.provider == 'github' else result.headline == 'unsupported'
            else: valid_status = False
            if not valid_status:
                target = parse_repository_url(repo.url) if repo.provider == 'github' else None
                code = 'timeout' if state == 'TIMED_OUT' else 'protocol'
                result = a.assess(target, ScanFailure('', code), None)
                note = 'Worker deadline exceeded; verification is incomplete.' if state == 'TIMED_OUT' else 'Health worker failed or returned invalid evidence.'
            self.results[repo.identity] = Checked(result, datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S'), self._batch_root, note)
            old = self.known.get(repo.identity, (None, None))
            self.known[repo.identity] = tuple(line.release or old[i] for i, line in enumerate((result.stable, result.prerelease)))
            if note or result.headline == 'unavailable': self.failed += 1
            elif result.headline in ('inconclusive', 'not_checked', 'unsupported'): self.inconclusive += 1
            else: self.completed += 1
            self._update_row(repo)
        else: self.inconclusive += 1
        self.processed += 1; self._selection()
        if rate_limited:
            self._end('Stopped at GitHub rate limit. Remaining repositories stay Not checked; retry manually later.'); return
        batch = self._batch
        QTimer.singleShot(0, lambda: self._next(batch))

    def _end(self, message):
        self.running = False; self._pending.clear(); self._active = None
        self._summary = self._progress(message); self._buttons()

    def cancel(self):
        if self.updates.running: self.updates.cancel(); return
        if not self.running or self._cancelled: return
        self._cancelled = True; self._pending.clear(); self._buttons()
        if self._active is not None: self.controller.cancel()
        else: self._end('Cancelled; completed results kept.')

    def _selection(self):
        selected = self._selected(); self._buttons()
        if len(selected) != 1:
            self.details.setPlainText('Select one repository to see its health evidence.'); return
        repo = selected[0]; checked = self.results.get(repo.identity)
        if not checked:
            self.details.setPlainText('Not checked. No verified health evidence for this repository.'); return
        status = checked.status; archive = status.archive
        counts = archive.counts if archive else None
        # Audit categories count observations (including missing roots and
        # metadata), not assets. Only N1b's matched comparisons count missing
        # and damaged assets; unavailable evidence must stay Unknown.
        comparisons = [line.comparison for line in (status.stable, status.prerelease)
                       if line.release is not None and line.comparison.state != 'archive_unavailable']
        missing = sum(c.missing_assets for c in comparisons) if comparisons else 'Unknown'
        damaged = sum(c.damaged_assets for c in comparisons) if comparisons else 'Unknown'
        known = self.known.get(repo.identity, (None, None))
        lines = [HEADLINES[status.headline], status.availability.message,
                 'Latest stable (last known): ' + (known[0].tag if known[0] else 'Unknown'),
                 'Latest prerelease (last known): ' + (known[1].tag if known[1] else 'Unknown'),
                 f'Verified assets: {counts.verified if counts else "Unknown"}',
                 f'Missing assets: {missing}', f'Damaged assets: {damaged}',
                 f'Audit missing observations: {counts.missing if counts else "Unknown"}',
                 f'Audit damaged observations: {counts.damaged if counts else "Unknown"}',
                 f'Incomplete / unverified evidence: {counts.unverified + counts.untrusted + counts.unsafe if counts else "Unknown"}',
                 'Local verification complete: ' + ('Yes' if archive and archive.complete else 'No'),
                 'Audit truncated: ' + ('Yes' if archive and archive.truncated else 'No' if archive else 'Unknown'),
                 COMPARISONS[status.stable.comparison.state] + '. ' + status.stable.comparison.message,
                 'Prerelease: ' + status.prerelease.comparison.message,
                 review_reason(status), checked.note, 'Last check (session): ' + checked.time]
        self.details.setPlainText(_safe_lines('\n'.join(filter(None, lines))))

    def open_selected(self):
        selected = self._selected()
        if len(selected) == 1: self.open_requested.emit(selected[0].url)

    def prerequisites(self, repo):
        settings = self.settings()
        messages = []
        if not settings.get('repositories', {}).get(repo.ref):
            messages.append('No saved rules. Choose formats and filename filters in Browse and save rules.')
        try: validate_root(settings.get('archive_root', ''))
        except (ValueError, TypeError):
            messages.append('No saved archive destination. Choose a valid archive folder in Browse.')
        return ' '.join(messages)

    def review(self):
        if self.running or self.updates.running or self._stopped: return
        if self._review_dialog is not None:
            self._review_dialog.raise_(); self._review_dialog.activateWindow(); return
        self.sync()
        dialog = ReviewDialog(self); dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.finished.connect(lambda *_: self._review_finished(dialog))
        dialog.destroyed.connect(lambda *_: self._review_finished(dialog))
        self._review_dialog = dialog; dialog.open()

    def _review_finished(self, dialog):
        if self._review_dialog is dialog: self._review_dialog = None

    def shutdown(self, timeout_ms=3000):
        self._stopped = True; self.running = False; self._batch += 1
        self._pending.clear(); self._active = None; self._buttons()
        if getattr(self, '_review_dialog', None): self._review_dialog.close()
        updates_done = self.updates.shutdown(timeout_ms=timeout_ms)
        checks_done = self.controller.shutdown(timeout_ms=timeout_ms)
        return bool(updates_done and checks_done)


class ReviewDialog(W.QDialog):
    """Select eligible repositories, prepare without writes, then confirm jobs."""
    def __init__(self, health):
        super().__init__(health)
        self.health = health; self._batch = None; self.setWindowTitle('Review Updates'); self.resize(740, 430)
        layout = W.QVBoxLayout(self)
        self.notice = W.QLabel('Batch queue: select eligible repositories, then Queue Selected to rescan and verify. Nothing is queued until final confirmation. Confirmed jobs may start downloads. Cancel stops preparation or remaining submissions; submitted jobs are kept.')
        self.notice.setTextFormat(Qt.PlainText); self.notice.setWordWrap(True)
        self.notice.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Preferred)
        _accessible(self.notice, 'Confirmed batch queue behavior'); layout.addWidget(self.notice)
        destination = W.QHBoxLayout(); layout.addLayout(destination)
        destination.addWidget(W.QLabel('Archive destination'))
        self.destination = W.QLineEdit(health.archive_root() or health.settings().get('archive_root', '')); self.destination.setMaxLength(4096)
        _accessible(self.destination, 'Archive destination for selected updates'); destination.addWidget(self.destination, 1)
        self.choose_destination = W.QPushButton('Choose…'); _accessible(self.choose_destination, 'Choose archive destination')
        destination.addWidget(self.choose_destination); self.choose_destination.clicked.connect(self._choose_destination)
        selections = W.QHBoxLayout(); layout.addLayout(selections)
        self.select_all = W.QPushButton('Select Eligible'); _accessible(self.select_all, 'Select all eligible updates')
        self.clear = W.QPushButton('Clear Selection'); _accessible(self.clear, 'Deselect all updates')
        selections.addWidget(self.select_all); selections.addWidget(self.clear); selections.addStretch()
        self.select_all.clicked.connect(lambda: self._set_checked(Qt.Checked))
        self.clear.clicked.connect(lambda: self._set_checked(Qt.Unchecked))
        self.table = _table(('Repository / queue selection', 'Health', 'Review / action required'))
        self.table.setSelectionMode(W.QAbstractItemView.SingleSelection)
        _accessible(self.table, 'Repositories needing attention'); layout.addWidget(self.table, 1)
        self.rows = []
        for repo in health._repos:
            checked = health.results.get(repo.identity)
            if checked and attention(checked.status): self.rows.append((repo.identity, checked))
        self.table.setRowCount(len(self.rows))
        for index, (identity, checked) in enumerate(self.rows):
            repo = next(r for r in health._repos if r.identity == identity)
            for column, text in enumerate((repo.display_name, HEADLINES[checked.status.headline],
                    review_reason(checked.status) + ' ' + health.prerequisites(repo))):
                self.table.setItem(index, column, _item(text))
        self.table.setColumnWidth(0, 170); self.table.setColumnWidth(1, 150)
        self.explanation = W.QLabel('Select a row to inspect its update and prerequisites.'); self.explanation.setTextFormat(Qt.PlainText)
        self.explanation.setWordWrap(True); self.explanation.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Preferred)
        layout.addWidget(self.explanation)
        self.progress = W.QLabel(); self.progress.setTextFormat(Qt.PlainText); self.progress.setWordWrap(True)
        self.progress.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Preferred)
        _accessible(self.progress, 'Batch update progress'); layout.addWidget(self.progress)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.Close)
        self.queue_button = buttons.addButton('Queue Selected…', W.QDialogButtonBox.ActionRole)
        _accessible(self.queue_button, 'Prepare and confirm selected updates')
        self.queue_button.setToolTip('Rescan and verify selected repositories, then ask for explicit confirmation before queueing.')
        self.queue_button.clicked.connect(self._queue)
        self.cancel_button = buttons.addButton('Cancel', W.QDialogButtonBox.ActionRole)
        _accessible(self.cancel_button, 'Cancel update preparation or remaining submissions')
        self.cancel_button.clicked.connect(lambda: self.health.updates.cancel())
        self.open_button = buttons.addButton('Open in Browse', W.QDialogButtonBox.ActionRole)
        _accessible(self.open_button, 'Open reviewed repository in Browse'); self.open_button.setEnabled(False)
        self.table.itemSelectionChanged.connect(self._selection)
        self.table.itemChanged.connect(self._selection)
        self.open_button.clicked.connect(self._open); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.destination.textChanged.connect(self._refresh)
        health.updates.changed.connect(self._refresh); health.updates.ready.connect(self._ready)
        self.finished.connect(self._finished)
        self._refresh()

    def _choose_destination(self):
        root = W.QFileDialog.getExistingDirectory(self, 'Choose archive destination', self.destination.text())
        if root: self.destination.setText(root)

    def _set_checked(self, state):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item.flags() & Qt.ItemIsUserCheckable: item.setCheckState(state)

    def _refresh(self, *_):
        updates = self.health.updates; busy = updates.running
        current = {repo.identity: repo for repo in self.health.library().repositories}
        self.table.blockSignals(True)
        eligible = 0
        for index, (identity, checked) in enumerate(self.rows):
            repo = current.get(identity)
            problem = 'Repository or health evidence changed; check again.'
            if repo is not None and self.health.results.get(identity) is checked:
                problem = updates.problem(repo, checked, self.destination.text())
            item = self.table.item(index, 0)
            flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
            if problem:
                item.setData(Qt.CheckStateRole, None)
            else:
                flags |= Qt.ItemIsUserCheckable; eligible += 1
                if item.data(Qt.CheckStateRole) is None: item.setCheckState(Qt.Unchecked)
            item.setFlags(flags)
            explanation = review_reason(checked.status)
            if problem: explanation += ' ' + problem
            if identity in updates.messages: explanation += ' ' + updates.messages[identity]
            self.table.item(index, 2).setText(_safe_lines(explanation))
            self.table.item(index, 2).setToolTip(_safe_lines(explanation))
        self.table.blockSignals(False)
        self.table.setEnabled(not busy); self.destination.setEnabled(not busy); self.choose_destination.setEnabled(not busy)
        self.select_all.setEnabled(not busy and bool(eligible)); self.clear.setEnabled(not busy)
        self.cancel_button.setEnabled(busy and updates.phase != 'stopping')
        self.progress.setText(_safe_lines(updates.progress) if updates.progress else 'Select eligible repositories to prepare updates.')
        self._selection()

    def _selection(self, *_):
        busy = self.health.updates.running
        rows = self.table.selectionModel().selectedRows(); self.open_button.setEnabled(len(rows) == 1 and not busy)
        if len(rows) == 1: self.explanation.setText(self.table.item(rows[0].row(), 2).text())
        self.queue_button.setEnabled(not busy and any(self.table.item(row, 0).flags() & Qt.ItemIsUserCheckable
            and self.table.item(row, 0).checkState() == Qt.Checked for row in range(len(self.rows))))

    def _queue(self):
        selected = [(identity, checked) for row, (identity, checked) in enumerate(self.rows)
                    if self.table.item(row, 0).flags() & Qt.ItemIsUserCheckable
                    and self.table.item(row, 0).checkState() == Qt.Checked]
        self._batch = self.health.updates.prepare(selected, self.destination.text())
        self._refresh()

    def _ready(self, batch):
        updates = self.health.updates
        if self._batch != batch or not self.isVisible(): return
        root = updates.proposals[0]['root']
        if self.destination.text() != root:
            updates.cancel('Archive destination changed; prepare again. Nothing queued.'); return
        accepted = self._confirm_queue(len(updates.proposals), sum(len(ctx['items']) for ctx in updates.proposals),
                                       root)
        if self.destination.text() != root:
            updates.cancel('Archive destination changed; prepare again. Nothing queued.'); return
        updates.confirm(batch, accepted)

    def _confirm_queue(self, repositories, files, root):
        dialog = W.QMessageBox(self); dialog.setWindowTitle('Confirm selected updates')
        dialog.setTextFormat(Qt.PlainText)
        dialog.setText(f'Queue {files} file(s) from {repositories} repository/repositories to:\n{root}\n\n'
                       'Saved format and filename rules apply. Verified local files and completed jobs are skipped. '
                       'New jobs may start downloading. Existing archived data will not be replaced.')
        dialog.setStandardButtons(W.QMessageBox.Yes | W.QMessageBox.No); dialog.setDefaultButton(W.QMessageBox.No)
        return dialog.exec() == W.QMessageBox.Yes

    def _finished(self, *_):
        if self._batch and self.health.updates.running and self.health.updates._batch == self._batch:
            self.health.updates.cancel()

    def _open(self):
        if self.health.updates.running: return
        rows = self.table.selectionModel().selectedRows()
        if len(rows) != 1: return
        identity, checked = self.rows[rows[0].row()]
        repo = next((r for r in self.health.library().repositories if r.identity == identity), None)
        if repo is None or self.health.results.get(identity) is not checked:
            self.explanation.setText('Repository or health evidence changed. Close this review and check again.'); self.open_button.setEnabled(False); return
        self.health.open_requested.emit(repo.url); self.accept()
