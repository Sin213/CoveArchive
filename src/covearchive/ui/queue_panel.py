"""Honest queue presentation; all backend work goes through the bridge."""
import re
import time
import unicodedata
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView)
from covearchive.queue.models import JobState as S, safe_code

def display(text, limit=8192):
    text = str(text)
    return "".join(f"\\u{ord(c):04x}" if unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp", "Co") else c
                   for c in text[:limit]) + ("\u2026" if len(text) > limit else "")


def size_text(size):
    for unit, divisor in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if size >= divisor: return f"{size/divisor:.1f} {unit}"
    return f"{size} B"


_ERRORS = {
    "database_busy":"Queue database is busy", "database_unavailable":"Queue database is unavailable",
    "rate_limited":"GitHub rate limited", "server_error":"Server unavailable",
    "network":"Network connection failed", "timeout":"Request timed out",
    "upstream_removed":"Upstream asset removed", "release_removed":"Release removed",
    "asset_unavailable":"Asset unavailable", "incomplete_scan":"Scan incomplete",
    "internal_error":"Internal error", "unknown_cdn_host":"Unapproved download host",
    "disk_full":"Disk full", "manifest_commit_incomplete":"Manifest commit incomplete",
    "permission_denied":"Permission denied", "unsafe_path":"Unsafe path",
    "size_mismatch":"Size mismatch", "partial_transfer":"Transfer incomplete",
    "file_busy":"File busy", "hash_mismatch":"Hash mismatch", "path_budget":"Path too long",
    "interrupted":"Interrupted", "retry_exhausted":"Retry limit reached",
    "tls_error":"TLS connection failed", "cancelled":"Cancelled",
}
_RESULTS = {"ARCHIVED":"Archived", "ALREADY_ARCHIVED":"Already archived", "VERIFIED_EXISTING":"Verified existing - no download needed", "MODIFIED":"Local archive was modified", "CONFLICT":"Archive conflicts with an existing file", "MISMATCH":"Archive data does not match", "RECONCILE_REQUIRED":"Archive reconciliation required", "UPSTREAM_REMOVED":"Upstream asset removed"}
_HISTORY = "This result was recorded when the job finished. Files are re-verified only when the job is queued again."

def _safe(text):
    text = str(text)
    if re.search(r"://|[?&](?:sig|token|se|sp|sv|sr|X-Amz-[A-Za-z-]+)=",text,re.I): return "[redacted]"
    return display(text).replace("<", "\\u003c").replace(">", "\\u003e")

def _error(code): return _ERRORS.get(code, "Error code: " + safe_code(code)) if code else ""

def detail_text(job):
    result = _RESULTS.get(job.result_status, "") if job.state == S.COMPLETED or job.result_status not in ("ARCHIVED","ALREADY_ARCHIVED","VERIFIED_EXISTING") else ""
    if job.integrity == "LOCAL_HASH_ONLY" and job.result_status == "VERIFIED_EXISTING": result = "Existing archive unchanged - no download needed" if job.state == S.COMPLETED else ""
    integrity = {"PUBLISHER_VERIFIED":"Matches publisher SHA-256", "LOCAL_HASH_ONLY":"Local SHA-256 recorded"}.get(job.integrity, "") if job.state == S.COMPLETED else ""
    return " | ".join(filter(None, (result, integrity, _error(job.error_code))))

def status_text(job, now):
    if job.state == S.RUNNING:
        if job.stop_pending: return "Cancelling..." if job.stop_pending == "cancel" else "Pausing..."
        if not job.owned: return "Interrupted or running elsewhere"
        if job.bytes_done is None: return "Starting"
        return "Download received - finalizing and verifying" if job.bytes_total and job.bytes_done >= job.bytes_total else "Downloading"
    if job.state == S.RETRY_WAIT:
        at = time.strftime("%H:%M:%S",time.localtime(job.next_attempt_at)) if job.next_attempt_at is not None else "unknown"
        return "Waiting to retry at " + at + " (local time)" + (" - " + _error(job.error_code) if job.error_code else "")
    return {S.QUEUED:"Queued",S.PAUSED:"Paused",S.CANCELLED:"Cancelled",S.FAILED:"Failed",S.COMPLETED:"Completed (queue record)"}[job.state] + (" - " + detail_text(job) if job.state in (S.COMPLETED,S.FAILED) and detail_text(job) else "")

def progress_text(job):
    if job.bytes_done is None and job.bytes_total is None: return "size "+size_text(job.asset_size) if job.asset_size is not None else "-"
    return (size_text(job.bytes_done) if job.bytes_done is not None else "unknown") + " / " + (size_text(job.bytes_total) if job.bytes_total is not None else "unknown")

def speed_eta_text(job):
    speed = job.speed_bps; eta = "unknown"
    if speed is not None and speed > 0 and job.bytes_total and job.bytes_done is not None: eta = f"{max(0,(job.bytes_total-job.bytes_done)/speed):.0f}s"
    return (size_text(speed)+"/s" if speed is not None else "unknown") + " / ETA " + eta

def action_states(jobs):
    permitted = {"pause":(S.QUEUED,S.RETRY_WAIT,S.RUNNING),"resume":(S.PAUSED,),"cancel":(S.QUEUED,S.RETRY_WAIT,S.PAUSED,S.RUNNING),"retry":(S.FAILED,S.CANCELLED)}
    return {a:bool(jobs) and all(j.state in states and (j.state != S.RUNNING or j.owned) for j in jobs) for a,states in permitted.items()}


def state_text(state): return "Waiting to retry" if state==S.RETRY_WAIT else state.value.title() if state else "unknown"


class QueuePanel(QWidget):
    def __init__(self, bridge, parent=None):
        super().__init__(parent); self.bridge=bridge; self.view=None; self.seq=-1; self.token=0; self.waiting=set(); self.ack={}
        layout=QVBoxLayout(self); row=QHBoxLayout(); layout.addLayout(row)
        self.start=QPushButton("Start Queue"); self.buttons={a:QPushButton(a.title()) for a in ("pause","resume","cancel","retry")}
        for button in [self.start,*self.buttons.values()]:
            row.addWidget(button); button.setAccessibleName(button.text()); button.setAccessibleDescription(button.text()+" for selected queue jobs")
        self.start.setAccessibleDescription("Start all pending queue jobs"); self.start.clicked.connect(self._start)
        for action,button in self.buttons.items(): button.clicked.connect(lambda checked=False,a=action:self._control(a))
        self.banner=QLabel("Loading queue"); self.summary=QLabel(); self.message=QLabel()
        for label in (self.banner,self.summary,self.message): label.setTextFormat(Qt.PlainText); label.setWordWrap(True); layout.addWidget(label)
        self.table=QTableWidget(0,6); self.table.setHorizontalHeaderLabels(("File","Repository / release","Status","Progress (bytes)","Speed / ETA","Detail/error"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows); self.table.setSelectionMode(QAbstractItemView.ExtendedSelection); self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setTextElideMode(Qt.ElideRight); self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive); self.table.horizontalHeader().setStretchLastSection(False)
        for column in (0,5): self.table.horizontalHeader().setSectionResizeMode(column,QHeaderView.Stretch)
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(max(header.fontMetrics().horizontalAdvance(self.table.horizontalHeaderItem(c).text())
                                        for c in (0,5))+24)
        for column,text in ((1,"Owner/Repository-Name / v10.20.30-rc.10"),
                            (2,"Download received - finalizing and verifying"),
                            (3,"1023.9 GiB / 1023.9 GiB"),(4,"1023.9 MiB/s / ETA 999999s")):
            width = max(self.table.fontMetrics().horizontalAdvance(text),
                        header.fontMetrics().horizontalAdvance(self.table.horizontalHeaderItem(column).text()))
            self.table.setColumnWidth(column,width+24)
        self.table.setAccessibleName("Archive queue"); self.table.setAccessibleDescription("Select one or more jobs to control"); layout.addWidget(self.table,1)
        self.table.itemSelectionChanged.connect(self._enabled); self.table.setStyleSheet("QTableWidget:focus { border:2px solid #50E6CF; } QTableWidget::item:selected { background:#285D59; }")
        help_label=QLabel("Completed = result recorded when the job finished; files are re-verified only when queued again.\nSelect a job to enable Pause, Resume, Cancel or Retry. Fix the cause of a failure, then Retry.\nResume restarts an incomplete file from the beginning (no byte-level resume)."); help_label.setTextFormat(Qt.PlainText); help_label.setWordWrap(True); layout.addWidget(help_label)
        for signal,slot in ((bridge.view_changed,self._view),(bridge.control_finished,self._result),(bridge.run_event,self._run)): signal.connect(slot,Qt.QueuedConnection)
        self.results=[]
        self.failed=False
        self._enabled()

    def selected_ids(self):
        return tuple(self.table.item(i.row(),0).data(Qt.UserRole) for i in self.table.selectionModel().selectedRows())

    def _enabled(self):
        selected=set(self.selected_ids()); jobs=tuple(j for j in self.view.jobs if j.job_id in selected) if self.view else ()
        available=bool(self.view) and self.view.usable and self.bridge.is_open and not self.bridge.closed and not self.bridge.shutting_down and not self.waiting
        for action,enabled in action_states(jobs).items(): self.buttons[action].setEnabled(bool(available and enabled))
        interrupted = bool(self.view and any(j.state==S.RUNNING and not j.owned and not j.stop_pending
                                            for j in self.view.jobs))
        self.start.setEnabled(bool(available and self.view.run_state in ("idle","blocked") and (interrupted or any(j.state in (S.QUEUED,S.RETRY_WAIT) for j in self.view.jobs))))

    @Slot(object)
    def _view(self, view):
        if view.seq < self.seq: return
        if not self.failed and self.ack and any(self.ack.get(j.job_id)!=(j.state,j.stop_pending) for j in view.jobs if j.job_id in self.ack): self.message.clear(); self.ack.clear()
        selected=set(self.selected_ids()); self.seq=view.seq; self.view=view; self.table.blockSignals(True); self.table.clearSelection(); self.table.setRowCount(len(view.jobs))
        for row,job in enumerate(view.jobs):
            values=(job.asset_name,job.repository+" / "+job.tag,status_text(job,time.time()),progress_text(job),speed_eta_text(job),detail_text(job))
            for column,text in enumerate(values):
                cell=QTableWidgetItem(_safe(text)); cell.setToolTip(_safe(text)+("\n"+_HISTORY if job.state==S.COMPLETED else "")); cell.setData(Qt.UserRole,job.job_id); self.table.setItem(row,column,cell)
            if job.job_id in selected:
                for column in range(6): self.table.item(row,column).setSelected(True)
        self.table.blockSignals(False)
        self.banner.setText(_safe(view.problem) if view.problem else ("GitHub rate limited until "+time.strftime("%H:%M:%S",time.localtime(view.cooldown_until))+"; retries wait automatically" if view.cooldown_until else ""))
        self.summary.setText(" | ".join(state_text(s)+" "+str(n) for s,n in view.counts if n)+" | "+(size_text(view.bytes_done) if view.bytes_done is not None else "unknown")+" / "+(size_text(view.bytes_total) if view.bytes_total is not None else "unknown")); self._enabled()

    def _start(self):
        self.failed=False
        self.ack.clear()
        if self.start.isEnabled(): self.bridge.start_queue(); self.message.setText("Starting queue...")
        else: self.message.setText("Nothing to run")

    def _control(self, action):
        if not self.buttons[action].isEnabled(): return
        self.results=[]
        self.failed=False
        self.ack.clear()
        self.message.clear()
        self.token+=1; ids=self.selected_ids(); self.waiting=set(ids); self._enabled(); self.bridge.control(self.token,action,ids)

    @Slot(object)
    def _result(self, result):
        if result.token!=self.token or result.job_id not in self.waiting: return
        self.waiting.discard(result.job_id)
        job=next((j for j in self.view.jobs if j.job_id==result.job_id),None) if self.view else None
        state=state_text(job.state) if job else "unknown"
        self.ack[result.job_id]=(result.state_after or (job.state if job else None),result.stop_pending if result.ok else job.stop_pending if job else None)
        name=job.asset_name if job else "selected file"
        reason=({"ownership":"running in another CoveArchive process","transition":"Not allowed in state "+state}.get(result.code,_error(result.code)))
        text=name+": "+("waiting for the worker to stop" if result.stop_pending else "acknowledged") if result.ok else name+": "+reason
        if result.startup_error: text+="; automatic start failed: "+_error(result.startup_error)+". Press Start Queue."
        self.results.append((result.ok,text))
        self.failed=self.failed or not result.ok or bool(result.startup_error)
        succeeded=sum(ok for ok,_ in self.results)
        text=f"{result.action.title()}: {succeeded} succeeded, {len(self.results)-succeeded} failed - "+"; ".join(sorted(t for _,t in self.results))
        if result.started: text+="; queue started"
        if result.deferred_pending: text+=f"; {result.deferred_pending} earlier queued job(s) are also waiting - press Start Queue to run all"
        self.message.setText(_safe(text)); self._enabled()

    @Slot(object)
    def _run(self, event):
        if self.failed: return
        self.ack.clear()
        self.message.setText({"started":"Queue started", "finished":"Queue run finished", "blocked":"Another CoveArchive process owns the queue lock", "error":"Queue run failed: "+_error(event.code)}.get(event.kind,"Queue updated"))
