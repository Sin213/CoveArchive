"""Immutable public repository records and conservative classification hints."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import re
import unicodedata

from covearchive.github.urls import RepositoryTarget, parse_github_url


UNKNOWN = "unknown"


class Tri(str, Enum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class ReleaseStatus(str, Enum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    NONE = "none"


class OfficialStatus(str, Enum):
    UNKNOWN = "unknown"
    OFFICIAL = "official"
    UNOFFICIAL = "unofficial"


class ProjectKind(str, Enum):
    EMULATOR = "emulator"
    RECOMP = "recomp"
    PORT = "port"
    DECOMP = "decomp"


class EvidenceSource(str, Enum):
    TOPIC = "topic"
    DESCRIPTION = "description"
    NAME = "name"


@dataclass(frozen=True)
class Evidence:
    kind: ProjectKind
    source: EvidenceSource
    matched: str


@dataclass(frozen=True)
class Classification:
    kind: ProjectKind
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class SearchResult:
    repository_id: int
    owner: str
    name: str
    url: str
    description: str | None
    topics: tuple[str, ...]
    language: str | None
    fork: Tri
    archived: Tri
    stars: int | None
    last_activity: str | None
    classifications: tuple[Classification, ...]
    provider: str = field(default="github", init=False)
    host: str = field(default="github.com", init=False)
    release_status: ReleaseStatus = ReleaseStatus.UNKNOWN
    official_status: OfficialStatus = OfficialStatus.UNKNOWN
    original_platform: str = UNKNOWN
    host_os: str = UNKNOWN

    @property
    def target(self) -> RepositoryTarget:
        return RepositoryTarget(self.owner, self.name)

    @property
    def kinds(self) -> tuple[ProjectKind, ...]:
        return tuple(classification.kind for classification in self.classifications)


@dataclass(frozen=True)
class SearchPage:
    results: tuple[SearchResult, ...]
    page: int
    per_page: int
    total_count: int
    retrievable_total: int
    incomplete_results: bool
    dropped: int
    duplicates: int
    has_next_page: bool


class SearchRecordError(ValueError):
    def __init__(self):
        super().__init__("Invalid public repository record.")


def sanitize_text(value, limit):
    """Remove invisible controls and bound plain display text, including ellipsis."""
    chars = []
    for char in value:
        category = unicodedata.category(char)
        if category.startswith("Z") or (category == "Cc" and char.isspace()):
            chars.append(" ")
        elif category not in {"Cc", "Cf", "Cs", "Co", "Cn"}:
            chars.append(char)
    text = " ".join(unicodedata.normalize("NFC", "".join(chars)).split())
    if len(text) > limit:
        return text[:max(0, limit - 1)] + "…" if limit else ""
    return text


_TOPICS = {
    ProjectKind.EMULATOR: {"emulator", "emulation", "emulators"},
    ProjectKind.RECOMP: {"static-recompilation", "recompilation", "recomp", "static-recompiler"},
    ProjectKind.PORT: {"pc-port", "source-port", "native-port", "game-port"},
    ProjectKind.DECOMP: {"decompilation", "decomp", "decompiled", "game-decompilation"},
}
_DESCRIPTIONS = {
    ProjectKind.EMULATOR: r"emulators?|emulation",
    ProjectKind.RECOMP: r"static(?:ally)?[ -]recompil\w*|recompilation|recompiled|recomp",
    ProjectKind.PORT: r"(?:pc|source|native|game)[ -]ports?",
    ProjectKind.DECOMP: r"decompilation|decompiled|decomp",
}
_PATTERNS = {kind: re.compile(r"\b(?:" + pattern + r")\b", re.IGNORECASE)
             for kind, pattern in _DESCRIPTIONS.items()}
_NAME_SUFFIXES = {
    ProjectKind.EMULATOR: ("emulator",),
    ProjectKind.RECOMP: ("recomp", "recompiled", "recompilation"),
    ProjectKind.DECOMP: ("decomp", "decompiled", "decompilation"),
}


def classify_project(name, topics, description) -> tuple[Classification, ...]:
    """Record lexical evidence only; this says nothing about releases or playability."""
    # Keep original spelling for evidence, splitting lower/digit-to-upper camel
    # boundaries and acronym-to-word boundaries before tokenizing punctuation.
    split_name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    split_name = re.sub(r"([A-Z])([A-Z][a-z])", r"\1 \2", split_name)
    tokens = "".join(char if char.isalnum() else " " for char in split_name).split()
    classifications = []
    for kind in ProjectKind:
        evidence = [Evidence(kind, EvidenceSource.TOPIC, topic)
                    for topic in topics if topic in _TOPICS[kind]]
        if description:
            evidence.extend(Evidence(kind, EvidenceSource.DESCRIPTION, match.group())
                            for match in _PATTERNS[kind].finditer(description))
        suffixes = _NAME_SUFFIXES.get(kind, ())
        evidence.extend(Evidence(kind, EvidenceSource.NAME, token)
                        for token in tokens if token.casefold().endswith(suffixes))
        if evidence:
            classifications.append(Classification(kind, tuple(dict.fromkeys(evidence))))
    return tuple(classifications)


def _optional_text(item, key, limit):
    value = item.get(key)
    if value is not None and not isinstance(value, str):
        raise SearchRecordError()
    return sanitize_text(value, limit) if value is not None else None


def _tri(item, key):
    if key not in item:
        return Tri.UNKNOWN
    if type(item[key]) is not bool:
        raise SearchRecordError()
    return Tri.YES if item[key] else Tri.NO


def _activity(value):
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value) is None:
        raise SearchRecordError()
    try:
        return datetime.fromisoformat(value).isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        raise SearchRecordError() from None


def parse_github_repository(item) -> SearchResult:
    """Validate a public, enabled repository and its mutually consistent identity."""
    if not isinstance(item, dict):
        raise SearchRecordError()
    repository_id = item.get("id")
    if type(repository_id) is not int or not 1 <= repository_id <= 2**63 - 1:
        raise SearchRecordError()
    if item.get("private") is not False or item.get("visibility") != "public" or item.get("disabled") is not False:
        raise SearchRecordError()
    owner_data, name = item.get("owner"), item.get("name")
    owner = owner_data.get("login") if isinstance(owner_data, dict) else None
    if not isinstance(owner, str) or not isinstance(name, str):
        raise SearchRecordError()
    canonical = f"https://github.com/{owner}/{name}"
    try:
        if parse_github_url(canonical) != RepositoryTarget(owner, name):
            raise SearchRecordError()
    except ValueError:
        raise SearchRecordError() from None
    if (item.get("full_name") != f"{owner}/{name}" or item.get("html_url") != canonical
            or item.get("url") != f"https://api.github.com/repos/{owner}/{name}"):
        raise SearchRecordError()
    topics = item.get("topics", [])
    if (not isinstance(topics, list) or len(topics) > 20
            or any(not isinstance(topic, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", topic) is None
                   for topic in topics)):
        raise SearchRecordError()
    stars = item.get("stargazers_count")
    if "stargazers_count" in item and (type(stars) is not int or stars < 0):
        raise SearchRecordError()
    description = _optional_text(item, "description", 280)
    language = _optional_text(item, "language", 50)
    return SearchResult(repository_id, owner, name, canonical, description, tuple(topics), language,
                        _tri(item, "fork"), _tri(item, "archived"), stars,
                        _activity(item.get("pushed_at")), classify_project(name, topics, description))
