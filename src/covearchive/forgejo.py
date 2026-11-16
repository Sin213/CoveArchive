"""Bounded anonymous Codeberg discovery, sharing the GitHub transport hardening.

Forgejo supplies no attachment updated_at: the adapter uses created_at. Missing
sizes become zero-sized external entries, never eligible uploaded attachments.
Injected DirectSessions have TLS verification and direct, anonymous settings
enforced; socket timeouts bound stalls, while the scan worker owns the deadline.
"""

import re
import socket
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests

from covearchive import __version__
from covearchive.github.client import (
    GitHubReleaseClient, GitHubApiError, InvalidTargetError, InvalidResponseError,
    IncompleteScanError, PaginationError, UnsafeRedirectError, _json, _integer,
    _link_targets, NetworkError,
)
from covearchive.github.models import ReleaseCatalog, ReleaseSchemaError, parse_release
from covearchive.github.urls import RepositoryTarget, ReleaseListTarget
from covearchive.providers import validate_repository
from covearchive.transfer.resolve import validate_hop, _peer, PRODUCTION_POLICY, DownloadError


API_ROOT = "https://codeberg.org/api/v1"


def _parts(url, error):
    try:
        if (not isinstance(url, str) or not 0 < len(url) <= 2048 or not url.isascii()
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in "\\#" for c in url)):
            raise ValueError
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "codeberg.org":
            raise ValueError
        return parts
    except (ValueError, TypeError, UnicodeError) as exc:
        raise error() from exc


def _repository(path):
    match = re.fullmatch(r"/api/v1/repos/([^/]+)/([^/]+)/releases", path)
    if match is None:
        raise ValueError
    return validate_repository(RepositoryTarget(*match.groups(), "codeberg", "codeberg.org"))


def _page_number(url, path):
    parts = _parts(url, PaginationError)
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        if (_repository(parts.path).repo_key != _repository(path).repo_key
                or len(pairs) != len(query) or set(query) != {"limit", "page"}
                or "%" in parts.query or "+" in parts.query
                or _integer(query["limit"]) != 50):
            raise ValueError
        number = _integer(query["page"])
        if number is None or number < 1:
            raise ValueError
        return number
    except (ValueError, TypeError) as exc:
        raise PaginationError() from exc


def _pagination(header, current):
    parts = _parts(current, PaginationError)
    number = _page_number(current, parts.path)
    if header is None:
        return None
    links = {rel: _link_targets(header, rel) for rel in ("next", "last", "prev", "first")}
    pages = {rel: _page_number(url, parts.path) for rel, url in links.items() if url is not None}
    if ("first" in pages and pages["first"] != 1
            or "prev" in pages and pages["prev"] != number - 1
            or "next" in pages and pages["next"] != number + 1
            or "last" in pages and (pages["last"] < number
                or pages.get("next", number) > pages["last"]
                or pages["last"] > number and "next" not in pages)):
        raise PaginationError()
    return (f"https://codeberg.org{parts.path}?limit=50&page={pages['next']}"
            if "next" in pages else None)


class ForgejoReleaseClient(GitHubReleaseClient):
    """Discover only codeberg.org; reuse bounded streaming, retries and errors."""

    def __init__(self, session=None, *, resolver=None, **options):
        super().__init__(session, **options)
        self.resolver = socket.getaddrinfo if resolver is None else resolver

    def _validate_transport(self, url, response=None):
        try:
            if response is None: validate_hop(url, provider='codeberg', entry=True, resolver=self.resolver)
            else: _peer(response, PRODUCTION_POLICY)
        except DownloadError as exc:
            raise (NetworkError() if exc.code == 'network' else UnsafeRedirectError()) from None

    def _request_headers(self):
        return {"Accept": "application/json", "User-Agent": f"CoveArchive/{__version__}",
                "Authorization": None, "Cookie": None, "Proxy-Authorization": None, "Host": None}

    @staticmethod
    def _redirect_url(location, current):
        try:
            _parts(current, UnsafeRedirectError)
            if (not isinstance(location, str) or not location or len(location) > 2048
                    or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in "\\#" for c in location)):
                raise ValueError
            parts = _parts(urljoin(current, location), UnsafeRedirectError)
            previous = urlsplit(current)
            if (_repository(parts.path).repo_key != _repository(previous.path).repo_key
                    or parts.query and parts.query != previous.query):
                raise ValueError
            return f"https://codeberg.org{parts.path}?{previous.query}"
        except (ValueError, TypeError) as exc:
            raise UnsafeRedirectError() from exc

    def _page(self, url, state):
        _parts(url, UnsafeRedirectError)
        if isinstance(self.session, requests.Session):
            self.session.trust_env, self.session.verify, self.session.cert = False, True, None
            self.session.params.clear()
            self.session.proxies.clear()
        final, headers, body = super()._page(url, state)
        content_type = headers.get("Content-Type", "")
        if not isinstance(content_type, str) or content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise InvalidResponseError("invalid_content_type")
        return final, headers, body

    def _release(self, obj, repository):
        if not isinstance(obj, dict) or not isinstance(obj.get("assets"), list):
            raise InvalidResponseError("invalid_release")
        try:
            parts = _parts(obj.get("url"), InvalidResponseError)
            match = re.fullmatch(r"(.*/releases)/([0-9]{1,20})", parts.path)
            if match is None or parts.query or type(obj.get("id")) is not int or obj["id"] < 1:
                raise ValueError
            canonical = _repository(match[1])
            if int(match[2]) != obj["id"] or canonical.repo_key != repository.repo_key:
                raise ValueError
            assets = []
            for item in obj["assets"]:
                if not isinstance(item, dict) or item.get("type") not in ("attachment", "external"):
                    raise ValueError
                size = item.get("size")
                if size is not None and (type(size) is not int or size < 0):
                    raise ValueError
                assets.append(dict(id=item.get("id"), name=item.get("name"),
                    size=0 if size is None else size,
                    state="uploaded" if item["type"] == "attachment" and size is not None else "external",
                    content_type="", browser_download_url="", digest=None,
                    created_at=item.get("created_at"), updated_at=item.get("created_at")))
            mapped = {key: obj.get(key) for key in
                      ("id", "tag_name", "name", "draft", "prerelease", "created_at", "published_at")}
            mapped.update(assets=assets, body=None, tarball_url=None, zipball_url=None)
            return canonical, parse_release(mapped)
        except (ValueError, TypeError, ReleaseSchemaError) as exc:
            raise InvalidResponseError("invalid_release") from exc

    def discover_releases(self, target):
        try:
            if not isinstance(target, (RepositoryTarget, ReleaseListTarget)):
                raise ValueError
            repository = validate_repository(RepositoryTarget(target.owner, target.repo, target.provider, target.host))
            if repository.provider != "codeberg":
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise InvalidTargetError() from exc
        state = dict(pages=0, requests=0, redirects=0)
        url = f"{API_ROOT}/repos/{repository.owner}/{repository.repo}/releases?limit=50&page=1"
        releases, seen, duplicates, asset_ids = [], {}, [], set()
        total, canonical = None, None
        while True:
            if state["pages"] >= self.max_pages:
                raise IncompleteScanError(state["pages"], "page_limit")
            try:
                final, headers, body = self._page(url, state)
                count = _integer(headers.get("X-Total-Count"))
                if count is None or total is not None and total != count:
                    raise PaginationError()
                total = count
                try:
                    raw = _json(body)
                except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
                    raise InvalidResponseError("invalid_json") from exc
                if not isinstance(raw, list) or len(raw) > 50:
                    raise InvalidResponseError("invalid_root")
                next_url = _pagination(headers.get("Link"), final)
                before = len(seen)
                for obj in raw:
                    spelling, release = self._release(obj, repository)
                    if canonical is not None and spelling != canonical:
                        raise InvalidResponseError("invalid_release")
                    canonical = spelling
                    if release.id in seen:
                        if seen[release.id] != obj:
                            raise PaginationError()
                        if release.id not in duplicates:
                            duplicates.append(release.id)
                        continue
                    ids = [item.id for item in release.assets]
                    if len(set(ids)) != len(ids) or asset_ids.intersection(ids):
                        raise PaginationError()
                    asset_ids.update(ids)
                    seen[release.id] = obj
                    if not release.draft:
                        releases.append(release)
                if len(seen) > total or next_url is not None and len(seen) == before:
                    raise PaginationError()
                state["pages"] += 1
                if next_url is None:
                    if len(seen) != total:
                        raise IncompleteScanError(state["pages"], "total_count_mismatch")
                    return ReleaseCatalog(canonical or repository, tuple(releases), state["pages"],
                        state["requests"], state["redirects"], tuple(duplicates), urlsplit(final).path, True)
                url = next_url
            except GitHubApiError as exc:
                if state["pages"] and not isinstance(exc, IncompleteScanError):
                    raise IncompleteScanError(state["pages"], "error", exc) from exc
                raise
