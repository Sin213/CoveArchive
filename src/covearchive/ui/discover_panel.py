"""Explicit, anonymous repository searches over the supervised Discover worker."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from PySide6.QtCore import QEvent, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QContextMenuEvent, QDesktopServices, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSplitter,
    QVBoxLayout, QWidget,
)

from covearchive.discover.models import EvidenceSource, SearchResult, Tri
from covearchive.github.urls import RepositoryTarget, parse_github_url
from covearchive.discover.query import MAX_RETRIEVABLE, SearchQueryError, build_search_query
from covearchive.discover.worker import DiscoverFailure, DiscoverSuccess, ERROR_CODES
from .queue_panel import _safe_lines
from .theme import set_role, set_tone


KINDS = (('All', 'all'), ('Emulators', 'emulators'), ('Recomps', 'recomps'),
         ('Ports', 'ports'), ('Decomps', 'decomps'))
SORTS = (('Relevance', 'relevance'), ('Most stars', 'stars'), ('Recently updated', 'updated'))
ERROR_TEXT = dict.fromkeys(ERROR_CODES, 'GitHub returned unexpected data. Retry the search.')
ERROR_TEXT.update(
    rate_limited='GitHub rate limit reached. Retry later.',
    timeout='GitHub did not respond in time. Retry the search.',
    network='Could not connect to GitHub. Check your connection and retry.',
    server_error='GitHub is unavailable. Retry later.',
    invalid_search='Enter a title or keywords (up to 100 searchable characters).',
    forbidden='GitHub denied this anonymous search. Retry later.',
    unauthorized='GitHub denied this anonymous search. Retry later.',
    process='The Discover worker could not start or stopped unexpectedly. Retry the search.',
    protocol='The Discover worker returned unexpected data. Retry the search.',
    internal_error='Discover could not complete the search. Retry the search.',
)


@dataclass(frozen=True)
class SearchSnapshot:
    text: str
    kind: str
    sort: str
    page: int = 1
    per_page: int = 30


def discovered_repository_url(result):
    """Use the same strict result/URL consistency check for every handoff."""
    if not isinstance(result, SearchResult): raise ValueError
    target = parse_github_url(result.url)
    if (target != RepositoryTarget(result.owner, result.name)
            or result.url != f'https://github.com/{result.owner}/{result.name}'):
        raise ValueError
    return result.url


class _ElidedLabel(QLabel):
    """Use the main window's single-line diagnostic pattern, with full text retained."""

    def __init__(self, text):
        super().__init__(text)
        self.setToolTip(_safe_lines(text))

    def setText(self, text):
        super().setText(text)
        self.setToolTip(_safe_lines(text))
        self.setAccessibleDescription(text)

    def sizeHint(self):
        return QSize(100, self.fontMetrics().height() + 2)

    def minimumSizeHint(self):
        return QSize(0, self.fontMetrics().height() + 2)

    def heightForWidth(self, width):
        return self.sizeHint().height()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.contentsRect().width())
        painter.drawText(self.contentsRect(), self.alignment() | Qt.AlignVCenter, text)


def failure_text(result):
    """Only fixed messages and typed, known rate-limit metadata cross into UI."""
    if not isinstance(result, DiscoverFailure):
        return 'Discover could not complete the search. Retry the search.'
    text = ERROR_TEXT.get(result.code, 'Discover could not complete the search. Retry the search.')
    if result.code != 'rate_limited' or not isinstance(result.detail, dict):
        return text
    delay = result.detail.get('retry_after_seconds')
    if type(delay) is int and delay >= 0:
        return text + f' Retry after {delay} seconds.'
    for key, label in (('retry_at', 'Retry at'), ('reset_at', 'Limit resets at')):
        value = result.detail.get(key)
        if type(value) is int and value >= 0:
            try:
                stamp = datetime.fromtimestamp(value, timezone.utc)
            except (ValueError, OverflowError, OSError):
                continue
            return text + f' {label} {stamp.isoformat()}.'
        if isinstance(value, str) and len(value) <= 40:
            try:
                stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                continue
            return text + f' {label} {stamp.isoformat()}.'
    return text


class DiscoverPanel(QWidget):
    open_requested = Signal(object)
    save_requested = Signal(object)
    library_requested = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.active_id = None
        self.snapshot = None
        self.display_snapshot = None
        self.page = None
        self._pending = None
        self._starting = False
        self._closed = False
        self._retryable = False
        self.saved_lookup = lambda url: None
        self.setAccessibleName('Discover repositories')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        self.intro = self._label(
            'Find public GitHub emulators, recompilations, ports and decomps. '
            'Open a repository in Browse to check its releases. Classification is a hint; '
            'official status and playability are not verified.', 'About Discover')
        set_tone(self.intro, 'muted')
        layout.addWidget(self.intro)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('Game title or keywords')
        self.search.setMaxLength(256)
        self.search.setMinimumWidth(80)
        self.search_button = QPushButton('Search')
        self.cancel = QPushButton('Cancel')
        set_role(self.search_button, 'primary')
        set_role(self.cancel, 'danger')
        row.addWidget(self.search, 1)
        row.addWidget(self.search_button)
        row.addWidget(self.cancel)
        layout.addLayout(row)
        filters = QHBoxLayout()
        self.category = QComboBox()
        self.sort = QComboBox()
        for label, value in KINDS:
            self.category.addItem(label, value)
        for label, value in SORTS:
            self.sort.addItem(label, value)
        category_label = self._label('Category', 'Category label')
        sort_label = self._label('Sort', 'Sort label')
        for label in (category_label, sort_label):
            label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        filters.addWidget(category_label)
        filters.addWidget(self.category)
        filters.addWidget(sort_label)
        filters.addWidget(self.sort)
        filters.addStretch(1)
        layout.addLayout(filters)
        self.query_label = self._label('No search yet', 'Displayed search', elide=True)
        layout.addWidget(self.query_label)
        self.splitter = QSplitter(Qt.Vertical)
        self.results = QListWidget()
        self.results.setMinimumSize(0, 90)
        self.results.setTextElideMode(Qt.ElideRight)
        self.results.setUniformItemSizes(True)
        self.results.setTabKeyNavigation(False)
        self.results.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._context_menu)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMinimumSize(0, 70)
        self.details.setPlainText('Select a repository to inspect its description and classification evidence.')
        self.splitter.addWidget(self.results)
        self.splitter.addWidget(self.details)
        self.splitter.setSizes([210, 160])
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter, 1)
        actions = QHBoxLayout()
        actions.setSpacing(4)
        self.add_button = QPushButton('Add to Library')
        self.open_button = QPushButton('Open in Browse')
        self.previous = QPushButton('Previous')
        self.next = QPushButton('Next')
        self.retry = QPushButton('Retry')
        self.more_button = QPushButton('More')
        self.more_menu = QMenu(self)
        self.more_menu.setAccessibleName('Discover repository actions')
        self.more_menu.setAccessibleDescription('Open on GitHub, copy the repository URL or go to Library.')
        self.github_action = self.more_menu.addAction('Open on GitHub', self.open_on_github)
        self.copy_action = self.more_menu.addAction('Copy Repository URL', self.copy_repository_url)
        self.library_action = self.more_menu.addAction('Go to Library', self.go_to_library)
        for action in self.more_menu.actions():
            action.setToolTip(action.text())
            action.setStatusTip(action.text())
        self.more_button.setMenu(self.more_menu)
        for widget in (self.add_button, self.open_button, self.previous, self.next, self.retry, self.more_button):
            actions.addWidget(widget)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumHeight(6)
        layout.addWidget(self.progress)
        self.status = self._label('Enter a title or keywords, then search. No requests run until you search.', 'Discover status', elide=True)
        layout.addWidget(self.status)
        controls = (
            (self.search, 'Discover search', 'Enter a title or keywords. Enter searches; Down selects a result.'),
            (self.search_button, 'Search', 'Search public GitHub repositories anonymously.'),
            (self.cancel, 'Cancel Discover search', 'Cancel the active search. Escape also cancels.'),
            (self.category, 'Discover category', 'Category for the next explicit search.'),
            (self.sort, 'Discover sort', 'Server-side sort for the next explicit search.'),
            (self.results, 'Discover results', 'Arrow keys select a repository; Enter opens it in Browse.'),
            (self.details, 'Repository details', 'Description, classification evidence and unverified metadata. Read only.'),
            (self.add_button, 'Add to Library', 'Save the selected repository to Unsorted without enabling Auto-Archive.'),
            (self.open_button, 'Open in Browse', 'Scan the selected repository for releases.'),
            (self.previous, 'Previous Discover page', 'Fetch the previous page of the displayed search.'),
            (self.next, 'Next Discover page', 'Fetch the next page of the displayed search.'),
            (self.retry, 'Retry Discover search', 'Retry the last failed, timed out or cancelled search. F5 also retries.'),
            (self.more_button, 'More repository actions', 'Open on GitHub, copy the repository URL or go to Library. Also available with Menu or Shift+F10 on a result.'),
        )
        self.focus_controls = [widget for widget, _, _ in controls]
        for widget, name, description in controls:
            widget.setAccessibleName(name)
            widget.setAccessibleDescription(description)
        for widget in (self.search, self.results):
            widget.installEventFilter(self)
        self.search.returnPressed.connect(self.submit)
        self.search_button.clicked.connect(self.submit)
        self.cancel.clicked.connect(self.cancel_search)
        self.previous.clicked.connect(lambda: self._turn_page(-1))
        self.next.clicked.connect(lambda: self._turn_page(1))
        self.retry.clicked.connect(self.retry_search)
        self.results.currentRowChanged.connect(self._selection_changed)
        self.results.itemActivated.connect(lambda *_: self.open_selected())
        self.open_button.clicked.connect(self.open_selected)
        self.add_button.clicked.connect(self.save_selected)
        self.shortcuts = []
        for key, callback in (('Esc', self.cancel_search), ('F5', self.retry_search)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)
        controller.state_changed.connect(self._state)
        controller.terminal.connect(self._terminal)
        self._controls()

    @staticmethod
    def _label(text, name, *, elide=False):
        label = _ElidedLabel(text) if elide else QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(not elide)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed if elide else QSizePolicy.Preferred)
        label.setAccessibleName(name)
        label.setAccessibleDescription(name)
        return label

    def focus_search(self):
        self.search.setFocus(Qt.ShortcutFocusReason)

    def eventFilter(self, watched, event):
        if watched is self.results and (
                (event.type() == QEvent.ContextMenu and event.reason() == QContextMenuEvent.Keyboard)
                or (event.type() == QEvent.KeyPress and (event.key() == Qt.Key_Menu
                    or (event.key() == Qt.Key_F10 and event.modifiers() == Qt.ShiftModifier)))):
            item = self.results.currentItem()
            if item is not None:
                self._context_menu(self.results.visualItemRect(item).center())
            return True
        if (watched is self.search and event.type() == QEvent.KeyPress
                and event.key() == Qt.Key_Down and self.results.count()):
            if self.results.currentRow() < 0:
                self.results.setCurrentRow(0)
            self.results.setFocus(Qt.TabFocusReason)
            return True
        return super().eventFilter(watched, event)

    def submit(self):
        self._submit(SearchSnapshot(self.search.text(), self.category.currentData(), self.sort.currentData()))

    def _submit(self, snapshot):
        if self._closed or self.active_id is not None or self._starting:
            return
        try:
            build_search_query(snapshot.text, snapshot.kind, snapshot.sort,
                               page=snapshot.page, per_page=snapshot.per_page)
        except SearchQueryError:
            self.show_message(ERROR_TEXT['invalid_search'], error=True)
            return
        # RUNNING can arrive inside start(), and a terminal can follow it before
        # start() returns. Never infer the request identity from its return value.
        self._pending = snapshot
        self._starting = True
        try:
            accepted = self.controller.start(snapshot.text, snapshot.kind, snapshot.sort,
                                             snapshot.page, snapshot.per_page)
        finally:
            self._starting = False
            self._pending = None
        if not accepted and self.active_id is None:
            self.show_message('Discover could not start. Another search may still be stopping; try again when it finishes.', error=True)
        self._controls()

    def _state(self, request_id, state):
        if self._closed:
            return
        if state == 'RUNNING':
            if not self._starting or self._pending is None or self.active_id is not None:
                return
            self.active_id = request_id
            self.snapshot = self._pending
            self._retryable = False
        elif request_id != self.active_id:
            return
        messages = dict(RUNNING='Searching GitHub…', STOPPING='Stopping search…',
                        STOP_SLOW='Stopping search: the worker is taking longer than expected…')
        if state in messages:
            self.show_message(messages[state])
        self.cancel.setEnabled(state == 'RUNNING')
        self._controls()

    def _terminal(self, request_id, state, result):
        if self._closed or self.active_id is None or request_id != self.active_id:
            return
        self.active_id = None
        self._retryable = state in ('FAILED', 'TIMED_OUT', 'CANCELLED')
        if state == 'COMPLETED' and isinstance(result, DiscoverSuccess):
            self.page = result.page
            self.display_snapshot = self.snapshot
            self.query_label.setText(f'Results for “{self.snapshot.text}” · {self.snapshot.kind} · {self.snapshot.sort} · Page {self.page.page}')
            self.results.clear()
            for record in self.page.results:
                kinds = ', '.join(kind.value for kind in record.kinds) or 'Unclassified'
                stars = f'{record.stars} stars' if record.stars is not None else 'Stars unknown'
                activity = record.last_activity[:10] if record.last_activity else 'unknown'
                flags = (' · Fork' if record.fork == Tri.YES else '') + (' · Archived' if record.archived == Tri.YES else '')
                item = QListWidgetItem(f'{record.owner}/{record.name}\n{kinds} · GitHub · {stars} · Last activity: {activity}{flags}')
                item.setData(Qt.UserRole, record)
                item.setData(Qt.UserRole + 1, item.text())
                self.results.addItem(item)
            self.refresh_saved_state()
            messages = [f'{len(self.page.results)} repositories on page {self.page.page}.' if self.page.results else 'No matches. Try another title or category.']
            if self.page.incomplete_results:
                messages.append('Partial results: GitHub reported an incomplete search. Retry may find more.')
            invalid = self.page.dropped - self.page.duplicates
            for count, category in ((invalid, 'invalid'), (self.page.duplicates, 'duplicate')):
                if count > 0:
                    noun = 'record' if count == 1 else 'records'
                    messages.append(f'{count} {category} {noun} omitted.')
            if self.page.total_count > self.page.retrievable_total:
                messages.append('GitHub limits retrieval to the first 1,000 results; narrow your search.')
            self.show_message(' '.join(messages))
        elif state == 'CANCELLED':
            self.show_message('Search cancelled. You can retry when ready.')
        elif state == 'TIMED_OUT':
            self.show_message('Search timed out. Retry when ready.', error=True)
        else:
            self.show_message(failure_text(result), error=True)
        self._controls()

    def _controls(self):
        active = self.active_id is not None
        for control in (self.search_button, self.category, self.sort):
            control.setEnabled(not active and not self._closed)
        self.cancel.setVisible(active)
        if not active:
            self.cancel.setEnabled(False)
        self.progress.setVisible(active)
        self.open_button.setEnabled(not active and self.results.currentItem() is not None and not self._closed)
        self.add_button.setEnabled(self.open_button.isEnabled())
        self.more_button.setEnabled(self.open_button.isEnabled())
        for action in (self.github_action, self.copy_action):
            action.setEnabled(self.open_button.isEnabled())
        saved = not active and not self._closed and self._selected_is_saved()
        self.library_action.setVisible(saved)
        self.library_action.setEnabled(saved)
        self.previous.setEnabled(not active and self.page is not None and self.page.page > 1 and not self._closed)
        self.next.setEnabled(not active and self.page is not None and self.page.has_next_page
                             and (self.page.page + 1) * 30 <= MAX_RETRIEVABLE and not self._closed)
        self.retry.setEnabled(not active and self._retryable and not self._closed)

    def cancel_search(self):
        if not self._closed and self.active_id is not None and self.cancel.isEnabled():
            self.controller.cancel()

    def retry_search(self):
        if self.retry.isEnabled() and self.snapshot is not None:
            self._submit(self.snapshot)

    def _turn_page(self, delta):
        button = self.next if delta > 0 else self.previous
        if button.isEnabled() and self.display_snapshot is not None:
            self._submit(replace(self.display_snapshot, page=self.page.page + delta))

    def _selection_changed(self, *_):
        if self._closed: return
        item = self.results.currentItem()
        if item is None:
            self._set_details('Select a repository to inspect its description and classification evidence.')
        else:
            record = item.data(Qt.UserRole)
            try: discovered_repository_url(record)
            except (ValueError, TypeError):
                self._set_details('This result has an invalid GitHub repository URL.')
                self._controls()
                return
            evidence = []
            for classification in record.classifications:
                for hint in classification.evidence:
                    source = 'weak name match' if hint.source == EvidenceSource.NAME else hint.source.value
                    evidence.append(f'{classification.kind.value}: {source} — {hint.matched}')
            self._set_details(
                f'{record.owner}/{record.name}\n\n{record.description or "Description: Unknown"}\n\n'
                'Why matched: title keywords in name, description or topics, plus category terms.\n'
                'Classification evidence (hints, not authenticity):\n' + ('\n'.join(evidence) or 'None recorded') +
                f'\n\nSource: GitHub\nSource URL: {record.url}\n'
                f'Fork: {record.fork.value}\nArchived: {record.archived.value}\n'
                'Release availability: Not checked\nOfficial status: Not verified\n'
                'Original platform: Unknown\nSupported host OS: Unknown\n'
                'A repository is not necessarily a playable game. Stars do not establish safety.' +
                f'\n\nLibrary: {self._saved_text(record.url) or "Not saved"}')
        self._controls()

    def _set_details(self, text):
        if self.details.toPlainText() != text:
            self.details.setPlainText(text)

    def _selected_is_saved(self):
        item = self.results.currentItem()
        if item is None: return False
        try: url = discovered_repository_url(item.data(Qt.UserRole))
        except (ValueError, TypeError): return False
        return self.saved_lookup(url) is True

    def _saved_text(self, url):
        saved = self.saved_lookup(url)
        return 'Library unavailable' if saved is None else 'In Library' if saved else ''

    def refresh_saved_state(self):
        """Update existing items in place; never search or rebuild the results list."""
        if self._closed: return
        scrollbars = [(bar, bar.value()) for view in (self.results, self.details)
                      for bar in (view.verticalScrollBar(), view.horizontalScrollBar())]
        for row in range(self.results.count()):
            item = self.results.item(row)
            try: url = discovered_repository_url(item.data(Qt.UserRole))
            except (ValueError, TypeError): marker = ''
            else: marker = self._saved_text(url)
            item.setText(item.data(Qt.UserRole + 1) + (f' · {marker}' if marker else ''))
        self._selection_changed()
        for bar, value in scrollbars:
            bar.setValue(value)

    def save_selected(self):
        item = self.results.currentItem()
        if not self._closed and self.add_button.isEnabled() and item is not None:
            self.save_requested.emit(item.data(Qt.UserRole))

    def show_save_outcome(self, outcome):
        from .library_panel import SaveDiscoveredOutcome as Outcome
        if self._closed: return
        messages = {
            Outcome.ADDED: 'Added to Library (Unsorted)',
            Outcome.ALREADY_SAVED: 'Already saved',
            Outcome.UNAVAILABLE: 'Library unavailable (busy or importing)...',
            Outcome.READ_ONLY: 'Library is read-only...',
            Outcome.INVALID: 'Cannot save this result: its GitHub repository URL is invalid.',
            Outcome.SAVE_FAILED: 'Save failed...',
        }
        self.refresh_saved_state()
        self.show_message(messages[outcome], error=outcome not in (Outcome.ADDED, Outcome.ALREADY_SAVED))

    def _selected_url(self):
        if self._closed or not self.more_button.isEnabled(): return None
        item = self.results.currentItem()
        try: return discovered_repository_url(item.data(Qt.UserRole)) if item else None
        except (ValueError, TypeError):
            self.show_message('Cannot open this result: its GitHub repository URL is invalid.', error=True)
            return None

    def open_on_github(self):
        url = self._selected_url()
        if url and not QDesktopServices.openUrl(QUrl(url)):
            self.show_message('Could not open the browser', error=True)

    def copy_repository_url(self):
        url = self._selected_url()
        if url:
            QApplication.clipboard().setText(url)
            self.show_message('Copied repository URL')

    def go_to_library(self):
        if not self._closed and self.library_action.isEnabled() and self._selected_is_saved():
            self.library_requested.emit()

    def _context_menu(self, pos):
        if self._closed or self.active_id is not None: return
        item = self.results.itemAt(pos)
        if item is None: return
        if item is not self.results.currentItem(): self.results.setCurrentItem(item)
        self.more_menu.popup(self.results.viewport().mapToGlobal(pos))
        return self.more_menu

    def open_selected(self):
        item = self.results.currentItem()
        if not self._closed and self.open_button.isEnabled() and item is not None:
            self.open_requested.emit(item.data(Qt.UserRole))

    def show_message(self, text, *, error=False):
        if self._closed: return
        self.status.setText(text)
        set_tone(self.status, 'error' if error else 'muted')
        self._controls()

    def disconnect_controller(self):
        if not self._closed:
            self.controller.state_changed.disconnect(self._state)
            self.controller.terminal.disconnect(self._terminal)
            for shortcut in self.shortcuts:
                shortcut.setEnabled(False)
            self._closed = True
            self.more_menu.close()
            self._controls()

    def closeEvent(self, event):
        self.disconnect_controller()
        super().closeEvent(event)
