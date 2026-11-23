"""Offline, confirmed Health batch queueing through the existing bridge."""
from dataclasses import replace
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from PySide6.QtCore import QObject, Signal
from covearchive import health_worker as wire
from covearchive.archive import availability as a
from covearchive.archive.health import audit_project
from covearchive.library.model import Library
from covearchive.scan_worker import ScanSuccess
from covearchive.ui.archive_bridge import EnqueueResult
from covearchive.ui.health_panel import Checked
from covearchive.ui.health_updates import HealthUpdates, eligibility_problem
from test_archive_availability import current, upstream
from test_archive_health import REPO, archive, snapshot
from test_scan_process import APP, until
from test_ui_archive import rig
from test_ui_health import Worker


class Scan(Worker):
    def start(self, url):
        assert self.job is None
        self.job = uuid4().hex; self.calls.append(url); self.state_changed.emit(self.job, 'RUNNING'); return True
    def finish(self, result, state='COMPLETED'):
        if isinstance(result, ScanSuccess): result = replace(result, request_id=self.job)
        super().finish(result, state)


class Check(Worker):
    def start(self, repo, root, *, prepare=False):
        assert prepare is True
        return super().start(repo, root)


class Bridge(QObject):
    enqueued = Signal(object)
    is_open = True; shutting_down = closed = False
    def __init__(self): super().__init__(); self.calls = []
    def enqueue(self, request): self.calls.append(request); return True


@pytest.fixture
def make_updates(tmp_path, monkeypatch):
    for key in ('HOME', 'LOCALAPPDATA', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    environments = []
    def make(bridge=None):
        root, _, _ = archive(tmp_path / str(len(environments)))
        lib = [Library().add_repository('https://github.com/Owner/Repo')]
        repo = lib[0].repositories[0]
        scan_result = upstream(current(id=2, tag_name='v2'))
        status = a.check_repository(str(root), REPO, scan=lambda _: scan_result)
        checked = Checked(status, 'now', str(root)); results = {repo.identity: checked}
        settings = dict(archive_root=str(root), repositories={repo.ref: dict(
            include=['app*'], exclude=['*debug*'], extensions=['.zip'], prereleases=False)})
        scan, check = Scan(), Check(); bridge = bridge if bridge is not None else Bridge()
        updates = HealthUpdates(check, scan, bridge, lambda: lib[0], lambda: results, lambda: settings)
        e = NS(u=updates, scan=scan, check=check, bridge=bridge, lib=lib, root=root,
               repo=repo, checked=checked, results=results, settings=settings, scan_result=scan_result)
        environments.append(e); return e
    yield make
    for e in environments: assert e.u.shutdown(1000)
    APP.processEvents()


def prepare(e):
    batch = e.u.prepare([(e.repo.identity, e.checked)], str(e.root))
    assert batch and e.u.running
    if hasattr(e.bridge, 'calls'): assert not e.bridge.calls
    return batch


def finish_preparation(e, *, status=None):
    e.scan.finish(e.scan_result); until(lambda: bool(e.check.calls))
    report = audit_project(str(e.root), REPO)
    fresh = wire.prepare_status(status or e.checked.status, report)
    e.check.finish(fresh); until(lambda: e.u.phase == 'ready' or not e.u.running)


def test_prepare_never_queues_until_explicit_confirmation(make_updates):
    e = make_updates(); before = snapshot(e.root); lib = e.lib[0]
    batch = prepare(e); finish_preparation(e)
    assert e.u.phase == 'ready' and not e.bridge.calls
    assert snapshot(e.root) == before and e.lib[0] is lib
    assert not e.u.confirm(batch + 1, True) and not e.bridge.calls
    assert e.u.confirm(batch, True); request = e.bridge.calls[-1]
    assert request.requeue_completed is False and request.archive_root == str(e.root)
    assert [(i.release.id, i.asset.id) for i in request.items] == [(2, 1001)]
    e.bridge.enqueued.emit(EnqueueResult(request.token, 1, 0, (), ('job',), False, 0, None))
    until(lambda: not e.u.running)
    assert snapshot(e.root) == before and e.lib[0] is lib
    assert all(repo.auto_archive is None for repo in e.lib[0].repositories)


@pytest.mark.parametrize('phase', ['scan', 'audit', 'ready', 'decline'])
def test_cancel_or_decline_creates_no_jobs(make_updates, phase):
    e = make_updates(); before = snapshot(e.root); batch = prepare(e)
    if phase != 'scan': e.scan.finish(e.scan_result); until(lambda: bool(e.check.calls))
    if phase in ('ready', 'decline'):
        e.check.finish(wire.prepare_status(e.checked.status, audit_project(str(e.root), REPO)))
        until(lambda: e.u.phase == 'ready')
    if phase == 'decline': e.u.confirm(batch, False)
    else:
        e.u.cancel()
        if e.scan.job: e.scan.finish(None, 'CANCELLED')
        if e.check.job: e.check.finish(None, 'CANCELLED')
    until(lambda: not e.u.running)
    assert not e.u.confirm(batch, True) and not e.bridge.calls and snapshot(e.root) == before


@pytest.mark.parametrize('state', ['changed_upstream', 'tag_reused', 'inconclusive', 'damaged',
                                  'missing_files', 'unverified', 'archive_unavailable'])
def test_unsafe_comparisons_not_selectable_or_prepared(make_updates, state):
    e = make_updates(); status = e.checked.status
    status = replace(status, stable=replace(status.stable, comparison=replace(status.stable.comparison, state=state)))
    e.results[e.repo.identity] = e.checked = replace(e.checked, status=status)
    assert eligibility_problem(status)
    assert not e.u.prepare([(e.repo.identity, e.checked)], str(e.root))
    assert not e.scan.calls and not e.bridge.calls and e.u.messages[e.repo.identity]


@pytest.mark.parametrize('missing', ['rules', 'root'])
def test_missing_rules_or_destination_has_actionable_message(make_updates, missing):
    e = make_updates()
    if missing == 'rules': e.settings['repositories'].clear()
    root = '' if missing == 'root' else str(e.root)
    assert not e.u.prepare([(e.repo.identity, e.checked)], root)
    assert not e.scan.calls and not e.bridge.calls
    assert ('rules' if missing == 'rules' else 'destination') in e.u.messages[e.repo.identity].lower()


@pytest.mark.parametrize('change', ['remove', 'remove_readd', 'status', 'rules'])
def test_revalidate_library_status_and_rules_before_submit(make_updates, change):
    e = make_updates(); batch = prepare(e); finish_preparation(e)
    if change in ('remove', 'remove_readd'):
        e.lib[0] = Library(); e.u.sync()
        if change == 'remove_readd': e.lib[0] = Library().add_repository(e.repo.url); e.u.sync()
    elif change == 'status': e.results[e.repo.identity] = replace(e.checked, time='later')
    else: e.settings['repositories'][e.repo.ref]['include'] = ['different*']
    e.u.confirm(batch, True); until(lambda: not e.u.running)
    assert not e.bridge.calls


def test_new_or_changed_metadata_is_not_queued_under_old_confirmation(make_updates):
    e = make_updates(); batch = prepare(e)
    e.scan_result = upstream(current(id=3, tag_name='v3'))
    e.scan.finish(e.scan_result); until(lambda: bool(e.check.calls))
    fresh = a.check_repository(str(e.root), REPO, scan=lambda _: e.scan_result)
    e.check.finish(wire.prepare_status(fresh, audit_project(str(e.root), REPO)))
    until(lambda: not e.u.running)
    assert not e.bridge.calls and 'changed' in e.u.messages[e.repo.identity].lower()


def test_real_bridge_repeat_confirmation_prevents_duplicate_jobs(make_updates, rig):
    bridge, sink = rig(); e = make_updates(bridge)
    for expected in (1, 2):
        batch = e.u.prepare([(e.repo.identity, e.checked)], str(e.root))
        finish_preparation(e); assert e.u.confirm(batch, True)
        until(lambda: len(sink.enqueues) == expected and not e.u.running)
    assert sink.enqueues[0].created == 1 and sink.enqueues[1].created == 0
    assert sink.enqueues[1].requeued == 0 and len(sink.views[-1].jobs) == 1


def test_real_bridge_completed_jobs_are_never_requeued(make_updates, rig):
    from covearchive.queue.models import JobState
    bridge, sink = rig(); e = make_updates(bridge)
    batch = e.u.prepare([(e.repo.identity, e.checked)], str(e.root)); finish_preparation(e)
    e.u.confirm(batch, True); until(lambda: not e.u.running)
    job = bridge._store.claim_next('test-owner')
    bridge._store.finish(job.job_id, 'test-owner', JobState.COMPLETED, result=NS(status='ARCHIVED'))
    before = bridge._store.get(job.job_id)
    batch = e.u.prepare([(e.repo.identity, e.checked)], str(e.root)); finish_preparation(e)
    e.u.confirm(batch, True); until(lambda: len(sink.enqueues) == 2 and not e.u.running)
    assert sink.enqueues[-1].created == sink.enqueues[-1].requeued == 0
    assert bridge._store.get(job.job_id) == before


def test_saved_filename_and_format_rules_apply_to_current_metadata(make_updates):
    from test_scan_worker import asset
    e = make_updates()
    e.scan_result = upstream(current(id=2, tag_name='v2', assets=[
        asset(id=1001, name='app.zip', size=1), asset(id=1002, name='app-debug.zip', size=1),
        asset(id=1003, name='other.zip', size=1), asset(id=1004, name='app.dmg', size=1)]))
    status = a.check_repository(str(e.root), REPO, scan=lambda _: e.scan_result)
    e.results[e.repo.identity] = e.checked = replace(e.checked, status=status)
    batch = prepare(e); finish_preparation(e); assert e.u.confirm(batch, True)
    assert [item.asset.name for item in e.bridge.calls[0].items] == ['app.zip']


def test_batch_deadline_and_expired_evidence_cannot_queue(make_updates):
    e = make_updates(); e.u.preparation_ms = 25
    batch = prepare(e); until(lambda: e.u.phase == 'stopping')
    e.scan.finish(None, 'CANCELLED'); until(lambda: not e.u.running)
    assert not e.u.confirm(batch, True) and not e.bridge.calls
    assert 'deadline' in e.u.progress
    e.u.preparation_ms = 300000
    batch = prepare(e); finish_preparation(e)
    e.u.clock = lambda: e.u.proposals[0]['prepared_at'] + e.u.MAX_AGE_SECONDS + 1
    e.u.confirm(batch, True); until(lambda: not e.u.running)
    assert not e.bridge.calls and 'expired' in e.u.messages[e.repo.identity]


def test_ambiguous_stable_release_cannot_queue_a_prerelease(make_updates):
    e = make_updates(); status = e.checked.status
    pre = replace(status.stable, release=replace(status.stable.release, prerelease=True))
    status = replace(status, stable=a.ReleaseLine('ambiguous', None, a.Comparison('inconclusive', '')), prerelease=pre)
    assert status.headline == 'inconclusive' and eligibility_problem(status)


@pytest.mark.parametrize('include_prereleases', [False, True])
def test_prerelease_only_update_respects_saved_prerelease_rule(make_updates, include_prereleases):
    e = make_updates()
    e.scan_result = upstream(current(), current(id=3, tag_name='v3-rc1', prerelease=True))
    status = a.check_repository(str(e.root), REPO, scan=lambda _: e.scan_result)
    e.results[e.repo.identity] = e.checked = replace(e.checked, status=status)
    e.settings['repositories'][e.repo.ref]['prereleases'] = include_prereleases
    problem = e.u.problem(e.repo, e.checked, str(e.root))
    assert (problem is None) == include_prereleases
    if not include_prereleases:
        assert 'prerelease' in problem.lower()
        assert not e.u.prepare([(e.repo.identity, e.checked)], str(e.root))
        assert not e.scan.calls and not e.bridge.calls


@pytest.mark.parametrize('shape', ['comparison', 'archive'])
@pytest.mark.parametrize('category', ['damaged', 'missing'])
def test_damaged_or_missing_counts_are_defensively_never_queueable(make_updates, shape, category):
    e = make_updates(); status = e.checked.status
    comparison = replace(status.stable.comparison, state='partial', local_release_id=2, local_tag='v2')
    if shape == 'comparison': comparison = replace(comparison, **{category + '_assets': 1})
    status = replace(status, stable=replace(status.stable, comparison=comparison))
    if shape == 'archive':
        status = replace(status, archive=replace(status.archive, status='attention',
            counts=replace(status.archive.counts, verified=0, **{category: 1})))
        # This is a valid global audit shape: damage may concern an older local
        # release. A partial latest release still must not become queueable.
        prepared = wire.PreparedStatus(status.repository, status.availability, status.stable,
                                        status.prerelease, status.archive, ())
        status = wire.decode_response(wire.encode_response('c' * 32, e.repo.identity, prepared), 'c' * 32, e.repo.identity)
    e.results[e.repo.identity] = e.checked = replace(e.checked, status=status)
    assert eligibility_problem(status)
    assert not e.u.prepare([(e.repo.identity, e.checked)], str(e.root))
    assert not e.scan.calls and not e.check.calls and not e.bridge.calls


def test_rate_limit_or_cancel_after_one_prepared_repo_never_queues(make_updates):
    from covearchive.scan_worker import ScanFailure
    e = make_updates(); second = e.lib[0].add_repository('https://github.com/o/second').repositories[-1]
    e.lib[0] = e.lib[0].add_repository(second.url); e.u.sync()
    from covearchive.providers import parse_repository_url
    target = parse_repository_url(second.url)
    # Keep this a two-eligible-repository rate-limit test: a missing project is
    # now deliberately excluded along with all other missing archive evidence.
    archive(e.root.parent, repository=target)
    second_scan = replace(e.scan_result, catalog=replace(e.scan_result.catalog, repository=target))
    status = a.check_repository(str(e.root), target, scan=lambda _: second_scan)
    assert status.archive.complete and status.archive.counts.missing == 0
    second_checked = replace(e.checked, status=status)
    e.results[second.identity] = second_checked
    e.settings['repositories'][second.ref] = e.settings['repositories'][e.repo.ref].copy()
    batch = e.u.prepare([(e.repo.identity, e.checked), (second.identity, second_checked)], str(e.root))
    e.scan.finish(e.scan_result); until(lambda: bool(e.check.calls))
    e.check.finish(wire.prepare_status(e.checked.status, audit_project(str(e.root), REPO)))
    until(lambda: len(e.scan.calls) == 2)
    assert not e.check.job and e.scan.job and len(e.u.proposals) == 1 and not e.bridge.calls
    e.scan.finish(ScanFailure(e.scan.job, 'rate_limited'), 'FAILED')
    until(lambda: not e.u.running)
    assert not e.u.confirm(batch, True) and not e.bridge.calls and 'Rate limit' in e.u.progress


def test_verified_archive_assets_are_not_submitted_for_partial_release(make_updates):
    from test_scan_worker import asset
    from test_archive_health import DATA, SHA
    e = make_updates()
    e.scan_result = upstream(current(assets=[asset(id=1001, size=len(DATA), digest='sha256:' + SHA),
                                            asset(id=1002, name='app-extra.zip', size=1)]))
    status = a.check_repository(str(e.root), REPO, scan=lambda _: e.scan_result)
    assert status.stable.comparison.state == 'partial'
    e.results[e.repo.identity] = e.checked = replace(e.checked, status=status)
    batch = prepare(e); finish_preparation(e); assert e.u.confirm(batch, True)
    assert [item.asset.id for item in e.bridge.calls[0].items] == [1002]


@pytest.mark.parametrize('accepted', [False, True])
@pytest.mark.parametrize('change_destination', [False, True])
def test_review_ui_requires_selection_and_final_confirmation(make_updates, tmp_path, monkeypatch, accepted, change_destination):
    from covearchive.ui.library_store import LibraryStore
    from covearchive.ui.library_panel import LibraryPanel
    from covearchive.ui.health_panel import ReviewDialog
    from PySide6.QtCore import Qt
    e = make_updates(); e.u.shutdown()
    store = LibraryStore(tmp_path / 'panel-library.json'); store.save(e.lib[0])
    panel = LibraryPanel(store, health_controller=Worker(), archive_root=lambda: str(e.root),
        health_settings=lambda: e.settings, health_update_scan=e.scan, health_update_check=e.check, bridge=e.bridge)
    h = panel.health; h.results = e.results; e.u = h.updates
    panel.resize(820, 520); panel.show(); panel.view_selector.setCurrentIndex(1); APP.processEvents()
    confirmations = []
    def confirm(dialog, repositories, files, root):
        assert not e.bridge.calls
        confirmations.append((repositories, files, root))
        if change_destination: dialog.destination.setText(str(e.root / 'changed-destination'))
        return accepted
    monkeypatch.setattr(ReviewDialog, '_confirm_queue', confirm)
    h.review(); dialog = h._review_dialog; APP.processEvents()
    try:
        assert not dialog.queue_button.isEnabled()
        dialog.table.item(0, 0).setCheckState(Qt.Checked); APP.processEvents()
        assert dialog.queue_button.isEnabled()
        before = snapshot(e.root); lib_before = store.path.read_bytes()
        dialog.queue_button.click(); assert not confirmations and not e.bridge.calls
        e.scan.finish(e.scan_result); until(lambda: bool(e.check.calls))
        e.check.finish(wire.prepare_status(e.checked.status, audit_project(str(e.root), REPO)))
        until(lambda: bool(confirmations))
        assert confirmations == [(1, 1, str(e.root))]
        submitted = accepted and not change_destination
        assert bool(e.bridge.calls) == submitted
        if submitted:
            request = e.bridge.calls[-1]
            e.bridge.enqueued.emit(EnqueueResult(request.token, 1, 0, (), ('job',), False, 0, None))
            until(lambda: not e.u.running)
        assert snapshot(e.root) == before and store.path.read_bytes() == lib_before
        assert 'incomplete' not in dialog.notice.text().lower()
    finally: dialog.close(); panel.close()


def test_review_close_cancels_preparation_without_confirmation(make_updates, tmp_path, monkeypatch):
    from covearchive.ui.library_store import LibraryStore
    from covearchive.ui.library_panel import LibraryPanel
    from covearchive.ui.health_panel import ReviewDialog
    from PySide6.QtCore import Qt
    e = make_updates(); e.u.shutdown()
    store = LibraryStore(tmp_path / 'panel-library.json'); store.save(e.lib[0])
    panel = LibraryPanel(store, health_controller=Worker(), archive_root=lambda: str(e.root),
        health_settings=lambda: e.settings, health_update_scan=e.scan, health_update_check=e.check, bridge=e.bridge)
    e.u = panel.health.updates; panel.health.results = e.results
    panel.show(); panel.health.review(); dialog = panel.health._review_dialog
    monkeypatch.setattr(ReviewDialog, '_confirm_queue', lambda *args: pytest.fail('Cancelled flow confirmed'))
    dialog.table.item(0, 0).setCheckState(Qt.Checked); dialog.queue_button.click()
    assert e.scan.job; dialog.close(); APP.processEvents()
    e.scan.finish(None, 'CANCELLED'); until(lambda: not e.u.running)
    assert not e.bridge.calls
    panel.close()
