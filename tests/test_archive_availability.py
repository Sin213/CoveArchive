"""Offline detection contracts for one selected repository; no archive writes."""
from dataclasses import FrozenInstanceError, replace
import builtins
import json
import shutil
import socket
import sys
from pathlib import Path

import pytest
import requests
from requests.adapters import HTTPAdapter

from covearchive.archive import availability as a, health
from covearchive.github.models import ReleaseCatalog, parse_release
from covearchive.github.client import DirectSession, GitHubReleaseClient
from covearchive.github.urls import RepositoryTarget
from covearchive import scan_worker as w
from covearchive.scan_worker import ScanFailure, ScanSuccess
from test_archive_health import DATA, REPO, SHA, TEMP, archive, asset_path, snapshot
from test_scan_worker import RID, asset, release


def upstream(*items, **changes):
    catalog = ReleaseCatalog(REPO, tuple(parse_release(item) for item in items),
                             1, 1, 0, (), "", True)
    return ScanSuccess(RID, replace(catalog, **changes))


def current(**changes):
    return release(**(dict(assets=[asset(id=1001, size=len(DATA), digest="sha256:" + SHA)])
                      | changes))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("availability tests must never use sockets")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def check(root, scan_result=None, **kwargs):
    return a.check_repository(str(root), REPO, scan=lambda repository: scan_result or upstream(current()),
                              **kwargs)


def test_verified_and_read_only(tmp_path):
    root, _, records = archive(tmp_path)
    before = snapshot(root)
    scan = lambda repository: upstream(current())
    result = a.check_repository(str(root), REPO, scan=scan)
    assert result.availability.state == "reachable"
    assert result.stable.state == "found"
    assert result.stable.comparison.state == "verified"
    assert result.stable.comparison.verified_assets == 1
    assert result.headline == "up_to_date"
    assert result == a.check_repository(str(root), REPO, scan=scan)
    assert snapshot(root) == before
    report = health.audit_project(str(root), REPO)
    assert report.indexed == (health.IndexedRelease(1, "v1", records[0].folder, (1001,)),)


@pytest.mark.parametrize("code,state,headline", [
    ("not_found", "not_found", "unavailable"), ("forbidden", "forbidden", "unavailable"),
    ("unauthorized", "forbidden", "unavailable"), ("rate_limited", "rate_limited", "inconclusive"),
    ("network", "network", "inconclusive"), ("timeout", "timeout", "inconclusive"),
    ("server_error", "server_error", "inconclusive"),
    *[(code, "invalid_response", "inconclusive") for code in (
        "invalid_json", "invalid_root", "too_many_items", "invalid_release",
        "response_too_large", "bad_pagination", "unsafe_redirect", "gone",
        "unexpected_status", "protocol", "repository_mismatch", "invalid_target",
        "credentialed_session", "github_api_error", "invalid_url", "unsupported_target",
        "bad_request", "result_too_large", "invalid_content_type", "asset_changed")],
    ("unrecognized-secret", "invalid_response", "inconclusive"),
    ("process", "not_checked", "not_checked"), ("internal_error", "not_checked", "not_checked"),
    ("cancelled", "not_checked", "not_checked"),
])
def test_failure_table_and_audit_always_runs(tmp_path, code, state, headline):
    root, _, _ = archive(tmp_path)
    result = check(root, ScanFailure(RID, code, message="https://user:SECRET@host/token",
                                     detail={"stderr": "SECRET"}))
    assert result.availability.state == state
    assert result.headline == headline
    assert result.archive.counts.verified == 1
    assert "SECRET" not in repr(result) and "unrecognized-secret" not in repr(result)
    if code == "not_found":
        assert all(word in result.availability.message for word in
                   ("deleted", "private", "renamed", "inaccessible"))


def test_rate_limit_details_are_bounded_safe_numbers():
    result = a.assess(REPO, ScanFailure(RID, "rate_limited", detail={
        "retry_at": 1005, "retry_after_seconds": 5}), None)
    assert (result.availability.retry_at, result.availability.retry_after) == (1005, 5)
    hostile = a.assess(REPO, ScanFailure(RID, "rate_limited", detail={
        "retry_at": "SECRET", "retry_after_seconds": True}), None)
    assert hostile.availability.retry_at is hostile.availability.retry_after is None
    assert "SECRET" not in repr(hostile)


@pytest.mark.parametrize("pages,state", [(0, "network"), (1, "reachable"), (9, "reachable")])
def test_incomplete_scan_has_no_negative_claim(tmp_path, pages, state):
    result = check(tmp_path, ScanFailure(RID, "incomplete_scan", detail={
        "pages_completed": pages, "reason": "SECRET", "cause_code": "network"}))
    assert result.availability.state == state
    assert result.availability.reason == "network"
    assert not result.availability.listing_complete
    assert result.stable.release is None and result.stable.comparison.state == "inconclusive"
    assert result.headline == "inconclusive" and "SECRET" not in repr(result)


@pytest.mark.parametrize("cause,state,reason", [
    ("not_found", "not_found", "not_found"), ("forbidden", "forbidden", "forbidden"),
    ("unauthorized", "forbidden", "unauthorized"), ("rate_limited", "rate_limited", "rate_limited"),
    ("network", "network", "network"), ("timeout", "timeout", "timeout"),
    ("server_error", "server_error", "server_error"), ("process", "not_checked", "process"),
    ("internal_error", "not_checked", "internal_error"), ("cancelled", "not_checked", "cancelled"),
    ("protocol", "not_checked", "protocol"), (None, "not_checked", "incomplete_scan"),
    ("SECRET", "not_checked", "incomplete_scan"),
])
def test_zero_page_incomplete_scan_uses_cause_state(cause, state, reason):
    result = a.assess(REPO, ScanFailure(RID, "incomplete_scan", detail={
        "pages_completed": 0, "reason": "SECRET", "cause_code": cause}), None)
    assert result.availability.state == state
    assert result.availability.reason == reason and not result.availability.listing_complete
    assert result.stable.release is None and "SECRET" not in repr(result)


def test_scan_none_and_missing_audit():
    result = a.assess(REPO, None, None)
    assert (result.availability.state, result.availability.reason) == ("not_checked", "unknown")
    assert result.archive is None and result.headline == "not_checked"
    assert a.assess(REPO, upstream(current()), None).stable.comparison.state == "inconclusive"


def test_pure_assess_does_no_io(tmp_path, monkeypatch):
    root, _, _ = archive(tmp_path)
    report = health.audit_project(str(root), REPO)
    scan = upstream(current())
    def denied(*args, **kwargs):
        pytest.fail("assess must be pure")
    monkeypatch.setattr(builtins, "open", denied)
    monkeypatch.setattr(a, "audit_project", denied)
    monkeypatch.setattr(a, "_anonymous_scan", denied)
    assert a.assess(REPO, scan, report).headline == "up_to_date"


def test_stable_and_prerelease_rank_only_publication_dates(tmp_path):
    scan = upstream(current(id=9, tag_name="v9", published_at="2026-11-13T12:00:00Z"),
                    current(id=10, tag_name="v10", published_at="2026-11-12T12:00:00Z"),
                    current(id=20, tag_name="nightly", prerelease=True,
                            published_at="2026-11-14T12:00:00+00:00"),
                    current(id=21, tag_name="alpha", prerelease=True,
                            published_at="2026-11-15T01:00:00+14:00"),
                    current(id=30, draft=True, published_at="2027-01-01T00:00:00Z"))
    result = check(tmp_path, scan)
    assert result.stable.release.release_id == 9
    assert result.prerelease.release.release_id == 20
    assert result.prerelease.comparison.state == "not_archived"
    assert result.headline == "update_available"


def test_prerelease_never_drives_headline(tmp_path):
    root, _, _ = archive(tmp_path)
    result = check(root, upstream(current(), current(id=2, prerelease=True)))
    assert result.prerelease.comparison.state == "not_archived"
    assert result.stable.comparison.state == "verified" and result.headline == "up_to_date"


@pytest.mark.parametrize("items", [(), (current(draft=True),), (current(prerelease=True),)])
def test_no_stable_releases_is_not_disappearance(tmp_path, items):
    result = check(tmp_path, upstream(*items))
    assert result.availability.state == "reachable" and result.stable.state == "none"
    assert result.headline == "no_releases"


def test_upstream_newer_than_local(tmp_path):
    root, _, _ = archive(tmp_path)
    result = check(root, upstream(current(), current(id=2, published_at="2027-01-01T00:00:00Z")))
    comparison = result.stable.comparison
    assert comparison.state == "not_archived" and comparison.unarchived_assets == 1
    assert comparison.local_release_id is None and result.headline == "update_available"


@pytest.mark.parametrize("planned", [False, True])
def test_partial_absent_or_planned_asset(tmp_path, planned):
    root, _, _ = archive(tmp_path, planned=planned)
    releases = upstream(current()) if planned else upstream(current(assets=[
        asset(id=1001, size=len(DATA)), asset(id=1002, size=len(DATA))]))
    result = check(root, releases)
    comparison = result.stable.comparison
    assert comparison.state == "partial" and result.headline == "update_available"
    assert comparison.expected_assets == (1 if planned else 2)
    assert comparison.verified_assets == (0 if planned else 1)
    assert comparison.unarchived_assets == 1


@pytest.mark.parametrize("damage,state", [("missing", "missing_files"), ("hash", "damaged"),
                                          ("size", "damaged")])
def test_local_damage(tmp_path, damage, state):
    root, project, records = archive(tmp_path)
    path = asset_path(project, records)
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"Payload" if damage == "hash" else b"short")
    result = check(root)
    assert result.stable.comparison.state == state and result.headline == "attention"
    assert result.stable.comparison.local_release_id == 1
    assert result.stable.comparison.local_tag == "v1"
    assert result.stable.comparison.missing_assets == (1 if damage == "missing" else 0)
    assert result.stable.comparison.damaged_assets == (0 if damage == "missing" else 1)


@pytest.mark.parametrize("target", ["folder", "manifest"])
def test_missing_release_metadata_is_positive_evidence(tmp_path, target):
    root, project, records = archive(tmp_path)
    directory = project / records[0].folder
    (directory / health.naming.RELEASE_FILENAME).unlink()
    if target == "folder":
        asset_path(project, records).unlink()
        directory.rmdir()
    result = check(root)
    assert result.stable.comparison.state == "missing_files"
    assert result.stable.comparison.missing_assets == 1 and not result.archive.complete


@pytest.mark.parametrize("target", ["index", "release"])
@pytest.mark.parametrize("damage", ["corrupt", "unsupported"])
def test_untrusted_metadata(tmp_path, target, damage):
    root, project, records = archive(tmp_path)
    path = (project / health.naming.INDEX_FILENAME if target == "index" else
            project / records[0].folder / health.naming.RELEASE_FILENAME)
    content = json.loads(path.read_text())
    content["schema_version"] = 999
    path.write_text("{broken" if damage == "corrupt" else json.dumps(content))
    result = check(root)
    expected = "archive_unavailable" if target == "index" else (
        "damaged" if damage == "corrupt" else "unverified")
    assert result.stable.comparison.state == expected
    assert not result.archive.complete


def test_missing_archive_root(tmp_path):
    result = check(tmp_path / "missing")
    assert result.stable.comparison.state == "archive_unavailable"
    assert result.archive.counts.missing == 1 and result.headline == "inconclusive"


def test_empty_missing_project_and_absent_index(tmp_path):
    assert check(tmp_path).stable.comparison.state == "not_archived"
    (tmp_path / "unrelated").mkdir()
    assert check(tmp_path).stable.comparison.state == "not_archived"
    root, _, _ = archive(tmp_path, releases=0, indexed=False)
    assert check(root).stable.comparison.state == "not_archived"


def test_unindexed_release_is_not_local_proof(tmp_path):
    root, _, _ = archive(tmp_path, indexed=False)
    assert check(root).stable.comparison.state == "archive_unavailable"


@pytest.mark.parametrize("indexed_state,comparison_state,headline", [
    ("healthy", "verified", "up_to_date"),
    ("damaged", "damaged", "attention"),
    ("missing", "missing_files", "attention"),
])
def test_unindexed_copy_cannot_override_indexed_folder(tmp_path, indexed_state,
                                                       comparison_state, headline):
    root, project, records = archive(tmp_path)
    backup = project / "release-backup--1"
    shutil.copytree(project / records[0].folder, backup)
    path = asset_path(project, records)
    if indexed_state == "damaged":
        path.write_bytes(b"Payload")
    elif indexed_state == "missing":
        path.unlink()
    before = snapshot(root)
    report = health.audit_project(str(root), REPO)
    copy_findings = [f for f in report.findings if f.folder == backup.name]
    assert {f.code for f in copy_findings} == {"consistency", "unindexed_release"}
    assert all(f.release_id == 1 for f in copy_findings)
    result = check(root)
    comparison = result.stable.comparison
    assert comparison.state == comparison_state and result.headline == headline
    assert comparison.verified_assets == (1 if indexed_state == "healthy" else 0)
    assert comparison.damaged_assets == (1 if indexed_state == "damaged" else 0)
    assert comparison.missing_assets == (1 if indexed_state == "missing" else 0)
    assert result.archive.status == report.status == "attention"
    assert result.archive.complete == report.complete == False
    assert result.archive.counts == report.counts
    assert result.archive.counts.untrusted == 1
    assert result.archive.counts.damaged == (2 if indexed_state == "damaged" else 1)
    assert snapshot(root) == before


def test_unindexed_copy_cannot_verify_unarchived_release(tmp_path):
    root, project, _ = archive(tmp_path, releases=0)
    assert check(root).stable.comparison.state == "not_archived"
    _, source, records = archive(tmp_path / "source")
    shutil.copytree(source / records[0].folder, project / "release-backup--1")
    result = check(root)
    assert result.stable.comparison.state == "inconclusive"
    assert result.stable.comparison.verified_assets == 0
    assert result.archive.counts.verified == 0 and result.archive.counts.untrusted == 1
    assert not result.archive.complete and result.archive.status == "attention"


def test_verified_evidence_from_other_folder_cannot_verify_indexed_release(tmp_path):
    root, _, _ = archive(tmp_path)
    report = health.audit_project(str(root), REPO)
    foreign = replace(report.verified_assets[0], folder="release-backup--1")
    report = replace(report, verified_assets=(foreign,), findings=(), complete=False)
    result = a.assess(REPO, upstream(current()), report)
    assert result.stable.comparison.state == "inconclusive"
    assert result.stable.comparison.verified_assets == 0


def test_index_lists_all_releases_beyond_audit_limit(tmp_path):
    root, _, records = archive(tmp_path, releases=3)
    report = health.audit_project(str(root), REPO, health.AuditLimits(max_releases=1))
    assert tuple(r.release_id for r in report.indexed) == (1, 2, 3)
    assert tuple(r.asset_ids for r in report.indexed) == ((1001,), (2001,), (3001,))
    assert report.indexed[-1].tag == records[-1].tag
    result = a.assess(REPO, upstream(current(id=4)), report)
    assert result.stable.comparison.state == "inconclusive"
    assert result.headline == "inconclusive"


def test_index_project_level_limit_and_identity_are_unavailable(tmp_path):
    root, project, records = archive(tmp_path)
    limited = check(root, limits=health.AuditLimits(max_metadata_bytes_read=1))
    assert limited.stable.comparison.state == "archive_unavailable"
    m = health.m
    (project / health.naming.INDEX_FILENAME).write_text(m.dumps_index(
        m.ProjectIndex(RepositoryTarget("Other", "Repo"), records)))
    report = health.audit_project(str(root), REPO)
    assert report.indexed is None
    assert check(root).stable.comparison.state == "archive_unavailable"


def test_indexed_audit_limits_are_unverified(tmp_path):
    root, _, _ = archive(tmp_path)
    result = check(root, limits=health.AuditLimits(max_asset_bytes=1))
    assert result.stable.comparison.state == "unverified"
    assert result.headline == "inconclusive" and result.archive.truncated


@pytest.mark.parametrize("code", ["unsupported_version", "busy", "inaccessible",
                                  "internal_error", "incomplete_state", "not_verified_limit"])
@pytest.mark.parametrize("association", ["project", "release", "unrelated_release", "unrelated_asset",
                                         "unrelated_folder"])
def test_unverified_findings_are_attributed(tmp_path, code, association):
    root, _, records = archive(tmp_path)
    report = health.audit_project(str(root), REPO)
    context = {"release_id": 2 if association == "unrelated_release" else 1,
               "folder": "release-backup--1" if association == "unrelated_folder" else records[0].folder}
    if association == "project":
        context = {}
    if association == "unrelated_asset":
        context["asset_id"] = 9999
    report = replace(report, complete=False, verified_assets=(),
                     findings=(health.Finding(code, "unverified", "Fixed audit message.", **context),))
    result = a.assess(REPO, upstream(current()), report)
    assert result.stable.comparison.state == (
        "unverified" if association in ("project", "release") else "inconclusive")


def test_planned_and_verified_assets_share_partial_counts(tmp_path):
    root, project, records = archive(tmp_path, assets=2)
    record = records[0]
    planned = replace(record.assets[1], status="planned", local_sha256=None)
    assets = (record.assets[0], planned)
    index = health.m.ProjectIndex(REPO, (replace(record, assets=assets),))
    (project / health.naming.INDEX_FILENAME).write_text(health.m.dumps_index(index))
    path = project / record.folder / health.naming.RELEASE_FILENAME
    manifest = health.m.loads_release_manifest(path.read_text())
    path.write_text(health.m.dumps_release_manifest(replace(manifest, assets=assets)))
    result = check(root, upstream(current(assets=[asset(id=1001, size=len(DATA)),
                                                   asset(id=1002, size=len(DATA))])))
    comparison = result.stable.comparison
    assert comparison.state == "partial" and not result.archive.complete
    assert (comparison.expected_assets, comparison.verified_assets, comparison.unarchived_assets) == (2, 1, 1)


def test_only_matching_release_assets_affect_comparison(tmp_path):
    root, project, records = archive(tmp_path, releases=2)
    asset_path(project, records[1:]).write_bytes(b"Payload")
    result = check(root)
    assert result.archive.counts.damaged == 1
    assert result.stable.comparison.state == "verified" and result.headline == "up_to_date"


@pytest.mark.parametrize("state", ["verified", "missing_files", "damaged"])
def test_positive_evidence_survives_unrelated_incompleteness(tmp_path, state):
    root, project, records = archive(tmp_path, releases=2)
    if state == "missing_files":
        asset_path(project, records).unlink()
    if state == "damaged":
        asset_path(project, records).write_bytes(b"Payload")
    result = check(root, limits=health.AuditLimits(max_releases=1))
    assert not result.archive.complete
    assert result.stable.comparison.state == state


def test_absence_cannot_be_partial_under_incomplete_audit(tmp_path):
    root, project, _ = archive(tmp_path)
    (project / TEMP).write_bytes(b"temporary")
    result = check(root, upstream(current(assets=[asset(id=1001, size=len(DATA)), asset(id=1002)])))
    assert result.stable.comparison.state == "unverified"
    # No finding association establishing a blocker: still cannot infer absence.
    report = replace(health.audit_project(str(root), REPO), findings=())
    assert a.assess(REPO, upstream(current(id=2)), report).stable.comparison.state == "inconclusive"


def test_same_tag_new_id_is_never_merged(tmp_path):
    root, _, _ = archive(tmp_path)
    before = snapshot(root)
    result = check(root, upstream(current(id=2, tag_name="v1")))
    comparison = result.stable.comparison
    assert comparison.state == "tag_reused"
    assert (comparison.local_release_id, comparison.local_tag) == (1, "v1")
    assert comparison.verified_assets == 0 and result.headline == "update_available"
    assert snapshot(root) == before


@pytest.mark.parametrize("change", ["size", "digest"])
def test_same_id_changed_upstream_asset(tmp_path, change):
    root, _, _ = archive(tmp_path)
    modified = asset(id=1001, size=len(DATA) + (1 if change == "size" else 0),
                     digest="sha256:" + ("0" * 64 if change == "digest" else SHA))
    result = check(root, upstream(current(assets=[modified])))
    assert result.stable.comparison.state == "changed_upstream"
    assert result.stable.comparison.verified_assets == 0
    assert result.headline == "update_available"


def test_same_id_changed_tag_cannot_verify(tmp_path):
    root, _, _ = archive(tmp_path)
    result = check(root, upstream(current(tag_name="v2")))
    assert result.stable.comparison.state == "changed_upstream"
    assert result.stable.comparison.local_release_id == 1
    assert result.stable.comparison.local_tag == "v1"
    assert result.headline == "update_available"


@pytest.mark.parametrize("local_assets,upstream_ids,state,verified,unarchived", [
    (1, (1002,), "changed_upstream", 0, 1),
    (2, (1002, 1003), "changed_upstream", 1, 1),
    (1, (1001, 1002), "partial", 1, 1),
    (2, (1001,), "verified", 1, 0),
])
def test_asset_replacement_addition_and_removal(tmp_path, local_assets, upstream_ids,
                                               state, verified, unarchived):
    root, _, _ = archive(tmp_path, assets=local_assets)
    before = snapshot(root)
    result = check(root, upstream(current(assets=[asset(id=id, size=len(DATA)) for id in upstream_ids])))
    comparison = result.stable.comparison
    assert comparison.state == state
    assert comparison.expected_assets == len(upstream_ids)
    assert (comparison.verified_assets, comparison.unarchived_assets) == (verified, unarchived)
    assert result.headline == ("up_to_date" if state == "verified" else "update_available")
    assert snapshot(root) == before


def test_scan_identity_mismatch_and_casefold_match(tmp_path):
    root, _, _ = archive(tmp_path)
    bad = upstream(current(), repository=RepositoryTarget("Other", "Repo"))
    result = check(root, bad)
    assert result.availability.state == "invalid_response"
    assert result.availability.reason == "repository_mismatch"
    assert result.stable.release is None and result.headline == "inconclusive"
    matching = upstream(current(), repository=RepositoryTarget("owner", "repo"))
    assert check(root, matching).headline == "up_to_date"
    foreign = replace(health.audit_project(str(root), REPO), repository=RepositoryTarget("Other", "Repo"))
    assert a.assess(REPO, upstream(current()), foreign).stable.comparison.state == "archive_unavailable"


@pytest.mark.parametrize("assets", [[], [asset(name="unsupported.txt")], [asset(state="new")]])
def test_zero_archivable_is_never_verified(tmp_path, assets):
    root, _, _ = archive(tmp_path)
    result = check(root, upstream(current(assets=assets)))
    assert result.stable.comparison.state == "no_archivable_assets"
    assert result.stable.comparison.expected_assets == 0
    assert result.headline == "inconclusive"


def test_supported_assets_deduplicate_before_filtering(tmp_path):
    root, _, _ = archive(tmp_path)
    result = check(root, upstream(current(assets=[asset(id=1001, size=len(DATA)),
        asset(id=1001, size=len(DATA)), asset(id=1002, name="readme.txt"),
        asset(id=1003, state="new"), asset(id=1003, state="new")])))
    assert result.stable.release.total_asset_count == 5
    assert len(result.stable.release.archivable_assets) == 1
    assert result.stable.comparison.state == "verified"


@pytest.mark.parametrize("change", [dict(size=999), dict(digest="sha256:" + "0" * 64),
                                    dict(state="new"), dict(name="different.zip")])
@pytest.mark.parametrize("prerelease", [False, True])
def test_conflicting_asset_duplicates_are_ambiguous(tmp_path, change, prerelease):
    root, _, _ = archive(tmp_path)
    original = asset(id=1001, size=len(DATA), digest="sha256:" + SHA)
    conflicting = original | change
    source = current(id=2 if prerelease else 1, prerelease=prerelease, assets=[original, conflicting])
    result = check(root, upstream(current(), source) if prerelease else upstream(source))
    line = result.prerelease if prerelease else result.stable
    assert line.state == "ambiguous" and line.release is None
    assert line.comparison.state == "inconclusive" and line.comparison.verified_assets == 0
    if prerelease:
        assert result.stable.comparison.state == "verified" and result.headline == "up_to_date"


@pytest.mark.parametrize("changes", [{}, {"tag_name": "reused"},
    {"published_at": "2026-01-01T00:00:00Z"}, {"prerelease": True}])
def test_duplicate_release_records_without_catalog_flag_are_ambiguous(tmp_path, changes):
    root, _, _ = archive(tmp_path)
    result = check(root, upstream(current(), current(**changes)))
    assert result.stable.state == "ambiguous" and result.stable.release is None
    assert result.stable.comparison.state == "inconclusive"
    assert result.stable.comparison.verified_assets == 0 and result.headline == "inconclusive"


@pytest.mark.parametrize("published_at", [None, "2026-01-01T00:00:00", "bad-date"])
def test_undated_candidate_makes_its_line_ambiguous(tmp_path, published_at):
    other = replace(parse_release(current(id=2)), published_at=published_at)
    scan = upstream(current())
    scan = replace(scan, catalog=replace(scan.catalog, releases=scan.catalog.releases + (other,)))
    result = check(tmp_path, scan)
    assert result.stable.state == "ambiguous" and result.stable.release is None
    assert result.headline == "inconclusive"


def test_equal_dates_different_ids_are_ambiguous(tmp_path):
    result = check(tmp_path, upstream(current(), current(id=2)))
    assert result.stable.state == "ambiguous" and result.headline == "inconclusive"


def test_duplicate_release_ids_make_whole_listing_ambiguous(tmp_path):
    result = check(tmp_path, upstream(current(), duplicate_release_ids=(1,)))
    assert result.stable.state == result.prerelease.state == "ambiguous"


def test_prerelease_ambiguity_is_independent(tmp_path):
    root, _, _ = archive(tmp_path)
    result = check(root, upstream(current(), current(id=2, prerelease=True, published_at=None)))
    assert result.prerelease.state == "ambiguous" and result.headline == "up_to_date"


@pytest.mark.parametrize("provider,host", [("codeberg", "codeberg.org"), ("gitlab", "gitlab.com")])
def test_unsupported_provider_does_no_io(monkeypatch, provider, host):
    def denied(*args, **kwargs):
        pytest.fail("unsupported provider must not scan or audit")
    monkeypatch.setattr(a, "audit_project", denied)
    result = a.check_repository("/absent", RepositoryTarget("Owner", "Repo", provider, host), scan=denied)
    assert result.availability.state == result.stable.comparison.state == "unsupported"
    assert result.prerelease.comparison.state == result.headline == "unsupported"
    assert result.archive is None


@pytest.mark.parametrize("repository", [None, "https://user:SECRET@github.com/O/R",
    RepositoryTarget("user:SECRET@host", "Repo"), RepositoryTarget("Owner", "Repo", host="bad")])
def test_invalid_repository_does_no_io(monkeypatch, repository):
    def denied(*args, **kwargs):
        pytest.fail("invalid repository must not scan or audit")
    monkeypatch.setattr(a, "audit_project", denied)
    result = a.check_repository("/absent", repository, scan=denied)
    assert result.repository is None and result.headline == "not_checked"
    assert result.availability.reason == "invalid_repository" and "SECRET" not in repr(result)


@pytest.mark.parametrize("before_scan", [True, False])
def test_cancellation_boundaries(monkeypatch, before_scan):
    calls = []
    def scan(repository):
        calls.append("scan")
        return upstream(current())
    checks = iter([before_scan, True])
    monkeypatch.setattr(a, "audit_project", lambda *args: pytest.fail("audit after cancellation"))
    result = a.check_repository("/absent", REPO, scan=scan, cancelled=lambda: next(checks))
    assert calls == ([] if before_scan else ["scan"])
    assert result.availability.reason == "cancelled" and result.headline == "not_checked"


def test_scan_exception_and_protocol_failure_still_audit(tmp_path):
    for exception, state in [(RuntimeError("SECRET"), "not_checked"),
                             (w.ProtocolError("SECRET"), "invalid_response")]:
        def scan(repository):
            raise exception
        result = a.check_repository(str(tmp_path), REPO, scan=scan)
        assert result.availability.state == state
        assert result.archive.complete and "SECRET" not in repr(result)


def test_incomplete_success_cannot_make_latest_or_absence_claim(tmp_path):
    result = check(tmp_path, upstream(current(), complete=False))
    assert result.availability.state == "reachable" and not result.availability.listing_complete
    assert result.headline == result.stable.comparison.state == "inconclusive"


def test_anonymous_scan_and_decode_with_fake_client(tmp_path, monkeypatch):
    from covearchive.github import client as client_module
    calls = []
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def discover_releases(self, repository):
            calls.append(repository)
            return upstream(current()).catalog
    monkeypatch.setattr(client_module, "GitHubReleaseClient", lambda **kwargs: Client())
    root, _, _ = archive(tmp_path)
    assert a.check_repository(str(root), REPO).headline == "up_to_date"
    assert calls == [REPO]


@pytest.mark.parametrize("event,state", [(200, "reachable"), (404, "not_found"),
    (403, "forbidden"), (429, "rate_limited"), (503, "server_error"),
    (requests.Timeout("SECRET"), "timeout"), (requests.ConnectionError("SECRET"), "network")])
def test_default_bounded_anonymous_scan_over_direct_session(tmp_path, monkeypatch, event, state):
    from covearchive.github import client as client_module
    original = client_module.GitHubReleaseClient
    calls, options = [], []
    token = "ghp_" + "A" * 36
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setenv("GH_TOKEN", token)
    class Adapter(HTTPAdapter):
        def send(self, request, **kwargs):
            calls.append(request)
            assert "Authorization" not in request.headers
            assert "Cookie" not in request.headers
            if isinstance(event, Exception):
                raise event
            response = requests.Response()
            response.status_code = event
            response.headers = {"Content-Type": "application/json"}
            if event == 429:
                response.headers["Retry-After"] = "5"
            response._content = json.dumps([current()] if event == 200 else {}).encode()
            response._content_consumed = True
            response.request, response.url = request, request.url
            return response
    def factory(**kwargs):
        options.append(kwargs)
        session = DirectSession()
        session.headers["Authorization"] = "Bearer " + token
        session.mount("https://", Adapter())
        client = original(session, clock=lambda: 1000, sleep=lambda _: None, **kwargs)
        assert client.auth is None
        return client
    monkeypatch.setattr(client_module, "GitHubReleaseClient", factory)
    root, _, _ = archive(tmp_path)
    result = a.check_repository(str(root), REPO)
    assert result.availability.state == state and result.archive.counts.verified == 1
    assert options == [dict(max_pages=10, max_requests=15, max_retries=1)]
    assert len(calls) == (2 if state in ("server_error", "timeout", "network") else 1)
    assert token not in repr(result) and "SECRET" not in repr(result)
    if event == 200:
        assert result.headline == "up_to_date"
    if event == 429:
        assert result.availability.retry_at == 1005 and result.availability.retry_after == 5


def test_default_scan_revalidates_dumped_response(tmp_path, monkeypatch):
    from covearchive.github import client as client_module
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def discover_releases(self, repository): return upstream(current()).catalog
    monkeypatch.setattr(client_module, "GitHubReleaseClient", lambda **kwargs: Client())
    def invalid(*args, **kwargs):
        return b'{"SECRET":"not a valid envelope"}\n'
    monkeypatch.setattr(a, "encode_success", invalid)
    result = a.check_repository(str(tmp_path), REPO)
    assert result.availability.state == "invalid_response" and "SECRET" not in repr(result)


@pytest.mark.parametrize("failure_at", ["factory", "transport", "ca_bundle"])
def test_anonymous_scan_exception_never_prints_credentials(tmp_path, monkeypatch, capfd, failure_at):
    from covearchive.github import client as client_module
    secret_url = "https://user:SECRET@host/token"
    original = client_module.GitHubReleaseClient
    class Adapter(HTTPAdapter):
        def send(self, request, **kwargs):
            raise RuntimeError(secret_url)
    def factory(**kwargs):
        if failure_at == "factory":
            raise RuntimeError(secret_url)
        session = DirectSession()
        session.mount("https://", Adapter())
        return original(session, **kwargs)
    if failure_at == "ca_bundle":
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/nonexistent/" + secret_url)
    else:
        monkeypatch.setattr(client_module, "GitHubReleaseClient", factory)
    root, _, _ = archive(tmp_path)
    result = a.check_repository(str(root), REPO)
    captured = capfd.readouterr()
    assert captured.out == captured.err == ""
    assert result.availability.state == "not_checked" and result.availability.reason == "internal_error"
    assert result.archive.counts.verified == 1
    assert "SECRET" not in repr(result) and secret_url not in repr(result)


def test_publisher_text_and_download_credentials_do_not_enter_repr(tmp_path):
    root, _, _ = archive(tmp_path)
    result = check(root, upstream(current(name="SECRET", tag_name="SECRET", assets=[
        asset(id=1001, size=len(DATA), browser_download_url="https://user:SECRET@host/file")])))
    assert result.stable.release.name == result.stable.release.tag == "SECRET"
    assert "SECRET" not in repr(result) and "https://" not in repr(result)


def test_frozen_status(tmp_path):
    result = check(tmp_path)
    for obj, key in [(result, "repository"), (result.availability, "state"),
                     (result.stable, "state"), (result.stable.comparison, "state"),
                     (result.archive, "complete"), (result.stable.release, "tag"),
                     (result.stable.release.archivable_assets[0], "size")]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, key, None)


def test_import_hygiene():
    # A fresh interpreter catches transitive imports hidden by the test runner.
    import subprocess
    root = Path(__file__).resolve().parents[1]
    script = """import sys
sys.path.insert(0, sys.argv[1])
class Guard:
    def find_spec(self, fullname, path=None, target=None):
        forbidden = ('PySide6', 'covearchive.ui', 'covearchive.library',
                     'covearchive.queue', 'covearchive.auto_archive', 'covearchive.githistory')
        if any(fullname == p or fullname.startswith(p + '.') for p in forbidden):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Guard())
import covearchive.archive.availability
print('Qt-free detection imports only')
"""
    completed = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(root / "src")],
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert "Qt-free detection imports only" in completed.stdout


def test_demonstration(tmp_path, capsys):
    root, _, _ = archive(tmp_path)
    samples = [
        ("healthy", check(root)),
        ("update_available", check(root, upstream(current(id=2)))),
        ("unavailable", check(root, ScanFailure(RID, "not_found"))),
        ("inconclusive", check(root, ScanFailure(RID, "network"))),
    ]
    for label, status in samples:
        print(f"{label}: availability={status.availability.state} "
              f"stable={status.stable.comparison.state} headline={status.headline}")
    output = capsys.readouterr().out
    assert output == (
        "healthy: availability=reachable stable=verified headline=up_to_date\n"
        "update_available: availability=reachable stable=not_archived headline=update_available\n"
        "unavailable: availability=not_found stable=inconclusive headline=unavailable\n"
        "inconclusive: availability=network stable=inconclusive headline=inconclusive\n")
    print(output, end="")
