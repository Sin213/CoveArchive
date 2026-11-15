"""Metadata-only repository organization; no scan or queue dependencies."""
import os
from concurrent.futures import ThreadPoolExecutor
from PySide6 import QtWidgets as W
from PySide6.QtCore import QAbstractTableModel, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from covearchive.library import model as m, transfer as t
from covearchive.ui.library_store import atomic_write
from covearchive.auto_archive import NEEDS_RULES


def reason(code):
    if code in {"empty", "too_long", "control_char", "scheme", "port", "host", "path", "owner", "repo", "unsupported"}: code = "url"
    return {"release_tag": "Use a repository URL, not a release tag", "userinfo": "Credentials are not allowed",
        "limit": "Library limit reached", "bulk_size": "Paste exceeds 1 MiB", "bulk_limit": "Too many lines",
        "newer_version": "Newer format version", "name": "Invalid name", "collection_duplicate": "Name already exists",
        "collection_depth": "Too many collection levels", "size": "File exceeds 8 MiB", "json": "Invalid JSON",
        "file": "Could not read file", "file_type": "Choose a regular file", "schema": "Invalid file structure",
        "provider": "Unsupported provider", "url": "Invalid repository URL", "entry": "Invalid repository entry",
        "collection_path": "Invalid collection path", "folder": "Invalid descriptive folder text", "version": "Unsupported format version",
        "duplicate_key": "Duplicate JSON fields are not allowed", "nonfinite": "Non-finite JSON numbers are not allowed"}.get(code, "Invalid or unsupported input")


class RepositoryModel(QAbstractTableModel):
    def rowCount(self, parent=None): return 0 if parent is not None and parent.isValid() else len(self.rows)
    def columnCount(self, parent=None): return 6
    def data(self, index, role=Qt.DisplayRole):
        if index.isValid() and index.column()>=4 and role in (Qt.DisplayRole,Qt.ToolTipRole):
            repo=self.rows[index.row()]; auto=self.parent().auto
            text=(auto.summary(repo) if auto else 'Off') if index.column()==4 else (auto.schedule_text(repo) if auto else 'Last - / Next -')
            status=auto.status(repo.ref) if auto else None
            if role==Qt.ToolTipRole and index.column()==4 and status and status.message: text+='\n'+status.message
            return text
        if index.isValid() and role == Qt.DisplayRole:
            return self.labels[index.row()][index.column()]
        if index.isValid() and index.column() == 1 and role == Qt.ToolTipRole: return self.rows[index.row()].url
    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole: return ("Name", "Repository", "Collection", "Status", "Auto-Archive", "Last / next check")[section]
    def set_rows(self, library, rows):
        self.beginResetModel(); self.rows = rows
        paths = library.collection_paths
        self.labels = [(r.display_name, r.label, paths.get(r.collection, "Unsorted"),
            "Active" if r.active else "Inactive: " + r.provider) for r in rows]
        self.endResetModel()


def _prepare_import(path, snapshot):
    plan = t.build_plan(t.read_import_file(path), snapshot)
    return plan, plan.apply(snapshot) if plan.valid else None


class LibraryPanel(W.QWidget):
    changed=Signal()
    def __init__(self, store, parent=None, protected=lambda: (), auto=None):
        super().__init__(parent)
        self.protected = protected; self.auto=auto
        self.store = store; self.library, _, _ = store.load(); self.scope = None
        self.busy = self._closed = False; self._worker = ThreadPoolExecutor(max_workers=1)
        self._poll = QTimer(self); self._poll.setInterval(25); self._poll.timeout.connect(self._finish_import)
        self.destroyed.connect(lambda *_, worker=self._worker: worker.shutdown(wait=False, cancel_futures=True))
        layout = W.QVBoxLayout(self); top = W.QHBoxLayout(); layout.addLayout(top)
        self.url = W.QLineEdit(); self.url.setPlaceholderText("GitHub repository URL"); top.addWidget(self.url, 1)
        self.mutating = []
        def button(text, callback, row, mutate=True):
            value = W.QPushButton(text); value.clicked.connect(callback); row.addWidget(value)
            if mutate: self.mutating.append(value)
            return value
        self.add = button("Add", self._add, top)
        self.bulk = button("Bulk Add...", self._bulk, top)
        self.import_button = button("Import...", self._import, top)
        self.export_button = button("Export...", self._export, top, False)
        self.search = W.QLineEdit(); self.search.setClearButtonEnabled(True); self.search.setPlaceholderText("Search repositories"); layout.addWidget(self.search)
        body = W.QHBoxLayout(); layout.addLayout(body, 1)
        left, right = W.QVBoxLayout(), W.QVBoxLayout(); body.addLayout(left, 1); body.addLayout(right, 2)
        self.tree = W.QTreeWidget(); self.tree.setHeaderHidden(True); left.addWidget(self.tree)
        folders = W.QHBoxLayout(); left.addLayout(folders)
        self.new = button("New", self._new, folders); self.rename_collection = button("Rename", self._rename_collection, folders)
        self.delete = button("Delete", self._delete, folders)
        self.table = W.QTableView(); self.model = RepositoryModel(self); self.model.rows = (); self.model.labels = []
        self.table.setModel(self.model); self.table.setSelectionBehavior(W.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(W.QAbstractItemView.ExtendedSelection); self.table.verticalHeader().hide()
        self.table.verticalHeader().setSectionResizeMode(W.QHeaderView.Fixed); self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.horizontalHeader().setSectionResizeMode(W.QHeaderView.Stretch); right.addWidget(self.table)
        self.table.horizontalHeader().setMinimumSectionSize(84)
        for column in (3,4,5): self.table.horizontalHeader().setSectionResizeMode(column,W.QHeaderView.ResizeToContents)
        actions = W.QHBoxLayout(); right.addLayout(actions)
        self.move = button("Move to...", self._move, actions); self.rename = button("Rename...", self._rename, actions)
        self.remove = button("Remove from list...", self._remove, actions)
        self.auto_button=button('Auto-Archive...',self._auto_archive,actions)
        self.auto_detail=W.QLabel(); self.auto_detail.setTextFormat(Qt.PlainText); self.auto_detail.setWordWrap(True); layout.addWidget(self.auto_detail)
        self.status = W.QLabel(); self.status.setTextFormat(Qt.PlainText); self.status.setWordWrap(True); layout.addWidget(self.status)
        controls = [self.url, self.add, self.bulk, self.import_button, self.export_button, self.search, self.tree,
            self.new, self.rename_collection, self.delete, self.table, self.move, self.rename, self.remove, self.auto_button,self.auto_detail,self.status]
        for widget, name in zip(controls, ["Repository URL", "Add repository", "Bulk add", "Import library", "Export library",
                "Search library", "Collections", "New collection", "Rename collection", "Delete collection", "Repositories",
                "Move repositories", "Rename repository", "Remove repositories", "Auto-Archive settings","Auto-Archive details","Library status"]):
            widget.setAccessibleName(name); widget.setAccessibleDescription(name)
        for first, second in zip(controls, controls[1:]): self.setTabOrder(first, second)
        self.paste_shortcut = QShortcut(QKeySequence.StandardKey.Paste, self)
        self.paste_shortcut.setContext(Qt.WidgetWithChildrenShortcut); self.paste_shortcut.activated.connect(self._paste)
        self.search.textChanged.connect(self._filter); self.tree.currentItemChanged.connect(self._scope)
        self.table.selectionModel().selectionChanged.connect(lambda *_: (self._lock(),self._show_auto_detail()))
        if self.auto is not None: self.auto.changed.connect(self._auto_changed)
        self.url.returnPressed.connect(self._add); self.refresh(); self._lock()

    def _lock(self):
        self.setEnabled(not self.busy)
        for widget in [self.url, self.export_button, *self.mutating]: widget.setEnabled(self.store.writable)
        if not self.store.writable: self.status.setText(self.store.problem or "Library is read-only.")
        self.paste_shortcut.setEnabled(self.store.writable)
        for widget in (self.rename_collection, self.delete): widget.setEnabled(self.store.writable and bool(self.scope))
        count = len(self.table.selectionModel().selectedRows())
        for widget in (self.move, self.remove, self.rename): widget.setEnabled(self.store.writable and (count == 1 if widget is self.rename else count > 0))
        self.auto_button.setEnabled(self.store.writable and count>0 and self.auto is not None)

    def _transaction(self, op):
        if not self.store.writable: return False
        try: candidate = op(self.library)
        except m.LibraryError as exc:
            self.status.setText(reason(exc.code)); return False
        problem = self.store.save(candidate)
        self._lock()
        if problem: self.status.setText(problem); return False
        self.library = candidate; self.refresh(); self.changed.emit(); return True

    def _show_auto_detail(self):
        rows=self.table.selectionModel().selectedRows()
        self.auto_detail.setText(self.auto.detail(self.model.rows[rows[0].row()]) if self.auto is not None and len(rows)==1 else '')

    def _auto_changed(self):
        if self.model.rows: self.model.dataChanged.emit(self.model.index(0,4),self.model.index(len(self.model.rows)-1,5),[Qt.DisplayRole,Qt.ToolTipRole])
        self._show_auto_detail()

    def _auto_archive(self):
        if self.auto is None or not self.store.writable: return
        selected=set(self._selected()); repos=[r for r in self.model.rows if r.identity in selected]
        if not repos: return
        hours=repos[0].auto_archive; choices=['Off','Every 1 hour','Every 6 hours','Every 12 hours','Every 24 hours']
        name,ok=W.QInputDialog.getItem(self,'Auto-Archive','Check interval',choices,0 if hours is None else m.AUTO_ARCHIVE_HOURS.index(hours)+1,False)
        if not ok: return
        hours=(None,*m.AUTO_ARCHIVE_HOURS)[choices.index(name)]; skipped=0
        def noun(count): return 'repository' if count==1 else 'repositories'
        interval=f'{hours} hour'+('' if hours==1 else 's')
        if hours is not None:
            active=[r for r in repos if r.active]; skipped=len(repos)-len(active); repos=active
            if not repos: self.status.setText(f'No active GitHub {noun(skipped)} selected; {skipped} inactive {noun(skipped)} skipped'); return
            problems=[(repo,problem) for repo in repos if (problem:=self.auto.problem(repo))]
            if problems:
                repo,problem=problems[0]; reason='no saved rules' if problem==NEEDS_RULES else 'no saved archive folder'
                count=sum(p==problem for _,p in problems)
                text=f"Not enabled: '{repo.display_name}' has {reason}. {problem}"
                if len(selected)>1: text+=f' ({count} of {len(selected)} selected repositories need this.)'
                self.status.setText(text); return
            root=self.auto.settings()['archive_root']
            text=(f'Enable Auto-Archive for {len(repos)} {noun(len(repos))} every {interval}? Checks run only while CoveArchive is open. '
                f'The first check happens in {interval}, using the saved archive folder: {root}. '
                'It queues every release asset matching the saved rules that is not already queued (possibly many). '
                'Downloads may start automatically and use disk space and bandwidth. Existing archives are never deleted. Downloads follow the queue rules.')
            if W.QMessageBox.question(self,'Enable Auto-Archive',text,W.QMessageBox.Yes|W.QMessageBox.No,W.QMessageBox.No)!=W.QMessageBox.Yes: return
        ids=[r.identity for r in repos]
        if self._transaction(lambda lib:lib.set_auto_archive(ids,hours)):
            text=(f'Auto-Archive on for {len(ids)} {noun(len(ids))} every {interval}; first check in {interval}' if hours is not None else
                f'Auto-Archive off for {len(ids)} {noun(len(ids))}; archives and queued downloads are unchanged')
            self.status.setText(text+(f'; {skipped} inactive {noun(skipped)} skipped' if skipped else ''))

    def refresh(self):
        self.tree.blockSignals(True); self.tree.clear()
        counts = {c.id: 0 for c in self.library.collections}; parents = {c.id: c.parent for c in self.library.collections}
        for repo in self.library.repositories:
            current = repo.collection
            while current is not None: counts[current] += 1; current = parents[current]
        items = {}
        for key, label in [(None, "All repositories"), (m.UNSORTED, "Unsorted")]:
            count = len(self.library.repositories) if key is None else sum(r.collection is None for r in self.library.repositories)
            item = W.QTreeWidgetItem([f"{label} ({count})"]); item.setData(0, Qt.UserRole, key)
            self.tree.addTopLevelItem(item); items[key] = item
        for c in self.library.collections:
            item = W.QTreeWidgetItem([f"{c.name} ({counts[c.id]})"]); item.setData(0, Qt.UserRole, c.id); items[c.id] = item
        for c in self.library.collections:
            if c.parent is None: self.tree.addTopLevelItem(items[c.id])
            else: items[c.parent].addChild(items[c.id])
        if self.scope not in items: self.scope = None
        self.tree.setCurrentItem(items[self.scope]); self.tree.expandAll(); self.tree.blockSignals(False); self._filter()

    def _scope(self, current, previous):
        self.scope = current.data(0, Qt.UserRole) if current else None; self._filter()
    def _filter(self): self.model.set_rows(self.library, self.library.browse(self.scope, search=self.search.text())); self._lock(); self._show_auto_detail()
    def _selected(self): return [self.model.rows[i.row()].identity for i in self.table.selectionModel().selectedRows()]
    def _add(self, checked=False, *, text=None):
        if not self.store.writable: return
        if text is None and not self.url.text().strip(): self.status.setText("Enter a repository URL"); return
        try: result = m.add_urls(self.library, self.url.text() if text is None else text, self.scope or None)
        except m.LibraryError as exc: self.status.setText(reason(exc.code)); return
        if not result.added or self._transaction(lambda lib: result.library):
            if result.added: self.url.clear()
            failures = [f"Line {r.line}: {reason(r.code)}" for r in result.results if r.status == "FAILED"][:5]
            self.status.setText(f"Added {result.added}, skipped {result.skipped} duplicate(s), failed {result.failed}" +
                ("\n" + "\n".join(failures) if failures else ""))
    def _bulk(self):
        text, ok = W.QInputDialog.getMultiLineText(self, "Bulk Add", "Repository URLs (one per line)")
        if ok: self._add(text=text)
    def _paste(self):
        if not self.store.writable: return
        if isinstance(W.QApplication.focusWidget(), (W.QLineEdit, W.QTextEdit, W.QPlainTextEdit, W.QAbstractSpinBox)): return
        if not W.QApplication.clipboard().text().strip(): self.status.setText("Clipboard has no text"); return
        self._add(text=W.QApplication.clipboard().text())
    def _new(self):
        name, ok = W.QInputDialog.getText(self, "New collection", "Name")
        if ok: self._transaction(lambda lib: lib.create_collection(name, self.scope or None))
    def _rename_collection(self):
        if not self.scope: return
        name, ok = W.QInputDialog.getText(self, "Rename collection", "Name")
        if ok: self._transaction(lambda lib: lib.rename_collection(self.scope, name))
    def _delete(self):
        if not self.scope: return
        count, repos = len(self.library.descendant_ids(self.scope)), len(self.library.browse(self.scope))
        if W.QMessageBox.question(self, "Delete collection", f"Delete {count} collections containing {repos} repositories? Repositories and archives are kept.", W.QMessageBox.Yes | W.QMessageBox.No, W.QMessageBox.No) == W.QMessageBox.Yes:
            self._transaction(lambda lib: lib.delete_collection(self.scope))
    def _move(self):
        selected = self._selected()
        if not selected: return
        choices = {"/ (Unsorted)": None, **{self.library.collection_path(c.id): c.id for c in self.library.collections}}
        name, ok = W.QInputDialog.getItem(self, "Move repositories", "Collection", list(choices), 0, False)
        if ok and self._transaction(lambda lib: lib.move_repositories(selected, choices[name])): self.status.setText(f"Moved {len(selected)} to {name}")
    def _rename(self):
        selected = self._selected()
        if len(selected) != 1: return
        name, ok = W.QInputDialog.getText(self, "Rename repository", "Name (empty resets)")
        if ok and self._transaction(lambda lib: lib.rename_repository(selected[0], name.strip() or None)): self.status.setText("Renamed")
    def _remove(self):
        selected = self._selected()
        if selected and W.QMessageBox.question(self, "Remove repositories", f"Remove {len(selected)} from the list? Archives and saved rules are NOT touched.", W.QMessageBox.Yes | W.QMessageBox.No, W.QMessageBox.No) == W.QMessageBox.Yes:
            if self._transaction(lambda lib: lib.remove_repositories(selected)): self.status.setText(f"Removed {len(selected)} from the list; archives untouched")
    def _import(self):
        if not self.store.writable or self.busy or self._closed: return
        path, _ = W.QFileDialog.getOpenFileName(self, "Import library", "", "JSON (*.json)")
        if not path: return
        self._snapshot = self.library; self.busy = True; self._lock(); self.status.setText("Reading import file...")
        self._future = self._worker.submit(_prepare_import, path, self._snapshot); self._poll.start()
    def closeEvent(self, event):
        self._closed = True; self._poll.stop(); self._worker.shutdown(wait=False, cancel_futures=True); super().closeEvent(event)
    def hideEvent(self, event):
        if self.busy and not self.window().isVisible(): self._closed = True; self._poll.stop(); self._worker.shutdown(wait=False, cancel_futures=True)
        super().hideEvent(event)
    def _finish_import(self):
        if not self._future.done(): return
        self._poll.stop(); self.busy = False
        self._lock()
        if self._closed or not self.window().isVisible(): return
        try: plan, candidate = self._future.result(); snapshot = self._snapshot
        except m.LibraryError as exc: self.status.setText("Nothing was imported: " + reason(exc.code)); return
        box = W.QMessageBox(self); box.setWindowTitle("Import preview"); box.setTextFormat(Qt.PlainText)
        title = "Import from Git-Sync data" if plan.kind == "git-sync" else "Import from a CoveArchive list"
        box.setText(f"{title}\n{plan.entries} repositories in the file\n{plan.to_add} will be added\n{plan.duplicates} duplicate{'s' if plan.duplicates != 1 else ''} (already in your list or repeated in the file; skipped)\n{plan.inactive} of these are inactive (GitLab/Gitea/other): kept in the list but not archived\n{plan.new_collections} new collections will be created" +
            (f"\n{plan.retention_notices} {'entry' if plan.retention_notices == 1 else 'entries'} in this file {'asks' if plan.retention_notices == 1 else 'ask'} to drop older versions; CoveArchive ignores this and never deletes archived versions." if plan.retention_notices else ""))
        if not plan.valid: box.setText("Nothing was imported: " + reason(plan.code) if plan.code else f"Nothing was imported. The file has {'50 or more' if len(plan.invalid) == 50 else len(plan.invalid)} invalid entries (first 50 shown). Fix the file and try again.")
        box.setDetailedText("\n".join(f"{'Entry ' + str(i) if i else 'File structure'}: {reason(code)}" for i, code in plan.invalid))
        box.addButton(W.QMessageBox.Cancel)
        accept = box.addButton("Import", W.QMessageBox.AcceptRole) if plan.valid else None
        box.exec()
        if accept is not None and box.clickedButton() == accept and self._transaction(lambda current: candidate if current is snapshot else plan.apply(current)):
            self.status.setText(f"Added {plan.to_add}, skipped {plan.duplicates} duplicate(s), inactive {plan.inactive}")
    def _export(self):
        if not self.store.writable: return
        path, _ = W.QFileDialog.getSaveFileName(self, "Export library", "", "JSON (*.json)")
        if not path: return
        target = os.path.normcase(os.path.realpath(path))
        roots = [self.store.path.parent if self.store.path is not None else None, *self.protected()]
        for folder in (os.path.normcase(os.path.realpath(root)) for root in roots if root):
            if target == folder or target.startswith(folder.rstrip(os.sep) + os.sep):
                self.status.setText("Choose a location outside CoveArchive's data and archive folders"); return
        if (":" in os.path.basename(target) or (os.path.lexists(path) and not t.is_export_file(path))
                or not os.path.isdir(os.path.dirname(os.path.abspath(path)))):
            self.status.setText("Choose a new file name; only a previous CoveArchive export can be replaced"); return
        try: problem = atomic_write(path, t.export_library(self.library))
        except m.LibraryError as exc: self.status.setText(reason(exc.code)); return
        self.status.setText(problem or "Library exported")
