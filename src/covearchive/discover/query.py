"""Build a bounded public search query from untrusted title words."""

from dataclasses import dataclass
import unicodedata
from urllib.parse import quote, urlencode


SEARCH_ENDPOINT = "https://api.github.com/search/repositories"
MAX_RETRIEVABLE = 1000
_KIND_CLAUSES = {
    "all": '(recomp OR recompilation OR decompilation OR "PC port" OR "source port" OR emulator)',
    "recomps": '(recomp OR recompilation OR recompiled OR "static recompilation")',
    "ports": '("PC port" OR "source port" OR "native port" OR "game port")',
    "decomps": '(decompilation OR decomp OR decompiled)',
    "emulators": '(emulator OR emulation)',
}
_FORBIDDEN = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_OPERATORS = frozenset({"AND", "OR", "NOT"})


class SearchQueryError(ValueError):
    """Invalid user input with a stable, non-sensitive reason."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SearchQuery:
    text: str
    kind: str
    sort: str
    page: int
    per_page: int
    q: str
    params: tuple[tuple[str, str | int], ...]
    url: str


def _clean_text(text):
    if not isinstance(text, str):
        raise SearchQueryError("type")
    if len(text) > 256:
        raise SearchQueryError("too_long")
    if any(unicodedata.category(char) in _FORBIDDEN for char in text):
        raise SearchQueryError("control_char")
    normalized = unicodedata.normalize("NFKC", text)
    if any(unicodedata.category(char) in _FORBIDDEN for char in normalized):
        raise SearchQueryError("control_char")
    if not normalized.strip():
        raise SearchQueryError("empty")
    # Syntax punctuation becomes a boundary, including compatibility colons and
    # quotes. Dots, underscores and hyphens have meaning only inside words.
    words = "".join(char if unicodedata.category(char)[0] in "LMN" or char in "._-"
                    else " " for char in normalized).split()
    tokens = [word.strip("-._") for word in words]
    cleaned = " ".join(word for word in tokens if word and word not in _OPERATORS)
    if not cleaned:
        raise SearchQueryError("no_terms")
    if len(cleaned) > 100:
        raise SearchQueryError("too_long")
    return cleaned


def build_search_query(text, kind="all", sort="relevance", *, page=1, per_page=30) -> SearchQuery:
    """Return immutable parameters and a URL pinned to GitHub's search API.

    Forks are excluded by GitHub's default; no fork qualifier is added here.
    Pagination respects the conservative 1,000-result retrieval ceiling.
    """
    cleaned = _clean_text(text)
    if not isinstance(kind, str) or kind not in _KIND_CLAUSES:
        raise SearchQueryError("kind")
    if not isinstance(sort, str) or sort not in ("relevance", "stars", "updated"):
        raise SearchQueryError("sort")
    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise SearchQueryError("per_page")
    if type(page) is not int or page < 1 or page * per_page > MAX_RETRIEVABLE:
        raise SearchQueryError("page")
    q = f"{cleaned} {_KIND_CLAUSES[kind]} in:name,description,topics is:public"
    if len(q) > 256:
        raise SearchQueryError("query_too_long")
    if sum(word in _OPERATORS for word in q.split()) > 5:
        raise SearchQueryError("too_many_operators")
    params = [("q", q)]
    if sort != "relevance":
        params.extend((("sort", sort), ("order", "desc")))
    params.extend((("per_page", per_page), ("page", page)))
    url = SEARCH_ENDPOINT + "?" + urlencode(params, quote_via=quote, safe="")
    return SearchQuery(cleaned, kind, sort, page, per_page, q, tuple(params), url)
