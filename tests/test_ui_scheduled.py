"""Settings schedules are explicitly queried/changed off the GUI thread."""
import threading
from covearchive import scheduler
from test_ui_window import ui, APP, isolated_data
from test_scan_process import until


def test_settings_never_registers_or_queries_on_open(ui,monkeypatch):
    calls=[]
    class Backend:
        def status(self): calls.append(('status',threading.get_ident())); return scheduler.Status(False,'off')
        def enable(self): calls.append(('enable',threading.get_ident())); return scheduler.Status(True,'on')
        def disable(self): calls.append(('disable',threading.get_ident())); return scheduler.Status(False,'off')
    monkeypatch.setattr(scheduler,'backend',lambda *a,**k:Backend())
    w,_=ui; w.settings_button.click(); APP.processEvents(); d=w.settings_dialog
    assert calls==[]
    d.schedule_check.click(); until(lambda:d.schedule_check.isEnabled())
    assert d.schedule_status.text()=='Off' and d.schedule_toggle.text()=='Enable'
    d.schedule_toggle.click(); until(lambda:d.schedule_toggle.isEnabled())
    assert d.schedule_status.text()=='On' and d.schedule_toggle.text()=='Disable'
    d.schedule_toggle.click(); until(lambda:d.schedule_toggle.isEnabled())
    assert d.schedule_status.text()=='Off'
    assert [a for a,_ in calls]==['status','enable','disable']
    assert all(t!=threading.get_ident() for _,t in calls)
    d.reject()
