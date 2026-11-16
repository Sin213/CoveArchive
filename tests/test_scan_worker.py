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
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def discover_releases(self, actual):
            assert actual == target
            return replace(catalog(release()), repository=target)
    monkeypatch.setattr(github if provider == "github" else forgejo,
                        "GitHubReleaseClient" if provider == "github" else "ForgejoReleaseClient", Client)
    result = w.decode_response(wire(w.run_scan(RID, f"https://{target.host}/Owner/Repo")), RID)
    assert result.catalog.repository == target
