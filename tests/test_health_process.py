"""Real killable Health checks, including blocked scan and audit phases."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
import sys

import pytest
from PySide6.QtCore import QTimer
from covearchive.library.model import Repository
from covearchive.ui.health_process import HealthController, worker_command
from test_scan_process import APP, ROOT, command, gone, until

REPO = Repository('github', 'https://github.com/Owner/Repo')


@pytest.mark.parametrize('phase', ['scan', 'audit'])
@pytest.mark.parametrize('stop', ['cancel', 'deadline', 'shutdown'])
def test_blocked_phases_responsive_killed_and_reaped(tmp_path, phase, stop):
    marker = tmp_path / 'phase'
    script = (f'import sys,time;sys.path.insert(0,{str(ROOT / "src")!r});'
              'from covearchive import health_worker as w;'
              'from covearchive.archive import availability as a;'
              f'define={phase!r};'
              f'exec("def blocked(*args, **kwargs):\\n open({marker.as_posix()!r}, \'w\').write(define)\\n time.sleep(60)");'
              + ('a._anonymous_scan=blocked;' if phase == 'scan' else
                 'a._anonymous_scan=lambda _: None;a.audit_project=blocked;') + 'w.main()')
    c = HealthController(command_factory=command(script), deadline_ms=350 if stop == 'deadline' else 5000)
    events = []; ticks = []; timer = QTimer(); timer.timeout.connect(lambda: ticks.append(1)); timer.start(10)
    c.terminal.connect(lambda *args: events.append(args))
    try:
        assert c.start(REPO, str(tmp_path))
        until(marker.exists); pid = c.job.process.processId()
        assert not c.start(REPO, str(tmp_path))
        if stop == 'cancel': c.cancel()
        elif stop == 'shutdown': assert c.shutdown(1000)
        until(lambda: bool(events), seconds=2)
        assert events[0][1] == ('TIMED_OUT' if stop == 'deadline' else 'CANCELLED')
        assert len(events) == 1 and gone(pid) and c.job is None
        assert ticks
    finally:
        timer.stop(); assert c.shutdown(1000)


def test_command_environment_and_frozen_route(monkeypatch):
    assert worker_command(ROOT / 'run_covearchive.py')[-1] == '--health-worker'
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert worker_command(None) == [str(Path(sys.executable).resolve()), '--health-worker']
    monkeypatch.delattr(sys, 'frozen'); monkeypatch.setenv('GITHUB_TOKEN', 'SECRET')
    monkeypatch.setenv('GH_TOKEN', 'SECRET'); monkeypatch.setenv('HOME', '/credential-profile')
    c = HealthController(command_factory=command('import time;time.sleep(60)'))
    try:
        c.start(REPO, '')
        env = c.job.process.processEnvironment()
        assert not any(env.contains(k) for k in ('GITHUB_TOKEN', 'GH_TOKEN', 'HOME', 'PYTHONPATH'))
        assert c.auth is None and c.job.token_sent is None
    finally: assert c.shutdown(1000)


@pytest.mark.parametrize('payload', ["b'bad\\n'", "b'x' * 65537"])
def test_bad_or_oversize_response_is_bounded_failure(payload, monkeypatch):
    oversize = payload != "b'bad\\n'"
    tail = ';import time;time.sleep(60)' if oversize else ''
    c = HealthController(command_factory=command(f'import sys;sys.stdin.buffer.readline();sys.stdout.buffer.write({payload});sys.stdout.flush()' + tail))
    events = []; c.terminal.connect(lambda *a: events.append(a))
    if oversize:
        monkeypatch.setattr(c, '_decode_response', lambda *args: pytest.fail('Oversized output reached the decoder'))
    try:
        c.start(REPO, ''); until(lambda: bool(events))
        assert events[0][1] == 'FAILED' and c.job is None
        assert c.response_limit == 65536
    finally: assert c.shutdown(1000)


def test_health_controller_cannot_receive_auth():
    with pytest.raises(TypeError): HealthController(auth=object())


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux process-group supervision')
def test_health_worker_descendants_terminated(tmp_path):
    marker = tmp_path / 'child'
    script = ('import subprocess,sys,time,os;assert os.getsid(0)==os.getpid();'
              'child=subprocess.Popen([sys.executable,"-I","-B","-c","import time;time.sleep(60)"]);'
              f'open({str(marker)!r},"w").write(str(child.pid));time.sleep(60)')
    c = HealthController(command_factory=command(script), deadline_ms=300)
    events = []; c.terminal.connect(lambda *a: events.append(a))
    try:
        c.start(REPO, ''); until(marker.exists); pid = int(marker.read_text())
        until(lambda: bool(events))
        def dead():
            try: return Path(f'/proc/{pid}/stat').read_text().split()[2] == 'Z'
            except FileNotFoundError: return True
        until(dead); assert c.job is None
    finally: assert c.shutdown(1000)


@pytest.mark.parametrize('phase', ['scan', 'audit', 'inventory'])
@pytest.mark.parametrize('stop', ['cancel', 'deadline', 'shutdown'])
def test_batch_blocked_workers_remain_responsive_and_reap(tmp_path, monkeypatch, phase, stop):
    import time
    from PySide6.QtTest import QTest
    from covearchive.ui.scan_process import ScanController
    from covearchive.ui.health_updates import HealthUpdates
    from covearchive.ui.health_panel import Checked
    from covearchive.library.model import Library
    from covearchive.archive import availability as availability
    from test_archive_availability import current, upstream
    from test_archive_health import archive, REPO as TARGET
    from test_health_updates import Bridge
    for key in ('HOME', 'LOCALAPPDATA', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    root, _, _ = archive(tmp_path)
    marker = tmp_path / 'blocked-phase'
    prefix = f'import os,sys;sys.path[:0]=[{str(ROOT / "src")!r},{str(ROOT / "tests")!r}];'
    scan_script = prefix + (f'import time;sys.stdin.buffer.readline();open({str(marker)!r},"w").write("scan");time.sleep(60)' if phase == 'scan' else
        'import json;from covearchive.scan_worker import encode_success;from test_archive_availability import current,upstream;'
        'request=json.loads(sys.stdin.buffer.readline());assert "github_token" not in request;'
        'sys.stdout.buffer.write(encode_success(request["request_id"],upstream(current(id=2,tag_name="v2")).catalog));sys.stdout.buffer.flush();os._exit(0)')
    health_script = prefix + ('import time;from covearchive import health_worker as w;'
        'from covearchive.archive import availability as a;from test_archive_availability import current,upstream;'
        'a._anonymous_scan=lambda _:upstream(current(id=2,tag_name="v2"));'
        f'exec("def blocked(*args, **kwargs):\\n open({marker.as_posix()!r}, \'w\').write({phase!r})\\n time.sleep(60)");' +
        ('a.audit_project=blocked;' if phase == 'audit' else 'w.audit_project=blocked;') + 'os._exit(w.main())')
    deadline = 600 if stop == 'deadline' else 5000
    scan = ScanController(command_factory=command(scan_script), deadline_ms=deadline)
    check = HealthController(command_factory=command(health_script), deadline_ms=deadline)
    lib = Library().add_repository('https://github.com/Owner/Repo'); repo = lib.repositories[0]
    status = availability.check_repository(str(root), TARGET, scan=lambda _: upstream(current(id=2, tag_name='v2')))
    checked = Checked(status, 'now', str(root)); results = {repo.identity: checked}
    settings = dict(repositories={repo.ref: dict(include=[], exclude=[], extensions=['.zip'], prereleases=False)})
    bridge = Bridge(); updates = HealthUpdates(check, scan, bridge, lambda: lib, lambda: results, lambda: settings)
    ticks = []; timer = QTimer(); timer.timeout.connect(lambda: ticks.append(time.monotonic())); timer.start(10)
    try:
        updates.prepare([(repo.identity, checked)], str(root)); until(marker.exists, seconds=2)
        active = scan if phase == 'scan' else check
        pid = active.job.process.processId(); QTest.qWait(30)
        if stop == 'cancel': updates.cancel()
        elif stop == 'shutdown': assert updates.shutdown(1000)
        until(lambda: not updates.running, seconds=2)
        assert gone(pid) and scan.job is None and check.job is None and not bridge.calls
        assert len(ticks) >= 2 and max((b-a for a,b in zip(ticks,ticks[1:])), default=0) < .25
    finally:
        timer.stop(); assert updates.shutdown(1000)
