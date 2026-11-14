"""Durable jobs; short connections and transactional project exclusion."""
from contextlib import contextmanager
from pathlib import Path
import ntpath
import os
import re
import sqlite3
import threading
import time
import uuid
from covearchive.archive.naming import validate_root
from covearchive.github.urls import RepositoryTarget, parse_github_url
from .models import (JobState, TRANSITIONS, QueueError, QueueCorruptError, QueueVersionError,
    QueueBusyError, UnknownJobError, InvalidTransitionError, InvalidJobError, QueueSettings,
    QueueJob, Enqueued, safe_code)

_WRITES=threading.RLock()
APPLICATION_ID=0x43415131
_SCHEMA="""CREATE TABLE jobs (
seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE, identity TEXT UNIQUE,
provider TEXT, owner TEXT, repo TEXT, repo_key TEXT, release_id INTEGER, asset_id INTEGER,
archive_root TEXT, project_key TEXT, tag_name TEXT, asset_name TEXT, asset_size INTEGER,
state TEXT CHECK(state IN ('QUEUED','RUNNING','PAUSED','RETRY_WAIT','COMPLETED','FAILED','CANCELLED')),
retry_count INTEGER, last_error_code TEXT, next_attempt_at REAL, claimed_by TEXT, stop_requested TEXT,
result_status TEXT, integrity TEXT, transferred INTEGER, created_at REAL, updated_at REAL)"""
_COOLDOWNS='CREATE TABLE cooldowns (scope TEXT PRIMARY KEY, until REAL)'

def default_queue_path():
    root=os.environ.get('LOCALAPPDATA')
    if not root: raise QueueError('localappdata_unset')
    return Path(root)/'CoveArchive'/'queue.sqlite3'

def _job(row):
    if row is None: return None
    data=dict(row); data.pop('stop_requested'); data['state']=JobState(data['state'])
    if data['transferred'] is not None: data['transferred']=bool(data['transferred'])
    return QueueJob(**data)

def _display(text):
    # Display metadata is not needed to fetch by id; never retain embedded secrets.
    return '[redacted]' if re.search(r'://|[?&](?:sig|se|sp|sv|sr|token|X-Amz-[A-Za-z-]+)=',text,re.I) else text

class QueueStore:
    def __init__(self,path=None,*,settings=None,clock=time.time):
        self.path=Path(path) if path is not None else default_queue_path()
        self.settings=settings or QueueSettings(); self.clock=clock
        try: self.path.parent.mkdir(parents=True,exist_ok=True)
        except OSError: raise QueueError('database_unavailable') from None
        with self._write(initializing=True) as c:
            version=c.execute('PRAGMA user_version').fetchone()[0]
            application=c.execute('PRAGMA application_id').fetchone()[0]
            if version>1: raise QueueVersionError(self.path)
            schema=c.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            if version==0 and application==0 and not schema:
                c.execute('PRAGMA journal_mode=DELETE')
                c.execute(_SCHEMA); c.execute(_COOLDOWNS); c.execute('PRAGMA user_version=1'); c.execute(f'PRAGMA application_id={APPLICATION_ID}')
            elif (version!=1 or application!=APPLICATION_ID or [(r[0],r[1]) for r in schema]!=[('cooldowns',_COOLDOWNS),('jobs',_SCHEMA)]
                    or c.execute('PRAGMA quick_check').fetchone()[0]!='ok'
                    or c.execute('PRAGMA journal_mode').fetchone()[0]!='delete'):
                raise QueueCorruptError(self.path)

    @contextmanager
    def _connection(self):
        c=None
        try:
            c=sqlite3.connect(str(self.path),isolation_level=None,timeout=self.settings.busy_timeout_seconds)
            c.row_factory=sqlite3.Row
            c.execute('PRAGMA synchronous=FULL')
            c.execute(f'PRAGMA busy_timeout={int(self.settings.busy_timeout_seconds*1000)}')
            yield c
        except sqlite3.OperationalError as exc:
            message=str(exc).lower()
            if 'locked' in message or 'busy' in message: raise QueueBusyError('database_busy') from None
            unavailable=any(s in message for s in ('unable to open','readonly','read-only','permission','disk i/o'))
            raise QueueError('database_unavailable' if unavailable else 'database_error') from None
        except sqlite3.Error as exc:
            if getattr(exc,'sqlite_errorcode',0)&255 in (sqlite3.SQLITE_NOTADB,sqlite3.SQLITE_CORRUPT):
                raise QueueCorruptError(self.path) from None
            raise QueueError('database_error') from None
        finally:
            if c is not None: c.close()

    @contextmanager
    def _write(self,*,initializing=False):
        with _WRITES, self._connection() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                if not initializing:
                    version=c.execute('PRAGMA user_version').fetchone()[0]
                    if version>1: raise QueueVersionError(self.path)
                    if version!=1 or c.execute('PRAGMA application_id').fetchone()[0]!=APPLICATION_ID:
                        raise QueueCorruptError(self.path)
                yield c; c.commit()
            except BaseException: c.rollback(); raise

    def _get(self,c,job_id):
        row=c.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
        if row is None: raise UnknownJobError('unknown_job')
        return _job(row)

    def get(self,job_id):
        with self._connection() as c: return self._get(c,job_id)

    def list_jobs(self,states=None):
        with self._connection() as c:
            if states is None: rows=c.execute('SELECT * FROM jobs ORDER BY seq')
            else:
                values=tuple(JobState(s).value for s in states)
                rows=c.execute('SELECT * FROM jobs WHERE state IN ('+','.join('?' for _ in values)+') ORDER BY seq',values)
            return tuple(map(_job,rows.fetchall()))

    def counts(self):
        with self._connection() as c:
            result=dict.fromkeys(JobState,0)
            for state,count in c.execute('SELECT state,count(*) FROM jobs GROUP BY state'): result[JobState(state)]=count
            return result

    def _validate(self,repository,release,asset,root):
        try:
            if not isinstance(repository,RepositoryTarget) or parse_github_url(
                    'https://github.com/'+repository.owner+'/'+repository.repo)!=repository: raise ValueError
            validate_root(root)
            root_key=ntpath.normpath(str(root).replace('/','\\')).casefold()
            if (any(type(i) is not int or not 1<=i<=2**63-1 for i in (release.id,asset.id))
                    or not any(a.id==asset.id for a in release.assets) or release.draft
                    or asset.state!='uploaded' or asset.supported_extension is None
                    or type(asset.size) is not int or not 0<=asset.size<=2**63-1
                    or any(not isinstance(s,str) or not 1<=len(s)<=1024 for s in (asset.name,release.tag_name))): raise ValueError
        except (ValueError,TypeError,AttributeError): raise InvalidJobError('invalid_job') from None
        key=repository.repo_key; project=root_key+'|'+key
        return ('github|'+key+'|'+str(release.id)+'|'+str(asset.id)+'|'+root_key,
            'github',repository.owner,repository.repo,key,release.id,asset.id,str(root),project,
            _display(release.tag_name),_display(asset.name),asset.size)

    def enqueue(self,repository,release,asset,archive_root):
        return self.enqueue_many(((repository,release,asset,archive_root),))[0]

    def enqueue_many(self,items):
        values=[self._validate(*item) for item in items]; result=[]
        with self._write() as c:
            for value in values:
                old=c.execute('SELECT * FROM jobs WHERE identity=?',(value[0],)).fetchone()
                if old is not None:
                    outcome='existing'
                    if old['state']=='COMPLETED':
                        c.execute("UPDATE jobs SET state='QUEUED',retry_count=0,last_error_code=NULL,next_attempt_at=NULL,claimed_by=NULL,result_status=NULL,integrity=NULL,transferred=NULL,updated_at=? WHERE job_id=?",(self.clock(),old['job_id']))
                        outcome='requeued'
                    result.append(Enqueued(self._get(c,old['job_id']),outcome)); continue
                job_id=uuid.uuid4().hex; now=self.clock()
                c.execute("INSERT INTO jobs (job_id,identity,provider,owner,repo,repo_key,release_id,asset_id,archive_root,project_key,tag_name,asset_name,asset_size,state,retry_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'QUEUED',0,?,?)",(job_id,*value,now,now))
                result.append(Enqueued(self._get(c,job_id),'created'))
        return tuple(result)

    def claim_next(self,owner,*,now=None):
        if not isinstance(owner,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',owner): raise InvalidJobError('owner')
        now=self.clock() if now is None else now
        with self._write() as c:
            row=c.execute("SELECT j.job_id FROM jobs j WHERE j.state IN ('QUEUED','RETRY_WAIT') AND (j.next_attempt_at IS NULL OR j.next_attempt_at<=?) AND NOT EXISTS (SELECT 1 FROM jobs r WHERE r.state='RUNNING' AND r.project_key=j.project_key) AND NOT EXISTS (SELECT 1 FROM cooldowns WHERE until > ?) ORDER BY j.seq LIMIT 1",(now,now)).fetchone()
            if row is None: return None
            changed=c.execute("UPDATE jobs SET state='RUNNING',claimed_by=?,stop_requested=NULL,next_attempt_at=NULL,updated_at=? WHERE job_id=? AND state IN ('QUEUED','RETRY_WAIT')",(owner,now,row[0])).rowcount
            if changed!=1: raise InvalidTransitionError('claim_race')
            return self._get(c,row[0])

    def cooldown_until(self):
        with self._connection() as c:
            row=c.execute("SELECT until FROM cooldowns WHERE scope='github'").fetchone()
            return row[0] if row else None

    def request_stop(self,job_id,owner,action):
        with self._write() as c:
            old=self._get(c,job_id)
            if action not in ('pause','cancel') or old.state!=JobState.RUNNING or old.claimed_by!=owner:
                raise InvalidTransitionError('ownership')
            c.execute("UPDATE jobs SET stop_requested=CASE WHEN stop_requested='cancel' THEN 'cancel' ELSE ? END,updated_at=? WHERE job_id=?",(action,self.clock(),job_id))
            return self._get(c,job_id)

    def finish(self,job_id,owner,to,*,code=None,next_attempt_at=None,retry_count=None,result=None,cooldown_until=None):
        to=JobState(to)
        with self._write() as c:
            old=self._get(c,job_id)
            if old.state!=JobState.RUNNING or old.claimed_by!=owner or to not in TRANSITIONS[old.state]: raise InvalidTransitionError('ownership')
            status=getattr(result,'status',None)
            status=getattr(status,'value',status)
            if to==JobState.COMPLETED and status not in ('ARCHIVED','ALREADY_ARCHIVED','VERIFIED_EXISTING'): raise InvalidTransitionError('success_required')
            if cooldown_until is not None:
                c.execute("INSERT INTO cooldowns(scope,until) VALUES ('github',?) ON CONFLICT(scope) DO UPDATE SET until=max(until,excluded.until)",(cooldown_until,))
            integrity=getattr(result,'integrity',None)
            if integrity not in (None,'LOCAL_HASH_ONLY','PUBLISHER_VERIFIED'): integrity=None
            known=('ARCHIVED','ALREADY_ARCHIVED','VERIFIED_EXISTING','MODIFIED','CONFLICT','MISMATCH','CANCELLED','UPSTREAM_REMOVED','RECONCILE_REQUIRED','FAILED')
            c.execute('UPDATE jobs SET state=?,claimed_by=NULL,stop_requested=NULL,last_error_code=?,next_attempt_at=?,retry_count=?,result_status=?,integrity=?,transferred=?,updated_at=? WHERE job_id=? AND state=\'RUNNING\' AND claimed_by=?',
                (to.value,None if to==JobState.COMPLETED or code is None else safe_code(code),next_attempt_at,
                 old.retry_count if retry_count is None else retry_count,status if status in known else None,
                 integrity,bool(getattr(result,'transferred',False)) if result is not None else None,self.clock(),job_id,owner))
            return self._get(c,job_id)

    def transition(self,job_id,to,expected=None):
        to=JobState(to)
        with self._write() as c:
            old=self._get(c,job_id)
            if (old.state==JobState.RUNNING or to==JobState.RUNNING or to not in TRANSITIONS[old.state]
                    or (expected is not None and old.state!=expected)): raise InvalidTransitionError('transition')
            retry=to==JobState.QUEUED and old.state in (JobState.FAILED,JobState.CANCELLED,JobState.COMPLETED)
            c.execute('UPDATE jobs SET state=?,retry_count=?,last_error_code=?,updated_at=? WHERE job_id=?',
                (to.value,0 if retry else old.retry_count,None if retry else old.last_error_code,self.clock(),job_id))
            return self._get(c,job_id)

    def recover_interrupted(self,keep=(),*,max_attempts=None):
        keep=tuple(keep)
        max_attempts=self.settings.max_attempts if max_attempts is None else max_attempts
        with self._write() as c:
            return c.execute("UPDATE jobs SET state=CASE WHEN stop_requested='cancel' THEN 'CANCELLED' WHEN stop_requested='pause' THEN 'PAUSED' WHEN retry_count+1>=? THEN 'FAILED' ELSE 'QUEUED' END,retry_count=retry_count+CASE WHEN stop_requested IN ('pause','cancel') THEN 0 ELSE 1 END,claimed_by=NULL,last_error_code=CASE WHEN stop_requested IN ('pause','cancel') THEN NULL ELSE 'interrupted' END,stop_requested=NULL,updated_at=? WHERE state='RUNNING'"+
                (' AND job_id NOT IN ('+','.join('?' for _ in keep)+')' if keep else ''),(max_attempts,self.clock(),*keep)).rowcount

class RunnerLock:
    def __init__(self,path):
        import msvcrt
        self._file=open(str(path)+'.lock','a+b')
        try:
            self._file.seek(0,2)
            if not self._file.tell(): self._file.write(b'\0'); self._file.flush()
            self._file.seek(0); msvcrt.locking(self._file.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            self._file.close(); self._file=None
            raise QueueBusyError('runner_active') from None
    def close(self):
        if self._file is not None:
            import msvcrt
            try: self._file.seek(0); msvcrt.locking(self._file.fileno(),msvcrt.LK_UNLCK,1)
            finally: self._file.close(); self._file=None
    def __enter__(self): return self
    def __exit__(self,*_): self.close()

__all__=('default_queue_path','QueueStore','RunnerLock')
