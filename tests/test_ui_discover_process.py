"""Real local child processes exercise Discover cancellation and supervision."""

import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import ctypes
from pathlib import Path
import signal
import sys
import time

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QProcess, QTimer, qInstallMessageHandler
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import delete, isValid

from covearchive.discover import worker as w
from covearchive.ui import discover_process as p
from covearchive.ui.discover_process import DiscoverController, worker_command

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]
TEXT = "Unique Zelda search"


def gone(pid):
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle: return ctypes.get_last_error() == 87
        try:
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            return kernel.WaitForSingleObject(handle, 0) == 0
        finally:
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(handle)
    return not Path(f"/proc/{pid}").exists()


def command(script):
    return lambda launcher: [str(Path(sys.executable).resolve()), "-I", "-B", "-c", script]


def reply(tail="", payload=None, success=False):
    prefix = (f"import sys,json;sys.path.insert(0,{str(ROOT/'src')!r});"
              "from covearchive.discover import worker as w;"
              "r=json.loads(sys.stdin.buffer.readline());")
    if payload is not None:
        output = f"sys.stdout.buffer.write({payload!r});"
    elif success:
        output = ("from covearchive.discover.models import SearchPage;"
                  "page=SearchPage((),r['page'],r['per_page'],0,0,False,0,0,False);"
                  "sys.stdout.buffer.write(w.encode_success(r['request_id'],page));")
    else:
        output = "sys.stdout.buffer.write(w.encode_error(r['request_id'],'not_found'));"
    return prefix + output + "sys.stdout.buffer.flush();" + tail


# On Windows, os.abort() was observed to stall; Windows Error Reporting latency
# is a suspected but unproven cause. TerminateProcess with an access-violation
# status gives Qt the same errorOccurred Crashed + finished CrashExit deterministically.
# Linux keeps os.abort().
CRASH = ("import ctypes;k=ctypes.WinDLL('kernel32');k.GetCurrentProcess.restype=ctypes.c_void_p;"
         "k.TerminateProcess.argtypes=[ctypes.c_void_p,ctypes.c_uint];k.TerminateProcess(k.GetCurrentProcess(),0xC0000005)"
         ) if sys.platform == "win32" else "import os;os.abort()"


@pytest.fixture
def rig():
    controllers, messages = [], []
    previous = qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    def make(script="import time;time.sleep(10)", **options):
        controller = DiscoverController(ROOT/"run_covearchive.py", command_factory=command(script), **options)
        events, states, pids = [], [], []
        controller.terminal.connect(lambda *args: events.append(args))
        def state(rid, value):
            states.append((rid, value))
            if value == "RUNNING":
                process = controller.job.process
                process.started.connect(lambda: pids.append(process.processId()))
        controller.state_changed.connect(state)
        controller.test_states, controller.test_pids = states, pids
        controllers.append(controller)
        return controller, events
    yield make
    try:
        for controller in controllers:
            assert controller.shutdown(1000)
            assert all(gone(pid) for pid in controller.test_pids)
        APP.processEvents()
        assert not any("Destroyed while process" in message for message in messages)
    finally:
        qInstallMessageHandler(previous)


def test_success_roundtrip_and_fresh_ids(rig):
    c, events = rig(reply(success=True))
    assert c.start(TEXT, "ports", "updated", 2, 10)
    first = c.active_request_id
    until(lambda: events)
    assert events[0][:2] == (first, "COMPLETED") and isinstance(events[0][2], w.DiscoverSuccess)
    assert events[0][2].page.page == 2 and events[0][2].page.per_page == 10
    assert c.test_states == [(first, "RUNNING")] and c.active_request_id is None
    assert c.start(TEXT)
    assert c.active_request_id != first
    until(lambda: len(events) == 2)
    assert len(events) == 2 and events[1][1] == "COMPLETED"


def test_concurrent_start_leaves_active_job_undisturbed(rig):
    c, events = rig()
    assert c.start(TEXT)
    old, rid = c.job, c.active_request_id
    assert c.start("Other", "ports") is False
    assert c.job is old and c.active_request_id == rid and not events
    c.cancel()
    if c.job is not None:
        assert c.start("Other") is False
    until(lambda: events)
    assert len(events) == 1 and events[0][1:] == ("CANCELLED", None)


@pytest.mark.parametrize("options", [dict(text=None), dict(text="x"*257), dict(kind="other"),
    dict(sort=[]), dict(page=True), dict(page=0), dict(per_page=0), dict(per_page=101), dict(per_page=True)])
def test_local_protocol_failure_creates_no_process(rig, monkeypatch, options):
    c, events = rig()
    monkeypatch.setattr(p, "QProcess", lambda *args: pytest.fail("invalid input spawned process"))
    assert c.start(**(dict(text=TEXT) | options)) is False
    assert not events and not c.test_states and c.job is None and c.active_request_id is None


def test_cancel_blocked_child_reaps_and_emits_once(rig):
    c, events = rig()
    assert c.start(TEXT)
    until(lambda: c.test_pids)
    rid = c.active_request_id
    c.cancel()
    c.cancel()
    until(lambda: events)
    assert events == [(rid, "CANCELLED", None)] and c.job is None
    assert c.test_states == [(rid, "RUNNING"), (rid, "STOPPING")]


def test_deadline_remains_responsive_and_reaps(rig):
    c, events = rig(deadline_ms=120)
    ticks, timer = [], QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(10)
    try:
        assert c.start(TEXT)
        until(lambda: events)
    finally:
        timer.stop()
    assert len(events) == 1 and events[0][1:] == ("TIMED_OUT", None) and len(ticks) >= 2
    assert c.job is None and c.test_states[-1][1] == "STOPPING"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux session/group kill evidence")
def test_deadline_kills_linux_descendant_and_worker(rig, tmp_path):
    pidfile = tmp_path/"descendant"
    script = ("import os,sys,subprocess,time;assert os.getsid(0)==os.getpid();"
              "child=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(60)']);"
              f"open({str(pidfile)!r},'w').write(str(child.pid));time.sleep(60)")
    c, events = rig(script, deadline_ms=500)
    assert c.start(TEXT)
    until(lambda: pidfile.exists())
    pid = int(pidfile.read_text())
    until(lambda: events)
    assert events[0][1:] == ("TIMED_OUT", None)
    def dead():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().split()
            return len(stat) < 3 or stat[2] == "Z"
        except (FileNotFoundError, ProcessLookupError):
            return True
    # Only the direct child is ours to waitpid. A killed grandchild may remain
    # a zombie pending host init's reap; it cannot execute or retain pipe fds.
    until(dead)


def test_shutdown_reaps_active_worker(rig):
    c, events = rig()
    assert c.start(TEXT)
    until(lambda: c.test_pids)
    assert c.shutdown(1000) and c.job is None
    assert len(events) == 1 and events[0][1:] == ("CANCELLED", None)
    assert c.shutdown(1000)


def test_late_envelope_cannot_override_cancel_or_new_request(rig):
    c, events = rig(reply("import time;time.sleep(10)", success=True))
    assert c.start(TEXT)
    old = c.job
    until(lambda: old.output)
    assert not events
    c.cancel()
    until(lambda: events)
    assert events[0][1:] == ("CANCELLED", None) and not old.output
    assert c.start("Other")
    current = c.job
    c._finish(old)
    c._read(old)
    c._deadline(old)
    c._stop(old, "FAILED")
    assert c.job is current and len(events) == 1


@pytest.mark.parametrize("script,code", [
    ("import sys;sys.exit(3)", "process"), pytest.param(CRASH, "process", id="crash"),
    (reply("sys.exit(7)"), "process"), (reply(payload=b"bad\n"), "protocol"),
    (reply(payload=b""), "protocol"), (reply(payload=b"{}\n{}\n"), "protocol"),
    (reply(payload=w.encode_error("b"*32, "network")), "protocol"),
])
def test_crash_malformed_and_mismatched_response(rig, script, code):
    c, events = rig(script)
    assert c.start(TEXT)
    until(lambda: events)
    assert len(events) == 1 and events[0][1] == "FAILED" and events[0][2].code == code


def test_failed_to_start_is_single_terminal(rig):
    c, events = rig()
    c.command_factory = lambda _: [str(ROOT/"missing-python")]
    assert c.start(TEXT)
    until(lambda: events)
    assert len(events) == 1 and events[0][1] == "FAILED" and events[0][2].code == "process"


def test_oversized_stdout_fails_and_kills(rig, monkeypatch):
    monkeypatch.setattr(p, "MAX_RESPONSE_BYTES", 500)
    c, events = rig("import sys,time;sys.stdout.write('x'*1000);sys.stdout.flush();time.sleep(10)")
    assert c.start(TEXT)
    old = c.job
    until(lambda: events)
    assert len(old.output) <= 500
    assert len(events) == 1 and events[0][1] == "FAILED"
    assert events[0][2].code == "protocol" and events[0][2].message == "Response too large."


def test_stderr_flood_is_drained_in_bounded_chunks(rig, monkeypatch):
    block = b"PRIVATE_QUERY ghp_SECRET123\x00\x1b\r\n"
    c, events = rig(f"import sys;sys.stderr.buffer.write({block!r}*40000);sys.stderr.flush();sys.exit(3)")
    assert c.start(TEXT)
    old = c.job
    reads, read = [], old.process.read
    def recorded_read(size):
        chunk = read(size)
        reads.append((size, chunk.size()))
        return chunk
    monkeypatch.setattr(old.process, "read", recorded_read)
    until(lambda: events)
    assert sum(size for _, size in reads) == len(block) * 40000
    assert all(0 <= size <= cap <= 65536 for cap, size in reads)
    assert not hasattr(old, "diagnostics")
    assert events[0][1] == "FAILED" and events[0][2].code == "process" and events[0][2].detail == {}
    assert "PRIVATE_QUERY" not in repr(events[0][2]) and "ghp_SECRET123" not in repr(events[0][2])


@pytest.mark.parametrize("diagnostic", [
    "RuntimeError: https://user:SECRET@proxy.invalid/ PRIVATE_QUERY\n",
    'Traceback (most recent call last):\n  File "PRIVATE_QUERY", line 1\nRuntimeError: SECRET\n',
    "RuntimeError\x00 rate_limited\n" + "SECRET" * 1000 + "\x1b PRIVATE_QUERY\n",
    "PRIVATE_QUERY\n", "ghp_SECRET123\n", "RuntimeError\n", "RuntimeError rate_limited\n",
])
@pytest.mark.parametrize("code", ["process", "protocol"])
def test_untrusted_stderr_never_surfaces(rig, diagnostic, code):
    tail = "sys.exit(3)" if code == "process" else "sys.stdout.write('malformed\\n');sys.stdout.flush()"
    c, events = rig(f"import sys;sys.stderr.write({diagnostic!r});sys.stderr.flush();" + tail)
    assert c.start(TEXT)
    until(lambda: events)
    assert events[0][1] == "FAILED" and events[0][2].code == code and events[0][2].detail == {}
    assert diagnostic.strip() not in repr(events[0][2])
    assert all(marker not in repr(events[0][2]) for marker in ("RuntimeError", "SECRET", "PRIVATE_QUERY", "ghp_SECRET123"))


@pytest.mark.skipif(sys.platform != "linux", reason="Linux QObject destruction/group kill evidence")
@pytest.mark.parametrize("owner,mode", [("controller", "later"), ("parent", "direct")])
def test_owner_destruction_kills_group_without_python_callbacks(tmp_path, monkeypatch, capsys, owner, mode):
    pidfile = tmp_path / "descendant"
    script = ("import os,sys,subprocess,time;assert os.getsid(0)==os.getpid();"
              "child=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(60)']);"
              f"open({str(pidfile)!r},'w').write(str(child.pid));time.sleep(60)")
    parent = QObject()
    c = DiscoverController(ROOT/"run_covearchive.py", command_factory=command(script), parent=parent)
    messages, exceptions, groups = [], [], []
    previous = qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    monkeypatch.setattr(sys, "excepthook", lambda *args: exceptions.append(args))
    killpg = os.killpg
    def recorded_kill(pid, sig):
        groups.append((pid, sig))
        killpg(pid, sig)
    monkeypatch.setattr(p.os, "killpg", recorded_kill)
    worker_pid = helper_pid = None
    def helper_dead():
        try:
            stat = Path(f"/proc/{helper_pid}/stat").read_text().split()
            return len(stat) < 3 or stat[2] == "Z"
        except (FileNotFoundError, ProcessLookupError):
            return True
    try:
        assert c.start(TEXT)
        until(lambda: pidfile.exists())
        old = c.job
        worker_pid, helper_pid = c.job.process.processId(), int(pidfile.read_text())
        target = c if owner == "controller" else parent
        if mode == "later":
            target.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        else:
            delete(target)
        until(lambda: not isValid(c))
        assert (worker_pid, signal.SIGKILL) in groups
        until(lambda: gone(worker_pid) and helper_dead())
        # Queued callbacks for the retired job must be safe even if invoked
        # after the underlying Qt children have already been deleted.
        c._read(old)
        c._finish(old)
        c._error(old, QProcess.Crashed)
        c._deadline(old)
        c._reap_slow(old)
        assert c.shutdown(1000)
        APP.processEvents()
        assert not exceptions and "RuntimeError" not in capsys.readouterr().err
        assert not any("Destroyed while process" in message for message in messages)
    finally:
        # A failing mutation must not leave its deliberately orphaned helper.
        if worker_pid:
            try: killpg(worker_pid, signal.SIGKILL)
            except ProcessLookupError: pass
        if isValid(c):
            c.shutdown(1000)
        if isValid(parent):
            delete(parent)
        qInstallMessageHandler(previous)


def test_shutdown_slot_can_destroy_controller_reentrantly(rig):
    c, _ = rig()
    assert c.start(TEXT)
    until(lambda: c.test_pids)
    c.state_changed.connect(lambda rid, state: delete(c) if state == "STOPPING" else None)
    assert c.shutdown(1000) and not isValid(c)


def test_no_early_terminal_while_child_still_runs(rig):
    c, events = rig(reply("import time;time.sleep(.25)", success=True))
    assert c.start(TEXT)
    until(lambda: c.job.output)
    assert not events
    until(lambda: events)
    assert len(events) == 1 and events[0][1] == "COMPLETED"


@pytest.mark.parametrize("frozen", [False, True])
def test_command_and_exact_environment_filter(rig, monkeypatch, frozen):
    keys = ("_PYI_ARCHIVE_FILE", "_PYI_APPLICATION_HOME_DIR", "_PYI_PARENT_PROCESS_LEVEL", "_PYI_SPLASH_IPC")
    values = dict(TEMP="isolated", GITHUB_TOKEN="SECRET", GH_TOKEN="SECRET", NETRC="SECRET",
                  HOME="SECRET", PATH="SECRET", _PYI_UNKNOWN="SECRET", SEARCH_TEXT=TEXT)
    values.update({key: str(i) for i, key in enumerate(keys)})
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    executable = str(Path(sys.executable).resolve())
    if not frozen and sys.platform == "win32" and Path(executable).with_name("pythonw.exe").is_file():
        executable = str(Path(executable).with_name("pythonw.exe").resolve())
    expected = [executable, "--discover-worker"] if frozen else [
        executable, "-I", "-B", str((ROOT/"run_covearchive.py").resolve()), "--discover-worker"]
    assert worker_command(ROOT/"run_covearchive.py") == expected
    c, events = rig()
    c.command_factory = worker_command
    monkeypatch.setattr(QProcess, "start", lambda self: None)
    assert c.start(TEXT)
    proc = c.job.process
    env = proc.processEnvironment()
    assert {key: env.value(key) for key in env.keys()} == (
        {"TEMP": "isolated", **{key: values[key] for key in keys}} if frozen else {"TEMP": "isolated"})
    assert [proc.program(), *proc.arguments()] == expected
    assert TEXT not in str(proc.arguments()) + str(env.toStringList()) + repr(c.job)
    assert "SECRET" not in str(proc.arguments()) + str(env.toStringList())
    assert c.shutdown(1000) and len(events) == 1


def test_actual_child_receives_text_only_through_stdin(rig, monkeypatch, caplog):
    monkeypatch.setenv("GITHUB_TOKEN", "SECRET")
    monkeypatch.setenv("SEARCH_TEXT", TEXT)
    script = ("import os,sys,json;request=json.loads(sys.stdin.buffer.readline());"
              f"assert request['text']=={TEXT!r};"
              "assert not {'GITHUB_TOKEN','GH_TOKEN','HOME','NETRC','SEARCH_TEXT'} & os.environ.keys();"
              "assert set(request)=={'protocol','request_id','action','text','kind','sort','page','per_page'};"
              f"sys.path.insert(0,{str(ROOT/'src')!r});"
              "from covearchive.discover.worker import encode_error;"
              "sys.stdout.buffer.write(encode_error(request['request_id'],'not_found'));sys.stdout.buffer.flush()")
    c, events = rig(script)
    assert c.start(TEXT)
    until(lambda: events)
    assert events[0][2].code == "not_found" and TEXT not in caplog.text


@pytest.mark.parametrize("has_pythonw", [True, False])
def test_windows_command_selects_pythonw(tmp_path, monkeypatch, has_pythonw):
    executable, windowless = tmp_path/"python.exe", tmp_path/"pythonw.exe"
    executable.touch()
    if has_pythonw:
        windowless.touch()
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "platform", "win32")
    assert worker_command(ROOT/"run_covearchive.py")[0] == str(windowless if has_pythonw else executable)


@pytest.mark.parametrize("state,action", [("RUNNING", "cancel"), ("RUNNING", "shutdown"),
    ("STOPPING", "shutdown"), ("STOP_SLOW", "shutdown")])
def test_reentrant_state_slots_do_not_leave_process_running(rig, state, action):
    c, events = rig()
    c.state_changed.connect(lambda rid, value: getattr(c, action)() if value == state else None)
    assert c.start(TEXT)
    if state == "STOPPING":
        c.cancel()
    if state == "STOP_SLOW":
        c._reap_slow(c.job)
    assert len(events) == 1 and events[0][1:] == ("CANCELLED", None) and c.job is None


def test_terminal_slot_can_start_new_job(rig):
    c, events = rig(reply(success=True))
    started = []
    c.terminal.connect(lambda *args: started.append(c.start(TEXT)) if len(events) == 1 else None)
    assert c.start(TEXT)
    until(lambda: started)
    assert started == [True] and c.job is not None
    until(lambda: len(events) == 2)
    assert len(events) == 2


def test_shutdown_rejects_terminal_restart_and_allows_later_start(rig, monkeypatch):
    c, events = rig()
    assert c.start(TEXT)
    until(lambda: c.test_pids)
    attempts, spawned = [], []
    start = QProcess.start
    def recorded_start(process, *args):
        spawned.append(1)
        start(process, *args)
    monkeypatch.setattr(QProcess, "start", recorded_start)
    c.terminal.connect(lambda *args: attempts.append(c.start(TEXT)) if len(events) == 1 else None)
    assert c.shutdown(1000)
    assert attempts == [False] and not spawned and c.job is None
    assert len(events) == 1 and all(gone(pid) for pid in c.test_pids)
    assert c.start(TEXT)
    until(lambda: len(c.test_pids) == 2)
    assert spawned == [1] and c.shutdown(1000) and c.job is None


@pytest.mark.parametrize("state_action", ["cancel", "shutdown"])
@pytest.mark.parametrize("terminal_action", ["cancel", "shutdown"])
def test_shutdown_reentrant_state_and_terminal_actions(rig, state_action, terminal_action):
    c, events = rig()
    assert c.start(TEXT)
    until(lambda: c.test_pids)
    callbacks = []
    def state_slot(rid, state):
        if state == "STOPPING":
            callbacks.append("state")
            getattr(c, state_action)()
    def terminal_slot(*args):
        callbacks.append("terminal")
        getattr(c, terminal_action)()
    c.state_changed.connect(state_slot)
    c.terminal.connect(terminal_slot)
    assert c.shutdown(1000) and c.job is None
    assert callbacks == ["state", "terminal"] and len(events) == 1
    assert c.start(TEXT) and c.shutdown(1000) and c.job is None


def until(predicate, seconds=2):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        APP.processEvents()
        QTest.qWait(5)
    assert predicate(), "bounded event wait expired"


def test_real_launcher_invalid_search():
    controller = DiscoverController(ROOT / "run_covearchive.py")
    events = []
    controller.terminal.connect(lambda *args: events.append(args))
    try:
        assert controller.start("!!!")
        until(lambda: events)
        assert len(events) == 1 and events[0][1] == "FAILED"
        assert events[0][2].code == "invalid_search"
        assert controller.active_request_id is None
    finally:
        assert controller.shutdown(1000)
