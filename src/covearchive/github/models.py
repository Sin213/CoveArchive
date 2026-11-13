"""Typed release metadata parsed from untrusted API data."""

from dataclasses import dataclass, field
from datetime import datetime
import re

from covearchive.formats import match_extension

from .urls import RepositoryTarget


class ReleaseSchemaError(ValueError):
    """Release metadata does not satisfy the supported schema."""


@dataclass(frozen=True)
class ReleaseAsset:
    id: int
    name: str
    size: int
    state: str
    content_type: str
    browser_download_url: str
    original_digest: str | None
    digest_algorithm: str | None
    sha256: str | None
    created_at: str
    updated_at: str
    supported_extension: str | None


@dataclass(frozen=True)
class Release:
    id: int
    tag_name: str
    name: str | None
    draft: bool
    prerelease: bool
    immutable: bool | None
    created_at: str
    published_at: str | None
    body: str | None
    assets: tuple[ReleaseAsset, ...]
    tarball_url: str | None
    zipball_url: str | None


@dataclass(frozen=True)
class ReleaseCatalog:
    repository: RepositoryTarget
    releases: tuple[Release, ...]
    pages: int
    requests: int
    redirects: int
    duplicate_release_ids: tuple[int, ...]
    endpoint_path: str
    complete: bool
    atomic_snapshot: bool = field(default=False, init=False)


def _object(obj):
    if not isinstance(obj, dict):
        raise ReleaseSchemaError("Expected an object.")
    return obj


def _value(obj, key, kind, *, optional=False, nonempty=False, minimum=None):
    if key not in obj:
        if optional:
            return None
        raise ReleaseSchemaError(f"Missing {key}.")
    value = obj[key]
    if optional and value is None:
        return None
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ReleaseSchemaError(f"Invalid type for {key}.")
    if nonempty and not value:
        raise ReleaseSchemaError(f"Empty {key}.")
    if minimum is not None and value < minimum:
        raise ReleaseSchemaError(f"Invalid value for {key}.")
    return value


def _date(obj, key, *, optional=False):
    value = _value(obj, key, str, optional=optional)
    if value is not None:
        try:
            datetime.fromisoformat(value)
        except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
            raise ReleaseSchemaError(f"Invalid date for {key}.") from exc
    return value


def _digest(obj):
    original = _value(obj, "digest", str, optional=True)
    if original is None:
        return None, None, None
    algorithm, separator, value = original.partition(":")
    if not separator or re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", algorithm) is None or not value:
        raise ReleaseSchemaError("Invalid digest.")
    algorithm = algorithm.translate(str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
    sha256 = None
    if algorithm == "sha256":
        if re.fullmatch(r"[0-9A-Fa-f]{64}", value) is None:
            raise ReleaseSchemaError("Invalid sha256 digest.")
        sha256 = value.lower()
    elif re.fullmatch(r"[\x20-\x7e]{1,512}", value) is None:
        raise ReleaseSchemaError("Invalid digest value.")
    return original, algorithm, sha256


def parse_asset(obj) -> ReleaseAsset:
    """Validate an asset, retaining publisher text and digest metadata verbatim."""
    obj = _object(obj)
    name = _value(obj, "name", str, nonempty=True)
    original, algorithm, sha256 = _digest(obj)
    return ReleaseAsset(
        id=_value(obj, "id", int, minimum=1),
        name=name,
        size=_value(obj, "size", int, minimum=0),
        state=_value(obj, "state", str),
        content_type=_value(obj, "content_type", str),
        browser_download_url=_value(obj, "browser_download_url", str),
        original_digest=original,
        digest_algorithm=algorithm,
        sha256=sha256,
        created_at=_date(obj, "created_at"),
        updated_at=_date(obj, "updated_at"),
        supported_extension=match_extension(name),
    )


def parse_release(obj) -> Release:
    """Validate release fields without interpreting untrusted publisher text."""
    obj = _object(obj)
    return Release(
        id=_value(obj, "id", int, minimum=1),
        tag_name=_value(obj, "tag_name", str, nonempty=True),
        name=_value(obj, "name", str, optional=True),
        draft=_value(obj, "draft", bool),
        prerelease=_value(obj, "prerelease", bool),
        immutable=_value(obj, "immutable", bool, optional=True),
        created_at=_date(obj, "created_at"),
        published_at=_date(obj, "published_at", optional=True),
        body=_value(obj, "body", str, optional=True),
        assets=tuple(parse_asset(item) for item in _value(obj, "assets", list)),
        tarball_url=_value(obj, "tarball_url", str, optional=True),
        zipball_url=_value(obj, "zipball_url", str, optional=True),
    )
