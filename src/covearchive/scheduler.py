"""Explicit per-user OS registration; no shell, credentials, or launch-time writes."""
from dataclasses import dataclass
import csv
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from covearchive.scheduled_state import atomic_write

SERVICE='covearchive-scheduled.service'
TIMER='covearchive-scheduled.timer'
TASK_PREFIX='\\CoveArchive Scheduled Preservation'
_MARKER='# CoveArchive scheduled preservation\n'
_NS='http://schemas.microsoft.com/windows/2004/02/mit/task'


@dataclass(frozen=True)
class Status:
    enabled: bool | None
    state: str
    error: str | None=None


def launch_command(launcher=None,*,executable=None,frozen=None):
    executable=os.path.abspath(executable or sys.executable)
    if frozen is None: frozen=getattr(sys,'frozen',False)
    if frozen:
        return [executable,'--scheduled-run']
    launcher=Path(launcher or Path(__file__).resolve().parents[2]/'run_covearchive.py').resolve()
    return [executable,'-I','-B',str(launcher),'--scheduled-run']


def _onefile(*,frozen=None,executable=None,bundle=None):
    """Detect onefile packaging; fail closed when frozen paths are uncertain."""
    if frozen is None: frozen=getattr(sys,'frozen',False)
    if executable is None: executable=sys.executable
    if bundle is None: bundle=getattr(sys,'_MEIPASS',None)
    if not frozen: return False
    if not bundle or not executable: return True
    home=os.path.normcase(os.path.realpath(os.path.dirname(os.path.abspath(executable))))
    base=os.path.normcase(os.path.realpath(bundle))
    if os.path.basename(base).lower().startswith('_mei'): return True
    try: return os.path.commonpath((home,base))!=home
    except ValueError: return True


def _text(value):
    if not isinstance(value,str) or not value or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError('scheduler_path')
    return value


def systemd_quote(value,*,executable=False):
    # The executable token does NOT expand $ (verified by the lead on systemd).
    # Keep %% for specifiers in both tokens per systemd.service(5); native % in
    # the executable path remains a lead verification gate. Arguments expand $.
    value=_text(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%')
    if not executable: value=value.replace('$','$$')
    return '"'+value+'"'


class _Commands:
    def __init__(self,runner): self.runner=runner
    def call(self,argv):
        try:
            result=self.runner(argv,capture_output=True,text=True,errors='replace',timeout=5,check=False)  # schtasks emits OEM text
            return result,None
        except subprocess.TimeoutExpired: return None,'scheduler_timeout'
        except (OSError,ValueError,subprocess.SubprocessError): return None,'scheduler_unavailable'


class Systemd(_Commands):
    def __init__(self,directory,argv,*,runner=subprocess.run):
        super().__init__(runner); self.directory=Path(directory); self.argv=list(argv)

    def status(self):
        result,error=self.call(['systemctl','--user','show',TIMER,'--property=LoadState,ActiveState,UnitFileState'])
        if error: return Status(None,'unavailable',error)
        fields=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
        if fields.get('LoadState')=='not-found': return Status(False,'off')
        if result.returncode: return Status(None,'unavailable','scheduler_unavailable')
        if not {'LoadState','ActiveState','UnitFileState'}<=fields.keys():
            return Status(None,'unavailable','scheduler_protocol')
        enabled=fields.get('UnitFileState') in ('enabled','enabled-runtime','linked','linked-runtime')
        # A linked but inactive timer is not enabled. A manually started timer still wakes.
        active=fields.get('ActiveState')=='active'
        enabled=active or enabled and fields.get('UnitFileState') not in ('linked','linked-runtime')
        return Status(enabled,'on' if active else 'inactive' if enabled else 'off')

    def _owned(self):
        for name in (SERVICE,TIMER):
            path=self.directory/name
            if path.is_symlink(): return False
            try:
                with path.open('rb') as stream: prefix=stream.read(len(_MARKER.encode()))
                if prefix!=_MARKER.encode(): return False
            except FileNotFoundError: pass
            except OSError: return False
        return True

    def _fragments(self):
        result,error=self.call(['systemctl','--user','show','--property=FragmentPath',TIMER,SERVICE])
        if error: return Status(None,'unavailable',error)
        blocks=[dict(line.split('=',1) for line in block.splitlines() if '=' in line)
            for block in re.split(r'\n\s*\n',result.stdout.strip())]
        if result.returncode and not any(blocks): return Status(None,'unavailable','scheduler_command')
        for name,fields in zip((TIMER,SERVICE),blocks):
            fragment=fields.get('FragmentPath')
            if fragment and os.path.realpath(fragment)!=os.path.realpath(self.directory/name):
                return Status(None,'unavailable','scheduler_files')
        return None

    def enable(self):
        if not self._owned(): return Status(None,'unavailable','scheduler_files')
        status=self._fragments()
        if status is not None: return status
        try:
            invocation=' '.join(systemd_quote(value,executable=i==0) for i,value in enumerate(self.argv))
            service=(_MARKER+'[Unit]\nDescription=CoveArchive scheduled preservation\n[Service]\nType=oneshot\n'
                'ExecStart='+invocation+'\nTimeoutStartSec=16min\nTimeoutStopSec=20s\nKillMode=control-group\n')
            timer=(_MARKER+'[Unit]\nDescription=Check CoveArchive preservation schedules\n[Timer]\n'
                'OnCalendar=*:0/15\nPersistent=true\nAccuracySec=1min\nUnit='+SERVICE+'\n[Install]\nWantedBy=timers.target\n')
            for name,payload in ((SERVICE,service),(TIMER,timer)):
                path=self.directory/name; raw=payload.encode('utf-8')
                if not path.exists() or path.read_bytes()!=raw: atomic_write(path,raw)
        except (OSError,ValueError): return Status(None,'unavailable','scheduler_files')
        for args in (['daemon-reload'],['enable','--now',TIMER]):
            result,error=self.call(['systemctl','--user',*args])
            if error or result.returncode: return Status(None,'unavailable',error or 'scheduler_command')
        return self.status()

    def disable(self):
        if not self._owned(): return Status(None,'unavailable','scheduler_files')
        # Missing files and an absent timer are already disabled; status never creates files.
        present=any((self.directory/name).exists() for name in (SERVICE,TIMER))
        if not present:
            status=self.status()
            if status.enabled is False or status.error: return status
        status=self._fragments()
        if status is not None: return status
        for args in (['disable','--now',TIMER],['stop',SERVICE]):
            result,error=self.call(['systemctl','--user',*args])
            if error or result.returncode: return Status(None,'unavailable',error or 'scheduler_command')
        try:
            for name in (SERVICE,TIMER): (self.directory/name).unlink(missing_ok=True)
        except OSError: return Status(None,'unavailable','scheduler_files')
        result,error=self.call(['systemctl','--user','daemon-reload'])
        if error or result.returncode: return Status(None,'unavailable',error or 'scheduler_command')
        return Status(False,'off')


def _sid(user_id):
    if type(user_id) is not str or re.fullmatch(r'S-1-\d+(?:-\d+)+',user_id) is None:
        raise ValueError('scheduler_user')
    return user_id


def _task_name(user_id): return TASK_PREFIX+' '+_sid(user_id)


def task_xml(argv,user_id):
    user_id=_sid(user_id)
    for value in argv: _text(value)
    ET.register_namespace('',_NS)
    root=ET.Element('Task',{'version':'1.2','xmlns':_NS})
    def child(parent,name,text=None,**attrs):
        node=ET.SubElement(parent,name,attrs); node.text=text; return node
    info=child(root,'RegistrationInfo'); child(info,'Description','CoveArchive scheduled preservation')
    triggers=child(root,'Triggers'); trigger=child(triggers,'TimeTrigger')
    repetition=child(trigger,'Repetition'); child(repetition,'Interval','PT15M'); child(repetition,'StopAtDurationEnd','false')
    child(trigger,'StartBoundary','2026-01-01T00:00:00'); child(trigger,'Enabled','true')
    principals=child(root,'Principals'); principal=child(principals,'Principal',id='User')
    child(principal,'UserId',user_id); child(principal,'LogonType','InteractiveToken'); child(principal,'RunLevel','LeastPrivilege')
    settings=child(root,'Settings'); child(settings,'MultipleInstancesPolicy','IgnoreNew')
    child(settings,'DisallowStartIfOnBatteries','false'); child(settings,'StopIfGoingOnBatteries','false')
    child(settings,'StartWhenAvailable','true'); child(settings,'ExecutionTimeLimit','PT17M'); child(settings,'Enabled','true')
    actions=child(root,'Actions',Context='User'); action=child(actions,'Exec')
    child(action,'Command',argv[0]); child(action,'Arguments',subprocess.list2cmdline(argv[1:]))
    # schtasks /Create /XML rejects UTF-8 input; it accepts UTF-16 with a byte-order mark.
    return ET.tostring(root,encoding='utf-16',xml_declaration=True)


class Windows(_Commands):
    def __init__(self,argv,*,runner=subprocess.run,user_id=None,temporary=None):
        super().__init__(runner); self.argv=list(argv); self.user_id=user_id; self.temporary=temporary

    def _identity(self):
        user_id=self.user_id
        if user_id is None:
            result,error=self.call(['whoami','/user','/fo','csv','/nh'])
            if error or result.returncode: return None,error or 'scheduler_user'
            try: rows=list(csv.reader(io.StringIO(result.stdout)))
            except csv.Error: return None,'scheduler_user'
            if len(rows)!=1 or len(rows[0])!=2: return None,'scheduler_user'
            user_id=rows[0][1]
        try: self.user_id=_sid(user_id)
        except ValueError: return None,'scheduler_user'
        return self.user_id,None

    def _status(self,user_id):
        # Querying all names distinguishes absence from access/command failures without
        # relying on translated schtasks error messages. /TN always addresses this SID.
        name=_task_name(user_id)
        result,error=self.call(['schtasks','/Query','/FO','CSV','/NH'])
        if error or result.returncode: return Status(None,'unavailable',error or 'scheduler_command'),False
        found=any(row and row[0].casefold()==name.casefold() for row in csv.reader(io.StringIO(result.stdout)))
        if not found: return Status(False,'off'),False
        result,error=self.call(['schtasks','/Query','/TN',name,'/XML'])
        if error or result.returncode: return Status(None,'unavailable',error or 'scheduler_command'),True
        try:
            if len(result.stdout)>65536: raise ValueError('scheduler_xml')
            root=ET.fromstring(result.stdout)
            description=root.find('{'+_NS+'}RegistrationInfo/{'+_NS+'}Description')
            principal=root.find('{'+_NS+'}Principals/{'+_NS+'}Principal/{'+_NS+'}UserId')
            if (description is None or description.text!='CoveArchive scheduled preservation'
                    or principal is None or principal.text!=user_id):
                return Status(None,'unavailable','scheduler_files'),True
            node=root.find('{'+_NS+'}Settings/{'+_NS+'}Enabled')
            # Windows omits default-valued elements when exporting, so a missing node means enabled.
            if node is not None and node.text not in ('true','false'): raise ValueError('scheduler_xml')
            enabled=node is None or node.text=='true'
            return Status(enabled,'on' if enabled else 'off'),True
        except (ET.ParseError,ValueError): return Status(None,'unavailable','scheduler_protocol'),True

    def status(self):
        user_id,error=self._identity()
        if error: return Status(None,'unavailable',error)
        return self._status(user_id)[0]

    def enable(self):
        if _onefile(): return Status(None,'unavailable','portable_build_unsupported')
        user_id,error=self._identity()
        if error: return Status(None,'unavailable',error)
        status,exists=self._status(user_id)
        if status.error: return status
        temporary=None
        try:
            payload=task_xml(self.argv,user_id)
            fd,temporary=tempfile.mkstemp(suffix='.xml',prefix='covearchive-task-',dir=self.temporary)
            with os.fdopen(fd,'wb') as stream: stream.write(payload)
            # Only a task whose marker AND principal were verified may be replaced.
            # An absent task uses no /F, so a concurrent creation cannot be clobbered.
            result,error=self.call(['schtasks','/Create','/TN',_task_name(user_id),'/XML',temporary,*(['/F'] if exists else [])])
            if error or result.returncode: return Status(None,'unavailable',error or 'scheduler_command')
        except (OSError,ValueError,TypeError): return Status(None,'unavailable','scheduler_files')
        finally:
            if temporary is not None: os.unlink(temporary)
        return self.status()

    def disable(self):
        user_id,error=self._identity()
        if error: return Status(None,'unavailable',error)
        status,exists=self._status(user_id)
        if status.error or not exists: return status
        # A disabled owned task is still removed; a foreign principal is never touched.
        result,error=self.call(['schtasks','/Delete','/TN',_task_name(user_id),'/F'])
        if error or result.returncode: return Status(None,'unavailable',error or 'scheduler_command')
        return Status(False,'off')


def backend(launcher=None):
    argv=launch_command(launcher)
    if sys.platform=='win32': return Windows(argv)
    if sys.platform=='linux':
        from covearchive.ui.preferences import PreferenceStore
        return Systemd(PreferenceStore().path.parent.parent/'systemd'/'user',argv)
    return None
