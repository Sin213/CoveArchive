"""Detection-only status for one explicitly selected GitHub repository.

``assess`` is pure and accepts decoded, supervised scan results. ``check_repository``
is synchronous/blocking: run it off the GUI thread. Client budgets bound work,
not elapsed time; CA-N1c must use ScanController for hard-deadline supervision.
The default scan is anonymous, with no settings, credentials, or auth object.
Only audit_project accesses the archive; no recovery or writer API is used.

Availability, upstream lines, and local comparisons are separate evidence.
Incomplete coverage preserves positive byte evidence but cannot prove absence.
Publisher text is retained as data, excluded from repr and never used in messages.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from uuid import uuid4

from covearchive.github.models import ReleaseCatalog
from covearchive.github.urls import RepositoryTarget
from covearchive.providers import validate_repository
from covearchive.scan_worker import (
    ProtocolError, ScanFailure, ScanSuccess, decode_response, encode_error, encode_success,
)
from .health import AuditLimits, CategoryCounts, HealthReport, audit_project


@dataclass(frozen=True)
class Availability:
    state: str
    reason: str
    message: str
    listing_complete: bool = False
    retry_at: int | None = None
    retry_after: int | None = None


@dataclass(frozen=True)
class UpstreamAsset:
    asset_id: int
    size: int
    sha256: str | None


@dataclass(frozen=True)
class UpstreamRelease:
    release_id: int | str
    tag: str = field(repr=False)
    name: str | None = field(repr=False)
    published_at: str
    prerelease: bool
    total_asset_count: int
    archivable_assets: tuple[UpstreamAsset, ...]


@dataclass(frozen=True)
class Comparison:
    state: str
    message: str
    expected_assets: int = 0
    verified_assets: int = 0
    unarchived_assets: int = 0
    damaged_assets: int = 0
    missing_assets: int = 0
    local_release_id: int | str | None = None
    local_tag: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ReleaseLine:
    state: str
    release: UpstreamRelease | None
    comparison: Comparison


@dataclass(frozen=True)
class ArchiveSummary:
    status: str
    complete: bool
    truncated: bool
    counts: CategoryCounts


@dataclass(frozen=True)
class RepositoryStatus:
    repository: RepositoryTarget | None
    availability: Availability
    stable: ReleaseLine
    prerelease: ReleaseLine
    archive: ArchiveSummary | None

    @property
    def headline(self) -> str:
        state = self.availability.state
        if state == "unsupported":
            return "unsupported"
        if state == "not_checked":
            return "not_checked"
        if state in ("not_found", "forbidden"):
            return "unavailable"
        if state != "reachable" or not self.availability.listing_complete:
            return "inconclusive"
        if self.stable.state == "ambiguous":
            return "inconclusive"
        if self.stable.state == "none":
            return "no_releases"
        return {
            "verified": "up_to_date",
            "not_archived": "update_available",
            "partial": "update_available",
            "changed_upstream": "update_available",
            "tag_reused": "update_available",
            "damaged": "attention",
            "missing_files": "attention",
        }.get(self.stable.comparison.state, "inconclusive")


_AVAILABILITY_MESSAGES = {
    "reachable": "GitHub is reachable.",
    "not_found": "The repository may be deleted, private, renamed, or inaccessible.",
    "forbidden": "GitHub denied access to the repository.",
    "rate_limited": "GitHub rate limited the check.",
    "network": "A network failure prevented the GitHub check.",
    "timeout": "The GitHub check timed out.",
    "server_error": "GitHub reported a server error.",
    "invalid_response": "The GitHub scan response could not be trusted.",
    "not_checked": "The repository check was not completed.",
    "unsupported": "Availability checks currently support GitHub only.",
}
_FAILURE_STATES = {
    "not_found": "not_found", "forbidden": "forbidden", "unauthorized": "forbidden",
    "rate_limited": "rate_limited", "network": "network", "timeout": "timeout",
    "server_error": "server_error", "process": "not_checked",
    "internal_error": "not_checked", "cancelled": "not_checked",
}
_INVALID_CODES = frozenset((
    "invalid_json", "invalid_root", "too_many_items", "invalid_release",
    "response_too_large", "bad_pagination", "unsafe_redirect", "gone",
    "unexpected_status", "protocol", "repository_mismatch", "invalid_target",
    "credentialed_session", "github_api_error", "invalid_url", "unsupported_target",
    "bad_request", "result_too_large", "invalid_content_type", "asset_changed",
))
_COMPARISON_MESSAGES = {
    "verified": "Every archivable upstream asset matches verified local bytes.",
    "partial": "Some archivable assets have not been archived.",
    "missing_files": "Indexed archive files or release metadata are missing.",
    "damaged": "The audit found damaged indexed archive data.",
    "not_archived": "This upstream release has not been archived.",
    "changed_upstream": "Upstream release or asset metadata differs from the local archive.",
    "tag_reused": "This tag is indexed locally under a different release ID.",
    "no_archivable_assets": "This release has no archivable uploaded assets.",
    "unverified": "Indexed data could not be fully verified by the audit.",
    "inconclusive": "Available evidence cannot establish the archive comparison.",
    "archive_unavailable": "The archive root or trusted project index is unavailable.",
    "unsupported": "Archive comparison currently supports GitHub only.",
}


def _availability(state: str, reason: str, **kwargs) -> Availability:
    return Availability(state, reason, _AVAILABILITY_MESSAGES[state], **kwargs)


def _identity(repository: RepositoryTarget) -> tuple[str, str, str]:
    target = validate_repository(repository)
    return target.provider, target.host, target.repo_key


def _same_repository(left: RepositoryTarget, right: RepositoryTarget) -> bool:
    try:
        return _identity(left) == _identity(right)
    except (ValueError, TypeError, AttributeError):
        return False


def _safe_integer(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**63 else None


def _scan_availability(repository: RepositoryTarget,
                       result: ScanSuccess | ScanFailure | None) -> Availability:
    if result is None:
        return _availability("not_checked", "unknown")
    if isinstance(result, ScanSuccess):
        if not _same_repository(repository, result.catalog.repository):
            return _availability("invalid_response", "repository_mismatch")
        return _availability("reachable", "ok" if result.catalog.complete else "incomplete_scan",
                             listing_complete=result.catalog.complete)
    code = result.code
    detail = result.detail or {}
    if code == "incomplete_scan":
        pages = _safe_integer(detail.get("pages_completed")) or 0
        cause = detail.get("cause_code")
        reason = cause if cause in _FAILURE_STATES or cause in _INVALID_CODES else "incomplete_scan"
        return _availability("reachable" if pages else _FAILURE_STATES.get(cause, "not_checked"), reason)
    state = _FAILURE_STATES.get(code, "invalid_response")
    reason = code if code in _FAILURE_STATES or code in _INVALID_CODES else "unknown"
    if state == "rate_limited":
        return _availability(state, reason, retry_at=_safe_integer(detail.get("retry_at")),
                             retry_after=_safe_integer(detail.get("retry_after_seconds")))
    return _availability(state, reason)


def _latest(catalog: ReleaseCatalog, prerelease: bool) -> tuple[str, UpstreamRelease | None]:
    if (catalog.duplicate_release_ids
            or len({r.id for r in catalog.releases}) != len(catalog.releases)):
        return "ambiguous", None
    candidates = [r for r in catalog.releases if not r.draft and r.prerelease == prerelease]
    if not candidates:
        return "none", None
    dated = []
    for release in candidates:
        try:
            date = datetime.fromisoformat(release.published_at)
            if date.tzinfo is None or date.utcoffset() is None:
                return "ambiguous", None
        except (ValueError, TypeError, OverflowError):
            return "ambiguous", None
        dated.append((date, release))
    latest_date = max(date for date, _ in dated)
    latest = [r for date, r in dated if date == latest_date]
    if len({r.id for r in latest}) != 1:
        return "ambiguous", None
    release = latest[0]
    seen, assets = {}, []
    for asset in release.assets:
        metadata = (asset.size, asset.sha256, asset.state, asset.name)
        if asset.id in seen:
            if seen[asset.id] != metadata:
                return "ambiguous", None
            continue
        seen[asset.id] = metadata
        if asset.state == "uploaded" and asset.supported_extension is not None:
            assets.append(UpstreamAsset(asset.id, asset.size, asset.sha256))
    return "found", UpstreamRelease(release.id, release.tag_name, release.name,
                                    release.published_at, release.prerelease,
                                    len(release.assets), tuple(assets))


def _compare(repository: RepositoryTarget, upstream: UpstreamRelease | None,
             report: HealthReport | None) -> Comparison:
    def result(state: str, **kwargs) -> Comparison:
        return Comparison(state, _COMPARISON_MESSAGES[state], **kwargs)

    if upstream is None:
        return result("inconclusive")
    expected = {a.asset_id: a for a in upstream.archivable_assets}
    counts = dict(expected_assets=len(expected))
    if not expected:
        return result("no_archivable_assets")
    if report is None:
        return result("inconclusive", **counts)
    if not _same_repository(repository, report.repository):
        return result("archive_unavailable", **counts)
    if report.indexed is None:
        codes = {f.code for f in report.findings}
        if codes == {"missing_project"} or (not codes and report.complete):
            return result("not_archived", **counts, unarchived_assets=len(expected))
        return result("archive_unavailable", **counts)

    local = next((r for r in report.indexed if r.release_id == upstream.release_id), None)
    if local is None:
        reused = next((r for r in report.indexed
                       if r.tag == upstream.tag and r.release_id != upstream.release_id), None)
        if reused is not None:
            return result("tag_reused", **counts, local_release_id=reused.release_id,
                          local_tag=reused.tag)
        return result("not_archived" if report.complete else "inconclusive", **counts,
                      unarchived_assets=len(expected) if report.complete else 0)

    counts.update(local_release_id=local.release_id, local_tag=local.tag)
    if local.tag != upstream.tag:
        return result("changed_upstream", **counts)
    registered = set(local.asset_ids) & expected.keys()
    verified = {v.asset_id: v for v in report.verified_assets
                if v.release_id == local.release_id and v.folder == local.folder
                and v.asset_id in registered}
    changed = {key for key, v in verified.items() if v.size != expected[key].size
               or (expected[key].sha256 is not None and expected[key].sha256 != v.sha256)}
    good = verified.keys() - changed
    counts["verified_assets"] = len(good)
    relevant = [f for f in report.findings if f.release_id == local.release_id
                and f.folder == local.folder
                and (f.asset_id is None or f.asset_id in registered)]
    damaged, missing, planned = set(), set(), set()
    for finding in relevant:
        ids = {finding.asset_id} if finding.asset_id is not None else registered
        if finding.category == "damaged":
            damaged.update(ids)
        if finding.category == "missing":
            missing.update(ids)
        if finding.code == "not_complete":
            planned.update(ids)
    counts.update(damaged_assets=len(damaged), missing_assets=len(missing),
                  unarchived_assets=len(planned - good))
    if damaged:
        return result("damaged", **counts)
    if missing:
        return result("missing_files", **counts)
    if changed:
        return result("changed_upstream", **counts)
    absent = expected.keys() - registered
    if absent and set(local.asset_ids) - expected.keys():
        counts["unarchived_assets"] = len(absent | (planned - good))
        return result("changed_upstream", **counts)
    if len(good) == len(expected):
        return result("verified", **counts)
    # Explicit planned findings prove pending work even when coverage is incomplete.
    if not absent and good | planned == expected.keys():
        return result("partial", **counts)
    blockers = [f for f in report.findings
                if ((f.release_id == local.release_id and f.folder == local.folder)
                    or f.release_id is None)
                and f.code != "not_complete"
                and f.category in ("unverified", "unsafe", "untrusted")
                and (f.asset_id is None or f.asset_id in registered)]
    if blockers:
        return result("unverified", **counts)
    if not report.complete:
        return result("inconclusive", **counts)
    counts["unarchived_assets"] = len(expected.keys() - good)
    return result("partial", **counts)


def assess(repository: RepositoryTarget, scan_result: ScanSuccess | ScanFailure | None,
           report: HealthReport | None) -> RepositoryStatus:
    """Pure projection of validated scan metadata and read-only audit evidence."""
    try:
        validate_repository(repository)
    except (ValueError, TypeError, AttributeError):
        availability = _availability("not_checked", "invalid_repository")
        repository, report = None, None
    else:
        availability = (_availability("unsupported", "unsupported") if repository.provider != "github"
                        else _scan_availability(repository, scan_result))
    summary = (ArchiveSummary(report.status, report.complete, report.truncated, report.counts)
               if report is not None and _same_repository(repository, report.repository) else None)
    lines = []
    for prerelease in (False, True):
        state, upstream = "not_checked", None
        if availability.state == "reachable" and isinstance(scan_result, ScanSuccess):
            state, upstream = _latest(scan_result.catalog, prerelease)
        comparison = _compare(repository, upstream, report)
        if (not availability.listing_complete
                and comparison.state in ("not_archived", "partial")):
            comparison = Comparison("inconclusive", _COMPARISON_MESSAGES["inconclusive"],
                                    expected_assets=comparison.expected_assets,
                                    verified_assets=comparison.verified_assets,
                                    local_release_id=comparison.local_release_id,
                                    local_tag=comparison.local_tag)
        if availability.state == "unsupported":
            state = "unsupported"
            comparison = Comparison("unsupported", _COMPARISON_MESSAGES["unsupported"])
        lines.append(ReleaseLine(state, upstream, comparison))
    return RepositoryStatus(repository, availability, lines[0], lines[1], summary)


def _anonymous_scan(repository: RepositoryTarget) -> ScanSuccess | ScanFailure:
    from covearchive.github.client import GitHubReleaseClient

    request_id = uuid4().hex
    try:
        with GitHubReleaseClient(max_pages=10, max_requests=15, max_retries=1) as client:
            catalog = client.discover_releases(repository)
        data = encode_success(request_id, catalog)
    except Exception as exc:
        data = encode_error(request_id, exc)
    return decode_response(data, request_id)


def check_repository(root_path: str, repository: RepositoryTarget, *,
                     scan: Callable[[RepositoryTarget], ScanSuccess | ScanFailure | None] | None = None,
                     limits: AuditLimits = AuditLimits(),
                     cancelled: Callable[[], bool] | None = None) -> RepositoryStatus:
    """Blocking check; inject a decoded scan callable for offline/supervised use.

    Cancellation is cooperative before scan and before audit, not during either.
    Upstream failure still runs the local audit. Exceptions never enter status text.
    """
    try:
        validate_repository(repository)
    except (ValueError, TypeError, AttributeError):
        return assess(repository, None, None)
    if repository.provider != "github":
        return assess(repository, None, None)
    if cancelled is not None and cancelled():
        return assess(repository, ScanFailure("", "cancelled"), None)
    try:
        scan_result = (scan if scan is not None else _anonymous_scan)(repository)
    except ProtocolError:
        scan_result = ScanFailure("", "protocol")
    except Exception:
        scan_result = ScanFailure("", "internal_error")
    if cancelled is not None and cancelled():
        return assess(repository, ScanFailure("", "cancelled"), None)
    return assess(repository, scan_result, audit_project(root_path, repository, limits))
