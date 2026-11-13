"""Pure release selection, using parsed publisher metadata."""

from dataclasses import FrozenInstanceError
import socket

import pytest

from covearchive.formats import SUPPORTED_EXTENSIONS
from covearchive.github.models import ReleaseCatalog, parse_release
from covearchive.github.select import select_releases, select_release_by_tag
from covearchive.github.urls import RepositoryTarget


def release(id=1, *, assets=(), **changes):
    data = dict(id=id, tag_name=f"v{id}", draft=False, prerelease=False,
                created_at="2026-11-13T12:00:00Z", assets=[
        dict(id=i, name=name, state=state, size=0, content_type="opaque",
             browser_download_url="https://example/asset", created_at="2026-11-13",
             updated_at="2026-11-13") for i, name, state in assets])
    data.update(changes)
    return parse_release(data)


def test_empty_selection_and_default_extensions():
    result = select_releases([])
    assert result.releases == () and result.excluded == ()
    assert result.include_prereleases is False
    assert result.enabled_extensions == SUPPORTED_EXTENSIONS


def test_enabled_extensions_are_canonical_and_unique():
    result = select_releases([], enabled_extensions=[".zip", ".zip", ".tar"])
    assert result.enabled_extensions == (".tar", ".zip")


@pytest.mark.parametrize("include,expected,excluded", [
    (False, [1], [(2, "draft"), (3, "prerelease")]),
    (True, [1, 3], [(2, "draft")]),
])
def test_draft_and_prerelease_filters(include, expected, excluded):
    source = [release(), release(2, draft=True, prerelease=True), release(3, prerelease=True)]
    result = select_releases(source, include_prereleases=include)
    assert [item.release.id for item in result.releases] == expected
    assert [(item.release_id, item.reason) for item in result.excluded] == excluded
    assert [item.tag_name for item in result.excluded] == [f"v{i}" for i, _ in excluded]
    assert result.include_prereleases is include


def test_all_formats_and_longest_suffix_preserve_asset_order():
    names = ["A.AppImage", "A.TAR.GZ", "A.TAR", "A.DMG", "A.ZIP", "A.7Z"]
    parsed = release(assets=[(i + 1, name, "uploaded") for i, name in enumerate(names)])
    selected = select_releases([parsed]).releases[0]
    assert [match.extension for match in selected.matches] == list(SUPPORTED_EXTENSIONS)
    assert [match.asset.name for match in selected.matches] == names
    assert selected.empty_reason is None and selected.release is parsed


@pytest.mark.parametrize("assets,reason", [
    ([], "no_assets"), ([(1, "a.exe", "uploaded")], "no_matching_assets"),
    ([(1, "a.zip", "new")], "no_matching_assets"),
    ([(1, "a.zip", "Uploaded")], "no_matching_assets"),
])
def test_empty_releases_are_kept(assets, reason):
    parsed = release(assets=assets, tarball_url="tar", zipball_url="zip")
    selected = select_releases([parsed]).releases[0]
    assert selected.matches == () and selected.empty_reason == reason
    assert selected.release.assets == parsed.assets
    assert (selected.release.tarball_url, selected.release.zipball_url) == ("tar", "zip")


def test_duplicate_release_and_asset_ids_keep_first_and_preserve_metadata():
    parsed = release(assets=[(1, "first.exe", "uploaded"), (1, "second.zip", "uploaded"),
                             (2, "third.zip", "uploaded")])
    result = select_releases([parsed, release(1), release(2)])
    assert [item.release.id for item in result.releases] == [1, 2]
    assert result.releases[0].release is parsed
    assert len(parsed.assets) == 3
    assert [match.asset.name for match in result.releases[0].matches] == ["third.zip"]
    assert select_releases([release(draft=True), release()]).releases == ()


def test_catalog_and_one_shot_extensions_and_source():
    releases = (release(assets=[(1, "a.zip", "uploaded"), (2, "a.tar.gz", "uploaded")]),
                release(2, assets=[(3, "b.zip", "uploaded")]))
    catalog = ReleaseCatalog(RepositoryTarget("o", "r"), releases, 1, 1, 0, (),
                             "/repos/o/r/releases", True)
    result = select_releases(catalog, enabled_extensions=iter([".zip"]))
    assert result.enabled_extensions == (".zip",)
    assert [[m.extension for m in r.matches] for r in result.releases] == [[".zip"], [".zip"]]
    assert select_releases(iter(releases)) == select_releases(catalog)
    assert select_releases(releases, enabled_extensions=[]).releases[0].empty_reason == "no_matching_assets"
    assert select_releases(releases, enabled_extensions=[".tar"]).releases[0].matches == ()


@pytest.mark.parametrize("enabled,error", [(".zip", TypeError), (b".zip", TypeError),
    ([".exe"], ValueError), ([None], ValueError), ([".ZIP"], ValueError)])
@pytest.mark.parametrize("selector", [select_releases, select_release_by_tag])
def test_enabled_extensions_validated_even_for_empty_source(enabled, error, selector):
    args = ([], "absent") if selector is select_release_by_tag else ([],)
    with pytest.raises(error):
        selector(*args, enabled_extensions=enabled)


def test_exact_tag_selection_prerelease_defaults_and_missing_tags():
    source = [release(1, draft=True), release(2, prerelease=True, assets=[(1, "a.zip", "uploaded")])]
    assert select_release_by_tag(source, "v1") is None
    assert select_release_by_tag(source, "V2") is None
    assert select_release_by_tag(source, "git-tag-only") is None
    assert select_release_by_tag(source, "v2", include_prereleases=False) is None
    selected = select_release_by_tag(source, "v2", enabled_extensions=iter([".zip"]))
    assert selected.release is source[1] and selected.matches[0].extension == ".zip"
    assert select_release_by_tag([release(), release(2, tag_name="v1")], "v1").release.id == 1


def test_selection_makes_zero_network_calls(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("selection attempted network access")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    assert select_releases([release()]).releases[0].release.id == 1
    assert select_release_by_tag([release()], "v1").release.id == 1


def test_selection_models_are_frozen():
    result = select_releases([release(assets=[(1, "a.zip", "uploaded")]), release(2, draft=True)])
    for obj, field in [(result, "releases"), (result.releases[0], "release"),
                       (result.releases[0].matches[0], "extension"), (result.excluded[0], "reason")]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, None)
