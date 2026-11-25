"""Qt-free, anonymous repository relocation worker with a closed IPC schema."""
from dataclasses import asdict
import os
from pathlib import Path

from covearchive.library.model import Repository
from covearchive import relocation as r
from covearchive.scan_worker import (
    ProtocolError, _dump, _load, _keys, _require, _id, _text, start_watchdog,
)
from covearchive.scheduled import _stream

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
# Whether the producer can include recorded history for each inconclusive reason.
INCONCLUSIVE_HISTORY = {
    'not_found': (True,),
    'tracked_alias': (False,), 'state_invalid': (False,), 'state_busy': (False,),
    'state_write': (False,), 'limit': (False,),
    'cancelled': (False, True), 'conflict': (False, True), 'network': (False, True),
    'rate_limited': (False, True), 'invalid_response': (False, True),
}


def encode_request(request_id, repository, path):
    data = _dump(dict(protocol=PROTOCOL_VERSION, request_id=request_id,
                      provider=repository.provider, url=repository.url, path=str(path)))
    parse_request(data)
    return data


def parse_request(data):
    try:
        obj = _load(data, MAX_REQUEST_BYTES)
        _keys(obj, ('protocol', 'request_id', 'provider', 'url', 'path'))
        _require(type(obj['protocol']) is int and obj['protocol'] == PROTOCOL_VERSION)
        _id(obj['request_id']); _text(obj['path'], 4096)
        _require(Path(obj['path']).is_absolute() and '\0' not in obj['path'])
        repo = Repository(obj['provider'], obj['url'])
        _require(repo.url == obj['url'])
        return obj['request_id'], repo, obj['path']
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ProtocolError('Invalid relocation request.') from exc


def encode_response(request_id, repository, result):
    data = _dump(dict(protocol=PROTOCOL_VERSION, request_id=request_id,
                      identity=repository.identity, result=asdict(result)))
    decode_response(data, request_id, repository)
    return data


def decode_response(data, request_id, repository):
    try:
        obj = _load(data, MAX_RESPONSE_BYTES)
        _keys(obj, ('protocol', 'request_id', 'identity', 'result'))
        _require(type(obj['protocol']) is int and obj['protocol'] == PROTOCOL_VERSION)
        _id(obj['request_id'])
        _require(obj['request_id'] == request_id and obj['identity'] == repository.identity)
        result = obj['result']; _keys(result, r.Result.__dataclass_fields__)
        _require(result['requested_url'] == repository.url)
        outcome = r.Outcome(result['outcome'])
        for field in ('repository_id', 'observed_id'):
            if result[field] is not None: r._id(result[field])
        _require(type(result['no_prior_identity']) is bool)
        locations = result['locations']
        _require(type(locations) is list and len(locations) <= r.MAX_LOCATIONS)
        for loc in locations:
            _require(type(loc) is list and len(loc) == 2)
            r._url(loc[0]); r._time(loc[1])
        _require(bool(locations) == (result['repository_id'] is not None))
        if locations:
            r.validate(dict(version=1, bindings=[], identities=[dict(provider='github', host='github.com',
                repository_id=result['repository_id'], locations=[dict(url=url, observed_at=stamp) for url, stamp in locations])]))
        prior_count = result['prior_location_count']
        _require(type(prior_count) is int and 0 <= prior_count <= len(locations))
        verified = result['verified_url']
        if verified is not None:
            r._url(verified)
        if outcome in (r.Outcome.UNCHANGED, r.Outcome.RENAMED, r.Outcome.TRANSFERRED):
            _require(verified is not None and locations and locations[-1][0].casefold() == verified.casefold())
            _require(prior_count == len(locations)
                     or (prior_count == len(locations) - 1 and locations[-1][0] == verified))
            _require(result['observed_id'] is not None and result['observed_id'] == result['repository_id'])
            _require(result['no_prior_identity'] == (prior_count == 0)
                     and result['reason'] == ('ok' if prior_count else 'no_prior_identity'))
            _require(r._move_outcome(r._url(repository.url), r._url(verified), locations[:prior_count]) == outcome)
        else:
            _require(prior_count == len(locations))
        if outcome in (r.Outcome.INCONCLUSIVE, r.Outcome.UNAVAILABLE):
            _require(verified is None and result['observed_id'] is None and not result['no_prior_identity'])
            if outcome == r.Outcome.INCONCLUSIVE:
                _require(bool(locations) in INCONCLUSIVE_HISTORY.get(result['reason'], ()))
            else: _require(result['reason'] == 'not_found')
        if outcome == r.Outcome.URL_REUSED:
            _require(result['repository_id'] is not None and result['observed_id'] is not None
                     and result['repository_id'] != result['observed_id']
                     and prior_count > 0 and not result['no_prior_identity']
                     and result['reason'] == ('ok' if verified is not None else 'not_found')
                     and (verified is None or r._url(verified).repo_key != r._url(repository.url).repo_key))
        if outcome == r.Outcome.UNSUPPORTED:
            _require(repository.provider != 'github' and not locations and verified is None
                     and result['repository_id'] is None and result['observed_id'] is None
                     and result['reason'] == 'unsupported' and not result['no_prior_identity'])
        else: _require(repository.provider == 'github')
        return r.Result(**(result | dict(outcome=outcome, locations=tuple(tuple(loc) for loc in locations))))
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError) as exc:
        raise ProtocolError('Invalid relocation response.') from exc


def main(stdin=None, stdout=None, *, deadline_seconds=30, exit_fn=os._exit):
    try:
        stdin = _stream('stdin') if stdin is None else stdin
        stdout = _stream('stdout') if stdout is None else stdout
        if stdin is None or stdout is None: return 2
        rid, repository, path = parse_request(stdin.readline(MAX_REQUEST_BYTES + 1))
        start_watchdog(stdin, deadline_seconds + 5, exit_fn)
        result = r.check_location(repository, path)
        stdout.write(encode_response(rid, repository, result)); stdout.flush()
        return 0
    except Exception: return 2
