"""GitHub release discovery interfaces and typed failures."""

import json
import math
import re
import time
from functools import wraps
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

import requests

from covearchive import __version__
from covearchive.jsondepth import check_json_depth

from .token import RateLimit, valid_token
from .models import ReleaseCatalog, ReleaseSchemaError, parse_release
from .urls import RepositoryTarget, ReleaseListTarget, ReleaseTagTarget, parse_github_url

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
DEFAULT_TIMEOUT = (10.0, 30.0)
DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class DirectSession(requests.Session):
    """Leave redirect validation and following exclusively to the client.

    Injected transports retain caller-owned verify/params/cert/proxies settings.
    Read timeouts apply per socket operation, not to the entire scan.
    """

    def __init__(self):
        super().__init__()
        self.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))

    def resolve_redirects(self, resp, req, **kwargs):
        return iter(())

    def prepare_request(self, request):
        """Keep response hooks from consuming unvalidated response bodies."""
        prepared = super().prepare_request(request)
        prepared.hooks = {"response": []}
        return prepared


class GitHubApiError(Exception):
    """A discovery failure with a stable code and optional HTTP status."""

    code = "github_api_error"
    status = None

    def __init__(self, message="GitHub API request failed.", *, status=None, code=None):
        super().__init__(message)
        self.status = type(self).status if status is None else status
        self.code = type(self).code if code is None else code


class InvalidTargetError(GitHubApiError):
    code = "invalid_target"


class CredentialedSessionError(GitHubApiError):
    code = "credentialed_session"


class NotFoundError(GitHubApiError):
    code, status = "not_found", 404


class AccessDeniedError(GitHubApiError):
    def __init__(self, status=403, *, token_rejected=False):
        super().__init__(status=status, code="unauthorized" if status == 401 else "forbidden")
        self.token_rejected = status == 401 and token_rejected


class RateLimitError(GitHubApiError):
    code = "rate_limited"

    def __init__(self, *, kind, status, retry_after_seconds, retry_at, reset_at=None,
                 remaining=None):
        super().__init__(status=status)
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds
        self.retry_at = retry_at
        self.reset_at = reset_at
        self.remaining = remaining


class TransientServerError(GitHubApiError):
    code = "server_error"


class RequestTimeoutError(GitHubApiError):
    code = "timeout"


class NetworkError(GitHubApiError):
    code = "network"


class InvalidResponseError(GitHubApiError):
    def __init__(self, code="invalid_json", *, status=200):
        super().__init__(status=status, code=code)


class PaginationError(GitHubApiError):
    code = "bad_pagination"


class UnsafeRedirectError(GitHubApiError):
    code = "unsafe_redirect"


class ApiGoneError(GitHubApiError):
    code, status = "gone", 410


class UnexpectedStatusError(GitHubApiError):
    code = "unexpected_status"


class IncompleteScanError(GitHubApiError):
    code = "incomplete_scan"

    def __init__(self, pages_completed, reason, cause=None):
        super().__init__()
        self.pages_completed, self.reason, self.cause = pages_completed, reason, cause
        self.__cause__ = cause


def _anonymous(request):
    return request


def _is_timeout(exc):
    return isinstance(exc, requests.Timeout) or (
        isinstance(exc, requests.ConnectionError) and bool(exc.args)
        and isinstance(exc.args[0], requests.packages.urllib3.exceptions.ReadTimeoutError))


def _integer(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,15}", value):
        try:
            return int(value)
        except ValueError:
            pass
    return None


def is_public_repository(data):
    return isinstance(data, dict) and data.get('private') is False and data.get('visibility') == 'public'


def observe_rate(auth, headers, token_sent, clock=time.time):
    """Observe only complete integer GitHub rate headers; never disrupt I/O."""
    if auth is None: return
    try:
        values = [_integer(headers.get("X-RateLimit-" + name))
                  for name in ("Limit", "Remaining", "Reset")]
        if all(value is not None for value in values):
            auth.observe(RateLimit(*values, token_sent is not None, clock()), token_sent)
    except Exception: pass


def _rate_signal(headers):
    """Return validated metadata and shared primary/decisive signal flags."""
    remaining = _integer(headers.get("X-RateLimit-Remaining"))
    reset = _integer(headers.get("X-RateLimit-Reset"))
    retry = _integer(headers.get("Retry-After"))
    primary = remaining == 0 and (reset is not None or retry is not None)
    decisive = retry is not None or primary
    return remaining, reset, retry, primary, decisive


def _typed_url(error):
    def decorate(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
                raise error() from exc
        return guarded
    return decorate


def _api_url(url, error):
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.netloc.lower() != "api.github.com"
                or "#" in url or "\\" in url
                or any(ord(char) <= 32 or ord(char) == 127 for char in url)):
            raise ValueError("Unsafe API URL.")
        return parts
    except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
        raise error() from exc


@_typed_url(PaginationError)
def _link_targets(header, relation="next"):
    if not isinstance(header, str) or len(header.encode("utf-8")) > 8192 or not header:
        raise PaginationError()
    pieces, start, angle, quoted, escaped = [], 0, False, False, False
    for index, char in enumerate(header):
        if char in "\r\n":
            raise PaginationError()
        if escaped:
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"' and not angle:
            quoted = not quoted
        elif char == "<" and not quoted:
            if angle:
                raise PaginationError()
            angle = True
        elif char == ">" and not quoted:
            if not angle:
                raise PaginationError()
            angle = False
        elif char == "," and not angle and not quoted:
            pieces.append(header[start:index])
            start = index + 1
    if angle or quoted or escaped:
        raise PaginationError()
    pieces.append(header[start:])
    targets = []
    for piece in pieces:
        match = re.fullmatch(r"\s*<([^<>\s]+)>\s*(.*)", piece)
        if match is None:
            raise PaginationError()
        url, params = match.groups()
        while params:
            param = re.match(r';\s*([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?:"([^"\\]*(?:\\.[^"\\]*)*)"|([^\s;,"<>]+))\s*', params)
            if param is None:
                raise PaginationError()
            name, quoted_value, token = param.groups()
            if name.lower() == "rel" and relation in (quoted_value or token or "").split():
                targets.append(url)
            params = params[param.end():]
    if len(set(targets)) > 1:
        raise PaginationError()
    return targets[0] if targets else None


@_typed_url(PaginationError)
def _next_url(header, current):
    if header is None:
        return None
    url = _link_targets(header)
    if url is None:
        return None
    parts = _api_url(url, PaginationError)
    previous = urlsplit(current)
    if parts.path != previous.path and not (
            re.fullmatch(r"/repos/[^/]+/[^/]+/releases", previous.path)
            and re.fullmatch(r"/repositories/[0-9]{1,20}/releases", parts.path)):
        raise PaginationError()
    try:
        if "%" in parts.query or "+" in parts.query:
            raise ValueError("Encoded query is not allowed.")
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        if len(pairs) != len(query) or not set(query) <= {"per_page", "page"}:
            raise ValueError("Unexpected query.")
        page = _integer(query.get("page"))
        per_page = _integer(query.get("per_page", "100"))
        old_page = int(dict(parse_qsl(previous.query)).get("page", "1"))
        if page is None or page <= old_page or per_page is None or not 1 <= per_page <= 100:
            raise ValueError("Invalid page.")
    except ValueError as exc:
        raise PaginationError() from exc
    canonical = (f"per_page={per_page}&" if "per_page" in query else "") + f"page={page}"
    return urlunsplit(("https", "api.github.com", parts.path, canonical, ""))


@_typed_url(UnsafeRedirectError)
def _redirect_url(location, current):
    if (not isinstance(location, str) or not location or len(location) > 2048
            or "#" in location or "\\" in location
            or any(ord(char) <= 32 or ord(char) == 127 for char in location)):
        raise UnsafeRedirectError()
    url = urljoin(current, location)
    parts = _api_url(url, UnsafeRedirectError)
    previous = urlsplit(current)
    if parts.query and parts.query != previous.query:
        raise UnsafeRedirectError()
    if re.fullmatch(r"/repositories/[0-9]{1,20}/releases", parts.path) is None:
        match = re.fullmatch(r"/repos/([^/]+)/([^/]+)/releases", parts.path)
        if match is None:
            raise UnsafeRedirectError()
        owner, repo = match.groups()
        try:
            if parse_github_url(f"https://github.com/{owner}/{repo}") != RepositoryTarget(owner, repo):
                raise ValueError("Invalid repository.")
        except (ValueError, TypeError) as exc:
            raise UnsafeRedirectError() from exc
    return urlunsplit(("https", "api.github.com", parts.path, previous.query, ""))


def _json(body):
    def invalid_constant(value):
        raise ValueError("Non-finite JSON constant.")
    text = body.decode("utf-8")
    check_json_depth(text)
    return json.loads(text, parse_constant=invalid_constant)


class GitHubReleaseClient:
    """Discover releases with injectable transport and bounded request budgets.

    An injected DirectSession is the caller's transport: verify/params/cert/proxies
    remain caller-owned. Read timeouts are per socket operation, not a scan deadline.
    """

    def __init__(self, session=None, *, clock=time.time, sleep=time.sleep,
                 timeout=DEFAULT_TIMEOUT, max_pages=100, max_requests=150,
                 max_retries=2, max_redirects=3, max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES, auth=None):
        if isinstance(session, requests.Session) and not isinstance(session, DirectSession):
            raise TypeError("Pass DirectSession instead of a stock requests.Session.")
        self._owns_session, self._closed = session is None, False
        self.session = session if session is not None else DirectSession()
        self.clock, self.sleep, self.timeout = clock, sleep, timeout
        self.max_pages, self.max_requests = max_pages, max_requests
        self.max_retries, self.max_redirects = max_retries, max_redirects
        self.max_response_bytes = max_response_bytes
        self.auth = auth if type(self) is __class__ else None

    def close(self):
        """Close an owned session; leave injected sessions with their caller."""
        if self._owns_session and not self._closed:
            self.session.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def discover_releases(self, target):
        """Return release discovery metadata for a supported target."""
        if not isinstance(target, (RepositoryTarget, ReleaseListTarget, ReleaseTagTarget)):
            raise InvalidTargetError()
        repository = RepositoryTarget(target.owner, target.repo)
        try:
            if parse_github_url(f"https://github.com/{target.owner}/{target.repo}") != repository:
                raise ValueError("Non-canonical target.")
        except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
            raise InvalidTargetError() from exc
        state = dict(pages=0, requests=0, redirects=0, public_token=None)
        url = f"{API_ROOT}/repos/{repository.owner}/{repository.repo}/releases?per_page=100"
        if self.auth is not None and valid_token(self.auth.token()):
            token, public = self.auth.token(), False
            state["public_token"] = token
            probe = f"{API_ROOT}/repos/{repository.owner}/{repository.repo}"
            try:
                final, _, body = self._page(probe, state)
                data = _json(body)
                public = (final == probe and is_public_repository(data) and self.auth.token() == token
                          and getattr(self.session, 'verify', True) not in (False, None))
            except (GitHubApiError, ValueError, TypeError, UnicodeError, OverflowError, RecursionError): pass
            state['public_token'] = token if public else None
        releases, seen, duplicates, visited = [], set(), [], set()
        while True:
            if state["pages"] >= self.max_pages:
                raise IncompleteScanError(state["pages"], "page_limit")
            try:
                final_url, headers, body = self._page(url, state)
                parsed = self._releases(body)
                next_url = _next_url(headers.get("Link"), final_url)
                if next_url in visited:
                    raise PaginationError()
            except GitHubApiError as exc:
                if state["pages"] and not isinstance(exc, IncompleteScanError):
                    raise IncompleteScanError(state["pages"], "error", exc) from exc
                raise
            state["pages"] += 1
            visited.update((url, final_url))
            for release in parsed:
                if release.id in seen:
                    if release.id not in duplicates:
                        duplicates.append(release.id)
                else:
                    seen.add(release.id)
                    releases.append(release)
            if next_url is None:
                return ReleaseCatalog(repository, tuple(releases), state["pages"],
                    state["requests"], state["redirects"], tuple(duplicates),
                    urlsplit(final_url).path, True)
            url = next_url

    def _budget(self, state):
        if state["requests"] >= self.max_requests:
            raise IncompleteScanError(state["pages"], "request_limit")

    def _backoff(self, retries, state):
        self._budget(state)
        self.sleep(min(2 ** min(retries, 2), 4))

    def _request_headers(self):
        return {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
                   "User-Agent": f"CoveArchive/{__version__}", "Authorization": None, "Cookie": None,
                   "Proxy-Authorization": None}

    _redirect_url = staticmethod(_redirect_url)

    def _validate_transport(self, url, response=None):
        pass

    def _page(self, url, state):
        redirects, retries, visited = 0, 0, {url}
        headers = self._request_headers()
        while True:
            if getattr(self.session, "cookies", ()):
                raise CredentialedSessionError()
            self._budget(state)
            state["requests"] += 1
            response = None
            retry_transport = False
            try:
                self._validate_transport(url)
                sent_token = None
                def request_auth(request):
                    nonlocal sent_token
                    for name in ("Authorization", "Cookie", "Proxy-Authorization"):
                        request.headers.pop(name, None)
                    parts = urlsplit(request.url)
                    if (self.auth is not None and redirects == 0
                            and getattr(self.session, 'verify', True) not in (False, None)
                            and parts.scheme == "https" and parts.netloc == "api.github.com"
                            and urlsplit(url).scheme == "https" and urlsplit(url).netloc == "api.github.com"):
                        candidate = self.auth.token()
                        if valid_token(candidate) and candidate == state.get("public_token"):
                            sent_token = candidate
                            request.headers["Authorization"] = "Bearer " + candidate
                    return request
                response = self.session.get(url, headers=headers.copy(), timeout=self.timeout,
                    allow_redirects=False, stream=True, auth=request_auth if self.auth is not None else _anonymous)
                self._validate_transport(url, response)
                status, response_headers = response.status_code, response.headers
                if self.auth is not None:
                    observe_rate(self.auth, response_headers, sent_token, self.clock)
                    if status == 401 and sent_token is not None:
                        try: self.auth.reject(sent_token)
                        except Exception: pass
                body = b""
                decisive = _rate_signal(response_headers)[-1]
                if status == 200 or (status == 403 and not decisive):
                    chunks, size = [], 0
                    for chunk in response.iter_content(chunk_size=65536):
                        size += len(chunk)
                        if size > self.max_response_bytes:
                            raise InvalidResponseError("response_too_large", status=status)
                        chunks.append(chunk)
                    body = b"".join(chunks)
            except (requests.RequestException, requests.packages.urllib3.exceptions.HTTPError) as exc:
                if response is not None and response.status_code == 403:
                    body = b""
                else:
                    failure = RequestTimeoutError() if _is_timeout(exc) else NetworkError()
                    if (isinstance(exc, (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError))
                            and not isinstance(exc, requests.exceptions.SSLError) and retries < self.max_retries):
                        retry_transport = True
                    else:
                        raise failure from exc
            finally:
                if response is not None:
                    response.close()
            if retry_transport:
                self._backoff(retries, state)
                retries += 1
                continue
            if status in (301, 302, 307, 308):
                if redirects >= self.max_redirects:
                    raise UnsafeRedirectError(status=status)
                moved = self._redirect_url(response_headers.get("Location"), url)
                if moved in visited:
                    raise UnsafeRedirectError(status=status)
                visited.add(moved)
                redirects += 1
                state["redirects"] += 1
                url = moved
                continue
            if status in (500, 502, 503, 504) and retries < self.max_retries:
                self._backoff(retries, state)
                retries += 1
                continue
            if status == 200:
                return url, response_headers, body
            if status in (403, 429):
                rate_limit = self._rate_limit(status, response_headers, body)
                if rate_limit is not None:
                    raise rate_limit
            if status in (401, 403):
                raise AccessDeniedError(status, token_rejected=sent_token is not None)
            if status == 404:
                raise NotFoundError()
            if status == 410:
                raise ApiGoneError()
            if 500 <= status <= 599:
                raise TransientServerError(status=status)
            raise UnexpectedStatusError(status=status)

    def _rate_limit(self, status, headers, body):
        remaining, reset, retry, primary, decisive = _rate_signal(headers)
        kind = "primary" if primary else "secondary"
        if not decisive:
            if status == 429:
                kind = "too_many_requests"
            else:
                try:
                    obj = _json(body)
                    message = obj.get("message", "") if isinstance(obj, dict) else ""
                except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
                    message = ""
                if not isinstance(message, str) or not any(term in message.lower() for term in ("rate limit", "abuse")):
                    return None
        now = math.floor(self.clock())
        waits = ([retry] if retry is not None else [])
        if primary and reset is not None:
            waits.append(reset - now)
        wait = max(1, min(86400, max(waits) if waits else 60))
        return RateLimitError(kind=kind, status=status, retry_after_seconds=wait,
                             retry_at=int(now) + wait, reset_at=reset, remaining=remaining)

    def _releases(self, body):
        try:
            obj = _json(body)
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
            raise InvalidResponseError("invalid_json") from exc
        if not isinstance(obj, list):
            raise InvalidResponseError("invalid_root")
        if len(obj) > 100:
            raise InvalidResponseError("too_many_items")
        try:
            return tuple(parse_release(item) for item in obj)
        except ReleaseSchemaError as exc:
            raise InvalidResponseError("invalid_release") from exc


class GitHubRepositoryClient(GitHubReleaseClient):
    """Anonymous bounded repository metadata, using the hardened release transport.

    Redirects route requests only. The caller must compare numeric identities.
    A shared budget bounds both lookups (saved URL and stable id).
    """
    def __init__(self, session=None):
        super().__init__(session, timeout=(5.0, 10.0), max_requests=8,
                         max_retries=0, max_redirects=3, max_response_bytes=256 * 1024)
        self._state = dict(pages=0, requests=0, redirects=0)

    @staticmethod
    def _metadata_path(url, error):
        from covearchive.providers import canonical_url, validate_repository
        parts = _api_url(url, error)
        if '?' in url: raise error()
        match = re.fullmatch(r'/repositories/([1-9][0-9]{0,18})', parts.path)
        if match:
            if int(match[1]) > 2**63 - 1: raise error()
            return parts.path
        match = re.fullmatch(r'/repos/([^/]+)/([^/]+)', parts.path)
        if match:
            try:
                target = validate_repository(RepositoryTarget(*match.groups()))
                if canonical_url(target) == 'https://github.com/' + '/'.join(match.groups()):
                    return parts.path
            except (ValueError, TypeError): pass
        raise error()

    def _validate_transport(self, url, response=None):
        path = self._metadata_path(url, InvalidTargetError)
        verify = getattr(self.session, 'verify', True)
        if not (verify is True or (isinstance(verify, str) and bool(verify))):
            raise InvalidTargetError()
        if response is None:
            headers = getattr(self.session, 'headers', {})
            if (getattr(self.session, 'params', None) or getattr(self.session, 'cert', None) is not None
                    or getattr(self.session, 'auth', None) is not None
                    or any(value is not None and name.lower() in ('authorization', 'cookie', 'proxy-authorization')
                           for name, value in headers.items())):
                raise CredentialedSessionError()
        if response is not None:
            effective_url = getattr(response, 'url', url)
            if self._metadata_path(effective_url, UnsafeRedirectError) != path:
                raise UnsafeRedirectError()

    def _redirect_url(self, location, current):
        if (type(location) is not str or not location or len(location) > 2048
                or '?' in location or '#' in location or '\\' in location
                or any(ord(c) <= 32 or ord(c) >= 127 for c in location)):
            raise UnsafeRedirectError()
        url = urljoin(current, location)
        self._metadata_path(url, UnsafeRedirectError)
        return url

    def repository(self, target=None, *, repository_id=None):
        from covearchive.providers import canonical_url, validate_repository
        if repository_id is not None:
            if type(repository_id) is not int or not 1 <= repository_id <= 2**63 - 1:
                raise InvalidTargetError()
            path = '/repositories/' + str(repository_id)
        else:
            target = validate_repository(target)
            if target.provider != 'github': raise InvalidTargetError()
            path = '/repos/' + target.owner + '/' + target.repo
        final, _, body = self._page(API_ROOT + path, self._state)
        try:
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result: raise ValueError('duplicate_metadata')
                    result[key] = value
                return result
            text = body.decode('utf-8'); check_json_depth(text)
            obj = json.loads(text, object_pairs_hook=pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not is_public_repository(obj):
                raise ValueError()
            rid, name, owner = obj['id'], obj['name'], obj['owner']['login']
            if type(rid) is not int or not 1 <= rid <= 2**63 - 1: raise ValueError()
            canonical = validate_repository(RepositoryTarget(owner, name))
            if (obj['full_name'] != owner + '/' + name or obj['html_url'] != canonical_url(canonical)
                    or (repository_id is not None and repository_id != rid)):
                raise ValueError()
            final_path = urlsplit(final).path
            if final_path.startswith('/repositories/'):
                if final_path != '/repositories/' + str(rid): raise ValueError()
            elif final_path.casefold() != ('/repos/' + owner + '/' + name).casefold():
                raise ValueError()
            return rid, canonical
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError) as exc:
            raise InvalidResponseError('invalid_repository') from exc
