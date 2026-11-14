"""Offline native-window interaction and lazy model behavior."""

import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from pathlib import Path
import subprocess
import sys
import time
import unicodedata

import pytest
from PySide6.QtCore import QObject, Signal, Qt, QModelIndex
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from covearchive import scan_worker as w
from covearchive.ui.window import MainWindow
from test_scan_worker import RID, URL, ROOT, catalog, release, asset, DATE
from test_scan_process import until, gone, command
from covearchive.ui.scan_process import ScanController

APP = QApplication.instance() or QApplication([])
if sys.platform == "win32" and not QFontDatabase.families():
    assert QFontDatabase.addApplicationFont(str(Path(os.environ.get("WINDIR","C:/Windows"))/"Fonts/segoeui.ttf")) >= 0


class FakeController(QObject):
    state_changed = Signal(str, str)
    terminal = Signal(str, str, object)
    deadline_ms = 180000
    def __init__(self):
        super().__init__(); self.calls = []; self.job = None; self.closed = False
    def start(self, url):
        if self.job: return False
        self.calls.append(url); self.job = RID; self.state_changed.emit(RID, "RUNNING"); return True
    def cancel(self):
        self.state_changed.emit(RID, "STOPPING")
    def shutdown(self, timeout_ms=3000):
        self.closed = True; self.job = None; return True
    def finish(self, result, state="COMPLETED", rid=RID):
        self.job = None; self.terminal.emit(rid, state, result)


@pytest.fixture
def ui():
    controller = FakeController(); window = MainWindow(ROOT/"run_covearchive.py", controller)
    window.show(); APP.processEvents()
    yield window, controller
    window.close(); APP.processEvents()


def succeed(window, controller, *items):
    window.url.setText(URL); window.scan.click()
    controller.finish(w.decode_response(w.encode_success(RID, catalog(*items)), RID))


def test_UI_01_launcher_smoke():
    result = subprocess.run([sys.executable, "-I", "-B", str(ROOT/"run_covearchive.py"),
        "--smoke-test"], capture_output=True, timeout=10, env=os.environ.copy())
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_UI_02_controls_defaults(ui):
    v, c = ui
    assert v.windowTitle() == "CoveArchive" and v.url.hasFocus()
    assert v.scan.isEnabled() and not v.cancel.isEnabled() and not v.prereleases.isChecked()
    assert len(v.formats) == 6 and all(x.isChecked() for x in v.formats.values())
    assert all(x.accessibleName() for x in [v.url,v.scan,v.cancel,v.prereleases,*v.formats.values(),v.tree])
    assert not v.windowFlags() & Qt.FramelessWindowHint
    assert v.status.text() == "Ready" and v.status.textFormat() == Qt.PlainText


@pytest.mark.parametrize("url", ["invalid", URL+"/releases/tag/v1"])
def test_UI_03_invalid_no_process(ui, url):
    v, c = ui; v.url.setText(url); v.scan.click()
    assert "Invalid URL" in v.status.text() and c.calls == [] and v.url.text() == url


def test_UI_04_23_start_enter_guard(ui):
    v, c = ui; v.url.setText(URL)
    QTest.keyClick(v.url, Qt.Key_Return); v.scan.click(); QTest.keyClick(v.url, Qt.Key_Return)
    assert c.calls == [URL] and v.cancel.isEnabled() and v.busy.isVisible()
    assert "Scanning" in v.status.text()
    v.cancel.click(); assert not v.cancel.isEnabled() and "Cancelling" in v.status.text()


def test_UI_06_07_08_hierarchy_and_details(ui):
    v, c = ui; succeed(v,c,release(),release(2, assets=[asset(2,"two.dmg")]))
    m = v.tree.model(); assert m.rowCount() == 2
    root = m.index(1,0); child = m.index(0,0,root)
    assert m.parent(child) == root and m.data(child) == "two.dmg"
    assert m.data(m.index(0,1)) == "v1" and m.data(m.index(0,2)) == DATE[:10]
    assert "2 releases" in v.summary.text() and "2 matching assets" in v.summary.text()
    v.tree.setCurrentIndex(child); assert str(2**63) in v.detail.text()
    assert v.detail.textFormat() == Qt.PlainText


@pytest.mark.parametrize("items,text", [((),"No releases found"),
    ((release(assets=[]),),"No matching assets"),
    ((release(prerelease=True),),"1 prereleases hidden")])
def test_UI_09_10_empty_states(ui, items, text):
    v,c=ui; succeed(v,c,*items); assert text in v.status.text()


def test_UI_11_12_13_local_filters(ui):
    v,c=ui
    names=["a.appimage","a.tar.gz","a.tar","a.dmg","a.zip","a.7z","a.exe"]
    succeed(v,c,release(assets=[asset(i+1,n) for i,n in enumerate(names)]),release(2,prerelease=True))
    original=v.catalog; calls=len(c.calls)
    for checkbox in v.formats.values(): checkbox.setChecked(False)
    assert "No formats selected" in v.status.text()
    for extension, checkbox in v.formats.items():
        checkbox.setChecked(True)
        assert "1 matching assets" in v.summary.text()
        checkbox.setChecked(False)
    v.formats[".zip"].setChecked(True); v.prereleases.setChecked(True)
    assert "2 releases" in v.summary.text() and len(c.calls)==calls and v.catalog is original
    model=v.tree.model(); parent=model.index(0,0)
    assert model.rowCount(parent)==7
    assert model.data(model.index(6,5,parent))=="Unsupported format"
    assert model.data(model.index(0,5,parent))=="Filtered out"


@pytest.mark.parametrize("count", [150,10000])
def test_UI_14_large_lazy_model(ui,count):
    v,c=ui; data=catalog(*(release(i+1) for i in range(count)))
    begin=time.monotonic(); v.url.setText(URL); v.scan.click()
    c.finish(w.decode_response(w.encode_success(RID,data),RID)); APP.processEvents()
    assert v.tree.model().rowCount()==count and time.monotonic()-begin < 1.5
    assert v.tree.indexWidget(v.tree.model().index(0,0)) is None


@pytest.mark.parametrize("code,status,text,detail", [
    ("rate_limited",403,"Rate limited",dict(kind="primary",retry_after_seconds=30,retry_at=1030,reset_at=None,remaining=0)),
    ("rate_limited",429,"Rate limited",dict(kind="secondary",retry_after_seconds=31,retry_at=1031,reset_at=None,remaining=None)),
    ("forbidden",403,"Access denied",{}),("unauthorized",401,"Access denied",{}),
    ("not_found",404,"Not found",{}),("network",None,"Network error",{}),
    ("timeout",None,"Timed out: GitHub did not respond in time; try again",{}), ("incomplete_scan",None,"Release listing incomplete, retry",
        dict(pages_completed=1,reason="error",cause_code="network")),
    ("invalid_json",200,"GitHub returned unexpected data",{})])
def test_UI_25_26_27_errors_no_partial(ui,code,status,text,detail):
    v,c=ui; succeed(v,c,release()); v.scan.click()
    c.finish(w.decode_response(w.encode_error(RID,code,http_status=status,detail=detail),RID),"FAILED")
    assert text in v.status.text() and v.tree.model().rowCount()==0 and v.url.text()==URL
    assert v.summary.text()=="" and v.scan.isEnabled()
    if code=="rate_limited": assert str(detail["retry_after_seconds"]) in v.status.text()


def test_UI_22_28_plain_text_sanitized_stale(ui):
    v,c=ui; text="<b>Name</b>\u202e\n"
    succeed(v,c,release(name=text,tag_name="<i>tag</i>"))
    m=v.tree.model(); assert m.data(m.index(0,0))=="<b>Name</b>\\u202e\\u000a"
    assert m.data(m.index(0,0),Qt.ToolTipRole) is None
    assert m.data(m.index(0,0),Qt.UserRole).name==text
    v.tree.setCurrentIndex(m.index(0,0)); assert "<b>Name</b>" in v.detail.text()
    c.finish(w.decode_response(w.encode_success("b"*32,catalog(release(2))),"b"*32),rid="b"*32)
    assert v.tree.model().data(v.tree.model().index(0,0)).startswith("<b>Name")


def test_UI_24_real_close_kills():
    c=ScanController(ROOT/"run_covearchive.py",command_factory=command("import time;time.sleep(10)"))
    v=MainWindow(ROOT/"run_covearchive.py",c); v.show(); v.url.setText(URL); v.scan.click()
    try:
        until(lambda:c.job.process.processId()>0); pid=c.job.process.processId()
        v.close(); assert gone(pid) and c.job is None
    finally: c.shutdown(1000); v.close()


@pytest.mark.parametrize("raw", [URL+"?q="+"\u00e9"*700,"   "+URL+" "*3000],ids=["query","padding"])
@pytest.mark.parametrize("enter", [False,True])
def test_C2_canonical_request(ui,raw,enter):
    v,c=ui; v.url.setText(raw)
    if enter: QTest.keyClick(v.url,Qt.Key_Return)
    else: v.scan.click()
    assert c.calls==[URL] and v.cancel.isEnabled()


def test_C2_protocol_error_handled(ui):
    v,c=ui
    def reject(url): raise w.ProtocolError()
    c.start=reject; v.url.setText(URL)
    try: v._start()
    except w.ProtocolError: pytest.fail("ProtocolError escaped the window")
    assert "Invalid URL" in v.status.text() and v.scan.isEnabled() and v.url.isEnabled()


@pytest.mark.parametrize("name", ["app.zip","other.zip"])
def test_C3_duplicate_asset_occurrence(ui,name):
    v,c=ui; succeed(v,c,release(assets=[asset(),asset(name=name)]))
    m=v.tree.model(); parent=m.index(0,0)
    assert [m.data(m.index(i,5,parent)) for i in range(2)]==["Eligible","Duplicate asset"]
    assert "1 matching assets" in v.summary.text()


@pytest.mark.parametrize("width,height", [(820,520),(900,600),(1280,720)])
def test_U1_status_visible_and_full_detail(ui,width,height):
    v,c=ui; v.prereleases.setChecked(True)
    succeed(v,c,release(name="Name"*100,tag_name="tag"*100,prerelease=True,
        assets=[asset(name="asset"*100+".zip"),asset(2,"a.exe"),asset(3,state="new"),asset(4,"a.dmg")]))
    v.formats[".dmg"].setChecked(False); v.resize(width,height); v.tree.expandAll(); APP.processEvents()
    h=v.tree.header(); viewport=v.tree.viewport().width()
    assert h.length()<=viewport and h.sectionViewportPosition(5)+h.sectionSize(5)<=viewport
    assert h.sectionSize(5)>=v.tree.fontMetrics().horizontalAdvance("Prerelease, 1 matching assets")+12
    m=v.tree.model(); v.tree.setCurrentIndex(m.index(0,0,m.index(0,0)))
    assert DATE in v.detail.text() and "tag"*100 in v.detail.text() and str(2**63) in v.detail.text()


@pytest.mark.parametrize("state", ["COMPLETED","CANCELLED","FAILED","TIMED_OUT"])
def test_U2_U5_focus_and_deadline(ui,state):
    v,c=ui; v.url.setText(URL); v.scan.click(); assert v.cancel.hasFocus()
    result=w.decode_response(w.encode_success(RID,catalog()),RID) if state=="COMPLETED" else w.ScanFailure(RID,"network")
    c.finish(result,state); assert v.url.hasFocus()
    if state=="TIMED_OUT": assert v.status.text()=="Timed out after 180 seconds; try again"
    if state=="FAILED": QTest.keyClick(v.url,Qt.Key_Return); assert len(c.calls)==2


def test_U5_deadline_message(ui):
    v,c=ui; v.url.setText(URL); v.scan.click(); c.finish(None,"TIMED_OUT")
    assert v.status.text()=="Timed out after 180 seconds; try again"


@pytest.mark.parametrize("width,height", [(820,520),(900,600),(1280,720)])
def test_B2_format_text_fits(ui,width,height):
    v,c=ui; succeed(v,c,release(assets=[asset(name="app.appimage")]))
    v.resize(width,height); APP.processEvents(); h=v.tree.header(); fm=v.tree.fontMetrics()
    assert h.sectionSize(3)>=fm.horizontalAdvance(".appimage")+16
    assert fm.elidedText(".appimage",Qt.ElideRight,h.sectionSize(3)-12)==".appimage"
    assert h.length()<=v.tree.viewport().width()


@pytest.mark.parametrize("width,height", [(820,520),(900,600),(1280,720)])
def test_B4_readable_columns(ui,width,height):
    v,c=ui; v.prereleases.setChecked(True)
    sizes=(4096,120*2**20,int(1023.9*2**20),int(9.9*2**30))
    succeed(v,c,release(name="Long release name "*20,tag_name="v0.0.0-rc.10",prerelease=True,
        published_at="2026-11-13T09:15:00Z",assets=[asset(i+1,"Long asset name "*20+".appimage",size=n) for i,n in enumerate(sizes)]))
    v.resize(width,height); v.tree.expandAll(); APP.processEvents()
    h=v.tree.header(); fm=v.tree.fontMetrics(); m=v.tree.model(); parent=m.index(0,0)
    widths=[(h.sectionSize(column),fm.horizontalAdvance(text)+16) for column,text in
            ((1,"v0.0.0-rc.10"),(2,"0000-00-00"),(4,"1023.9 MiB"))]
    assert all(actual>=required for actual,required in widths), widths
    assert [m.data(m.index(i,4,parent)) for i in range(4)]==["4.0 KiB","120.0 MiB","1023.9 MiB","9.9 GiB"]
    for index in [m.index(0,2),*(m.index(i,4,parent) for i in range(4))]:
        text=m.data(index); available=h.sectionSize(index.column())-12
        assert fm.elidedText(text,Qt.ElideRight,available)==text
    assert h.sectionSize(0)>=200
    assert h.length()<=v.tree.viewport().width() and h.sectionViewportPosition(5)+h.sectionSize(5)<=v.tree.viewport().width()
    assert h.sectionSize(3)>=fm.horizontalAdvance(".appimage")+16
    assert h.sectionSize(5)>=max(210,fm.horizontalAdvance("Prerelease, 1000 matching assets")+16)


@pytest.mark.parametrize("separator", ["\u202e","\ud800"],ids=["bidi","surrogate"])
@pytest.mark.parametrize("asset_row", [False,True],ids=["release","asset"])
def test_P1_detail_timestamp_escaped(ui,separator,asset_row):
    v,c=ui; timestamp="2026-11-13"+separator+"12:00:00Z"
    succeed(v,c,release(published_at=timestamp))
    m=v.tree.model(); parent=m.index(0,0)
    v.tree.setCurrentIndex(m.index(0,0,parent) if asset_row else parent)
    text=v.detail.text()
    assert f"\\u{ord(separator):04x}" in text and separator not in text
    assert all(unicodedata.category(char) not in ("Cc","Cf","Cs","Zl","Zp","Co") for char in text)
    assert not v.detail.grab().isNull()


def test_R1_prerelease_indicator(ui):
    v,c=ui; v.prereleases.setChecked(True); succeed(v,c,release(prerelease=True),release(2))
    m=v.tree.model()
    assert m.data(m.index(0,5))=="Prerelease, 1 matching assets"
    assert m.data(m.index(1,5))=="1 matching assets"


def test_R2_hostile_text_paints_and_escapes(ui):
    from covearchive.ui.window import display
    v,c=ui; hostile="<b>hostile</b>\ud800\u2028\u2029\ue000\u202e\n"
    # Preserve the production decoding and painting path for hostile valid JSON.
    succeed(v,c,release(name=hostile,tag_name=hostile,assets=[asset(name=hostile+".zip")]))
    v.tree.expandAll(); v.tree.setCurrentIndex(v.tree.model().index(0,0))
    assert not v.tree.grab().isNull() and not v.detail.grab().isNull()
    assert display(hostile)=="<b>hostile</b>\\ud800\\u2028\\u2029\\ue000\\u202e\\u000a"


@pytest.mark.parametrize("message,text", [("Worker could not start.","Failed: the scan process could not start"),
    ("Worker exited unexpectedly.","Failed: the scan process stopped unexpectedly")])
def test_R3_R4_friendly_process_error_and_logging(ui,caplog,message,text):
    v,c=ui; v.url.setText(URL); v.scan.click()
    failure=w.ScanFailure(RID,"process",message=message,detail={"stderr":"PRIVATE diagnostic"})
    with caplog.at_level("WARNING",logger="covearchive.ui"): c.finish(failure,"FAILED")
    assert v.status.text()==text and "PRIVATE" not in v.status.text()
    assert "PRIVATE diagnostic" in caplog.text


def test_R4_ui_logs_diagnostics(ui,caplog):
    v,c=ui; v.url.setText(URL); v.scan.click()
    with caplog.at_level("WARNING",logger="covearchive.ui"):
        c.finish(w.ScanFailure(RID,"protocol",detail={"stderr":"diagnostic marker"}),"FAILED")
    assert "diagnostic marker" in caplog.text and "marker" not in v.status.text()


def test_R7_UI_06_real_process_success():
    script=(f"import sys,json;sys.path[:0]=[{str(ROOT/'src')!r},{str(ROOT/'tests')!r}];"
        "from test_scan_worker import catalog,release;from covearchive.scan_worker import encode_success;"
        "r=json.loads(sys.stdin.buffer.readline());"
        "sys.stdout.buffer.write(encode_success(r['request_id'],catalog(release())));sys.stdout.buffer.flush()")
    c=ScanController(ROOT/"run_covearchive.py",command_factory=command(script))
    v=MainWindow(ROOT/"run_covearchive.py",c); v.show(); v.url.setText(URL); v.scan.click()
    try:
        until(lambda:v.scan.isEnabled())
        m=v.tree.model(); assert m.rowCount()==1
        assert m.data(m.index(0,0))=="Release 1"
        assert m.data(m.index(0,0,m.index(0,0)))=="app.zip"
    finally: c.shutdown(1000); v.close()
