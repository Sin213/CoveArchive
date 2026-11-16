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

from covearchive.archive import naming, store
from covearchive.archive.store import winfs
from covearchive.archive.manifest import AssetRecord
from covearchive.github.urls import RepositoryTarget

NTFS_JUNCTION = pytest.mark.skipif(sys.platform != "win32", reason="requires NTFS junction creation via cmd mklink /J")
DATA = b"synthetic payload"
SHA = hashlib.sha256(DATA).hexdigest()


def record(**changes):
    return replace(AssetRecord(1, "asset", "asset.bin", len(DATA), None, None, "planned"), **changes)


def test_codeberg_component_chain_from_pinned_root():
    from contextlib import contextmanager
    calls=[]
    class Directory:
        @contextmanager
        def open_dir(self,name,**options):
            calls.append((self,name,options)); yield Directory()
    root=Directory()
    with store.ensure_project(root,RepositoryTarget('Owner','Repo','codeberg','codeberg.org')) as project:
        assert project.role=='project' and calls[0][0] is root
    assert [name for parent,name,options in calls]==['codeberg.org','Owner','Repo'] and len({id(parent) for parent,name,options in calls})==3
    assert all(options==dict(create=True,fold_case=True) for parent,name,options in calls)


@pytest.mark.parametrize('level',[0,1,2])
@pytest.mark.parametrize('kind',[pytest.param('junction',marks=NTFS_JUNCTION),'symlink'])
def test_codeberg_ancestor_links_refused(tmp_path,level,kind):
    names=('codeberg.org','Owner','Repo'); link=tmp_path.joinpath(*names[:level+1]); link.parent.mkdir(parents=True,exist_ok=True)
    target=tmp_path/'outside'; target.mkdir(); (target/'sentinel').write_bytes(DATA)
    if kind=='junction': junction(link,target)
    else:
        try: os.symlink(target,link,target_is_directory=True)
        except OSError as exc: pytest.skip(f'symlink creation unavailable: {exc}')
    try:
        with winfs.ArchiveRoot.open(str(tmp_path)) as root:
            with pytest.raises(winfs.ArchiveUnsafePathError,match='reparse_point' if sys.platform=='win32' else 'not_regular_file'):
                with store.ensure_project(root,RepositoryTarget('Owner','Repo','codeberg','codeberg.org')): pass
        assert list(target.iterdir())==[target/'sentinel'] and (target/'sentinel').read_bytes()==DATA
    finally:
        link.rmdir() if kind=='junction' else link.unlink()


def test_codeberg_host_case_reused_without_github_collision(tmp_path):
    (tmp_path/'CODEBERG.ORG'/'OWNER'/'REPO').mkdir(parents=True)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        for repository in (RepositoryTarget('Owner','Repo'),RepositoryTarget('Owner','Repo','codeberg','codeberg.org')):
            with store.ensure_project(root,repository) as project: assert project.info().is_directory
    assert (tmp_path/'Owner'/'Repo').is_dir() and (tmp_path/'CODEBERG.ORG'/'OWNER'/'REPO').is_dir()
    assert sorted(p.name for p in tmp_path.iterdir())==['CODEBERG.ORG','Owner']


def junction(link, target):
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@NTFS_JUNCTION
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


@pytest.mark.parametrize("kind", ["different", "directory", pytest.param("junction",marks=NTFS_JUNCTION),
    "during", pytest.param("during_junction",marks=NTFS_JUNCTION)])
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
                result = process("from covearchive.archive import store\nfrom covearchive.archive.store import winfs\n"
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
from covearchive.archive import store
from covearchive.archive.store import winfs
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
            assert store.verify_asset(root, record()).code == ("reparse_point" if sys.platform == "win32" else "unsafe_path")
            with pytest.raises(winfs.ArchiveUnsafePathError, match="reparse_point" if sys.platform == "win32" else "not_regular_file"):
                store.ensure_release(root, FOLDER, 1)
    finally:
        for link in links:
            if link.is_symlink():
                if sys.platform == "win32" and link.is_dir():
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


@NTFS_JUNCTION
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
@pytest.mark.parametrize("race", ["appears", pytest.param("write", marks=pytest.mark.skipif(
    sys.platform != "win32", reason="requires NTFS share modes denying writes through a held manifest handle")),
    "swap", "replace", "unsupported"])
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


# A GitHub owner cannot contain '.', so release.json is reachable only as a repo.
@pytest.mark.parametrize("owner,repo", [("owner", "release.json")])
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


def test_single_unit_asset_publish_verify_and_no_replace(tmp_path):
    claim = record(saved_name="a", publisher_sha256=SHA)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        first = store.write_asset(root, "a", [DATA], len(DATA), SHA)
        assert (first.code, first.local_sha256, first.size) == ("published", SHA, len(DATA))
        verified = store.verify_asset(root, claim)
        assert (verified.code, verified.local_sha256, verified.size) == ("verified", SHA, len(DATA))
        assert store.completed_record(claim, verified).status == "completed"
        assert store.write_asset(root, "a", [DATA], len(DATA), SHA).code == "already_present"
        with pytest.raises(winfs.ArchiveConflictError):
            store.write_asset(root, "a", [b"x" * len(DATA)], len(DATA))
    assert os.listdir(tmp_path) == ["a"]
    assert (tmp_path / "a").read_bytes() == DATA


@pytest.mark.parametrize("actual", [DATA[:-1], DATA + b"x"], ids=["too-small", "too-large"])
def test_verify_size_mismatch_before_read_or_hash(tmp_path, monkeypatch, actual):
    (tmp_path / "asset.bin").write_bytes(actual)
    claim = record(status="completed", local_sha256=SHA)

    def forbidden_read(*args, **kwargs):
        pytest.fail("size mismatch must be rejected before reading asset bytes")

    def forbidden_hash(*args, **kwargs):
        pytest.fail("size mismatch must be rejected before constructing SHA-256")

    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with monkeypatch.context() as guard:
            guard.setattr(winfs.Handle, "read", forbidden_read)
            result = store.verify_asset(root, claim)
        with monkeypatch.context() as guard:
            guard.setattr(store.hashlib, "sha256", forbidden_hash)
            hash_guarded = store.verify_asset(root, claim)
        for verification in (result, hash_guarded):
            assert verification.code == "size_mismatch"
            assert verification.size == len(actual)
            assert verification.local_sha256 is None
            with pytest.raises(winfs.ArchiveError, match="unverified"):
                store.completed_record(claim, verification)
    assert (tmp_path / "asset.bin").read_bytes() == actual


@pytest.mark.parametrize("which", ["index", "release"])
@pytest.mark.parametrize("window", ["rename-aside", "posix-replace"])
def test_accepted_limitation_manifest_race_after_precheck(tmp_path, monkeypatch, which, window):
    """ACCEPTED LIMITATION: foreign replacements can be overwritten (CA-S5.1)."""
    index, release = documents()
    index = replace(index, releases=())
    committed = (m.dumps_index(index) if which == "index" else m.dumps_release_manifest(release)).encode()
    original_bytes = committed + b"\n"
    foreign_bytes = b"synthetic foreign manifest replacement"
    calls = 0
    real_unlinked = winfs.Handle.unlinked
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.project_writer(root):
            with root.open_dir(FOLDER, create=True) as folder:
                directory = root if which == "index" else folder
                path = tmp_path / "archive-index.json" if which == "index" else tmp_path / FOLDER / "release.json"
                moved = path.with_suffix(".moved")
                path.write_bytes(original_bytes)

                def after_precheck(held):
                    nonlocal calls
                    result = real_unlinked(held)
                    calls += 1
                    if calls == 1:
                        assert result is False
                        if window == "rename-aside":
                            os.rename(path, moved)
                            path.write_bytes(foreign_bytes)
                        else:
                            with directory.create_temp() as foreign:
                                foreign.write(foreign_bytes)
                                foreign.rename(directory, path.name, replace=True, metadata=True)
                        assert path.read_bytes() == foreign_bytes
                    return result

                monkeypatch.setattr(winfs.Handle, "unlinked", after_precheck)

                def commit():
                    if which == "index":
                        store.commit_index(root, index, hashlib.sha256(original_bytes).hexdigest())
                    else:
                        store.commit_release(folder, release)

                if window == "rename-aside":
                    with pytest.raises(winfs.ArchiveConflictError, match="destination_changed"):
                        commit()
                    assert moved.read_bytes() == original_bytes
                else:
                    commit()
                    assert not moved.exists()
                assert calls == 2
                assert path.read_bytes() == committed
                assert not any(winfs.TEMP_PATTERN.fullmatch(name) for name in directory.names())


@pytest.mark.parametrize("owner,repo", [("owner", "repo"), ("OWNER", "REPO"),
                                       ("owner", "Repo"), ("Owner", "repo")])
def test_f2_project_reuses_established_case(tmp_path, owner, repo):
    def identity(handle):
        info = handle.info()
        return info.volume_serial, info.file_id

    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.ensure_project(root, RepositoryTarget("Owner", "Repo")) as project:
            before = identity(project.parent), identity(project)
            assert (project.parent.name, project.name) == ("Owner", "Repo")
        with store.ensure_project(root, RepositoryTarget(owner, repo)) as project:
            assert (identity(project.parent), identity(project)) == before
            assert (project.parent.name, project.name) == ("Owner", "Repo")
        assert not root._children
    assert os.listdir(tmp_path) == ["Owner"]
    assert os.listdir(tmp_path / "Owner") == ["Repo"]


def test_f2_new_project_uses_requested_spelling(tmp_path):
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.ensure_project(root, RepositoryTarget("NewOwner", "NewRepo")) as project:
            assert (project.parent.name, project.name) == ("NewOwner", "NewRepo")
    assert os.listdir(tmp_path) == ["NewOwner"]
    assert os.listdir(tmp_path / "NewOwner") == ["NewRepo"]


@pytest.mark.parametrize("where", ["owner", "repo"])
@pytest.mark.parametrize("kind", [pytest.param("junction",marks=NTFS_JUNCTION), "file"])
def test_f2_case_variant_unsafe_project_entry(tmp_path, where, kind):
    target = tmp_path / "target"
    target.mkdir()
    if where == "repo":
        (tmp_path / "Owner").mkdir()
    entry = tmp_path / "Owner" if where == "owner" else tmp_path / "Owner" / "Repo"
    if kind == "junction":
        junction(entry, target)
    else:
        entry.write_bytes(b"synthetic existing file")
    try:
        with winfs.ArchiveRoot.open(str(tmp_path)) as root:
            code = "reparse_point" if kind == "junction" else "not_regular_file"
            with pytest.raises(winfs.ArchiveUnsafePathError, match=code):
                with store.ensure_project(root, RepositoryTarget("owner", "repo")):
                    pytest.fail("unsafe project entry was accepted")
            assert not root._children
        assert os.listdir(target) == []
        assert sorted(os.listdir(tmp_path)) == ["Owner", "target"]
        if where == "repo":
            assert os.listdir(tmp_path / "Owner") == ["Repo"]
        if kind == "file":
            assert entry.read_bytes() == b"synthetic existing file"
    finally:
        if kind == "junction":
            entry.rmdir()


@pytest.mark.parametrize("which", ["index", "release"])
@pytest.mark.parametrize("repository", [RepositoryTarget("Foreign", "Repo"), RepositoryTarget("owner", "repo")])
def test_f2_commit_identity_remains_exact(tmp_path, which, repository):
    established = RepositoryTarget("Owner", "Repo")
    index, release = documents()
    index, release = replace(index, repository=established), replace(release, repository=established)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.ensure_project(root, established) as project:
            with store.project_writer(project):
                with store.ensure_release(project, FOLDER, 1) as folder:
                    store.commit_release(folder, release)
                    store.commit_index(project, index, None)
        directory = tmp_path / "Owner" / "Repo"
        paths = [directory / "archive-index.json", directory / FOLDER / "release.json"]
        before = [path.read_bytes() for path in paths]
        with store.ensure_project(root, RepositoryTarget("owner", "repo")) as project:
            with store.project_writer(project):
                state = store.load_project_state(project)
                with store.ensure_release(project, FOLDER, 1) as folder:
                    with pytest.raises(winfs.ArchiveConflictError, match="^identity$"):
                        if which == "index":
                            store.commit_index(project, replace(index, repository=repository), state.index_sha256)
                        else:
                            store.commit_release(folder, replace(release, repository=repository))
        assert [path.read_bytes() for path in paths] == before


def test_f2_reopen_load_plan_commit_adopts_persisted_case(tmp_path):
    established = RepositoryTarget("Owner", "Repo")
    requested = RepositoryTarget("owner", "repo")
    remote = parse_release(dict(id=1, tag_name="v1", draft=False, prerelease=False,
        created_at="2026-11-13T12:00:00Z", assets=[]))
    catalog = ReleaseCatalog(requested, (remote,), 1, 1, 0, (), "/repos/owner/repo/releases", True)
    index, release = documents()
    index, release = replace(index, repository=established), replace(release, repository=established)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.ensure_project(root, established) as project:
            with store.project_writer(project):
                with store.ensure_release(project, FOLDER, 1) as folder:
                    store.commit_release(folder, release)
                    store.commit_index(project, index, None)
        with store.ensure_project(root, requested) as project:
            with store.project_writer(project):
                state = store.load_project_state(project)
                assert not state.issues
                plan = plan_archive(catalog, archive_root=str(tmp_path), repository=requested, index=state.index)
                assert plan.index.repository == established
                with store.ensure_release(project, FOLDER, 1) as folder:
                    store.commit_release(folder, release)
                    store.commit_index(project, plan.index, state.index_sha256)
                assert store.load_project_state(project).index.repository == established
    assert os.listdir(tmp_path) == ["Owner"]
    assert os.listdir(tmp_path / "Owner") == ["Repo"]
    assert sorted(os.listdir(tmp_path / "Owner" / "Repo")) == [".covearchive.lock", "archive-index.json", FOLDER]


def _f4_snapshot(path):
    return tuple(sorted((entry.relative_to(path).as_posix(),
                         None if entry.is_dir() else entry.read_bytes())
                        for entry in path.rglob("*")))


def _f4_put(path, release, folder=None):
    directory = path / (release.folder if folder is None else folder)
    directory.mkdir(exist_ok=True)
    (directory / "release.json").write_bytes(m.dumps_release_manifest(release).encode())


def test_f4_project_state_default_preserves_four_argument_constructor():
    state = store.ProjectState(None, (), None, ())
    assert state.unindexed == ()
    assert store.ProjectState(None, (), None, (), unindexed=()).unindexed == ()


@pytest.mark.parametrize("index_kind", ["missing", "empty", "directory"])
def test_f4_returns_unindexed_without_modification(tmp_path, index_kind):
    index, release = documents()
    _f4_put(tmp_path, release)
    if index_kind == "empty":
        (tmp_path / "archive-index.json").write_bytes(m.dumps_index(replace(index, releases=())).encode())
    elif index_kind == "directory":
        (tmp_path / "archive-index.json").mkdir()
    before = _f4_snapshot(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        state = store.load_project_state(root)
        assert state.unindexed == (release,)
        assert state.releases == ()
        assert (FOLDER, "unindexed_release") in state.issues
        if index_kind != "empty":
            assert ("archive-index.json", "missing" if index_kind == "missing" else "not_regular_file") in state.issues
        assert not root._children
    assert _f4_snapshot(tmp_path) == before


def test_f4_unindexed_sorted_by_release_id(tmp_path):
    _, release = documents()
    first = replace(release, folder="release-z--1")
    second = replace(release, release_id=2, folder="release-a--2")
    for item in (second, first):
        _f4_put(tmp_path, item)
    before = _f4_snapshot(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        state = store.load_project_state(root)
        assert state.unindexed == (first, second)
        assert state.releases == ()
        assert {(item.folder, "unindexed_release") for item in (first, second)} <= set(state.issues)
    assert _f4_snapshot(tmp_path) == before


@pytest.mark.parametrize("kind,code", [("corrupt", "corrupt"), ("unsupported", "unsupported_version")])
def test_f4_invalid_release_not_returned(tmp_path, kind, code):
    _, release = documents()
    _f4_put(tmp_path, release)
    (tmp_path / FOLDER / "release.json").write_bytes(b"invalid" if kind == "corrupt" else b'{"schema_version":99}')
    before = _f4_snapshot(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        state = store.load_project_state(root)
        assert state.unindexed == ()
        assert (FOLDER, code) in state.issues
        assert (FOLDER, "unindexed_release") not in state.issues
    assert _f4_snapshot(tmp_path) == before


@pytest.mark.parametrize("folder", ["release-other--1", "release-other--2"])
def test_f4_folder_or_id_mismatch_rejected(tmp_path, folder):
    _, release = documents()
    _f4_put(tmp_path, release, folder)
    before = _f4_snapshot(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        state = store.load_project_state(root)
        assert state.unindexed == ()
        assert {(folder, "unindexed_release"), (folder, "consistency")} <= set(state.issues)
    assert _f4_snapshot(tmp_path) == before


@pytest.mark.parametrize("repository,accepted", [(RepositoryTarget("foreign", "repo"), False),
    (RepositoryTarget("owner", "other"), False), (RepositoryTarget("OWNER", "REPO"), False)])
def test_f4_repository_key_validation(tmp_path, repository, accepted):
    index, release = documents()
    candidate = replace(release, repository=repository)
    _f4_put(tmp_path, candidate)
    (tmp_path / "archive-index.json").write_bytes(m.dumps_index(replace(index, releases=())).encode())
    before = _f4_snapshot(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        state = store.load_project_state(root)
        assert state.unindexed == ((candidate,) if accepted else ())
        assert (FOLDER, "unindexed_release") in state.issues
        assert ((FOLDER, "consistency") in state.issues) is not accepted
    assert _f4_snapshot(tmp_path) == before


def test_f4_duplicate_id_against_index_rejected(tmp_path):
    index, release = documents()
    candidate = replace(release, folder="release-duplicate--1")
    for item in (release, candidate):
        _f4_put(tmp_path, item)
    (tmp_path / "archive-index.json").write_bytes(m.dumps_index(index).encode())
    before = _f4_snapshot(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        state = store.load_project_state(root)
        assert state.unindexed == ()
        assert state.releases == (release,)
        assert {(candidate.folder, "unindexed_release"), (candidate.folder, "consistency")} <= set(state.issues)
    assert _f4_snapshot(tmp_path) == before


def test_f4_duplicate_unindexed_ids_reject_both(tmp_path):
    _, release = documents()
    candidates = [replace(release, folder="release-alpha--1"), replace(release, folder="release-beta--1")]
    for item in candidates:
        _f4_put(tmp_path, item)
    before = _f4_snapshot(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        state = store.load_project_state(root)
        assert state.unindexed == ()
        for item in candidates:
            assert {(item.folder, "unindexed_release"), (item.folder, "consistency")} <= set(state.issues)
    assert _f4_snapshot(tmp_path) == before


@pytest.mark.parametrize("cap", [0, 1])
def test_f4_unindexed_cap_preserves_excess_and_issue(tmp_path, monkeypatch, cap):
    _, release = documents()
    candidates = [replace(release, release_id=i, folder=f"release-cap--{i}") for i in (3, 1, 2)]
    for item in candidates:
        _f4_put(tmp_path, item)
    monkeypatch.setattr(m, "MAX_RELEASES", cap)
    before = _f4_snapshot(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        state = store.load_project_state(root)
        ordered = sorted(candidates, key=lambda item: item.release_id)
        assert state.unindexed == tuple(ordered[:cap])
        for item in ordered:
            assert (item.folder, "unindexed_release") in state.issues
            assert ((item.folder, "limit") in state.issues) == (item not in ordered[:cap])
    assert _f4_snapshot(tmp_path) == before


def test_f4_duplicate_id_across_retention_cap(tmp_path, monkeypatch):
    _, release = documents()
    first = replace(release, folder="release-alpha--1")
    second = replace(release, folder="release-beta--1")
    for candidate in (first, second):
        _f4_put(tmp_path, candidate)
    monkeypatch.setattr(m, "MAX_RELEASES", 1)
    before = _f4_snapshot(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        state = store.load_project_state(root)
        assert state.unindexed == ()
        for candidate in (first, second):
            assert (candidate.folder, "consistency") in state.issues
            assert (candidate.folder, "unindexed_release") in state.issues
        assert not any(code == "limit" for _, code in state.issues)
    assert _f4_snapshot(tmp_path) == before


@pytest.mark.parametrize("kind,code", [("corrupt", "corrupt"), ("unsupported", "unsupported_version")])
def test_f4_bad_index_keeps_early_return_without_scanning(tmp_path, kind, code):
    _, release = documents()
    _f4_put(tmp_path, release)
    raw = b"invalid" if kind == "corrupt" else b'{"schema_version":99}'
    (tmp_path / "archive-index.json").write_bytes(raw)
    before = _f4_snapshot(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        state = store.load_project_state(root)
        assert state.unindexed == () and state.releases == () and state.index is None
        assert state.issues == (("archive-index.json", code),)
        assert state.index_sha256 == hashlib.sha256(raw).hexdigest()
    assert _f4_snapshot(tmp_path) == before


@pytest.mark.parametrize("actual,code", [(None, "missing"), (b"x" * len(DATA), "hash_mismatch")])
def test_f4_recovered_completed_claim_requires_actual_verification(tmp_path, actual, code):
    _, release = documents()
    completed = record(status="completed", local_sha256=SHA)
    release = replace(release, assets=(completed,))
    _f4_put(tmp_path, release)
    if actual is not None:
        (tmp_path / FOLDER / completed.saved_name).write_bytes(actual)
    before = _f4_snapshot(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        state = store.load_project_state(root)
        assert state.unindexed == (release,)
        recovered = state.unindexed[0].assets[0]
        with root.open_dir(FOLDER) as folder:
            result = store.verify_asset(folder, recovered)
            assert result.code == code
            with pytest.raises(winfs.ArchiveError, match="unverified"):
                store.completed_record(recovered, result)
    assert _f4_snapshot(tmp_path) == before


def test_r1_unindexed_manifest_retention_is_bounded(tmp_path, monkeypatch):
    import gc
    import weakref

    _, release = documents()
    for release_id in range(1, 6):
        _f4_put(tmp_path, replace(release, release_id=release_id, folder=f"release-memory--{release_id}"))
    cap = 1
    monkeypatch.setattr(m, "MAX_RELEASES", cap)
    earlier = []
    real_read = store._read_document

    def bounded_read(directory, name):
        gc.collect()
        alive = sum(reference() is not None for reference in earlier)
        assert alive <= cap + 1, f"retained {alive} earlier release manifests; cap is {cap}"
        document, raw = real_read(directory, name)
        if name == "release.json":
            earlier.append(weakref.ref(document))
        return document, raw

    monkeypatch.setattr(store, "_read_document", bounded_read)
    before = _f4_snapshot(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        state = store.load_project_state(root)
        assert len(earlier) == 5
        assert len(state.unindexed) == cap
        assert sum(code == "limit" for _, code in state.issues) == 4
    assert _f4_snapshot(tmp_path) == before


def test_docs_claims_manifest_replacement_limitations():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    for text in (readme, store.__doc__):
        normalized = " ".join(text.split())
        assert "may overwrite" in normalized
        assert "a replacement race is detected after publication" not in normalized
        assert "detect namespace changes on a best-effort basis" not in normalized
    assert "Known limitations carried forward" in readme


def _r3a_enable_case_sensitive(directory):
    if sys.platform == "linux": return  # Native Linux test directories are case-sensitive.
    try:
        result = subprocess.run(["fsutil.exe", "file", "setCaseSensitiveInfo", str(directory), "enable"],
                                capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        pytest.skip("fsutil.exe is unavailable; case-sensitive NTFS fixture cannot be enabled")
    print(f"CASE-SENSITIVE NTFS SETUP: {directory}; exit={result.returncode}; {result.stdout}{result.stderr}")
    if result.returncode:
        output = (result.stdout + result.stderr).lower()
        if any(marker in output for marker in ("not supported", "not implemented", "access is denied", "privilege")):
            pytest.skip(f"case-sensitive NTFS feature unavailable: {output}")
        pytest.fail(f"case-sensitive NTFS setup failed with exit {result.returncode}: {output}")


@pytest.mark.parametrize("where", ["owner", "repo"])
@pytest.mark.parametrize("spelling", ["title", "lower", "upper"])
def test_r3a_project_case_sensitive_ambiguity_rejected(tmp_path, where, spelling):
    archive = tmp_path / "archive"
    archive.mkdir()
    if where == "repo":
        (archive / "Owner").mkdir()
    parent = archive if where == "owner" else archive / "Owner"
    _r3a_enable_case_sensitive(parent)
    established = "Owner" if where == "owner" else "Repo"
    for name in (established, established.lower()):
        (parent / name).mkdir()
    assert sorted(os.listdir(parent)) == [established, established.lower()]
    requested = {"title": established, "lower": established.lower(), "upper": established.upper()}[spelling]
    repository = RepositoryTarget(requested, "Repo") if where == "owner" else RepositoryTarget("Owner", requested)
    before = _f4_snapshot(archive)
    with winfs.ArchiveRoot.open(str(archive)) as root:
        with pytest.raises(winfs.ArchiveConflictError, match="^case_alias$"):
            with store.ensure_project(root, repository):
                pytest.fail("ambiguous case-sensitive project was accepted")
        assert not root._children
    assert _f4_snapshot(archive) == before


@pytest.mark.parametrize("where", ["owner", "repo"])
def test_r3a_project_case_sensitive_unique_case_reused(tmp_path, where):
    archive = tmp_path / "archive"
    archive.mkdir()
    if where == "repo":
        (archive / "Owner").mkdir()
    parent = archive if where == "owner" else archive / "Owner"
    _r3a_enable_case_sensitive(parent)
    if where == "owner":
        (archive / "Owner").mkdir()
    (archive / "Owner" / "Repo").mkdir()
    before = _f4_snapshot(archive)
    with winfs.ArchiveRoot.open(str(archive)) as root:
        with store.ensure_project(root, RepositoryTarget("Owner", "Repo")) as project:
            owner_info, repo_info = project.parent.info(), project.info()
        with store.ensure_project(root, RepositoryTarget("OWNER", "REPO")) as project:
            assert (project.parent.name, project.name) == ("Owner", "Repo")
            for handle, previous in ((project.parent, owner_info), (project, repo_info)):
                actual = handle.info()
                assert (actual.volume_serial, actual.file_id) == (previous.volume_serial, previous.file_id)
        assert not root._children
    assert _f4_snapshot(archive) == before


def test_r3b_duplicates_free_capacity_for_later_unique_manifest(tmp_path, monkeypatch):
    _, release = documents()
    alpha = replace(release, folder="release-alpha--1")
    beta = replace(release, folder="release-beta--1")
    gamma = replace(release, release_id=2, folder="release-gamma--2")
    for candidate in (alpha, beta, gamma):
        _f4_put(tmp_path, candidate)
    monkeypatch.setattr(m, "MAX_RELEASES", 2)
    before = _f4_snapshot(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        state = store.load_project_state(root)
        assert state.unindexed == (gamma,)
        for candidate in (alpha, beta):
            assert (candidate.folder, "consistency") in state.issues
        assert not any(code == "limit" for _, code in state.issues)
        assert (gamma.folder, "consistency") not in state.issues
        for candidate in (alpha, beta, gamma):
            assert (candidate.folder, "unindexed_release") in state.issues
    assert _f4_snapshot(tmp_path) == before


def test_r3b_retention_uses_folder_scan_order_before_result_id_order(tmp_path, monkeypatch):
    _, release = documents()
    first = replace(release, release_id=3, folder="release-a--3")
    second = replace(release, folder="release-b--1")
    for candidate in (second, first):
        _f4_put(tmp_path, candidate)
    monkeypatch.setattr(m, "MAX_RELEASES", 1)
    before = _f4_snapshot(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        state = store.load_project_state(root)
        assert state.unindexed == (first,)
        assert (second.folder, "limit") in state.issues
        assert (first.folder, "limit") not in state.issues
        assert not any(code == "consistency" for _, code in state.issues)
    assert _f4_snapshot(tmp_path) == before


def _r3b_completed_recovery_fixture(tmp_path):
    _, release = documents()
    release = replace(release, repository=RepositoryTarget("Recovered", "Spelling"),
                      assets=(record(status="completed", local_sha256=SHA),))
    _f4_put(tmp_path, release)
    (tmp_path / FOLDER / release.assets[0].saved_name).write_bytes(DATA)
    return release


def test_r3b_missing_index_reconciliation_adopts_recovered_spelling(tmp_path):
    original = _r3b_completed_recovery_fixture(tmp_path)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.project_writer(root):
            state = store.load_project_state(root)
            assert state.index is None and state.unindexed == (original,)
            recovered = state.unindexed[0]
            repository = recovered.repository
            with store.ensure_release(root, recovered.folder, recovered.release_id) as folder:
                proof = store.verify_asset(folder, recovered.assets[0])
                assert proof.code == "verified"
                completed = store.completed_record(recovered.assets[0], proof)
                reconciled = replace(recovered, assets=(completed,))
                store.commit_release(folder, reconciled)
                record = m.ReleaseRecord(reconciled.release_id, reconciled.tag, reconciled.folder, reconciled.assets)
                store.commit_index(root, m.ProjectIndex(repository, (record,)), None)
                assert store.verify_asset(folder, completed).code == "verified"
            loaded = store.load_project_state(root)
            assert not loaded.issues and loaded.unindexed == ()
            assert loaded.releases == (reconciled,) and loaded.index.repository == repository
    assert (tmp_path / FOLDER / original.assets[0].saved_name).read_bytes() == DATA


def test_r3b_accepted_limitation_case_changed_reconciliation_identity(tmp_path):
    """ACCEPTED LIMITATION: callers must adopt recovered repository spelling."""
    original = _r3b_completed_recovery_fixture(tmp_path)
    path = tmp_path / FOLDER / "release.json"
    before = path.read_bytes()
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.project_writer(root):
            state = store.load_project_state(root)
            assert state.index is None and state.unindexed == (original,)
            recovered = state.unindexed[0]
            different = RepositoryTarget(recovered.repository.owner.lower(), recovered.repository.repo.lower())
            assert different.repo_key == recovered.repository.repo_key and different != recovered.repository
            with store.ensure_release(root, recovered.folder, recovered.release_id) as folder:
                with pytest.raises(winfs.ArchiveConflictError, match="^identity$"):
                    store.commit_release(folder, replace(recovered, repository=different))
    assert path.read_bytes() == before
    assert (tmp_path / FOLDER / original.assets[0].saved_name).read_bytes() == DATA
