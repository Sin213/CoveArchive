"""Controlled local transport fixtures and CA-S7 boundary behavior."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import http.server
import io
import socket
import threading
import traceback
from urllib.parse import urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict
from covearchive.github.client import DirectSession
from covearchive.github.models import ReleaseAsset
from covearchive.github.urls import RepositoryTarget
from covearchive.transfer import resolve as r

TARGET = RepositoryTarget("Owner", "Repo")
BODY = b"fixture archive bytes"
SECRET = "sig=private&se=secret&sp=read"

def asset(size=len(BODY)):
    return ReleaseAsset(7, "app.zip", size, "uploaded", "application/zip",
        "https://hostile.invalid/never", None, None, None, "", "", ".zip")

@contextmanager
def fixture_server():
    """Reusable deterministic API/CDN server; routes are mutable response callbacks."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            server.requests.append((self.path, dict(self.headers)))
            route = server.routes.get(urlsplit(self.path).path)
            status, headers, body = route(self.path) if callable(route) else (
                route or (404, {}, b""))
            self.send_response(status)
            for key, value in headers.items(): self.send_header(key, value)
            self.end_headers()
            try: self.wfile.write(body)
            except (ConnectionError, OSError): pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False  # server_close joins every request worker.
    server.requests, server.routes = [], {}
    server.origin = f"http://127.0.0.1:{server.server_port}"
    server.routes["/repos/Owner/Repo/releases/assets/7"] = (302, {"Location": "/cdn?" + SECRET}, b"")
    server.routes["/cdn"] = (200, {"Content-Length": str(len(BODY))}, BODY)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try: yield server
    finally:
        server.shutdown(); server.server_close(); thread.join(3)
        assert not thread.is_alive()

@pytest.fixture
def server():
    with fixture_server() as value: yield value

def consume(server, **kwargs):
    kwargs.setdefault("policy", r.TransferPolicy.for_loopback_test(server.origin))
    with r.open_asset_stream(TARGET, asset(), **kwargs) as stream: return b"".join(stream)

@pytest.mark.parametrize("url", [None, 7, "", "http://api.github.com/a", "https://evil.invalid/a",
    "https://api.github.com/a#sig=secret", "https://API.github.com/a", "https://api.github.com./a",
    "https://api.github.com:8443/a", "https://user:secret@api.github.com/a", "https://127.0.0.1/a",
    "https://[::1]/a", "https://localhost/a", "https://api.github.com/a\\b",
    "https://api.github.com/a b", "https://api.github.com/a\n", "https://api.github.com/" + "x"*2048,
    "https://?.github.com/a"])
def test_unsafe_hop_is_rejected(url):
    with pytest.raises(r.DownloadError): r.validate_hop(url, resolver=lambda *a: pytest.fail("DNS before validation"))

@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "224.0.0.1"])
def test_private_dns_rejected(address):
    with pytest.raises(r.DownloadError) as caught:
        r.validate_hop("https://api.github.com/a", resolver=lambda *a: [(2,1,6,"",(address,443))])
    assert caught.value.code == "unsafe_redirect"

def test_policy_isolation(server):
    with pytest.raises(FrozenInstanceError): r.PRODUCTION_POLICY.max_redirects = 9
    with pytest.raises(r.DownloadError): r.validate_hop(server.origin)
    with pytest.raises(ValueError): r.TransferPolicy.for_loopback_test("http://localhost:80")
    with pytest.raises(TypeError): r.TransferPolicy(_test_origin=server.origin)
    assert consume(server) == BODY

def test_anonymous_direct_environment(server, monkeypatch, tmp_path):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    netrc = tmp_path / "netrc"; netrc.write_text("machine 127.0.0.1 login secret password secret\n")
    monkeypatch.setenv("NETRC", str(netrc))
    session = DirectSession(); session.auth = ("secret", "secret"); session.verify = False
    session.headers.update({"Authorization": "secret", "Cookie": "secret", "Proxy-Authorization": "secret"})
    session.params = {"sig": "secret"}; session.proxies = {"http": "http://127.0.0.1:1"}
    session.hooks["response"] = [lambda *a, **k: pytest.fail("hook")]
    assert consume(server, session=session) == BODY
    assert session.verify is True and session.trust_env is False and not session.proxies
    for path, headers in server.requests:
        assert "hostile" not in path and path.count("sig=") <= 1
        assert not any(k.lower() in ("authorization", "cookie", "proxy-authorization") for k in headers)
        assert headers["Accept-Encoding"] == "identity"
    assert len(server.requests) == 2

@pytest.mark.parametrize("status,headers,body,code", [
    (200,{"Content-Length":"1"},BODY,"size_mismatch"), (200,{"Content-Encoding":"gzip"},BODY,"unexpected_encoding"),
    (206,{},BODY,"unexpected_status"), (200,{"Content-Range":"bytes 0-1/20"},BODY,"unexpected_status"),
    (200,{"Content-Type":"text/html"},BODY,"unexpected_content"),
    (200,{},BODY+b"extra","size_mismatch"), (200,{},BODY[:-1],"partial_transfer"),
    (200,{"Content-Length":str(len(BODY))},BODY[:-1],"partial_transfer")])
def test_bad_body(server, status, headers, body, code):
    server.routes["/cdn"] = status, headers, body
    with pytest.raises(r.DownloadError) as caught: consume(server)
    assert caught.value.code == code

@pytest.mark.parametrize("location", ["http://localhost:1/a", "http://127.0.0.2:1/a", "https://evil.invalid/a",
    "http://127.0.0.1:1/a", "/bad\\path", "/bad path", "/a#fragment", "", None])
def test_redirect_rejected_without_follow(server, location):
    server.routes["/cdn"] = 302, ({} if location is None else {"Location":location}), b""
    with pytest.raises(r.DownloadError): consume(server)
    assert len(server.requests) == 2

@pytest.mark.parametrize("status", [300,304,305,306])
def test_invalid_redirect_status(server, status):
    server.routes["/cdn"] = status, {"Location":"/other"}, b""
    with pytest.raises(r.DownloadError): consume(server)
    assert len(server.requests) == 2

def test_loop_and_hop_limit(server):
    server.routes["/cdn"] = 302, {"Location":"/cdn?"+SECRET}, b""
    with pytest.raises(r.DownloadError) as caught: consume(server)
    assert caught.value.code == "unsafe_redirect" and len(server.requests) == 2
    server.requests.clear(); server.routes["/cdn"] = 302, {"Location":"/other"}, b""
    with pytest.raises(r.DownloadError): consume(server, policy=r.TransferPolicy.for_loopback_test(server.origin,max_redirects=1))
    assert len(server.requests) == 2

@pytest.mark.parametrize("status,headers,code,wait", [(404,{},"upstream_removed",None),
    (403,{},"forbidden",None), (429,{"Retry-After":"123"},"rate_limited",123),
    (403,{"X-RateLimit-Remaining":"0"},"rate_limited",60),
    (429,{"Retry-After":"999999999"},"rate_limited",86400)])
def test_api_status(server, status, headers, code, wait):
    server.routes["/repos/Owner/Repo/releases/assets/7"] = status, headers, b""
    with pytest.raises(r.DownloadError) as caught: consume(server)
    assert caught.value.code == code and caught.value.retry_after_seconds == wait
    assert len(server.requests) == 1

@pytest.mark.parametrize('headers,wait',[({},60),({'Retry-After':'20'},20),({'X-RateLimit-Remaining':'0','X-RateLimit-Reset':'130'},30),({'X-RateLimit-Remaining':'0','X-RateLimit-Reset':'90'},1),({'X-RateLimit-Remaining':'0','X-RateLimit-Reset':'9999999'},86400),({'X-RateLimit-Reset':'130'},60),({'Retry-After':'20','X-RateLimit-Remaining':'0','X-RateLimit-Reset':'130'},30)])
def test_Q19_S7_reset_header(server,monkeypatch,headers,wait):
    monkeypatch.setattr(r.time,'time',lambda:100)
    server.routes['/repos/Owner/Repo/releases/assets/7']=(429,headers,b'')
    with pytest.raises(r.DownloadError) as exc: consume(server)
    assert exc.value.retry_after_seconds==wait

@pytest.mark.parametrize("status", [400,403,410])
def test_expiry_refresh_once(server, status):
    count = []
    def expired(path):
        count.append(1)
        return (status,{},b"") if len(count)==1 else (200,{},BODY)
    server.routes["/cdn"] = expired
    assert consume(server) == BODY
    assert len(server.requests) == 4
    server.requests.clear(); server.routes["/cdn"] = status,{},b""
    with pytest.raises(r.DownloadError) as caught: consume(server)
    assert caught.value.code == "expired_url" and len(server.requests) == 4

@pytest.mark.parametrize("status", [500,502,503,504])
def test_server_retries_bounded(server, status):
    server.routes["/cdn"] = status,{},b""; waits=[]
    with pytest.raises(r.DownloadError) as caught: consume(server, sleep=waits.append)
    assert caught.value.code == "server_error" and waits == [1,2]
    assert len(server.requests) == 4

def test_cancel_deadline_and_no_length(server):
    assert consume(server) == BODY
    server.requests.clear()
    with pytest.raises(r.DownloadError) as caught: consume(server, cancel=lambda: True)
    assert caught.value.code == "cancelled" and not server.requests
    with pytest.raises(r.DownloadError) as caught:
        consume(server, policy=r.TransferPolicy.for_loopback_test(server.origin,deadline_seconds=.001), clock=iter([0,1]).__next__)
    assert caught.value.code == "timeout" and not server.requests
    server.routes["/cdn"] = 200,{},BODY
    assert consume(server) == BODY
    calls=[]
    def cancel(): calls.append(1); return len(calls)>4
    with pytest.raises(r.DownloadError) as caught: consume(server, cancel=cancel)
    assert caught.value.code == "cancelled"

class ScriptAdapter(BaseAdapter):
    def __init__(self, script): self.script=list(script); self.calls=[]; self.closed=False
    def send(self, request, **kwargs):
        self.calls.append((request,kwargs)); item=self.script.pop(0)
        if isinstance(item, Exception): raise item
        status, headers, body = item
        response=requests.Response(); response.status_code=status; response.headers=CaseInsensitiveDict(headers)
        response.raw=io.BytesIO(body); response.url=request.url; response.request=request
        return response
    def close(self): self.closed=True

def test_transport_privacy_tls_retry_and_verify(server, caplog):
    for error,code,count in [(requests.exceptions.SSLError("https://secret/?"+SECRET),"tls_error",1),
        (requests.Timeout("https://secret/?"+SECRET),"timeout",3),
        (requests.ConnectionError("https://secret/?"+SECRET),"network",3)]:
        session=DirectSession(); adapter=ScriptAdapter([error]*count); session.mount("http://",adapter); waits=[]
        with pytest.raises(r.DownloadError) as caught: consume(server,session=session,sleep=waits.append)
        assert caught.value.code == code and len(adapter.calls)==count
        text="".join(traceback.format_exception(caught.value))+repr(caught.value)+caplog.text
        assert "sig=" not in text and "https://secret" not in text and caught.value.__cause__ is None
        assert all(k["verify"] is True and k["proxies"] == {} and k["stream"] is True for _,k in adapter.calls)
        assert adapter.closed

def test_header_checks_before_yield_and_resources(server):
    session=DirectSession(); adapter=ScriptAdapter([(302,{"Location":"/cdn"},b""),(200,{"Content-Length":"1"},b"x")])
    session.mount("http://",adapter)
    with pytest.raises(r.DownloadError):
        with r.open_asset_stream(TARGET,asset(),policy=r.TransferPolicy.for_loopback_test(server.origin),session=session):
            pytest.fail("entered despite invalid headers")
    assert adapter.closed


def public_dns(*args): return [(2,1,6,"",("140.82.112.6",443))]

@pytest.mark.parametrize("host", sorted(r._HOSTS))
def test_public_hosts_and_mixed_dns(host):
    url="https://"+host+"/asset?"+SECRET
    assert r.validate_hop(url,resolver=public_dns) == url
    with pytest.raises(r.DownloadError):
        r.validate_hop(url,resolver=lambda *a: public_dns()+[(2,1,6,"",("10.0.0.1",443))])

def test_changed_redirect_is_revalidated(server):
    assert consume(server) == BODY
    server.requests.clear()
    server.routes["/repos/Owner/Repo/releases/assets/7"] = 302,{"Location":"http://127.0.0.2:1/?"+SECRET},b""
    with pytest.raises(r.DownloadError) as caught: consume(server)
    assert len(server.requests)==1 and caught.value.code == "unsafe_redirect"
    assert SECRET not in str(caught.value)

@pytest.mark.parametrize("location", ["/repos/Owner/Repo/releases/assets/8", "https://evil.invalid/?"+SECRET,
    "http://objects.githubusercontent.com/a", "https://objects.githubusercontent.com:8443/a"])
def test_production_actual_redirect_boundary(location):
    session=DirectSession(); adapter=ScriptAdapter([(302,{"Location":location},b"")]); session.mount("https://",adapter)
    with pytest.raises(r.DownloadError):
        with r.open_asset_stream(TARGET,asset(),session=session,resolver=public_dns): pytest.fail("entered")
    assert len(adapter.calls)==1 and adapter.closed

def test_connected_private_peer_rejected(monkeypatch):
    class PeerRaw(io.BytesIO):
        _connection=type("Connection",(),{"sock":type("Sock",(),{"getpeername":lambda self: ("10.0.0.1",443)})()})()
    session=DirectSession(); adapter=ScriptAdapter([(302,{"Location":"https://objects.githubusercontent.com/a"},b"")])
    send=adapter.send
    def peer(*args,**kwargs):
        response=send(*args,**kwargs); response.raw=PeerRaw(); return response
    monkeypatch.setattr(adapter,"send",peer); session.mount("https://",adapter)
    with pytest.raises(r.DownloadError) as caught:
        with r.open_asset_stream(TARGET,asset(),session=session,resolver=public_dns): pytest.fail("entered")
    assert caught.value.code=="unsafe_redirect" and len(adapter.calls)==1


def test_cancel_and_deadline_between_real_chunks(server):
    data=b"x"*131072
    server.routes["/cdn"] = 200,{},data
    policy=r.TransferPolicy.for_loopback_test(server.origin)
    cancelled=[False]
    with r.open_asset_stream(TARGET,asset(len(data)),policy=policy,cancel=lambda: cancelled[0]) as chunks:
        assert 0<len(next(chunks))<=65536
        cancelled[0]=True
        with pytest.raises(r.DownloadError) as caught: next(chunks)
        assert caught.value.code=="cancelled"
    now=[0]
    with r.open_asset_stream(TARGET,asset(len(data)),policy=policy,clock=lambda: now[0]) as chunks:
        assert 0<len(next(chunks))<=65536
        now[0]=policy.deadline_seconds+1
        with pytest.raises(r.DownloadError) as caught: next(chunks)
        assert caught.value.code=="timeout"

def test_oversize_reads_only_expected_plus_one():
    class Raw(io.BytesIO):
        def read(self, amount, decode_content=False):
            assert decode_content is False and amount<=1024*1024
            return super().read(amount)
    raw=Raw(BODY+b"extra")
    response=type("Response",(),{"raw":raw})()
    with pytest.raises(r.DownloadError) as caught:
        list(r._chunks(response,len(BODY),None,lambda:0,1))
    assert caught.value.code=="size_mismatch" and raw.tell()==len(BODY)+1

@pytest.mark.parametrize("target,selected", [(RepositoryTarget("Owner","Repo.git"),asset()),
    (TARGET,ReleaseAsset(True,"app.zip",1,"uploaded","", "",None,None,None,"","",".zip"))])
def test_invalid_identity_never_requests(target, selected):
    with pytest.raises(r.DownloadError) as caught:
        with r.open_asset_stream(target,selected): pytest.fail("entered")
    assert caught.value.code=="invalid_asset_identity"


@pytest.mark.parametrize('mode',['parse','dns','answer','peer','fields'])
def test_pf3_boundary_errors_are_detached(mode):
    marker='https://[sig=private&se=secret&sp=read]:bad/'
    def bad_dns(*args): raise OSError(marker)
    with pytest.raises(r.DownloadError) as caught:
        if mode=='parse': r.validate_hop(marker)
        elif mode=='dns': r.validate_hop('https://api.github.com/',resolver=bad_dns)
        elif mode=='answer': r.validate_hop('https://api.github.com/',resolver=lambda *a:[(2,1,6,'',(marker,443))])
        elif mode=='peer':
            sock=type('Sock',(),{'getpeername':lambda self:(marker,443)})()
            raw=type('Raw',(),{'_connection':type('Conn',(),{'sock':sock})()})()
            r._peer(type('Response',(),{'raw':raw})(),r.PRODUCTION_POLICY)
        else: raise r.DownloadError(marker,retry_after_seconds=marker)
    error=caught.value
    assert error.__context__ is None and error.__cause__ is None
    assert not any(part in str(error)+repr(vars(error)) for part in ('sig=','se=','sp=',marker))
