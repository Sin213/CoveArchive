"""One selected asset, verified bytes, no-replace publication, release-first commit.

Statuses: ARCHIVED, ALREADY_ARCHIVED, VERIFIED_EXISTING, MODIFIED, CONFLICT,
MISMATCH, CANCELLED, UPSTREAM_REMOVED, RECONCILE_REQUIRED, FAILED.
Integrity is PUBLISHER_VERIFIED or LOCAL_HASH_ONLY, derived from AssetRecord.
Empty directories may remain after failure. F1 manifest replacement limitations
remain; repository identity is checked here before asset/metadata writes (F3).
This module has no queue/GUI/resume/background work or private-repository auth; the optional progress callback reports bytes received (queue lives in covearchive.queue).
"""
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import PurePath
import time

from covearchive.github.models import Release, ReleaseAsset
from covearchive.github.urls import RepositoryTarget
from covearchive.providers import validate_repository
from covearchive.transfer.resolve import DownloadError, open_asset_stream
from . import manifest as m, naming, plan, store
from .store import winfs


class ArchiveStatus(str, Enum):
    ARCHIVED = "ARCHIVED"
    ALREADY_ARCHIVED = "ALREADY_ARCHIVED"
    VERIFIED_EXISTING = "VERIFIED_EXISTING"
    MODIFIED = "MODIFIED"
    CONFLICT = "CONFLICT"
    MISMATCH = "MISMATCH"
    CANCELLED = "CANCELLED"
    UPSTREAM_REMOVED = "UPSTREAM_REMOVED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ArchiveResult:
    status: ArchiveStatus
    code: str | None
    asset_id: int
    release_id: int
    saved_name: str | None = None
    release_folder: str | None = None
    change: str | None = None
    integrity: str | None = None
    local_sha256: str | None = None
    size: int | None = None
    transferred: bool = False
    retry_after_seconds: int | None = None
    retryable: bool = False



def _ahead(state, release_id):
    """Recover only a release manifest that conservatively extends its index."""
    if state.index is None: return None
    old = next((r for r in state.index.releases if r.release_id == release_id),None)
    remote = next((r for r in state.releases if r.release_id == release_id),None)
    if old is None or remote is None or (remote.repository,remote.release_id,remote.tag,remote.folder) != (
            state.index.repository,old.release_id,old.tag,old.folder): return None
    claims = {a.asset_id:a for a in remote.assets}
    for record in old.assets:
        claim = claims.get(record.asset_id)
        if claim == record: continue
        if (claim is None or record.status != "planned" or claim.status != "completed"
                or (record.publisher_sha256 is not None and claim.publisher_sha256 != record.publisher_sha256)
                or replace(claim,status="planned",local_sha256=None,
                    publisher_sha256=record.publisher_sha256) != record): return None
        # Index values win, including an absent publisher digest. A digest added
        # by the interrupted commit must be supplied again by current metadata.
    known = {a.asset_id for a in old.assets}
    extras = tuple(replace(a,status="planned",local_sha256=None) for a in remote.assets if a.asset_id not in known)
    return replace(old,assets=old.assets+extras)



def _restorable(project, state, release_id, folder, selected_name=None):
    record = next((r for r in state.index.releases if r.release_id == release_id),None) if state.index else None
    if record is None or record.folder != folder: return False
    try:
        with project.open_dir(folder) as directory:
            directory.role = "release"
            allowed = {a.saved_name for a in record.assets}
            if selected_name is not None: allowed.add(selected_name)
            for name in directory.names():
                if winfs.TEMP_PATTERN.fullmatch(name): continue
                if name not in allowed: return False
                # Reuse handle-relative type/reparse checks; no file establishes
                # identity or integrity here. The index remains authoritative.
                with directory.open_file(name): pass
            return True
    except winfs.ArchiveError as error:
        if error.code == "missing": return True
        raise


def _state(project, repository, release_id, folder=None, selected_name=None):
    state = store.load_project_state(project)
    documents = state.releases + state.unindexed
    if ((state.index is not None and state.index.repository.repo_key != repository.repo_key)
            or any(r.repository.repo_key != repository.repo_key for r in documents)):
        raise DownloadError("repository_mismatch")
    for name, issue in state.issues:
        if name == naming.INDEX_FILENAME:
            if issue == "corrupt": raise DownloadError("manifest_corrupt")
            if issue == "unsupported_version": raise DownloadError("manifest_unsupported")
            if issue != "missing": raise DownloadError("manifest_inconsistent")
        elif name == folder and issue != "unindexed_release":
            if issue == "consistency" and _ahead(state,release_id) is not None: continue
            if issue in ("reparse_point","not_regular_file","case_alias"):
                raise winfs.ArchiveUnsafePathError(issue)
            if issue == "missing" and _restorable(project,state,release_id,folder,selected_name): continue
            raise DownloadError("manifest_inconsistent")
    spelling = state.index.repository if state.index is not None else next(
        (r.repository for r in state.unindexed if r.release_id == release_id),repository)
    return state, spelling


def _base(state, repository, release_id):
    records = list(state.index.releases) if state.index else []
    ahead = _ahead(state,release_id) if any(issue == "consistency" and name == r.folder
        for r in records if r.release_id == release_id for name,issue in state.issues) else None
    if ahead is not None: records = [ahead if r.release_id == release_id else r for r in records]
    recovered = next((r for r in state.unindexed if r.release_id == release_id),None)
    if recovered is not None:
        # Unindexed metadata is a claim: retain names and publisher digests only.
        records.append(m.ReleaseRecord(recovered.release_id,recovered.tag,recovered.folder,
            tuple(replace(a,status="planned",local_sha256=None) for a in recovered.assets)))
    return m.ProjectIndex(repository,tuple(records)), recovered is not None or ahead is not None


def _filesystem_code(error):
    if error.status in (0xc000007f,112,39): return "disk_full"
    if error.status in (0xc0000022,5): return "permission_denied"
    if error.code in ("reparse_point","not_regular_file","component","reserved_name","case_alias",
                      "root","remote_volume","filesystem","not_directory","release_folder"):
        return "unsafe_path"
    if error.code == "overflow": return "size_mismatch"
    if error.code == "underflow": return "partial_transfer"
    if error.code == "busy": return "file_busy"
    if error.code == "hash_mismatch": return "hash_mismatch"
    return "filesystem_error"


def _observed(chunks, progress, total):
    received = 0
    for chunk in chunks:
        received += len(chunk)
        progress(received,total)
        yield chunk


def archive_asset(repository, release, asset, archive_root, *, policy=None, session=None,
                  cancel=None, sleep=time.sleep, clock=time.monotonic, progress=None):
    """Archive one identity-bound asset; expected failures return URL-free results.

    Completed JSON is always reverified. Planned files without publisher hashes
    require freshly downloaded matching bytes. Publication precedes manifest commit;
    a failed commit preserves verified bytes for reconciliation on the next call.
    Cancel is honored through the last stream read/EOF check; requests during the
    final flush/rename or later commit are treated as after publication.
    """
    if (not isinstance(repository,RepositoryTarget) or not isinstance(release,Release)
            or not isinstance(asset,ReleaseAsset) or not isinstance(archive_root,(str,PurePath))):
        raise TypeError("Expected repository, release, asset and archive root.")
    result = ArchiveResult(ArchiveStatus.FAILED,None,asset.id,release.id)
    committing = False
    try:
        invalid = False
        try:
            validate_repository(repository)
        except (ValueError,TypeError,UnicodeError): invalid = True
        if invalid: raise DownloadError("invalid_asset_identity") from None
        selected = [a for a in release.assets if a.id == asset.id]
        if not selected: return replace(result,status=ArchiveStatus.UPSTREAM_REMOVED,code="asset_unavailable")
        if any(a != asset for a in selected): raise DownloadError("invalid_asset_identity")
        if release.draft: raise DownloadError("release_not_archivable")
        if asset.state != "uploaded": raise DownloadError("asset_unavailable")
        if asset.supported_extension is None: raise DownloadError("unsupported_asset")
        # Validate planning/identity before ensure_project can create directories.
        plan.plan_archive([release],archive_root=archive_root,repository=repository,include_prereleases=True)
        if cancel is not None and cancel(): raise DownloadError("cancelled")
        with winfs.ArchiveRoot.open(archive_root) as root:
            with store.ensure_project(root,repository) as project:
                state,repository = _state(project,repository,release.id)
                base,recovery = _base(state,repository,release.id)
                planned = plan.plan_archive([release],archive_root=archive_root,repository=repository,
                    index=base,include_prereleases=True)
                rp = next((r for r in planned.releases if r.release.id == release.id),None)
                ap = next((a for a in rp.assets if a.asset.id == asset.id),None) if rp else None
                if ap is None: raise DownloadError("unsupported_asset")
                state,repository = _state(project,repository,release.id,rp.folder,ap.record.saved_name)
                old_release = next((r for r in base.releases if r.release_id == release.id),None)
                change = ("MANIFEST_RECOVERY" if recovery else "NEW" if not base.releases else
                    "NEW_RELEASE" if old_release is None else
                    "NEW_ASSET" if not any(a.asset_id == asset.id for a in old_release.assets) else "NEW")
                result = replace(result,saved_name=ap.record.saved_name,release_folder=rp.folder,change=change)
                if ap.action == "reconcile":
                    return replace(result,status=ArchiveStatus.RECONCILE_REQUIRED,code="metadata_changed")
                original = ap.record
                record = (replace(original,publisher_sha256=original.publisher_sha256 or asset.sha256)
                    if original.status == "planned" else original)
                added_digest = asset.sha256 if original.publisher_sha256 is None else None
                restoring = old_release is not None and any(name == rp.folder and issue == "missing" for name,issue in state.issues)
                with store.ensure_release(project,rp.folder,release.id) as directory:
                    recovered_temps = store.recover(directory)
                    if restoring and recovered_temps.skipped:
                        code = recovered_temps.skipped[0][1]
                        if code in ("reparse_point","not_regular_file","case_alias"):
                            raise winfs.ArchiveUnsafePathError(code)
                        raise DownloadError("file_busy" if code == "busy" else "manifest_inconsistent",retryable=code == "busy")
                    if restoring:
                        if cancel is not None and cancel(): raise DownloadError("cancelled")
                        # Restore indexed claims before any new filename can be
                        # published. A crash then leaves recoverable metadata for
                        # either this asset or a sibling; bytes remain untrusted.
                        committing = True
                        with store.project_writer(project):
                            latest,repository = _state(project,repository,release.id,rp.folder,ap.record.saved_name)
                            restored,_ = _base(latest,repository,release.id)
                            if any(name == rp.folder and issue == "missing" for name,issue in latest.issues):
                                indexed = next(r for r in restored.releases if r.release_id == release.id)
                                store.commit_release(directory,m.build_release_manifest(repository,release,indexed))
                                if restored != latest.index: store.commit_index(project,restored,latest.index_sha256)
                        committing = False
                    completed, published = None, False
                    status = ArchiveStatus.ARCHIVED
                    if record.status == "completed" or record.publisher_sha256 is not None:
                        verification = store.verify_asset(directory,record)
                        if cancel is not None and cancel(): raise DownloadError("cancelled")
                        if verification.code == "verified":
                            if added_digest is not None and verification.local_sha256 != added_digest:
                                return replace(result,status=ArchiveStatus.MISMATCH,code="publisher_digest_mismatch")
                            completed = store.completed_record(record,verification)
                            if record.status == "completed" and not (restoring or recovery):
                                return replace(result,status=ArchiveStatus.VERIFIED_EXISTING,code=None,
                                    change="VERIFIED_EXISTING",local_sha256=completed.local_sha256,size=completed.size,
                                    integrity="PUBLISHER_VERIFIED" if completed.publisher_verified else "LOCAL_HASH_ONLY")
                            if restoring or recovery: result = replace(result,change="MANIFEST_RECOVERY")
                            status = ArchiveStatus.ALREADY_ARCHIVED
                        elif verification.code in ("size_mismatch","hash_mismatch","multiple_links"):
                            return replace(result,status=ArchiveStatus.MODIFIED if record.status == "completed" else ArchiveStatus.CONFLICT,
                                code=verification.code)
                        elif verification.code != "missing":
                            raise winfs.ArchiveError(verification.code)
                        elif record.status == "completed":
                            if added_digest is not None and record.local_sha256 != added_digest:
                                return replace(result,status=ArchiveStatus.MISMATCH,code="publisher_digest_mismatch")
                            result = replace(result,change="MISSING")
                    if completed is None:
                        with open_asset_stream(repository,asset,release=release,policy=policy,session=session,cancel=cancel,sleep=sleep,clock=clock) as chunks:
                            result = replace(result,transferred=True)
                            if progress is not None: chunks = _observed(chunks,progress,asset.size)
                            outcome = store.write_asset(directory,record.saved_name,chunks,record.size,
                                record.local_sha256 if record.status == "completed" else record.publisher_sha256)
                        published = outcome.code == "published"
                        if not published and cancel is not None and cancel(): raise DownloadError("cancelled")
                        # Close the L2 size-only hole: completed_record gets proof from
                        # a COMPLETED candidate with the actual local hash. Never use
                        # a planned/no-publisher verification, or fake publisher hash.
                        candidate = replace(record,status="completed",local_sha256=outcome.local_sha256)
                        verification = store.verify_asset(directory,candidate)
                        if not published and cancel is not None and cancel(): raise DownloadError("cancelled")
                        if verification.code != "verified": raise winfs.ArchiveConflictError(verification.code)
                        completed = store.completed_record(candidate,verification)
                        if outcome.code == "already_present": status = ArchiveStatus.ALREADY_ARCHIVED
                    result = replace(result,local_sha256=completed.local_sha256,size=completed.size,
                        integrity="PUBLISHER_VERIFIED" if completed.publisher_verified else "LOCAL_HASH_ONLY")
                    if not published and cancel is not None and cancel(): raise DownloadError("cancelled")
                    # Existing-file hashing is uninterruptible in the store. Adoption
                    # remains cancellable; only a real publication exempts this call.
                    committing = True
                    with store.project_writer(project):
                        latest,repository = _state(project,repository,release.id,rp.folder,ap.record.saved_name)
                        merged,_ = _base(latest,repository,release.id)
                        records = {r.release_id:r for r in merged.releases}
                        previous = records.get(release.id)
                        assets = {a.asset_id:a for a in previous.assets} if previous else {}
                        current = assets.get(asset.id)
                        if not published and cancel is not None and cancel():
                            return replace(result,status=ArchiveStatus.CANCELLED,code="cancelled")
                        needs_recovery = any(name == rp.folder and issue in ("consistency","unindexed_release","missing") for name,issue in latest.issues)
                        if not (restoring or recovery or needs_recovery) and current is not None and current.status == "completed" and current.local_sha256 == completed.local_sha256:
                            return replace(result,status=status,code=None)
                        if current is not None and current != original:
                            raise winfs.ArchiveConflictError("asset_changed")
                        assets[asset.id] = completed
                        updated = m.ReleaseRecord(release.id,previous.tag if previous else release.tag_name,
                            previous.folder if previous else rp.folder,tuple(assets.values()))
                        records[release.id] = updated
                        index = m.ProjectIndex(repository,tuple(records.values()))
                        if not published and cancel is not None and cancel():
                            return replace(result,status=ArchiveStatus.CANCELLED,code="cancelled")
                        store.commit_release(directory,m.build_release_manifest(repository,release,updated))
                        store.commit_index(project,index,latest.index_sha256)
                    return replace(result,status=status,code=None)
    except DownloadError as error:
        status = (ArchiveStatus.CANCELLED if error.code == "cancelled" else
            ArchiveStatus.MISMATCH if error.code == "size_mismatch" else
            ArchiveStatus.UPSTREAM_REMOVED if error.code == "upstream_removed" else ArchiveStatus.FAILED)
        return replace(result,status=status,code="manifest_commit_incomplete" if committing else error.code,
            retry_after_seconds=error.retry_after_seconds,retryable=committing or error.retryable)
    except (plan.PlanError,naming.PathBudgetError,m.ManifestError) as error:
        code = "path_budget" if isinstance(error,naming.PathBudgetError) else error.code
        return replace(result,status=ArchiveStatus.FAILED,code="manifest_commit_incomplete" if committing else code,retryable=committing)
    except winfs.ArchiveError as error:
        code = _filesystem_code(error)
        status = (ArchiveStatus.MISMATCH if code in ("size_mismatch","hash_mismatch") and not isinstance(error,winfs.ArchiveConflictError)
            else ArchiveStatus.CONFLICT if isinstance(error,winfs.ArchiveConflictError) and code != "unsafe_path"
            else ArchiveStatus.FAILED)
        return replace(result,status=ArchiveStatus.FAILED if committing else status,
            code="manifest_commit_incomplete" if committing else code,retryable=committing or code == "file_busy")
