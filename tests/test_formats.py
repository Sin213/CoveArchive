"""Behavioral contract for ASCII-only release-asset suffix matching."""

from types import MappingProxyType

import pytest

from covearchive.formats import FORMAT_LABELS, SUPPORTED_EXTENSIONS, match_extension


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("file.appimage", ".appimage"),
        ("file.APPIMAGE", ".appimage"),
        ("file.AppImage", ".appimage"),
        ("file.tar.gz", ".tar.gz"),
        ("file.TAR.GZ", ".tar.gz"),
        ("file.TaR.gZ", ".tar.gz"),
        ("file.tar", ".tar"),
        ("file.TAR", ".tar"),
        ("file.TaR", ".tar"),
        ("file.dmg", ".dmg"),
        ("file.DMG", ".dmg"),
        ("file.DmG", ".dmg"),
        ("file.zip", ".zip"),
        ("file.ZIP", ".zip"),
        ("file.ZiP", ".zip"),
        ("file.7z", ".7z"),
        ("file.7Z", ".7z"),
        ("file.7z.zip", ".zip"),
        ("file.zip.tar.gz", ".tar.gz"),
        ("café.ZIP", ".zip"),
        ("a b.zip", ".zip"),
        (".hidden.zip", ".zip"),
        ("", None),
        ("file", None),
        (".appimage", None),
        (".tar.gz", None),
        (".tar", None),
        (".dmg", None),
        (".zip", None),
        (".7z", None),
        (".TAR.GZ", None),
        ("file.zip.001", None),
        ("file.zipx", None),
        ("file.tar.gz.sig", None),
        ("file.zip ", None),
        ("file.zip.", None),
        ("file.zip\n", None),
        ("file.zip\x00", None),
        ("file.appimage.sig", None),
        ("file.dmg.part", None),
        ("file.7zip", None),
        ("file.zıp", None),
        ("file.appımage", None),
        ("FILE.ZİP", None),
        ("file.zÄ±p", None),
        ("file.appÄ±mage", None),
        ("FILE.ZÄ°P", None),
        ("file.zſp", None),
        ("file.Kip", None),
        ("file.ｚｉｐ", None),
        ("file.appﬁmage", None),
        ("file．zip", None),
    ],
)
def test_filename_suffixes(filename, expected):
    assert match_extension(filename) == expected


@pytest.mark.parametrize(
    "filename, enabled, expected",
    [
        ("file.zip", None, ".zip"),
        ("file.zip", [".zip"], ".zip"),
        ("file.ZIP", (".zip",), ".zip"),
        ("file.zip", {".zip", ".tar"}, ".zip"),
        ("file.zip", [".zip", ".zip"], ".zip"),
        ("file.zip", [".tar"], None),
        ("file.tar.gz", [".tar"], None),
        ("file.TAR.GZ", [".tar"], None),
        ("file.tar.gz", [".tar.gz"], ".tar.gz"),
        ("file.tar.gz", [".tar", ".tar.gz"], ".tar.gz"),
        ("file.tar", [".tar.gz"], None),
        ("file.tar.gz.sig", [".tar"], None),
        ("file.7z.zip", [".7z"], None),
        ("file.7z.zip", [".zip"], ".zip"),
        ("file.zip", [], None),
        ("file.tar.gz", (), None),
        (".tar.gz", [".tar.gz", ".tar"], None),
    ],
)
def test_enabled_formats_filter_the_actual_extension(filename, enabled, expected):
    assert match_extension(filename, enabled) == expected


def test_enabled_can_be_a_single_use_iterable():
    enabled = (ext for ext in [".tar", ".zip"])
    assert match_extension("file.ZIP", enabled) == ".zip"


@pytest.mark.parametrize("filename", ["file.zip", "no-extension", ""])
@pytest.mark.parametrize("unknown", [".rar", ".ZIP", "zip", "", ".tar.GZ", None, 1])
def test_every_enabled_entry_is_validated_even_without_a_match(filename, unknown):
    with pytest.raises(ValueError):
        match_extension(filename, [".zip", unknown])


@pytest.mark.parametrize("enabled", [".zip", "", b".zip", b""])
def test_bare_string_or_bytes_is_not_an_enabled_collection(enabled):
    with pytest.raises(TypeError):
        match_extension("file.zip", enabled)


@pytest.mark.parametrize("filename", [None, 1, b"file.zip", [], {}, True])
def test_filename_must_be_a_string(filename):
    with pytest.raises(TypeError):
        match_extension(filename)


def test_supported_extensions_have_exact_type_and_order():
    assert isinstance(SUPPORTED_EXTENSIONS, tuple)
    assert SUPPORTED_EXTENSIONS == (
        ".appimage", ".tar.gz", ".tar", ".dmg", ".zip", ".7z",
    )


def test_format_labels_cover_the_registry_and_are_read_only():
    assert isinstance(FORMAT_LABELS, MappingProxyType)
    assert set(FORMAT_LABELS) == set(SUPPORTED_EXTENSIONS)
    assert dict(FORMAT_LABELS) == {
        ".appimage": "AppImage",
        ".tar.gz": "TAR.GZ",
        ".tar": "TAR",
        ".dmg": "DMG",
        ".zip": "ZIP",
        ".7z": "7z",
    }
    with pytest.raises(TypeError):
        FORMAT_LABELS[".zip"] = "changed"
    with pytest.raises(TypeError):
        FORMAT_LABELS[".rar"] = "RAR"
