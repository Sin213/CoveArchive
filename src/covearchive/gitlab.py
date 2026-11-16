"""Bounded anonymous GitLab.com releases and first-party generic packages."""
import re
import socket
from dataclasses import replace
from urllib.parse import quote, unquote_to_bytes, urlencode
import requests
from covearchive import __version__
from covearchive.github.client import (GitHubReleaseClient, GitHubApiError, InvalidTargetError,
    InvalidResponseError, IncompleteScanError, PaginationError, UnsafeRedirectError, NetworkError, NotFoundError, _json)
from covearchive.github.models import (Release, ReleaseAsset, ReleaseCatalog, ReleaseSchemaError,
    release_key_ok, _date)
from covearchive.github.urls import RepositoryTarget, ReleaseListTarget
from covearchive.providers import validate_repository
from covearchive.formats import match_extension
from covearchive.transfer.resolve import validate_hop, _peer, PRODUCTION_POLICY, DownloadError

API_ROOT = "https://gitlab.com/api/v4/projects/"
_PACKAGE = re.compile(r"https://gitlab[.]com/api/v4/projects/([^/?#\\\s]+)/packages/generic/([^/?#\\\s]+)/([^/?#\\\s]+)/([^/?#\\\s]+)")
_SEGMENT = re.compile(r"[A-Za-z0-9._-]{1,255}")


def _positive(value):
    return type(value) is int and 1 <= value <= 2**63 - 1


def _parts(url):
    if not isinstance(url, str) or not url.isascii():
        raise ValueError
    match = _PACKAGE.fullmatch(url)
    if match is None:
        raise ValueError
    return match.groups()


def _segment(raw):
    if re.fullmatch(r"(?:[A-Za-z0-9._-]|%[0-9A-Fa-f]{2})+", raw) is None:
        raise ValueError
    value = unquote_to_bytes(raw).decode("ascii")
    if _SEGMENT.fullmatch(value) is None or value in (".", ".."):
        raise ValueError
    return value


def _project(raw, project_id=None, path_with_namespace=None):
    if re.fullmatch(r"[0-9]{1,20}", raw):
        value = int(raw)
        if not _positive(value) or project_id is not None and value != project_id:
            raise ValueError
        return value
    if (project_id is None or not re.search(r"%2f", raw, re.I)
            or re.fullmatch(r"(?:[A-Za-z0-9._-]|%[0-9A-Fa-f]{2})+", raw) is None
            or unquote_to_bytes(raw).decode("ascii").casefold() != path_with_namespace.casefold()):
        raise ValueError
    return project_id


def parse_package_url(url, *, project_id=None, path_with_namespace=None):
    """Parse once; without a repository context only numeric project URLs qualify."""
    raw, package, version, filename = _parts(url)
    return (_project(raw, project_id, path_with_namespace),
            _segment(package), _segment(version), _segment(filename))


def build_package_url(project_id, package, version, filename):
    """Build the canonical numeric-id URL from decoded, validated components."""
    if not _positive(project_id) or any(not isinstance(s, str) or _SEGMENT.fullmatch(s) is None
            or s in (".", "..") for s in (package, version, filename)):
        raise ValueError
    return API_ROOT + str(project_id) + "/packages/generic/" + "/".join((package, version, filename))


class ReleaseNotFoundError(NotFoundError):
    code = "release_removed"


class GitLabReleaseClient(GitHubReleaseClient):
    def __init__(self, session=None, *, resolver=None, **options):
        options.setdefault("max_requests", 400)
        super().__init__(session, **options)
        self.resolver = socket.getaddrinfo if resolver is None else resolver

    def _request_headers(self):
        return {"Accept": "application/json", "User-Agent": f"CoveArchive/{__version__}",
                "Authorization": None, "Cookie": None, "Proxy-Authorization": None, "Host": None}

    @staticmethod
    def _redirect_url(location, current):
        raise UnsafeRedirectError()

    def _validate_transport(self, url, response=None):
        try:
            if response is None:
                if not url.startswith(API_ROOT):
                    raise UnsafeRedirectError()
                validate_hop(url, provider="gitlab", entry=True, resolver=self.resolver)
            else:
                _peer(response, PRODUCTION_POLICY)
                if 300 <= response.status_code < 400:
                    raise UnsafeRedirectError(status=response.status_code)
        except DownloadError as exc:
            raise (NetworkError() if exc.code == "network" else UnsafeRedirectError()) from None

    def _page(self, url, state):
        if isinstance(self.session, requests.Session):
            self.session.trust_env, self.session.verify, self.session.cert = False, True, None
            self.session.params.clear(); self.session.proxies.clear()
        final, headers, body = super()._page(url, state)
        kind = headers.get("Content-Type", "")
        if not isinstance(kind, str) or kind.split(";", 1)[0].strip().lower() != "application/json":
            raise InvalidResponseError("invalid_content_type")
        return final, headers, body

    def _get(self, url, state):
        _, headers, body = self._page(url, state)
        try:
            return headers, _json(body)
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
            raise InvalidResponseError() from exc

    def _listing(self, path, query, state):
        result, number, expected = [], 1, None
        while True:
            if number > self.max_pages:
                raise IncompleteScanError(state["pages"], "page_limit")
            headers, data = self._get(API_ROOT + path + "?" + urlencode((*query, ("per_page", 100), ("page", number))), state)
            if not isinstance(data, list) or len(data) > 100:
                raise InvalidResponseError("invalid_root")
            values = []
            for key in ("x-page", "x-total", "x-total-pages", "x-next-page"):
                value = headers.get(key)
                if key == "x-next-page" and value == "":
                    values.append(None); continue
                if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,20}", value) is None:
                    raise PaginationError()
                values.append(int(value))
            page, total, pages, next_page = values
            if (len(data) != min(100, total - (number - 1)*100)
                    or page != number or pages not in (max(1, (total + 99)//100), 0 if total == 0 else -1)
                    or expected is not None and expected != (total, pages)
                    or number > max(1, pages) or next_page != (number + 1 if number < pages else None)):
                raise PaginationError()
            expected = total, pages
            result.extend(data); state["pages"] += 1
            if len(result) > total or next_page is not None and not data:
                raise PaginationError()
            if next_page is None:
                if len(result) != total:
                    raise IncompleteScanError(state["pages"], "total_count_mismatch")
                return result
            number = next_page

    def _repository(self, target, state, *, numeric=False):
        try:
            if not isinstance(target, (RepositoryTarget, ReleaseListTarget)):
                raise ValueError
            repo = validate_repository(RepositoryTarget(target.owner, target.repo, target.provider, target.host, target.project_id))
            if repo.provider != "gitlab" or numeric and not _positive(repo.project_id):
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise InvalidTargetError() from exc
        path = repo.owner + "/" + repo.repo
        _, obj = self._get(API_ROOT + (str(repo.project_id) if numeric else quote(path, safe="")), state)
        if not isinstance(obj, dict) or not _positive(obj.get("id")) or obj.get("visibility") != "public":
            raise InvalidResponseError("invalid_project")
        canonical = obj.get("path_with_namespace")
        if not isinstance(canonical, str) or canonical.casefold() != path.casefold():
            raise InvalidResponseError("repository_mismatch")
        if repo.project_id is not None and obj["id"] != repo.project_id:
            raise GitHubApiError(code="repository_mismatch")
        owner, name = canonical.rsplit("/", 1)
        try:
            return validate_repository(replace(repo, owner=owner, repo=name, project_id=obj["id"]))
        except (ValueError, TypeError) as exc:
            raise InvalidResponseError("invalid_project") from exc

    def _asset(self, obj, repo, state, packages, files, created_at):
        if not isinstance(obj, dict) or not _positive(obj.get("id")) or not isinstance(obj.get("name"), str):
            raise InvalidResponseError("invalid_release")
        name, status, canonical, metadata = obj["name"], "external_link", "", None
        try:
            raw, _, _, _ = _parts(obj.get("url"))
            _project(raw, repo.project_id, repo.owner + "/" + repo.repo)
        except (ValueError, TypeError, UnicodeError):
            pass
        else:
            status = "unverifiable"
            try:
                parts = parse_package_url(obj["url"], project_id=repo.project_id, path_with_namespace=repo.owner + "/" + repo.repo)
                _, package, version, name = parts
                canonical = build_package_url(*parts)
            except (ValueError, TypeError, UnicodeError):
                pass
            else:
                candidates = packages(package, version)
                candidates = [p for p in candidates if isinstance(p, dict) and p.get("name") == package
                    and p.get("version") == version and p.get("status") == "default" and p.get("package_type") == "generic"]
                if len(candidates) == 1 and _positive(candidates[0].get("id")):
                    pid = candidates[0]["id"]
                    if pid not in files:
                        files[pid] = self._listing(f"{repo.project_id}/packages/{pid}/package_files", (), state)
                    matches = [f for f in files[pid] if isinstance(f, dict) and f.get("file_name") == name]
                    if len(matches) == 1:
                        f = matches[0]
                        if (_positive(f.get("id")) and f.get("package_id") == pid
                                and type(f.get("size")) is int and 0 <= f["size"] <= 2**53 - 1
                                and isinstance(f.get("file_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", f["file_sha256"])):
                            try:
                                _date(f, "created_at")
                                metadata, status = f, "uploaded"
                            except ReleaseSchemaError:
                                pass
        digest = metadata["file_sha256"] if metadata else None
        date = metadata["created_at"] if metadata else created_at
        return ReleaseAsset(obj["id"], name, metadata["size"] if metadata else 0, status, "", canonical,
            "sha256:" + digest if digest else None, "sha256" if digest else None, digest, date, date,
            match_extension(name), metadata["id"] if metadata else None)

    def _release(self, obj, repo, state, packages, files):
        try:
            if not isinstance(obj, dict) or not release_key_ok("gitlab", obj.get("tag_name")):
                raise ValueError
            tag = obj["tag_name"]
            if type(obj.get("upcoming_release")) is not bool or obj.get("name") is not None and not isinstance(obj["name"], str):
                raise ValueError
            created, published = _date(obj, "created_at"), _date(obj, "released_at", optional=True)
            links = obj["assets"]["links"]
            if not isinstance(links, list):
                raise ValueError
            assets = tuple(self._asset(a, repo, state, packages, files, created) for a in links)
            if len({a.id for a in assets}) != len(assets):
                raise PaginationError()
            return Release(tag, tag, obj.get("name"), False, obj["upcoming_release"], None,
                           created, published, None, assets, None, None)
        except (ValueError, TypeError, KeyError, ReleaseSchemaError) as exc:
            raise InvalidResponseError("invalid_release") from exc

    def discover_releases(self, target):
        state = dict(pages=0, requests=0, redirects=0)
        repo = self._repository(target, state)
        raw = self._listing(f"{repo.project_id}/releases", (), state)
        pages = state["pages"]
        tags = [r.get("tag_name") if isinstance(r, dict) else None for r in raw]
        if any(not release_key_ok("gitlab", t) for t in tags):
            raise InvalidResponseError("invalid_release")
        if len(set(tags)) != len(tags):
            raise PaginationError()
        packages = self._listing(f"{repo.project_id}/packages", (("package_type", "generic"),), state)
        mapped = {}
        for package in packages:
            if (isinstance(package, dict) and isinstance(package.get("name"), str)
                    and isinstance(package.get("version"), str) and package.get("status") == "default"
                    and package.get("package_type") == "generic"):
                mapped.setdefault((package["name"], package["version"]), []).append(package)
        files = {}
        releases = tuple(self._release(r, repo, state, lambda n, v: mapped.get((n, v), ()), files) for r in raw)
        return ReleaseCatalog(repo, releases, pages, state["requests"], state["redirects"], (),
                              f"/api/v4/projects/{repo.project_id}/releases", True)

    def resolve_release(self, repository, tag):
        if not release_key_ok("gitlab", tag):
            raise InvalidTargetError()
        state = dict(pages=0, requests=0, redirects=0)
        repo = self._repository(repository, state, numeric=True)
        try:
            _, raw = self._get(API_ROOT + f"{repo.project_id}/releases/" + quote(tag, safe=""), state)
        except NotFoundError as exc:
            raise ReleaseNotFoundError() from exc
        if not isinstance(raw, dict) or raw.get("tag_name") != tag:
            raise InvalidResponseError("invalid_release")
        cache, files = {}, {}
        def packages(name, version):
            key = name, version
            if key not in cache:
                cache[key] = self._listing(f"{repo.project_id}/packages", (("package_type", "generic"),
                    ("package_name", name), ("package_version", version)), state)
            return cache[key]
        return self._release(raw, repo, state, packages, files)
