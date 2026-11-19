"""Anonymous, public-only GitHub repository search over the release transport."""

import json
import time

from covearchive.github.client import (
    DEFAULT_TIMEOUT, CredentialedSessionError, GitHubApiError, GitHubReleaseClient,
    InvalidResponseError, UnsafeRedirectError,
)
from covearchive.jsondepth import check_json_depth

from .models import SearchPage, SearchRecordError, parse_github_repository
from .query import MAX_RETRIEVABLE, SEARCH_ENDPOINT, build_search_query


class InsecureTransportError(GitHubApiError):
    code = "insecure_transport"


def _tls_verified(session):
    verify = getattr(session, "verify", True)
    return verify is True or (isinstance(verify, str) and bool(verify))


def _object_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise InvalidResponseError("duplicate_key")
        obj[key] = value
    return obj


def _invalid_constant(value):
    raise ValueError("Non-finite JSON constant.")


def _search_json(body):
    try:
        text = body.decode("utf-8")
        check_json_depth(text)
        data = json.loads(text, object_pairs_hook=_object_pairs, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, OverflowError, RecursionError):
        raise InvalidResponseError("invalid_json") from None
    if not isinstance(data, dict):
        raise InvalidResponseError("invalid_root")
    total = data.get("total_count")
    if (type(total) is not int or not 0 <= total <= 10**9
            or type(data.get("incomplete_results")) is not bool
            or not isinstance(data.get("items"), list)):
        raise InvalidResponseError("invalid_envelope")
    return data


class GitHubSearchClient(GitHubReleaseClient):
    """One logical page per search; headers never determine pagination or URLs."""

    def __init__(self, session=None, *, clock=time.time, sleep=time.sleep,
                 timeout=DEFAULT_TIMEOUT, max_retries=2, max_response_bytes=4 * 1024 * 1024):
        super().__init__(session, clock=clock, sleep=sleep, timeout=timeout, auth=None,
                         max_pages=1, max_requests=max_retries + 1, max_retries=max_retries,
                         max_redirects=0, max_response_bytes=max_response_bytes)
        assert self.auth is None

    @staticmethod
    def _redirect_url(location, current):
        raise UnsafeRedirectError()

    def _validate_transport(self, url, response=None):
        if not isinstance(url, str) or not url.startswith(SEARCH_ENDPOINT + "?") or "#" in url:
            raise UnsafeRedirectError()
        if not _tls_verified(self.session):
            raise InsecureTransportError()
        if response is None and getattr(self.session, "params", None):
            raise CredentialedSessionError()
        # Real DirectSession responses expose the prepared URL. Check it too,
        # so an injected transport cannot substitute an endpoint or query.
        if response is not None and getattr(response, "url", url) != url:
            raise UnsafeRedirectError()

    def search(self, text, kind="all", sort="relevance", *, page=1, per_page=30) -> SearchPage:
        query = build_search_query(text, kind, sort, page=page, per_page=per_page)
        if not _tls_verified(self.session):
            raise InsecureTransportError()
        if getattr(self.session, "params", None):
            raise CredentialedSessionError()
        _, _, body = self._page(query.url, dict(pages=0, requests=0, redirects=0))
        data = _search_json(body)
        items, total = data["items"], data["total_count"]
        if len(items) > per_page:
            raise InvalidResponseError("too_many_items")
        if len(items) > total:
            raise InvalidResponseError("invalid_envelope")
        results, ids, keys, duplicates = [], set(), set(), 0
        for item in items:
            try:
                result = parse_github_repository(item)
            except SearchRecordError:
                continue
            key = result.target.repo_key
            if result.repository_id in ids or key in keys:
                duplicates += 1
                continue
            ids.add(result.repository_id)
            keys.add(key)
            results.append(result)
        if items and not results:
            raise InvalidResponseError("no_valid_items")
        retrievable = min(total, MAX_RETRIEVABLE)
        return SearchPage(tuple(results), page, per_page, total, retrievable,
                          data["incomplete_results"], len(items) - len(results), duplicates,
                          page * per_page < retrievable
                          and (page + 1) * per_page <= MAX_RETRIEVABLE)
