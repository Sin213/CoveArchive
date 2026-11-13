"""Offline schema, digest, and immutable metadata checks."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess
import sys

import pytest

from covearchive.github.models import (
    ReleaseAsset, Release, ReleaseCatalog, ReleaseSchemaError, parse_asset, parse_release,
)
from covearchive.github.urls import RepositoryTarget


DATE = "2026-11-13T12:00:00Z"


def asset(**changes):
    return dict(id=10, name="app.ZIP", size=42, state="uploaded",
                content_type="application/zip", browser_download_url="https://example/a",
                created_at=DATE, updated_at=DATE, **changes)


def release(**changes):
    data = dict(id=1, tag_name="v1", draft=False, prerelease=False,
                created_at=DATE, assets=[asset()])
    data.update(changes)
    return data


def test_parse_release_preserves_metadata_and_defaults():
    parsed = parse_release(release())
    assert isinstance(parsed, Release)
    assert parsed.id == 1 and parsed.tag_name == "v1"
    assert parsed.assets == (parse_asset(asset()),)
    assert isinstance(parsed.assets[0], ReleaseAsset)
    assert parsed.assets[0].supported_extension == ".zip"
    assert parsed.created_at == DATE
    assert (parsed.name, parsed.published_at, parsed.body, parsed.immutable,
            parsed.tarball_url, parsed.zipball_url) == (None,) * 6
    assert (parsed.assets[0].original_digest, parsed.assets[0].digest_algorithm,
            parsed.assets[0].sha256) == (None,) * 3


def test_optional_metadata_is_verbatim_and_unknown_fields_ignored():
    text = " <script>do_stuff()</script>\n$(echo x)\x00 "
    parsed = parse_release(release(name=text, body=text, tag_name=text,
        published_at=DATE, immutable=True, tarball_url="tar", zipball_url="zip", extra=1))
    assert (parsed.name, parsed.body, parsed.tag_name) == (text,) * 3
    assert (parsed.tarball_url, parsed.zipball_url) == ("tar", "zip")
    assert parsed.published_at == DATE and parsed.immutable is True
    data = asset()
    data.update(name=text, extra="ignored", state="pending")
    assert parse_asset(data).name == text


def test_explicit_null_metadata_is_not_invented():
    parsed = parse_release(release(name=None, body=None, published_at=None,
        immutable=None, tarball_url=None, zipball_url=None))
    assert (parsed.name, parsed.body, parsed.published_at, parsed.immutable,
            parsed.tarball_url, parsed.zipball_url) == (None,) * 6


@pytest.mark.parametrize("suffix", [".AppImage", ".TAR.GZ", ".TAR", ".DMG", ".ZIP", ".7Z"])
def test_supported_extension_is_metadata_regardless_of_state(suffix):
    parsed = parse_asset(dict(asset(), name="app" + suffix, state="new"))
    assert parsed.supported_extension == suffix.lower()
    assert parsed.state == "new"


@pytest.mark.parametrize("digest,algorithm,sha", [
    (None, None, None), ("SHA256:" + "Ab" * 32, "sha256", "ab" * 32),
    ("sha512:opaque-value", "sha512", None),
])
def test_digest_metadata(digest, algorithm, sha):
    parsed = parse_asset(dict(asset(), digest=digest))
    assert (parsed.original_digest, parsed.digest_algorithm, parsed.sha256) == (
        digest, algorithm, sha)


@pytest.mark.parametrize("digest", [True, 1, [], "", "sha256", ":abc", "sha256:",
    "sha256:" + "a" * 63, "sha256:" + "a" * 65, "sha256:" + "g" * 64,
    "sha256:" + "a" * 64 + "\n"])
def test_invalid_digest(digest):
    with pytest.raises(ReleaseSchemaError):
        parse_asset(dict(asset(), digest=digest))


@pytest.mark.parametrize("digest", ["sha 256:abc", "x" * 33 + ":a", "sha512:\n",
    "sha512:" + "a" * 513, "sha512:nonascii\u00e9", "sh\u212a:abc", "sha512:\x7f"])
def test_invalid_digest_algorithm_or_opaque_value(digest):
    with pytest.raises(ReleaseSchemaError):
        parse_asset(dict(asset(), digest=digest))


def test_uppercase_other_digest_algorithm():
    parsed = parse_asset(dict(asset(), digest="SHA512:abc"))
    assert (parsed.original_digest, parsed.digest_algorithm, parsed.sha256) == (
        "SHA512:abc", "sha512", None)


@pytest.mark.parametrize("factory,parser,fields", [
    (release, parse_release, ("id", "tag_name", "draft", "prerelease", "assets", "created_at")),
    (asset, parse_asset, ("id", "name", "size", "state", "content_type",
                        "browser_download_url", "created_at", "updated_at")),
])
def test_all_mandatory_fields(factory, parser, fields):
    for field in fields:
        data = factory()
        del data[field]
        with pytest.raises(ReleaseSchemaError, match=field):
            parser(data)


@pytest.mark.parametrize("field,value", [("id", True), ("id", 0), ("id", -1),
    ("tag_name", ""), ("tag_name", 1), ("draft", 0), ("prerelease", "false"),
    ("assets", ()), ("assets", None), ("assets", [None]), ("created_at", "bad"),
    ("created_at", None), ("published_at", "bad"), ("name", 1), ("body", False),
    ("immutable", 1), ("tarball_url", 4), ("zipball_url", []),])
def test_invalid_release_fields(field, value):
    with pytest.raises(ReleaseSchemaError):
        parse_release(release(**{field: value}))


@pytest.mark.parametrize("field,value", [("id", False), ("id", 0), ("size", True),
    ("size", -1), ("size", 1.5), ("name", ""), ("name", None), ("state", None),
    ("content_type", 1), ("browser_download_url", None), ("created_at", "bad"),
    ("updated_at", 2)])
def test_invalid_asset_fields(field, value):
    with pytest.raises(ReleaseSchemaError):
        parse_asset(dict(asset(), **{field: value}))


@pytest.mark.parametrize("value", [None, [], 1, "object", True])
@pytest.mark.parametrize("parser", [parse_asset, parse_release])
def test_object_required(parser, value):
    with pytest.raises(ReleaseSchemaError):
        parser(value)


def test_models_are_frozen_and_catalog_is_non_atomic():
    parsed = parse_release(release())
    catalog = ReleaseCatalog(RepositoryTarget("owner", "repo"), (parsed,), 1, 1, 0,
                             (), "/repos/owner/repo/releases", True)
    assert catalog.atomic_snapshot is False and catalog.complete is True
    for obj, field in [(parsed, "name"), (parsed.assets[0], "name"), (catalog, "complete")]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, None)


def test_github_exports_are_lazy_and_pure_exports_work_without_requests():
    script = '''
import sys
sys.path.insert(0, sys.argv[1])
sys.modules['requests'] = None
import covearchive.github as github
assert not any(n.startswith('covearchive.github.') for n in sys.modules)
assert 'Release' in dir(github) and 'select_releases' in github.__all__
assert github.Release.__module__ == 'covearchive.github.models'
assert callable(github.select_releases)
assert 'covearchive.github.client' not in sys.modules
try:
    github.unknown_name
except AttributeError:
    pass
else:
    raise AssertionError('unknown export accepted')
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script,
        str(Path(__file__).resolve().parents[1] / "src")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
