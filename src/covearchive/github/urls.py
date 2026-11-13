"""Classify supported GitHub URLs without contacting GitHub."""

from dataclasses import dataclass
import re
import unicodedata
from urllib.parse import unquote_to_bytes


ERROR_CODES = frozenset(
    {
        "empty",
        "too_long",
        "control_char",
        "scheme",
        "userinfo",
        "port",
        "host",
        "path",
        "owner",
        "repo",
        "unsupported",
    }
)

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zs", "Zl", "Zp"})


class GitHubUrlError(ValueError):
    """A URL validation failure with a stable, read-only error code."""

    def __init__(self, code: str, message: str = "") -> None:
        if not isinstance(code, str) or code not in ERROR_CODES:
            raise ValueError("Unknown GitHub URL error code.")
        self._code = code
        super().__init__("Invalid or unsupported GitHub URL.")

    @property
    def code(self) -> str:
        return self._code


@dataclass(frozen=True)
class RepositoryTarget:
    owner: str
    repo: str

    @property
    def repo_key(self) -> str:
        return self.owner.casefold() + "/" + self.repo.casefold()


@dataclass(frozen=True)
class ReleaseListTarget:
    owner: str
    repo: str

    @property
    def repo_key(self) -> str:
        return self.owner.casefold() + "/" + self.repo.casefold()


@dataclass(frozen=True)
class ReleaseTagTarget:
    owner: str
    repo: str
    tag: str

    @property
    def repo_key(self) -> str:
        return self.owner.casefold() + "/" + self.repo.casefold()


def _has_forbidden_characters(text: str) -> bool:
    return any(
        char.isspace() or unicodedata.category(char) in _FORBIDDEN_CATEGORIES
        for char in text
    )


def parse_github_url(text: str) -> RepositoryTarget | ReleaseListTarget | ReleaseTagTarget:
    """Return a target, rejecting invalid inputs in the documented check order."""
    if not isinstance(text, str):
        raise TypeError("GitHub URL must be a string.")
    text = text.strip(" \t\n\r\f\v")
    if not text:
        raise GitHubUrlError("empty")
    if len(text) > 2048:
        raise GitHubUrlError("too_long")
    if _has_forbidden_characters(text):
        raise GitHubUrlError("control_char")
    if "\\" in text:
        raise GitHubUrlError("path")

    lowered = text.translate(_ASCII_LOWER)
    if lowered.startswith("https://"):
        rest = text[len("https://"):]
    elif lowered.startswith(("github.com/", "www.github.com/")):
        rest = text
    else:
        raise GitHubUrlError("scheme")

    # Inspect the raw authority before parsing paths or discarding queries.
    authority_end = min(
        (index for index, char in enumerate(rest) if char in "/?#"),
        default=len(rest),
    )
    authority = rest[:authority_end]
    if "@" in authority:
        raise GitHubUrlError("userinfo")
    if "[" in authority or "]" in authority:
        raise GitHubUrlError("host")
    if ":" in authority:
        raise GitHubUrlError("port")
    host = authority.translate(_ASCII_LOWER)
    if not authority.isascii() or host not in ("github.com", "www.github.com"):
        raise GitHubUrlError("host")

    path = re.split(r"[?#]", rest[authority_end:], maxsplit=1)[0]
    if not path.startswith("/"):
        raise GitHubUrlError("path")
    path = path.removesuffix("/")
    segments = path[1:].split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise GitHubUrlError("path")

    owner = segments[0]
    if _OWNER.fullmatch(owner) is None:
        raise GitHubUrlError("owner")
    if len(segments) < 2:
        raise GitHubUrlError("repo")
    repo = segments[1].removesuffix(".git")
    if _REPO.fullmatch(repo) is None or repo in (".", ".."):
        raise GitHubUrlError("repo")

    remaining = segments[2:]
    if not remaining:
        return RepositoryTarget(owner, repo)
    if remaining == ["releases"]:
        return ReleaseListTarget(owner, repo)
    if remaining[:2] != ["releases", "tag"]:
        raise GitHubUrlError("unsupported")
    if len(remaining) == 2:
        raise GitHubUrlError("path")

    raw_tag = "/".join(remaining[2:])
    if re.search(r"%(?![0-9A-Fa-f]{2})", raw_tag):
        raise GitHubUrlError("path")
    try:
        tag = unquote_to_bytes(raw_tag).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise GitHubUrlError("path") from None
    if (
        not tag
        or len(tag) > 255
        or _has_forbidden_characters(tag)
        or "\\" in tag
        or any(segment in ("", ".", "..") for segment in tag.split("/"))
    ):
        raise GitHubUrlError("path")
    return ReleaseTagTarget(owner, repo, tag)
