"""Isolated preference persistence and non-destructive failure handling."""
import json
import sys
import pytest
from covearchive.ui.preferences import PreferenceStore
from test_ui_window import ui

@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def test_roundtrip_and_repository_isolation(tmp_path):
    store=PreferenceStore(); prefs, problem, writable=store.load()
    assert writable and problem is None
    prefs["archive_root"]=str(tmp_path / "archives")
    prefs["repositories"]["owner/repo"]={"include":["*WIN*"], "exclude":["*debug*"],
        "extensions":[".zip", ".exe"], "prereleases":True}
    prefs["window"]={"width":1, "height":99999, "tab":"queue"}
    assert store.save(prefs) is None
    loaded, problem, writable=PreferenceStore().load()
    assert problem is None and writable and loaded["repositories"]==prefs["repositories"]
    assert "other/repo" not in loaded["repositories"]
    assert loaded["archive_root"]==prefs["archive_root"]
    assert 600 <= loaded["window"]["width"] <= 3840
    assert 400 <= loaded["window"]["height"] <= 2160


@pytest.mark.parametrize('appearance',['light','dark'])
def test_optional_appearance_roundtrip(tmp_path,appearance):
    store=PreferenceStore(tmp_path/'prefs.json'); prefs=store.load()[0]
    assert 'appearance' not in prefs
    prefs['appearance']=appearance
    assert store.save(prefs) is None
    assert PreferenceStore(store.path).load()[0]['appearance']==appearance


@pytest.mark.parametrize('appearance',[None,'system','',True,[],{}])
def test_invalid_appearance_latches_readonly(tmp_path,appearance):
    path=tmp_path/'prefs.json'; prefs=PreferenceStore(path).load()[0]
    prefs['appearance']=appearance; raw=json.dumps(prefs); path.write_text(raw)
    store=PreferenceStore(path); _,problem,writable=store.load()
    assert problem and not writable and path.read_text()==raw


@pytest.mark.parametrize("data", [b"broken", b'{"version":2}', b'{"version":1,"version":1}',
    b'{"version":NaN}', b"x"*(256*1024+1),
    b'{"version":1,"archive_root":null,"window":{"width":900,"height":600,"tab":"browse"},"repositories":{"o/r":{"include":[],"exclude":[],"extensions":[],"prereleases":false,"token":"SECRET"}}}',
    b'{"version":1,"archive_root":null,"window":{"width":900,"height":600,"tab":"browse","height":700},"repositories":{}}'], ids=["corrupt", "newer", "duplicate", "nonfinite", "oversized", "nested_unknown", "nested_duplicate"])
def test_bad_files_are_preserved_and_saves_refused(tmp_path, data):
    path=tmp_path/"prefs.json"; path.write_bytes(data); store=PreferenceStore(path)
    assert store.save(PreferenceStore().load()[0])
    prefs, problem, writable=store.load()
    assert problem and not writable and prefs["repositories"]=={}
    assert "read-only" in store.save(prefs) and path.read_bytes()==data
    assert list(tmp_path.iterdir())==[path]


@pytest.mark.parametrize("change", ["url", "repo", "extensions", "patterns", "boolean", "count", "unknown", "window"])
def test_invalid_schema_preserved(tmp_path, change):
    prefs=PreferenceStore().load()[0]
    rule={"include":[],"exclude":[],"extensions":[".zip"],"prereleases":False}
    prefs["repositories"]={"o/r":rule}
    if change=="url": prefs["archive_root"]="https://host/path?token=SECRET"
    if change=="repo": prefs["repositories"]={"https://host/x":rule}
    if change=="extensions": rule["extensions"]=[".rar"]
    if change=="patterns": rule["exclude"]=["bad/path"]
    if change=="boolean": rule["prereleases"]=1
    if change=="count": prefs["repositories"]={f"o/r{i}":rule for i in range(501)}
    if change=="unknown": prefs["token"]="SECRET"
    if change=="window": prefs["window"]["width"]=True
    path=tmp_path/"prefs.json"; data=json.dumps(prefs).encode(); path.write_bytes(data)
    store=PreferenceStore(path); _, problem, writable=store.load()
    assert problem and not writable and path.read_bytes()==data


def test_no_localappdata_has_no_cwd_fallback(monkeypatch,tmp_path):
    monkeypatch.delenv("LOCALAPPDATA")
    store=PreferenceStore(); prefs, problem, writable=store.load()
    if sys.platform == "win32":
        assert store.path is None and problem and not writable
        assert store.save(prefs)
    else:
        assert store.path == tmp_path/"config"/"covearchive"/"preferences.json"
        assert writable and problem is None
    store = PreferenceStore(tmp_path/"prefs.json")
    prefs = store.load()[0]
    store.path.mkdir()
    assert "Could not read preferences" in store.save(prefs) and store.writable
    store.path.rmdir()
    assert store.save(prefs) is None


@pytest.mark.parametrize("stage", ["open", "write", "commit", "validate"])
def test_atomic_failures_preserve_file(tmp_path, monkeypatch, stage):
    from covearchive.ui import preferences as module
    path=tmp_path/"prefs.json"; store=PreferenceStore(path); prefs=store.load()[0]
    assert store.save(prefs) is None
    original=path.read_bytes(); calls=[]
    class FailedSave:
        def __init__(self, name): calls.append(("path",name))
        def setDirectWriteFallback(self, value): calls.append(("fallback",value))
        def open(self, mode): return stage!="open"
        def write(self, data): return -1 if stage=="write" else len(data)
        def commit(self): return stage!="commit"
        def cancelWriting(self): calls.append(("cancel",True))
    monkeypatch.setattr(module,"QSaveFile",FailedSave)
    prefs["window"]["tab"]="queue"
    if stage=="validate": prefs["archive_root"]="https://host/?token=SECRET"
    assert store.save(prefs) and path.read_bytes()==original
    if stage!="validate": assert ("fallback",False) in calls
    assert store.load()[0]["archive_root"] is None


@pytest.mark.parametrize("kind", ["corrupt", "newer", "valid", "appeared", "oversized"])
def test_external_changes_preserved(ui,kind):
    from test_ui_window import succeed, release
    v,c = ui
    succeed(v,c,release())
    if kind!="appeared":
        v.save_rules.click()
        v.save_rules.click()
        assert v.rule_error.text()=="Rules saved"
        v.preferences = PreferenceStore(v.preferences.path)
        assert v.preferences.save(v.settings) is None
    external = v.preferences.load()[0]
    external["window"]["tab"] = "queue"
    data = b"broken" if kind=="corrupt" else b'{"version":2}' if kind=="newer" else json.dumps(external).encode("utf-8")
    if kind=="oversized": data = b"x"*(256*1024+1)
    v.preferences.path.write_bytes(data)
    v.save_rules.click()
    assert "changed on disk" in v.rule_error.text() and not v.preferences.writable
    assert v.preferences.path.read_bytes()==data and v.preferences.save(v.settings)
    assert v.preferences.path.read_bytes()==data


@pytest.mark.parametrize('appearance',[None,'light','dark'])
def test_m1_baseline_bytes_and_older_reader(tmp_path,monkeypatch,appearance):
    from covearchive.ui import preferences as module
    store=PreferenceStore(tmp_path/'prefs.json'); prefs=store.load()[0]
    if appearance: prefs['appearance']=appearance
    assert store.save(prefs) is None
    baseline=b'{"version":1,"archive_root":null,"repositories":{},"window":{"width":1180,"height":720,"tab":"browse"}}\n'
    raw=store.path.read_bytes(); assert raw==(baseline if appearance is None else baseline[:-2]+b',"appearance":"'+appearance.encode()+b'"}\n')
    if appearance: # Older builds reject the extra key, latch read-only, and preserve the file.
        original=module._validated
        def older(data): module._keys(data,('version','archive_root','window','repositories')); return original(data)
        monkeypatch.setattr(module,'_validated',older); reader=PreferenceStore(store.path); _,problem,writable=reader.load()
        assert problem and not writable and reader.save(prefs) and store.path.read_bytes()==raw
