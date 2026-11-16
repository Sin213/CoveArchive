"""Offline history contract. Private seam changes only URL and allowed protocols.

Production must never expose _remote_url/_test_protocols through environment,
configuration, CLI or widgets. All ordinary integration cases use real Git.
Snapshot API: result.snapshot and status rows' path are snapshot directory Paths;
status rows expose status (valid/corrupt/incomplete). verify_snapshot raises on damage.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest

from covearchive.archive import githistory as gh, store
from covearchive.github.urls import RepositoryTarget

REPO = RepositoryTarget("Owner", "Repo")


def git(path, *args, check=True, data=None):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run([shutil.which("git"), "-C", str(path), *args], input=data,
                            capture_output=True, env=env, timeout=15)
    if check:
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    else:
        return result
    return result.stdout.decode().strip()


@pytest.fixture
def local(tmp_path, monkeypatch):
    source = tmp_path / "source"; source.mkdir()
    git(source, "init", "--bare", "--template=", "--ref-format=reftable")
    tree = git(source, "mktree", data=b"")
    env = dict(GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
    for key, value in env.items(): monkeypatch.setenv(key, value)
    # commit-tree author identity is provided by command config, not user settings.
    sequence = [0]
    def commit(parent=None):
        sequence[0] += 1
        args = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit-tree", tree]
        if parent: args += ["-p", parent]
        return git(source, *args, data=f"history test {sequence[0]}\n".encode())
    first = commit()
    git(source, "update-ref", "refs/heads/main", first)
    git(source, "symbolic-ref", "HEAD", "refs/heads/main")
    git(source, "update-ref", "refs/tags/v1", first)
    root = tmp_path / "archive"; root.mkdir()
    monkeypatch.setattr(gh, "_remote_url", lambda repository: source.as_uri())
    monkeypatch.setattr(gh, "_test_protocols", ("file",))
    return source, root, first, commit


def archive(local, **kw):
    return gh.archive_history(REPO, local[1], **kw)


def snapshot_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def remove_source(path):
    def writable(function, name, exc):
        os.chmod(name, stat.S_IWRITE | stat.S_IREAD); function(name)
    shutil.rmtree(path, onerror=writable)


def manifest(snapshot):
    return json.loads((Path(snapshot) / "history.json").read_text())


def test_initial_repeat_and_incremental_zero_transfer(local, monkeypatch):
    first = archive(local)
    assert first.status == "ARCHIVED" and first.ref_count == 2 and first.size > 0
    saved = snapshot_bytes(first.snapshot)
    calls = []; spawn = gh._spawn
    def record(args, **kw):
        calls.append(args); return spawn(args, **kw)
    monkeypatch.setattr(gh, "_spawn", record)
    repeat = archive(local)
    assert repeat.status == "UNCHANGED" and repeat.snapshot == first.snapshot
    assert not any("fetch" in a or "bundle" in a for a in calls)
    newer = local[3](local[2]); git(local[0], "update-ref", "refs/heads/main", newer)
    second = archive(local)
    assert second.status == "ARCHIVED" and second.snapshot != first.snapshot
    assert snapshot_bytes(first.snapshot) == saved
    assert manifest(second.snapshot)["refs_digest"] != manifest(first.snapshot)["refs_digest"]


@pytest.mark.parametrize("change", ["delete_branch", "delete_tag", "move_tag", "rewrite"])
def test_rewritten_deleted_tips_remain_restorable(local, tmp_path, change):
    git(local[0], "update-ref", "refs/heads/old", local[2])
    before = archive(local); saved = snapshot_bytes(before.snapshot)
    new = local[3](local[2] if change != "rewrite" else None)
    if change.startswith("delete"):
        ref = "refs/heads/old" if change == "delete_branch" else "refs/tags/v1"
        git(local[0], "update-ref", "-d", ref)
    else:
        ref = "refs/tags/v1" if change == "move_tag" else "refs/heads/main"
        git(local[0], "update-ref", ref, new)
    after = archive(local)
    refs = {r["name"]: r["sha"] for r in manifest(after.snapshot)["refs"]}
    assert ref not in refs if change.startswith("delete") else refs[ref] == new
    assert snapshot_bytes(before.snapshot) == saved
    restored = tmp_path / "restored"
    git(tmp_path, "clone", "--ref-format=reftable", str(Path(before.snapshot) / "history.bundle"), str(restored))
    assert local[2] in git(restored, "rev-list", "--all").splitlines()
    git(restored, "fsck", "--full")


def test_restore_without_remote_special_refs_and_head(local, tmp_path):
    names = ["Foo", "foo", "aux", "con", "long-" + "a" * 150]
    for name in names: git(local[0], "update-ref", "refs/heads/" + name, local[2])
    git(local[0], "update-ref", "refs/pull/1/head", local[2])
    git(local[0], "update-ref", "refs/notes/test", local[2])
    result = archive(local)
    remove_source(local[0])
    restored = tmp_path / "restored"
    git(tmp_path, "clone", "--bare", "--ref-format=reftable", str(Path(result.snapshot) / "history.bundle"), str(restored))
    assert git(restored, "symbolic-ref", "HEAD") == "refs/heads/main"
    refs = git(restored, "for-each-ref", "--format=%(refname)").splitlines()
    assert set(refs) == {"refs/heads/" + n for n in ["main", *names]} | {"refs/tags/v1"}
    git(restored, "fsck", "--full")


def test_missing_upstream_preserves_snapshots(local):
    result = archive(local); saved = snapshot_bytes(local[1]); remove_source(local[0])
    with pytest.raises(gh.HistoryError) as error: archive(local)
    assert error.value.code in {"not_found", "network"}
    assert snapshot_bytes(local[1]) == saved


@pytest.mark.parametrize("damage", ["truncate", "flip", "missing"])
def test_damaged_bundle_detected_and_new_snapshot_retains_damage(local, damage):
    result = archive(local); bundle = Path(result.snapshot) / "history.bundle"
    if damage == "missing": bundle.unlink()
    else:
        raw = bundle.read_bytes()
        bundle.write_bytes(raw[:20] if damage == "truncate" else raw[:-1] + bytes([raw[-1] ^ 1]))
    damaged = snapshot_bytes(result.snapshot)
    with pytest.raises(gh.HistoryError, match="."): gh.verify_snapshot(result.snapshot)
    assert any(s.status == "corrupt" for s in gh.history_status(REPO, local[1], verify=True))
    replacement = archive(local)
    assert replacement.status == "ARCHIVED" and replacement.snapshot != result.snapshot
    assert snapshot_bytes(result.snapshot) == damaged


@pytest.mark.parametrize("damage", ["schema", "duplicate", "oversize", "depth", "unknown", "filename", "digest", "emptyrefs", "multiple_links"])
def test_strict_manifest_rejection_without_modification(local, damage):
    result = archive(local); path = Path(result.snapshot) / "history.json"; value = manifest(result.snapshot)
    if damage == "duplicate": raw = b'{"schema":1,' + path.read_bytes()[1:]
    elif damage == "oversize": raw = b" " * (16 * 1024**2 + 1)
    elif damage == "depth": raw = b"[" * 2000 + b"0" + b"]" * 2000
    else:
        if damage == "schema": value["schema"] = 999
        elif damage == "unknown": value["unexpected"] = 1
        elif damage == "filename": value["bundle"]["name"] = "../escape"
        elif damage == "emptyrefs": value["refs"]=[]; value["head"]=None; value["refs_digest"]=gh._digest({},None)
        elif damage == "multiple_links": os.link(path,path.with_name("linked.json"))
        else: value["refs_digest"] = "xyz"
        raw = json.dumps(value).encode()
    path.write_bytes(raw); saved = snapshot_bytes(result.snapshot)
    assert any(s.status == "corrupt" for s in gh.history_status(REPO, local[1]))
    assert snapshot_bytes(result.snapshot) == saved
    assert archive(local).status == "ARCHIVED"


def test_interruption_incomplete_and_release_isolation(local):
    root = local[1]
    with store.ArchiveRoot.open(str(root)) as opened:
        with store.ensure_project(opened, REPO) as project: before = store.load_project_state(project)
    result = archive(local); history = Path(result.snapshot).parent
    incomplete = history / "snapshot-20000101T000000Z--000000000000"; incomplete.mkdir()
    (incomplete / "history.bundle").write_bytes(b"interrupted")
    assert any(s.status == "incomplete" for s in gh.history_status(REPO, root))
    assert archive(local).status == "UNCHANGED"
    assert (incomplete / "history.bundle").read_bytes() == b"interrupted"
    with store.ArchiveRoot.open(str(root)) as opened:
        with store.ensure_project(opened, REPO) as project:
            assert store.load_project_state(project) == before
            with store.ensure_release(project, "release-v1--7", 7) as release:
                assert store.write_asset(release, "payload.zip", [b"release"], 7).code == "published"


@pytest.mark.parametrize("repository,parts,url", [
    (REPO, ("Owner", "Repo"), "https://github.com/Owner/Repo.git"),
    (RepositoryTarget("Owner", "Repo", "codeberg", "codeberg.org"), ("codeberg.org", "Owner", "Repo"), "https://codeberg.org/Owner/Repo.git"),
    (RepositoryTarget("group/sub", "Repo", "gitlab", "gitlab.com"), ("gitlab.com", "group", "sub", "Repo"), "https://gitlab.com/group/sub/Repo.git"),
])
def test_canonical_url_and_provider_layout(tmp_path, repository, parts, url):
    assert gh._remote_url(repository) == url
    with store.ArchiveRoot.open(str(tmp_path)) as opened:
        with store.ensure_project(opened, repository): pass
    assert tmp_path.joinpath(*parts).is_dir()


def test_manifest_digest_and_bundle_commit_marker(local, monkeypatch):
    writes = []; original = store.write_asset
    def write(directory, name, *args, **kw):
        writes.append(name); return original(directory, name, *args, **kw)
    monkeypatch.setattr(store, "write_asset", write)
    result = archive(local); doc = manifest(result.snapshot)
    refs = doc["refs"]
    assert refs == sorted(refs, key=lambda r: r["name"])
    lines = "".join(r["name"] + "\0" + r["sha"] + "\n" for r in refs)
    # HEAD uses the same name-NUL-target-newline encoding as refs.
    assert doc["refs_digest"] == hashlib.sha256((lines + "HEAD\0" + doc["head"] + "\n").encode()).hexdigest()
    assert writes[-2:] == ["history.bundle", "history.json"]
    assert doc["schema"] == 1 and doc["bundle"]["name"] == "history.bundle"
    assert doc["source_url"] == "https://github.com/Owner/Repo"


def test_idle_orphan_recovered_but_incomplete_snapshot_and_lookalike_retained(local, monkeypatch):
    history = local[1] / "Owner" / "Repo" / "git-history"; history.mkdir(parents=True)
    incomplete = history / "snapshot-20000101T000000Z--000000000000"; incomplete.mkdir()
    orphan = incomplete / (".covearchive-tmp-" + "a" * 32 + ".part")
    lookalike = incomplete / ".covearchive-tmp-not-a-token.part"
    bundle = incomplete / "history.bundle"
    parent_orphan = history / orphan.name
    for path in [orphan, parent_orphan, lookalike, bundle]: path.write_bytes(b"interrupted bytes")
    calls = []; recover = store.recover
    def recovered(directory):
        result = recover(directory); calls.append(result); return result
    monkeypatch.setattr(store, "recover", recovered)
    result = archive(local)
    assert result.status == "ARCHIVED" and not orphan.exists() and not parent_orphan.exists()
    assert any(orphan.name in r.removed for r in calls), "reuse store.recover"
    assert lookalike.read_bytes() == bundle.read_bytes() == b"interrupted bytes"
    assert any(s.path == incomplete and s.status == "incomplete" for s in gh.history_status(REPO, local[1]))


def test_fetch_movement_uses_local_authoritative_refs(local, monkeypatch):
    spawn = gh._spawn; moved = []
    def race(args, **kw):
        if "fetch" in args and not moved:
            moved.append(local[3](local[2])); git(local[0], "update-ref", "refs/heads/main", moved[0])
        return spawn(args, **kw)
    monkeypatch.setattr(gh, "_spawn", race)
    result = archive(local)
    assert moved
    assert {r["name"]: r["sha"] for r in manifest(result.snapshot)["refs"]}["refs/heads/main"] == moved[0]


def test_repeat_hashes_only_newest_candidate_and_status_is_light(local, monkeypatch):
    snapshots = [archive(local)]
    for _ in range(2):
        git(local[0], "update-ref", "refs/heads/main", local[3](local[2])); snapshots.append(archive(local))
    calls = []; verify = gh._verify
    def hashed(directory, doc, **kw): calls.append(directory.name); return verify(directory, doc, **kw)
    monkeypatch.setattr(gh, "_verify", hashed)
    assert archive(local).status == "UNCHANGED"
    assert calls == [Path(snapshots[-1].snapshot).name]
    calls.clear(); assert len(gh.history_status(REPO, local[1])) == 3 and calls == []
    assert len(gh.history_status(REPO, local[1], verify=True)) == 3 and len(calls) == 3


def test_revert_past_damaged_newest_is_a_change(local):
    first = archive(local)
    git(local[0], "update-ref", "refs/heads/main", local[3](local[2])); newest = archive(local)
    bundle = Path(newest.snapshot)/"history.bundle"; bundle.write_bytes(b"damaged")
    saved = snapshot_bytes(newest.snapshot)
    git(local[0], "update-ref", "refs/heads/main", local[2])
    result = archive(local)
    assert result.status == "ARCHIVED" and result.snapshot not in (first.snapshot, newest.snapshot)
    assert snapshot_bytes(newest.snapshot) == saved


@pytest.mark.parametrize("damage", ["missing", "truncate", "flip"])
def test_matching_damaged_newest_is_retained_and_reported(local, damage):
    result = archive(local); bundle = Path(result.snapshot)/"history.bundle"; raw = bundle.read_bytes()
    if damage == "missing": bundle.unlink()
    else: bundle.write_bytes(raw[:20] if damage == "truncate" else raw[:-1]+bytes([raw[-1]^1]))
    saved = snapshot_bytes(result.snapshot)
    new = archive(local)
    assert new.status == "ARCHIVED" and new.snapshot != result.snapshot
    assert result.snapshot in new.damaged and snapshot_bytes(result.snapshot) == saved


def test_progress_patterns_and_fetch_validation_without_full_fsck(local, monkeypatch):
    import time
    stages = []; calls = []; fsck_seconds = []; run = gh._run_git
    def measured(args, **kwargs):
        calls.append(args); start = time.monotonic()
        try: return run(args, **kwargs)
        finally:
            if "fsck" in args: fsck_seconds.append(time.monotonic()-start)
    monkeypatch.setattr(gh, "_run_git", measured)
    start = time.monotonic(); archive(local, progress=stages.append)
    print("archive_seconds", time.monotonic()-start, "full_fsck_seconds", fsck_seconds)
    assert stages == ["checking", "downloading", "verifying", "publishing"]
    remote = next(a for a in calls if "ls-remote" in a)
    assert remote[remote.index(local[0].as_uri())+1:] == ["HEAD", "refs/heads/*", "refs/tags/*"]
    assert not fsck_seconds


@pytest.mark.parametrize("stage", ["checking", "downloading", "verifying", "publishing"])
def test_progress_exception_fails_safely(local, stage):
    def broken(value):
        if value == stage: raise RuntimeError("callback failure")
    with pytest.raises(gh.HistoryError) as error: archive(local, progress=broken)
    assert error.value.code == "internal_error" and not list(local[1].rglob("history.json"))
    assert archive(local).status == "ARCHIVED"


@pytest.mark.parametrize("operation", [gh.archive_history, gh.history_status])
@pytest.mark.parametrize("kind", ["missing", "file"])
def test_invalid_archive_root_is_unsafe(local, tmp_path, operation, kind):
    root = tmp_path/"invalid"
    if kind == "file": root.write_bytes(b"not directory")
    with pytest.raises(gh.HistoryError) as error: operation(REPO, root)
    assert error.value.code == "unsafe_path"


def test_unicode_ref_separators_roundtrip_exactly(local, tmp_path):
    names = [prefix+"x"+separator+"y" for prefix in ("refs/heads/", "refs/tags/") for separator in ("\x85", "\u2028", "\u2029")]
    for name in names: git(local[0], "update-ref", name, local[2])
    git(local[0], "symbolic-ref", "HEAD", names[0])
    result = None
    try: result = archive(local)
    except gh.HistoryError as error: code = error.code
    assert result is not None, code
    assert set(names) <= {r["name"] for r in manifest(result.snapshot)["refs"]}
    assert archive(local).status == "UNCHANGED"
    restored=tmp_path/"restored"
    git(tmp_path,"clone","--bare","--ref-format=reftable",str(Path(result.snapshot)/"history.bundle"),str(restored))
    assert set(names) <= set(git(restored,"for-each-ref","--format=%(refname)").split("\n"))


def test_non_utf8_ref_rejected_cleanly(local):
    git(local[0],"update-ref","--stdin",data=b"update refs/heads/bad-\xff "+local[2].encode()+b"\n")
    with pytest.raises(gh.HistoryError) as error: archive(local)
    assert error.value.code == "invalid_objects" and not list(local[1].rglob("history.json"))


@pytest.mark.parametrize("operation", ["repeat", "verify", "status"])
@pytest.mark.parametrize("reason", ["cancel", "timeout"])
def test_local_hash_observes_cancel_and_deadline(local, monkeypatch, operation, reason):
    import threading, time
    from dataclasses import replace
    result=archive(local); saved=snapshot_bytes(local[1]); event=threading.Event(); reads=[]
    read=store.winfs.Handle.read
    def paused(handle, *args, **kw):
        data=read(handle,*args,**kw)
        if handle.name == "history.bundle" and data:
            reads.append(len(data))
            if reason=="cancel": event.set()
            elif operation=="repeat": gh._state.deadline=0
            else: time.sleep(1.05)
        return data
    monkeypatch.setattr(store.winfs.Handle,"read",paused)
    limits=replace(gh.Limits(),verify_seconds=1)
    code=None; start=time.monotonic()
    try:
        if operation=="repeat": archive(local,cancel=event.is_set)
        elif operation=="verify": gh.verify_snapshot(result.snapshot,cancel=event.is_set,limits=limits)
        else: gh.history_status(REPO,local[1],verify=True,cancel=event.is_set,limits=limits)
    except gh.HistoryError as error: code=error.code
    except TypeError as error: code=type(error).__name__
    assert code == ("cancelled" if reason=="cancel" else "timeout"), (code,reads)
    assert reads and time.monotonic()-start < 3 and snapshot_bytes(local[1])==saved


def test_incomplete_busy_temp_is_skipped(local, monkeypatch):
    first=archive(local); history=first.snapshot.parent
    incomplete=history/"snapshot-20000101T000000Z--000000000000"; incomplete.mkdir()
    name=".covearchive-tmp-"+"b"*32+".part"; (incomplete/name).write_bytes(b"busy bytes")
    attempted=[]; original=store.Directory.open_temp
    def busy(directory, filename):
        if directory.name==incomplete.name and filename==name:
            attempted.append(filename); raise store.ArchiveError("busy")
        return original(directory,filename)
    monkeypatch.setattr(store.Directory,"open_temp",busy)
    assert archive(local).status=="UNCHANGED"
    assert attempted==[name] and (incomplete/name).read_bytes()==b"busy bytes"
    assert any(s.path==incomplete and s.status=="incomplete" for s in gh.history_status(REPO,local[1]))


@pytest.mark.parametrize("head_kind", ["missing", "detached", "present"])
def test_bundle_refs_ignore_ambient_default_head(local, tmp_path, head_kind):
    git(local[0],"update-ref","refs/heads/master",local[2])
    if head_kind=="missing": git(local[0],"symbolic-ref","HEAD","refs/heads/unborn")
    elif head_kind=="detached": git(local[0],"update-ref","--no-deref","HEAD",local[2])
    result=None; code=None
    try: result=archive(local)
    except gh.HistoryError as error: code=error.code
    assert result is not None and result.status=="ARCHIVED", code
    doc=manifest(result.snapshot); head=doc["head"]
    assert head == ("refs/heads/main" if head_kind=="present" else None)
    expected={r["sha"]+" "+r["name"] for r in doc["refs"]}
    if head: expected.add(local[2]+" HEAD")
    assert set(git(tmp_path,"bundle","list-heads",str(result.snapshot/"history.bundle")).split("\n"))==expected
    restored=tmp_path/"restore-head"
    git(tmp_path,"clone","--bare","--ref-format=reftable",str(result.snapshot/"history.bundle"),str(restored))
    assert set(git(restored,"for-each-ref","--format=%(refname)").split("\n"))=={r["name"] for r in doc["refs"]}
    assert gh.verify_snapshot(result.snapshot) and gh.history_status(REPO,local[1],verify=True)[0].status=="valid"
    assert archive(local).status=="UNCHANGED"


@pytest.mark.parametrize("previous", [False, True])
def test_manifest_size_checked_before_any_snapshot_publication(local, monkeypatch, previous):
    if previous: archive(local)
    saved=snapshot_bytes(local[1]); history=local[1]/"Owner"/"Repo"/"git-history"
    dirs=set(history.glob("snapshot-*"))
    # A small cap models JSON escaping amplification without a 16MiB fixture.
    monkeypatch.setattr(gh,"MAX_MANIFEST_BYTES",2048,raising=False)
    git(local[0],"update-ref","refs/heads/"+"\u754c"*400,local[2])
    code=None
    try: archive(local)
    except gh.HistoryError as error: code=error.code
    assert code=="too_large", code
    assert set(history.glob("snapshot-*"))==dirs
    # The zero-byte history lock is infrastructure, not snapshot content.
    content=lambda rows:{p:b for p,b in rows.items() if not (Path(p).name==".covearchive.lock" and b==b"")}
    assert content(snapshot_bytes(local[1]))==content(saved)


@pytest.mark.parametrize("other", [RepositoryTarget("Fork","Repo"),RepositoryTarget("Owner","Other"),
    RepositoryTarget("Owner","Repo","codeberg","codeberg.org"),RepositoryTarget("Owner","Repo","gitlab","gitlab.com")])
def test_foreign_snapshot_is_bound_and_retained(local, tmp_path, other):
    foreign_root=tmp_path/"foreign";foreign_root.mkdir()
    foreign=gh.archive_history(other,foreign_root)
    history=local[1]/"Owner"/"Repo"/"git-history";history.mkdir(parents=True)
    imported=history/foreign.snapshot.name;shutil.copytree(foreign.snapshot,imported);saved=snapshot_bytes(imported)
    assert gh.history_status(REPO,local[1])[0].status=="corrupt"
    with pytest.raises(gh.HistoryError) as error:gh.verify_snapshot(imported,repository=REPO)
    assert error.value.code=="corrupt"
    result=archive(local)
    assert result.status=="ARCHIVED" and imported in result.damaged and snapshot_bytes(imported)==saved
    assert manifest(result.snapshot)["repository"]==dict(provider="github",host="github.com",owner="Owner",repo="Repo")


@pytest.mark.parametrize("change", ["timestamp","digest","malformed"])
def test_snapshot_directory_name_is_bound(local, change):
    first=archive(local);doc=manifest(first.snapshot)
    name="snapshot-20990101T000000Z--"+doc["refs_digest"][:12] if change=="timestamp" else first.snapshot.name[:-12]+"0"*12
    if change=="malformed": name="snapshot-invalid"
    renamed=first.snapshot.with_name(name);first.snapshot.rename(renamed);saved=snapshot_bytes(renamed)
    rows=gh.history_status(REPO,local[1],verify=True)
    assert rows and rows[0].status=="corrupt"
    with pytest.raises(gh.HistoryError):gh.verify_snapshot(renamed)
    result=archive(local)
    assert result.status=="ARCHIVED" and renamed in result.damaged and snapshot_bytes(renamed)==saved
    assert manifest(result.snapshot)["created_utc"][:4]!="2099", "invalid directory must not control newest ordering"


@pytest.mark.parametrize("repository,alias", [(REPO,RepositoryTarget("owner","repo")),
    (RepositoryTarget("Group/Sub","Repo","gitlab","gitlab.com"),RepositoryTarget("group/sub","repo","gitlab","gitlab.com"))])
def test_case_only_identity_reuses_snapshot(local, repository, alias):
    first=gh.archive_history(repository,local[1]);saved=snapshot_bytes(local[1])
    repeat=gh.archive_history(alias,local[1])
    assert repeat.status=="UNCHANGED" and repeat.snapshot==first.snapshot and snapshot_bytes(local[1])==saved
    assert gh.history_status(alias,local[1])[0].status=="valid"
    try: accepted=gh.verify_snapshot(first.snapshot,repository=alias)
    except TypeError: accepted=False
    assert accepted


def test_restore_shared_tip_with_explicit_manifest_branch(local, tmp_path):
    git(local[0],"update-ref","refs/heads/master",local[2])
    result=archive(local);head=manifest(result.snapshot)["head"];remove_source(local[0])
    restored=tmp_path/"explicit-restore"
    git(tmp_path,"-c","init.defaultBranch=master","clone","--branch",head.removeprefix("refs/heads/"),str(result.snapshot/"history.bundle"),str(restored))
    assert git(restored,"symbolic-ref","HEAD")==head=="refs/heads/main"
