"""Offline protocol, serialization and worker self-defence behavior."""

import io
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import queue

import pytest

from covearchive import scan_worker as w
from covearchive.github.models import ReleaseCatalog, parse_release
from covearchive.github.urls import RepositoryTarget
from covearchive.github.client import IncompleteScanError, NetworkError, RateLimitError

RID = "a" * 32
URL = "https://github.com/Owner/Repo"
ROOT = Path(__file__).resolve().parents[1]
DATE = "2026-11-13T12:00:00Z"


def asset(id=1, name="app.zip", **changes):
    obj = dict(id=id, name=name, size=2**63, state="uploaded", content_type="opaque",
                browser_download_url="https://example/a", created_at=DATE, updated_at=DATE,
                )
    obj.update(changes)
    return obj


def release(id=1, **changes):
    obj = dict(id=id, name=f"Release {id}", tag_name=f"v{id}", draft=False,
               prerelease=False, created_at=DATE, published_at=DATE, assets=[asset()])
    obj.update(changes)
    return obj


def catalog(*items):
    return ReleaseCatalog(RepositoryTarget("Owner", "Repo"),
                          tuple(parse_release(x) for x in items), 1, 1, 0, (), "", True)


def wire(obj):
    return (json.dumps(obj) + "\n").encode()


def test_request_roundtrip():
    assert w.parse_request(w.encode_request(RID, URL)) == (RID, URL)


@pytest.mark.parametrize("change", [dict(protocol=True), dict(protocol=1), dict(action="exec"),
    dict(request_id="A"*32), dict(url=True), dict(url="x"*2049), dict(extra=0)])
def test_request_strict(change):
    obj = dict(protocol=2, request_id=RID, action="scan", url=URL) | change
    with pytest.raises(w.ProtocolError):
        w.parse_request(wire(obj))


@pytest.mark.parametrize("data", [b'{}', b'{}\n{}\n', b'\xff\n', b'{"a":1,"a":2}\n',
                                     b'{"a":NaN}\n', b'x'*4097])
def test_request_hostile(data):
    with pytest.raises(w.ProtocolError):
        w.parse_request(data)


def test_success_roundtrip_omits_notes():
    result = w.decode_response(w.encode_success(RID, catalog(release(body="secret notes",
                              tarball_url="https://example/t"))), RID)
    assert result.catalog.releases[0].body is None
    assert result.catalog.releases[0].tarball_url is None
    assert result.catalog.releases[0].assets[0].size == 2**63


@pytest.mark.parametrize("change", [dict(protocol=True), dict(request_id="b"*32),
    dict(status="maybe"), dict(extra=0), dict(result={}), dict(protocol=1)])
def test_response_strict(change):
    obj = json.loads(w.encode_success(RID, catalog(release()))) | change
    with pytest.raises(w.ProtocolError):
        w.decode_response(wire(obj), RID)


@pytest.mark.parametrize("data", [b'{}\n', b'{}\n{}\n', b'{"x":NaN}\n',
    b'{"x":1,"x":1}\n', b'\xff\n', b'['*1500+b'0'+b']'*1500+b'\n'])
def test_UI_15_hostile_response(data):
    with pytest.raises(w.ProtocolError):
        w.decode_response(data, RID)


@pytest.mark.parametrize("field,value", [("pages", True), ("requests", -1),
    ("redirects", 2**100), ("owner", "bad/owner"), ("releases", [release()]*10001),
    ("duplicate_release_ids", [True])])
def test_result_bounds(field, value):
    obj = json.loads(w.encode_success(RID, catalog()))
    obj["result"][field] = value
    with pytest.raises(w.ProtocolError):
        w.decode_response(wire(obj), RID)


@pytest.mark.parametrize("patch", [dict(assets=[asset()]*1001), dict(name="x"*8193),
    dict(id=True), dict(assets=[asset(size=-1)])])
def test_release_bounds_and_backend_validation(patch):
    obj = json.loads(w.encode_success(RID, catalog()))
    obj["result"]["releases"] = [release(**patch)]
    with pytest.raises(w.ProtocolError):
        w.decode_response(wire(obj), RID)


def test_UI_16_worker_size(monkeypatch):
    monkeypatch.setattr(w, "MAX_RESPONSE_BYTES", 400)
    encoded = w.encode_success(RID, catalog(release()))
    assert len(encoded) <= w.MAX_RESPONSE_BYTES
    result = w.decode_response(encoded, RID)
    assert result.code == "result_too_large"
    with pytest.raises(w.ProtocolError):
        w.decode_response(b"x"*401, RID)


@pytest.mark.parametrize("exc,code", [(NetworkError("SECRET"), "network"),
    (IncompleteScanError(1, "error", NetworkError()), "incomplete_scan"),
    (RateLimitError(kind="primary", status=403, retry_after_seconds=30, retry_at=1030), "rate_limited"),
    (ValueError("SECRET"), "internal_error")])
def test_errors_closed_sanitized(exc, code):
    data = w.encode_error(RID, exc)
    assert b"SECRET" not in data
    assert w.decode_response(data, RID).code == code
    obj = json.loads(data)
    obj["error"]["code"] = "unknown"
    with pytest.raises(w.ProtocolError):
        w.decode_response(wire(obj), RID)


@pytest.mark.parametrize("url,code", [("invalid", "invalid_url"),
    (URL+"/releases/tag/v1", "unsupported_target")])
def test_run_scan_revalidates_without_client(url, code):
    def forbidden():
        pytest.fail("client created for invalid target")
    assert w.run_scan(RID, url, client_factory=forbidden)["error"]["code"] == code


def test_run_scan_reuses_backend():
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def discover_releases(self, target):
            assert target == RepositoryTarget("Owner", "Repo")
            return catalog(release())
    result = w.decode_response(wire(w.run_scan(RID, URL, client_factory=Client)), RID)
    assert result.catalog.releases[0].id == 1


@pytest.mark.parametrize("eof", [True, False])
def test_watchdog_eof_and_deadline(eof):
    event = threading.Event()
    class Pipe:
        def read(self, n):
            if not eof: event.wait()
            return b""
    called = threading.Event()
    begin=time.monotonic()
    w.start_watchdog(Pipe(), .05 if not eof else 1, lambda code: called.set())
    try:
        if not eof: assert not called.wait(.02)
        assert called.wait(.2)
        if not eof: assert time.monotonic()-begin >= .045
    finally: event.set()


def test_real_worker_invalid_url_and_watchdog():
    child = subprocess.Popen([sys.executable, "-I", "-B", str(ROOT/"run_covearchive.py"),
                              "--scan-worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    child.stdin.write(w.encode_request(RID, "invalid")); child.stdin.flush()
    replies=queue.Queue()
    threading.Thread(target=lambda:replies.put(child.stdout.readline()),daemon=True).start()
    try: data=replies.get(timeout=1.5)
    except queue.Empty:
        child.kill(); child.communicate(timeout=1.5); pytest.fail("worker response deadline")
    assert w.decode_response(data, RID).code == "invalid_url"
    child.stdin.close()
    assert child.wait(timeout=1.5) == 0
    script = (f"import sys,time;sys.path.insert(0,{str(ROOT/'src')!r});"
              "from covearchive.scan_worker import start_watchdog;import os;"
              "start_watchdog(sys.stdin.buffer,1,os._exit);time.sleep(10)")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script],
                            input=b"", capture_output=True, timeout=1.5)
    assert result.returncode != 0


def test_R4_worker_bounded_diagnostics(capsys):
    def broken(): raise RuntimeError("bad\n" + "x"*1000)
    assert w.run_scan(RID, URL, client_factory=broken)["error"]["code"] == "internal_error"
    line = capsys.readouterr().err
    assert "RuntimeError" in line and len(line.splitlines()) == 1 and len(line) < 350
    assert w.main(stdin=io.BytesIO(b"bad\n"), stdout=io.BytesIO()) == 2
    assert "ProtocolError" in capsys.readouterr().err


@pytest.mark.parametrize("provider,owner", [("github", "Owner"), ("codeberg", "Owner"), ("codeberg", "a"*40)])
def test_provider_wire_roundtrip(provider, owner):
    target = RepositoryTarget(owner, "Repo", provider, "github.com" if provider == "github" else "codeberg.org")
    source = replace(catalog(release(assets=[asset(state="external", content_type="", browser_download_url="")])), repository=target)
    data = w.encode_success(RID, source)
    result = w.decode_response(data, RID).catalog
    assert result.repository == target
    assert result.releases == source.releases
    assert json.loads(data)["protocol"] == 2


@pytest.mark.parametrize("patch,missing", [
    ({"provider": "github", "host": "codeberg.org"}, None),
    ({"provider": "codeberg", "host": "github.com"}, None),
    ({"provider": "other", "host": "codeberg.org"}, None),
    ({"provider": True}, None), ({"host": None}, None), ({"extra": 1}, None),
    ({"owner": "bad/owner"}, None), ({"repo": "Repo?query"}, None),
    ({}, "provider"), ({}, "host"),
])
def test_provider_wire_identity_is_strict(patch, missing):
    obj = json.loads(w.encode_success(RID, catalog()))
    obj["result"].update(patch)
    if missing: obj["result"].pop(missing, None)
    with pytest.raises(w.ProtocolError): w.decode_response(wire(obj), RID)


@pytest.mark.parametrize("provider", ["github", "codeberg"])
def test_run_scan_dispatches_real_provider_factory(monkeypatch, provider):
    import covearchive.github.client as github
    import covearchive.forgejo as forgejo
    target = RepositoryTarget("Owner", "Repo", provider, "github.com" if provider == "github" else "codeberg.org")
    class Client:
        def __init__(self, **options): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def discover_releases(self, actual):
            assert actual == target
            return replace(catalog(release()), repository=target)
    monkeypatch.setattr(github if provider == "github" else forgejo,
                        "GitHubReleaseClient" if provider == "github" else "ForgejoReleaseClient", Client)
    result = w.decode_response(wire(w.run_scan(RID, f"https://{target.host}/Owner/Repo")), RID)
    assert result.catalog.repository == target


@pytest.mark.parametrize('tag',['1.0','007'])
def test_gitlab_wire(tag):
    from test_gitlab import Wire, TARGET
    wire_source=Wire(); source=wire_source.client().discover_releases(TARGET)
    repo=replace(source.repository,owner='g/'+'sub/'*10+'end')
    source=replace(source,repository=repo,releases=(replace(source.releases[0],id=tag,tag_name=tag),))
    encoded=w.encode_success(RID,source); decoded=w.decode_response(encoded,RID).catalog
    assert decoded.repository==repo and decoded.releases==source.releases and w.PROTOCOL_VERSION==2
    obj=json.loads(encoded); obj['result']['releases'][0]['id']=5
    with pytest.raises(w.ProtocolError): w.decode_response(wire(obj),RID)
    obj=json.loads(w.encode_success(RID,catalog(release()))); obj['result']['releases'][0]['id']='5'
    with pytest.raises(w.ProtocolError): w.decode_response(wire(obj),RID)


def test_gitlab_scan_dispatch(monkeypatch):
    import covearchive.gitlab as gl
    from test_gitlab import Wire
    transport=Wire(); monkeypatch.setattr(gl,'GitLabReleaseClient',transport.client)
    decoded=w.decode_response(wire(w.run_scan(RID,'https://gitlab.com/gitlab-org/cli')),RID)
    assert decoded.catalog.repository.project_id==34675721 and len(transport.calls)==4


@pytest.mark.parametrize('field,value',[('project_id',None),('project_id',True),('duplicates',[5]),('file_id',True),('file_id',2**63)])
def test_gitlab_wire_bounds(field,value):
    from test_gitlab import Wire, TARGET
    obj=json.loads(w.encode_success(RID,Wire().client().discover_releases(TARGET)))
    if field=='file_id': obj['result']['releases'][0]['assets'][0]['file_id']=value
    else: obj['result']['duplicate_release_ids' if field=='duplicates' else field]=value
    with pytest.raises(w.ProtocolError): w.decode_response(wire(obj),RID)


@pytest.mark.parametrize("provider",["gitlab","github"])
def test_duplicate_wire_release_keys(provider):
    from test_gitlab import Wire, TARGET
    source=Wire().client().discover_releases(TARGET) if provider=="gitlab" else catalog(release())
    obj=json.loads(w.encode_success(RID,source)); obj["result"]["releases"]*=2
    if provider=="gitlab":
        with pytest.raises(w.ProtocolError): w.decode_response(wire(obj),RID)
    else: assert w.decode_response(wire(obj),RID).catalog.releases==source.releases*2


def test_token_request_wire_bounds():
    token = 'ghp_'+'A'*36
    data = w.encode_request_with_token(RID, URL, token)
    assert w.parse_request(data) == (RID, URL)
    assert w.parse_request_with_token(data) == (RID, URL, token)
    assert 'github_token' not in json.loads(w.encode_request(RID, URL))
    for bad in ('a'*19, 'a'*256, token+'\r\nX:y', None):
        obj = json.loads(data); obj['github_token'] = bad
        with pytest.raises(w.ProtocolError): w.parse_request_with_token(wire(obj))
    with pytest.raises(w.ProtocolError): w.parse_request_with_token(data+b'x'*4096)


@pytest.mark.parametrize('provider', ['github', 'codeberg', 'gitlab'])
def test_token_ignored_for_injected_client(provider, capsys):
    token = 'ghp_'+'A'*36
    def broken(): raise RuntimeError('failure ' + token)
    result = w.run_scan(RID, f'https://{dict(github="github.com",codeberg="codeberg.org",gitlab="gitlab.com")[provider]}/Owner/Repo',
                        token=token, client_factory=broken)
    assert token not in json.dumps(result) and token not in capsys.readouterr().err


def test_worker_token_auth_rate(monkeypatch):
    import covearchive.github.client as github
    from covearchive.github.token import RateLimit
    token = 'ghp_'+'A'*36
    class Client:
        def __init__(self, *, auth): self.auth = auth
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def discover_releases(self, target):
            assert self.auth.token() == token
            self.auth.observe(RateLimit(5000, 4999, 1234, True, 100),token)
            return catalog()
    monkeypatch.setattr(github, 'GitHubReleaseClient', Client)
    result = w.run_scan(RID, URL, token=token)
    assert token not in json.dumps(result)
    decoded = w.decode_response(wire(result), RID)
    assert decoded.rate_limit == dict(limit=5000, remaining=4999, reset=1234, authenticated=True)
    assert w.decode_response(w.encode_success(RID, catalog()), RID).rate_limit is None
    assert w.decode_response(w.encode_success(RID, catalog()), RID).token_rejected is False
    for value in (None, True, -1, '5000'):
        obj = dict(result); obj['rate_limit'] = dict(result['rate_limit'], limit=value)
        with pytest.raises(w.ProtocolError): w.decode_response(wire(obj), RID)

def test_main_token_diagnostics_and_stdout(monkeypatch, capsys):
    token = 'ghp_'+'A'*36; output = io.BytesIO()
    original = w.run_scan
    def broken(): raise RuntimeError('failure '+token)
    monkeypatch.setattr(w, 'run_scan', lambda rid,url,token=None: original(rid,url,token=token,client_factory=broken))
    monkeypatch.setattr(w, 'start_watchdog', lambda *a: None)
    assert w.main(stdin=io.BytesIO(w.encode_request_with_token(RID, URL, token)), stdout=output) == 0
    assert token not in output.getvalue().decode() and token not in capsys.readouterr().err
    assert w.decode_response(output.getvalue(), RID).code == 'internal_error'

@pytest.mark.parametrize('bad', [True, 123, 'x'*256, 'x'*19, 'x'*20+'\r\nX:y'])
def test_run_scan_invalid_token_is_bounded(bad):
    assert w.run_scan(RID, URL, token=bad)['error']['code']=='bad_request'

def test_worker_success_redacts_upstream_echoed_token():
    token='ghp_'+'A'*36
    source=catalog(release(name='Upstream echo '+token))
    assert token.encode() in w.encode_success(RID,source)
    class Client:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def discover_releases(self,target): return source
    result=w.run_scan(RID,URL,token=token,client_factory=Client)
    assert result['status']=='ok' and token not in json.dumps(result)
    decoded=w.decode_response(wire(result),RID)
    assert decoded.catalog.releases[0].name=='Upstream echo [redacted]'

@pytest.mark.parametrize('patch',[
 {'authenticated':None},{'authenticated':0},{'authenticated':1},{'authenticated':'true'},
 {'extra':1},{'limit':10**15},{'remaining':10**15},{'reset':10**15}])
def test_rate_wire_rejects_invalid_provenance(patch):
    obj=json.loads(w.encode_success(RID,catalog())); obj['rate_limit']=dict(limit=5000,remaining=1,reset=123,authenticated=True)
    obj['rate_limit'].update(patch)
    with pytest.raises(w.ProtocolError): w.decode_response(wire(obj),RID)

def test_wire_provenance_on_errors_and_intrinsic_constraints():
    rate=dict(limit=5000,remaining=1,reset=123,authenticated=False)
    obj=json.loads(w.encode_error(RID,'unauthorized')); obj.update(rate_limit=dict(rate),token_rejected=True)
    decoded=w.decode_response(wire(obj),RID)
    assert decoded.rate_limit==rate and decoded.token_rejected is True
    for value in (False,1,'true',None):
        bad=dict(obj,token_rejected=value)
        with pytest.raises(w.ProtocolError): w.decode_response(wire(bad),RID)
    for code in ('not_found','internal_error'):
        bad=json.loads(w.encode_error(RID,code)); bad['token_rejected']=True
        assert w.decode_response(wire(bad),RID).token_rejected is True
    ok=json.loads(w.encode_success(RID,catalog())); ok['token_rejected']=True
    assert w.decode_response(wire(ok),RID).token_rejected is True
    for provider in ('codeberg','gitlab'):
        target=RepositoryTarget('Owner','Repo',provider,'codeberg.org' if provider=='codeberg' else 'gitlab.com',1 if provider=='gitlab' else None)
        bad=json.loads(w.encode_success(RID,replace(catalog(),repository=target))); bad['rate_limit']=rate; bad['token_rejected']=True
        with pytest.raises(w.ProtocolError): w.decode_response(wire(bad),RID)
        del bad['rate_limit']
        with pytest.raises(w.ProtocolError): w.decode_response(wire(bad),RID)
    del obj['rate_limit']['authenticated']
    with pytest.raises(w.ProtocolError): w.decode_response(wire(obj),RID)
    duplicate=wire(dict(obj,rate_limit=rate)).replace(b'"token_rejected": true',b'"token_rejected":true,"token_rejected":true')
    with pytest.raises(w.ProtocolError): w.decode_response(duplicate,RID)

@pytest.mark.parametrize('kind',['large','incomplete','internal','encode'])
def test_worker_preserves_rate_on_all_error_paths(monkeypatch,kind):
    import covearchive.github.client as github
    from covearchive.github.token import RateLimit
    class Client:
        def __init__(self,*,auth): self.auth=auth
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def discover_releases(self,target):
            self.auth.reject(self.auth.token()); self.auth.observe(RateLimit(5000,4999,1234,False,100),None)
            if kind=='internal': raise RuntimeError('upstream failure')
            if kind=='encode': return None
            return replace(catalog(),complete=False) if kind=='incomplete' else catalog(release(name='x'*2000))
    monkeypatch.setattr(github,'GitHubReleaseClient',Client)
    if kind=='large': monkeypatch.setattr(w,'MAX_RESPONSE_BYTES',400)
    obj=w.run_scan(RID,URL,token='ghp_'+'A'*36)
    assert obj['token_rejected'] is True
    assert obj['status']=='error' and obj['rate_limit']['authenticated'] is False
    assert obj['error']['code']==dict(large='result_too_large',incomplete='incomplete_scan',internal='internal_error',encode='internal_error')[kind]

@pytest.mark.parametrize('probe,status',[(200,404),(401,200),(401,404)])
def test_worker_private_repository_release_is_anonymous(monkeypatch,probe,status):
    import covearchive.github.client as github
    from test_github_client import real_transport
    session,client,calls,_=real_transport([(probe,{},io.BytesIO(b'{"private":true}')),(status,{},None)],probe=False)
    def factory(*,auth): client.auth=auth; return client
    monkeypatch.setattr(github,'GitHubReleaseClient',factory)
    token='ghp_'+'A'*36; result=w.run_scan(RID,URL,token=token)
    assert (result['status']=='ok')==(status==200) and token not in json.dumps(result)
    assert w.decode_response(wire(result),RID).token_rejected is (probe==401)
    if status==404: assert result['error']['code']=='not_found'
    assert len(calls)==2 and 'Authorization' in calls[0].headers and 'Authorization' not in calls[1].headers
