"""OS registration is explicit, escaped, and tested only with injected commands."""
from pathlib import Path
import os
import sys
import subprocess
import xml.etree.ElementTree as ET
from types import SimpleNamespace as NS

import pytest
from covearchive import scheduler as s

SID='S-1-5-21-123'
OTHER_SID='S-1-5-21-456'


def user_task(sid): return '\\CoveArchive Scheduled Preservation '+sid


class Tasks:
    """Machine-wide task namespace; all OS calls stay in this offline double."""
    def __init__(self): self.tasks={}; self.calls=[]

    def __call__(self,argv,**kw):
        self.calls.append(list(argv)); assert kw['timeout']<=5 and not kw.get('shell',False)
        if argv[0]=='whoami': return NS(returncode=0,stdout='"DOMAIN\\Person","'+SID+'"\n')
        assert argv[0]=='schtasks'
        name=argv[argv.index('/TN')+1] if '/TN' in argv else None
        if '/Create' in argv:
            assert '/F' in argv or name not in self.tasks
            self.tasks[name]=Path(argv[argv.index('/XML')+1]).read_bytes().decode('utf-16')
        elif '/Delete' in argv: del self.tasks[name]
        elif '/XML' in argv: return NS(returncode=0,stdout=self.tasks[name])
        elif '/Query' in argv:
            return NS(returncode=0,stdout=''.join('"'+key+'","N/A","Ready"\n' for key in self.tasks))
        return NS(returncode=0,stdout='')


def test_source_and_frozen_argv(tmp_path):
    exe=str(tmp_path/'Python ü % $'/'python'); launcher=tmp_path/'Cove Archive'/'run_covearchive.py'
    assert s.launch_command(launcher,executable=exe,frozen=False)==[exe,'-I','-B',str(launcher),'--scheduled-run']
    assert s.launch_command(launcher,executable=exe,frozen=True)==[exe,'--scheduled-run']


@pytest.mark.parametrize('frozen',[False,True])
def test_default_scheduler_interpreter_preserves_symlink(tmp_path,monkeypatch,frozen):
    link=tmp_path/'.venv'/'bin'/'python'; link.parent.mkdir(parents=True)
    if os.name=='nt':
        # Simulate resolution on Windows without requiring symlink privileges.
        resolve=Path.resolve; real=Path(sys.executable)
        monkeypatch.setattr(Path,'resolve',lambda path,*a,**kw:real if path==link else resolve(path,*a,**kw))
    else: link.symlink_to(sys.executable)
    monkeypatch.setattr(sys,'executable',str(link))
    monkeypatch.setattr(sys,'frozen',frozen,raising=False)
    assert s.launch_command(tmp_path/'run_covearchive.py')[0]==os.path.abspath(link)


def test_systemd_exact_escaping():
    assert s.systemd_quote('a b%$"\\ü')=='"a b%%$$\\"\\\\ü"'
    for value in ('a\nInjected=yes','x\0y','\rx'):
        with pytest.raises(ValueError): s.systemd_quote(value)


def test_linux_explicit_idempotent_enable_status_disable(tmp_path):
    commands=[]; active=[False]
    def run(argv,**kw):
        commands.append(argv)
        assert kw['timeout']<=5 and not kw.get('shell',False)
        if 'show' in argv: return NS(returncode=0,stdout='LoadState=loaded\nActiveState='+('active' if active[0] else 'inactive')+'\nUnitFileState='+('enabled' if active[0] else 'disabled')+'\n')
        if 'enable' in argv: active[0]=True
        if 'disable' in argv: active[0]=False
        return NS(returncode=0,stdout='')
    backend=s.Systemd(tmp_path/'units',['/path with ü%$/python','script\\a','--scheduled-run'],runner=run)
    assert backend.status().enabled is False and not list(tmp_path.iterdir())
    assert backend.enable().enabled is True
    service=(backend.directory/'covearchive-scheduled.service').read_text(encoding='utf-8')
    assert '"/path with ü%%$/python" "script\\\\a" "--scheduled-run"' in service
    assert 'Persistent=true' in (backend.directory/'covearchive-scheduled.timer').read_text(encoding='utf-8')
    assert backend.enable().enabled is True
    unrelated=backend.directory/'unrelated'; unrelated.write_text('keep')
    assert backend.disable().enabled is False
    assert list(backend.directory.iterdir())==[unrelated]
    assert backend.disable().enabled is False
    assert all('sudo' not in c and 'loginctl' not in c for c in commands)


def test_systemd_executable_and_argument_dollar_percent_are_distinct(tmp_path):
    def run(*a,**kw): return NS(returncode=0,stdout='LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n')
    backend=s.Systemd(tmp_path,['/Cove$Archive%/python','arg$%\\ü','--scheduled-run'],runner=run)
    assert backend.enable().error is None
    line=next(line for line in (tmp_path/s.SERVICE).read_text(encoding='utf-8').splitlines() if line.startswith('ExecStart='))
    assert line=='ExecStart="/Cove$Archive%%/python" "arg$$%%\\\\ü" "--scheduled-run"'


def test_windows_xml_argv_and_cleanup(tmp_path):
    commands=[]; exists=[False]; xml=[]
    argv=['C:\\Cove & ü%$\\python.exe','C:\\a b\\run_covearchive.py','--scheduled-run']
    def run(command,**kw):
        commands.append(command); assert not kw.get('shell',False) and kw['timeout']<=5
        if '/Create' in command:
            path=Path(command[command.index('/XML')+1]); xml.append(path.read_bytes()); exists[0]=True
        if '/Delete' in command: exists[0]=False
        if '/Query' in command:
            if '/XML' in command: return NS(returncode=0,stdout=xml[-1].decode('utf-16'))
            return NS(returncode=0,stdout='"'+user_task(SID)+'","N/A","Ready"\n' if exists[0] else '')
        return NS(returncode=0,stdout='')
    backend=s.Windows(argv,runner=run,user_id='S-1-5-21-123',temporary=tmp_path)
    assert backend.status().enabled is False and not list(tmp_path.iterdir())
    assert backend.enable().enabled is True and not list(tmp_path.iterdir())
    assert xml[-1].startswith((b'\xff\xfe',b'\xfe\xff')) and 'utf-16' in xml[-1][:80].decode('utf-16').lower()  # schtasks needs UTF-16 with a BOM
    root=ET.fromstring(xml[-1]); ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    assert root.find('.//t:Command',ns).text==argv[0]
    assert root.find('.//t:Arguments',ns).text==subprocess.list2cmdline(argv[1:])
    assert root.find('.//t:LogonType',ns).text=='InteractiveToken'
    assert root.find('.//t:RunLevel',ns).text=='LeastPrivilege'
    assert root.find('.//t:MultipleInstancesPolicy',ns).text=='IgnoreNew'
    assert root.find('.//t:StartWhenAvailable',ns).text=='true'
    assert root.find('.//t:Interval',ns).text=='PT15M'
    assert backend.disable().enabled is False and backend.disable().enabled is False
    assert not list(tmp_path.iterdir())
    assert not any('/RP' in c or '/RU' in c for c in commands)


def test_backend_errors_hide_command_output_and_leave_no_temp(tmp_path):
    def run(argv,**kw): raise subprocess.TimeoutExpired(argv,5,output='secret')
    backend=s.Windows(['C:\\app.exe','--scheduled-run'],runner=run,user_id='S-1-5-21-123',temporary=tmp_path)
    assert backend.enable().error=='scheduler_timeout' and not list(tmp_path.iterdir())
    assert backend.status().error=='scheduler_timeout'


def test_linux_does_not_overwrite_foreign_unit(tmp_path):
    p=tmp_path/'covearchive-scheduled.service'; p.write_text('foreign')
    backend=s.Systemd(tmp_path,['/app','--scheduled-run'],runner=lambda *a,**k:pytest.fail('external command'))
    assert backend.enable().error=='scheduler_files' and p.read_text()=='foreign'
    assert backend.disable().error=='scheduler_files' and p.read_text()=='foreign'


def test_windows_does_not_replace_or_remove_foreign_task(tmp_path):
    calls=[]
    def run(argv,**kw):
        calls.append(argv)
        if '/XML' in argv: return NS(returncode=0,stdout='<Task xmlns="'+s._NS+'"><RegistrationInfo><Description>foreign</Description></RegistrationInfo><Settings><Enabled>true</Enabled></Settings></Task>')
        return NS(returncode=0,stdout='"'+user_task(SID)+'","N/A","Ready"\n')
    backend=s.Windows(['C:\\app.exe','--scheduled-run'],runner=run,user_id='S-1-5-21-123',temporary=tmp_path)
    assert backend.enable().error=='scheduler_files'
    assert backend.disable().error=='scheduler_files'
    assert not any('/Create' in c or '/Delete' in c for c in calls)


@pytest.mark.parametrize('code,output,enabled,error',[(4,'LoadState=not-found\n',False,None),
    (0,'',None,'scheduler_protocol'),(1,'',None,'scheduler_unavailable')])
def test_systemd_absence_is_distinct_from_unknown(tmp_path,code,output,enabled,error):
    backend=s.Systemd(tmp_path,['/app','--scheduled-run'],runner=lambda *a,**k:NS(returncode=code,stdout=output))
    result=backend.status()
    assert result.enabled is enabled and result.error==error
    assert not list(tmp_path.iterdir())


def test_windows_sid_names_isolate_accounts(tmp_path):
    tasks=Tasks(); argv=['C:\\app.exe','--scheduled-run']
    first=s.Windows(argv,runner=tasks,user_id=SID,temporary=tmp_path)
    second=s.Windows(argv,runner=tasks,user_id=OTHER_SID,temporary=tmp_path)
    assert first.enable().enabled is True
    original=dict(tasks.tasks); tasks.calls.clear()
    assert second.status().enabled is False
    assert second.enable().enabled is True
    assert set(tasks.tasks)=={user_task(SID),user_task(OTHER_SID)}
    assert tasks.tasks[user_task(SID)]==original[user_task(SID)]
    assert second.disable().enabled is False and second.disable().enabled is False
    assert tasks.tasks==original
    assert all(call[call.index('/TN')+1]==user_task(OTHER_SID) for call in tasks.calls if '/TN' in call)
    assert first.status().enabled is True
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('foreign',['principal','missing_principal','description'])
def test_windows_sid_task_requires_marker_and_current_principal(tmp_path,foreign):
    tasks=Tasks(); argv=['C:\\app.exe','--scheduled-run']; ns={'t':s._NS}
    root=ET.fromstring(s.task_xml(argv,OTHER_SID if foreign=='principal' else SID))
    if foreign=='description': root.find('t:RegistrationInfo/t:Description',ns).text='foreign'
    if foreign=='missing_principal':
        principal=root.find('t:Principals/t:Principal',ns); principal.remove(principal.find('t:UserId',ns))
    tasks.tasks[user_task(SID)]=ET.tostring(root,encoding='unicode'); before=dict(tasks.tasks)
    backend=s.Windows(argv,runner=tasks,user_id=SID,temporary=tmp_path)
    assert backend.status().error=='scheduler_files'
    assert backend.enable().error=='scheduler_files'
    assert backend.disable().error=='scheduler_files'
    assert tasks.tasks==before and not any('/Create' in call or '/Delete' in call for call in tasks.calls)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('enabled',[True,False])
def test_windows_sid_task_lifecycle_is_idempotent(tmp_path,enabled):
    tasks=Tasks(); argv=['C:\\app.exe','--scheduled-run']
    backend=s.Windows(argv,runner=tasks,user_id=SID,temporary=tmp_path)
    assert backend.status().enabled is False
    assert backend.disable().enabled is False and not tasks.tasks
    assert not any('/Create' in call or '/Delete' in call for call in tasks.calls)
    assert backend.enable().enabled is True
    assert set(tasks.tasks)=={user_task(SID)}
    first=next(call for call in tasks.calls if '/Create' in call)
    assert '/F' not in first  # An unseen task must never be blindly replaced.
    assert backend.enable().enabled is True and len(tasks.tasks)==1
    root=ET.fromstring(tasks.tasks[user_task(SID)]); node=root.find('{'+s._NS+'}Settings/{'+s._NS+'}Enabled')
    if not enabled: node.text='false'
    else: root.find('{'+s._NS+'}Settings').remove(node)  # Windows exports omit default-valued elements.
    tasks.tasks[user_task(SID)]=ET.tostring(root,encoding='unicode')
    before=dict(tasks.tasks)
    assert backend.status().enabled is enabled and tasks.tasks==before
    assert backend.disable().enabled is False and not tasks.tasks
    deletes=sum('/Delete' in call for call in tasks.calls)
    assert backend.disable().enabled is False and backend.status().enabled is False
    assert sum('/Delete' in call for call in tasks.calls)==deletes==1
    assert not list(tmp_path.iterdir())


def test_windows_sid_is_resolved_once_by_existing_whoami(tmp_path):
    tasks=Tasks(); backend=s.Windows(['C:\\app.exe','--scheduled-run'],runner=tasks,temporary=tmp_path)
    assert backend.status().enabled is False
    assert backend.enable().enabled is True and set(tasks.tasks)=={user_task(SID)}
    assert backend.disable().enabled is False
    assert [call for call in tasks.calls if call[0]=='whoami']==[['whoami','/user','/fo','csv','/nh']]


@pytest.mark.parametrize('sid',[True,123,'bad','S-1-5-21/other','S-1-5-21\\other','S-1-5-21\n'])
def test_windows_invalid_sid_never_addresses_a_task(tmp_path,sid):
    tasks=Tasks(); backend=s.Windows(['C:\\app.exe','--scheduled-run'],runner=tasks,user_id=sid,temporary=tmp_path)
    assert backend.status().error=='scheduler_user'
    assert backend.enable().error=='scheduler_user' and backend.disable().error=='scheduler_user'
    assert tasks.calls==[] and not tasks.tasks and not list(tmp_path.iterdir())


@pytest.mark.parametrize('frozen,bundle,expected',[(False,None,False),
    (True,'app/_internal',False),(True,'app',False),(True,'_MEI123',True),
    (True,'app/_MEI123',True),(True,'sibling',True),(True,None,True)])
def test_onefile_detection(tmp_path,monkeypatch,frozen,bundle,expected):
    monkeypatch.delattr(sys,'_MEIPASS',raising=False)
    assert s._onefile(frozen=frozen,executable=str(tmp_path/'app'/'CoveArchive.exe'),
        bundle=tmp_path/bundle if bundle is not None else None) is expected


def test_windows_onefile_refuses_registration_but_can_remove_task(tmp_path,monkeypatch):
    tasks=Tasks(); backend=s.Windows(['C:\\app.exe','--scheduled-run'],runner=tasks,temporary=tmp_path)
    monkeypatch.setattr(s,'_onefile',lambda:True)
    result=backend.enable()
    assert result.enabled is None and result.state=='unavailable' and result.error=='portable_build_unsupported'
    assert tasks.calls==[] and not tasks.tasks and not list(tmp_path.iterdir())
    monkeypatch.setattr(s,'_onefile',lambda:False)
    assert backend.enable().enabled is True and set(tasks.tasks)=={user_task(SID)}
    monkeypatch.setattr(s,'_onefile',lambda:True)
    assert backend.status().enabled is True
    assert backend.disable().enabled is False and not tasks.tasks and not list(tmp_path.iterdir())


@pytest.mark.parametrize('foreign',[s.TIMER,s.SERVICE])
def test_systemd_refuses_foreign_loaded_fragments(tmp_path,foreign):
    calls=[]
    def run(argv,**kw):
        calls.append(argv)
        if '--property=FragmentPath' in argv:
            return NS(returncode=0,stdout='\n\n'.join('FragmentPath='+str(tmp_path/'elsewhere'/name)
                if name==foreign else 'FragmentPath=' for name in (s.TIMER,s.SERVICE)))
        return NS(returncode=0,stdout='LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n')
    backend=s.Systemd(tmp_path,['/app','--scheduled-run'],runner=run)
    assert backend.enable().error=='scheduler_files'
    assert backend.disable().error=='scheduler_files'
    assert all(call[:3]==['systemctl','--user','show'] for call in calls)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('fragments',['local','symlink','absent','missing','absent_nonzero'])
def test_systemd_accepts_owned_or_absent_fragments(tmp_path,fragments):
    directory=tmp_path/'units'; directory.mkdir(); configured=directory; calls=[]; active=[False]
    if fragments=='symlink':
        configured=tmp_path/'linked'
        try: configured.symlink_to(directory,target_is_directory=True)
        except OSError: pytest.skip('directory symlinks unavailable')
    def run(argv,**kw):
        calls.append(argv)
        if '--property=FragmentPath' in argv:
            output='\n\n'.join('FragmentPath='+str(directory/name) if fragments in ('local','symlink')
                else 'LoadState=not-found' if fragments=='missing' else 'FragmentPath=' for name in (s.TIMER,s.SERVICE))
            return NS(returncode=4 if fragments=='absent_nonzero' else 0,stdout=output)
        if 'show' in argv:
            return NS(returncode=0,stdout='LoadState=loaded\nActiveState='+('active' if active[0] else 'inactive')+'\nUnitFileState='+('enabled' if active[0] else 'disabled')+'\n')
        if 'enable' in argv: active[0]=True
        if 'disable' in argv: active[0]=False
        return NS(returncode=0,stdout='')
    backend=s.Systemd(configured,['/app','--scheduled-run'],runner=run)
    assert backend.enable().enabled is True
    assert all((directory/name).is_file() for name in (s.TIMER,s.SERVICE))
    assert backend.disable().enabled is False and not list(directory.iterdir())
    assert any('enable' in call for call in calls) and any('disable' in call for call in calls)


@pytest.mark.parametrize('failure',['command','timeout'])
@pytest.mark.parametrize('method',['enable','disable'])
def test_systemd_fragment_query_errors_do_not_mutate(tmp_path,failure,method):
    calls=[]
    def run(argv,**kw):
        calls.append(argv)
        if '--property=FragmentPath' in argv:
            if failure=='timeout': raise subprocess.TimeoutExpired(argv,5)
            return NS(returncode=1,stdout='')
        return NS(returncode=0,stdout='LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n')
    backend=s.Systemd(tmp_path,['/app','--scheduled-run'],runner=run)
    assert getattr(backend,method)().error==('scheduler_timeout' if failure=='timeout' else 'scheduler_command')
    assert all(call[:3]==['systemctl','--user','show'] for call in calls)
    assert not list(tmp_path.iterdir())


def test_systemd_absent_disabled_shortcut_needs_no_fragment_query(tmp_path):
    calls=[]
    def run(argv,**kw):
        calls.append(argv)
        assert '--property=FragmentPath' not in argv
        return NS(returncode=4,stdout='LoadState=not-found\n')
    backend=s.Systemd(tmp_path,['/app','--scheduled-run'],runner=run)
    assert backend.disable()==s.Status(False,'off') and len(calls)==1 and not list(tmp_path.iterdir())
