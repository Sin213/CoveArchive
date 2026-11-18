"""Offline diagnostics for a built application; never use normal user storage."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from uuid import uuid4

from PySide6.QtWidgets import QApplication

from covearchive.github.models import Release, ReleaseAsset
from covearchive.github.token import GitHubAuth, WindowsCredentialVault
from covearchive.github.urls import RepositoryTarget
from covearchive.queue.store import QueueStore
from covearchive.scan_worker import ScanFailure
from covearchive.ui.scan_process import ScanController


@contextmanager
def isolated_paths(root):
    """Redirect defaults before constructing any object that can open user data."""
    keys = ("LOCALAPPDATA", "APPDATA", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
            "XDG_DATA_HOME", "XDG_CACHE_HOME", "HOME", "USERPROFILE")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            directory = root / key.lower()
            directory.mkdir(parents=True, exist_ok=True)
            os.environ[key] = str(directory)
        (root / "archive").mkdir(exist_ok=True)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def wait_for(app, predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    if not predicate():
        raise RuntimeError("diagnostic deadline exceeded")


def check_queue(root):
    path = root / "state" / "queue.sqlite3"
    store = QueueStore(path)
    asset = ReleaseAsset(1, "offline.zip", 0, "uploaded", "application/zip",
                         "https://github.com/Example/Offline/releases/download/v1/offline.zip",
                         None, None, None, "", "", ".zip")
    release = Release(1, "v1", None, False, False, None, "", None, None, (asset,), None, None)
    job = store.enqueue(RepositoryTarget("Example", "Offline"), release, asset,
                        str(root / "archive")).job
    if store.get(job.job_id).state.value != "QUEUED" or len(store.list_jobs()) != 1:
        raise RuntimeError("queue round trip failed")
    # QueueStore closes each connection inside its operation. Windows rename
    # also proves SQLite did not leave a connection holding the database open.
    del store
    moved = path.with_suffix(".closed")
    path.rename(moved)
    moved.rename(path)
    if QueueStore(path).get(job.job_id).job_id != job.job_id:
        raise RuntimeError("queue reopen failed")


def check_vault():
    token = "package_selftest_" + uuid4().hex
    if sys.platform == "win32":
        vault = WindowsCredentialVault(target="CoveArchive/package-selftest/" + uuid4().hex)
        try:
            if vault.load() is not None:
                raise RuntimeError("throwaway target unexpectedly exists")
            vault.save(token)
            if vault.load() != token:
                raise RuntimeError("throwaway credential round trip failed")
        finally:
            vault.delete()
        if vault.load() is not None:
            raise RuntimeError("throwaway credential was not deleted")
        return "throwaway_roundtrip_deleted"
    from PySide6.QtDBus import QDBusConnection
    from covearchive.ui.credential_vault import KWalletVault, kwallet_services
    # Discover availability without reading, opening or unlocking a real wallet.
    services = kwallet_services(QDBusConnection.sessionBus())
    disconnected = QDBusConnection("covearchive-package-selftest-" + uuid4().hex)
    auth = GitHubAuth(KWalletVault(bus_factory=lambda: disconnected))
    auth.load()
    problem = auth.save(token)
    if not problem or auth.status().state != "SESSION" or auth.token() != token:
        raise RuntimeError("unavailable KWallet fallback failed")
    return "kwallet_available_session_fallback" if services else "kwallet_unavailable_session_fallback"


def check_cancel(app, controller):
    """Prove requests reached an offline proxy and cancellation closes its socket."""
    keys = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")
    previous = {key: os.environ.get(key) for key in keys}
    accepted, results = [], []
    def terminal(*args): results.append(args)
    controller.terminal.connect(terminal)
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0)); listener.listen(1); listener.setblocking(False)
            proxy = "http://127.0.0.1:" + str(listener.getsockname()[1])
            for key in keys: os.environ[key] = "" if key.lower() == "no_proxy" else proxy
            def connected():
                if accepted: return True
                try: accepted.append(listener.accept()[0])
                except BlockingIOError: return False
                accepted[0].setblocking(False)
                return True
            if not controller.start("https://github.com/Example/Offline"):
                raise RuntimeError("cancel worker did not start")
            wait_for(app, connected)
            controller.cancel()
            wait_for(app, lambda: bool(results), seconds=5)
            if results[0][1] != "CANCELLED" or controller.job is not None:
                raise RuntimeError("cancel worker was not reaped")
            def disconnected():
                try: return accepted[0].recv(65536) == b""
                except BlockingIOError: return False
                except ConnectionResetError: return True
            wait_for(app, disconnected, seconds=5)
    finally:
        controller.terminal.disconnect(terminal)
        controller.shutdown()
        for connection in accepted: connection.close()
        for key, value in previous.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value


def run_checks(launcher, root, checks):
    from covearchive.ui.window import MainWindow
    app = QApplication.instance() or QApplication(["CoveArchive package selftest"])
    controller = ScanController(launcher, deadline_ms=8000)
    window = None
    results = []
    controller.terminal.connect(lambda *args: results.append(args))
    try:
        # Parsing rejects this URL before any HTTP client is constructed.
        if not controller.start("not-a-repository-url"):
            raise RuntimeError("worker did not start")
        wait_for(app, lambda: bool(results), seconds=12)
        _, state, result = results[0]
        if (state != "FAILED" or not isinstance(result, ScanFailure)
                or result.code != "invalid_url" or controller.job is not None):
            raise RuntimeError("worker did not return a reaped structured invalid_url failure")
        checks["worker"] = "invalid_url_reaped"
        check_cancel(app, controller)
        checks["cancel"] = "proxy_connected_cancelled_reaped_eof"
        check_queue(root)
        checks["queue"] = "enqueue_read_closed_reopened"
        checks["vault"] = check_vault()
        # Match the existing smoke test, with isolated defaults and session auth.
        window = MainWindow(launcher, controller=controller, auth=GitHubAuth(None))
        window.show()
        app.processEvents()
        window.close()
        wait_for(app, lambda: window.bridge.closed)
        if not controller.shutdown() or controller.job is not None:
            raise RuntimeError("worker remained after shutdown")
        checks["shutdown"] = "closed_no_worker"
    finally:
        if not controller.shutdown():
            raise RuntimeError("worker cleanup failed")
        if window is not None:
            window.close()
            wait_for(app, lambda: window.bridge.closed)
        app.processEvents()


def emit_result(report):
    data = (json.dumps(report, sort_keys=True) + "\n").encode("utf-8")
    if sys.stdout is not None:
        sys.stdout.write(data.decode("utf-8"))
        sys.stdout.flush()
    elif sys.platform == "win32":
        # PyInstaller windowed mode clears Python's stdout, even if the caller
        # supplied a pipe. Write only to that inherited handle; create no console.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.WriteFile.restype = wintypes.BOOL
        written = wintypes.DWORD()
        if not kernel.WriteFile(kernel.GetStdHandle(-11), data, len(data), ctypes.byref(written), None):
            raise RuntimeError("diagnostic output unavailable")
        if written.value != len(data):
            raise RuntimeError("diagnostic output incomplete")
    else:
        raise RuntimeError("diagnostic output unavailable")


def main(launcher):
    report = {"ok": False, "frozen": bool(getattr(sys, "frozen", False)), "checks": {}}
    try:
        with tempfile.TemporaryDirectory(prefix="covearchive-package-selftest-") as directory:
            root = Path(directory)
            with isolated_paths(root):
                run_checks(launcher, root, report["checks"])
        report["ok"] = True
    except Exception as exc:
        # Never print exception text, paths, credential targets or tokens.
        report["error"] = type(exc).__name__
    emit_result(report)
    return 0 if report["ok"] else 1
