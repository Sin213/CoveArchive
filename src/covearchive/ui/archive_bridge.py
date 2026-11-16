"""Serialized queue commands, immutable results, and no widget access.

Connect receivers with Qt.QueuedConnection. Only the command thread touches
store/runner; opening never starts restored work.
"""
from dataclasses import dataclass, replace
import queue
import re
import threading
import time
from PySide6.QtCore import QObject, Signal, QTimer, Qt, Slot
from covearchive.github.models import Release, ReleaseAsset
from covearchive.github.urls import RepositoryTarget
from covearchive.providers import project_components
from covearchive.queue.models import JobState, QueueError, InvalidTransitionError, QueueSettings, safe_code
from covearchive.queue.runner import QueueRunner, RunSummary
from covearchive.queue.store import QueueStore


@dataclass(frozen=True)
class EnqueueItem:
    release: Release
    asset: ReleaseAsset

@dataclass(frozen=True)
class EnqueueRequest:
    token: int
    repository: RepositoryTarget
    archive_root: str
    items: tuple[EnqueueItem, ...]
    requeue_completed: bool = True
    def __post_init__(self): object.__setattr__(self, "items", tuple(self.items))

@dataclass(frozen=True)
class EnqueueResult:
    token: int
    created: int
    requeued: int
    existing: tuple[tuple[str, JobState], ...]
    job_ids: tuple[str, ...]
    started: bool
    deferred_pending: int
    error: str | None
    startup_error: str | None = None

@dataclass(frozen=True)
class ControlResult:
    token: int
    action: str
    job_id: str
    ok: bool
    code: str | None
    state_after: JobState | None
    stop_pending: str | None
    started: bool = False
    deferred_pending: int = 0
    startup_error: str | None = None

@dataclass(frozen=True)
class RunEvent:
    kind: str
    summary: RunSummary | None = None
    code: str | None = None

@dataclass(frozen=True)
class ShutdownStatus:
    phase: str
    unresolved: tuple[str, ...]
    run_active: bool
    intent_saved: bool = True

@dataclass(frozen=True)
class JobView:
    job_id: str
    repository: str
    tag: str
    asset_name: str
    asset_size: int
    state: JobState
    retry_count: int
    error_code: str | None
    next_attempt_at: float | None
    result_status: str | None
    integrity: str | None
    transferred: bool | None
    owned: bool
    stop_pending: str | None
    bytes_done: int | None
    bytes_total: int | None
    speed_bps: float | None

@dataclass(frozen=True)
class QueueView:
    seq: int
    jobs: tuple[JobView, ...]
    counts: tuple[tuple[JobState, int], ...]
    bytes_done: int
    bytes_total: int
    cooldown_until: float | None
    run_state: str
    problem: str | None
    usable: bool = True


def _text(value):
    return "[redacted]" if re.search(r"://|[?&](?:sig|token|se|sp|sv|sr|X-Amz-[A-Za-z-]+)=", value, re.I) else value


def _code(error): return safe_code(error.code) if isinstance(error, QueueError) else "internal_error"


class ArchiveBridge(QObject):
    view_changed = Signal(object)
    enqueued = Signal(object)
    control_finished = Signal(object)
    run_event = Signal(object)
    shutdown_status = Signal(object)
    _timer_update = Signal()
    _shutdown_ready = Signal()

    def __init__(self, queue_path=None, runner_options=None, parent=None, *, refresh_ms=500,
                 idle_refresh_ms=2000, observation_timeout=None):
        super().__init__(parent)
        self._path = queue_path; self._options = dict(runner_options or {})
        self.refresh_ms = refresh_ms; self.idle_refresh_ms = idle_refresh_ms
        self.observation_timeout = QueueSettings().shutdown_timeout_seconds if observation_timeout is None else observation_timeout
        self._state = threading.RLock(); self._commands = queue.Queue()
        self._flags = dict(is_open=False, run_active=False, shutting_down=False, closed=False)
        self._attempted = False; self._refresh_pending = False; self._inflight = set()
        self._store = None; self._runner = None; self._problem = None
        self._pending = {}; self._seq = 0; self._fatal = False; self._opening = False
        self._last_good = QueueView(0, (), (), 0, 0, None, "idle", None)
        self._intent_saved = True
        self._retiring = False
        self._shutdown_thread = None; self._commands_stopping = False
        self._shutdown_ready.connect(self._finish_shutdown, Qt.QueuedConnection)
        self._run_thread = None; self._rerun = False; self._run_blocked = False; self._run_problem = None
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh)
        self._timer_update.connect(self._tune_timer, Qt.QueuedConnection)
        self._thread = threading.Thread(target=self._work, name="covearchive-commands", daemon=True)

    def _flag(self, name):
        with self._state: return self._flags[name]
    is_open = property(lambda self: self._flag("is_open"))
    run_active = property(lambda self: self._flag("run_active"))
    shutting_down = property(lambda self: self._flag("shutting_down"))
    closed = property(lambda self: self._flag("closed"))

    def open(self):
        with self._state:
            if self._fatal or self._opening or self.is_open or self.closed or self.shutting_down: return
            if not self.timer.isActive(): self.timer.start(self.idle_refresh_ms)
            self._opening = True; self._commands.put(("open", None))
            if not self._attempted:
                self._attempted = True; self._thread.start()

    def refresh(self):
        with self._state:
            if not self._attempted or self._refresh_pending or self.closed or self._opening or self._commands_stopping: return
            self._refresh_pending = True
            if not self.is_open and not self._fatal and not self.shutting_down: self._opening = True
            self._commands.put(("refresh", None))

    def enqueue(self, request):
        with self._state:
            if (not self._flags["is_open"] or self._flags["shutting_down"] or self._flags["closed"]
                    or not isinstance(request, EnqueueRequest) or request.token in self._inflight): return False
            self._inflight.add(request.token); self._commands.put(("enqueue", request)); return True

    def control(self, token, action, job_ids):
        with self._state:
            if self.shutting_down or self.closed: return
            self._commands.put(("control", (token, action, tuple(job_ids))))

    def _unavailable(self, error):
        code = _code(error)
        self._problem = {"corrupt": "Queue file is corrupt; preserve it for repair.",
            "unsupported_version": "Queue file uses a newer unsupported version.",
            "database_busy": "Queue database is busy; retrying.",
            "database_unavailable": "Queue database is unavailable; retrying.",
            "localappdata_unset": "LOCALAPPDATA is unset; queue is unavailable."}.get(code, "Queue unavailable: " + code)
        if code in ("corrupt", "unsupported_version", "localappdata_unset"):
            with self._state:
                self._fatal = True; self._flags["is_open"] = False

    def _publish(self, clear_problem=False):
        self._seq += 1
        if not self.is_open:
            view = replace(self._last_good, seq=self._seq, run_state=self._run_state() if self.shutting_down else "blocked", problem=self._problem, usable=False)
        else:
            snapshot = self._runner.snapshot(); jobs = self._store.list_jobs()
            active = {p.job_id: p for p in snapshot.active}; rows = []; counts = dict.fromkeys(JobState, 0)
            for job in jobs:
                counts[job.state] += 1
                if job.state != JobState.RUNNING or job.job_id not in active: self._pending.pop(job.job_id, None)
                progress = active.get(job.job_id) if job.state == JobState.RUNNING else None
                rows.append(JobView(job.job_id, _text('/'.join(project_components(RepositoryTarget(job.owner,job.repo,job.provider,job.host)))), _text(job.tag_name),
                    _text(job.asset_name), job.asset_size, job.state, job.retry_count,
                    safe_code(job.last_error_code) if job.last_error_code else None, job.next_attempt_at,
                    job.result_status, job.integrity, job.transferred, progress is not None,
                    self._pending.get(job.job_id), progress.bytes_done if progress else None,
                    progress.bytes_total if progress else None, progress.speed_bps if progress else None))
            if clear_problem: self._problem = None
            view = QueueView(self._seq, tuple(rows), tuple(counts.items()), snapshot.bytes_done,
                snapshot.bytes_total, snapshot.cooldown_until, self._run_state(), self._problem or self._run_problem)
            self._last_good = view
        self._emit(self.view_changed, view)

    def _enqueue(self, request):
        if not self.is_open or self.shutting_down: raise QueueError("queue_unavailable")
        rows = self._store.enqueue_many(tuple((request.repository, i.release, i.asset, request.archive_root) for i in request.items),
            requeue_completed=request.requeue_completed)
        changed = tuple(r.job.job_id for r in rows if r.outcome != "existing")
        existing = tuple((r.job.job_id, r.job.state) for r in rows if r.outcome == "existing")
        started, pending, startup_error = self._startup(changed)
        self._emit(self.enqueued, EnqueueResult(request.token, sum(r.outcome == "created" for r in rows),
            sum(r.outcome == "requeued" for r in rows), existing, changed, started, pending, None, startup_error))

    def _control(self, token, action, job_ids):
        results = []; changed = []
        for job_id in job_ids:
            try:
                if action not in ("pause", "resume", "cancel", "retry"): raise QueueError("invalid_action")
                if not self.is_open or self.shutting_down: raise QueueError("queue_unavailable")
                job = getattr(self._runner, action)(job_id)
                if job.state == JobState.RUNNING and action in ("pause", "cancel"):
                    if self._pending.get(job_id) != "cancel": self._pending[job_id] = action
                else: self._pending.pop(job_id, None)
                if action in ("resume", "retry"): changed.append(job_id)
                result = ControlResult(token, action, job_id, True, None, job.state, self._pending.get(job_id))
            except Exception as error:
                if _code(error) not in ("ownership", "transition", "unknown_job", "invalid_action", "queue_unavailable"):
                    self._unavailable(error)
                result = ControlResult(token, action, job_id, False, _code(error), None, None)
            results.append(result)
        started, pending, startup_error = self._startup(tuple(changed))
        for result in results:
            self._emit(self.control_finished, replace(result, started=started, deferred_pending=pending, startup_error=startup_error))

    def _work(self):
        while (command := self._commands.get()) is not None:
            kind, value = command; succeeded = False
            try:
                with self._state:
                    if self._retiring and not self._run_thread.is_alive() and not self.shutting_down:
                        self._runner = QueueRunner(self._store, **self._options)
                        self._retiring = False
                if (kind == "open" or (kind == "refresh" and not self.is_open and not self._fatal)) and not self.shutting_down:
                    self._store = QueueStore(self._path, settings=self._options.get("settings"))
                    self._runner = QueueRunner(self._store, **self._options)
                    with self._state: self._flags["is_open"] = True
                elif kind == "enqueue": self._enqueue(value)
                elif kind == "control": self._control(*value)
                elif kind == "start": self._start_run()
                elif kind == "pause_all": self._pause_all(value[0])
                succeeded = True
            except Exception as error:
                if kind == "enqueue":
                    self._emit(self.enqueued, EnqueueResult(value.token, 0, 0, (), (), False, 0, _code(error)))
                self._unavailable(error)
            finally:
                try: self._publish(clear_problem=succeeded and kind in ("open", "refresh"))
                except Exception as error:
                    self._unavailable(error)
                    self._emit(self.view_changed, replace(self._last_good, seq=self._seq, problem=self._problem,
                        run_state="blocked" if self._fatal else self._run_state(), usable=self.is_open))
                with self._state:
                    if kind in ("open", "refresh"): self._opening = False
                    if kind == "enqueue": self._inflight.discard(value.token)
                    if kind == "refresh": self._refresh_pending = False
                self._emit(self._timer_update, )
                if kind == "pause_all": value[1].put(succeeded)
                if kind == "final": value.set()

    @Slot()
    def _tune_timer(self):
        if self.closed: self.timer.stop()
        else: self.timer.setInterval(self.refresh_ms if self.run_active or self._retiring or self._pending or self.shutting_down else self.idle_refresh_ms)

    def _run_state(self):
        if self.closed: return "closed"
        if self.shutting_down: return "stopping"
        return "running" if self.run_active else "blocked" if self._run_blocked else "idle"

    def start_queue(self):
        with self._state:
            if self.is_open and not self.shutting_down and not self.closed:
                self._commands.put(("start", None))

    def _startup(self, changed):
        try:
            started, pending = self._maybe_start(changed)
            return started, pending, None
        except Exception as error:
            self._unavailable(error)
            return False, 0, _code(error)

    def _maybe_start(self, changed):
        if not changed: return False, 0
        pending = sum(j.state in (JobState.QUEUED, JobState.RETRY_WAIT, JobState.RUNNING)
                      and j.job_id not in changed for j in self._store.list_jobs())
        started = self._start_run() if self.run_active or not pending else False
        return started, 0 if started else pending

    def _start_run(self):
        with self._state:
            if not self.is_open or self.shutting_down or self.closed: return False
            if self._retiring: return False
            if self.run_active: self._rerun = True; return True
            self._flags["run_active"] = True; self._run_blocked = False; self._run_problem = None
            self._run_thread = threading.Thread(target=self._run_loop, name="covearchive-run", daemon=True)
            self._run_thread.start(); self._emit(self._timer_update, ); return True

    def _run_loop(self):
        while True:
            failed = False
            self._emit(self.run_event, RunEvent("started"))
            try:
                summary = self._runner.run_pending(wait_for_retries=True)
                self._emit(self.run_event, RunEvent("finished", summary))
            except Exception as error:
                failed = True; code = _code(error)
                with self._state:
                    self._run_blocked = code == "runner_active"
                    self._retiring = not self._run_blocked
                    self._run_problem = "Another CoveArchive process owns the queue lock" if self._run_blocked else "Queue run failed: " + code
                self._emit(self.run_event, RunEvent("blocked" if code == "runner_active" else "error", code=code))
            if failed and self._retiring:
                while True:
                    try:
                        if not self._runner.shutdown(self.observation_timeout).unresolved: break
                    except Exception: pass
                    self.refresh()
                    time.sleep(0.05)
            with self._state:
                again = self._rerun and not failed and not self.shutting_down
                self._rerun = False
                if not again: self._flags["run_active"] = False
            self.refresh(); self._emit(self._timer_update, )
            if not again: return

    def _emit(self, signal, *args):
        with self._state:
            if not self.closed: signal.emit(*args)

    def begin_shutdown(self, pause_active=True):
        with self._state:
            if self.shutting_down or self.closed: return
            self._flags["shutting_down"] = True
            owned = tuple(j.job_id for j in self._last_good.jobs if j.owned and j.state == JobState.RUNNING)
            self._emit(self.shutdown_status, ShutdownStatus("draining", owned, self.run_active))
            self._shutdown_thread = threading.Thread(target=self._shutdown_worker, args=(pause_active,),
                                                     name="covearchive-shutdown", daemon=True)
            self._shutdown_thread.start()
        self._emit(self._timer_update, )

    def _pause_all(self, pause_active):
        if not pause_active or self._runner is None: return
        # Exclude claims/finishes across pause + stop; only shutdown(0) is allowed on this command thread.
        with self._runner._state:
            for progress in self._runner.snapshot().active:
                try:
                    job = self._runner.pause(progress.job_id)
                    if job.state == JobState.RUNNING and self._pending.get(job.job_id) != "cancel":
                        self._pending[job.job_id] = "pause"
                except InvalidTransitionError: pass
            self._runner.shutdown(0)

    def _shutdown_worker(self, pause_active):
        # FIFO barrier also reconciles an open that was already in flight.
        if self._attempted:
            opened = threading.Event(); self._commands.put(("final", opened))
            while not opened.wait(0.05):
                if not self._thread.is_alive(): break
        deadline = time.monotonic() + self.observation_timeout
        if self._attempted:
            while True:
                reply = queue.Queue(maxsize=1); self._commands.put(("pause_all", (pause_active, reply)))
                try: saved = reply.get(timeout=max(0.001, deadline-time.monotonic()))
                except queue.Empty: saved = False
                if saved: break
                if self._fatal or time.monotonic() >= deadline:
                    self._intent_saved = False; break
                threading.Event().wait(min(0.05, max(0,deadline-time.monotonic())))
        runner = self._runner; previous = None; timeout = self.observation_timeout
        while runner is not None:
            try:
                report = runner.shutdown(timeout)
                run = self._run_thread
                if run is not None: run.join(0.01)
                alive = run is not None and run.is_alive()
                if not report.unresolved and not alive:
                    final = runner.shutdown(0)
                    if not final.unresolved: break
                status = ShutdownStatus("timeout_waiting", report.unresolved, alive, self._intent_saved)
            except Exception:
                status = ShutdownStatus("timeout_waiting", tuple(j.job_id for j in self._last_good.jobs if j.owned), self.run_active, self._intent_saved)
            if status != previous: self._emit(self.shutdown_status, status); previous = status
            timeout = 0.2; threading.Event().wait(0.02)
        with self._state: self._commands_stopping = True
        if self._attempted:
            final_view = threading.Event(); self._commands.put(("final", final_view))
            while not final_view.wait(0.2):
                if not self._thread.is_alive(): break
            self._commands.put(None); self._thread.join()
        self._shutdown_ready.emit()

    @Slot()
    def _finish_shutdown(self):
        if self.closed: return
        # Never wait for a live thread on the GUI thread. Reap only after exit.
        if self._shutdown_thread.is_alive():
            QTimer.singleShot(10, self._finish_shutdown); return
        self._shutdown_thread.join(0)
        self.timer.stop()
        self._emit(self.view_changed, replace(self._last_good, seq=self._seq + 1, run_state="closed"))
        with self._state:
            self._flags.update(closed=True, is_open=False, run_active=False)
        self.shutdown_status.emit(ShutdownStatus("done", (), False, self._intent_saved))
