"""Portable metadata interchange with synthetic, fictional repositories."""
import json
import builtins
import os
from pathlib import Path
import shutil
import sqlite3
from time import perf_counter
import pytest
from covearchive.library import model as m, transfer as t
from library_fixtures import git_sync_document, native_document, encoded, GIT_SYNC_LITERAL
from test_ui_library import isolated_data


def test_supplied_git_sync_schema():
    plan = t.build_plan(encoded(GIT_SYNC_LITERAL), m.Library())
    assert plan.valid and (plan.entries, plan.to_add, plan.new_collections, plan.retention_notices) == (1, 1, 4, 1)
    lib = plan.apply(m.Library())
    assert lib.repositories[0].identity == "github:fictional/tool" and "Empty/Collection" in lib.collection_paths.values()
    assert m.load_library(m.dump_library(lib)) == lib and b"SECRET" not in t.export_library(lib)


@pytest.mark.parametrize("scope,key,value", [("top", "format", "wrong"), ("missing", "format", None), ("legacy", "repos", None),
    ("top", "version", 2), ("missing", "repos", None), ("top", "repos", {}), ("entry_missing", "repository", None),
    ("entry", "repository", ""), ("entry", "repository", "https://github.com/o/r"), ("entry", "repository", "o/r?SECRET"),
    ("entry", "repository", "o/r#SECRET"), ("entry", "repository", "o/with space"), ("entry", "repository", "o/\\r"),
    ("entry", "site", "https://SECRET:pw@github.com"), ("entry", "site", "ftp://github.com"),
    ("entry", "site", "github.com/path"), ("entry", "site", "github.com:99999"), ("entry", "site", "git\thub.com"),
    ("entry", "repository", False), ("entry", "repository", "o/bad\x00")])
def test_real_schema_negative_edges(scope, key, value):
    doc = json.loads(encoded(GIT_SYNC_LITERAL)); target = doc["repos"][0] if scope.startswith("entry") else doc
    if scope == "legacy": doc["repositories"] = doc.pop("repos")
    elif scope.endswith("missing"): target.pop(key)
    else: target[key] = value
    lib = m.Library(); plan = t.build_plan(encoded(doc), lib)
    assert not plan.valid and plan.invalid and "SECRET" not in repr(plan)
    if scope == "legacy": assert plan.code == "schema"
    with pytest.raises(m.LibraryError): plan.apply(lib)
    assert not lib.repositories and not lib.collections


@pytest.mark.parametrize("api,site,provider", [("github", "", "github"), ("", "www.github.com", "github"),
    ("", "gitlab.com", "gitlab"), ("github", "enterprise.example", "other"), ("forgejo", "https://Forge:3000", "other"),
    ("gitea", "http://Forge:3000", "gitea"), ("gitlab", "gitlab.com", "gitlab"), (" GitHub ", "HTTPS://WWW.GITHUB.COM", "github"),
    ("github", None, "github"), (None, "github.com", "github"), (None, "gitlab.com", "gitlab"),
    (None, "forge.example", "other"), ("GitHub API", "github.com", "github"), ("a" * 33, "github.com", "github"),
    ("github", 42, "github"), (42, "github.com", "github"), ("github", "github.example.com", "other")])
def test_real_schema_provider_resolution(api, site, provider):
    doc = json.loads(encoded(GIT_SYNC_LITERAL)); doc["repos"][0].update(api=api, site=site, repository=" /Group/Repo.git/ ")
    plan = t.build_plan(encoded(doc), m.Library()); assert plan.valid
    lib = plan.apply(m.Library()); repo = lib.repositories[0]
    assert repo.provider == provider and repo.active == (provider == "github") and repo.label.endswith("Group/Repo")
    if site == "http://Forge:3000": assert repo.url == "http://forge:3000/Group/Repo"
    if site == "forge.example": assert repo.url == "https://forge.example/Group/Repo"
    assert m.load_library(m.dump_library(lib)) == lib and "SECRET" not in repr(plan)


@pytest.mark.parametrize("api,site", [("gitlab", None), ("gitea", 42), ("forgejo", ""), (None, None), (42, 42)])
def test_real_schema_requires_a_usable_host(api, site):
    doc = json.loads(encoded(GIT_SYNC_LITERAL)); doc["repos"][0].update(api=api, site=site)
    plan = t.build_plan(encoded(doc), m.Library())
    assert not plan.valid and plan.invalid == ((1, "url"),) and plan.code is None


@pytest.mark.parametrize("filters,count", [(["Empty / Collection", {}, None, 1, "bad//path", "x/" * 10, "bad\x00"], 4), ({"SECRET": 1}, 2), (None, 2)])
def test_real_schema_filters_are_tolerant(filters, count):
    doc = json.loads(encoded(GIT_SYNC_LITERAL)); doc.update(filters=filters, exported={"SECRET": True}, credentials="SECRET")
    plan = t.build_plan(encoded(doc), m.Library()); assert plan.valid and plan.new_collections == count
    assert b"SECRET" not in t.export_library(plan.apply(m.Library())) and "SECRET" not in repr(plan)


def test_export_native_schema():
    assert json.loads(t.export_library(m.Library())) == {
        "format": "covearchive-repository-list", "version": 1, "collections": [], "repositories": []}


def test_native_roundtrip_empty_collections_determinism_and_second_import():
    plan = t.build_plan(encoded(native_document(4)), m.Library())
    assert plan.valid and plan.kind == "native" and plan.to_add == 4 and plan.new_collections == 3
    lib = plan.apply(m.Library()); before = m.dump_library(lib)
    exported = t.export_library(lib)
    data = json.loads(exported)
    assert data["collections"] == ["Games", "Games/Empty", "Utilities"]
    assert all(set(r) == {"url", "provider", "name", "collection", "folder"} for r in data["repositories"])
    again = t.build_plan(exported, m.Library()).apply(m.Library())
    assert t.export_library(again) == exported
    second = t.build_plan(exported, lib)
    assert second.valid and second.to_add == 0 and second.duplicates == 4 and second.new_collections == 0
    assert second.apply(lib) == lib and m.dump_library(lib) == before
    reversed_lib = m.Library(tuple(reversed(lib.collections)), tuple(reversed(lib.repositories)))
    assert t.export_library(reversed_lib) == exported


def test_git_sync_mapping_secrets_retention_inactive_and_first_wins():
    doc = git_sync_document()
    plan = t.build_plan(b"\xef\xbb\xbf" + encoded(doc), m.Library())
    assert plan.valid and plan.kind == "git-sync" and plan.entries == 500
    assert plan.to_add + plan.duplicates == 500 and plan.duplicates > 0 and plan.inactive > 0
    assert plan.retention_notices == sum(r["keepPreviousVersions"] is False for r in doc["repos"])
    lib = plan.apply(m.Library())
    assert {r.provider for r in lib.repositories} == {"github", "gitlab", "gitea", "other"}
    assert any(lib.collection_path(r.collection) == "Emulators/Nintendo/Switch" for r in lib.repositories)
    assert any(r.collection is None for r in lib.repositories)
    assert all(r.folder is None or r.folder.startswith("inert-folder-") for r in lib.repositories)
    assert b"SECRET" not in t.export_library(lib) and "SECRET" not in repr(plan)
    native = t.export_library(lib)
    assert t.export_library(t.build_plan(native, m.Library()).apply(m.Library())) == native
    assert not any(word in repr(plan) for word in ("password", "Fictional", "inert-folder"))
    repeat = t.build_plan(encoded(doc), lib)
    assert repeat.valid and repeat.to_add == 0 and repeat.duplicates == 500 and repeat.apply(lib) == lib
    doc = {"format": "Git-Sync data", "version": 1, "repos": [
        {"api": "github", "repository": "O/R", "name": "First", "filter": "Café"},
        {"api": "github", "repository": "o/r.git", "name": "Second", "filter": "Other"},
        {"api": "github", "repository": "o/s", "filter": "CAFE\u0301/Child"}]}
    lib = t.build_plan(encoded(doc), m.Library()).apply(m.Library())
    assert len(lib.collections) == 2 and lib.repositories[0].name == "First"
    assert lib.collection_path(lib.repositories[1].collection) == "Café/Child"
    existing = m.Library().add_repository("https://github.com/o/r", name="Local")
    applied = t.build_plan(encoded(doc), existing).apply(existing)
    assert applied.repositories[0].name == "Local" and applied.repositories[0].collection is None


@pytest.mark.parametrize("change", ["format", "neither", "newer", "bool", "version_type", "entries_type",
    "entry_type", "name", "folder", "filter_type", "filter_empty", "depth", "bidi", "userinfo", "provider",
    "unknown_host", "native_unknown", "native_entry_unknown", "native_collection_type", "entry_cap", "collection_cap"])
def test_invalid_inputs_are_safe_and_all_or_nothing(change):
    doc = {"format": "Git-Sync data", "version": 1, "repos": [{"api": "github", "repository": "o/good"}, {"api": "github", "repository": "o/bad"}]}
    bad = doc["repos"][1]
    if change == "format": doc["format"] = "wrong"
    if change == "neither": del doc["repos"]
    if change == "newer": doc["version"] = 2
    if change == "bool": doc["version"] = True
    if change == "version_type": doc["version"] = "1"
    if change == "entries_type": doc["repos"] = {}
    if change == "entry_type": doc["repos"][1] = None
    if change == "name": bad["name"] = True
    if change == "folder": bad["folder"] = "x" * 513
    if change == "filter_type": bad["filter"] = ["a"]
    if change == "filter_empty": bad["filter"] = "a//b"
    if change == "depth": bad["filter"] = "/".join(["a"] * 9)
    if change == "bidi": bad["filter"] = "bad\u202e"
    if change == "userinfo": bad["site"] = "https://SECRET:pw@github.com"
    if change == "provider": bad["api"] = False
    if change == "unknown_host": bad.update(api="", site="forge.example")
    if change.startswith("native_") or change == "collection_cap":
        doc = native_document(1)
        if change == "native_unknown": doc["SECRET"] = "SECRET"
        if change == "native_entry_unknown": doc["repositories"][0]["token"] = "SECRET"
        if change == "native_collection_type": doc["collections"] = [None]
        if change == "collection_cap": doc["collections"] = [str(i) for i in range(2001)]
    if change == "entry_cap": doc["repos"] = [bad] * 10001
    lib = m.Library().add_repository("https://github.com/o/existing"); before = m.dump_library(lib)
    plan = t.build_plan(encoded(doc), lib)
    if change == "unknown_host":
        assert plan.valid and plan.apply(lib).repositories[-1].provider == "other" and m.dump_library(lib) == before
        return
    assert not plan.valid and plan.invalid and "SECRET" not in repr(plan)
    with pytest.raises(m.LibraryError): plan.apply(lib)
    assert m.dump_library(lib) == before
    if change == "newer": assert plan.code == "newer_version"


@pytest.mark.parametrize("raw,code", [(b"\xff", "json"), (b"broken", "json"),
    (encoded(git_sync_document(1)).replace(b'"version": 1', b'"version":1,"version":1'), "duplicate_key"),
    (encoded(git_sync_document(1)).replace(b'"repository":', b'"repository":"o/first","repository":', 1), "duplicate_key"),
    (encoded(native_document(1)).replace(b'"version": 1', b'"version":1,"version":1'), "duplicate_key"),
    (encoded(native_document(1)).replace(b'"url":', b'"url":"https://github.com/o/first","url":', 1), "duplicate_key"),
    (b'{"version":NaN}', "nonfinite"), (b'{"version":Infinity}', "nonfinite"), (b"[" * 10000 + b"]" * 10000, "json"),
    (encoded(native_document(0)).ljust(8 * 1024 * 1024 + 1), "size"), (b"true", "schema")],
    ids=["utf8", "syntax", "sync_duplicate", "sync_nested", "native_duplicate", "native_nested", "nan", "infinity", "depth_bomb", "oversized", "root_type"])
def test_bad_json_is_a_plan_not_an_exception(raw, code):
    lib = m.Library(); plan = t.build_plan(raw, lib)
    assert not plan.valid and plan.code == code
    with pytest.raises(m.LibraryError): plan.apply(lib)
    assert lib == m.Library()


def test_invalid_list_bounded_and_result_caps():
    plan = t.build_plan(encoded({"format": "Git-Sync data", "version": 1, "repos": [{"api": "github", "repository": "SECRET"}] * 70}), m.Library())
    assert not plan.valid and len(plan.invalid) == 50 and plan.invalid[0][0] == 1
    full = m.Library(collections=tuple(m.Collection(str(i), str(i)) for i in range(2000)))
    assert not t.build_plan(encoded(native_document(1)), full).valid


def test_realistic_invalid_import_and_apply_against_changed_library():
    lib = m.Library(); before = m.dump_library(lib)
    plan = t.build_plan(encoded(git_sync_document(invalid=True)), lib)
    assert not plan.valid and plan.entries == 500 and "SECRET" not in repr(plan)
    with pytest.raises(m.LibraryError): plan.apply(lib)
    assert m.dump_library(lib) == before
    plan = t.build_plan(encoded(native_document(1)), lib)
    current = lib.add_repository("https://github.com/Fictional/Repo0", name="Local")
    assert plan.apply(current).repositories[0].name == "Local"
    assert not lib.repositories and len(current.repositories) == 1
    full = m.Library(repositories=tuple(m.Repository("github", f"https://github.com/full/r{i}") for i in range(10000)))
    assert not t.build_plan(encoded(native_document(1)), full).valid


def test_import_export_never_mutate_files_or_preferences(monkeypatch, tmp_path):
    prefs = tmp_path / "preferences.json"; prefs.write_bytes(b"SMART_BUILD_RULES")
    folder = tmp_path / "archive"; folder.mkdir(); (folder / "old.zip").write_bytes(b"ARCHIVE")
    queue = tmp_path / "queue.db"
    with sqlite3.connect(queue) as db:
        db.execute("CREATE TABLE jobs (id INTEGER, status TEXT)")
        db.execute("INSERT INTO jobs VALUES (1, 'queued')")
    db.close()
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    doc = git_sync_document(); doc["repos"][0]["folder"] = str(folder)
    def forbidden(*args, **kwargs): pytest.fail("Import/export attempted filesystem access")
    with monkeypatch.context() as patch:
        for obj, names in [(builtins, ["open"]), (Path, ["open", "mkdir", "unlink", "rename", "replace", "rmdir"]),
            (os, ["open", "mkdir", "makedirs", "remove", "unlink", "rename", "replace", "rmdir"]),
            (shutil, ["rmtree", "move", "copy", "copyfile", "copytree"])]:
            for name in names: patch.setattr(obj, name, forbidden)
        lib = m.Library(); plan = t.build_plan(encoded(doc), lib)
        assert plan.valid and plan.retention_notices > 0
        assert t.export_library(plan.apply(lib)) and not lib.repositories
    assert {p: p.read_bytes() for p in before} == before
    with sqlite3.connect(queue) as db:
        assert db.execute("SELECT * FROM jobs").fetchall() == [(1, "queued")]
    db.close()


def test_read_import_file_regular_bounded_safe_errors(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.json"; path.write_bytes(b"{}")
    assert t.read_import_file(path) == b"{}"
    for target in (tmp_path, tmp_path / "SECRET_missing"):
        with pytest.raises(m.LibraryError) as exc: t.read_import_file(target)
        assert "SECRET" not in str(exc.value)
    path.write_bytes(encoded(native_document(0)).ljust(8 * 1024 * 1024 + 1))
    with pytest.raises(m.LibraryError) as exc: t.read_import_file(path)
    assert exc.value.code == "size"
    original = os.fdopen; reads = []
    class Reader:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def fileno(self): return self.stream.fileno()
        def read(self, count): reads.append(count); return b"{}"
    monkeypatch.setattr(os, "fdopen", lambda *args: Reader(original(*args)))
    assert t.read_import_file(path) == b"{}" and reads == [8 * 1024 * 1024 + 1]


@pytest.mark.parametrize("n", [500, 5000, 10000])
def test_synthetic_performance(n):
    raw = encoded(git_sync_document(n)); lib = m.Library()
    start = perf_counter(); plan = t.build_plan(raw, lib); preview_ms = (perf_counter() - start) * 1000
    assert plan.valid
    start = perf_counter(); result = plan.apply(lib); apply_ms = (perf_counter() - start) * 1000
    print(f"synthetic {n}: build_plan={preview_ms:.3f} ms, apply={apply_ms:.3f} ms")
    assert plan.valid and len(result.repositories) == plan.to_add and not lib.repositories
    assert preview_ms < 5000 and apply_ms < 5000


@pytest.mark.parametrize("value", ["", "  \t "])
def test_git_sync_blank_optional_text_is_none(value):
    doc = {"format": "Git-Sync data", "version": 1, "repos": [{"api": "github", "repository": "o/r", "name": value, "folder": value, "filter": value}]}
    plan = t.build_plan(encoded(doc), m.Library())
    assert plan.valid
    repo = plan.apply(m.Library()).repositories[0]
    assert repo.name is None and repo.folder is None and repo.collection is None


@pytest.mark.parametrize("value", [False, True, None, 0, 1, "false", [], {}])
def test_retention_only_literal_false_counts(value):
    raw = encoded({"format": "Git-Sync data", "version": 1, "repos": [{"api": "github", "repository": "o/r", "keepPreviousVersions": value}]})
    plan = t.build_plan(raw, m.Library())
    assert plan.valid and plan.retention_notices == int(value is False)


def test_slash_friendly_name_roundtrip_and_native_blank_name_rejected():
    lib = m.Library().add_repository("https://github.com/o/r", name="Dolphin / Nightly")
    assert t.build_plan(t.export_library(lib), m.Library()).apply(m.Library()) == lib
    doc = native_document(1); doc["repositories"][0]["name"] = "  "
    assert not t.build_plan(encoded(doc), m.Library()).valid
