"""Offline search protocol and import boundaries; no real socket may connect."""

import ast
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

from covearchive.discover.github_search import GitHubSearchClient, InsecureTransportError
from covearchive.discover.models import ProjectKind, Tri
from covearchive.discover.query import SEARCH_ENDPOINT, SearchQueryError, build_search_query
from covearchive.github.client import (
    AccessDeniedError, CredentialedSessionError, DirectSession, InvalidResponseError,
    RateLimitError, RequestTimeoutError, TransientServerError, UnsafeRedirectError,
)


ROOT = Path(__file__).resolve().parents[1]


def repository(number=123, owner="Zelda64Recomp", name="Zelda64Recomp", **changes):
    return {
        "id": number, "owner": {"login": owner}, "name": name,
        "full_name": f"{owner}/{name}", "html_url": f"https://github.com/{owner}/{name}",
        "url": f"https://api.github.com/repos/{owner}/{name}",
        "private": False, "visibility": "public", "disabled": False,
        "description": "Static recompilation of Majora's Mask for PC",
        "topics": ["static-recompilation", "zelda"], "fork": False, "archived": False,
    } | changes


def envelope(items=(), total=None, **changes):
    return {"items": list(items), "total_count": len(items) if total is None else total,
            "incomplete_results": False} | changes


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, *, read_error=None):
        self.status_code, self.headers = status, CaseInsensitiveDict(headers or {})
        body = envelope() if body is None else body
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.read_error, self.closed, self.reads = read_error, 0, 0

    def iter_content(self, chunk_size):
        self.reads += 1
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]
        if self.read_error:
            raise self.read_error

    def close(self):
        self.closed += 1


class FakeSession:
    def __init__(self, script):
        self.script, self.calls, self.responses = list(script), [], []
        self.verify, self.cookies = True, ()

    def get(self, url, **kwargs):
        assert all(response.closed == 1 for response in self.responses)
        self.calls.append(dict(url=url, **kwargs))
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        assert kwargs["timeout"] == (10.0, 30.0)
        assert all(kwargs["headers"][key] is None for key in ("Authorization", "Cookie", "Proxy-Authorization"))
        prepared = requests.Request("GET", url).prepare()
        assert kwargs["auth"](prepared) is prepared
        assert "Authorization" not in prepared.headers
        assert self.script, "unexpected extra request"
        response = self.script.pop(0)
        if isinstance(response, Exception):
            raise response
        self.responses.append(response)
        return response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("real network is forbidden")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def rig():
    sessions = []
    def make(script, **options):
        session, sleeps = FakeSession(script), []
        sessions.append(session)
        return GitHubSearchClient(session, clock=lambda: 1000, sleep=sleeps.append, **options), session, sleeps
    yield make
    for session in sessions:
        assert all(response.closed == 1 for response in session.responses)


def test_realistic_search_to_page_ignores_link_headers(rig):
    client, session, sleeps = rig([FakeResponse(body=envelope([repository()], 42),
        headers={"Link": '<https://evil.invalid/private>; rel="next"'})])
    result = client.search("Zelda 64", "recomps", "stars", page=2, per_page=10)
    assert result.page == 2 and result.per_page == 10 and result.total_count == 42
    assert result.retrievable_total == 42 and result.has_next_page
    assert result.incomplete_results is False and result.dropped == result.duplicates == 0
    assert result.results[0].kinds == (ProjectKind.RECOMP,)
    assert result.results[0].target.repo_key == "zelda64recomp/zelda64recomp"
    assert len(session.calls) == 1 and sleeps == []
    assert session.calls[0]["url"] == build_search_query("Zelda 64", "recomps", "stars", page=2, per_page=10).url
    assert client.auth is None and client.max_redirects == 0 and client.max_pages == 1


@pytest.mark.parametrize("status", [301, 302, 307, 308])
@pytest.mark.parametrize("location", [SEARCH_ENDPOINT + "?page=2", "https://evil.invalid", "/search/repositories"])
def test_every_redirect_rejected_without_second_request(rig, status, location):
    response = FakeResponse(status, headers={"Location": location}, read_error=AssertionError("body read"))
    client, session, sleeps = rig([response, FakeResponse()])
    with pytest.raises(UnsafeRedirectError):
        client.search("Mario")
    assert len(session.calls) == 1 and sleeps == [] and response.reads == 0
    with pytest.raises(UnsafeRedirectError):
        client._redirect_url(location, SEARCH_ENDPOINT)


@pytest.mark.parametrize("url", [
    "http://api.github.com/search/repositories?q=x", "https://api.github.com.evil/search/repositories?q=x",
    "https://secret@api.github.com/search/repositories?q=x", "https://api.github.com:443/search/repositories?q=x",
    "https://api.github.com/search/repositories/other?q=x", "https://api.github.com/repos/a/b?q=x",
    "https://api.github.com/search/repositories?q=x#fragment", None,
])
def test_transport_guard_rejects_other_endpoints(rig, url):
    client, session, _ = rig([])
    with pytest.raises(UnsafeRedirectError):
        client._page(url, dict(pages=0, requests=0, redirects=0))
    assert session.calls == []


@pytest.mark.parametrize("verify", [False, None])
def test_insecure_session_fails_before_request_and_on_reuse(rig, verify):
    client, session, _ = rig([FakeResponse()])
    assert client.search("Mario").results == ()
    session.verify = verify
    with pytest.raises(InsecureTransportError) as caught:
        client.search("Mario")
    assert caught.value.code == "insecure_transport" and len(session.calls) == 1


@pytest.mark.parametrize("verify", [False, None, "", 0, 1, b"", object()])
@pytest.mark.parametrize("guard", ["search", "transport"])
def test_tls_guards_reject_unverified_values_before_get(rig, monkeypatch, verify, guard):
    client, session, _ = rig([FakeResponse()])
    session.verify = verify
    if guard == "search":
        # Isolate the search guard so the transport guard cannot mask its loss.
        monkeypatch.setattr(client, "_validate_transport", lambda url, response=None: None)
    with pytest.raises(InsecureTransportError) as caught:
        if guard == "search":
            client.search("Mario")
        else:
            client._page(build_search_query("Mario").url, dict(pages=0, requests=0, redirects=0))
    assert caught.value.code == "insecure_transport" and session.calls == []


@pytest.mark.parametrize("verify", [True, "/caller/ca-bundle.pem"])
def test_tls_guards_accept_verified_values(rig, verify):
    client, session, _ = rig([FakeResponse()])
    session.verify = verify
    assert client.search("Mario").results == () and len(session.calls) == 1
    assert session.verify == verify


def test_tls_guards_default_to_verified_for_fake_session(rig):
    client, session, _ = rig([FakeResponse()])
    del session.verify
    assert client.search("Mario").results == () and len(session.calls) == 1


@pytest.mark.parametrize("entry", ["search", "page"])
def test_session_params_rejected_before_request(monkeypatch, entry):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return FakeResponse()
    with DirectSession() as session:
        session.params.update({"access_token": "secret", "q": "is:private"})
        monkeypatch.setattr(session, "get", get)
        client = GitHubSearchClient(session)
        with pytest.raises(CredentialedSessionError) as caught:
            if entry == "search":
                client.search("Mario")
            else:
                client._page(build_search_query("Mario").url, dict(pages=0, requests=0, redirects=0))
        assert caught.value.code == "credentialed_session" and calls == []
        assert session.params == {"access_token": "secret", "q": "is:private"}


def test_search_checks_params_before_invoking_page(rig, monkeypatch):
    client, session, _ = rig([])
    session.params = {"access_token": "secret", "q": "is:private"}
    page_calls = []
    def page(url, state):
        page_calls.append(url)
        return url, {}, json.dumps(envelope()).encode()
    monkeypatch.setattr(client, "_page", page)
    with pytest.raises(CredentialedSessionError) as caught:
        client.search("Mario")
    assert caught.value.code == "credentialed_session"
    assert page_calls == session.calls == []


@pytest.mark.parametrize("extra", ["&access_token=secret", "&q=is%3Aprivate", "&page=2"])
def test_prepared_response_cannot_append_query_parameters(rig, extra):
    response = FakeResponse()
    response.url = build_search_query("Mario").url + extra
    client, session, _ = rig([response])
    with pytest.raises(UnsafeRedirectError):
        client.search("Mario")
    assert len(session.calls) == 1 and response.reads == 0


@pytest.mark.parametrize("jar_cookie", [False, True])
def test_real_prepared_request_is_anonymous(monkeypatch, tmp_path, jar_cookie):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_environment_secret")
    monkeypatch.setenv("GH_TOKEN", "ghp_other_secret")
    netrc = tmp_path / "netrc"
    netrc.write_text("machine api.github.com login planted password secret\n", encoding="utf-8")
    monkeypatch.setenv("NETRC", str(netrc))
    calls = []
    class Adapter(HTTPAdapter):
        def send(self, request, **kwargs):
            calls.append(request)
            assert not {"authorization", "cookie", "proxy-authorization"} & {k.lower() for k in request.headers}
            assert "secret" not in request.url and "token" not in request.url
            response = requests.Response()
            response.status_code, response._content = 200, json.dumps(envelope()).encode()
            response._content_consumed = True
            response.request, response.url = request, request.url
            return response
    with DirectSession() as session:
        session.mount("https://", Adapter())
        session.auth = ("planted", "secret")
        session.headers.update({"Authorization": "Bearer secret", "Cookie": "secret=1",
                                "Proxy-Authorization": "Basic secret"})
        if jar_cookie:
            session.cookies.set("secret", "1", domain="api.github.com", path="/")
        before = dict(session.headers)
        client = GitHubSearchClient(session)
        if jar_cookie:
            with pytest.raises(CredentialedSessionError):
                client.search("Mario")
            assert calls == []
        else:
            assert client.search("Mario").results == () and len(calls) == 1
        assert dict(session.headers) == before and session.auth == ("planted", "secret")


@pytest.mark.parametrize("body,code", [
    (b"{", "invalid_json"), (b"\xff", "invalid_json"),
    (b'{"items":[],"total_count":NaN,"incomplete_results":false}', "invalid_json"),
    (b'{"items":[],"total_count":Infinity,"incomplete_results":false}', "invalid_json"),
    (b'{"items":[],"items":[],"total_count":0,"incomplete_results":false}', "duplicate_key"),
    (b'{"items":[{"id":1,"id":2}],"total_count":1,"incomplete_results":false}', "duplicate_key"),
    (b"[" * 70 + b"]" * 70, "invalid_json"), ([], "invalid_root"),
    (42, "invalid_root"), ({}, "invalid_envelope"),
    (envelope(total=True), "invalid_envelope"), (envelope(total=-1), "invalid_envelope"),
    (envelope(total=10**9 + 1), "invalid_envelope"), (envelope(total=1.0), "invalid_envelope"),
    (envelope(incomplete_results=0), "invalid_envelope"),
    (envelope() | {"items": {}}, "invalid_envelope"),
    (envelope([repository()], 0), "invalid_envelope"),
    (envelope([repository(private=True)]), "no_valid_items"),
    (envelope([None, {}]), "no_valid_items"),
])
def test_strict_json_and_envelopes(rig, body, code):
    client, session, sleeps = rig([FakeResponse(body=body)])
    with pytest.raises(InvalidResponseError) as caught:
        client.search("Mario")
    assert caught.value.code == code and len(session.calls) == 1 and sleeps == []


def test_item_cap_and_empty_page(rig):
    client, _, _ = rig([FakeResponse(body=envelope([repository(), repository(124)]))])
    with pytest.raises(InvalidResponseError) as caught:
        client.search("Mario", per_page=1)
    assert caught.value.code == "too_many_items"
    client, _, _ = rig([FakeResponse(body=envelope())])
    result = client.search("Mario")
    assert result.results == () and result.dropped == result.duplicates == 0
    assert result.total_count == result.retrievable_total == 0 and not result.has_next_page


def test_partial_page_counts_validation_and_both_duplicate_keys(rig):
    items = [repository(private=True), repository(), repository(123, name="other"),
             repository(124, owner="zelda64recomp", name="zelda64recomp"),
             repository(125, name="Other", fork=True), repository(126, disabled=True)]
    client, _, _ = rig([FakeResponse(body=envelope(items, 100, incomplete_results=True))])
    result = client.search("Zelda")
    assert [r.repository_id for r in result.results] == [123, 125]
    assert result.results[1].fork is Tri.YES
    assert result.dropped == 4 and result.duplicates == 2
    assert result.incomplete_results and result.total_count == 100


@pytest.mark.parametrize("page,per_page,total,expected", [
    (1, 30, 30, False), (1, 30, 31, True), (10, 100, 2000, False),
    (9, 100, 2000, True), (1000, 1, 10**9, False), (2, 30, 1, False),
    (10, 100, 1000, False), (9, 100, 1000, True),
    (32, 30, 1000, True), (33, 30, 1000, False), (33, 30, 2000, False),
    (32, 30, 990, True), (32, 30, 960, False), (33, 30, 999, False),
])
def test_has_next_uses_totals_and_github_cap(rig, page, per_page, total, expected):
    client, session, _ = rig([FakeResponse(body=envelope(total=total))])
    result = client.search("Mario", page=page, per_page=per_page)
    assert result.has_next_page is expected
    assert result.retrievable_total == min(total, 1000) and len(session.calls) == 1
    if result.has_next_page:
        next_query = build_search_query("Mario", page=page + 1, per_page=per_page)
        assert next_query.page == page + 1
    elif (page + 1) * per_page > 1000:
        with pytest.raises(SearchQueryError) as caught:
            build_search_query("Mario", page=page + 1, per_page=per_page)
        assert caught.value.code == "page"
    else:
        assert page * per_page >= result.retrievable_total


@pytest.mark.parametrize("status,headers,body,cls", [
    (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1060"}, {}, RateLimitError),
    (403, {}, {"message": "API rate limit exceeded"}, RateLimitError),
    (403, {}, {"message": "Forbidden"}, AccessDeniedError),
    (429, {"Retry-After": "12"}, {}, RateLimitError),
])
def test_rate_limits_and_forbidden_do_not_retry(rig, status, headers, body, cls):
    client, session, sleeps = rig([FakeResponse(status, body, headers)])
    with pytest.raises(cls) as caught:
        client.search("Mario")
    assert caught.value.status == status and len(session.calls) == 1 and sleeps == []
    if status == 429:
        assert caught.value.retry_after_seconds == 12 and caught.value.retry_at == 1012


@pytest.mark.parametrize("max_retries", [0, 1, 2])
@pytest.mark.parametrize("failure,cls", [
    (requests.Timeout, RequestTimeoutError), (500, TransientServerError),
    (502, TransientServerError), (503, TransientServerError), (504, TransientServerError),
])
def test_retry_exhaustion_is_bounded(rig, max_retries, failure, cls):
    script = [failure() if callable(failure) else FakeResponse(failure) for _ in range(max_retries + 1)]
    client, session, sleeps = rig(script, max_retries=max_retries)
    with pytest.raises(cls):
        client.search("Mario")
    assert len(session.calls) == max_retries + 1 and len(sleeps) == max_retries


def test_retry_recovers_and_read_timeout_is_bounded(rig):
    client, session, sleeps = rig([requests.Timeout(), FakeResponse(503), FakeResponse()])
    assert client.search("Mario").results == ()
    assert len(session.calls) == 3 and sleeps == [1, 2]
    client, session, _ = rig([FakeResponse(read_error=requests.Timeout()) for _ in range(3)])
    with pytest.raises(RequestTimeoutError):
        client.search("Mario")
    assert len(session.calls) == 3


def test_response_body_size_limit(rig):
    client, session, sleeps = rig([FakeResponse(body=b" " * 65537)], max_response_bytes=65536)
    with pytest.raises(InvalidResponseError) as caught:
        client.search("Mario")
    assert caught.value.code == "response_too_large" and len(session.calls) == 1 and sleeps == []


def test_search_rejects_prebuilt_objects_before_transport(rig):
    client, session, _ = rig([])
    with pytest.raises(SearchQueryError):
        client.search(build_search_query("Mario"))
    assert session.calls == []
    with pytest.raises(TypeError):
        GitHubSearchClient(auth=object())


@pytest.mark.parametrize("transport", [False, True])
def test_import_isolation_and_lazy_package(transport):
    script = "\n".join([
        "import sys", "from pathlib import Path", "assert sys.flags.isolated == 1",
        "sys.path.insert(0, sys.argv[1])", "sys.modules['PySide6'] = None",
        "sys.modules['requests'] = None" if not transport else "pass",
        "import covearchive.discover as discover",
        "assert 'covearchive.discover.github_search' not in sys.modules",
        "assert 'covearchive.discover.models' not in sys.modules",
        "import covearchive.discover.models, covearchive.discover.query",
        "assert discover.Tri is covearchive.discover.models.Tri",
        "import covearchive.discover.github_search" if transport else "assert sys.modules['requests'] is None",
        "assert sys.modules['PySide6'] is None",
        "assert Path(discover.__file__).resolve().is_relative_to(Path(sys.argv[1]))",
    ])
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(ROOT / "src")],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_source_import_boundaries():
    paths = sorted((ROOT / "src/covearchive/discover").glob("*.py"))
    assert {p.name for p in paths} == {"__init__.py", "models.py", "query.py", "github_search.py", "worker.py"}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                modules = []
            assert not any(m.split(".")[0] in {"PySide6", "requests"} for m in modules)
            if path.name in {"models.py", "query.py"}:
                assert all(m.split(".")[0] in sys.stdlib_module_names or m == "covearchive.github.urls"
                           for m in modules), (path, modules)
            if path.name != "github_search.py":
                if path.name == "worker.py" and modules == ["github_search"]:
                    # The Qt-free worker may lazily construct only DS1's public
                    # client, inside run_search, after offline query validation.
                    assert isinstance(node, ast.ImportFrom) and node.level == 1
                    assert [(alias.name, alias.asname) for alias in node.names] == [("GitHubSearchClient", None)]
                    assert node in list(ast.walk(next(n for n in tree.body
                        if isinstance(n, ast.FunctionDef) and n.name == "run_search")))
                else:
                    assert not any(m.endswith("client") or m.endswith("github_search") for m in modules)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "__import__"
                if node.func.id == "import_module":
                    assert path.name == "__init__.py"


@pytest.mark.parametrize("field", ["items", "total_count", "incomplete_results"])
def test_each_envelope_field_is_required(rig, field):
    body = envelope()
    del body[field]
    client, session, _ = rig([FakeResponse(body=body)])
    with pytest.raises(InvalidResponseError) as caught:
        client.search("Mario")
    assert caught.value.code == "invalid_envelope" and len(session.calls) == 1


@pytest.mark.parametrize("field,value", [
    ("private", True), ("visibility", "internal"), ("disabled", True),
    ("html_url", "https://github.com.evil/Zelda64Recomp/Zelda64Recomp"),
])
def test_search_drops_unsafe_records_but_keeps_public_record(rig, field, value):
    items = [repository(**{field: value}), repository(124, name="Safe")]
    client, _, _ = rig([FakeResponse(body=envelope(items))])
    page = client.search("Zelda")
    assert tuple(r.name for r in page.results) == ("Safe",)
    assert page.dropped == 1 and page.duplicates == 0


def test_prepared_response_endpoint_is_checked_before_read(rig):
    response = FakeResponse()
    response.url = "https://api.github.com/repos/Other/Private"
    client, session, _ = rig([response])
    with pytest.raises(UnsafeRedirectError):
        client.search("Mario")
    assert len(session.calls) == 1 and response.reads == 0


def test_default_client_uses_direct_transport_and_four_mib_limit():
    with GitHubSearchClient() as client:
        assert isinstance(client.session, DirectSession)
        assert client.max_response_bytes == 4 * 1024 * 1024
        assert client.max_requests == client.max_retries + 1 == 3
        assert not client.session.cookies


def test_custom_timeout_is_forwarded():
    calls = []
    class Session(FakeSession):
        def get(self, url, **kwargs):
            calls.append(kwargs["timeout"])
            kwargs["timeout"] = (10.0, 30.0)
            return super().get(url, **kwargs)
    session = Session([FakeResponse()])
    client = GitHubSearchClient(session, timeout=(1.5, 4.5))
    assert client.search("Mario").results == ()
    assert calls == [(1.5, 4.5)] and session.responses[0].closed == 1
