"""QProcess supervision for one anonymous Discover request at a time."""

from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import signal
import sys
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from covearchive.discover.worker import (
    DEFAULT_DEADLINE_SECONDS, MAX_RESPONSE_BYTES, DiscoverFailure, ProtocolError,
    decode_response, encode_request,
)
from .scan_process import ENV_KEYS, KILL_REAP_MS

DEFAULT_DEADLINE_MS = DEFAULT_DEADLINE_SECONDS * 1000


def worker_command(launcher):
    executable = str(Path(sys.executable).resolve())
    if getattr(sys, "frozen", False):
        return [executable, "--discover-worker"]
    windowless = Path(executable).with_name("pythonw.exe")
    if sys.platform == "win32" and windowless.is_file():
        executable = str(windowless.resolve())
    return [executable, "-I", "-B", str(Path(launcher).resolve()), "--discover-worker"]


@dataclass(repr=False)
class Job:
    request_id: str
    process: QProcess
    deadline: QTimer
    reaper: QTimer
    started: float = field(default_factory=time.monotonic)
    output: bytearray = field(default_factory=bytearray, repr=False)
    reason: str | None = None
    failure: DiscoverFailure | None = None
    pid: int = 0
    retired: bool = False
    teardown: object = field(default=None, repr=False)


def _destroy_job(job):
    """QObject emits destroyed before deleting children; never capture its owner."""
    if job.retired:
        return
    job.retired = True
    # Disconnect Python callbacks before kill/wait can deliver process signals.
    # The RuntimeError guard also covers children explicitly deleted by a caller.
    try:
        for process_signal in (job.process.started, job.process.readyReadStandardOutput,
                               job.process.readyReadStandardError, job.process.finished, job.process.errorOccurred):
            process_signal.disconnect()
        for timer in (job.deadline, job.reaper):
            timer.stop()
            timer.timeout.disconnect()
        pid = job.pid or job.process.processId()
    except RuntimeError:
        pid = job.pid
    if sys.platform == "linux" and pid:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        if job.process.state() != QProcess.NotRunning:
            job.process.kill()
            job.process.waitForFinished(KILL_REAP_MS)
    except RuntimeError:
        pass


class DiscoverController(QObject):
    state_changed = Signal(str, str)
    terminal = Signal(str, str, object)

    def __init__(self, launcher=None, *, command_factory=worker_command,
                 deadline_ms=DEFAULT_DEADLINE_MS, parent=None):
        super().__init__(parent)
        self.launcher, self.command_factory, self.deadline_ms = launcher, command_factory, deadline_ms
        self.job = None
        self._shutting_down = False

    @property
    def active_request_id(self):
        return self.job.request_id if self.job is not None and not self.job.retired else None

    def start(self, text, kind="all", sort="relevance", page=1, per_page=30):
        if self._shutting_down or self.job is not None:
            return False
        request_id = uuid4().hex
        try:
            request = encode_request(request_id, text, kind, sort, page, per_page)
        except ProtocolError:
            return False
        process = QProcess(self)
        if sys.platform == "linux":
            process.setUnixProcessParameters(QProcess.UnixProcessFlag.CreateNewSession)
        env = QProcessEnvironment()
        for key in ENV_KEYS:
            if key in os.environ:
                env.insert(key, os.environ[key])
        if getattr(sys, "frozen", False):
            for key in ("_PYI_ARCHIVE_FILE", "_PYI_APPLICATION_HOME_DIR", "_PYI_PARENT_PROCESS_LEVEL", "_PYI_SPLASH_IPC"):
                if key in os.environ:
                    env.insert(key, os.environ[key])
        process.setProcessEnvironment(env)
        modifier = getattr(process, "setCreateProcessArgumentsModifier", None)
        if sys.platform == "win32" and modifier is not None:
            def no_window(arguments):
                arguments.flags |= 0x08000000
            modifier(no_window)
        command = self.command_factory(self.launcher)
        process.setProgram(command[0])
        process.setArguments(command[1:])
        job = Job(request_id, process, QTimer(self), QTimer(self))
        self.job = job
        job.teardown = lambda *_: _destroy_job(job)
        self.destroyed.connect(job.teardown)
        job.deadline.setSingleShot(True)
        job.reaper.setSingleShot(True)
        job.deadline.timeout.connect(lambda: self._deadline(job))
        job.reaper.timeout.connect(lambda: self._reap_slow(job))
        process.started.connect(lambda: setattr(job, "pid", process.processId()))
        process.started.connect(lambda: process.write(request) if self.job is job and not job.reason else None)
        process.readyReadStandardOutput.connect(lambda: self._read(job))
        process.readyReadStandardError.connect(lambda: self._read(job))
        process.finished.connect(lambda *args: self._finish(job))
        process.errorOccurred.connect(lambda error: self._error(job, error))
        self.state_changed.emit(request_id, "RUNNING")
        if self.job is not job or job.retired:
            return True
        job.deadline.start(self.deadline_ms)
        process.start()
        return True

    def _read(self, job):
        if self.job is not job or job.retired:
            return
        chunk = bytes(job.process.readAllStandardOutput())
        if not job.reason:
            if len(job.output) + len(chunk) > MAX_RESPONSE_BYTES:
                job.failure = DiscoverFailure(job.request_id, "protocol", message="Response too large.")
                self._stop(job, "FAILED")
                if self.job is not job or job.retired:
                    return
            else:
                job.output.extend(chunk)
        # Drain without retaining any untrusted text, even identifier-like text.
        job.process.setReadChannel(QProcess.StandardError)
        while job.process.bytesAvailable():
            job.process.read(65536)
        job.process.setReadChannel(QProcess.StandardOutput)

    def _deadline(self, job):
        if self.job is not job or job.retired or job.reason:
            return
        remaining = self.deadline_ms - int((time.monotonic() - job.started) * 1000)
        if remaining > 0:
            job.deadline.start(remaining)
        else:
            self._stop(job, "TIMED_OUT")

    def _stop(self, job, reason):
        if self.job is not job or job.retired or job.reason:
            return
        job.reason = reason
        job.deadline.stop()
        job.output.clear()
        self.state_changed.emit(job.request_id, "STOPPING")
        if self.job is not job or job.retired:
            return
        self._kill(job)
        job.reaper.start(KILL_REAP_MS)
        if job.process.state() == QProcess.NotRunning:
            self._finish(job)

    def _reap_slow(self, job):
        if self.job is not job or job.retired:
            return
        self.state_changed.emit(job.request_id, "STOP_SLOW")
        if self.job is not job or job.retired:
            return
        self._kill(job)
        job.reaper.start(KILL_REAP_MS)

    def _kill(self, job):
        if job.retired:
            return
        if sys.platform == "linux":
            pid = job.process.processId()
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        job.process.kill()

    def cancel(self):
        if self.job is not None:
            self._stop(self.job, "CANCELLED")

    def _error(self, job, error):
        if self.job is not job or job.retired:
            return
        if error == QProcess.FailedToStart:
            job.failure = DiscoverFailure(job.request_id, "process", message="Worker could not start.")
            self._finish(job)

    def _finish(self, job):
        if self.job is not job or job.retired or job.process.state() != QProcess.NotRunning:
            return
        self._read(job)
        if self.job is not job or job.retired:
            return
        state, result = job.reason, job.failure
        if state is None and result is not None:
            state = "FAILED"
        if state is None:
            try:
                if job.process.exitStatus() != QProcess.NormalExit or job.process.exitCode() != 0:
                    result = DiscoverFailure(job.request_id, "process", message="Worker exited unexpectedly.")
                else:
                    result = decode_response(bytes(job.output), job.request_id)
                state = "FAILED" if isinstance(result, DiscoverFailure) else "COMPLETED"
            except ProtocolError:
                state, result = "FAILED", DiscoverFailure(job.request_id, "protocol", message="Invalid worker response.")
        for process_signal in (job.process.started, job.process.readyReadStandardOutput,
                               job.process.readyReadStandardError, job.process.finished, job.process.errorOccurred):
            process_signal.disconnect()
        for timer in (job.deadline, job.reaper):
            timer.stop()
            timer.timeout.disconnect()
            timer.deleteLater()
        self.destroyed.disconnect(job.teardown)
        job.retired = True
        job.process.deleteLater()
        if state == "FAILED" and isinstance(result, DiscoverFailure) and result.code in ("process", "protocol"):
            result = replace(result, detail={})
        self.job = None
        self.terminal.emit(job.request_id, state, result)

    def shutdown(self, timeout_ms=3000):
        previous = self._shutting_down
        self._shutting_down = True
        try:
            # The guard rejects starts in synchronous signal slots. Also handle
            # a replacement defensively, without allowing an unbounded loop.
            for _ in range(3):
                job = self.job
                if job is None:
                    break
                if not job.retired and not job.reason:
                    self._stop(job, "CANCELLED")
                if self.job is job and not job.retired and job.process.state() != QProcess.NotRunning:
                    self._kill(job)
                    job.process.waitForFinished(timeout_ms)
                if self.job is job and not job.retired and job.process.state() == QProcess.NotRunning:
                    self._finish(job)
                if self.job is job:
                    if job.retired:
                        self.job = None
                    else:
                        break
            return self.job is None
        finally:
            # Nested shutdown calls must preserve their caller's guard.
            self._shutting_down = previous
