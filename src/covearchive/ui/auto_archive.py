"""GUI-thread supervision of opt-in automatic checks."""
from pathlib import Path
import time
from PySide6.QtCore import QObject, QTimer, Signal, Qt
from covearchive.auto_archive import Schedule, eligible_items, NEEDS_RULES, NEEDS_ROOT
from covearchive.archive.naming import validate_root
from covearchive.scan_worker import ScanSuccess
from covearchive.ui.archive_bridge import EnqueueRequest, EnqueueItem


class AutoArchive(QObject):
    changed=Signal()

    def __init__(self, scan, bridge, library, settings, *, clock=time.time, tick_ms=30000, parent=None):
        super().__init__(parent)
        self.scan=scan; self.bridge=bridge; self.library=library; self.settings=settings; self.clock=clock
        self.schedule=Schedule(clock); self._end(None,None); self._token=0; self._stopped=False; self._wanted={}
        self.timer=QTimer(self); self.timer.setInterval(tick_ms); self.timer.timeout.connect(self.tick)
        scan.state_changed.connect(self._state); scan.terminal.connect(self._terminal)
        bridge.enqueued.connect(self._enqueued,Qt.QueuedConnection)

    def start(self):
        if self._stopped: return
        self.sync(); self.timer.start()

    def sync(self):
        if self._stopped: return
        wanted={r.ref:r.auto_archive for r in self.library().repositories if r.active and r.auto_archive is not None}
        removed=self.schedule.sync(wanted)
        if self._active is not None and self._active['key'] in removed:
            active=self._active; self._end(active,None)
            if active['token'] is None: self.scan.cancel()
        if wanted!=self._wanted: self._wanted=wanted; self.changed.emit()

    def problem(self, repo):
        settings=self.settings()
        if settings.get('repositories',{}).get(repo.ref) is None: return NEEDS_RULES
        try: validate_root(settings.get('archive_root',''))
        except (ValueError,TypeError): return NEEDS_ROOT
        return None

    def tick(self):
        if self._active is None and self.schedule.checking is not None: self.schedule.release(self.schedule.checking)
        if self._stopped: return
        self.sync()
        if self._active is not None:
            ctx=self._active
            if ctx['token'] is not None:
                ctx['ticks']+=1
                if ctx['ticks']>=20: self._end(ctx,('failed','queue_unavailable',None))
            return
        key=self.schedule.due()
        if key is None: return
        repo=next((r for r in self.library().repositories if r.ref==key and r.active),None)
        if repo is None: return
        settings=self.settings(); rule=settings.get('repositories',{}).get(key)
        if rule is None: self._failed(key,'needs_rules'); return
        try: root=settings.get('archive_root',''); validate_root(root); root=str(Path(root))
        except (ValueError,TypeError): self._failed(key,'needs_root'); return
        if not self.bridge.is_open or self.bridge.shutting_down or self.bridge.closed:
            self._failed(key,'queue_unavailable'); return
        ctx=dict(key=key,rule=rule,root=root,request_id=None,token=None,ticks=0); self._active=ctx
        if not self.scan.start(repo.url):
            self._end(ctx,None); return
        if self._active is ctx: self.schedule.started(key); self.changed.emit()

    def _end(self, ctx, outcome):
        if getattr(self,'_active',None) is not ctx: return
        self._active=None
        if ctx is None: return
        key=ctx['key']
        if outcome is None: self.schedule.release(key); return
        if outcome[0]=='succeeded': self.schedule.succeeded(key,outcome[1])
        else: self.schedule.failed(key,outcome[1],retry_after=outcome[2])
        self.changed.emit()

    def _state(self, request_id, state):
        if not self._stopped and self._active is not None and state=='RUNNING' and self._active['request_id'] is None:
            self._active['request_id']=request_id

    def _failed(self, key, code, retry_after=None):
        self.schedule.failed(key,code,retry_after=retry_after); self.changed.emit()

    def _terminal(self, request_id, state, result):
        active=self._active
        if self._stopped or active is None or active['request_id']!=request_id or active['token'] is not None: return
        key=active['key']
        if state=='CANCELLED': self._end(active,('failed','process',None)); return
        if state!='COMPLETED':
            code='timeout' if state=='TIMED_OUT' else getattr(result,'code','internal_error')
            detail=getattr(result,'detail',None) or {}; hint=None
            if state=='FAILED' and code=='incomplete_scan' and type(detail.get('cause_code')) is str and detail['cause_code'] in ('rate_limited','unauthorized'): code=detail['cause_code']
            if code=='unauthorized' and not getattr(result,'token_rejected',False): code='forbidden'
            for name in ('retry_after_seconds','retry_at','reset_at'):
                value=detail.get(name)
                if type(value) in (int,float): hint=value if name=='retry_after_seconds' else value-self.clock(); break
            self._end(active,('failed',code,hint)); return
        if not isinstance(result,ScanSuccess) or result.catalog.repository.repo_key!=key:
            self._end(active,('failed','internal_error',None)); return
        try: items=eligible_items(result.catalog,active['rule'])
        except (ValueError,TypeError,KeyError,AttributeError):
            self._end(active,('failed','internal_error',None)); return
        if not items:
            self._end(active,('succeeded','No new assets')); return
        self._token-=1; active['token']=self._token
        request=EnqueueRequest(self._token,result.catalog.repository,active['root'],
            tuple(EnqueueItem(r,a) for r,a in items),requeue_completed=False)
        if not self.bridge.enqueue(request): self._end(active,('failed','queue_unavailable',None))

    def _enqueued(self, result):
        active=self._active
        if self._stopped or active is None or active['token'] is None or result.token!=active['token']: return
        if result.error: self._end(active,('failed',result.error,None)); return
        notes=[f'{result.created} new asset(s) queued']
        if result.started: notes.append('downloads started')
        if result.deferred_pending: notes.append(f'{result.deferred_pending} earlier queued job(s) are waiting - press Start Queue on the Queue tab')
        if result.startup_error: notes.append('Downloads could not start; press Start Queue on the Queue tab')
        self._end(active,('succeeded','; '.join(notes)))

    def status(self, key): return self.schedule.status(key)

    def summary(self, repo):
        status=self.status(repo.ref)
        if repo.auto_archive is None: return 'Off'
        return {'checking':'Checking now...','retrying':'Retrying','error':'Error'}.get(
            status.state if status else 'waiting',f'Every {repo.auto_archive} hour'+('' if repo.auto_archive==1 else 's'))

    @staticmethod
    def _time(value): return time.strftime('%Y-%m-%d %H:%M',time.localtime(value)) if value is not None else '-'

    def schedule_text(self, repo):
        status=self.status(repo.ref)
        return f'Last {self._time(status.last_check if status else None)} / Next {self._time(status.next_check if status else None)}'

    def detail(self, repo):
        status=self.status(repo.ref); problem=self.problem(repo) if repo.auto_archive is not None else None
        return '\n'.join(filter(None,(self.summary(repo),self.schedule_text(repo),status.message if status else '',problem)))

    def shutdown(self, *args, **kwargs):
        self._stopped=True; self.timer.stop(); self._end(self._active,None)
        return bool(self.scan.shutdown(*args,**kwargs))
