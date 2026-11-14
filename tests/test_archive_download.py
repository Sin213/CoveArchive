"""Selected-asset orchestration on actual NTFS handles."""
from dataclasses import replace
from contextlib import contextmanager
import os
import hashlib
import json
import socket
import pytest
from covearchive.archive import download as d, manifest as m, naming, plan, store, winfs
from covearchive.github.models import Release
from covearchive.github.urls import RepositoryTarget
from covearchive.transfer.resolve import TransferPolicy
from test_transfer_resolve import fixture_server, asset, BODY, TARGET

@pytest.fixture
def rig(tmp_path):
    with fixture_server() as server:
        yield tmp_path, server

def release(selected=None, **changes):
    return replace(Release(10,"v1",None,False,False,None,"2026-11-13T00:00:00Z",None,None,
        (selected or asset(),),None,None),**changes)

def run(rig, selected=None, source=None, **options):
    root,server=rig; selected=selected or asset(); source=source or release(selected)
    return d.archive_asset(TARGET,source,selected,str(root),policy=TransferPolicy.for_loopback_test(server.origin),**options)

def project(root): return root / "Owner" / "Repo"
def folder(root): return project(root) / "release-v1--10"
def no_temps(root): assert not list(root.rglob("*.part"))

def test_D01_download_and_persist(rig):
    result=run(rig)
    assert result is not None, "archive_asset must download and return a structured outcome"
    assert result.status.value == "ARCHIVED"
    root,server=rig
    assert (folder(root)/"app.zip").read_bytes()==BODY

@pytest.mark.parametrize('observe',[False,True])
def test_Q33_progress_counts_and_default(rig,observe):
    root,server=rig; data=bytes(range(256))*1024; selected=asset(len(data)); counts=[]
    server.routes['/cdn']=(200,{'Content-Length':str(len(data))},data)
    options={'progress':lambda received,total:counts.append((received,total))} if observe else {}
    result=run(rig,selected,**options)
    assert result.status.value=='ARCHIVED' and (folder(root)/'app.zip').read_bytes()==data
    if observe:
        assert len(counts)>=4 and counts[-1]==(len(data),len(data))
        assert all(a[0]<b[0] for a,b in zip([(0,len(data))]+counts,counts))
        assert all(total==len(data) for received,total in counts)
    else: assert not counts
    no_temps(root)

def test_Q16_progress_cancel_no_publication(rig):
    root,server=rig; data=bytes(range(256))*1024; counts=[]
    server.routes['/cdn']=(200,{'Content-Length':str(len(data))},data)
    result=run(rig,asset(len(data)),progress=lambda n,total:counts.append(n),cancel=lambda:bool(counts))
    assert result.status.value=='CANCELLED' and len(counts)==1 and counts[0]<len(data)
    assert not (folder(root)/'app.zip').exists() and not (project(root)/'archive-index.json').exists()
    no_temps(root)


def test_D02_D07_D09_D17_identity_names_hash_and_consistency(rig):
    root,server=rig; selected=replace(asset(),sha256=hashlib.sha256(BODY).hexdigest())
    result=run(rig,selected)
    assert result.status.value=="ARCHIVED" and result.integrity=="PUBLISHER_VERIFIED"
    assert result.local_sha256==selected.sha256 and result.saved_name=="app.zip" and result.release_folder=="release-v1--10"
    index=m.loads_index((project(root)/"archive-index.json").read_text())
    manifest=m.loads_release_manifest((folder(root)/"release.json").read_text())
    m.check_consistent(index,manifest)
    assert manifest.assets[0].publisher_verified and manifest.assets[0].original_name=="app.zip"
    assert result.asset_id==7 and result.release_id==10 and result.size==len(BODY) and result.transferred
    assert len(server.requests)==2; no_temps(root)

def test_D05_unsafe_name(rig):
    selected=replace(asset(),name="bad:name.zip")
    result=run(rig,selected)
    assert result.status.value=="ARCHIVED" and result.saved_name=="bad_name.zip"
    assert (folder(rig[0])/result.saved_name).read_bytes()==BODY

@pytest.mark.parametrize("kind", ["digest","size"])
def test_D08_D10_D32_mismatch_publishes_nothing(rig,kind):
    root,server=rig
    selected=replace(asset(),sha256="0"*64) if kind=="digest" else replace(asset(),size=len(BODY)+1)
    result=run(rig,selected)
    assert result.status.value=="MISMATCH" and not (folder(root)/"app.zip").exists()
    assert not (project(root)/"archive-index.json").exists(); no_temps(root)

def test_D11_D12_D31_verify_existing_and_modified(rig,monkeypatch):
    root,server=rig; first=run(rig); server.requests.clear()
    assert first.integrity=="LOCAL_HASH_ONLY"
    with monkeypatch.context() as patch:
        patch.setattr(socket,"getaddrinfo",lambda *a,**k: pytest.fail("network while verifying"))
        patch.setattr(socket,"create_connection",lambda *a,**k: pytest.fail("network while planning"))
        assert run(rig).status.value=="VERIFIED_EXISTING"
        plan.plan_archive([release()],archive_root=str(root),repository=TARGET)
    assert not server.requests
    (folder(root)/"app.zip").write_bytes(b"x"*len(BODY))
    result=run(rig)
    assert result.status.value=="MODIFIED" and not server.requests
    assert (folder(root)/"app.zip").read_bytes()==b"x"*len(BODY)

@pytest.mark.parametrize("same",[False,True])
def test_D13_D22_unindexed_same_size_requires_fresh_bytes(rig,same):
    root,server=rig; folder(root).mkdir(parents=True)
    data=BODY if same else b"x"*len(BODY); (folder(root)/"app.zip").write_bytes(data)
    result=run(rig)
    assert result.status.value == ("ALREADY_ARCHIVED" if same else "CONFLICT")
    assert len(server.requests)==2 and (folder(root)/"app.zip").read_bytes()==data
    if same: assert result.integrity=="LOCAL_HASH_ONLY" and result.transferred
    else: assert not (folder(root)/"release.json").exists()
    no_temps(root)

def test_D14_D15_D16_incremental_preserves_siblings_and_removed(rig):
    root,server=rig; run(rig)
    extra=replace(asset(),id=8,name="second.zip")
    server.routes["/repos/Owner/Repo/releases/assets/8"]=(302,{"Location":"/cdn"},b"")
    result=run(rig,extra,release(assets=(asset(),extra)))
    assert result.status.value=="ARCHIVED" and result.change=="NEW_ASSET"
    index=m.loads_index((project(root)/"archive-index.json").read_text())
    assert {a.asset_id for a in index.releases[0].assets}=={7,8}
    newest=replace(asset(),id=9,name="new.zip")
    server.routes["/repos/Owner/Repo/releases/assets/9"]=(302,{"Location":"/cdn"},b"")
    result=run(rig,newest,release(newest,id=11,tag_name="v2"))
    assert result.change=="NEW_RELEASE"
    index=m.loads_index((project(root)/"archive-index.json").read_text())
    assert {r.release_id for r in index.releases}=={10,11} and (folder(root)/"app.zip").read_bytes()==BODY
    for record in index.releases:
        m.check_consistent(index,m.loads_release_manifest((project(root)/record.folder/"release.json").read_text()))
    assert len(list(project(root).glob("release-*")))==2
    removed=run(rig,asset(),release(assets=()))
    assert removed.status.value=="UPSTREAM_REMOVED" and (folder(root)/"app.zip").read_bytes()==BODY

def test_missing_completed_rechecks_recorded_hash(rig):
    root,server=rig; run(rig); path=folder(root)/"app.zip"; path.unlink()
    server.routes["/cdn"]=(200,{},b"x"*len(BODY))
    result=run(rig)
    assert result.status.value=="MISMATCH" and not path.exists(); no_temps(root)
    server.routes["/cdn"]=(200,{},BODY)
    result=run(rig); assert result.status.value=="ARCHIVED" and result.change=="MISSING"

@pytest.mark.parametrize("publisher",[False,True])
def test_planned_publisher_adoption_or_conflict(rig,publisher):
    root,server=rig; folder(root).mkdir(parents=True)
    (folder(root)/"app.zip").write_bytes(BODY if publisher else b"x"*len(BODY))
    selected=replace(asset(),sha256=hashlib.sha256(BODY).hexdigest())
    result=run(rig,selected)
    assert result.status.value==("ALREADY_ARCHIVED" if publisher else "CONFLICT")
    assert not server.requests

def test_reconcile_no_transfer(rig):
    run(rig); rig[1].requests.clear()
    result=run(rig,replace(asset(),name="changed.zip"))
    assert result.status.value=="RECONCILE_REQUIRED" and not rig[1].requests

@pytest.mark.parametrize("failure,code",[("corrupt","manifest_corrupt"),("unsupported","manifest_unsupported"),("foreign","repository_mismatch"),("inconsistent","manifest_inconsistent")])
def test_D21_manifest_fail_closed_before_writes(rig,failure,code):
    root,server=rig; run(rig); server.requests.clear()
    index_path=project(root)/"archive-index.json"; manifest_path=folder(root)/"release.json"
    if failure=="corrupt": index_path.write_text("{")
    elif failure=="unsupported":
        data=json.loads(index_path.read_text()); data["schema_version"]=999; index_path.write_text(json.dumps(data))
    elif failure=="foreign":
        for path in (index_path,manifest_path):
            data=json.loads(path.read_text()); data["repository"]={"owner":"Foreign","name":"Repo","key":"foreign/repo"}
            if path==manifest_path: data["release"]["source_url"]="https://github.com/Foreign/Repo/releases/tag/v1"
            path.write_text(json.dumps(data))
    else:
        data=json.loads(manifest_path.read_text()); data["assets"][0]["original_name"]="changed.zip"; manifest_path.write_text(json.dumps(data))
    before={p:p.read_bytes() for p in project(root).rglob("*") if p.is_file()}
    result=run(rig)
    assert result.status.value=="FAILED" and result.code==code and not server.requests
    assert before=={p:p.read_bytes() for p in project(root).rglob("*") if p.is_file()}

def test_recovery_downgrades_untrusted_hash_claim(rig):
    root,server=rig; run(rig); (project(root)/"archive-index.json").unlink()
    (folder(root)/"app.zip").write_bytes(b"x"*len(BODY)); server.requests.clear()
    result=run(rig)
    assert result.status.value=="CONFLICT" and len(server.requests)==2
    (folder(root)/"app.zip").write_bytes(BODY)
    result=run(rig)
    assert result.status.value=="ALREADY_ARCHIVED" and result.change=="MANIFEST_RECOVERY"

def test_D25_disk_full_cleanup(rig,monkeypatch):
    original=winfs.Handle.write
    def disk_full(handle,data):
        if handle.name and handle.name.endswith(".part"):
            original(handle,data[:2]); raise winfs.ArchiveError("os",status=112)
        return original(handle,data)
    monkeypatch.setattr(winfs.Handle,"write",disk_full)
    result=run(rig)
    assert result.status.value=="FAILED" and result.code=="disk_full"
    assert not (folder(rig[0])/"app.zip").exists(); no_temps(rig[0])

@pytest.mark.parametrize("commit",["commit_release","commit_index"])
def test_commit_failure_preserves_published_file(rig,monkeypatch,commit):
    with monkeypatch.context() as patch:
        patch.setattr(store,commit,lambda *a,**k: (_ for _ in ()).throw(winfs.ArchiveConflictError("stale_index")))
        result=run(rig)
    assert result.status.value=="FAILED" and result.code=="manifest_commit_incomplete"
    assert (folder(rig[0])/"app.zip").read_bytes()==BODY; no_temps(rig[0])
    assert run(rig).status.value=="ALREADY_ARCHIVED"

def test_closed_status_vocabulary():
    assert {s.value for s in d.ArchiveStatus}=={"ARCHIVED","ALREADY_ARCHIVED","VERIFIED_EXISTING","MODIFIED","CONFLICT","MISMATCH","CANCELLED","UPSTREAM_REMOVED","RECONCILE_REQUIRED","FAILED"}


def test_missing_completed_release_folder_is_restored(rig):
    root,server=rig; run(rig)
    for child in folder(root).iterdir(): child.unlink()
    folder(root).rmdir(); server.requests.clear()
    result=run(rig)
    assert result.status.value=="ARCHIVED" and result.change=="MISSING"
    index=m.loads_index((project(root)/"archive-index.json").read_text())
    m.check_consistent(index,m.loads_release_manifest((folder(root)/"release.json").read_text()))
    assert (folder(root)/"app.zip").read_bytes()==BODY and len(server.requests)==2

@pytest.mark.parametrize("failure",["foreign_recovery","missing_manifest"])
def test_recovery_identity_and_missing_manifest_fail_closed(rig,failure):
    root,server=rig; run(rig); server.requests.clear()
    if failure=="foreign_recovery":
        (project(root)/"archive-index.json").unlink()
        path=folder(root)/"release.json"; data=json.loads(path.read_text())
        data["repository"]={"owner":"Foreign","name":"Repo","key":"foreign/repo"}
        data["release"]["source_url"]="https://github.com/Foreign/Repo/releases/tag/v1"; path.write_text(json.dumps(data))
    else:
        (folder(root)/"release.json").unlink(); (folder(root)/"foreign.txt").write_bytes(b'foreign')
    before={p:p.read_bytes() for p in project(root).rglob("*") if p.is_file()}
    result=run(rig)
    assert result.status.value=="FAILED" and result.code==("repository_mismatch" if failure=="foreign_recovery" else "manifest_inconsistent")
    assert not server.requests and before=={p:p.read_bytes() for p in project(root).rglob("*") if p.is_file()}

@pytest.mark.parametrize("state",["unsupported","unavailable","draft","wrong_asset"])
def test_selection_rejected_without_writes(rig,state):
    root,server=rig; selected=asset(); source=release()
    if state=="unsupported": selected=replace(selected,supported_extension=None); source=release(selected)
    if state=="unavailable": selected=replace(selected,state="new"); source=release(selected)
    if state=="draft": source=release(draft=True)
    if state=="wrong_asset": selected=replace(selected,size=1)
    result=run(rig,selected,source)
    assert result.status.value=="FAILED" and not server.requests and not list(root.iterdir())


def test_R1_busy_asset_is_retryable_file_busy(rig,monkeypatch):
    run(rig)
    monkeypatch.setattr(store,"verify_asset",lambda directory,record: store.Verification("busy",record))
    result=run(rig)
    assert result.status.value=="FAILED" and result.code=="file_busy" and result.retryable

@pytest.mark.parametrize("failure",["download","manifest","filesystem"])
def test_R2_all_incomplete_commit_paths_retryable(rig,monkeypatch,failure):
    from covearchive.transfer.resolve import DownloadError
    error={"download":DownloadError("repository_mismatch"),"manifest":m.ManifestError("field"),
        "filesystem":winfs.ArchiveError("os")}[failure]
    def fail(*args,**kwargs): raise error
    monkeypatch.setattr(store,"commit_index",fail)
    result=run(rig)
    assert result.status.value=="FAILED" and result.code=="manifest_commit_incomplete" and result.retryable


@pytest.mark.parametrize("completed",[False,True])
def test_hardlinked_destination_never_overwritten(rig,completed):
    root,server=rig
    if completed: run(rig)
    else:
        folder(root).mkdir(parents=True); (folder(root)/"app.zip").write_bytes(BODY)
    path=folder(root)/"app.zip"; alias=root/"hardlink"; os.link(path,alias)
    identity=path.stat().st_ino; server.requests.clear()
    result=run(rig)
    assert result.status.value==("MODIFIED" if completed else "CONFLICT")
    assert path.stat().st_ino==identity and path.read_bytes()==alias.read_bytes()==BODY
    assert len(server.requests)==(0 if completed else 2); no_temps(root)

def test_directory_destination_fails_without_replacement(rig):
    root,server=rig; path=folder(root)/"app.zip"; path.mkdir(parents=True)
    result=run(rig)
    assert result.status.value=="FAILED" and result.code=="unsafe_path"
    assert path.is_dir() and not list(path.iterdir()); no_temps(root)
    assert not (project(root)/"archive-index.json").exists()

def test_exclusive_destination_is_retryable_and_untouched(rig):
    root,server=rig; run(rig); server.requests.clear()
    with winfs.ArchiveRoot.open(str(root)) as authorized:
        with store.ensure_project(authorized,TARGET) as pinned:
            with store.ensure_release(pinned,"release-v1--10",10) as directory:
                with directory._open("app.zip",share=0):
                    result=run(rig)
    assert result.code=="file_busy" and result.retryable and result.status.value=="FAILED"
    assert not server.requests and (folder(root)/"app.zip").read_bytes()==BODY; no_temps(root)

@pytest.mark.parametrize("indexed",[False,True])
@pytest.mark.parametrize("corrupt",[False,True])
def test_unrelated_release_issues_preserve_bytes_and_expose_commit_limit(rig,indexed,corrupt):
    root,server=rig; run(rig)
    bad=folder(root)/"release.json"
    if corrupt: bad.write_text("{")
    else:
        data=json.loads(bad.read_text()); data["assets"][0]["original_name"]="different.zip"; bad.write_text(json.dumps(data))
    index=project(root)/"archive-index.json"
    if not indexed: index.unlink()
    before={p:p.read_bytes() for p in folder(root).iterdir() if p.is_file()}
    old_index=index.read_bytes() if indexed else None
    extra=replace(asset(),id=8,name="other.zip")
    server.routes["/repos/Owner/Repo/releases/assets/8"]=(302,{"Location":"/cdn"},b"")
    result=run(rig,extra,release(extra,id=11,tag_name="v2"))
    new_folder=project(root)/"release-v2--11"
    assert (new_folder/"other.zip").read_bytes()==BODY
    assert before=={p:p.read_bytes() for p in folder(root).iterdir() if p.is_file()}
    if indexed:
        # Phase A ignores unrelated issues; commit_index validates ALL indexed
        # releases and legitimately refuses this inconsistent project snapshot.
        assert result.code=="manifest_commit_incomplete" and result.retryable
        assert index.read_bytes()==old_index
    else: assert result.status.value=="ARCHIVED"
    no_temps(root)

def test_real_stale_index_token_preserves_publication_and_retry_converges(rig,monkeypatch):
    root,server=rig; run(rig)
    extra=replace(asset(),id=8,name="other.zip"); source=release(extra,id=11,tag_name="v2")
    server.routes["/repos/Owner/Repo/releases/assets/8"]=(302,{"Location":"/cdn"},b"")
    load=store.load_project_state; calls=[]
    def race(project_handle):
        state=load(project_handle); calls.append(state)
        if len(calls)==3:
            # An external writer changes the bytes after the Phase B snapshot.
            # Logical identity is unchanged, but the real on-disk CAS token is stale.
            path=project(root)/"archive-index.json"; path.write_bytes(path.read_bytes()+b"\n")
        return state
    with monkeypatch.context() as patch:
        patch.setattr(store,"load_project_state",race)
        result=run(rig,extra,source)
    assert result.code=="manifest_commit_incomplete" and result.retryable
    path=project(root)/"release-v2--11"/"other.zip"; identity=path.stat().st_ino
    assert path.read_bytes()==BODY
    result=run(rig,extra,source)
    assert result.status.value=="ALREADY_ARCHIVED" and result.change=="MANIFEST_RECOVERY"
    assert path.stat().st_ino==identity and path.read_bytes()==BODY; no_temps(root)

def test_interleaved_sibling_archives_merge_latest_index(rig,monkeypatch):
    root,server=rig; extra=replace(asset(),id=8,name="second.zip"); source=release(assets=(asset(),extra))
    server.routes["/repos/Owner/Repo/releases/assets/8"]=(302,{"Location":"/cdn"},b"")
    writer=store.project_writer; nested=[]
    @contextmanager
    def interleave(project_handle):
        if not nested:
            nested.append(None); nested[0]=run(rig,extra,source)
        with writer(project_handle): yield project_handle
    monkeypatch.setattr(store,"project_writer",interleave)
    result=run(rig,asset(),source)
    assert result.status.value==nested[0].status.value=="ARCHIVED"
    index=m.loads_index((project(root)/"archive-index.json").read_text())
    manifest=m.loads_release_manifest((folder(root)/"release.json").read_text())
    m.check_consistent(index,manifest)
    assert {a.asset_id for a in manifest.assets}=={7,8}
    assert (folder(root)/"app.zip").read_bytes()==(folder(root)/"second.zip").read_bytes()==BODY
    no_temps(root)


def test_completed_candidate_rechecks_bytes_after_store_publication(rig,monkeypatch):
    write=store.write_asset
    def changed(*args,**kwargs):
        outcome=write(*args,**kwargs)
        (folder(rig[0])/"app.zip").write_bytes(b"x"*len(BODY))
        return outcome
    monkeypatch.setattr(store,"write_asset",changed)
    result=run(rig)
    assert result.status.value=="CONFLICT"
    assert not (folder(rig[0])/"release.json").exists()
    assert not (project(rig[0])/"archive-index.json").exists(); no_temps(rig[0])


@pytest.mark.parametrize("phase",["existing","missing","recovered","planned"])
@pytest.mark.parametrize("matches",[False,True])
def test_repair_C1_added_digest_enforced(rig,phase,matches):
    root,server=rig; run(rig); index=project(root)/"archive-index.json"; manifest=folder(root)/"release.json"
    if phase=="missing": (folder(root)/"app.zip").unlink()
    if phase=="recovered": index.unlink()
    if phase=="planned":
        for path in (index,manifest):
            data=json.loads(path.read_text()); assets=data["releases"][0]["assets"] if path==index else data["assets"]
            assets[0].update(status="planned",local_sha256=None); path.write_text(json.dumps(data))
    before={p:p.read_bytes() for p in (index,manifest) if p.exists()}; server.requests.clear()
    item=replace(asset(),sha256=hashlib.sha256(BODY).hexdigest() if matches else "0"*64)
    result=run(rig,item)
    expected=("VERIFIED_EXISTING" if phase=="existing" else "ARCHIVED" if phase=="missing" else "ALREADY_ARCHIVED") if matches else ("MISMATCH" if phase in ("existing","missing") else "CONFLICT")
    assert result.status.value==expected
    if phase=="existing" or not matches:
        assert not server.requests and before=={p:p.read_bytes() for p in before}
    if matches:
        assert result.integrity==("LOCAL_HASH_ONLY" if phase in ("existing","missing") else "PUBLISHER_VERIFIED")
        if phase in ("planned","recovered"):
            assert m.loads_release_manifest(manifest.read_text()).assets[0].publisher_sha256==item.sha256
    no_temps(root)

def test_repair_C2_incremental_commit_converges_without_losing_siblings(rig,monkeypatch):
    root,server=rig; run(rig); identity=(folder(root)/"app.zip").stat().st_ino
    extra=replace(asset(),id=8,name="second.zip"); source=release(assets=(asset(),extra))
    server.routes["/repos/Owner/Repo/releases/assets/8"]=(302,{"Location":"/cdn"},b"")
    with monkeypatch.context() as patch:
        patch.setattr(store,"commit_index",lambda *a,**k: (_ for _ in ()).throw(winfs.ArchiveConflictError("stale_index")))
        assert run(rig,extra,source).code=="manifest_commit_incomplete"
    other=replace(asset(),id=9,name="other.zip"); other_source=release(other,id=11,tag_name="v2")
    server.routes["/repos/Owner/Repo/releases/assets/9"]=(302,{"Location":"/cdn"},b"")
    assert run(rig,other,other_source).code=="manifest_commit_incomplete"
    server.requests.clear(); result=run(rig,extra,source)
    assert result.status.value=="ALREADY_ARCHIVED"
    assert (folder(root)/"app.zip").stat().st_ino==identity and len(server.requests)==2
    index=m.loads_index((project(root)/"archive-index.json").read_text()); manifest=m.loads_release_manifest((folder(root)/"release.json").read_text())
    m.check_consistent(index,manifest); assert {a.asset_id for a in manifest.assets}=={7,8}
    server.requests.clear()
    assert run(rig,extra,source).status.value=="VERIFIED_EXISTING" and not server.requests
    assert run(rig,other,other_source).status.value=="ALREADY_ARCHIVED"


@pytest.mark.parametrize("recovered",[False,True])
@pytest.mark.parametrize("matches",[False,True])
def test_repair_C1_planned_missing_enforces_added_digest(rig,recovered,matches):
    root,server=rig; run(rig); index=project(root)/"archive-index.json"; manifest=folder(root)/"release.json"
    if recovered: index.unlink()
    else:
        for path in (index,manifest):
            data=json.loads(path.read_text()); assets=data["releases"][0]["assets"] if path==index else data["assets"]
            assets[0].update(status="planned",local_sha256=None); path.write_text(json.dumps(data))
    (folder(root)/"app.zip").unlink(); server.requests.clear()
    item=replace(asset(),sha256=hashlib.sha256(BODY).hexdigest() if matches else "0"*64)
    result=run(rig,item)
    assert result.status.value==("ARCHIVED" if matches else "MISMATCH") and len(server.requests)==2
    if matches: assert m.loads_release_manifest(manifest.read_text()).assets[0].publisher_verified
    else: assert not (folder(root)/"app.zip").exists()
    no_temps(root)

@pytest.mark.parametrize("change",["missing_asset","changed_name","changed_tag"])
def test_repair_C2_rejects_other_manifest_differences(rig,change):
    root,server=rig; run(rig); path=folder(root)/"release.json"; data=json.loads(path.read_text())
    if change=="missing_asset": data["assets"]=[]
    elif change=="changed_name": data["assets"][0]["saved_name"]="changed.zip"
    else:
        data["release"]["tag"]="changed"; data["release"]["source_url"]="https://github.com/Owner/Repo/releases/tag/changed"
    path.write_text(json.dumps(data)); before=path.read_bytes(); server.requests.clear()
    assert run(rig).code=="manifest_inconsistent" and not server.requests and path.read_bytes()==before


def test_repair_C2_index_planned_version_wins_over_completed_claim(rig):
    root,server=rig; run(rig); path=project(root)/"archive-index.json"
    data=json.loads(path.read_text()); data["releases"][0]["assets"][0].update(status="planned",local_sha256=None); path.write_text(json.dumps(data))
    before=path.read_bytes(); server.routes["/cdn"]=(200,{},b"x"*len(BODY)); server.requests.clear()
    assert run(rig).status.value=="CONFLICT" and len(server.requests)==2
    assert path.read_bytes()==before and (folder(root)/"app.zip").read_bytes()==BODY
    server.routes["/cdn"]=(200,{},BODY)
    assert run(rig).status.value=="ALREADY_ARCHIVED"
    m.check_consistent(m.loads_index(path.read_text()),m.loads_release_manifest((folder(root)/"release.json").read_text()))


@pytest.mark.parametrize("transition",["local","added","publisher"])
def test_postfinal_F1_planned_completion_interruption_converges(rig,monkeypatch,transition):
    root,server=rig; run(rig); digest=hashlib.sha256(BODY).hexdigest()
    old=digest if transition=="publisher" else None
    current=digest if transition!="local" else None
    item=replace(asset(),id=8,name="second.zip",sha256=old)
    source=release(assets=(asset(),item)); server.routes["/repos/Owner/Repo/releases/assets/8"]=(302,{"Location":"/cdn"},b"")
    run(rig,item,source)
    index=project(root)/"archive-index.json"; manifest=folder(root)/"release.json"
    for path in (index,manifest):
        data=json.loads(path.read_text()); records=data["releases"][0]["assets"] if path==index else data["assets"]
        next(a for a in records if a['asset_id']==8).update(status='planned',local_sha256=None)
        path.write_text(json.dumps(data))
    item=replace(item,sha256=current); source=release(assets=(asset(),item))
    with monkeypatch.context() as patch:
        patch.setattr(store,'commit_index',lambda *a,**k: (_ for _ in ()).throw(winfs.ArchiveConflictError('stale_index')))
        assert run(rig,item,source).code=='manifest_commit_incomplete'
    if transition=='added':
        wrong=replace(item,sha256='0'*64)
        assert run(rig,wrong,release(assets=(asset(),wrong))).status.value=='CONFLICT'
    result=run(rig,item,source)
    assert result.status.value=='ALREADY_ARCHIVED'
    m.check_consistent(m.loads_index(index.read_text()),m.loads_release_manifest(manifest.read_text()))
    assert {a.asset_id for a in m.loads_release_manifest(manifest.read_text()).assets}=={7,8}
    assert (folder(root)/'app.zip').read_bytes()==(folder(root)/'second.zip').read_bytes()==BODY
    assert run(rig,item,source).status.value=='VERIFIED_EXISTING'; no_temps(root)

@pytest.mark.parametrize('mode',['publisher_adopt','already_present','published'])
def test_postfinal_F2_cancel_adoption_but_finish_real_publication(rig,monkeypatch,mode):
    root,server=rig; flag=[False]
    item=replace(asset(),sha256=hashlib.sha256(BODY).hexdigest() if mode=='publisher_adopt' else None)
    if mode!='published': folder(root).mkdir(parents=True); (folder(root)/'app.zip').write_bytes(BODY)
    verify,write=store.verify_asset,store.write_asset
    def verification(*args,**kwargs):
        result=verify(*args,**kwargs)
        if mode=='publisher_adopt': flag[0]=True
        return result
    def publication(*args,**kwargs):
        result=write(*args,**kwargs); flag[0]=True; return result
    monkeypatch.setattr(store,'verify_asset',verification); monkeypatch.setattr(store,'write_asset',publication)
    result=run(rig,item,cancel=lambda:flag[0])
    assert result.status.value==('ARCHIVED' if mode=='published' else 'CANCELLED')
    if mode!='published':
        assert not (folder(root)/'release.json').exists() and not (project(root)/'archive-index.json').exists()
    assert (folder(root)/'app.zip').read_bytes()==BODY; no_temps(root)


def test_postfinal_F2_cancel_completed_verification_preserves_manifests(rig,monkeypatch):
    root,server=rig; run(rig)
    paths=[folder(root)/'release.json',project(root)/'archive-index.json']; before=[p.read_bytes() for p in paths]
    verify=store.verify_asset; flag=[False]
    def cancelled(*args):
        result=verify(*args); flag[0]=True; return result
    monkeypatch.setattr(store,'verify_asset',cancelled)
    assert run(rig,cancel=lambda:flag[0]).status.value=='CANCELLED'
    assert [p.read_bytes() for p in paths]==before; no_temps(root)


@pytest.mark.parametrize('foreign',['none','file','directory','temp_directory'])
def test_pf2_missing_indexed_manifest_narrow_restoration(rig,foreign):
    root,server=rig; run(rig); manifest=folder(root)/'release.json'; manifest.unlink()
    identity=(folder(root)/'app.zip').stat().st_ino
    if foreign=='file': (folder(root)/'foreign').write_bytes(b'sentinel')
    if foreign=='directory':
        (folder(root)/'app.zip').unlink(); (folder(root)/'app.zip').mkdir()
    if foreign=='temp_directory': (folder(root)/('.covearchive-tmp-'+'a'*32+'.part')).mkdir()
    if foreign=='none': (folder(root)/('.covearchive-tmp-'+'a'*32+'.part')).write_bytes(b'orphan')
    server.requests.clear(); result=run(rig)
    if foreign=='none':
        assert result.status.value=='ALREADY_ARCHIVED' and result.change=='MANIFEST_RECOVERY'
        assert manifest.exists() and (folder(root)/'app.zip').stat().st_ino==identity
        m.check_consistent(m.loads_index((project(root)/'archive-index.json').read_text()),m.loads_release_manifest(manifest.read_text()))
        no_temps(root)
    else: assert result.status.value=='FAILED' and result.code in ('manifest_inconsistent','unsafe_path') and not manifest.exists()
    assert not server.requests


def test_pf2_restore_metadata_preserves_indexed_siblings(rig):
    root,server=rig; run(rig); extra=replace(asset(),id=8,name='second.zip')
    source=release(assets=(asset(),extra)); server.routes['/repos/Owner/Repo/releases/assets/8']=(302,{'Location':'/cdn'},b'')
    run(rig,extra,source)
    before=m.loads_index((project(root)/'archive-index.json').read_text())
    identity=(folder(root)/'second.zip').stat().st_ino; (folder(root)/'release.json').unlink(); server.requests.clear()
    result=run(rig,asset(),source)
    assert result.status.value=='ALREADY_ARCHIVED' and result.change=='MANIFEST_RECOVERY' and not server.requests
    after=m.loads_index((project(root)/'archive-index.json').read_text()); assert after==before
    m.check_consistent(after,m.loads_release_manifest((folder(root)/'release.json').read_text()))
    assert (folder(root)/'second.zip').stat().st_ino==identity; no_temps(root)


@pytest.mark.parametrize('removed',[False,True])
def test_pf3_completed_sibling_repairs_pending_recovery(rig,monkeypatch,removed):
    root,server=rig; run(rig); extra=replace(asset(),id=8,name='second.zip')
    source=release(assets=(asset(),extra)); server.routes['/repos/Owner/Repo/releases/assets/8']=(302,{'Location':'/cdn'},b'')
    with monkeypatch.context() as patch:
        patch.setattr(store,'commit_index',lambda *a: (_ for _ in ()).throw(winfs.ArchiveError('os')))
        assert run(rig,extra,source).code=='manifest_commit_incomplete'
    identity=(folder(root)/'second.zip').stat().st_ino; server.requests.clear()
    result=run(rig,asset(),release() if removed else source)
    assert result.status.value=='ALREADY_ARCHIVED' and result.change=='MANIFEST_RECOVERY' and not server.requests
    index=m.loads_index((project(root)/'archive-index.json').read_text())
    manifest=m.loads_release_manifest((folder(root)/'release.json').read_text()); m.check_consistent(index,manifest)
    assert {a.asset_id:a.status for a in manifest.assets}=={7:'completed',8:'planned'}
    assert (folder(root)/'second.zip').stat().st_ino==identity
    other=replace(asset(),id=9,name='other.zip'); server.routes['/repos/Owner/Repo/releases/assets/9']=(302,{'Location':'/cdn'},b'')
    assert run(rig,other,release(other,id=11,tag_name='v2')).status.value=='ARCHIVED'; no_temps(root)


def test_pf3_cancel_during_final_flush_is_after_publication(rig,monkeypatch):
    root,server=rig; flag=[False]; flush=winfs.Handle.flush
    def final_flush(self):
        flag[0]=True; return flush(self)
    monkeypatch.setattr(winfs.Handle,'flush',final_flush)
    result=run(rig,cancel=lambda:flag[0])
    assert flag[0] and result.status.value=='ARCHIVED'
    assert hashlib.sha256((folder(root)/'app.zip').read_bytes()).hexdigest()==result.local_sha256
    m.check_consistent(m.loads_index((project(root)/'archive-index.json').read_text()),
        m.loads_release_manifest((folder(root)/'release.json').read_text())); no_temps(root)



def test_pf3_phase_b_missing_manifest_cannot_take_idempotent_noop(rig,monkeypatch):
    root,server=rig; run(rig); (folder(root)/'app.zip').unlink(); writer=store.project_writer
    @contextmanager
    def interrupted(project_handle):
        (folder(root)/'release.json').unlink()
        with writer(project_handle): yield
    monkeypatch.setattr(store,'project_writer',interrupted)
    assert run(rig).status.value=='ARCHIVED'
    m.check_consistent(m.loads_index((project(root)/'archive-index.json').read_text()),
        m.loads_release_manifest((folder(root)/'release.json').read_text())); no_temps(root)


@pytest.mark.parametrize('foreign',[False,True])
def test_pf4_restore_before_new_asset_transfer(rig,monkeypatch,foreign):
    root,server=rig; run(rig); (folder(root)/'release.json').unlink()
    item=replace(asset(),id=8,name='second.zip'); source=release(assets=(asset(),item))
    server.routes['/repos/Owner/Repo/releases/assets/8']=(302,{'Location':'/cdn'},b'')
    if foreign: (folder(root)/'foreign.txt').write_bytes(b'untouched')
    before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}; opened=d.open_asset_stream
    @contextmanager
    def checked(*args,**kwargs):
        m.check_consistent(m.loads_index((project(root)/'archive-index.json').read_text()),
            m.loads_release_manifest((folder(root)/'release.json').read_text()))
        with opened(*args,**kwargs) as chunks: yield chunks
    monkeypatch.setattr(d,'open_asset_stream',checked); server.requests.clear()
    result=run(rig,item,source)
    if foreign:
        assert result.code=='manifest_inconsistent' and not server.requests
        assert {str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}==before
    else:
        assert result.status.value=='ARCHIVED'
        m.check_consistent(m.loads_index((project(root)/'archive-index.json').read_text()),m.loads_release_manifest((folder(root)/'release.json').read_text()))
        assert (folder(root)/'app.zip').read_bytes()==BODY and (folder(root)/'second.zip').read_bytes()==BODY
    no_temps(root)


@pytest.mark.parametrize('entry',['selected','foreign','directory'])
def test_pf4_selected_name_residual_is_narrow(rig,entry):
    root,server=rig; run(rig); (folder(root)/'release.json').unlink()
    item=replace(asset(),id=8,name='second.zip'); source=release(assets=(asset(),item))
    server.routes['/repos/Owner/Repo/releases/assets/8']=(302,{'Location':'/cdn'},b'')
    path=folder(root)/('foreign.zip' if entry=='foreign' else item.name)
    path.mkdir() if entry=='directory' else path.write_bytes(BODY)
    identity=path.stat().st_ino; server.requests.clear(); result=run(rig,item,source)
    if entry=='selected':
        assert result.status.value=='ALREADY_ARCHIVED' and path.read_bytes()==BODY
        m.check_consistent(m.loads_index((project(root)/'archive-index.json').read_text()),m.loads_release_manifest((folder(root)/'release.json').read_text()))
    else:
        assert result.code in ('manifest_inconsistent','unsafe_path') and not server.requests
        assert not (folder(root)/'release.json').exists()
    assert path.stat().st_ino==identity; no_temps(root)
