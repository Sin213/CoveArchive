"""Deterministic state-machine oracle for the relocation invariants."""
from copy import deepcopy
import json
import random

import pytest

from covearchive import relocation, relocation_worker
from covearchive.github.client import AccessDeniedError, NetworkError, NotFoundError, RateLimitError
from covearchive.github.urls import RepositoryTarget
from covearchive.library.model import Repository

A, B, C, D, E = (('Owner', 'Repo'), ('Owner', 'Renamed'), ('Other', 'Repo'),
                 ('Others', 'Moved'), ('Owner', 'Alias'))
REQUEST_ID = 'a' * 32
FAILURES = {
    'forbidden': ('invalid_response', AccessDeniedError),
    'network': ('network', NetworkError),
    'rate_limited': ('rate_limited', lambda: RateLimitError(
        kind='primary', status=403, retry_after_seconds=60, retry_at=2000)),
}


def url(location):
    return 'https://github.com/' + '/'.join(location)


def key(location):
    return tuple(part.casefold() for part in location)


class World:
    def __init__(self, urls, stable=None):
        self.urls = {key(place): value for place, value in urls.items()}
        self.stable = stable if stable is not None else {
            value[0]: value for value in self.urls.values() if isinstance(value, tuple)
        }
        self.before_reply = None
        self.lookups = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def repository(self, target=None, *, repository_id=None):
        self.lookups.append(repository_id if target is None else (target.owner, target.repo))
        value = (self.stable.get(repository_id) if target is None
                 else self.urls.get(key((target.owner, target.repo))))
        if self.before_reply is not None:
            writer, self.before_reply = self.before_reply, None
            writer()
        if isinstance(value, str):
            raise FAILURES[value][1]()
        if value is None:
            raise NotFoundError()
        rid, place = value
        return rid, RepositoryTarget(*place)


def predict(histories, bindings, world, requested, stamp, location_limit):
    """Return the expected response and durable state using only world data."""
    request_key = key(requested)

    def owners(place):
        place_key = key(place)
        if place_key in bindings:
            return {bindings[place_key][1]}
        return {number for number, entries in histories.items()
                if any(key(location) == place_key for location, _ in entries)}

    possible = owners(requested)
    tracked = next(iter(possible)) if len(possible) == 1 else None
    rid = tracked
    observed = world.urls.get(request_key)

    def response(outcome, reason, *, current=None, occupant=None, first=False, history=()):
        return (outcome, tuple((url(place), t) for place, t in history),
                url(current) if current else None, reason, rid, occupant, first)

    def failure(reason, outcome='INCONCLUSIVE', *, empty=False):
        identity = None if empty else tracked
        locations = tuple((url(place), t) for place, t in histories.get(identity, ()))
        return (outcome, locations, None, reason, identity, None, False), histories, bindings

    if len(possible) > 1:
        return failure('conflict')
    if tracked is None:
        if isinstance(observed, str):
            return failure(FAILURES[observed][0])
        if observed is None:
            return failure('not_found', 'UNAVAILABLE')
        rid, _ = observed
        if rid in histories:
            return failure('tracked_alias')
    prior = histories.get(rid, [])
    stable = world.stable.get(tracked) if tracked is not None else observed
    for evidence in (stable, observed):
        if isinstance(evidence, str):
            return failure(FAILURES[evidence][0])
    reused = tracked is not None and observed is not None and observed[0] != rid
    if stable is None and not reused:
        return failure('not_found', 'UNAVAILABLE' if observed is None else 'INCONCLUSIVE')
    current = stable[1] if stable else None
    if stable and (stable[0] != rid or (
        observed and observed[0] == rid and key(observed[1]) != key(current)
    )):
        return failure('conflict')
    if observed is None and current and key(current) == request_key:
        return failure('conflict')
    if reused:
        if current and key(current) in (request_key, key(observed[1])):
            return failure('conflict')
        if current and owners(current) - {rid}:
            current = None
        return (response('URL_REUSED', 'ok' if current else 'not_found', current=current,
                         occupant=observed[0], history=prior), histories, bindings)
    if owners(current) - {rid}:
        return failure('conflict')

    append = not prior or key(current) != key(prior[-1][0])
    if append and len(prior) == location_limit:
        return failure('limit', empty=True)
    new_histories, new_bindings = deepcopy(histories), dict(bindings)
    last_seen = next((t for place, t in reversed(prior) if key(place) == request_key), stamp)
    first = not prior
    new_bindings.setdefault(request_key, (requested, rid, last_seen, first))
    new_bindings.setdefault(key(current), (current, rid, stamp, first))
    if append:
        new_histories.setdefault(rid, []).append((current, stamp))
    outcome = 'UNCHANGED'
    if prior:
        baseline = request_key if request_key in {key(place) for place, _ in prior} else key(prior[-1][0])
        if key(current)[0] != baseline[0]:
            outcome = 'TRANSFERRED'
        elif key(current)[1] != baseline[1]:
            outcome = 'RENAMED'
    return (response(outcome, 'no_prior_identity' if first else 'ok', current=current, occupant=rid,
                     first=first, history=new_histories[rid]), new_histories, new_bindings)


class Run:
    def __init__(self, path, location_limit=64):
        self.path = path
        self.stamp = 0
        self.histories = {}
        self.bindings = {}
        self.location_limit = location_limit

    def check(self, requested, world, *, writer=None):
        self.stamp += 1
        stamp = self.stamp
        expected, histories, bindings = predict(self.histories, self.bindings, world, requested,
                                               stamp, self.location_limit)
        prior_count = len(self.histories.get(expected[4], ()))
        before = self.path.read_bytes() if self.path.exists() else None
        if writer is not None:
            def concurrent_write():
                nonlocal before
                self.check(*writer)
                committed = self.path.read_bytes()
                assert committed != before
                before = committed
            world.before_reply = concurrent_write
        repository = Repository('github', url(requested))
        result = relocation.check_location(repository, self.path,
                                           client_factory=lambda: world, clock=lambda: stamp)
        raw = relocation_worker.encode_response(REQUEST_ID, repository, result)
        assert relocation_worker.decode_response(raw, REQUEST_ID, repository) == result
        if writer is not None:
            assert world.before_reply is None
            expected = ('INCONCLUSIVE', (), None, 'conflict', None, None, False)
            histories, bindings = self.histories, self.bindings
            prior_count = 0
        actual = (result.outcome.value, result.locations, result.verified_url, result.reason,
                  result.repository_id, result.observed_id, result.no_prior_identity)
        assert actual == expected
        assert result.prior_location_count == prior_count
        if histories == self.histories and bindings == self.bindings:
            assert (self.path.read_bytes() if self.path.exists() else None) == before
        # I2. Parse actual bytes and check exact prefixes and immutable bindings.
        state = (json.loads(self.path.read_bytes()) if self.path.exists()
                 else {'version': 1, 'identities': [], 'bindings': []})
        assert state['version'] == 1
        records = state['identities']
        assert len(records) == len(histories)
        actual_histories = {
            record['repository_id']: [(entry['url'], entry['observed_at']) for entry in record['locations']]
            for record in records
        }
        assert all((record['provider'], record['host']) == ('github', 'github.com') for record in records)
        for rid, old in self.histories.items():
            assert actual_histories[rid][:len(old)] == [(url(place), t) for place, t in old]
        assert actual_histories == {rid: [(url(p), t) for p, t in entries] for rid, entries in histories.items()}
        assert len(state['bindings']) == len(bindings)
        actual_bindings = {row['url']: (row['repository_id'], row['observed_at'], row['no_prior_identity'])
                           for row in state['bindings']}
        for place, rid, t, first in self.bindings.values():
            assert actual_bindings[url(place)] == (rid, t, first)
        assert actual_bindings == {url(place): (rid, t, first) for place, rid, t, first in bindings.values()}
        self.histories, self.bindings = histories, bindings
        return raw


@pytest.mark.parametrize('stable', [{1: (1, B)}, {}, {1: (1, C)}],
                         ids=['corroborated', 'stable-id-404', 'different-canonical-location'])
def test_i1_tracked_alias_is_inconclusive_without_stable_lookup_or_write(tmp_path, stable):
    run = Run(tmp_path / 'r.json')
    run.check(A, World({A: (1, A)}))
    world = World({E: (1, B)}, stable)
    run.check(E, world)
    assert world.lookups == [E]


@pytest.mark.parametrize('stable', [{2: (2, C)}, {}, {2: (2, D)}],
                         ids=['corroborated', 'stable-id-404', 'different-canonical-location'])
def test_i1_reused_url_does_not_check_or_record_moved_occupant(tmp_path, stable):
    run = Run(tmp_path / 'r.json')
    run.check(A, World({A: (1, A)}))
    run.check(B, World({B: (2, B)}))
    world = World({A: (2, C)}, stable)
    run.check(A, world)
    assert world.lookups == [1, A]


def test_i1_reuse_warns_even_with_historically_bound_destination(tmp_path):
    run = Run(tmp_path / 'r.json')
    run.check(A, World({A: (1, A)}))
    run.check(B, World({B: (3, B)}))
    run.check(A, World({A: (2, A)}, {1: (1, B)}))


@pytest.mark.parametrize('start,end,moved', [(B, C, 'TRANSFERRED'), (A, B, 'RENAMED')])
@pytest.mark.parametrize('repeat', [False, True], ids=['move-as-unchanged', 'repeat-as-move'])
def test_astra_2_alias_move_and_repeat_reject_relabeling(tmp_path, start, end, moved, repeat):
    # I4, I5. Same final history, different pre-check history.
    run = Run(tmp_path / 'r.json')
    run.check(E, World({E: (1, start)}))
    world = World({E: (1, end)})
    frame = json.loads(run.check(E, world))
    if repeat:
        frame = json.loads(run.check(E, world))
    repository = Repository('github', url(E))
    assert frame['result']['outcome'] == ('UNCHANGED' if repeat else moved)
    frame['result']['outcome'] = moved if repeat else 'UNCHANGED'
    with pytest.raises(relocation_worker.ProtocolError):
        relocation_worker.decode_response((json.dumps(frame) + '\n').encode(), REQUEST_ID, repository)


def test_i5_first_observation_cannot_claim_a_preexisting_history(tmp_path):
    run = Run(tmp_path / 'r.json')
    run.check(E, World({E: (1, A)}))
    world = World({E: (1, C)})
    run.check(E, world)
    frame = json.loads(run.check(E, world))
    frame['result'].update(no_prior_identity=True, reason='no_prior_identity')
    with pytest.raises(relocation_worker.ProtocolError):
        relocation_worker.decode_response((json.dumps(frame) + '\n').encode(), REQUEST_ID,
                                          Repository('github', url(E)))


@pytest.mark.parametrize('requested,world_urls,stable', [
    (A, {A: (1, A)}, {1: (1, A)}),
    (A, {A: (1, B)}, {1: (1, B)}),
    (A, {A: (1, C)}, {1: (1, C)}),
    (A, {A: (2, B)}, {1: (1, C)}),
    (A, {}, {}),
    (A, {A: (1, B)}, {}),
    (A, {A: (1, B)}, {1: (1, C)}),
    (A, {A: 'network'}, {1: (1, A)}),
    (E, {}, {}),
    (E, {E: (1, E)}, {}),
    (E, {E: (2, E)}, {}),
], ids=['unchanged', 'renamed', 'transferred', 'url-reused', 'unavailable-astra-3',
        'inconclusive-not-found', 'inconclusive-conflict', 'inconclusive-network',
        'unavailable-no-history', 'inconclusive-new-alias', 'first-observation'])
def test_i3_concurrent_writer_between_snapshot_and_publish(tmp_path, requested, world_urls, stable):
    run = Run(tmp_path / 'r.json')
    run.check(A, World({A: (1, A)}))
    writer = (D, World({D: (3, D)}))
    run.check(requested, World(world_urls, stable), writer=writer)


def test_i3_concurrent_writer_claims_verified_binding_with_different_case(tmp_path):
    run = Run(tmp_path / 'r.json')
    run.check(A, World({A: (1, A)}))
    lower = key(B)
    run.check(A, World({A: (1, B)}), writer=(lower, World({lower: (2, lower)})))


def test_i3_concurrent_writer_changes_reused_occupant(tmp_path):
    run = Run(tmp_path / 'r.json')
    run.check(A, World({A: (1, A)}))
    run.check(B, World({B: (2, B)}))
    run.check(A, World({A: (2, B)}, {1: (1, D)}),
              writer=(B, World({B: (2, C)})))


@pytest.mark.parametrize('failure', FAILURES)
@pytest.mark.parametrize('lookup', ['stable-id', 'saved-url'])
@pytest.mark.parametrize('concurrent', [False, True])
def test_i3_failed_lookup_never_writes_and_still_verifies_history(tmp_path, failure, lookup, concurrent):
    run = Run(tmp_path / 'r.json')
    run.check(A, World({A: (1, A)}))
    if lookup == 'stable-id':
        world = World({A: (2, B)}, {1: failure})
    else:
        world = World({A: failure}, {1: (1, B)})
    writer = (D, World({D: (3, D)})) if concurrent else None
    run.check(A, world, writer=writer)


@pytest.mark.parametrize('concurrent', [False, True])
def test_i2_capacity_fails_without_losing_history(tmp_path, monkeypatch, concurrent):
    monkeypatch.setattr(relocation, 'MAX_LOCATIONS', 1)
    run = Run(tmp_path / 'r.json', location_limit=1)
    run.check(A, World({A: (1, A)}))
    writer = (D, World({D: (3, D)})) if concurrent else None
    run.check(A, World({A: (1, B)}), writer=writer)


def test_i1_404_with_matching_url_and_case_only_changes(tmp_path):
    run = Run(tmp_path / 'r.json')
    run.check(A, World({A: (1, A)}))
    run.check(A, World({A: (1, B)}, {}))
    run.check(key(A), World({A: (1, A)}, {1: (1, key(A))}))
    run.check(A, World({A: (1, B)}))
    run.check(A, World({}, {1: (1, key(A))}))


def test_i4_move_back_to_initial_alias(tmp_path):
    run = Run(tmp_path / 'r.json')
    run.check(E, World({E: (1, C)}))
    run.check(E, World({E: (1, E)}))
    run.check(E, World({E: (1, E)}))


def test_i1_i5_seeded_worlds(tmp_path):
    places = (A, B, C, D, E)
    for seed in range(350):
        rng = random.Random(seed)
        run = Run(tmp_path / f'{seed}.json')
        for step in range(7):
            stable = {}
            for rid in (1, 2):
                canonical = rng.choice(places)
                stable[rid] = rng.choice([None, (rid, canonical), (rid, key(canonical)),
                                          (rid, canonical), *FAILURES])
            requests = [place for place in places if rng.randrange(3)]
            urls = {place: rng.choice([None, stable[1], stable[2], (1, rng.choice(places)),
                                       (2, rng.choice(places)), *FAILURES]) for place in requests}
            requested = rng.choice(places)
            try:
                run.check(rng.choice([requested, key(requested)]), World(urls, stable))
            except AssertionError as exc:
                raise AssertionError(f'seed={seed}, step={step}, urls={urls}, stable={stable}') from exc
