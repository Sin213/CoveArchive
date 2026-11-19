"""Versioned, bounded JSON lines for one anonymous, Qt-free search per process."""

from dataclasses import dataclass
import json
import os
import re
import sys

from covearchive.scan_worker import start_watchdog

from .models import SearchPage, Tri, parse_github_repository, sanitize_text
from .query import MAX_RETRIEVABLE, SearchQueryError, build_search_query

PROTOCOL_VERSION = 1
# Each Unicode scalar can need a 12-byte surrogate pair in ensure_ascii JSON.
# 256 * 12 + 256 admits the full text plus the fixed envelope and 64-bit page.
MAX_REQUEST_BYTES = 256 * 12 + 256
# 100 records: <=330 display scalars * 12, 20 ASCII topics * 50, identities,
# dates, counters and JSON punctuation fit comfortably below one MiB.
MAX_RESPONSE_BYTES = 1024 * 1024
# Three (10s connect + 30s read) attempts and <=8s backoff, with 22s margin.
# Reads can trickle indefinitely, so the wall watchdog remains essential.
DEFAULT_DEADLINE_SECONDS = 150
ERROR_CODES = frozenset("rate_limited timeout network server_error forbidden unauthorized not_found gone "
    "unexpected_status github_api_error unsafe_redirect invalid_json invalid_root invalid_envelope "
    "too_many_items no_valid_items response_too_large bad_pagination invalid_content_type insecure_transport "
    "credentialed_session invalid_search result_too_large bad_request internal_error invalid_target "
    "incomplete_scan invalid_release invalid_url unsupported_target repository_mismatch asset_changed duplicate_key".split())
KINDS = frozenset(("all", "recomps", "ports", "decomps", "emulators"))
SORTS = frozenset(("relevance", "stars", "updated"))
REQUEST_KEYS = frozenset("protocol request_id action text kind sort page per_page".split())
PAGE_KEYS = frozenset("page per_page total_count retrievable_total incomplete_results dropped duplicates has_next_page results".split())
RESULT_KEYS = frozenset("repository_id owner name url description topics language fork archived stars last_activity".split())
RATE_KEYS = ("kind", "retry_after_seconds", "retry_at", "reset_at", "remaining")


class ProtocolError(ValueError):
    """An envelope did not satisfy the Discover wire contract."""


@dataclass(frozen=True)
class DiscoverSuccess:
    request_id: str
    page: SearchPage


@dataclass(frozen=True)
class DiscoverFailure:
    request_id: str
    code: str
    http_status: int | None = None
    message: str = "Search failed."
    detail: dict | None = None


def _require(condition):
    if not condition:
        raise ProtocolError("Invalid Discover protocol.")


def _keys(obj, keys):
    _require(type(obj) is dict and set(obj) == set(keys))


def _int(value, maximum=2**63 - 1, minimum=0):
    _require(type(value) is int and minimum <= value <= maximum)


def _id(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) is not None)


def _pairs(pairs):
    obj = {}
    for key, value in pairs:
        _require(key not in obj)
        obj[key] = value
    return obj


def _reject_constant(value):
    raise ProtocolError("Non-finite JSON.")


def _load(data, cap):
    _require(type(data) is bytes and 0 < len(data) <= cap and data.endswith(b"\n")
             and b"\n" not in data[:-1] and b"\r" not in data)
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise ProtocolError("Invalid JSON envelope.") from exc


def _dump(obj):
    try:
        return (json.dumps(obj, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("ascii")
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ProtocolError("Invalid JSON envelope.") from exc


def encode_request(request_id, text, kind="all", sort="relevance", page=1, per_page=30):
    data = _dump(dict(protocol=PROTOCOL_VERSION, request_id=request_id, action="search", text=text,
                      kind=kind, sort=sort, page=page, per_page=per_page))
    parse_request(data)
    return data


def parse_request(line):
    obj = _load(line, MAX_REQUEST_BYTES)
    _keys(obj, REQUEST_KEYS)
    _require(type(obj["protocol"]) is int and obj["protocol"] == PROTOCOL_VERSION and obj["action"] == "search")
    _id(obj["request_id"])
    _require(type(obj["text"]) is str and len(obj["text"]) <= 256)
    _require(type(obj["kind"]) is str and obj["kind"] in KINDS)
    _require(type(obj["sort"]) is str and obj["sort"] in SORTS)
    _int(obj["page"], minimum=1)
    _int(obj["per_page"], 100, 1)
    # Empty/control/no-terms text, normalized length and the retrieval ceiling
    # are semantic query errors, handled offline after trusting the envelope.
    return tuple(obj[key] for key in ("request_id", "text", "kind", "sort", "page", "per_page"))


def encode_success(request_id, page):
    result = {key: getattr(page, key) for key in PAGE_KEYS - {"results"}}
    result["results"] = [{key: getattr(item, key) for key in RESULT_KEYS} for item in page.results]
    data = _dump(dict(protocol=PROTOCOL_VERSION, request_id=request_id, status="ok", result=result))
    if len(data) > MAX_RESPONSE_BYTES:
        return encode_error(request_id, "result_too_large")
    # Validate the outbound record too; a defective client cannot cross the boundary.
    decode_response(data, request_id)
    return data


def _rate_detail(detail):
    _keys(detail, RATE_KEYS)
    _require(type(detail["kind"]) is str and detail["kind"] in ("primary", "secondary", "too_many_requests"))
    for key in RATE_KEYS[1:]:
        if detail[key] is not None:
            _int(detail[key])


def encode_error(request_id, exc_or_code):
    code = exc_or_code if isinstance(exc_or_code, str) else getattr(exc_or_code, "code", "internal_error")
    if type(code) is not str or code not in ERROR_CODES:
        code = "internal_error"
    status = getattr(exc_or_code, "status", None)
    detail = {}
    try:
        if status is not None:
            _int(status, 599, 100)
        if code == "rate_limited":
            detail = {key: getattr(exc_or_code, key, None) for key in RATE_KEYS}
            _rate_detail(detail)
    except ProtocolError:
        code, status, detail = "internal_error", None, {}
    return _dump(dict(protocol=PROTOCOL_VERSION, request_id=request_id, status="error", error=dict(
        code=code, http_status=status, message="Search failed: " + code, detail=detail)))


def _record(item):
    _keys(item, RESULT_KEYS)
    for key, limit in (("description", 280), ("language", 50)):
        value = item[key]
        _require(value is None or (type(value) is str and len(value) <= limit and sanitize_text(value, limit) == value))
    _require(type(item["owner"]) is str and type(item["name"]) is str and type(item["url"]) is str)
    # Reuse DS1's exact identity, id, timestamp, topic, star and classification
    # validation; only the already-sanitized public display fields cross IPC.
    upstream = dict(id=item["repository_id"], owner={"login": item["owner"]}, name=item["name"],
                    full_name=f"{item['owner']}/{item['name']}", html_url=item["url"],
                    url=f"https://api.github.com/repos/{item['owner']}/{item['name']}",
                    private=False, visibility="public", disabled=False, description=item["description"],
                    language=item["language"], topics=item["topics"], pushed_at=item["last_activity"])
    if item["stars"] is not None:
        upstream["stargazers_count"] = item["stars"]
    for key in ("fork", "archived"):
        _require(type(item[key]) is str and item[key] in tuple(t.value for t in Tri))
        if item[key] != Tri.UNKNOWN.value:
            upstream[key] = item[key] == Tri.YES.value
    return parse_github_repository(upstream)


def decode_response(data, expected_request_id):
    try:
        obj = _load(data, MAX_RESPONSE_BYTES)
        _require(type(obj) is dict and obj.get("status") in ("ok", "error"))
        body = "result" if obj["status"] == "ok" else "error"
        _keys(obj, ("protocol", "request_id", "status", body))
        _require(type(obj["protocol"]) is int and obj["protocol"] == PROTOCOL_VERSION)
        _id(obj["request_id"])
        _require(obj["request_id"] == expected_request_id)
        item = obj[body]
        if body == "error":
            _keys(item, ("code", "http_status", "message", "detail"))
            code = item["code"]
            _require(type(code) is str and code in ERROR_CODES)
            _require(item["message"] == "Search failed: " + code)
            if item["http_status"] is not None:
                _int(item["http_status"], 599, 100)
            if code == "rate_limited":
                _rate_detail(item["detail"])
            else:
                _keys(item["detail"], ())
            return DiscoverFailure(obj["request_id"], code, item["http_status"], item["message"], item["detail"])
        _keys(item, PAGE_KEYS)
        _int(item["page"], MAX_RETRIEVABLE, 1)
        _int(item["per_page"], 100, 1)
        _require(item["page"] * item["per_page"] <= MAX_RETRIEVABLE)
        _int(item["total_count"], 10**9)
        _int(item["retrievable_total"], MAX_RETRIEVABLE)
        _require(item["retrievable_total"] == min(item["total_count"], MAX_RETRIEVABLE))
        for key in ("incomplete_results", "has_next_page"):
            _require(type(item[key]) is bool)
        for key in ("dropped", "duplicates"):
            _int(item[key], item["per_page"])
        _require(type(item["results"]) is list and len(item["results"]) <= item["per_page"])
        results = tuple(_record(record) for record in item["results"])
        _require(len({r.repository_id for r in results}) == len(results)
                 and len({r.target.repo_key for r in results}) == len(results))
        _require(item["duplicates"] <= item["dropped"]
                 and len(results) + item["dropped"] <= min(item["per_page"], item["total_count"])
                 and (bool(results) or item["dropped"] == 0))
        expected_next = (item["page"] * item["per_page"] < item["retrievable_total"]
                         and (item["page"] + 1) * item["per_page"] <= MAX_RETRIEVABLE)
        _require(item["has_next_page"] == expected_next)
        page = SearchPage(results, *(item[key] for key in ("page", "per_page", "total_count", "retrievable_total",
                          "incomplete_results", "dropped", "duplicates", "has_next_page")))
        return DiscoverSuccess(obj["request_id"], page)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        raise ProtocolError("Invalid response envelope.") from exc


def _diagnostic(exc, code=None):
    # Never call str(exc): upstream errors can contain headers, URLs or tokens.
    name = re.sub(r"[^a-zA-Z0-9_]", "", type(exc).__name__)[:80]
    code = code if type(code) is str and code in ERROR_CODES else None
    if sys.stderr is not None:
        sys.stderr.write(name + (" " + code if code else "") + "\n")
        sys.stderr.flush()


def run_search(request_id, text, kind="all", sort="relevance", page=1, per_page=30, *, client_factory=None):
    try:
        build_search_query(text, kind, sort, page=page, per_page=per_page)
        if client_factory is None:
            from .github_search import GitHubSearchClient
            client_factory = GitHubSearchClient
        with client_factory() as client:
            result = client.search(text, kind, sort, page=page, per_page=per_page)
        data = encode_success(request_id, result)
    except SearchQueryError:
        data = encode_error(request_id, "invalid_search")
    except Exception as exc:
        data = encode_error(request_id, exc)
        _diagnostic(exc, json.loads(data)["error"]["code"])
    return json.loads(data)


def main(stdin=None, stdout=None, *, deadline_seconds=DEFAULT_DEADLINE_SECONDS, exit_fn=os._exit):
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    try:
        request = parse_request(stdin.readline(MAX_REQUEST_BYTES + 1))
        # Parent normally enforces 150s; this independent fallback grants 5s
        # slack. Keeping stdin open is the parent's liveness signal.
        start_watchdog(stdin, deadline_seconds + 5, exit_fn)
        stdout.write(_dump(run_search(*request)))
        stdout.flush()
        return 0
    except Exception as exc:
        _diagnostic(exc)
        return 2
