"""Offline, strict detection-only Health worker contract."""
from dataclasses import replace
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from covearchive import health_worker as w
from covearchive.archive import availability as a
from covearchive.library.model import Repository
from test_archive_availability import current, upstream
from test_archive_health import REPO, archive, snapshot

RID = 'b' * 32
SAVED = Repository('github', 'https://github.com/Owner/Repo')
ROOT = Path(__file__).resolve().parents[1]


def test_request_strict_anonymous_and_bounded(tmp_path):
    request = w.encode_request(RID, SAVED, str(tmp_path))
    assert w.parse_request(request) == (RID, SAVED, str(tmp_path))
    obj = json.loads(request)
    for key, value in [('github_token', 'secret'), ('protocol', True), ('root', '\x00'),
                       ('request_id', 'foreign'), ('url', 'https://user:secret@github.com/o/r')]:
        with pytest.raises(w.ProtocolError):
            w.parse_request(w._dump(obj | {key: value}))
    for data in [request * 2, b'x' * (w.MAX_REQUEST_BYTES + 1), request[:-1],
                 request.replace(b'"protocol":2', b'"protocol":2,"protocol":2')]:
        with pytest.raises(w.ProtocolError): w.parse_request(data)


def test_status_roundtrip_omits_remote_titles_and_exceptions(tmp_path):
    root, _, _ = archive(tmp_path)
    status = a.check_repository(str(root), REPO, scan=lambda _: upstream(current(name='SECRET <b>')))
    data = w.encode_response(RID, SAVED.identity, status)
    assert b'SECRET' not in data and len(data) <= w.MAX_RESPONSE_BYTES
    decoded = w.decode_response(data, RID, SAVED.identity)
    assert decoded.headline == 'up_to_date'
    assert decoded.archive.complete and decoded.archive.counts.verified == 1
    assert decoded.stable.release.tag == 'v1' and decoded.stable.release.name is None
    for rid, identity in [('f' * 32, SAVED.identity), (RID, 'github:other/repo')]:
        with pytest.raises(w.ProtocolError): w.decode_response(data, rid, identity)
    obj = json.loads(data)
    obj['status']['availability']['message'] = 'SECRET'
    with pytest.raises(w.ProtocolError): w.decode_response(w._dump(obj), RID, SAVED.identity)


def test_worker_reuses_check_and_is_read_only(tmp_path, monkeypatch):
    root, _, _ = archive(tmp_path); before = snapshot(root); calls = []
    def check(path, repository):
        calls.append((path, repository))
        return a.check_repository(path, repository, scan=lambda _: upstream(current()))
    monkeypatch.setattr(w, 'check_repository', check)
    result = w.run_check(RID, SAVED, str(root))
    assert w.decode_response(result, RID, SAVED.identity).headline == 'up_to_date'
    assert calls == [(str(root), REPO)] and snapshot(root) == before


@pytest.mark.parametrize('provider,url', [('gitlab', 'https://gitlab.com/o/r'),
                                         ('gitea', 'https://forge.example/o/r')])
def test_unsupported_never_scans_or_audits(monkeypatch, provider, url):
    monkeypatch.setattr(w, 'check_repository', lambda *a: pytest.fail('unsupported check'))
    repo = Repository(provider, url)
    result = w.decode_response(w.run_check(RID, repo, ''), RID, repo.identity)
    assert result.headline == 'unsupported'


def test_source_launcher_dispatch_is_qt_free_and_offline():
    repo = Repository('gitlab', 'https://gitlab.com/o/r')
    process = subprocess.Popen([sys.executable, '-I', '-B', str(ROOT / 'run_covearchive.py'),
                                '--health-worker'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    try:
        process.stdin.write(w.encode_request(RID, repo, '')); process.stdin.flush()
        assert process.wait(timeout=5) == 0
        assert not process.stderr.read()
        assert w.decode_response(process.stdout.read(), RID, repo.identity).headline == 'unsupported'
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=3)
        process.stdin.close(); process.stdout.close(); process.stderr.close()


def test_main_rejects_unknown_keys_without_echoing_secrets(monkeypatch):
    output = io.BytesIO()
    request = w._dump(json.loads(w.encode_request(RID, SAVED, '')) | {'token': 'SECRET'})
    assert w.main(io.BytesIO(request), output) == 2
    assert not output.getvalue()


@pytest.mark.parametrize('change', ['version', 'boolean_count', 'unknown_state', 'missing_key',
                                   'verified_without_evidence', 'mismatched_repository'])
def test_rejects_malformed_status(tmp_path, change):
    root, _, _ = archive(tmp_path)
    status = a.check_repository(str(root), REPO, scan=lambda _: upstream(current()))
    obj = json.loads(w.encode_response(RID, SAVED.identity, status))
    if change == 'version': obj['protocol'] = 99
    elif change == 'boolean_count': obj['status']['archive']['counts']['verified'] = True
    elif change == 'unknown_state': obj['status']['stable']['comparison']['state'] = 'SECRET'
    elif change == 'missing_key': del obj['status']['prerelease']
    elif change == 'verified_without_evidence': obj['status']['stable']['comparison']['verified_assets'] = 0
    elif change == 'mismatched_repository': obj['status']['repository']['owner'] = 'Other'
    with pytest.raises(w.ProtocolError): w.decode_response(w._dump(obj), RID, SAVED.identity)


@pytest.mark.parametrize('change', ['audit_zero', 'too_many_expected', 'too_many_missing',
    'too_many_damaged', 'too_many_unarchived', 'wrong_local_id', 'wrong_local_tag',
    'missing_local_id', 'same_stable_prerelease_id', 'both_claim_same_audited_asset'])
def test_rejects_cross_field_contradictions(tmp_path, change):
    from copy import deepcopy
    root, _, _ = archive(tmp_path)
    status = a.check_repository(str(root), REPO, scan=lambda _: upstream(current()))
    obj = json.loads(w.encode_response(RID, SAVED.identity, status))
    stable = obj['status']['stable']; comparison = stable['comparison']
    if change == 'audit_zero': obj['status']['archive']['counts']['verified'] = 0
    elif change == 'too_many_expected': comparison['expected_assets'] = comparison['verified_assets'] = 2
    elif change == 'too_many_missing': comparison.update(state='missing_files', verified_assets=0, missing_assets=2)
    elif change == 'too_many_damaged': comparison.update(state='damaged', verified_assets=0, damaged_assets=2)
    elif change == 'too_many_unarchived': comparison.update(state='partial', verified_assets=0, unarchived_assets=2)
    elif change == 'wrong_local_id': comparison['local_release_id'] = 42
    elif change == 'wrong_local_tag': comparison['local_tag'] = 'unrelated'
    elif change == 'missing_local_id': comparison['local_release_id'] = None
    else:
        other = deepcopy(stable); other['release']['prerelease'] = True
        if change == 'both_claim_same_audited_asset':
            other['release']['release_id'] = other['comparison']['local_release_id'] = 2
            other['release']['tag'] = other['comparison']['local_tag'] = 'v2'
        obj['status']['prerelease'] = other
    with pytest.raises(w.ProtocolError): w.decode_response(w._dump(obj), RID, SAVED.identity)


def test_partial_audit_retains_legitimate_positive_verification(tmp_path):
    from covearchive.archive.health import AuditLimits
    root, _, _ = archive(tmp_path, releases=2)
    status = a.check_repository(str(root), REPO, scan=lambda _: upstream(current()),
                                limits=AuditLimits(max_assets=1))
    assert not status.archive.complete and status.stable.comparison.verified_assets == 1
    assert w.decode_response(w.encode_response(RID, SAVED.identity, status), RID, SAVED.identity).headline == 'up_to_date'


def test_preparation_inventory_is_bounded_safe_read_only(tmp_path, monkeypatch):
    from covearchive.archive.health import audit_project
    root, _, _ = archive(tmp_path); before = snapshot(root)
    monkeypatch.setattr(w, 'check_repository', lambda path, repo: a.check_repository(
        path, repo, scan=lambda _: upstream(current())))
    request = w.encode_request(RID, SAVED, str(root), prepare=True)
    assert 'token' not in json.loads(request) and w.parse_request(request, with_action=True)[-1] is True
    data = w.run_check(RID, SAVED, str(root), prepare=True)
    decoded = w.decode_response(data, RID, SAVED.identity)
    assert isinstance(decoded, w.PreparedStatus) and decoded.verified_asset_ids == ((1, 1001),)
    assert decoded.stable.release.archivable_assets[0].asset_id == 1001
    assert snapshot(root) == before
    obj = json.loads(data); obj['verified_asset_ids'] = [[2, 1001]]
    with pytest.raises(w.ProtocolError): w.decode_response(w._dump(obj), RID, SAVED.identity)
    obj['verified_asset_ids'] = [[1, 1001], [1, 1001]]
    with pytest.raises(w.ProtocolError): w.decode_response(w._dump(obj), RID, SAVED.identity)
    report = audit_project(str(root), REPO)
    changed = replace(report, complete=False, status='unverified')
    status = a.check_repository(str(root), REPO, scan=lambda _: upstream(current()))
    assert w.prepare_status(status, changed).headline == 'not_checked'


@pytest.mark.parametrize('category', ['damaged', 'missing'])
def test_partial_comparison_rejects_damaged_or_missing_assets(tmp_path, category):
    root, _, _ = archive(tmp_path)
    status = a.check_repository(str(root), REPO, scan=lambda _: upstream(current()))
    obj = json.loads(w.encode_response(RID, SAVED.identity, status))
    obj['status']['stable']['comparison'].update(state='partial', verified_assets=0,
        unarchived_assets=1, **{category + '_assets': 1})
    obj['status']['archive']['counts'].update(verified=0, **{category: 1})
    obj['status']['archive']['status'] = 'attention'
    obj['verified_asset_ids'] = []
    with pytest.raises(w.ProtocolError): w.decode_response(w._dump(obj), RID, SAVED.identity)


def test_response_accepts_64_kib_but_rejects_one_byte_more(tmp_path):
    root, _, _ = archive(tmp_path)
    status = a.check_repository(str(root), REPO, scan=lambda _: upstream(current()))
    data = w.encode_response(RID, SAVED.identity, status)
    exact = data[:-1] + b' ' * (65536 - len(data)) + b'\n'
    assert len(exact) == 65536 and w.decode_response(exact, RID, SAVED.identity).headline == 'up_to_date'
    with pytest.raises(w.ProtocolError): w.decode_response(exact[:-1] + b' \n', RID, SAVED.identity)
    assert w.MAX_RESPONSE_BYTES == 65536


@pytest.mark.parametrize('evidence', ['upstream_assets', 'verified_inventory'])
def test_large_legitimate_evidence_degrades_to_inconclusive(tmp_path, evidence):
    from covearchive.ui.health_updates import eligibility_problem
    from test_archive_health import SHA
    root, _, _ = archive(tmp_path)
    status = a.check_repository(str(root), REPO, scan=lambda _: upstream(current()))
    if evidence == 'upstream_assets':
        release = replace(status.stable.release, total_asset_count=1000,
            archivable_assets=tuple(a.UpstreamAsset(i, 1, SHA) for i in range(1, 1001)))
        comparison = replace(status.stable.comparison, expected_assets=1000, verified_assets=1000)
        status = replace(status, stable=replace(status.stable, release=release, comparison=comparison),
                         archive=replace(status.archive, counts=replace(status.archive.counts, verified=1000)))
        pairs = tuple((1, i) for i in range(1, 1001))
    else:
        status = replace(status, archive=replace(status.archive, counts=replace(status.archive.counts, verified=10000)))
        pairs = tuple((rid, aid) for rid in range(1, 11) for aid in range(1001, 2001))
    prepared = w.PreparedStatus(status.repository, status.availability, status.stable, status.prerelease, status.archive, pairs)
    data = w.encode_response(RID, SAVED.identity, prepared)
    assert len(data) <= 65536
    decoded = w.decode_response(data, RID, SAVED.identity)
    assert decoded.repository == REPO and decoded.headline == 'inconclusive'
    assert decoded.availability.reason == 'result_too_large' and not decoded.availability.listing_complete
    assert decoded.archive is None and decoded.stable.release is None and decoded.prerelease.release is None
    assert not isinstance(decoded, w.PreparedStatus) and eligibility_problem(decoded)
