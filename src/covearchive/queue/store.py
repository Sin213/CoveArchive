"""Durable jobs; short connections and transactional project exclusion."""
from contextlib import contextmanager, nullcontext
import hashlib
import json
from pathlib import Path
import ntpath
import os
import re
import sqlite3
import stat
import sys
import threading
import time
import uuid
from covearchive.archive.naming import validate_root
from covearchive.providers import validate_repository
from covearchive.github.models import release_key_ok
from .models import (JobState, TRANSITIONS, QueueError, QueueCorruptError, QueueVersionError,
    QueueBusyError, UnknownJobError, InvalidTransitionError, InvalidJobError, QueueSettings,
    QueueJob, Enqueued, safe_code)

_WRITES=threading.RLock()
APPLICATION_ID=0x43415131
_SCHEMA_V1="""CREATE TABLE jobs (
seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE, identity TEXT UNIQUE,
provider TEXT, owner TEXT, repo TEXT, repo_key TEXT, release_id INTEGER, asset_id INTEGER,
archive_root TEXT, project_key TEXT, tag_name TEXT, asset_name TEXT, asset_size INTEGER,
state TEXT CHECK(state IN ('QUEUED','RUNNING','PAUSED','RETRY_WAIT','COMPLETED','FAILED','CANCELLED')),
retry_count INTEGER, last_error_code TEXT, next_attempt_at REAL, claimed_by TEXT, stop_requested TEXT,
result_status TEXT, integrity TEXT, transferred INTEGER, created_at REAL, updated_at REAL)"""
_SCHEMA=_SCHEMA_V1[:-1]+', host TEXT)'
_SCHEMA_V3=_SCHEMA[:-1]+', project_id INTEGER, release_key TEXT, file_id INTEGER)'
_COOLDOWNS='CREATE TABLE cooldowns (scope TEXT PRIMARY KEY, until REAL)'

def default_queue_path():
    if sys.platform != 'win32':
        state=os.environ.get('XDG_STATE_HOME','')
        base=Path(state) if os.path.isabs(state) else Path.home()/'.local'/'state'
        return base/'covearchive'/'queue.sqlite3'
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
        try:
            if sys.platform == 'win32': self.path.parent.mkdir(parents=True,exist_ok=True)
            else:
                missing,directory=[],self.path.parent
                while not directory.exists():
                    missing.append(directory); directory=directory.parent
                for directory in reversed(missing): directory.mkdir(mode=0o700,exist_ok=True)
        except OSError: raise QueueError('database_unavailable') from None
        # Inspect the SQLite header without opening a writer before runner exclusion.
        try:
            header=b''
            if self.path.is_file():
                with self.path.open('rb') as source: header=source.read(72)
        except OSError: raise QueueError('database_unavailable') from None
        migrating=header[:16]==b'SQLite format 3\0' and int.from_bytes(header[60:64],'big') in (1,2)
        try:
            if migrating and self.path.stat().st_nlink!=1: raise OSError
        except OSError: raise QueueError('database_unavailable') from None
        with (RunnerLock(self.path) if migrating else nullcontext()), self._write(initializing=True) as c:
            version=c.execute('PRAGMA user_version').fetchone()[0]
            application=c.execute('PRAGMA application_id').fetchone()[0]
            if version>3: raise QueueVersionError(self.path)
            schema=c.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            if version==0 and application==0 and not schema:
                c.execute('PRAGMA journal_mode=DELETE')
                c.execute(_SCHEMA_V3); c.execute(_COOLDOWNS); c.execute('PRAGMA user_version=3'); c.execute(f'PRAGMA application_id={APPLICATION_ID}')
            else:
                self._check_schema(c,version)
                if version==1:
                    if not migrating: raise QueueBusyError('runner_active')
                    self._migrate(c)
                if version in (1,2):
                    if not migrating: raise QueueBusyError('runner_active')
                    self._migrate_v3(c)
                self._check_schema(c,3)

    def _check_schema(self,c,version,*,full=True):
        schema=c.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        if (version not in (1,2,3) or c.execute('PRAGMA application_id').fetchone()[0]!=APPLICATION_ID
                or [(r[0],r[1]) for r in schema]!=[('cooldowns',_COOLDOWNS),('jobs',{1:_SCHEMA_V1,2:_SCHEMA,3:_SCHEMA_V3}[version])]
                or full and (c.execute('PRAGMA quick_check').fetchone()[0]!='ok'
                    or c.execute('PRAGMA journal_mode').fetchone()[0]!='delete')):
            raise QueueCorruptError(self.path)

    def _migration_snapshot(self,c,columns):
        rows=[tuple(r) for r in c.execute('SELECT '+columns+' FROM jobs ORDER BY seq')]
        cooldowns=[tuple(r) for r in c.execute('SELECT * FROM cooldowns ORDER BY scope')]
        return len(rows),hashlib.sha256(repr((rows,cooldowns)).encode()).digest()

    def _migrate(self,c):
        columns=','.join(r[1] for r in c.execute('PRAGMA table_info(jobs)'))
        before=self._migration_snapshot(c,columns)
        sequence=c.execute("SELECT seq FROM sqlite_sequence WHERE name='jobs'").fetchone()
        c.execute('ALTER TABLE jobs RENAME TO jobs_v1'); c.execute(_SCHEMA)
        c.execute('INSERT INTO jobs ('+columns+",host) SELECT "+columns+",'github.com' FROM jobs_v1 ORDER BY seq")
        if self._migration_snapshot(c,columns)!=before: raise QueueCorruptError(self.path)
        c.execute('DROP TABLE jobs_v1')
        if sequence:
            c.execute("DELETE FROM sqlite_sequence WHERE name='jobs'")
            c.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('jobs',?)",(sequence[0],))
        c.execute('PRAGMA user_version=2')

    def _migrate_v3(self,c):
        columns=','.join(r[1] for r in c.execute('PRAGMA table_info(jobs)'))
        before=self._migration_snapshot(c,columns)
        sequence=c.execute("SELECT seq FROM sqlite_sequence WHERE name='jobs'").fetchone()
        c.execute('ALTER TABLE jobs RENAME TO jobs_v2'); c.execute(_SCHEMA_V3)
        c.execute('INSERT INTO jobs ('+columns+') SELECT '+columns+' FROM jobs_v2 ORDER BY seq')
        if self._migration_snapshot(c,columns)!=before: raise QueueCorruptError(self.path)
        c.execute('DROP TABLE jobs_v2')
        if sequence:
            c.execute("DELETE FROM sqlite_sequence WHERE name='jobs'")
            c.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('jobs',?)",(sequence[0],))
        c.execute('PRAGMA user_version=3')

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
                    if version>3: raise QueueVersionError(self.path)
                    if version!=3:
                        raise QueueCorruptError(self.path)
                    self._check_schema(c,3,full=False)
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
            validate_repository(repository)
            validate_root(root)
            root_key=(ntpath.normpath(str(root).replace('/','\\')).casefold()
                      if sys.platform == 'win32' else str(root))
            gitlab=repository.provider=='gitlab'
            if (not release_key_ok(repository.provider,release.id,release.tag_name)
                    or type(asset.id) is not int or not 1<=asset.id<=2**63-1
                    or gitlab and (repository.project_id is None or type(asset.file_id) is not int or not 1<=asset.file_id<=2**63-1)
                    or not any(a.id==asset.id for a in release.assets) or release.draft
                    or asset.state!='uploaded' or asset.supported_extension is None
                    or type(asset.size) is not int or not 0<=asset.size<=2**63-1
                    or any(not isinstance(s,str) or not 1<=len(s)<=1024 for s in (asset.name,release.tag_name))): raise ValueError
        except (ValueError,TypeError,AttributeError): raise InvalidJobError('invalid_job') from None
        key=repository.repo_key; project=root_key+'|'+key
        identity=(json.dumps([repository.provider,key,repository.project_id,release.id,asset.id,asset.file_id,root_key],ensure_ascii=True,separators=(',',':'))
                  if gitlab else repository.provider+'|'+key+'|'+str(release.id)+'|'+str(asset.id)+'|'+root_key)
        return (identity,repository.provider,repository.owner,repository.repo,key,None if gitlab else release.id,asset.id,str(root),project,
            _display(release.tag_name),_display(asset.name),asset.size,repository.host,
            repository.project_id,release.id if gitlab else None,asset.file_id if gitlab else None)

    def enqueue(self,repository,release,asset,archive_root):
        return self.enqueue_many(((repository,release,asset,archive_root),))[0]

    def enqueue_many(self,items,*,requeue_completed=True):
        values=[self._validate(*item) for item in items]; result=[]
        with self._write() as c:
            for value in values:
                old=c.execute('SELECT * FROM jobs WHERE identity=?',(value[0],)).fetchone()
                if old is not None:
                    outcome='existing'
                    if requeue_completed and old['state']=='COMPLETED':
                        c.execute("UPDATE jobs SET state='QUEUED',retry_count=0,last_error_code=NULL,next_attempt_at=NULL,claimed_by=NULL,result_status=NULL,integrity=NULL,transferred=NULL,updated_at=? WHERE job_id=?",(self.clock(),old['job_id']))
                        outcome='requeued'
                    result.append(Enqueued(self._get(c,old['job_id']),outcome)); continue
                job_id=uuid.uuid4().hex; now=self.clock()
                c.execute("INSERT INTO jobs (job_id,identity,provider,owner,repo,repo_key,release_id,asset_id,archive_root,project_key,tag_name,asset_name,asset_size,host,project_id,release_key,file_id,state,retry_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'QUEUED',0,?,?)",(job_id,*value,now,now))
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
        # Legacy 'github' scope remains a conservative queue-wide cooldown for both providers.
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
        path=os.path.realpath(path)
        if sys.platform != 'win32':
            import fcntl
            self._file=None
            fd=None
            try:
                fd=os.open(str(path)+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK,0o600)
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise OSError('unsafe lock')
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self._file=os.fdopen(fd,'r+b')
            except OSError:
                if fd is not None: os.close(fd)
                raise QueueBusyError('runner_active') from None
            return
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
            if sys.platform != 'win32':
                # Closing the last descriptor releases flock; never unlink it.
                self._file.close(); self._file=None
                return
            import msvcrt
            try: self._file.seek(0); msvcrt.locking(self._file.fileno(),msvcrt.LK_UNLCK,1)
            finally: self._file.close(); self._file=None
    def __enter__(self): return self
    def __exit__(self,*_): self.close()

__all__=('default_queue_path','QueueStore','RunnerLock')
