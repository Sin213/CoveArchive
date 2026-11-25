"""Durable GitHub location observations, independent of Library and archives.

Only numeric repository ids prove continuity between observations. A first
observation never assigns an identity to a legacy archive. No preservation APIs
are used here. Bounds fail closed; history is never pruned to make room.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import json
import math
import os
from pathlib import Path
import stat
import threading
import time

from covearchive.github import client as github
from covearchive.github.urls import RepositoryTarget
from covearchive.jsondepth import check_json_depth
from covearchive.providers import canonical_url, parse_repository_url
from covearchive.queue.models import QueueBusyError
from covearchive.scheduled_state import ScheduleLock, atomic_write, private_directory

MAX_BYTES = 4 * 1024 * 1024
MAX_IDENTITIES = 10000
MAX_BINDINGS = 20000
MAX_LOCATIONS = 64
_WRITER = threading.Lock()


class Outcome(str, Enum):
    UNCHANGED = 'UNCHANGED'
    RENAMED = 'RENAMED'
    TRANSFERRED = 'TRANSFERRED'
    URL_REUSED = 'URL_REUSED'
    UNAVAILABLE = 'UNAVAILABLE'
    INCONCLUSIVE = 'INCONCLUSIVE'
    UNSUPPORTED = 'UNSUPPORTED'


class RelocationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('Repository location history could not be used: ' + code)


def _require(condition, code='state_invalid'):
    if not condition: raise RelocationError(code)


def _keys(obj, keys):
    _require(type(obj) is dict and set(obj) == set(keys))


def _id(value, code='state_invalid'):
    _require(type(value) is int and 1 <= value <= 2**63 - 1, code)


def _time(value, code='state_invalid'):
    _require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 2**40, code)


def _url(value):
    _require(type(value) is str and len(value) <= 2048)
    target = parse_repository_url(value)
    _require(type(target) is RepositoryTarget and target.provider == 'github'
             and canonical_url(target) == value)
    return target


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result)
        result[key] = value
    return result


def _reject(_): raise RelocationError('state_invalid')


def validate(data, *, capacity_error='state_invalid'):
    _keys(data, ('version', 'identities', 'bindings'))
    _require(type(data['version']) is int and data['version'] == 1)
    records, bindings = data['identities'], data['bindings']
    _require(type(records) is list and type(bindings) is list)
    _require(len(records) <= MAX_IDENTITIES and len(bindings) <= MAX_BINDINGS, capacity_error)
    ids = set()
    for record in records:
        _keys(record, ('provider', 'host', 'repository_id', 'locations'))
        _id(record['repository_id']); _require(record['repository_id'] not in ids)
        ids.add(record['repository_id'])
        _require(record['provider'] == 'github' and record['host'] == 'github.com')
        locations = record['locations']
        _require(type(locations) is list and locations)
        _require(len(locations) <= MAX_LOCATIONS, capacity_error)
        previous = None; stamp = 0
        for location in locations:
            _keys(location, ('url', 'observed_at'))
            target = _url(location['url']); _time(location['observed_at'])
            key = target.repo_key
            _require(key != previous and location['observed_at'] >= stamp)
            previous, stamp = key, location['observed_at']
    urls = set()
    for binding in bindings:
        _keys(binding, ('url', 'repository_id', 'observed_at', 'no_prior_identity'))
        key = _url(binding['url']).repo_key
        _id(binding['repository_id']); _time(binding['observed_at'])
        _require(key not in urls and binding['repository_id'] in ids
                 and type(binding['no_prior_identity']) is bool)
        urls.add(key)
    return data


def _safe_path(path):
    # Refuse links, reparse points and special files, including state ancestors.
    for candidate in (path, *path.parents):
        try: info = candidate.lstat()
        except FileNotFoundError: continue
        _require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400)
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 if candidate == path
                 else stat.S_ISDIR(info.st_mode))


class RelocationStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def locked(self):
        with _WRITER:
            _safe_path(self.path); private_directory(self.path.parent)
            end = time.monotonic() + 2
            while True:
                try:
                    lock = ScheduleLock(self.path); break
                except QueueBusyError:
                    if time.monotonic() >= end: raise RelocationError('state_busy') from None
                    time.sleep(.01)
            try: yield
            finally: lock.close()

    def load(self):
        try:
            for attempt in range(3):
                _safe_path(self.path)
                try:
                    fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                                 | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))
                except FileNotFoundError:
                    if attempt == 0: return dict(version=1, identities=[], bindings=[])
                    continue
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    _require(stat.S_ISREG(info.st_mode) and info.st_nlink <= 1
                             and not getattr(info, 'st_file_attributes', 0) & 0x400)
                    if info.st_nlink == 0: continue
                    raw = stream.read(MAX_BYTES + 1)
                try: current = self.path.lstat()
                except FileNotFoundError: continue
                if not os.path.samestat(info, current) or current.st_nlink == 0: continue
                _require(current.st_nlink == 1 and len(raw) <= MAX_BYTES)
                text = raw.decode('utf-8'); check_json_depth(text)
                return validate(json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject))
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            raise RelocationError('state_invalid') from None
        raise RelocationError('state_busy')

    def update(self, snapshot, candidate):
        """Verify the whole snapshot under both locks and publish an append-only change."""
        with self.locked():
            _require(self.load() == snapshot, 'conflict')
            if candidate is None or candidate == snapshot: return
            validate(candidate, capacity_error='limit')
            bindings = snapshot['bindings']
            _require(candidate['bindings'][:len(bindings)] == bindings)
            prior = snapshot['identities']
            _require(len(candidate['identities']) >= len(prior))
            for old, new in zip(prior, candidate['identities']):
                _require(old['repository_id'] == new['repository_id']
                         and old['locations'] == new['locations'][:len(old['locations'])])
            payload = (json.dumps(candidate, allow_nan=False, separators=(',', ':')) + '\n').encode()
            if len(payload) > MAX_BYTES: raise RelocationError('limit')
            _safe_path(self.path)
            atomic_write(self.path, payload)


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    requested_url: str
    repository_id: int | None = None
    observed_id: int | None = None
    verified_url: str | None = None
    locations: tuple[tuple[str, float], ...] = ()
    no_prior_identity: bool = False
    reason: str = 'ok'
    prior_location_count: int = 0


def _record(data, rid):
    return next((r for r in data['identities'] if r['repository_id'] == rid), None)


def _history(record):
    return tuple((loc['url'], loc['observed_at']) for loc in record['locations']) if record else ()


def _binding(data, target):
    return next((b for b in data['bindings'] if _url(b['url']).repo_key == target.repo_key), None)


def _known_ids(data, target):
    binding = _binding(data, target)
    if binding is not None: return {binding['repository_id']}
    # Version-1 history may predate canonical URL bindings.
    return {record['repository_id'] for record in data['identities']
            if any(_url(loc['url']).repo_key == target.repo_key for loc in record['locations'])}


def _bind(data, rid, url, now, *, no_prior_identity):
    if _binding(data, _url(url)) is None:
        data['bindings'].append(dict(url=url, repository_id=rid, observed_at=now,
                                     no_prior_identity=no_prior_identity))


def _observe(data, rid, target, now):
    record = _record(data, rid)
    url = canonical_url(target)
    _bind(data, rid, url, now, no_prior_identity=record is None)
    if record is None:
        record = dict(provider='github', host='github.com', repository_id=rid, locations=[])
        data['identities'].append(record)
    locations = record['locations']
    if not locations or _url(locations[-1]['url']).repo_key != target.repo_key:
        locations.append(dict(url=url, observed_at=max(now, locations[-1]['observed_at'] if locations else 0)))


def _move_outcome(target, current, prior_history):
    if not prior_history: return Outcome.UNCHANGED
    # An initially redirecting saved URL only becomes evidence once observed
    # as a canonical location for this identity.
    reference = target
    if not any(_url(loc).repo_key == target.repo_key for loc, _ in prior_history):
        reference = _url(prior_history[-1][0])
    if current.owner.casefold() != reference.owner.casefold(): return Outcome.TRANSFERRED
    if current.repo.casefold() != reference.repo.casefold(): return Outcome.RENAMED
    return Outcome.UNCHANGED


def check_location(repository, path, *, client_factory=github.GitHubRepositoryClient,
                   clock=time.time, cancel=lambda: False):
    """Resolve a location, then verify the snapshot before publishing evidence."""
    url = repository.url
    if repository.provider != 'github': return Result(Outcome.UNSUPPORTED, url, reason='unsupported')
    snapshot = candidate = None
    rid = None
    history = ()
    try:
        target = _url(url); store = RelocationStore(path)
        snapshot = store.load()
        prior_ids = _known_ids(snapshot, target)
        _require(len(prior_ids) <= 1, 'conflict')
        rid = next(iter(prior_ids), None)
        history = _history(_record(snapshot, rid))
        if cancel(): raise RelocationError('cancelled')
        with client_factory() as client:
            def fetch(**kwargs):
                if cancel(): raise RelocationError('cancelled')
                try: number, place = client.repository(**kwargs)
                except (github.NotFoundError, github.ApiGoneError): return None
                _id(number, 'invalid_response')
                _require(type(place) is RepositoryTarget and place.provider == 'github', 'invalid_response')
                canonical_url(place)
                return number, place
            known = fetch(repository_id=rid) if rid else None
            observed = fetch(target=target)
        if cancel(): raise RelocationError('cancelled')
        observed_id = observed[0] if observed else None
        if rid is None and observed:
            _require(_record(snapshot, observed_id) is None, 'tracked_alias')
            known = observed
        reused = rid is not None and observed is not None and observed_id != rid
        verified = None
        if known:
            current_id, current = known
            _require(rid is None or current_id == rid, 'conflict')
            if observed and not reused:
                _require(observed[1].repo_key == current.repo_key, 'conflict')
            _require(observed is not None or current.repo_key != target.repo_key, 'conflict')
            if not (_known_ids(snapshot, current) - {current_id}):
                verified = canonical_url(current)
        if known is None and not reused:
            # A matching saved-URL lookup alone cannot confirm the bound id.
            outcome = Outcome.INCONCLUSIVE if observed is not None else Outcome.UNAVAILABLE
            result = Result(outcome, url, rid, locations=history, reason='not_found',
                            prior_location_count=len(history))
        elif reused:
            _require(known is None or current.repo_key not in (target.repo_key, observed[1].repo_key), 'conflict')
            result = Result(Outcome.URL_REUSED, url, rid, observed_id, verified, history,
                            reason='ok' if verified else 'not_found', prior_location_count=len(history))
        else:
            _require(verified is not None, 'conflict')
            pending = deepcopy(snapshot); now = clock(); _time(now, 'invalid_response')
            stamp = next((stamp for loc, stamp in reversed(history) if _url(loc).repo_key == target.repo_key), now)
            _bind(pending, current_id, url, stamp, no_prior_identity=not history)
            _observe(pending, current_id, current, now)
            if cancel(): raise RelocationError('cancelled')
            result = Result(_move_outcome(target, current, history), url, current_id, current_id, verified,
                            _history(_record(pending, current_id)), not history,
                            'ok' if history else 'no_prior_identity', prior_location_count=len(history))
            candidate = pending
    except RelocationError as exc: reason = exc.code
    except github.RateLimitError: reason = 'rate_limited'
    except github.GitHubApiError as exc:
        reason = 'network' if isinstance(exc, (github.NetworkError, github.RequestTimeoutError,
                                              github.TransientServerError)) else 'invalid_response'
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError): reason = 'invalid_response'
    else: reason = None
    if reason is not None:
        result = Result(Outcome.INCONCLUSIVE, url, rid, locations=history, reason=reason,
                        prior_location_count=len(history))

    if snapshot is not None:
        try:
            store.update(snapshot, candidate)
        except RelocationError as exc:
            return Result(Outcome.INCONCLUSIVE, url, reason=exc.code)
        except OSError:
            return Result(Outcome.INCONCLUSIVE, url, reason='state_write')
    return result
