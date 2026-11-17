"""In-memory watches and Browse-compatible eligibility; no I/O or Qt."""
from dataclasses import dataclass
from math import isfinite
import time
from covearchive.build_filters import BuildRules, allowed
from covearchive.github.select import select_releases
from covearchive.library.model import AUTO_ARCHIVE_HOURS

TRANSIENT = frozenset(('network','timeout','server_error','incomplete_scan','process','protocol',
    'rate_limited','queue_unavailable','database_busy','database_unavailable'))
NEEDS_RULES = "Scan this repository on the Browse tab, choose formats and filters, press 'Save rules for this repository', then enable Auto-Archive."
NEEDS_ROOT = "Choose an archive folder on the Browse tab and archive something once to save it."


def failure_text(code, *, retry_at=None):
    if code=='rate_limited':
        try: resume=time.strftime('%Y-%m-%d %H:%M',time.localtime(retry_at)) if retry_at is not None else 'after the rate-limit delay'
        except (ValueError,OverflowError,OSError,TypeError): resume='after the rate-limit delay'
        return 'GitHub rate limit reached; checks resume '+resume
    return {
        'network':'No connection to GitHub; will retry automatically',
        'timeout':'GitHub check timed out; will retry automatically',
        'server_error':'GitHub is temporarily unavailable; will retry automatically',
        'incomplete_scan':'GitHub check was incomplete; will retry automatically',
        'process':'The scan worker stopped; will retry automatically',
        'protocol':'The scan worker returned an invalid response; will retry automatically',
        'queue_unavailable':'The archive queue is unavailable; will retry automatically',
        'database_busy':'The archive queue is busy; will retry automatically',
        'database_unavailable':'The archive queue database is unavailable; will retry automatically',
        'not_found':'Repository not found; check the URL or turn Auto-Archive off',
        'forbidden':'GitHub denied access; check repository access or turn Auto-Archive off',
        'unauthorized':'GitHub rejected the access token; replace or clear it in Settings, or turn Auto-Archive off',
        'needs_rules':NEEDS_RULES,'needs_root':NEEDS_ROOT,
    }.get(code,'Auto-Archive could not check this repository; try a scan on the Browse tab')


@dataclass(frozen=True)
class WatchStatus:
    key: str
    hours: int
    state: str
    last_check: float | None
    next_check: float | None
    message: str


@dataclass
class _Watch:
    hours: int
    next_due: float
    state: str = 'waiting'
    last_check: float | None = None
    message: str = ''
    failures: int = 0


class Schedule:
    def __init__(self, clock=time.time):
        self.clock=clock; self._watches={}; self._checking=None; self.blocked_until=0

    def _read(self):
        now=self.clock()
        self.blocked_until=min(self.blocked_until,now+3600)
        for watch in self._watches.values(): watch.next_due=min(watch.next_due,now+watch.hours*3600)
        return now

    def sync(self, wanted):
        if any(type(h) is not int or h not in AUTO_ARCHIVE_HOURS for h in wanted.values()):
            raise ValueError('Invalid Auto-Archive interval')
        now=self._read(); removed=tuple(sorted(self._watches.keys()-wanted.keys()))
        for key in removed:
            del self._watches[key]
            if self._checking==key: self._checking=None
        for key,hours in wanted.items():
            watch=self._watches.get(key)
            if watch is None: self._watches[key]=_Watch(hours,now+hours*3600)
            elif watch.hours!=hours: watch.hours=hours; watch.next_due=now+hours*3600
        return removed

    def due(self):
        now=self._read()
        if self._checking is not None or now<self.blocked_until: return None
        pending=[(w.next_due,key) for key,w in self._watches.items() if w.next_due<=now]
        return min(pending)[1] if pending else None

    def started(self, key):
        self._read()
        if key not in self._watches or self._checking is not None: return
        self._checking=key; self._watches[key].state='checking'

    @property
    def checking(self): return self._checking

    def release(self, key):
        if self._checking==key: self._checking=None
        watch=self._watches.get(key)
        if watch is not None and watch.state=='checking': watch.state='waiting'

    def succeeded(self, key, message):
        now=self._read(); watch=self._watches.get(key)
        if watch is None: return
        if self._checking==key: self._checking=None
        watch.failures=0; watch.last_check=now; watch.next_due=now+watch.hours*3600
        watch.state='waiting'; watch.message=message

    def failed(self, key, code, retry_after=None, message=None):
        now=self._read(); watch=self._watches.get(key)
        if watch is None: return
        if self._checking==key: self._checking=None
        delay=watch.hours*3600; watch.state='error'
        if code in TRANSIENT:
            watch.failures+=1
            if watch.failures<=3: delay=(300,900,1800)[watch.failures-1]; watch.state='retrying'
            else: watch.failures=0
        else: watch.failures=0
        if code=='rate_limited':
            hint=retry_after if type(retry_after) is int or (type(retry_after) is float and isfinite(retry_after)) else 60
            hint=max(60,min(hint,3600)); self.blocked_until=max(self.blocked_until,now+hint)
            if watch.state=='retrying': delay=hint
        watch.last_check=now; watch.next_due=now+delay
        watch.message=message or failure_text(code,retry_at=self.blocked_until if code=='rate_limited' else None)

    def status(self, key):
        self._read(); watch=self._watches.get(key)
        if watch is None: return None
        return WatchStatus(key,watch.hours,watch.state,watch.last_check,
            None if self._checking==key else watch.next_due,watch.message)


def eligible_items(catalog, rule):
    rules=BuildRules(rule['include'],rule['exclude'])
    selection=select_releases(catalog,include_prereleases=rule['prereleases'],enabled_extensions=rule['extensions'])
    return tuple((row.release,match.asset) for row in selection.releases for match in row.matches
        if all(isinstance(s,str) and 1<=len(s)<=1024 for s in (row.release.tag_name,match.asset.name))
        and all(type(i) is int and 1<=i<=2**63-1 for i in (row.release.id,match.asset.id))
        and type(match.asset.size) is int and 0<=match.asset.size<=2**63-1
        and allowed(rules,match.asset.name))
