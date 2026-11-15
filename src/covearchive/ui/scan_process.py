"""Asynchronous hard-deadline supervision using a killable process per scan."""

from dataclasses import dataclass, field, replace
import os
import signal
from pathlib import Path
import sys
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from covearchive.scan_worker import MAX_RESPONSE_BYTES, ProtocolError, ScanFailure, decode_response, encode_request

DEFAULT_DEADLINE_MS = 180_000
KILL_REAP_MS = 5_000
ENV_KEYS = frozenset("SYSTEMROOT SYSTEMDRIVE WINDIR TEMP TMP HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY "
    "http_proxy https_proxy all_proxy no_proxy REQUESTS_CA_BUNDLE SSL_CERT_FILE SSL_CERT_DIR".split())


def worker_command(launcher):
    executable = str(Path(sys.executable).resolve())
    if getattr(sys, "frozen", False):
        return [executable, "--scan-worker"]
    windowless = Path(executable).with_name("pythonw.exe")
    if sys.platform == "win32" and windowless.is_file(): executable = str(windowless.resolve())
    return [executable, "-I", "-B", str(Path(launcher).resolve()), "--scan-worker"]


@dataclass
class Job:
    request_id: str
    process: QProcess
    deadline: QTimer
    reaper: QTimer
    started: float = field(default_factory=time.monotonic)
    output: bytearray = field(default_factory=bytearray)
    diagnostics: bytearray = field(default_factory=bytearray)
    reason: str | None = None
    failure: ScanFailure | None = None


class ScanController(QObject):
    state_changed = Signal(str, str)
    terminal = Signal(str, str, object)

    def __init__(self, launcher=None, *, command_factory=worker_command,
                 deadline_ms=DEFAULT_DEADLINE_MS, parent=None):
        super().__init__(parent)
        self.launcher, self.command_factory, self.deadline_ms = launcher, command_factory, deadline_ms
        self.job = None

    def start(self, url):
        if self.job is not None:
            return False
        request_id = uuid4().hex
        request = encode_request(request_id, url)
        process = QProcess(self)
        if sys.platform == "linux":
            # Qt's Unix CreateNewSession is the start_new_session equivalent.
            process.setUnixProcessParameters(QProcess.UnixProcessFlag.CreateNewSession)
        env = QProcessEnvironment()
        for key in ENV_KEYS:
            if key in os.environ: env.insert(key, os.environ[key])
        process.setProcessEnvironment(env)
        modifier = getattr(process, "setCreateProcessArgumentsModifier", None)
        if sys.platform == "win32" and modifier is not None:
            def no_window(arguments): arguments.flags |= 0x08000000
            modifier(no_window)
        command = self.command_factory(self.launcher)
        process.setProgram(command[0]); process.setArguments(command[1:])
        job = Job(request_id, process, QTimer(self), QTimer(self))
        self.job = job
        job.deadline.setSingleShot(True); job.reaper.setSingleShot(True)
        job.deadline.timeout.connect(lambda: self._deadline(job))
        job.reaper.timeout.connect(lambda: self._reap_slow(job))
        process.started.connect(lambda: process.write(request) if self.job is job and not job.reason else None)
        process.readyReadStandardOutput.connect(lambda: self._read(job))
        process.readyReadStandardError.connect(lambda: self._read(job))
        process.finished.connect(lambda *args: self._finish(job))
        process.errorOccurred.connect(lambda error: self._error(job, error))
        self.state_changed.emit(request_id, "RUNNING")
        if self.job is not job: return True
        job.deadline.start(self.deadline_ms)
        process.start()
        return True

    def _read(self, job):
        if self.job is not job: return
        chunk = bytes(job.process.readAllStandardOutput())
        if not job.reason:
            if len(job.output) + len(chunk) > MAX_RESPONSE_BYTES:
                job.failure = ScanFailure(job.request_id, "protocol", message="Response too large.")
                self._stop(job, "FAILED")
                if self.job is not job: return
            else: job.output.extend(chunk)
        diagnostic = bytes(job.process.readAllStandardError())
        job.diagnostics.extend(diagnostic)
        del job.diagnostics[:-65536]

    def _deadline(self, job):
        if self.job is not job or job.reason: return
        remaining = self.deadline_ms - int((time.monotonic()-job.started)*1000)
        if remaining > 0:
            job.deadline.start(remaining)
        else: self._stop(job, "TIMED_OUT")

    def _stop(self, job, reason):
        if self.job is not job or job.reason: return
        job.reason = reason; job.deadline.stop(); job.output.clear()
        self.state_changed.emit(job.request_id, "STOPPING")
        if self.job is not job: return
        self._kill(job)
        job.reaper.start(KILL_REAP_MS)
        if job.process.state() == QProcess.NotRunning: self._finish(job)

    def _reap_slow(self, job):
        if self.job is not job: return
        self.state_changed.emit(job.request_id, "STOP_SLOW")
        if self.job is not job: return
        self._kill(job); job.reaper.start(KILL_REAP_MS)

    def _kill(self, job):
        if sys.platform == "linux":
            pid = job.process.processId()
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        job.process.kill()

    def cancel(self):
        if self.job is not None: self._stop(self.job, "CANCELLED")

    def _error(self, job, error):
        if self.job is not job: return
        if error == QProcess.FailedToStart:
            job.failure = ScanFailure(job.request_id, "process", message="Worker could not start.")
            self._finish(job)

    def _finish(self, job):
        if self.job is not job: return
        if job.process.state() != QProcess.NotRunning: return
        self._read(job)
        if self.job is not job: return
        state, result = job.reason, job.failure
        if state is None and result is not None: state = "FAILED"
        if state is None:
            try:
                if job.process.exitStatus() != QProcess.NormalExit or job.process.exitCode() != 0:
                    result = ScanFailure(job.request_id, "process", message="Worker exited unexpectedly.")
                else: result = decode_response(bytes(job.output), job.request_id)
                state = "FAILED" if isinstance(result, ScanFailure) else "COMPLETED"
            except ProtocolError:
                state, result = "FAILED", ScanFailure(job.request_id, "protocol", message="Invalid worker response.")
        for signal in (job.process.started, job.process.readyReadStandardOutput,
                       job.process.readyReadStandardError, job.process.finished, job.process.errorOccurred):
            signal.disconnect()
        for timer in (job.deadline, job.reaper):
            timer.stop(); timer.timeout.disconnect(); timer.deleteLater()
        job.process.deleteLater()
        if state == "FAILED" and isinstance(result, ScanFailure) and result.code in ("process", "protocol"):
            result = replace(result, detail={"stderr": bytes(job.diagnostics[-2048:]).decode("utf-8", errors="replace")})
        self.job = None
        self.terminal.emit(job.request_id, state, result)

    def shutdown(self, timeout_ms=3000):
        job = self.job
        if job is None: return True
        if not job.reason: self._stop(job, "CANCELLED")
        if self.job is job and job.process.state() != QProcess.NotRunning:
            self._kill(job); job.process.waitForFinished(timeout_ms)
        if self.job is job and job.process.state() == QProcess.NotRunning: self._finish(job)
        return self.job is None
