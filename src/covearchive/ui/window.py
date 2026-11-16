"""Native, read-only release scanner with local filtering and lazy tree rows."""

import logging
import os
import sys
import time
import unicodedata

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QLabel, QLineEdit,
    QHeaderView, QMainWindow, QProgressBar, QPushButton, QTreeView, QVBoxLayout, QWidget, QFileDialog, QTabWidget, QMessageBox)

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
from covearchive.github.urls import GitHubUrlError, ReleaseTagTarget, RepositoryTarget
from covearchive.providers import parse_repository_url, canonical_url, display_name
from covearchive.scan_worker import ProtocolError, ScanFailure
from covearchive.ui.scan_process import ScanController
from covearchive.ui.auto_archive import AutoArchive


from covearchive.ui.queue_panel import display, size_text, state_text, QueuePanel, _error


class ReleaseModel(QAbstractItemModel):
    """Indexes reference immutable backend objects; children are built on demand."""
    headers = ("Name", "Tag", "Published", "Format", "Size", "Status")

    def __init__(self, selection=None, parent=None, *, checked=None, rules=BuildRules()):
        super().__init__(parent)
        self.rows = () if selection is None else selection.releases
        self.enabled = () if selection is None else selection.enabled_extensions
        self.checked = checked if checked is not None else set(); self.rules=rules
        self.base = tuple(frozenset(id(m.asset) for m in row.matches) for row in self.rows)
        self.matches = tuple(frozenset(id(m.asset) for m in row.matches if allowed(rules,m.asset.name)) for row in self.rows)

    def _keys(self,index):
        if not index.isValid() or index.column()!=0: return set()
        root=index.internalId()-1 if index.internalId() else index.row(); release=self.rows[root].release
        assets=(release.assets[index.row()],) if index.internalId() else release.assets
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
        asset = release.assets[index.row()] if index.internalId() else None
        obj = asset or release
        if role == Qt.UserRole: return obj
        if role == Qt.CheckStateRole and index.column()==0:
            keys=self._keys(index); count=len(keys & self.checked)
            return Qt.Checked if keys and count==len(keys) else Qt.PartiallyChecked if count else Qt.Unchecked
        if role != Qt.DisplayRole: return None
        if asset:
            status = ("External link, not archived" if asset.state == "external_link" else
                "Not verifiable: needs exactly one package file with size and SHA-256" if asset.state == "unverifiable" else
                "Not uploaded" if asset.state != "uploaded" else
                "Unsupported format" if asset.supported_extension is None else
                "Filtered out" if asset.supported_extension not in self.enabled else
                "Eligible" if id(asset) in self.matches[root] else ("Excluded by filename rules" if not allowed(BuildRules((),self.rules.exclude),asset.name) else "Not matched by include rules") if id(asset) in self.base[root] else "Duplicate asset")
            values = (asset.name, "", "",
                      asset.supported_extension or "-", size_text(asset.size), status)
        else:
            values = (release.name or release.tag_name, release.tag_name, (release.published_at or "-")[:10],
                      "-", "-", f"{len(self.matches[root])} matching assets" if self.matches[root] else
                      "No assets" if not release.assets else "No matching assets")
            if release.prerelease: values = (*values[:-1], "Prerelease, " + values[-1])
        return display(values[index.column()])


class OptionalLabel(QLabel):
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
        central = QWidget(); self.tabs=QTabWidget(); self.setCentralWidget(self.tabs); self.tabs.addTab(central,"Browse"); layout = QVBoxLayout(central); layout.setSpacing(4); layout.setContentsMargins(8,8,8,8)
        layout.addWidget(QLabel("CoveArchive - part of the Cove Nexus family"))
        row = QHBoxLayout(); layout.addLayout(row)
        self.url = QLineEdit(); self.url.setPlaceholderText("https://github.com/owner/repository/releases, codeberg.org/owner/repository or gitlab.com/group/project")
        self.scan = QPushButton("Scan"); self.cancel = QPushButton("Cancel Scan"); self.cancel.setEnabled(False)
        row.addWidget(self.url,1); row.addWidget(self.scan); row.addWidget(self.cancel)
        filters = QHBoxLayout(); layout.addLayout(filters)
        self.prereleases = QCheckBox("Include prereleases"); filters.addWidget(self.prereleases)
        self.formats = {}
        for extension in SUPPORTED_EXTENSIONS:
            box = QCheckBox(extension); box.setChecked(extension in DEFAULT_EXTENSIONS); box.setAccessibleName(FORMAT_LABELS[extension])
            if extension not in DEFAULT_EXTENSIONS: box.setToolTip("archived only; never launched or installed")
            self.formats[extension] = box; filters.addWidget(box); box.toggled.connect(self._refresh)
        filters.addStretch()
        rules_row=QHBoxLayout(); layout.addLayout(rules_row); self.include=QLineEdit(); self.exclude=QLineEdit(); self.save_rules=QPushButton("Save rules for this repository")
        for name,edit in (("Include",self.include),("Exclude",self.exclude)): rules_row.addWidget(QLabel(name)); rules_row.addWidget(edit); edit.setPlaceholderText("*win64*, *windows*  (comma-separated; * ? [ ])"); edit.textChanged.connect(self._rules_changed)
        self.save_rules.setEnabled(False)
        rules_row.addWidget(self.save_rules); self.save_rules.clicked.connect(self._save_rules)
        self.rule_error=OptionalLabel(self.preference_problem or ""); self.preview=OptionalLabel(); self.held_back=OptionalLabel()
        for label in (self.rule_error,self.preview,self.held_back): label.setTextFormat(Qt.PlainText); label.setWordWrap(True); layout.addWidget(label)
        destination=QHBoxLayout(); layout.addLayout(destination); self.root=QLineEdit(self.settings["archive_root"] or ""); self.root.setPlaceholderText("Archive root folder"); self.browse=QPushButton("Browse..."); destination.addWidget(self.root,1); destination.addWidget(self.browse)
        self.destination=QLabel(); self.destination.setTextFormat(Qt.PlainText); self.destination.setWordWrap(True); layout.addWidget(self.destination)
        self.root.textChanged.connect(self._destination); self.root.returnPressed.connect(self._destination); self.browse.clicked.connect(self._browse)
        selection_row=QHBoxLayout(); layout.addLayout(selection_row); self.select_matching=QPushButton("Select Matching"); self.clear_selection=QPushButton("Clear Selection"); self.archive_selected=QPushButton("Archive Selected (0)"); self.archive_selected.setEnabled(False)
        for button in (self.select_matching,self.clear_selection,self.archive_selected): selection_row.addWidget(button)
        self.select_matching.clicked.connect(lambda:self._select(False)); self.clear_selection.clicked.connect(lambda:self._select(True))
        for widget,name in ((self.include,"Include filename patterns"),(self.exclude,"Exclude filename patterns"),(self.root,"Archive root"),(self.browse,"Browse archive root"),(self.save_rules,"Save rules for this repository"),(self.select_matching,"Select Matching"),(self.clear_selection,"Clear Selection"),(self.archive_selected,"Archive Selected")):
            widget.setAccessibleName(name); widget.setAccessibleDescription(name)
        self.include.returnPressed.connect(self._rules_changed); self.exclude.returnPressed.connect(self._rules_changed)
        self.summary = QLabel(); self.summary.setTextFormat(Qt.PlainText); layout.addWidget(self.summary)
        self.tree = QTreeView(); self.tree.setUniformRowHeights(True); self.tree.setWordWrap(False)
        self.tree.setMinimumHeight(140); self.tree.setTextElideMode(Qt.ElideRight); layout.addWidget(self.tree,1)
        self._model(None)
        self.detail = OptionalLabel(); self.detail.setTextFormat(Qt.PlainText); self.detail.setWordWrap(True)
        self.detail.setMaximumHeight(96); layout.addWidget(self.detail)
        self.status = QLabel("Ready"); self.status.setTextFormat(Qt.PlainText); self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.busy = QProgressBar(); self.busy.setRange(0,0); self.busy.setVisible(False); layout.addWidget(self.busy)
        self.elapsed = QLabel(); self.elapsed.setTextFormat(Qt.PlainText); self.elapsed.hide(); layout.addWidget(self.elapsed)
        self.clock = QTimer(self); self.clock.setInterval(250); self.clock.timeout.connect(self._tick)
        controls = [self.url,self.scan,self.cancel,self.prereleases,*self.formats.values(),self.include,self.exclude,self.save_rules,self.root,self.browse,self.select_matching,self.clear_selection,self.archive_selected,self.tree]
        for widget,name in zip(controls[:4],("GitHub URL","Scan","Cancel Scan","Include prereleases")):
            widget.setAccessibleName(name)
        self.tree.setAccessibleName("Releases and assets")
        for previous,following in zip(controls,controls[1:]): QWidget.setTabOrder(previous,following)
        self.scan.clicked.connect(self._start); self.url.returnPressed.connect(self._start)
        self.cancel.clicked.connect(self.controller.cancel); self.prereleases.toggled.connect(self._refresh)
        self.controller.state_changed.connect(self._state); self.controller.terminal.connect(self._terminal)
        QApplication.instance().aboutToQuit.connect(self.controller.shutdown)
        QApplication.instance().aboutToQuit.connect(self.auto.shutdown)
        self.setStyleSheet("""
            QWidget { background:#182125; color:#EDF4F4; font-size:13px; }
            QLineEdit,QTreeView { background:#10191D; border:1px solid #53696D; padding:5px; }
            QPushButton { padding:8px 15px; border:1px solid #50E6CF; }
            QPushButton:disabled { color:#8B9B9E; border-color:#53696D; }
            QLineEdit:focus,QPushButton:focus,QCheckBox:focus,QTreeView:focus { border:2px solid #50E6CF; }
            QPushButton:focus { background:#285D59; }
            QTreeView::item:selected { background:#285D59; }
            QProgressBar::chunk { background:#50E6CF; }
        """)
        self.queue_panel=QueuePanel(self.bridge,self); self.tabs.addTab(self.queue_panel,"Queue")
        self.library_store = library if library is not None else LibraryStore()
        self.library_panel = LibraryPanel(self.library_store, self, protected=self._protected_roots,auto=self.auto); self.tabs.addTab(self.library_panel, "Library")
        self.library_panel.changed.connect(self.auto.sync)
        self.tabs.tabBar().setAccessibleName("Browse, Queue and Library tabs"); self.tabs.setAccessibleName("Browse, Queue and Library"); self.tabs.setAccessibleDescription("Switch between build selection, archive queue and repository library")
        for previous,following in zip([self.tree,self.tabs,self.queue_panel.start,*self.queue_panel.buttons.values()],[self.tabs,self.queue_panel.start,*self.queue_panel.buttons.values(),self.queue_panel.table]): QWidget.setTabOrder(previous,following)
        self._bridge_slots=((self.bridge.enqueued,self._enqueued),(self.bridge.view_changed,self._queue_view),(self.bridge.shutdown_status,self._shutdown_status))
        for signal,slot in self._bridge_slots: signal.connect(slot,Qt.QueuedConnection)
        self.archive_selected.clicked.connect(self._archive); self.bridge.open()
        window=self.settings["window"]; self.resize(window["width"],window["height"]); self.tabs.setCurrentIndex(window["tab"]=="queue")
        self.url.setFocus()
        self.auto.start()

    def _protected_roots(self):
        try: queue_dir = default_queue_path().parent
        except QueueError: queue_dir = None
        return [queue_dir, self.root.text(), self.settings["archive_root"]]

    def _model(self, selection):
        old = self.tree.model(); model = ReleaseModel(selection,self.tree,checked=self.checked,rules=self.rules); self.tree.setModel(model)
        model.layoutChanged.connect(self._selection_changed)
        if old is not None: old.deleteLater()
        header=self.tree.header(); header.setStretchLastSection(False); header.setMinimumSectionSize(40)
        header.setSectionResizeMode(QHeaderView.Fixed); header.setSectionResizeMode(0,QHeaderView.Stretch)
        fm=self.tree.fontMetrics()
        format_width=max(fm.horizontalAdvance(ext) for ext in SUPPORTED_EXTENSIONS)+16
        tag_width=fm.horizontalAdvance("v0.0.0-rc.10")+16
        published_width=fm.horizontalAdvance("0000-00-00")+16
        size_width=max(fm.horizontalAdvance(text) for text in ("1023.9 KiB","1023.9 MiB","1023.9 GiB"))+16
        for column,width in enumerate((0,tag_width,published_width,format_width,size_width,max(210,fm.horizontalAdvance("Prerelease, 1000 matching assets")+16))):
            if column: header.resizeSection(column,width)
        self.tree.selectionModel().currentChanged.connect(self._detail)

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
        self.status.setText(text); self.status.setStyleSheet("color:"+("#FFB0A6" if error else "#50E6CF"))

    def _clear(self):
        self.catalog=None
        self.preview.clear()
        self.show_catalog=False; self.summary.clear(); self.detail.clear(); self._model(None); self.eligible={}; self._selection_changed()

    def _start(self):
        if self.active_id is not None or self.controller.job is not None: return
        text=self.url.text().strip().lower()
        self._scan_provider=next((p for p,h in (('gitlab','gitlab.com'),('codeberg','codeberg.org')) if text.startswith(('https://'+h+'/',h+'/'))),'github')
        try:
            target = parse_repository_url(self.url.text())
            if isinstance(target,ReleaseTagTarget):
                self._clear(); self._status("Invalid URL: paste the repository or its /releases URL",True); return
            self._scan_provider=target.provider; self.url.setAccessibleName(display_name(target.provider)+' URL')
            self.controller.start(canonical_url(RepositoryTarget(target.owner,target.repo,target.provider,target.host)))
        except (GitHubUrlError,TypeError,ProtocolError):
            self._clear(); self._status(f"Invalid URL: not a supported {display_name(self._scan_provider)} repository/releases URL",True); return

    def _state(self, request_id, state):
        if self._window_closed: return
        if state == "RUNNING":
            self.active_id=request_id; self.enqueue_token+=1; self.enqueue_pending=False; self.checked.clear(); self._clear(); self.started=time.monotonic()
        elif request_id != self.active_id: return
        running = state == "RUNNING"
        self.scan.setEnabled(False); self.url.setEnabled(False); self.cancel.setEnabled(running)
        self.busy.setVisible(running); self.elapsed.setVisible(running)
        if running: self.clock.start(); self._tick(); self.cancel.setFocus()
        else: self.clock.stop()
        self._status("Scanning" if running else "Cancelling" if state == "STOPPING" else
                     "Cancelling: stop is taking longer than expected")

    def _tick(self): self.elapsed.setText(f"{int(time.monotonic()-self.started)} seconds elapsed")

    def _terminal(self, request_id, state, result):
        if self._window_closed: return
        if request_id != self.active_id: return
        self.active_id=None; self.clock.stop(); self.busy.hide(); self.elapsed.hide()
        self.url.setEnabled(True); self.scan.setEnabled(True); self.cancel.setEnabled(False)
        self.url.setFocus()
        if state == "COMPLETED":
            self.catalog=result.catalog; key=self.catalog.repository.repo_key; github=self.catalog.repository.provider=='github'
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
                             f"{len(selection.releases)} releases | {count} matching assets")
        hidden=sum(x.reason=="prerelease" for x in selection.excluded)
        text = ("No formats selected" if not enabled else "No releases found" if not self.catalog.releases else
                f"{hidden} prereleases hidden" if not selection.releases and hidden else
                "No matching assets" if not count else "Completed")
        self._status(text)

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
        except ValueError: self._status("Choose an absolute archive root without '..' or device paths",True); return
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

    def _confirm_close(self):
        dialog=QMessageBox(self); dialog.setWindowTitle("Close CoveArchive?")
        dialog.setText("Running downloads will be paused and restart from the beginning on resume. Queued jobs stay queued. The window closes automatically when done.")
        pause=dialog.addButton("Pause and Close",QMessageBox.AcceptRole); keep=dialog.addButton("Keep Open",QMessageBox.RejectRole); dialog.setDefaultButton(keep); dialog.exec()
        return "Pause and Close" if dialog.clickedButton() is pause else "Keep Open"

    def _shutdown_status(self,status):
        if self._window_closed: return
        self._close_status=status; count=len(status.unresolved)
        text=(f"Still active: {count} job(s) remain; the window will close when they finish" if status.phase=="timeout_waiting" else f"Stopping: waiting for {count} job(s) (catalog discovery and final file operations cannot be interrupted)")
        if status.phase=="timeout_waiting" and not count: text="Finishing up...; the window will close automatically when done"
        if not status.intent_saved: text+="; Warning: stop intent not persisted; incomplete jobs may be queued on restart"
        self._status(text); self.queue_panel.message.setText(text); self.archive_selected.setEnabled(False); self.queue_panel.start.setEnabled(False)
        if status.phase=="done":
            self.settings["window"]=dict(width=self.width(),height=self.height(),tab="queue" if self.tabs.currentIndex() == 1 else "browse")
            if self.preferences.writable: self.preferences.save(self.settings)
            self.close()

    def closeEvent(self,event):
        if not self.controller.shutdown(): event.ignore(); return
        if self.bridge.closed:
            if not self.auto.shutdown(): event.ignore(); return
            if not self._window_closed:
                for signal,slot in self._bridge_slots: signal.disconnect(slot)
                self.controller.state_changed.disconnect(self._state); self.controller.terminal.disconnect(self._terminal)
                QApplication.instance().aboutToQuit.disconnect(self.controller.shutdown); self.clock.stop()
                QApplication.instance().aboutToQuit.disconnect(self.auto.shutdown)
                self._window_closed=True
            event.accept(); return
        event.ignore()
        if self.bridge.shutting_down:
            if self._close_status: self._shutdown_status(self._close_status)
            return
        active=self.bridge.run_active or (self.last_view and any(j.owned and j.state.value=="RUNNING" for j in self.last_view.jobs))
        if active and self._confirm_close()!="Pause and Close": return
        if not self.auto.shutdown(): return
        self.archive_selected.setEnabled(False); self.queue_panel.start.setEnabled(False); self.bridge.begin_shutdown(True)


def main(argv=None, launcher=None):
    app=QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    app.setWindowIcon(app_icon())
    window=MainWindow(launcher); window.show()
    return app.exec()
