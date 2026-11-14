"""P01-P15: synthetic incremental scenarios A-N and execution boundaries."""

import ast
import builtins
from dataclasses import FrozenInstanceError, replace
from itertools import permutations
import os
from pathlib import Path, PureWindowsPath
import socket
import subprocess
import sys

import pytest

from covearchive.archive.manifest import AssetRecord, ProjectIndex, ReleaseRecord
from covearchive.archive import manifest as m
from covearchive.archive.naming import PathBudgetError, filename_limit
from covearchive.archive.plan import PlanError, RetainedEntry, SkippedAsset, plan_archive
from covearchive.github.models import ReleaseCatalog, parse_release
from covearchive.github.select import ExcludedRelease
from covearchive.github.urls import RepositoryTarget


ROOT = PureWindowsPath(r"Q:\ca-s4-nonexistent-archive")
REPO = RepositoryTarget("Owner", "Repo")
HASH = "a" * 64


def release(i=1, *, assets=((1, "a.zip", "uploaded"),), **changes):
    data = dict(id=i, tag_name=f"v{i}", draft=False, prerelease=False,
        created_at="2026-11-13T12:00:00Z", assets=[dict(id=j, name=name, state=state,
        size=0, content_type="opaque", browser_download_url="https://example/asset",
        created_at="2026-11-13", updated_at="2026-11-13") for j, name, state in assets])
    data.update(changes)
    return parse_release(data)


def known(status="planned", **changes):
    data = dict(asset_id=1, original_name="a.zip", saved_name="persisted.zip", size=0,
                publisher_sha256=None, local_sha256=HASH if status == "completed" else None, status=status)
    data.update(changes)
    return ProjectIndex(REPO, (ReleaseRecord(1, "v1", "release-v1--1", (AssetRecord(**data),)),))


def run(source=(), **options):
    arguments = dict(archive_root=ROOT, repository=REPO)
    arguments.update(options)
    return plan_archive(source, **arguments)


def rejected(call, code, error=PlanError):
    try:
        call()
    except error as exc:
        assert exc.code in (code if isinstance(code, tuple) else (code,))
    else:
        assert False, f"expected {error.__name__}: {code}"


def test_P01_new_project_paths_and_publisher_metadata():
    source = release()
    source = replace(source, assets=(replace(source.assets[0], sha256=HASH),))
    plan = run([source])
    item = plan.releases[0].assets[0]
    assert plan.repository == REPO and plan.project_dir == PureWindowsPath("Owner/Repo")
    assert not plan.index_present and item.state == "new" and item.action == "download"
    assert item.record == AssetRecord(1, "a.zip", "a.zip", 0, HASH, None, "planned")
    assert item.extension == ".zip" and item.path == PureWindowsPath("Owner/Repo/release-v1--1/a.zip")
    assert plan.index.releases == (ReleaseRecord(1, "v1", "release-v1--1", (item.record,)),)


@pytest.mark.parametrize("status,action", [("planned", "download"), ("completed", "verify")])
def test_P02_rescan_same_id_preserves_record_and_requests_bytes_check(status, action):
    index = known(status)
    plan = run([release()], index=index)
    item = plan.releases[0].assets[0]
    assert item.action == action and item.state == "recorded"
    assert item.record == index.releases[0].assets[0] and item.record.saved_name == "persisted.zip"
    assert plan.index == index and plan.index_present and item.notes == ()


@pytest.mark.parametrize("new_release", [False, True])
def test_P03_new_asset_in_known_release_or_new_release_is_appended(new_release):
    index = known()
    source = [release(), release(2, assets=((2, "b.zip", "uploaded"),))] if new_release else [
        release(assets=((1, "a.zip", "uploaded"), (2, "b.zip", "uploaded")))]
    plan = run(source, index=index)
    proposed = [a for r in plan.index.releases for a in r.assets]
    assert proposed == [index.releases[0].assets[0], AssetRecord(2, "b.zip", "b.zip", 0, None, None, "planned")]
    assert plan.releases[-1].assets[-1].state == "new" and plan.releases[-1].assets[-1].action == "download"


@pytest.mark.parametrize("selected_old", [False, True])
@pytest.mark.parametrize("new_name,saved_name", [("A.zip", "A--2.zip"), ("a.ZIP", "a--2.ZIP")])
def test_P04_colliding_new_id_reserves_all_persisted_names(selected_old, new_name, saved_name):
    index = known(saved_name="a.zip")
    assets = ((2, new_name, "uploaded"),) + (((1, "a.zip", "uploaded"),) if selected_old else ())
    plan = run([release(assets=assets)], index=index)
    item = next(a for a in plan.releases[0].assets if a.asset.id == 2)
    assert item.record.saved_name == saved_name and item.notes == ("name_collision",)
    assert plan.index.releases[0].assets[0] == index.releases[0].assets[0]


@pytest.mark.parametrize("kind,asset_id,reason", [("removed_release", None, "release_removed_remotely"),
    ("draft", None, "release_excluded_draft"), ("prerelease", None, "release_excluded_prerelease"),
    ("removed_asset", 1, "asset_removed_remotely"), ("unavailable", 1, "asset_unavailable"), ("filtered", 1, "asset_filtered")])
def test_P05_remote_removal_and_filter_changes_retain_records(kind, asset_id, reason):
    source = [] if kind == "removed_release" else [release(assets=() if kind == "removed_asset" else
        ((1, "a.zip", "new" if kind == "unavailable" else "uploaded"),),
        draft=kind == "draft", prerelease=kind == "prerelease")]
    index = known("completed")
    plan = run(source, index=index, enabled_extensions=[] if kind == "filtered" else None)
    assert plan.index == index and plan.retained == (RetainedEntry(1, asset_id, reason),)


@pytest.mark.parametrize("old_digest,changes,notes,action", [(None, {"name": "renamed.zip"}, ("name_changed",), "reconcile"),
    (None, {"size": 1}, ("size_changed",), "reconcile"), (HASH, {"sha256": "b" * 64}, ("digest_changed",), "reconcile"),
    (HASH, {"sha256": None}, ("digest_changed",), "reconcile"), (None, {"sha256": HASH}, ("digest_added",), "verify"),
    (None, {"size": 1, "name": "changed.zip", "sha256": HASH}, ("digest_added", "name_changed", "size_changed"), "reconcile")])
def test_P06_metadata_changes_reconcile_without_refreshing_record(old_digest, changes, notes, action):
    index = known("completed", publisher_sha256=old_digest)
    source = release()
    source = replace(source, assets=(replace(source.assets[0], **changes),))
    item = run([source], index=index).releases[0].assets[0]
    assert item.action == action and item.notes == notes and item.record == index.releases[0].assets[0]


def test_P07_tag_change_keeps_exact_recorded_folder_and_tag():
    index = known()
    plan = run([release(tag_name="changed/tag")], index=index)
    assert len(plan.releases) == 1 and plan.releases[0].folder == "release-v1--1"
    assert plan.releases[0].notes == ("tag_changed",) and plan.releases[0].state == "known"
    assert plan.index == index


@pytest.mark.parametrize("assets,reason", [((), "no_assets"), (((1, "a.exe", "uploaded"),), "no_matching_assets")])
def test_P08_no_index_and_empty_release_do_not_invent_registry(assets, reason):
    plan = run([release(assets=assets)], index=None)
    assert not plan.index_present and plan.releases[0].assets == ()
    assert plan.releases[0].empty_reason == reason and plan.index.releases == ()


def test_P09_shuffled_releases_assets_and_root_independent_folders():
    assets = ((2, "a.zip", "uploaded"), (1, "A.zip", "uploaded"))
    expected = run([release(assets=assets), release(2, assets=((3, "c.zip", "uploaded"),))])
    for asset_order in permutations(assets):
        for source in permutations((release(assets=asset_order), release(2, assets=((3, "c.zip", "uploaded"),)))):
            assert run(source) == expected
    assert run([release()], archive_root=PureWindowsPath(r"R:\elsewhere")).releases == run([release()]).releases


@pytest.mark.parametrize("case,code", [("incomplete", "incomplete_catalog"), ("missing_repo", "repository"),
    ("catalog_repo", "repository"), ("index_repo", "repository"), ("release_conflict", "duplicate_conflict"),
    ("asset_conflict", ("duplicate_conflict", "asset_conflict")), ("cross_release", "asset_conflict"), ("recorded_moved", "asset_conflict")])
def test_P10_reject_incomplete_identity_and_conflicts_before_selection(case, code):
    catalog = ReleaseCatalog(REPO, (release(),), 1, 1, 0, (), "/repos/Owner/Repo/releases", case != "incomplete")
    source, options = [release()], {}
    if case in ("incomplete", "catalog_repo"):
        source = catalog
    if case in ("missing_repo", "catalog_repo"):
        options["repository"] = None if case == "missing_repo" else RepositoryTarget("other", "repo")
    if case == "index_repo":
        options["index"] = ProjectIndex(RepositoryTarget("other", "repo"), ())
    if case == "release_conflict":
        source = [release(draft=True), release(tag_name="different", draft=True)]
    if case == "asset_conflict":
        source = [release(assets=((1, "a.zip", "uploaded"), (1, "b.zip", "uploaded")), draft=True)]
    if case in ("cross_release", "recorded_moved"):
        source = [release(), release(2)] if case == "cross_release" else [release(2)]
        options.update(index=known() if case == "recorded_moved" else None)
    rejected(lambda: run(source, **options), code)


def test_P10_complete_catalog_iterable_and_exact_duplicates_are_tolerated():
    source = release(assets=((1, "a.zip", "uploaded"), (1, "a.zip", "uploaded")))
    catalog = ReleaseCatalog(REPO, (source,), 1, 1, 0, (), "/repos/Owner/Repo/releases", True)
    plan = run([source])
    assert run(catalog) == run(iter([source, source])) == plan
    assert len(plan.releases) == len(plan.releases[0].assets) == 1


def test_P10_index_spelling_is_the_established_project_identity():
    plan = run([release()], repository=RepositoryTarget("owner", "repo"), index=known())
    assert plan.repository == REPO and plan.project_dir == PureWindowsPath("Owner/Repo")
    assert plan.releases[0].assets[0].path.parts[:2] == ("Owner", "Repo")


@pytest.mark.parametrize("root,code", [(PureWindowsPath("Q:\\" + "x" * 200), "budget"),
    (PureWindowsPath("relative"), "root"), (PureWindowsPath(r"Q:\a\..\b"), "root"), (PureWindowsPath(r"\\?\Q:\a"), "root")])
@pytest.mark.parametrize("recorded", [False, True])
def test_P11_path_budget_and_invalid_roots_for_new_and_recorded(root, code, recorded):
    rejected(lambda: run([release()], archive_root=root, index=known() if recorded else None), code, PathBudgetError)


def test_P12_skip_reason_precedence_and_prerelease_selection():
    source = release(assets=((1, "a.exe", "new"), (2, "b.exe", "uploaded"), (3, "c.tar", "uploaded"), (4, "d.zip", "uploaded")))
    plan = run([source], enabled_extensions=[".zip"])
    assert plan.releases[0].skipped == (SkippedAsset(1, "a.exe", "unavailable"), SkippedAsset(2, "b.exe", "unsupported_format"), SkippedAsset(3, "c.tar", "format_disabled"))
    assert run([release(prerelease=True)]).releases == ()
    assert len(run([release(prerelease=True)], include_prereleases=True).releases) == 1


@pytest.mark.parametrize("recorded", [False, True])
def test_P13_planning_has_no_network_or_filesystem_access(monkeypatch, recorded):
    def deny(*args, **kwargs):
        assert False, "planning attempted network or filesystem access"
    source = [release()]
    with monkeypatch.context() as patch:
        for obj, names in ((socket, ("socket", "create_connection", "getaddrinfo")), (builtins, ("open",)),
                           (os, ("mkdir",)), (Path, ("write_text", "write_bytes", "mkdir", "open", "exists", "resolve", "stat"))):
            for name in names:
                patch.setattr(obj, name, deny)
        plan = run(source, index=known("completed") if recorded else None)
    assert len(plan.releases[0].assets) == 1


def test_P14_subprocess_import_isolation_and_archive_import_allowlist():
    src = Path(__file__).resolve().parents[1] / "src"
    code = "import sys; sys.path.insert(0, sys.argv[1]); sys.modules['requests']=None; sys.modules['PySide6']=None; "
    code += "import covearchive.archive, covearchive.archive.naming, covearchive.archive.manifest, covearchive.archive.plan; "
    code += "assert all(not any(k == p or k.startswith(p + '.') for k,v in sys.modules.items() if v is not None) for p in ('requests','PySide6','aria2','sqlite3','socket','_socket'))"
    child = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(src)], capture_output=True, text=True)
    assert child.returncode == 0, child.stdout + child.stderr
    for path in (src / "covearchive" / "archive").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node, ast.ImportFrom) and not node.level else [])
            assert all(name.split(".")[0] in sys.stdlib_module_names | {"covearchive"} for name in names)


def test_P15_frozen_tuple_outputs_and_permitted_actions():
    plan = run([release()])
    r, a = plan.releases[0], plan.releases[0].assets[0]
    assert all(isinstance(value, tuple) for value in (plan.releases, plan.retained, plan.excluded, r.assets, r.skipped, r.notes, a.notes))
    for obj, field in ((plan, "releases"), (r, "folder"), (a, "action"), (SkippedAsset(1, "a", "unavailable"), "reason"), (RetainedEntry(1, None, "release_removed_remotely"), "reason")):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, None)
    assert {item.action for rel in plan.releases for item in rel.assets} <= {"download", "verify", "reconcile"}
    assert all(item.record.status == "planned" and item.record.local_sha256 is None for rel in plan.releases for item in rel.assets)


@pytest.mark.parametrize("case", ["release", "empty", "asset", "tag", "name", "size", "record_error"])
def test_R13_unrepresentable_is_typed(monkeypatch, case):
    source = release(id=10**5000) if case == "release" else release(id=10**5000, assets=()) if case == "empty" else release()
    if case in ("asset", "name"):
        source = release(assets=((10**5000 if case == "asset" else 1, "a.zip" if case == "asset" else "x" * 1021 + ".zip", "uploaded"),))
    if case == "tag": source = release(tag_name="x" * 1025)
    if case == "size": source = replace(source, assets=(replace(source.assets[0], size=2**53),))
    if case == "record_error": monkeypatch.setattr(sys.modules[ProjectIndex.__module__], "MAX_INDEX_BYTES", 1)
    with pytest.raises(Exception) as caught: run([source])
    assert type(caught.value) is PlanError and caught.value.code == "unrepresentable"
    if case == "record_error": assert caught.value.__cause__ is not None


def test_R13_unselected_values_do_not_block():
    assert run([release(id=10**5000, prerelease=True)]).releases == ()
    assert run([release(assets=((10**5000, "x" * 1025, "new"),))]).releases[0].assets == ()
    assert run([release(tag_name="x" * 1024)]).index.releases[0].tag == "x" * 1024


@pytest.mark.parametrize("root", ["relative", r"\\?\Q:\archive"])
def test_R14_empty_root_validation(root):
    catalog = ReleaseCatalog(REPO, (), 1, 1, 0, (), "/repos/Owner/Repo/releases", True)
    rejected(lambda: run(catalog, archive_root=root), "root", PathBudgetError)


def test_R14_empty_valid_root():
    assert run([]).releases == ()


@pytest.mark.parametrize("changes,action", [({"sha256": HASH}, "download"), ({"name": "new.zip"}, "reconcile"), ({"size": 1}, "reconcile")])
def test_R17_planned_metadata(changes, action):
    index, source = known(), release()
    source = replace(source, assets=(replace(source.assets[0], **changes),))
    item = run([source], index=index).releases[0].assets[0]
    assert item.action == action and item.record == index.releases[0].assets[0]


@pytest.mark.parametrize("status", ["planned", "completed"])
def test_R21_recorded_name_under_deeper_root(status):
    index = known(status, saved_name="x" * 100 + ".zip")
    root = "Q:\\" + "x" * 107
    item = run([release()], archive_root=root, index=index).releases[0].assets[0]
    assert item.state == "recorded" and item.record is index.releases[0].assets[0]
    assert len(str(item.path)) + len(root) + 1 == 240
    assert filename_limit(root + "x", "Owner", "Repo", "release-v1--1") == 103
    rejected(lambda: run([release()], archive_root=root + "x", index=index), "budget", PathBudgetError)


@pytest.mark.parametrize("draft,pre,include,expected", [(True, False, False, (ExcludedRelease(1, "v1", "draft"),)),
    (False, True, False, (ExcludedRelease(1, "v1", "prerelease"),)), (False, True, True, ())])
def test_R22_excluded_releases(draft, pre, include, expected):
    assert run([release(draft=draft, prerelease=pre)], include_prereleases=include).excluded == expected


@pytest.mark.parametrize("index", [None, known()])
def test_R3_remote_pair_is_unrepresentable(index):
    source = release()
    source = replace(source, assets=(replace(source.assets[0], name="a\ud83d\ude00.zip"),))
    try: run([source], index=index)
    except Exception as exc: assert type(exc) is PlanError and exc.code == "unrepresentable"
    else: assert False, "an adjacent surrogate pair must block the plan"


def r4_verify(plan, index=None):
    assert m.loads_index(m.dumps_index(plan.index)) == plan.index
    records = {r.release_id: r for r in plan.index.releases}
    for selected in plan.releases:
        r = selected.release
        m.ReleaseManifest(plan.repository, r.id, r.tag_name, selected.folder, None, None, False, None, ())
        for a in selected.assets: AssetRecord(a.asset.id, a.asset.name, "a.zip", a.asset.size, a.asset.sha256, None, "planned")
        if r.id in records:
            value = m.build_release_manifest(plan.repository, r, records[r.id])
            assert m.loads_release_manifest(m.dumps_release_manifest(value)) == value
    if index: assert all(a in records[r.release_id].assets for r in index.releases for a in r.assets)


def test_R4_thousand_cjk_assets_roundtrip():
    source = release(assets=tuple((i, "\u4e00" * 247 + f"{i:04}.zip", "uploaded") for i in range(1, 1001)))
    plan = run([source])
    try: r4_verify(plan)
    except m.ManifestError: assert False, "a legal 1000-asset release must have a writable manifest"
    assert len(plan.releases[0].assets) == 1000


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("field,value", [(f, v) for f in ("id", "asset.id") for v in (0, 2**63, True, 1.0)] +
    [(f, v) for f in ("tag_name", "name", "asset.name") for v in ("\ud83d\ude00", "x" * 1025)] +
    [("asset.size", v) for v in (m.MAX_SIZE + 1, -1, "1")] + [("published_at", "not a date"), ("asset.sha256", "A" * 64)])
def test_R4_hostile_oracle(indexed, field, value):
    source, index = release(), known() if indexed else None
    asset_field = field.startswith("asset.")
    if asset_field:
        key = field.split(".")[1]
        source = replace(source, assets=(replace(source.assets[0], **{key: "a" + value + ".zip" if key == "name" else value}),))
    else: source = replace(source, **{field: value})
    if indexed and field == "id" and value != 1: source = replace(source, assets=())
    for mode in ("selected", "zero", "draft", "prerelease") + (("unsupported", "unavailable") if asset_field else ()):
        current = replace(source, draft=mode == "draft", prerelease=mode == "prerelease")
        if mode == "zero": current = replace(current, assets=())
        if mode in ("unsupported", "unavailable"):
            a = current.assets[0]
            a = replace(a, name=a.name.removesuffix(".zip") + ".exe", supported_extension=None) if mode == "unsupported" else replace(a, state="new")
            current = replace(current, assets=(a,))
        ignored = mode in ("draft", "prerelease", "unsupported", "unavailable")
        try: plan = run([current], index=index)
        except Exception as exc: assert not ignored and type(exc) is PlanError and exc.code == "unrepresentable"
        else:
            try: r4_verify(plan, index)
            except m.ManifestError: assert False, "returned plan contains unrepresentable remote metadata"
