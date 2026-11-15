"""Validated, deterministic archive metadata; no filesystem or network access.

archive-index.json is authoritative for repository identity and the established
release-folder/saved-name registry. release.json is per-release metadata; index
asset records are a derived copy for planning. CA-S5.1 returns validated unindexed
manifests for detect-only recovery: nothing repairs or rebuilds the index, and
recovered claims require actual-byte verification and the normal writer/CAS flow.
ACTUAL FILE BYTES alone establish integrity: completed in JSON is a CLAIM, never
proof. A corrupt index must never authorize deletion.
"""

from dataclasses import dataclass
from datetime import datetime
import json
import re
from urllib.parse import quote

from covearchive.github.models import Release
from covearchive.jsondepth import check_json_depth
from covearchive.github.urls import GitHubUrlError, RepositoryTarget, parse_github_url

from .naming import RELEASE_FILENAME, is_release_folder, is_safe_component, name_key


SCHEMA_VERSION = 1
PROVIDER = "github"
STATUS_PLANNED = "planned"
STATUS_COMPLETED = "completed"
# 64 MiB cannot cover MAX_TOTAL_ASSETS at worst-case size; planning wraps overflow.
MAX_INDEX_BYTES = 64 * 1024 * 1024
# Asset: 1024 original chars * 12 B + saved name (<=120 units) * 12 B + keys/hashes
# is ~14.5 KB; *1000 assets is ~14.6 MB <16 MiB, including release metadata.
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_RELEASES = 50_000
MAX_ASSETS_PER_RELEASE = 1_000
MAX_TOTAL_ASSETS = 100_000
MAX_SIZE = 2**53 - 1
MAX_TAG_LENGTH = 1024
_ASSET_KEYS = ("asset_id", "original_name", "saved_name", "size", "publisher_sha256", "local_sha256", "status")
_RECORD_KEYS = ("release_id", "tag", "folder", "assets")
_RELEASE_KEYS = ("release_id", "tag", "folder", "title", "published_at", "prerelease", "source_url")
_HASH = re.compile(r"[0-9a-f]{64}")
_SURROGATE_PAIR = re.compile(r"[\ud800-\udbff][\udc00-\udfff]")


class ManifestError(ValueError):
    """Manifest failure with a stable code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class UnsupportedVersionError(ManifestError):
    """A schema version that must never be silently overwritten."""


def _integer(value, maximum, code, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ManifestError(code)


def _text(value, limit, *, optional=False):
    if value is None and optional:
        return
    if not isinstance(value, str):
        raise ManifestError("field")
    # JSON ensure_ascii/loads merges adjacent high+low surrogates into one char.
    # Lone surrogates and low+high pairs round-trip and remain accepted.
    if _SURROGATE_PAIR.search(value):
        raise ManifestError("field")
    if len(value) > limit:
        raise ManifestError("limit")


def _collection(value, limit):
    if not isinstance(value, (tuple, list)):
        raise ManifestError("field")
    if len(value) > limit:
        raise ManifestError("limit")
    return value


def _unique(values):
    seen = set()
    for value in values:
        if value in seen:
            raise ManifestError("duplicate")
        seen.add(value)


def _repository(repository):
    if not isinstance(repository, RepositoryTarget) or not all(
            isinstance(v, str) for v in (repository.owner, repository.repo)):
        raise ManifestError("identity")
    try:
        target = parse_github_url(f"https://github.com/{repository.owner}/{repository.repo}")
    except GitHubUrlError as exc:
        raise ManifestError("identity") from exc
    if target != repository:
        raise ManifestError("identity")


def _release_fields(record):
    _integer(record.release_id, 2**63 - 1, "id", 1)
    _text(record.tag, MAX_TAG_LENGTH)
    if not isinstance(record.folder, str) or len(record.folder) > 64 or not is_release_folder(record.folder, record.release_id):
        raise ManifestError("path")


def _assets(items):
    items = _collection(items, MAX_ASSETS_PER_RELEASE)
    if not all(isinstance(a, AssetRecord) for a in items):
        raise ManifestError("field")
    _unique(a.asset_id for a in items)
    _unique(name_key(a.saved_name) for a in items)
    return tuple(sorted(items, key=lambda a: a.asset_id))


def _source(repository, tag):
    try:
        return f"https://github.com/{repository.owner}/{repository.repo}/releases/tag/" + quote(tag, safe="/")
    except UnicodeError:
        return None


@dataclass(frozen=True)
class AssetRecord:
    asset_id: int
    original_name: str
    saved_name: str
    size: int
    publisher_sha256: str | None
    local_sha256: str | None
    status: str

    def __post_init__(self):
        _integer(self.asset_id, 2**63 - 1, "id", 1)
        _integer(self.size, MAX_SIZE, "size")
        _text(self.original_name, 1024)
        if (not isinstance(self.saved_name, str) or len(self.saved_name) > 120
                or not is_safe_component(self.saved_name) or name_key(self.saved_name) == name_key(RELEASE_FILENAME)):
            raise ManifestError("path")
        for digest in (self.publisher_sha256, self.local_sha256):
            if digest is not None and (not isinstance(digest, str) or _HASH.fullmatch(digest) is None):
                raise ManifestError("hash")
        if self.status not in (STATUS_PLANNED, STATUS_COMPLETED):
            raise ManifestError("status")
        if (self.status == STATUS_PLANNED and self.local_sha256 is not None
                or self.status == STATUS_COMPLETED and self.local_sha256 is None):
            raise ManifestError("status")
        if self.status == STATUS_COMPLETED and self.publisher_sha256 is not None and self.publisher_sha256 != self.local_sha256:
            raise ManifestError("hash")

    @property
    def publisher_verified(self) -> bool:
        return self.status == STATUS_COMPLETED and self.publisher_sha256 is not None and self.publisher_sha256 == self.local_sha256


@dataclass(frozen=True)
class ReleaseRecord:
    release_id: int
    tag: str
    folder: str
    assets: tuple[AssetRecord, ...]

    def __post_init__(self):
        _release_fields(self)
        object.__setattr__(self, "assets", _assets(self.assets))


@dataclass(frozen=True)
class ProjectIndex:
    repository: RepositoryTarget
    releases: tuple[ReleaseRecord, ...]

    def __post_init__(self):
        _repository(self.repository)
        releases = _collection(self.releases, MAX_RELEASES)
        if not all(isinstance(r, ReleaseRecord) for r in releases):
            raise ManifestError("field")
        if sum(len(r.assets) for r in releases) > MAX_TOTAL_ASSETS:
            raise ManifestError("limit")
        _unique(r.release_id for r in releases)
        _unique(name_key(r.folder) for r in releases)
        _unique(a.asset_id for r in releases for a in r.assets)
        object.__setattr__(self, "releases", tuple(sorted(releases, key=lambda r: r.release_id)))
        # Every constructible document must round-trip through its capped reader.
        if len(dumps_index(self)) > MAX_INDEX_BYTES:
            raise ManifestError("limit")


@dataclass(frozen=True)
class ReleaseManifest:
    repository: RepositoryTarget
    release_id: int
    tag: str
    folder: str
    title: str | None
    published_at: str | None
    prerelease: bool
    source_url: str | None
    assets: tuple[AssetRecord, ...]

    def __post_init__(self):
        _repository(self.repository)
        _release_fields(self)
        _text(self.title, 1024, optional=True)
        if self.published_at is not None:
            try:
                if not isinstance(self.published_at, str):
                    raise TypeError
                datetime.fromisoformat(self.published_at)
            except (ValueError, TypeError, OverflowError, UnicodeError) as exc:
                raise ManifestError("field") from exc
        if type(self.prerelease) is not bool:
            raise ManifestError("field")
        if self.source_url is not None and self.source_url != _source(self.repository, self.tag):
            raise ManifestError("path")
        object.__setattr__(self, "assets", _assets(self.assets))
        if len(dumps_release_manifest(self)) > MAX_MANIFEST_BYTES:
            raise ManifestError("limit")


def build_release_manifest(repository: RepositoryTarget, release: Release,
                           record: ReleaseRecord) -> ReleaseManifest:
    if release.id != record.release_id:
        raise ManifestError("identity")
    return ReleaseManifest(repository, record.release_id, record.tag, record.folder,
                           release.name, release.published_at, release.prerelease,
                           _source(repository, record.tag), record.assets)


def _values(record, keys):
    return {key: getattr(record, key) for key in keys}


def _header(repository):
    return dict(schema_version=SCHEMA_VERSION, provider=PROVIDER,
                repository=dict(owner=repository.owner, name=repository.repo, key=repository.repo_key))


def dumps_index(index: ProjectIndex) -> str:
    data = _header(index.repository)
    data["releases"] = [dict(release_id=r.release_id, tag=r.tag, folder=r.folder,
        assets=[_values(a, _ASSET_KEYS) for a in r.assets]) for r in index.releases]
    return json.dumps(data, indent=2, ensure_ascii=True) + "\n"


def dumps_release_manifest(manifest: ReleaseManifest) -> str:
    data = _header(manifest.repository)
    data.update(release=_values(manifest, _RELEASE_KEYS), assets=[_values(a, _ASSET_KEYS) for a in manifest.assets])
    return json.dumps(data, indent=2, ensure_ascii=True) + "\n"


def _keys(obj, keys):
    if not isinstance(obj, dict) or obj.keys() != set(keys):
        raise ManifestError("field")
    return obj


def _pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ManifestError("syntax")
        obj[key] = value
    return obj


def _constant(value):
    raise ManifestError("syntax")


def _parse(text, cap, keys):
    if not isinstance(text, str):
        raise TypeError("Manifest JSON must be a string.")
    # Character bound first avoids encoding oversized input; then enforce bytes.
    if len(text) > cap or len(text.encode("utf-8", "surrogatepass")) > cap:
        raise ManifestError("limit")
    try:
        check_json_depth(text)
        data = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except ManifestError:
        raise
    except RecursionError as exc:
        raise ManifestError("limit") from exc
    except ValueError as exc:
        raise ManifestError("syntax") from exc
    if not isinstance(data, dict):
        raise ManifestError("root")
    if "schema_version" not in data:
        raise ManifestError("field")
    if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
        raise UnsupportedVersionError("version")
    _keys(data, keys)
    if data["provider"] != PROVIDER:
        raise ManifestError("field")
    return data


def _load_repository(obj):
    _keys(obj, ("owner", "name", "key"))
    repository = RepositoryTarget(obj["owner"], obj["name"])
    _repository(repository)
    if obj["key"] != repository.repo_key:
        raise ManifestError("identity")
    return repository


def _load_assets(items):
    return tuple(AssetRecord(**_keys(a, _ASSET_KEYS)) for a in _collection(items, MAX_ASSETS_PER_RELEASE))


def loads_index(text: str) -> ProjectIndex:
    data = _parse(text, MAX_INDEX_BYTES, ("schema_version", "provider", "repository", "releases"))
    repository = _load_repository(data["repository"])
    releases = _collection(data["releases"], MAX_RELEASES)
    if sum(len(_collection(_keys(r, _RECORD_KEYS)["assets"], MAX_ASSETS_PER_RELEASE)) for r in releases) > MAX_TOTAL_ASSETS:
        raise ManifestError("limit")
    records = tuple(ReleaseRecord(r["release_id"], r["tag"], r["folder"], _load_assets(r["assets"])) for r in releases)
    return ProjectIndex(repository, records)


def loads_release_manifest(text: str) -> ReleaseManifest:
    data = _parse(text, MAX_MANIFEST_BYTES, ("schema_version", "provider", "repository", "release", "assets"))
    return ReleaseManifest(repository=_load_repository(data["repository"]),
        **_keys(data["release"], _RELEASE_KEYS), assets=_load_assets(data["assets"]))


def check_consistent(index: ProjectIndex, manifest: ReleaseManifest) -> None:
    record = next((r for r in index.releases if r.release_id == manifest.release_id), None)
    if (index.repository != manifest.repository or record is None
            or (record.tag, record.folder, record.assets) != (manifest.tag, manifest.folder, manifest.assets)):
        raise ManifestError("consistency")
