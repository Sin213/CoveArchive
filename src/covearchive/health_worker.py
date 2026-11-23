"""Anonymous, Qt-free Health worker. Only bounded detection evidence crosses IPC.

No catalogs, download URLs, titles, bodies, credentials, or exception text leave
this worker. Release tags are bounded data for plain-text display, never markup.
The parent supervises both the remote scan and local audit in the same process.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
import re
import os
from pathlib import Path
import sys

from covearchive.archive import availability as a
from covearchive.archive.availability import check_repository
from covearchive.archive.health import CategoryCounts, audit_project
from covearchive.github.urls import RepositoryTarget
from covearchive.library.model import Repository
from covearchive.providers import parse_repository_url, validate_repository
from covearchive.scan_worker import (
    ProtocolError, ScanFailure, _dump, _load, _require, _keys, _id, _int, _text, start_watchdog,
)

PROTOCOL_VERSION = 2
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
REASONS = frozenset(a._FAILURE_STATES) | a._INVALID_CODES | frozenset((
    'ok', 'unknown', 'unsupported', 'incomplete_scan', 'invalid_repository',
))
COUNTS = tuple(CategoryCounts.__dataclass_fields__)
COMPARISON_COUNTS = ('expected_assets', 'verified_assets', 'unarchived_assets',
                     'damaged_assets', 'missing_assets')

# Closed image of availability._scan_availability for a valid requested repo.
# Direct failures use _FAILURE_STATES / invalid_response. Incomplete failures
# use reachable after positive pages, or the failure mapping before any page;
# their unknown cause is normalized to incomplete_scan (never unknown).
_CAUSES = frozenset(a._FAILURE_STATES) | a._INVALID_CODES | {'incomplete_scan'}
AVAILABILITY_SHAPES = frozenset({
    ('reachable', 'ok', True), ('reachable', 'incomplete_scan', False),
    ('not_checked', 'unknown', False), ('invalid_response', 'unknown', False),
    ('unsupported', 'unsupported', False),
    *((state, reason, False) for reason, state in a._FAILURE_STATES.items()),
    *(('invalid_response', reason, False) for reason in a._INVALID_CODES),
    *(('reachable', reason, False) for reason in _CAUSES),
    *((a._FAILURE_STATES.get(reason, 'not_checked'), reason, False) for reason in _CAUSES),
})


def valid_availability(value):
    """Shared wire/queue guard; only direct rate limits may carry retry hints."""
    try:
        return (type(value.listing_complete) is bool
                and (value.state, value.reason, value.listing_complete) in AVAILABILITY_SHAPES
                and all(item is None or (type(item) is int and 0 <= item <= 2**63)
                        for item in (value.retry_at, value.retry_after))
                and (value.state == 'rate_limited' or (value.retry_at is None and value.retry_after is None)))
    except (AttributeError, TypeError):
        return False


# Closed branches of availability._compare. The count predicates describe
# (expected, verified, unarchived, damaged, missing); local identity and audit
# requirements are checked together below, including assess's incomplete-scan
# downgrade of not_archived/partial into inconclusive.
COMPARISON_SHAPES = {
    'verified': ('match', lambda n, v, u, d, m: n > 0 and v == n and u == d == m == 0),
    'partial': ('match', lambda n, v, u, d, m: n > 0 and u > 0 and v + u == n and d == m == 0),
    'not_archived': ('none', lambda n, v, u, d, m: n > 0 and u == n and v == d == m == 0),
    'damaged': ('match', lambda n, v, u, d, m: n > 0 and d > 0),
    'missing_files': ('match', lambda n, v, u, d, m: n > 0 and d == 0 and m > 0),
    'changed_upstream': ('changed', lambda n, v, u, d, m: n > 0 and v < n and d == m == 0),
    'tag_reused': ('reused', lambda n, v, u, d, m: n > 0 and v == u == d == m == 0),
    'no_archivable_assets': ('none', lambda n, v, u, d, m: n == v == u == d == m == 0),
    'archive_unavailable': ('none', lambda n, v, u, d, m: n > 0 and v == u == d == m == 0),
    'unverified': ('match', lambda n, v, u, d, m: n > 0 and v + u < n and d == m == 0),
    'inconclusive': ('optional', lambda n, v, u, d, m: n > 0 and v + u < n and d == m == 0),
}
CATALOG_LINE_SHAPES = frozenset({('none', False, 'inconclusive'), ('ambiguous', False, 'inconclusive'),
                               *(('found', True, state) for state in COMPARISON_SHAPES)})


@dataclass(frozen=True)
class PreparedStatus(a.RepositoryStatus):
    # Repository-qualified by the envelope. No filenames or download metadata.
    verified_asset_ids: tuple[tuple[int, int], ...] = ()


def _plain(value, limit=8192):
    _text(value, limit)
    _require(not any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value))


def encode_request(request_id, repository, root, *, prepare=False):
    data = _dump(dict(protocol=PROTOCOL_VERSION, request_id=request_id, action='prepare_update' if prepare else 'health',
                      provider=repository.provider, url=repository.url, root=root))
    parse_request(data)
    return data


def parse_request(data, *, with_action=False):
    try:
        obj = _load(data, MAX_REQUEST_BYTES)
        _keys(obj, ('protocol', 'request_id', 'action', 'provider', 'url', 'root'))
        _require(type(obj['protocol']) is int and obj['protocol'] == PROTOCOL_VERSION
                 and obj['action'] in ('health', 'prepare_update'))
        _id(obj['request_id']); _plain(obj['url'], 2048); _plain(obj['provider'], 16)
        _plain(obj['root'], 4096)
        _require(not obj['root'] or Path(obj['root']).is_absolute())
        repository = Repository(obj['provider'], obj['url'])
        _require(repository.url == obj['url'])
        result = (obj['request_id'], repository, obj['root'])
        return result + (obj['action'] == 'prepare_update',) if with_action else result
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ProtocolError('Invalid Health request.') from exc


def _line_wire(line):
    release = line.release
    return dict(state=line.state, release=None if release is None else dict(
        release_id=release.release_id, tag=release.tag, published_at=release.published_at,
        prerelease=release.prerelease, total_asset_count=release.total_asset_count,
        archivable_assets=[asdict(asset) for asset in release.archivable_assets]),
        comparison={key: getattr(line.comparison, key) for key in
                    ('state', *COMPARISON_COUNTS, 'local_release_id', 'local_tag')})


def _response_wire(request_id, identity, status):
    return dict(protocol=PROTOCOL_VERSION, request_id=request_id, identity=identity,
        verified_asset_ids=list(status.verified_asset_ids) if isinstance(status, PreparedStatus) else None,
        status=dict(repository=asdict(status.repository) if status.repository else None,
                    availability={key: getattr(status.availability, key) for key in
                                  ('state', 'reason', 'listing_complete', 'retry_at', 'retry_after')},
                    stable=_line_wire(status.stable), prerelease=_line_wire(status.prerelease),
                    archive=asdict(status.archive) if status.archive else None))


def encode_response(request_id, identity, status):
    data = _dump(_response_wire(request_id, identity, status))
    if len(data) > MAX_RESPONSE_BYTES:
        # Never truncate a verified-ID inventory or asset list into apparently
        # complete evidence. Drop the entire projection inside the worker;
        # the GUI receives only a small, non-queueable Inconclusive result.
        status = a.assess(status.repository, ScanFailure('', 'result_too_large'), None)
        data = _dump(_response_wire(request_id, identity, status))
    decode_response(data, request_id, identity)
    return data


def _release_id(value):
    # N1 Health detection currently supports GitHub only.
    _int(value); _require(value > 0)


def _decode_line(obj, prerelease):
    _keys(obj, ('state', 'release', 'comparison'))
    _require(obj['state'] in ('found', 'none', 'ambiguous', 'not_checked', 'unsupported'))
    release = obj['release']
    if release is not None:
        _keys(release, ('release_id', 'tag', 'published_at', 'prerelease', 'total_asset_count', 'archivable_assets'))
        _release_id(release['release_id']); _plain(release['tag']); _plain(release['published_at'], 100)
        published = datetime.fromisoformat(release['published_at'])
        _require(published.tzinfo is not None and published.utcoffset() is not None)
        _require(type(release['prerelease']) is bool and release['prerelease'] == prerelease)
        _int(release['total_asset_count'], 1000)
        assets = release['archivable_assets']
        _require(type(assets) is list and len(assets) <= release['total_asset_count'])
        for asset in assets:
            _keys(asset, ('asset_id', 'size', 'sha256')); _release_id(asset['asset_id']); _int(asset['size'])
            _require(asset['sha256'] is None or (type(asset['sha256']) is str
                     and re.fullmatch('[0-9a-f]{64}', asset['sha256']) is not None))
        _require(len({asset['asset_id'] for asset in assets}) == len(assets))
        release = a.UpstreamRelease(**(release | {'archivable_assets': tuple(a.UpstreamAsset(**asset) for asset in assets)}), name=None)
    _require((obj['state'] == 'found') == (release is not None))
    comparison = obj['comparison']
    _keys(comparison, ('state', *COMPARISON_COUNTS, 'local_release_id', 'local_tag'))
    _require(comparison['state'] in a._COMPARISON_MESSAGES)
    for key in COMPARISON_COUNTS: _int(comparison[key], 10000)
    if comparison['local_release_id'] is not None: _release_id(comparison['local_release_id'])
    if comparison['local_tag'] is not None: _plain(comparison['local_tag'])
    return a.ReleaseLine(obj['state'], release, a.Comparison(
        **comparison, message=a._COMPARISON_MESSAGES[comparison['state']]))


def _validate_evidence(repository, availability, stable, prerelease, archive, pairs):
    """Validate a whole N1b projection, rather than independent field enums."""
    _require(valid_availability(availability))
    lines = (stable, prerelease)
    if availability.state == 'unsupported':
        _require(repository is None and archive is None and pairs is None)
        shapes = {('unsupported', False, 'unsupported')}
    else:
        _require(repository is not None and repository.provider == 'github')
        # assess runs _latest for BOTH lines only with a reachable ScanSuccess.
        # A reachable incomplete ScanFailure still leaves BOTH not_checked.
        catalog = (availability.state == 'reachable' and availability.reason in ('ok', 'incomplete_scan')
                   and any(line.state != 'not_checked' for line in lines))
        if availability.state == 'reachable' and availability.reason == 'ok': catalog = True
        shapes = CATALOG_LINE_SHAPES if catalog else {('not_checked', False, 'inconclusive')}
    if archive is not None:
        counts = archive.counts
        expected_status = ('attention' if any((counts.missing, counts.damaged, counts.unsafe, counts.untrusted))
                           else 'unverified' if counts.unverified or not archive.complete else 'healthy')
        # health._Audit.add marks unverified/untrusted findings incomplete;
        # findings/directory limits also record an unverified observation.
        _require(archive.status == expected_status and (not archive.truncated or not archive.complete))
        _require(not (counts.unverified or counts.untrusted) or not archive.complete)
        _require(not archive.truncated or counts.unverified > 0)
    releases = [line.release for line in lines if line.release is not None]
    _require(len({r.release_id for r in releases}) == len(releases))
    verified = 0
    for line in lines:
        release, comparison = line.release, line.comparison
        _require((line.state, release is not None, comparison.state) in shapes)
        n, v, u, d, m = (getattr(comparison, key) for key in COMPARISON_COUNTS)
        _require((comparison.local_release_id is None) == (comparison.local_tag is None))
        if release is None:
            _require(n == v == u == d == m == 0 and comparison.local_release_id is None)
            continue
        state = comparison.state
        local, count_shape = COMPARISON_SHAPES[state]
        _require(n == len(release.archivable_assets) <= release.total_asset_count)
        _require(all(count <= n for count in (v, u, d, m)) and v + u <= n and count_shape(n, v, u, d, m))
        if local == 'none': _require(comparison.local_release_id is None)
        elif local == 'reused':
            _require(comparison.local_release_id is not None and comparison.local_release_id != release.release_id
                     and comparison.local_tag == release.tag)
        elif local == 'optional' and comparison.local_release_id is None:
            _require(v == u == 0)
        else:
            _require(comparison.local_release_id == release.release_id)
            if local == 'changed' and comparison.local_tag != release.tag: _require(v == u == 0)
            else: _require(comparison.local_tag == release.tag)
        if state not in ('no_archivable_assets', 'archive_unavailable', 'inconclusive'):
            _require(archive is not None)
        if state in ('not_archived', 'partial'): _require(availability.listing_complete)
        if state == 'damaged': _require(archive.counts.damaged > 0)
        if state == 'missing_files': _require(archive.counts.missing > 0)
        if state == 'unverified': _require(any((archive.counts.unverified, archive.counts.unsafe, archive.counts.untrusted)))
        if state == 'inconclusive':
            if archive is None: _require(comparison.local_release_id is None and v == u == 0)
            else: _require(not archive.complete or not availability.listing_complete)
        verified += v
    _require(verified <= (archive.counts.verified if archive is not None else 0))
    if pairs is not None:
        _require(archive is not None and len(set(pairs)) == len(pairs) == archive.counts.verified)
        for line in lines:
            if line.release is None: continue
            ids = {asset.asset_id for asset in line.release.archivable_assets}
            matched = sum(rid == line.release.release_id and aid in ids for rid, aid in pairs)
            _require(line.comparison.verified_assets <= matched)
            if line.comparison.state in ('verified', 'partial', 'not_archived'):
                _require(line.comparison.verified_assets == matched)


def decode_response(data, request_id, identity):
    try:
        obj = _load(data, MAX_RESPONSE_BYTES)
        _keys(obj, ('protocol', 'request_id', 'identity', 'status', 'verified_asset_ids'))
        _require(type(obj['protocol']) is int and obj['protocol'] == PROTOCOL_VERSION)
        _id(obj['request_id']); _require(obj['request_id'] == request_id and obj['identity'] == identity)
        _plain(obj['identity'], 2300)
        status = obj['status']
        _keys(status, ('repository', 'availability', 'stable', 'prerelease', 'archive'))
        repository = status['repository']
        if repository is not None:
            _keys(repository, ('owner', 'repo', 'provider', 'host', 'project_id'))
            repository = validate_repository(RepositoryTarget(**repository))
            _require(repository.provider == 'github' and 'github:' + repository.repo_key == identity)
        availability = status['availability']
        _keys(availability, ('state', 'reason', 'listing_complete', 'retry_at', 'retry_after'))
        _require(availability['state'] in a._AVAILABILITY_MESSAGES and availability['reason'] in REASONS)
        _require(type(availability['listing_complete']) is bool)
        for key in ('retry_at', 'retry_after'):
            if availability[key] is not None: _int(availability[key])
        _require(repository is not None or availability['state'] == 'unsupported')
        availability = a.Availability(**availability, message=a._AVAILABILITY_MESSAGES[availability['state']])
        stable = _decode_line(status['stable'], False)
        prerelease = _decode_line(status['prerelease'], True)
        archive = status['archive']
        if archive is not None:
            _keys(archive, ('status', 'complete', 'truncated', 'counts'))
            _require(archive['status'] in ('healthy', 'attention', 'unverified'))
            _require(type(archive['complete']) is bool and type(archive['truncated']) is bool)
            _require(not archive['truncated'] or not archive['complete'])
            _keys(archive['counts'], COUNTS)
            for value in archive['counts'].values(): _int(value)
            archive = a.ArchiveSummary(**(archive | {'counts': CategoryCounts(**archive['counts'])}))
        pairs = obj['verified_asset_ids']
        if pairs is not None:
            _require(type(pairs) is list and len(pairs) <= 10000 and archive is not None)
            for pair in pairs:
                _require(type(pair) is list and len(pair) == 2)
                for value in pair: _release_id(value)
            pairs = tuple(tuple(pair) for pair in pairs)
        _validate_evidence(repository, availability, stable, prerelease, archive, pairs)
        if pairs is not None:
            return PreparedStatus(repository, availability, stable, prerelease, archive, pairs)
        return a.RepositoryStatus(repository, availability, stable, prerelease, archive)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError) as exc:
        raise ProtocolError('Invalid Health response.') from exc


def unsupported():
    line = a.ReleaseLine('unsupported', None, a.Comparison('unsupported', a._COMPARISON_MESSAGES['unsupported']))
    return a.RepositoryStatus(None, a.Availability('unsupported', 'unsupported',
                              a._AVAILABILITY_MESSAGES['unsupported']), line, line, None)


def prepare_status(status, report):
    """Second bounded, read-only audit identifies bytes the queue must skip.

    Keep N1b's public check_repository API intact. If evidence changed between
    its audit and this inventory, discard the upstream comparison and fail closed.
    Both audits run inside the same deadline-supervised child, never in Qt.
    """
    summary = a.ArchiveSummary(report.status, report.complete, report.truncated, report.counts)
    same = (status.repository is not None and report.repository.provider == status.repository.provider
            and report.repository.repo_key == status.repository.repo_key and summary == status.archive)
    expected = {(line.release.release_id, asset.asset_id): asset
                for line in (status.stable, status.prerelease) if line.release is not None
                for asset in line.release.archivable_assets}
    for verified in report.verified_assets:
        asset = expected.get((verified.release_id, verified.asset_id))
        if asset is not None and (asset.size != verified.size or (asset.sha256 is not None and asset.sha256 != verified.sha256)):
            same = False
    if not same: status = a.assess(report.repository, None, report)
    pairs = tuple(sorted({(asset.release_id, asset.asset_id) for asset in report.verified_assets}))
    return PreparedStatus(status.repository, status.availability, status.stable, status.prerelease, status.archive, pairs)


def run_check(request_id, repository, root, *, prepare=False):
    if repository.provider != 'github': status = unsupported()
    else:
        target = parse_repository_url(repository.url)
        try: status = check_repository(root, target)
        except Exception: status = a.assess(target, None, None)
        if prepare: status = prepare_status(status, audit_project(root, target))
    return encode_response(request_id, repository.identity, status)


def main(stdin=None, stdout=None, *, deadline_seconds=180, exit_fn=os._exit):
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    try:
        request_id, repository, root, prepare = parse_request(stdin.readline(MAX_REQUEST_BYTES + 1), with_action=True)
        start_watchdog(stdin, deadline_seconds + 5, exit_fn)
        stdout.write(run_check(request_id, repository, root, prepare=prepare)); stdout.flush()
        return 0
    except Exception:
        # Never echo remote data, archive paths, or exceptions to the parent.
        return 2
