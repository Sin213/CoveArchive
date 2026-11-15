"""Real Linux filesystem and process exclusion evidence (no network required)."""
from dataclasses import replace
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import textwrap

import pytest

from covearchive.archive import store, naming, manifest as m
if sys.platform == "linux":
    from covearchive.archive import posixfs as fs
from covearchive.github.urls import RepositoryTarget
from covearchive.queue.store import RunnerLock, QueueStore, default_queue_path
from covearchive.queue.models import QueueBusyError
from covearchive.ui.preferences import PreferenceStore

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="requires Linux dirfd, proc fd linkat and flock")
SOURCE = Path(__file__).resolve().parents[1]/"src"
DATA = b"native filesystem payload"
SHA = hashlib.sha256(DATA).hexdigest()


def child(code, *args, **options):
    return subprocess.Popen([sys.executable, "-I", "-B", "-c",
        f"import sys; sys.path.insert(0,{str(SOURCE)!r});\n" + textwrap.dedent(code),
        *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **options)


def result(process):
    try:
        out, err = process.communicate(timeout=5)
        assert process.returncode == 0, out + err
        return out.strip()
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=3)


def test_backend_and_error_identity():
    from covearchive.archive import winfs
    assert store.ArchiveRoot is fs.ArchiveRoot
    assert fs.ArchiveError is winfs.ArchiveError
    assert fs._component is winfs._component


@pytest.mark.parametrize("position", ["root", "directory", "file", "lock", "temp"])
def test_symlinks_rejected_before_target_open(tmp_path, monkeypatch, position):
    target = tmp_path/"target"; target.mkdir()
    (target/"data").write_bytes(DATA)
    link = tmp_path/"link"
    link.symlink_to(target, target_is_directory=True)
    if position == "root":
        with pytest.raises(fs.ArchiveUnsafePathError): fs.ArchiveRoot.open(str(link))
        return
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        name = {"directory":"link", "file":"asset.bin", "lock":".covearchive.lock",
                "temp":".covearchive-tmp-" + "a"*32 + ".part"}[position]
        if position != "directory": (tmp_path/name).symlink_to(target/"data")
        original = os.open
        def checked_open(path, flags, *args, **kwargs):
            fd = original(path, flags, *args, **kwargs)
            if path == name:
                os.close(fd)
                pytest.fail("kernel followed a child symlink before verification")
            return fd
        monkeypatch.setattr(fs.os, "open", checked_open)
        with pytest.raises(fs.ArchiveUnsafePathError):
            if position == "directory": root.open_dir(name)
            elif position == "file": root.open_file(name)
            elif position == "lock": root.open_lock()
            else: root.open_temp(name)
    assert (target/"data").read_bytes() == DATA


@pytest.mark.parametrize("where", ["root", "directory"])
def test_substitution_after_pin_fails_closed(tmp_path, where):
    authorized = tmp_path/"authorized"; authorized.mkdir()
    outside = tmp_path/"outside"; outside.mkdir()
    with fs.ArchiveRoot.open(str(authorized)) as root:
        with root.open_dir("child", create=True) as directory:
            path = authorized if where == "root" else authorized/"child"
            path.rename(path.with_name(path.name + "-old"))
            path.symlink_to(outside, target_is_directory=True)
            with pytest.raises(fs.ArchiveUnsafePathError, match="directory_changed"):
                directory.create_temp()
    assert not list(outside.iterdir())


def test_directory_substitution_during_child_open(tmp_path, monkeypatch):
    (tmp_path/"child").mkdir()
    outside = tmp_path/"outside"; outside.mkdir()
    original = os.open
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        def racing_open(path, flags, *args, **kwargs):
            fd = original(path, flags, *args, **kwargs)
            if path == "child":
                (tmp_path/"child").rename(tmp_path/"old")
                (tmp_path/"child").symlink_to(outside, target_is_directory=True)
            return fd
        monkeypatch.setattr(fs.os, "open", racing_open)
        with pytest.raises(fs.ArchiveUnsafePathError, match="directory_changed"):
            root.open_dir("child")
    assert not list(outside.iterdir())


@pytest.mark.parametrize("kind", ["fifo", "socket", "hardlink"])
def test_nonregular_and_aliases_rejected(tmp_path, kind):
    path = tmp_path/"asset.bin"
    if kind == "fifo": os.mkfifo(path)
    elif kind == "socket":
        # A stale Unix socket inode needs no bind permission: mknod is sufficient.
        os.mknod(path, stat.S_IFSOCK | 0o600)
    else:
        path.write_bytes(DATA); os.link(path, tmp_path/"alias")
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        with pytest.raises(fs.ArchiveError): root.open_file("asset.bin")


def test_device_rejected():
    # Read-only open of an existing device needs no mknod privileges.
    with fs.ArchiveRoot.open("/dev") as root:
        with pytest.raises(fs.ArchiveUnsafePathError, match="not_regular_file"):
            root.open_file("null")


def test_temp_exclusive_collision_and_cleanup(tmp_path):
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        with root.create_temp(token="a"*32) as temp:
            assert stat.S_IMODE(os.fstat(temp._get()).st_mode) == 0o600
            temp.write(DATA)
            with pytest.raises(fs.ArchiveConflictError, match="exists"):
                root.create_temp(token="a"*32)
            temp.rewind(); assert temp.read() == DATA
        assert root.names() == ()
        with pytest.raises(RuntimeError):
            with root.create_temp(): raise RuntimeError("interrupted stream")
        assert root.names() == ()


def test_recovery_does_not_delete_live_process_temp(tmp_path):
    process = child("""
        from covearchive.archive import posixfs as fs
        with fs.ArchiveRoot.open(sys.argv[1]) as root, root.create_temp() as temp:
            print(temp.name, flush=True); sys.stdin.readline()
    """, tmp_path, stdin=subprocess.PIPE)
    try:
        name = process.stdout.readline().strip()
        assert fs.TEMP_PATTERN.fullmatch(name)
        with fs.ArchiveRoot.open(str(tmp_path)) as root:
            recovery = store.recover(root)
            assert recovery.removed == () and recovery.skipped == ((name,"busy"),)
        assert (tmp_path/name).exists()
        process.stdin.write("close\n"); process.stdin.flush()
        assert result(process) == ""
        assert not (tmp_path/name).exists()
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=3)


def test_noreplace_never_overwrites_binary(tmp_path):
    path = tmp_path/"asset.bin"; path.write_bytes(DATA); inode = path.stat().st_ino
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        with root.create_temp() as temp:
            temp.write(b"replacement"); temp.flush()
            with pytest.raises(fs.ArchiveConflictError, match="exists"):
                temp.rename(root, "asset.bin")
            with pytest.raises(fs.ArchiveUnsafePathError, match="replace_forbidden"):
                temp.rename(root, "asset.bin", replace=True)
    assert path.read_bytes() == DATA and path.stat().st_ino == inode
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("error", [errno.EPERM, errno.EACCES, errno.EOPNOTSUPP, errno.EMLINK,
    errno.ENOENT, errno.EXDEV, errno.ENOSYS, errno.EINVAL, None])
def test_unsupported_noreplace_fails_closed(tmp_path, monkeypatch, error):
    def unsupported(*args):
        fs.C.set_errno(error); return -1
    monkeypatch.setattr(fs, "_linkat", unsupported if error else None)
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        with pytest.raises(fs.ArchiveError, match="noreplace_unsupported"):
            store.write_asset(root, "asset.bin", [DATA], len(DATA))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("size,digest,code", [(1,None,"overflow"), (100,None,"underflow"),
                                                        (len(DATA),"0"*64,"hash_mismatch")])
def test_mismatch_never_publishes(tmp_path, size, digest, code):
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        with pytest.raises(fs.ArchiveError, match=code):
            store.write_asset(root, "asset.bin", [DATA], size, digest)
    assert not list(tmp_path.iterdir())


def test_actual_bytes_verification_and_orphan_cleanup(tmp_path):
    record = m.AssetRecord(1, "asset.bin", "asset.bin", len(DATA), SHA, None, "planned")
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        assert store.write_asset(root, "asset.bin", [DATA], len(DATA), SHA).code == "published"
        assert store.verify_asset(root, record).code == "verified"
        orphan = ".covearchive-tmp-" + "b"*32 + ".part"
        (tmp_path/orphan).write_bytes(b"unfinished")
        assert store.recover(root).removed == (orphan,)
        (tmp_path/"asset.bin").write_bytes(b"x"*len(DATA))
        assert store.verify_asset(root, record).code == "hash_mismatch"


def test_project_lock_exclusion_real_process(tmp_path):
    code = """
        from covearchive.archive import posixfs as fs
        with fs.ArchiveRoot.open(sys.argv[1]) as root:
            try: lock=root.open_lock()
            except fs.ArchiveBusyError: print('busy')
            else: lock.close(); print('owned')
    """
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        with root.open_lock(): assert result(child(code, tmp_path)) == "busy"
        assert result(child(code, tmp_path)) == "owned"
    assert (tmp_path/".covearchive.lock").is_file()


def test_runner_lock_exclusion_close_and_crash(tmp_path):
    path = tmp_path/"queue.sqlite3"
    code = """
        from covearchive.queue.store import RunnerLock
        from covearchive.queue.models import QueueBusyError
        try: lock=RunnerLock(sys.argv[1])
        except QueueBusyError as exc: print(exc.code)
        else: lock.close(); print('owned')
    """
    lock = RunnerLock(path)
    assert result(child(code, path)) == "runner_active"
    lock.close(); lock.close()
    assert result(child(code, path)) == "owned"
    process = child("""
        import os
        from covearchive.queue.store import RunnerLock
        lock=RunnerLock(sys.argv[1]); os._exit(0)
    """, path)
    result(process)
    assert result(child(code, path)) == "owned"
    assert Path(str(path)+".lock").exists()


def test_lock_not_inherited_by_exec(tmp_path):
    path = tmp_path/"q.db"
    with RunnerLock(path) as lock:
        fd = lock._file.fileno()
        assert not os.get_inheritable(fd)
        assert result(child("""
            import os
            try: os.fstat(int(sys.argv[1]))
            except OSError: print('closed')
            else: raise AssertionError('lock descriptor inherited')
        """, fd, close_fds=False)) == "closed"


def test_worker_shutdown_retains_cross_process_ownership(tmp_path):
    import threading
    from test_queue_runner import prepare, _start, result as archive_result
    from test_queue_store import item
    entered, finish = threading.Event(), threading.Event()
    def archive(*args, **kwargs):
        entered.set(); assert finish.wait(5)
        return archive_result("CANCELLED", "cancelled")
    db, runner = prepare(tmp_path,[item()],archive=archive)
    thread, errors, _ = _start(runner)
    code = """
        from covearchive.queue.store import RunnerLock
        from covearchive.queue.models import QueueBusyError
        try: lock=RunnerLock(sys.argv[1])
        except QueueBusyError: print('busy')
        else: lock.close(); print('owned')
    """
    try:
        assert entered.wait(3)
        assert runner.shutdown(timeout=0).unresolved
        assert result(child(code,db.path)) == "busy"
    finally:
        finish.set(); thread.join(3); runner.shutdown()
    assert not thread.is_alive() and not errors
    assert result(child(code,db.path)) == "owned"


def test_linux_queue_root_identity_is_case_sensitive(tmp_path):
    from test_queue_store import item
    db = QueueStore(tmp_path/"q.db")
    first = db.enqueue(*item(root="/Archive")).job
    second = db.enqueue(*item(root="/archive")).job
    assert first.job_id != second.job_id and first.project_key != second.project_key


@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink"])
def test_runner_lock_unsafe_inode(tmp_path, kind):
    path = tmp_path/"q.db"; lockpath = Path(str(path)+".lock")
    target = tmp_path/"target"; target.write_bytes(DATA)
    if kind == "symlink": lockpath.symlink_to(target)
    elif kind == "fifo": os.mkfifo(lockpath)
    else: os.link(target, lockpath)
    with pytest.raises(QueueBusyError, match="runner_active"): RunnerLock(path)
    assert target.read_bytes() == DATA


@pytest.mark.parametrize("relative", [False, True])
def test_xdg_paths_and_private_directory_modes(tmp_path, monkeypatch, relative):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config" if relative else str(tmp_path/"config"))
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state" if relative else str(tmp_path/"state"))
    config = tmp_path/(".config" if relative else "config")/"covearchive"
    state = tmp_path/(".local/state" if relative else "state")/"covearchive"
    prefs = PreferenceStore(); assert prefs.path == config/"preferences.json"
    assert prefs.save(prefs.load()[0]) is None
    assert default_queue_path() == state/"queue.sqlite3"
    QueueStore()
    for directory in (config, state):
        while directory != tmp_path:
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            directory = directory.parent
    assert not (tmp_path/"relative").exists()


@pytest.mark.parametrize("root", ["relative", "C:/Archive", r"C:\Archive", "/a/../b", "/a/./b", "/a//b", "/a/", "//a", "/a\0b"])
def test_invalid_native_roots(root):
    with pytest.raises(naming.PathBudgetError, match="root"): naming.validate_root(root)


def test_native_byte_budgets():
    assert naming.validate_root(PurePosixPath("/native/archive")) == 15
    assert naming.validate_root("/" + "é"*127) == 255
    with pytest.raises(naming.PathBudgetError, match="budget"):
        naming.validate_root("/" + "é"*128)
    root = "/" + "/".join(["r"*250]*15)
    naming.check_path_budget(root, "o", "r", "release-v1--1", "é"*127)
    with pytest.raises(naming.PathBudgetError, match="budget"):
        naming.check_path_budget(root, "o", "r", "release-v1--1", "é"*128)
    deeper = root + "/" + "s"*250
    with pytest.raises(naming.PathBudgetError, match="budget"):
        naming.check_path_budget(deeper, "o", "r", "release-v1--1", "x"*100)


def test_native_asset_allocation_uses_bytes_without_changing_identity():
    assert naming.filename_limit("/archive","Owner","Repo","release-v1--1") == 120
    assert naming.project_folders("Owner","Repo") == ("Owner","Repo")
    assert naming.release_folder_name("v1",7) == "release-v1--7"
    assets = {1:"é"*100+".zip", 2:"é"*100+".zip", 3:"x"*110+".zip"}
    names = naming.allocate_asset_names(assets,limit=120)
    assert names[3].saved_name == assets[3]
    assert len({n.saved_name for n in names.values()}) == 3
    assert all(len(os.fsencode(n.saved_name)) <= 120 for n in names.values())
    assert all(naming.is_safe_component(n.saved_name) for n in names.values())


def test_manifest_release_first_interruption_and_repair(tmp_path):
    repo = RepositoryTarget("Owner", "Repo")
    asset = m.AssetRecord(1,"asset.bin","asset.bin",len(DATA),SHA,SHA,"completed")
    record = m.ReleaseRecord(7,"v1","release-v1--7",(asset,))
    index = m.ProjectIndex(repo,(record,))
    release = m.ReleaseManifest(repo,7,"v1",record.folder,None,None,False,None,(asset,))
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.ensure_project(root,repo) as project, store.project_writer(project):
            with store.ensure_release(project,record.folder,7) as directory:
                store.write_asset(directory,asset.saved_name,[DATA],len(DATA),SHA)
                store.commit_release(directory,release)
            # A stopped publisher leaves release metadata ahead of a missing index.
            state = store.load_project_state(project)
            assert state.index is None and state.unindexed == (release,)
            store.commit_index(project,index,None)
            state = store.load_project_state(project)
            assert state.index == index and state.releases == (release,) and not state.issues
            with store.ensure_release(project,record.folder,7) as directory:
                store.commit_release(directory,release)
            store.commit_index(project,index,state.index_sha256)
            assert not store.load_project_state(project).issues
    assert (tmp_path/"Owner"/"Repo"/record.folder/asset.saved_name).read_bytes() == DATA


@pytest.mark.parametrize("interrupted", [False, True])
def test_native_archive_engine_stream_commit_reopen(tmp_path, monkeypatch, interrupted):
    from contextlib import contextmanager
    from covearchive.archive import download
    from test_queue_store import item
    repo, release, asset, _ = item()
    asset = replace(asset,size=len(DATA),sha256=SHA)
    release = replace(release,assets=(asset,))
    calls = []
    @contextmanager
    def stream(repository, selected, **options):
        assert repository == repo and selected == asset
        calls.append(selected.id)
        yield iter((DATA[:4],DATA[4:]))
    monkeypatch.setattr(download,"open_asset_stream",stream)
    if interrupted:
        with monkeypatch.context() as patch:
            def fail(*args): raise fs.ArchiveError("os")
            patch.setattr(store,"commit_index",fail)
            failed = download.archive_asset(repo,release,asset,tmp_path)
            assert failed.code == "manifest_commit_incomplete"
    result = download.archive_asset(repo,release,asset,tmp_path)
    assert result.status.value == ("ALREADY_ARCHIVED" if interrupted else "ARCHIVED")
    assert result.local_sha256 == SHA and result.integrity == "PUBLISHER_VERIFIED"
    assert download.archive_asset(repo,release,asset,tmp_path).status.value == "VERIFIED_EXISTING"
    assert calls == [asset.id]
    with fs.ArchiveRoot.open(str(tmp_path)) as root, store.ensure_project(root,repo) as project:
        state = store.load_project_state(project)
        assert not state.issues and len(state.releases) == 1
        m.check_consistent(state.index,state.releases[0])


def _swap_temp(directory, name, kind):
    kept = directory/(".covearchive-tmp-" + "e"*32 + ".part")
    (directory/name).rename(kept)
    if kind == "regular": (directory/name).write_bytes(b"foreign")
    elif kind == "symlink":
        (directory/"foreign-target").write_bytes(b"foreign")
        (directory/name).symlink_to(directory/"foreign-target")
    else: (directory/name).mkdir()
    return kept


def _foreign_untouched(directory, name, kind):
    path = directory/name
    if kind == "regular": assert path.read_bytes() == b"foreign"
    elif kind == "symlink": assert path.is_symlink() and path.read_bytes() == b"foreign"
    else: assert path.is_dir() and not list(path.iterdir())


@pytest.mark.parametrize("kind", ["regular", "symlink", "directory"])
@pytest.mark.parametrize("boundary", ["linkat", "cleanup"])
def test_fd_publication_survives_temp_name_substitution(tmp_path, monkeypatch, kind, boundary):
    with fs.ArchiveRoot.open(str(tmp_path)) as root, root.create_temp() as temp:
        temp.write(DATA); temp.flush()
        identity = fs._identity(os.fstat(temp._get())); old_name = temp.name
        link, unlink = fs._linkat, fs._unlink_owned
        def publish(*args):
            _swap_temp(tmp_path,old_name,kind)
            return link(*args)
        def cleanup(*args):
            _swap_temp(tmp_path,old_name,kind)
            return unlink(*args)
        monkeypatch.setattr(fs,"_linkat",publish) if boundary == "linkat" else monkeypatch.setattr(fs,"_unlink_owned",cleanup)
        temp.rename(root,"asset.bin")
        assert (tmp_path/"asset.bin").read_bytes() == DATA
        assert fs._identity((tmp_path/"asset.bin").stat()) == identity
        _foreign_untouched(tmp_path,old_name,kind)
        claim = m.AssetRecord(1,"asset.bin","asset.bin",len(DATA),SHA,None,"planned")
        assert store.verify_asset(root,claim).code == "verified"


@pytest.mark.parametrize("kind", ["regular", "symlink", "directory"])
def test_fd_publication_remains_bound_during_residual_unlink_race(tmp_path, monkeypatch, kind):
    """Linux cannot atomically compare-and-unlink; final publication stays fd-bound."""
    with fs.ArchiveRoot.open(str(tmp_path)) as root, root.create_temp() as temp:
        temp.write(DATA); temp.flush()
        identity = fs._identity(os.fstat(temp._get())); old_name = temp.name
        unlink = os.unlink
        def at_syscall(name, *args, **kwargs):
            if name == old_name:
                # Deliberately swap AFTER the final stat, at the unlink syscall.
                _swap_temp(tmp_path,old_name,kind)
            return unlink(name,*args,**kwargs)
        monkeypatch.setattr(fs.os,"unlink",at_syscall)
        temp.rename(root,"asset.bin")
        assert (tmp_path/"asset.bin").read_bytes() == DATA
        assert fs._identity((tmp_path/"asset.bin").stat()) == identity
        if kind == "directory": _foreign_untouched(tmp_path,old_name,kind)
        else: assert not (tmp_path/old_name).is_symlink() and not (tmp_path/old_name).exists()


@pytest.mark.parametrize("kind", ["regular", "symlink", "directory"])
def test_delete_temp_rechecks_identity_before_unlink(tmp_path, monkeypatch, kind):
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        with root.create_temp() as temp:
            temp.write(DATA); old_name = temp.name
            unlink = fs._unlink_owned
            def cleanup(*args):
                _swap_temp(tmp_path,old_name,kind)
                return unlink(*args)
            monkeypatch.setattr(fs,"_unlink_owned",cleanup)
            temp.delete_temp()
        _foreign_untouched(tmp_path,old_name,kind)
        assert (tmp_path/(".covearchive-tmp-" + "e"*32 + ".part")).read_bytes() == DATA


def test_unlinked_source_fails_closed_without_publishing_substitute(tmp_path, monkeypatch):
    with fs.ArchiveRoot.open(str(tmp_path)) as root, root.create_temp() as temp:
        temp.write(DATA); temp.flush(); old_name = temp.name
        link = fs._linkat
        def publish(*args):
            (tmp_path/old_name).unlink()
            (tmp_path/old_name).write_bytes(b"foreign")
            return link(*args)
        monkeypatch.setattr(fs,"_linkat",publish)
        with pytest.raises(fs.ArchiveError,match="noreplace_unsupported"):
            temp.rename(root,"asset.bin")
    assert not (tmp_path/"asset.bin").exists()
    assert (tmp_path/old_name).read_bytes() == b"foreign"


def test_missing_proc_fd_links_fails_closed(tmp_path, monkeypatch):
    link = fs._linkat
    def no_proc(source_fd, source, directory_fd, name, flags):
        assert source.startswith(b"/proc/self/fd/")
        return link(source_fd,b"/proc/self/covearchive-no-proc-fd",directory_fd,name,flags)
    monkeypatch.setattr(fs,"_linkat",no_proc)
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        with pytest.raises(fs.ArchiveError,match="noreplace_unsupported"):
            store.write_asset(root,"asset.bin",[DATA],len(DATA),SHA)
    assert not list(tmp_path.iterdir())


def test_crash_between_fd_link_and_unlink_is_recoverable(tmp_path):
    process = child("""
        import os
        from covearchive.archive import posixfs as fs
        with fs.ArchiveRoot.open(sys.argv[1]) as root, root.create_temp(token='a'*32) as temp:
            temp.write(sys.argv[2].encode()); temp.flush()
            fs._unlink_owned=lambda *args: os._exit(71)
            temp.rename(root,'asset.bin')
    """,tmp_path,DATA.decode())
    try:
        out,err = process.communicate(timeout=5)
        assert process.returncode == 71, out+err
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=3)
    final = tmp_path/"asset.bin"; orphan = ".covearchive-tmp-" + "a"*32 + ".part"
    assert final.stat().st_nlink == 2 and fs._identity(final.stat()) == fs._identity((tmp_path/orphan).stat())
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        claim = m.AssetRecord(1,"asset.bin","asset.bin",len(DATA),SHA,None,"planned")
        assert store.verify_asset(root,claim).code == "verified"
        assert store.recover(root).removed == (orphan,)
    assert final.read_bytes() == DATA and final.stat().st_nlink == 1
    assert list(tmp_path.iterdir()) == [final]


def test_recovery_alias_disappearing_during_open_is_safe(tmp_path, monkeypatch):
    final = tmp_path/"asset.bin"; final.write_bytes(DATA)
    orphan = tmp_path/(".covearchive-tmp-" + "a"*32 + ".part")
    os.link(final,orphan)
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        scan = fs._temp_sibling
        def concurrent_cleanup(directory_fd,name,info):
            orphan.unlink()
            return scan(directory_fd,name,info)
        monkeypatch.setattr(fs,"_temp_sibling",concurrent_cleanup)
        with root.open_file(final.name) as opened:
            assert opened.info().links == 1 and opened.read() == DATA


@pytest.mark.parametrize("mismatch", ["inode", "device", "symlink"])
def test_two_links_need_identical_regular_temp_sibling(tmp_path, monkeypatch, mismatch):
    final = tmp_path/"asset.bin"; final.write_bytes(DATA)
    os.link(final,tmp_path/"foreign-alias")
    sibling = tmp_path/(".covearchive-tmp-" + "a"*32 + ".part")
    if mismatch == "symlink": sibling.symlink_to(final)
    else: sibling.write_bytes(b"foreign")
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        if mismatch == "device":
            from types import SimpleNamespace
            real_stat = os.stat; info = final.stat()
            def stat_other_device(path,*args,**kwargs):
                if path == sibling.name:
                    return SimpleNamespace(st_dev=info.st_dev+1,st_ino=info.st_ino,st_mode=stat.S_IFREG|0o600)
                return real_stat(path,*args,**kwargs)
            monkeypatch.setattr(fs.os,"stat",stat_other_device)
        with pytest.raises(fs.ArchiveUnsafePathError,match="multiple_links"):
            root.open_file(final.name)


def test_more_than_two_links_rejected_even_with_matching_temp_siblings(tmp_path):
    final = tmp_path/"asset.bin"; final.write_bytes(DATA)
    for token in ("a","b"):
        os.link(final,tmp_path/(".covearchive-tmp-" + token*32 + ".part"))
    with fs.ArchiveRoot.open(str(tmp_path)) as root:
        with pytest.raises(fs.ArchiveUnsafePathError,match="multiple_links"):
            root.open_file(final.name)


def test_recovery_temp_cannot_publish_a_third_link(tmp_path):
    final = tmp_path/"asset.bin"; final.write_bytes(DATA)
    name = ".covearchive-tmp-" + "a"*32 + ".part"; os.link(final,tmp_path/name)
    with fs.ArchiveRoot.open(str(tmp_path)) as root, root.open_temp(name) as temp:
        with pytest.raises(fs.ArchiveUnsafePathError,match="multiple_links"):
            temp.rename(root,"another.bin")
    assert not (tmp_path/"another.bin").exists() and final.stat().st_nlink == 1


def test_archive_engine_restarts_after_fd_link_crash_without_transfer(tmp_path, monkeypatch):
    from covearchive.archive import download
    from test_queue_store import item
    process = child(f"sys.path.insert(0,{str(Path(__file__).parent)!r});\n" + textwrap.dedent("""
        import os
        from dataclasses import replace
        from contextlib import contextmanager
        from covearchive.archive import posixfs as fs, download
        from test_queue_store import item
        repo,release,asset,_=item()
        data=sys.argv[2].encode()
        asset=replace(asset,size=len(data),sha256=sys.argv[3]); release=replace(release,assets=(asset,))
        @contextmanager
        def stream(*args,**kwargs): yield iter((data,))
        download.open_asset_stream=stream
        fs._unlink_owned=lambda *args: os._exit(71)
        download.archive_asset(repo,release,asset,sys.argv[1])
    """),tmp_path,DATA.decode(),SHA)
    try:
        out,err = process.communicate(timeout=5)
        assert process.returncode == 71, out+err
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=3)
    orphans = list(tmp_path.rglob("*.part")); assert len(orphans) == 1
    assert orphans[0].stat().st_nlink == 2
    repo,release,asset,_ = item()
    asset = replace(asset,size=len(DATA),sha256=SHA); release = replace(release,assets=(asset,))
    def no_network(*args,**kwargs): raise AssertionError("verified crash recovery requested network")
    monkeypatch.setattr(download,"open_asset_stream",no_network)
    outcome = download.archive_asset(repo,release,asset,tmp_path)
    assert outcome.status.value == "ALREADY_ARCHIVED" and outcome.local_sha256 == SHA
    assert not list(tmp_path.rglob("*.part"))
    owner,project = naming.project_folders(repo.owner,repo.repo)
    binary = tmp_path/owner/project/outcome.release_folder/outcome.saved_name
    assert binary.read_bytes() == DATA and binary.stat().st_nlink == 1


def test_folded_creation_serializes_processes_on_parent(tmp_path):
    import fcntl
    import time
    archive = tmp_path/"archive"; archive.mkdir()
    barrier = tmp_path/"barrier"; barrier.mkdir()
    code = """
        import time
        from pathlib import Path
        from covearchive.archive import posixfs as fs, store
        from covearchive.github.urls import RepositoryTarget
        archive,barrier,number,owner,repo=sys.argv[1:]
        barrier=Path(barrier); names=fs.Directory.names
        def snapshot(directory):
            seen=names(directory)
            if directory.name=='archive':
                (barrier/number).write_bytes(b'ready')
                end=time.monotonic()+5
                while not (barrier/'release').exists():
                    if time.monotonic()>end: raise AssertionError('parent did not release snapshot barrier')
                    time.sleep(.005)
            return seen
        fs.Directory.names=snapshot
        print('ready',flush=True); sys.stdin.readline()
        with fs.ArchiveRoot.open(archive) as root, store.ensure_project(root,RepositoryTarget(owner,repo)) as project:
            print(project.parent.name+'/'+project.name,flush=True)
    """
    processes = [child(code,archive,barrier,str(i),owner,repo,stdin=subprocess.PIPE)
                 for i,(owner,repo) in enumerate((("Owner","Repo"),("owner","repo")))]
    def wait_for(predicate):
        end = time.monotonic()+3
        while not predicate():
            assert time.monotonic() < end, "snapshot barrier timed out"
            time.sleep(.005)
    try:
        for process in processes: assert process.stdout.readline().strip() == "ready"
        for process in processes: process.stdin.write("go\n"); process.stdin.flush()
        wait_for(lambda: (barrier/'0').exists() or (barrier/'1').exists())
        # The first process now holds its names snapshot until explicitly
        # released. Probe kernel ownership, without assuming scheduler timing.
        probe = os.open(archive,os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
        try:
            try:
                fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                serialized = False
            except BlockingIOError:
                serialized = True
        finally: os.close(probe)
        if not serialized:
            # A dropped-lock mutant must let both empty snapshots reach the
            # barrier before either creator proceeds, forcing duplicate trees.
            wait_for(lambda: (barrier/'0').exists() and (barrier/'1').exists())
        (barrier/'release').touch()
        paths = [result(process) for process in processes]
        assert paths[0] == paths[1]
        owners = list(archive.iterdir()); assert len(owners) == 1
        assert len(list(owners[0].iterdir())) == 1
    finally:
        (barrier/'release').touch()
        for process in processes:
            if process.poll() is None: process.kill(); process.wait(timeout=3)


@pytest.mark.parametrize("suffix", ["\udcff", "\ud800"])
def test_non_utf8_roots_are_typed_rejections(tmp_path, suffix):
    from covearchive.queue.models import InvalidJobError
    from test_queue_store import item
    bad_root = str(tmp_path/"archive") + suffix
    if suffix == "\udcff": Path(bad_root).mkdir()
    with pytest.raises(naming.PathBudgetError,match="^root$"): naming.validate_root(bad_root)
    db = QueueStore(tmp_path/"q.db")
    with pytest.raises(InvalidJobError,match="invalid_job"): db.enqueue(*item(root=bad_root))
    assert not db.list_jobs()
    with pytest.raises(fs.ArchiveUnsafePathError,match="^root$"): fs.ArchiveRoot.open(bad_root)


@pytest.mark.parametrize("busy", ["project", "temp"])
def test_linux_busy_ownership_is_retryable_without_network_or_binary_change(tmp_path, monkeypatch, busy):
    from contextlib import contextmanager
    from covearchive.archive import download
    from test_queue_store import item
    repo, release, asset, _ = item()
    asset = replace(asset,size=len(DATA),sha256=SHA); release = replace(release,assets=(asset,))
    requests = []
    @contextmanager
    def stream(*args,**kwargs):
        requests.append(asset.id); yield iter((DATA,))
    monkeypatch.setattr(download,"open_asset_stream",stream)
    initial = download.archive_asset(repo,release,asset,tmp_path)
    assert initial.status.value == "ARCHIVED"
    requests.clear()
    def unexpected_network(*args,**kwargs): raise AssertionError("busy archive requested network")
    monkeypatch.setattr(download,"open_asset_stream",unexpected_network)
    with fs.ArchiveRoot.open(str(tmp_path)) as root, store.ensure_project(root,repo) as project:
        with store.ensure_release(project,initial.release_folder,release.id) as directory:
            binary = tmp_path/project.parent.name/project.name/directory.name/initial.saved_name
            identity = fs._identity(binary.stat())
            (binary.parent/"release.json").unlink()  # Restoration checks ownership before transfer.
            with (project.open_lock() if busy == "project" else directory.create_temp()):
                outcome = download.archive_asset(repo,release,asset,tmp_path)
                assert outcome.status.value == "FAILED" and outcome.retryable
                assert outcome.code == ("manifest_commit_incomplete" if busy == "project" else "file_busy")
            assert binary.read_bytes() == DATA and fs._identity(binary.stat()) == identity
    assert not requests and not list(tmp_path.rglob("*.part"))
