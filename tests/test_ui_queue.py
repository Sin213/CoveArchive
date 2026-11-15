"""Offline queue vocabulary and identity-keyed native controls."""
import os
os.environ["QT_QPA_PLATFORM"]="offscreen"
from dataclasses import replace
import pytest
from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from covearchive.queue.models import JobState as S
from covearchive.ui.archive_bridge import JobView, QueueView, ControlResult
from covearchive.ui.queue_panel import QueuePanel, status_text, progress_text, speed_eta_text, detail_text, action_states


def job(state=S.RUNNING, **changes):
    return replace(JobView("one","o/r","v1","a.zip",42,state,0,None,None,None,None,None,True,None,None,None,None),**changes)

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch): monkeypatch.setenv("LOCALAPPDATA",str(tmp_path))

@pytest.mark.parametrize("changes,text",[({},"Starting"),({"bytes_done":42,"bytes_total":42},"finalizing and verifying"),
    ({"stop_pending":"pause"},"Pausing..."),({"stop_pending":"cancel"},"Cancelling..."),({"owned":False},"Interrupted or running elsewhere"),
    ({"state":S.COMPLETED,"result_status":"ARCHIVED"},"Completed (queue record)"),({"state":S.RETRY_WAIT,"next_attempt_at":100},"local time")])
def test_honest_status(changes,text): assert text in status_text(job(**changes),0)

@pytest.mark.parametrize("state,owned,expected",[(S.QUEUED,False,(1,0,1,0)),(S.RETRY_WAIT,False,(1,0,1,0)),(S.RUNNING,True,(1,0,1,0)),
    (S.RUNNING,False,(0,0,0,0)),(S.PAUSED,False,(0,1,1,0)),(S.CANCELLED,False,(0,0,0,1)),(S.COMPLETED,False,(0,0,0,0)),(S.FAILED,False,(0,0,0,1))])
def test_actions(state,owned,expected):
    assert tuple(action_states((job(state,owned=owned),)).values())==expected
    assert not any(action_states(()).values())


def test_measurements_and_integrity():
    assert progress_text(job())=="size 42 B" and "ETA unknown" in speed_eta_text(job(speed_bps=0))
    assert "%" not in progress_text(job(bytes_done=0,bytes_total=0))
    assert "ETA unknown" in speed_eta_text(job(bytes_done=1,bytes_total=None,speed_bps=1))
    assert "verified" not in detail_text(job(S.COMPLETED,integrity="LOCAL_HASH_ONLY",result_status="VERIFIED_EXISTING")).lower()
    assert "Matches publisher SHA-256" in detail_text(job(S.COMPLETED,integrity="PUBLISHER_VERIFIED"))
    assert "publisher" not in detail_text(job(integrity="PUBLISHER_VERIFIED")).lower()
    assert "Error code: unknown_error" in detail_text(job(error_code="https://secret"))


class FakeBridge(QObject):
    view_changed=Signal(object); control_finished=Signal(object); run_event=Signal(object)
    is_open=True; run_active=False; shutting_down=False; closed=False
    def __init__(self): super().__init__(); self.calls=[]
    def control(self,*args): self.calls.append(args)
    def start_queue(self): self.calls.append("start")


def view(seq,jobs,problem=None): return QueueView(seq,tuple(jobs),((S.QUEUED,len(jobs)),),0,84,None,"idle",problem,not bool(problem))


def pump(): QApplication.processEvents(); QTest.qWait(1)


def test_selection_controls_stale_views_plain_text_and_accessibility():
    app=QApplication.instance() or QApplication([]); bridge=FakeBridge(); panel=QueuePanel(bridge)
    a=job(S.QUEUED); b=job(S.PAUSED,job_id="two",asset_name="https://host/?token=SECRET")
    bridge.view_changed.emit(view(1,(a,b))); pump(); panel.table.selectRow(0)
    bridge.view_changed.emit(view(2,(b,a))); pump()
    assert panel.selected_ids()==("one",) and panel.buttons["pause"].isEnabled()
    panel.buttons["pause"].click(); token,action,ids=bridge.calls[-1]
    assert action=="pause" and ids==("one",) and not panel.buttons["pause"].isEnabled()
    bridge.control_finished.emit(ControlResult(token,"pause","one",True,None,S.RUNNING,"pause")); pump()
    assert "waiting for the worker to stop" in panel.message.text()
    bridge.view_changed.emit(view(3,(b,replace(a,state=S.RUNNING,stop_pending="pause")))); pump()
    assert panel.table.item(1,2).text()=="Pausing..."
    bridge.view_changed.emit(view(1,())); pump(); assert panel.table.rowCount()==2
    panel.buttons["cancel"].click(); token=bridge.calls[-1][0]
    bridge.control_finished.emit(ControlResult(token,"cancel","one",False,"ownership",None,None)); pump()
    assert "another CoveArchive process" in panel.message.text()
    panel.buttons["cancel"].click(); token=bridge.calls[-1][0]
    bridge.control_finished.emit(ControlResult(token,"cancel","one",False,"transition",None,None)); pump()
    assert "Not allowed in state Running" in panel.message.text()
    for row in range(2):
        for col in range(6):
            cell=panel.table.item(row,col); assert "https://" not in cell.text()+cell.toolTip() and "SECRET" not in cell.text()+cell.toolTip()
    assert "all pending" in panel.start.accessibleDescription()
    assert all(w.accessibleName() and w.accessibleDescription() for w in [panel.table,panel.start,*panel.buttons.values()])
    bridge.view_changed.emit(view(4,(a,replace(b,state=S.QUEUED)))); pump(); panel.table.selectAll()
    panel.buttons["pause"].click(); token,action,ids=bridge.calls[-1]; assert set(ids)=={"one","two"}
    bridge.control_finished.emit(ControlResult(token,"pause","one",True,None,S.PAUSED,None)); pump()
    assert not panel.buttons["cancel"].isEnabled()
    bridge.control_finished.emit(ControlResult(token,"pause","two",True,None,S.PAUSED,None)); pump()
    bridge.view_changed.emit(view(5,(a,b),"Queue unavailable")); pump()
    assert panel.banner.text()=="Queue unavailable" and not any(w.isEnabled() for w in [panel.start,*panel.buttons.values()])
    panel.close(); panel.deleteLater(); pump()


@pytest.mark.parametrize("states",[(S.QUEUED,S.PAUSED),(S.PAUSED,S.COMPLETED),(S.FAILED,S.CANCELLED)])
def test_mixed_selection_requires_every_row(states):
    rows=tuple(job(s,job_id=str(i)) for i,s in enumerate(states))
    expected={a:all(action_states((row,))[a] for row in rows) for a in action_states(rows)}
    assert action_states(rows)==expected


@pytest.mark.parametrize("integrity,expected",[("LOCAL_HASH_ONLY","Local SHA-256 recorded"),(None,""),("OTHER","")])
def test_M9_integrity_exact_without_publisher(integrity,expected):
    text=detail_text(job(S.COMPLETED,integrity=integrity))
    assert text==expected and "publisher" not in text.lower()


@pytest.mark.parametrize("width,height", [(820,520), (900,600), (1280,720)])
def test_R3b_queue_widths_history_and_ack_lifecycle(width,height):
    from PySide6.QtWidgets import QLabel
    app = QApplication.instance() or QApplication([])
    bridge = FakeBridge()
    panel = QueuePanel(bridge)
    panel.resize(width, height)
    panel.show()
    try:
        a = job(S.RUNNING, job_id="private-job-id", stop_pending="pause", repository="Owner/Repository-Name")
        b = replace(a, job_id="second", tag="v10.20.30-rc.10")
        bridge.view_changed.emit(replace(view(1, (a,b)), counts=((S.RUNNING, 1), (S.RETRY_WAIT, 1))))
        pump()
        for col, text in ((2, "Download received - finalizing and verifying"),
                          (3, "1023.9 GiB / 1023.9 GiB"), (4, "1023.9 MiB/s / ETA 999999s")):
            assert panel.table.columnWidth(col) >= panel.table.fontMetrics().horizontalAdvance(text) + 16
        fm = panel.table.fontMetrics()
        for row, tag in enumerate((a.tag,b.tag)):
            text = panel.table.item(row,1).text()
            assert tag in text
            assert panel.table.columnWidth(1) >= fm.horizontalAdvance(text) + 16
            assert fm.elidedText(text,Qt.ElideRight,panel.table.columnWidth(1)-16) == text
        assert panel.table.item(0,0).text() == panel.table.item(1,0).text()
        assert panel.table.item(0,1).text() != panel.table.item(1,1).text()
        for col in range(6):
            header = panel.table.horizontalHeaderItem(col).text()
            assert panel.table.columnWidth(col) >= panel.table.horizontalHeader().fontMetrics().horizontalAdvance(header)+16
        assert abs(panel.table.columnWidth(0) - panel.table.columnWidth(5)) <= 3
        labels = " ".join(w.text() for w in panel.findChildren(QLabel))
        assert ("Completed = result recorded" in labels and "Select a job to enable" in labels
                and "Waiting to retry" in labels and "RETRY_WAIT" not in labels and "RUNNING" not in labels)
        panel.token = 1
        panel.waiting = {a.job_id}
        bridge.control_finished.emit(ControlResult(1, "pause", a.job_id, True, None, S.RUNNING, "pause"))
        pump()
        assert "a.zip" in panel.message.text() and a.job_id not in panel.message.text()
        bridge.view_changed.emit(view(2, (replace(a, state=S.PAUSED, stop_pending=None),)))
        pump()
        assert not panel.message.text()
    finally:
        panel.close()
        panel.deleteLater()
        pump()


def test_display_truncation_marker():
    from covearchive.ui.queue_panel import display
    assert display("x"*8300).endswith("\u2026") and len(display("x"*8300))==8193 and display("short")=="short"


@pytest.mark.parametrize("order", [(0,1,2), (2,1,0)])
@pytest.mark.parametrize("failed", [(0,), (1,), (2,), (), (0,1,2)])
def test_H1_control_batch_preserves_every_failure(failed,order):
    app = QApplication.instance() or QApplication([])
    bridge = FakeBridge()
    panel = QueuePanel(bridge)
    jobs = tuple(job(job_id=str(i),asset_name=f"file{i}.zip") for i in range(3))
    bridge.view_changed.emit(view(1,jobs))
    pump()
    panel.table.selectAll()
    panel.buttons["pause"].click()
    token = bridge.calls[-1][0]
    for i in order:
        bridge.control_finished.emit(ControlResult(token,"pause",str(i),i not in failed,
            "ownership" if i in failed else None,S.RUNNING,"pause" if i not in failed else None))
        pump()
    text = panel.message.text()
    assert f"{3-len(failed)} succeeded, {len(failed)} failed" in text
    for i in failed:
        assert f"file{i}.zip: running in another CoveArchive process" in text
    if failed:
        bridge.view_changed.emit(view(2,jobs))
        pump()
        assert panel.message.text()==text
        bridge.view_changed.emit(view(3,tuple(replace(j,stop_pending="pause") for j in jobs)))
        pump()
        assert panel.message.text()==text
    panel.close()
    panel.deleteLater()
    pump()


def test_H2_control_startup_problem_is_visible():
    app = QApplication.instance() or QApplication([])
    bridge = FakeBridge()
    panel = QueuePanel(bridge)
    panel._view(view(1,(job(S.PAUSED),)))
    panel.table.selectAll()
    panel.buttons["resume"].click()
    panel._result(ControlResult(panel.token,"resume","one",True,None,S.QUEUED,None,startup_error="database_busy"))
    assert "1 succeeded, 0 failed" in panel.message.text() and "automatic start failed: Queue database is busy. Press Start Queue." in panel.message.text()
    panel.close()
