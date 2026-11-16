"""Handle-anchored asset persistence on an explicitly authorized local root.

Asset publication never replaces an existing destination; temporary creation is
exclusive. Verification describes the bytes observed through one pinned read
handle at a point in time; closed files can subsequently change. File flushes
provide no directory/power-loss guarantee. Held manifests deny in-place writes.
A non-cooperating process can replace a manifest between validation and
publication, and CoveArchive may overwrite that replacement without detecting
the conflict. Unlink checks before and after publication are best-effort: they
narrow the window, are not an atomic compare-and-replace, and do not detect every
foreign replacement. This is an accepted limitation for CA-S5.1; guaranteed
compare-and-replace is not provided.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
import hashlib
import re
import sys
import time

from covearchive.github.urls import RepositoryTarget
from . import manifest as m, naming
if sys.platform == "win32":
    from . import winfs
else:
    from . import posixfs as winfs
from .manifest import AssetRecord, MAX_SIZE
ArchiveError = winfs.ArchiveError
ArchiveConflictError = winfs.ArchiveConflictError
ArchiveUnsafePathError = winfs.ArchiveUnsafePathError
ArchiveRoot, Directory = winfs.ArchiveRoot, winfs.Directory

_PROOF = object()


@dataclass(frozen=True)
class AssetOutcome:
    code: str
    local_sha256: str
    size: int


@dataclass(frozen=True)
class Verification:
    code: str
    record: AssetRecord
    local_sha256: str | None = None
    size: int | None = None
    _proof: object = field(default=None, init=False, repr=False, compare=False)


@dataclass(frozen=True)
class Recovery:
    removed: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]


@contextmanager
def ensure_project(root: ArchiveRoot, repository: RepositoryTarget) -> Iterator[Directory]:
    """Pin project ancestors, reusing ASCII case differences without renaming.

    GitHub owner/repo names are ASCII; ambiguous case-equivalent entries are
    rejected even when one matches exactly. A unique variant keeps its established
    on-disk spelling; non-ASCII names require exact spelling.
    New directories use the requested spelling. Persisted manifest spelling owns
    repository identity: callers adopt state.index.repository before committing;
    commit identity checks remain exact.
    """
    with ExitStack() as stack:
        project = root
        for component in naming.project_components(repository):
            project = stack.enter_context(project.open_dir(component, create=True, fold_case=True))
        project.role = "project"
        yield project


def ensure_release(project: Directory, folder: str, release_id: int | str) -> Directory:
    """Use the established plan folder, including its release identity."""
    winfs._platform()
    if not naming.is_release_folder(folder, release_id):
        raise ArchiveUnsafePathError("release_folder")
    directory = project.open_dir(folder, create=True)
    directory.role = "release"
    return directory


def verify_asset(directory: Directory, record: AssetRecord) -> Verification:
    """Read actual bytes from one share-read handle; JSON status is only a claim."""
    winfs._platform()
    try:
        with directory.open_file(record.saved_name) as file:
            info = file.info()
            if info.links != 1:
                return Verification("multiple_links", record, size=info.size)
            if info.size != record.size:
                return Verification("size_mismatch", record, None, info.size)
            digest, size = hashlib.sha256(), 0
            while chunk := file.read():
                digest.update(chunk)
                size += len(chunk)
            local = digest.hexdigest()
            if size != record.size or file.info().size != size:
                return Verification("size_mismatch", record, local, size)
            if ((record.status == "completed" and record.local_sha256 != local)
                    or (record.publisher_sha256 is not None and record.publisher_sha256 != local)):
                return Verification("hash_mismatch", record, local, size)
            result = Verification("verified", record, local, size)
            object.__setattr__(result, "_proof", _PROOF)
            return result
    except ArchiveError as exc:
        return Verification(exc.code, record)


def completed_record(record: AssetRecord, verification: Verification) -> AssetRecord:
    """Only a genuine, matching successful verification authorizes completion."""
    if (not isinstance(verification, Verification) or verification._proof is not _PROOF
            or verification.code != "verified" or verification.record != record
            or verification.size != record.size or verification.local_sha256 is None):
        raise ArchiveError("unverified")
    return replace(record, status="completed", local_sha256=verification.local_sha256)


def _published_verification(directory, record):
    # A competing publisher may still hold its exclusive handle after rename.
    deadline = time.monotonic() + 2
    while True:
        result = verify_asset(directory, record)
        if result.code != "busy" or time.monotonic() >= deadline:
            return result
        time.sleep(0.01)


def write_asset(directory: Directory, saved_name: str, chunks: Iterable[bytes],
                expected_size: int, publisher_sha256: str | None = None) -> AssetOutcome:
    """Stream to an exclusive temp, flush, publish without replacement, verify."""
    winfs._platform()
    winfs._component(saved_name, role=directory.role)
    if type(expected_size) is not int or not 0 <= expected_size <= MAX_SIZE:
        raise ArchiveError("size")
    if publisher_sha256 is not None and (not isinstance(publisher_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", publisher_sha256) is None):
        raise ArchiveError("hash")
    digest, size, published = hashlib.sha256(), 0, False
    with directory.create_temp() as temporary:
        for chunk in chunks:
            view = memoryview(chunk).cast("B")
            if len(view) > expected_size - size:
                raise ArchiveError("overflow")
            for offset in range(0, len(view), 1024 * 1024):
                # Copy mutable caller buffers once so hashing matches written bytes.
                piece = bytes(view[offset:offset + 1024 * 1024])
                actual = temporary.write(piece)
                digest.update(piece[:actual])
                size += actual
        if size != expected_size:
            raise ArchiveError("underflow")
        local = digest.hexdigest()
        if publisher_sha256 is not None and publisher_sha256 != local:
            raise ArchiveError("hash_mismatch")
        temporary.flush()
        claim = AssetRecord(1, saved_name, saved_name, size, local, None, "planned")
        try:
            temporary.rename(directory, saved_name)
            published = True
        except ArchiveError as exc:
            existing = _published_verification(directory, claim)
            if existing.code != "verified":
                if existing.code == "missing":
                    raise exc
                raise ArchiveConflictError(existing.code) from exc
            return AssetOutcome("already_present", local, size)
    # Reopen the published name after releasing the exclusive temp handle.
    result = _published_verification(directory, claim)
    if not published or result.code != "verified":
        raise ArchiveConflictError(result.code)
    return AssetOutcome("published", local, size)


def recover(directory: Directory) -> Recovery:
    """Delete only exact-pattern, idle, regular, single-link orphan temps."""
    winfs._platform()
    removed, skipped = [], []
    for name in sorted(directory.names()):
        if not winfs.TEMP_PATTERN.fullmatch(name):
            continue
        try:
            with directory.open_temp(name) as temporary:
                temporary.delete_temp()
            removed.append(name)
        except ArchiveError as exc:
            skipped.append((name, exc.code))
    return Recovery(tuple(removed), tuple(skipped))


@dataclass(frozen=True)
class ProjectState:
    """Loaded metadata, including detect-only unindexed manifests as untrusted claims.

    Recovered completed/local_sha256 fields prove nothing about asset bytes.
    Reconciliation requires verify_asset on actual bytes and re-committing release
    and index metadata through the normal project writer/CAS flow. Loading never
    repairs or rebuilds the index and does not authorize completion. With a valid
    index, recovered repository spelling must match exactly. Otherwise repository
    identity is an unverified claim: adopt the recovered spelling for commits;
    a case-only different spelling cannot pass the exact commit identity checks.
    Retention follows sorted folder-name scan order, results are sorted by release
    id, and duplicate ids consume no retention capacity. Only ids/folders are kept
    for overflow/duplicates; at most MAX_RELEASES parsed candidates plus the single
    iteration-local manifest are held. Later duplicates free retained slots.
    """

    index: m.ProjectIndex | None
    releases: tuple[m.ReleaseManifest, ...]
    index_sha256: str | None
    issues: tuple[tuple[str, str], ...]
    unindexed: tuple[m.ReleaseManifest, ...] = field(default=(), kw_only=True)


_WRITERS = ContextVar("archive_writers", default=())


def _identity(directory):
    info = directory.info()
    return info.volume_serial, info.file_id


@contextmanager
def project_writer(project: Directory) -> Iterator[Directory]:
    """Acquire the OS lock; context-local ownership also guards commit APIs."""
    with project.open_lock() as lock:
        token = _WRITERS.set((*_WRITERS.get(), (_identity(project), lock)))
        try:
            yield project
        finally:
            _WRITERS.reset(token)


def _require_writer(project):
    if project is not None:
        identity = _identity(project)
        for held_identity, lock in _WRITERS.get():
            if identity == held_identity:
                lock._get()
                return
    raise ArchiveError("writer_required")


def _read_handle(file, name):
    reader, cap = ((m.loads_index, m.MAX_INDEX_BYTES) if name == naming.INDEX_FILENAME
                   else (m.loads_release_manifest, m.MAX_MANIFEST_BYTES))
    if file.info().links != 1:
        raise ArchiveUnsafePathError("multiple_links")
    raw = file.read_bounded(cap)
    try:
        document = reader(raw.decode("utf-8", "strict"))
    except m.UnsupportedVersionError as exc:
        exc.document_sha256 = hashlib.sha256(raw).hexdigest()
        raise
    except (m.ManifestError, UnicodeError) as exc:
        error = winfs.ArchiveCorruptError("corrupt")
        error.document_sha256 = hashlib.sha256(raw).hexdigest()
        raise error from exc
    return document, raw


def _read_document(directory, name):
    with directory.open_file(name, metadata=True) as file:
        return _read_handle(file, name)


@contextmanager
def _manifest_destination(directory, name):
    try:
        file = directory.open_file(name, metadata=True, hold=True)
    except ArchiveError as exc:
        if exc.code != "missing":
            raise
        yield None, None, None
        return
    with file:
        document, raw = _read_handle(file, name)
        yield document, raw, file


def _issue(exc):
    return "unsupported_version" if isinstance(exc, m.UnsupportedVersionError) else exc.code


def load_project_state(project: Directory) -> ProjectState:
    """Load metadata and detect validated unindexed manifests without modifying disk.

    Unindexed manifests are untrusted claims: completed/local_sha256 fields prove
    nothing. Reconciliation requires verify_asset on actual bytes and release/index
    re-commit through the normal project writer/CAS flow before counting assets as
    verified. Nothing is written, deleted, renamed, quarantined, or rebuilt here.
    Missing/directory indexes permit scanning; corrupt/unsupported indexes return
    early without scanning. With a valid index, any repository spelling difference
    (including case only) is a consistency issue and prevents recovery. Without a
    valid index, adopt the recovered manifest's repository spelling, an unverified
    claim, for reconciliation: exact commit identity checks reject case changes.
    Retain eligible candidates in sorted folder-name scan order up to MAX_RELEASES,
    plus one iteration-local manifest; return them sorted by release id. Duplicate
    ids free retained slots for subsequent candidates and receive consistency, never
    limit. Unique overflow receives limit; only ids/folders survive for discarded
    manifests. recover() remains a separate temp-cleanup operation.
    """
    releases, issues = [], []
    try:
        index, raw = _read_document(project, naming.INDEX_FILENAME)
    except (ArchiveError, m.UnsupportedVersionError) as exc:
        if _issue(exc) in ("corrupt", "unsupported_version"):
            return ProjectState(None, (), getattr(exc, "document_sha256", None),
                                ((naming.INDEX_FILENAME, _issue(exc)),))
        index, raw = None, None
        issues.append((naming.INDEX_FILENAME, _issue(exc)))
    records = () if index is None else index.releases
    for record in records:
        try:
            with project.open_dir(record.folder) as directory:
                release, _ = _read_document(directory, naming.RELEASE_FILENAME)
            releases.append(release)
            try:
                if (release.release_id, release.folder) != (record.release_id, record.folder):
                    raise m.ManifestError("consistency")
                m.check_consistent(index, release)
            except m.ManifestError:
                issues.append((record.folder, "consistency"))
        except (ArchiveError, m.UnsupportedVersionError) as exc:
            issues.append((record.folder, _issue(exc)))
    known = {record.folder for record in records}
    indexed_ids = {record.release_id for record in records}
    candidates, seen = {}, {}
    for folder in sorted(project.names()):
        suffix = re.search(r"--([0-9]+)$", folder)
        if folder in known or suffix is None or not naming.is_release_folder(folder, int(suffix[1])):
            continue
        try:
            with project.open_dir(folder) as directory:
                release, _ = _read_document(directory, naming.RELEASE_FILENAME)
            issues.append((folder, "unindexed_release"))
            if (release.folder != folder or not naming.is_release_folder(folder, release.release_id)
                    or (index is not None and release.repository != index.repository)
                    or release.release_id in indexed_ids):
                issues.append((folder, "consistency"))
                continue
            folders = seen.setdefault(release.release_id, [])
            folders.append(folder)
            if len(folders) > 1:
                candidates.pop(release.release_id, None)
            elif len(candidates) < m.MAX_RELEASES:
                candidates[release.release_id] = release
        except (ArchiveError, m.UnsupportedVersionError) as exc:
            if exc.code != "missing":
                issues.append((folder, _issue(exc)))
    for release_id, folders in seen.items():
        if len(folders) > 1:
            issues.extend((folder, "consistency") for folder in folders)
        elif release_id not in candidates:
            issues.append((folders[0], "limit"))
    unindexed = sorted(candidates.values(), key=lambda release: (isinstance(release.release_id, str), release.release_id))
    token = None if raw is None else hashlib.sha256(raw).hexdigest()
    return ProjectState(index, tuple(releases), token, tuple(issues), unindexed=tuple(unindexed))


def _replace_document(directory, name, raw, held):
    with directory.create_temp() as temporary:
        temporary.write(raw)
        temporary.flush()
        # Best-effort pre-check; a foreign replacement can still race publication.
        if held is not None and held.unlinked():
            raise ArchiveConflictError("destination_changed")
        try:
            temporary.rename(directory, name, replace=held is not None, metadata=True)
        except ArchiveConflictError as exc:
            raise ArchiveConflictError("destination_changed") from exc
        # A detected rename-aside conflict is reported only after publication.
        # POSIX replacement can unlink held before publication and escape detection;
        # neither check provides an atomic compare-and-replace (CA-S5.1 limitation).
        if held is not None and not held.unlinked():
            raise ArchiveConflictError("destination_changed")


def commit_release(directory: Directory, release: m.ReleaseManifest) -> None:
    _require_writer(directory.parent)
    if directory.name != release.folder:
        raise ArchiveConflictError("release_folder")
    with _manifest_destination(directory, naming.RELEASE_FILENAME) as (previous, _, held):
        if previous is not None and previous.repository != release.repository:
            raise ArchiveConflictError("identity")
        _replace_document(directory, naming.RELEASE_FILENAME,
                          m.dumps_release_manifest(release).encode("utf-8"), held)


def commit_index(project: Directory, index: m.ProjectIndex,
                 expected_index_sha256: str | None) -> None:
    """Hold the validated index through CAS, release checks and final publication."""
    _require_writer(project)
    with _manifest_destination(project, naming.INDEX_FILENAME) as (previous, raw, held):
        current = None if raw is None else hashlib.sha256(raw).hexdigest()
        if current != expected_index_sha256:
            raise ArchiveConflictError("stale_index")
        if previous is not None and previous.repository != index.repository:
            raise ArchiveConflictError("identity")
        for record in index.releases:
            try:
                with project.open_dir(record.folder) as directory:
                    release, _ = _read_document(directory, naming.RELEASE_FILENAME)
                if (release.release_id, release.folder) != (record.release_id, record.folder):
                    raise m.ManifestError("consistency")
                m.check_consistent(index, release)
            except (ArchiveError, m.ManifestError) as exc:
                raise ArchiveConflictError(_issue(exc)) from exc
        _replace_document(project, naming.INDEX_FILENAME, m.dumps_index(index).encode("utf-8"), held)


def quarantine_manifest(directory: Directory, name: str) -> str:
    if name not in (naming.INDEX_FILENAME, naming.RELEASE_FILENAME):
        raise ArchiveUnsafePathError("reserved_name")
    _require_writer(directory if name == naming.INDEX_FILENAME else directory.parent)
    with directory.open_file(name, metadata=True, hold=True) as file:
        try:
            _read_handle(file, name)
        except (winfs.ArchiveCorruptError, m.UnsupportedVersionError):
            pass
        else:
            raise ArchiveConflictError("not_corrupt")
        for number in range(1, 10001):
            target = name + ".corrupt-" + str(number)
            try:
                file.rename(directory, target, metadata=True)
                return target
            except ArchiveConflictError:
                continue
    raise ArchiveConflictError("quarantine_full")
