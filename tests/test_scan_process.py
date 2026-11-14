"""Real offscreen QProcess lifecycle tests, with OS process-handle evidence."""

import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import ctypes
from pathlib import Path
import sys
import time

import pytest
from PySide6.QtCore import QProcess, QTimer, qInstallMessageHandler
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from covearchive import scan_worker as w
from covearchive.ui.scan_process import ScanController, worker_command

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]
URL = "https://github.com/Owner/Repo"
QT_MESSAGES = []


def qt_messages(kind, context, message):
    QT_MESSAGES.append(message)


PREVIOUS_HANDLER = qInstallMessageHandler(qt_messages)


def until(predicate, seconds=1.4):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        APP.processEvents(); QTest.qWait(5)
    assert predicate(), "bounded event wait expired"


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


def reply(tail="", payload=None):
    return (f"import sys,json;sys.path.insert(0,{str(ROOT/'src')!r});"
            "from covearchive.scan_worker import encode_error;"
            "r=json.loads(sys.stdin.buffer.readline());"
            + (f"sys.stdout.buffer.write({payload!r});" if payload is not None else
               "sys.stdout.buffer.write(encode_error(r['request_id'],'not_found'));")
            + "sys.stdout.buffer.flush();" + tail)


@pytest.fixture
def rig():
    controllers = []
    message_start = len(QT_MESSAGES)
    def make(script="import time;time.sleep(10)", **options):
        controller = ScanController(ROOT/"run_covearchive.py", command_factory=command(script), **options)
        events = []
        controller.terminal.connect(lambda *args: events.append(args))
        controllers.append(controller)
        return controller, events
    yield make
    for controller in controllers: assert controller.shutdown(1000)
    APP.processEvents()
    assert not any("Destroyed while process" in message for message in QT_MESSAGES[message_start:])


def test_command_and_environment(rig, monkeypatch):
    cmd = worker_command(ROOT/"run_covearchive.py")
    executable = Path(sys.executable).resolve()
    if sys.platform == "win32" and executable.with_name("pythonw.exe").is_file():
        executable = executable.with_name("pythonw.exe")
    assert cmd == [str(executable), "-I", "-B",
                   str((ROOT/"run_covearchive.py").resolve()), "--scan-worker"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert worker_command(ROOT/"run_covearchive.py") == [str(Path(sys.executable).resolve()), "--scan-worker"]
    monkeypatch.setenv("GITHUB_TOKEN", "SECRET"); monkeypatch.setenv("HTTPS_PROXY", "http://proxy")
    c, _ = rig(); assert c.start(URL)
    env = c.job.process.processEnvironment()
    assert not env.contains("GITHUB_TOKEN") and not env.contains("PATH")
    assert env.value("HTTPS_PROXY") == "http://proxy"
    assert URL not in c.job.process.arguments()


def test_UI_05_18_19_responsive_deadline(rig):
    c, events = rig(deadline_ms=120)
    ticks = []; timer = QTimer(); timer.timeout.connect(lambda: ticks.append(1)); timer.start(10)
    assert c.start(URL); until(lambda: c.job.process.processId() > 0)
    pid = c.job.process.processId()
    until(lambda: events)
    timer.stop()
    assert events[0][1] == "TIMED_OUT" and len(events) == 1 and len(ticks) >= 2
    assert gone(pid) and c.job is None


def test_UI_20_21_23_cancel_guard_and_reap(rig):
    c, events = rig(); assert c.start(URL)
    until(lambda: c.job.process.processId() > 0)
    pid = c.job.process.processId(); job = c.job
    assert not c.start(URL)
    c.cancel()
    assert not c.start(URL)
    until(lambda: events)
    assert events[0][1] == "CANCELLED" and len(events) == 1 and gone(pid)
    assert c.start(URL); c.shutdown(1000)
    assert job is not c.job


def test_UI_21_cancel_wins_even_with_envelope(rig):
    c, events = rig(reply("import time;time.sleep(10)")); c.start(URL)
    until(lambda: bool(c.job.output))
    assert not events
    c.cancel(); until(lambda: events)
    assert events[0][1] == "CANCELLED"


@pytest.mark.parametrize("script", ["import sys;sys.exit(3)", reply(payload=b"bad\n"),
    reply("sys.exit(7)"), reply(payload=b""), reply(payload=w.encode_error("b"*32, "network"))])
def test_UI_15_17_invalid_or_crashed_child(rig, script):
    c, events = rig(script); assert c.start(URL); until(lambda: events)
    assert events[0][1] == "FAILED" and len(events) == 1
    assert events[0][2].code in {"protocol", "process"}


def test_UI_17_failed_to_start(rig):
    c, events = rig(); c.command_factory = lambda launcher: [str(ROOT/"missing-python.exe")]
    assert c.start(URL); until(lambda: events)
    assert events[0][1] == "FAILED" and events[0][2].code == "process"


def test_UI_16_gui_cap(rig, monkeypatch):
    import covearchive.ui.scan_process as p
    monkeypatch.setattr(p, "MAX_RESPONSE_BYTES", 500)
    c, events = rig("import sys,time;sys.stdout.write('x'*1000);sys.stdout.flush();time.sleep(10)")
    c.start(URL); until(lambda: c.job.process.processId() > 0)
    pid = c.job.process.processId(); until(lambda: events)
    assert events[0][1] == "FAILED" and events[0][2].code == "protocol" and gone(pid)


def test_UI_22_retired_job_ignored(rig):
    c, events = rig(reply()); c.start(URL); old = c.job; until(lambda: events)
    c.command_factory = command("import time;time.sleep(10)")
    c.start(URL); current = c.job
    c._finish(old)
    assert c.job is current and len(events) == 1


def test_stderr_backpressure_and_no_early_envelope(rig):
    c, events = rig(reply("sys.stderr.write('x'*100000);sys.stderr.flush();import time;time.sleep(.15)"))
    c.start(URL); until(lambda: bool(c.job.output))
    assert not events
    job = c.job; until(lambda: events)
    assert len(job.diagnostics) <= 65536 and events[0][2].code == "not_found"


def test_UI_24_shutdown_reaps(rig):
    c, events = rig(); c.start(URL); until(lambda: c.job.process.processId() > 0)
    pid = c.job.process.processId()
    assert c.shutdown(1000) and gone(pid) and c.job is None


@pytest.mark.parametrize("has_pythonw", [True, False])
def test_R6_windowless_command(tmp_path, monkeypatch, has_pythonw):
    executable = tmp_path/"python.exe"; executable.touch()
    windowless = tmp_path/"pythonw.exe"
    if has_pythonw: windowless.touch()
    monkeypatch.setattr(sys,"executable",str(executable))
    monkeypatch.setattr(sys,"platform","win32")
    assert worker_command(ROOT/"run_covearchive.py")[0] == str(windowless if has_pythonw else executable)


def test_R4_process_diagnostic_tail(rig):
    c,events=rig("import sys;sys.stderr.write('x'*10000+'TAIL');sys.stderr.flush();sys.exit(3)")
    c.start(URL); until(lambda:events)
    assert events[0][2].code == "process"
    assert events[0][2].detail is not None
    assert events[0][2].detail["stderr"].endswith("TAIL")
    assert len(events[0][2].detail["stderr"]) <= 2048


def test_R7_default_worker_roundtrip():
    c=ScanController(ROOT/"run_covearchive.py"); events=[]; pids=[]
    c.terminal.connect(lambda *args:events.append(args)); begin=len(QT_MESSAGES)
    try:
        assert c.start(URL+"/releases/tag/v1")
        if c.job.process.processId(): pids.append(c.job.process.processId())
        else: c.job.process.started.connect(lambda:pids.append(c.job.process.processId()))
        until(lambda:events)
        assert events[0][1]=="FAILED" and events[0][2].code=="unsupported_target"
        assert pids and gone(pids[0]) and c.job is None
        assert not any("Destroyed while process" in x for x in QT_MESSAGES[begin:])
    finally: assert c.shutdown(1000)


@pytest.mark.parametrize("state,action", [("RUNNING","cancel"),("RUNNING","shutdown"),
    ("STOPPING","shutdown"),("STOP_SLOW","shutdown")])
def test_C1_reentrant_state_slots(rig,state,action):
    c,events=rig(); processes=[]
    def slot(rid,value):
        if value==state:
            processes.append(c.job.process); getattr(c,action)()
    c.state_changed.connect(slot)
    try:
        c.start(URL)
        if state=="STOPPING": c.cancel()
        if state=="STOP_SLOW": c._reap_slow(c.job)
        assert len(events)==1 and events[0][1]=="CANCELLED" and c.job is None
        assert processes[0].state()==QProcess.NotRunning
    finally:
        for p in processes:
            pid=p.processId()
            if pid: p.kill(); p.waitForFinished(1000); assert gone(pid)


def test_C1_terminal_slot_can_rescan(rig):
    c,events=rig(reply()); started=[]
    c.terminal.connect(lambda *args: started.append(c.start(URL)) if len(events)==1 else None)
    c.start(URL); until(lambda:bool(started))
    assert started==[True] and c.job is not None
