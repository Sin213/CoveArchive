"""Isolated, non-destructive library persistence."""
import sys
from pathlib import Path
import pytest
from covearchive.library import model as m
from covearchive.ui import library_store as s
from test_ui_library import isolated_data


@pytest.mark.parametrize("provider,url", [("github", "https://www.github.com/O/R.git/releases/?x#y"), ("github", "https://github.com/O/R.GIT"), ("gitlab", "http://Host:3000/Group/Repo.git/"), ("gitea", "https://forge.example/g/r.git/")])
def test_roundtrip_and_no_implicit_creation(tmp_path, provider, url):
    path = tmp_path / "missing" / "library.json"; store = s.LibraryStore(path)
    lib, problem, writable = store.load()
    assert writable and problem is None and not path.parent.exists()
    lib = lib.create_collection("Games", id="a").add_repository(url, provider=provider, name="Owner / Repo", collection="a")
    assert store.save(lib) is None and s.LibraryStore(path).load() == (lib, None, True)


@pytest.mark.parametrize("raw", [b"broken", b'{"version":2}', b'{"version":1,"version":1,"collections":[],"repositories":[]}', b'{"version":NaN}',
    b'{"version":1,"collections":[],"repositories":[]}'.ljust(8 * 1024 * 1024 + 1), b'{"version":true,"collections":[],"repositories":[]}', b"\xff",
    b'{"version":1,"collections":[{"id":"a","name":"a","name":"b","parent":null}],"repositories":[]}'],
    ids=["corrupt", "newer", "duplicate", "nonfinite", "oversized", "schema", "utf8", "nested_duplicate"])
def test_bad_files_preserved_and_latched(tmp_path, raw):
    path = tmp_path / "library.json"; path.write_bytes(raw); store = s.LibraryStore(path)
    lib, problem, writable = store.load()
    assert lib == m.Library() and not writable and str(path) in problem
    assert "read-only" in store.save(lib) and path.read_bytes() == raw
    assert list(tmp_path.iterdir()) == [path]
    if raw == b'{"version":2}': assert "newer version" in problem.lower()
    if len(raw) > 8 * 1024 * 1024: assert "8 MiB" in problem


@pytest.mark.parametrize("kind", ["corrupt", "newer", "valid", "appeared", "oversized"])
def test_external_changes_refused(tmp_path, kind):
    path = tmp_path / "library.json"; store = s.LibraryStore(path); lib = m.Library()
    if kind != "appeared": assert store.save(lib) is None
    store.load()
    raw = {"corrupt": b"broken", "newer": b'{"version":2}', "valid": m.dump_library(lib.create_collection("External", id="e")),
           "appeared": m.dump_library(lib), "oversized": m.dump_library(lib).ljust(8 * 1024 * 1024 + 1)}[kind]
    path.write_bytes(raw)
    assert "changed on disk" in store.save(lib) and not store.writable
    assert path.read_bytes() == raw and store.load()[0] == lib and list(tmp_path.iterdir()) == [path]


def failed_save(monkeypatch, stage):
    calls = []
    class FailedSave:
        def __init__(self, name): calls.append(("path", name))
        def setDirectWriteFallback(self, value): calls.append(("fallback", value))
        def open(self, mode): return stage != "open"
        def write(self, data):
            if stage == "exception": raise OSError("injected")
            return -1 if stage == "write" else len(data)
        def commit(self): return stage != "commit"
        def cancelWriting(self): calls.append(("cancel", True))
    monkeypatch.setattr(s, "QSaveFile", FailedSave)
    return calls


@pytest.mark.parametrize("stage", ["open", "write", "commit", "validate", "exception"])
def test_atomic_failure_preserves_file_and_snapshot(tmp_path, monkeypatch, stage):
    path = tmp_path / "library.json"; store = s.LibraryStore(path); lib = m.Library()
    assert store.save(lib) is None
    original = path.read_bytes(); calls = failed_save(monkeypatch, stage)
    change = {} if stage == "validate" else lib.create_collection("Changed", id="c")
    assert store.save(change) and path.read_bytes() == original and store.load()[0] == lib
    assert list(tmp_path.iterdir()) == [path]
    if stage != "validate": assert ("fallback", False) in calls and ("cancel", True) in calls


@pytest.mark.parametrize("platform,xdg", [("win32", None), ("linux", "absolute"), ("linux", "relative")])
def test_default_path_derivation_only(tmp_path, monkeypatch, platform, xdg):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config") if xdg == "absolute" else "relative")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    expected = tmp_path / "local" / "CoveArchive" if platform == "win32" else tmp_path / ("config" if xdg == "absolute" else ".config") / "covearchive"
    store = s.LibraryStore()
    assert store.path == expected / "library.json" and store.load() == (m.Library(), None, True)
    assert not expected.exists()


def test_no_localappdata_and_unreadable_load(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32"); monkeypatch.delenv("LOCALAPPDATA", raising=False)
    store = s.LibraryStore()
    assert store.path is None and store.load()[0] == m.Library() and store.problem and not store.writable
    assert store.save(m.Library())
    path = tmp_path / "library.json"; path.write_bytes(b"preserved")
    def denied(*args, **kwargs): raise OSError("injected unreadable file")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", denied); store = s.LibraryStore(path)
        assert store.load()[0] == m.Library() and not store.writable and store.save(m.Library())
    assert path.read_bytes() == b"preserved"


def test_posix_parent_modes_and_bounded_read(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    original = Path.mkdir; modes = []
    def mkdir(path, mode=0o777, **kwargs):
        modes.append(mode); return original(path, mode=mode, **kwargs)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    store = s.LibraryStore(tmp_path / "private" / "nested" / "library.json")
    assert store.save(m.Library()) is None and modes == [0o700, 0o700]
    reads = []
    class Reader:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, count): reads.append(count); return m.dump_library(m.Library())
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: Reader())
    assert s.LibraryStore(store.path).load()[2] and reads == [8 * 1024 * 1024 + 1]


def test_failed_atomic_export_keeps_existing_file(tmp_path, monkeypatch):
    path = tmp_path / "export.json"; path.write_bytes(b"existing")
    monkeypatch.setattr(s, "QSaveFile", lambda path: (_ for _ in ()).throw(OSError("injected")))
    assert s.atomic_write(path, b"new") and path.read_bytes() == b"existing"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows open handles deny file replacement")
def test_real_qsavefile_replace_failure(tmp_path):
    path = tmp_path / "library.json"; store = s.LibraryStore(path); lib = m.Library()
    assert store.save(lib) is None
    raw = path.read_bytes()
    with open(path, "rb"):
        assert store.save(lib.create_collection("Changed", id="c"))
    assert path.read_bytes() == raw and store.load()[0] == lib and list(tmp_path.iterdir()) == [path]
