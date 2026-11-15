"""M01-M15: typed schema and hostile JSON acceptance boundaries."""

from dataclasses import FrozenInstanceError, asdict, replace
from itertools import product
import json
from urllib.parse import quote

import pytest

from covearchive.archive import manifest as m
from covearchive.github.models import parse_release
from covearchive.github.urls import RepositoryTarget


REPO = RepositoryTarget("Owner", "Repo")
HASH = "a" * 64


def asset(i=1, **changes):
    values = dict(asset_id=i, original_name=f"a{i}.zip", saved_name=f"a{i}.zip", size=0,
                  publisher_sha256=None, local_sha256=None, status="planned")
    values.update(changes)
    return m.AssetRecord(**values)


def record(i=1, **changes):
    values = dict(release_id=i, tag=f"v{i}", folder=f"release-v{i}--{i}", assets=(asset(i),))
    values.update(changes)
    return m.ReleaseRecord(**values)


def manifest(**changes):
    values = dict(repository=REPO, release_id=1, tag="v1", folder="release-v1--1",
                  title="Title", published_at="2026-11-13T12:00:00Z", prerelease=False,
                  source_url="https://github.com/Owner/Repo/releases/tag/v1", assets=(asset(),))
    values.update(changes)
    return m.ReleaseManifest(**values)


def document(release=False):
    a = dict(asset_id=1, original_name="a1.zip", saved_name="a1.zip", size=0,
             publisher_sha256=None, local_sha256=None, status="planned")
    r = dict(release_id=1, tag="v1", folder="release-v1--1")
    data = dict(schema_version=1, provider="github", repository=dict(owner="Owner", name="Repo", key="owner/repo"))
    if release:
        data.update(release=dict(**r, title="Title", published_at="2026-11-13T12:00:00Z",
                                prerelease=False, source_url="https://github.com/Owner/Repo/releases/tag/v1"), assets=[a])
    else:
        data["releases"] = [dict(**r, assets=[a])]
    return data


def rejected(call, code=None, error=m.ManifestError):
    try:
        call()
    except error as exc:
        if code is not None:
            assert exc.code == code
    else:
        assert False, f"expected {error}: {code}"


def test_M01_valid_boundaries_frozen_and_normalized():
    a = asset(2**63 - 1, size=m.MAX_SIZE)
    r = record(2, assets=(a, asset()))
    index = m.ProjectIndex(REPO, (r, record(1, assets=())))
    assert [x.release_id for x in index.releases] == [1, 2]
    assert [x.asset_id for x in r.assets] == [1, 2**63 - 1]
    assert record(1, assets=()).assets == ()
    for obj, field in ((a, "size"), (r, "tag"), (index, "releases"), (manifest(), "title")):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, None)


@pytest.mark.parametrize("field,value,code", [("asset_id", True, "id"), ("asset_id", 0, "id"),
    ("asset_id", 2**63, "id"), ("asset_id", "1", "id"), ("size", True, "size"),
    ("size", -1, "size"), ("size", 2**53, "size"), ("size", 1.0, "size"),
    ("original_name", "x" * 1025, "limit"), ("original_name", None, "field")])
def test_M02_asset_scalar_validation(field, value, code):
    rejected(lambda: asset(**{field: value}), code)


@pytest.mark.parametrize("changes,code", [
    ({"publisher_sha256": "A" * 64}, "hash"), ({"local_sha256": "bad"}, "hash"),
    ({"status": "unknown"}, "status"), ({"local_sha256": HASH}, "status"),
    ({"status": "completed"}, "status"),
    ({"status": "completed", "local_sha256": HASH, "publisher_sha256": "b" * 64}, "hash")])
def test_M03_hash_status_invariants(changes, code):
    rejected(lambda: asset(**changes), code)


def test_M04_publisher_evidence_not_invented():
    assert not asset().publisher_verified
    assert not asset(status="completed", local_sha256=HASH).publisher_verified
    assert asset(status="completed", local_sha256=HASH, publisher_sha256=HASH).publisher_verified


@pytest.mark.parametrize("name", ["..", "a/b", "a\\b", "C:x", "CON", "release.json", "RELEASE.JSON", "a.", "a" * 121])
def test_M05_saved_names_are_safe_and_not_metadata(name):
    rejected(lambda: asset(saved_name=name), "path")


@pytest.mark.parametrize("changes,code", [({"release_id": True}, "id"),
    ({"release_id": 0}, "id"), ({"release_id": 2**63}, "id"), ({"tag": "x" * 1025}, "limit"),
    ({"folder": "release-v1"}, "path"), ({"folder": "release-v1--2"}, "path"),
    ({"assets": (asset(), asset())}, "duplicate"),
    ({"assets": (asset(1, saved_name="A.zip"), asset(2, saved_name="a.zip"))}, "duplicate"),
    ({"assets": (asset(1, saved_name="\u00e9.zip"), asset(2, saved_name="e\u0301.zip"))}, "duplicate")])
def test_M06_release_record_validation(changes, code):
    rejected(lambda: record(**changes), code)


@pytest.mark.parametrize("repo", [RepositoryTarget("bad/owner", "r"), RepositoryTarget("o", ".."), RepositoryTarget("o", "repo.git")])
def test_M07_repository_roundtrip_validation(repo):
    rejected(lambda: m.ProjectIndex(repo, ()), "identity")
    rejected(lambda: manifest(repository=repo), "identity")


def test_M08_index_duplicate_ids_and_count_caps():
    rejected(lambda: m.ProjectIndex(REPO, (record(), record())), "duplicate")
    rejected(lambda: m.ProjectIndex(REPO, (record(), record(2, assets=(asset(),)))), "duplicate")
    rejected(lambda: record(assets=tuple(asset(i) for i in range(1, 1002))), "limit")
    rejected(lambda: m.ProjectIndex(REPO, tuple(record(i, assets=()) for i in range(1, 50_002))), "limit")
    releases = tuple(record(i + 1, assets=tuple(asset(i * 1000 + j + 1) for j in range(1000))) for i in range(101))
    rejected(lambda: m.ProjectIndex(REPO, releases), "limit")


@pytest.mark.parametrize("changes,code", [({"title": "x" * 1025}, "limit"),
    ({"published_at": "not a date"}, "field"), ({"published_at": 12}, "field"),
    ({"prerelease": 1}, "field"), ({"source_url": "https://cdn.example/a?token=secret"}, "path"),
    ({"source_url": "https://github.com/Owner/Repo/releases/tag/v1?x=1"}, "path"),
    ({"source_url": "https://github.com/Owner/Repo/releases/tag/v1#x"}, "path"),
    ({"source_url": "https://x@github.com/Owner/Repo/releases/tag/v1"}, "path"),
    ({"source_url": "https://github.com/other/Repo/releases/tag/v1"}, "path"),
    ({"folder": "release-v1--2"}, "path"), ({"assets": (asset(), asset())}, "duplicate")])
def test_M09_release_manifest_validation(changes, code):
    rejected(lambda: manifest(**changes), code)


@pytest.mark.parametrize("release", [False, True])
def test_M10_exact_json_order_roundtrip_and_ascii(release):
    value = manifest() if release else m.ProjectIndex(REPO, (record(),))
    dump = m.dumps_release_manifest if release else m.dumps_index
    load = m.loads_release_manifest if release else m.loads_index
    expected = json.dumps(document(release), indent=2, ensure_ascii=True) + "\n"
    assert dump(value) == expected
    assert load(expected) == value and dump(load(expected)) == expected
    unicode_value = replace(value, title="\u00e9") if release else m.ProjectIndex(REPO, (record(tag="\u00e9"),))
    assert "\\u00e9" in dump(unicode_value) and "\u00e9" not in dump(unicode_value)


@pytest.mark.parametrize("version", [0, 2, -1, True, "1", None, 1.0])
@pytest.mark.parametrize("release", [False, True])
def test_M11_unsupported_versions_never_downgraded(version, release):
    data = document(release)
    data["schema_version"] = version
    load = m.loads_release_manifest if release else m.loads_index
    rejected(lambda: load(json.dumps(data)), "version", m.UnsupportedVersionError)


@pytest.mark.parametrize("text,code", [("{", "syntax"), ("[]", "root"), ("null", "root"),
    ('{"schema_version":1,"schema_version":1}', "syntax"),
    ('{"schema_version":NaN}', "syntax"), ('{"schema_version":Infinity}', "syntax"),
    ("[" * 10_000 + "0" + "]" * 10_000, "limit")])
@pytest.mark.parametrize("load", [m.loads_index, m.loads_release_manifest])
def test_M12_parser_malformed_duplicates_numbers_depth(text, code, load):
    rejected(lambda: load(text), code)


@pytest.mark.parametrize("load", [m.loads_index, m.loads_release_manifest])
@pytest.mark.parametrize("depth", [1500, 10000])
@pytest.mark.parametrize("opening,closing", [("[", "]"), ('{"a":', "}")])
def test_json_nesting_bombs(load, depth, opening, closing):
    rejected(lambda: load(opening * depth + "0" + closing * depth), "limit")


@pytest.mark.parametrize("release", [False, True])
def test_modest_json_depth_loads(release):
    load = m.loads_release_manifest if release else m.loads_index
    assert load(json.dumps(document(release))) == (manifest() if release else m.ProjectIndex(REPO, (record(),)))


@pytest.mark.parametrize("release", [False, True])
def test_M13_strict_keys_identity_float_sizes_and_input_caps(release):
    load = m.loads_release_manifest if release else m.loads_index
    for location in ("root", "repository", "release", "asset"):
        for operation in ("unknown", "missing"):
            data = document(release)
            target = {"root": data, "repository": data["repository"],
                      "release": data["release"] if release else data["releases"][0],
                      "asset": data["assets"][0] if release else data["releases"][0]["assets"][0]}[location]
            if operation == "unknown":
                target["unexpected"] = 1
            else:
                target.pop(next(iter(target)))
            # Missing schema_version is a missing field, not an unsupported version.
            rejected(lambda: load(json.dumps(data)), "field")
    for key, value, code in (("key", "wrong/repo", "identity"), ("provider", "other", "field")):
        data = document(release)
        (data["repository"] if key == "key" else data)[key] = value
        rejected(lambda: load(json.dumps(data)), code)
    text = json.dumps(document(release))
    for value in ("1.0", "1e3", "true"):
        rejected(lambda: load(text.replace('"size": 0', '"size": ' + value)), "size")
    rejected(lambda: load(b"{}"), error=(TypeError, m.ManifestError))
    cap = m.MAX_MANIFEST_BYTES if release else m.MAX_INDEX_BYTES
    rejected(lambda: load(" " * (cap + 1)), "limit")


def test_M14_builder_record_authority_and_secret_absence():
    tag = "v1/a +b"
    parsed = parse_release(dict(id=1, tag_name=tag, name="Title", draft=False, prerelease=True,
        created_at="2026-11-13", published_at="2026-11-13T12:00:00Z", assets=[dict(id=1,
        name="a1.zip", size=0, state="uploaded", content_type="opaque", created_at="2026-11-13",
        updated_at="2026-11-13", browser_download_url="https://cdn.example/a?token=secret&X-Amz-Signature=x")]))
    rec = record(tag=tag)
    built = m.build_release_manifest(REPO, parsed, rec)
    assert (built.release_id, built.tag, built.folder, built.assets) == (rec.release_id, rec.tag, rec.folder, rec.assets)
    assert (built.title, built.published_at, built.prerelease) == (parsed.name, parsed.published_at, True)
    assert built.source_url == "https://github.com/Owner/Repo/releases/tag/" + quote(tag, safe="/")
    text = m.dumps_release_manifest(built) + m.dumps_index(m.ProjectIndex(REPO, (rec,)))
    assert all(secret not in text for secret in ("browser_download_url", "token", "X-Amz", "?", "cdn.example"))


@pytest.mark.parametrize("changes", [{"repository": RepositoryTarget("other", "repo"),
    "source_url": "https://github.com/other/repo/releases/tag/v1"},
    {"release_id": 2, "folder": "release-v1--2"}, {"tag": "changed",
    "source_url": "https://github.com/Owner/Repo/releases/tag/changed"},
    {"folder": "release-other--1"}, {"assets": ()}, {"assets": (asset(size=1),)}])
def test_M15_consistency_full_records_and_identity(changes):
    index = m.ProjectIndex(REPO, (record(),))
    assert m.check_consistent(index, manifest()) is None
    rejected(lambda: m.check_consistent(index, manifest(**changes)), "consistency")


@pytest.mark.parametrize("char", ["\u4e00", "\U0001f600"])
@pytest.mark.parametrize("release", [False, True])
def test_R12_expanded_manifest_rejected_at_construction(monkeypatch, char, release):
    monkeypatch.setattr(m, "MAX_MANIFEST_BYTES" if release else "MAX_INDEX_BYTES", 2 * 1024 * 1024)
    items = tuple(asset(i, original_name=char * 1020 + ".zip") for i in range(1, 401))
    rejected(lambda: manifest(assets=items) if release else m.ProjectIndex(REPO, (record(assets=items),)), "limit")


@pytest.mark.parametrize("release", [False, True])
def test_R12_exact_serialized_cap_and_roundtrip(monkeypatch, release):
    make = (lambda: manifest(title="\U0001f600")) if release else (lambda: m.ProjectIndex(REPO, (record(tag="\U0001f600"),)))
    dump, load = (m.dumps_release_manifest, m.loads_release_manifest) if release else (m.dumps_index, m.loads_index)
    text = dump(make())
    cap = "MAX_MANIFEST_BYTES" if release else "MAX_INDEX_BYTES"
    monkeypatch.setattr(m, cap, len(text))
    assert load(dump(make())) == make() and dump(load(text)) == text
    monkeypatch.setattr(m, cap, len(text) - 1)
    rejected(make, "limit")


def test_R13_tag_boundary():
    assert getattr(m, "MAX_TAG_LENGTH", None) == 1024 and record(tag="x" * 1024).tag == "x" * 1024

def test_R16_surrogate_source_and_R17_builder_identity():
    tag = "v\ud800"
    source = parse_release(dict(id=1, tag_name=tag, draft=False, prerelease=False, created_at="2026-11-13", assets=[]))
    try: built = m.build_release_manifest(REPO, source, record(tag=tag))
    except Exception: assert False, "an unencodable tag must omit its derived URL"
    assert built.tag == tag and built.source_url is None
    rejected(lambda: manifest(tag=tag, source_url="https://github.com/Owner/Repo/releases/tag/v1"), "path")
    rejected(lambda: m.build_release_manifest(REPO, replace(source, id=2), record(tag=tag)), "identity")


def r3_document(position, text):
    if position == "index_tag": value = m.ProjectIndex(REPO, (record(tag=text),))
    elif position == "original_name": value = m.ProjectIndex(REPO, (record(assets=(asset(original_name=text),)),))
    else: value = manifest(source_url=None, **{position: text})
    return (value, m.dumps_index, m.loads_index) if position in ("index_tag", "original_name") else (value, m.dumps_release_manifest, m.loads_release_manifest)


@pytest.mark.parametrize("position", ["index_tag", "original_name", "title", "tag"])
@pytest.mark.parametrize("text,accepted", [("\ud83d\ude00", False), ("\ud800", True), ("\udfff", True),
    ("\udfff\ud800", True), ("\U0001f600", True), ("\x00", True), ("\u2028", True)])
def test_R3_text_roundtrip(position, text, accepted):
    if not accepted:
        rejected(lambda: r3_document(position, text), "field")
        return
    value, dump, load = r3_document(position, text)
    serialized = dump(value); restored = load(serialized)
    assert restored == value and dump(restored) == serialized


def test_R3_mini_corpus_roundtrip():
    fragments = ("a", "\ud800", "\udfff", "\ud83d\ude00", "\udfff\ud800", "\U0001f600", "\x00", "\u2028", "e\u0301", ".")
    for length in range(1, 4):
        for parts in product(fragments, repeat=length):
            for position in ("index_tag", "original_name", "title", "tag"):
                try: value, dump, load = r3_document(position, "".join(parts))
                except Exception as exc: assert type(exc) is m.ManifestError
                else:
                    serialized = dump(value); restored = load(serialized)
                    assert restored == value and dump(restored) == serialized


def test_R4_manifest_cap_derivation():
    worst = asset(original_name="\U0001f600" * 1024, saved_name="\U0001f600" * 60,
                  size=m.MAX_SIZE, status="completed", publisher_sha256=HASH, local_sha256=HASH)
    assert m.MAX_ASSETS_PER_RELEASE * len(json.dumps(asdict(worst), indent=2, ensure_ascii=True)) + 3 * 1024 * 12 + 4096 <= m.MAX_MANIFEST_BYTES
