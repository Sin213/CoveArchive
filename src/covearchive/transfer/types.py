"""Pure transfer contracts; completion describes transport, never verification."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from enum import Enum
from datetime import datetime
import re
from urllib.parse import urlsplit


class EngineState(Enum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class TransferState(Enum):
    WAITING = "waiting"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    REMOVED = "removed"


class FailureKind(Enum):
    HTTP_NOT_FOUND = "http_not_found"
    HTTP_RATE_LIMITED = "http_rate_limited"
    HTTP_SERVER_ERROR = "http_server_error"
    HTTP_CLIENT_ERROR = "http_client_error"
    TLS = "tls"
    TIMEOUT = "timeout"
    NETWORK = "network"
    INTERRUPTED = "interrupted"
    DISK_FULL = "disk_full"
    FILE_IO = "file_io"
    TARGET_EXISTS = "target_exists"
    RESUME_UNSUPPORTED = "resume_unsupported"
    SIZE_MISMATCH = "size_mismatch"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    UNKNOWN = "unknown"


class TransferError(Exception):
    def __init__(self, code, *, aria2_code=None, detail=None):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else "transfer_error"
        self.aria2_code = aria2_code if type(aria2_code) is int else None
        self.detail = sanitize_detail(detail) if detail is not None else None
        super().__init__(self.code)


class EngineUnavailableError(TransferError): pass
class EngineStartError(TransferError): pass
class RpcAuthError(TransferError): pass
class RpcTimeoutError(TransferError): pass
class RpcProtocolError(TransferError): pass
class RpcResponseError(TransferError): pass
class EngineExitedError(TransferError): pass
class InvalidJobError(TransferError): pass
class InvalidJobStateError(TransferError): pass
class InvalidRequestError(TransferError): pass
class InvalidSettingsError(TransferError): pass
class TargetExistsError(TransferError): pass
class ResumeNotAllowedError(TransferError): pass
class ShutdownError(TransferError): pass


def validate_output_name(name):
    if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}\.part", name)
            or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", name[:-5], re.I)):
        raise InvalidRequestError("output_name")
    return name


def validate_uri(uri):
    if (not isinstance(uri, str) or not 1 <= len(uri) <= 8192 or not uri.isascii()
            or re.search(r"[\x00-\x20\x7f\\#]|%(?:0[0-9a-f]|1[0-9a-f]|20|7f)", uri, re.I)):
        raise InvalidRequestError("uri")
    try:
        parsed = urlsplit(uri)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.netloc.endswith(":")
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)
                or not re.fullmatch(r"[A-Za-z0-9.:-]+", parsed.hostname)):
            raise ValueError
    except ValueError:
        raise InvalidRequestError("uri") from None
    return uri


def validate_uri_policy(uri, test_only_allow_loopback_http=False):
    validate_uri(uri); parsed = urlsplit(uri)
    if ((test_only_allow_loopback_http and parsed.hostname not in ("127.0.0.1", "::1"))
            or (not test_only_allow_loopback_http and parsed.scheme != "https")): raise InvalidRequestError("uri_policy")
    return uri


def redact_uri(uri):
    try:
        validate_uri(uri)
        parsed = urlsplit(uri)
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        return f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "") + "/..."
    except InvalidRequestError:
        return "<redacted>"


def sanitize_detail(detail):
    if not isinstance(detail, str): return ""
    detail = re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://\S+|[A-Za-z]:[\\/]\S+|(?<!\w)/\S+", "<redacted>", detail)
    return re.sub(r"[\x00-\x1f\x7f]", " ", detail)[:200]


def _integer(value, low, high, error, code):
    if type(value) is not int or not low <= value <= high: raise error(code)


MAX_SPEED = 2**50
MAX_SIZE = 2**63 - 1


@dataclass(frozen=True)
class TransferSettings:
    max_active: int = 2
    connections_per_download: int = 4
    global_speed_limit: int = 0
    connect_timeout: int = 15
    io_timeout: int = 60

    def __post_init__(self):
        for name, low, high in (("max_active", 1, 16), ("connections_per_download", 1, 16),
                ("global_speed_limit", 0, MAX_SPEED), ("connect_timeout", 1, 3600), ("io_timeout", 1, 3600)):
            _integer(getattr(self, name), low, high, InvalidSettingsError, name)


@dataclass(frozen=True)
class DownloadRequest:
    uri: str = field(repr=False)
    output_name: str
    expected_size: int | None = None
    connections: int | None = None
    speed_limit: int | None = None
    resume: bool = False
    validator: str | None = field(default=None, repr=False)

    def __post_init__(self):
        validate_uri(self.uri); validate_output_name(self.output_name)
        for name, low, high in (("expected_size", 0, MAX_SIZE), ("connections", 1, 16), ("speed_limit", 0, MAX_SPEED)):
            if getattr(self, name) is not None: _integer(getattr(self, name), low, high, InvalidRequestError, name)
        if type(self.resume) is not bool or (self.resume and self.expected_size is None):
            raise InvalidRequestError("resume")
        if self.validator is not None:
            value = self.validator
            etag = isinstance(value, str) and re.fullmatch(r'"[\x21\x23-\x7e]{0,128}"', value)
            date = isinstance(value, str) and re.fullmatch(
                r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-3][0-9] (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [0-9]{4} [0-2][0-9]:[0-5][0-9]:[0-5][0-9] GMT", value)
            if date:
                try: datetime.strptime(value, "%a, %d %b %Y %H:%M:%S GMT")
                except ValueError: date = False
            if not (etag or date): raise InvalidRequestError("validator")

    def __repr__(self):
        return f"DownloadRequest(uri={redact_uri(self.uri)!r}, output_name={self.output_name!r}, resume={self.resume!r})"


@dataclass(frozen=True)
class TransferProgress:
    completed_bytes: int = 0
    total_bytes: int | None = None
    speed_bps: int = 0
    connections: int = 0

    def __post_init__(self):
        for name in ("completed_bytes", "speed_bps", "connections"):
            _integer(getattr(self, name), 0, MAX_SIZE, InvalidRequestError, "progress")
        if self.total_bytes is not None:
            _integer(self.total_bytes, self.completed_bytes, MAX_SIZE, InvalidRequestError, "progress")


@dataclass(frozen=True)
class TransferFailure:
    kind: FailureKind
    aria2_code: int | None = None
    http_status: int | None = None
    detail: str = ""

    def __post_init__(self):
        if not isinstance(self.kind, FailureKind): raise InvalidRequestError("failure")
        if self.aria2_code is not None: _integer(self.aria2_code, 0, MAX_SIZE, InvalidRequestError, "failure")
        if self.http_status is not None: _integer(self.http_status, 100, 599, InvalidRequestError, "failure")
        object.__setattr__(self, "detail", sanitize_detail(self.detail))


@dataclass(frozen=True)
class TransferStatus:
    """COMPLETED = transport complete only; no verification is performed."""

    job_id: str
    state: TransferState
    progress: TransferProgress
    failure: TransferFailure | None = None
    verified: bool = False
    publisher_verified: bool = False

    def __post_init__(self):
        if (not isinstance(self.job_id, str) or not re.fullmatch(r"[0-9a-f]{16}", self.job_id)
                or not isinstance(self.state, TransferState) or not isinstance(self.progress, TransferProgress)
                or self.verified is not False or self.publisher_verified is not False
                or (self.state is TransferState.FAILED) != isinstance(self.failure, TransferFailure)
                or (self.failure is not None and self.state is not TransferState.FAILED)
                or (self.state is not TransferState.ACTIVE and self.progress.speed_bps != 0)):
            raise InvalidRequestError("status")


class DownloadEngine(ABC):
    def __enter__(self):
        self.start(); return self

    def __exit__(self, *_):
        self.stop()

    @property
    @abstractmethod
    def state(self): ...

    @abstractmethod
    def start(self): ...

    @abstractmethod
    def submit(self, request): ...

    @abstractmethod
    def status(self, job_id): ...

    @abstractmethod
    def pause(self, job_id): ...

    @abstractmethod
    def resume(self, job_id): ...

    @abstractmethod
    def cancel(self, job_id): ...

    @abstractmethod
    def update_settings(self, settings): ...

    @abstractmethod
    def stop(self): ...


def validate_settings_update(current, new):
    if not isinstance(new, TransferSettings): raise InvalidSettingsError("settings")
    if (current.connect_timeout,current.io_timeout) != (new.connect_timeout,new.io_timeout): raise InvalidSettingsError("restart_required")
    return new


class FakeEngine(DownloadEngine):
    """In-memory scripted transport; no files, hashing, networking, or redirect policy."""

    def __init__(self, settings=None, *, test_only_allow_loopback_http=False):
        self.test_only_allow_loopback_http = test_only_allow_loopback_http
        self.settings = TransferSettings() if settings is None else settings
        if not isinstance(self.settings, TransferSettings): raise InvalidSettingsError("settings")
        self._state, self._jobs, self._requests, self._serial = EngineState.NEW, {}, {}, 0

    @property
    def state(self): return self._state

    def _running(self):
        if self.state is not EngineState.RUNNING: raise InvalidJobStateError("not_running")

    def start(self):
        if self.state not in (EngineState.NEW, EngineState.STOPPED, EngineState.RUNNING):
            raise InvalidJobStateError("not_startable")
        self._state = EngineState.RUNNING

    def _schedule(self):
        active = sum(s.state is TransferState.ACTIVE for s in self._jobs.values())
        for job, status in self._jobs.items():
            if status.state is TransferState.WAITING and active < self.settings.max_active:
                self._jobs[job] = replace(status, state=TransferState.ACTIVE); active += 1

    def submit(self, request):
        self._running()
        if not isinstance(request, DownloadRequest): raise InvalidRequestError("request")
        validate_uri_policy(request.uri, self.test_only_allow_loopback_http); validate_output_name(request.output_name)
        if (request.connections or self.settings.connections_per_download) > self.settings.connections_per_download:
            raise InvalidRequestError("connections")
        live = (TransferState.ACTIVE, TransferState.WAITING, TransferState.PAUSED)
        if any(self._requests[j].output_name.casefold() == request.output_name.casefold()
               and s.state in live for j, s in self._jobs.items()): raise TargetExistsError("duplicate_output")
        self._serial += 1; job = f"{self._serial:016x}"
        self._requests[job] = request
        self._jobs[job] = TransferStatus(job, TransferState.WAITING, TransferProgress(0, request.expected_size))
        self._schedule(); return job

    def status(self, job_id):
        if not isinstance(job_id, str) or job_id not in self._jobs: raise InvalidJobError("unknown_job")
        return self._jobs[job_id]

    def _change(self, job, state, allowed, *, failure=None):
        self._running(); status = self.status(job)
        if status.state not in allowed: raise InvalidJobStateError("job_state")
        self._jobs[job] = replace(status, state=state, failure=failure,
                                  progress=replace(status.progress, speed_bps=0))
        self._schedule(); return self.status(job)

    def pause(self, job_id):
        return self._change(job_id, TransferState.PAUSED, (TransferState.ACTIVE, TransferState.WAITING))

    def resume(self, job_id):
        return self._change(job_id, TransferState.WAITING, (TransferState.PAUSED,))

    def cancel(self, job_id):
        return self._change(job_id, TransferState.REMOVED,
                            (TransferState.ACTIVE, TransferState.WAITING, TransferState.PAUSED))

    def update_settings(self, settings):
        self._running()
        validate_settings_update(self.settings, settings)
        self.settings = settings; self._schedule()

    def stop(self):
        for job, status in self._jobs.items():
            if status.state in (TransferState.ACTIVE, TransferState.WAITING, TransferState.PAUSED):
                self._jobs[job] = replace(status, state=TransferState.REMOVED,
                                          progress=replace(status.progress, speed_bps=0))
        self._state = EngineState.STOPPED

    def advance(self, job_id, completed_bytes, *, total_bytes=None, speed_bps=1):
        self._running(); status = self.status(job_id)
        if status.state is not TransferState.ACTIVE: raise InvalidJobStateError("job_state")
        expected = self._requests[job_id].expected_size
        total = status.progress.total_bytes if total_bytes is None else total_bytes
        _integer(completed_bytes, 0, MAX_SIZE, InvalidRequestError, "progress")
        _integer(speed_bps, 0, MAX_SIZE, InvalidRequestError, "progress")
        if total is not None: _integer(total, 0, MAX_SIZE, InvalidRequestError, "progress")
        if completed_bytes < status.progress.completed_bytes: raise InvalidRequestError("progress_regression")
        if expected is not None and (completed_bytes > expected or total != expected):
            return self.fail(job_id, TransferFailure(FailureKind.SIZE_MISMATCH))
        progress = TransferProgress(completed_bytes, total, speed_bps,
                                    self._requests[job_id].connections or self.settings.connections_per_download)
        self._jobs[job_id] = replace(status, progress=progress)
        return self.status(job_id)

    def complete(self, job_id):
        self._running(); status = self.status(job_id)
        if status.state is not TransferState.ACTIVE: raise InvalidJobStateError("job_state")
        total = status.progress.total_bytes
        if total is None: total = status.progress.completed_bytes
        self._jobs[job_id] = replace(status, progress=TransferProgress(total, total))
        return self._change(job_id, TransferState.COMPLETED, (TransferState.ACTIVE,))

    def fail(self, job_id, failure):
        if not isinstance(failure, TransferFailure): raise InvalidRequestError("failure")
        return self._change(job_id, TransferState.FAILED,
                            (TransferState.ACTIVE, TransferState.WAITING, TransferState.PAUSED), failure=failure)
