"""Validate public identities and keep classification evidence explicit."""

from dataclasses import FrozenInstanceError, fields

import pytest

from covearchive.discover.models import (
    Classification, Evidence, EvidenceSource, OfficialStatus, ProjectKind,
    ReleaseStatus, SearchPage, SearchRecordError, Tri, classify_project,
    parse_github_repository, sanitize_text,
)
from covearchive.github.urls import RepositoryTarget


def repository(**changes):
    item = {
        "id": 123, "owner": {"login": "Zelda64Recomp"}, "name": "Zelda64Recomp",
        "full_name": "Zelda64Recomp/Zelda64Recomp",
        "html_url": "https://github.com/Zelda64Recomp/Zelda64Recomp",
        "url": "https://api.github.com/repos/Zelda64Recomp/Zelda64Recomp",
        "private": False, "visibility": "public", "disabled": False,
        "description": "Static recompilation of Majora's Mask for PC",
        "topics": ["static-recompilation", "zelda"], "language": "C++",
        "fork": False, "archived": False, "stargazers_count": 17800,
        "pushed_at": "2026-11-13T12:00:00Z",
    }
    return item | changes


def test_realistic_result_is_immutable_and_has_only_repository_metadata():
    result = parse_github_repository(repository(homepage="https://untrusted.invalid", avatar_url="secret"))
    assert (result.provider, result.host, result.repository_id) == ("github", "github.com", 123)
    assert result.target == RepositoryTarget("Zelda64Recomp", "Zelda64Recomp")
    assert result.url == "https://github.com/Zelda64Recomp/Zelda64Recomp"
    assert result.topics == ("static-recompilation", "zelda")
    assert result.kinds == (ProjectKind.RECOMP,)
    assert (result.stars, result.last_activity, result.language) == (17800, "2026-11-13T12:00:00Z", "C++")
    assert result.release_status is ReleaseStatus.UNKNOWN
    assert result.official_status is OfficialStatus.UNKNOWN
    assert result.original_platform == result.host_os == "unknown"
    assert not {"homepage", "avatar_url", "links"} & {f.name for f in fields(result)}
    for value, attr in [(result, "name"), (result.classifications[0], "kind"),
                        (result.classifications[0].evidence[0], "matched")]:
        with pytest.raises(FrozenInstanceError):
            setattr(value, attr, "changed")
    page = SearchPage((result,), 1, 30, 1, 1, False, 0, 0, False)
    with pytest.raises(FrozenInstanceError):
        page.results = ()


@pytest.mark.parametrize("changes", [
    {"private": True}, {"private": None}, {"private": 0},
    {"visibility": "private"}, {"visibility": "internal"}, {"visibility": None},
    {"disabled": True}, {"disabled": 0}, {"disabled": None},
    {"id": True}, {"id": 0}, {"id": -1}, {"id": 2**63}, {"id": "123"},
    {"owner": None}, {"owner": []}, {"owner": {"login": "Other"}},
    {"name": "Repo.git"}, {"name": "bad?"}, {"name": "bad#"},
    {"name": "bad name"}, {"name": "a/b"}, {"name": None},
    {"full_name": "other/Zelda64Recomp"},
    {"html_url": "http://github.com/Zelda64Recomp/Zelda64Recomp"},
    {"html_url": "https://github.com.evil/Zelda64Recomp/Zelda64Recomp"},
    {"url": "https://api.github.com.evil/repos/Zelda64Recomp/Zelda64Recomp"},
    {"url": "http://api.github.com/repos/Zelda64Recomp/Zelda64Recomp"},
    {"url": "https://secret@api.github.com/repos/Zelda64Recomp/Zelda64Recomp"},
    {"url": "https://api.github.com:443/repos/Zelda64Recomp/Zelda64Recomp"},
    {"url": "https://api.github.com/repos/Other/Zelda64Recomp"},
    {"description": 42}, {"language": []}, {"topics": None}, {"topics": "emulator"},
    {"topics": ["Emulator"]}, {"topics": ["-emulator"]}, {"topics": ["e" * 51]},
    {"topics": ["emulator"] * 21}, {"topics": [None]},
    {"fork": 0}, {"fork": None}, {"archived": "false"}, {"archived": None},
    {"stargazers_count": True}, {"stargazers_count": -1}, {"stargazers_count": None},
    {"pushed_at": 123}, {"pushed_at": "2026-02-30T12:00:00Z"},
    {"pushed_at": "2026-11-13T12:00:00+00:00"}, {"pushed_at": "2026-11-13"},
    {"pushed_at": "2026-11-13T25:00:00Z"},
])
def test_invalid_records_have_generic_errors(changes):
    with pytest.raises(SearchRecordError) as caught:
        parse_github_repository(repository(**changes))
    assert str(caught.value) == "Invalid public repository record."


@pytest.mark.parametrize("key", ["id", "owner", "name", "full_name", "html_url", "url", "private", "visibility", "disabled"])
def test_missing_required_fields(key):
    item = repository()
    del item[key]
    with pytest.raises(SearchRecordError):
        parse_github_repository(item)


@pytest.mark.parametrize("item", [None, [], "secret", 123])
def test_non_objects_are_rejected(item):
    with pytest.raises(SearchRecordError):
        parse_github_repository(item)


def test_missing_optional_fields_are_unknown_not_false():
    item = repository()
    for key in ("description", "topics", "language", "fork", "archived", "stargazers_count", "pushed_at"):
        del item[key]
    result = parse_github_repository(item)
    assert result.fork is result.archived is Tri.UNKNOWN
    assert result.description is result.language is result.stars is result.last_activity is None
    assert result.topics == ()
    explicit = parse_github_repository(repository(fork=False, archived=True, pushed_at=None))
    assert (explicit.fork, explicit.archived) == (Tri.NO, Tri.YES)
    assert parse_github_repository(repository(fork=True)).fork is Tri.YES


def test_optional_boundaries():
    result = parse_github_repository(repository(id=2**63 - 1, stargazers_count=0,
        description=None, language=None, topics=["e" * 50], pushed_at="0001-01-01T00:00:00Z"))
    assert result.repository_id == 2**63 - 1 and result.stars == 0
    assert result.last_activity == "0001-01-01T00:00:00Z"


def test_text_sanitization_removes_hidden_text_and_bounds_output():
    value = "  cafe\u0301\tport\ntext\u202e\u200b\x00\ue000\ud800\u0378\u2028end  "
    assert sanitize_text(value, 100) == "café port text end"
    assert sanitize_text("a" * 281, 280) == "a" * 279 + "…"
    assert sanitize_text("x", 1) == "x"
    assert sanitize_text("xy", 1) == "…"
    result = parse_github_repository(repository(description="x" * 300, language="y" * 60))
    assert result.description == "x" * 279 + "…"
    assert result.language == "y" * 49 + "…"


@pytest.mark.parametrize("kind,topics", [
    (ProjectKind.RECOMP, ("static-recompilation", "recompilation", "recomp", "static-recompiler")),
    (ProjectKind.PORT, ("pc-port", "source-port", "native-port", "game-port")),
    (ProjectKind.DECOMP, ("decompilation", "decomp", "decompiled", "game-decompilation")),
    (ProjectKind.EMULATOR, ("emulator", "emulation", "emulators")),
])
def test_every_exact_topic_has_evidence(kind, topics):
    for topic in topics:
        assert classify_project("Ares", (topic,), None) == (
            Classification(kind, (Evidence(kind, EvidenceSource.TOPIC, topic),)),)
    assert classify_project("Ares", ("not-" + topics[0],), None) == ()


@pytest.mark.parametrize("phrase,kind", [
    ("static recompilation", ProjectKind.RECOMP), ("statically-recompiled", ProjectKind.RECOMP),
    ("RECOMP", ProjectKind.RECOMP), ("recompiled", ProjectKind.RECOMP),
    ("recompilation", ProjectKind.RECOMP), ("PC port", ProjectKind.PORT),
    ("source-ports", ProjectKind.PORT), ("native port", ProjectKind.PORT),
    ("game ports", ProjectKind.PORT), ("decompilation", ProjectKind.DECOMP),
    ("decompiled", ProjectKind.DECOMP), ("decomp", ProjectKind.DECOMP),
    ("emulator", ProjectKind.EMULATOR), ("Emulators", ProjectKind.EMULATOR),
    ("EMULATION", ProjectKind.EMULATOR),
])
def test_description_evidence_is_word_bounded(phrase, kind):
    assert classify_project("Ares", (), f"A {phrase} project.") == (
        Classification(kind, (Evidence(kind, EvidenceSource.DESCRIPTION, phrase),)),)
    assert classify_project("Ares", (), "x" + phrase + "x") == ()


@pytest.mark.parametrize("name,kind", [
    ("Zelda64Recomp", ProjectKind.RECOMP), ("N64Recomp", ProjectKind.RECOMP),
    ("RecompFrontend", ProjectKind.RECOMP), ("sm64decomp", ProjectKind.DECOMP),
    ("game-recompiled", ProjectKind.RECOMP), ("GameDecompilation", ProjectKind.DECOMP),
    ("CoolEmulator", ProjectKind.EMULATOR), ("GameRecompilation", ProjectKind.RECOMP),
])
def test_weak_names(name, kind):
    classifications = classify_project(name, (), None)
    assert len(classifications) == 1 and classifications[0].kind is kind
    assert all(e.kind is kind and e.source is EvidenceSource.NAME for e in classifications[0].evidence)


@pytest.mark.parametrize("name", ["Ares", "Recompile-kernel", "portable", "someport", "emulation-tools"])
def test_names_do_not_invent_stronger_claims(name):
    assert classify_project(name, (), None) == ()


def test_multiple_kinds_and_evidence_order():
    result = classify_project("N64Recomp", ("pc-port", "recomp", "emulator", "decomp"),
                              "A recompilation and source port")
    assert tuple(c.kind for c in result) == tuple(ProjectKind)
    recomp = result[1]
    assert tuple(e.source for e in recomp.evidence) == (
        EvidenceSource.TOPIC, EvidenceSource.DESCRIPTION, EvidenceSource.NAME)
    assert tuple(e.matched for e in recomp.evidence) == ("recomp", "recompilation", "Recomp")
