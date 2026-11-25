"""Strict relocation IPC, Qt-free dispatch and real supervised lifecycle."""
from dataclasses import replace
import errno
from io import BytesIO
import json
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
import subprocess
import sys
import time

import pytest
from PySide6.QtCore import QTimer

from covearchive import relocation as r, relocation_worker as w
from covearchive.ui.relocation_process import RelocationController, worker_command
from test_relocation import A, B, C, Provider, check
from test_scan_process import APP, ROOT, command, gone, until

RID = 'a' * 32


def test_wire_roundtrip_and_request_has_no_archive_or_auth_access(tmp_path):
    path = tmp_path / 'relocation.json'
    assert w.parse_request(w.encode_request(RID, A, path)) == (RID, A, str(path))
    result = check(path, Provider())
    assert w.decode_response(w.encode_response(RID, A, result), RID, A) == result
    assert set(json.loads(w.encode_request(RID, A, path))) == {'protocol', 'request_id', 'provider', 'url', 'path'}


@pytest.mark.parametrize('missing', ['stdin', 'stdout'])
def test_worker_exits_cleanly_when_stream_is_unavailable(monkeypatch, missing):
    streams = {'stdin': BytesIO(), 'stdout': BytesIO()}
    streams[missing] = None
    with monkeypatch.context() as patch:
        patch.setattr(sys, missing, None)
        patch.setattr(sys, 'platform', 'linux')
        status = w.main(**streams)
    assert status == 2


def test_worker_recovers_inherited_windows_pipes(tmp_path, monkeypatch):
    import ctypes
    from types import SimpleNamespace

    from covearchive import scheduled
    from covearchive.library.model import Repository

    repo = Repository('gitlab', 'https://gitlab.com/o/r')
    stdin = BytesIO(w.encode_request(RID, repo, tmp_path / 'r.json'))
    stdout = BytesIO()
    handles = {-10: 10, -11: 11}
    pipes = {10: stdin, 11: stdout}
    opened = []

    def fdopen(fd, mode, *, buffering):
        opened.append((fd, mode, buffering))
        return pipes[fd]

    with monkeypatch.context() as patch:
        patch.setattr(sys, 'stdin', None)
        patch.setattr(sys, 'stdout', None)
        patch.setattr(sys, 'platform', 'win32')
        patch.setattr(ctypes, 'WinDLL', lambda *a, **k: SimpleNamespace(GetStdHandle=lambda n: handles[n]),
                      raising=False)
        patch.setitem(sys.modules, 'msvcrt', SimpleNamespace(open_osfhandle=lambda handle, flags: handle))
        patch.setattr(scheduled, 'os', SimpleNamespace(O_BINARY=0x8000, O_RDONLY=0, O_WRONLY=1, fdopen=fdopen))
        patch.setattr(w, 'start_watchdog', lambda *args: None)
        status = w.main()

    assert status == 0 and opened == [(10, 'rb', 0), (11, 'wb', 0)]
    assert w.decode_response(stdout.getvalue(), RID, repo).outcome is r.Outcome.UNSUPPORTED


@pytest.mark.parametrize('prior,outcome', [(B, r.Outcome.RENAMED), (C, r.Outcome.TRANSFERRED)],
                         ids=['rename', 'transfer'])
def test_worker_reports_committed_move_back_to_initial_alias(tmp_path, monkeypatch, prior, outcome):
    path = tmp_path / 'r.json'
    provider = Provider()
    provider.move(prior)
    first = check(path, provider)
    provider.move(r._url(A.url))
    original = r.check_location
    monkeypatch.setattr(r, 'check_location', lambda repo, path:
                       original(repo, path, client_factory=lambda: provider, clock=lambda: 1234))
    monkeypatch.setattr(w, 'start_watchdog', lambda *args: None)
    stdout = BytesIO()
    status = w.main(BytesIO(w.encode_request(RID, A, path)), stdout)
    history = r._history(r._record(r.RelocationStore(path).load(), 1))
    assert history == first.locations + ((A.url, 1234),)
    assert status == 0
    result = w.decode_response(stdout.getvalue(), RID, A)
    assert result.outcome is outcome and result.verified_url == A.url and result.locations == history


@pytest.mark.parametrize('field,value', [('protocol', True), ('protocol', 2), ('request_id', 'bad'),
    ('path', 'relative'), ('path', '/tmp/\0'), ('github_token', 'secret'), ('url', 'https://user@github.com/o/r')])
def test_invalid_requests_fail_closed(tmp_path, field, value):
    data = json.loads(w.encode_request(RID, A, tmp_path / 'r.json')); data[field] = value
    with pytest.raises(w.ProtocolError): w.parse_request(w._dump(data))


@pytest.mark.parametrize('field,value', [('repository_id', True), ('repository_id', 2**63), ('outcome', 'unknown'),
    ('reason', 'untrusted text'), ('no_prior_identity', 1), ('extra', 0),
    ('prior_location_count', True), ('prior_location_count', -1), ('prior_location_count', 2),
    ('verified_url', 'https://user@github.com/o/r'), ('requested_url', 'https://github.com/o/r'),
    ('locations', [['https://gitlab.com/o/r', 0]]), ('outcome', 'URL_REUSED'), ('outcome', 'UNSUPPORTED')])
def test_invalid_responses_fail_closed(tmp_path, field, value):
    result = check(tmp_path / 'r.json', Provider())
    data = json.loads(w.encode_response(RID, A, result)); data['result'][field] = value
    with pytest.raises(w.ProtocolError): w.decode_response(w._dump(data), RID, A)


@pytest.mark.parametrize('outcome', [r.Outcome.UNCHANGED, r.Outcome.RENAMED, r.Outcome.TRANSFERRED])
@pytest.mark.parametrize('observed_id', [None, 2])
@pytest.mark.parametrize('direction', ['encode', 'decode'])
def test_success_requires_matching_observed_identity(tmp_path, outcome, observed_id, direction):
    path = tmp_path / 'r.json'; p = Provider(); result = check(path, p)
    if outcome != r.Outcome.UNCHANGED:
        p.move(B if outcome == r.Outcome.RENAMED else C); result = check(path, p)
    assert result.outcome is outcome and result.observed_id == result.repository_id == 1
    if direction == 'encode':
        with pytest.raises(w.ProtocolError): w.encode_response(RID, A, replace(result, observed_id=observed_id))
    else:
        data = json.loads(w.encode_response(RID, A, result)); data['result']['observed_id'] = observed_id
        with pytest.raises(w.ProtocolError): w.decode_response(w._dump(data), RID, A)


def test_stable_id_observation_with_missing_old_url_roundtrips(tmp_path):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); p.move(B); p.by_url.clear()
    result = check(path, p)
    assert result.outcome is r.Outcome.RENAMED and result.observed_id == result.repository_id == 1
    assert w.decode_response(w.encode_response(RID, A, result), RID, A) == result


@pytest.mark.parametrize('outcome,changes', [
    (r.Outcome.INCONCLUSIVE, dict(no_prior_identity=True, reason='no_prior_identity')),
    (r.Outcome.INCONCLUSIVE, dict(reason='ok')),
    (r.Outcome.INCONCLUSIVE, dict(observed_id=1)),
    (r.Outcome.INCONCLUSIVE, dict(locations=())),
    (r.Outcome.UNAVAILABLE, dict(reason='network')),
    (r.Outcome.UNAVAILABLE, dict(no_prior_identity=True)),
    (r.Outcome.UNAVAILABLE, dict(observed_id=1)),
    (r.Outcome.UNAVAILABLE, dict(locations=())),
    (r.Outcome.URL_REUSED, dict(locations=())),
    (r.Outcome.RENAMED, dict(outcome=r.Outcome.UNCHANGED)),
    (r.Outcome.TRANSFERRED, dict(outcome=r.Outcome.UNCHANGED)),
], ids=['inconclusive-first-observation', 'inconclusive-success-reason', 'inconclusive-observed-id',
        'inconclusive-no-history', 'unavailable-failure-reason', 'unavailable-first-observation',
        'unavailable-observed-id', 'unavailable-no-history', 'reuse-no-history',
        'rename-as-unchanged', 'transfer-as-unchanged'])
def test_worker_rejects_contradictory_evidence(tmp_path, outcome, changes):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p)
    if outcome is r.Outcome.INCONCLUSIVE:
        p.by_id[1] = r.github.NetworkError()
    elif outcome in (r.Outcome.UNAVAILABLE, r.Outcome.URL_REUSED):
        p.by_id.clear(); p.by_url.clear()
        if outcome is r.Outcome.URL_REUSED: p.by_url[A.ref] = (2, r._url(A.url))
    else:
        p.move(B if outcome is r.Outcome.RENAMED else C)
    result = check(path, p)
    assert result.outcome is outcome
    data = json.loads(w.encode_response(RID, A, result)); data['result'].update(changes)
    with pytest.raises(w.ProtocolError): w.decode_response(w._dump(data), RID, A)


@pytest.mark.parametrize('reason,history_allowed,empty_allowed', [
    ('state_busy', False, True), ('state_invalid', False, True),
    ('state_write', False, True), ('limit', False, True), ('tracked_alias', False, True),
    ('conflict', True, True), ('cancelled', True, True), ('network', True, True),
    ('rate_limited', True, True), ('invalid_response', True, True), ('not_found', True, False),
])
@pytest.mark.parametrize('has_history', [False, True])
def test_inconclusive_reason_requires_producer_evidence_shape(reason, history_allowed, empty_allowed, has_history):
    result = r.Result(r.Outcome.INCONCLUSIVE, A.url, reason=reason)
    if has_history:
        result = replace(result, repository_id=1, locations=((A.url, 1234),), prior_location_count=1)
    raw = w._dump(dict(protocol=w.PROTOCOL_VERSION, request_id=RID, identity=A.identity,
                       result=w.asdict(result)))
    allowed = history_allowed if has_history else empty_allowed
    if allowed:
        assert w.decode_response(raw, RID, A) == result
    else:
        with pytest.raises(w.ProtocolError): w.decode_response(raw, RID, A)


def test_url_reuse_cannot_claim_appended_history(tmp_path):
    path = tmp_path / 'r.json'; p = Provider(); first = check(path, p)
    p.move(B); p.by_url[A.ref] = (2, r._url(A.url))
    reused = check(path, p)
    assert reused.outcome is r.Outcome.URL_REUSED and reused.verified_url == r.canonical_url(B)
    assert reused.locations == first.locations and reused.prior_location_count == 1
    assert w.decode_response(w.encode_response(RID, A, reused), RID, A) == reused
    forged = replace(reused, locations=first.locations + ((reused.verified_url, 1234),))
    with pytest.raises(w.ProtocolError): w.encode_response(RID, A, forged)


@pytest.mark.parametrize('field,value', [('no_prior_identity', True), ('reason', 'no_prior_identity'), ('reason', 'not_found')])
@pytest.mark.parametrize('direction', ['encode', 'decode'])
def test_reuse_cannot_claim_initial_or_unavailable_verified_identity(tmp_path, field, value, direction):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); p.move(B)
    p.by_url[A.ref] = (2, r.RepositoryTarget('Owner', 'Repo')); result = check(path, p)
    assert result.outcome is r.Outcome.URL_REUSED
    if direction == 'encode':
        with pytest.raises(w.ProtocolError): w.encode_response(RID, A, replace(result, **{field: value}))
    else:
        data = json.loads(w.encode_response(RID, A, result)); data['result'][field] = value
        with pytest.raises(w.ProtocolError): w.decode_response(w._dump(data), RID, A)


@pytest.mark.parametrize('field,value', [('no_prior_identity', True), ('reason', 'ok')])
@pytest.mark.parametrize('direction', ['encode', 'decode'])
def test_unsupported_response_cannot_claim_success_or_prior_identity(tmp_path, field, value, direction):
    from covearchive.library.model import Repository
    repo = Repository('gitlab', 'https://gitlab.com/o/r')
    result = r.check_location(repo, tmp_path / 'r.json')
    if direction == 'encode':
        with pytest.raises(w.ProtocolError): w.encode_response(RID, repo, replace(result, **{field: value}))
    else:
        data = json.loads(w.encode_response(RID, repo, result)); data['result'][field] = value
        with pytest.raises(w.ProtocolError): w.decode_response(w._dump(data), RID, repo)


def test_mismatched_envelopes_duplicates_and_oversized_response(tmp_path):
    result = check(tmp_path / 'r.json', Provider()); raw = w.encode_response(RID, A, result)
    with pytest.raises(w.ProtocolError): w.decode_response(raw, 'b' * 32, A)
    with pytest.raises(w.ProtocolError): w.decode_response(raw.replace(b'"protocol":1', b'"protocol":1,"protocol":1'), RID, A)
    with pytest.raises(w.ProtocolError): w.decode_response(b'x' * (w.MAX_RESPONSE_BYTES + 1), RID, A)


def test_response_requires_pre_check_history_length(tmp_path):
    result = check(tmp_path / 'r.json', Provider())
    data = json.loads(w.encode_response(RID, A, result))
    del data['result']['prior_location_count']
    with pytest.raises(w.ProtocolError):
        w.decode_response(w._dump(data), RID, A)


def test_launcher_dispatch_is_qt_free_and_unsupported_is_offline(tmp_path):
    from covearchive.library.model import Repository
    repo = Repository('gitlab', 'https://gitlab.com/o/r')
    script = ('import sys,runpy;sys.modules["PySide6"]=None;'
              'sys.argv=[sys.argv[1],"--relocation-worker"];runpy.run_path(sys.argv[0],run_name="__main__")')
    process = subprocess.Popen([sys.executable, '-I', '-B', '-c', script, str(ROOT / 'run_covearchive.py')],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        process.stdin.write(w.encode_request(RID, repo, tmp_path / 'r.json')); process.stdin.flush()
        assert process.wait(timeout=5) == 0, process.stderr.read()
        assert w.decode_response(process.stdout.read(), RID, repo).outcome is r.Outcome.UNSUPPORTED
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=3)
        process.stdin.close(); process.stdout.close(); process.stderr.close()
    assert not (tmp_path / 'r.json').exists()


@pytest.mark.parametrize('phase', ['metadata', 'persistence'])
@pytest.mark.parametrize('stop', ['cancel', 'deadline', 'shutdown'])
@pytest.mark.parametrize('directory', ["with'apostrophe", 'with space', 'with-é'],
                         ids=['apostrophe', 'space', 'unicode'])
def test_blocked_workers_responsive_cancel_deadline_shutdown(tmp_path, phase, stop, directory):
    phase_path = tmp_path / directory
    try:
        phase_path.mkdir()
    except (OSError, UnicodeError) as exc:
        if directory == 'with-é' and (isinstance(exc, UnicodeError) or exc.errno in (errno.EILSEQ, errno.EINVAL)):
            pytest.skip(f'Filesystem rejected Unicode path: {exc}')
        raise
    marker = phase_path / 'phase'
    script = """\
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from covearchive import relocation as r, relocation_worker as w

def block(*args, **kwargs):
    Path(sys.argv[2]).write_text('blocked', encoding='utf-8')
    time.sleep(60)

if sys.argv[3] == 'metadata':
    r.github.GitHubRepositoryClient.repository = block
else:
    r.github.GitHubRepositoryClient.repository = lambda *a, **k: (1, r.RepositoryTarget('Owner', 'Repo'))
    r.RelocationStore.update = block
w.main()
"""

    def worker(launcher):
        return [*command(script)(launcher), str(ROOT / 'src'), str(marker), phase]

    c = RelocationController(command_factory=worker, deadline_ms=500 if stop == 'deadline' else 5000)
    events = []; ticks = []; timer = QTimer(); timer.timeout.connect(lambda: ticks.append(1)); timer.start(10)
    c.terminal.connect(lambda *args: events.append(args))
    try:
        assert c.start(A, tmp_path / 'r.json')
        job = c.job
        if stop == 'deadline':
            # The short deadline measures time in the blocked phase.
            job.deadline.stop()
        until(lambda: marker.exists() or events, seconds=5)
        assert marker.exists() and not events
        pid = job.process.processId()
        assert not c.start(A, tmp_path / 'r.json')
        if stop == 'cancel': c.cancel()
        elif stop == 'shutdown': assert c.shutdown(1000)
        else:
            job.started = time.monotonic()
            job.deadline.start(c.deadline_ms)
        until(lambda: bool(events), seconds=2)
        assert events[0][1] == ('TIMED_OUT' if stop == 'deadline' else 'CANCELLED')
        assert len(events) == 1 and gone(pid) and c.job is None and ticks
        assert not (tmp_path / 'r.json').exists()
    finally: timer.stop(); assert c.shutdown(1000)


def test_worker_command_and_environment_exclude_credentials(tmp_path, monkeypatch):
    assert worker_command(ROOT / 'run_covearchive.py')[-1] == '--relocation-worker'
    with monkeypatch.context() as m:
        m.setattr(sys, 'frozen', True, raising=False)
        assert worker_command(None) == [str(Path(sys.executable).resolve()), '--relocation-worker']
    for key in ('GITHUB_TOKEN', 'GH_TOKEN', 'HOME', 'LOCALAPPDATA'): monkeypatch.setenv(key, 'SECRET')
    c = RelocationController(command_factory=command('import time;time.sleep(60)'))
    try:
        assert c.start(A, tmp_path / 'r.json')
        env = c.job.process.processEnvironment()
        assert not any(env.contains(key) for key in ('GITHUB_TOKEN', 'GH_TOKEN', 'HOME', 'LOCALAPPDATA', 'PYTHONPATH'))
        assert c.auth is None and c.job.token_sent is None
        with pytest.raises(TypeError): RelocationController(auth=object())
    finally: assert c.shutdown(1000)


@pytest.mark.parametrize('payload', ["b'bad\\n'", "b'x'*65537"])
def test_bad_or_oversized_worker_response_is_reaped(tmp_path, payload):
    c = RelocationController(command_factory=command(
        f'import sys;sys.stdin.buffer.readline();sys.stdout.buffer.write({payload});sys.stdout.flush()'))
    events = []; c.terminal.connect(lambda *args: events.append(args))
    try:
        c.start(A, tmp_path / 'r.json'); until(lambda: bool(events))
        assert events[0][1] == 'FAILED' and c.job is None
    finally: assert c.shutdown(1000)
