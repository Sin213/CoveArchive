"""Offline Discover wire contract, anonymous transport and launcher checks."""

import io
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading

import pytest
import requests
from requests.adapters import HTTPAdapter

from covearchive.discover import worker as w
from covearchive.discover.github_search import GitHubSearchClient
from covearchive.discover.models import SearchPage, Tri, classify_project, parse_github_repository
from covearchive.github.client import DirectSession, GitHubApiError, RateLimitError
from test_discover_github_search import FakeResponse, FakeSession, envelope, repository

RID = "a" * 32
ROOT = Path(__file__).resolve().parents[1]


def wire(obj):
    return (json.dumps(obj, ensure_ascii=True) + "\n").encode()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("unexpected real network access")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def page():
    record = parse_github_repository(repository(stargazers_count=42, language="C++",
                                               pushed_at="2026-11-13T12:00:00Z"))
    return SearchPage((record,), 1, 30, 42, 42, False, 0, 0, True)


def success():
    return json.loads(w.encode_success(RID, page()))


def call(script, **options):
    session = FakeSession(script)
    client = GitHubSearchClient(session, sleep=lambda _: None, **options)
    obj = w.run_search(RID, "Zelda", client_factory=lambda: client)
    assert all(response.closed == 1 for response in session.responses)
    return w.decode_response(wire(obj), RID), session


def test_search_roundtrip_recomputes_immutable_classifications():
    result, session = call([FakeResponse(body=envelope([repository()], 42))])
    assert isinstance(result, w.DiscoverSuccess) and result.request_id == RID
    record = result.page.results[0]
    assert record == parse_github_repository(repository())
    assert record.classifications and record.fork == Tri.NO
    assert len(session.calls) == 1  # FakeSession checks anonymous request headers/auth.
    assert type(result.page.results) is tuple and type(record.topics) is tuple
    for obj in (result, result.page, record):
        with pytest.raises(FrozenInstanceError):
            obj.page = 0
    assert "classifications" not in success()["result"]["results"][0]


@pytest.mark.parametrize("change", [
    {"protocol": 2}, {"protocol": True}, {"request_id": "A" * 32}, {"request_id": "a" * 31},
    {"request_id": 123}, {"action": "scan"}, {"action": []}, {"text": None}, {"text": "x" * 257},
    {"kind": "url"}, {"kind": []}, {"sort": "desc"}, {"sort": True},
    {"page": True}, {"page": 0}, {"page": -1}, {"page": 1.0}, {"page": 2**63},
    {"per_page": True}, {"per_page": 0}, {"per_page": 101}, {"per_page": "30"},
    {"url": "https://evil.invalid"}, {"endpoint": "https://evil.invalid"},
    {"token": "secret"}, {"github_token": "secret"}, {"unknown": 1},
])
def test_request_rejects_non_contract_fields(change):
    obj = json.loads(w.encode_request(RID, "Zelda")) | change
    with pytest.raises(w.ProtocolError):
        w.parse_request(wire(obj))


@pytest.mark.parametrize("field", sorted(w.REQUEST_KEYS))
def test_request_requires_every_field(field):
    obj = json.loads(w.encode_request(RID, "Zelda"))
    obj.pop(field)
    with pytest.raises(w.ProtocolError):
        w.parse_request(wire(obj))


@pytest.mark.parametrize("data", [
    b"{}", b"{}\r\n", b"{}\n{}\n", b"{}\n\n", b'\xff\n', b"[]\n",
    b'{"x":NaN}\n', b'{"x":Infinity}\n', b'{"x":-Infinity}\n',
    b'{"x":1,"x":2}\n', b'[' * 1500 + b'0' + b']' * 1500 + b'\n',
    b'x' * (w.MAX_REQUEST_BYTES + 1),
])
def test_hostile_requests_have_no_stdout(data, monkeypatch, capsys):
    monkeypatch.setattr(w, "start_watchdog", lambda *args: pytest.fail("untrusted envelope started watchdog"))
    output = io.BytesIO()
    assert w.main(io.BytesIO(data), output) == 2
    assert output.getvalue() == b""
    assert capsys.readouterr().err == "ProtocolError\n"


def test_duplicate_trusted_fields_rejected():
    data = w.encode_request(RID, "Zelda").replace(b'"protocol":1', b'"protocol":1,"protocol":1')
    with pytest.raises(w.ProtocolError):
        w.parse_request(data)


def test_request_cap_admits_worst_case_unicode_and_envelope():
    data = w.encode_request(RID, "\U0001f3ae" * 256, "emulators", "relevance", 2**63 - 1, 100)
    assert data.isascii() and len(data) <= w.MAX_REQUEST_BYTES
    assert w.parse_request(data)[1] == "\U0001f3ae" * 256


@pytest.mark.parametrize("options", [dict(page=34, per_page=30), dict(page=1001, per_page=1)])
def test_semantic_pagination_errors_are_offline(options):
    def forbidden():
        pytest.fail("semantic pagination failure constructed client")
    request = w.parse_request(w.encode_request(RID, "Zelda", **options))
    assert w.decode_response(wire(w.run_search(*request, client_factory=forbidden)), RID).code == "invalid_search"


@pytest.mark.parametrize("change", [
    {"protocol": 2}, {"protocol": True}, {"request_id": "b" * 32}, {"request_id": "A" * 32},
    {"status": "maybe"}, {"status": []}, {"extra": 0}, {"result": []},
])
def test_response_envelope_is_strict(change):
    with pytest.raises(w.ProtocolError):
        w.decode_response(wire(success() | change), RID)


@pytest.mark.parametrize("data", [
    pytest.param(b"{}", id="empty-object"),
    pytest.param(b"{}\r\n", id="crlf-object"),
    pytest.param(b"{}\n{}\n", id="two-objects"),
    pytest.param(b"\xff\n", id="invalid-utf8"),
    pytest.param(b"[]\n", id="array"),
    pytest.param(b'{"x":NaN}\n', id="nan"),
    pytest.param(b'{"x":Infinity}\n', id="infinity"),
    pytest.param(b'{"x":-Infinity}\n', id="negative-infinity"),
    pytest.param(b'{"x":1,"x":1}\n', id="duplicate-key"),
    pytest.param(b'[' * 1500 + b'0' + b']' * 1500 + b'\n', id="deep-nesting"),
    pytest.param(b'x' * (w.MAX_RESPONSE_BYTES + 1), id="oversized"),
])
def test_hostile_responses_are_protocol_errors(data):
    with pytest.raises(w.ProtocolError):
        w.decode_response(data, RID)


@pytest.mark.parametrize("field,value", [
    ("page", True), ("page", 0), ("page", 34), ("page", 1001),
    ("per_page", True), ("per_page", 0), ("per_page", 101),
    ("total_count", True), ("total_count", -1), ("total_count", 10**9 + 1), ("total_count", 0),
    ("retrievable_total", 41), ("retrievable_total", True), ("retrievable_total", 1001),
    ("incomplete_results", 0), ("has_next_page", 1), ("has_next_page", False),
    ("dropped", True), ("dropped", -1), ("dropped", 30), ("duplicates", 1),
    ("duplicates", True), ("duplicates", -1), ("results", None), ("results", []),
    ("results", [{}] * 101), ("extra", "secret"),
])
def test_page_fields_revalidated(field, value):
    obj = success()
    obj["result"][field] = value
    # Empty results are valid independently of total_count, so pair the empty
    # list with dropped records: DS1 would have rejected no_valid_items.
    if field == "results" and value == []:
        obj["result"]["dropped"] = 1
    with pytest.raises(w.ProtocolError):
        w.decode_response(wire(obj), RID)


@pytest.mark.parametrize("field,value", [
    ("repository_id", True), ("repository_id", 0), ("repository_id", 2**63),
    ("owner", None), ("owner", "bad/owner"), ("owner", "a" * 40), ("owner", "-bad"),
    ("name", "bad?query"), ("name", ".."), ("name", "a" * 101), ("name", 1),
    ("url", "https://github.com/wrong/Repo"), ("url", "https://github.com/Zelda64Recomp/Zelda64Recomp/"),
    ("url", "https://evil.invalid/Zelda64Recomp/Zelda64Recomp"),
    ("description", 1), ("description", "x" * 281), ("description", "hello\nworld"),
    ("description", "hidden\u200btext"), ("description", "e\u0301"),
    ("language", True), ("language", "x" * 51), ("language", " C++ "),
    ("topics", "recomp"), ("topics", ["a"] * 21), ("topics", ["Upper"]),
    ("topics", ["a" * 51]), ("topics", ["-a"]), ("topics", [True]),
    ("stars", True), ("stars", -1), ("stars", 1.0),
    ("last_activity", "2026-02-30T12:00:00Z"), ("last_activity", "2026-11-13"),
    ("last_activity", "2026-11-13T12:00:00+00:00"), ("last_activity", 1),
    ("fork", True), ("fork", "maybe"), ("archived", False), ("archived", None),
    ("classifications", []), ("token", "secret"),
])
def test_result_fields_revalidated(field, value):
    obj = success()
    obj["result"]["results"][0][field] = value
    with pytest.raises(w.ProtocolError):
        w.decode_response(wire(obj), RID)


@pytest.mark.parametrize("level,keys", [("envelope", ("protocol", "request_id", "status", "result")),
    ("page", sorted(w.PAGE_KEYS)), ("record", sorted(w.RESULT_KEYS))])
def test_response_requires_all_fields(level, keys):
    for key in keys:
        obj = success()
        target = obj if level == "envelope" else obj["result"] if level == "page" else obj["result"]["results"][0]
        target.pop(key)
        with pytest.raises(w.ProtocolError):
            w.decode_response(wire(obj), RID)


@pytest.mark.parametrize("duplicate", ["id", "key"])
def test_duplicate_repository_id_and_casefolded_key_rejected(duplicate):
    obj = success()
    other = obj["result"]["results"][0].copy()
    if duplicate == "key":
        other.update(repository_id=124, owner="zelda64recomp", name="zelda64recomp",
                     url="https://github.com/zelda64recomp/zelda64recomp")
    else:
        other.update(owner="Other", url="https://github.com/Other/Zelda64Recomp")
    obj["result"]["results"].append(other)
    with pytest.raises(w.ProtocolError):
        w.decode_response(wire(obj), RID)


def test_nested_duplicate_json_keys_rejected():
    data = w.encode_success(RID, page()).replace(b'"repository_id":123', b'"repository_id":123,"repository_id":123')
    assert data != w.encode_success(RID, page())
    with pytest.raises(w.ProtocolError):
        w.decode_response(data, RID)


def test_valid_page_count_boundaries_and_unknown_fields():
    for total, number, per_page in [(0, 1, 30), (10000, 33, 30), (10**9, 10, 100)]:
        empty = SearchPage((), number, per_page, total, min(total, 1000), True, 0, 0, False)
        assert w.decode_response(w.encode_success(RID, empty), RID).page == empty
    original = page()
    record = replace(original.results[0], description=None, language=None, topics=(), stars=None,
                     last_activity=None, fork=Tri.UNKNOWN, archived=Tri.UNKNOWN,
                     classifications=classify_project(original.results[0].name, (), None))
    expected = replace(original, results=(record,), dropped=2, duplicates=1)
    assert w.decode_response(w.encode_success(RID, expected), RID).page == expected


def test_maximum_records_fit_response_cap_and_overflow_is_structured(monkeypatch):
    records = tuple(parse_github_repository(repository(number=i+1, owner="a"*39, name="r"*97+str(i),
        description="\U0001f3ae"*280, language="\U0001f3ae"*50,
        topics=["a"*50]*20, stargazers_count=10**100)) for i in range(100))
    big = SearchPage(records, 1, 100, 100, 100, False, 0, 0, False)
    encoded = w.encode_success(RID, big)
    assert len(encoded) < w.MAX_RESPONSE_BYTES and len(w.decode_response(encoded, RID).page.results) == 100
    monkeypatch.setattr(w, "MAX_RESPONSE_BYTES", 400)
    encoded = w.encode_success(RID, page())
    assert len(encoded) <= 400 and w.decode_response(encoded, RID).code == "result_too_large"


@pytest.mark.parametrize("script,code", [
    ([FakeResponse(status=429)], "rate_limited"), ([requests.Timeout("SECRET")] * 3, "timeout"),
    ([requests.ConnectionError("SECRET")] * 3, "network"),
    ([FakeResponse(status=503) for _ in range(3)], "server_error"),
    ([FakeResponse(status=403)], "forbidden"), ([FakeResponse(status=401)], "unauthorized"),
    ([FakeResponse(status=404)], "not_found"), ([FakeResponse(status=410)], "gone"),
    ([FakeResponse(status=418)], "unexpected_status"), ([FakeResponse(body=b"SECRET{")], "invalid_json"),
])
def test_upstream_failures_are_stable_and_redacted(script, code, capsys):
    result, session = call(script)
    assert result.code == code and result.message == "Search failed: " + code
    assert "SECRET" not in str(result) + capsys.readouterr().err
    assert 1 <= len(session.calls) <= 3


def test_internal_exception_and_unknown_code_are_redacted(capsys):
    for exc in (RuntimeError("SECRET"), GitHubApiError("SECRET", code="SECRET"),
                GitHubApiError("SECRET", code=["SECRET"])):
        result, _ = call([exc])
        assert result.code == "internal_error"
        assert "SECRET" not in str(result)
    diagnostic = capsys.readouterr().err
    assert "SECRET" not in diagnostic
    assert all(len(line) < 120 and line.endswith(" internal_error") for line in diagnostic.splitlines())


def rate_error():
    return RateLimitError(kind="primary", status=403, retry_after_seconds=30,
                          retry_at=1030, reset_at=1030, remaining=0)


def test_rate_limit_detail_roundtrip():
    result = w.decode_response(w.encode_error(RID, rate_error()), RID)
    assert result.http_status == 403
    assert result.detail == dict(kind="primary", retry_after_seconds=30, retry_at=1030, reset_at=1030, remaining=0)


@pytest.mark.parametrize("field,value", [("code", "secret"), ("code", []), ("message", "SECRET"),
    ("http_status", True), ("http_status", 99), ("http_status", 600), ("detail", {"extra": 1}), ("extra", 1)])
def test_error_fields_are_strict(field, value):
    obj = json.loads(w.encode_error(RID, "network"))
    obj["error"][field] = value
    with pytest.raises(w.ProtocolError):
        w.decode_response(wire(obj), RID)


@pytest.mark.parametrize("field,value", [("kind", "secret"), ("kind", []), ("remaining", True),
    ("remaining", -1), ("retry_after_seconds", 1.5), ("retry_at", -1), ("reset_at", 2**63), ("extra", 0)])
def test_rate_detail_fields_are_strict(field, value):
    obj = json.loads(w.encode_error(RID, rate_error()))
    obj["error"]["detail"][field] = value
    with pytest.raises(w.ProtocolError):
        w.decode_response(wire(obj), RID)


def test_errors_require_each_field_and_rate_detail_key():
    for field in ("code", "message", "http_status", "detail"):
        obj = json.loads(w.encode_error(RID, "network"))
        obj["error"].pop(field)
        with pytest.raises(w.ProtocolError):
            w.decode_response(wire(obj), RID)
    for field in w.RATE_KEYS:
        obj = json.loads(w.encode_error(RID, rate_error()))
        obj["error"]["detail"].pop(field)
        with pytest.raises(w.ProtocolError):
            w.decode_response(wire(obj), RID)


def test_worker_default_factory_is_anonymous_closes_owned_session(monkeypatch, tmp_path):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine api.github.com login planted password SECRET\n")
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("GITHUB_TOKEN", "SECRET")
    monkeypatch.setenv("GH_TOKEN", "SECRET")
    calls, sessions, closed = [], [], []
    class Adapter(HTTPAdapter):
        def send(self, request, **kwargs):
            calls.append(request)
            assert not {"authorization", "cookie", "proxy-authorization"} & {key.lower() for key in request.headers}
            assert "SECRET" not in request.url and "SECRET" not in str(request.headers)
            response = requests.Response()
            response.status_code, response._content = 200, json.dumps(envelope()).encode()
            response._content_consumed = True
            response.request, response.url = request, request.url
            return response
    original_init, original_close = DirectSession.__init__, DirectSession.close
    def init(session):
        original_init(session)
        session.mount("https://", Adapter())
        sessions.append(session)
    def close(session):
        closed.append(session)
        original_close(session)
    monkeypatch.setattr(DirectSession, "__init__", init)
    monkeypatch.setattr(DirectSession, "close", close)
    result = w.decode_response(wire(w.run_search(RID, "Zelda")), RID)
    assert isinstance(result, w.DiscoverSuccess) and len(calls) == 1
    assert sessions == closed and len(sessions) == 1
    assert not {"authorization", "cookie", "proxy-authorization"} & {key.lower() for key in calls[0].headers}


def test_worker_honours_proxy_and_ca_environment(monkeypatch, tmp_path):
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    proxy = "http://proxy.example.invalid:8080"
    bundle = tmp_path / "intercepting-ca.pem"
    bundle.write_text("fake adapter does not open a TLS connection\n")
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    calls = []
    client = GitHubSearchClient()
    class Adapter(HTTPAdapter):
        def send(self, request, **kwargs):
            calls.append(request)
            # Inspect requests' actual merged transport arguments, not a flag:
            # the fake adapter replaces only socket/TLS I/O.
            assert kwargs["proxies"]["https"] == proxy
            assert kwargs["verify"] == str(bundle)
            settings = client.session.merge_environment_settings(request.url, {}, True, None, None)
            assert settings["proxies"]["https"] == proxy and settings["verify"] == str(bundle)
            assert client.session.verify is True
            response = requests.Response()
            response.status_code, response._content = 200, json.dumps(envelope()).encode()
            response._content_consumed = True
            response.request, response.url = request, request.url
            return response
    client.session.mount("https://", Adapter())
    result = w.decode_response(wire(w.run_search(RID, "Zelda", client_factory=lambda: client)), RID)
    assert isinstance(result, w.DiscoverSuccess) and len(calls) == 1 and client._closed


def test_main_one_terminal_line_and_watchdog_slack(monkeypatch):
    calls = []
    monkeypatch.setattr(w, "start_watchdog", lambda *args: calls.append(args))
    output = io.BytesIO()
    source = io.BytesIO(w.encode_request(RID, "!!!"))
    exit_fn = lambda code: None
    assert w.main(source, output, deadline_seconds=7, exit_fn=exit_fn) == 0
    data = output.getvalue()
    assert data.count(b"\n") == 1 and b"\r" not in data
    assert w.decode_response(data, RID).code == "invalid_search"
    assert calls == [(source, 12, exit_fn)]


@pytest.mark.parametrize("trigger", ["eof", "deadline"])
def test_main_watchdog_forces_exit_during_blocked_search(monkeypatch, trigger):
    exits, pending, finished = queue.Queue(), threading.Event(), threading.Event()
    request = w.encode_request(RID, "Zelda")
    class Input:
        def readline(self, cap):
            assert cap == w.MAX_REQUEST_BYTES + 1
            return request
        def read(self, size):
            if trigger == "deadline":
                finished.wait(2)
            return b""
    def blocked(*args):
        pending.set()
        assert finished.wait(2)
        return json.loads(w.encode_error(RID, "network"))
    monkeypatch.setattr(w, "run_search", blocked)
    results = []
    thread = threading.Thread(target=lambda: results.append(w.main(Input(), io.BytesIO(),
        deadline_seconds=-4.9 if trigger == "deadline" else 10, exit_fn=exits.put)))
    thread.start()
    try:
        assert pending.wait(1) and exits.get(timeout=1) == 70
    finally:
        finished.set()
        thread.join(2)
    assert not thread.is_alive() and results == [0]


def subprocess_response(command, request):
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        process.stdin.write(request)
        process.stdin.flush()  # Keep pipe open until worker exits, as QProcess does.
        data = process.stdout.readline(w.MAX_RESPONSE_BYTES + 1)
        assert process.wait(timeout=10) == 0, process.stderr.read()
        assert process.stdout.read() == b""
        return data, process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def test_real_main_subprocess_uses_offline_client_patch():
    script = f"""
import os, sys
sys.path[:0] = [{str(ROOT/'src')!r}, {str(ROOT/'tests')!r}]
from covearchive.discover import github_search as g, worker as w
from test_discover_github_search import FakeSession, FakeResponse, repository, envelope
original = g.GitHubSearchClient
g.GitHubSearchClient = lambda: original(FakeSession([FakeResponse(body=envelope([repository()]))]))
code = w.main()
assert not any(k.startswith('PySide6') for k in sys.modules)
os._exit(code)
"""
    data, err = subprocess_response([sys.executable, "-I", "-B", "-c", script], w.encode_request(RID, "Zelda"))
    assert err == b"" and data.count(b"\n") == 1
    assert w.decode_response(data, RID).page.results[0].repository_id == 123


@pytest.mark.parametrize("frozen", [False, True])
def test_launcher_routes_before_qt_and_stdin_text_cannot_hijack_scan(frozen):
    script = f"""
import os, runpy, sys
sys.frozen = {frozen!r}
original_exit = os._exit
def checked_exit(code):
    assert not any(k.startswith('PySide6') for k in sys.modules)
    original_exit(code)
os._exit = checked_exit
sys.argv = [{str(ROOT/'run_covearchive.py')!r}, '--discover-worker']
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    # This text cleans to SCAN syntax, then exceeds the semantic text limit;
    # argv routing must still produce Discover's invalid_search envelope.
    text = "--scan-worker " + "x" * 101
    data, err = subprocess_response([sys.executable, "-I", "-B", "-c", script], w.encode_request(RID, text))
    assert err == b"" and w.decode_response(data, RID).code == "invalid_search"


def test_direct_source_launcher_offline_smoke_and_existing_scan_route():
    data, err = subprocess_response([sys.executable, "-I", "-B", str(ROOT/"run_covearchive.py"), "--discover-worker"],
                                    w.encode_request(RID, "!!!"))
    assert not err and w.decode_response(data, RID).code == "invalid_search"
    from covearchive import scan_worker
    data, err = subprocess_response([sys.executable, "-I", "-B", str(ROOT/"run_covearchive.py"), "--scan-worker"],
                                    scan_worker.encode_request(RID, "invalid"))
    assert not err and scan_worker.decode_response(data, RID).code == "invalid_url"


def test_request_roundtrip():
    assert w.parse_request(w.encode_request(RID, "Zelda")) == (RID, "Zelda", "all", "relevance", 1, 30)


@pytest.mark.parametrize("text", ["", "!!!", "x\x00", "x" * 101])
def test_invalid_text_is_offline_structured_error(text):
    def forbidden():
        pytest.fail("invalid text must not construct a client")
    obj = w.run_search(*w.parse_request(w.encode_request(RID, text)), client_factory=forbidden)
    assert w.decode_response(wire(obj), RID).code == "invalid_search"


def test_worker_import_is_qt_free():
    script = (f"import sys;sys.path.insert(0,{str(ROOT/'src')!r});"
              "import covearchive.discover.worker;"
              "assert not any(k.startswith('PySide6') for k in sys.modules)")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
