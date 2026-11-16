"""Behavioral contract for GitHub URL classification; no network access."""

from dataclasses import FrozenInstanceError

import pytest

from covearchive.github.urls import (
    ERROR_CODES,
    GitHubUrlError,
    ReleaseListTarget,
    ReleaseTagTarget,
    RepositoryTarget,
    parse_github_url,
)


ACCEPTED = [
    ("https://github.com/Alice/Repo", RepositoryTarget("Alice", "Repo")),
    ("https://github.com/o/r/", RepositoryTarget("o", "r")),
    ("https://github.com/o/r/releases", ReleaseListTarget("o", "r")),
    ("https://github.com/o/r/releases/", ReleaseListTarget("o", "r")),
    ("https://github.com/o/r/releases/tag/v1", ReleaseTagTarget("o", "r", "v1")),
    ("https://github.com/o/r/releases/tag/v1/", ReleaseTagTarget("o", "r", "v1")),
    ("github.com/Alice/Repo", RepositoryTarget("Alice", "Repo")),
    ("www.github.com/Alice/Repo", RepositoryTarget("Alice", "Repo")),
    ("GITHUB.COM/Alice/Repo", RepositoryTarget("Alice", "Repo")),
    ("WWW.GITHUB.COM/Alice/Repo", RepositoryTarget("Alice", "Repo")),
    ("HTTPS://GITHUB.COM/Alice/Repo", RepositoryTarget("Alice", "Repo")),
    ("hTtPs://WwW.GitHub.Com/Alice/Repo", RepositoryTarget("Alice", "Repo")),
    ("https://www.github.com/o/r", RepositoryTarget("o", "r")),
    ("https://github.com/o/r.git", RepositoryTarget("o", "r")),
    ("https://github.com/o/r.git/releases", ReleaseListTarget("o", "r")),
    ("https://github.com/o/r.GIT", RepositoryTarget("o", "r.GIT")),
    ("https://github.com/o/r.git.git", RepositoryTarget("o", "r.git")),
    ("https://github.com/o/r?tab=readme#top", RepositoryTarget("o", "r")),
    ("https://github.com/o/r#top?ignored", RepositoryTarget("o", "r")),
    ("https://github.com/o/r?/../", RepositoryTarget("o", "r")),
    ("https://github.com/o/r/releases?q=x#top", ReleaseListTarget("o", "r")),
    (
        "https://github.com/o/r/releases/tag/v1?download=1#asset",
        ReleaseTagTarget("o", "r", "v1"),
    ),
    (
        "https://github.com/o/r/releases/tag/release/v1.0",
        ReleaseTagTarget("o", "r", "release/v1.0"),
    ),
    (
        "https://github.com/o/r/releases/tag/release%2Fv1.0",
        ReleaseTagTarget("o", "r", "release/v1.0"),
    ),
    (
        "https://github.com/o/r/releases/tag/%76%31",
        ReleaseTagTarget("o", "r", "v1"),
    ),
    (
        "https://github.com/o/r/releases/tag/%E6%97%A5%E6%9C%AC",
        ReleaseTagTarget("o", "r", "日本"),
    ),
    ("https://github.com/o/r/releases/tag/café", ReleaseTagTarget("o", "r", "café")),
    (" \t\n\r\f\vhttps://github.com/o/r\v\f\r\n\t ", RepositoryTarget("o", "r")),
    ("https://github.com/a-b/R_e.p-o", RepositoryTarget("a-b", "R_e.p-o")),
    ("https://github.com/o/.hidden", RepositoryTarget("o", ".hidden")),
    ("https://github.com/" + "a" * 39 + "/r", RepositoryTarget("a" * 39, "r")),
    ("https://github.com/o/" + "r" * 100, RepositoryTarget("o", "r" * 100)),
    (
        "https://github.com/o/" + "r" * 100 + ".git",
        RepositoryTarget("o", "r" * 100),
    ),
    (
        "https://github.com/o/r/releases/tag/" + "v" * 255,
        ReleaseTagTarget("o", "r", "v" * 255),
    ),
    (
        "https://github.com/o/r/releases/tag/a%252Fb",
        ReleaseTagTarget("o", "r", "a%2Fb"),
    ),
]


@pytest.mark.parametrize("text, expected", ACCEPTED)
def test_accepted_urls(text, expected):
    actual = parse_github_url(text)
    assert type(actual) is type(expected)
    assert actual == expected
    assert actual.repo_key == expected.owner.casefold() + "/" + expected.repo.casefold()


# Each row pins the first failing validation step, not merely rejection.
INVALID = [
    ("", "empty"),
    (" \t\n\r\f\v", "empty"),
    ("x" * 2049, "too_long"),
    ("https://github.com/o/r\x00", "control_char"),
    ("https://github.com/o/\nr", "control_char"),
    ("https://github.com/o/\tr", "control_char"),
    ("https://github.com/o/r\x7f", "control_char"),
    ("https://github.com/o/r\u202e", "control_char"),
    ("https://github.com/o/r\u2066", "control_char"),
    ("https://github.com/o/r\u200e", "control_char"),
    ("https://github.com/o/r\u200f", "control_char"),
    ("https://github.com/o/r\u061c", "control_char"),
    ("https://github.com/o/r\u200b", "control_char"),
    ("\u00a0https://github.com/o/r", "control_char"),
    ("https://github.com/o/r\u00a0", "control_char"),
    ("https://github.com/o/r\u2028", "control_char"),
    ("https://github.com/o/r\u2029", "control_char"),
    ("https://github.com/o/r\ud800", "control_char"),
    ("https://github.com/o/r x", "control_char"),
    ("http://github.com/o/r", "scheme"),
    ("HTTP://github.com/o/r", "scheme"),
    ("ftp://github.com/o/r", "scheme"),
    ("javascript:github.com/o/r", "scheme"),
    ("git@github.com:o/r", "scheme"),
    ("example.com/o/r", "scheme"),
    ("o/r", "scheme"),
    ("https:/github.com/o/r", "scheme"),
    ("github.com", "scheme"),
    ("https://user@github.com/o/r", "userinfo"),
    ("https://github.com@evil.com/o/r", "userinfo"),
    ("https://user:secret@github.com/o/r", "userinfo"),
    ("https://github.com:443/o/r", "port"),
    ("https://github.com:80/o/r", "port"),
    ("https://github.com:/o/r", "port"),
    ("https://gіthub.com/o/r", "host"),
    ("https://ｇｉｔｈｕｂ.com/o/r", "host"),
    ("https://github.com.evil.com/o/r", "host"),
    ("https://evilgithub.com/o/r", "host"),
    ("https://notgithub.com/o/r", "host"),
    ("https://api.github.com/o/r", "host"),
    ("https://gist.github.com/o/r", "host"),
    ("https://github.com./o/r", "host"),
    ("https://%67ithub.com/o/r", "host"),
    ("https://[::1]/o/r", "host"),
    ("https://github.com]/o/r", "host"),
    ("https:///o/r", "host"),
    ("https://github.com", "path"),
    ("https://github.com?o/r", "path"),
    ("https://github.com/", "path"),
    ("https://github.com/o//r", "path"),
    ("https://github.com/o/r//", "path"),
    ("https://github.com/./r", "path"),
    ("https://github.com/o/..", "path"),
    ("https://github.com/o/r/tree/../main", "path"),
    ("https://github.com/o\\r", "path"),
    ("https://github.com/o/r?x=\\", "path"),
    ("https://github.com/%41lice/r", "owner"),
    ("https://github.com/café/r", "owner"),
    ("https://github.com/α/r", "owner"),
    ("https://github.com/_owner/r", "owner"),
    ("https://github.com/a.b/r", "owner"),
    ("https://github.com/" + "a" * 40 + "/r", "owner"),
    ("https://github.com/-owner/r", "owner"),
    ("https://github.com/owner-/r", "owner"),
    ("https://github.com/o", "repo"),
    ("https://github.com/o/", "repo"),
    ("https://github.com/o/repo%2e", "repo"),
    ("https://github.com/o/café", "repo"),
    ("https://github.com/o/α", "repo"),
    ("https://github.com/o/" + "r" * 101, "repo"),
    ("https://github.com/o/.git", "repo"),
    ("https://github.com/o/..git", "repo"),
    ("https://github.com/o/...git", "repo"),
    ("https://github.com/o/r/tree/main", "unsupported"),
    ("https://github.com/o/r/issues", "unsupported"),
    ("https://github.com/o/r/pull/1", "unsupported"),
    ("https://github.com/o/r/releases/latest", "unsupported"),
    ("https://github.com/o/r/releases/download/v1/a.zip", "unsupported"),
    ("https://github.com/o/r/Releases", "unsupported"),
    ("https://github.com/o/r/releases/Tag/v1", "unsupported"),
    ("https://github.com/o/r/wiki", "unsupported"),
    ("https://github.com/o/r/releases/tag", "path"),
    ("https://github.com/o/r/releases/tag/", "path"),
    ("https://github.com/o/r/releases/tag/%ff", "path"),
    ("https://github.com/o/r/releases/tag/%c0%af", "path"),
    ("https://github.com/o/r/releases/tag/%ed%a0%80", "path"),
    ("https://github.com/o/r/releases/tag/%zz", "path"),
    ("https://github.com/o/r/releases/tag/%", "path"),
    ("https://github.com/o/r/releases/tag/%2", "path"),
    ("https://github.com/o/r/releases/tag/" + "v" * 256, "path"),
    ("https://github.com/o/r/releases/tag/%00", "path"),
    ("https://github.com/o/r/releases/tag/%0a", "path"),
    ("https://github.com/o/r/releases/tag/%5c", "path"),
    ("https://github.com/o/r/releases/tag/%e2%80%ae", "path"),
    ("https://github.com/o/r/releases/tag/%e2%80%8b", "path"),
    ("https://github.com/o/r/releases/tag/%c2%a0", "path"),
    ("https://github.com/o/r/releases/tag/..", "path"),
    ("https://github.com/o/r/releases/tag/%2e%2e", "path"),
    ("https://github.com/o/r/releases/tag/a/%2E/b", "path"),
    ("https://github.com/o/r/releases/tag/%20", "path"),
    ("https://github.com/o/r/releases/tag/%2f", "path"),
    ("https://github.com/o/r/releases/tag/a%2f%2fb", "path"),
]


@pytest.mark.parametrize("text, code", INVALID)
def test_invalid_urls_report_exact_code(text, code):
    with pytest.raises(GitHubUrlError) as caught:
        parse_github_url(text)
    assert caught.value.code == code
    assert str(caught.value)
    assert len(str(caught.value)) <= 100
    if text:
        assert text not in str(caught.value)


@pytest.mark.parametrize(
    "text, code",
    [
        (" " + "x" * 2049 + " ", "too_long"),
        ("\x00" + "x" * 2048, "too_long"),
        ("http://user@evil.com/o/r\x00", "control_char"),
        ("http://evil.com/o\\r", "path"),
        ("http://user@github.com:443/o/r", "scheme"),
        ("https://user@[::1]:443/o/r", "userinfo"),
        ("https://[::1]:443/o/r", "host"),
        ("https://evil.com:443/o/r", "port"),
        ("https://evil.com/%41lice/r", "host"),
        ("https://github.com/%41lice//bad", "path"),
        ("https://github.com/%41lice/repo%2e/tree", "owner"),
        ("https://github.com/o/repo%2e/tree", "repo"),
        ("https://github.com/o/r/tree/%zz", "unsupported"),
        ("https://github.com/o/r/releases/tag/v1?x=\x00", "control_char"),
        ("http://evil.com/o\x00\\r", "control_char"),
        ("https://evil.com/o//r", "host"),
        ("https://github.com/-owner", "owner"),
        ("http://github.com\\o\\r", "path"),
        ("https://u@github.com:443/o/r", "userinfo"),
        ("https://evil.com/../x", "host"),
        ("https://github.com/../r", "path"),
        ("https://github.com/o%41//r", "path"),
        ("https://github.com/-o/r/issues", "owner"),
        ("https://github.com/o/%/issues", "repo"),
        ("https://github.com/o/r/issues/%ff", "unsupported"),
        ("https://github.com/o/r#a\\b", "path"),
        ("https://github.com/" + "a" * 2040 + "\x00", "too_long"),
    ],
)
def test_validation_order(text, code):
    with pytest.raises(GitHubUrlError) as caught:
        parse_github_url(text)
    assert caught.value.code == code


def test_length_boundary_counts_stripped_input_including_query():
    prefix = "https://github.com/o/r?"
    text = prefix + "x" * (2048 - len(prefix))
    assert len(text) == 2048
    assert parse_github_url(text) == RepositoryTarget("o", "r")
    assert parse_github_url(" \t" + text + "\r\n") == RepositoryTarget("o", "r")
    with pytest.raises(GitHubUrlError) as caught:
        parse_github_url(text + "x")
    assert caught.value.code == "too_long"


@pytest.mark.parametrize(
    "raw_tag, expected_tag",
    [
        ("%76" * 255, "v" * 255),
        ("é" * 255, "é" * 255),
        ("%C3%A9" * 255, "é" * 255),
        ("%76" * 256, None),
        ("é" * 256, None),
        ("%C3%A9" * 256, None),
    ],
    ids=[
        "encoded-ascii-255", "raw-unicode-255", "encoded-unicode-255",
        "encoded-ascii-256", "raw-unicode-256", "encoded-unicode-256",
    ],
)
def test_tag_length_counts_decoded_characters(raw_tag, expected_tag):
    text = "https://github.com/o/r/releases/tag/" + raw_tag
    if expected_tag is None:
        with pytest.raises(GitHubUrlError) as caught:
            parse_github_url(text)
        assert caught.value.code == "path"
    else:
        target = parse_github_url(text)
        assert target == ReleaseTagTarget("o", "r", expected_tag)
        assert target.tag == expected_tag
        assert len(target.tag) == 255


@pytest.mark.parametrize("value", [None, 123, b"github.com/o/r", [], {}, True])
def test_non_string_input_raises_type_error(value):
    with pytest.raises(TypeError):
        parse_github_url(value)


@pytest.mark.parametrize(
    "target",
    [
        RepositoryTarget("ALIce", "REpo"),
        ReleaseListTarget("ALIce", "REpo"),
        ReleaseTagTarget("ALIce", "REpo", "Release/V1"),
    ],
)
def test_targets_preserve_case_have_repo_key_and_are_frozen(target):
    assert target.owner == "ALIce"
    assert target.repo == "REpo"
    assert target.repo_key == "alice/repo"
    with pytest.raises(FrozenInstanceError):
        target.owner = "other"
    with pytest.raises(FrozenInstanceError):
        target.repo = "other"
    if isinstance(target, ReleaseTagTarget):
        assert target.tag == "Release/V1"
        with pytest.raises(FrozenInstanceError):
            target.tag = "other"


def test_error_registry_is_exact_and_all_codes_have_reachable_examples():
    expected = {
        "empty", "too_long", "control_char", "scheme", "userinfo", "port",
        "host", "path", "owner", "repo", "unsupported",
    }
    assert isinstance(ERROR_CODES, frozenset)
    assert ERROR_CODES == expected
    assert {code for _, code in INVALID} == expected
    reached = set()
    for text, expected_code in INVALID:
        with pytest.raises(GitHubUrlError) as caught:
            parse_github_url(text)
        assert caught.value.code == expected_code
        reached.add(caught.value.code)
    assert reached == expected


@pytest.mark.parametrize("code", sorted(ERROR_CODES))
def test_error_code_is_read_only_and_message_never_echoes_input(code):
    secret = "https://user:private-token@github.com/o/r"
    error = GitHubUrlError(code, secret)
    assert isinstance(error, ValueError)
    assert error.code == code
    assert 0 < len(str(error)) <= 100
    assert secret not in str(error)
    assert "private-token" not in str(error)
    assert str(error) == str(GitHubUrlError(code, "different raw input"))
    with pytest.raises(AttributeError):
        error.code = "host"
    assert error.code == code


@pytest.mark.parametrize("code", ["unknown", "", "HOST"])
def test_unknown_error_codes_raise_value_error(code):
    with pytest.raises(ValueError):
        GitHubUrlError(code)


def test_repository_target_defaults_keep_github_identity():
    target = RepositoryTarget("Alice", "Repo")
    assert getattr(target, "provider", None) == "github"
    assert getattr(target, "host", None) == "github.com"
    assert target.repo_key == "alice/repo"


@pytest.mark.parametrize("target_type", [RepositoryTarget, ReleaseListTarget])
def test_provider_targets_separate_keys_equality_and_preserve_case(target_type):
    github = target_type("ALIce", "REpo")
    codeberg = target_type("ALIce", "REpo", "codeberg", "codeberg.org")
    assert github != codeberg
    assert github.repo_key == "alice/repo"
    assert codeberg.repo_key == "codeberg.org/alice/repo"
    assert codeberg.owner == "ALIce" and codeberg.repo == "REpo"
    assert codeberg != target_type("ALIce", "REpo", "github", "codeberg.org")
    assert codeberg != target_type("ALIce", "REpo", "codeberg", "other.org")
    assert target_type("ALIce", "REpo", "codeberg", "CODEBERG.ORG").repo_key == codeberg.repo_key
    with pytest.raises(FrozenInstanceError):
        codeberg.provider = "github"
    with pytest.raises(FrozenInstanceError):
        codeberg.host = "github.com"
