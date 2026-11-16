"""Pure queue contracts; no transfer or SQLite imports."""
from dataclasses import dataclass
from enum import Enum
import math
import re

class JobState(str, Enum):
    QUEUED='QUEUED'; RUNNING='RUNNING'; PAUSED='PAUSED'; RETRY_WAIT='RETRY_WAIT'
    COMPLETED='COMPLETED'; FAILED='FAILED'; CANCELLED='CANCELLED'

class QueueError(Exception):
    def __init__(self, code): self.code=code; super().__init__(code)
class QueueCorruptError(QueueError):
    def __init__(self,path):
        from pathlib import Path
        self.path=Path(path); super().__init__('corrupt'); self.args=('corrupt: '+str(path),)
class QueueVersionError(QueueError):
    def __init__(self,path):
        from pathlib import Path
        self.path=Path(path); super().__init__('unsupported_version'); self.args=('unsupported_version: '+str(path),)
class QueueBusyError(QueueError): pass
class UnknownJobError(QueueError): pass
class InvalidTransitionError(QueueError): pass
class InvalidJobError(QueueError): pass

TRANSITIONS={JobState(k):frozenset(map(JobState,v)) for k,v in {
    'QUEUED':('RUNNING','PAUSED','CANCELLED'), 'RETRY_WAIT':('RUNNING','PAUSED','CANCELLED'),
    'RUNNING':('COMPLETED','RETRY_WAIT','FAILED','PAUSED','CANCELLED','QUEUED'),
    'PAUSED':('QUEUED','CANCELLED'), 'COMPLETED':('QUEUED',), 'FAILED':('QUEUED',),
    'CANCELLED':('QUEUED',)}.items()}

def safe_code(code):
    return code if isinstance(code,str) and re.fullmatch(r'[a-z_]{1,64}',code) else 'unknown_error'

@dataclass(frozen=True)
class QueueSettings:
    max_active: int=2
    max_attempts: int=5
    retry_base_seconds: float=5
    retry_max_seconds: float=300
    shutdown_timeout_seconds: float=30.0
    busy_timeout_seconds: float=5.0
    def __post_init__(self):
        for name,high in (('max_active',8),('max_attempts',20)):
            value=getattr(self,name)
            if type(value) is not int or not 1<=value<=high: raise QueueError(name)
        for name in ('retry_base_seconds','retry_max_seconds','shutdown_timeout_seconds','busy_timeout_seconds'):
            value=getattr(self,name)
            if type(value) not in (int,float) or not math.isfinite(value) or value<0: raise QueueError(name)
        if self.retry_base_seconds>self.retry_max_seconds: raise QueueError('retry_base_seconds')

@dataclass(frozen=True)
class QueueJob:
    seq: int
    job_id: str
    identity: str
    provider: str
    owner: str
    repo: str
    repo_key: str
    release_id: int
    asset_id: int
    archive_root: str
    project_key: str
    tag_name: str
    asset_name: str
    asset_size: int
    state: JobState
    retry_count: int
    last_error_code: str | None
    next_attempt_at: float | None
    claimed_by: str | None
    result_status: str | None
    integrity: str | None
    transferred: bool | None
    created_at: float
    updated_at: float
    host: str='github.com'

@dataclass(frozen=True)
class Enqueued:
    job: QueueJob
    outcome: str

@dataclass(frozen=True)
class Outcome:
    kind: str
    code: str | None
    retry_after_seconds: float | None=None

NEVER_RETRY=frozenset({'unknown_cdn_host','disk_full'})
LIMITED_RETRY={'manifest_commit_incomplete':2}
def classify_result(result):
    code=None if result.code is None else safe_code(result.code)
    if result.status in ('ARCHIVED','ALREADY_ARCHIVED','VERIFIED_EXISTING'): kind='completed'
    elif result.status=='CANCELLED': kind='cancelled'
    elif result.status=='FAILED' and result.retryable is True and code not in NEVER_RETRY: kind='retry'
    else: kind='failed'
    return Outcome(kind,code,result.retry_after_seconds)

def backoff_seconds(n, settings, rng):
    delay=min(settings.retry_max_seconds,settings.retry_base_seconds*2**(n-1))
    return min(settings.retry_max_seconds,delay+rng()*0.2*delay)

__all__=('JobState','TRANSITIONS','QueueError','QueueCorruptError','QueueVersionError','QueueBusyError',
    'UnknownJobError','InvalidTransitionError','InvalidJobError','QueueSettings','QueueJob','Enqueued',
    'Outcome','NEVER_RETRY','LIMITED_RETRY','classify_result','backoff_seconds','safe_code')
