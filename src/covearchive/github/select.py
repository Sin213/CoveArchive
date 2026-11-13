"""Choose releases and assets without network access."""

from dataclasses import dataclass
from typing import Literal

from covearchive.formats import SUPPORTED_EXTENSIONS, match_extension

from .models import Release, ReleaseAsset, ReleaseCatalog


@dataclass(frozen=True)
class AssetMatch:
    asset: ReleaseAsset
    extension: str


@dataclass(frozen=True)
class SelectedRelease:
    release: Release
    matches: tuple[AssetMatch, ...]
    empty_reason: Literal["no_assets", "no_matching_assets"] | None


@dataclass(frozen=True)
class ExcludedRelease:
    release_id: int
    tag_name: str
    reason: Literal["draft", "prerelease"]


@dataclass(frozen=True)
class ReleaseSelection:
    releases: tuple[SelectedRelease, ...]
    excluded: tuple[ExcludedRelease, ...]
    include_prereleases: bool
    enabled_extensions: tuple[str, ...]


def select_releases(source, *, include_prereleases=False, enabled_extensions=None):
    """Preserve input order, excluding drafts and optionally prereleases."""
    if enabled_extensions is None:
        enabled = SUPPORTED_EXTENSIONS
    else:
        if isinstance(enabled_extensions, (str, bytes)):
            raise TypeError("Enabled formats must be an iterable of extensions.")
        enabled = tuple(enabled_extensions)
        match_extension("", enabled)
        selected_extensions = set(enabled)
        enabled = tuple(ext for ext in SUPPORTED_EXTENSIONS if ext in selected_extensions)
    releases = source.releases if isinstance(source, ReleaseCatalog) else source
    selected, excluded, seen = [], [], set()
    for release in releases:
        if release.id in seen:
            continue
        seen.add(release.id)
        reason = "draft" if release.draft else (
            "prerelease" if release.prerelease and not include_prereleases else None
        )
        if reason is not None:
            excluded.append(ExcludedRelease(release.id, release.tag_name, reason))
            continue
        matches, asset_ids = [], set()
        for asset in release.assets:
            if asset.id in asset_ids:
                continue
            asset_ids.add(asset.id)
            extension = match_extension(asset.name, enabled)
            if asset.state == "uploaded" and extension is not None:
                matches.append(AssetMatch(asset, extension))
        empty_reason = None if matches else (
            "no_assets" if not release.assets else "no_matching_assets"
        )
        selected.append(SelectedRelease(release, tuple(matches), empty_reason))
    return ReleaseSelection(tuple(selected), tuple(excluded), include_prereleases, enabled)


def select_release_by_tag(source, tag, *, include_prereleases=True, enabled_extensions=None):
    """Select an exact tag from a complete scan; no tag endpoint is queried."""
    selection = select_releases(source, include_prereleases=include_prereleases,
                                enabled_extensions=enabled_extensions)
    return next((item for item in selection.releases if item.release.tag_name == tag), None)
