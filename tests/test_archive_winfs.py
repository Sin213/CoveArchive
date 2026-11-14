"""Real NTFS behaviour; fixtures use only disposable synthetic data."""
import ctypes
import os
import subprocess
import sys

import pytest

from covearchive.archive import winfs

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires Windows NTFS handles")


def test_pinned_root_and_child(tmp_path):
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        assert root.info().is_directory
        with root.open_dir("Owner", create=True) as child:
            assert child.info().is_directory
            with pytest.raises(OSError):
                os.rename(tmp_path / "Owner", tmp_path / "Moved")
            with pytest.raises(OSError):
                os.rmdir(tmp_path / "Owner")
            with pytest.raises(OSError):
                os.rename(tmp_path, str(tmp_path) + "-moved")


@pytest.mark.parametrize("name", ["..", ".", "C:\\bad", "\\bad", "a:b", "a/b", "a\\b",
                                      "bad.", "bad ", "CON"])
def test_components_rejected_before_open(tmp_path, name):
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with pytest.raises(winfs.ArchiveUnsafePathError):
            root.open_dir(name, create=True)
    assert list(tmp_path.iterdir()) == []


def test_exclusive_temp_io_identity_and_no_replace(tmp_path):
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with root.create_temp(token="a" * 32) as temp:
            with pytest.raises(winfs.ArchiveConflictError):
                root.create_temp(token="a" * 32)
            temp.write(b"payload")
            temp.flush()
            before = temp.info()
            assert before.size == 7 and before.links == 1
            temp.rewind()
            assert temp.read(3) == b"pay"
            temp.rename(root, "asset.bin")
            assert temp.info().file_id == before.file_id
        with root.open_file("asset.bin") as saved:
            assert saved.read_bounded(7) == b"payload"
            saved.rewind()
            with pytest.raises(winfs.ArchiveError, match="oversized"):
                saved.read_bounded(6)
        with root.create_temp() as second:
            second.write(b"different")
            with pytest.raises(winfs.ArchiveConflictError):
                second.rename(root, "asset.bin")
            second.delete_temp()
    assert (tmp_path / "asset.bin").read_bytes() == b"payload"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["asset.bin"]


def test_case_alias_rejected(tmp_path):
    (tmp_path / "ExactCase").mkdir()
    (tmp_path / "ExactFile.bin").write_bytes(b"x")
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        for name, opener in [("exactcase", root.open_dir), ("exactfile.bin", root.open_file)]:
            with pytest.raises(winfs.ArchiveConflictError, match="case_alias"):
                opener(name)


def junction(link, target):
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_junction_root_child_and_file_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "junction"
    junction(link, target)
    try:
        with pytest.raises(winfs.ArchiveUnsafePathError, match="reparse_point"):
            winfs.ArchiveRoot.open(str(link))
        with winfs.ArchiveRoot.open(str(tmp_path)) as root:
            for opener in [root.open_dir, root.open_file]:
                with pytest.raises(winfs.ArchiveUnsafePathError, match="reparse_point"):
                    opener("junction")
    finally:
        os.rmdir(link)


def test_hardlink_query_and_metadata_replace(tmp_path):
    (tmp_path / "original").write_bytes(b"one")
    os.link(tmp_path / "original", tmp_path / "linked")
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with root.open_file("linked") as file:
            assert file.info().links == 2
        for data in [b"old", b"new"]:
            with root.create_temp() as temp:
                temp.write(data)
                temp.flush()
                temp.rename(root, "release.json", replace=True, metadata=True)
        with root.open_file("release.json", metadata=True) as file:
            assert file.read_bounded(10) == b"new"
        with root.create_temp() as temp:
            with pytest.raises(winfs.ArchiveUnsafePathError):
                temp.rename(root, "original", replace=True)
            temp.delete_temp()
    assert (tmp_path / "original").read_bytes() == b"one"


def test_lock_busy_and_directory_enumeration(tmp_path):
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with root.open_lock():
            with pytest.raises(winfs.ArchiveBusyError):
                root.open_lock()
        with root.open_lock():
            assert ".covearchive.lock" in root.names()
        with pytest.raises(winfs.ArchiveError, match="missing"):
            root.open_file("missing")


def test_short_name_alias_rejected_when_available(tmp_path):
    path = tmp_path / "LongDirectoryName"
    path.mkdir()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetShortPathNameW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    kernel.GetShortPathNameW.restype = ctypes.c_uint32
    buf = ctypes.create_unicode_buffer(32768)
    assert kernel.GetShortPathNameW(str(path), buf, len(buf))
    short = buf.value.rsplit("\\", 1)[-1]
    if short == path.name:
        pytest.skip("8.3 aliases disabled on this volume")
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with pytest.raises(winfs.ArchiveConflictError, match="case_alias"):
            root.open_dir(short)


def test_close_invalidates_descendants_and_delete_only_owned_temps(tmp_path):
    root = winfs.ArchiveRoot.open(str(tmp_path))
    child = root.open_dir("child", create=True)
    temp = child.create_temp()
    temp.write(b"x")
    temp.delete_temp()
    root.close()
    root.close()
    with pytest.raises(winfs.ArchiveError, match="closed"):
        child.info()
    (tmp_path / "asset").write_bytes(b"keep")
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with root.open_file("asset") as file:
            with pytest.raises(winfs.ArchiveUnsafePathError, match="not_temp"):
                file.delete_temp()
    assert (tmp_path / "asset").read_bytes() == b"keep"


def test_close_continues_after_each_child_cleanup_failure(tmp_path):
    root = winfs.ArchiveRoot.open(str(tmp_path))
    children = []
    try:
        for token in ("a" * 32, "b" * 32):
            name = ".covearchive-tmp-" + token + ".part"
            path = tmp_path / name
            path.write_bytes(b"keep")
            os.link(path, tmp_path / token)
            children.append(root.open_temp(name))
        with pytest.raises(winfs.ArchiveUnsafePathError, match="not_regular_file"):
            root.close()
        for handle in [root, *children]:
            with pytest.raises(winfs.ArchiveError, match="closed"):
                handle.info()
        assert not root._children
        root.close()
        for token in ("a" * 32, "b" * 32):
            assert (tmp_path / token).read_bytes() == b"keep"
            assert (tmp_path / (".covearchive-tmp-" + token + ".part")).read_bytes() == b"keep"
    finally:
        for handle in [*children, root]:
            try:
                handle.close()
            except winfs.ArchiveUnsafePathError:
                pass


def test_held_manifest_posix_replace_unlinks_validated_handle(tmp_path):
    (tmp_path / "release.json").write_bytes(b"old")
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with root.open_file("release.json", metadata=True, hold=True) as held:
            assert not held.unlinked()
            with root.create_temp() as temp:
                temp.write(b"new")
                temp.flush()
                temp.rename(root, "release.json", replace=True, metadata=True)
            assert held.unlinked()
            assert held.read_bounded(10) == b"old"
    assert (tmp_path / "release.json").read_bytes() == b"new"


@pytest.mark.parametrize("name", ["a", "1", "\u00e9", "ab", "\U0001F600", "a\U0001F600"])
def test_rename_utf16_lengths_real_ntfs(tmp_path, name):
    payload = b"synthetic UTF-16 rename payload"
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with root.create_temp() as temporary:
            temporary.write(payload)
            temporary.rename(root, name)
            assert temporary.name == name
            temporary.rewind()
            assert temporary.read() == payload
        assert os.listdir(tmp_path) == [name]
    assert (tmp_path / name).read_bytes() == payload
    assert os.listdir(tmp_path) == [name]


def test_f2_fold_case_opt_in_preserves_disk_name(tmp_path):
    (tmp_path / "Established").mkdir()
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with pytest.raises(winfs.ArchiveConflictError, match="case_alias"):
            root.open_dir("established")
        assert not root._children
        with root.open_dir("established", fold_case=True) as child:
            assert child.name == "Established"
        assert not root._children
    assert os.listdir(tmp_path) == ["Established"]


@pytest.mark.parametrize("actual", ["Different", "ESTABL~1", "Establishe\u0301d"])
def test_f2_fold_rejects_other_disk_names_without_leak(tmp_path, monkeypatch, actual):
    (tmp_path / "Established").mkdir()
    monkeypatch.setattr(winfs.Handle, "disk_name", lambda self: actual)
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with pytest.raises(winfs.ArchiveConflictError, match="case_alias"):
            root.open_dir("established", fold_case=True)
        assert not root._children
    assert os.listdir(tmp_path) == ["Established"]


def test_f2_fold_rejects_ambiguity_without_leak(tmp_path, monkeypatch):
    (tmp_path / "Established").mkdir()
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        monkeypatch.setattr(root, "names", lambda: ("Established", "ESTABLISHED"))
        with pytest.raises(winfs.ArchiveConflictError, match="case_alias"):
            root.open_dir("established", fold_case=True)
        assert not root._children
    assert os.listdir(tmp_path) == ["Established"]


@pytest.mark.parametrize("name", ["..", "a/b", "C:\\bad", "CON", "bad.", "bad "])
def test_f2_fold_unsafe_components_before_open(tmp_path, name):
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with pytest.raises(winfs.ArchiveUnsafePathError, match="^component$"):
            root.open_dir(name, create=True, fold_case=True)
        assert not root._children
    assert os.listdir(tmp_path) == []


def test_f2_fold_rejects_real_short_alias(tmp_path):
    from pathlib import Path
    long_path = tmp_path / "EstablishedLongDirectory"
    long_path.mkdir()
    short_path = ctypes.create_unicode_buffer(32768)
    get_short = winfs._kernel.GetShortPathNameW
    get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_short.restype = ctypes.c_uint32
    length = get_short(str(long_path), short_path, len(short_path))
    assert 0 < length < len(short_path)
    alias = Path(short_path.value).name
    if alias == long_path.name:
        pytest.skip("8.3 alias generation is disabled for this directory")
    assert alias.casefold() != long_path.name.casefold()
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with pytest.raises(winfs.ArchiveConflictError, match="case_alias"):
            root.open_dir(alias, fold_case=True)
        assert not root._children
    assert os.listdir(tmp_path) == [long_path.name]


def _r3a_enable_case_sensitive(directory):
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


@pytest.mark.parametrize("requested", ["Owner", "owner", "OWNER"])
def test_r3a_real_case_sensitive_ambiguity_rejected(tmp_path, requested):
    parent = tmp_path / "sensitive"
    parent.mkdir()
    _r3a_enable_case_sensitive(parent)
    for name in ("Owner", "owner"):
        (parent / name).mkdir()
    assert sorted(os.listdir(parent)) == ["Owner", "owner"]
    before = os.listdir(parent)
    with winfs.ArchiveRoot.open(str(parent)) as root:
        with pytest.raises(winfs.ArchiveConflictError, match="^case_alias$"):
            root.open_dir(requested, create=True, fold_case=True)
        assert not root._children
    assert os.listdir(parent) == before


def test_r3a_real_case_sensitive_unique_case_reused(tmp_path):
    parent = tmp_path / "sensitive"
    parent.mkdir()
    _r3a_enable_case_sensitive(parent)
    (parent / "Owner").mkdir()
    with winfs.ArchiveRoot.open(str(parent)) as root:
        with root.open_dir("Owner") as established:
            before = established.info()
        with root.open_dir("OWNER", create=True, fold_case=True) as reopened:
            after = reopened.info()
            assert reopened.name == "Owner"
            assert (after.volume_serial, after.file_id) == (before.volume_serial, before.file_id)
        assert not root._children
    assert os.listdir(parent) == ["Owner"]


@pytest.mark.parametrize("requested", ["established", "Established", "ESTABLISHED"])
def test_r3a_names_ambiguity_including_exact_spelling(tmp_path, monkeypatch, requested):
    (tmp_path / "Established").mkdir()
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        monkeypatch.setattr(root, "names", lambda: ("Established", "ESTABLISHED"))
        with pytest.raises(winfs.ArchiveConflictError, match="^case_alias$"):
            root.open_dir(requested, fold_case=True)
        assert not root._children
    assert os.listdir(tmp_path) == ["Established"]


def test_r3a_non_ascii_case_variant_remains_strict(tmp_path):
    established, requested = "\u00c9stablished", "\u00e9stablished"
    (tmp_path / established).mkdir()
    with winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with pytest.raises(winfs.ArchiveConflictError, match="^case_alias$"):
            root.open_dir(requested, create=True, fold_case=True)
        assert not root._children
    assert os.listdir(tmp_path) == [established]
