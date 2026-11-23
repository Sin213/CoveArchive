"""Confirmed, session-only Health updates using existing supervised processes.

Preparation never writes. Validated catalogs, fresh health and verified asset
IDs must agree before the final confirmation. Only ArchiveBridge inserts jobs.
"""
from copy import deepcopy
from dataclasses import replace
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal, Qt

from covearchive import health_worker as wire
from covearchive.archive import availability as a
from covearchive.archive.naming import validate_root
from covearchive.auto_archive import eligible_items
from covearchive.build_filters import BuildRules
from covearchive.providers import parse_repository_url
from covearchive.scan_worker import ScanSuccess
from covearchive.ui.archive_bridge import EnqueueRequest, EnqueueItem

QUEUEABLE = frozenset(('not_archived', 'partial'))
UNSAFE = frozenset(('changed_upstream', 'tag_reused', 'damaged', 'missing_files',
                    'inconclusive', 'unverified', 'archive_unavailable'))


def eligibility_problem(status):
    if status.repository is None or status.repository.provider != 'github': return 'Unsupported provider.'
    if (not wire.valid_availability(status.availability) or status.availability.state != 'reachable'
            or status.availability.reason != 'ok' or not status.availability.listing_complete):
        return 'Inconclusive or unavailable upstream evidence; check again.'
    if status.headline == 'inconclusive': return 'Inconclusive release evidence; check again.'
    if status.archive is None: return 'Local archive verification is unavailable.'
    if (status.archive.counts.damaged or status.archive.counts.missing
            or any(line.comparison.damaged_assets or line.comparison.missing_assets
                   for line in (status.stable, status.prerelease))):
        return 'Not queueable: damaged or missing archive evidence. Open in Browse to inspect.'
    lines = [line for line in (status.stable, status.prerelease) if line.release is not None]
    if any(line.comparison.state in UNSAFE for line in lines):
        return 'Not queueable: changed/reused, damaged/missing, or inconclusive evidence. Open in Browse to inspect.'
    if not any(line.comparison.state in QUEUEABLE for line in lines): return 'No eligible new or partially archived release.'
    return None


def _signature(release):
    if release is None: return None
    return (release.release_id, release.tag, release.published_at, release.prerelease,
            release.total_asset_count, tuple(sorted((asset.asset_id, asset.size, asset.sha256)
                                                    for asset in release.archivable_assets)))


class HealthUpdates(QObject):
    changed = Signal()
    ready = Signal(int)
    MAX_AGE_SECONDS = 180

    def __init__(self, check, scan, bridge, library, results, settings, *, clock=time.monotonic,
                 preparation_ms=300000, parent=None):
        super().__init__(parent)
        self.check, self.scan, self.bridge = check, scan, bridge
        self.library, self.results, self.settings, self.clock = library, results, settings, clock
        self.running = False; self.phase = 'idle'; self.messages = {}; self.progress = ''
        self._batch = 0; self._stopped = False; self._active = None
        self._pending = []; self.proposals = []; self._queue = []
        self._members = {repo.identity for repo in library().repositories}
        self.total = self.processed = self.created = self.existing = 0
        self.ack_timer = QTimer(self); self.ack_timer.setSingleShot(True)
        self.ack_timer.timeout.connect(self._ack_timeout)
        self.preparation_ms = preparation_ms
        self.preparation_timer = QTimer(self); self.preparation_timer.setSingleShot(True)
        self.preparation_timer.timeout.connect(lambda: self.cancel('Preparation deadline reached; nothing queued.'))
        scan.state_changed.connect(lambda rid, state: self._state('scan', rid, state))
        check.state_changed.connect(lambda rid, state: self._state('audit', rid, state))
        scan.terminal.connect(self._scan_terminal); check.terminal.connect(self._check_terminal)
        if bridge is not None: bridge.enqueued.connect(self._enqueued, Qt.QueuedConnection)

    def sync(self):
        members = {repo.identity for repo in self.library().repositories}
        removed = self._members - members; self._members = members
        if removed:
            for ctx in [*self._pending, *self.proposals, *self._queue, *([self._active] if self._active else [])]:
                if ctx['key'] in removed: ctx['valid'] = False

    def _rule(self, repo):
        rule = self.settings().get('repositories', {}).get(repo.ref)
        if not isinstance(rule, dict): raise ValueError('rules')
        if (type(rule.get('prereleases')) is not bool or not isinstance(rule.get('extensions'), list)
                or not all(isinstance(value, str) for value in rule['extensions'])): raise ValueError('rules')
        BuildRules(rule['include'], rule['exclude'])
        return deepcopy(rule)

    def problem(self, repo, checked, root):
        if checked is None: return 'Not checked. Run a health check first.'
        problem = eligibility_problem(checked.status)
        if problem: return problem
        try: rule = self._rule(repo)
        except (ValueError, TypeError, KeyError, AttributeError):
            return 'No valid saved rules. Choose formats and filename filters in Browse and save rules.'
        if not any(line.release is not None and line.comparison.state in QUEUEABLE
                   and (not line.release.prerelease or rule['prereleases'])
                   for line in (checked.status.stable, checked.status.prerelease)):
            return 'The latest prerelease is excluded by saved prerelease rules. Open in Browse to change rules.'
        try: validate_root(root)
        except (ValueError, TypeError): return 'Choose a valid archive destination in Browse or this review.'
        if self.bridge is None or not self.bridge.is_open or self.bridge.closed or self.bridge.shutting_down:
            return 'Archive queue unavailable. Open in Browse after the queue becomes available.'
        return None

    def prepare(self, selected, root):
        if self.running or self._stopped: return False
        self.sync(); self._batch += 1; self.messages = {}; self.proposals = []
        self._pending = []; self._queue = []; self._active = None
        self.created = self.existing = self.processed = 0
        current = {repo.identity: repo for repo in self.library().repositories}
        seen = set()
        for identity, checked in selected:
            if identity in seen: continue
            seen.add(identity); repo = current.get(identity)
            problem = 'Repository or health evidence changed; check again.'
            if repo is not None and self.results().get(identity) is checked:
                problem = self.problem(repo, checked, root)
            if problem: self.messages[identity] = problem; continue
            self._pending.append(dict(key=identity, repo=repo, checked=checked, root=root,
                                      rule=self._rule(repo), valid=True, request_id=None))
        self.total = len(self._pending)
        if not self._pending:
            self.progress = 'Nothing eligible to prepare.'; self.changed.emit(); return False
        self.running = True; self.phase = 'scan'
        self.preparation_timer.start(self.preparation_ms)
        batch = self._batch; self._next_prepare(batch)
        return batch

    def _current(self, ctx):
        self.sync()
        repo = next((repo for repo in self.library().repositories if repo.identity == ctx['key']), None)
        if not ctx['valid'] or repo is None or self.results().get(ctx['key']) is not ctx['checked']:
            return 'Repository or health evidence changed; check again.'
        problem = self.problem(repo, ctx['checked'], ctx['root'])
        if problem: return problem
        try:
            if self._rule(repo) != ctx['rule']: return 'Saved rules changed; review again.'
        except (ValueError, TypeError, KeyError, AttributeError): return 'Saved rules changed; review again.'
        return None

    def _next_prepare(self, batch):
        if batch != self._batch or not self.running or self._stopped: return
        while self._pending:
            ctx = self._pending.pop(0); problem = self._current(ctx)
            if problem:
                self.messages[ctx['key']] = problem; self.processed += 1; continue
            self._active = ctx; self.phase = 'scan'; ctx['request_id'] = None
            self.messages[ctx['key']] = 'Rescanning current release metadata…'
            self.progress = f'Preparing {self.processed + 1}/{self.total}: {ctx["repo"].label}'
            self.changed.emit()
            try: started = self.scan.start(ctx['repo'].url)
            except (ValueError, TypeError, OSError): started = False
            if not started: self._skip('Release scan could not start. No jobs submitted.')
            return
        if not self.proposals:
            self._end('No eligible matching files; nothing queued.'); return
        self._active = None; self.phase = 'ready'
        self.preparation_timer.stop()
        self.progress = f'{len(self.proposals)} repositories prepared. Waiting for explicit queue confirmation.'
        self.changed.emit(); self.ready.emit(batch)

    def _state(self, phase, request_id, state):
        if self.running and self.phase == phase and self._active is not None and state == 'RUNNING' and self._active['request_id'] is None:
            self._active['request_id'] = request_id

    def _event(self, phase, request_id):
        return (self.running and not self._stopped and self.phase == phase and self._active is not None
                and self._active['request_id'] == request_id)

    @staticmethod
    def _limited(result):
        availability = getattr(result, 'availability', None)
        detail = getattr(result, 'detail', None) or {}
        rate = getattr(result, 'rate_limit', None)
        return (getattr(result, 'code', None) == 'rate_limited' or detail.get('cause_code') == 'rate_limited'
                or (availability is not None and 'rate_limited' in (availability.state, availability.reason))
                or (rate is not None and rate.get('remaining') == 0))

    def _scan_terminal(self, request_id, state, result):
        if self.phase == 'stopping': self._drained(); return
        if not self._event('scan', request_id): return
        if self._limited(result): self.cancel('Rate limit reached. Remaining updates deferred; nothing queued.'); return
        ctx = self._active; problem = self._current(ctx)
        if problem: self._skip(problem); return
        if (state != 'COMPLETED' or not isinstance(result, ScanSuccess) or result.request_id != request_id
                or not result.catalog.complete or result.catalog.repository.provider != 'github'
                or 'github:' + result.catalog.repository.repo_key != ctx['key']):
            self._skip('Release scan incomplete, cancelled, or timed out. Nothing queued.'); return
        ctx['catalog'] = result.catalog; ctx['request_id'] = None; self.phase = 'audit'
        self.messages[ctx['key']] = 'Rechecking health and verified local assets…'; self.changed.emit()
        try: started = self.check.start(ctx['repo'], ctx['root'], prepare=True)
        except (ValueError, TypeError, OSError): started = False
        if not started: self._skip('Health verification could not start. Nothing queued.')

    def _items(self, ctx, fresh):
        problem = eligibility_problem(fresh)
        if problem: return (), problem
        target = parse_repository_url(ctx['repo'].url)
        if fresh.repository.repo_key != target.repo_key: return (), 'Repository identity changed.'
        scan_status = a.assess(target, ScanSuccess('', ctx['catalog']), None)
        wanted = {}
        for old, line, scanned in zip((ctx['checked'].status.stable, ctx['checked'].status.prerelease),
                                      (fresh.stable, fresh.prerelease), (scan_status.stable, scan_status.prerelease)):
            if line.release is None or line.comparison.state not in QUEUEABLE: continue
            if line.release.prerelease and not ctx['rule']['prereleases']: continue
            if (old.comparison.state not in QUEUEABLE or _signature(old.release) != _signature(line.release)
                    or _signature(scanned.release) != _signature(line.release)):
                return (), 'Upstream release metadata changed since review; check and review again.'
            wanted[line.release.release_id] = line
        catalog = replace(ctx['catalog'], releases=tuple(release for release in ctx['catalog'].releases if release.id in wanted))
        try: selected = eligible_items(catalog, ctx['rule'])
        except (ValueError, TypeError, KeyError, AttributeError): return (), 'Saved rules could not be applied; open in Browse.'
        verified = set(fresh.verified_asset_ids)
        items = {(release.id, asset.id): EnqueueItem(release, asset) for release, asset in selected
                 if (release.id, asset.id) not in verified}
        return tuple(items.values()), None

    def _check_terminal(self, request_id, state, result):
        if self.phase == 'stopping': self._drained(); return
        if not self._event('audit', request_id): return
        if self._limited(result): self.cancel('Rate limit reached. Remaining updates deferred; nothing queued.'); return
        ctx = self._active; problem = self._current(ctx)
        if problem: self._skip(problem); return
        if state != 'COMPLETED' or not isinstance(result, wire.PreparedStatus):
            self._skip('Archive verification incomplete, cancelled, or timed out. Nothing queued.'); return
        items, problem = self._items(ctx, result)
        if problem: self._skip(problem); return
        if not items: self._skip('No unarchived files match saved formats and filename rules.'); return
        ctx.update(items=items, fresh=result, prepared_at=self.clock())
        self.proposals.append(ctx); self.messages[ctx['key']] = f'{len(items)} file(s) prepared; not queued.'
        self._active = None; self.processed += 1; self.changed.emit()
        batch = self._batch; QTimer.singleShot(0, lambda: self._next_prepare(batch))

    def _skip(self, message):
        if self._active is not None: self.messages[self._active['key']] = message
        self._active = None; self.processed += 1; self.changed.emit()
        batch = self._batch; QTimer.singleShot(0, lambda: self._next_prepare(batch))

    def confirm(self, batch, accepted):
        if batch != self._batch or not self.running or self.phase != 'ready' or self._stopped: return False
        if not accepted:
            for ctx in self.proposals: self.messages[ctx['key']] = 'Confirmation declined; nothing queued.'
            self._end('Confirmation declined; nothing queued.'); return False
        self.phase = 'queue'; self._queue = list(self.proposals)
        self._next_enqueue(batch); return True

    def _next_enqueue(self, batch):
        if batch != self._batch or not self.running or self.phase != 'queue' or self._stopped: return
        while self._queue:
            ctx = self._queue.pop(0); problem = self._current(ctx)
            if not problem and self.clock() - ctx['prepared_at'] > self.MAX_AGE_SECONDS:
                problem = 'Prepared evidence expired; check and review again.'
            if not problem: problem = eligibility_problem(ctx['fresh'])
            if problem: self.messages[ctx['key']] = problem; continue
            ctx['token'] = -(2**128 + uuid4().int); self._active = ctx
            request = EnqueueRequest(ctx['token'], ctx['catalog'].repository, ctx['root'], ctx['items'], requeue_completed=False)
            if not self.bridge.enqueue(request):
                self.messages[ctx['key']] = 'Queue unavailable; selection was not submitted.'; self._active = None; continue
            self.messages[ctx['key']] = 'Confirmed files submitted to the archive queue.'
            self.progress = 'Queueing ' + ctx['repo'].label; self.ack_timer.start(30000); self.changed.emit(); return
        self._end(f'Finished: {self.created} new file(s) queued; {self.existing} existing job(s) kept.')

    def _enqueued(self, result):
        ctx = self._active
        if self._stopped or self.phase != 'queue' or ctx is None or result.token != ctx.get('token'): return
        self.ack_timer.stop()
        if result.error: self.messages[ctx['key']] = 'Queue submission failed; inspect the Queue tab.'
        else:
            self.created += result.created; self.existing += len(result.existing)
            note = f'{result.created} new file(s) queued; {len(result.existing)} existing job(s) kept.'
            if result.started: note += ' Downloads started.'
            if result.deferred_pending or result.startup_error: note += ' Check the Queue tab; some jobs await Start Queue.'
            self.messages[ctx['key']] = note
        self._active = None; self.changed.emit()
        batch = self._batch; QTimer.singleShot(0, lambda: self._next_enqueue(batch))

    def _ack_timeout(self):
        self._end('Queue acknowledgement unavailable. Submitted jobs may exist; inspect Queue before retrying.')

    def cancel(self, message=None):
        if not self.running: return
        queued = self.phase == 'queue'
        self._stop_message = message or ('Stopped remaining submissions; already submitted jobs are kept.' if queued
                                       else 'Preparation cancelled; nothing queued.')
        for ctx in [*self._pending, *self.proposals, *([self._active] if self._active else [])]:
            self.messages[ctx['key']] = self._stop_message
        self._batch += 1; self.phase = 'stopping'; self._pending.clear(); self._queue.clear()
        self.preparation_timer.stop()
        self.proposals.clear(); self._active = None; self.ack_timer.stop()
        self.scan.cancel(); self.check.cancel(); self._drained(); self.changed.emit()

    def _drained(self):
        if self.phase == 'stopping' and self.scan.job is None and self.check.job is None:
            self._end(self._stop_message)

    def _end(self, message):
        self.running = False; self.phase = 'idle'; self._active = None
        self._pending.clear(); self._queue.clear(); self.proposals.clear(); self.ack_timer.stop()
        self.preparation_timer.stop()
        self.progress = message; self.changed.emit()

    def shutdown(self, timeout_ms=3000):
        if self.running: self.cancel()
        self._stopped = True; self._batch += 1; self.ack_timer.stop()
        scan_done = self.scan.shutdown(timeout_ms=timeout_ms)
        check_done = self.check.shutdown(timeout_ms=timeout_ms)
        if scan_done and check_done: self._end('Health update workers stopped.')
        return bool(scan_done and check_done)
