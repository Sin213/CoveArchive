"""CA-S6 native startup, ownership, private ACLs and configuration evidence."""

import ast
import http.client
import http.server
import hashlib
import json
import os
import queue
from pathlib import Path
import secrets
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import pytest

from covearchive.transfer import aria2 as a
from covearchive.transfer.rpc import Aria2RpcClient
from covearchive.transfer.types import EngineState, EngineUnavailableError, EngineStartError, EngineExitedError, RpcAuthError, RpcTimeoutError, TransferSettings
from covearchive.transfer.types import DownloadRequest, TransferState as S, FailureKind as K, InvalidRequestError, InvalidJobError, InvalidJobStateError, InvalidSettingsError, RpcProtocolError, TargetExistsError, ResumeNotAllowedError
from covearchive.transfer.types import ShutdownError, FakeEngine
from dataclasses import replace

def raw_rpc(engine, method="getVersion", *params):
    connection = http.client.HTTPConnection("127.0.0.1", engine.port, timeout=2)
    try:
        connection.request("POST", "/jsonrpc", json.dumps({"jsonrpc": "2.0", "id": "raw",
            "method": "aria2." + method, "params": ["token:" + engine._secret, *params]}), {"Content-Type": "application/json"})
        response = connection.getresponse(); return response.status, json.loads(response.read())
    finally: connection.close()

def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); return sock.getsockname()[1]

def process_gone(process):
    assert process.wait(timeout=3) is not None
    # Popen.wait uses the original native process handle, including after PID reuse.
    assert process.poll() is not None

@pytest.fixture
def executable():
    override = os.environ.get("COVEARCHIVE_TEST_ARIA2")
    path = Path(override) if override else a.find_aria2()
    if override: assert path.is_absolute()
    if path is None: pytest.skip("aria2c not installed: NOT VERIFIED")
    return path

@pytest.fixture
def engine(tmp_path, executable):
    value = a.Aria2Engine(tmp_path, executable=executable)
    try: yield value
    finally:
        if value._process is not None and value._process.poll() is None: value._process.terminate()
        value.stop()

@pytest.mark.parametrize("path", ["missing.exe", "C:/definitely-missing-aria2.exe"])
def test_E01_missing_executable(tmp_path, path):
    engine = a.Aria2Engine(tmp_path, executable=path)
    with pytest.raises(EngineUnavailableError): engine.start()
    assert engine.state is EngineState.FAILED

@pytest.mark.parametrize("failure", ["version", "timeout"])
def test_E02_version_and_rejection(engine, monkeypatch, failure):
    run = a.subprocess.run
    def wrong(*args, **kwargs):
        assert kwargs["timeout"] == 10
        if failure == "timeout": raise subprocess.TimeoutExpired("version", 10)
        result = run(*args, **kwargs); result.stdout = "aria2 version 1.36.0\nEnabled Features: HTTPS\n"; return result
    monkeypatch.setattr(a.subprocess, "run", wrong)
    with pytest.raises(EngineUnavailableError) as caught: engine.start()
    assert caught.value.code == "version"

def test_E03_E04_E05_E10_R09_real_start_auth_and_listeners(engine):
    engine.start(); process, private = engine._process, engine._private_dir
    assert engine.state is EngineState.RUNNING and a.listener_pid(engine.port) == {process.pid}
    code, reply = raw_rpc(engine)
    assert code == 200 and reply["result"]["version"] == engine.version and engine.version.startswith("1.37.")
    options = raw_rpc(engine, "getGlobalOption")[1]["result"]
    assert options["conf-path"] == str(private / "aria2.conf")
    with pytest.raises(RpcAuthError): Aria2RpcClient(engine.port, secrets.token_urlsafe(32)).call("getVersion")
    result = subprocess.run([r"C:\Windows\System32\NETSTAT.EXE", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    listeners = [line.split()[1] for line in result.stdout.splitlines()
                 if "LISTENING" in line and line.split()[-1] == str(process.pid)]
    assert listeners and all(x.startswith(("127.0.0.1:", "[::1]:")) for x in listeners)
    assert a.verify_private_acl(private, a.current_user_sid())
    assert a.verify_private_acl(private / "aria2.conf", a.current_user_sid())
    port = engine.port; engine.stop(); engine.stop(); process_gone(process)
    assert not engine.forced_stop and not private.exists() and a.listener_pid(port) == set()

def test_E06_busy_ipv4_not_adopted(engine):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0)); busy.listen()
        port = busy.getsockname()[1]; choices = iter((port, free_port(), free_port()))
        engine.port_source = lambda: next(choices)
        engine.start(); assert engine.port != port
        assert a.listener_pid(port) == {os.getpid()}

def test_E07_foreign_real_aria2_untouched(tmp_path, executable):
    foreign = a.Aria2Engine(tmp_path, executable=executable)
    ours = a.Aria2Engine(tmp_path, executable=executable)
    try:
        foreign.start(); pid = foreign._process.pid
        choices = iter((foreign.port, free_port(), free_port())); ours.port_source = lambda: next(choices)
        ours.start()
        assert ours.port != foreign.port and raw_rpc(foreign)[0] == 200
        assert foreign._process.pid == pid and foreign._process.poll() is None
    finally:
        for item in (ours, foreign):
            if item._process is not None and item._process.poll() is None: item._process.terminate()
            item.stop()

def test_E08_exit_during_start_cleanup(engine, monkeypatch):
    popen = a.subprocess.Popen; children, directories = [], []
    def short_lived(args, **kwargs):
        if "--version" in args: return popen(args, **kwargs)
        directories.append(Path(kwargs["cwd"]))
        child = popen([str(engine.executable), "--version"], **kwargs); children.append(child); return child
    monkeypatch.setattr(a.subprocess, "Popen", short_lived)
    with pytest.raises(EngineStartError): engine.start()
    assert engine.state is EngineState.FAILED and children
    for child in children: process_gone(child)
    assert all(not path.exists() for path in directories)

def test_security_acl_rejects_everyone(tmp_path):
    private = tmp_path / "private"; sid = a.current_user_sid()
    a.create_private_dir(private, sid)
    assert a.verify_private_acl(private, sid)
    result = subprocess.run([r"C:\Windows\System32\icacls.exe", str(private), "/grant", "*S-1-1-0:(OI)(CI)R"],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert a.verify_private_acl(private, sid) is False

def test_security_config_env_and_spawn(engine, monkeypatch):
    expected = set(("enable-rpc rpc-listen-all rpc-allow-origin-all rpc-listen-port rpc-secret rpc-max-request-size allow-overwrite auto-file-renaming continue "
        "follow-torrent follow-metalink file-allocation max-tries remote-time no-netrc check-certificate min-tls-version enable-dht enable-dht6 bt-enable-lpd "
        "enable-peer-exchange console-log-level quiet summary-interval show-console-readout enable-color keep-unfinished-download-result max-download-result rpc-save-upload-metadata "
        "http-accept-gzip content-disposition-default-utf8 min-split-size connect-timeout timeout max-concurrent-downloads max-overall-download-limit split max-connection-per-server "
        "stop-with-process all-proxy http-proxy https-proxy ftp-proxy no-proxy").split())
    config = dict(line.split("=", 1) for line in a.build_config(49153, "TEST_ONLY", TransferSettings(), os.getpid()).splitlines())
    assert set(config) == expected
    for key in ("allow-overwrite", "auto-file-renaming", "continue", "follow-torrent", "follow-metalink"): assert config[key] == "false"
    assert config["file-allocation"] == "none" and config["stop-with-process"] == str(os.getpid())
    env = a.minimal_env("X:/private")
    assert set(env) <= {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP"}
    assert env["TEMP"] == env["TMP"] == "X:/private"
    popen = a.subprocess.Popen; spawns = []
    def record(args, **kwargs):
        if "--version" not in args: spawns.append((args, kwargs))
        return popen(args, **kwargs)
    monkeypatch.setattr(a.subprocess, "Popen", record)
    engine.start(); args, kwargs = spawns[-1]
    assert len(args) == 2 and args[1].startswith("--conf-path=") and bool(engine._secret not in repr(args))
    assert kwargs["creationflags"] == 0x208 and kwargs["shell"] is False and kwargs["close_fds"] is True
    assert all(kwargs[k] == subprocess.DEVNULL for k in ("stdin", "stdout", "stderr"))
    assert kwargs["env"] == a.minimal_env(engine._private_dir)

def test_security_transfer_AST():
    for path in (Path(__file__).parents[1] / "src/covearchive/transfer").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                assert not any(n.startswith(("covearchive.ui", "covearchive.github", "covearchive.archive", "requests"))
                    and not (path.name == "resolve.py" and n in ("requests", "covearchive.github.client",
                        "covearchive.github.models", "covearchive.github.urls")) for n in names)
            if isinstance(node, ast.Call):
                assert not any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords)
                assert not (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "os" and node.func.attr == "system")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value.casefold() not in ("cmd", "cmd.exe", "powershell", "powershell.exe")

@pytest.fixture(scope="module", autouse=True)
def child_guard():
    """E12: record held Popen objects, assert no module-spawned child survives."""
    original, children = a.subprocess.Popen, []
    def record(*args, **kwargs):
        child = original(*args, **kwargs); children.append(child); return child
    a.subprocess.Popen = record
    try: yield
    finally:
        a.subprocess.Popen = original
        alive = [child for child in children if child.poll() is None]
        for child in alive: child.terminate(); child.wait(3)
        assert not alive

def test_E06_multi_owner_rule(engine, monkeypatch):
    real_lookup, rejected = a.listener_pid, []
    def conflicting(port):
        owners = real_lookup(port)
        if not rejected: rejected.append(port)
        return {engine._process.pid, os.getpid()} if port == rejected[0] else owners
    monkeypatch.setattr(a, "listener_pid", conflicting)
    engine.start(); assert engine.port != rejected[0]

def test_E06_listener_table_all_owners(monkeypatch):
    import ctypes as C
    from ctypes import wintypes as W
    port = 49153; row = lambda host, pid: (2, int.from_bytes(socket.inet_aton(host), "little"), socket.htons(port), 0, 0, pid)
    table = (W.DWORD * 19)(3, *row("127.0.0.1", 111), *row("0.0.0.0", 222), *row("127.0.0.2", 333))
    original = a._bind
    def api(data, size, *args):
        C.cast(size, C.POINTER(W.DWORD))[0] = C.sizeof(table)
        if data is None: return 122
        C.memmove(data, table, C.sizeof(table)); return 0
    monkeypatch.setattr(a, "_bind", lambda lib, name, args, result: api if name == "GetExtendedTcpTable" else original(lib, name, args, result))
    assert a.listener_pid(port) == {111, 222}

def test_E11_F06_suspended_forced_stop(engine):
    import ctypes as C
    engine.rpc_timeout = .15; engine.start()
    child, private = engine._process, engine._private_dir
    suspend = C.WinDLL("ntdll").NtSuspendProcess; suspend.argtypes = [C.c_void_p]; suspend.restype = C.c_long
    assert suspend(int(child._handle)) == 0
    started = time.monotonic()
    with pytest.raises(RpcTimeoutError): engine._rpc.call("getVersion")
    assert time.monotonic() - started < engine.rpc_timeout + 1
    started = time.monotonic(); engine.stop(); process_gone(child)
    assert time.monotonic() - started < 3 and engine.forced_stop and not private.exists()

def test_E12_concurrent_stop(engine):
    assert a.Aria2Engine(engine.work_dir, executable=engine.executable, shutdown_grace=.1).shutdown_grace == .1
    engine.shutdown_grace = .1; engine.start(); child, private, failures = engine._process, engine._private_dir, []
    def stop():
        try: engine.stop()
        except Exception as error: failures.append(type(error).__name__)
    threads = [threading.Thread(target=stop) for _ in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(4)
    assert not failures and all(not t.is_alive() for t in threads)
    engine.stop(); process_gone(child); assert not private.exists()

def test_F08_R08_engine_context_diagnostics(engine, caplog, monkeypatch):
    with caplog.at_level("DEBUG"):
        with pytest.raises(ValueError):
            with engine:
                child, private, secret = engine._process, engine._private_dir, engine._secret
                representation = str(engine) + repr(engine)
                raise ValueError("fixture")
        process_gone(child); assert not private.exists()
        monkeypatch.setattr(a, "listener_pid", lambda port: {os.getpid()})
        with pytest.raises(EngineStartError) as caught: engine.start()
    diagnostics = caplog.text + representation + str(caught.value) + repr(caught.value)
    assert bool(secret not in diagnostics) and bool(str(private) not in diagnostics)

@pytest.fixture
def http_fixture():
    class Requests(list):
        active = peak = 0
        mode = "ok"
        lock = threading.Lock()
    stopped, requests = threading.Event(), Requests()
    body = bytes(range(256)) * 4096
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            with requests.lock: requests.active += 1; requests.peak = max(requests.peak, requests.active)
            try: self.send_body()
            finally:
                with requests.lock: requests.active -= 1
        def send_body(self):
            requests.append((self.path, dict(self.headers)))
            if self.path == "/res": return self.send_resume()
            route = self.path.split("?", 1)[0]
            if route in ("/404", "/429", "/503"):
                self.send_response(int(route[1:])); self.send_header("Content-Length", "0"); self.end_headers(); return
            unknown, slow = self.path == "/unknown", self.path in ("/slow", "/unknown", "/lie")
            large = self.path in ("/large", "/norange")
            content = body * 8 if large else body[:200000] if self.path == "/bandwidth" else body if slow else body[:32768]
            start, end = 0, len(content) - 1
            range_header = self.headers.get("Range") if self.path != "/norange" else None
            if range_header:
                first, _, last = range_header[6:].partition("-"); start = int(first); end = min(int(last) if last else end, end)
            self.send_response(206 if range_header or self.path == "/lie" else 200)
            if self.path == "/disposition": self.send_header("Content-Disposition", 'attachment; filename="..\\evil.exe"')
            if self.path == "/lie": self.send_header("Content-Range", "bytes 5-9/999999999"); content = b"wrong"; start, end = 0, 4
            elif range_header: self.send_header("Content-Range", f"bytes {start}-{end}/{len(content)}")
            if self.path != "/norange": self.send_header("Accept-Ranges", "bytes")
            if not unknown: self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Connection", "close"); self.end_headers()
            try:
                step = 65536 if large else 4096
                for offset in range(start, end + 1, step):
                    if stopped.is_set(): return
                    self.wfile.write(content[offset:min(offset + step, end + 1)]); self.wfile.flush()
                    if (slow or large) and stopped.wait(.015): return
            except OSError: pass
        def send_resume(self):
            mode = requests.mode; content = bytes((b + 5) % 256 for b in body[:300000]) if mode == "changed" else body[:300000]
            etag = '"v1"' if mode == "changed" else '"v0"'; header = self.headers.get("Range")
            start, end = int(header[6:].partition("-")[0]) if header else 0, 299999
            ranged = bool(header) and mode != "ignore" and self.headers.get("If-Range", etag) == etag
            if mode == "badcr": start = 0
            if mode == "shortcr": end -= 10
            self.send_response(206 if ranged else 200)
            if ranged: self.send_header("Content-Range", f"bytes {start}-{end}/300000")
            chunk = content[start:end + 1] if ranged else content
            self.send_header("ETag", etag); self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(chunk))); self.end_headers()
            try:
                self.wfile.write(chunk[:100000] if mode == "drop_first" else chunk); self.wfile.flush()
                if mode == "drop_first": self.connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler); server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}); thread.start()
    try: yield f"http://127.0.0.1:{server.server_port}", body, requests
    finally: stopped.set(); server.shutdown(); server.server_close(); thread.join(2); assert not thread.is_alive()

def wait_for(engine, job, states, deadline=8):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        status = engine.status(job)
        if status.state in states: return status
        threading.Event().wait(.025)
    pytest.fail(f"State deadline: {status.state}")

@pytest.mark.parametrize("route", ["/data", "/unknown", "/disposition"])
def test_T01_T02_T03_complete(engine, http_fixture, route):
    url, body, _ = http_fixture; engine.test_only_allow_loopback_http = True; engine.start()
    before = set(engine.work_dir.parent.iterdir())
    job = engine.submit(DownloadRequest(url + route, "a.part"))
    initial = engine.status(job)
    assert initial.progress.total_bytes in (None, 32768, len(body))
    status = wait_for(engine, job, (S.COMPLETED, S.FAILED))
    assert status.state is S.COMPLETED and not status.verified and not status.publisher_verified
    expected = body if route == "/unknown" else body[:32768]
    assert (engine.work_dir / "a.part").read_bytes() == expected
    assert status.progress.total_bytes == len(expected)
    assert set(engine.work_dir.parent.iterdir()) == before and {p.name for p in engine.work_dir.iterdir()} <= {"a.part","a.part.aria2"}

def test_T04_T05_T06_T07_T08_controls(engine, http_fixture):
    url, body, _ = http_fixture; engine.test_only_allow_loopback_http = True; engine.start()
    job = engine.submit(DownloadRequest(url + "/slow", "a.part"))
    end = time.monotonic() + 5; samples = []
    while time.monotonic() < end:
        status = engine.status(job); samples.append(status.progress.completed_bytes)
        if status.progress.speed_bps > 0 and status.progress.completed_bytes > 0: break
        threading.Event().wait(.025)
    assert status.state is S.ACTIVE and status.progress.speed_bps > 0 and max(samples) > min(samples)
    assert engine.pause(job).state is S.PAUSED and engine.status(job).progress.speed_bps == 0
    raw = raw_rpc(engine, "tellStatus", job)[1]["result"]; assert raw["status"] == "paused"
    threading.Event().wait(.15)
    assert raw_rpc(engine, "tellStatus", job)[1]["result"]["completedLength"] == raw["completedLength"]
    engine.resume(job); assert wait_for(engine, job, (S.COMPLETED, S.FAILED)).state is S.COMPLETED
    assert (engine.work_dir / "a.part").read_bytes() == body
    job = engine.submit(DownloadRequest(url + "/slow", "b.part")); wait_for(engine, job, (S.ACTIVE,))
    assert engine.cancel(job).state is S.REMOVED and engine.status(job).state is S.REMOVED
    assert raw_rpc(engine,"tellStatus",job)[1]["result"]["status"] == "removed"
    assert (engine.work_dir / "b.part").exists() and (engine.work_dir / "b.part.aria2").exists()
    with pytest.raises(InvalidJobStateError): engine.resume(job)

def test_E09_child_killed_during_transfer(engine, http_fixture):
    engine.test_only_allow_loopback_http = True; engine.start()
    job = engine.submit(DownloadRequest(http_fixture[0] + "/slow", "a.part")); wait_for(engine, job, (S.ACTIVE,))
    private = engine._private_dir; engine._process.terminate(); engine._process.wait(3)
    with pytest.raises(EngineExitedError): engine.status(job)
    assert engine.state is EngineState.FAILED and not private.exists() and engine._secret is None
    with pytest.raises(EngineExitedError): engine._check_alive()

def test_T14_expected_size_mismatch(engine, http_fixture):
    engine.test_only_allow_loopback_http = True; engine.start()
    job = engine.submit(DownloadRequest(http_fixture[0] + "/lie", "a.part", expected_size=5))
    end = time.monotonic() + 3
    while time.monotonic() < end:
        raw = raw_rpc(engine, "tellStatus", job)[1]["result"]
        if raw["totalLength"] == "999999999": break
        threading.Event().wait(.025)
    assert raw["totalLength"] == "999999999"
    status = wait_for(engine, job, (S.FAILED,))
    assert status.failure.kind is K.SIZE_MISMATCH and engine.status(job) == status
    path = engine.work_dir / "a.part"; assert not path.exists() or path.stat().st_size <= 1024

def test_security_policy_and_T17_duplicate_foreign(engine, http_fixture, monkeypatch):
    engine.start(); url = http_fixture[0]
    engine.test_only_allow_loopback_http = True
    call, calls = engine._rpc.call, []
    def record(method, params=()): calls.append(method); return call(method, params)
    monkeypatch.setattr(engine._rpc, "call", record)
    with pytest.raises(InvalidRequestError): engine.submit(DownloadRequest("http://example.com/a", "a.part"))
    with pytest.raises(InvalidJobError): engine.status("ffffffffffffffff")
    assert calls == []
    engine.submit(DownloadRequest(url + "/slow", "a.part"))
    with pytest.raises(TargetExistsError): engine.submit(DownloadRequest(url, "A.part"))

@pytest.mark.parametrize("code,message,kind,http_status", [(3,"",K.HTTP_NOT_FOUND,404), (22,"status=429",K.HTTP_RATE_LIMITED,429),
    (22,"status=503",K.HTTP_SERVER_ERROR,503), (22,"status=403",K.HTTP_CLIENT_ERROR,403), (22,"",K.UNKNOWN,None),
    (29,"",K.HTTP_SERVER_ERROR,None), (29,"status=429",K.HTTP_RATE_LIMITED,429), (24,"",K.HTTP_CLIENT_ERROR,401),
    (1,"SSL/TLS",K.TLS,None), (1,"WinTLS",K.TLS,None), (1,"No URI available",K.INTERRUPTED,None), (1,"",K.UNKNOWN,None), (2,"",K.TIMEOUT,None),
    (6,"",K.NETWORK,None), (19,"",K.NETWORK,None), (8,"",K.RESUME_UNSUPPORTED,None), (9,"",K.DISK_FULL,None),
    (13,"",K.TARGET_EXISTS,None), *[(n,"",K.FILE_IO,None) for n in (15,16,17,18)], (23,"",K.TOO_MANY_REDIRECTS,None), (99,"",K.UNKNOWN,None)])
def test_F01_F05_failure_mapper(code, message, kind, http_status):
    failure = a.map_failure(code, message, 1, 10)
    assert (failure.kind, failure.aria2_code, failure.http_status) == (kind, code, http_status)

def test_D1_delayed_headers_unknown_total(engine, monkeypatch):
    stopped, received, content = threading.Event(), threading.Event(), b"healthy" * 5000
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(2); self.rfile.readline()
            while self.rfile.readline() not in (b"\r\n", b""): pass
            received.set()
            if not stopped.wait(1.5):
                try: self.wfile.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(content)}\r\nConnection: close\r\n\r\n".encode() + content)
                except OSError: pass
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}); thread.start()
    try:
        engine.test_only_allow_loopback_http = True; engine.start(); call, methods = engine._rpc.call, []
        def record(method, params=()): methods.append(method); return call(method, params)
        monkeypatch.setattr(engine._rpc, "call", record)
        job = engine.submit(DownloadRequest(f"http://127.0.0.1:{server.server_address[1]}/delayed", "a.part", expected_size=len(content)))
        assert received.wait(2); unknown = engine.status(job)
        assert unknown.state in (S.WAITING, S.ACTIVE) and unknown.progress.total_bytes is None
        assert wait_for(engine, job, (S.COMPLETED, S.FAILED)).state is S.COMPLETED and "remove" not in methods
        assert (engine.work_dir / "a.part").read_bytes() == content
    finally: stopped.set(); server.shutdown(); server.server_close(); thread.join(2); assert not thread.is_alive()

@pytest.mark.parametrize("code,kind", [(404,K.HTTP_NOT_FOUND), (429,K.HTTP_RATE_LIMITED), (503,K.HTTP_SERVER_ERROR)])
def test_D1_F01_F02_F03_T20_http_failure(engine, http_fixture, code, kind):
    engine.test_only_allow_loopback_http = True; engine.start()
    job = engine.submit(DownloadRequest(http_fixture[0] + f"/{code}", "a.part", expected_size=123))
    status = wait_for(engine, job, (S.FAILED,))
    assert status.failure.kind is kind and status.failure.http_status == code
    assert status.verified is False and status.publisher_verified is False

def test_T09_two_active_one_waiting(engine, http_fixture):
    engine.test_only_allow_loopback_http = True; engine.start()
    jobs = [engine.submit(DownloadRequest(http_fixture[0] + "/slow", f"a{n}.part")) for n in range(3)]
    for job in jobs[:2]: assert wait_for(engine, job, (S.ACTIVE,)).state is S.ACTIVE
    assert raw_rpc(engine, "tellStatus", jobs[2])[1]["result"]["status"] == "waiting"
    engine.cancel(jobs[0]); assert wait_for(engine, jobs[2], (S.ACTIVE,)).state is S.ACTIVE

@pytest.mark.parametrize("cap,route", [(1,"/large"), (4,"/large"), (4,"/norange")])
def test_T10_T12_connections_and_rangeless(engine, http_fixture, cap, route):
    url, body, requests = http_fixture; engine.test_only_allow_loopback_http = True; engine.start()
    job = engine.submit(DownloadRequest(url + route, "a.part", connections=cap))
    end = time.monotonic() + 8; seen = []
    while time.monotonic() < end:
        status = engine.status(job); seen.append(status.progress.connections)
        if status.state in (S.COMPLETED, S.FAILED): break
        threading.Event().wait(.025)
    assert status.state is S.COMPLETED and max(seen) <= cap and requests.peak <= cap + 1
    if cap == 1: assert requests.peak == 1
    assert (engine.work_dir / "a.part").read_bytes() == body * 8

def test_T11_global_limit_and_settings(engine, http_fixture, monkeypatch):
    engine.test_only_allow_loopback_http = True; engine.start()
    settings = TransferSettings(max_active=1, global_speed_limit=51200); engine.update_settings(settings)
    options = raw_rpc(engine, "getGlobalOption")[1]["result"]
    assert options["max-overall-download-limit"] == "51200" and options["max-concurrent-downloads"] == "1"
    started = time.monotonic(); job = engine.submit(DownloadRequest(http_fixture[0] + "/bandwidth", "a.part"))
    assert wait_for(engine, job, (S.COMPLETED, S.FAILED), 10).state is S.COMPLETED and time.monotonic() - started >= 3
    first = engine.submit(DownloadRequest(http_fixture[0] + "/slow", "b.part"))
    second = engine.submit(DownloadRequest(http_fixture[0] + "/slow", "c.part"))
    assert engine.status(second).state is S.WAITING; engine.cancel(first); wait_for(engine, second, (S.ACTIVE,))
    with pytest.raises(InvalidSettingsError): engine.update_settings(TransferSettings(max_active=0))
    original = engine._rpc.call
    monkeypatch.setattr(engine._rpc, "call", lambda method, params=(): {} if method == "getGlobalOption" else original(method, params))
    with pytest.raises(RpcProtocolError): engine.update_settings(TransferSettings(max_active=3))
    assert engine.settings == settings

def test_T17_T18_raw_backstop_and_T22_listing(engine, http_fixture):
    engine.test_only_allow_loopback_http = True; engine.start(); path = engine.work_dir / "a.part"; path.write_bytes(b"keep")
    request = DownloadRequest(http_fixture[0] + "/data", "a.part")
    with pytest.raises(TargetExistsError): engine.submit(request)
    options = {"dir": str(engine.work_dir), "out": "a.part", "continue": "false", "allow-overwrite": "false", "auto-file-renaming": "false"}
    job = raw_rpc(engine, "addUri", [request.uri], options)[1]["result"]; end = time.monotonic() + 3
    while time.monotonic() < end:
        raw = raw_rpc(engine, "tellStatus", job)[1]["result"]
        if raw["status"] == "error": break
        threading.Event().wait(.025)
    assert raw["errorCode"] == "13" and path.read_bytes() == b"keep"
    completed = engine.submit(DownloadRequest(request.uri, "b.part")); wait_for(engine, completed, (S.COMPLETED,))
    assert {p.name for p in engine.work_dir.iterdir()} <= {"a.part", "a.part.aria2", "b.part", "b.part.aria2"}

@pytest.mark.parametrize("name", ["../escape.part", "..\\escape.part", "C:\\escape.part", "a:b.part", "NUL.part", "é.part", "x" * 64 + ".part"])
def test_T19_engine_output_validation(engine, monkeypatch, name):
    engine.test_only_allow_loopback_http = True; engine.start()
    before = set(engine.work_dir.parent.iterdir()), set(engine.work_dir.iterdir())
    request = DownloadRequest("http://127.0.0.1/a", "a.part"); object.__setattr__(request, "output_name", name)
    original = engine._rpc.call
    monkeypatch.setattr(engine._rpc, "call", lambda method, params=(): original(method, params) if method == "shutdown" else pytest.fail("Invalid output reached RPC"))
    with pytest.raises(InvalidRequestError): engine.submit(request)
    assert before == (set(engine.work_dir.parent.iterdir()), set(engine.work_dir.iterdir()))

def file_hashes(paths): return [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]

@pytest.fixture
def resume_case(engine, http_fixture, record_property):
    url, body, requests = http_fixture; requests.mode = "drop_first"
    engine.test_only_allow_loopback_http = True; engine.start(); old_pid = engine._process.pid
    job = engine.submit(DownloadRequest(url + "/res", "a.part"))
    status = wait_for(engine, job, (S.FAILED, S.COMPLETED))
    assert status.state is S.FAILED and status.failure.kind is K.INTERRUPTED
    assert raw_rpc(engine, "tellStatus", job)[1]["result"]["errorCode"] == "1"
    paths = [engine.work_dir / "a.part", engine.work_dir / "a.part.aria2"]
    assert all(p.is_file() and p.stat().st_size > 0 for p in paths)
    record_property("partial_sizes", [p.stat().st_size for p in paths])
    engine.shutdown_grace = .1; engine.stop(); requests.mode = "ok"; requests.clear()
    fresh = a.Aria2Engine(engine.work_dir, executable=engine.executable, test_only_allow_loopback_http=True, shutdown_grace=.1)
    try:
        fresh.start(); assert fresh._process.pid != old_pid
        yield fresh, paths, url + "/res", body[:300000], requests
    finally:
        if fresh._process is not None and fresh._process.poll() is None: fresh._process.terminate()
        fresh.stop()

@pytest.mark.parametrize("mode,validator", [("ok",None), ("ignore",None), ("badcr",None), ("shortcr",None),
                                          ("changed",'"v0"'), ("changed",None)])
def test_T13_T14_T15_T16_resume_and_F10_limits(resume_case, mode, validator):
    engine, paths, url, original, requests = resume_case; requests.mode = mode; before = file_hashes(paths)
    job = engine.submit(DownloadRequest(url, "a.part", expected_size=300000, resume=True, validator=validator))
    status = wait_for(engine, job, (S.FAILED, S.COMPLETED))
    ranged = [headers for path, headers in requests if path == "/res" and headers.get("Range")]
    assert ranged and int(ranged[0]["Range"][6:].partition("-")[0]) > 0
    raw = raw_rpc(engine, "tellStatus", job)[1]["result"]
    if mode in ("ignore", "badcr", "shortcr") or validator:
        assert status.state is S.FAILED and status.failure.kind is K.RESUME_UNSUPPORTED and raw["errorCode"] == "8"
        assert file_hashes(paths) == before
        if validator: assert ranged[0]["If-Range"] == validator
    else:
        assert status.state is S.COMPLETED and raw["status"] == "complete" and status.verified is False
        result = paths[0].read_bytes()
        if mode == "ok": assert result == original
        else:
            # aria2 trusts partial bytes: CA-S7 MUST fully hash every completed download.
            changed = bytes((b + 5) % 256 for b in original)
            assert len(result) == 300000 and result != original and result != changed

@pytest.mark.parametrize("missing", [0, 1])
def test_resume_limits_missing_data_or_control(resume_case, missing, monkeypatch):
    engine, paths, url, _, requests = resume_case; paths[missing].unlink(); before = [p.read_bytes() if p.exists() else None for p in paths]
    original, methods = engine._rpc.call, []
    def record(method, params=()): methods.append(method); return original(method, params)
    monkeypatch.setattr(engine._rpc, "call", record)
    with pytest.raises(ResumeNotAllowedError): engine.submit(DownloadRequest(url, "a.part", expected_size=300000, resume=True))
    with pytest.raises(TargetExistsError): engine.submit(DownloadRequest(url, "a.part"))
    assert not methods and not requests and before == [p.read_bytes() if p.exists() else None for p in paths]

def test_resume_limits_garbage_control(resume_case):
    engine, paths, url, _, _ = resume_case; paths[1].write_bytes(b"garbage-control" * 3); before = file_hashes(paths)
    job = engine.submit(DownloadRequest(url, "a.part", expected_size=300000, resume=True))
    assert wait_for(engine, job, (S.FAILED, S.COMPLETED)).state is S.FAILED and file_hashes(paths) == before

def test_F07_stop_retains_partial(engine, http_fixture, record_property):
    engine.test_only_allow_loopback_http = True; engine.start()
    job = engine.submit(DownloadRequest(http_fixture[0] + "/slow", "a.part")); end = time.monotonic() + 3
    while time.monotonic() < end:
        status = engine.status(job)
        if status.progress.completed_bytes > 0: break
        threading.Event().wait(.025)
    assert 0 < status.progress.completed_bytes < len(http_fixture[1])
    engine.stop(); assert engine.state is EngineState.STOPPED and engine.forced_stop is False
    with pytest.raises(InvalidJobStateError): engine.status(job)
    path = engine.work_dir / "a.part"; assert 0 < path.stat().st_size < len(http_fixture[1])
    record_property("control_exists_after_stop", (engine.work_dir / "a.part.aria2").exists())

@pytest.mark.parametrize("self_signed", [True, False])
def test_F04_TLS_failures(engine, http_fixture, tmp_path, self_signed, record_property):
    openssl = next((p for p in (Path("C:/Program Files/Git/usr/bin/openssl.exe"), Path("C:/Program Files/Git/mingw64/bin/openssl.exe")) if p.is_file()), None)
    if openssl is None: pytest.skip("Git openssl absent from both approved paths: TLS NOT VERIFIED")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    result = subprocess.run([str(openssl), "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
        "-out", str(cert), "-days", "1", "-subj", "/CN=localhost"], capture_output=True, timeout=10)
    assert result.returncode == 0
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}); thread.start()
    try:
        engine.test_only_allow_loopback_http = True; engine.start()
        uri = f"https://127.0.0.1:{server.server_port}/private" if self_signed else http_fixture[0].replace("http:", "https:") + "/data"
        job = engine.submit(DownloadRequest(uri, "a.part")); status = wait_for(engine, job, (S.FAILED, S.COMPLETED))
        raw = raw_rpc(engine, "tellStatus", job)[1]["result"]
        record_property("tls_raw_error", str((raw.get("errorCode"), raw.get("errorMessage"))))
        if raw.get("errorMessage") == "Failed to initialize WinTLS context handle": pytest.skip("WinTLS cannot initialize under this restricted token: TLS handshake NOT VERIFIED here; host run is authoritative")
        assert raw["errorCode"] == "1" and "SSL/TLS" in raw["errorMessage"], raw
        assert status.state is S.FAILED and status.failure.kind is K.TLS
        assert raw_rpc(engine, "getGlobalOption")[1]["result"]["check-certificate"] == "true" and not status.verified
    finally: server.shutdown(); server.server_close(); thread.join(2); assert not thread.is_alive()

def test_F05_real_file_io(engine, http_fixture, monkeypatch, record_property):
    engine.test_only_allow_loopback_http = True; engine.start(); original = engine._rpc.call; sid = "*" + a.current_user_sid()
    tool = r"C:\Windows\System32\icacls.exe"
    def deny_before_add(method, params=()):
        if method == "addUri":
            result = subprocess.run([tool, str(engine.work_dir), "/deny", sid + ":(W)"], capture_output=True, timeout=5)
            assert result.returncode == 0
        return original(method, params)
    monkeypatch.setattr(engine._rpc, "call", deny_before_add)
    try:
        job = engine.submit(DownloadRequest(http_fixture[0] + "/data", "a.part"))
        status = wait_for(engine, job, (S.FAILED, S.COMPLETED)); raw = raw_rpc(engine, "tellStatus", job)[1]["result"]
        record_property("real_file_io_code", raw.get("errorCode"))
        assert status.state is S.FAILED and status.failure.kind in (K.FILE_IO, K.DISK_FULL)
        assert int(raw["errorCode"]) in (9,15,16,17,18) and not status.verified
        # Real disk-full NOT VERIFIED; this bounded attempt exercises file-I/O on a test-owned directory.
    finally:
        result = subprocess.run([tool, str(engine.work_dir), "/remove:d", sid], capture_output=True, timeout=5)
        assert result.returncode == 0

def test_security_parent_proxies_stripped(engine, http_fixture, monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"): monkeypatch.setenv(name, "http://127.0.0.1:9")
    popen, environments = a.subprocess.Popen, []
    def capture(*args, **kwargs): environments.append(kwargs.get("env", {})); return popen(*args, **kwargs)
    monkeypatch.setattr(a.subprocess, "Popen", capture)
    engine.test_only_allow_loopback_http = True; engine.start()
    assert environments and all("proxy" not in name.casefold() for env in environments for name in env)
    job = engine.submit(DownloadRequest(http_fixture[0] + "/data", "a.part"))
    assert wait_for(engine, job, (S.COMPLETED, S.FAILED)).state is S.COMPLETED
    assert (engine.work_dir / "a.part").read_bytes() == http_fixture[1][:32768]

def test_security_cross_origin_redirect(engine, http_fixture):
    # aria2 follows cross-origin redirects: NO redirect-host enforcement; CA-S7 must use its own no-follow probe and audit currentUri.
    class Redirect(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self): self.send_response(302); self.send_header("Location", http_fixture[0] + "/data"); self.send_header("Content-Length", "0"); self.end_headers()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}); thread.start()
    try:
        engine.test_only_allow_loopback_http = True; engine.start()
        job = engine.submit(DownloadRequest(f"http://127.0.0.1:{server.server_port}/redirect", "a.part"))
        assert wait_for(engine, job, (S.COMPLETED, S.FAILED)).state is S.COMPLETED
        assert any(path == "/data" for path, _ in http_fixture[2]) and (engine.work_dir / "a.part").read_bytes() == http_fixture[1][:32768]
    finally: server.shutdown(); server.server_close(); thread.join(2); assert not thread.is_alive()

def test_security_URL_secrets_in_failures(engine, http_fixture, caplog):
    with caplog.at_level("DEBUG"):
        engine.start(); uri = http_fixture[0] + "/404?token=TOPSECRET"; request = DownloadRequest(uri, "a.part")
        with pytest.raises(InvalidRequestError) as credentials: DownloadRequest("https://user@host/private?token=TOPSECRET", "a.part")
        with pytest.raises(InvalidRequestError) as policy: engine.submit(request)
        engine.test_only_allow_loopback_http = True; job = engine.submit(request)
        status = wait_for(engine, job, (S.FAILED, S.COMPLETED)); assert status.failure.kind is K.HTTP_NOT_FOUND
    diagnostic = repr(request) + repr(status) + str(status.failure.detail) + caplog.text
    for error in (credentials.value, policy.value): diagnostic += str(error) + repr(error) + repr(vars(error))
    assert all(value not in diagnostic for value in ("TOPSECRET", "?token=", "/404", "/private"))

def test_security_DETACHED_PROCESS_no_console_or_windows(engine):
    import ctypes as C
    from ctypes import wintypes as W
    engine.start(); pid = engine._process.pid; kernel = C.WinDLL("kernel32", use_last_error=True); user = C.WinDLL("user32", use_last_error=True)
    class Entry(C.Structure):
        _fields_ = [("size",W.DWORD),("usage",W.DWORD),("pid",W.DWORD),("heap",C.c_size_t),("module",W.DWORD),
                    ("threads",W.DWORD),("parent",W.DWORD),("priority",W.LONG),("flags",W.DWORD),("exe",W.WCHAR*260)]
    snapshot = a._bind(kernel,"CreateToolhelp32Snapshot",[W.DWORD,W.DWORD],W.HANDLE)(2,0); assert snapshot != C.c_void_p(-1).value
    entry, children = Entry(), []; entry.size = C.sizeof(Entry)
    try:
        first = a._bind(kernel,"Process32FirstW",[W.HANDLE,C.POINTER(Entry)],W.BOOL); assert first(snapshot,C.byref(entry))
        next_entry = a._bind(kernel,"Process32NextW",[W.HANDLE,C.POINTER(Entry)],W.BOOL)
        while True:
            if entry.parent == pid: children.append(entry.pid)
            if not next_entry(snapshot,C.byref(entry)): break
    finally: a._bind(kernel,"CloseHandle",[W.HANDLE],W.BOOL)(snapshot)
    assert not children; windows = []; callback_type = C.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
    get_pid = a._bind(user,"GetWindowThreadProcessId",[W.HWND,C.POINTER(W.DWORD)],W.DWORD)
    def inspect(hwnd, _):
        owner = W.DWORD(); get_pid(hwnd,C.byref(owner))
        if owner.value == pid: windows.append(hwnd)
        return True
    C.set_last_error(0)
    enumerated = a._bind(user,"EnumWindows",[callback_type,W.LPARAM],W.BOOL)(callback_type(inspect),0)
    if not enumerated and C.get_last_error() == 0: pytest.skip("EnumWindows unavailable in this restricted window station: window absence NOT VERIFIED here; host run is authoritative")
    assert enumerated, C.WinError(C.get_last_error())
    assert not windows  # DETACHED_PROCESS avoids even a hidden conhost child.

def test_security_ORPHAN_stop_with_process(tmp_path, executable):
    import ctypes as C
    from ctypes import wintypes as W
    source = Path(__file__).parents[1] / "src"
    script = ("import sys,json;sys.path.insert(0,sys.argv[1]);from covearchive.transfer.aria2 import Aria2Engine;"
              "e=Aria2Engine(sys.argv[2],executable=sys.argv[3]);e.start();"
              "print(json.dumps([e._process.pid,str(e._private_dir)]),flush=True);sys.stdin.read()")
    parent = subprocess.Popen([sys.executable,"-I","-B","-c",script,str(source),str(tmp_path),str(executable)],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,creationflags=0x208)
    messages = queue.Queue(); reader = threading.Thread(target=lambda: messages.put(parent.stdout.readline())); reader.start()
    kernel = C.WinDLL("kernel32"); handle = None; private = None
    wait = a._bind(kernel,"WaitForSingleObject",[W.HANDLE,W.DWORD],W.DWORD)
    try:
        pid, directory = json.loads(messages.get(timeout=15)); private = Path(directory)
        handle = a._bind(kernel,"OpenProcess",[W.DWORD,W.BOOL,W.DWORD],W.HANDLE)(0x100001,False,pid); assert handle
        parent.kill(); parent.wait(3); assert wait(handle,10000) == 0
        assert private.parent == Path(a.tempfile.gettempdir()) and private.name.startswith("covearchive-aria2-")
        assert [p.name for p in private.iterdir()] == ["aria2.conf"]
        # A hard-killed parent leaves an ACL-protected, now-dead secret; sweeping is future work.
        (private / "aria2.conf").unlink(); private.rmdir()
    finally:
        if parent.poll() is None: parent.kill(); parent.wait(3)
        reader.join(2); parent.stdin.close(); parent.stdout.close()
        if handle:
            if wait(handle,0) == 258: a._bind(kernel,"TerminateProcess",[W.HANDLE,W.UINT],W.BOOL)(handle,1); wait(handle,3000)
            a._bind(kernel,"CloseHandle",[W.HANDLE],W.BOOL)(handle)

def test_repair3_T17_engine_TOCTOU_options(engine, http_fixture, monkeypatch):
    engine.test_only_allow_loopback_http = True; engine.start(); original = engine._rpc.call; target = engine.work_dir / "a.part"
    def race(method, params=()):
        if method == "addUri": target.write_bytes(b"sentinel")
        return original(method, params)
    monkeypatch.setattr(engine._rpc, "call", race)
    job = engine.submit(DownloadRequest(http_fixture[0] + "/data", "a.part"))
    options = raw_rpc(engine, "getOption", job)[1]["result"]
    assert all(options[k] == v for k,v in {"allow-overwrite":"false","auto-file-renaming":"false","continue":"false","file-allocation":"none"}.items())
    status = wait_for(engine, job, (S.FAILED,S.COMPLETED))
    assert status.state is S.FAILED and status.failure.kind is K.TARGET_EXISTS and status.failure.aria2_code == 13
    assert target.read_bytes() == b"sentinel" and {p.name for p in engine.work_dir.iterdir()} == {"a.part"}

@pytest.mark.parametrize("failure", ["terminate","wait"])
def test_repair3_retained_process_blocks_restart(engine, monkeypatch, failure):
    class Retained:
        def poll(self): return None
        def terminate(self):
            if failure == "terminate": raise OSError("private-path")
        def wait(self, timeout): raise subprocess.TimeoutExpired("private-path",timeout)
    retained = Retained(); engine._process = retained; engine._state = EngineState.FAILED; spawned = []
    original = a.subprocess.Popen
    def spy(*args, **kwargs): spawned.append(1); return original(*args, **kwargs)
    monkeypatch.setattr(a.subprocess,"Popen",spy)
    try:
        with pytest.raises(ShutdownError): engine.start()
        assert not spawned and engine._process is retained and engine.state is EngineState.FAILED
    finally:
        if engine._process is retained: engine._process = None

@pytest.mark.parametrize("fault", ["acl","work_directory","cleanup","state","deadline"])
def test_repair3_lifecycle_faults(engine, monkeypatch, fault):
    if fault == "acl":
        created, secure = [], a.create_private_dir
        def record(path,sid): created.append(path); return secure(path,sid)
        monkeypatch.setattr(a,"create_private_dir",record); monkeypatch.setattr(a,"verify_private_acl",lambda *args: False)
        try:
            with pytest.raises(EngineStartError): engine.start()
            assert created and all(not path.exists() for path in created)
        finally:
            for path in created:
                if path.exists(): path.rmdir()
        return
    if fault == "work_directory":
        engine.work_dir.rmdir()
        with pytest.raises(EngineStartError) as caught: engine._work_directory()
        assert caught.value.code == "work_directory" and str(engine.work_dir) not in str(caught.value)+repr(caught.value)+str(caught.value.detail)
        engine.work_dir.mkdir(); engine._work_directory(); return
    if fault == "deadline":
        original = a.Aria2RpcClient.call; children = []
        def delayed(client,*args): threading.Event().wait(.08); return original(client,*args)
        popen = a.subprocess.Popen
        def record(*args,**kwargs): child=popen(*args,**kwargs); children.append(child); return child
        monkeypatch.setattr(a.Aria2RpcClient,"call",delayed); monkeypatch.setattr(a.subprocess,"Popen",record); engine.start_timeout=.1
        started=time.monotonic()
        with pytest.raises(EngineStartError): engine.start()
        assert time.monotonic()-started < 3 and all(child.poll() is not None for child in children); return
    engine.start(); child, private = engine._process, engine._private_dir
    if fault == "state":
        child.terminate(); child.wait(3)
        assert engine.state is EngineState.FAILED and not private.exists() and engine._secret is None; return
    original = a.shutil.rmtree
    with monkeypatch.context() as patch:
        def fail(path): raise OSError(str(path))
        patch.setattr(a.shutil,"rmtree",fail); child.terminate(); child.wait(3)
        with pytest.raises(ShutdownError) as caught: engine.stop()
        assert caught.value.code == "cleanup" and engine._private_dir == private
        assert str(private) not in str(caught.value)+repr(caught.value)+str(caught.value.detail)
    engine.stop(); assert not private.exists(); engine.start()

@pytest.mark.parametrize("fake", [True,False])
@pytest.mark.parametrize("flag,uri,allowed", [(False,"https://example.com/a",True),(False,"http://127.0.0.1/a",False),
    (True,"https://example.com/a",False),(True,"http://127.0.0.1/a",True),(True,"https://[::1]/a",True),(True,"http://localhost/a",False)])
def test_repair3_URI_policy_parity(tmp_path, fake, flag, uri, allowed):
    from types import SimpleNamespace
    engine = FakeEngine(test_only_allow_loopback_http=flag) if fake else a.Aria2Engine(tmp_path,executable="C:/missing.exe",test_only_allow_loopback_http=flag)
    if fake: engine.start()
    else: engine._state=EngineState.RUNNING; engine._rpc=SimpleNamespace(call=lambda *args: "0123456789abcdef")
    if allowed: assert engine.submit(DownloadRequest(uri,"a.part"))
    else:
        with pytest.raises(InvalidRequestError): engine.submit(DownloadRequest(uri,"a.part"))

@pytest.mark.parametrize("resume", [False,True])
@pytest.mark.parametrize("name", ["a.part","a.part.aria2"])
def test_post1_inaccessible_output(engine, monkeypatch, resume, name):
    engine.test_only_allow_loopback_http=True; engine.start(); original=Path.lstat; target=engine.work_dir/name; calls=[]
    def denied(path,*args,**kwargs):
        if path == target: raise PermissionError(str(path))
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,"lstat",denied); monkeypatch.setattr(engine._rpc,"call",lambda *args: calls.append(args))
    kind=ResumeNotAllowedError if resume else TargetExistsError
    with pytest.raises(kind) as caught: engine.submit(DownloadRequest("http://127.0.0.1/a","a.part",resume=resume,expected_size=1))
    assert not calls and not engine._jobs and caught.value.code == ("resume_files" if resume else "target_inaccessible")
    assert str(target) not in str(caught.value)+repr(caught.value)+str(caught.value.detail)

@pytest.mark.parametrize("fake", [False,True])
@pytest.mark.parametrize("field", ["connect_timeout","io_timeout"])
def test_post1_restart_required(engine, monkeypatch, fake, field):
    value=FakeEngine() if fake else engine; value.start(); before=value.settings; calls=[]
    if not fake: monkeypatch.setattr(value._rpc,"call",lambda *args: calls.append(args))
    with pytest.raises(InvalidSettingsError) as caught: value.update_settings(replace(before,**{field:getattr(before,field)+1}))
    assert caught.value.code == "restart_required" and value.settings == before and not calls

@pytest.mark.parametrize("error", [RpcTimeoutError,RpcProtocolError])
def test_post1_mismatch_removal_retry(engine, http_fixture, monkeypatch, error):
    engine.test_only_allow_loopback_http=True; engine.start(); original=engine._rpc.call; attempts=[]
    job=engine.submit(DownloadRequest(http_fixture[0]+"/slow","a.part",expected_size=3))
    def unreliable(method,params=()):
        if method == "remove":
            attempts.append(1)
            if len(attempts)==1: original(method,params); raise error("injected")
        return original(method,params)
    monkeypatch.setattr(engine._rpc,"call",unreliable)
    status=wait_for(engine,job,(S.FAILED,)); assert status.failure.kind is K.SIZE_MISMATCH
    assert raw_rpc(engine,"tellStatus",job)[1]["result"]["status"] == "removed"
    assert engine.status(job) == status and engine.status(job) == status and len(attempts)==2
    assert raw_rpc(engine,"tellStatus",job)[1]["result"]["status"] == "removed"
