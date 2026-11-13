"""Lazy public interfaces for GitHub release metadata and selection."""

from importlib import import_module


_EXPORTS = {
    name: module
    for module, names in {
        "client": ("DirectSession", "GitHubReleaseClient", "GitHubApiError", "InvalidTargetError", "CredentialedSessionError",
                   "NotFoundError", "AccessDeniedError", "RateLimitError",
                   "TransientServerError", "RequestTimeoutError", "NetworkError",
                   "InvalidResponseError", "PaginationError", "UnsafeRedirectError",
                   "ApiGoneError", "UnexpectedStatusError", "IncompleteScanError"),
        "urls": ("GitHubUrlError", "RepositoryTarget", "ReleaseListTarget",
                 "ReleaseTagTarget", "parse_github_url"),
        "models": ("ReleaseSchemaError", "ReleaseAsset", "Release", "ReleaseCatalog",
                   "parse_asset", "parse_release"),
        "select": ("AssetMatch", "SelectedRelease", "ExcludedRelease", "ReleaseSelection",
                   "select_releases", "select_release_by_tag"),
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
