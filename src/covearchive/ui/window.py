"""Native, read-only release scanner with local filtering and lazy tree rows."""

import logging
import sys
import time
import unicodedata

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QLabel, QLineEdit,
    QHeaderView, QMainWindow, QProgressBar, QPushButton, QTreeView, QVBoxLayout, QWidget)

from covearchive.formats import FORMAT_LABELS, SUPPORTED_EXTENSIONS
from covearchive.github.select import select_releases
from covearchive.github.urls import GitHubUrlError, ReleaseTagTarget, parse_github_url
from covearchive.scan_worker import ProtocolError, ScanFailure
from covearchive.ui.scan_process import ScanController


def display(text, limit=8192):
    text = str(text)
    return "".join(f"\\u{ord(c):04x}" if unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp", "Co") else c
                   for c in text[:limit]) + ("…" if len(text) > limit else "")


def size_text(size):
    for unit, divisor in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if size >= divisor: return f"{size/divisor:.1f} {unit}"
    return f"{size} B"


class ReleaseModel(QAbstractItemModel):
    """Indexes reference immutable backend objects; children are built on demand."""
    headers = ("Name", "Tag", "Published", "Format", "Size", "Status")

    def __init__(self, selection=None, parent=None):
        super().__init__(parent)
        self.rows = () if selection is None else selection.releases
        self.enabled = () if selection is None else selection.enabled_extensions
        self.matches = tuple(frozenset(id(match.asset) for match in row.matches) for row in self.rows)

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
        if role != Qt.DisplayRole: return None
        if asset:
            status = ("Not uploaded" if asset.state != "uploaded" else
                "Unsupported format" if asset.supported_extension is None else
                "Filtered out" if asset.supported_extension not in self.enabled else
                "Eligible" if id(asset) in self.matches[root] else "Duplicate asset")
            values = (asset.name, "", "",
                      asset.supported_extension or "-", size_text(asset.size), status)
        else:
            values = (release.name or release.tag_name, release.tag_name, (release.published_at or "-")[:10],
                      "-", "-", f"{len(selected.matches)} matching assets" if selected.matches else
                      "No assets" if not release.assets else "No matching assets")
            if release.prerelease: values = (*values[:-1], "Prerelease, " + values[-1])
        return display(values[index.column()])


class MainWindow(QMainWindow):
    def __init__(self, launcher=None, controller=None):
        super().__init__()
        self.setWindowTitle("CoveArchive"); self.resize(1180,720); self.setMinimumSize(820,520)
        self.controller = controller if controller is not None else ScanController(launcher, parent=self)
        self.catalog = None; self.show_catalog = False; self.active_id = None
        self.started = 0
        central = QWidget(); self.setCentralWidget(central); layout = QVBoxLayout(central)
        layout.addWidget(QLabel("CoveArchive"))
        row = QHBoxLayout(); layout.addLayout(row)
        self.url = QLineEdit(); self.url.setPlaceholderText("https://github.com/owner/repository/releases")
        self.scan = QPushButton("Scan"); self.cancel = QPushButton("Cancel Scan"); self.cancel.setEnabled(False)
        row.addWidget(self.url,1); row.addWidget(self.scan); row.addWidget(self.cancel)
        filters = QHBoxLayout(); layout.addLayout(filters)
        self.prereleases = QCheckBox("Include prereleases"); filters.addWidget(self.prereleases)
        self.formats = {}
        for extension in SUPPORTED_EXTENSIONS:
            box = QCheckBox(extension); box.setChecked(True); box.setAccessibleName(FORMAT_LABELS[extension])
            self.formats[extension] = box; filters.addWidget(box); box.toggled.connect(self._refresh)
        filters.addStretch()
        self.summary = QLabel(); self.summary.setTextFormat(Qt.PlainText); layout.addWidget(self.summary)
        self.tree = QTreeView(); self.tree.setUniformRowHeights(True); self.tree.setWordWrap(False)
        self.tree.setTextElideMode(Qt.ElideRight); layout.addWidget(self.tree,1)
        self._model(None)
        self.detail = QLabel(); self.detail.setTextFormat(Qt.PlainText); self.detail.setWordWrap(True)
        self.detail.setMaximumHeight(96); layout.addWidget(self.detail)
        self.status = QLabel("Ready"); self.status.setTextFormat(Qt.PlainText); self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.busy = QProgressBar(); self.busy.setRange(0,0); self.busy.setVisible(False); layout.addWidget(self.busy)
        self.elapsed = QLabel(); self.elapsed.setTextFormat(Qt.PlainText); self.elapsed.hide(); layout.addWidget(self.elapsed)
        self.clock = QTimer(self); self.clock.setInterval(250); self.clock.timeout.connect(self._tick)
        controls = [self.url,self.scan,self.cancel,self.prereleases,*self.formats.values(),self.tree]
        for widget,name in zip(controls[:4],("GitHub URL","Scan","Cancel Scan","Include prereleases")):
            widget.setAccessibleName(name)
        self.tree.setAccessibleName("Releases and assets")
        for previous,following in zip(controls,controls[1:]): QWidget.setTabOrder(previous,following)
        self.scan.clicked.connect(self._start); self.url.returnPressed.connect(self._start)
        self.cancel.clicked.connect(self.controller.cancel); self.prereleases.toggled.connect(self._refresh)
        self.controller.state_changed.connect(self._state); self.controller.terminal.connect(self._terminal)
        QApplication.instance().aboutToQuit.connect(self.controller.shutdown)
        self.setStyleSheet("""
            QWidget { background:#182125; color:#EDF4F4; font-size:13px; }
            QLineEdit,QTreeView { background:#10191D; border:1px solid #53696D; padding:5px; }
            QPushButton { padding:8px 15px; border:1px solid #50E6CF; }
            QPushButton:disabled { color:#8B9B9E; border-color:#53696D; }
            QLineEdit:focus,QPushButton:focus,QCheckBox:focus,QTreeView:focus { border:2px solid #50E6CF; }
            QTreeView::item:selected { background:#285D59; }
            QProgressBar::chunk { background:#50E6CF; }
        """)
        self.url.setFocus()

    def _model(self, selection):
        old = self.tree.model(); model = ReleaseModel(selection,self.tree); self.tree.setModel(model)
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
        self.show_catalog=False; self.summary.clear(); self.detail.clear(); self._model(None)

    def _start(self):
        if self.active_id is not None or self.controller.job is not None: return
        try:
            target = parse_github_url(self.url.text())
            if isinstance(target,ReleaseTagTarget):
                self._clear(); self._status("Invalid URL: paste the repository or its /releases URL",True); return
            self.controller.start(f"https://github.com/{target.owner}/{target.repo}")
        except (GitHubUrlError,TypeError,ProtocolError):
            self._clear(); self._status("Invalid URL: not a supported GitHub repository/releases URL",True); return

    def _state(self, request_id, state):
        if state == "RUNNING":
            self.active_id=request_id; self._clear(); self.started=time.monotonic()
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
        if request_id != self.active_id: return
        self.active_id=None; self.clock.stop(); self.busy.hide(); self.elapsed.hide()
        self.url.setEnabled(True); self.scan.setEnabled(True); self.cancel.setEnabled(False)
        self.url.setFocus()
        if state == "COMPLETED":
            self.catalog=result.catalog; self.show_catalog=True; self._refresh(); return
        self._clear()
        if state == "CANCELLED": self._status("Cancelled"); return
        if state == "TIMED_OUT": self._status(f"Timed out after {self.controller.deadline_ms/1000:g} seconds; try again",True); return
        if result.detail and "stderr" in result.detail:
            logging.getLogger("covearchive.ui").warning("Scan worker diagnostics: %s", result.detail["stderr"])
        self._status(self._error_text(result),True)

    def _error_text(self, error):
        code=error.code; detail=error.detail or {}
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
        if code == "timeout": return "Timed out: GitHub did not respond in time; try again"
        if code == "incomplete_scan": return "Release listing incomplete, retry"
        if code in ("invalid_json","invalid_root","too_many_items","invalid_release","response_too_large","protocol"):
            return "Failed: GitHub returned unexpected data"
        if code == "invalid_url": return "Invalid URL: not a supported GitHub repository/releases URL"
        return f"Failed ({code})"

    def _refresh(self):
        if not self.show_catalog: return
        enabled=tuple(ext for ext,box in self.formats.items() if box.isChecked())
        selection=select_releases(self.catalog,include_prereleases=self.prereleases.isChecked(),enabled_extensions=enabled)
        self._model(selection); self.detail.clear()
        count=sum(len(row.matches) for row in selection.releases)
        self.summary.setText(f"{display(self.catalog.repository.owner)}/{display(self.catalog.repository.repo)} | "
                             f"{len(selection.releases)} releases | {count} matching assets")
        hidden=sum(x.reason=="prerelease" for x in selection.excluded)
        text = ("No formats selected" if not enabled else "No releases found" if not self.catalog.releases else
                f"{hidden} prereleases hidden" if not selection.releases and hidden else
                "No matching assets" if not count else "Completed")
        self._status(text)

    def closeEvent(self,event):
        if self.controller.shutdown(): event.accept()
        else: event.ignore()


def main(argv=None, launcher=None):
    app=QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    window=MainWindow(launcher); window.show()
    return app.exec()
