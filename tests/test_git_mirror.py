"""Persistent mirror contract: small real local Git repositories, never network."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from covearchive.archive import githistory as gh
from test_git_history import REPO, local, git, remove_source, snapshot_bytes


def update(local, **kwargs):
    return gh.update_mirror(REPO, local[1], **kwargs)


def document(result):
    return json.loads(result.capture.read_text())


def test_initial_incremental_and_zero_transfer(local, monkeypatch):
    first = update(local)
    assert first.status == "UPDATED" and first.ref_count == 2
    before = snapshot_bytes(first.path / "repository.git" / "objects")
    calls = []; original = gh._run_git
    def record(args, **kwargs):
        calls.append(args); return original(args, **kwargs)
    monkeypatch.setattr(gh, "_run_git", record)
    same = update(local)
    assert same.status == "UNCHANGED" and same.capture == first.capture
    assert not any("fetch" in args for args in calls)
    newer = local[3](local[2]); git(local[0], "update-ref", "refs/heads/main", newer)
    second = update(local)
    assert second.path == first.path and second.capture != first.capture
    assert document(second)["refs"]["refs/heads/main"] == newer
    after = snapshot_bytes(first.path / "repository.git" / "objects")
    assert before and all(after[name] == data for name, data in before.items())
    assert not any("init" in args for args in calls)


@pytest.mark.parametrize("change", ["delete_branch", "delete_tag", "rewrite_tag", "force_push"])
def test_retention_export_restore_without_upstream(local, tmp_path, change):
    git(local[0], "update-ref", "refs/heads/old", local[2])
    first = update(local); old = first.capture.read_bytes()
    newer = local[3]()
    if change == "delete_branch": git(local[0], "update-ref", "-d", "refs/heads/old")
    elif change == "delete_tag": git(local[0], "update-ref", "-d", "refs/tags/v1")
    elif change == "rewrite_tag": git(local[0], "update-ref", "refs/tags/v1", newer)
    else: git(local[0], "update-ref", "refs/heads/main", newer)
    second = update(local)
    assert first.capture.read_bytes() == old
    remove_source(local[0])
    for index, capture in enumerate((first.capture.name, None)):
        exported = gh.export_mirror(REPO, local[1], tmp_path / f"export-{index}", capture=capture)
        restored = tmp_path / f"restore-{index}"; restored.mkdir()
        git(restored, "clone", "--bare", "--ref-format=reftable", str(exported), ".")
        expected = document(first if index == 0 else second)["refs"]
        actual = dict(line.split(" ", 1)[::-1] for line in git(restored, "for-each-ref", "--format=%(objectname) %(refname)").splitlines())
        assert actual == expected
    assert gh.verify_mirror(REPO, local[1]).status == "VERIFIED"


@pytest.mark.parametrize("attack", ["include", "alternates", "commondir", "hook", "symlink"])
def test_reject_untrusted_repository_before_any_git(local, monkeypatch, tmp_path, attack):
    result = update(local); repo = result.path / "repository.git"
    if attack == "include": (repo / "config").write_text('[include]\n path = /tmp/evil\n')
    elif attack == "alternates": (repo / "objects" / "info" / "alternates").write_text("/tmp/evil\n")
    elif attack == "commondir": (repo / "commondir").write_text("/tmp/evil\n")
    elif attack == "hook": (repo / "hooks").mkdir()
    else: (repo / "objects" / "evil").symlink_to(tmp_path)
    def forbidden(*args, **kwargs): pytest.fail("Git ran before trust validation")
    monkeypatch.setattr(gh, "_run_git", forbidden)
    with pytest.raises(gh.HistoryError, match="Git history") as error: update(local)
    assert error.value.code == "corrupt"


def test_publication_failure_retry_retains_previous(local, monkeypatch):
    first = update(local); newer = local[3](local[2])
    git(local[0], "update-ref", "refs/heads/main", newer)
    original = gh.store.write_asset
    def fail(directory, name, *args, **kwargs):
        if name.endswith(".json") and name.startswith("capture-"): raise OSError(28, "full")
        return original(directory, name, *args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(gh.store, "write_asset", fail)
        with pytest.raises(gh.HistoryError) as error: update(local)
        assert error.value.code == "disk_full"
    assert first.capture.exists()
    assert gh.verify_mirror(REPO, local[1]).capture == first.capture
    assert update(local).status == "UPDATED"


def test_added_branches_annotated_tags_and_changed_head(local, tmp_path):
    first = update(local)
    newer = local[3](local[2])
    git(local[0], "update-ref", "refs/heads/other", newer)
    git(local[0], "-c", "user.name=Test", "-c", "user.email=t@example.invalid", "tag", "-a", "annotated", "-m", "preserve annotation", newer)
    tag = git(local[0], "rev-parse", "refs/tags/annotated")
    git(local[0], "symbolic-ref", "HEAD", "refs/heads/other")
    second = update(local)
    assert second.ref_count == 4 and document(second)["head"] == "refs/heads/other"
    assert document(second)["refs"]["refs/tags/annotated"] == tag
    exported = gh.export_mirror(REPO, local[1], tmp_path / "export")
    restored = tmp_path / "restored"; restored.mkdir()
    git(restored, "clone", "--bare", "--ref-format=reftable", str(exported), ".")
    assert git(restored, "symbolic-ref", "HEAD") == "refs/heads/other"
    assert git(restored, "cat-file", "-t", tag) == "tag"
    assert "preserve annotation" in git(restored, "cat-file", "-p", tag)
    assert document(first)["head"] == "refs/heads/main"


def test_default_head_only_change_creates_capture(local):
    update(local)
    git(local[0], "update-ref", "refs/heads/alias", local[2]); first = update(local)
    git(local[0], "symbolic-ref", "HEAD", "refs/heads/alias")
    second = update(local)
    assert first.capture != second.capture and document(second)["head"] == "refs/heads/alias"


def test_empty_remote_and_all_refs_deleted(local, tmp_path):
    first = update(local)
    for ref in ("refs/heads/main", "refs/tags/v1"): git(local[0], "update-ref", "-d", ref)
    empty = update(local)
    assert empty.ref_count == 0 and document(empty)["refs"] == {}
    assert update(local).status == "UNCHANGED"
    export = gh.export_mirror(REPO, local[1], tmp_path / "empty")
    assert git(export, "for-each-ref") == ""
    assert first.capture.exists()


def test_initial_empty_is_not_a_success(local):
    for ref in ("refs/heads/main", "refs/tags/v1"): git(local[0], "update-ref", "-d", ref)
    with pytest.raises(gh.HistoryError) as error: update(local)
    assert error.value.code == "empty_repository"
    assert not list(local[1].rglob("capture-*.json"))


def test_unavailable_remote_preserves_usable_capture(local):
    first = update(local); before = first.capture.read_bytes()
    remove_source(local[0])
    with pytest.raises(gh.HistoryError) as error: update(local)
    assert error.value.code == "not_found"
    assert first.capture.read_bytes() == before
    assert gh.verify_mirror(REPO, local[1]).capture == first.capture


@pytest.mark.parametrize("stage", ["before_fetch", "after_fetch", "publication"])
def test_interruption_retry_and_orphan_namespaces(local, monkeypatch, stage):
    first = update(local); newer = local[3](local[2])
    git(local[0], "update-ref", "refs/heads/main", newer)
    original = gh._run_git
    def interrupted(args, **kwargs):
        if "fetch" in args:
            if stage == "after_fetch": original(args, **kwargs)
            raise gh.HistoryError("cancelled")
        return original(args, **kwargs)
    def progress(value):
        if value == "publishing": raise gh.HistoryError("cancelled")
    with monkeypatch.context() as m:
        if stage != "publication": m.setattr(gh, "_run_git", interrupted)
        with pytest.raises(gh.HistoryError) as error: update(local, progress=progress if stage == "publication" else None)
        assert error.value.code == "cancelled"
    assert gh.verify_mirror(REPO, local[1]).capture == first.capture
    assert len(list(first.capture.parent.glob("capture-*.json"))) == 1
    second = update(local)
    assert document(second)["refs"]["refs/heads/main"] == newer


@pytest.mark.parametrize("damage", ["object", "ref", "manifest", "identity", "head"])
def test_corruption_never_reinitializes(local, monkeypatch, damage):
    first = update(local); repo = first.path / "repository.git"
    if damage == "object":
        obj = next(p for p in (repo / "objects").rglob("*") if p.is_file())
        obj.chmod(0o600); obj.write_bytes(b"damaged")
    elif damage == "ref": git(repo, "update-ref", "-d", gh._capture_ref(first.capture.name, "refs/heads/main"))
    elif damage == "manifest": first.capture.write_text("{}")
    elif damage == "identity": (first.path / "identity.json").write_text("{}")
    else: (repo / "HEAD").write_text("ref: /tmp/evil\n")
    before = snapshot_bytes(first.path); calls = []; original = gh._run_git
    def record(args, **kwargs): calls.append(args); return original(args, **kwargs)
    monkeypatch.setattr(gh, "_run_git", record)
    with pytest.raises(gh.HistoryError): update(local)
    assert not any("init" in args or "fetch" in args for args in calls)
    assert snapshot_bytes(first.path) == before


@pytest.mark.parametrize("field,value,code", [("min_free_bytes", 2**63, "disk_full"), ("max_working_bytes", 1, "too_large"), ("max_refs", 1, "corrupt"), ("max_stdout_bytes", 1, "too_large"), ("overall_seconds", 0, "timeout"), ("max_mirror_entries", 1, "too_large"), ("max_mirror_metadata_bytes", 1, "too_large")])
def test_limits_leave_prior_capture(local, field, value, code):
    first = update(local); before = snapshot_bytes(first.path)
    newer = local[3](local[2]); git(local[0], "update-ref", "refs/heads/main", newer)
    with pytest.raises(gh.HistoryError) as error: update(local, limits=replace(gh.Limits(), **{field: value}))
    assert error.value.code == code
    assert snapshot_bytes(first.path) == before


def test_lock_excludes_updates_verify_export_but_not_releases(local, tmp_path):
    first = update(local)
    with gh.store.ArchiveRoot.open(str(first.path.parent)) as history, history.open_lock():
        for action in (lambda: update(local), lambda: gh.verify_mirror(REPO, local[1]), lambda: gh.export_mirror(REPO, local[1], tmp_path / "export")):
            with pytest.raises(gh.HistoryError) as error: action()
            assert error.value.code == "busy"
        project = first.path.parent.parent
        with gh.store.ArchiveRoot.open(str(project)) as directory, gh.store.project_writer(directory): pass


def test_hostile_environment_hooks_lfs_submodules_inert(local, tmp_path, monkeypatch):
    marker = tmp_path / "executed"
    evil = tmp_path / "config"; evil.write_text('[include]\npath = /tmp/evil\n[core]\nhooksPath=/tmp/evil\n')
    for name, value in {"GIT_CONFIG_GLOBAL": str(evil), "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/tmp/evil", "GIT_OBJECT_DIRECTORY": "/tmp/evil", "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/tmp/evil", "GIT_DIR": "/tmp/evil", "GIT_TRACE": str(marker), "GIT_ASKPASS": str(marker)}.items(): monkeypatch.setenv(name, value)
    blob = git(local[0], "hash-object", "-w", "--stdin", data=b'version https://git-lfs.github.com/spec/v1\noid sha256:' + b'0' * 64 + b'\nsize 999\n')
    modules = git(local[0], "hash-object", "-w", "--stdin", data=b'[submodule "evil"]\npath = evil\nurl = ext::evil\n')
    tree = git(local[0], "mktree", data=f"100644 blob {blob}\tlfs.bin\n100644 blob {modules}\t.gitmodules\n160000 commit {local[2]}\tevil\n".encode())
    commit = git(local[0], "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit-tree", tree, data=b"inert content\n")
    git(local[0], "update-ref", "refs/heads/main", commit)
    calls = []; original = gh._spawn
    def record(args, **kwargs):
        calls.append((args, kwargs)); return original(args, **kwargs)
    monkeypatch.setattr(gh, "_spawn", record)
    first = update(local)
    assert not marker.exists() and first.status == "UPDATED"
    for args, kwargs in calls:
        assert not set(args) & {"checkout", "submodule", "lfs", "gc", "prune", "repack"}
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert "GIT_OBJECT_DIRECTORY" not in kwargs["env"]
        assert "core.hooksPath=" + os.devnull in args


def test_windows_invalid_case_colliding_and_unicode_names(local, tmp_path):
    for ref in ("refs/heads/CON", "refs/heads/Case", "refs/heads/case", "refs/heads/a:b", "refs/heads/snow-雪"):
        # Colon is invalid in Git itself on every platform; a valid reserved
        # Windows filename is enough to require a reftable representation.
        if ":" in ref:
            assert git(local[0], "update-ref", ref, local[2], check=False).returncode != 0
        else: git(local[0], "update-ref", ref, local[2])
    first = update(local)
    assert {"refs/heads/CON", "refs/heads/Case", "refs/heads/case"} <= set(document(first)["refs"])
    output = gh.export_mirror(REPO, local[1], tmp_path / "export")
    assert "refs/heads/CON" in git(tmp_path, "bundle", "list-heads", str(output))
    restored = tmp_path / "restored"; restored.mkdir()
    git(restored, "clone", "--bare", "--ref-format=reftable", str(output), ".")
    actual = dict(line.split(" ", 1)[::-1] for line in git(restored, "for-each-ref", "--format=%(objectname) %(refname)").splitlines())
    assert actual == document(first)["refs"]


def test_no_prune_gc_force_or_old_capture_modification(local, monkeypatch):
    first = update(local); before = first.capture.read_bytes(); calls = []; original = gh._run_git
    def enforce(args, **kwargs):
        assert not set(args) & {"gc", "prune", "repack", "--prune", "--prune-tags", "--mirror", "--force"}
        assert not any(a.startswith("+refs/") for a in args)
        calls.append(args); return original(args, **kwargs)
    monkeypatch.setattr(gh, "_run_git", enforce)
    newer = local[3](); git(local[0], "update-ref", "refs/heads/main", newer)
    git(local[0], "update-ref", "-d", "refs/tags/v1")
    update(local)
    assert first.capture.read_bytes() == before
    assert git(first.path / "repository.git", "rev-parse", gh._capture_ref(first.capture.name, "refs/tags/v1")) == local[2]
    assert any("fetch" in args for args in calls)


def test_bundle_snapshot_compatibility(local):
    bundle = gh.archive_history(REPO, local[1]); before = snapshot_bytes(bundle.snapshot)
    update(local); gh.verify_mirror(REPO, local[1])
    assert gh.archive_history(REPO, local[1]).status == "UNCHANGED"
    assert gh.verify_snapshot(bundle.snapshot, repository=REPO)
    assert snapshot_bytes(bundle.snapshot) == before


def test_provider_identity_separation_and_case_alias(local):
    from covearchive.github.urls import RepositoryTarget
    first = update(local)
    alias = gh.update_mirror(RepositoryTarget("owner", "repo"), local[1])
    assert alias.path == first.path and alias.status == "UNCHANGED"
    for target in (RepositoryTarget("Owner", "Repo", "codeberg", "codeberg.org"), RepositoryTarget("Group/Sub", "Repo", "gitlab", "gitlab.com")):
        other = gh.update_mirror(target, local[1])
        assert other.path != first.path
        assert gh.verify_mirror(target, local[1]).capture == other.capture


def test_frozen_environment_and_private_subprocess_lifecycle(local, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "frozen"), raising=False)
    monkeypatch.setenv("GIT_EXEC_PATH", str(tmp_path / "hostile"))
    calls = []; original = gh._spawn
    def record(args, **kwargs): calls.append((args, kwargs)); return original(args, **kwargs)
    monkeypatch.setattr(gh, "_spawn", record)
    update(local)
    assert calls
    for args, kwargs in calls:
        assert Path(args[0]).is_absolute() and not Path(kwargs["cwd"]).exists()
        assert "GIT_EXEC_PATH" not in kwargs["env"]
        assert kwargs.get("start_new_session") or kwargs.get("creationflags")


def test_actual_child_cancellation_reaps_and_retry(local, monkeypatch):
    first = update(local); newer = local[3](local[2])
    git(local[0], "update-ref", "refs/heads/main", newer)
    original = gh._spawn; entered = threading.Event(); cancel = threading.Event(); children = []
    def blocked(args, **kwargs):
        if "fetch" in args:
            child = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(child); entered.set(); return child
        return original(args, **kwargs)
    def stop():
        if entered.wait(5): cancel.set()
    thread = threading.Thread(target=stop); thread.start()
    try:
        with monkeypatch.context() as m:
            m.setattr(gh, "_spawn", blocked)
            with pytest.raises(gh.HistoryError) as error: update(local, cancel=cancel)
            assert error.value.code == "cancelled"
        assert children and all(child.poll() is not None for child in children)
    finally: cancel.set(); thread.join(6)
    assert gh.verify_mirror(REPO, local[1]).capture == first.capture
    assert update(local).status == "UPDATED"


def test_capture_namespace_collision_never_changes_old_refs(local, monkeypatch):
    first = update(local); before = snapshot_bytes(first.path)
    newer = local[3](local[2]); git(local[0], "update-ref", "refs/heads/main", newer)
    monkeypatch.setattr(gh, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=first.capture.name[8:-5])))
    with pytest.raises(gh.HistoryError) as error: update(local)
    assert error.value.code == "conflict"
    assert snapshot_bytes(first.path) == before


def test_interrupted_link_publication_recovers_adapter_temp(local):
    first = update(local)
    if os.name == "nt":
        # Windows no-replace rename publication has no second-link crash window.
        assert gh.verify_mirror(REPO, local[1]).capture == first.capture
        return
    orphan = first.capture.parent / (".covearchive-tmp-" + "a" * 32 + ".part")
    os.link(first.capture, orphan)
    assert first.capture.stat().st_nlink == 2
    assert gh.verify_mirror(REPO, local[1]).capture == first.capture
    assert not orphan.exists() and first.capture.stat().st_nlink == 1


def test_fetch_race_records_fetched_refs(local, monkeypatch):
    update(local)
    advertised = local[3](local[2]); fetched = local[3](advertised)
    git(local[0], "update-ref", "refs/heads/main", advertised)
    original = gh._run_git
    def moving(args, **kwargs):
        if "fetch" in args: git(local[0], "update-ref", "refs/heads/main", fetched)
        return original(args, **kwargs)
    monkeypatch.setattr(gh, "_run_git", moving)
    assert document(update(local))["refs"]["refs/heads/main"] == fetched


def test_unborn_default_head_exports_with_all_refs(local, tmp_path):
    git(local[0], "symbolic-ref", "HEAD", "refs/heads/missing")
    first = update(local)
    assert document(first)["head"] is None
    assert gh.export_mirror(REPO, local[1], tmp_path / "export").is_file()


def test_restore_shared_tip_default_uses_capture_metadata(local, tmp_path):
    git(local[0], "update-ref", "refs/heads/alias", local[2])
    git(local[0], "symbolic-ref", "HEAD", "refs/heads/alias")
    first = update(local); doc = document(first)
    output = gh.export_mirror(REPO, local[1], tmp_path / "export")
    restored = tmp_path / "restored"; restored.mkdir()
    git(restored, "clone", "--bare", "--ref-format=reftable", "--branch", doc["head"][11:], str(output), ".")
    assert git(restored, "symbolic-ref", "HEAD") == doc["head"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode/owner trust; NTFS pins ancestors with handles")
@pytest.mark.parametrize("parent", ["archive", "project", "history", "mirror"])
def test_repair_rejects_foreign_writable_ancestors(local, monkeypatch, parent):
    first = update(local)
    paths = dict(archive=local[1], project=first.path.parent.parent,
                 history=first.path.parent, mirror=first.path)
    target = paths[parent]
    original = Path.lstat
    def writable(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == target:
            values = list(info); values[0] |= 0o022
            return os.stat_result(values)
        return info
    monkeypatch.setattr(Path, "lstat", writable)
    monkeypatch.setattr(gh, "_run_git", lambda *a, **kw: pytest.fail("Git ran with a writable ancestor"))
    with pytest.raises(gh.HistoryError) as error: update(local)
    assert error.value.code == "unsafe_path"


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership; NTFS pins ancestors with handles")
def test_repair_rejects_foreign_owned_ancestor(local, monkeypatch):
    first = update(local); target = first.path.parent
    original = Path.lstat
    def foreign(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == target:
            values = list(info); values[4] = info.st_uid + 1
            return os.stat_result(values)
        return info
    monkeypatch.setattr(Path, "lstat", foreign)
    monkeypatch.setattr(gh, "_run_git", lambda *a, **kw: pytest.fail("Git ran with a foreign-owned ancestor"))
    with pytest.raises(gh.HistoryError) as error: update(local)
    assert error.value.code == "unsafe_path"


@pytest.mark.parametrize("failure", ["git_missing", "git_unsupported", "disk_full"])
def test_repair_failed_initialization_is_retryable(local, monkeypatch, failure):
    original = gh._run_git
    def fail(args, **kwargs):
        if "init" in args: raise gh.HistoryError(failure)
        return original(args, **kwargs)
    with monkeypatch.context() as m:
        if failure == "git_missing":
            def missing(): raise gh.HistoryError("git_missing")
            m.setattr(gh, "_git_executable", missing)
        else: m.setattr(gh, "_run_git", fail)
        with pytest.raises(gh.HistoryError) as error: update(local)
        assert error.value.code == failure
    assert not list(local[1].rglob("mirror"))
    assert update(local).status == "UPDATED"


def test_repair_empty_owned_initial_directory_retries(local):
    path = local[1] / "Owner" / "Repo" / "git-history" / "mirror"
    path.mkdir(parents=True, mode=0o700)
    assert update(local).status == "UPDATED"


def test_repair_initial_publication_disk_full_is_retryable(local, monkeypatch):
    original = gh.store.write_asset
    def full(directory, name, *args, **kwargs):
        if name == "identity.json": raise OSError(28, "disk full")
        return original(directory, name, *args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(gh.store, "write_asset", full)
        with pytest.raises(gh.HistoryError) as error: update(local)
        assert error.value.code == "disk_full"
    assert not list(local[1].rglob("mirror"))
    assert not list(local[1].rglob("mirror-init-*"))
    assert update(local).status == "UPDATED"


@pytest.mark.parametrize("operation", ["update", "verify", "export"])
def test_repair_old_git_reports_upgrade_after_trust(local, monkeypatch, tmp_path, operation):
    update(local)
    def old(args, **kwargs):
        if args == ["--version"]: return b"git version 2.44.9\n"
        pytest.fail("Unsupported Git was used against the persistent repository")
    monkeypatch.setattr(gh, "_run_git", old)
    actions = dict(update=lambda: update(local), verify=lambda: gh.verify_mirror(REPO, local[1]),
                   export=lambda: gh.export_mirror(REPO, local[1], tmp_path / "export"))
    with pytest.raises(gh.HistoryError) as error: actions[operation]()
    assert error.value.code == "git_unsupported"


def test_repair_unsupported_reftable_capability_reports_upgrade(local, monkeypatch):
    update(local); original = gh._spawn
    def unsupported(args, **kwargs):
        if "fsck" in args:
            return original([sys.executable, "-c", "import sys; sys.stderr.write('fatal: unknown repository extension refstorage'); sys.exit(1)"], **kwargs)
        return original(args, **kwargs)
    monkeypatch.setattr(gh, "_spawn", unsupported)
    with pytest.raises(gh.HistoryError) as error: update(local)
    assert error.value.code == "git_unsupported"


def test_repair_null_head_restore_with_colliding_default(local, tmp_path):
    git(local[0], "symbolic-ref", "HEAD", "refs/heads/missing")
    first = update(local)
    output = gh.export_mirror(REPO, local[1], tmp_path / "export")
    restored = tmp_path / "restored"; restored.mkdir()
    git(restored, "-c", "init.defaultBranch=main", "clone", "--bare", "--ref-format=reftable", str(output), ".")
    assert git(restored, "symbolic-ref", "HEAD") == "refs/heads/main"
    target = (output.parent / "restore-head.txt").read_text().strip()
    git(restored, "symbolic-ref", "HEAD", target)
    assert git(restored, "rev-parse", "--verify", "HEAD", check=False).returncode != 0
    actual = dict(line.split(" ", 1)[::-1] for line in git(restored, "for-each-ref", "--format=%(objectname) %(refname)").splitlines())
    assert actual == document(first)["refs"]


@pytest.mark.parametrize("cancel_validation", [False, True])
def test_repair_reftable_validation_linear_and_cancellable(local, monkeypatch, cancel_validation):
    first = update(local); path = first.path / "repository.git"
    tables = path / "reftable"; listing = tables / "tables.list"
    names = listing.read_text().splitlines()
    for i in range(4000):
        name = f"0x{i + 100:012x}-0x{i + 100:012x}-abcdef01.ref"
        (tables / name).write_bytes(b""); names.append(name)
    listing.write_text("".join(name + "\n" for name in names))
    calls = []; original = gh.store.Directory.names; read = gh._read_mirror_file
    cancel = threading.Event()
    def counted(directory, **kwargs):
        if directory.name == "reftable":
            calls.append(1)
            assert len(calls) <= 2, "reftable listing repeated for every table"
        return original(directory, **kwargs)
    def after_read(directory, name):
        raw = read(directory, name)
        if name == "tables.list" and cancel_validation: cancel.set()
        return raw
    monkeypatch.setattr(gh.store.Directory, "names", counted)
    monkeypatch.setattr(gh, "_read_mirror_file", after_read)
    with gh.store.ArchiveRoot.open(str(path)) as repo:
        if cancel_validation:
            with pytest.raises(gh.HistoryError) as error: gh._trust_repository(repo, path, cancel=cancel, limits=gh.Limits())
            assert error.value.code == "cancelled"
        else: gh._trust_repository(repo, path, cancel=cancel, limits=gh.Limits())
    assert len(calls) == 1


@pytest.mark.parametrize("reason", ["cancel", "timeout"])
def test_repair_cancelled_git_transaction_recovers_previous_capture(local, monkeypatch, tmp_path, reason):
    first = update(local); newer = local[3](local[2])
    git(local[0], "update-ref", "refs/heads/main", newer)
    ready = tmp_path / "prepared"; script = tmp_path / "gate.py"
    # POSIX: stop a genuine prepared Git reftable transaction, then exercise the
    # real runner's cancellation/timeout and process-tree termination. Windows
    # lacks SIGSTOP; use deterministic reftable-style transient injection there.
    script.write_text("""import os, pathlib, signal, subprocess, sys, time
repo, ready, sha = map(str, sys.argv[1:4])
if os.name != 'nt':
    child = subprocess.Popen(sys.argv[4:], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    child.stdin.write(('start\\ncreate refs/covearchive/captures/' + 'f'*32 + '/heads/pending ' + sha + '\\nprepare\\n').encode()); child.stdin.flush()
    assert child.stdout.readline().strip() == b'start: ok'
    assert child.stdout.readline().strip() == b'prepare: ok'
    os.kill(child.pid, signal.SIGSTOP)
else:
    (pathlib.Path(repo)/'reftable'/'tables.list.lock').write_bytes(b'')
names = [p.name for p in (pathlib.Path(repo)/'reftable').iterdir()]
assert 'tables.list.lock' in names, names
pathlib.Path(ready).write_text('\\n'.join(names))
time.sleep(30)
""")
    original = gh._spawn; children = []; cancel = threading.Event()
    def gate(args, **kwargs):
        if "fetch" in args:
            command = [*args[:args.index("fetch")], "update-ref", "--stdin"]
            child = original([sys.executable, str(script), str(first.path / "repository.git"), str(ready), local[2], *command], **kwargs)
            children.append(child); return child
        return original(args, **kwargs)
    def stop():
        import time
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline: cancel.wait(.01)
        if reason == "cancel" and ready.exists(): cancel.set()
    thread = threading.Thread(target=stop); thread.start()
    try:
        with monkeypatch.context() as m:
            m.setattr(gh, "_spawn", gate)
            limits = replace(gh.Limits(), fetch_seconds=2) if reason == "timeout" else gh.Limits()
            with pytest.raises(gh.HistoryError) as error: update(local, cancel=cancel, limits=limits)
            assert error.value.code == ("cancelled" if reason == "cancel" else "timeout")
        assert ready.exists() and "tables.list.lock" in ready.read_text()
        assert all(child.poll() is not None for child in children)
    finally: cancel.set(); thread.join(12)
    assert gh.verify_mirror(REPO, local[1]).capture == first.capture
    assert gh.export_mirror(REPO, local[1], tmp_path / "recovered").is_file()
    assert update(local).status == "UPDATED"


def test_repair_cleanup_never_removes_listed_or_preexisting_files(local):
    first = update(local); repo = first.path / "repository.git"; tables = repo / "reftable"
    before = set(p.name for p in tables.iterdir())
    preexisting = "tables.list.temp.preexisting"; (tables / preexisting).write_bytes(b"keep")
    before.add(preexisting)
    new = "tables.list.lock"; (tables / new).write_bytes(b"remove")
    listed = "0x000000000099-0x000000000099-abcdef01.ref.lock"
    (tables / listed).write_bytes(b"keep")
    with (tables / "tables.list").open("a") as file: file.write(listed + "\n")
    with gh.store.ArchiveRoot.open(str(repo)) as directory:
        gh._cleanup_fetch_transients(directory, repo, before, gh.Limits())
    assert not (tables / new).exists()
    assert (tables / listed).read_bytes() == b"keep"
    assert (tables / preexisting).read_bytes() == b"keep"


@pytest.mark.parametrize("budget", ["size", "entries"])
def test_repair2_over_budget_fetch_keeps_prior_capture_accessible(local, tmp_path, monkeypatch, budget):
    first = update(local); repo = first.path / "repository.git"
    before = first.capture.read_bytes()
    blob = git(local[0], "hash-object", "-w", "--stdin", data=os.urandom(128 * 1024))
    tree = git(local[0], "mktree", data=f"100644 blob {blob}\tlarge.bin\n".encode())
    commit = git(local[0], "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit-tree", tree, "-p", local[2], data=b"larger update\n")
    git(local[0], "update-ref", "refs/heads/main", commit)
    limits = (replace(gh.Limits(), max_working_bytes=gh._size(first.path) + 16 * 1024)
              if budget == "size" else replace(gh.Limits(), max_mirror_entries=len(list(repo.rglob("*"))) + 2))
    calls = []; original = gh._spawn
    def record(args, **kwargs): calls.append(args); return original(args, **kwargs)
    monkeypatch.setattr(gh, "_spawn", record)
    with pytest.raises(gh.HistoryError) as error: update(local, limits=limits)
    assert error.value.code == "too_large"
    assert any("fetch" in args for args in calls), "must cross the budget during a real fetch"
    assert first.capture.read_bytes() == before
    assert len(list(first.capture.parent.glob("capture-*.json"))) == 1
    if budget == "size": assert gh._size(first.path) > limits.max_working_bytes
    else: assert len(list(repo.rglob("*"))) > limits.max_mirror_entries
    assert gh.verify_mirror(REPO, local[1], limits=limits).capture == first.capture
    output = gh.export_mirror(REPO, local[1], tmp_path / "export", limits=limits)
    assert f"{local[2]} refs/heads/main" in git(tmp_path, "bundle", "list-heads", str(output))
    assert document(update(local))["refs"]["refs/heads/main"] == commit


def test_repair2_oversized_quarantine_is_cleaned_after_reaping(local, tmp_path, monkeypatch):
    first = update(local); repo = first.path / "repository.git"
    newer = local[3](local[2]); git(local[0], "update-ref", "refs/heads/main", newer)
    quarantine = repo / "objects" / "tmp_objdir-incoming-A1b2C3"
    original = gh._spawn; children = []
    script = "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); p.mkdir(); (p/'unpublished').write_bytes(b'x'*131072); time.sleep(30)"
    def incoming(args, **kwargs):
        if "fetch" in args:
            child = original([sys.executable, "-c", script, str(quarantine)], **kwargs)
            children.append(child); return child
        return original(args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(gh, "_spawn", incoming)
        with pytest.raises(gh.HistoryError) as error:
            update(local, limits=replace(gh.Limits(), max_working_bytes=gh._size(first.path) + 16 * 1024))
        assert error.value.code == "too_large"
    assert children and all(child.poll() is not None for child in children)
    assert not quarantine.exists()
    assert gh.verify_mirror(REPO, local[1]).capture == first.capture
    assert gh.export_mirror(REPO, local[1], tmp_path / "export").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess umask; Windows creation policy is unchanged")
def test_repair2_permissive_parent_umask_does_not_poison_mirror(local):
    update(local)
    newer = local[3](local[2]); git(local[0], "update-ref", "refs/heads/main", newer)
    previous = os.umask(0o002)
    try:
        second = update(local)
        assert gh.verify_mirror(REPO, local[1]).capture == second.capture
        assert update(local).status == "UNCHANGED"
    finally: os.umask(previous)
    assert all(not path.stat().st_mode & 0o022 for path in (second.path / "repository.git").rglob("*"))


@pytest.mark.parametrize("field", ["max_mirror_read_bytes", "max_mirror_read_entries"])
def test_repair2_read_access_keeps_independent_hard_bounds(local, tmp_path, field):
    first = update(local); before = snapshot_bytes(first.path)
    limits = replace(gh.Limits(), **{field: 1})
    for action in (lambda: gh.verify_mirror(REPO, local[1], limits=limits),
                   lambda: gh.export_mirror(REPO, local[1], tmp_path / "export", limits=limits)):
        with pytest.raises(gh.HistoryError) as error: action()
        assert error.value.code == "too_large"
    assert snapshot_bytes(first.path) == before


def test_repair2_quarantine_cleanup_keeps_preexisting_and_aliased_data(local, tmp_path):
    first = update(local); repo = first.path / "repository.git"; objects = repo / "objects"
    preserved = objects / "incoming-OLD123"; preserved.mkdir(); (preserved / "keep").write_bytes(b"old")
    before = set(p.name for p in objects.iterdir())
    new = objects / "tmp_objdir-incoming-NEW123"; new.mkdir(); (new / "unpublished").write_bytes(b"new")
    linked = objects / "incoming-ALIAS1"; linked.mkdir()
    old = tmp_path / "historical-object"; old.write_bytes(b"must keep")
    os.link(old, linked / "alias")
    with gh.store.ArchiveRoot.open(str(repo)) as directory:
        gh._cleanup_fetch_transients(directory, repo, set(p.name for p in (repo / "reftable").iterdir()), gh.Limits(), objects_before=before)
    assert not new.exists()
    assert (preserved / "keep").read_bytes() == b"old"
    assert old.read_bytes() == b"must keep" and (linked / "alias").exists()


def test_repair3_many_captures_have_bounded_reads_and_old_export(local, tmp_path, monkeypatch):
    for i in range(12): git(local[0], "update-ref", f"refs/heads/branch-{i:02}", local[2])
    limits = replace(gh.Limits(), max_stdout_bytes=4096)
    first = update(local, limits=limits); previous = local[2]
    for _ in range(9):
        previous = local[3](previous); git(local[0], "update-ref", "refs/heads/main", previous)
        update(local, limits=limits)
    assert gh.verify_mirror(REPO, local[1], limits=limits).status == "VERIFIED"
    original = gh._run_git
    def selected(args, **kwargs):
        if "fsck" in args and "--git-dir=" + str(first.path / "repository.git") in args:
            pytest.fail("Old export must not scan every retained object's contents")
        return original(args, **kwargs)
    monkeypatch.setattr(gh, "_run_git", selected)
    output = gh.export_mirror(REPO, local[1], tmp_path / "export", capture=first.capture.name,
        limits=replace(limits, max_mirror_metadata_bytes=len(first.capture.read_bytes()) + 32))
    assert f"{local[2]} refs/heads/main" in git(tmp_path, "bundle", "list-heads", str(output))


def test_repair3_single_capture_output_budget_rejected_before_fetch(local, monkeypatch):
    first = update(local)
    for i in range(40): git(local[0], "update-ref", f"refs/heads/branch-{i:02}", local[2])
    calls = []; original = gh._run_git
    def record(args, **kwargs): calls.append(args); return original(args, **kwargs)
    monkeypatch.setattr(gh, "_run_git", record)
    with pytest.raises(gh.HistoryError) as error: update(local, limits=replace(gh.Limits(), max_stdout_bytes=4096))
    assert error.value.code == "too_large"
    assert not any("fetch" in args for args in calls)
    assert gh.verify_mirror(REPO, local[1]).capture == first.capture


def test_repair3_metadata_capacity_rejected_before_fetch(local, monkeypatch):
    first = update(local)
    newer = local[3](local[2]); git(local[0], "update-ref", "refs/heads/main", newer)
    calls = []; original = gh._run_git
    def record(args, **kwargs): calls.append(args); return original(args, **kwargs)
    monkeypatch.setattr(gh, "_run_git", record)
    with pytest.raises(gh.HistoryError) as error:
        update(local, limits=replace(gh.Limits(), max_mirror_metadata_bytes=2 * len(first.capture.read_bytes()) - 1))
    assert error.value.code == "too_large"
    assert not any("fetch" in args for args in calls)
    assert gh.verify_mirror(REPO, local[1]).capture == first.capture


def test_repair3_fetch_race_cannot_expand_admitted_capture(local, monkeypatch):
    update(local)
    newer = local[3](local[2]); git(local[0], "update-ref", "refs/heads/main", newer)
    original = gh._run_git
    def expanded(args, **kwargs):
        if "fetch" in args:
            for i in range(40): git(local[0], "update-ref", f"refs/heads/extra-{i:02}", local[2])
        return original(args, **kwargs)
    monkeypatch.setattr(gh, "_run_git", expanded)
    assert update(local, limits=replace(gh.Limits(), max_stdout_bytes=4096)).ref_count == 2


def test_repair3_capture_capacity_stops_ingestion_but_keeps_reads(local, tmp_path, monkeypatch):
    limits = replace(gh.Limits(), max_captures=2)
    first = update(local, limits=limits)
    second = local[3](local[2]); git(local[0], "update-ref", "refs/heads/main", second)
    update(local, limits=limits)
    third = local[3](second); git(local[0], "update-ref", "refs/heads/main", third)
    original = gh._run_git
    def no_fetch(args, **kwargs):
        assert "fetch" not in args, "capacity must be enforced before Git retains another namespace"
        return original(args, **kwargs)
    monkeypatch.setattr(gh, "_run_git", no_fetch)
    with pytest.raises(gh.HistoryError) as error: update(local, limits=limits)
    assert error.value.code == "too_large"
    assert gh.verify_mirror(REPO, local[1], limits=limits).status == "VERIFIED"
    assert gh.export_mirror(REPO, local[1], tmp_path / "export", capture=first.capture.name, limits=limits).is_file()


def test_repair3_update_preflight_reads_latest_namespace_only(local, monkeypatch):
    first = update(local); latest = first; previous = local[2]
    for _ in range(3):
        previous = local[3](previous); git(local[0], "update-ref", "refs/heads/main", previous)
        latest = update(local)
    previous = local[3](previous); git(local[0], "update-ref", "refs/heads/main", previous)
    calls = []; original = gh._run_git
    def record(args, **kwargs): calls.append(args); return original(args, **kwargs)
    monkeypatch.setattr(gh, "_run_git", record)
    update(local)
    old_prefix = "refs/covearchive/captures/" + first.capture.name[8:-5] + "/"
    latest_prefix = "refs/covearchive/captures/" + latest.capture.name[8:-5] + "/"
    assert any(latest_prefix in args for args in calls)
    assert not any(old_prefix in args for args in calls)


def test_repair3_long_head_name_rejected_before_fetch(local, monkeypatch):
    update(local)
    ref = "refs/heads/" + "a" * 1024
    git(local[0], "update-ref", ref, local[2]); git(local[0], "symbolic-ref", "HEAD", ref)
    original = gh._run_git
    def no_fetch(args, **kwargs):
        assert "fetch" not in args
        return original(args, **kwargs)
    monkeypatch.setattr(gh, "_run_git", no_fetch)
    with pytest.raises(gh.HistoryError) as error: update(local)
    assert error.value.code == "too_large"


@pytest.mark.parametrize("operation", ["verify", "export"])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_repair3_fsck_diagnostics_stay_bounded_without_masking_errors(local, tmp_path, monkeypatch, operation, exit_code):
    first = update(local); original = gh._spawn
    def diagnostics(args, **kwargs):
        if "fsck" in args:
            return original([sys.executable, "-c", f"import sys; sys.stderr.write('warning: legacy object metadata\\n'*200); sys.exit({exit_code})"], **kwargs)
        return original(args, **kwargs)
    monkeypatch.setattr(gh, "_spawn", diagnostics)
    limits = replace(gh.Limits(), max_stderr_bytes=1024)  # Allow init hints; audit output still exceeds this cap.
    action = (lambda: gh.verify_mirror(REPO, local[1], limits=limits)) if operation == "verify" else (
        lambda: gh.export_mirror(REPO, local[1], tmp_path / "export", capture=first.capture.name, limits=limits))
    if exit_code:
        with pytest.raises(gh.HistoryError) as error: action()
        assert error.value.code == "invalid_objects"
    else:
        result = action()
        assert result.status == "VERIFIED" if operation == "verify" else result.is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX metadata ownership/modes; Windows uses descriptor checks")
@pytest.mark.parametrize("target", ["identity", "captures", "capture", "history_lock", "mirror_lock"])
@pytest.mark.parametrize("violation", ["writable", "foreign_owner"])
@pytest.mark.parametrize("operation", ["update", "verify", "export"])
def test_repair4_metadata_trust_precedes_recovery_and_git(local, tmp_path, monkeypatch, target, violation, operation):
    first = update(local)
    mirror_lock = first.path / ".covearchive.lock"; mirror_lock.write_bytes(b""); mirror_lock.chmod(0o600)
    paths = dict(identity=first.path / "identity.json", captures=first.path / "captures", capture=first.capture,
                 history_lock=first.path.parent / ".covearchive.lock", mirror_lock=mirror_lock)
    original = Path.lstat
    def untrusted(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == paths[target]:
            values = list(info)
            if violation == "writable": values[0] |= 0o022
            else: values[4] = info.st_uid + 1
            return os.stat_result(values)
        return info
    monkeypatch.setattr(Path, "lstat", untrusted)
    monkeypatch.setattr(gh.store, "recover", lambda *a: pytest.fail("Recovery ran before metadata trust"))
    monkeypatch.setattr(gh, "_run_git", lambda *a, **kw: pytest.fail("Git ran before metadata trust"))
    actions = dict(update=lambda: update(local), verify=lambda: gh.verify_mirror(REPO, local[1]),
        export=lambda: gh.export_mirror(REPO, local[1], tmp_path / "export", capture=first.capture.name))
    with pytest.raises(gh.HistoryError) as error: actions[operation]()
    assert error.value.code == "unsafe_path"


def test_repair4_full_verify_counts_real_objects_but_old_export_is_scoped(local, tmp_path, monkeypatch):
    first = update(local)
    previous = local[2]
    for _ in range(3):
        blob = git(local[0], "hash-object", "-w", "--stdin", data=os.urandom(12 * 1024))
        tree = git(local[0], "mktree", data=f"100644 blob {blob}\tlarge.bin\n".encode())
        previous = git(local[0], "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit-tree", tree,
                       "-p", previous, data=b"captured object bytes\n")
        git(local[0], "update-ref", "refs/heads/main", previous); update(local)
    limits = replace(gh.Limits(), max_mirror_read_bytes=32 * 1024)
    original = gh._run_git
    def no_fsck(args, **kwargs):
        assert "fsck" not in args, "full verification must reject the object byte budget before fsck"
        return original(args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(gh, "_run_git", no_fsck)
        with pytest.raises(gh.HistoryError) as error: gh.verify_mirror(REPO, local[1], limits=limits)
        assert error.value.code == "too_large"
    output = gh.export_mirror(REPO, local[1], tmp_path / "export", capture=first.capture.name, limits=limits)
    assert f"{local[2]} refs/heads/main" in git(tmp_path, "bundle", "list-heads", str(output))
    assert gh.verify_mirror(REPO, local[1]).status == "VERIFIED"


@pytest.mark.skipif(os.name == "nt", reason="POSIX metadata trust; Windows descriptor/reparse checks are separate")
@pytest.mark.parametrize("kind", ["hardlink", "symlink"])
def test_repair4_metadata_external_alias_fails_closed(local, tmp_path, monkeypatch, kind):
    first = update(local); external = tmp_path / "external"
    if kind == "hardlink": os.link(first.capture, external)
    else:
        external.write_bytes(first.capture.read_bytes()); first.capture.unlink(); first.capture.symlink_to(external)
    monkeypatch.setattr(gh.store, "recover", lambda *a: pytest.fail("Unsafe alias reached recovery"))
    monkeypatch.setattr(gh, "_run_git", lambda *a, **kw: pytest.fail("Unsafe alias reached Git"))
    with pytest.raises(gh.HistoryError) as error: gh.verify_mirror(REPO, local[1])
    assert error.value.code == "unsafe_path"


@pytest.mark.skipif(os.name == "nt", reason="POSIX sticky-directory semantics")
def test_repair4_owned_sticky_capture_directory_remains_usable(local):
    first = update(local); directory = first.path / "captures"; previous = directory.stat().st_mode
    try:
        directory.chmod(0o1777)
        assert gh.verify_mirror(REPO, local[1]).capture == first.capture
    finally: directory.chmod(previous)


@pytest.mark.skipif(os.name == "nt", reason="POSIX metadata modes")
def test_repair4_selected_export_checks_other_capture_permissions(local, tmp_path, monkeypatch):
    first = update(local); newer = local[3](local[2]); git(local[0], "update-ref", "refs/heads/main", newer)
    second = update(local); original = Path.lstat
    def writable(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == second.capture:
            values = list(info); values[0] |= 0o022; return os.stat_result(values)
        return info
    monkeypatch.setattr(Path, "lstat", writable)
    monkeypatch.setattr(gh.store, "recover", lambda *a: pytest.fail("Untrusted unselected metadata reached recovery"))
    with pytest.raises(gh.HistoryError) as error:
        gh.export_mirror(REPO, local[1], tmp_path / "export", capture=first.capture.name)
    assert error.value.code == "unsafe_path"


def test_repair4_missing_selected_capture_keeps_specific_error(local, tmp_path, monkeypatch):
    update(local)
    monkeypatch.setattr(gh, "_run_git", lambda *a, **kw: pytest.fail("Missing capture reached Git"))
    with pytest.raises(gh.HistoryError) as error:
        gh.export_mirror(REPO, local[1], tmp_path / "export", capture="capture-" + "0" * 32 + ".json")
    assert error.value.code == "missing_capture"


def mirror_tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(b"D" if path.is_dir() else b"F" + path.read_bytes())
    return digest.hexdigest()


@pytest.mark.parametrize("kind", ["mirror", "child", "symlink", "symlink_parent", "containing_directory", "symlink_containing_directory"])
def test_repair5_export_overlap_rejected_without_writes_and_mirror_stays_usable(local, tmp_path, monkeypatch, kind):
    if "symlink" in kind and os.name == "nt":
        pytest.skip("Native Windows junction/reparse cases require the pending native verification environment")
    first = update(local); bundle = gh.archive_history(REPO, local[1])
    saved_bundle = snapshot_bytes(bundle.snapshot)
    if kind == "mirror": destination = first.path
    elif kind == "child": destination = first.path / "export"
    elif kind == "containing_directory": destination = first.path.parent
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(first.path.parent if kind == "symlink_containing_directory" else first.path / "captures", target_is_directory=True)
        destination = alias / "export" if kind == "symlink_parent" else alias
    before = mirror_tree_digest(first.path)
    def forbidden(*args, **kwargs): pytest.fail("Unsafe destination reached staging, recovery or Git")
    with monkeypatch.context() as m:
        m.setattr(gh, "_temporary", forbidden); m.setattr(gh.store, "recover", forbidden); m.setattr(gh, "_run_git", forbidden)
        with pytest.raises(gh.HistoryError) as error:
            gh.export_mirror(REPO, local[1], destination, capture=first.capture.name)
        assert error.value.code == "unsafe_destination"
    assert mirror_tree_digest(first.path) == before
    assert gh.verify_mirror(REPO, local[1]).capture == first.capture
    assert update(local).status == "UNCHANGED"
    assert gh.export_mirror(REPO, local[1], tmp_path / "outside", capture=first.capture.name).is_file()
    assert mirror_tree_digest(first.path) == before and snapshot_bytes(bundle.snapshot) == saved_bundle


def test_repair5_export_resolution_error_fails_closed_before_writes(local, tmp_path, monkeypatch):
    first = update(local); before = mirror_tree_digest(first.path)
    destination = tmp_path / "outside" / "export"
    destination.parent.mkdir()
    original = Path.resolve
    def denied(path, *args, **kwargs):
        if path == destination.parent: raise OSError("resolution denied")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(Path, "resolve", denied)
        m.setattr(gh, "_temporary", lambda: pytest.fail("Resolution failure reached staging"))
        with pytest.raises(gh.HistoryError) as error: gh.export_mirror(REPO, local[1], destination)
        assert error.value.code == "unsafe_destination"
    assert mirror_tree_digest(first.path) == before
    assert gh.verify_mirror(REPO, local[1]).status == "VERIFIED"
    assert update(local).status == "UNCHANGED"
    assert gh.export_mirror(REPO, local[1], tmp_path / "recovered").is_file()
    assert mirror_tree_digest(first.path) == before


def test_repair5_normal_outside_export_stays_supported(local, tmp_path):
    first = update(local); before = mirror_tree_digest(first.path)
    output = gh.export_mirror(REPO, local[1], tmp_path / "outside")
    assert output.is_file() and mirror_tree_digest(first.path) == before
    assert f"{local[2]} refs/heads/main" in git(tmp_path, "bundle", "list-heads", str(output))


def test_windows_acceptance_nested_branch_and_tag_names_are_captured_and_exported(local, tmp_path):
    source, root, first, commit = local
    for ref in ("refs/heads/feature/deep/x", "refs/heads/Feature/X", "refs/tags/rel/1.0"): git(source, "update-ref", ref, first)
    result = update(local)
    assert {"refs/heads/feature/deep/x", "refs/heads/Feature/X", "refs/tags/rel/1.0"} <= set(document(result)["refs"])
    assert gh.verify_mirror(REPO, root).status == "VERIFIED"
    heads = git(tmp_path, "bundle", "list-heads", str(gh.export_mirror(REPO, root, tmp_path / "nested")))
    assert f"{first} refs/heads/feature/deep/x" in heads and f"{first} refs/tags/rel/1.0" in heads


@pytest.mark.skipif(os.name == "nt", reason="POSIX ancestor symlink retarget; native junction verification is pending")
@pytest.mark.parametrize("operation", ["update", "verify", "export"])
def test_mirror_root_retarget_after_trust_stays_on_validated_root(local, tmp_path, monkeypatch, operation):
    first = update(local)
    attacker = tmp_path / "attacker"; attacker.mkdir()
    shutil.copytree(local[1], attacker / "archive")
    before = snapshot_bytes(attacker / "archive")
    alias = tmp_path / "alias"; alias.symlink_to(tmp_path, target_is_directory=True)
    root = alias / "archive"
    newer = local[3](local[2]); git(local[0], "update-ref", "refs/heads/main", newer)
    trust = gh._trust_ancestors; opened = gh.store.ArchiveRoot.open
    with opened(str(attacker / "archive")) as directory: attacker_identity = directory.identity
    redirected = []; swapped = []
    def retarget(path, archive_root):
        trust(path, archive_root)
        if path == local[1] and not swapped:
            alias.unlink(); alias.symlink_to(attacker, target_is_directory=True); swapped.append(True)
    def record(path):
        directory = opened(path)
        if directory.identity == attacker_identity: redirected.append(path)
        return directory
    monkeypatch.setattr(gh, "_trust_ancestors", retarget)
    monkeypatch.setattr(gh.store.ArchiveRoot, "open", staticmethod(record))
    actions = dict(update=lambda: gh.update_mirror(REPO, root), verify=lambda: gh.verify_mirror(REPO, root),
        export=lambda: gh.export_mirror(REPO, root, tmp_path / "export"))
    try:
        result = actions[operation]()
    except gh.HistoryError as error:
        assert error.code == "unsafe_path"
    else:
        if operation == "update":
            assert result.path == first.path and document(result)["refs"]["refs/heads/main"] == newer
        elif operation == "verify": assert result.capture == first.capture
        else: assert f"{local[2]} refs/heads/main" in git(tmp_path, "bundle", "list-heads", str(result))
    assert swapped and not redirected
    assert snapshot_bytes(attacker / "archive") == before


@pytest.mark.parametrize("operation", ["update", "verify", "export"])
def test_mirror_root_handle_identity_mismatch_fails_closed(local, tmp_path, monkeypatch, operation):
    update(local)
    attacker = tmp_path / "attacker"
    shutil.copytree(local[1], attacker)
    before = snapshot_bytes(attacker)
    opened = gh.store.ArchiveRoot.open; calls = []
    def substitute(path):
        if Path(path) == local[1]:
            calls.append(path)
            if len(calls) == 2: return opened(str(attacker))
        return opened(path)
    monkeypatch.setattr(gh.store.ArchiveRoot, "open", staticmethod(substitute))
    monkeypatch.setattr(gh, "_run_git", lambda *a, **kw: pytest.fail("Mismatched root reached Git"))
    actions = dict(update=lambda: update(local), verify=lambda: gh.verify_mirror(REPO, local[1]),
        export=lambda: gh.export_mirror(REPO, local[1], tmp_path / "export"))
    with pytest.raises(gh.HistoryError) as error: actions[operation]()
    assert error.value.code == "unsafe_path"
    assert len(calls) == 2 and snapshot_bytes(attacker) == before
    assert not (tmp_path / "export").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ancestor symlink retarget; native junction verification is pending")
def test_export_root_retarget_after_overlap_stays_on_validated_root(local, tmp_path, monkeypatch):
    first = update(local)
    attacker = tmp_path / "attacker"; attacker.mkdir()
    shutil.copytree(local[1], attacker / "archive")
    alias = tmp_path / "alias"; alias.symlink_to(tmp_path, target_is_directory=True)
    before = snapshot_bytes(attacker)
    destination = gh._export_destination; opened = gh.store.ArchiveRoot.open
    with opened(str(attacker / "archive")) as directory: attacker_identity = directory.identity
    redirected = []
    def retarget(*args, **kwargs):
        result = destination(*args, **kwargs)
        alias.unlink(); alias.symlink_to(attacker, target_is_directory=True)
        return result
    def record(path):
        directory = opened(path)
        if directory.identity == attacker_identity: redirected.append(path)
        return directory
    monkeypatch.setattr(gh, "_export_destination", retarget)
    monkeypatch.setattr(gh.store.ArchiveRoot, "open", staticmethod(record))
    try:
        result = gh.export_mirror(REPO, alias / "archive", tmp_path / "export", capture=first.capture.name)
    except gh.HistoryError as error:
        assert error.code == "unsafe_path"
    else: assert result.is_file()
    assert not redirected and snapshot_bytes(attacker) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX ancestor symlink retarget; native junction verification is pending")
def test_export_parent_retarget_during_resolution_fails_before_writes(local, tmp_path, monkeypatch):
    first = update(local)
    outside = tmp_path / "outside"; (outside / "parent").mkdir(parents=True)
    attacker = tmp_path / "attacker"; (attacker / "parent").mkdir(parents=True)
    saved = tmp_path / "saved-outside"
    destination = outside / "parent" / "export"
    resolve = Path.resolve; swapped = []
    def retarget(path, *args, **kwargs):
        result = resolve(path, *args, **kwargs)
        if path == destination.parent and not swapped:
            outside.rename(saved); outside.symlink_to(attacker, target_is_directory=True); swapped.append(True)
        return result
    monkeypatch.setattr(Path, "resolve", retarget)
    monkeypatch.setattr(gh, "_run_git", lambda *a, **kw: pytest.fail("Retargeted parent reached Git"))
    with pytest.raises(gh.HistoryError) as error:
        gh.export_mirror(REPO, local[1], destination, capture=first.capture.name)
    assert error.value.code == "unsafe_destination" and swapped
    assert not (attacker / "parent" / "export").exists()
    assert not (saved / "parent" / "export").exists()
