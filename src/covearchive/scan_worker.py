"""Bounded JSON protocol and Qt-free, synchronous scan subprocess."""

from dataclasses import dataclass
import json
import os
import re
import sys
import threading

from covearchive.github.models import ReleaseCatalog, parse_release
from covearchive.github.urls import GitHubUrlError, RepositoryTarget, ReleaseTagTarget
from covearchive.providers import parse_repository_url, validate_repository

PROTOCOL_VERSION = 2
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_DEADLINE_SECONDS = 180
ERROR_CODES = frozenset("invalid_target credentialed_session not_found unauthorized forbidden rate_limited "
    "server_error timeout network invalid_json invalid_root too_many_items invalid_release response_too_large "
    "bad_pagination unsafe_redirect gone unexpected_status incomplete_scan github_api_error invalid_url "
    "unsupported_target bad_request result_too_large internal_error invalid_content_type".split())
RELEASE_KEYS = frozenset("id tag_name name draft prerelease immutable created_at published_at body assets tarball_url zipball_url".split())
ASSET_KEYS = frozenset("id name size state content_type browser_download_url digest created_at updated_at".split())


class ProtocolError(ValueError):
    """A complete envelope did not satisfy the wire contract."""


@dataclass(frozen=True)
class ScanSuccess:
    request_id: str
    catalog: ReleaseCatalog


@dataclass(frozen=True)
class ScanFailure:
    request_id: str
    code: str
    http_status: int | None = None
    message: str = "Scan failed."
    detail: dict | None = None


def _require(condition):
    if not condition:
        raise ProtocolError("Invalid scan protocol.")


def _keys(obj, keys):
    _require(type(obj) is dict and set(obj) == set(keys))


def _int(value, maximum=2**63):
    _require(type(value) is int and 0 <= value <= maximum)


def _text(value, limit=8192):
    _require(type(value) is str and len(value) <= limit)


def _id(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) is not None)


def _pairs(pairs):
    obj = {}
    for key, value in pairs:
        _require(key not in obj)
        obj[key] = value
    return obj


def _reject(value):
    raise ProtocolError("Non-finite JSON.")


def _load(data, cap):
    _require(type(data) is bytes and 0 < len(data) <= cap and data.endswith(b"\n")
             and b"\n" not in data[:-1] and b"\r" not in data)
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject)
    except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise ProtocolError("Invalid JSON envelope.") from exc


def _dump(obj):
    return (json.dumps(obj, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


def encode_request(request_id, url):
    data = _dump(dict(protocol=PROTOCOL_VERSION, request_id=request_id, action="scan", url=url))
    parse_request(data)
    return data


def parse_request(line):
    obj = _load(line, MAX_REQUEST_BYTES)
    _keys(obj, ("protocol", "request_id", "action", "url"))
    _require(type(obj["protocol"]) is int and obj["protocol"] == PROTOCOL_VERSION and obj["action"] == "scan")
    _id(obj["request_id"]); _text(obj["url"], 2048)
    return obj["request_id"], obj["url"]


def _release_wire(release):
    obj = {key: getattr(release, key) for key in RELEASE_KEYS - {"assets", "body", "tarball_url", "zipball_url"}}
    obj["assets"] = [{key: (asset.original_digest if key == "digest" else getattr(asset, key))
                      for key in ASSET_KEYS} for asset in release.assets]
    return obj


def encode_success(request_id, catalog):
    if not catalog.complete:
        return encode_error(request_id, "incomplete_scan", detail=dict(pages_completed=catalog.pages,
                            reason="incomplete", cause_code=None))
    repository = validate_repository(catalog.repository)
    data = _dump(dict(protocol=PROTOCOL_VERSION, request_id=request_id, status="ok", result=dict(
        owner=repository.owner, repo=repository.repo, provider=repository.provider, host=repository.host, pages=catalog.pages,
        requests=catalog.requests, redirects=catalog.redirects,
        duplicate_release_ids=list(catalog.duplicate_release_ids),
        releases=[_release_wire(release) for release in catalog.releases])))
    if len(data) > MAX_RESPONSE_BYTES:
        return encode_error(request_id, "result_too_large")
    return data


def encode_error(request_id, exc_or_code, **fields):
    code = exc_or_code if isinstance(exc_or_code, str) else getattr(exc_or_code, "code", "internal_error")
    if code not in ERROR_CODES:
        code = "internal_error"
    detail = {}
    if code == "rate_limited":
        detail = {key: getattr(exc_or_code, key, None) for key in
                  ("kind", "retry_after_seconds", "retry_at", "reset_at", "remaining")}
    if code == "incomplete_scan":
        detail = dict(pages_completed=getattr(exc_or_code, "pages_completed", 0),
                      reason=getattr(exc_or_code, "reason", "incomplete")[:200],
                      cause_code=getattr(getattr(exc_or_code, "cause", None), "code", None))
    return _dump(dict(protocol=PROTOCOL_VERSION, request_id=request_id, status="error", error=dict(
        code=code, http_status=fields.get("http_status", getattr(exc_or_code, "status", None)),
        message="Scan failed: " + code, detail=fields.get("detail", detail))))


def _bounded(value):
    if type(value) is str: _text(value)
    elif type(value) is int: _int(value)
    elif type(value) is list:
        _require(len(value) <= 10000)
        for item in value: _bounded(item)
    elif type(value) is dict:
        for item in value.values(): _bounded(item)
    else: _require(value is None or type(value) is bool)


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
            _require(type(item["code"]) is str and item["code"] in ERROR_CODES)
            if item["http_status"] is not None:
                _int(item["http_status"], 599); _require(item["http_status"] >= 100)
            _text(item["message"], 200)
            detail, code = item["detail"], item["code"]
            keys = ("kind", "retry_after_seconds", "retry_at", "reset_at", "remaining") if code == "rate_limited" else (
                ("pages_completed", "reason", "cause_code") if code == "incomplete_scan" else ())
            _keys(detail, keys)
            if code == "rate_limited":
                _require(detail["kind"] in ("primary", "secondary", "too_many_requests"))
                for key in keys[1:]:
                    if detail[key] is not None: _int(detail[key])
            if code == "incomplete_scan":
                _int(detail["pages_completed"]); _text(detail["reason"], 200)
                _require(detail["cause_code"] is None or detail["cause_code"] in ERROR_CODES)
            return ScanFailure(obj["request_id"], code, item["http_status"], item["message"], detail)
        _keys(item, ("owner", "repo", "provider", "host", "pages", "requests", "redirects", "duplicate_release_ids", "releases"))
        _text(item["owner"], 40); _text(item["repo"], 100)
        _text(item["provider"], 16); _text(item["host"], 253)
        target = validate_repository(RepositoryTarget(item["owner"], item["repo"], item["provider"], item["host"]))
        for key in ("pages", "requests", "redirects"): _int(item[key])
        _require(type(item["duplicate_release_ids"]) is list and len(item["duplicate_release_ids"]) <= 10000)
        for value in item["duplicate_release_ids"]: _int(value)
        _require(type(item["releases"]) is list and len(item["releases"]) <= 10000)
        releases = []
        for release in item["releases"]:
            _require(type(release) is dict and set(release) <= RELEASE_KEYS)
            _require(all(release.get(key) is None for key in ("body", "tarball_url", "zipball_url")))
            _require(type(release.get("assets")) is list and len(release["assets"]) <= 1000)
            for asset in release["assets"]:
                _require(type(asset) is dict and set(asset) <= ASSET_KEYS)
            _bounded(release)
            releases.append(parse_release(release))
        catalog = ReleaseCatalog(target, tuple(releases), item["pages"], item["requests"], item["redirects"],
                                 tuple(item["duplicate_release_ids"]), "", True)
        return ScanSuccess(obj["request_id"], catalog)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        raise ProtocolError("Invalid response envelope.") from exc


def run_scan(request_id, url, *, client_factory=None):
    # Importing this module must remain possible without requests or Qt installed.
    try:
        target = parse_repository_url(url)
        if isinstance(target, ReleaseTagTarget):
            return json.loads(encode_error(request_id, "unsupported_target"))
        if client_factory is None:
            if target.provider == "codeberg":
                from covearchive.forgejo import ForgejoReleaseClient
                client_factory = ForgejoReleaseClient
            else:
                from covearchive.github.client import GitHubReleaseClient
                client_factory = GitHubReleaseClient
        with client_factory() as client:
            data = encode_success(request_id, client.discover_releases(target))
    except GitHubUrlError:
        data = encode_error(request_id, "invalid_url")
    except Exception as exc:
        data = encode_error(request_id, exc)
        if json.loads(data)["error"]["code"] == "internal_error": _diagnostic(exc)
    return json.loads(data)


def _diagnostic(exc):
    message = str(exc).replace("\r", "\\r").replace("\n", "\\n")[:300]
    if sys.stderr is not None:
        sys.stderr.write(f"{type(exc).__name__}: {message}\n"); sys.stderr.flush()


def start_watchdog(stdin, deadline_seconds, exit_fn):
    """Parent pipe EOF and independent wall deadline both force child exit."""
    fired = threading.Event()
    def leave():
        if not fired.is_set():
            fired.set(); exit_fn(70)
    def eof():
        try:
            while stdin.read(1): pass
        finally: leave()
    def deadline():
        if not fired.wait(deadline_seconds): leave()
    for target in (eof, deadline):
        threading.Thread(target=target, daemon=True).start()


def main(stdin=None, stdout=None, *, deadline_seconds=DEFAULT_DEADLINE_SECONDS, exit_fn=os._exit):
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    try:
        request_id, url = parse_request(stdin.readline(MAX_REQUEST_BYTES + 1))
        start_watchdog(stdin, deadline_seconds + 5, exit_fn)
        stdout.write(_dump(run_scan(request_id, url))); stdout.flush()
        return 0
    except Exception as exc:
        _diagnostic(exc)
        return 2
