"""Honest queue presentation; all backend work goes through the bridge."""
import re
import time
import unicodedata
from PySide6.QtCore import Qt, Slot, QEvent
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QSizePolicy, QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView)
from covearchive.queue.models import JobState as S, safe_code
from covearchive.ui.theme import set_role, set_tone, current_tokens, fixed_font

def display(text, limit=8192):
    text = str(text)
    return "".join(f"\\u{ord(c):04x}" if unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp", "Co") else c
                   for c in text[:limit]) + ("\u2026" if len(text) > limit else "")


def size_text(size):
    for unit, divisor in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if size >= divisor: return f"{size/divisor:.1f} {unit}"
    return f"{size} B"


_ERRORS = {
    "unauthorized":"GitHub rejected the access token; replace or clear it in Settings",
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

def _safe_lines(text):
    text = str(text)
    if re.search(r"://|[?&](?:sig|token|se|sp|sv|sr|X-Amz-[A-Za-z-]+)=",text,re.I): return "[redacted]"
    return "\n".join(display(line).replace("<", "\\u003c").replace(">", "\\u003e") for line in text.split("\n"))


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
        self.setObjectName("queuePage")
        layout=QVBoxLayout(self); layout.setContentsMargins(8,6,8,6); layout.setSpacing(3)
        toolbar=QFrame(); layout.addWidget(toolbar)
        row=QHBoxLayout(toolbar); row.setContentsMargins(0,0,0,0); row.setSpacing(5)
        self.start=QPushButton("Start Queue"); self.buttons={a:QPushButton(a.title()) for a in ("pause","resume","cancel","retry")}
        for button in [self.start,*self.buttons.values()]:
            button.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed); row.addWidget(button); button.setAccessibleName(button.text()); button.setAccessibleDescription(button.text()+" for selected queue jobs")
        row.addStretch()
        for button in self.buttons.values(): set_role(button, "secondary")
        set_role(self.start, "primary"); set_role(self.buttons["cancel"], "danger")
        self.start.setAccessibleDescription("Start all pending queue jobs"); self.start.clicked.connect(self._start)
        for action,button in self.buttons.items(): button.clicked.connect(lambda checked=False,a=action:self._control(a))
        self.banner=QLabel("Loading queue"); self.summary=QLabel(); self.message=QLabel()
        for label in (self.banner,self.summary,self.message): label.setTextFormat(Qt.PlainText); label.setWordWrap(True)
        self.summary.setFont(fixed_font(self)); self.summary.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
        self.summary.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred); row.addWidget(self.summary,1)
        layout.addWidget(self.banner); layout.addWidget(self.message)
        self.banner.setProperty("notice", True); set_tone(self.banner,"warning")
        self.banner.setAccessibleName("Queue notice")
        set_tone(self.summary,"muted"); set_tone(self.message,"normal")
        self.table=QTableWidget(0,6); self.table.setHorizontalHeaderLabels(("File","Repository / release","Status","Progress (bytes)","Speed / ETA","Detail/error"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows); self.table.setSelectionMode(QAbstractItemView.ExtendedSelection); self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setTextElideMode(Qt.ElideRight); self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive); self.table.horizontalHeader().setStretchLastSection(False)
        for column in (0,5): self.table.horizontalHeader().setSectionResizeMode(column,QHeaderView.Stretch)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignLeft|Qt.AlignVCenter)
        header.setMinimumSectionSize(max(header.fontMetrics().horizontalAdvance(self.table.horizontalHeaderItem(c).text())
                                        for c in (0,5))+24)
        for column,text in ((1,"Owner/Repository-Name / v10.20.30-rc.10"),
                            (2,"Download received - finalizing and verifying"),
                            (3,"1023.9 GiB / 1023.9 GiB"),(4,"1023.9 MiB/s / ETA 999999s")):
            width = max(self.table.fontMetrics().horizontalAdvance(text),
                        header.fontMetrics().horizontalAdvance(self.table.horizontalHeaderItem(column).text()))
            self.table.setColumnWidth(column,width+24)
        self.table.setAccessibleName("Archive queue"); self.table.setAccessibleDescription("Select one or more jobs to control"); layout.addWidget(self.table,1)
        self.table.setMinimumHeight(140)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        overlay=QVBoxLayout(self.table.viewport()); overlay.setContentsMargins(16,12,16,12)
        self.empty=QLabel("Select assets in Browse and choose Archive Selected.\nNothing starts automatically.")
        self.empty.setTextFormat(Qt.PlainText); self.empty.setWordWrap(True); self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setAttribute(Qt.WA_TransparentForMouseEvents); set_tone(self.empty,"muted"); overlay.addWidget(self.empty)
        self.table.itemSelectionChanged.connect(self._enabled)
        help_label=QLabel("Completed = result recorded when the job finished; files are re-verified only when queued again.\nSelect a job to enable Pause, Resume, Cancel or Retry. Fix the cause of a failure, then Retry.\nResume restarts an incomplete file from the beginning (no byte-level resume)."); help_label.setTextFormat(Qt.PlainText); help_label.setWordWrap(True); help_label.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred); set_tone(help_label,"muted"); layout.addWidget(help_label)
        self._bridge_slots=((bridge.view_changed,self._view),(bridge.control_finished,self._result),(bridge.run_event,self._run))
        for signal,slot in self._bridge_slots: signal.connect(slot,Qt.QueuedConnection)
        self.results=[]
        self.failed=False; self._closed=False
        self._enabled()

    def selected_ids(self):
        return tuple(self.table.item(i.row(),0).data(Qt.UserRole) for i in self.table.selectionModel().selectedRows())

    def closeEvent(self,event): self.disconnect_bridge(); super().closeEvent(event)

    def disconnect_bridge(self):
        if self._closed: return
        self._closed=True
        for signal,slot in self._bridge_slots: signal.disconnect(slot)

    def _enabled(self):
        selected=set(self.selected_ids()); jobs=tuple(j for j in self.view.jobs if j.job_id in selected) if self.view else ()
        available=bool(self.view) and self.view.usable and self.bridge.is_open and not self.bridge.closed and not self.bridge.shutting_down and not self.waiting
        for action,enabled in action_states(jobs).items(): self.buttons[action].setEnabled(bool(available and enabled))
        interrupted = bool(self.view and any(j.state==S.RUNNING and not j.owned and not j.stop_pending
                                            for j in self.view.jobs))
        self.start.setEnabled(bool(available and self.view.run_state in ("idle","blocked") and (interrupted or any(j.state in (S.QUEUED,S.RETRY_WAIT) for j in self.view.jobs))))

    @Slot(object)
    def _view(self, view):
        if self._closed or view.seq < self.seq: return
        if not self.failed and self.ack and any(self.ack.get(j.job_id)!=(j.state,j.stop_pending) for j in view.jobs if j.job_id in self.ack): self._message(""); self.ack.clear()
        selected=set(self.selected_ids()); self.seq=view.seq; self.view=view; self.table.blockSignals(True); self.table.clearSelection(); self.table.setRowCount(len(view.jobs))
        finished=(S.COMPLETED,S.FAILED,S.CANCELLED)
        pending=tuple(j for j in view.jobs if j.state not in finished)
        done=tuple(j for j in view.jobs if j.state in finished)
        for row,job in enumerate((*pending,*done)):
            marker="Finished" if job.state in finished else "Pending"
            self.table.setVerticalHeaderItem(row,QTableWidgetItem(marker))
            values=(job.asset_name,job.repository+" / "+job.tag,status_text(job,time.time()),progress_text(job),speed_eta_text(job),detail_text(job))
            for column,text in enumerate(values):
                cell=QTableWidgetItem(_safe(text)); cell.setToolTip(_safe(text)+("\n"+_HISTORY if job.state==S.COMPLETED else "")); cell.setData(Qt.UserRole,job.job_id); cell.setData(Qt.UserRole+1,job.state.value); self.table.setItem(row,column,cell)
            if job.job_id in selected:
                for column in range(6): self.table.item(row,column).setSelected(True)
        self.table.blockSignals(False)
        self.empty.setText("Queue unavailable." if view.problem or not view.usable else
                           "Select assets in Browse and choose Archive Selected.\nNothing starts automatically.")
        self.empty.setVisible(not view.jobs); self._style_rows()
        self.banner.setText(_safe(view.problem) if view.problem else ("GitHub rate limited until "+time.strftime("%H:%M:%S",time.localtime(view.cooldown_until))+"; retries wait automatically" if view.cooldown_until else ""))
        self.banner.setVisible(bool(self.banner.text()))
        self.banner.setToolTip(_safe(self.banner.text()))
        set_tone(self.banner,"error" if view.problem else "warning")
        counts=" | ".join(state_text(s)+" "+str(n) for s,n in view.counts if n)
        self.summary.setText((counts+" | " if counts else "")+(size_text(view.bytes_done) if view.bytes_done is not None else "unknown")+" / "+(size_text(view.bytes_total) if view.bytes_total is not None else "unknown")); self._enabled()

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type() in (QEvent.PaletteChange,QEvent.StyleChange) and hasattr(self,'table'):
            self._style_rows()

    def _style_rows(self):
        tokens=current_tokens(self)
        tones={S.RUNNING:'accent',S.RETRY_WAIT:'warning',S.PAUSED:'warning',
               S.FAILED:'danger',S.COMPLETED:'success',S.CANCELLED:'muted',S.QUEUED:'text'}
        for row in range(self.table.rowCount()):
            first=self.table.item(row,0)
            if first is None: continue
            state=S(first.data(Qt.UserRole+1))
            finished=state in (S.COMPLETED,S.FAILED,S.CANCELLED)
            for column in range(6):
                cell=self.table.item(row,column)
                cell.setBackground(QBrush(QColor(tokens['surface' if finished else 'card'])))
                cell.setForeground(QBrush(QColor(tokens[tones[state] if column==2 else 'text'])))
                font=cell.font(); font.setBold(column==2 and state==S.RUNNING); cell.setFont(font)

    def _message(self,text):
        text=_safe(text); self.message.setText(text); self.message.setToolTip(text)

    def _start(self):
        self.failed=False
        self.ack.clear()
        if self.start.isEnabled(): self.bridge.start_queue(); self._message("Starting queue...")
        else: self._message("Nothing to run")

    def _control(self, action):
        if not self.buttons[action].isEnabled(): return
        self.results=[]
        self.failed=False
        self.ack.clear()
        self._message("")
        self.token+=1; ids=self.selected_ids(); self.waiting=set(ids); self._enabled(); self.bridge.control(self.token,action,ids)

    @Slot(object)
    def _result(self, result):
        if self._closed or result.token!=self.token or result.job_id not in self.waiting: return
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
        set_tone(self.message,"error" if self.failed else "normal")
        self._message(text); self._enabled()

    @Slot(object)
    def _run(self, event):
        if self._closed or self.failed: return
        self.ack.clear()
        set_tone(self.message,"error" if event.kind=="error" else "warning" if event.kind=="blocked" else "normal")
        self._message({"started":"Queue started", "finished":"Queue run finished", "blocked":"Another CoveArchive process owns the queue lock", "error":"Queue run failed: "+_error(event.code)}.get(event.kind,"Queue updated"))
