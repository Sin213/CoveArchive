"""Offline behavioral contract for bounded anonymous GitHub discovery."""

import json
import gzip
import io
import ssl
from email.message import Message
from types import SimpleNamespace

import urllib3
import socket
from urllib.request import Request

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

from covearchive import __version__
from covearchive.github.client import (
    DirectSession, GitHubApiError, GitHubReleaseClient, InvalidTargetError, NotFoundError,
    AccessDeniedError, RateLimitError, TransientServerError, RequestTimeoutError,
    NetworkError, InvalidResponseError, PaginationError, UnsafeRedirectError,
    ApiGoneError, UnexpectedStatusError, IncompleteScanError,
)
from covearchive.github.models import ReleaseCatalog, ReleaseSchemaError
from covearchive.github.select import select_releases, select_release_by_tag
from covearchive.github.urls import RepositoryTarget, ReleaseListTarget, ReleaseTagTarget


ROOT = "https://api.github.com"
PATH = "/repos/Owner/Repo/releases"
URL = ROOT + PATH + "?per_page=100"
TARGET = RepositoryTarget("Owner", "Repo")
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
           "User-Agent": f"CoveArchive/{__version__}"}
DATE = "2026-11-13T12:00:00Z"


def asset(id=1, name="app.zip", **changes):
    data = dict(id=id, name=name, size=5, state="uploaded", content_type="opaque",
                browser_download_url="https://example/asset", created_at=DATE, updated_at=DATE)
    data.update(changes)
    return data


def release(id=1, **changes):
    data = dict(id=id, tag_name=f"v{id}", draft=False, prerelease=False,
                created_at=DATE, assets=[asset()])
    data.update(changes)
    return data


def page(number, path=PATH):
    return ROOT + path + f"?per_page=100&page={number}"


def link(url, rel="next"):
    return f'<{url}>; rel="{rel}"'


class FakeResponse:
    def __init__(self, status=200, body=(), headers=None, *, read_error=None):
        self.status_code, self.headers = status, CaseInsensitiveDict(headers or {})
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.read_error, self.closed, self.reads = read_error, 0, 0

    def iter_content(self, chunk_size):
        self.reads += 1
        yield b""
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]
        if self.read_error:
            raise self.read_error

    def close(self):
        self.closed += 1


class FakeSession:
    def __init__(self, script, timeout=(10.0, 30.0)):
        self.script, self.timeout = list(script), timeout
        self.calls, self.responses = [], []

    def get(self, url, **kwargs):
        assert all(response.closed == 1 for response in self.responses), "unclosed prior response"
        self.calls.append(dict(url=url, **kwargs))
        assert set(kwargs) == {"headers", "timeout", "allow_redirects", "stream", "auth"}
        assert kwargs["headers"] == HEADERS | {"Authorization": None, "Cookie": None, "Proxy-Authorization": None}
        assert kwargs["timeout"] == self.timeout
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        assert callable(kwargs["auth"]), "explicit no-op auth required"
        prepared = requests.Request("GET", url).prepare()
        assert kwargs["auth"](prepared) is prepared
        assert "Authorization" not in prepared.headers
        assert self.script, "unexpected extra request"
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        response = item(url) if callable(item) else item
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
        session = FakeSession(script, options.get("timeout", (10.0, 30.0)))
        sessions.append(session)
        sleeps = []
        return GitHubReleaseClient(session, clock=lambda: 1000, sleep=sleeps.append,
                                   **options), session, sleeps
    yield make
    for session in sessions:
        assert all(response.closed == 1 for response in session.responses)


def discover(client, target=TARGET):
    result = client.discover_releases(target)
    assert isinstance(result, ReleaseCatalog), "discovery must return a complete catalog"
    assert result.complete is True and result.atomic_snapshot is False
    return result


def error(client, cls, code, status=None, target=TARGET):
    with pytest.raises(cls) as caught:
        client.discover_releases(target)
    exc = caught.value
    assert isinstance(exc, GitHubApiError) and exc.code == code
    assert hasattr(exc, "status")
    if status is not None:
        assert exc.status == status
    return exc


@pytest.mark.parametrize("target", [TARGET, ReleaseListTarget("Owner", "Repo"),
                                   ReleaseTagTarget("Owner", "Repo", "v1")])
@pytest.mark.parametrize("count", [0, 1, 100])
def test_single_page_empty_and_100(rig, target, count):
    client, session, sleeps = rig([FakeResponse(body=[release(i + 1) for i in range(count)])])
    result = discover(client, target)
    assert [r.id for r in result.releases] == list(range(1, count + 1))
    assert result.repository == TARGET and result.endpoint_path == PATH
    assert (result.pages, result.requests, result.redirects, result.duplicate_release_ids) == (1, 1, 0, ())
    assert [c["url"] for c in session.calls] == [URL] and sleeps == []


def test_three_pages_order_duplicates_and_no_guessed_next(rig):
    client, session, _ = rig([
        FakeResponse(body=[release(1), release(1, tag_name="ignored")], headers={"Link": link(page(2))}),
        FakeResponse(body=[release(2), release(1)], headers={"Link": link(page(5))}),
        FakeResponse(body=[release(3)], headers={"Link": link(page(2), "prev")}),
    ])
    result = discover(client)
    assert [r.id for r in result.releases] == [1, 2, 3] and result.releases[0].tag_name == "v1"
    assert 1 in result.duplicate_release_ids
    assert (result.pages, result.requests, result.redirects) == (3, 3, 0)
    assert [c["url"] for c in session.calls] == [URL, page(2), page(5)]


@pytest.mark.parametrize("redirect", [None, "/repositories/571770/releases", "/repos/New/Name/releases"])
def test_B1_named_to_numeric_pagination(rig, redirect):
    numeric = "/repositories/571770/releases"
    script = ([FakeResponse(302, headers={"Location": redirect})] if redirect else []) + [
        FakeResponse(body=[release(1)], headers={"Link": link(page(2, numeric))}),
        FakeResponse(body=[release(2)], headers={"Link": link(page(3, numeric))}),
        FakeResponse(body=[release(3)])]
    client, session, _ = rig(script)
    try: result = discover(client)
    except GitHubApiError as exc: pytest.fail(f"Valid numeric pagination rejected: {exc.code}")
    assert [r.id for r in result.releases] == [1, 2, 3]
    assert (result.pages, result.requests, result.redirects) == (3, 3 + bool(redirect), bool(redirect))
    assert [c["url"] for c in session.calls] == [URL] + (
        [ROOT + redirect + "?per_page=100"] if redirect else []) + [page(2, numeric), page(3, numeric)]


def test_B1_exact_pandoc_link_shape(rig):
    header = ('<https://api.github.com/repositories/571770/releases?per_page=100&page=2>; rel="next", '
              '<https://api.github.com/repositories/571770/releases?per_page=100&page=2>; rel="last"')
    client, session, _ = rig([FakeResponse(body=[release(1)], headers={"Link": header}),
                              FakeResponse(body=[release(2)])])
    try: result = discover(client, RepositoryTarget("jgm", "pandoc"))
    except GitHubApiError as exc: pytest.fail(f"Real header shape rejected: {exc.code}")
    assert [r.id for r in result.releases] == [1, 2] and result.requests == 2
    assert [c["url"] for c in session.calls] == [
        ROOT + "/repos/jgm/pandoc/releases?per_page=100", page(2, "/repositories/571770/releases")]


@pytest.mark.parametrize("stage", ["named", "redirected", "switched"])
@pytest.mark.parametrize("bad", ["/repositories/abc/releases", "/repositories/"+"1"*21+"/releases",
    "/repositories/1/issues", "/repositories/1/releases/extra", "/repos/Other/Repo/releases"])
def test_B1_reject_unrelated_next_paths(rig, stage, bad):
    numeric = "/repositories/571770/releases"
    prefix = ([FakeResponse(302, headers={"Location": numeric})] if stage == "redirected" else
              [FakeResponse(headers={"Link": link(page(2, numeric))})] if stage == "switched" else [])
    client, session, _ = rig(prefix + [FakeResponse(headers={"Link": link(page(3 if stage == "switched" else 2, bad))})])
    late = stage == "switched"
    exc = error(client, IncompleteScanError if late else PaginationError, "incomplete_scan" if late else "bad_pagination")
    assert isinstance(exc.cause if late else exc, PaginationError)
    assert len(session.calls) == len(prefix) + 1


@pytest.mark.parametrize("stage", ["redirected", "switched"])
@pytest.mark.parametrize("bad", ["/repositories/571771/releases", PATH])
def test_B1_numeric_id_is_pinned(rig, stage, bad):
    numeric = "/repositories/571770/releases"
    prefix = FakeResponse(302, headers={"Location": numeric}) if stage == "redirected" else FakeResponse(headers={"Link": link(page(2,numeric))})
    client, session, _ = rig([prefix, FakeResponse(headers={"Link": link(page(3,bad))})])
    try: discover(client)
    except GitHubApiError as exc:
        assert isinstance(exc.cause if stage == "switched" else exc, PaginationError)
    else: pytest.fail("Unpinned numeric pagination accepted")
    assert len(session.calls) == 2


@pytest.mark.parametrize("header", ["garbage", "<broken", "x" * 8193,
    link(page(2)) + ", " + link(page(3)), '<https://api.github.com/x>; nope',
    link("https://evil.test" + PATH + "?page=2"), link("http://api.github.com" + PATH + "?page=2"),
    link("https://u@api.github.com" + PATH + "?page=2"),
    link("https://api.github.com:443" + PATH + "?page=2"), link(page(2) + "#x"),
    link("/repos/Owner/Repo/releases?page=2"), link(page(2, PATH + "/other")),
    link(page(2) + "&page=3"), link(page(2) + "&per_page=2"), link(page(2) + "&x=1"),
    link(ROOT + PATH + "?per_page=101&page=2"), link(ROOT + PATH + "?per_page=0&page=2"),
    link(ROOT + PATH + "?page=0"), link(ROOT + PATH + "?page=1"),
    link(ROOT + PATH + "?page=abc"), link(ROOT + PATH + "?per_page=100"),
])
def test_bad_pagination_is_never_followed(rig, header):
    client, session, sleeps = rig([FakeResponse(headers={"Link": header}), FakeResponse()])
    error(client, PaginationError, "bad_pagination")
    assert len(session.calls) == 1 and sleeps == []


def test_next_host_case_and_multiple_relations_are_supported(rig):
    next_url = page(3).replace("api.github.com", "API.GITHUB.COM")
    client, session, _ = rig([FakeResponse(headers={"Link": link(page(9), "last") + ", " + link(next_url)}),
                              FakeResponse()])
    assert discover(client).pages == 2
    assert session.calls[1]["url"].lower() == next_url.lower()


def test_repeated_identical_next_and_exact_budget_completion(rig):
    client, session, _ = rig([FakeResponse(headers={"Link": link(page(2)) + ", " + link(page(2))}),
                              FakeResponse()], max_pages=2, max_requests=2)
    assert discover(client).pages == 2 and len(session.calls) == 2


def test_redirect_loops_and_redirect_on_later_page(rig):
    first, second = "/repositories/1/releases", "/repositories/2/releases"
    client, session, _ = rig([FakeResponse(302, headers={"Location": first}),
        FakeResponse(302, headers={"Location": second}), FakeResponse(302, headers={"Location": first})])
    error(client, UnsafeRedirectError, "unsafe_redirect")
    assert len(session.calls) == 3
    client, session, _ = rig([FakeResponse(headers={"Link": link(page(2))}),
        FakeResponse(301, headers={"Location": first}), FakeResponse()])
    result = discover(client)
    assert result.endpoint_path == first and result.pages == 2
    assert [c["url"] for c in session.calls] == [URL, page(2), page(2, first)]


@pytest.mark.parametrize("status", [301, 302, 307, 308])
@pytest.mark.parametrize("path", ["/repositories/123/releases", "/repos/New/Name/releases"])
def test_safe_redirect_and_new_pagination_endpoint(rig, status, path):
    redirected = ROOT + path + "?per_page=100"
    client, session, _ = rig([FakeResponse(status, headers={"Location": path}),
        FakeResponse(body=[release()], headers={"Link": link(page(2, path))}), FakeResponse()])
    result = discover(client)
    assert (result.pages, result.requests, result.redirects, result.endpoint_path) == (2, 3, 1, path)
    assert [c["url"] for c in session.calls] == [URL, redirected, page(2, path)]
    assert all(v is None for c in session.calls for k, v in c["headers"].items()
               if k.lower() == "authorization")


@pytest.mark.parametrize("location", [None, "x" * 2049, "https://evil.test/releases",
    "http://api.github.com" + PATH, "https://u@api.github.com" + PATH,
    "https://api.github.com:443" + PATH, PATH + "#x", "/repos/o/../x/releases",
    "/repositories/123456789012345678901/releases", "/repositories/abc/releases",
    "/repos/o/r/releases/other", PATH + "?page=2", URL])
def test_unsafe_redirect_is_not_followed(rig, location):
    client, session, sleeps = rig([FakeResponse(302, headers={} if location is None else {"Location": location})])
    error(client, UnsafeRedirectError, "unsafe_redirect")
    assert len(session.calls) == 1 and sleeps == []


def test_redirect_query_preserved_and_redirect_budget(rig):
    moved = ROOT + "/repositories/1/releases?per_page=100"
    client, session, _ = rig([FakeResponse(302, headers={"Location": moved}), FakeResponse()])
    assert discover(client).redirects == 1 and session.calls[1]["url"] == moved
    client, session, _ = rig([FakeResponse(302, headers={"Location": "/repositories/1/releases"}),
        FakeResponse(302, headers={"Location": "/repositories/2/releases"})], max_redirects=1)
    error(client, UnsafeRedirectError, "unsafe_redirect")
    assert len(session.calls) == 2


@pytest.mark.parametrize("status,cls,code", [(401, AccessDeniedError, "unauthorized"),
    (403, AccessDeniedError, "forbidden"), (404, NotFoundError, "not_found"),
    (410, ApiGoneError, "gone"), (418, UnexpectedStatusError, "unexpected_status"),
    (204, UnexpectedStatusError, "unexpected_status"), (303, UnexpectedStatusError, "unexpected_status")])
def test_status_errors_are_not_retried(rig, status, cls, code):
    client, session, sleeps = rig([FakeResponse(status, {"message": "no access"})])
    error(client, cls, code, status)
    assert len(session.calls) == 1 and sleeps == []


@pytest.mark.parametrize("status,headers,message,kind,wait,reset,remaining", [
    (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1120"}, "", "primary", 120, 1120, 0),
    (403, {"Retry-After": "20"}, "", "secondary", 20, None, None),
    (429, {"Retry-After": "30"}, "", "secondary", 30, None, None),
    (403, {"Retry-After": "200", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"}, "", "primary", 200, 1100, 0),
    (429, {"Retry-After": "2", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"}, "", "primary", 100, 1100, 0),
    (403, {}, "API rate limit exceeded", "secondary", 60, None, None),
    (403, {}, "ABUSE detected", "secondary", 60, None, None),
    (429, {}, "", "too_many_requests", 60, None, None),
    (429, {"Retry-After": "invalid", "X-RateLimit-Remaining": "bad", "X-RateLimit-Reset": "bad"}, "", "too_many_requests", 60, None, None),
    (429, {"Retry-After": "0"}, "", "secondary", 1, None, None),
    (429, {"Retry-After": "999999"}, "", "secondary", 86400, None, None),
    (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "900"}, "", "primary", 1, 900, 0),
])
def test_rate_limits_never_retry_or_sleep(rig, status, headers, message, kind, wait, reset, remaining):
    client, session, sleeps = rig([FakeResponse(status, {"message": message}, headers), FakeResponse()])
    exc = error(client, RateLimitError, "rate_limited", status)
    assert (exc.kind, exc.retry_after_seconds, exc.retry_at, exc.reset_at, exc.remaining) == (
        kind, wait, 1000 + wait, reset, remaining)
    assert type(exc.retry_after_seconds) is int and type(exc.retry_at) is int
    assert len(session.calls) == 1 and sleeps == []


@pytest.mark.parametrize("body,code", [(b"not json", "invalid_json"), (b"\xff", "invalid_json"),
    (b"[NaN]", "invalid_json"), (b"[Infinity]", "invalid_json"),
    ({}, "invalid_root"), (None, "invalid_root"), ("list", "invalid_root"),
    ([{}], "invalid_release"), ([dict(release(), draft=1)], "invalid_release"),
    ([release(assets=[asset(digest="sha256:bad")])], "invalid_release"),
    ([release(i + 1) for i in range(101)], "too_many_items")])
def test_invalid_response_types_and_schema(rig, body, code):
    client, session, sleeps = rig([FakeResponse(body=body)])
    exc = error(client, InvalidResponseError, code, 200)
    if code == "invalid_release":
        assert isinstance(exc.__cause__, ReleaseSchemaError)
    assert len(session.calls) == 1 and sleeps == []


def test_response_byte_cap_including_error_body(rig):
    for status in (200, 403):
        client, session, _ = rig([FakeResponse(status, b" " * 65)], max_response_bytes=64)
        error(client, InvalidResponseError, "response_too_large", status)
        assert len(session.calls) == 1
    client, _, _ = rig([FakeResponse(body=b"[]")], max_response_bytes=2)
    assert discover(client).releases == ()


@pytest.mark.parametrize("failure,cls,code", [(requests.Timeout(), RequestTimeoutError, "timeout"),
    (requests.ConnectionError(), NetworkError, "network"), (requests.RequestException(), NetworkError, "network")])
def test_transport_failures_and_exact_retry_counts(rig, failure, cls, code):
    retries = 2 if isinstance(failure, (requests.Timeout, requests.ConnectionError)) else 0
    client, session, sleeps = rig([failure] * (retries + 1))
    error(client, cls, code)
    assert len(session.calls) == retries + 1 and len(sleeps) == retries
    assert all(0 < delay <= 4 for delay in sleeps)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_retry_exhaustion_and_recovery(rig, status):
    client, session, sleeps = rig([FakeResponse(status) for _ in range(3)])
    error(client, TransientServerError, "server_error", status)
    assert len(session.calls) == 3 and len(sleeps) == 2
    assert all(0 < delay <= 4 for delay in sleeps)
    client, session, sleeps = rig([FakeResponse(status), FakeResponse(body=[release()])])
    assert discover(client).requests == 2
    assert [c["url"] for c in session.calls] == [URL, URL] and len(sleeps) == 1


def test_other_server_status_is_not_retried_and_backoff_is_capped(rig):
    client, session, sleeps = rig([FakeResponse(501)])
    error(client, TransientServerError, "server_error", 501)
    assert len(session.calls) == 1 and sleeps == []
    client, session, sleeps = rig([FakeResponse(503) for _ in range(7)], max_retries=6)
    error(client, TransientServerError, "server_error", 503)
    assert len(session.calls) == 7 and len(sleeps) == 6
    assert all(0 < delay <= 4 for delay in sleeps)


def test_transport_recovery_custom_timeout_and_read_failure(rig):
    client, session, sleeps = rig([requests.Timeout(), requests.ConnectionError(), FakeResponse()], timeout=(1.0, 2.0))
    assert discover(client).requests == 3 and len(session.calls) == 3 and len(sleeps) == 2
    client, session, sleeps = rig([FakeResponse(read_error=requests.RequestException())])
    error(client, NetworkError, "network")
    assert len(session.calls) == 1 and sleeps == []


@pytest.mark.parametrize("failure,cls,code", [(FakeResponse(404), NotFoundError, "not_found"),
    (FakeResponse(body=b"bad"), InvalidResponseError, "invalid_json"),
    (FakeResponse(headers={"Link": link(page(2))}), PaginationError, "bad_pagination"),
    (requests.Timeout(), RequestTimeoutError, "timeout")])
def test_later_failure_wraps_cause_without_partial_releases(rig, failure, cls, code):
    client, session, _ = rig([FakeResponse(body=[release()], headers={"Link": link(page(2))}), failure], max_retries=0)
    exc = error(client, IncompleteScanError, "incomplete_scan")
    assert (exc.pages_completed, exc.reason) == (1, "error")
    assert isinstance(exc.cause, cls) and exc.cause.code == code and exc.__cause__ is exc.cause
    assert not hasattr(exc, "releases") and not hasattr(exc, "catalog")
    assert [c["url"] for c in session.calls] == [URL, page(2)]


def test_later_rate_limit_wraps_without_partial_releases(rig):
    client, session, sleeps = rig([
        FakeResponse(body=[release()], headers={"Link": link(page(2))}),
        FakeResponse(403, headers={"Retry-After": "30"}),
    ])
    exc = error(client, IncompleteScanError, "incomplete_scan")
    assert (exc.pages_completed, exc.reason) == (1, "error")
    assert isinstance(exc.cause, RateLimitError) and exc.__cause__ is exc.cause
    assert (exc.cause.kind, exc.cause.retry_after_seconds) == ("secondary", 30)
    assert not hasattr(exc, "releases") and not hasattr(exc, "catalog")
    assert [c["url"] for c in session.calls] == [URL, page(2)] and sleeps == []


@pytest.mark.parametrize("options,script,reason,pages,calls", [
    ({"max_pages": 1}, [FakeResponse(headers={"Link": link(page(2))})], "page_limit", 1, 1),
    ({"max_requests": 1}, [FakeResponse(headers={"Link": link(page(2))})], "request_limit", 1, 1),
    ({"max_requests": 1}, [FakeResponse(302, headers={"Location": "/repositories/1/releases"})], "request_limit", 0, 1),
    ({"max_requests": 1}, [FakeResponse(503)], "request_limit", 0, 1),
])
def test_budgets_raise_incomplete_never_truncate(rig, options, script, reason, pages, calls):
    client, session, _ = rig(script, **options)
    exc = error(client, IncompleteScanError, "incomplete_scan")
    assert (exc.reason, exc.pages_completed, exc.cause, exc.__cause__) == (reason, pages, None, None)
    assert len(session.calls) == calls and not hasattr(exc, "releases")


@pytest.mark.parametrize("target", [None, "https://github.com/Owner/Repo", object(),
    RepositoryTarget("o/../x", "r"), RepositoryTarget("o", "r/releases"),
    ReleaseListTarget("o", "r.git"), ReleaseTagTarget("bad owner", "r", "v1")])
def test_invalid_target_rejected_before_request(rig, target):
    client, session, sleeps = rig([])
    error(client, InvalidTargetError, "invalid_target", target=target)
    assert session.calls == [] and sleeps == []


def test_catalog_selection_metadata_and_no_further_requests(rig):
    names = ["app.AppImage", "app.TAR.GZ", "app.TAR", "app.DMG", "app.ZIP", "app.7Z"]
    assets = [asset(i + 1, name, digest="SHA256:" + "Ab" * 32) for i, name in enumerate(names)]
    assets += [asset(7, "pending.zip", state="new"), asset(8, "app.rar", digest="sha512:opaque")]
    client, session, _ = rig([FakeResponse(body=[release(1, assets=assets, body="<script>untrusted</script>",
        tarball_url="tar", zipball_url="zip"), release(2, draft=True), release(3, prerelease=True),
        release(4, assets=[]), release(5, assets=[asset(name="only.rar")])])])
    catalog = discover(client, ReleaseTagTarget("Owner", "Repo", "v3"))
    result = select_releases(catalog)
    assert [r.release.id for r in result.releases] == [1, 4, 5]
    assert [r.empty_reason for r in result.releases] == [None, "no_assets", "no_matching_assets"]
    assert [m.extension for m in result.releases[0].matches] == [".appimage", ".tar.gz", ".tar", ".dmg", ".zip", ".7z"]
    assert [r.release.id for r in select_releases(catalog, include_prereleases=True).releases] == [1, 3, 4, 5]
    assert select_release_by_tag(catalog, "v3").release.prerelease is True
    assert select_release_by_tag(catalog, "V3") is None
    assert select_release_by_tag(catalog, "git-tag-only") is None
    assert select_releases(catalog, enabled_extensions=iter([".zip"])).releases[0].matches[0].asset.name == "app.ZIP"
    first = catalog.releases[0]
    assert (first.body, first.tarball_url, first.zipball_url) == ("<script>untrusted</script>", "tar", "zip")
    assert (first.assets[0].original_digest, first.assets[0].sha256) == ("SHA256:" + "Ab" * 32, "ab" * 32)
    assert first.assets[-1].digest_algorithm == "sha512" and first.assets[-1].sha256 is None
    assert catalog.releases[1].assets[0].original_digest is None
    assert len(session.calls) == 1


@pytest.mark.parametrize("credentials", ["netrc", "session_auth", "session_header"])
def test_real_session_neutralizes_netrc_and_other_credentials(tmp_path, monkeypatch, credentials):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine api.github.com login planted password secret\n", encoding="utf-8")
    monkeypatch.setenv("NETRC", str(netrc))
    calls = []
    class Adapter(HTTPAdapter):
        def send(self, request, **kwargs):
            calls.append(request)
            assert "Authorization" not in request.headers
            response = requests.Response()
            response.status_code, response._content = 200, b"[]"
            response._content_consumed = True
            response.request, response.url = request, request.url
            return response
    with DirectSession() as session:
        session.mount("https://", Adapter())
        if credentials == "session_auth":
            session.auth = ("planted", "secret")
        if credentials == "session_header":
            session.headers["Authorization"] = "Bearer planted"
        sleeps = []
        client = GitHubReleaseClient(session, clock=lambda: 1000, sleep=sleeps.append)
        assert discover(client).releases == ()
    assert [request.url for request in calls] == [URL] and sleeps == []
    assert all(request.headers[key] == value for request in calls for key, value in HEADERS.items())


@pytest.mark.parametrize("failure", [requests.Timeout, requests.ConnectionError])
@pytest.mark.parametrize("status,cls,code", [(401, AccessDeniedError, "unauthorized"),
    (404, NotFoundError, "not_found"), (410, ApiGoneError, "gone"),
    (429, RateLimitError, "rate_limited"), (302, UnsafeRedirectError, "unsafe_redirect"),
    (403, RateLimitError, "rate_limited")])
def test_c1_status_precedes_unreadable_body(rig, failure, status, cls, code):
    response = FakeResponse(status, headers={"Retry-After": "120"}, read_error=failure())
    client, session, sleeps = rig([response, FakeResponse()])
    error(client, cls, code, status if status != 302 else None)
    assert len(session.calls) == 1 and sleeps == []
    assert response.reads == 0 and response.closed == 1


@pytest.mark.parametrize("failure", [requests.Timeout, requests.ConnectionError])
def test_c1_forbidden_message_failure_does_not_retry(rig, failure):
    response = FakeResponse(403, read_error=failure())
    client, session, sleeps = rig([response, FakeResponse()])
    error(client, AccessDeniedError, "forbidden", 403)
    assert len(session.calls) == 1 and sleeps == [] and response.closed == 1


@pytest.mark.parametrize("jar_cookie", [False, True])
def test_c2_real_session_cookies_are_never_sent(monkeypatch, jar_cookie):
    calls = []
    class Adapter(HTTPAdapter):
        def send(self, request, **kwargs):
            calls.append(request)
            response = requests.Response()
            response.status_code, response._content = 200, b"[]"
            response._content_consumed = True
            response.request, response.url = request, request.url
            return response
    with DirectSession() as session:
        session.mount("https://", Adapter())
        session.headers["Cookie"] = "header_session=planted"
        if jar_cookie:
            session.cookies.set("auth_session", "planted", domain="api.github.com", path="/")
        before_headers, before_cookies = dict(session.headers), list(session.cookies)
        client = GitHubReleaseClient(session, clock=lambda: 1000, sleep=lambda _: None)
        if jar_cookie:
            exc = error(client, GitHubApiError, "credentialed_session")
            assert type(exc).__name__ == "CredentialedSessionError" and calls == []
        else:
            assert discover(client).releases == () and len(calls) == 1
            assert "Cookie" not in calls[0].headers
        assert dict(session.headers) == before_headers and list(session.cookies) == before_cookies


def test_c2_default_session_rejects_cookies(monkeypatch):
    with GitHubReleaseClient() as client:
        assert not client.session.cookies._policy.domain_return_ok("api.github.com", Request(URL))


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("kind", ["redirect", "link"])
@pytest.mark.parametrize("bad", ["https://[", "//[", ROOT + PATH + "\x00",
    "https://api.github.com:" + "9" * 80 + PATH, ROOT + PATH + "\ud800", ROOT + PATH + "\x1f"])
def test_c3_untrusted_url_errors_stay_typed(rig, late, kind, bad):
    cls, code = (UnsafeRedirectError, "unsafe_redirect") if kind == "redirect" else (PaginationError, "bad_pagination")
    response = FakeResponse(302, headers={"Location": bad}) if kind == "redirect" else FakeResponse(headers={"Link": link(bad)})
    script = ([FakeResponse(headers={"Link": link(page(2))})] if late else []) + [response, FakeResponse()]
    client, session, _ = rig(script)
    exc = error(client, IncompleteScanError if late else cls, "incomplete_scan" if late else code)
    root = exc.cause if late else exc
    assert isinstance(root, cls) and root.code == code
    if bad in ("https://[", "//["):
        assert isinstance(root.__cause__, ValueError)
    if late:
        assert exc.pages_completed == 1 and exc.__cause__ is root
    assert len(session.calls) == 1 + late


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("status", [200, 403])
def test_c4_nested_json_is_typed(rig, late, status):
    deep = b"[" * 10000 + b"]" * 10000
    script = ([FakeResponse(headers={"Link": link(page(2))})] if late else []) + [FakeResponse(status, deep)]
    client, _, _ = rig(script)
    cls, code = (InvalidResponseError, "invalid_json") if status == 200 else (AccessDeniedError, "forbidden")
    exc = error(client, IncompleteScanError if late else cls, "incomplete_scan" if late else code)
    root = exc.cause if late else exc
    assert isinstance(root, cls) and root.code == code
    if late:
        assert exc.pages_completed == 1 and exc.__cause__ is root


@pytest.mark.parametrize("field", ["X-RateLimit-Reset", "Retry-After", "X-RateLimit-Remaining"])
@pytest.mark.parametrize("value", ["9" * 400, "1" * 16, "\u0661"])
def test_c5_oversized_headers_ignored_with_float_clock(rig, field, value):
    headers = {field: value}
    if field == "X-RateLimit-Reset":
        headers["X-RateLimit-Remaining"] = "0"
    client, session, sleeps = rig([FakeResponse(429, headers=headers), FakeResponse()])
    client.clock = lambda: 1000.5
    exc = error(client, RateLimitError, "rate_limited", 429)
    assert (exc.kind, exc.retry_after_seconds, exc.retry_at, exc.reset_at, exc.remaining) == (
        "too_many_requests", 60, 1060, None, 0 if field == "X-RateLimit-Reset" else None)
    assert len(session.calls) == 1 and sleeps == []


def test_c5_valid_integer_boundary_clamps_without_float_arithmetic(rig):
    headers = {"X-RateLimit-Reset": "999999999999999", "Retry-After": "999999999999999",
               "X-RateLimit-Remaining": "0"}
    client, _, _ = rig([FakeResponse(429, headers=headers)])
    client.clock = lambda: 1000.5
    exc = error(client, RateLimitError, "rate_limited", 429)
    assert (exc.kind, exc.retry_after_seconds, exc.retry_at, exc.reset_at, exc.remaining) == (
        "primary", 86400, 87400, 999999999999999, 0)


@pytest.mark.parametrize("query", ["pa%67e=2", "page=+2", "page=%32"])
def test_c7_encoded_pagination_query_rejected(rig, query):
    client, session, _ = rig([FakeResponse(headers={"Link": link(ROOT + PATH + "?" + query)}), FakeResponse()])
    error(client, PaginationError, "bad_pagination")
    assert len(session.calls) == 1


def test_c7_pagination_query_is_canonical(rig):
    client, session, _ = rig([FakeResponse(headers={"Link": link(ROOT + PATH + "?page=002&per_page=0100")}), FakeResponse()])
    assert discover(client).pages == 2
    assert session.calls[1]["url"] == page(2)


@pytest.mark.parametrize("owned", [False, True])
def test_c7_lifecycle_closes_only_owned_sessions(monkeypatch, owned):
    session, closes = DirectSession(), []
    monkeypatch.setattr(DirectSession, "close", lambda self: closes.append(self))
    client = GitHubReleaseClient(None if owned else session)
    assert callable(getattr(client, "close", None)), "client must support explicit close"
    with client as entered:
        assert entered is client
    client.close()
    assert closes == ([client.session] if owned else [])


@pytest.mark.parametrize("headers", [
    {"X-RateLimit-Remaining": "4999", "X-RateLimit-Reset": "2000000000"},
    {"X-RateLimit-Remaining": "0"},
    {"X-RateLimit-Remaining": "invalid", "X-RateLimit-Reset": "invalid"},
])
@pytest.mark.parametrize("message", ["You have exceeded a secondary rate limit", "abuse detected"])
def test_r3_nondecisive_headers_require_message_sniff(rig, headers, message):
    response = FakeResponse(403, {"message": message}, headers)
    client, session, sleeps = rig([response, FakeResponse()])
    exc = error(client, RateLimitError, "rate_limited", 403)
    assert (exc.kind, exc.retry_after_seconds) == ("secondary", 60)
    assert len(session.calls) == 1 and sleeps == []
    assert response.reads == 1 and response.closed == 1


@pytest.mark.parametrize("failure", [None, requests.Timeout(), requests.ConnectionError(), requests.RequestException()])
def test_r3_nondecisive_headers_keep_forbidden_body_failures_terminal(rig, failure):
    response = FakeResponse(403, {"message": "Resource not accessible by integration"},
        {"X-RateLimit-Remaining": "4999", "X-RateLimit-Reset": "2000000000"}, read_error=failure)
    client, session, sleeps = rig([response, FakeResponse()])
    error(client, AccessDeniedError, "forbidden", 403)
    assert len(session.calls) == 1 and sleeps == []
    assert response.reads == 1 and response.closed == 1


@pytest.mark.parametrize("headers,kind,wait", [({"Retry-After": "30"}, "secondary", 30),
    ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1120"}, "primary", 120),
    ({"Retry-After": "0", "X-RateLimit-Remaining": "4999"}, "secondary", 1),
])
def test_r3_decisive_headers_never_read_body(rig, headers, kind, wait):
    response = FakeResponse(403, headers=headers, read_error=requests.Timeout())
    client, session, sleeps = rig([response, FakeResponse()])
    exc = error(client, RateLimitError, "rate_limited", 403)
    assert (exc.kind, exc.retry_after_seconds) == (kind, wait)
    assert len(session.calls) == 1 and sleeps == []
    assert response.reads == 0 and response.closed == 1


class RedirectRaw:
    def __init__(self, remaining=0, failure=None):
        self.remaining, self.failure, self.read_calls, self.closed = remaining, failure, 0, 0

    def stream(self, amount, decode_content=True):
        self.read_calls += 1
        if self.failure:
            raise self.failure
        while self.remaining:
            size = min(amount, self.remaining)
            self.remaining -= size
            yield b"x" * size

    def close(self):
        self.closed += 1

    def release_conn(self):
        pass


def real_transport(script):
    calls, sleeps = [], []
    class Adapter(HTTPAdapter):
        def send(self, request, **kwargs):
            calls.append(request)
            status, headers, raw = script[len(calls) - 1] if len(calls) <= len(script) else (200, {}, None)
            response = requests.Response()
            response.status_code, response.headers = status, CaseInsensitiveDict(headers)
            response.request, response.url = request, request.url
            if raw is None:
                response._content, response._content_consumed = b"[]", True
            else:
                response.raw = raw
            return response
    session = DirectSession()
    session.mount("https://", Adapter())
    return session, GitHubReleaseClient(session, clock=lambda: 1000, sleep=sleeps.append), calls, sleeps


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("location", ["https://[", "//[", ROOT + PATH + "\x00"])
def test_direct_real_session_unsafe_redirect_is_typed(late, location):
    raw = RedirectRaw()
    script = ([(200, {"Link": link(page(2))}, None)] if late else []) + [(302, {"Location": location}, raw)]
    session, client, calls, sleeps = real_transport(script)
    with session:
        exc = error(client, IncompleteScanError if late else UnsafeRedirectError,
                    "incomplete_scan" if late else "unsafe_redirect")
    if late:
        assert exc.pages_completed == 1 and exc.reason == "error"
        assert isinstance(exc.cause, UnsafeRedirectError) and exc.__cause__ is exc.cause
    assert len(calls) == 1 + late and sleeps == [] and raw.read_calls == 0
    assert raw.closed == 1


@pytest.mark.parametrize("failure", [requests.ConnectionError(), requests.Timeout(), None])
def test_direct_real_session_redirect_body_untouched(failure):
    size = 40 * 1024 * 1024
    raw = RedirectRaw(size, failure)
    session, client, calls, sleeps = real_transport([(302, {"Location": "/repositories/1/releases"}, raw), (200, {}, None)])
    with session:
        result = discover(client)
    assert (result.requests, result.redirects) == (2, 1)
    assert [request.url for request in calls] == [URL, ROOT + "/repositories/1/releases?per_page=100"]
    assert sleeps == [] and raw.read_calls == 0 and raw.remaining == size and raw.closed == 1


def test_direct_stock_session_rejected_and_direct_accepted():
    with requests.Session() as stock:
        with pytest.raises(TypeError, match="DirectSession"):
            GitHubReleaseClient(stock)
    with DirectSession() as session:
        client = GitHubReleaseClient(session)
        assert client.session is session
    with GitHubReleaseClient() as client:
        assert isinstance(client.session, DirectSession)
        assert list(client.session.resolve_redirects(None, None, yield_requests=True)) == []


def test_direct_response_hooks_cannot_eagerly_read_redirects():
    raw, hooks = RedirectRaw(40 * 1024 * 1024), []
    session, client, _, _ = real_transport([(302, {"Location": "/repositories/1/releases"}, raw), (200, {}, None)])
    def eager(response, **kwargs):
        hooks.append(response.status_code)
        response.content
    session.hooks["response"].append(eager)
    with session:
        assert discover(client).redirects == 1
    assert hooks == [] and raw.read_calls == 0
    assert session.hooks["response"] == [eager]


class Wire(io.BytesIO):
    def __init__(self, data, failure=None, limit=None):
        super().__init__(data)
        self.failure, self.read_bytes, self.limit = failure, 0, limit
    def read(self, amount=-1):
        if self.failure and self.tell() >= 10:
            raise self.failure
        amount = min(amount, self.limit) if self.limit and amount > 0 else amount
        data = super().read(min(amount, 10) if self.failure and amount > 0 else amount)
        self.read_bytes += len(data)
        return data


def library_stack(factory, late=False):
    calls, streams, sleeps = [], [], []
    class Pool:
        def urlopen(self, **kw):
            calls.append(kw)
            assert kw["redirect"] is False and kw["decode_content"] is False
            assert kw["preload_content"] is False and kw["retries"].total == 0
            item = (200, [("Link", link(page(2)))], Wire(b"[]")) if late and len(calls) == 1 else factory()
            if isinstance(item, Exception):
                raise item
            status, headers, stream = item
            streams.append(stream)
            message = Message()
            for key, value in headers:
                message[key] = value
            original = SimpleNamespace(msg=message, isclosed=lambda: stream.closed, close=stream.close)
            try:
                return urllib3.HTTPResponse(body=stream, headers=urllib3._collections.HTTPHeaderDict(headers),
                    status=status, original_response=original, preload_content=False, decode_content=False)
            except urllib3.exceptions.HTTPError:
                stream.close()  # The pool owns streams when response construction fails.
                raise
    pool = Pool()
    class Adapter(HTTPAdapter):
        def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
            return pool
    session = DirectSession()
    session.mount("https://", Adapter())
    return session, GitHubReleaseClient(session, clock=lambda: 1000, sleep=sleeps.append), calls, streams, sleeps


def stack_failure(mode):
    e = urllib3.exceptions
    failures = {
        "stall": lambda: (200, [], Wire(b"x" * 10, socket.timeout("stall"))),
        "read": lambda: e.ReadTimeoutError(None, URL, "stall"),
        "connect": lambda: e.MaxRetryError(None, URL, e.ConnectTimeoutError("connect")),
        "tls_connect": lambda: e.MaxRetryError(None, URL, e.SSLError("certificate")),
        "tls_body": lambda: (200, [], Wire(b"x" * 10, ssl.SSLError("TLS framing"))),
        "new_connection": lambda: e.MaxRetryError(None, URL, e.NewConnectionError(None, "refused")),
        "protocol": lambda: e.ProtocolError("reset", ConnectionResetError()),
        "truncated": lambda: (200, [("Content-Length", "100")], Wire(b"x" * 10)),
        "reset": lambda: (200, [], Wire(b"x" * 10, ConnectionResetError())),
        "gzip": lambda: (200, [("Content-Encoding", "gzip")], Wire(b"garbage")),
        "raw_http": lambda: e.HTTPError("adapter error"),
        "length": lambda: (200, [("Content-Length", "10"), ("Content-Length", "100")], Wire(b"[]")),
    }
    return failures[mode]()


@pytest.mark.parametrize("mode,cls,attempts", [("stall", RequestTimeoutError, 3),
    ("read", RequestTimeoutError, 3), ("connect", RequestTimeoutError, 3),
    ("tls_connect", NetworkError, 1), ("tls_body", NetworkError, 1),
    ("truncated", NetworkError, 3), ("reset", NetworkError, 3),
    ("new_connection", NetworkError, 3), ("protocol", NetworkError, 3),
    ("raw_http", NetworkError, 1), ("gzip", NetworkError, 1), ("length", NetworkError, 1)])
@pytest.mark.parametrize("late", [False, True])
def test_library_stack_errors(mode, cls, attempts, late):
    session, client, calls, streams, sleeps = library_stack(lambda: stack_failure(mode), late)
    with session:
        exc = error(client, IncompleteScanError if late else cls,
                    "incomplete_scan" if late else ("timeout" if cls is RequestTimeoutError else "network"))
    root = exc.cause if late else exc
    assert isinstance(root, cls)
    if late:
        assert exc.pages_completed == 1 and exc.__cause__ is root
    assert len(calls) == attempts + late and sleeps == ([1, 2] if attempts == 3 else [])
    assert all(stream.closed for stream in streams)



def test_library_cookie_extraction_never_breaks_pagination():
    script = iter([(200, [("Set-Cookie", "auth_session=planted; Path=/"), ("Link", link(page(2)))], Wire(b"[]")),
                   (200, [], Wire(b"[]"))])
    session, client, calls, _, _ = library_stack(lambda: next(script))
    with session:
        assert discover(client).pages == 2
    assert not session.cookies and len(calls) == 2
    assert all("Cookie" not in call["headers"] for call in calls)



def test_library_proxy_authorization_is_removed_without_mutating_session():
    session, client, calls, _, _ = library_stack(lambda: (200, [], Wire(b"[]")))
    session.headers["Proxy-Authorization"] = "Basic planted"
    with session:
        assert discover(client).pages == 1
    assert "Proxy-Authorization" not in calls[0]["headers"]
    assert session.headers["Proxy-Authorization"] == "Basic planted"


@pytest.mark.parametrize("compressed", [False, True])
def test_library_stream_caps_and_closure(compressed):
    data = gzip.compress(b"\0" * (64 * 1024 * 1024)) if compressed else b"x" * (16 * 1024 * 1024 + 1)
    raw = Wire(data, limit=1024 if compressed else None)
    headers = [("Content-Encoding", "gzip")] if compressed else []
    session, client, calls, _, sleeps = library_stack(lambda: (200, headers, raw))
    with session:
        error(client, InvalidResponseError, "response_too_large", 200)
    assert raw.closed and len(calls) == 1 and sleeps == []
    if compressed:
        assert raw.read_bytes < len(data) // 2


@pytest.mark.parametrize("name,values,cls,code", [
    ("Link", [link(page(2)), link(page(2))], None, None),
    ("Link", [link(page(2)), link(page(3))], PaginationError, "bad_pagination"),
    ("Retry-After", ["30", "30"], RateLimitError, "rate_limited"),
    ("Location", ["/repositories/1/releases", "/repositories/2/releases"], UnsafeRedirectError, "unsafe_redirect"),
])
def test_library_duplicate_headers(name, values, cls, code):
    status = 429 if name == "Retry-After" else 302 if name == "Location" else 200
    script = iter([(status, [(name, value) for value in values], Wire(b"[]")), (200, [], Wire(b"[]"))])
    session, client, calls, _, sleeps = library_stack(lambda: next(script))
    with session:
        if cls is None:
            assert discover(client).pages == 2
        else:
            exc = error(client, cls, code)
            if cls is RateLimitError:
                assert (exc.kind, exc.retry_after_seconds) == ("too_many_requests", 60)
    assert len(calls) == (2 if cls is None else 1) and sleeps == []
