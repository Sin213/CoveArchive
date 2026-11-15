"""Immutable library snapshots, strict JSON, and bulk URL parsing; no I/O.

All operations return a validated new Library. Collection ids are stable;
repository operations address provider-qualified identities. browse(None) is
All repositories, browse(UNSORTED) is unassigned, otherwise scope is recursive.
"""
from dataclasses import dataclass, field, replace
from functools import cached_property, lru_cache
from types import MappingProxyType
import json
import re
import unicodedata
from urllib.parse import urlsplit
from uuid import uuid4

from covearchive.github.urls import GitHubUrlError, ReleaseTagTarget, parse_github_url
from covearchive.jsondepth import check_json_depth

MAX_BYTES = 8 * 1024 * 1024
MAX_BULK_BYTES = 1024 * 1024
MAX_REPOSITORIES = 10000
MAX_COLLECTIONS = 2000
MAX_DEPTH = 8
UNSORTED = ""
AUTO_ARCHIVE_HOURS = (1, 6, 12, 24)
_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
_HOST = re.compile(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?")
_SEGMENT = re.compile(r"[A-Za-z0-9._-]{1,100}")
_BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


class LibraryError(ValueError):
    """Stable code and safe message; untrusted input is never interpolated."""
    def __init__(self, code):
        self.code = code
        super().__init__("Invalid library data: " + code.replace("_", " ") + ".")


def _fail(code):
    raise LibraryError(code)


@lru_cache(maxsize=4096)
def _forbidden(text):
    return any(unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"} or c in _BIDI for c in text)


def validate_name(value, *, allow_slash=False):
    if not isinstance(value, str) or _forbidden(value):
        _fail("name")
    value = unicodedata.normalize("NFC", value.strip())
    if not 1 <= len(value) <= 100 or (not allow_slash and "/" in value):
        _fail("name")
    return value


def _id(value):
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _fail("collection_id")
    return value


_NORMALIZED = {}


def normalize_repository(url, provider="github"):
    if not isinstance(url, str) or not isinstance(provider, str): return _normalize_repository(url, provider)
    key = (provider, url); result = _NORMALIZED.get(key)
    if result is None:
        result = _normalize_repository(url, provider)
        if len(_NORMALIZED) >= 20000: _NORMALIZED.clear()
        _NORMALIZED[key] = _NORMALIZED[(result[0], result[1])] = result
    return result


def _normalize_repository(url, provider):
    """Return (provider, URL, ref, label); accepted canonical tuples are fixed points."""
    if not isinstance(provider, str) or provider.casefold() not in {"github", "gitlab", "gitea", "other"}:
        _fail("provider")
    provider = provider.casefold()
    if not isinstance(url, str) or len(url) > 2048:
        _fail("url")
    if provider == "github":
        try:
            target = parse_github_url(url)
        except GitHubUrlError as exc:
            raise LibraryError(exc.code) from None
        if isinstance(target, ReleaseTagTarget):
            _fail("release_tag")
        if target.repo.endswith(".git"): _fail("url")
        label = target.owner + "/" + target.repo
        return provider, "https://github.com/" + label, target.repo_key, label
    if _forbidden(url) or any(c.isspace() for c in url) or any(c in url for c in "@?#\\"):
        _fail("url")
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"https", "http"} or _HOST.fullmatch(parts.netloc) is None
                or (parts.port is not None and not 1 <= parts.port <= 65535)):
            _fail("url")
    except ValueError:
        _fail("url")
    path = parts.path.removesuffix("/").removesuffix(".git")
    if path.endswith(".git"): _fail("url")
    segments = path[1:].split("/") if path.startswith("/") else []
    if not 2 <= len(segments) <= 10 or any(
            _SEGMENT.fullmatch(s) is None or s in {".", ".."} for s in segments):
        _fail("url")
    label = parts.netloc.lower() + "/" + "/".join(segments)
    return provider, parts.scheme + "://" + label, label.casefold(), label


@dataclass(frozen=True)
class Collection:
    id: str
    name: str
    parent: str | None = None

    def __post_init__(self):
        _id(self.id)
        object.__setattr__(self, "name", validate_name(self.name))
        if self.parent is not None: _id(self.parent)


@dataclass(frozen=True)
class Repository:
    provider: str
    url: str
    name: str | None = None
    collection: str | None = None
    folder: str | None = None
    auto_archive: int | None = None
    _ref: str = field(init=False, repr=False, compare=False)
    _identity: str = field(init=False, repr=False, compare=False)
    _label: str = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        provider, url, ref, label = normalize_repository(self.url, self.provider)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "_ref", ref)
        object.__setattr__(self, "_identity", provider + ":" + ref)
        object.__setattr__(self, "_label", label)
        if self.auto_archive is not None and (type(self.auto_archive) is not int
                or self.auto_archive not in AUTO_ARCHIVE_HOURS or provider != "github"):
            _fail("auto_archive")
        if self.name is not None:
            object.__setattr__(self, "name", validate_name(self.name, allow_slash=True))
        if self.collection is not None: _id(self.collection)
        if self.folder is not None and (not isinstance(self.folder, str)
                or len(self.folder) > 512 or _forbidden(self.folder)):
            _fail("folder")

    @property
    def ref(self):
        return self._ref

    @property
    def label(self): return self._label

    @property
    def identity(self):
        return self._identity

    @property
    def active(self):
        return self.provider == "github"

    @property
    def display_name(self):
        return self.name if self.name is not None else self._label


@dataclass(frozen=True)
class Library:
    collections: tuple[Collection, ...] = ()
    repositories: tuple[Repository, ...] = ()

    def __post_init__(self):
        try:
            collections, repositories = tuple(self.collections), tuple(self.repositories)
        except TypeError:
            _fail("schema")
        object.__setattr__(self, "collections", collections)
        object.__setattr__(self, "repositories", repositories)
        if len(collections) > MAX_COLLECTIONS or len(repositories) > MAX_REPOSITORIES:
            _fail("limit")
        by_id, siblings = {}, set()
        for c in collections:
            if not isinstance(c, Collection):
                _fail("schema")
            sibling = (c.parent, c.name.casefold())
            if c.id in by_id or sibling in siblings:
                _fail("collection_duplicate")
            by_id[c.id] = c
            siblings.add(sibling)
        for c in collections:
            seen, current = set(), c.id
            while current is not None:
                if current not in by_id:
                    _fail("collection_missing")
                if current in seen:
                    _fail("collection_cycle")
                seen.add(current)
                if len(seen) > MAX_DEPTH:
                    _fail("collection_depth")
                current = by_id[current].parent
        identities = set()
        for r in repositories:
            if not isinstance(r, Repository):
                _fail("schema")
            if r.collection is not None and r.collection not in by_id:
                _fail("collection_missing")
            identity = r.identity
            if identity in identities:
                _fail("repository_duplicate")
            identities.add(identity)

    def clone(self):
        return Library(self.collections, self.repositories)

    def _collection(self, id):
        _id(id)
        for c in self.collections:
            if c.id == id:
                return c
        _fail("collection_missing")

    def _parent(self, parent):
        if parent is not None:
            self._collection(parent)

    def create_collection(self, name, parent=None, *, id=None):
        self._parent(parent)
        c = Collection(uuid4().hex if id is None else id, name, parent)
        return Library(self.collections + (c,), self.repositories)

    def rename_collection(self, id, name):
        self._collection(id)
        return Library(tuple(replace(c, name=name) if c.id == id else c for c in self.collections), self.repositories)

    def move_collection(self, id, parent):
        self._collection(id)
        self._parent(parent)
        return Library(tuple(replace(c, parent=parent) if c.id == id else c for c in self.collections), self.repositories)

    def descendant_ids(self, id):
        self._collection(id)
        children = {}
        for c in self.collections:
            children.setdefault(c.parent, []).append(c.id)
        found, pending = set(), [id]
        while pending:
            current = pending.pop()
            found.add(current)
            pending.extend(children.get(current, ()))
        return frozenset(found)

    def delete_collection(self, id):
        parent = self._collection(id).parent
        removed = self.descendant_ids(id)
        return Library(tuple(c for c in self.collections if c.id not in removed),
            tuple(replace(r, collection=parent) if r.collection in removed else r for r in self.repositories))

    @cached_property
    def collection_paths(self):
        by_id = {c.id: c for c in self.collections}
        paths = {}
        def compute(c):
            if c.id not in paths: paths[c.id] = (compute(by_id[c.parent]) + "/" if c.parent else "") + c.name
            return paths[c.id]
        for c in self.collections: compute(c)
        return MappingProxyType(paths)

    def collection_path(self, id):
        if id is None: return ""
        if _id(id) not in self.collection_paths: _fail("collection_missing")
        return self.collection_paths[id]

    def add_repository(self, url, *, provider="github", name=None, collection=None, folder=None):
        self._parent(collection)
        repo = url if isinstance(url, Repository) else Repository(provider, url, name, collection, folder)
        if any(r.identity == repo.identity for r in self.repositories):
            return self.clone()
        return Library(self.collections, self.repositories + (replace(repo, auto_archive=None),))

    def _identities(self, identities):
        if isinstance(identities, str):
            identities = [identities]
        try:
            identities = tuple(identities)
        except TypeError:
            _fail("repository_missing")
        if any(not isinstance(i, str) for i in identities):
            _fail("repository_missing")
        selected = {i.casefold() for i in identities}
        if not selected <= {r.identity for r in self.repositories}:
            _fail("repository_missing")
        return selected

    def move_repositories(self, identities, collection=None):
        selected = self._identities(identities)
        self._parent(collection)
        return Library(self.collections, tuple(replace(r, collection=collection)
            if r.identity in selected else r for r in self.repositories))

    def set_auto_archive(self, identities, hours):
        selected = self._identities(identities)
        if hours is not None and (type(hours) is not int or hours not in AUTO_ARCHIVE_HOURS):
            _fail("auto_archive")
        return Library(self.collections, tuple(replace(r, auto_archive=hours)
            if r.identity in selected else r for r in self.repositories))

    def rename_repository(self, identity, name):
        selected = self._identities([identity])
        return Library(self.collections, tuple(replace(r, name=name)
            if r.identity in selected else r for r in self.repositories))

    def remove_repositories(self, identities):
        selected = self._identities(identities)
        return Library(self.collections, tuple(r for r in self.repositories if r.identity not in selected))

    def browse(self, collection=None, *, search=""):
        if not isinstance(search, str):
            _fail("search")
        scope = None if collection is None else ({None} if collection == UNSORTED else self.descendant_ids(collection))
        query = search.casefold()
        paths = self.collection_paths if query else {}
        return tuple(r for r in self.repositories if (scope is None or r.collection in scope) and
            (not query or any(query in text.casefold() for text in
                (r.display_name, r.ref, paths.get(r.collection, ""), r.provider))))


def _keys(data, expected):
    if not isinstance(data, dict) or set(data) != set(expected):
        _fail("schema")


def _object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            _fail("duplicate_key")
        data[key] = value
    return data


def strict_json_loads(raw):
    """Decode bounded UTF-8 JSON (BOM accepted) without duplicate/nonfinite values."""
    if not isinstance(raw, bytes):
        _fail("json")
    if len(raw) > MAX_BYTES:
        _fail("size")
    try:
        text = raw.decode("utf-8-sig")
        check_json_depth(text)
        return json.loads(text, object_pairs_hook=_object, parse_constant=lambda _: _fail("nonfinite"))
    except LibraryError:
        raise
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise LibraryError("json") from None


def library_from_data(data):
    if isinstance(data, dict) and type(data.get("version")) is int and data["version"] > 1:
        _fail("newer_version")
    _keys(data, ("version", "collections", "repositories"))
    version = data["version"]
    if type(version) is not int:
        _fail("version")
    if version != 1:
        _fail("version")
    collections, repositories = data["collections"], data["repositories"]
    if not isinstance(collections, list) or not isinstance(repositories, list):
        _fail("schema")
    if len(collections) > MAX_COLLECTIONS or len(repositories) > MAX_REPOSITORIES:
        _fail("limit")
    for c in collections:
        _keys(c, ("id", "name", "parent"))
    for r in repositories:
        keys = ("provider", "url", "name", "collection", "folder")
        _keys(r, keys + ("auto_archive",) if isinstance(r, dict) and "auto_archive" in r else keys)
    return Library(tuple(Collection(**c) for c in collections), tuple(Repository(**r) for r in repositories))


def library_to_data(library):
    if not isinstance(library, Library):
        _fail("schema")
    library = library.clone()
    return {"version": 1,
        "collections": [{"id": c.id, "name": c.name, "parent": c.parent} for c in library.collections],
        "repositories": [{"provider": r.provider, "url": r.url, "name": r.name,
            "collection": r.collection, "folder": r.folder,
            **({"auto_archive": r.auto_archive} if r.auto_archive is not None else {})} for r in library.repositories]}


def load_library(raw):
    return library_from_data(strict_json_loads(raw))


def dump_library(library):
    raw = (json.dumps(library_to_data(library), ensure_ascii=True, allow_nan=False,
        separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES:
        _fail("size")
    return raw


@dataclass(frozen=True)
class BulkLineResult:
    line: int
    status: str
    code: str | None = None


@dataclass(frozen=True)
class BulkResult:
    library: Library
    results: tuple[BulkLineResult, ...]

    @property
    def added(self):
        return sum(r.status == "ADDED" for r in self.results)

    @property
    def skipped(self):
        return sum(r.status == "SKIPPED" for r in self.results)

    @property
    def failed(self):
        return sum(r.status == "FAILED" for r in self.results)


def add_urls(library, text, collection=None):
    """Parse a bounded paste atomically; bad lines fail independently, no I/O.

    Result messages contain only stable codes and line numbers, never raw input.
    Whole-input limits raise LibraryError without changing the source snapshot.
    """
    if not isinstance(text, str):
        _fail("bulk_text")
    try:
        if len(text) > MAX_BULK_BYTES or len(text.encode("utf-8")) > MAX_BULK_BYTES:
            _fail("bulk_size")
    except UnicodeEncodeError:
        raise LibraryError("bulk_text") from None
    lines = [(n, line.strip()) for n, line in enumerate(text.splitlines(), 1) if line.strip()]
    if len(lines) > MAX_REPOSITORIES:
        _fail("bulk_limit")
    library._parent(collection)
    repos = list(library.repositories)
    seen = {r.identity for r in repos}
    results = []
    for number, url in lines:
        try:
            repo = Repository("github", url, collection=collection)
            if repo.identity in seen:
                results.append(BulkLineResult(number, "SKIPPED"))
                continue
            if len(repos) >= MAX_REPOSITORIES:
                _fail("limit")
            seen.add(repo.identity)
            repos.append(repo)
            results.append(BulkLineResult(number, "ADDED"))
        except LibraryError as exc:
            results.append(BulkLineResult(number, "FAILED", exc.code))
    return BulkResult(Library(library.collections, tuple(repos)), tuple(results))
