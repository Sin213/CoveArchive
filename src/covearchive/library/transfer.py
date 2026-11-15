"""Portable metadata interchange; only read_import_file performs file I/O."""
from dataclasses import dataclass, field, replace
import json
import os
import re
import stat
from urllib.parse import urlsplit
from uuid import uuid4

from covearchive.library import model as m

FORMAT = "covearchive-repository-list"


def _path(value):
    if value is None or value == "":
        return ()
    if not isinstance(value, str) or len(value) > m.MAX_DEPTH * 101:
        raise m.LibraryError("collection_path")
    parts = tuple(m.validate_name(s) for s in value.split("/"))
    if len(parts) > m.MAX_DEPTH:
        raise m.LibraryError("collection_depth")
    return parts


def _git_sync_entry(entry):
    """Supplied Git-Sync v1: site + repository path, with api identifying provider.

    filter is a collection path; folder is inert. Unknown fields are never read.
    False retention only increments a notice; it never changes archive policy.
    """
    if not isinstance(entry, dict) or not isinstance(entry.get("repository"), str):
        raise m.LibraryError("entry")
    path = entry["repository"].strip().strip("/")
    if "@" in path: raise m.LibraryError("userinfo")
    if not path or any(c in path for c in ("://", "?", "#", "\\")) or m._forbidden(path) or any(c.isspace() for c in path):
        raise m.LibraryError("url")
    api, site = entry.get("api", ""), entry.get("site", "")
    api = api.strip().casefold() if isinstance(api, str) else ""
    site = site.strip().lower() if isinstance(site, str) else ""
    if api and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", api) is None: api = ""
    if m._forbidden(site) or any(c.isspace() for c in site): raise m.LibraryError("url")
    if not site and api == "github": site = "github.com"
    if "@" in site: raise m.LibraryError("userinfo")
    try:
        parts = urlsplit(site if "://" in site else "https://" + site)
        port = parts.port
    except ValueError:
        raise m.LibraryError("url") from None
    if (parts.scheme not in {"http", "https"} or m._HOST.fullmatch(parts.netloc) is None
            or parts.path or parts.query or parts.fragment or "?" in site or "#" in site
            or (port is not None and not 1 <= port <= 65535)):
        raise m.LibraryError("url")
    host = parts.netloc
    if not api: api = "github" if host in {"github.com", "www.github.com"} else "gitlab" if host == "gitlab.com" else ""
    provider = api if api in {"gitlab", "gitea"} else "github" if api == "github" and host in {"github.com", "www.github.com"} else "other"
    url = "https://github.com/" + path if provider == "github" else parts.scheme + "://" + host + "/" + path
    def optional(key):
        value = entry.get(key)
        return None if isinstance(value, str) and not value.strip() else value
    return (m.Repository(provider, url, optional("name"), folder=optional("folder")),
        _path(optional("filter")), entry.get("keepPreviousVersions") is False)


def _native_entry(entry):
    m._keys(entry, ("url", "provider", "name", "collection", "folder"))
    if entry["collection"] == "":
        raise m.LibraryError("collection_path")
    return (m.Repository(entry["provider"], entry["url"], entry["name"], folder=entry["folder"]),
        _path(entry["collection"]), False)


def _merge(library, paths, records):
    """Assemble once, then validate once, keeping existing and first metadata."""
    collections, repositories = list(library.collections), list(library.repositories)
    siblings = {(c.parent, c.name.casefold()): c.id for c in collections}
    seen = {r.identity for r in repositories}
    def ensure(path):
        parent = None
        for name in path:
            key = (parent, name.casefold())
            if key not in siblings:
                if len(collections) >= m.MAX_COLLECTIONS:
                    raise m.LibraryError("limit")
                c = m.Collection(uuid4().hex, name, parent)
                collections.append(c)
                siblings[key] = c.id
            parent = siblings[key]
        return parent
    for path in paths:
        ensure(path)
    for repo, path in records:
        if repo.identity in seen:
            continue
        if len(repositories) >= m.MAX_REPOSITORIES:
            raise m.LibraryError("limit")
        repositories.append(replace(repo, collection=ensure(path)))
        seen.add(repo.identity)
    return m.Library(tuple(collections), tuple(repositories))


@dataclass(frozen=True)
class ImportPlan:
    kind: str | None = None
    entries: int = 0
    to_add: int = 0
    duplicates: int = 0
    inactive: int = 0
    new_collections: int = 0
    retention_notices: int = 0
    invalid: tuple[tuple[int, str], ...] = ()
    code: str | None = None
    _paths: tuple = field(default=(), repr=False)
    _records: tuple = field(default=(), repr=False)

    @property
    def valid(self):
        return not self.invalid and self.code is None

    def apply(self, current_library):
        """Revalidate against current state; invalid plans cannot be applied."""
        if not self.valid:
            raise m.LibraryError("invalid_plan")
        return _merge(current_library, self._paths, self._records)


def build_plan(raw_bytes, library):
    """Return a safe preview, never raising for malformed input.

    Invalid entries use 1-based indexes; index 0 denotes a structural error.
    Only the first 50 invalid entries are retained; raw values never enter repr.
    """
    kind, count = None, 0
    try:
        data = m.strict_json_loads(raw_bytes)
        if not isinstance(data, dict) or not isinstance(library, m.Library):
            raise m.LibraryError("schema")
        if data.get("format") not in (FORMAT, "Git-Sync data"): raise m.LibraryError("schema")
        kind = "native" if data["format"] == FORMAT else "git-sync"
        version = data.get("version")
        if type(version) is not int or version != 1:
            raise m.LibraryError("newer_version" if type(version) is int and version > 1 else "version")
        paths = ()
        if kind == "native":
            m._keys(data, ("format", "version", "collections", "repositories"))
            if data["format"] != FORMAT or not isinstance(data["collections"], list):
                raise m.LibraryError("schema")
            if len(data["collections"]) > m.MAX_COLLECTIONS:
                raise m.LibraryError("limit")
            if any(not isinstance(p, str) or not p for p in data["collections"]):
                raise m.LibraryError("collection_path")
            paths = tuple(_path(p) for p in data["collections"])
            entries = data["repositories"]
        else:
            entries = data.get("repos")
            filters = data.get("filters")
            paths = []
            for value in filters if isinstance(filters, list) else ():
                if isinstance(value, str):
                    try: paths.append(_path(value))
                    except m.LibraryError: pass
            paths = tuple(paths)
        if not isinstance(entries, list):
            raise m.LibraryError("schema")
        count = len(entries)
        if count > m.MAX_REPOSITORIES:
            raise m.LibraryError("limit")
        records, invalid, seen = [], [], {r.identity for r in library.repositories}
        duplicates = inactive = retention = 0
        for index, entry in enumerate(entries, 1):
            try:
                repo, path, notice = (_native_entry if kind == "native" else _git_sync_entry)(entry)
                retention += notice
                if repo.identity in seen:
                    duplicates += 1
                    continue
                records.append((repo, path))
                seen.add(repo.identity)
                inactive += not repo.active
            except m.LibraryError as exc:
                if len(invalid) < 50:
                    invalid.append((index, exc.code))
        merged = _merge(library, paths, records)
        return ImportPlan(kind, count, len(records), duplicates, inactive,
            len(merged.collections) - len(library.collections), retention, tuple(invalid),
            _paths=paths, _records=tuple(records))
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        code = exc.code if isinstance(exc, m.LibraryError) else "schema"
        return ImportPlan(kind=kind, entries=count, invalid=((0, code),), code=code)


def export_library(library):
    library = library.clone()
    paths = library.collection_paths
    data = {"format": FORMAT, "version": 1, "collections": sorted(paths.values()),
        "repositories": [{"provider": r.provider, "url": r.url, "name": r.name,
            "collection": paths.get(r.collection), "folder": r.folder}
            for r in sorted(library.repositories, key=lambda r: r.identity)]}
    raw = (json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > m.MAX_BYTES:
        raise m.LibraryError("size")
    return raw


def is_export_file(path):
    try:
        data = m.strict_json_loads(read_import_file(path))
        return isinstance(data, dict) and data.get("format") == FORMAT
    except m.LibraryError:
        return False


def read_import_file(path):
    """Read a regular file, bounded to MAX+1; never echo its path on failure."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise m.LibraryError("file_type")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise m.LibraryError("file_type")
            raw = stream.read(m.MAX_BYTES + 1)
        if len(raw) > m.MAX_BYTES:
            raise m.LibraryError("size")
        return raw
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, m.LibraryError):
            raise
        raise m.LibraryError("file") from None
