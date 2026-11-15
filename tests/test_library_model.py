"""Behavioral checks for pure library metadata."""
import pytest
import builtins
import json
import os
from pathlib import Path
import shutil
from dataclasses import FrozenInstanceError, replace
from time import perf_counter
from unittest.mock import Mock

from covearchive.library import model as m


@pytest.mark.parametrize("url", ["https://github.com/Owner/Repo", "https://github.com/owner/repo.git",
    "https://www.github.com/OWNER/REPO/releases/?tab=x#y", "github.com/Owner/Repo/"])
def test_identity_matches_repository_key(url):
    assert m.Repository("github", url).identity == "github:owner/repo"


@pytest.mark.parametrize("provider,url,valid", [("github", "https://github.com/o/r.git.git", False),
    ("github", "https://www.github.com/O/R.git.git/releases/?x#y", False), ("gitlab", "https://host/g/r.git.git", False),
    ("gitea", "http://Host:3000/g/r.git.git/", False), ("github", "https://www.github.com/Owner/Repo.git/releases/?x#y", True),
    ("github", "https://github.com/Owner/Repo.GIT?x", True), ("gitlab", "HTTP://Host.Example:3000/Group/Repo.git/", True),
    ("gitea", "https://forge.example/g/r/", True)])
def test_normalization_is_idempotent_or_rejected(provider, url, valid):
    if not valid:
        error = pytest.raises(m.LibraryError, m.normalize_repository, url, provider)
        assert error.value.code == "url"
        if provider == "github": assert m.add_urls(m.Library(), url).failed == 1
        return
    normalized = m.normalize_repository(url, provider)
    assert m.normalize_repository(normalized[1], provider) == normalized
    lib = m.Library().create_collection("Group", id="g").add_repository(url, provider=provider)
    assert m.load_library(m.dump_library(lib)) == lib
    moved = lib.rename_repository(lib.repositories[0].identity, "Name").move_repositories([lib.repositories[0].identity], "g")
    assert moved.repositories[0].identity == lib.repositories[0].identity


@pytest.mark.parametrize("name", ["owner/repo", "  Dolphin / Nightly  "])
def test_repository_names_allow_slashes_but_collections_do_not(name):
    lib = m.Library().add_repository("https://github.com/o/r", name=name)
    assert lib is not None, "Friendly repository names must accept slashes"
    assert lib.repositories[0].name == name.strip()
    assert lib.rename_repository("github:o/r", name).repositories[0].name == name.strip()
    with pytest.raises(m.LibraryError): lib.create_collection(name)


def sample():
    return (m.Library().create_collection("Top", id="a")
            .create_collection("Child", "a", id="b")
            .create_collection("Empty", id="c")
            .add_repository("https://github.com/Owner/One", collection="a")
            .add_repository("https://github.com/Owner/Two", collection="b")
            .add_repository("https://github.com/Owner/Three"))


def test_immutable_operations_and_delete_preserves_descendant_repositories():
    lib = sample()
    original = m.dump_library(lib)
    clone = lib.clone()
    assert clone is not lib and clone == lib
    renamed = lib.rename_repository("github:owner/one", "Friendly")
    assert renamed.repositories[0].identity == "github:owner/one"
    assert renamed.repositories[0].display_name == "Friendly"
    moved = lib.move_repositories(["github:owner/one", "github:owner/three"], "b")
    assert len(moved.browse("b")) == 3
    deleted = lib.delete_collection("b")
    assert len(deleted.repositories) == 3
    assert deleted.repositories[1].collection == "a"
    deleted = lib.delete_collection("a")
    assert [c.id for c in deleted.collections] == ["c"]
    assert all(r.collection is None for r in deleted.repositories)
    assert len(lib.remove_repositories(["github:owner/one"]).repositories) == 2
    assert m.dump_library(lib) == original
    pytest.raises((FrozenInstanceError, AttributeError), setattr, lib, "repositories", ())
    pytest.raises(FrozenInstanceError, setattr, lib.repositories[0], "name", "Changed")


def test_recursive_browse_search_and_collection_operations():
    lib = sample()
    assert len(lib.browse()) == 3 and len(lib.browse("a")) == 2
    assert len(lib.browse(m.UNSORTED)) == 1
    assert [r.ref for r in lib.browse("a", search="CHILD")] == ["owner/two"]
    assert len(lib.browse(search="GITHUB")) == 3
    assert not lib.browse("a", search="three")
    renamed = lib.rename_collection("a", "Games")
    assert renamed.collection_path("b") == "Games/Child"
    moved = lib.move_collection("b", "c")
    assert moved.collection_path("b") == "Empty/Child"
    assert len(moved.browse("a")) == 1


@pytest.mark.parametrize("action", [
    lambda x: x.create_collection("top", id="d"),
    lambda x: x.create_collection("Other", id="a"),
    lambda x: x.rename_collection("c", "TOP"),
    lambda x: x.move_collection("a", "b"),
    lambda x: x.move_collection("a", "missing"),
    lambda x: x.move_repositories(["github:owner/one", "missing"], "b"),
    lambda x: x.remove_repositories(["github:owner/one", "missing"]),
    lambda x: x.rename_repository("missing", "Name"),
    lambda x: x.add_repository("https://github.com/o/r", collection="missing"),
])
def test_failures_do_not_partially_mutate(action):
    lib = sample(); before = m.dump_library(lib)
    pytest.raises(m.LibraryError, action, lib)
    assert m.dump_library(lib) == before


@pytest.mark.parametrize("name", ["", "  ", "a/b", "x" * 101, "bad\x00", "bad\ud800", "bad\u2028",
    "bad\u2029", "bad\u061c", "bad\u200e", "bad\u200f", "bad\u202a", "bad\u2069", 3, True])
def test_bad_names(name):
    pytest.raises(m.LibraryError, m.Library().create_collection, name)


def test_unicode_names_normalized_unique_and_zwj_allowed():
    lib = m.Library().create_collection("  Café  ", id="a")
    pytest.raises(m.LibraryError, lib.create_collection, "CAFE\u0301", id="b")
    lib = lib.create_collection("Straße", id="b")
    pytest.raises(m.LibraryError, lib.create_collection, "STRASSE", id="c")
    assert lib.create_collection("👩\u200d💻", id="c").collections[-1].name == "👩\u200d💻"


@pytest.mark.parametrize("provider,url,code", [
    ("github", "https://github.com/o/r/releases/tag/v1", "release_tag"),
    ("github", "https://user:SECRET@github.com/o/r", "userinfo"),
    ("gitlab", "https://user:SECRET@gitlab.com/o/r", "url"),
    ("gitlab", "https://gitlab.com/o/r?token=SECRET", "url"),
    ("gitea", "https://forge.example/o/r#frag", "url"),
    ("gitea", "ftp://forge.example/o/r", "url"),
    ("gitea", "https://forge.example:99999/o/r", "url"),
    ("gitea", "https://forge.example/o/../r", "url"),
    ("gitea", "https://forge.example/o/%72", "url"),
    ("gitea", "https://forge.example/o", "url"),
    ("gitea", "https://forge.example/" + "/".join(["a"] * 11), "url"),
    ("unsupported", "https://github.com/o/r", "provider"),
])
def test_invalid_urls_are_typed_and_never_echo_secrets(provider, url, code):
    exc = pytest.raises(m.LibraryError, m.Repository, provider, url)
    assert exc.value.code == code
    assert "SECRET" not in str(exc.value) and url not in str(exc.value)


def test_inactive_normalization_and_dedupe_preserves_first_metadata():
    repo = m.Repository("GITEA", "HTTP://Forge.Example:3000/Owner/Repo.git/", folder="C:/inert")
    assert repo.identity == "gitea:forge.example:3000/owner/repo"
    assert repo.url == "http://forge.example:3000/Owner/Repo" and not repo.active
    lib = m.Library().add_repository(repo)
    assert lib.add_repository("http://forge.example:3000/owner/repo", provider="gitea") == lib
    lib = lib.add_repository("https://gitlab.com/Group/Repo.git", provider="gitlab")
    assert len(lib.repositories) == 2 and all(not r.active for r in lib.repositories)
    lib = lib.add_repository("https://github.com/Owner/Repo", name="First")
    assert lib.add_repository("https://github.com/owner/repo/releases", name="Second") == lib


def test_bulk_line_results_first_wins_and_secrets_suppressed():
    lib = m.Library().create_collection("Group", id="a")
    result = m.add_urls(lib, "\nhttps://github.com/O/R\nhttps://github.com/o/r.git\n"
        "https://SECRET:pw@github.com/o/r\ninvalid\n", collection="a")
    assert (result.added, result.skipped, result.failed) == (1, 1, 2)
    assert [r.line for r in result.results] == [2, 3, 4, 5]
    assert [r.status for r in result.results] == ["ADDED", "SKIPPED", "FAILED", "FAILED"]
    assert "SECRET" not in repr(result)
    assert len(result.library.repositories) == 1 and not lib.repositories
    assert result.library.repositories[0].collection == "a"
    again = m.add_urls(result.library, "https://github.com/o/r/releases")
    assert again.skipped == 1 and again.added == 0


def test_limits_and_depth_move_validate_whole_subtree():
    lib = m.Library()
    for i in range(8):
        lib = lib.create_collection(str(i), str(i - 1) if i else None, id=str(i))
    pytest.raises(m.LibraryError, lib.create_collection, "deep", "7", id="deep").match("depth")
    lib = lib.create_collection("root", id="root").create_collection("child", "root", id="child")
    pytest.raises(m.LibraryError, lib.move_collection, "root", "6")
    pytest.raises(m.LibraryError, m.Repository, "github", "https://github.com/o/r", folder="x" * 513)
    pytest.raises(m.LibraryError, m.Repository, "github", "https://github.com/o/r?" + "x" * 2048)
    pytest.raises(m.LibraryError, m.add_urls, lib, "x" * (1024 * 1024 + 1))
    pytest.raises(m.LibraryError, m.add_urls, lib, "x\n" * 10001)
    pytest.raises(m.LibraryError, m.Library, collections=[m.Collection(str(i), str(i)) for i in range(2001)])
    pytest.raises(m.LibraryError, m.Library, repositories=[m.Repository("github", f"https://github.com/o/r{i}") for i in range(10001)])


def test_strict_schema_roundtrip_bom_and_original_case():
    lib = sample().add_repository("https://gitlab.com/G/R", provider="gitlab", folder="../inert")
    raw = m.dump_library(lib)
    assert m.load_library(raw) == lib and m.load_library(b"\xef\xbb\xbf" + raw) == lib
    data = json.loads(raw)
    assert set(data) == {"version", "collections", "repositories"}
    assert set(data["repositories"][0]) == {"provider", "url", "name", "collection", "folder"}
    assert data["repositories"][0]["url"] == "https://github.com/Owner/One"


@pytest.mark.parametrize("raw,code", [(b"broken", "json"), (b"\xff", "json"),
    (b'{"version":1,"version":1,"collections":[],"repositories":[]}', "duplicate_key"),
    (b'{"version":1,"collections":[{"id":"a","name":"a","name":"b","parent":null}],"repositories":[]}', "duplicate_key"),
    (b'{"version":NaN}', "nonfinite"), (b'{"version":Infinity}', "nonfinite"), (b"[" * 10000 + b"]" * 10000, "json"),
    (b'{"version":1,"collections":[],"repositories":[]}'.ljust(8 * 1024 * 1024 + 1), "size")],
    ids=["syntax", "utf8", "duplicate", "nested_duplicate", "nan", "infinity", "depth_bomb", "oversized"])
def test_bad_json(raw, code):
    for load in (m.load_library, m.strict_json_loads):
        with pytest.raises(m.LibraryError) as exc: load(raw)
        assert exc.value.code == code


@pytest.mark.parametrize("depth", [1500, 10000])
@pytest.mark.parametrize("opening,closing", [(b"[", b"]"), (b'{"a":', b"}")])
def test_json_nesting_bombs(depth, opening, closing):
    test_bad_json(opening * depth + b"0" + closing * depth, "json")


def test_modest_json_depth_loads():
    raw = b'[' * 8 + b'0' + b']' * 8
    assert m.strict_json_loads(raw) == json.loads(raw)
    lib = m.Library(repositories=(m.Repository("github", "https://github.com/o/r"),))
    assert m.load_library(m.dump_library(lib)) == lib


@pytest.mark.parametrize("change", ["bool_version", "newer", "unknown", "collection_type", "repo_type",
    "parent", "cycle", "duplicate_id", "duplicate_identity", "bad_id", "repo_unknown", "collection_ref"])
def test_bad_schema(change):
    data = json.loads(m.dump_library(sample()))
    if change == "bool_version": data["version"] = True
    if change == "newer": data["version"] = 2
    if change == "unknown": data["token"] = "SECRET"
    if change == "collection_type": data["collections"] = {}
    if change == "repo_type": data["repositories"][0]["name"] = False
    if change == "parent": data["collections"][0]["parent"] = "missing"
    if change == "cycle": data["collections"][0]["parent"] = "b"
    if change == "duplicate_id": data["collections"].append(data["collections"][0])
    if change == "duplicate_identity": data["repositories"].append(data["repositories"][0])
    if change == "bad_id": data["collections"][0]["id"] = "bad/id"
    if change == "repo_unknown": data["repositories"][0]["token"] = "SECRET"
    if change == "collection_ref": data["repositories"][0]["collection"] = "missing"
    exc = pytest.raises(m.LibraryError, m.load_library, json.dumps(data).encode())
    assert "SECRET" not in str(exc.value)
    if change == "newer": assert exc.value.code == "newer_version"


def test_model_never_touches_filesystem(monkeypatch, tmp_path):
    archive = tmp_path / "archive"; archive.mkdir(); (archive / "release.zip").write_bytes(b"archive")
    prefs = tmp_path / "preferences.json"; prefs.write_bytes(b"preferences")
    queue = tmp_path / "queue.db"; queue.write_bytes(b"queue")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    forbidden = Mock(side_effect=AssertionError("Library touched the filesystem"))
    with monkeypatch.context() as patch:
        for obj, names in [(builtins, ["open"]), (Path, ["open", "mkdir", "unlink", "rename", "replace", "rmdir", "touch"]),
                           (os, ["open", "mkdir", "makedirs", "remove", "unlink", "rename", "replace", "rmdir"]),
                           (shutil, ["rmtree", "move", "copy", "copyfile", "copytree"])]:
            for name in names: patch.setattr(obj, name, forbidden)
        lib = sample().rename_collection("a", "Renamed").move_collection("b", "c")
        lib = lib.delete_collection("c").remove_repositories(["github:owner/one"])
        lib = m.add_urls(lib, "https://github.com/o/new").library
        assert m.load_library(m.dump_library(lib)) == lib
    assert {p: p.read_bytes() for p in before} == before


def test_repository_normalizes_once_and_replace_preserves_value_semantics(monkeypatch):
    counted = Mock(wraps=m.normalize_repository)
    monkeypatch.setattr(m, "normalize_repository", counted)
    repo = m.Repository("github", "https://github.com/Owner/Repo.git")
    for _ in range(3):
        assert repo.ref == "owner/repo"
        assert repo.identity == "github:owner/repo"
        assert repo.display_name == "Owner/Repo"
    assert counted.call_count == 1
    changed = replace(repo, url="https://github.com/Other/New.git")
    assert changed.ref == "other/new" and changed.identity == "github:other/new"
    assert changed.display_name == "Other/New" and counted.call_count == 2
    renamed = replace(repo, name="Friendly")
    assert renamed.identity == repo.identity and renamed.display_name == "Friendly"
    equal = m.Repository("github", "https://github.com/Owner/Repo")
    assert equal == repo and hash(equal) == hash(repo)
    pytest.raises(FrozenInstanceError, setattr, repo, "ref", "other/new")


def test_ten_thousand_entry_search_and_mutation_performance():
    lib = m.Library(repositories=tuple(m.Repository("github", f"https://github.com/Fictional/Repo{i}")
        for i in range(10000)))
    start = perf_counter()
    found = lib.browse(search="repo9999")
    search_ms = (perf_counter() - start) * 1000
    start = perf_counter()
    grouped = lib.create_collection("Group", id="group")
    create_ms = (perf_counter() - start) * 1000
    start = perf_counter()
    moved = grouped.move_repositories(["github:fictional/repo9999"], "group")
    move_ms = (perf_counter() - start) * 1000
    print(f"10000 repositories: search={search_ms:.3f} ms, create={create_ms:.3f} ms, move-one={move_ms:.3f} ms")
    assert len(found) == 1 and found[0].identity == "github:fictional/repo9999"
    assert moved.repositories[-1].collection == "group" and lib.repositories[-1].collection is None
    assert search_ms <= 300 and create_ms <= 300 and move_ms <= 300
def test_auto_archive_loader_accepts_optional_key():
    data = m.library_to_data(m.Library().add_repository("https://github.com/o/r"))
    data["repositories"][0]["auto_archive"] = 6
    try: lib = m.library_from_data(data)
    except m.LibraryError as exc: pytest.fail("optional auto_archive rejected: " + exc.code)
    assert lib.repositories[0].auto_archive == 6
    assert m.load_library(m.dump_library(lib)) == lib


def test_auto_archive_default_off_and_legacy_bytes():
    raw = b'{"version":1,"collections":[],"repositories":[{"provider":"github","url":"https://github.com/o/r","name":null,"collection":null,"folder":null}]}\n'
    lib = m.load_library(raw)
    assert lib.repositories[0].auto_archive is None and m.dump_library(lib) == raw
    assert m.Repository("github", "https://github.com/o/r").auto_archive is None
    assert m.add_urls(m.Library(), "https://github.com/o/r").library == lib
    enabled = lib.set_auto_archive("github:o/r", 6)
    assert lib.repositories[0].auto_archive is None
    assert m.Library().add_repository(enabled.repositories[0]) == lib
    assert enabled.remove_repositories("github:o/r").add_repository("https://github.com/o/r") == lib
    assert enabled.set_auto_archive("github:o/r", None) == lib
    assert m.dump_library(enabled.set_auto_archive("github:o/r", None)) == raw


@pytest.mark.parametrize("hours", [1, 6, 12, 24])
def test_auto_archive_retained_by_metadata_operations(hours):
    assert m.AUTO_ARCHIVE_HOURS == (1, 6, 12, 24)
    lib = sample().set_auto_archive("github:owner/one", hours)
    changed = lib.rename_repository("github:owner/one", "New").move_repositories("github:owner/one", "b")
    changed = changed.rename_collection("a", "Renamed").move_collection("b", None).delete_collection("b")
    assert changed.repositories[0].auto_archive == hours
    assert m.load_library(m.dump_library(changed)) == changed
    with pytest.raises(m.LibraryError) as exc: lib.set_auto_archive("github:missing/repo", hours)
    assert exc.value.code == "repository_missing"


@pytest.mark.parametrize("hours", [True, False, 3, 6.0, "6", 0, 2, 25])
def test_auto_archive_invalid_values(hours):
    with pytest.raises(m.LibraryError) as exc: m.Repository("github", "https://github.com/o/r", auto_archive=hours)
    assert exc.value.code == "auto_archive"
    lib = m.Library().add_repository("https://github.com/o/r")
    with pytest.raises(m.LibraryError) as exc: lib.set_auto_archive("github:o/r", hours)
    assert exc.value.code == "auto_archive"


@pytest.mark.parametrize("provider", ["gitlab", "gitea", "other"])
def test_auto_archive_inactive_provider_rejected(provider):
    with pytest.raises(m.LibraryError) as exc: m.Repository(provider, "https://host/o/r", auto_archive=6)
    assert exc.value.code == "auto_archive"


@pytest.mark.parametrize("hours", [True, 3, 6.0, "6", 0, 25])
def test_auto_archive_invalid_file_stays_read_only(tmp_path, monkeypatch, hours):
    from covearchive.ui.library_store import LibraryStore
    for key in ("LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_STATE_HOME"): monkeypatch.setenv(key, str(tmp_path/key))
    data = m.library_to_data(m.Library().add_repository("https://github.com/o/r"))
    data["repositories"][0]["auto_archive"] = hours
    with pytest.raises(m.LibraryError) as exc: m.library_from_data(data)
    assert exc.value.code == "auto_archive"
    path = tmp_path/"library.json"; raw = json.dumps(data).encode(); path.write_bytes(raw)
    store = LibraryStore(path); lib, problem, writable = store.load()
    assert not writable and problem and not lib.repositories
    assert store.save(m.Library()) and path.read_bytes() == raw


def test_auto_archive_loader_keeps_strict_keys():
    data = m.library_to_data(m.Library().add_repository("https://github.com/o/r"))
    data["repositories"][0].update(auto_archive=6, unknown=True)
    with pytest.raises(m.LibraryError) as exc: m.library_from_data(data)
    assert exc.value.code == "schema"
