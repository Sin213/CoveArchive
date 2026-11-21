"""Synchronous, read-only health evidence for one explicitly selected project.

Only completed INDEX records with consistent release metadata can be verified.
Codes and categories below are stable API values; messages are fixed safe text.
Counts count observations (including aggregate limit findings), before capping
the finding list. Aggregate asset_count/release_count describe skipped records.
complete describes coverage, not integrity: a fully inspected damaged asset can
be complete. Uncertain evidence, untrusted metadata, and limits make it false.

No recovery or writer API is used. Reads may update atime. POSIX project case
resolution uses non-blocking directory flocks without creating lock files.
Directory enumeration stops within the remaining entry budget plus one overflow
lookahead, including ancestor case lookup. Only the bounded sample is sorted.
Truncated samples follow filesystem order; repeated-report equality is guaranteed
only for non-truncated audits. A truncated case lookup cannot establish absence
or unique spelling, so it reports a limit even if the requested name was seen.
Pinned handles narrow TOCTOU races, but cannot prevent same-uid in-place changes.
Windows uses the shared store abstraction; native Windows validation is separate.
"""
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, fields
import re

from covearchive.github.urls import RepositoryTarget
from covearchive.providers import validate_repository
from . import manifest as m, naming, store
from .fscommon import TEMP_PATTERN


@dataclass(frozen=True)
class AuditLimits:
    """Positive budgets; asset bytes are reserved before each verification.

    max_releases bounds opened indexed/unindexed release directories together.
    max_assets bounds indexed asset attempts, including planned/skipped records.
    Metadata additionally obeys the manifest module's document/schema limits.
    """
    max_releases: int = 1_000
    max_assets: int = 10_000
    max_bytes_hashed: int = 10 * 1024**3
    max_asset_bytes: int = 2 * 1024**3
    max_metadata_bytes_read: int = 128 * 1024**2
    max_directory_entries: int = 20_000
    max_findings: int = 10_000

    def __post_init__(self):
        if any(type(getattr(self, f.name)) is not int or getattr(self, f.name) <= 0 for f in fields(self)):
            raise ValueError("Audit limits must be positive integers.")


@dataclass(frozen=True)
class Finding:
    code: str
    category: str
    message: str
    release_id: int | str | None = None
    folder: str | None = None
    asset_id: int | None = None
    saved_name: str | None = None
    asset_count: int = 0
    release_count: int = 0


@dataclass(frozen=True)
class CategoryCounts:
    verified: int = 0
    missing: int = 0
    damaged: int = 0
    unsafe: int = 0
    unverified: int = 0
    untrusted: int = 0


@dataclass(frozen=True)
class VerifiedAsset:
    release_id: int | str
    folder: str
    asset_id: int
    saved_name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class HealthReport:
    repository: RepositoryTarget
    status: str
    counts: CategoryCounts
    findings: tuple[Finding, ...]
    verified_assets: tuple[VerifiedAsset, ...]
    complete: bool
    truncated: bool
    limits: AuditLimits


# Never expose exception text or an unrecognized archive-layer code.
_FINDINGS = {
    "verified": ("verified", "Archived asset bytes match the indexed size and SHA-256."),
    "missing_root": ("missing", "The selected archive root is missing."),
    "missing_project": ("missing", "The selected project directory is missing."),
    "missing_release": ("missing", "An indexed release directory is missing."),
    "missing_release_manifest": ("missing", "Indexed release metadata is missing."),
    "missing": ("missing", "An indexed completed asset is missing."),
    "size_mismatch": ("damaged", "Archived asset size differs from the index."),
    "hash_mismatch": ("damaged", "Archived asset SHA-256 differs from the index."),
    "corrupt": ("damaged", "Archive metadata is invalid or corrupt."),
    "consistency": ("damaged", "Release metadata disagrees with the index or its folder."),
    "identity_mismatch": ("damaged", "Archive metadata belongs to a different repository identity."),
    "unsupported_version": ("unverified", "The metadata version is unsupported."),
    "unindexed_release": ("untrusted", "Unindexed release metadata is an untrusted claim; its assets were not verified."),
    "incomplete_state": ("unverified", "A leftover temporary entry indicates incomplete state; it was left untouched."),
    "not_complete": ("unverified", "The indexed asset is planned and has not completed."),
    "not_verified_limit": ("unverified", "An audit work limit prevented further inspection."),
    "findings_limit": ("unverified", "The finding limit omitted details; category counts retain all observations."),
    "unsafe_path": ("unsafe", "An archive path or object is unsafe."),
    "multiple_links": ("unsafe", "An archive file has multiple links."),
    "not_regular_file": ("unsafe", "An archive object has an unexpected filesystem type."),
    "reparse_point": ("unsafe", "An archive object is a reparse point."),
    "directory_changed": ("unsafe", "An archive object changed during inspection."),
    "case_alias": ("unsafe", "An archive name has an ambiguous or unexpected alias."),
    "busy": ("unverified", "An archive object is busy and could not be inspected."),
    "inaccessible": ("unverified", "An archive object could not be read."),
    "internal_error": ("unverified", "An unexpected failure prevented inspection."),
    "invalid_repository": ("unverified", "The requested repository identity is invalid."),
}


def _release_id(value: int | str) -> int | str:
    # GitLab release ids are tags; schema validation permits arbitrary tag text.
    return value if isinstance(value, int) or naming.is_safe_component(value) else ascii(value)[:160]


class _CappedReader:
    """Bound actual reads even if another process grows an asset while hashing.

    verify_asset checks size before and after reading this pinned handle. The
    reader stops at the indexed size; it never spends extra bytes probing EOF.
    """
    def __init__(self, file, size: int):
        self.file, self.remaining = file, size

    def info(self):
        return self.file.info()

    def read(self):
        if not self.remaining:
            return b""
        chunk = self.file.read(min(1024 * 1024, self.remaining))
        self.remaining -= len(chunk)
        return chunk


class _AssetDirectory:
    """Read-only structural adapter for store.verify_asset, not a new backend."""
    def __init__(self, directory: store.Directory, size: int):
        self.directory, self.size = directory, size

    @contextmanager
    def open_file(self, name: str):
        with self.directory.open_file(name, strict_links=True) as file:
            yield _CappedReader(file, self.size)


class _Audit:
    def __init__(self, repository: RepositoryTarget, limits: AuditLimits):
        self.repository, self.limits = repository, limits
        self.counts = {f.name: 0 for f in fields(CategoryCounts)}
        self.findings: list[Finding] = []
        self.verified: list[VerifiedAsset] = []
        self.complete, self.truncated, self.findings_capped = True, False, False
        self.metadata_used = self.bytes_reserved = self.entries_used = 0
        self.releases_used = self.assets_used = 0

    def add(self, code: str, **association):
        category, message = _FINDINGS[code]
        self.counts[category] += 1
        if category in ("unverified", "untrusted"):
            self.complete = False
        finding = Finding(code, category, message, **association)
        if not self.findings_capped and len(self.findings) < self.limits.max_findings:
            self.findings.append(finding)
        elif not self.findings_capped:
            self.findings_capped = self.truncated = True
            self.complete = False
            self.counts["unverified"] += 1
            category, message = _FINDINGS["findings_limit"]
            self.findings[-1] = Finding("findings_limit", category, message)

    def limit(self, **association):
        self.complete, self.truncated = False, True
        self.add("not_verified_limit", **association)

    def error(self, exc: Exception, *, missing="missing", **association):
        if isinstance(exc, m.UnsupportedVersionError):
            code = "unsupported_version"
        elif isinstance(exc, m.ManifestError):
            code = ("consistency" if exc.code == "consistency" else
                    "unsafe_path" if exc.code == "path" else "corrupt")
        elif isinstance(exc, UnicodeError):
            code = "corrupt"
        elif isinstance(exc, store.ArchiveError):
            code = exc.code
            if code == "limit":
                self.limit(**association)
                return
            if code == "missing":
                code = missing
            elif code == "os":
                code = "inaccessible"
            elif code == "verified" or code not in _FINDINGS:
                code = "unsafe_path" if isinstance(exc, store.ArchiveUnsafePathError) else "inaccessible"
        else:
            code = "internal_error"
        self.add(code, **association)

    def document(self, directory: store.Directory, name: str):
        """Read only a pinned, single-link document within both byte budgets."""
        reader, cap = ((m.loads_index, m.MAX_INDEX_BYTES) if name == naming.INDEX_FILENAME
                       else (m.loads_release_manifest, m.MAX_MANIFEST_BYTES))
        with directory.open_file(name, metadata=True, strict_links=True) as file:
            info = file.info()
            if info.links != 1:
                raise store.ArchiveUnsafePathError("multiple_links")
            if info.size > cap:
                raise m.ManifestError("limit")
            if info.size > self.limits.max_metadata_bytes_read - self.metadata_used:
                return None
            chunks, used = [], 0
            while used < info.size:
                chunk = file.read(min(1024 * 1024, info.size - used))
                self.metadata_used += len(chunk)
                used += len(chunk)
                if not chunk:
                    raise store.ArchiveUnsafePathError("directory_changed")
                chunks.append(chunk)
            if file.info().size != info.size:
                raise store.ArchiveUnsafePathError("directory_changed")
            return reader(b"".join(chunks).decode("utf-8", "strict"))

    def names(self, directory: store.Directory, **association) -> tuple[str, ...]:
        remaining = self.limits.max_directory_entries - self.entries_used
        if remaining <= 0:
            self.limit(**association)
            return ()
        names = directory.names(limit=remaining)
        if len(names) > remaining:
            self.limit(**association)
        selected = tuple(sorted(names[:remaining]))
        self.entries_used += len(selected)
        return selected

    def temps(self, directory: store.Directory, **association):
        for name in self.names(directory, **association):
            if TEMP_PATTERN.fullmatch(name):
                self.add("incomplete_state", saved_name=name, **association)
            elif not naming.is_safe_component(name):
                self.add("unsafe_path", **association)

    def assets(self, directory: store.Directory, record: m.ReleaseRecord):
        context = dict(release_id=_release_id(record.release_id), folder=record.folder)
        for position, asset in enumerate(record.assets):
            if self.assets_used >= min(self.limits.max_assets, m.MAX_TOTAL_ASSETS):
                self.limit(asset_count=len(record.assets) - position, **context)
                break
            self.assets_used += 1
            association = dict(**context, asset_id=asset.asset_id, saved_name=asset.saved_name)
            if asset.status != m.STATUS_COMPLETED:
                self.add("not_complete", **association)
                continue
            if (asset.size > self.limits.max_asset_bytes
                    or asset.size > self.limits.max_bytes_hashed - self.bytes_reserved):
                self.limit(asset_count=1, **association)
                continue
            self.bytes_reserved += asset.size
            try:
                result = store.verify_asset(_AssetDirectory(directory, asset.size), asset)
                if result.code == "verified":
                    self.verified.append(VerifiedAsset(context["release_id"], record.folder,
                                                       asset.asset_id, asset.saved_name,
                                                       result.local_sha256, result.size))
                    self.add("verified", **association)
                else:
                    self.error(store.ArchiveError(result.code), **association)
            except Exception as exc:
                self.error(exc, **association)

    def indexed(self, project: store.Directory, index: m.ProjectIndex):
        selected = index.releases[:min(self.limits.max_releases, m.MAX_RELEASES)]
        if len(selected) < len(index.releases):
            skipped = index.releases[len(selected):]
            self.limit(release_count=len(skipped), asset_count=sum(len(r.assets) for r in skipped))
        for record in selected:
            self.releases_used += 1
            context = dict(release_id=_release_id(record.release_id), folder=record.folder)
            missing = "missing_release"
            try:
                with project.open_dir(record.folder) as directory:
                    directory.role = "release"
                    missing = "missing_release_manifest"
                    self.temps(directory, **context)
                    doc = self.document(directory, naming.RELEASE_FILENAME)
                    if doc is None:
                        self.limit(asset_count=len(record.assets), **context)
                        continue
                    if (doc.release_id, doc.folder) != (record.release_id, record.folder):
                        raise m.ManifestError("consistency")
                    m.check_consistent(index, doc)
                    self.assets(directory, record)
            except Exception as exc:
                self.complete = False
                self.error(exc, missing=missing, **context)

    def scan(self, project: store.Directory, index: m.ProjectIndex | None):
        known = set() if index is None else {r.folder for r in index.releases}
        for folder in self.names(project):
            if TEMP_PATTERN.fullmatch(folder):
                self.add("incomplete_state", saved_name=folder)
                continue
            if not naming.is_safe_component(folder):
                self.add("unsafe_path")
                continue
            if folder in known:
                continue
            suffix = re.search(r"--([0-9]{1,20})$", folder)
            if suffix is None or not naming.is_release_folder(folder, int(suffix[1])):
                continue
            if self.releases_used >= min(self.limits.max_releases, m.MAX_RELEASES):
                self.limit(folder=folder, release_count=1)
                continue
            self.releases_used += 1
            try:
                with project.open_dir(folder) as directory:
                    directory.role = "release"
                    self.temps(directory, folder=folder)
                    doc = self.document(directory, naming.RELEASE_FILENAME)
                if doc is None:
                    self.limit(folder=folder, release_count=1)
                    continue
                context = dict(release_id=_release_id(doc.release_id), folder=folder)
                self.add("unindexed_release", asset_count=len(doc.assets), **context)
                if doc.repository != self.repository:
                    self.add("identity_mismatch", **context)
                if doc.folder != folder:
                    self.add("consistency", **context)
            except Exception as exc:
                self.complete = False
                self.error(exc, missing="missing_release_manifest", folder=folder)

    def project(self, project: store.Directory):
        try:
            index = self.document(project, naming.INDEX_FILENAME)
        except store.ArchiveError as exc:
            if exc.code != "missing":
                raise
            index = None  # An empty project need not yet have an index.
        else:
            if index is None:
                self.limit()
                return
        if index is not None:
            if index.repository != self.repository:
                self.complete = False
                self.add("identity_mismatch")
                return
            self.indexed(project, index)
        self.scan(project, index)

    def report(self) -> HealthReport:
        counts = CategoryCounts(**self.counts)
        status = ("attention" if any((counts.missing, counts.damaged, counts.unsafe, counts.untrusted))
                  else "unverified" if counts.unverified or not self.complete else "healthy")
        return HealthReport(self.repository, status, counts, tuple(self.findings),
                            tuple(self.verified), self.complete, self.truncated, self.limits)


def audit_project(root_path: str, repository: RepositoryTarget,
                  limits: AuditLimits = AuditLimits()) -> HealthReport:
    """Inspect one project's archive without creating, modifying, or recovering it.

    All filesystem operations use shared pinned-handle APIs. Invalid repository
    input and archive failures become fixed-message findings; process interrupts
    propagate. An existing empty archive/project is valid and healthy; a missing
    selected project in a populated archive is reported separately.
    """
    audit = _Audit(repository, limits)
    try:
        validate_repository(repository)
    except (ValueError, TypeError, AttributeError):
        audit.add("invalid_repository")
        return audit.report()
    missing = "missing_root"
    try:
        with ExitStack() as stack:
            directory = stack.enter_context(store.ArchiveRoot.open(root_path))
            missing = "missing_project"
            for depth, component in enumerate(naming.project_components(repository)):
                remaining = limits.max_directory_entries - audit.entries_used
                if remaining <= 0:
                    audit.limit()
                    return audit.report()
                try:
                    child = directory.open_dir(component, fold_case=True, entry_limit=remaining, wait=False)
                except store.ArchiveError as exc:
                    audit.entries_used += getattr(exc, "lookup_entries", 0)
                    if exc.code == "missing" and depth == 0 and getattr(exc, "lookup_entries", None) == 0:
                        return audit.report()  # A completed zero-entry root scan proves an empty archive.
                    raise
                audit.entries_used += child.lookup_entries
                directory = stack.enter_context(child)
            directory.role = "project"
            audit.project(directory)
    except Exception as exc:
        audit.complete = False
        audit.error(exc, missing=missing)
    return audit.report()
