"""N01-N20: independent string-level archive naming oracles."""

from dataclasses import FrozenInstanceError
import hashlib
from itertools import permutations, product
from pathlib import PureWindowsPath
from types import SimpleNamespace
import re
import unicodedata as ud

import pytest


@pytest.fixture(autouse=True)
def windows_path_policy(monkeypatch):
    # These independent string oracles explicitly exercise Windows budgets.
    monkeypatch.setattr(n, "sys", SimpleNamespace(platform="win32"))

from covearchive.archive import naming as n


def units(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def rejected(call, code=None, error=n.NamingError):
    try:
        call()
    except error as exc:
        if code is not None:
            assert exc.code == code
    else:
        assert False, f"expected {error}: {code}"


@pytest.mark.parametrize("name", ["a.zip", "A.TAR.GZ", "a b", ".hidden", "caf\u00e9", "\U0001f600.zip"])
def test_N01_safe_names_byte_identical(name):
    assert n.sanitize_component(name) == name
    assert n.is_safe_component(name)


@pytest.mark.parametrize("name,expected", [(" .. ", "unnamed"), ("", "unnamed"),
    (" a. ", "a"), ('a<>:"/\\|?*b', "a_________b"),
    ("a\x00\u202e\ud800\u2028\u2029\u00a0b", "a______b"), (" \ta b\n", "a b")])
def test_N02_replacement_and_whitespace(name, expected):
    assert n.sanitize_component(name) == expected
    assert not n.is_safe_component(name)


@pytest.mark.parametrize("base", ["CON", "prn", "AUX", "NUL", "CONIN$", "CONOUT$",
    *[f"{p}{i}" for p in ("COM", "LPT") for i in range(10)],
    *[p + i for p in ("COM", "LPT") for i in "\u00b9\u00b2\u00b3"], "\uff23\uff2f\uff2e"])
def test_N03_devices_even_with_extension(base):
    for suffix in ("", ".zip", ".tar.gz"):
        assert n.sanitize_component(base + suffix) == base + "_" + suffix


@pytest.mark.parametrize("ext", [".tar.gz", ".TAR.XZ", ".tar.bz2", ".tar.zst", ".tar.lz", ".tar.z", ".zip", ".ABC123"])
def test_N04_truncation_hashes_original_and_preserves_extension(ext):
    original = " " + "x" * 200 + ext
    h8 = hashlib.sha256(original.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    assert n.sanitize_component(original, limit=48) == "x" * (48 - 10 - len(ext)) + "--" + h8 + ext


def test_N05_hostile_product_all_output_invariants():
    fragments = ("..", "/", "\\", ":", "CON", "nul.txt", "COM\u00b9", "\u202e", "\x00",
                 "\ud800", " ", ".", "x" * 180, "a.tar.gz", "\U0001f600", "\u0378")
    devices = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
        p + i for p in ("com", "lpt") for i in (*map(str, range(10)), "\u00b9", "\u00b2", "\u00b3")}
    for parts in product(fragments, repeat=2):
        original = "".join(parts)
        saved = n.sanitize_component(original)
        assert isinstance(saved, str) and saved and units(saved) <= 120
        assert saved not in (".", "..") and not saved.endswith((".", " "))
        assert all(c not in '<>:"/\\|?*' and ud.category(c) not in {"Cc", "Cf", "Cs", "Zl", "Zp"}
                   and (ud.category(c) != "Zs" or c == " ") for c in saved)
        assert all(form.split(".", 1)[0].rstrip(" ").casefold() not in devices
                   for form in (saved, ud.normalize("NFKC", saved)))
        assert n.sanitize_component(saved) == saved
        assert set(saved) <= set(original) | set("_unnamed-0123456789abcdef")


@pytest.mark.parametrize("left,right", [("A", "a"), ("\u00e9", "e\u0301"), ("\uff21", "a"), ("\u00df", "SS")])
def test_N06_name_key_normalization(left, right):
    assert n.name_key(left) == n.name_key(right) == ud.normalize("NFKC", ud.normalize("NFKC", left).casefold())


def test_N07_project_folders_hash_only_changed_spelling():
    assert n.project_folders("Owner", "Repo") == ("Owner", "Repo")
    for name in ("con", "foo."):
        expected = n.sanitize_component(name) + "+" + hashlib.sha256(name.casefold().encode()).hexdigest()[:12]
        assert n.project_folders("Owner", name) == ("Owner", expected)
    assert n.project_folders("Owner", "foo.") != n.project_folders("Owner", "foo")


def test_R11_project_namespace_disjoint_and_case_stable():
    fragments = ("con", "CON", "nul.txt", "foo.", "foo..", "COM1", "abcdef123456", "foo--0bda519d")
    names = set(fragments) | {"".join(p) for p in product(fragments, repeat=2)}
    unchanged = {name.casefold() for name in names if n.is_safe_component(name)}
    seen = {}
    for name in sorted(names):
        saved = n.project_folders("Owner", name)[1]
        assert n.is_safe_component(saved) and units(saved) <= 120
        assert n.is_safe_component(name) or saved.casefold() not in unchanged
        assert seen.setdefault(saved.casefold(), name.casefold()) == name.casefold()
        assert n.project_folders("Owner", name.upper())[1].casefold() == saved.casefold()


@pytest.mark.parametrize("tag", ["..", "", " ", "a/b", "CON", "x" * 5000, "v1.0.", "\x00", "\u202e", "\U0001f600" * 100])
def test_N08_release_folders_hostile_and_id_identity(tag):
    folders = [n.release_folder_name(tag, i) for i in (1, 11, 111, 2**63 - 1)]
    assert len({f.casefold() for f in folders}) == len(folders)
    for folder, i in zip(folders, (1, 11, 111, 2**63 - 1)):
        assert units(folder) <= 64 and n.is_safe_component(folder)
        assert n.is_release_folder(folder, i) and folder.endswith(f"--{i}")
        # The maximal trailing digit run identifies the release, independent of tag.
        assert re.search(r"\d+$", folder).group() == str(i)
    assert n.release_folder_name("v1.0.", 1) == "release-v1.0--1"
    assert n.release_folder_name("..", 1) == "release-untagged--1"


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "1", None])
def test_N09_release_ids_strict(value):
    rejected(lambda: n.release_folder_name("v1", value), error=(TypeError, ValueError))


@pytest.mark.parametrize("folder", ["release-v1", "release-v1--2", "release-a/b--1", "release-CON--1.", "release-" + "x" * 80 + "--1"])
def test_N10_release_folder_validation(folder):
    assert not n.is_release_folder(folder, 1)
    assert n.is_release_folder("release-v1--1", 1)


def test_N11_allocation_singleton_and_empty_boundaries():
    assert n.allocate_asset_names({}) == {}
    assert n.allocate_asset_names({7: "a.zip"}) == {7: n.AssetName(7, "a.zip", "a.zip", False)}


@pytest.mark.parametrize("left,right", [("A.zip", "a.zip"), ("\u00e9.zip", "e\u0301.zip"), ("a/b.zip", "a\\b.zip")])
def test_N12_collision_groups_all_get_id_suffix(left, right):
    result = n.allocate_asset_names({20: right, 10: left})
    assert list(result) == [10, 20]
    for i, name in ((10, left), (20, right)):
        assert result[i].saved_name == n.sanitize_component(name)[:-4] + f"--{i}.zip"
        assert result[i].disambiguated and result[i].original_name == name


def test_N13_every_permutation_and_independent_collision_keys():
    items = [(8, "A.zip"), (3, "a.zip"), (9, "release.json"), (1, "unique.zip")]
    expected = n.allocate_asset_names(dict(items))
    for order in permutations(items):
        actual = n.allocate_asset_names(dict(order))
        assert actual == expected and list(actual) == sorted(actual)
        keys = [ud.normalize("NFKC", ud.normalize("NFKC", v.saved_name).casefold()) for v in actual.values()]
        assert len(keys) == len(set(keys)) and "release.json" not in keys


@pytest.mark.parametrize("name", ["release.json", "RELEASE.JSON", "A.zip"])
def test_N14_persisted_names_reserved_and_untouched(name):
    taken = {1: "a.zip", 6: "unselected.zip"}
    result = n.allocate_asset_names({2: name}, taken=taken)
    assert taken == {1: "a.zip", 6: "unselected.zip"}
    assert result[2].disambiguated and result[2].saved_name == name.rsplit(".", 1)[0] + "--2." + name.rsplit(".", 1)[1]


def test_N15_suffix_fallback_and_truncation_collision():
    result = n.allocate_asset_names({2: "a.zip", 3: "A.zip", 4: "a--2.zip"})
    assert result[2].saved_name == "a--2-2.zip" and not result[4].disambiguated
    result = n.allocate_asset_names({1: "x" * 200 + "a.tar.gz", 2: "x" * 200 + "b.tar.gz"}, limit=48)
    assert len({v.saved_name.casefold() for v in result.values()}) == 2
    assert all(units(v.saved_name) <= 48 and v.saved_name.endswith(".tar.gz") for v in result.values())


def test_N16_utf16_astral_truncation_never_splits_character():
    saved = n.sanitize_component("\U0001f600" * 100 + ".zip", limit=49)
    assert units(saved) <= 49 and saved.endswith(".zip")
    assert all(ud.category(c) != "Cs" for c in saved) and "--" in saved
    assert n.sanitize_component("a\u0378") == "a\u0378"  # Cn must remain unchanged.


@pytest.mark.parametrize("root", ["relative", "C:relative", "\\relative", r"\\?\C:\a", r"\\.\C:\a", r"C:\a\..\b", 'C:\\bad?', 'C:\\bad\x00', 'C:\\bad:part'])
def test_N17_invalid_roots(root):
    rejected(lambda: n.filename_limit(root, "o", "r", "release-v1--1"), "root", n.PathBudgetError)
    rejected(lambda: n.check_path_budget(root, "o", "r", "release-v1--1", "a.zip"), "root", n.PathBudgetError)


@pytest.mark.parametrize("root", ["C:\\archive", PureWindowsPath(r"\\server\share\archive"), "C:\\" + "\U0001f600" * 55])
def test_N18_budget_formula_and_deep_root(root):
    prefix = units(str(root).rstrip("\\")) + 1 + 1 + 1 + 1 + 1 + 13 + 1
    assert n.filename_limit(root, "o", "r", "release-v1--1") == min(120, 240 - prefix)
    rejected(lambda: n.filename_limit("C:\\" + "x" * 200, "o", "r", "release-v1--1"), "budget", n.PathBudgetError)


def test_N19_full_path_and_component_boundaries():
    assert n.check_path_budget("C:\\archive", "o", "r", "release-v1--1", "a.zip") is None
    rejected(lambda: n.check_path_budget("C:\\" + "x" * 170, "owner", "repo", "release-v1--1", "a" * 60), "budget", n.PathBudgetError)
    rejected(lambda: n.check_path_budget("C:\\a", "o", "r", "release-v1--1", "a" * 121), "budget", n.PathBudgetError)


def test_N20_types_constants_and_frozen_record():
    assert (n.INDEX_FILENAME, n.RELEASE_FILENAME, n.MAX_PATH_BUDGET, n.MAX_COMPONENT_LENGTH,
            n.MAX_RELEASE_FOLDER_LENGTH, n.MIN_FILENAME_LENGTH) == ("archive-index.json", "release.json", 240, 120, 64, 48)
    rejected(lambda: n.sanitize_component(None), error=TypeError)
    with pytest.raises(FrozenInstanceError):
        n.AssetName(1, "a.zip", "a.zip", False).saved_name = "b.zip"


@pytest.mark.parametrize("tag", [None, 1, [], False])
def test_R15_tag_type(tag):
    with pytest.raises(Exception) as caught: n.release_folder_name(tag, 1)
    assert type(caught.value) is TypeError

@pytest.mark.parametrize("root", ["C:\\abc", r"\\server\share\abc", "C:\\\U0001f600"])
def test_R17_exact_budgets(root):
    assert n.filename_limit("C:\\" + "x" * 170, "o", "r", "release-v1--1") == 48
    rejected(lambda: n.filename_limit("C:\\" + "x" * 171, "o", "r", "release-v1--1"), "budget", n.PathBudgetError)
    prefix = units(root) + 60 + 60 + 13 + 4
    for total in (239, 240):
        assert n.check_path_budget(root, "o" * 60, "r" * 60, "release-v1--1", "a" * (total - prefix)) is None
    rejected(lambda: n.check_path_budget(root, "o" * 60, "r" * 60, "release-v1--1", "a" * (241 - prefix)), "budget", n.PathBudgetError)


@pytest.mark.parametrize("name", ["x" * 150 + ".", "\U0001f600" * 130])
def test_R23_project_long_unvalidated_name(name):
    saved = n.project_folders("Owner", name)[1]
    assert units(saved) <= 120 and n.is_safe_component(saved) and re.search(r"\+[0-9a-f]{12}$", saved)


def test_R23_plus_cannot_alias_transformed_namespace():
    transformed = n.project_folders("Owner", "foo.")[1]
    saved = n.project_folders("Owner", transformed)[1]
    assert saved != transformed and n.is_safe_component(saved) and re.search(r"\+[0-9a-f]{12}$", saved)
    assert n.project_folders("Owner", "a+b")[1] not in ("a+b", saved, transformed)
