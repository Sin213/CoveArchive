"""N1b-derived valid fixtures and exhaustive finite single-field mutations.

Enums/bools/null are exhaustively enumerated. Infinite integer/text/container
domains use every fixture value plus explicit valid-typed boundary alternatives.
Each scalar field, optional-record presence, and list cardinality is mutated
alone. Replacing an entire non-null record changes multiple scalar fields and
is not a single-field mutation (nor distinguishable from a different valid report).
"""
from copy import deepcopy
from dataclasses import replace
import json

import pytest
from covearchive import health_worker as w
from covearchive.archive import availability as a
from covearchive.archive.health import AuditLimits
from covearchive.library.model import Repository
from covearchive.scan_worker import ScanFailure, ScanSuccess
from covearchive.ui.health_updates import eligibility_problem
from test_archive_availability import current, upstream
from test_archive_health import REPO, archive, asset_path
from test_scan_worker import asset

RID = 'b' * 32
IDENTITY = 'github:owner/repo'
KINDS = ('healthy', 'outdated', 'partial', 'damaged', 'missing', 'unavailable',
         'rate_limited', 'unsupported', 'inconclusive', 'no_releases',
         'changed_upstream', 'tag_reused', 'unverified', 'archive_unavailable',
         'no_archivable_assets', 'ambiguous', 'incomplete_verified', 'partial_audit_verified', 'prerelease')


@pytest.fixture(scope='module')
def evidence(tmp_path_factory):
    base = tmp_path_factory.mktemp('health-evidence')
    fixtures = {}
    for kind in KINDS:
        root, project, records = archive(base / kind, releases=2 if kind == 'partial_audit_verified' else 1)
        scan = upstream(current()); limits = AuditLimits()
        if kind == 'outdated': scan = upstream(current(id=2, tag_name='v2'))
        elif kind == 'partial': scan = upstream(current(assets=[current()['assets'][0], asset(id=1002, size=1)]))
        elif kind == 'damaged': asset_path(project, records).write_bytes(b'bad')
        elif kind == 'missing': asset_path(project, records).unlink()
        elif kind == 'unavailable': scan = ScanFailure('', 'not_found')
        elif kind == 'rate_limited': scan = ScanFailure('', 'rate_limited', detail={'retry_at': 100, 'retry_after_seconds': 5})
        elif kind == 'inconclusive': scan = ScanFailure('', 'network')
        elif kind == 'no_releases': scan = upstream()
        elif kind == 'changed_upstream': scan = upstream(current(tag_name='renamed-tag'))
        elif kind == 'tag_reused': scan = upstream(current(id=2, tag_name='v1'))
        elif kind == 'unverified': limits = AuditLimits(max_asset_bytes=1)
        elif kind == 'archive_unavailable': root = root / 'absent'
        elif kind == 'no_archivable_assets': scan = upstream(current(assets=[]))
        elif kind == 'ambiguous': scan = upstream(current(), current(id=2))
        elif kind == 'incomplete_verified': scan = replace(scan, catalog=replace(scan.catalog, complete=False))
        elif kind == 'partial_audit_verified': limits = AuditLimits(max_assets=1)
        elif kind == 'prerelease': scan = upstream(current(), current(id=3, tag_name='v3-rc1', prerelease=True))
        if kind == 'unsupported':
            identity = Repository('gitlab', 'https://gitlab.com/o/r').identity
            status = w.unsupported()
        else:
            identity = IDENTITY
            status = a.check_repository(str(root), REPO, scan=lambda _: scan, limits=limits)
        fixtures[kind] = (identity, status)
    return fixtures


def availability_oracle():
    """Enumerate actual assess outputs, independently of worker validation."""
    codes = set(a._FAILURE_STATES) | a._INVALID_CODES
    scans = [None, upstream(), replace(upstream(), catalog=replace(upstream().catalog, complete=False)),
             ScanFailure('', 'unrecognized')]
    scans.extend(ScanFailure('', code) for code in codes)
    for cause in [*sorted(codes), None, 'unrecognized']:
        for pages in (0, 1):
            scans.append(ScanFailure('', 'incomplete_scan', detail={'pages_completed': pages, 'cause_code': cause}))
    for retry_at in (None, 0):
        for retry_after in (None, 0):
            scans.append(ScanFailure('', 'rate_limited', detail={'retry_at': retry_at, 'retry_after_seconds': retry_after}))
    values = [a.assess(REPO, scan, None).availability for scan in scans]
    values.append(w.unsupported().availability)
    return {(v.state, v.reason, v.listing_complete, v.retry_at is not None, v.retry_after is not None) for v in values}


def test_closed_availability_table_matches_assess_outputs():
    oracle = availability_oracle()
    assert w.AVAILABILITY_SHAPES == {shape[:3] for shape in oracle}
    for state in a._AVAILABILITY_MESSAGES:
        for reason in w.REASONS:
            for complete in (False, True):
                for retry_at in (None, 0):
                    for retry_after in (None, 0):
                        value = a.Availability(state, reason, 'Fixed message.', complete, retry_at, retry_after)
                        shape = (state, reason, complete, retry_at is not None, retry_after is not None)
                        assert w.valid_availability(value) == (shape in oracle), shape


def test_failure_availability_cannot_carry_catalog_lines(evidence):
    identity, status = evidence['healthy']
    obj = w._response_wire(RID, identity, status)
    failure = a.assess(REPO, ScanFailure('', 'incomplete_scan', detail={
        'pages_completed': 1, 'cause_code': 'network'}), None)
    obj['status']['availability'] = w._response_wire(RID, identity, failure)['status']['availability']
    with pytest.raises(w.ProtocolError): w.decode_response(w._dump(obj), RID, identity)


@pytest.mark.parametrize('reason', ['network', 'incomplete_scan'])
def test_reachable_complete_failure_reason_is_rejected(evidence, reason):
    identity, healthy = evidence['healthy']
    obj = w._response_wire(RID, identity, healthy)
    obj['status']['availability']['reason'] = reason
    with pytest.raises(w.ProtocolError): w.decode_response(w._dump(obj), RID, identity)
    identity, outdated = evidence['outdated']
    status = replace(outdated, availability=replace(outdated.availability, reason=reason))
    assert eligibility_problem(status)


@pytest.mark.parametrize('kind', KINDS)
def test_assess_fixture_roundtrip(evidence, kind):
    identity, status = evidence[kind]
    decoded = w.decode_response(w.encode_response(RID, identity, status), RID, identity)
    assert decoded.headline == status.headline
    assert decoded.stable.comparison == status.stable.comparison
    assert decoded.prerelease.comparison == status.prerelease.comparison


def fields(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield path + (key,), child
            yield from fields(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield path + (index,), child
            yield from fields(child, path + (index,))


def normalized(path):
    return tuple('#' if type(part) is int else part for part in path)


def replacements(path, value, pool):
    key = path[-1]
    if isinstance(value, dict):
        if key in ('repository', 'archive', 'release'): yield None
        return
    values = list(pool.get(normalized(path), ()))
    if key == 'reason': values += sorted(w.REASONS)
    elif key == 'state':
        values += (list(a._AVAILABILITY_MESSAGES) if 'availability' in path else
                   list(a._COMPARISON_MESSAGES) if 'comparison' in path else
                   ['found', 'none', 'ambiguous', 'not_checked', 'unsupported'])
    elif key == 'status' and 'archive' in path: values += ['healthy', 'attention', 'unverified']
    elif key in ('retry_at', 'retry_after', 'local_release_id', 'project_id'):
        values += [None, 0, 1, 2, 2**63]
    elif key == 'local_tag': values += [None, '', 'v1', 'v2', 'renamed-tag']
    elif key == 'sha256': values += [None, '0' * 64, 'f' * 64]
    elif key == 'verified_asset_ids': values += [None, [], [[1, 1001]], [[2, 1001]], [[1, 1001], [1, 1001]]]
    elif key in ('repository', 'archive', 'release'): values += [None]
    elif type(value) is bool: values += [False, True]
    elif type(value) is int: values += [0, 1, 2, 3, 7, 1000, 1001, 1002, 10000, 2**63]
    elif isinstance(value, str):
        if key == 'request_id': values += ['a' * 32, 'c' * 32]
        elif key == 'identity': values += ['github:other/repo', 'gitlab:gitlab.com/o/r']
        elif key == 'provider': values += ['github', 'gitlab', 'codeberg']
        elif key == 'host': values += ['github.com', 'gitlab.com', 'codeberg.org']
        elif key == 'published_at': values += ['2026-01-01T00:00:00Z', '2026-01-01T00:00:00', 'invalid']
        else: values += ['', 'Other', 'v1', 'v2']
    elif isinstance(value, list): values += [[], value + value[:1]]
    unique = {json.dumps(item, sort_keys=True): item for item in values}
    for item in unique.values():
        if type(item) is type(value) and item == value: continue
        yield item


@pytest.mark.parametrize('kind', KINDS)
def test_every_single_wire_field_mutation_fails_closed(evidence, kind, record_property):
    wire_fixtures = {name: w._response_wire(RID, identity, status) for name, (identity, status) in evidence.items()}
    pool = {}
    for obj in wire_fixtures.values():
        for path, value in fields(obj): pool.setdefault(normalized(path), []).append(value)
    identity, truth = evidence[kind]; original = wire_fixtures[kind]
    oracle = availability_oracle(); unsafe = []; checked = 0; mutated_paths = set()
    for path, value in fields(original):
        for replacement in replacements(path, value, pool):
            checked += 1; mutated_paths.add(path)
            mutant = deepcopy(original); parent = mutant
            for part in path[:-1]: parent = parent[part]
            parent[path[-1]] = deepcopy(replacement)
            try: decoded = w.decode_response(w._dump(mutant), RID, identity)
            except w.ProtocolError: continue
            availability = decoded.availability
            signature = (availability.state, availability.reason, availability.listing_complete,
                         availability.retry_at is not None, availability.retry_after is not None)
            if signature not in oracle:
                unsafe.append((path, replacement, 'not an N1b availability tuple'))
            if truth.headline != 'up_to_date' and decoded.headline == 'up_to_date':
                unsafe.append((path, replacement, 'invented up_to_date'))
            if eligibility_problem(truth) is not None and eligibility_problem(decoded) is None:
                unsafe.append((path, replacement, 'invented queue eligibility'))
    expected_paths = {path for path, value in fields(original)
                      if not isinstance(value, dict) or path[-1] in ('repository', 'archive', 'release')}
    assert checked >= 100 and mutated_paths == expected_paths
    assert not unsafe, unsafe[:12]
    record_property('mutations_checked', checked)
    record_property('wire_fields_mutated', len(mutated_paths))
