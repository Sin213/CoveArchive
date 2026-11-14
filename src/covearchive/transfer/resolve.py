"""Anonymous, redirect-controlled release streaming; private repositories unsupported.

DNS validation precedes each request; requests resolves again, so a DNS rebinding
window remains. Connected-peer validation is best effort when a socket is exposed.
Stalls and cancel latency are bounded by the per-socket read timeout. Partial reads
return control between byte arrivals; the overall budget is six hours.
No resume, buffering of whole bodies, automatic redirects, or URL-bearing errors.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import ipaddress
import logging
import math
import re
import socket
import time
import threading
from urllib.parse import urljoin, urlsplit

import requests

from covearchive import __version__
from covearchive.github.client import API_VERSION, DirectSession
from covearchive.github.models import ReleaseAsset
from covearchive.github.urls import RepositoryTarget, parse_github_url

# Documented/observed GitHub release-asset hosts; re-verify when GitHub changes:
# API serves the identity-bound entry; release-assets is the current signed CDN;
# objects and github-releases are established release-asset storage endpoints.
_HOSTS = frozenset(("api.github.com", "release-assets.githubusercontent.com",
                    "objects.githubusercontent.com", "github-releases.githubusercontent.com"))


@dataclass(frozen=True)
class TransferPolicy:
    max_redirects: int = 3
    timeout: tuple = (10.0, 30.0)
    deadline_seconds: float = 21600.0
    _test_origin: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        if (type(self.max_redirects) is not int or not 0 <= self.max_redirects <= 10
                or not isinstance(self.timeout, tuple) or len(self.timeout) != 2
                or any(type(v) not in (float,int) or not math.isfinite(v) or v <= 0 for v in self.timeout)
                or type(self.deadline_seconds) not in (float,int)
                or not math.isfinite(self.deadline_seconds) or self.deadline_seconds <= 0):
            raise ValueError("Invalid transfer policy.")

    @classmethod
    def for_loopback_test(cls, origin, **options):
        """Explicit fixture-only bypass, pinned to one IPv4 loopback listener."""
        if not isinstance(origin, str) or re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", origin) is None:
            raise ValueError("Invalid test origin.")
        if not 1 <= int(origin.rsplit(":",1)[1]) <= 65535:
            raise ValueError("Invalid test origin.")
        policy = cls(**options)
        object.__setattr__(policy, "_test_origin", origin)
        return policy


PRODUCTION_POLICY = TransferPolicy()


class DownloadError(Exception):
    """Stable, URL-free error metadata; remote error text is never retained."""
    def __init__(self, code, *, retry_after_seconds=None, retryable=False):
        self.code = code if isinstance(code,str) and re.fullmatch(r"[a-z_]{1,64}",code) else "download_error"
        self.retry_after_seconds = retry_after_seconds if type(retry_after_seconds) is int else None
        self.retryable = bool(retryable)
        super().__init__("GitHub CDN may have changed; retry after updating CoveArchive."
            if self.code == "unknown_cdn_host" else self.code)


def _safe_text(url):
    return (isinstance(url,str) and 0 < len(url) <= 2048 and url.isascii()
        and not any(c.isspace() or ord(c)<32 or ord(c)==127 or c in "\\#" for c in url))


def _public(address):
    value = ipaddress.ip_address(address)
    return value.is_global and not value.is_multicast


def validate_hop(url, *, policy=PRODUCTION_POLICY, resolver=socket.getaddrinfo):
    """Validate syntax, exact host and every DNS answer without requesting bytes."""
    if not isinstance(policy,TransferPolicy): raise TypeError("Expected TransferPolicy.")
    if not _safe_text(url): raise DownloadError("unsafe_redirect") from None
    invalid = False
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if (parts.username is not None or parts.password is not None or not host
                or parts.netloc.endswith(":") or host != host.lower()):
            raise ValueError
        if policy._test_origin:
            if parts.scheme != "http" or parts.netloc != urlsplit(policy._test_origin).netloc:
                raise ValueError
            return url
        if (parts.scheme != "https" or parts.port not in (None,443)
                or parts.netloc != host + (":443" if parts.port else "")
                or host.endswith(".") or re.fullmatch(r"[a-z0-9.-]+",host) is None):
            raise ValueError
        try: ipaddress.ip_address(host)
        except ValueError: pass
        else: raise ValueError
    except (ValueError,TypeError,UnicodeError): invalid = True
    if invalid: raise DownloadError("unsafe_redirect") from None
    if host not in _HOSTS: raise DownloadError("unknown_cdn_host",retryable=True) from None
    failed = False
    try:
        answers = resolver(host,443,0,socket.SOCK_STREAM)
        allowed = bool(answers) and all(_public(answer[4][0]) for answer in answers)
    except (OSError,ValueError,TypeError,IndexError): failed = True
    if failed: raise DownloadError("network",retryable=True) from None
    if not allowed: raise DownloadError("unsafe_redirect") from None
    return url


def _peer(response, policy):
    # urllib3's private socket surface is not guaranteed; absence is a known limit.
    try:
        connection = getattr(response.raw,"_connection",None)
        sock = getattr(connection,"sock",None)
        if sock is None:
            fp = getattr(getattr(response.raw,"_fp",None),"fp",None)
            sock = getattr(getattr(fp,"raw",None),"_sock",None)
        if sock is None: return
        address = sock.getpeername()[0]
    except (AttributeError,OSError,ValueError,TypeError,IndexError): return
    try: allowed = address == "127.0.0.1" if policy._test_origin else _public(address)
    except (ValueError,TypeError): allowed = False
    if not allowed: raise DownloadError("unsafe_redirect") from None



@contextmanager
def _quiet_transport():
    # urllib3 DEBUG records include signed request targets. Suppress only this
    # thread's connection-pool records during I/O; keep other threads untouched.
    owner = threading.get_ident()
    class PrivateTransfer(logging.Filter):
        def filter(self, record): return record.thread != owner
    logger, guard = logging.getLogger("urllib3.connectionpool"), PrivateTransfer()
    logger.addFilter(guard)
    try: yield
    finally: logger.removeFilter(guard)


def _anonymous(request):
    for key in ("Authorization","Cookie","Proxy-Authorization"): request.headers.pop(key,None)
    return request


def _check(cancel, clock, deadline):
    if cancel is not None and cancel(): raise DownloadError("cancelled") from None
    if clock() >= deadline: raise DownloadError("timeout",retryable=True) from None



def _pause(seconds, policy, cancel, sleep, clock, deadline):
    while seconds > 0:
        _check(cancel,clock,deadline)
        step = min(seconds,policy.timeout[1],max(0,deadline-clock()))
        if step <= 0: raise DownloadError("timeout",retryable=True) from None
        sleep(step); seconds -= step
        _check(cancel,clock,deadline)


def _chunks(response, size, cancel, clock, deadline):
    total = 0
    while True:
        _check(cancel,clock,deadline)
        failed = False
        try:
            with _quiet_transport():
                amount = min(65536,size-total+1)
                read1 = getattr(response.raw,"read1",None)
                if isinstance(response.raw,requests.packages.urllib3.response.HTTPResponse) and callable(read1):
                    try: chunk = read1(amount,decode_content=False)
                    except NotImplementedError:
                        chunk = response.raw.read(min(4096,amount),decode_content=False)
                else:  # urllib3 1.x/injected transports: coarser bounded reads.
                    chunk = response.raw.read(min(4096,amount),decode_content=False)
        except (requests.RequestException, requests.packages.urllib3.exceptions.HTTPError,
                OSError, AttributeError, TypeError, NotImplementedError): failed = True
        if failed: raise DownloadError("partial_transfer",retryable=True) from None
        _check(cancel,clock,deadline)
        if not chunk: break
        total += len(chunk)
        if total > size: raise DownloadError("size_mismatch") from None
        yield chunk
    if total != size: raise DownloadError("partial_transfer",retryable=True) from None


@contextmanager
def open_asset_stream(repository, asset, *, policy=None, session=None, cancel=None,
                      sleep=time.sleep, clock=time.monotonic, resolver=socket.getaddrinfo):
    """Yield checked chunks after headers; always close response and session.

    Injected sessions must be DirectSession and are consumed/closed by this call.
    Network retries apply only before body consumption; rate limits return guidance.
    """
    policy = PRODUCTION_POLICY if policy is None else policy
    if not isinstance(policy,TransferPolicy): raise TypeError("Expected TransferPolicy.")
    if not isinstance(repository,RepositoryTarget) or not isinstance(asset,ReleaseAsset):
        raise TypeError("Expected repository and asset metadata.")
    invalid = False
    try:
        if (parse_github_url(f"https://github.com/{repository.owner}/{repository.repo}") != repository
                or type(asset.id) is not int or not 1 <= asset.id <= 2**63-1
                or type(asset.size) is not int or not 0 <= asset.size <= 2**53-1):
            raise ValueError
    except (ValueError,TypeError,UnicodeError): invalid = True
    if invalid: raise DownloadError("invalid_asset_identity") from None
    if session is not None and not isinstance(session,DirectSession):
        raise TypeError("Expected DirectSession.")
    session = DirectSession() if session is None else session
    response = None
    try:
        session.trust_env, session.verify, session.auth = False, True, _anonymous
        session.proxies.clear(); session.params.clear(); session.cookies.clear(); session.hooks.clear()
        session.headers.clear(); session.cert = None
        headers = {"Accept":"application/octet-stream", "Accept-Encoding":"identity",
            "User-Agent":f"CoveArchive/{__version__}", "X-GitHub-Api-Version":API_VERSION}
        entry = (policy._test_origin or "https://api.github.com") + f"/repos/{repository.owner}/{repository.repo}/releases/assets/{asset.id}"
        url, visited, hops, retries, refreshed = entry, {entry}, 0, 0, False
        deadline = clock()+policy.deadline_seconds
        while True:
            _check(cancel,clock,deadline)
            validate_hop(url,policy=policy,resolver=resolver)
            failure = None
            try:
                remaining = deadline-clock()
                if remaining <= 0: raise DownloadError("timeout",retryable=True)
                with _quiet_transport():
                    response = session.get(url,headers=headers.copy(),allow_redirects=False,stream=True,
                        verify=True,proxies={},auth=_anonymous,timeout=tuple(min(v,remaining) for v in policy.timeout))
            except requests.exceptions.SSLError: failure = ("tls_error",False,False)
            except (requests.Timeout,requests.ConnectionError) as exc:
                failure = ("timeout" if isinstance(exc,requests.Timeout) else "network",True,True)
            except requests.RequestException: failure = ("network",True,False)
            if failure is not None:
                code,retryable,retry = failure
                if not retry or retries >= 2: raise DownloadError(code,retryable=retryable) from None
                _pause(2**retries,policy,cancel,sleep,clock,deadline); retries += 1; continue
            _check(cancel,clock,deadline); _peer(response,policy)
            status, values = response.status_code, response.headers
            if status in (301,302,303,307,308):
                location = values.get("Location")
                if not _safe_text(location): raise DownloadError("unsafe_redirect") from None
                invalid = False
                try:
                    moved = urljoin(url,location)
                    validate_hop(moved,policy=policy,resolver=resolver)
                except (ValueError,TypeError,UnicodeError,OverflowError): invalid = True
                if invalid: raise DownloadError("unsafe_redirect") from None
                if (moved in visited or hops >= policy.max_redirects
                        or (not policy._test_origin and urlsplit(moved).hostname == "api.github.com")):
                    raise DownloadError("unsafe_redirect") from None
                response.close(); response=None
                url=moved; visited.add(url); hops += 1; continue
            rate = status == 429 or (status == 403 and ("Retry-After" in values or values.get("X-RateLimit-Remaining") == "0"))
            if rate:
                raw = values.get("Retry-After","")
                wait = min(86400,int(raw)) if re.fullmatch(r"[0-9]{1,15}",raw) else 60
                raise DownloadError("rate_limited",retry_after_seconds=max(1,wait),retryable=True) from None
            if url != entry and status in (400,403,410):
                if refreshed: raise DownloadError("expired_url",retryable=True) from None
                response.close(); response=None
                url,visited,hops,refreshed = entry,{entry},0,True
                continue
            if status in (500,502,503,504):
                response.close(); response=None
                if retries >= 2: raise DownloadError("server_error",retryable=True) from None
                _check(cancel,clock,deadline); _pause(2**retries,policy,cancel,sleep,clock,deadline); retries += 1; continue
            if status == 404: raise DownloadError("upstream_removed" if url == entry else "not_found") from None
            if status == 403: raise DownloadError("forbidden") from None
            if status != 200 or "Content-Range" in values:
                raise DownloadError("unexpected_status") from None
            if values.get("Content-Encoding","identity").lower() != "identity":
                raise DownloadError("unexpected_encoding") from None
            if values.get("Content-Type","").split(";",1)[0].lower() == "text/html":
                raise DownloadError("unexpected_content") from None
            length = values.get("Content-Length")
            if length is not None and (not re.fullmatch(r"[0-9]{1,20}",length) or int(length) != asset.size):
                raise DownloadError("size_mismatch") from None
            yield _chunks(response,asset.size,cancel,clock,deadline)
            break
    finally:
        if response is not None: response.close()
        session.close()
