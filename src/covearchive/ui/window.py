"""Native, read-only release scanner with local filtering and lazy tree rows."""

import logging
import os
import sys
import time
import unicodedata

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, QTimer, QSize
from PySide6.QtGui import QKeySequence, QShortcut, QBrush, QColor, QPainter
from covearchive.ui.theme import ThemeTracker, set_role, set_tone, current_tokens, fixed_font
from PySide6.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QLabel, QLineEdit,
    QFrame, QSizePolicy, QHeaderView, QMainWindow, QProgressBar, QPushButton, QTreeView, QVBoxLayout, QWidget, QFileDialog, QTabWidget, QTabBar, QMessageBox)
from covearchive.ui.icons import chrome_icon
from covearchive.ui.settings_dialog import SettingsDialog
from covearchive.ui import browse_panes

from covearchive.formats import FORMAT_LABELS, SUPPORTED_EXTENSIONS, DEFAULT_EXTENSIONS
from covearchive.build_filters import BuildRules, BuildFilterError, parse_patterns, allowed
from covearchive.archive.naming import validate_root, project_components, release_folder_name
from pathlib import Path
from covearchive.ui.preferences import PreferenceStore
from covearchive.ui.library_store import LibraryStore
from covearchive.ui.library_panel import LibraryPanel
from covearchive.queue.store import default_queue_path, QueueError
from covearchive.ui.branding import app_icon
from covearchive.ui.archive_bridge import ArchiveBridge, EnqueueRequest, EnqueueItem
from covearchive.github.select import select_releases
from covearchive.github.urls import GitHubUrlError, ReleaseTagTarget, ReleaseListTarget, RepositoryTarget
from covearchive.providers import parse_repository_url, canonical_url, display_name
from covearchive.scan_worker import ProtocolError, ScanFailure
from covearchive.ui.scan_process import ScanController
from covearchive.ui.auto_archive import AutoArchive
from covearchive.ui.git_history import HistoryController, ERRORS, STAGES, result_text


from covearchive.ui.queue_panel import display, size_text, state_text, QueuePanel, _error, _safe, _safe_lines


class ReleaseModel(QAbstractItemModel):
    """Indexes reference immutable backend objects; children are built on demand."""
    headers = ("Name", "Tag", "Published", "Format", "Size", "Status")

    def __init__(self, selection=None, parent=None, *, checked=None, rules=BuildRules()):
        super().__init__(parent)
        self._theme_widget = parent
        self.rows = () if selection is None else selection.releases
        self.enabled = () if selection is None else selection.enabled_extensions
        self.checked = checked if checked is not None else set(); self.rules=rules
        self.base = tuple(frozenset(id(m.asset) for m in row.matches) for row in self.rows)
        self.matches = tuple(frozenset(id(m.asset) for m in row.matches if allowed(rules,m.asset.name)) for row in self.rows)
        self.order=[list(range(len(row.release.assets))) for row in self.rows]
        self.name_parts=[]
        for row in self.rows:
            names=[display(a.name) for a in row.release.assets]; prefix=os.path.commonprefix(names); suffix=os.path.commonprefix([name[len(prefix):][::-1] for name in names])[::-1]; self.name_parts.append((prefix,suffix))

    def sort(self,column,order=Qt.AscendingOrder):
        if column not in (0,3,4,5): return
        self.layoutAboutToBeChanged.emit()
        old=self.persistentIndexList(); identities=[(i.internalId()-1,self.order[i.internalId()-1][i.row()]) if i.internalId() else None for i in old]
        ranks={'Eligible':0,'Excluded by filename rules':1,'Not matched by include rules':1,'Filtered out':2,'Duplicate asset':3,'Unsupported format':4,'Not uploaded':5,'Not verifiable: needs exactly one package file with size and SHA-256':5,'External link, not archived':6}
        for root,row in enumerate(self.rows):
            parent=self.index(root,0)
            status={source:ranks[self.data(self.index(i,5,parent))] for i,source in enumerate(self.order[root])} if column==5 else {}
            def key(source):
                asset=row.release.assets[source]
                return asset.name.casefold() if column==0 else (asset.supported_extension or '') if column==3 else (asset.size if asset.size is not None else -1) if column==4 else status[source]
            self.order[root]=sorted(range(len(row.release.assets)),key=key,reverse=order==Qt.DescendingOrder)
        new=[self.index(self.order[root].index(source),i.column(),self.index(root,0)) if identity is not None else i for i,identity in zip(old,identities) for root,source in ([identity] if identity is not None else [(0,0)])]
        self.changePersistentIndexList(old,new); self.layoutChanged.emit()
        if self._theme_widget is not None:
            window=self._theme_widget.window()
            if hasattr(window,'tree'):
                window._sort_state=(column,order); window.tree.header().setSortIndicator(column,order); window.tree.header().setSortIndicatorShown(True)

    def _keys(self,index):
        if not index.isValid() or index.column()!=0: return set()
        root=index.internalId()-1 if index.internalId() else index.row(); release=self.rows[root].release
        assets=(release.assets[self.order[root][index.row()]],) if index.internalId() else release.assets
        return {(release.id,a.id) for a in assets if id(a) in self.matches[root]}

    def flags(self,index):
        return super().flags(index) | (Qt.ItemIsUserCheckable if self._keys(index) else Qt.NoItemFlags)

    def setData(self,index,value,role=Qt.EditRole):
        keys=self._keys(index)
        if role!=Qt.CheckStateRole or not keys: return False
        if value in (Qt.Checked,Qt.Checked.value): self.checked.update(keys)
        else: self.checked.difference_update(keys)
        self.layoutChanged.emit(); return True

    def columnCount(self, parent=QModelIndex()): return 6

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid(): return len(self.rows)
        if parent.column() != 0 or parent.internalId() != 0: return 0
        return len(self.rows[parent.row()].release.assets)

    def index(self, row, column, parent=QModelIndex()):
        if row < 0 or column < 0 or column >= 6 or row >= self.rowCount(parent): return QModelIndex()
        return self.createIndex(row, column, parent.row()+1 if parent.isValid() else 0)

    def parent(self, index):
        if not index.isValid() or index.internalId() == 0: return QModelIndex()
        return self.createIndex(index.internalId()-1, 0, 0)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal: return self.headers[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid(): return None
        root = index.internalId()-1 if index.internalId() else index.row()
        selected = self.rows[root]; release = selected.release
        asset = release.assets[self.order[root][index.row()]] if index.internalId() else None
        obj = asset or release
        if role == Qt.UserRole: return obj
        if asset and role == Qt.FontRole and index.column() in (0,4): return fixed_font(self._theme_widget)
        if asset and role == Qt.TextAlignmentRole and index.column()==4: return Qt.AlignRight|Qt.AlignVCenter
        if role == Qt.ToolTipRole and index.column()==0:
            return _safe(asset.name if asset else f'{release.tag_name} · {release.name or release.tag_name}')
        if role == Qt.CheckStateRole and index.column()==0:
            keys=self._keys(index); count=len(keys & self.checked)
            if not keys: return None
            return Qt.Checked if keys and count==len(keys) else Qt.PartiallyChecked if count else Qt.Unchecked
        if role not in (Qt.DisplayRole, Qt.ToolTipRole, Qt.ForegroundRole): return None
        if asset:
            status = ("External link, not archived" if asset.state == "external_link" else
                "Not verifiable: needs exactly one package file with size and SHA-256" if asset.state == "unverifiable" else
                "Not uploaded" if asset.state != "uploaded" else
                "Unsupported format" if asset.supported_extension is None else
                "Filtered out" if asset.supported_extension not in self.enabled else
                "Eligible" if id(asset) in self.matches[root] else ("Excluded by filename rules" if not allowed(BuildRules((),self.rules.exclude),asset.name) else "Not matched by include rules") if id(asset) in self.base[root] else "Duplicate asset")
            if role == Qt.ToolTipRole:
                explanation = {
                    "Eligible": "Can be selected for archiving with the current formats and filename rules.",
                    "Filtered out": "Enable this format to make this uploaded asset eligible.",
                    "Unsupported format": "This file type is not supported for archiving.",
                    "Not uploaded": "The provider has not marked this asset as uploaded; it cannot be selected.",
                    "External link, not archived": "External links are listed for reference and cannot be archived.",
                    "Not verifiable: needs exactly one package file with size and SHA-256": "This package cannot be safely verified and cannot be selected.",
                    "Excluded by filename rules": "An exclude pattern prevents this asset from being selected.",
                    "Not matched by include rules": "This asset does not match any of the include patterns.",
                    "Duplicate asset": "Another occurrence of this asset is already represented; this duplicate cannot be selected.",
                }
                return _safe(status + ": " + explanation[status])
            if role == Qt.ForegroundRole:
                return QBrush(QColor(current_tokens(self._theme_widget)['muted'])) if status != "Eligible" else None
            values = (asset.name, "", "",
                      asset.supported_extension or "-", size_text(asset.size), status)
        else:
            if role != Qt.DisplayRole: return None
            values = (release.name or release.tag_name, release.tag_name, (release.published_at or "-")[:10],
                      "-", "-", browse_panes.count_text(len(self.matches[root]),"matching asset") if self.matches[root] else
                      "No assets" if not release.assets else "No matching assets")
            if release.prerelease: values = (*values[:-1], "Prerelease, " + values[-1])
        return display(values[index.column()])


class ElidedLabel(QLabel):
    """Keep full accessible text while containing long paths and diagnostics."""
    def __init__(self, *args):
        super().__init__(*args)
        self.setTextFormat(Qt.PlainText)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setToolTip(_safe_lines(self.text()))

    def setText(self, text):
        text="" if text is None else str(text)
        super().setText(text)
        self.setToolTip(_safe_lines(text))

    def clear(self): self.setText("")
    def heightForWidth(self,width): return self.sizeHint().height()

    def sizeHint(self): return QSize(100, self.fontMetrics().height() + 2)
    def minimumSizeHint(self): return QSize(0, self.fontMetrics().height() + 2)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text().replace('\n', ' | '), Qt.ElideRight, self.contentsRect().width())
        painter.drawText(self.contentsRect(), self.alignment() | Qt.AlignVCenter, text)


class OptionalLabel(ElidedLabel):
    def __init__(self,*args): super().__init__(*args); self.setVisible(bool(self.text()))
    def setText(self,text): super().setText(text); self.setVisible(bool(text))
    def clear(self): self.setText("")


class MainWindow(QMainWindow):
    def __init__(self, launcher=None, controller=None, *, bridge=None, preferences=None, library=None, auto_scan=None, auto_clock=time.time):
        super().__init__()
        self.setWindowTitle("CoveArchive"); self.resize(1180,720); self.setMinimumSize(820,520)
        self.setWindowIcon(app_icon())
        self.controller = controller if controller is not None else ScanController(launcher, parent=self)
        self.catalog = None; self.show_catalog = False; self.active_id = None
        self.started = 0; self.checked=set(); self.eligible={}; self.rules=BuildRules(); self._rules_repo=None; self.enqueue_pending=False; self._scan_provider='github'
        self.bridge=bridge if bridge is not None else ArchiveBridge(parent=self)
        self.preferences=preferences if preferences is not None else PreferenceStore()
        self.settings, self.preference_problem, _=self.preferences.load()
        self.auto=AutoArchive(auto_scan if auto_scan is not None else ScanController(launcher,parent=self),self.bridge,
            lambda:self.library_panel.library,lambda:self.preferences.load()[0],clock=auto_clock,parent=self)
        self.enqueue_token=0; self.last_view=None; self._close_status=None; self._window_closed=False
        self.history_controller=HistoryController(self); self._history_closing=False; self._history_cancelling=False; self._history_pending=False
        shell=QWidget(); shell_layout=QVBoxLayout(shell); shell_layout.setContentsMargins(0,0,0,0); shell_layout.setSpacing(0)
        self.setCentralWidget(shell)
        self.toolbar=QFrame(); self.toolbar.setObjectName('topToolbar'); shell_layout.addWidget(self.toolbar)
        toolbar=QHBoxLayout(self.toolbar); toolbar.setContentsMargins(8,4,8,4); toolbar.setSpacing(4)
        self.nav=browse_panes.NavTabs(); self.nav.setDocumentMode(True); self.nav.setExpanding(False)
        self.nav.setUsesScrollButtons(False); self.nav.setSizePolicy(QSizePolicy.Fixed,QSizePolicy.Fixed)
        self.nav.setAccessibleName('Browse, Queue and Library tabs')
        for name in ('Browse','Queue','Library'): self.nav.addTab(name)
        toolbar.addWidget(self.nav)
        central = QWidget(); self.tabs=QTabWidget(); shell_layout.addWidget(self.tabs,1); self.tabs.addTab(central,"Browse"); layout = QVBoxLayout(central); central.setObjectName("browsePage"); layout.setSpacing(3); layout.setContentsMargins(8,6,8,6)
        self.tabs.tabBar().hide(); self.nav.currentChanged.connect(self.tabs.setCurrentIndex)
        self.tabs.currentChanged.connect(self.nav.setCurrentIndex)
        url_container=QFrame(); url_container.setObjectName('urlContainer'); toolbar.addWidget(url_container,1)
        row = QHBoxLayout(url_container); row.setContentsMargins(4,0,4,0); row.setSpacing(4)
        self.url = QLineEdit(); self.url.setPlaceholderText("https://github.com/owner/repository/releases, codeberg.org/owner/repository or gitlab.com/group/project")
        self.scan = QPushButton("Scan"); self.cancel = QPushButton("Cancel Scan"); self.cancel.setEnabled(False)
        self.url.setMinimumWidth(40); self.cancel.hide()
        toolbar.addWidget(self.scan); toolbar.addWidget(self.cancel)
        self.provider = QLabel("Unsupported"); self.provider.setTextFormat(Qt.PlainText)
        self.provider.setObjectName("providerBadge"); self.provider.setAlignment(Qt.AlignCenter)
        self.provider.setSizePolicy(QSizePolicy.Fixed,QSizePolicy.Fixed); self.provider.setWordWrap(False)
        self.provider.setAccessibleName("Repository provider"); set_tone(self.provider, "muted"); row.addWidget(self.provider)
        row.addWidget(self.url,1)
        chip=QWidget(); chip_layout=QVBoxLayout(chip); chip_layout.setContentsMargins(0,0,0,0); chip_layout.setSpacing(0)
        self.queue_chip=QPushButton(); self.queue_chip.setAccessibleName('Open Queue'); chip_layout.addWidget(self.queue_chip)
        self.queue_progress=QProgressBar(); self.queue_progress.setRange(0,100); self.queue_progress.setTextVisible(False)
        self.queue_progress.setMaximumHeight(4); chip_layout.addWidget(self.queue_progress)
        self.queue_status=chip; toolbar.addWidget(chip); chip.hide()
        self.queue_chip.clicked.connect(lambda:self.tabs.setCurrentIndex(1))
        self.theme_toggle=QPushButton(); self.settings_button=QPushButton()
        for button in (self.theme_toggle,self.settings_button): button.setProperty('chrome',True); toolbar.addWidget(button)
        self.settings_button.setAccessibleName('Settings'); self.settings_button.setToolTip('Settings (Ctrl+,)')
        self.settings_button.clicked.connect(self._open_settings)
        self.theme_toggle.clicked.connect(lambda:self.set_appearance('light' if self.property('themeName')=='dark' else 'dark'))
        self.url.textChanged.connect(self._provider_changed); self._provider_changed()
        self.prereleases = QCheckBox("Include prereleases")
        self.formats = {}
        for extension in SUPPORTED_EXTENSIONS:
            box = QCheckBox(extension); box.setChecked(extension in DEFAULT_EXTENSIONS); box.setAccessibleName(FORMAT_LABELS[extension])
            if extension not in DEFAULT_EXTENSIONS: box.setToolTip("archived only; never launched or installed")
            self.formats[extension] = box; box.toggled.connect(self._refresh)
        self.include=QLineEdit(); self.exclude=QLineEdit(); self.save_rules=QPushButton("Save rules for this repository")
        for edit in (self.include,self.exclude): edit.setPlaceholderText("*win64*, *windows*  (comma-separated; * ? [ ])"); edit.textChanged.connect(self._rules_changed)
        self.save_rules.setEnabled(False)
        self.save_rules.clicked.connect(self._save_rules)
        self.rule_error=OptionalLabel(self.preference_problem or ""); self.preview=OptionalLabel(); self.held_back=browse_panes.WrappedPath()
        for label in (self.rule_error,self.preview,self.held_back): label.setTextFormat(Qt.PlainText); label.setWordWrap(True)
        self.root=QLineEdit(self.settings["archive_root"] or ""); self.root.setPlaceholderText("Archive root folder"); self.browse=QPushButton("Browse...")
        self.destination=browse_panes.WrappedPath(); self.destination.setTextFormat(Qt.PlainText); self.destination.setWordWrap(True)
        self.root.textChanged.connect(self._destination); self.root.returnPressed.connect(self._destination); self.browse.clicked.connect(self._browse)
        self.select_matching=QPushButton("Select Matching"); self.clear_selection=QPushButton("Clear Selection"); self.archive_selected=QPushButton("Archive Selected (0)"); self.archive_selected.setEnabled(False)
        self.history_button=QPushButton("Archive Git History"); self.history_cancel=QPushButton("Cancel Git History")
        self.history_button.setEnabled(False); self.history_cancel.setEnabled(False)
        for button in (self.history_button,self.history_cancel):
            button.setAccessibleName(button.text())
        description="Archive public commits, branches and tags only; NOT Git LFS file contents or submodule repository contents."
        self.history_button.setToolTip(description); self.history_button.setAccessibleDescription(description)
        self.history_cancel.setToolTip("Stop the running Git history archive"); self.history_cancel.setAccessibleDescription(self.history_cancel.toolTip())
        self.history_status=browse_panes.WrappedPath(); self.history_status.setTextFormat(Qt.PlainText); self.history_status.setWordWrap(True)
        self.history_button.clicked.connect(self._archive_history); self.history_cancel.clicked.connect(self._cancel_history)
        self.history_controller.progress.connect(self._history_progress,Qt.QueuedConnection)
        self.history_controller.finished.connect(self._history_finished,Qt.QueuedConnection)
        self.history_controller.failed.connect(self._history_failed,Qt.QueuedConnection)
        self.select_matching.clicked.connect(lambda:self._select(False)); self.clear_selection.clicked.connect(lambda:self._select(True))
        for widget,name in ((self.include,"Include filename patterns"),(self.exclude,"Exclude filename patterns"),(self.root,"Archive root"),(self.browse,"Browse archive root"),(self.save_rules,"Save rules for this repository"),(self.select_matching,"Select Matching"),(self.clear_selection,"Clear Selection"),(self.archive_selected,"Archive Selected")):
            widget.setAccessibleName(name); widget.setAccessibleDescription(name)
        self.include.returnPressed.connect(self._rules_changed); self.exclude.returnPressed.connect(self._rules_changed)
        self.summary = OptionalLabel(); self.summary.setTextFormat(Qt.PlainText)
        self.tree = QTreeView(); self.tree.setUniformRowHeights(True); self.tree.setWordWrap(False)
        self.tree.setMinimumHeight(140); self.tree.setTextElideMode(Qt.ElideRight)
        overlay = QVBoxLayout(self.tree.viewport()); overlay.setContentsMargins(12,12,12,12)
        self.empty = QLabel("No repository scanned")
        self.empty.setTextFormat(Qt.PlainText); self.empty.setWordWrap(True); self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setAttribute(Qt.WA_TransparentForMouseEvents); set_tone(self.empty, "muted")
        overlay.addWidget(self.empty)
        self._model(None)
        self.detail = OptionalLabel(); self.detail.setTextFormat(Qt.PlainText); self.detail.setWordWrap(True)
        self.detail.setMaximumHeight(96)
        self.status = ElidedLabel("Ready"); self.status.setTextFormat(Qt.PlainText); self.status.setWordWrap(True)
        self.busy = QProgressBar(); self.busy.setRange(0,0); self.busy.setVisible(False)
        self.elapsed = QLabel(); self.elapsed.setTextFormat(Qt.PlainText); self.elapsed.hide()
        for label, tone in ((self.rule_error,"error"),(self.preview,"muted"),(self.held_back,"warning"),
                (self.history_status,"normal"),(self.summary,"muted"),(self.detail,"muted"),(self.destination,"muted")):
            set_tone(label,tone)
        self.clock = QTimer(self); self.clock.setInterval(250); self.clock.timeout.connect(self._tick)
        controls = [self.url,self.scan,self.cancel,self.prereleases,*self.formats.values(),self.include,self.exclude,self.save_rules,self.root,self.browse,self.select_matching,self.clear_selection,self.archive_selected,self.history_button,self.history_cancel,self.tree]
        for widget,name in zip(controls[:4],("GitHub URL","Scan","Cancel Scan","Include prereleases")):
            widget.setAccessibleName(name)
        self.tree.setAccessibleName("Releases and assets")
        self.scan.clicked.connect(self._scan_from_toolbar); self.url.returnPressed.connect(self._scan_from_toolbar)
        self.cancel.clicked.connect(self.controller.cancel); self.prereleases.toggled.connect(self._refresh)
        self.controller.state_changed.connect(self._state); self.controller.terminal.connect(self._terminal)
        QApplication.instance().aboutToQuit.connect(self.controller.shutdown)
        QApplication.instance().aboutToQuit.connect(self.auto.shutdown)
        self.queue_panel=QueuePanel(self.bridge,self); self.tabs.addTab(self.queue_panel,"Queue")
        self.library_store = library if library is not None else LibraryStore()
        self.library_panel = LibraryPanel(self.library_store, self, protected=self._protected_roots,auto=self.auto); self.tabs.addTab(self.library_panel, "Library")
        self.library_panel.changed.connect(self.auto.sync)
        self.tabs.tabBar().setAccessibleName("Browse, Queue and Library tabs"); self.tabs.setAccessibleName("Browse, Queue and Library"); self.tabs.setAccessibleDescription("Switch between build selection, archive queue and repository library")
        for button in (self.scan, self.archive_selected): set_role(button, "primary")
        for button in (self.cancel, self.history_cancel): set_role(button, "danger")
        self._theme = ThemeTracker(self,self.settings.get('appearance'))
        self._theme.changed.connect(self._chrome_icons); self._chrome_icons()
        self._navigation_shortcuts = []
        for index in range(3):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self)
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(lambda tab=index: self.tabs.setCurrentIndex(tab))
            self._navigation_shortcuts.append(shortcut)
            self.tabs.setTabToolTip(index, f"{self.tabs.tabText(index)} (Ctrl+{index + 1})")
        shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        shortcut.setContext(Qt.WindowShortcut)
        shortcut.activated.connect(self._focus_source)
        self._navigation_shortcuts.append(shortcut)
        shortcut=QShortcut(QKeySequence('Ctrl+,'),self); shortcut.activated.connect(self._open_settings)
        self._navigation_shortcuts.append(shortcut)
        self.url.setToolTip("Repository URL (Ctrl+L)")
        self._bridge_slots=((self.bridge.enqueued,self._enqueued),(self.bridge.view_changed,self._queue_view),(self.bridge.shutdown_status,self._shutdown_status))
        for signal,slot in self._bridge_slots: signal.connect(slot,Qt.QueuedConnection)
        self.archive_selected.clicked.connect(self._archive); self.bridge.open()
        window=self.settings["window"]; self.resize(window["width"],window["height"]); self.tabs.setCurrentIndex(window["tab"]=="queue")
        self.url.setFocus()
        self.auto.start()
        self._history_enabled()

        browse_panes.install(self)
        self.tabs.setFocusPolicy(Qt.NoFocus); self.tabs.tabBar().setFocusPolicy(Qt.NoFocus)
        order=[self.nav,self.url,self.scan,self.cancel,self.queue_chip,self.theme_toggle,self.settings_button,self.filter_toggle,*self.formats.values(),self.prereleases,self.include,self.exclude,self.save_rules,self.releases_toggle,self.release_list,self.show_prereleases,self.select_matching,self.clear_selection,self.tree,self.root,self.browse,self.archive_selected,self.history_button,self.history_cancel,self.queue_panel.start,*self.queue_panel.buttons.values(),self.queue_panel.table]
        order += [getattr(self.library_panel,name) for name in ('url','add','bulk','import_button','export_button','search','tree','new','rename_collection','delete','table','move','rename','remove','auto_button')]
        for view in (self.tree,self.release_list,self.queue_panel.table,self.library_panel.tree,self.library_panel.table): view.setTabKeyNavigation(False)
        self.inspector.setFocusPolicy(Qt.NoFocus); self.center_overlay.setFocusPolicy(Qt.NoFocus)
        for previous,following in zip(order,order[1:]+order[:1]): QWidget.setTabOrder(previous,following)
        self._history_enabled()

    def _chrome_icons(self):
        target='light' if self.property('themeName')=='dark' else 'dark'
        self.theme_toggle.setIcon(chrome_icon('sun' if target=='light' else 'moon',current_tokens(self)['text']))
        self.theme_toggle.setAccessibleName('Switch to '+target+' theme'); self.theme_toggle.setToolTip(self.theme_toggle.accessibleName())
        self.settings_button.setIcon(chrome_icon('gear',current_tokens(self)['text']))
        self.nav.setMinimumWidth(self.nav.sizeHint().width())

    def _scan_from_toolbar(self):
        self.tabs.setCurrentIndex(0); self._start()

    def set_appearance(self, name, *, persist=True):
        self._theme.set_override(name)
        if name is None: self.settings.pop('appearance',None)
        else: self.settings['appearance']=name
        if persist:
            problem=self.preferences.save(self.settings)
            if problem: self._status("Appearance applies for this session and won't persist. "+problem,True)

    def _open_settings(self):
        if not hasattr(self,'settings_dialog'):
            self.settings_dialog=SettingsDialog(self); self.settings_dialog.finished.connect(self._settings_closed)
        self.settings_dialog.root.setText(self.root.text()); self.settings_dialog.appearance.setCurrentIndex({None:0,'light':1,'dark':2}[self._theme.override]); self.settings_dialog.feedback.clear()
        self.settings_dialog.open()

    def _settings_closed(self, result):
        self.activateWindow(); self.settings_button.setFocus()
        QTimer.singleShot(0,self.settings_button.setFocus)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'queue_status'): self.queue_status.setVisible(bool(getattr(self,'_queue_count',0)) and self.width()>=1100)
        browse_panes.responsive(self)

    def _provider_changed(self):
        try:
            target=parse_repository_url(self.url.text())
            provider=display_name(target.provider) if isinstance(target,(RepositoryTarget,ReleaseListTarget)) else "Unsupported"
        except (GitHubUrlError, TypeError, ProtocolError): provider="Unsupported"
        self.provider.setText(provider); self.provider.setVisible(bool(self.url.text().strip()))
        set_tone(self.provider,"muted" if provider=="Unsupported" else "normal")

    def _focus_source(self):
        self.tabs.setCurrentIndex(0)
        self.url.setFocus()

    def _history_enabled(self):
        try: validate_root(self.root.text()); valid=True
        except ValueError: valid=False
        active=self._history_pending or self.history_controller.running
        self.history_button.setEnabled(bool(self.catalog) and valid and not active and not self.bridge.shutting_down and not self.bridge.closed and not self._history_closing)
        self.history_cancel.setEnabled(active and not self._history_closing and not self._history_cancelling and getattr(self,'_history_stage',None)!='publishing')
        self.history_cancel.setVisible(active)

    def _archive_history(self):
        self._history_enabled()
        if not self.history_button.isEnabled(): return
        self._history_cancelling=False
        self._history_stage=None
        if self.history_controller.start(self.catalog.repository,str(Path(self.root.text()))):
            self._history_pending=True; set_tone(self.history_status,"normal"); self.history_status.setText("Archiving Git history..."); self._history_enabled()

    def _cancel_history(self):
        self._history_cancelling=True; self.history_controller.cancel()
        self.history_status.setText("Cancelling Git history..."); self._history_enabled()

    def _history_progress(self,stage):
        self._history_stage=stage; self._history_enabled()
        if not self._history_closing and not self._history_cancelling: self.history_status.setText(STAGES.get(stage,"Archiving Git history..."))

    def _history_finished(self,result):
        set_tone(self.history_status,"success"); self.history_status.setText(result_text(result)); self._history_terminal()

    def _history_failed(self,error):
        set_tone(self.history_status,"error")
        text=ERRORS.get(error.code,ERRORS["internal_error"])
        self.history_status.setText(text if text.startswith("Git history") else "Git history: "+text); self._history_terminal()

    def _history_terminal(self):
        if self.history_controller.running:
            QTimer.singleShot(10,self._history_terminal); return
        self._history_pending=False; self._history_cancelling=False; self.history_controller.shutdown(); self._history_enabled()
        if self._history_closing: self.close()

    def _protected_roots(self):
        try: queue_dir = default_queue_path().parent
        except QueueError: queue_dir = None
        return [queue_dir, self.root.text(), self.settings["archive_root"]]

    def _model(self, selection):
        if selection is None: self.empty.setText("No repository scanned")
        self.empty.setVisible(selection is None)
        old = self.tree.model(); model = ReleaseModel(selection,self.tree,checked=self.checked,rules=self.rules); self.tree.setModel(model)
        model.layoutChanged.connect(self._selection_changed)
        if old is not None: old.deleteLater()
        header=self.tree.header(); header.setStretchLastSection(False); header.setMinimumSectionSize(40)
        header.setSectionResizeMode(QHeaderView.Fixed); header.setSectionResizeMode(0,QHeaderView.Stretch)
        fm=self.tree.fontMetrics()
        format_width,size_width=browse_panes.fixed_column_widths(self.tree)
        tag_width=fm.horizontalAdvance("v0.0.0-rc.10")+16
        published_width=fm.horizontalAdvance("0000-00-00")+16
        for column,width in enumerate((0,tag_width,published_width,format_width,size_width,max(210,fm.horizontalAdvance("Prerelease, 1000 matching assets")+16))):
            if column: header.resizeSection(column,width)
        self.tree.selectionModel().currentChanged.connect(self._detail)
        if getattr(self,'_sort_state',None): model.sort(*self._sort_state)
        if selection is None: browse_panes.sync(self)

    def _detail(self, index, previous=QModelIndex()):
        obj = self.tree.model().data(index,Qt.UserRole)
        if obj is None: self.detail.clear(); return
        if hasattr(obj,"size"):
            parent = self.tree.model().parent(index)
            release = self.tree.model().data(parent,Qt.UserRole)
            text = f"{display(obj.name)} | {display(release.tag_name)} | {obj.size} bytes | {display(release.published_at or '-')}"
        else: text = f"{display(obj.name or obj.tag_name)} | {display(obj.tag_name)} | {display(obj.published_at or '-')}"
        self.detail.setText(text)

    def _status(self, text, error=False):
        self.status.setText(text); set_tone(self.status, "error" if error else "success" if text == "Completed" else "normal")
        if hasattr(self,'status_dot'): set_tone(self.status_dot,self.status.property('tone'))
        if error and self.catalog is None and text.startswith(('Invalid URL','Failed','Rate limited','Access denied','Not found','Network error','Timed out','Release listing')):
            browse_panes.center_state(self,'error',text)

    def _clear(self):
        self.catalog=None
        self.preview.clear()
        self.show_catalog=False; self.summary.clear(); self.detail.clear(); self._model(None); self.eligible={}; self._selection_changed()
        browse_panes.center_state(self,'empty')

    def _start(self):
        if self.active_id is not None or self.controller.job is not None: return
        text=self.url.text().strip().lower()
        self._scan_provider=next((p for p,h in (('gitlab','gitlab.com'),('codeberg','codeberg.org')) if text.startswith(('https://'+h+'/',h+'/'))),'github')
        try:
            target = parse_repository_url(self.url.text())
            if isinstance(target,ReleaseTagTarget):
                self._clear(); self._status("Invalid URL: paste the repository or its /releases URL",True); return
            self._scan_provider=target.provider; self.url.setAccessibleName(display_name(target.provider)+' URL')
            self._scan_url=canonical_url(RepositoryTarget(target.owner,target.repo,target.provider,target.host))
            self.controller.start(self._scan_url)
        except (GitHubUrlError,TypeError,ProtocolError):
            self._clear(); self._status(f"Invalid URL: not a supported {display_name(self._scan_provider)} repository/releases URL",True); return

    def _state(self, request_id, state):
        if self._window_closed: return
        if state == "RUNNING":
            if not self._history_pending and not self.history_controller.running: self.history_status.clear()
            try: target_key=parse_repository_url(self.url.text()).repo_key
            except (ValueError,TypeError): target_key=None
            previous=self.catalog.repository.repo_key if self.catalog else getattr(self,'_selection_repo',None)
            if target_key!=previous: self.checked.clear()
            self._selection_repo=target_key
            self.active_id=request_id; self.enqueue_token+=1; self.enqueue_pending=False; self._clear(); self.started=time.monotonic()
        elif request_id != self.active_id: return
        running = state == "RUNNING"
        self.scan.setEnabled(False); self.url.setEnabled(False); self.cancel.setEnabled(running); self.cancel.show()
        self.busy.setVisible(running); self.elapsed.setVisible(running)
        if running: self.clock.start(); self._tick(); self.cancel.setFocus()
        else: self.clock.stop()
        self.empty.hide()
        self._status("Scanning" if running else "Cancelling" if state == "STOPPING" else
                     "Cancelling: stop is taking longer than expected")
        browse_panes.center_state(self,'scanning')

    def _tick(self): self.elapsed.setText(f"{int(time.monotonic()-self.started)} seconds elapsed")

    def _terminal(self, request_id, state, result):
        if self._window_closed: return
        if request_id != self.active_id: return
        self.active_id=None; self.clock.stop(); self.busy.hide(); self.elapsed.hide()
        self.url.setEnabled(True); self.scan.setEnabled(True); self.cancel.setEnabled(False); self.cancel.hide()
        self.url.setFocus()
        if state == "COMPLETED":
            if not self._history_pending and not self.history_controller.running: self.history_status.clear()
            self.catalog=result.catalog; key=self.catalog.repository.repo_key; github=self.catalog.repository.provider=='github'
            self._selection_repo=key
            self.checked.intersection_update((release.id,asset.id) for release in self.catalog.releases for asset in release.assets)
            saved=self.settings["repositories"].get(key) if github else None
            if saved or self._rules_repo not in (None,key) or not github:
                values=saved or dict(include=[],exclude=[],extensions=list(DEFAULT_EXTENSIONS),prereleases=False)
                self.rules=BuildRules(values["include"],values["exclude"])
                for edit,text in ((self.include,", ".join(self.rules.include)),(self.exclude,", ".join(self.rules.exclude))): edit.blockSignals(True); edit.setText(text); edit.blockSignals(False)
                for ext,box in self.formats.items(): box.blockSignals(True); box.setChecked(ext in values["extensions"]); box.blockSignals(False)
                self.prereleases.blockSignals(True); self.prereleases.setChecked(values["prereleases"]); self.prereleases.blockSignals(False)
            self._rules_repo=key; self.show_catalog=True; self._refresh()
            if saved: self.checked.update(self.eligible); self._selection_changed()
            return
        self._clear()
        if state == "CANCELLED": self._status("Cancelled"); return
        if state == "TIMED_OUT": self._status(f"Timed out after {self.controller.deadline_ms/1000:g} seconds; try again",True); return
        if result.detail and "stderr" in result.detail:
            logging.getLogger("covearchive.ui").warning("Scan worker diagnostics: %s", result.detail["stderr"])
        self._status(self._error_text(result),True)

    def _error_text(self, error):
        code=error.code; detail=error.detail or {}; provider=display_name(getattr(self,'_scan_provider','github'))
        if code == "process":
            return ("Failed: the scan process could not start" if error.message == "Worker could not start." else
                    "Failed: the scan process stopped unexpectedly")
        if code == "rate_limited":
            retry=detail.get("retry_after_seconds"); at=detail.get("retry_at")
            return "Rate limited"+(f": retry after {retry} seconds" if retry is not None else
                                    f": retry at {at}" if at is not None else "")
        if code in ("forbidden","unauthorized"): return f"Access denied ({error.http_status})"
        if code == "not_found": return "Not found: repository not found or inaccessible"
        if code == "network": return "Network error: connection failed"
        if code == "timeout": return f"Timed out: {provider} did not respond in time; try again"
        if code == "incomplete_scan": return "Release listing incomplete, retry"
        if code in ("invalid_json","invalid_root","too_many_items","invalid_release","response_too_large","protocol","invalid_content_type"):
            return f"Failed: {provider} returned unexpected data"
        if code == "invalid_url": return f"Invalid URL: not a supported {provider} repository/releases URL"
        return f"Failed ({code})"

    def _refresh(self):
        if not self.show_catalog: return
        enabled=tuple(ext for ext,box in self.formats.items() if box.isChecked())
        selection=select_releases(self.catalog,include_prereleases=self.prereleases.isChecked(),enabled_extensions=enabled)
        self._model(selection); self.detail.clear()
        count=sum(len(ids) for ids in self.tree.model().matches)
        self.eligible={(row.release.id,m.asset.id):(row.release,m.asset) for i,row in enumerate(selection.releases) for m in row.matches if id(m.asset) in self.tree.model().matches[i]}
        self.preview.setText(f"Rules match {count} of {sum(len(r.matches) for r in selection.releases)} eligible assets"); self._selection_changed()
        prefix='' if self.catalog.repository.provider=='github' else display_name(self.catalog.repository.provider)+' | '
        self.summary.setText(prefix+f"{display(self.catalog.repository.owner)}/{display(self.catalog.repository.repo)} | "
                             f"{browse_panes.count_text(len(selection.releases),'release')} | {browse_panes.count_text(count,'matching asset')}")
        hidden=sum(x.reason=="prerelease" for x in selection.excluded)
        text = ("No formats selected" if not enabled else "No releases found" if not self.catalog.releases else
                browse_panes.count_text(hidden,"prerelease")+" hidden" if not selection.releases and hidden else
                "No matching assets" if not count else "Completed")
        self.empty.setText(text)
        self.empty.setVisible(self.tree.model().rowCount()==0 and self.active_id is None)
        self._status(text)
        browse_panes.sync(self)
        browse_panes.center_state(self,'result_empty' if not count else 'results',text)

    def _rules_changed(self):
        try: rules=BuildRules(parse_patterns(self.include.text()),parse_patterns(self.exclude.text()))
        except BuildFilterError as error: self.rule_error.setText(str(error)); return
        self.rules=rules; self.rule_error.clear(); self._refresh()

    def _select(self,clear):
        if clear: self.checked.clear()
        else: self.checked.update(self.eligible)
        self._refresh()

    def _selection_changed(self):
        github=bool(self.catalog and self.catalog.repository.provider=='github')
        self.save_rules.setEnabled(bool(self.show_catalog and github))
        self.save_rules.setToolTip('Saving rules is available for GitHub repositories only.' if self.catalog and not github else '')
        text='Save rules (GitHub only)' if self.catalog and not github else 'Save rules for this repository'
        self.save_rules.setText(text); self.save_rules.setAccessibleName(text)
        self.save_rules.setAccessibleDescription(self.save_rules.toolTip() or 'Save rules for this repository')
        count=len(self.checked & self.eligible.keys()); hidden=len(self.checked-self.eligible.keys())
        self.archive_selected.setText(f"Archive Selected ({count})"); self.archive_selected.setEnabled(bool(count) and not self.enqueue_pending and not self.bridge.shutting_down and not self.bridge.closed)
        self.held_back.setText(f"{hidden} checked item(s) hidden by filters are not included" if hidden else ""); self._destination()
        browse_panes.selection(self)

    def _save_rules(self):
        if not self.show_catalog or self.catalog is None:
            self.rule_error.setText("Scan a repository before saving rules")
            return
        if self.catalog.repository.provider!='github':
            self.rule_error.setText('Saving rules is available for GitHub repositories only.'); return
        try:
            self.rules=BuildRules(parse_patterns(self.include.text()),parse_patterns(self.exclude.text()))
        except BuildFilterError as error:
            self.rule_error.setText(str(error))
            return
        self.settings["repositories"][self.catalog.repository.repo_key]=dict(include=list(self.rules.include),exclude=list(self.rules.exclude),extensions=[e for e,b in self.formats.items() if b.isChecked()],prereleases=self.prereleases.isChecked())
        self.rule_error.setText(self.preferences.save(self.settings) or "Rules saved")

    def _browse(self):
        path=QFileDialog.getExistingDirectory(self,"Choose archive root",self.root.text())
        if path: self.root.setText(path)

    def _destination(self):
        if hasattr(self,"history_button"): self._history_enabled()
        try:
            validate_root(self.root.text()); root=Path(self.root.text())
            if not self.catalog or not self.tree.model().rows: self.destination.setText(str(root)); return
            project=root.joinpath(*project_components(self.catalog.repository))
            releases={r.id:r for key,(r,asset) in self.eligible.items() if key in self.checked}
            path=str(project/release_folder_name(next(iter(releases.values())).tag_name,next(iter(releases)))) if len(releases)==1 else f"{len(releases)} release folders under {project}{os.sep}" if releases else str(project/"<release folder>")
            self.destination.setText(path+" (existing folder spelling is reused if present)")
        except ValueError: self.destination.setText("Choose an absolute archive root without '..' or device paths")

    def _archive(self):
        if self.enqueue_pending or self.bridge.shutting_down or self._window_closed: return
        items=tuple(EnqueueItem(*pair) for key,pair in self.eligible.items() if key in self.checked)
        if not items or not self.show_catalog: return
        try: validate_root(self.root.text()); root=str(Path(self.root.text()))
        except ValueError: self._status("Choose an absolute archive root without '..' or device paths",True); self.root.setFocus(); self.root.selectAll(); return
        self.enqueue_token+=1; self.enqueue_pending=True; self._selection_changed()
        request=EnqueueRequest(self.enqueue_token,self.catalog.repository,root,items)
        if not self.bridge.enqueue(request): self.enqueue_pending=False; self._selection_changed(); self._status("Queue unavailable; selection was not submitted",True); return
        self.settings["archive_root"]=root
        if self.preferences.writable: self.rule_error.setText(self.preferences.save(self.settings) or "")

    def _enqueued(self,result):
        if self._window_closed or self.bridge.shutting_down or result.token!=self.enqueue_token: return
        self.enqueue_pending=False; self._selection_changed()
        text=f"Created {result.created}; re-queued {result.requeued}; already queued {len(result.existing)}"
        if result.existing: text+=" ("+", ".join(state_text(state) for _,state in result.existing)+"); use Resume/Retry for stopped jobs"
        if result.started: text+="; queue started"
        if result.deferred_pending: text+=f"; {result.deferred_pending} earlier queued job(s) are also waiting - press Start Queue"
        if result.startup_error: text+="; automatic start failed: "+_error(result.startup_error)+". Press Start Queue."
        self._status("Could not enqueue: "+result.error if result.error else text+"; see the Queue tab",bool(result.error or result.startup_error))

    def _queue_view(self,view):
        if self._window_closed or (self.last_view and view.seq<self.last_view.seq): return
        self.last_view=view
        running=tuple(j for j in view.jobs if j.state.value=='RUNNING')
        waiting=sum(j.state.value in ('QUEUED','RETRY_WAIT') for j in view.jobs)
        self._queue_count=len(running)+waiting
        self.nav.setTabText(1,'Queue'+(f' ({self._queue_count})' if self._queue_count else ''))
        self.nav.setMinimumWidth(self.nav.sizeHint().width())
        speed=sum(j.speed_bps or 0 for j in running if j.owned)
        self.queue_chip.setText(f'{len(running)} running · {size_text(speed)}/s')
        self.queue_chip.setToolTip(f'{len(running)} running, {waiting} waiting')
        known=view.bytes_done is not None and view.bytes_total is not None and view.bytes_total>0
        self.queue_progress.setVisible(known)
        if known: self.queue_progress.setValue(min(100,max(0,int(100*view.bytes_done/view.bytes_total))))
        self.queue_status.setVisible(bool(self._queue_count) and self.width()>=1100)
        self._history_enabled()
        browse_panes.status_tab(self)

    def _confirm_close(self):
        dialog=QMessageBox(self); dialog.setWindowTitle("Close CoveArchive?")
        dialog.setText("Running downloads will be paused and restart from the beginning on resume. Queued jobs stay queued. The window closes automatically when done.")
        pause=dialog.addButton("Pause and Close",QMessageBox.AcceptRole); keep=dialog.addButton("Keep Open",QMessageBox.RejectRole); dialog.setDefaultButton(keep); dialog.exec()
        return "Pause and Close" if dialog.clickedButton() is pause else "Keep Open"

    def _shutdown_status(self,status):
        if self._window_closed: return
        self._history_enabled()
        self._close_status=status; count=len(status.unresolved)
        text=(f"Still active: {count} job(s) remain; the window will close when they finish" if status.phase=="timeout_waiting" else f"Stopping: waiting for {count} job(s) (catalog discovery and final file operations cannot be interrupted)")
        if status.phase=="timeout_waiting" and not count: text="Finishing up...; the window will close automatically when done"
        if not status.intent_saved: text+="; Warning: stop intent not persisted; incomplete jobs may be queued on restart"
        self._status(text); self.queue_panel._message(text); set_tone(self.queue_panel.message,"warning" if not status.intent_saved else "normal"); self.archive_selected.setEnabled(False); self.queue_panel.start.setEnabled(False)
        if status.phase=="done":
            self.settings["window"]=dict(width=self.width(),height=self.height(),tab="queue" if self.tabs.currentIndex() == 1 else "browse")
            if self.preferences.writable: self.preferences.save(self.settings)
            self.close()

    def closeEvent(self,event):
        if self._history_pending or self.history_controller.running:
            self._history_closing=True; self.history_controller.cancel()
            self.history_status.setText("Stopping Git history before closing...")
            self._history_enabled(); event.ignore(); return
        self.history_controller.shutdown()
        if not self.controller.shutdown(): event.ignore(); return
        if self.bridge.closed:
            if not self.auto.shutdown(): event.ignore(); return
            if not self._window_closed:
                for timer in self.findChildren(QTimer): timer.stop()
                for shortcut in self.findChildren(QShortcut): shortcut.setEnabled(False)
                self.nav.currentChanged.disconnect(); self.tabs.currentChanged.disconnect(); self.queue_chip.clicked.disconnect()
                self.queue_panel.disconnect_bridge()
                for signal,slot in self._bridge_slots: signal.disconnect(slot)
                self.controller.state_changed.disconnect(self._state); self.controller.terminal.disconnect(self._terminal)
                QApplication.instance().aboutToQuit.disconnect(self.controller.shutdown); self.clock.stop()
                QApplication.instance().aboutToQuit.disconnect(self.auto.shutdown)
                self._theme.disconnect()
                self._window_closed=True
            event.accept(); return
        event.ignore()
        if self.bridge.shutting_down:
            if self._close_status: self._shutdown_status(self._close_status)
            return
        active=self.bridge.run_active or (self.last_view and any(j.owned and j.state.value=="RUNNING" for j in self.last_view.jobs))
        if active and self._confirm_close()!="Pause and Close":
            self._history_closing=False; self._history_enabled(); return
        if not self.auto.shutdown(): return
        self.archive_selected.setEnabled(False); self.queue_panel.start.setEnabled(False); self.bridge.begin_shutdown(True)


def main(argv=None, launcher=None):
    app=QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    app.setWindowIcon(app_icon())
    window=MainWindow(launcher); window.show()
    return app.exec()
