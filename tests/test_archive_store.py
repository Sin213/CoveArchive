"""Synthetic NTFS asset-side adversarial behaviour."""
from concurrent.futures import ThreadPoolExecutor
import ctypes
from dataclasses import replace
import hashlib
import os
import subprocess
import sys
from threading import Barrier

import pytest

from covearchive.archive import naming, store, winfs
from covearchive.archive.manifest import AssetRecord
from covearchive.github.urls import RepositoryTarget

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires Windows NTFS handles")
DATA = b"synthetic payload"
SHA = hashlib.sha256(DATA).hexdigest()


def record(**changes):
    return replace(AssetRecord(1, "asset", "asset.bin", len(DATA), None, None, "planned"), **changes)


def junction(link, target):
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_ensure_directories_and_release_junction(tmp_path):
    repo = RepositoryTarget("owner", "repo")
    folder = naming.release_folder_name("v1", 7)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.ensure_project(root, repo) as project:
            with store.ensure_release(project, folder, 7) as release:
                assert release.info().is_directory
            for bad in ["..", "release-v1--8", "a/b"]:
                with pytest.raises(winfs.ArchiveUnsafePathError):
                    store.ensure_release(project, bad, 7)
    path = tmp_path / "owner" / "repo" / folder
    path.rmdir()
    target = tmp_path / "target"
    target.mkdir()
    junction(path, target)
    try:
        with winfs.ArchiveRoot.open(str(tmp_path)) as root:
            with store.ensure_project(root, repo) as project:
                with pytest.raises(winfs.ArchiveUnsafePathError, match="reparse_point"):
                    store.ensure_release(project, folder, 7)
    finally:
        path.rmdir()


@pytest.mark.parametrize("name", ["..", "C:\\bad", "a:b", "a/b", "bad.", "bad ",
                                      "release.json", "archive-index.json", ".covearchive.lock"])
def test_asset_malicious_components(tmp_path, name):
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with pytest.raises(winfs.ArchiveUnsafePathError):
            store.write_asset(root, name, [DATA], len(DATA))
    assert list(tmp_path.iterdir()) == []


def test_publish_verify_complete_and_retry(tmp_path):
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        first = store.write_asset(root, "asset.bin", [DATA[:3], DATA[3:]], len(DATA), SHA)
        assert (first.code, first.local_sha256, first.size) == ("published", SHA, len(DATA))
        result = store.verify_asset(root, record())
        assert (result.code, result.local_sha256, result.size) == ("verified", SHA, len(DATA))
        completed = store.completed_record(record(), result)
        assert completed.status == "completed" and completed.local_sha256 == SHA
        assert store.verify_asset(root, completed).code == "verified"
        assert store.write_asset(root, "asset.bin", [DATA], len(DATA)).code == "already_present"
        with pytest.raises(winfs.ArchiveError, match="unverified"):
            store.completed_record(record(asset_id=2), result)
        with pytest.raises(winfs.ArchiveError, match="unverified"):
            store.completed_record(record(), replace(result, local_sha256="0" * 64))
    assert [p.name for p in tmp_path.iterdir()] == ["asset.bin"]


@pytest.mark.parametrize("failure", ["overflow", "underflow", "hash", "exception", "base_exception"])
def test_failed_stream_cleanup(tmp_path, failure):
    class Abort(BaseException):
        pass

    def chunks():
        yield DATA[:2]
        if failure == "exception":
            raise RuntimeError("iterator failed")
        if failure == "base_exception":
            raise Abort()
        yield DATA[2:] if failure != "underflow" else b""
        if failure == "overflow":
            yield b"extra"

    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        expected = Abort if failure == "base_exception" else RuntimeError if failure == "exception" else winfs.ArchiveError
        with pytest.raises(expected) as caught:
            store.write_asset(root, "asset.bin", chunks(), len(DATA), "0" * 64 if failure == "hash" else None)
        assert type(caught.value) is expected
        if expected is winfs.ArchiveError:
            assert caught.value.code == ("hash_mismatch" if failure == "hash" else failure)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["different", "directory", "junction", "during", "during_junction"])
def test_destination_conflicts_are_preserved(tmp_path, kind):
    destination = tmp_path / "asset.bin"
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_bytes(b"safe")

    def install():
        if kind in ("junction", "during_junction"):
            junction(destination, target)
        elif kind == "directory":
            destination.mkdir()
        else:
            destination.write_bytes(b"existing")

    def chunks():
        yield DATA[:2]
        if kind.startswith("during"):
            install()
        yield DATA[2:]

    if not kind.startswith("during"):
        install()
    try:
        with winfs.ArchiveRoot.open(str(tmp_path)) as root:
            with pytest.raises(winfs.ArchiveConflictError):
                store.write_asset(root, "asset.bin", chunks(), len(DATA))
            if kind in ("junction", "during_junction"):
                assert store.verify_asset(root, record()).code == "reparse_point"
        assert (target / "keep").read_bytes() == b"safe"
        if kind in ("different", "during"):
            assert destination.read_bytes() == b"existing"
        else:
            assert destination.is_dir()
        assert not any(winfs.TEMP_PATTERN.fullmatch(p.name) for p in tmp_path.iterdir())
    finally:
        if kind in ("junction", "during_junction"):
            destination.rmdir()


@pytest.mark.parametrize("kind,code", [("missing", "missing"), ("directory", "not_regular_file"),
    ("hardlink", "multiple_links"), ("short", "size_mismatch"), ("long", "size_mismatch"),
    ("modified", "hash_mismatch"), ("publisher", "hash_mismatch"), ("valid", "verified")])
def test_verification_outcomes_and_untrusted_completion(tmp_path, kind, code):
    path = tmp_path / "asset.bin"
    if kind == "directory":
        path.mkdir()
    elif kind != "missing":
        path.write_bytes(DATA[:-1] if kind == "short" else DATA + b"x" if kind == "long" else
                         b"x" * len(DATA) if kind == "modified" else DATA)
        if kind == "hardlink":
            os.link(path, tmp_path / "link")
    claim = record(status="completed", local_sha256=SHA)
    if kind == "publisher":
        claim = record(publisher_sha256="0" * 64)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        result = store.verify_asset(root, claim)
        assert result.code == code
        if code != "verified":
            with pytest.raises(winfs.ArchiveError, match="unverified"):
                store.completed_record(claim, result)


def test_recover_exact_idle_only(tmp_path):
    idle = ".covearchive-tmp-" + "a" * 32 + ".part"
    lookalike = ".covearchive-tmp-" + "B" * 32 + ".part"
    unsafe = ".covearchive-tmp-" + "c" * 32 + ".part"
    (tmp_path / idle).write_bytes(b"orphan")
    (tmp_path / lookalike).write_bytes(b"keep")
    (tmp_path / unsafe).mkdir()
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with root.create_temp() as held:
            result = store.recover(root)
            assert result.removed == (idle,)
            assert (held.name, "busy") in result.skipped
            assert (unsafe, "not_regular_file") in result.skipped
            assert (tmp_path / held.name).exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([lookalike, unsafe])


@pytest.mark.parametrize("same", [True, False])
def test_two_threads_publish_one_winner(tmp_path, same):
    barrier = Barrier(2)

    def worker(data):
        def chunks():
            yield data[:2]
            barrier.wait(timeout=10)
            yield data[2:]
        with winfs.ArchiveRoot.open(str(tmp_path)) as root:
            try:
                return store.write_asset(root, "asset.bin", chunks(), len(data)).code
            except winfs.ArchiveConflictError:
                return "conflict"

    other = DATA if same else b"different bytes!"
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(worker, DATA), pool.submit(worker, other)
        outcomes = sorted([a.result(timeout=15), b.result(timeout=15)])
    assert outcomes == sorted(["published", "already_present" if same else "conflict"])
    assert (tmp_path / "asset.bin").read_bytes() in (DATA, other)
    assert [p.name for p in tmp_path.iterdir()] == ["asset.bin"]

from pathlib import Path
import json
from covearchive.archive import manifest as m
from covearchive.archive.plan import plan_archive
from covearchive.github.models import ReleaseCatalog, parse_release

REPO = RepositoryTarget("owner", "repo")
FOLDER = "release-v1--1"


def documents():
    release = m.ReleaseManifest(REPO, 1, "v1", FOLDER, None, None, False, None, ())
    return m.ProjectIndex(REPO, (m.ReleaseRecord(1, "v1", FOLDER, ()),)), release


def process(code, *args):
    source = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); " + code,
                           source, *map(str, args)], capture_output=True, text=True, timeout=30)


def test_writer_lock_required_and_cross_process_busy(tmp_path):
    index, release = documents()
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with root.open_dir(FOLDER, create=True) as folder:
            for call in [lambda: store.commit_index(root, index, None),
                         lambda: store.commit_release(folder, release),
                         lambda: store.quarantine_manifest(root, "archive-index.json")]:
                with pytest.raises(winfs.ArchiveError, match="writer_required"):
                    call()
            with store.project_writer(root):
                with pytest.raises(winfs.ArchiveBusyError):
                    with store.project_writer(root):
                        pass
                result = process("from covearchive.archive import store, winfs\n"
                    "with winfs.ArchiveRoot.open(sys.argv[1]) as root:\n"
                    " try:\n  with store.project_writer(root): pass\n"
                    " except winfs.ArchiveBusyError: print('busy')\n", tmp_path)
                assert result.returncode == 0, result.stderr
                assert result.stdout.strip() == "busy"


def test_atomic_commit_cas_and_crash_mismatch(tmp_path):
    index, release = documents()
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.project_writer(root):
            empty = store.load_project_state(root)
            assert empty.index is None and empty.index_sha256 is None
            with pytest.raises(winfs.ArchiveConflictError, match="missing"):
                store.commit_index(root, index, None)
            with root.open_dir(FOLDER, create=True) as folder:
                store.commit_release(folder, release)
                store.commit_index(root, index, None)
                first = store.load_project_state(root)
                assert first.index == index and first.releases == (release,) and not first.issues
                assert first.index_sha256 == hashlib.sha256((tmp_path / "archive-index.json").read_bytes()).hexdigest()
                store.commit_release(folder, replace(release, assets=(record(),)))
                assert (FOLDER, "consistency") in store.load_project_state(root).issues
                updated = replace(index, releases=(replace(index.releases[0], assets=(record(),)),))
                store.commit_index(root, updated, first.index_sha256)
                with pytest.raises(winfs.ArchiveConflictError, match="stale_index"):
                    store.commit_index(root, index, first.index_sha256)
                assert store.load_project_state(root).index == updated
            leftover = ".covearchive-tmp-" + "d" * 32 + ".part"
            (tmp_path / leftover).write_bytes(b"partial")
            assert store.recover(root).removed == (leftover,)
    assert b"\r" not in (tmp_path / "archive-index.json").read_bytes()


@pytest.mark.parametrize("kind,code", [("json", "corrupt"), ("duplicate", "corrupt"),
    ("version", "unsupported_version"), ("utf8", "corrupt"), ("oversized", "oversized"),
    ("directory", "not_regular_file")])
def test_bad_index_preserved_and_classified(tmp_path, monkeypatch, kind, code):
    index, _ = documents()
    index = replace(index, releases=())
    path = tmp_path / "archive-index.json"
    raw = (b'{"schema_version":9}' if kind == "version" else b'\xff' if kind == "utf8" else
           b'{"schema_version":1,"schema_version":1}' if kind == "duplicate" else b"invalid JSON")
    if kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(raw)
    if kind == "oversized":
        monkeypatch.setattr(m, "MAX_INDEX_BYTES", 3)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.project_writer(root):
            state = store.load_project_state(root)
            assert ("archive-index.json", code) in state.issues
            error = (m.UnsupportedVersionError if kind == "version" else
                     winfs.ArchiveCorruptError if code == "corrupt" else winfs.ArchiveError)
            for token in (None, state.index_sha256):
                with pytest.raises((winfs.ArchiveError, m.ManifestError)) as caught:
                    store.commit_index(root, index, token)
                assert isinstance(caught.value, error)
                assert caught.value.code == ("version" if kind == "version" else code)
    assert path.is_dir() if kind == "directory" else path.read_bytes() == raw


def test_corrupt_release_quarantine_and_missing_release(tmp_path):
    index, release = documents()
    directory = tmp_path / FOLDER
    directory.mkdir()
    raw = b'{"schema_version":8}'
    (directory / "release.json").write_bytes(raw)
    (tmp_path / "archive-index.json").write_text(m.dumps_index(index), encoding="utf-8")
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.project_writer(root):
            assert (FOLDER, "unsupported_version") in store.load_project_state(root).issues
            with root.open_dir(FOLDER) as folder:
                with pytest.raises(m.UnsupportedVersionError):
                    store.commit_release(folder, release)
                assert (directory / "release.json").read_bytes() == raw
                (directory / "release.json.corrupt-1").write_bytes(b"keep")
                name = store.quarantine_manifest(folder, "release.json")
                assert name == "release.json.corrupt-2"
                assert (directory / name).read_bytes() == raw
                assert (directory / "release.json.corrupt-1").read_bytes() == b"keep"
                assert (FOLDER, "missing") in store.load_project_state(root).issues
                store.commit_release(folder, release)
            assert not store.load_project_state(root).issues


def test_end_to_end_plan_publish_commit_reload_and_retry(tmp_path):
    remote = parse_release(dict(id=1, tag_name="v1", draft=False, prerelease=False,
        created_at="2026-11-13T12:00:00Z", assets=[dict(id=1, name="asset.zip", state="uploaded",
        size=len(DATA), content_type="opaque", browser_download_url="https://example/asset",
        created_at="2026-11-13", updated_at="2026-11-13", digest="sha256:" + SHA)]))
    catalog = ReleaseCatalog(REPO, (remote,), 1, 1, 0, (), "/repos/owner/repo/releases", True)
    plan = plan_archive(catalog, archive_root=str(tmp_path), repository=REPO)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.ensure_project(root, REPO) as project:
            for retry in (False, True):
                with store.project_writer(project):
                    state = store.load_project_state(project)
                    proposed = plan.index.releases[0]
                    with store.ensure_release(project, proposed.folder, proposed.release_id) as folder:
                        asset = proposed.assets[0]
                        outcome = store.write_asset(folder, asset.saved_name, [DATA], asset.size, asset.publisher_sha256)
                        assert outcome.code == ("already_present" if retry else "published")
                        completed = store.completed_record(asset, store.verify_asset(folder, asset))
                        updated = replace(proposed, assets=(completed,))
                        index = replace(plan.index, releases=(updated,))
                        release = m.build_release_manifest(REPO, remote, updated)
                        store.commit_release(folder, release)
                        store.commit_index(project, index, state.index_sha256)
                    loaded = store.load_project_state(project)
                    assert loaded.index == index and loaded.releases == (release,) and not loaded.issues


@pytest.mark.parametrize("same", [True, False])
def test_two_processes_publish(tmp_path, same):
    source = str(Path(__file__).resolve().parents[1] / "src")
    code = """import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
import time
from covearchive.archive import store, winfs
path, data, marker, peer = sys.argv[2:]
def chunks():
    yield data.encode()[:2]
    Path(marker).write_bytes(b'ready')
    end = time.monotonic() + 10
    while not Path(peer).exists():
        if time.monotonic() > end: raise RuntimeError('peer timeout')
        time.sleep(.01)
    yield data.encode()[2:]
with winfs.ArchiveRoot.open(path) as root:
    try: print(store.write_asset(root, 'asset.bin', chunks(), len(data)).code)
    except winfs.ArchiveConflictError: print('conflict')
"""
    a, b = tmp_path / "ready-a", tmp_path / "ready-b"
    children = []
    try:
        for data, marker, peer in [("first", a, b), ("first" if same else "other", b, a)]:
            children.append(subprocess.Popen([sys.executable, "-I", "-c", code, source,
                str(tmp_path), data, str(marker), str(peer)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True))
        outcomes = []
        for child in children:
            stdout, stderr = child.communicate(timeout=20)
            assert child.returncode == 0, stderr
            outcomes.append(stdout.strip())
        assert sorted(outcomes) == sorted(["published", "already_present" if same else "conflict"])
        assert (tmp_path / "asset.bin").read_bytes() in (b"first", b"other")
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)

def test_manifest_from_another_indexed_folder_is_mismatch(tmp_path):
    index, release = documents()
    second = replace(index.releases[0], release_id=2, tag="v2", folder="release-v2--2")
    index = replace(index, releases=(*index.releases, second))
    wrong = replace(release, release_id=2, tag="v2", folder=second.folder)
    (tmp_path / FOLDER).mkdir()
    (tmp_path / FOLDER / "release.json").write_text(m.dumps_release_manifest(wrong), encoding="utf-8")
    (tmp_path / "archive-index.json").write_text(m.dumps_index(index), encoding="utf-8")
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        assert (FOLDER, "consistency") in store.load_project_state(root).issues


def test_symlink_file_and_directory_rejected_if_supported(tmp_path):
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "file").write_bytes(DATA)
    links = [tmp_path / "asset.bin", tmp_path / FOLDER]
    try:
        try:
            os.symlink(tmp_path / "target" / "file", links[0])
            os.symlink(tmp_path / "target", links[1], target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation unavailable: {exc}")
        with winfs.ArchiveRoot.open(str(tmp_path)) as root:
            assert store.verify_asset(root, record()).code == "reparse_point"
            with pytest.raises(winfs.ArchiveUnsafePathError, match="reparse_point"):
                store.ensure_release(root, FOLDER, 1)
    finally:
        for link in links:
            if link.is_symlink():
                if link.is_dir():
                    link.rmdir()
                else:
                    link.unlink()

@pytest.mark.parametrize("indexed", [False, True, "directory"])
@pytest.mark.parametrize("kind,code", [("valid", "unindexed_release"), ("corrupt", "corrupt"),
                                      ("unsupported", "unsupported_version")])
def test_unindexed_release_reported_without_modification(tmp_path, indexed, kind, code):
    index, release = documents()
    if indexed == "directory":
        (tmp_path / "archive-index.json").mkdir()
    elif indexed:
        (tmp_path / "archive-index.json").write_text(m.dumps_index(replace(index, releases=())), encoding="utf-8")
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.project_writer(root):
            with root.open_dir(FOLDER, create=True) as folder:
                store.commit_release(folder, release)
            path = tmp_path / FOLDER / "release.json"
            if kind != "valid":
                path.write_bytes(b"bad" if kind == "corrupt" else b'{"schema_version":99}')
            before = path.read_bytes()
            state = store.load_project_state(root)
            assert (FOLDER, code) in state.issues
            if not indexed:
                assert ("archive-index.json", "missing") in state.issues
            assert path.read_bytes() == before
            assert not any(winfs.TEMP_PATTERN.fullmatch(name) for name in root.names())


def test_unindexed_scan_filters_and_rejects_junction(tmp_path):
    for name in ["foreign", "release-empty--2", "release-invalid--0", "release-leading--003"]:
        (tmp_path / name).mkdir()
    (tmp_path / "foreign" / "release.json").write_bytes(b"bad")
    link = tmp_path / "release-junction--3"
    junction(link, tmp_path / "foreign")
    try:
        with winfs.ArchiveRoot.open(str(tmp_path)) as root:
            state = store.load_project_state(root)
            assert set(state.issues) == {("archive-index.json", "missing"), (link.name, "reparse_point")}
        assert (tmp_path / "foreign" / "release.json").read_bytes() == b"bad"
    finally:
        link.rmdir()

@pytest.mark.parametrize("which", ["index", "release"])
@pytest.mark.parametrize("race", ["appears", "write", "swap", "replace", "unsupported"])
def test_manifest_commit_races(tmp_path, monkeypatch, which, race):
    index, release = documents()
    index = replace(index, releases=())
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.project_writer(root):
            with root.open_dir(FOLDER, create=True) as folder:
                directory = root if which == "index" else folder
                path = tmp_path / ("archive-index.json" if which == "index" else FOLDER + "/release.json")
                name = path.name
                raw = (m.dumps_index(index) if which == "index" else m.dumps_release_manifest(release)).encode()
                if race != "appears":
                    path.write_bytes(raw)
                token = hashlib.sha256(raw).hexdigest() if which == "index" and race != "appears" else None
                if race == "unsupported":
                    path.write_bytes(b'{"schema_version":99}')
                original = directory.create_temp
                def hook(*args, **kwargs):
                    if race == "appears":
                        path.write_bytes(b'{"schema_version":99}')
                    elif race == "write":
                        raw_handle = winfs._create(str(path), winfs._WRITE, 7, None, 5, 0, None)
                        error = ctypes.get_last_error()
                        if raw_handle != ctypes.c_void_p(-1).value:
                            winfs._close(raw_handle)
                        assert raw_handle == ctypes.c_void_p(-1).value and error == 32
                    elif race == "replace":
                        with original() as external:
                            external.write(b'{"schema_version":99}')
                            external.rename(directory, name, replace=True, metadata=True)
                    elif race == "swap":
                        # Move the validated object away, preserving its live link.
                        os.rename(path, path.with_suffix(".old"))
                        path.write_bytes(b'{"schema_version":99}')
                    return original(*args, **kwargs)
                monkeypatch.setattr(directory, "create_temp", hook)
                def commit():
                    if which == "index":
                        store.commit_index(root, index, token)
                    else:
                        store.commit_release(folder, release)
                if race == "unsupported":
                    with pytest.raises(m.UnsupportedVersionError):
                        commit()
                    assert path.read_bytes() == b'{"schema_version":99}'
                elif race == "write":
                    commit()
                    assert path.read_bytes() == raw
                else:
                    with pytest.raises(winfs.ArchiveConflictError, match="destination_changed"):
                        commit()
                    if race in ("appears", "replace"):
                        assert path.read_bytes() == b'{"schema_version":99}'
                    else:
                        assert path.with_suffix(".old").read_bytes() == raw


@pytest.mark.parametrize("owner,repo", [("release.json", "repo"), ("owner", "release.json")])
def test_role_specific_reservations(tmp_path, owner, repo):
    try:
        with winfs.ArchiveRoot.open(str(tmp_path)) as root:
            with store.ensure_project(root, RepositoryTarget(owner, repo)) as project:
                with store.ensure_release(project, FOLDER, 1) as folder:
                    for name in ["release.json.corrupt-1.zip", ".covearchive-notes.txt", "archive-index.json", ".covearchive.lock"]:
                        store.write_asset(folder, name, [DATA], len(DATA))
                        assert store.verify_asset(folder, record(saved_name=name)).code == "verified"
                    assert not store.recover(folder).removed
                    for name in ["archive-index.json", ".covearchive.lock", ".covearchive-tmp-" + "a" * 32 + ".part"]:
                        with pytest.raises(winfs.ArchiveUnsafePathError, match="reserved_name"):
                            store.write_asset(project, name, [], 0)
                    for name in ["release.json", ".covearchive-tmp-" + "a" * 32 + ".part"]:
                        with pytest.raises(winfs.ArchiveUnsafePathError, match="reserved_name"):
                            store.write_asset(folder, name, [], 0)
    except winfs.ArchiveError as exc:
        raise AssertionError(f"unexpected reservation or filesystem error: {exc.code}") from exc
