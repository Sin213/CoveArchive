"""Offline provider identity and URL contracts."""

from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from covearchive.github.urls import (
    GitHubUrlError, ReleaseListTarget, RepositoryTarget, parse_github_url,
)
from covearchive.providers import (
    canonical_url, display_name, parse_repository_url, project_components,
    validate_repository,
)

def test_release_list_defaults_keep_github_identity():
    target = ReleaseListTarget("Alice", "Repo")
    assert getattr(target, "provider", None) == "github"
    assert getattr(target, "host", None) == "github.com"
    assert target.repo_key == "alice/repo"

@pytest.mark.parametrize("text, owner, repo, releases", [
    ("https://codeberg.org/Alice/Repo", "Alice", "Repo", False),
    ("codeberg.org/Alice/Repo/", "Alice", "Repo", False),
    ("HTTPS://CODEBERG.ORG/Alice/Repo", "Alice", "Repo", False),
    (" \tcodeberg.org/Alice/Repo\r\n", "Alice", "Repo", False),
    ("https://codeberg.org/a._-b/.hidden", "a._-b", ".hidden", False),
    ("https://codeberg.org/o/r/releases", "o", "r", True),
    ("codeberg.org/o/r/releases/", "o", "r", True),
    ("https://codeberg.org/o/r/releases?q=x#top", "o", "r", True),
    ("https://codeberg.org/o/r?tab=readme#top", "o", "r", False),
    ("https://codeberg.org/" + "a" * 40 + "/" + "r" * 100,
     "a" * 40, "r" * 100, False),
])
def test_codeberg_targets(text, owner, repo, releases):
    target_type = ReleaseListTarget if releases else RepositoryTarget
    target = parse_repository_url(text)
    assert type(target) is target_type
    assert target == target_type(owner, repo, "codeberg", "codeberg.org")
    assert target.repo_key == f"codeberg.org/{owner}/{repo}".casefold()

@pytest.mark.parametrize("text, code", [ (text,code) for code,texts in [
    ('empty', ["", " \t\n", ]),
    ('too_long', ["x" * 2049, ]),
    ('control_char', ["https://codeberg.org/o/r\x00", "https://codeberg.org/o/\nr",
        "https://codeberg.org/o/r\u202e", "https://codeberg.org/o/r\u200b", "\u00a0https://codeberg.org/o/r",
        "https://codeberg.org/o/r\ud800", ]),
    ('path', ["https://codeberg.org/o/r?x=\\", "https://codeberg.org", "https://codeberg.org/",
        "https://codeberg.org/o//r", "https://codeberg.org/o/r//", "https://codeberg.org/./r",
        "https://codeberg.org/o/..", "https://codeberg.org/o/r/tree/../main", ]),
    ('scheme', ["http://codeberg.org/o/r", "ftp://codeberg.org/o/r", "//codeberg.org/o/r", "codeberg.org", ]),
    ('userinfo', ["https://user:secret@codeberg.org/o/r", "https://codeberg.org@evil.com/o/r",
        "https://evil.com@codeberg.org/o/r", ]),
    ('port', ["https://codeberg.org:443/o/r", "https://codeberg.org:/o/r", ]),
    ('host', ["https://www.codeberg.org/o/r", "https://codeberg.org.evil.com/o/r", "https://codeberg.org./o/r",
        "https://codeberg.org%2eevil.com/o/r", "https://%63odeberg.org/o/r",
        "https://c\u043ed eberg.org/o/r".replace(" ", ""), "https://xn--codeberg-9jg.org/o/r",
        "https://gitlab.com/o/r", "https://[::1]/o/r", ]),
    ('owner', ["https://codeberg.org/-owner/r", "https://codeberg.org/owner_/r",
        "https://codeberg.org/" + "a" * 41 + "/r", "https://codeberg.org/%41lice/r", ]),
    ('repo', ["https://codeberg.org/o", "https://codeberg.org/o/r.git", "https://codeberg.org/o/r.GIT",
        "https://codeberg.org/o/r.wiki", "https://codeberg.org/o/r.WIKI", "https://codeberg.org/o/r.atom",
        "https://codeberg.org/o/r.ATOM", "https://codeberg.org/o/" + "r" * 101,
        "https://codeberg.org/o/r%2Frepo", ]),
    ('unsupported', ["https://codeberg.org/o/r/releases/tag/v1", "https://codeberg.org/o/r/releases/latest",
        "https://codeberg.org/o/r/releases/download/v1/a.zip", "https://codeberg.org/o/r/Releases",
        "https://codeberg.org/o/r/issues", ]),
] for text in texts ])
def test_rejects_unsafe_or_unsupported_urls_without_echoing_input(text, code):
    with pytest.raises(GitHubUrlError) as caught:
        parse_repository_url(text)
    assert caught.value.code == code
    assert 0 < len(str(caught.value)) <= 100
    assert text not in str(caught.value) if text else True
    assert "secret" not in str(caught.value)

@pytest.mark.parametrize("text", [
    "https://github.com/Alice/Repo", "www.github.com/o/r.git/releases",
    "GITHUB.COM/o/r", "https://github.com/o/r/releases/tag/v1%2Ftest",
])
def test_github_dispatch_preserves_existing_parser(text):
    assert parse_repository_url(text) == parse_github_url(text)
    assert type(parse_repository_url(text)) is type(parse_github_url(text))

@pytest.mark.parametrize("text", [
    "http://github.com/o/r", "https://user@github.com/o/r",
    "https://github.com:443/o/r", "https://github.com/o//r",
    "https://github.com/-o/r", "https://github.com/o/r/issues",
])
def test_github_dispatch_preserves_error_codes(text):
    with pytest.raises(GitHubUrlError) as old:
        parse_github_url(text)
    with pytest.raises(GitHubUrlError) as new:
        parse_repository_url(text)
    assert new.value.code == old.value.code

@pytest.mark.parametrize("value", [None, 123, b"codeberg.org/o/r", [], {}, True])
def test_non_string_urls(value):
    with pytest.raises(TypeError):
        parse_repository_url(value)

def test_codeberg_length_boundary_and_validation_order():
    prefix = "https://codeberg.org/o/r?"
    text = prefix + "x" * (2048 - len(prefix))
    assert parse_repository_url(text) == RepositoryTarget("o", "r", "codeberg", "codeberg.org")
    for text, code in [(text + "x", "too_long"),
                       ("http://user@codeberg.org:443/o/r\x00", "control_char"),
                       ("https://user@[::1]:443/o/r", "userinfo"),
                       ("https://evil.com:443/o/r", "port")]:
        with pytest.raises(GitHubUrlError) as caught:
            parse_repository_url(text)
        assert caught.value.code == code

@pytest.mark.parametrize("provider, host, components, label", [
    ("github", "github.com", ("Alice", "Repo"), "GitHub"),
    ("codeberg", "codeberg.org", ("codeberg.org", "Alice", "Repo"), "Codeberg"),
])
def test_validated_identity_helpers(provider, host, components, label):
    repository = RepositoryTarget("Alice", "Repo", provider, host)
    assert validate_repository(repository) == repository
    assert canonical_url(repository) == f"https://{host}/Alice/Repo"
    assert project_components(repository) == components
    assert display_name(provider) == label

@pytest.mark.parametrize("provider,host,changes", [("github","github.com",c) for c in [
    {"provider": "gitlab"}, {"provider": "GitHub"}, {"provider": None},
    {"host": "codeberg.org"}, {"host": "www.github.com"},
    {"host": "github.com:443"}, {"host": "GITHUB.COM"},
    {"owner": "-bad"}, {"owner": "o/r"}, {"owner": " o"},
    {"owner": 1}, {"repo": "r.git"}, {"repo": "r?query"},
    {"repo": "r/releases"}, {"repo": "r#fragment"}, {"repo": None},
]]+[("codeberg","codeberg.org",c) for c in [
    {"host": "github.com"}, {"host": "evil.com"},
    {"host": "CODEBERG.ORG"}, {"host": "www.codeberg.org"},
    {"repo": "r.git"}, {"repo": "r.wiki"}, {"repo": "r.atom"},
    {"owner": "bad_"}, {"provider": "github"},
]])
def test_identity_helpers_reject_noncanonical_or_spoofed(provider,host,changes):
    repository = replace(RepositoryTarget("o", "r", provider, host), **changes)
    for helper in (validate_repository, canonical_url, project_components):
        with pytest.raises(ValueError): helper(repository)

@pytest.mark.parametrize("repository", [None, "https://github.com/o/r", ReleaseListTarget("o", "r")])
def test_helpers_require_a_repository_target(repository):
    for helper in (validate_repository, canonical_url, project_components):
        with pytest.raises(ValueError):
            helper(repository)

@pytest.mark.parametrize("provider", ["gitlab", "GitHub", "", None])
def test_unknown_display_provider_rejected(provider):
    with pytest.raises(ValueError):
        display_name(provider)

def test_providers_import_without_network_or_qt_dependencies():
    source = Path(__file__).resolve().parents[1] / "src"
    script = ("import sys; sys.path.insert(0, sys.argv[1]);"
              "sys.modules['requests']=None; sys.modules['PySide6']=None;"
              "from covearchive.providers import parse_repository_url;"
              "assert parse_repository_url('codeberg.org/o/r').provider == 'codeberg';"
              "assert sys.modules['requests'] is None; assert sys.modules['PySide6'] is None")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(source)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
