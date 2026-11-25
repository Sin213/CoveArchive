"""Offline relocation evidence, durable storage, and preservation isolation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import subprocess
import sys
import threading

import pytest

from covearchive import relocation as r
from covearchive.github import client as g
from covearchive.github.urls import RepositoryTarget
from covearchive.library.model import Repository, Library
from covearchive.ui.library_store import LibraryStore

A = Repository('github', 'https://github.com/Owner/Repo')
B = RepositoryTarget('Owner', 'Renamed')
C = RepositoryTarget('Other', 'Transferred')


class Provider:
    def __init__(self):
        self.by_url = {A.ref: (1, RepositoryTarget('Owner', 'Repo'))}
        self.by_id = {1: self.by_url[A.ref]}
        self.calls = []

    def __enter__(self): return self
    def __exit__(self, *args): pass

    def repository(self, target=None, *, repository_id=None):
        key = target.repo_key if target is not None else repository_id
        self.calls.append(key)
        value = (self.by_url if target is not None else self.by_id).get(key)
        if value is None: raise g.NotFoundError()
        if isinstance(value, Exception): raise value
        return value

    def move(self, target, rid=1):
        self.by_id[rid] = (rid, target)
        self.by_url[A.ref] = (rid, target)


def check(path, provider, repo=A, **kwargs):
    return r.check_location(repo, path, client_factory=lambda: provider, clock=lambda: 1234, **kwargs)


def test_initial_legacy_observation_and_unchanged_are_idempotent(tmp_path):
    path = tmp_path / 'relocation.json'; p = Provider()
    first = check(path, p)
    assert first.outcome is r.Outcome.UNCHANGED and first.no_prior_identity
    assert first.reason == 'no_prior_identity' and first.repository_id == 1
    assert first.locations == ((A.url, 1234),)
    document = r.RelocationStore(path).load()
    assert document['bindings'][0]['no_prior_identity'] is True
    before = path.read_bytes(); stamp = path.stat().st_mtime_ns
    second = check(path, p)
    assert second.outcome is r.Outcome.UNCHANGED and not second.no_prior_identity
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    assert p.calls == [A.ref, 1, A.ref]


def test_first_redirect_observation_never_claims_a_move(tmp_path):
    p = Provider(); p.move(C)
    path = tmp_path / 'r.json'; result = check(path, p)
    assert result.outcome is r.Outcome.UNCHANGED and result.no_prior_identity
    assert result.locations == (('https://github.com/Other/Transferred', 1234),)
    assert result.verified_url != A.url
    repeated = check(path, p)
    assert repeated.outcome is r.Outcome.UNCHANGED and not repeated.no_prior_identity
    assert repeated.locations == result.locations
    # A later move is proved only from the first observed canonical location.
    p.move(B)
    moved = check(path, p)
    assert moved.outcome is r.Outcome.TRANSFERRED
    from covearchive import relocation_worker as wire
    assert wire.decode_response(wire.encode_response('a' * 32, A, moved), 'a' * 32, A) == moved
    repeated = check(path, p)
    assert repeated.outcome is r.Outcome.UNCHANGED and repeated.locations == moved.locations
    assert wire.decode_response(wire.encode_response('a' * 32, A, repeated), 'a' * 32, A) == repeated


def test_successive_rename_transfer_return_and_dedup(tmp_path):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p)
    p.move(B); rename = check(path, p)
    assert rename.outcome is r.Outcome.RENAMED
    p.move(C); transfer = check(path, p)
    assert transfer.outcome is r.Outcome.TRANSFERRED
    p.move(RepositoryTarget('Owner', 'Repo')); returned = check(path, p)
    assert returned.outcome is r.Outcome.UNCHANGED
    assert len(returned.locations) == 4 and returned.locations[0] == returned.locations[-1]
    before = path.read_bytes(); check(path, p); assert path.read_bytes() == before


def test_known_id_resolves_even_when_old_url_missing(tmp_path):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p)
    p.by_id[1] = (1, B); p.by_url.clear()
    result = check(path, p)
    assert result.outcome is r.Outcome.RENAMED and result.verified_url.endswith('/Renamed')
    assert not p.by_url and p.calls[-2:] == [1, A.ref]
    assert result.observed_id == result.repository_id == 1


@pytest.mark.parametrize('absence', [g.NotFoundError, g.ApiGoneError], ids=['404', '410'])
def test_absence_at_claimed_current_url_conflicts_without_recording_return(tmp_path, absence):
    path = tmp_path / 'r.json'; p = Provider()
    p.move(B); first = check(path, p)
    before = path.read_bytes()
    p.by_id[1] = (1, RepositoryTarget('owner', 'repo'))
    p.by_url[A.ref] = absence()
    result = check(path, p)
    assert result.outcome is r.Outcome.INCONCLUSIVE and result.reason == 'conflict'
    assert result.locations == first.locations and result.verified_url is None
    assert path.read_bytes() == before


@pytest.mark.parametrize('legacy', [False, True])
def test_verified_destination_retains_identity_after_save_move_and_reuse(tmp_path, legacy):
    path = tmp_path / 'relocation.json'; p = Provider(); check(path, p)
    p.move(B); moved = check(path, p)
    destination = Repository('github', moved.verified_url)
    store = LibraryStore(tmp_path / 'library.json')
    lib = Library().add_repository(A.url).add_repository(destination.url)
    assert store.save(lib) is None and store.load()[0] == lib
    data = r.RelocationStore(path).load()
    assert next(b for b in data['bindings'] if b['url'] == destination.url)['repository_id'] == 1
    if legacy:
        # Same version-1 schema, as written before canonical bindings existed.
        data['bindings'] = [binding for binding in data['bindings'] if binding['url'] != destination.url]
        path.write_text(json.dumps(data))
    before = path.read_bytes()
    p.move(C); p.by_url[B.repo_key] = (2, B)
    reused = check(path, p, destination)
    assert reused.outcome is r.Outcome.URL_REUSED and not reused.no_prior_identity
    assert reused.repository_id == 1 and reused.observed_id == 2
    assert reused.verified_url == r.canonical_url(C) and reused.verified_url != destination.url
    assert p.calls[-2:] == [1, B.repo_key]
    data = r.RelocationStore(path).load()
    assert data['version'] == 1
    assert r._history(r._record(data, 1)) == moved.locations
    assert r._record(data, 2) is None
    assert reused.prior_location_count == 2
    assert check(path, p, destination) == reused
    assert path.read_bytes() == before
    located = check(path, p)
    assert located.repository_id == 1 and located.verified_url == r.canonical_url(C)
    assert p.calls[-2:] == [1, A.ref]


@pytest.mark.parametrize('legacy', [False, True])
def test_canonical_observation_never_overwrites_conflicting_binding(tmp_path, legacy):
    path = tmp_path / 'r.json'; destination = Repository('github', r.canonical_url(B))
    occupant = Provider(); occupant.by_url = {B.repo_key: (2, B)}
    assert check(path, occupant, destination).repository_id == 2
    p = Provider(); check(path, p)
    if legacy:
        data = r.RelocationStore(path).load()
        data['bindings'] = [binding for binding in data['bindings'] if binding['url'] != destination.url]
        path.write_text(json.dumps(data))
    before = path.read_bytes(); p.move(B)
    result = check(path, p)
    assert result.outcome is r.Outcome.INCONCLUSIVE and result.reason == 'conflict'
    assert path.read_bytes() == before
    data = r.RelocationStore(path).load()
    bindings = [binding for binding in data['bindings'] if binding['url'] == destination.url]
    assert not bindings if legacy else bindings[0]['repository_id'] == 2
    assert r._history(r._record(data, 1)) == ((A.url, 1234),)


def test_reuse_reports_occupant_without_recording_either_location(tmp_path):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); p.move(B)
    before = path.read_bytes()
    p.by_url[A.ref] = (2, C)
    result = check(path, p)
    assert result.outcome is r.Outcome.URL_REUSED
    assert result.observed_id == 2 and result.verified_url == r.canonical_url(B)
    assert result.locations == ((A.url, 1234),) and result.prior_location_count == 1
    assert path.read_bytes() == before


def test_ambiguous_legacy_canonical_history_fails_closed(tmp_path):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); p.move(B); check(path, p)
    data = r.RelocationStore(path).load(); r._observe(data, 2, B, 1234)
    data['bindings'] = [binding for binding in data['bindings'] if binding['url'] != r.canonical_url(B)]
    path.write_text(json.dumps(data)); before = path.read_bytes(); p.calls.clear()
    result = check(path, p, Repository('github', r.canonical_url(B)))
    assert result.outcome is r.Outcome.INCONCLUSIVE and result.reason == 'conflict'
    assert not p.calls and path.read_bytes() == before


def test_unchanged_legacy_destination_backfills_binding_once(tmp_path):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); p.move(B); check(path, p)
    destination = Repository('github', r.canonical_url(B)); p.by_url[B.repo_key] = (1, B)
    data = r.RelocationStore(path).load()
    data['bindings'] = [binding for binding in data['bindings'] if binding['url'] != destination.url]
    path.write_text(json.dumps(data)); history = deepcopy(data['identities'])
    result = check(path, p, destination)
    assert result.outcome is r.Outcome.UNCHANGED and not result.no_prior_identity
    repaired = r.RelocationStore(path).load()
    assert repaired['identities'] == history
    assert next(binding for binding in repaired['bindings'] if binding['url'] == destination.url)['repository_id'] == 1
    before = path.read_bytes(); check(path, p, destination); assert path.read_bytes() == before


@pytest.mark.parametrize('failure,reason', [
    (g.AccessDeniedError(), 'invalid_response'),
    (g.NetworkError(), 'network'), (g.RequestTimeoutError(), 'network'),
    (g.TransientServerError(), 'network'), (g.InvalidResponseError(), 'invalid_response'),
    (g.UnsafeRedirectError(), 'invalid_response'),
    (g.RateLimitError(kind='primary', status=403, retry_after_seconds=60, retry_at=2000), 'rate_limited'),
])
@pytest.mark.parametrize('phase', ['known', 'url'])
def test_failures_never_mutate_confirmed_state(tmp_path, failure, reason, phase):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); before = path.read_bytes()
    p.by_id[1] = failure if phase == 'known' else (1, B)
    p.by_url[A.ref] = (2, C) if phase == 'known' else failure
    result = check(path, p)
    assert result.outcome is r.Outcome.INCONCLUSIVE and result.reason == reason
    assert result.locations == ((A.url, 1234),) and result.verified_url is None
    assert path.read_bytes() == before


@pytest.mark.parametrize('lookup', ['stable-id', 'saved-url'])
def test_empty_provider_response_is_not_confirmed_absence(tmp_path, lookup):
    path = tmp_path / 'r.json'; p = Provider(); first = check(path, p); before = path.read_bytes()
    p.move(B); p.by_url[A.ref] = (2, C)
    fetch = p.repository

    def empty_reply(**kwargs):
        if ('repository_id' in kwargs) == (lookup == 'stable-id'):
            return None
        return fetch(**kwargs)

    p.repository = empty_reply
    result = check(path, p)
    assert result.outcome is r.Outcome.INCONCLUSIVE and result.reason == 'invalid_response'
    assert result.locations == first.locations and path.read_bytes() == before


@pytest.mark.parametrize('failure,reason', [
    (g.AccessDeniedError(), 'invalid_response'),
    (g.NetworkError(), 'network'),
    (g.RateLimitError(kind='primary', status=403, retry_after_seconds=60, retry_at=2000), 'rate_limited'),
])
def test_failed_first_check_is_not_an_observation(tmp_path, failure, reason):
    path = tmp_path / 'r.json'; p = Provider()
    p.by_url[A.ref] = failure
    result = check(path, p)
    assert result == r.Result(r.Outcome.INCONCLUSIVE, A.url, reason=reason)
    assert not path.exists()


@pytest.mark.parametrize('failure', [g.NotFoundError(), g.ApiGoneError()])
def test_confirmed_absence_is_unavailable(tmp_path, failure):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); before = path.read_bytes()
    p.by_id[1] = p.by_url[A.ref] = failure
    assert check(path, p).outcome is r.Outcome.UNAVAILABLE
    assert path.read_bytes() == before
    fresh = tmp_path / 'fresh.json'
    assert check(fresh, p).outcome is r.Outcome.UNAVAILABLE and not fresh.exists()


def test_saved_url_with_bound_id_does_not_replace_unavailable_stable_lookup(tmp_path):
    from covearchive import relocation_worker as wire
    path = tmp_path / 'r.json'; p = Provider(); first = check(path, p); before = path.read_bytes()
    p.move(B); p.by_id.clear(); p.calls.clear()
    result = check(path, p)
    assert p.calls == [1, A.ref]
    assert result == r.Result(r.Outcome.INCONCLUSIVE, A.url, repository_id=1,
                              locations=first.locations, reason='not_found', prior_location_count=1)
    assert path.read_bytes() == before
    assert wire.decode_response(wire.encode_response('a' * 32, A, result), 'a' * 32, A) == result


@pytest.mark.parametrize('known,observed', [
    ((2, B), (1, B)), ((1, B), (1, C)), ((1, B), (2, B)),
    ((1, RepositoryTarget('Owner', 'Repo')), (2, RepositoryTarget('Owner', 'Repo'))),
])
def test_conflicting_provider_evidence_never_merges(tmp_path, known, observed):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); before = path.read_bytes()
    p.by_id[1] = known; p.by_url[A.ref] = observed
    result = check(path, p)
    assert result.outcome is r.Outcome.INCONCLUSIVE and result.reason == 'conflict'
    assert path.read_bytes() == before


def test_reused_url_with_unavailable_original_has_no_verified_continuation(tmp_path):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p)
    store = r.RelocationStore(path); before = store.load()
    p.by_id.clear(); p.by_url[A.ref] = (2, RepositoryTarget('Owner', 'Repo'))
    result = check(path, p)
    assert result.outcome is r.Outcome.URL_REUSED and result.verified_url is None
    assert result.repository_id == 1 and result.observed_id == 2
    assert result.locations == ((A.url, 1234),)
    assert store.load() == before
    saved = path.read_bytes()
    assert check(path, p) == result and path.read_bytes() == saved


@pytest.mark.parametrize('provider,url', [('gitea', 'https://codeberg.org/o/r'), ('gitlab', 'https://gitlab.com/o/r')])
def test_unsupported_providers_do_not_read_state_or_use_network(tmp_path, provider, url):
    path = tmp_path / 'r.json'; path.write_bytes(b'corrupt')
    def forbidden(): pytest.fail('Unsupported provider contacted network')
    result = r.check_location(Repository(provider, url), path, client_factory=forbidden)
    assert result.outcome is r.Outcome.UNSUPPORTED and path.read_bytes() == b'corrupt'


@pytest.mark.parametrize('raw', [b'bad', b'{"version":1,"version":1}', b'[' * 70 + b']' * 70,
    b'{"version":NaN}', b'{"version":true,"identities":[],"bindings":[]}',
    b'{"version":1,"identities":[],"bindings":[],"extra":0}'])
def test_corruption_fails_closed_before_network(tmp_path, raw):
    path = tmp_path / 'r.json'; path.write_bytes(raw); p = Provider()
    result = check(path, p)
    assert result.outcome is r.Outcome.INCONCLUSIVE and result.reason == 'state_invalid'
    assert path.read_bytes() == raw and not p.calls


@pytest.mark.parametrize('change', ['unknown', 'bool_id', 'large_id', 'nan_time', 'negative_time', 'duplicate_id',
    'duplicate_binding', 'duplicate_location', 'unsafe_url', 'cross_provider', 'host', 'missing_id', 'bad_flag'])
def test_strict_stored_schema(tmp_path, change):
    path = tmp_path / 'r.json'; check(path, Provider()); data = json.loads(path.read_bytes())
    record = data['identities'][0]; binding = data['bindings'][0]
    if change == 'unknown': record['extra'] = 0
    elif change == 'bool_id': record['repository_id'] = True
    elif change == 'large_id': record['repository_id'] = 2**63
    elif change == 'nan_time': record['locations'][0]['observed_at'] = float('nan')
    elif change == 'negative_time': binding['observed_at'] = -1
    elif change == 'duplicate_id': data['identities'].append(deepcopy(record))
    elif change == 'duplicate_binding': data['bindings'].append(deepcopy(binding))
    elif change == 'duplicate_location': record['locations'].append(deepcopy(record['locations'][0]))
    elif change == 'unsafe_url': binding['url'] = 'https://user@github.com/Owner/Repo'
    elif change == 'cross_provider': record['provider'] = 'gitlab'
    elif change == 'host': record['host'] = 'evil.example'
    elif change == 'missing_id': binding['repository_id'] = 999
    else: binding['no_prior_identity'] = 1
    raw = json.dumps(data).encode(); path.write_bytes(raw)
    assert check(path, Provider()).reason == 'state_invalid' and path.read_bytes() == raw


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'directory', 'fifo', 'parent_symlink'])
def test_unsafe_state_files_refused(tmp_path, kind):
    if os.name == 'nt' and kind in ('fifo', 'symlink', 'parent_symlink'): pytest.skip('POSIX file types')
    path = tmp_path / 'r.json'; other = tmp_path / 'other'; other.write_bytes(b'unchanged')
    if kind == 'symlink': path.symlink_to(other)
    elif kind == 'hardlink': os.link(other, path)
    elif kind == 'directory': path.mkdir()
    elif kind == 'fifo': os.mkfifo(path)
    else:
        directory = tmp_path / 'dir'; directory.mkdir(); link = tmp_path / 'link'; link.symlink_to(directory)
        path = link / 'r.json'
    result = check(path, Provider())
    assert result.reason == 'state_invalid' and other.read_bytes() == b'unchanged'


@pytest.mark.parametrize('failure', ['fsync', 'replace'])
def test_interrupted_persistence_retains_file_and_cleans_temp(tmp_path, monkeypatch, failure):
    from covearchive import scheduled_state as s
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); before = path.read_bytes(); p.move(B)
    def fail(*args): raise OSError('interrupted')
    monkeypatch.setattr(s.os, failure, fail)
    assert check(path, p).reason == 'state_write'
    assert path.read_bytes() == before and not list(tmp_path.glob('*.tmp'))


def test_corruption_after_network_is_never_overwritten(tmp_path):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); p.move(B)
    original = p.repository
    def corrupt(*args, **kwargs):
        result = original(*args, **kwargs); path.write_bytes(b'corrupt'); return result
    p.repository = corrupt
    assert check(path, p).reason == 'state_invalid' and path.read_bytes() == b'corrupt'


@pytest.mark.parametrize('failure', [
    g.NetworkError(),
    g.RateLimitError(kind='primary', status=403, retry_after_seconds=60, retry_at=2000),
])
def test_tracked_alias_stops_after_one_lookup_even_if_stable_lookup_would_fail(tmp_path, failure):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); before = path.read_bytes()
    alias = Repository('github', r.canonical_url(B))
    p.by_url[alias.ref] = (1, B); p.by_id[1] = failure; p.calls.clear()
    assert check(path, p, alias) == r.Result(r.Outcome.INCONCLUSIVE, alias.url, reason='tracked_alias')
    assert p.calls == [alias.ref] and path.read_bytes() == before


@pytest.mark.parametrize('unlinked', [True, False], ids=['unlinked-inode', 'different-inode'])
def test_load_retries_concurrent_atomic_replace(tmp_path, monkeypatch, unlinked):
    from pathlib import Path
    from types import SimpleNamespace

    path = tmp_path / 'r.json'; p = Provider(); check(path, p)
    replacement = tmp_path / 'replacement.json'; p.move(B); check(replacement, p)
    store = r.RelocationStore(path)
    expected = r.RelocationStore(replacement).load()
    original_fstat, original_lstat = os.fstat, Path.lstat
    opened = False

    def fstat(fd):
        nonlocal opened
        info = original_fstat(fd)
        if not opened:
            opened = True
            if unlinked:
                # Simulate an unlinked descriptor without replacing an open file on Windows.
                return SimpleNamespace(st_nlink=0, st_file_attributes=0, st_mode=info.st_mode)
        return info

    def lstat(candidate):
        if candidate == path and opened and replacement.exists():
            os.replace(replacement, path)
        return original_lstat(candidate)

    monkeypatch.setattr(os, 'fstat', fstat)
    monkeypatch.setattr(Path, 'lstat', lstat)
    assert store.load() == expected


def test_load_bounds_atomic_replace_retries(tmp_path, monkeypatch):
    from types import SimpleNamespace

    path = tmp_path / 'r.json'; check(path, Provider()); before = path.read_bytes()
    original, saved_inode = os.fstat, path.stat()
    attempts = 0

    def replaced(fd):
        nonlocal attempts
        info = original(fd)
        if os.path.samestat(info, saved_inode):
            attempts += 1
            # Positional stat reconstruction drops the Windows attributes mask.
            return SimpleNamespace(st_nlink=0, st_file_attributes=0, st_mode=info.st_mode,
                                   st_dev=info.st_dev, st_ino=info.st_ino)
        return info

    monkeypatch.setattr(os, 'fstat', replaced)
    with pytest.raises(r.RelocationError, match='state_busy'):
        r.RelocationStore(path).load()
    assert attempts == 3 and path.read_bytes() == before


def test_thread_writers_reject_stale_independent_records(tmp_path):
    path = tmp_path / 'r.json'; barrier = threading.Barrier(2)
    def run(number):
        repo = Repository('github', f'https://github.com/O/R{number}'); p = Provider()
        p.by_url = {repo.ref: (number, RepositoryTarget('O', f'R{number}'))}
        original = p.repository
        def fetch(**kwargs): barrier.wait(timeout=3); return original(**kwargs)
        p.repository = fetch
        return check(path, p, repo)
    with ThreadPoolExecutor(2) as pool: results = list(pool.map(run, (1, 2)))
    winner, = [result for result in results if result.outcome is r.Outcome.UNCHANGED]
    loser, = [result for result in results if result.outcome is r.Outcome.INCONCLUSIVE]
    assert loser == r.Result(r.Outcome.INCONCLUSIVE, loser.requested_url, reason='conflict')
    data = r.RelocationStore(path).load()
    assert {entry['repository_id'] for entry in data['identities']} == {winner.repository_id}
    assert len(data['bindings']) == 1


def test_stale_same_identity_is_inconclusive(tmp_path):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p)
    store = r.RelocationStore(path); old = store.load()
    candidate = deepcopy(old); r._observe(candidate, 1, B, 2000); store.update(old, candidate)
    stale = deepcopy(old); r._observe(stale, 1, C, 2001)
    with pytest.raises(r.RelocationError, match='conflict'): store.update(old, stale)
    # Even an unchanged candidate cannot return stale verified evidence.
    with pytest.raises(r.RelocationError, match='conflict'): store.update(old, None)
    assert store.load() == candidate


def test_reuse_conflicts_when_unchanged_occupant_moves_concurrently(tmp_path):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p)
    occupant = Repository('github', 'https://github.com/Owner/Renamed')
    p.by_url[occupant.ref] = p.by_id[2] = (2, B)
    check(path, p, occupant)
    p.by_url[A.ref] = (2, B)
    p.by_id[1] = (1, C)

    other = Provider()
    destination = RepositoryTarget('Owner', 'OccupantMoved')
    other.by_url[occupant.ref] = other.by_id[2] = (2, destination)
    original = p.repository
    committed = None

    def fetch(**kwargs):
        nonlocal committed
        evidence = original(**kwargs)
        if 'target' in kwargs:
            assert check(path, other, occupant).outcome is r.Outcome.RENAMED
            committed = path.read_bytes()
        return evidence

    p.repository = fetch
    result = check(path, p)
    assert result.outcome is r.Outcome.INCONCLUSIVE and result.reason == 'conflict'
    assert committed is not None and path.read_bytes() == committed


def test_process_writers_use_os_lock_and_reject_stale_snapshots(tmp_path):
    from pathlib import Path
    path = tmp_path / 'r.json'; source = str(Path(__file__).resolve().parents[1] / 'src')
    script = ('import sys;sys.path.insert(0,sys.argv[1]);from covearchive import relocation as r;'
              'from covearchive.github.urls import RepositoryTarget;'
              's=r.RelocationStore(sys.argv[2]);old=s.load();print("ready",flush=True);input();'
              'new=__import__("copy").deepcopy(old);rid=int(sys.argv[3]);'
              'r._observe(new,rid,RepositoryTarget("O","R"+str(rid)),100);'
              '\ntry: s.update(old,new)\nexcept r.RelocationError as exc: print(exc.code)\nelse: print("written")')
    processes = [subprocess.Popen([sys.executable, '-I', '-B', '-c', script, source, str(path), str(n)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for n in range(1, 5)]
    with ThreadPoolExecutor(len(processes)) as pool:
        try:
            ready = [pool.submit(process.stdout.readline) for process in processes]
            assert all(marker.result(timeout=5).strip() == b'ready' for marker in ready)
            runs = [pool.submit(process.communicate, input=b'\n', timeout=5) for process in processes]
            replies = [run.result() for run in runs]
            assert all(process.returncode == 0 for process in processes), replies
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    assert sorted(out.strip() for out, _ in replies) == [b'conflict'] * 3 + [b'written']
    assert len(r.RelocationStore(path).load()['identities']) == 1


def test_limits_fail_closed_without_pruning(tmp_path, monkeypatch):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); before = path.read_bytes(); p.move(B)
    monkeypatch.setattr(r, 'MAX_LOCATIONS', 1)
    result = check(path, p)
    assert result.reason == 'limit' and path.read_bytes() == before


@pytest.mark.parametrize('change', ['truncate-history', 'reassign-binding', 'rewrite-binding',
                                    'delete-binding', 'delete-identity'])
def test_store_rejects_rewriting_recorded_evidence(tmp_path, change):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); p.move(B); check(path, p)
    other = Repository('github', r.canonical_url(C))
    p.by_url[other.ref] = (2, C); check(path, p, other)
    store = r.RelocationStore(path); snapshot = store.load(); before = path.read_bytes()
    candidate = deepcopy(snapshot)
    if change == 'truncate-history':
        r._record(candidate, 1)['locations'].pop(0)
    elif change == 'delete-binding':
        candidate['bindings'].pop(0)
    elif change == 'delete-identity':
        candidate['identities'].pop()
        candidate['bindings'].pop()
    else:
        binding = r._binding(candidate, r._url(A.url))
        if change == 'reassign-binding': binding['repository_id'] = 2
        else: binding['observed_at'] += 1
    r.validate(candidate)
    with pytest.raises(r.RelocationError, match='state_invalid'):
        store.update(snapshot, candidate)
    assert path.read_bytes() == before


@pytest.mark.parametrize('bound', ['MAX_IDENTITIES', 'MAX_BINDINGS', 'MAX_LOCATIONS', 'MAX_BYTES'])
def test_store_classifies_oversized_candidate_as_limit(tmp_path, monkeypatch, bound):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); before = path.read_bytes()
    store = r.RelocationStore(path); snapshot = store.load(); candidate = deepcopy(snapshot)
    r._observe(candidate, 1 if bound == 'MAX_LOCATIONS' else 2, B, 2000)
    monkeypatch.setattr(r, bound, len(before) if bound == 'MAX_BYTES' else 1)
    with pytest.raises(r.RelocationError, match='limit'):
        store.update(snapshot, candidate)
    assert path.read_bytes() == before


@pytest.mark.parametrize('bound', ['MAX_BYTES', 'MAX_IDENTITIES', 'MAX_BINDINGS'])
def test_store_bounds_fail_closed(tmp_path, monkeypatch, bound):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); before = path.read_bytes()
    monkeypatch.setattr(r, bound, len(before) - 1 if bound == 'MAX_BYTES' else 0)
    assert check(path, p).reason == 'state_invalid' and path.read_bytes() == before


def test_case_only_spelling_deduplicates_locations_and_bindings(tmp_path):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); before = path.read_bytes()
    result = check(path, p, Repository('github', A.url.lower()))
    assert result.outcome is r.Outcome.UNCHANGED and not result.no_prior_identity
    assert path.read_bytes() == before


@pytest.mark.parametrize('matching_case', [True, False], ids=['matching-case', 'different-case'])
def test_api_case_change_does_not_break_wire_or_duplicate_history(tmp_path, matching_case):
    from covearchive import relocation_worker as wire
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); before = path.read_bytes()
    lower = RepositoryTarget('owner', 'repo'); p.by_id[1] = (1, lower)
    if matching_case: p.by_url[A.ref] = (1, lower)
    result = check(path, p)
    assert result.outcome is r.Outcome.UNCHANGED and result.verified_url == A.url.lower()
    assert path.read_bytes() == before
    assert wire.decode_response(wire.encode_response('a' * 32, A, result), 'a' * 32, A) == result


def test_os_lock_contention_is_reported_without_mutation(tmp_path, monkeypatch):
    from covearchive.queue.models import QueueBusyError
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); p.move(B); before = path.read_bytes()
    def busy(*args): raise QueueBusyError('runner_active')
    monkeypatch.setattr(r, 'ScheduleLock', busy)
    ticks = iter((0, 0, 3)); monkeypatch.setattr(r.time, 'monotonic', lambda: next(ticks))
    assert check(path, p).reason == 'state_busy' and path.read_bytes() == before


def test_cancel_before_commit_retains_state(tmp_path):
    path = tmp_path / 'r.json'; p = Provider(); check(path, p); before = path.read_bytes(); p.move(B)
    assert check(path, p, cancel=lambda: len(p.calls) >= 3).reason == 'cancelled'
    assert path.read_bytes() == before


def test_library_removal_does_not_remove_location_history(tmp_path):
    path = tmp_path / 'relocation.json'; check(path, Provider()); before = path.read_bytes()
    store = LibraryStore(tmp_path / 'library.json'); lib = Library().add_repository(A.url)
    assert store.save(lib) is None
    assert store.save(lib.remove_repositories((A.identity,))) is None
    assert path.read_bytes() == before


def test_scenario_move_reuse_preserves_real_archives_mirror_bundle_and_queue(local, tmp_path):
    from covearchive.archive import githistory as gh
    from test_archive_health import archive, snapshot
    from test_git_history import REPO as HISTORY_REPO
    root, _, _ = archive(tmp_path / 'releases')
    bundle = gh.archive_history(HISTORY_REPO, local[1])
    mirror = gh.update_mirror(HISTORY_REPO, local[1])
    assert bundle.snapshot.exists() and mirror.capture.exists()
    protected = tmp_path / 'protected'; protected.mkdir()
    from covearchive.queue.store import QueueStore
    from test_queue_store import item
    queue = QueueStore(protected / 'queue.sqlite3')
    queue.enqueue(*item(root=str(root)))
    for name in ('scheduled.json', 'preferences.json'):
        (protected / name).write_bytes(b'pre-existing authorized settings and jobs')
    before = (snapshot(root), snapshot(local[1]), snapshot(protected))
    path = tmp_path / 'relocation.json'; p = Provider(); check(path, p)
    p.move(B); moved = check(path, p)
    saved = path.read_bytes()
    p.by_url[A.ref] = (2, RepositoryTarget('Owner', 'Repo'))
    reused = check(path, p)
    assert moved.outcome is r.Outcome.RENAMED and reused.outcome is r.Outcome.URL_REUSED
    assert reused.repository_id == 1 and reused.observed_id == 2 and reused.verified_url.endswith('/Renamed')
    data = r.RelocationStore(path).load()
    assert path.read_bytes() == saved
    assert {record['repository_id'] for record in data['identities']} == {1}
    assert data['bindings'][0]['repository_id'] == 1
    assert data['identities'][0]['locations'][0]['url'] == A.url
    # Distinct Library paths remain accessible without changing either entry.
    lib = Library().add_repository(A.url).add_repository(reused.verified_url)
    assert {repo.url for repo in lib.repositories} == {A.url, reused.verified_url}
    assert all(repo.auto_archive is None for repo in lib.repositories)
    assert (snapshot(root), snapshot(local[1]), snapshot(protected)) == before


# Reuse the real local Git fixture; it patches only the existing private network seam.
from test_git_history import local  # noqa: E402,F401
