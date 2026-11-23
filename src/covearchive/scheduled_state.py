"""Bounded durable scheduling evidence; never writes Library or preferences."""
import json
import errno
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from covearchive.auto_archive import TRANSIENT
from covearchive.jsondepth import check_json_depth
from covearchive.library.model import AUTO_ARCHIVE_HOURS, MAX_REPOSITORIES, normalize_repository
from covearchive.queue.store import default_queue_path
from covearchive.queue.models import QueueBusyError

MAX_BYTES=4*1024*1024
_TIMES=('last_attempt','last_success','next_due')


def default_state_path(): return default_queue_path().with_name('scheduled.json')


class ScheduleLock:
    """Nonblocking lifetime lock; contention is distinct from unsafe/unwritable files."""
    def __init__(self,path):
        self.fd=None
        if Path(path).is_symlink(): raise OSError('unsafe_state')
        lock_path=Path(str(path)+'.lock')
        if lock_path.is_symlink(): raise OSError('unsafe_lock')
        fd=os.open(lock_path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_CLOEXEC',0),0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or getattr(info,'st_file_attributes',0)&0x400:
                raise OSError('unsafe_lock')
            try:
                if sys.platform=='win32':
                    import msvcrt
                    msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
                    if info.st_size==0: os.write(fd,b'\0')
                else:
                    import fcntl
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    os.fchmod(fd,0o600)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN,errno.EACCES,errno.EDEADLK): raise QueueBusyError('runner_active') from None
                raise
            self.fd=fd
        except BaseException: os.close(fd); raise

    def close(self):
        if self.fd is not None:
            try:
                if sys.platform=='win32':
                    import msvcrt
                    os.lseek(self.fd,0,os.SEEK_SET); msvcrt.locking(self.fd,msvcrt.LK_UNLCK,1)
            finally: os.close(self.fd); self.fd=None


def private_directory(path):
    missing=[]; directory=Path(path)
    while not directory.exists(): missing.append(directory); directory=directory.parent
    for directory in reversed(missing): directory.mkdir(mode=0o700,exist_ok=True)


def atomic_write(path,payload):
    """Same no-direct-write-fallback policy as LibraryStore, without Qt."""
    path=Path(path); private_directory(path.parent); temporary=None
    try:
        fd,temporary=tempfile.mkstemp(prefix='.'+path.name+'-',suffix='.tmp',dir=path.parent)
        with os.fdopen(fd,'wb') as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,path); temporary=None
        if os.name!='nt':
            fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
    finally:
        if temporary is not None: os.unlink(temporary)


def _keys(value,keys):
    if type(value) is not dict or set(value)!=set(keys): raise ValueError('state_fields')


def _time(value):
    if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=2**40):
        raise ValueError('state_time')


def _code(value):
    if value is not None and (type(value) is not str or re.fullmatch(r'[a-z_]{1,64}',value) is None):
        raise ValueError('state_code')


def _pairs(pairs):
    data={}
    for key,value in pairs:
        if key in data: raise ValueError('duplicate_state_key')
        data[key]=value
    return data


def _reject(_): raise ValueError('state_number')


def _validated(data):
    _keys(data,('version','last_start','last_finish','last_outcome','last_error','blocked_until','repositories'))
    if type(data['version']) is not int or data['version']!=1: raise ValueError('state_version')
    for name in ('last_start','last_finish','blocked_until'): _time(data[name])
    for name in ('last_outcome','last_error'): _code(data[name])
    repos=data['repositories']
    if type(repos) is not dict or len(repos)>MAX_REPOSITORIES: raise ValueError('state_limit')
    for key,entry in repos.items():
        if type(key) is not str or normalize_repository('https://github.com/'+key,'github')[2]!=key:
            raise ValueError('state_repository')
        _keys(entry,('hours',*_TIMES,'failures','last_code'))
        if type(entry['hours']) is not int or entry['hours'] not in AUTO_ARCHIVE_HOURS: raise ValueError('state_interval')
        for name in _TIMES: _time(entry[name])
        if type(entry['failures']) is not int or not 0<=entry['failures']<=3: raise ValueError('state_failures')
        _code(entry['last_code'])
    return data


class State:
    def __init__(self,path,data=None,error=None):
        self.path=Path(path); self.error=error
        self.data=data if data is not None else dict(version=1,last_start=None,last_finish=None,
            last_outcome=None,last_error=None,blocked_until=0,repositories={})

    @classmethod
    def load(cls,path):
        try:
            with Path(path).open('rb') as stream: raw=stream.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES: raise ValueError('state_size')
            text=raw.decode('utf-8'); check_json_depth(text)
            data=_validated(json.loads(text,object_pairs_hook=_pairs,parse_constant=_reject))
            return cls(path,data)
        except FileNotFoundError: return cls(path)
        except (OSError,ValueError,TypeError,RecursionError,OverflowError): return cls(path,error='state_invalid')

    def save(self):
        payload=(json.dumps(_validated(self.data),ensure_ascii=True,allow_nan=False,separators=(',',':'))+'\n').encode()
        if len(payload)>MAX_BYTES: raise ValueError('state_size')
        atomic_write(self.path,payload)

    def sync(self,wanted,now):
        repos=self.data['repositories']; self.data['repositories']={k:v for k,v in repos.items() if k in wanted}
        repos=self.data['repositories']
        self.data['blocked_until']=min(self.data['blocked_until'] or 0,now+3600)
        for key,hours in wanted.items():
            entry=repos.setdefault(key,dict(hours=hours,last_attempt=None,last_success=None,next_due=now,failures=0,last_code=None))
            if entry['hours']!=hours: entry.update(hours=hours,next_due=now,failures=0)
            ceiling=now+hours*3600
            if entry['next_due'] is None: entry['next_due']=now
            elif entry['next_due']>ceiling:
                entry['next_due']=ceiling if (entry['last_attempt'] or 0)>now else now
            if entry['last_code']=='checking': entry.update(next_due=now,last_code='interrupted')

    def succeeded(self,key,now):
        entry=self.data['repositories'][key]
        entry.update(last_success=now,next_due=now+entry['hours']*3600,failures=0,last_code='succeeded')

    def failed(self,key,code,now,retry_after=None):
        entry=self.data['repositories'][key]; delay=entry['hours']*3600; count=0
        if code in TRANSIENT:
            count=entry['failures']+1
            if count<=3: delay=(300,900,1800)[count-1]
            else: count=0
        if code=='rate_limited':
            hint=retry_after if type(retry_after) is int or (type(retry_after) is float and math.isfinite(retry_after)) else 60
            hint=max(60,min(hint,3600)); self.data['blocked_until']=max(self.data['blocked_until'],now+hint)
            if count: delay=hint
        entry.update(last_attempt=now,next_due=now+delay,failures=count,last_code=code)
