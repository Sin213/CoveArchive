"""Pure URL dispatch and canonical identities for the supported public forges."""

import re

from covearchive.github.urls import (
    GitHubUrlError, ReleaseListTarget, ReleaseTagTarget, RepositoryTarget,
    _ASCII_LOWER, _has_forbidden_characters, parse_github_url,
)


_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,38}[A-Za-z0-9])?")
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
_HOSTS = {"github": "github.com", "codeberg": "codeberg.org", "gitlab": "gitlab.com"}


class RepositoryUrlError(GitHubUrlError):
    """Provider-neutral validation failure, compatible with GitHub URL errors."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.args = ("Invalid or unsupported repository URL.",)


def parse_repository_url(text: str) -> RepositoryTarget | ReleaseListTarget | ReleaseTagTarget:
    """Dispatch GitHub URLs unchanged; accept Codeberg and GitLab release lists."""
    if not isinstance(text, str):
        raise TypeError("Repository URL must be a string.")
    text = text.strip(" \t\n\r\f\v")
    if not text:
        raise RepositoryUrlError("empty")
    if len(text) > 2048:
        raise RepositoryUrlError("too_long")
    if _has_forbidden_characters(text):
        raise RepositoryUrlError("control_char")
    if "\\" in text:
        raise RepositoryUrlError("path")

    lowered = text.translate(_ASCII_LOWER)
    if lowered.startswith("https://"):
        rest = text[len("https://"):]
    elif lowered.startswith(("github.com/", "www.github.com/", "codeberg.org/", "gitlab.com/")):
        rest = text
    else:
        raise RepositoryUrlError("scheme")

    authority_end = min(
        (index for index, char in enumerate(rest) if char in "/?#"),
        default=len(rest),
    )
    authority = rest[:authority_end]
    if "@" in authority:
        raise RepositoryUrlError("userinfo")
    if "[" in authority or "]" in authority:
        raise RepositoryUrlError("host")
    if ":" in authority:
        raise RepositoryUrlError("port")
    host = authority.translate(_ASCII_LOWER)
    if not authority.isascii():
        raise RepositoryUrlError("host")
    if host in ("github.com", "www.github.com"):
        return parse_github_url(text)
    if host == "gitlab.com":
        path = re.split(r"[?#]", rest[authority_end:], maxsplit=1)[0]
        segments = path.removesuffix("/")[1:].split("/") if path.startswith("/") else []
        if "-" in segments and segments.index("-") >= 2 and segments[segments.index("-"):] != ["-", "releases"]:
            raise RepositoryUrlError("unsupported")
        releases = segments[-2:] == ["-", "releases"]
        if releases:
            segments = segments[:-2]
        if not 2 <= len(segments) <= 21 or any(
                re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,254}", s) is None
                or s in (".", "..") or s.casefold().endswith((".git", ".atom")) for s in segments):
            raise RepositoryUrlError("path")
        kind = ReleaseListTarget if releases else RepositoryTarget
        return kind("/".join(segments[:-1]), segments[-1], "gitlab", host)
    if host != "codeberg.org":
        raise RepositoryUrlError("host")

    path = re.split(r"[?#]", rest[authority_end:], maxsplit=1)[0]
    if not path.startswith("/"):
        raise RepositoryUrlError("path")
    segments = path.removesuffix("/")[1:].split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise RepositoryUrlError("path")
    owner = segments[0]
    if _OWNER.fullmatch(owner) is None:
        raise RepositoryUrlError("owner")
    if len(segments) < 2:
        raise RepositoryUrlError("repo")
    repo = segments[1]
    if _REPO.fullmatch(repo) is None or repo.casefold().endswith((".git", ".wiki", ".atom")):
        raise RepositoryUrlError("repo")
    remaining = segments[2:]
    if not remaining:
        return RepositoryTarget(owner, repo, "codeberg", host)
    if remaining == ["releases"]:
        return ReleaseListTarget(owner, repo, "codeberg", host)
    raise RepositoryUrlError("unsupported")


def validate_repository(repository: RepositoryTarget) -> RepositoryTarget:
    """Reparse and require an exact canonical repository identity."""
    if not isinstance(repository, RepositoryTarget):
        raise RepositoryUrlError("unsupported")
    if not isinstance(repository.provider, str) or repository.provider not in _HOSTS:
        raise RepositoryUrlError("unsupported")
    if repository.host != _HOSTS[repository.provider]:
        raise RepositoryUrlError("host")
    if not isinstance(repository.owner, str):
        raise RepositoryUrlError("owner")
    if not isinstance(repository.repo, str):
        raise RepositoryUrlError("repo")
    if repository.project_id is not None and (
            repository.provider != "gitlab" or type(repository.project_id) is not int
            or not 1 <= repository.project_id <= 2**63 - 1):
        raise RepositoryUrlError("path")
    canonical = parse_repository_url(f"https://{repository.host}/{repository.owner}/{repository.repo}")
    if not isinstance(canonical, RepositoryTarget) or (canonical.owner, canonical.repo, canonical.provider, canonical.host) != (repository.owner, repository.repo, repository.provider, repository.host):
        raise RepositoryUrlError("path")
    return repository


def canonical_url(repository: RepositoryTarget) -> str:
    repository = validate_repository(repository)
    return f"https://{repository.host}/{repository.owner}/{repository.repo}"


def display_name(provider: str) -> str:
    if provider == "github":
        return "GitHub"
    if provider == "codeberg":
        return "Codeberg"
    if provider == "gitlab":
        return "GitLab"
    raise RepositoryUrlError("unsupported")


def project_components(repository: RepositoryTarget) -> tuple[str, ...]:
    repository = validate_repository(repository)
    if repository.provider == "github":
        return repository.owner, repository.repo
    return (repository.host, *repository.owner.split("/"), repository.repo)
