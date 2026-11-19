"""Lazy public interfaces for anonymous repository discovery."""

from importlib import import_module


_EXPORTS = {
    name: module
    for module, names in {
        "models": ("UNKNOWN", "Tri", "ReleaseStatus", "OfficialStatus", "ProjectKind", "EvidenceSource",
                   "Evidence", "Classification", "SearchResult", "SearchPage", "SearchRecordError",
                   "parse_github_repository", "sanitize_text", "classify_project"),
        "query": ("SEARCH_ENDPOINT", "SearchQuery", "SearchQueryError", "build_search_query"),
        "github_search": ("GitHubSearchClient", "InsecureTransportError"),
    }.items()
    for name in names
}
__all__ = tuple(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{_EXPORTS[name]}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
