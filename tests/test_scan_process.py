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


@pytest.mark.skipif(sys.platform != "linux", reason="Linux session and process-group deadline termination")
def test_linux_deadline_kills_worker_descendants(rig, tmp_path):
    pidfile = tmp_path/"descendant"
    script = ("import os,sys,subprocess,time;"
        "assert os.getsid(0)==os.getpid();"
        "p=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(60)']);"
        f"open({str(pidfile)!r},'w').write(str(p.pid));time.sleep(60)")
    controller, events = rig(script, deadline_ms=300)
    assert controller.start(URL)
    until(lambda: pidfile.exists())
    pid = int(pidfile.read_text())
    until(lambda: events, seconds=2)
    assert events[0][1] == "TIMED_OUT"
    def dead():
        try: return Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z"
        except FileNotFoundError: return True
    until(dead)


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
    from covearchive.github.token import StaticAuth
    c=ScanController(ROOT/"run_covearchive.py",auth=StaticAuth("ghp_"+"A"*36)); events=[]; pids=[]
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


@pytest.mark.parametrize('provider', ['github', 'codeberg', 'gitlab'])
def test_token_stdin_only_and_provider_guard(rig, provider, caplog):
    from covearchive.github.token import GitHubAuth
    auth = GitHubAuth(None); token = 'ghp_'+'A'*36; auth.save(token)
    script = ("import sys,json,time;r=json.loads(sys.stdin.buffer.readline());"
              + ("assert r['github_token']=='ghp_'+'A'*36;" if provider=='github' else "assert 'github_token' not in r;")
              + "sys.stderr.write(r.get('github_token','')+'TAIL');sys.stderr.flush();time.sleep(.1);sys.exit(3)")
    c, events = rig(script, auth=auth)
    host = dict(github='github.com',codeberg='codeberg.org',gitlab='gitlab.com')[provider]
    assert c.start(f'https://{host}/Owner/Repo'); job = c.job
    assert token not in repr(job)
    assert token not in job.process.program()
    assert token not in str(job.process.arguments()) and token not in str(job.process.processEnvironment().toStringList())
    until(lambda: events)
    assert events[0][2].code == 'process'
    assert token not in str(events[0][2]) and token not in repr(job) and token not in caplog.text
    assert events[0][2].detail['stderr'].endswith('TAIL')


@pytest.mark.parametrize('mode',['redirect','first','later','success','probe','probe_missing'])
def test_controller_explicit_worker_provenance(rig,mode):
    from covearchive.github.token import GitHubAuth
    from test_github_token import Vault,TOKEN
    auth=GitHubAuth(Vault()); auth.save(TOKEN)
    c,events=rig(later_401_script(mode),auth=auth); c.start(URL); until(lambda:events)
    result=events[0][2]; rejected=mode in ('first','later','probe','probe_missing')
    assert auth.status().state==('REJECTED' if rejected else 'SAVED')
    assert getattr(result,'token_rejected',False)==rejected
    if mode=='success': assert auth.rate is not None and auth.rate.authenticated
    elif mode.startswith('probe'): assert auth.rate is not None and not auth.rate.authenticated
    if mode=='probe': assert events[0][1]=='COMPLETED' and auth.token() is None
    elif mode!='success' and not mode.startswith('probe'): assert auth.rate is None
    if mode=='redirect': assert result.rate_limit['authenticated'] is False

def test_tag_with_auth_is_unsupported_without_token(rig):
    from covearchive.github.token import GitHubAuth
    auth = GitHubAuth(None); auth.save('ghp_'+'A'*36)
    script = reply().replace("'not_found'", "'unsupported_target'").replace(
        "r=json.loads(sys.stdin.buffer.readline());", "r=json.loads(sys.stdin.buffer.readline());assert 'github_token' not in r;")
    c, events = rig(script, auth=auth)
    assert c.start(URL+'/releases/tag/v1'); until(lambda: events)
    assert events[0][2].code == 'unsupported_target'

def later_401_script(mode='later'):
    return f"""
import sys,json
sys.path[:0]=[{str(ROOT/'src')!r},{str(ROOT/'tests')!r}]
from covearchive import scan_worker as w
import covearchive.github.client as github
from test_github_client import real_transport,link,page,ROOT
r=json.loads(sys.stdin.buffer.readline())
rate={{'X-RateLimit-Limit':'5000','X-RateLimit-Remaining':'4999','X-RateLimit-Reset':'9999999999'}}
scripts={{'redirect':[(302,{{'Location':ROOT+'/repositories/1/releases'}},None),(401,rate,None)],
'first':[(401,rate,None)],'later':[(200,{{'Link':link(page(2))}},None),(401,rate,None)],'success':[(200,rate,None)],'probe':[(401,rate,None),(200,rate,None)],'probe_missing':[(401,rate,None),(404,rate,None)]}}
session,client,calls,_=real_transport(scripts[{mode!r}],probe=not {mode!r}.startswith('probe'))
def factory(*,auth): client.auth=auth; return client
github.GitHubReleaseClient=factory
result=w.run_scan(r['request_id'],r['url'],token=r.get('github_token'))
assert [bool(q.headers.get('Authorization')) for q in calls]==([True,False] if {mode!r}=='redirect' or {mode!r}.startswith('probe') else [True]*len(calls))
sys.stdout.buffer.write(w._dump(result));sys.stdout.buffer.flush()
"""

@pytest.mark.parametrize('mode',['later','probe'])
def test_later_page_401_rejects_controller_token(rig,mode):
    from covearchive.github.token import GitHubAuth
    auth=GitHubAuth(None); auth.save('ghp_'+'A'*36)
    c,events=rig(later_401_script(mode),auth=auth); assert c.start(URL); until(lambda:events)
    failure=events[0][2]
    if mode=='later': assert failure.code=='incomplete_scan' and failure.detail['cause_code']=='unauthorized'
    assert auth.status().state=='REJECTED' and auth.token() is None
    c.command_factory=command(reply().replace('r=json.loads(sys.stdin.buffer.readline());',
        "r=json.loads(sys.stdin.buffer.readline());assert 'github_token' not in r;"))
    c.start(URL); until(lambda:len(events)==2)
    assert events[1][2].code=='not_found'

@pytest.mark.parametrize('host',['codeberg.org','gitlab.com'])
def test_foreign_rate_failure_preserves_github_rate(rig,host):
    from covearchive.github.token import GitHubAuth, RateLimit
    auth=GitHubAuth(None); rate=RateLimit(5000,4000,12345,True,100); auth.observe(rate,None)
    script=reply().replace("encode_error(r['request_id'],'not_found')",
        "encode_error(r['request_id'],'rate_limited',detail=dict(kind='primary',retry_after_seconds=10,retry_at=100,remaining=0,reset_at=123))")
    c,events=rig(script,auth=auth); c.start('https://'+host+'/Owner/Repo'); until(lambda:events)
    assert events[0][2].code=='rate_limited' and auth.rate==rate

def test_revoke_token_uses_existing_cancel_and_reap(rig):
    from covearchive.github.token import GitHubAuth
    auth=GitHubAuth(None); auth.save('ghp_'+'A'*36)
    c,events=rig(auth=auth); c.start(URL); until(lambda:c.job.process.processId()>0)
    pid=c.job.process.processId(); auth.clear(); c.revoke_token(); until(lambda:events)
    assert events[0][1]=='CANCELLED' and gone(pid) and c.job is None
    c.command_factory=command(reply().replace('r=json.loads(sys.stdin.buffer.readline());',
        "r=json.loads(sys.stdin.buffer.readline());assert 'github_token' not in r;"))
    c.start(URL); until(lambda:len(events)==2); assert events[1][2].code=='not_found'

@pytest.mark.parametrize('host,authenticated',[('github.com',True),('codeberg.org',True),('codeberg.org',False)])
def test_controller_ignores_forged_authenticated_observation(rig,host,authenticated):
    from covearchive.github.token import GitHubAuth
    auth=GitHubAuth(None)
    script=reply().replace("encode_error(r['request_id'],'not_found')",
        "encode_error(r['request_id'],'unauthorized',token_rejected=True,rate_limit=dict(limit=5000,remaining=1,reset=9999999999,authenticated=True))")
    script=script.replace('authenticated=True',f'authenticated={authenticated!r}')
    c,events=rig(script,auth=auth); c.start('https://'+host+'/Owner/Repo'); until(lambda:events)
    assert events[0][2].token_rejected is True and events[0][2].rate_limit['authenticated'] is authenticated
    assert auth.status().state=='NONE' and auth.rate is None

@pytest.mark.parametrize('change',['save','clear','replace'])
def test_controller_ignores_stale_rate(rig,change):
    from covearchive.github.token import GitHubAuth
    from test_github_token import TOKEN,OTHER
    auth=GitHubAuth(None)
    if change!='save': auth.save(TOKEN)
    script=reply().replace("encode_error(r['request_id'],'not_found')",
        "encode_error(r['request_id'],'not_found',rate_limit=dict(limit=5000,remaining=1,reset=9999999999,authenticated=False))").replace(
        'sys.stdout.buffer.write(', 'import time;time.sleep(.2);sys.stdout.buffer.write(')
    script=script.replace('authenticated=False',f'authenticated={change!="save"!r}')
    c,events=rig(script,auth=auth); c.start(URL)
    if change=='clear': auth.clear()
    else: auth.save(OTHER)
    until(lambda:events); assert events[0][2].rate_limit is not None and auth.rate is None

def test_scan_anonymous_rate_after_midflight_rejection(rig):
    from covearchive.github.token import GitHubAuth
    token='ghp_'+'A'*36; auth=GitHubAuth(None); auth.save(token)
    script=reply().replace("encode_error(r['request_id'],'not_found')",
        "encode_error(r['request_id'],'unauthorized',token_rejected=True,rate_limit=dict(limit=60,remaining=59,reset=9999999999,authenticated=False))")
    c,events=rig(script,auth=auth); c.start(URL); auth.reject(token); until(lambda:events)
    assert auth.status().state=='REJECTED' and auth.rate is not None and auth.rate.authenticated is False
