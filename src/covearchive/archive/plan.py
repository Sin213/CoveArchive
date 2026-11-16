"""Pure incremental planning; no filesystem or network access.

Callers must not convert a damaged/unsupported index to None: index=None means no
index is known, so CA-S5 must discover existing files. Actions are requests: CA-S5
must verify real files before executing ANY action, including actual size/hashes
before skipping a JSON completed CLAIM. The plan never authorizes deleting,
overwriting or renaming. Reparse points, junctions, symlinks, directory swaps,
TOCTOU, existing-file replacement and concurrent writers belong to CA-S5.
Selected remote values that cannot be recorded verbatim block the whole plan.

If plan_archive returns plan P: (1) every selected release's remote id/tag and every matched asset's id/name/size/sha256 can be stored; (2) P.index is a valid ProjectIndex; (3) for every selected release whose id is in P.index, manifest.build_release_manifest(P.repository, release, record) succeeds (title, published_at, prerelease, tag, folder, all assets, size cap). Otherwise it raises ONLY PlanError("unrepresentable") or PathBudgetError (never an untyped exception from untrusted data). Hostile values in EXCLUDED (draft/prerelease) releases, filtered/unavailable/unsupported ASSETS and unselected assets never block planning. The plan never modifies a recorded entry (I3).
"""

from dataclasses import dataclass, replace
from pathlib import PurePath, PureWindowsPath, PurePosixPath
import sys

from covearchive.github.models import Release, ReleaseAsset, ReleaseCatalog
from covearchive.github.select import ExcludedRelease, select_releases
from covearchive.github.urls import RepositoryTarget
from covearchive.providers import validate_repository

from . import naming
from .manifest import AssetRecord, ManifestError, ProjectIndex, ReleaseManifest, ReleaseRecord, build_release_manifest


class PlanError(ValueError):
    """Codes: incomplete_catalog, repository, duplicate_conflict, asset_conflict, unrepresentable."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AssetPlan:
    asset: ReleaseAsset
    extension: str
    record: AssetRecord
    state: str
    action: str
    notes: tuple[str, ...]
    path: PurePath


@dataclass(frozen=True)
class SkippedAsset:
    asset_id: int
    name: str
    reason: str


@dataclass(frozen=True)
class ReleasePlan:
    release: Release
    state: str
    folder: str
    assets: tuple[AssetPlan, ...]
    skipped: tuple[SkippedAsset, ...]
    notes: tuple[str, ...]
    empty_reason: str | None


@dataclass(frozen=True)
class RetainedEntry:
    release_id: int
    asset_id: int | None
    reason: str


@dataclass(frozen=True)
class ArchivePlan:
    repository: RepositoryTarget
    project_dir: PurePath
    index_present: bool
    releases: tuple[ReleasePlan, ...]
    retained: tuple[RetainedEntry, ...]
    excluded: tuple[ExcludedRelease, ...]
    index: ProjectIndex


def _repository_key(repository):
    try:
        validate_repository(repository)
    except (ValueError, TypeError) as exc:
        raise PlanError("repository") from exc
    return repository.repo_key


def _normalize(source, recorded_owners):
    originals, normalized, owners = {}, {}, {}
    for release in source:
        if release.id in originals:
            if originals[release.id] != release:
                raise PlanError("duplicate_conflict")
            continue
        originals[release.id] = release
        assets = {}
        for asset in release.assets:
            if asset.id in assets and assets[asset.id] != asset:
                raise PlanError("duplicate_conflict")
            if ((asset.id in owners and owners[asset.id] != release.id)
                    or (asset.id in recorded_owners and recorded_owners[asset.id] != release.id)):
                raise PlanError("asset_conflict")
            owners[asset.id] = release.id
            assets[asset.id] = asset
        normalized[release.id] = replace(release, assets=tuple(assets[i] for i in sorted(assets)))
    return {i: normalized[i] for i in sorted(normalized)}


def _retained(current, recorded, selection):
    excluded = {r.release_id: r.reason for r in selection.excluded}
    selected = {r.release.id: {m.asset.id for m in r.matches} for r in selection.releases}
    result = []
    for release_id, record in recorded.items():
        if release_id not in current:
            result.append(RetainedEntry(release_id, None, "release_removed_remotely"))
            continue
        if release_id in excluded:
            result.append(RetainedEntry(release_id, None, "release_excluded_" + excluded[release_id]))
            continue
        assets = {a.id: a for a in current[release_id].assets}
        for old in record.assets:
            asset = assets.get(old.asset_id)
            reason = ("asset_removed_remotely" if asset is None else
                      "asset_unavailable" if asset.state != "uploaded" else
                      "asset_filtered" if old.asset_id not in selected[release_id] else None)
            if reason:
                result.append(RetainedEntry(release_id, old.asset_id, reason))
    return tuple(sorted(result, key=lambda r: (r.release_id, r.asset_id or 0, r.reason)))


def plan_archive(source, *, archive_root, repository=None, index=None,
                 include_prereleases=False, enabled_extensions=None) -> ArchivePlan:
    naming.validate_root(archive_root)
    try:
        return _plan_archive(source, archive_root=archive_root, repository=repository, index=index,
            include_prereleases=include_prereleases, enabled_extensions=enabled_extensions)
    except ManifestError as exc:
        raise PlanError("unrepresentable") from exc


def _plan_archive(source, *, archive_root, repository, index, include_prereleases, enabled_extensions):
    if isinstance(source, ReleaseCatalog):
        if not source.complete:
            raise PlanError("incomplete_catalog")
        if repository is not None and _repository_key(repository) != _repository_key(source.repository):
            raise PlanError("repository")
        repository, source = source.repository, source.releases
    key = _repository_key(repository)
    if index is not None:
        if not isinstance(index, ProjectIndex) or index.repository.repo_key != key:
            raise PlanError("repository")
        repository = index.repository  # Persisted spelling owns the directory names.
    recorded = {r.release_id: r for r in index.releases} if index is not None else {}
    recorded_owners = {a.asset_id: r.release_id for r in recorded.values() for a in r.assets}
    # Consume a caller's complete iterable once; check conflicts before filtering.
    current = _normalize(tuple(source), recorded_owners)
    selection = select_releases(current.values(), include_prereleases=include_prereleases,
                                enabled_extensions=enabled_extensions)
    for selected in selection.releases:
        r = selected.release
        if type(r.id) is not int or not 1 <= r.id <= 2**63 - 1:
            raise PlanError("unrepresentable")
        for match in selected.matches:
            a = match.asset
            if type(a.id) is not int or not 1 <= a.id <= 2**63 - 1:
                raise PlanError("unrepresentable")
            AssetRecord(a.id, a.name, "a.zip", a.size, a.sha256, None, "planned")
        # Use a safe placeholder tag until the real constructor validates remote text.
        folder = recorded[r.id].folder if r.id in recorded else naming.release_folder_name("", r.id)
        ReleaseManifest(repository, r.id, r.tag_name, folder, None, None, False, None, ())
    components = naming.project_components(repository)
    owner_dir, repo_dir = components[-2:]
    project = (PureWindowsPath if sys.platform == "win32" else PurePosixPath)(*components)
    releases, proposed = [], dict(recorded)
    for selected in selection.releases:
        release = selected.release
        old_release = recorded.get(release.id)
        folder = old_release.folder if old_release else naming.release_folder_name(release.tag_name, release.id)
        old_assets = {a.asset_id: a for a in old_release.assets} if old_release else {}
        names = {}
        if selected.matches:
            limit = naming.filename_limit(archive_root, owner_dir, repo_dir, folder, prefix=components[:-2])
            names = naming.allocate_asset_names(
                {m.asset.id: m.asset.name for m in selected.matches if m.asset.id not in old_assets},
                taken={i: a.saved_name for i, a in old_assets.items()}, limit=limit)
        assets, additions = [], []
        for match in selected.matches:
            asset, notes = match.asset, []
            record = old_assets.get(asset.id)
            if record is None:
                allocated = names[asset.id]
                record = AssetRecord(asset.id, asset.name, allocated.saved_name, asset.size,
                                     asset.sha256, None, "planned")
                additions.append(record)
                if allocated.disambiguated:
                    notes.append("name_collision")
                state, action = "new", "download"
            else:
                if record.original_name != asset.name:
                    notes.append("name_changed")
                if record.size != asset.size:
                    notes.append("size_changed")
                if record.publisher_sha256 is not None and record.publisher_sha256 != asset.sha256:
                    notes.append("digest_changed")
                elif record.publisher_sha256 is None and asset.sha256 is not None:
                    notes.append("digest_added")
                changed = any(n in notes for n in ("name_changed", "size_changed", "digest_changed"))
                state = "recorded"
                action = "reconcile" if changed else "verify" if record.status == "completed" else "download"
            naming.check_path_budget(archive_root, owner_dir, repo_dir, folder, record.saved_name, prefix=components[:-2])
            assets.append(AssetPlan(asset, match.extension, record, state, action,
                                    tuple(sorted(notes)), project / folder / record.saved_name))
        selected_ids = {m.asset.id for m in selected.matches}
        skipped = []
        for asset in release.assets:
            reason = ("unavailable" if asset.state != "uploaded" else
                      "unsupported_format" if asset.supported_extension is None else
                      "format_disabled" if asset.id not in selected_ids else None)
            if reason:
                skipped.append(SkippedAsset(asset.id, asset.name, reason))
        notes = ("tag_changed",) if old_release and old_release.tag != release.tag_name else ()
        releases.append(ReleasePlan(release, "known" if old_release else "new", folder,
                                    tuple(assets), tuple(skipped), notes, selected.empty_reason))
        if additions:
            proposed[release.id] = ReleaseRecord(release.id, old_release.tag if old_release else release.tag_name,
                folder, (old_release.assets if old_release else ()) + tuple(additions))
    next_index = ProjectIndex(repository, tuple(proposed.values()))
    for selected in selection.releases:
        if selected.release.id in proposed:
            build_release_manifest(repository, selected.release, proposed[selected.release.id])
    return ArchivePlan(repository, project, index is not None, tuple(releases),
        _retained(current, recorded, selection), selection.excluded, next_index)
