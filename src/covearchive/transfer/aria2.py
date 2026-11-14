"""Owned aria2 child. Redirects cannot be restricted; CA-S7 must probe and hash."""

import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

from .rpc import Aria2RpcClient
from .types import (DownloadEngine, EngineState, TransferSettings, EngineUnavailableError,
                    EngineStartError, EngineExitedError, InvalidSettingsError, InvalidJobStateError,
                    RpcProtocolError, RpcTimeoutError, ShutdownError, TransferError)
from .types import (DownloadRequest, TransferStatus, TransferProgress, TransferFailure, TransferState as S, FailureKind as K,
                    InvalidRequestError, InvalidJobError, TargetExistsError, ResumeNotAllowedError, RpcResponseError, MAX_SIZE,
                    validate_output_name, validate_uri_policy, validate_settings_update)

CREATION_FLAGS = 0x208


def map_failure(aria2_code, message, completed, total):
    match = re.search(r"status=([1-5][0-9]{2})(?![0-9])", message)
    http = int(match[1]) if match else {3: 404, 24: 401}.get(aria2_code)
    kind = {3: K.HTTP_NOT_FOUND, 24: K.HTTP_CLIENT_ERROR, 29: K.HTTP_SERVER_ERROR, 2: K.TIMEOUT, 6: K.NETWORK,
        19: K.NETWORK, 8: K.RESUME_UNSUPPORTED, 9: K.DISK_FULL, 13: K.TARGET_EXISTS, 23: K.TOO_MANY_REDIRECTS,
        15: K.FILE_IO, 16: K.FILE_IO, 17: K.FILE_IO, 18: K.FILE_IO}.get(aria2_code, K.UNKNOWN)
    if aria2_code in (22, 29):
        if http == 429: kind = K.HTTP_RATE_LIMITED
        elif http is not None and http >= 500: kind = K.HTTP_SERVER_ERROR
        elif http is not None and http >= 400: kind = K.HTTP_CLIENT_ERROR
    if aria2_code == 1:
        if "SSL/TLS" in message or "WinTLS" in message: kind = K.TLS
        elif "No URI available" in message and total is not None and 0 < completed < total: kind = K.INTERRUPTED
    return TransferFailure(kind, aria2_code, http)


def find_aria2():
    """PATH-trusting convenience; never used implicitly by Aria2Engine."""
    path = shutil.which("aria2c")
    return Path(os.path.realpath(path)) if path else None


def minimal_env(private_dir):
    return {**{k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE") if k in os.environ},
            "TEMP": str(private_dir), "TMP": str(private_dir)}


def build_config(port, secret, settings, parent_pid):
    values = dict.fromkeys(("rpc-listen-all", "rpc-allow-origin-all", "allow-overwrite", "auto-file-renaming", "continue",
        "follow-torrent", "follow-metalink", "remote-time", "enable-dht", "enable-dht6", "bt-enable-lpd",
        "enable-peer-exchange", "show-console-readout", "enable-color", "rpc-save-upload-metadata", "http-accept-gzip",
        "content-disposition-default-utf8"), "false")
    values.update(dict.fromkeys(("enable-rpc", "no-netrc", "check-certificate", "quiet", "keep-unfinished-download-result"), "true"))
    values.update(dict.fromkeys(("all-proxy", "http-proxy", "https-proxy", "ftp-proxy", "no-proxy"), ""))
    values.update({"rpc-listen-port": port, "rpc-secret": secret, "rpc-max-request-size": "1M", "file-allocation": "none",
        "max-tries": 1, "min-tls-version": "TLSv1.2", "console-log-level": "error", "summary-interval": 0,
        "max-download-result": 1000, "min-split-size": "1M", "connect-timeout": settings.connect_timeout,
        "timeout": settings.io_timeout, "max-concurrent-downloads": settings.max_active,
        "max-overall-download-limit": settings.global_speed_limit, "split": settings.connections_per_download,
        "max-connection-per-server": settings.connections_per_download, "stop-with-process": parent_pid})
    return "".join(f"{key}={value}\n" for key, value in values.items())


def _bind(library, name, args, result):
    fn = getattr(library, name); fn.argtypes, fn.restype = args, result
    return fn


def _native():
    import ctypes as C
    from ctypes import wintypes as W
    kernel, advapi = C.WinDLL("kernel32", use_last_error=True), C.WinDLL("advapi32", use_last_error=True)
    _bind(kernel, "LocalFree", [C.c_void_p], C.c_void_p)
    return C, W, kernel, advapi


def _sid_text(pointer):
    C, W, kernel, advapi = _native(); text = W.LPWSTR()
    if not _bind(advapi, "ConvertSidToStringSidW", [C.c_void_p, C.POINTER(W.LPWSTR)], W.BOOL)(pointer, C.byref(text)):
        raise EngineStartError("sid")
    try: return text.value
    finally: kernel.LocalFree(text)


def current_user_sid():
    C, W, kernel, advapi = _native(); token, size = W.HANDLE(), W.DWORD()
    if not _bind(advapi, "OpenProcessToken", [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL)(W.HANDLE(-1), 8, C.byref(token)):
        raise EngineStartError("token")
    try:
        get = _bind(advapi, "GetTokenInformation", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL)
        get(token, 1, None, 0, C.byref(size)); data = C.create_string_buffer(size.value)
        if not get(token, 1, data, size, C.byref(size)): raise EngineStartError("token")
        return _sid_text(C.c_void_p.from_buffer(data).value)
    finally: _bind(kernel, "CloseHandle", [W.HANDLE], W.BOOL)(token)


def _secure(path, sid, *, create=False):
    C, W, kernel, advapi = _native(); descriptor = C.c_void_p()
    sddl = f"O:{sid}D:P(A;;FA;;;{sid})(A;;FA;;;SY)"
    if not _bind(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p], W.BOOL)(sddl, 1, C.byref(descriptor), None):
        raise EngineStartError("acl_build")
    try:
        if create:
            class Attributes(C.Structure):
                _fields_ = [("length", W.DWORD), ("descriptor", C.c_void_p), ("inherit", W.BOOL)]
            attrs = Attributes(C.sizeof(Attributes), descriptor, False)
            ok = _bind(kernel, "CreateDirectoryW", [W.LPCWSTR, C.POINTER(Attributes)], W.BOOL)(str(path), C.byref(attrs))
        else:
            ok = _bind(advapi, "SetFileSecurityW", [W.LPCWSTR, W.DWORD, C.c_void_p], W.BOOL)(str(path), 0x80000005, descriptor)
        if not ok: raise EngineStartError("acl_apply")
    finally: kernel.LocalFree(descriptor)
    if not verify_private_acl(path, sid):
        if create:
            try: os.rmdir(path)
            except OSError: raise ShutdownError("cleanup") from None
        raise EngineStartError("acl_verify")


def create_private_dir(path, sid): _secure(path, sid, create=True)


def verify_private_acl(path, sid):
    C, W, kernel, advapi = _native(); owner, descriptor, text = C.c_void_p(), C.c_void_p(), W.LPWSTR()
    get = _bind(advapi, "GetNamedSecurityInfoW", [W.LPCWSTR, C.c_int, W.DWORD] + [C.c_void_p] * 5, W.DWORD)
    if get(str(path), 1, 5, C.byref(owner), None, None, None, C.byref(descriptor)): return False
    try:
        convert = _bind(advapi, "ConvertSecurityDescriptorToStringSecurityDescriptorW",
                       [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(W.LPWSTR), C.c_void_p], W.BOOL)
        if not convert(descriptor, 1, 4, C.byref(text), None): return False
        dacl = text.value
        return (_sid_text(owner) == sid and bool(re.fullmatch(r"D:P(?:AI)?(?:\([^()]+\)){2}", dacl))
                and set(re.findall(r"\(([^()]+)\)", dacl)) == {f"A;;FA;;;{sid}", "A;;FA;;;SY"})
    finally:
        if text: kernel.LocalFree(text)
        kernel.LocalFree(descriptor)


def listener_pid(port):
    C, W, _, _ = _native(); size = W.DWORD()
    api = _bind(C.WinDLL("iphlpapi"), "GetExtendedTcpTable",
                [C.c_void_p, C.POINTER(W.DWORD), W.BOOL, W.ULONG, C.c_int, W.ULONG], W.DWORD)
    if api(None, C.byref(size), False, socket.AF_INET, 3, 0) != 122: raise EngineStartError("tcp_table")
    data = C.create_string_buffer(size.value)
    if api(data, C.byref(size), False, socket.AF_INET, 3, 0): raise EngineStartError("tcp_table")
    owners = set()
    for offset in range(4, 4 + W.DWORD.from_buffer(data).value * 24, 24):
        row = (W.DWORD * 6).from_buffer(data, offset)
        if row[0] == 2 and socket.inet_ntoa(row[1].to_bytes(4, "little")) in ("127.0.0.1", "0.0.0.0") and socket.ntohs(row[2] & 65535) == port:
            owners.add(row[5])
    return owners


class Aria2Engine(DownloadEngine):
    def __init__(self, work_dir, *, executable, settings=None, test_only_allow_loopback_http=False,
                 port_source=None, start_timeout=5, shutdown_grace=10, rpc_timeout=2):
        self.work_dir, self.executable = Path(work_dir), Path(executable)
        self.settings = TransferSettings() if settings is None else settings
        if (not isinstance(self.settings, TransferSettings) or type(start_timeout) not in (int, float)
                or not 0 < start_timeout <= 60 or type(shutdown_grace) not in (int, float) or not .1 <= shutdown_grace <= 60
                or type(rpc_timeout) not in (int, float) or not 0 < rpc_timeout <= 60): raise InvalidSettingsError("engine_settings")
        self.test_only_allow_loopback_http = test_only_allow_loopback_http
        self.port_source = port_source or (lambda: 49152 + secrets.randbelow(16384))
        self.start_timeout, self.shutdown_grace, self.rpc_timeout = start_timeout, shutdown_grace, rpc_timeout
        self._lock, self._state = threading.RLock(), EngineState.NEW
        self._process = self._private_dir = self._secret = self._rpc = self._exit_code = None
        self.forced_stop = False
        self._jobs, self._failed = {}, {}
        self._pending_remove = set()

    @property
    def state(self):
        with self._lock:
            try: self._check_alive()
            except TransferError: pass
            return self._state

    def _work_directory(self):
        if not self.work_dir.is_absolute(): raise EngineStartError("work_directory")
        try: info = self.work_dir.lstat()
        except OSError: raise EngineStartError("work_directory") from None
        if not stat.S_ISDIR(info.st_mode) or info.st_file_attributes & 0x400: raise EngineStartError("work_directory")

    def _cleanup(self):
        if self._process is not None:
            try:
                if self._process.poll() is None: self.forced_stop = True; self._process.terminate()
                self._process.wait(3)
            except (OSError, subprocess.TimeoutExpired):
                self._state = EngineState.FAILED; raise ShutdownError("shutdown_timeout") from None
            self._process = None
        self._secret = self._rpc = None
        if self._private_dir is not None:
            try: shutil.rmtree(self._private_dir)
            except FileNotFoundError: pass
            except OSError:
                self._state = EngineState.FAILED; raise ShutdownError("cleanup") from None
            self._private_dir = None

    def start(self):
        with self._lock:
            if self._state is EngineState.RUNNING: self._check_alive(); return
            if self._state not in (EngineState.NEW, EngineState.STOPPED, EngineState.FAILED): raise InvalidJobStateError("engine_state")
            self._cleanup()
            self._state = EngineState.STARTING
            try:
                if sys.platform != "win32" or not self.executable.is_absolute() or not self.executable.is_file():
                    raise EngineUnavailableError("executable")
                self._work_directory()
                try:
                    version = subprocess.run([str(self.executable), "--version"], shell=False, stdin=subprocess.DEVNULL,
                        capture_output=True, text=True, timeout=10, env=minimal_env(self.work_dir), creationflags=CREATION_FLAGS)
                except subprocess.TimeoutExpired: raise EngineUnavailableError("version") from None
                match = re.search(r"^aria2 version (1\.37\.[0-9]+)\s*$", version.stdout, re.M)
                if version.returncode or not match or not re.search(r"^Enabled Features:.*\bHTTPS\b", version.stdout, re.M):
                    raise EngineUnavailableError("version")
                self.version, sid = match[1], current_user_sid()
                for _ in range(3):
                    self.port = self.port_source()
                    if type(self.port) is not int or not 1 <= self.port <= 65535 or self.port == 6800: raise EngineStartError("port")
                    private = Path(tempfile.gettempdir()) / ("covearchive-aria2-" + secrets.token_hex(16))
                    try: create_private_dir(private, sid)
                    except ShutdownError: self._private_dir = private; raise
                    self._private_dir = private
                    config = private / "aria2.conf"
                    fd = os.open(config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    try:
                        _secure(config, sid); self._secret = secrets.token_urlsafe(32)
                        with os.fdopen(fd, "w", encoding="utf-8", newline="\n", closefd=False) as stream:
                            stream.write(build_config(self.port, self._secret, self.settings, os.getpid()))
                    finally: os.close(fd)
                    self._process = subprocess.Popen([str(self.executable), "--conf-path=" + str(config)], shell=False,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=CREATION_FLAGS, cwd=private, env=minimal_env(private), close_fds=True)
                    self._rpc = Aria2RpcClient(self.port, self._secret, timeout=self.rpc_timeout)
                    deadline = time.monotonic() + self.start_timeout
                    while time.monotonic() < deadline and self._process.poll() is None:
                        owners = listener_pid(self.port)
                        if owners - {self._process.pid}: break
                        if owners == {self._process.pid}:
                            try:
                                version_reply = self._rpc.call("getVersion")
                                options = self._rpc.call("getGlobalOption")
                                expected = {"conf-path": str(config), "rpc-listen-all": "false", "allow-overwrite": "false",
                                    "auto-file-renaming": "false", "follow-torrent": "false", "follow-metalink": "false",
                                    "file-allocation": "none", "max-concurrent-downloads": str(self.settings.max_active)}
                                if (isinstance(version_reply, dict) and version_reply.get("version") == self.version
                                        and isinstance(options, dict) and all(options.get(k) == v for k, v in expected.items())
                                        and self._process.poll() is None and time.monotonic() < deadline):
                                    self._state = EngineState.RUNNING; self._exit_code = None; self.forced_stop = False
                                    self._jobs.clear(); self._failed.clear(); self._pending_remove.clear(); return
                                break
                            except (RpcProtocolError, RpcTimeoutError): pass
                        time.sleep(.05)
                    self._cleanup()
                raise EngineStartError("readiness")
            except BaseException as error:
                self._state = EngineState.FAILED; self._cleanup()
                if isinstance(error, (EngineUnavailableError, EngineStartError, ShutdownError, KeyboardInterrupt, SystemExit)): raise
                raise EngineStartError("startup") from None

    def _check_alive(self):
        with self._lock:
            try: exit_code = self._process.poll() if self._process is not None else None
            except OSError: self._state = EngineState.FAILED; raise EngineExitedError("liveness") from None
            if exit_code is not None:
                self._exit_code = exit_code; self._state = EngineState.FAILED; self._cleanup()
            if self._state is EngineState.FAILED and self._exit_code is not None:
                error = EngineExitedError("engine_exited", detail=str(self._exit_code)); error.exit_code = self._exit_code
                raise error

    def stop(self):
        with self._lock:
            if self._state is EngineState.STOPPED: return
            self._state = EngineState.STOPPING
            try:
                if self._process is not None and self._process.poll() is None:
                    try: self._rpc.call("shutdown")
                    except TransferError: pass
                    else:
                        try: self._process.wait(self.shutdown_grace)
                        except subprocess.TimeoutExpired: pass
            except OSError: raise ShutdownError("shutdown") from None
            finally:
                self._cleanup(); self._state = EngineState.STOPPED

    def _ready(self, job=None):
        self._check_alive()
        if self._state is not EngineState.RUNNING: raise InvalidJobStateError("not_running")
        if job is not None and (not isinstance(job, str) or job not in self._jobs): raise InvalidJobError("unknown_job")

    def _inspect_output(self, name, resume):
        try: return (self.work_dir / name).lstat()
        except FileNotFoundError: return None
        except OSError:
            kind = ResumeNotAllowedError if resume else TargetExistsError
            raise kind("resume_files" if resume else "target_inaccessible") from None

    def submit(self, request):
        with self._lock:
            self._ready(); self._work_directory()
            if not isinstance(request, DownloadRequest): raise InvalidRequestError("request")
            validate_output_name(request.output_name); validate_uri_policy(request.uri, self.test_only_allow_loopback_http)
            connections = request.connections or self.settings.connections_per_download
            if connections > self.settings.connections_per_download: raise InvalidRequestError("connections")
            if any(r.output_name.casefold() == request.output_name.casefold() and self.status(j).state in (S.WAITING, S.ACTIVE, S.PAUSED)
                   for j, r in self._jobs.items()): raise TargetExistsError("duplicate_output")
            files = [self._inspect_output(name,request.resume) for name in (request.output_name,request.output_name + ".aria2")]
            if not request.resume and any(f is not None for f in files): raise TargetExistsError("target_exists")
            if request.resume and (request.expected_size is None or any(f is None or not stat.S_ISREG(f.st_mode)
                    or f.st_file_attributes & 0x400 or f.st_nlink != 1 for f in files)): raise ResumeNotAllowedError("resume_files")
            options = {"dir": str(self.work_dir), "out": request.output_name, "split": str(connections),
                "max-connection-per-server": str(connections), "max-download-limit": str(request.speed_limit or 0),
                "file-allocation": "none", "allow-overwrite": "false", "auto-file-renaming": "false", "continue": "false"}
            if request.validator is not None: options["header"] = ["If-Range: " + request.validator]
            job = self._rpc.call("addUri", [[request.uri], options])
            if not isinstance(job, str) or not re.fullmatch(r"[0-9a-f]{16}", job) or job in self._jobs: raise RpcProtocolError("rpc_gid")
            self._jobs[job] = request; return job

    def _retry_remove(self, job):
        if job not in self._pending_remove: return
        try: self._rpc.call("remove", [job])
        except RpcResponseError as error:
            if error.aria2_code != 1: return
        except TransferError: return
        self._pending_remove.discard(job)

    def status(self, job_id):
        with self._lock:
            self._ready()
            if not isinstance(job_id, str) or job_id not in self._jobs: raise InvalidJobError("unknown_job")
            if job_id in self._failed: self._retry_remove(job_id); return self._failed[job_id]
            try: raw = self._rpc.call("tellStatus", [job_id, ["gid", "status", "completedLength", "totalLength", "downloadSpeed", "connections", "errorCode", "errorMessage"]])
            except RpcResponseError: raise InvalidJobError("unknown_job") from None
            if not isinstance(raw, dict) or raw.get("gid") != job_id: raise RpcProtocolError("rpc_status")
            states = {"waiting": S.WAITING, "active": S.ACTIVE, "paused": S.PAUSED, "complete": S.COMPLETED, "error": S.FAILED, "removed": S.REMOVED}
            if raw.get("status") not in states or not isinstance(raw.get("errorMessage", ""), str): raise RpcProtocolError("rpc_status")
            numbers = []
            for key in ("completedLength", "totalLength", "downloadSpeed", "connections", "errorCode"):
                value = raw.get(key, "0" if key == "errorCode" else None)
                if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,18}", value) or int(value) > MAX_SIZE: raise RpcProtocolError("rpc_number")
                numbers.append(int(value))
            done, total, speed, connections, code = numbers; state = states[raw["status"]]
            expected = self._jobs[job_id].expected_size
            mismatch = state is not S.REMOVED and expected is not None and (
                (total > 0 and total != expected) or done > expected or (state is S.COMPLETED and total != expected))
            if mismatch:
                state, failure = S.FAILED, TransferFailure(K.SIZE_MISMATCH)
            else: failure = map_failure(code, raw.get("errorMessage", ""), done, total) if state is S.FAILED else None
            if connections > 16 or (total and done > total): raise RpcProtocolError("rpc_number")
            progress = TransferProgress(done, total if total or state is S.COMPLETED else None, speed if state is S.ACTIVE else 0, connections)
            status = TransferStatus(job_id, state, progress, failure)
            if mismatch: self._failed[job_id] = status; self._pending_remove.add(job_id); self._retry_remove(job_id)
            return status

    def _operate(self, job, method, allowed):
        with self._lock:
            self._ready(job)
            if self.status(job).state not in allowed: raise InvalidJobStateError("job_state")
            try: self._rpc.call(method, [job])
            except RpcResponseError: raise InvalidJobError("unknown_job") from None
            return self.status(job)

    def pause(self, job_id): return self._operate(job_id, "pause", (S.WAITING, S.ACTIVE))
    def resume(self, job_id): return self._operate(job_id, "unpause", (S.PAUSED,))
    def cancel(self, job_id): return self._operate(job_id, "remove", (S.WAITING, S.ACTIVE, S.PAUSED))

    def update_settings(self, settings):
        with self._lock:
            self._ready()
            validate_settings_update(self.settings, settings)
            options = {"max-concurrent-downloads": str(settings.max_active), "max-overall-download-limit": str(settings.global_speed_limit)}
            self._rpc.call("changeGlobalOption", [options]); actual = self._rpc.call("getGlobalOption")
            if not isinstance(actual, dict) or any(actual.get(k) != v for k, v in options.items()): raise RpcProtocolError("settings_readback")
            self.settings = settings
