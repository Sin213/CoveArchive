"""Offline Codeberg wire-format discovery and fail-closed contracts."""

import io
import json
import socket

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from types import SimpleNamespace as NS
from covearchive.forgejo import ForgejoReleaseClient
from test_transfer_resolve import public_dns
from covearchive.github.client import (
    DirectSession, IncompleteScanError, PaginationError, InvalidResponseError,
    InvalidTargetError, UnsafeRedirectError, RateLimitError, NotFoundError,
    RequestTimeoutError, NetworkError, TransientServerError, CredentialedSessionError,
)
from covearchive.github.select import select_releases
from covearchive.github.urls import RepositoryTarget, ReleaseListTarget

ROOT = "https://codeberg.org"
PATH = "/api/v1/repos/Owner/Repo/releases"
TARGET = RepositoryTarget("Owner", "Repo", "codeberg", "codeberg.org")
DATE = "2026-10-26T19:39:38+01:00"

def page(number):
    return ROOT + PATH + f"?limit=50&page={number}"

def link(number, relation="next"):
    return f'<{page(number)}>; rel="{relation}"'

def asset(id=1, **changes):
    result = dict(id=id, name="app.zip", size=93, type="attachment", created_at=DATE,
                  uuid="bbde8b93-f9d8-4323-a4c6-3fbd119e5fd4", download_count=1,
                  browser_download_url="https://evil.example/secret")
    result.update(changes)
    return result

def release(id=1, **changes):
    result = dict(id=id, tag_name=f"v{id}", name=f"Release {id}", draft=False,
                  prerelease=False, created_at=DATE, published_at=DATE,
                  url=ROOT + PATH + f"/{id}", assets=[asset(id)])
    result.update(changes)
    return result

class Response:
    def __init__(self, body=(), *, total=0, headers=None, status=200):
        self.status_code = status
        self.headers = CaseInsensitiveDict({"X-Total-Count": str(total), "Content-Type": "application/json"} | (headers or {}))
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.closed = 0

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        self.closed += 1

class Session:
    def __init__(self, script):
        self.script, self.calls, self.responses = list(script), [], []

    def get(self, url, **kwargs):
        assert all(r.closed == 1 for r in self.responses)
        self.calls.append((url, kwargs))
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        assert kwargs["timeout"] == (10.0, 30.0)
        assert kwargs["headers"]["Accept"] == "application/json"
        assert "X-GitHub-Api-Version" not in kwargs["headers"]
        assert all(kwargs["headers"][key] is None for key in
                   ("Authorization", "Cookie", "Proxy-Authorization", "Host"))
        assert callable(kwargs["auth"])
        assert self.script, "unexpected request"
        response = self.script.pop(0)
        if isinstance(response, Exception):
            raise response
        self.responses.append(response)
        return response

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("real network forbidden")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)

def client(script, **options):
    session, sleeps = Session(script), []
    return ForgejoReleaseClient(session, resolver=public_dns, clock=lambda: 1000, sleep=sleeps.append, **options), session, sleeps

def rejects(script, *, error, **options):
    api, _, _ = client(script, **options)
    with pytest.raises(error): api.discover_releases(TARGET)

def test_native_attachment_mapping_and_provider_identity():
    api, session, _ = client([Response([release()], total=1)])
    result = api.discover_releases(TARGET)
    assert [r.id for r in result.releases] == [1]
    item = result.releases[0].assets[0]
    assert (item.state, item.size, item.updated_at) == ("uploaded", 93, DATE)
    assert item.browser_download_url == item.content_type == ""
    assert item.original_digest is item.digest_algorithm is item.sha256 is None
    assert result.repository == TARGET and result.complete
    assert result.releases[0].body is result.releases[0].tarball_url is result.releases[0].zipball_url is None
    assert (result.pages, result.requests, result.redirects) == (1, 1, 0)
    assert session.calls[0][0] == page(1)

def test_partial_listing_cannot_be_reported_complete():
    rejects([Response([release()], total=2)], error=IncompleteScanError)

@pytest.mark.parametrize("target", [TARGET, ReleaseListTarget("Owner", "Repo", "codeberg", "codeberg.org")])
@pytest.mark.parametrize("query", ["limit=50&page=2", "page=2&limit=50"])
def test_real_wire_pagination_and_canonical_case(target, query):
    rows = [release(i) for i in range(1, 51)]
    header = (link(2) + ", " + link(2, "last")).replace("limit=50&page=2", query)
    api, session, _ = client([Response(rows, total=51, headers={"Link": header}),
                              Response([release(51)], total=51, headers={"Link": link(1, "prev")})])
    lower = type(target)("owner", "repo", "codeberg", "codeberg.org")
    catalog = api.discover_releases(lower)
    assert catalog.repository == TARGET
    assert [r.id for r in catalog.releases] == list(range(1, 52))
    assert catalog.pages == 2 and catalog.requests == 2
    assert session.calls[0][0] == page(1).replace("Owner/Repo", "owner/repo")
    assert session.calls[1][0] == page(2).replace("Owner/Repo", "owner/repo")

def test_empty_catalog():
    api, _, _ = client([Response()])
    result = api.discover_releases(TARGET)
    assert result.complete and result.releases == () and result.repository == TARGET

@pytest.mark.parametrize("headers", [
    {"Link": link(99, "first")}, {"Link": link(2, "prev")},
    {"Link": "broken"}, {"Link": ""}, {"Link": '<bad; rel="next"'},
    {"Link": link(1)}, {"Link": link(0)}, {"Link": link(3)},
    {"Link": link(2) + ", " + link(3)}, {"Link": link(2, "last")},
    {"Link": link(2) + ", " + link(1, "last")},
    {"Link": link(2).replace("codeberg.org", "api.github.com")},
    {"Link": link(2).replace("limit=50", "limit=500")},
    {"Link": link(2).replace("page=2", "page=2&page=3")},
    {"Link": link(2).replace("page=2", "page=%32")},
    {"Link": link(2).replace("/Owner/Repo/", "/Other/Repo/")},
    {"Link": link(2).replace("/api/v1/", "/API/v1/")},
    {"Link": link(2).replace("page=2", "page=2&draft=true")},
    {"Link": link(2).replace("page=2", "page=2&pre-release=false")},
    {"X-Total-Count": "-1"}, {"X-Total-Count": "True"}, {"X-Total-Count": None},
])
def test_bad_pagination_fails_closed(headers):
    api, session, _ = client([Response([release()], total=2, headers=headers)])
    with pytest.raises(PaginationError):
        api.discover_releases(TARGET)
    assert len(session.calls) == 1

@pytest.mark.parametrize('addresses', [('10.0.0.1',), ('fd00::1',), ('127.0.0.1',), ('::1',), ('8.8.8.8','10.0.0.1'), None])
def test_discovery_public_dns_before_transport(addresses):
    api, session, _ = client([Response()])
    def resolver(*args):
        if addresses is None: raise OSError('https://secret')
        return [(2,1,6,'',(ip,443)) for ip in addresses]
    api.resolver = resolver
    with pytest.raises(NetworkError if addresses is None else UnsafeRedirectError): api.discover_releases(TARGET)
    assert session.calls == []

@pytest.mark.parametrize("second", [
    Response([release(2)], total=3),
    Response([release(2)], total=2, headers={"Link": link(1)}),
    Response([], total=2, headers={"Link": link(3)}),
    Response([release()], total=2, headers={"Link": link(3)}),
    Response([release(1, tag_name="changed")], total=2),
    Response([release(2, assets=[asset(1)])], total=2),
    Response(b"not json", total=2),
])
def test_late_inconsistency_never_returns_partial_catalog(second):
    api, _, _ = client([Response([release()], total=2, headers={"Link": link(2)}), second])
    with pytest.raises(IncompleteScanError) as caught:
        api.discover_releases(TARGET)
    assert caught.value.pages_completed == 1

def test_caps_and_total_count_mismatch():
    for options in ({"max_pages": 1}, {"max_requests": 1}):
        api, session, _ = client([Response([release()], total=2, headers={"Link": link(2)})], **options)
        with pytest.raises(IncompleteScanError):
            api.discover_releases(TARGET)
        assert len(session.calls) == 1
    api, _, _ = client([Response([release()], total=0)])
    with pytest.raises(PaginationError):
        api.discover_releases(TARGET)

def test_identical_release_duplicates_deduplicate_without_attachment_collision():
    api, _, _ = client([Response([release(), release()], total=1)])
    result = api.discover_releases(TARGET)
    assert len(result.releases) == 1 and result.duplicate_release_ids == (1,)

@pytest.mark.parametrize("rows", [
    [release(), release(tag_name="changed")],
    [release(assets=[asset(), asset()])],
    [release(), release(2, assets=[asset()])],
])
def test_conflicting_or_duplicate_ids_rejected(rows):
    rejects([Response(rows, total=len(rows))], error=PaginationError)

@pytest.mark.parametrize("value", [True, False, -1, 1.5, "1", None, 0])
@pytest.mark.parametrize("field", ["release_id", "asset_id", "asset_size"])
def test_invalid_numeric_fields(value, field):
    if field == "asset_size" and value in (None, 0) and type(value) is not bool:
        return  # Unknown/zero sizes have separate behavioral tests below.
    row = release(id=value) if field == "release_id" else release(
        assets=[asset(**{field.removeprefix("asset_"): value})])
    api, _, _ = client([Response([row], total=1)])
    with pytest.raises(InvalidResponseError):
        api.discover_releases(TARGET)

def test_external_missing_size_zero_size_and_drafts_use_existing_selection():
    unknown = asset(2)
    del unknown["size"]
    rows = [release(assets=[asset(1, size=0), unknown, asset(3, type="external"),
                            asset(4, size=None)]), release(2, draft=True, assets=[])]
    api, _, _ = client([Response(rows, total=2)])
    catalog = api.discover_releases(TARGET)
    assert [r.id for r in catalog.releases] == [1]
    assert [(a.state, a.size) for a in catalog.releases[0].assets] == [
        ("uploaded", 0), ("external", 0), ("external", 93), ("external", 0)]
    assert [m.asset.id for m in select_releases(catalog).releases[0].matches] == [1]

@pytest.mark.parametrize("row", [
    None, {}, release(assets=None), release(assets=[None]), release(draft="false"),
    release(assets=[asset(type="unknown")]), release(assets=[asset(created_at="invalid")]),
    release(url="https://evil.example/secret"), release(url=ROOT + PATH + "/2"),
    release(url=ROOT + PATH.replace("Owner", "Other") + "/1"),
])
def test_malformed_release_or_ownership_rejected(row):
    rejects([Response([row], total=1)], error=InvalidResponseError)

@pytest.mark.parametrize("body,headers", [
    (b"not json", {}), (b"[NaN]", {}), (b"{}", {}),
    (b"[]", {"Content-Type": "text/html"}), (b"[]", {"Content-Type": None}),
])
def test_non_json_or_wrong_content_type(body, headers):
    rejects([Response(body, headers=headers)], error=InvalidResponseError)

def test_oversized_body_is_closed_and_never_retried():
    response = Response(b"x" * 65)
    api, session, sleeps = client([response], max_response_bytes=64)
    with pytest.raises(InvalidResponseError) as caught:
        api.discover_releases(TARGET)
    assert caught.value.code == "response_too_large"
    assert response.closed == 1 and len(session.calls) == 1 and not sleeps

@pytest.mark.parametrize("location", [
    "https://evil.example/secret", "https://api.github.com/repos/o/r/releases",
    "https://codeberg.org:443" + PATH, "https://user@codeberg.org" + PATH,
    "http://codeberg.org" + PATH, ROOT + PATH.replace("Owner", "Other"),
    ROOT + PATH + "?page=9", "\\evil.example/secret", None,
])
def test_unsafe_redirects_rejected_before_second_request(location):
    api, session, _ = client([Response(status=302, headers={"Location": location})])
    with pytest.raises(UnsafeRedirectError) as caught:
        api.discover_releases(TARGET)
    assert len(session.calls) == 1 and "secret" not in str(caught.value)

def test_checked_same_host_case_redirect_and_hop_cap():
    moved = PATH.replace("Owner/Repo", "owner/repo")
    api, session, _ = client([Response(status=302, headers={"Location": moved}),
                              Response([release()], total=1)])
    result = api.discover_releases(TARGET)
    assert result.redirects == 1 and session.calls[1][0] == ROOT + moved + "?limit=50&page=1"
    api, _, _ = client([Response(status=302, headers={"Location": moved})], max_redirects=0)
    with pytest.raises(UnsafeRedirectError):
        api.discover_releases(TARGET)

def test_shared_not_found_and_rate_limit_errors():
    rejects([Response(status=404)], error=NotFoundError)
    api, session, sleeps = client([Response(status=429, headers={"Retry-After": "30"})])
    with pytest.raises(RateLimitError) as caught:
        api.discover_releases(TARGET)
    assert caught.value.retry_after_seconds == 30 and caught.value.retry_at == 1030
    assert len(session.calls) == 1 and not sleeps

@pytest.mark.parametrize("failure,error", [
    (lambda: requests.Timeout("secret"), RequestTimeoutError),
    (lambda: requests.ConnectionError("secret"), NetworkError),
    (lambda: Response(status=503), TransientServerError),
])
def test_shared_retry_budget_and_url_free_errors(failure, error):
    api, session, sleeps = client([failure() for _ in range(3)])
    with pytest.raises(error) as caught:
        api.discover_releases(TARGET)
    assert len(session.calls) == 3 and sleeps == [1, 2]
    assert "secret" not in str(caught.value)

@pytest.mark.parametrize("target", [RepositoryTarget("Owner", "Repo"), None,
    RepositoryTarget("Owner", "Repo", "codeberg", "evil.example")])
def test_non_codeberg_targets_rejected_without_requests(target):
    api, session, _ = client([])
    with pytest.raises(InvalidTargetError):
        api.discover_releases(target)
    assert session.calls == []

def test_real_direct_session_forces_anonymous_verified_transport(monkeypatch):
    calls = []
    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **options):
            calls.append(request)
            assert options["verify"] is True and options["cert"] is None
            assert not options["proxies"]
            assert request.url == page(1)
            assert all(name not in request.headers for name in
                       ("Authorization", "Cookie", "Proxy-Authorization", "Host"))
            response = requests.Response()
            response.status_code, response.raw = 200, io.BytesIO(b"[]")
            response.headers.update({"Content-Type": "application/json", "X-Total-Count": "0"})
            return response
        def close(self):
            pass
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:9999")
    with DirectSession() as session:
        session.mount("https://", Adapter())
        session.auth = ("secret", "secret")
        session.headers.update({key: "secret" for key in ("Authorization", "Cookie", "Proxy-Authorization", "Host")})
        session.params, session.proxies = {"token": "secret"}, {"https": "http://evil.example"}
        session.verify, session.cert = False, "secret.pem"
        api = ForgejoReleaseClient(session, resolver=public_dns)
        assert api.discover_releases(TARGET).complete
        assert not session.trust_env and len(calls) == 1
        session.cookies.set("auth", "secret", domain="codeberg.org", path="/")
        with pytest.raises(CredentialedSessionError):
            api.discover_releases(TARGET)
        assert len(calls) == 1

@pytest.mark.parametrize('status',[302,503])
def test_discovery_dns_rechecked_on_every_attempt(status):
    api, session, _ = client([Response(status=status,headers={'Location':PATH.lower()}),Response()]); calls=[]
    def resolver(*args):
        calls.append(args); return public_dns() if len(calls)==1 else [(2,1,6,'',('10.0.0.1',443))]
    api.resolver=resolver
    with pytest.raises(UnsafeRedirectError): api.discover_releases(TARGET)
    assert len(session.calls)==1 and len(calls)==2

def test_discovery_default_dns_and_connected_peer(monkeypatch):
    calls=[]; monkeypatch.setattr(socket,'getaddrinfo',lambda *a:calls.append(a) or public_dns())
    response=Response(); response.raw=NS(_connection=NS(sock=NS(getpeername=lambda:('127.0.0.1',443))))
    session=Session([response]); api=ForgejoReleaseClient(session)
    with pytest.raises(UnsafeRedirectError): api.discover_releases(TARGET)
    assert len(calls)==len(session.calls)==response.closed==1
