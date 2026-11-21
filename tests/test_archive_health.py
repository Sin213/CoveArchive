"""Synthetic health audits: byte evidence, bounded work, and no mutation.

Snapshots deliberately exclude atime: reading may update it on the host filesystem.
"""
from dataclasses import FrozenInstanceError, fields, replace
from contextlib import contextmanager
import builtins
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time

import pytest

from covearchive.archive import health, manifest as m, naming, store
from covearchive.github.urls import RepositoryTarget


REPO = RepositoryTarget("Owner", "Repo")
DATA = b"payload"
SHA = hashlib.sha256(DATA).hexdigest()
TEMP = ".covearchive-tmp-" + "a" * 32 + ".part"
POSIX = pytest.mark.skipif(os.name != "posix", reason="requires POSIX filesystem objects")
NONROOT = pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0,
                            reason="requires non-root POSIX permission enforcement")


def archive(tmp_path, *, releases=1, assets=1, planned=False, indexed=True, repository=REPO):
    root = tmp_path / "archive"
    project = root.joinpath(*naming.project_components(repository))
    project.mkdir(parents=True)
    records = []
    for release_id in range(1, releases + 1):
        folder = naming.release_folder_name(f"v{release_id}", release_id)
        directory = project / folder
        directory.mkdir()
        items = tuple(m.AssetRecord(release_id * 1000 + i, f"a{i}.bin", f"a{i}.bin",
                                   len(DATA), None, None if planned else SHA,
                                   "planned" if planned else "completed")
                      for i in range(1, assets + 1))
        for item in items:
            if not planned:
                (directory / item.saved_name).write_bytes(DATA)
        record = m.ReleaseRecord(release_id, f"v{release_id}", folder, items)
        records.append(record)
        doc = m.ReleaseManifest(repository, release_id, record.tag, folder,
                                None, None, False, None, items)
        (directory / naming.RELEASE_FILENAME).write_text(m.dumps_release_manifest(doc))
    if indexed:
        (project / naming.INDEX_FILENAME).write_text(m.dumps_index(m.ProjectIndex(repository, tuple(records))))
    return root, project, tuple(records)


def snapshot(root):
    """Names/types/size/mode/inode/timestamps/content for the entire tree, no follows."""
    result = []
    def visit(path):
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
        target = os.readlink(path) if stat.S_ISLNK(info.st_mode) else None
        result.append((str(path.relative_to(root)), stat.S_IFMT(info.st_mode), info.st_size,
                       info.st_mode, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, digest, target))
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):
                visit(child)
    visit(root)
    return tuple(result)


def codes(report):
    return {finding.code for finding in report.findings}


def asset_path(project, records, index=0):
    return project / records[0].folder / records[0].assets[index].saved_name


def assert_unverified(report, code):
    assert code in codes(report)
    assert report.status != "healthy"
    assert not report.complete


def test_healthy_frozen_deterministic(tmp_path):
    root, project, records = archive(tmp_path, releases=2, assets=2)
    report = health.audit_project(str(root), REPO)
    assert report == health.audit_project(str(root), REPO)
    assert report.repository == REPO and report.status == "healthy"
    assert report.complete and not report.truncated
    assert report.counts.verified == len(report.verified_assets) == 4
    assert codes(report) == {"verified"}
    assert report.limits == health.AuditLimits()
    verified = report.verified_assets[0]
    assert (verified.release_id, verified.folder, verified.asset_id, verified.saved_name,
            verified.sha256, verified.size) == (1, records[0].folder, 1001, "a1.bin", SHA, len(DATA))
    with pytest.raises(FrozenInstanceError):
        report.complete = False


@pytest.mark.parametrize("index", [False, True])
def test_empty_project(tmp_path, index):
    root, _, _ = archive(tmp_path, releases=0, indexed=index)
    report = health.audit_project(str(root), REPO)
    assert report.status == "healthy" and report.complete and not report.verified_assets


def test_empty_archive(tmp_path):
    report = health.audit_project(str(tmp_path), REPO)
    assert report.status == "healthy" and report.complete
    assert not report.findings and not report.verified_assets
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("existing", ["Owner", "Unrelated"])
def test_missing_project(tmp_path, existing):
    (tmp_path / existing).mkdir()
    before = snapshot(tmp_path)
    report = health.audit_project(str(tmp_path), REPO)
    assert report.status == "attention" and not report.complete
    assert codes(report) == {"missing_project"}
    assert snapshot(tmp_path) == before


def test_missing_root(tmp_path):
    root = tmp_path / "absent"
    report = health.audit_project(str(root), REPO)
    assert codes(report) == {"missing_root"} and report.counts.missing == 1
    assert not report.complete and not root.exists()


@pytest.mark.parametrize("damage,code,category", [
    ("missing", "missing", "missing"), ("modified", "hash_mismatch", "damaged"),
    ("short", "size_mismatch", "damaged"), ("long", "size_mismatch", "damaged"),
])
def test_asset_damage(tmp_path, damage, code, category):
    root, project, records = archive(tmp_path)
    path = asset_path(project, records)
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes({"modified": b"Payload", "short": b"pay", "long": DATA + b"!"}[damage])
    report = health.audit_project(str(root), REPO)
    finding, = report.findings
    assert (finding.code, finding.category) == (code, category)
    assert (finding.release_id, finding.folder, finding.asset_id, finding.saved_name) == (
        1, records[0].folder, 1001, "a1.bin")
    assert report.status == "attention" and not report.verified_assets


@pytest.mark.parametrize("document", ["index", "release"])
@pytest.mark.parametrize("damage,code", [("corrupt", "corrupt"), ("version", "unsupported_version")])
def test_bad_metadata(tmp_path, document, damage, code):
    root, project, records = archive(tmp_path)
    path = project / naming.INDEX_FILENAME if document == "index" else project / records[0].folder / naming.RELEASE_FILENAME
    if damage == "corrupt":
        path.write_bytes(b"{broken\x00")
    else:
        doc = json.loads(path.read_text())
        doc["schema_version"] = 999
        path.write_text(json.dumps(doc))
    report = health.audit_project(str(root), REPO)
    assert code in codes(report) and not report.verified_assets
    assert report.status != "healthy" and not report.complete
    assert getattr(report.counts, "damaged" if damage == "corrupt" else "unverified") == 1


@pytest.mark.parametrize("missing", ["manifest", "folder"])
def test_missing_release(tmp_path, missing):
    root, project, records = archive(tmp_path)
    directory = project / records[0].folder
    (directory / naming.RELEASE_FILENAME).unlink()
    if missing == "folder":
        asset_path(project, records).unlink()
        directory.rmdir()
    report = health.audit_project(str(root), REPO)
    assert codes(report) == {"missing_release_manifest" if missing == "manifest" else "missing_release"}
    assert report.counts.missing == 1 and not report.verified_assets and not report.complete


@pytest.mark.parametrize("change", ["assets", "tag", "release_id", "repository"])
def test_inconsistent_release(tmp_path, change):
    root, project, records = archive(tmp_path)
    path = project / records[0].folder / naming.RELEASE_FILENAME
    doc = m.loads_release_manifest(path.read_text())
    changes = {"assets": {"assets": ()}, "tag": {"tag": "other"},
               "release_id": {"release_id": 2, "folder": naming.release_folder_name("v2", 2)},
               "repository": {"repository": RepositoryTarget("Other", "Repo")}}[change]
    path.write_text(m.dumps_release_manifest(replace(doc, **changes)))
    report = health.audit_project(str(root), REPO)
    assert "consistency" in codes(report) and report.counts.damaged == 1
    assert not report.verified_assets and not report.complete


def test_identity_mismatch_stops_all_asset_reads(tmp_path, monkeypatch):
    root, project, records = archive(tmp_path)
    (project / naming.INDEX_FILENAME).write_text(m.dumps_index(m.ProjectIndex(RepositoryTarget("Other", "Repo"), records)))
    monkeypatch.setattr(store, "verify_asset", lambda *args: pytest.fail("must not verify foreign identity"))
    report = health.audit_project(str(root), REPO)
    assert codes(report) == {"identity_mismatch"} and not report.complete


@pytest.mark.parametrize("indexed", [False, True])
def test_unindexed_release_is_untrusted_and_never_hashed(tmp_path, monkeypatch, indexed):
    root, project, records = archive(tmp_path, assets=2, indexed=indexed)
    if indexed:
        (project / naming.INDEX_FILENAME).write_text(m.dumps_index(m.ProjectIndex(REPO, ())))
    monkeypatch.setattr(store, "verify_asset", lambda *args: pytest.fail("untrusted assets must not be hashed"))
    report = health.audit_project(str(root), REPO)
    finding = next(f for f in report.findings if f.code == "unindexed_release")
    assert (finding.category, finding.release_id, finding.folder, finding.asset_count) == (
        "untrusted", 1, records[0].folder, 2)
    assert report.counts.untrusted == 1 and not report.verified_assets


@POSIX
@pytest.mark.parametrize("position", ["asset", "release", "project", "owner", "root"])
def test_symlinks_are_unsafe(tmp_path, position):
    root, project, records = archive(tmp_path)
    path = {"asset": asset_path(project, records), "release": project / records[0].folder,
            "project": project, "owner": project.parent, "root": root}[position]
    target = tmp_path / "outside"
    path.rename(target)
    path.symlink_to(target, target_is_directory=position != "asset")
    before = snapshot(root)
    report = health.audit_project(str(root), REPO)
    assert report.counts.unsafe == 1 and not report.verified_assets
    assert report.status == "attention" and snapshot(root) == before


@POSIX
@pytest.mark.parametrize("kind", ["hardlink", "fifo", "directory"])
def test_unsafe_asset_objects_do_not_hang(tmp_path, kind):
    root, project, records = archive(tmp_path)
    path = asset_path(project, records)
    path.unlink()
    if kind == "hardlink":
        other = tmp_path / "other"
        other.write_bytes(DATA)
        os.link(other, path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    report = health.audit_project(str(root), REPO)
    assert report.counts.unsafe == 1 and not report.verified_assets
    assert codes(report) == {"multiple_links" if kind == "hardlink" else "not_regular_file"}


@NONROOT
def test_inaccessible_asset_real_permissions(tmp_path):
    root, project, records = archive(tmp_path)
    path = asset_path(project, records)
    path.chmod(0)
    try:
        report = health.audit_project(str(root), REPO)
        assert_unverified(report, "inaccessible")
        assert report.counts.unverified == 1 and not report.verified_assets
    finally:
        path.chmod(0o600)


def test_busy_and_unexpected_errors_are_safe(tmp_path, monkeypatch):
    root, _, _ = archive(tmp_path)
    def fail(*args, **kwargs):
        raise store.ArchiveError("busy")
    monkeypatch.setattr(store, "verify_asset", fail)
    report = health.audit_project(str(root), REPO)
    assert_unverified(report, "busy")
    def unexpected(*args, **kwargs):
        raise RuntimeError("SECRET\n\x1b[31m")
    monkeypatch.setattr(store, "verify_asset", unexpected)
    report = health.audit_project(str(root), REPO)
    assert_unverified(report, "internal_error")
    assert "SECRET" not in repr(report)


def test_planned_and_orphan_temp_are_incomplete_without_recovery(tmp_path):
    root, project, records = archive(tmp_path, planned=True)
    (project / TEMP).write_bytes(b"partial")
    (project / records[0].folder / TEMP).write_bytes(b"partial")
    before = snapshot(root)
    report = health.audit_project(str(root), REPO)
    assert_unverified(report, "not_complete")
    assert sum(f.code == "incomplete_state" for f in report.findings) == 2
    assert not report.verified_assets and report.counts.damaged == 0
    assert snapshot(root) == before


def test_interrupted_release_without_metadata_still_reports_temps(tmp_path):
    root, project, records = archive(tmp_path)
    directory = project / records[0].folder
    (directory / naming.RELEASE_FILENAME).unlink()
    (directory / TEMP).write_bytes(b"partial")
    before = snapshot(root)
    report = health.audit_project(str(root), REPO)
    assert codes(report) == {"missing_release_manifest", "incomplete_state"}
    assert not report.complete and not report.verified_assets and snapshot(root) == before


@pytest.mark.parametrize("name", [f.name for f in fields(health.AuditLimits)])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_limits_validate_positive_integers(name, value):
    with pytest.raises(ValueError):
        health.AuditLimits(**{name: value})


@pytest.mark.parametrize("name,value,verified", [
    ("max_releases", 1, 2), ("max_assets", 1, 1),
    ("max_bytes_hashed", len(DATA), 1), ("max_asset_bytes", len(DATA) - 1, 0),
    ("max_metadata_bytes_read", 1, 0), ("max_directory_entries", 1, 0),
    ("max_findings", 1, 4),
])
def test_every_work_limit(tmp_path, name, value, verified):
    root, _, _ = archive(tmp_path, releases=2, assets=2)
    limits = health.AuditLimits(**{name: value})
    report = health.audit_project(str(root), REPO, limits)
    assert not report.complete and report.truncated and report.status != "healthy"
    assert report.limits == limits
    assert len(report.verified_assets) == report.counts.verified == verified
    assert len(report.findings) <= limits.max_findings
    assert ("findings_limit" if name == "max_findings" else "not_verified_limit") in codes(report)
    if name == "max_findings":
        assert report.counts.unverified == 1  # summary counted once, verified counts survive capping


def test_metadata_total_budget_includes_index_and_release(tmp_path):
    root, project, _ = archive(tmp_path)
    size = (project / naming.INDEX_FILENAME).stat().st_size
    report = health.audit_project(str(root), REPO, health.AuditLimits(max_metadata_bytes_read=size))
    assert_unverified(report, "not_verified_limit")
    assert not report.verified_assets


def test_byte_budgets_bound_actual_reads(tmp_path, monkeypatch):
    root, _, _ = archive(tmp_path, releases=2, assets=2)
    handle = store.Directory.__mro__[1]
    original = handle.read
    used = {"metadata": 0, "assets": 0}
    def read(self, count=1024 * 1024):
        data = original(self, count)
        kind = "metadata" if self.name in (naming.INDEX_FILENAME, naming.RELEASE_FILENAME) else "assets"
        used[kind] += len(data)
        return data
    monkeypatch.setattr(handle, "read", read)
    limits = health.AuditLimits(max_bytes_hashed=len(DATA), max_metadata_bytes_read=4000)
    report = health.audit_project(str(root), REPO, limits)
    assert not report.complete
    assert used["assets"] <= limits.max_bytes_hashed
    assert used["metadata"] <= limits.max_metadata_bytes_read


def test_growing_asset_is_bounded_and_never_verified(tmp_path, monkeypatch):
    root, project, records = archive(tmp_path)
    path = asset_path(project, records)
    handle = store.Directory.__mro__[1]
    original, original_info = handle.read, handle.info
    used, grown = 0, False
    def read(self, count=1024 * 1024):
        nonlocal used, grown
        if self.name == path.name:
            if os.name == "posix":
                with path.open("ab") as file:
                    file.write(b"growth")
            # Windows read handles deny concurrent writers, so growth is simulated there.
            grown = True
            data = original(self, count)
            used += len(data)
            return data
        return original(self, count)
    def info(self):
        result = original_info(self)
        if grown and self.name == path.name and result.size == len(DATA):
            return replace(result, size=len(DATA) + len(b"growth"))
        return result
    monkeypatch.setattr(handle, "read", read)
    monkeypatch.setattr(handle, "info", info)
    report = health.audit_project(str(root), REPO, health.AuditLimits(max_bytes_hashed=len(DATA)))
    assert codes(report) == {"size_mismatch"} and not report.verified_assets
    assert used == len(DATA)


@pytest.mark.parametrize("name,value", [("max_releases", 1), ("max_assets", 1),
                                       ("max_bytes_hashed", len(DATA)), ("max_asset_bytes", 1)])
def test_limits_prevent_skipped_asset_opens(tmp_path, monkeypatch, name, value):
    root, _, _ = archive(tmp_path, releases=2, assets=2)
    opened = []
    original = store.Directory.open_file
    def open_file(self, filename, **kwargs):
        if not kwargs.get("metadata"):
            opened.append((self.name, filename))
        return original(self, filename, **kwargs)
    monkeypatch.setattr(store.Directory, "open_file", open_file)
    report = health.audit_project(str(root), REPO, health.AuditLimits(**{name: value}))
    assert len(opened) == len(report.verified_assets)
    assert not report.complete


def test_unindexed_release_budget_prevents_opening_overflow(tmp_path, monkeypatch):
    root, _, _ = archive(tmp_path, releases=3, indexed=False)
    opened = []
    original = store.Directory.open_dir
    def open_dir(self, name, **kwargs):
        if name.startswith("release-"):
            opened.append(name)
        return original(self, name, **kwargs)
    monkeypatch.setattr(store.Directory, "open_dir", open_dir)
    report = health.audit_project(str(root), REPO, health.AuditLimits(max_releases=1))
    assert len(opened) == 1 and not report.complete and report.truncated
    assert report.counts.untrusted == 1 and not report.verified_assets


def test_unindexed_scan_respects_manifest_release_ceiling(tmp_path, monkeypatch):
    root, _, _ = archive(tmp_path, releases=2, indexed=False)
    monkeypatch.setattr(m, "MAX_RELEASES", 1)
    report = health.audit_project(str(root), REPO, health.AuditLimits(max_releases=10))
    assert report.counts.untrusted == 1 and not report.complete and report.truncated


def test_manifest_path_cannot_authorize_an_escape(tmp_path):
    root, project, _ = archive(tmp_path)
    path = project / naming.INDEX_FILENAME
    data = json.loads(path.read_text())
    data["releases"][0]["assets"][0]["saved_name"] = "../outside"
    path.write_text(json.dumps(data))
    report = health.audit_project(str(root), REPO)
    assert codes(report) == {"unsafe_path"} and report.counts.unsafe == 1
    assert not report.verified_assets and not report.complete


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_process_interrupts_propagate(tmp_path, monkeypatch, signal):
    root, _, _ = archive(tmp_path)
    def interrupt(*args):
        raise signal()
    monkeypatch.setattr(store, "verify_asset", interrupt)
    with pytest.raises(signal):
        health.audit_project(str(root), REPO)


@contextmanager
def audit_tripwires(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("audit attempted mutation, network, or subprocess")
    with monkeypatch.context() as guard:
        for name in ("mkdir", "makedirs", "rename", "replace", "unlink", "rmdir", "remove", "link", "symlink", "truncate"):
            guard.setattr(os, name, forbidden)
        for name in ("ensure_project", "ensure_release", "recover", "project_writer", "write_asset", "commit_release", "commit_index", "quarantine_manifest"):
            guard.setattr(store, name, forbidden)
        for name in ("create_temp", "open_lock", "open_temp"):
            guard.setattr(store.Directory, name, forbidden)
        guard.setattr(socket, "socket", forbidden)
        guard.setattr(subprocess, "Popen", forbidden)
        for module in (builtins, io):
            original = module.open
            def opened(file, mode="r", *args, _open=original, **kwargs):
                if any(flag in mode for flag in "wax+"):
                    forbidden()
                return _open(file, mode, *args, **kwargs)
            guard.setattr(module, "open", opened)
        original_os_open = os.open
        def os_open(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                forbidden()
            return original_os_open(path, flags, *args, **kwargs)
        guard.setattr(os, "open", os_open)
        original_dir = store.Directory.open_dir
        def open_dir(self, name, **kwargs):
            if kwargs.get("create", False):
                forbidden()
            return original_dir(self, name, **kwargs)
        guard.setattr(store.Directory, "open_dir", open_dir)
        yield


@pytest.mark.parametrize("damaged", [False, True])
def test_whole_tree_no_mutation_and_tripwires(tmp_path, monkeypatch, damaged):
    root, project, records = archive(tmp_path, assets=3)
    if damaged:
        asset_path(project, records).write_bytes(b"Payload")
        asset_path(project, records, 1).unlink()
        if os.name == "posix":
            asset_path(project, records, 2).unlink()
            asset_path(project, records, 2).symlink_to(tmp_path / "absent")
    before = snapshot(root)
    with audit_tripwires(monkeypatch):
        report = health.audit_project(str(root), REPO)
    assert report.status == ("attention" if damaged else "healthy")
    assert "internal_error" not in codes(report)
    assert snapshot(root) == before
    assert not any(".covearchive.lock" in row[0] or ".corrupt-" in row[0] or ".covearchive-tmp-" in row[0] for row in before)


@pytest.mark.parametrize("operation", ["builtins_open", "io_open", "os_open", "create_dir"])
def test_tripwires_detect_injected_forbidden_call_even_inside_audit(tmp_path, monkeypatch, operation):
    root, _, _ = archive(tmp_path)
    outside = tmp_path / "forbidden"
    before = snapshot(root)
    def mutation(directory, record):
        if operation == "builtins_open":
            return builtins.open(outside, "wb")
        if operation == "io_open":
            return io.open(outside, "wb")
        if operation == "os_open":
            return os.open(outside, os.O_WRONLY | os.O_CREAT)
        return directory.directory.open_dir("new", create=True)
    with pytest.raises(pytest.fail.Exception, match="audit attempted"):
        with audit_tripwires(monkeypatch):
            monkeypatch.setattr(store, "verify_asset", mutation)
            health.audit_project(str(root), REPO)
    assert not outside.exists() and snapshot(root) == before


@POSIX
@pytest.mark.parametrize("position,budget", [("root", 1), ("owner", 2), ("project", 3), ("release", 3)])
def test_ten_thousand_entries_are_bounded_during_enumeration(tmp_path, monkeypatch, position, budget):
    root, project, records = archive(tmp_path, releases=0 if position == "project" else 1)
    directory = {"root": root, "owner": project.parent, "project": project,
                 "release": project / records[0].folder if records else project}[position]
    for number in range(10_000):
        (directory / f"entry-{number:05}").touch()
    original = os.scandir
    enumerated = 0
    @contextmanager
    def scandir(*args, **kwargs):
        with original(*args, **kwargs) as entries:
            def counted():
                nonlocal enumerated
                for entry in entries:
                    enumerated += 1
                    yield entry
            yield counted()
    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "listdir", lambda *args: pytest.fail("audit used unbounded listdir"))
    report = health.audit_project(str(root), REPO, health.AuditLimits(max_directory_entries=budget))
    assert_unverified(report, "not_verified_limit")
    assert report.truncated
    assert len(report.verified_assets) == (1 if position == "release" else 0)
    assert 0 < enumerated <= budget + 1
    assert "missing_project" not in codes(report) and "internal_error" not in codes(report)


@POSIX
def test_hardlink_validation_cannot_hide_an_unbounded_recovery_scan(tmp_path, monkeypatch):
    root, project, records = archive(tmp_path)
    os.link(asset_path(project, records), tmp_path / "external-link")
    monkeypatch.setattr(os, "listdir", lambda *args: pytest.fail("audit used an unbounded recovery scan"))
    report = health.audit_project(str(root), REPO)
    assert codes(report) == {"multiple_links"} and report.counts.unsafe == 1
    assert not report.verified_assets


@POSIX
@pytest.mark.parametrize("component", ["Owner", "OWNER"])
def test_case_fold_lookup_budget_cannot_false_missing_or_accept_unseen_aliases(tmp_path, component):
    root, project, _ = archive(tmp_path)
    if component != "Owner":
        project.parent.rename(root / component)
    (root / "Other").mkdir()
    report = health.audit_project(str(root), REPO, health.AuditLimits(max_directory_entries=1))
    assert_unverified(report, "not_verified_limit")
    assert report.truncated and not report.verified_assets and "missing_project" not in codes(report)


@POSIX
@pytest.mark.parametrize("level", ["root", "owner"])
def test_audit_busy_parent_lock_has_real_process_deadline(tmp_path, level):
    root, project, _ = archive(tmp_path)
    source = str(Path(__file__).resolve().parents[1] / "src")
    holder_code = ("import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY); "
                   "fcntl.flock(fd,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.readline()")
    holder = subprocess.Popen([sys.executable, "-I", "-B", "-c", holder_code,
                               str(root if level == "root" else project.parent)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        code = (f"import sys,json; sys.path.insert(0,{source!r}); "
                "from covearchive.archive.health import audit_project; "
                "from covearchive.github.urls import RepositoryTarget; "
                f"r=audit_project({str(root)!r},RepositoryTarget('Owner','Repo')); "
                "print(json.dumps([r.status,r.complete,[f.code for f in r.findings]]))")
        start = time.monotonic()
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code],
                                capture_output=True, text=True, timeout=3)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == ["unverified", False, ["busy"]]
        assert time.monotonic() - start < 3
    finally:
        holder.communicate("release\n", timeout=3)


@NONROOT
def test_read_only_tree_works_and_is_unchanged(tmp_path):
    root, _, _ = archive(tmp_path)
    paths = [root, *root.rglob("*")]
    original = {path: path.stat().st_mode for path in paths}
    for path in paths:
        path.chmod(0o500 if path.is_dir() else 0o400)
    try:
        before = snapshot(root)
        report = health.audit_project(str(root), REPO)
        assert report.status == "healthy" and report.complete
        assert snapshot(root) == before
    finally:
        for path, mode in original.items():
            path.chmod(mode)


def test_disk_names_do_not_enter_messages_and_other_projects_are_not_walked(tmp_path, monkeypatch):
    root, project, _ = archive(tmp_path)
    # Windows rejects control characters in names; use line/bidi separators there.
    (project / ("release-evil\n\x1b[31m--9" if os.name == "posix"
                else "release-evil\u2028\u202e[31m--9")).mkdir()
    other = root / "Unrelated"
    other.mkdir()
    (other / "secret").write_bytes(b"secret")
    original = store.Directory.open_dir
    def open_dir(self, name, **kwargs):
        assert name != "Unrelated", "audit walked another project"
        return original(self, name, **kwargs)
    monkeypatch.setattr(store.Directory, "open_dir", open_dir)
    before = snapshot(root)
    report = health.audit_project(str(root), REPO)
    assert report.findings
    assert all("evil" not in f.message and "\n" not in f.message and "\x1b" not in f.message
               and "\u2028" not in f.message and "\u202e" not in f.message for f in report.findings)
    assert snapshot(root) == before


def test_import_is_qt_independent():
    source = str(Path(__file__).resolve().parents[1] / "src")
    code = (f"import sys; sys.path.insert(0, {source!r}); import covearchive.archive.health; "
            "assert not any(n.startswith(('PySide', 'PyQt')) for n in sys.modules)")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
