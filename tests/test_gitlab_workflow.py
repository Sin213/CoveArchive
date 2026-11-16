"""Real GitLab queue/archive pipeline with only wire transport faked."""
from dataclasses import replace
from types import SimpleNamespace as NS
from urllib.parse import urlsplit
import hashlib
import io
import json
import sqlite3
import uuid
import pytest
from covearchive.github import client as g
from covearchive import gitlab as gl
from covearchive.archive import download as d, manifest as m, store, naming
from covearchive.github.select import select_releases
from covearchive.github.urls import RepositoryTarget
from covearchive.queue import QueueStore, QueueRunner, JobState as S
from covearchive.transfer import resolve
from test_gitlab import Wire
from test_provider_workflow import wire as legacy_wire, scan as legacy_scan, snapshot, GH, CB
from test_transfer_resolve import BODY, public_dns
from test_queue_store import legacy_queue

TARGET=RepositoryTarget('Owner/nested','Repo','gitlab','gitlab.com')
SHA=hashlib.sha256(BODY).hexdigest()

@pytest.fixture
def rig(tmp_path,monkeypatch,legacy_wire):
    state=NS(log=[],mode='ok',runner=None,job=None)
    root=tmp_path.parent/('g'+uuid.uuid4().hex[:8]); root.mkdir()
    state.root=root; state.path=tmp_path/'q.db'; state.store=QueueStore(state.path,clock=lambda:100)
    api=Wire(); state.api=api; api.project['path_with_namespace']='Owner/nested/Repo'
    api.releases[0].update(tag_name='v10',name='v10')
    api.releases[0]['assets']['links'][0].update(id=7,name='app.zip',url=gl.build_package_url(34675721,'glab','1.130.0','app.zip'))
    api.files[0].update(file_name='app.zip',size=len(BODY),file_sha256=SHA)
    class Raw(io.BytesIO):
        def read(self,n=-1,decode_content=False):
            value=super().read(n)
            if state.mode=='cancel' and value: state.runner.cancel(state.job)
            return value
    class Adapter:
        def __init__(self,old): self.old=old
        def close(self): self.old.close()
        def send(self,request,**options):
            state.log.append(request.url); parts=urlsplit(request.url)
            assert parts.hostname in ('gitlab.com','api.github.com','codeberg.org')
            if parts.hostname!='gitlab.com': return self.old.send(request,**options)
            if '/packages/generic/' not in parts.path: return api.send(request,options)
            response=g.requests.Response(); response.status_code=302 if state.mode=='redirect' else 429 if state.mode=='rate' else 200
            response.headers.update({'Content-Type':'application/octet-stream','Content-Length':str(len(BODY)),'x-checksum-sha256':SHA})
            if state.mode=='redirect': response.headers['Location']='https://gitlab.com/unsafe'
            if state.mode=='rate': response.headers['Retry-After']='17'
            response.raw=Raw(b'x'*len(BODY) if state.mode=='hash' else BODY); return response
    old=g.DirectSession
    class Session(old):
        def __init__(self):
            super().__init__(); self.mount('https://',Adapter(self.adapters['https://']))
    monkeypatch.setattr(g,'DirectSession',Session); monkeypatch.setattr(resolve,'DirectSession',Session)
    return state

def scan(rig):
    with gl.GitLabReleaseClient() as client: return client.discover_releases(TARGET)
def enqueue(rig,queue=None):
    source=scan(rig); row=select_releases(source).releases[0]; a=row.matches[0].asset
    return (queue or rig.store).enqueue(source.repository,row.release,a,str(rig.root))
def run(rig,queue=None):
    with QueueRunner(queue or rig.store,clock=lambda:100) as runner:
        rig.runner=runner; return runner.run_pending()
def index_path(rig): return rig.root/'gitlab.com'/'Owner'/'nested'/'Repo'/naming.INDEX_FILENAME
def bytes_requests(rig): return [u for u in rig.log if '/packages/generic/' in u]
def no_published(rig):
    assert not any(list(rig.root.rglob(p)) for p in ('*.zip','*.part','archive-index.json'))

def test_pipeline(rig):
    job=enqueue(rig).job; assert run(rig).completed==1
    job=rig.store.get(job.job_id); index=m.loads_index(index_path(rig).read_text()); rec=index.releases[0]
    folder=index_path(rig).parent/rec.folder; doc=m.loads_release_manifest((folder/'release.json').read_text())
    assert (folder/'app.zip').read_bytes()==BODY and job.integrity=='PUBLISHER_VERIFIED'
    assert index.repository.project_id==34675721 and rec.release_id==doc.release_id=='v10'
    for path in (index_path(rig),folder/'release.json'):
        data=json.loads(path.read_text()); assert data['schema_version']==3 and data['repository']['project_id']==34675721
    m.check_consistent(index,doc)
    queue=QueueStore(rig.path,clock=lambda:100); assert enqueue(rig,queue).outcome=='requeued'; rig.log.clear()
    assert run(rig,queue).completed==1 and queue.get(job.job_id).result_status=='VERIFIED_EXISTING' and not bytes_requests(rig)
    (folder/'app.zip').write_bytes(b'x'*len(BODY)); enqueue(rig,queue); rig.log.clear()
    assert run(rig,queue).failed==1 and queue.get(job.job_id).result_status=='MODIFIED' and not bytes_requests(rig)
    assert {urlsplit(u).hostname for u in rig.log}=={'gitlab.com'}

@pytest.mark.parametrize('mode,code',[('file','asset_changed'),('hash','hash_mismatch'),('redirect','unsafe_redirect'),('rate','rate_limited'),('cancel',None)])
def test_failure_boundaries(rig,mode,code):
    job=enqueue(rig).job; rig.job=job.job_id
    if mode=='file': rig.api.files[0]['id']+=1
    rig.mode=mode; rig.log.clear(); summary=run(rig); job=rig.store.get(job.job_id)
    assert job.last_error_code==code
    if mode=='rate': assert job.state==S.RETRY_WAIT and rig.store.cooldown_until()==117
    elif mode=='cancel': assert job.state==S.CANCELLED
    else: assert summary.failed==1
    no_published(rig)
    if mode=='file': assert not bytes_requests(rig)
    assert {urlsplit(u).hostname for u in rig.log}=={'gitlab.com'}

def test_squatting(rig):
    enqueue(rig); assert run(rig).completed==1; before=snapshot(rig.root)
    rig.api.project['id']=6; rig.api.releases[0]['assets']['links'][0]['url']=gl.build_package_url(6,'glab','1.130.0','app.zip')
    job=enqueue(rig).job; rig.log.clear(); assert run(rig).failed==1
    assert rig.store.get(job.job_id).last_error_code=='repository_mismatch' and snapshot(rig.root)==before and not bytes_requests(rig)

def test_interrupted_commit(rig,monkeypatch):
    original=store.commit_index; calls=[]
    def commit(*a,**k):
        calls.append(1)
        if len(calls)==1: raise store.ArchiveError('disk_full')
        return original(*a,**k)
    monkeypatch.setattr(store,'commit_index',commit)
    job=enqueue(rig).job; assert run(rig).retry_wait==1 and not index_path(rig).exists()
    source=scan(rig); release=source.releases[0]
    result=d.archive_asset(source.repository,release,release.assets[0],str(rig.root))
    assert result.change=='MANIFEST_RECOVERY' and result.status.value=='ALREADY_ARCHIVED'
    assert index_path(rig).exists() and not list(rig.root.rglob('*.part'))

def test_coexistence(rig):
    jobs=[]
    for repo in (GH,CB):
        catalog=legacy_scan(None,repo); row=select_releases(catalog).releases[0]
        jobs.append(rig.store.enqueue(repo,row.release,row.matches[0].asset,str(rig.root)).job)
    jobs.append(enqueue(rig).job); assert len({j.identity for j in jobs})==3 and run(rig).completed==3
    old=[]
    for repo,version in ((GH,1),(CB,2)):
        base=rig.root.joinpath(*naming.project_components(repo)); raw=(base/naming.INDEX_FILENAME).read_text()
        index=m.loads_index(raw); assert json.loads(raw)['schema_version']==version
        with store.winfs.ArchiveRoot.open(str(rig.root)) as root:
            with store.ensure_project(root,repo) as project: assert not store.load_project_state(project).issues
        old.append((base,snapshot(base)))
    enqueue(rig); assert run(rig).completed==1
    assert all(snapshot(base)==before for base,before in old)

def test_v2_restore(rig,tmp_path):
    path=tmp_path/'old.db'
    with sqlite3.connect(path) as c:
        legacy_queue(c); c.execute("DELETE FROM jobs WHERE state!='QUEUED'")
        columns=','.join(r[1] for r in c.execute('PRAGMA table_info(jobs)'))
        c.execute('ALTER TABLE jobs RENAME TO old_jobs'); from covearchive.queue.store import _SCHEMA
        c.execute(_SCHEMA); c.execute('INSERT INTO jobs ('+columns+",host) SELECT "+columns+",'github.com' FROM old_jobs")
        c.execute('DROP TABLE old_jobs'); c.execute('PRAGMA user_version=2')
    before=list(rig.log); queue=QueueStore(path)
    assert len(queue.list_jobs())==1 and queue.list_jobs()[0].state==S.QUEUED and rig.log==before
    with sqlite3.connect(path) as c: assert c.execute('PRAGMA user_version').fetchone()[0]==3


def test_empty_index_squatting(rig):
    repo=scan(rig).repository
    with store.winfs.ArchiveRoot.open(str(rig.root)) as root:
        with store.ensure_project(root,repo) as project:
            with store.project_writer(project): store.commit_index(project,m.ProjectIndex(repo,()),None)
            before=index_path(rig).read_bytes()
            with pytest.raises(d.DownloadError,match='repository_mismatch'): d._state(project,replace(repo,project_id=6),'v10')
            assert index_path(rig).read_bytes()==before


@pytest.mark.parametrize("indexed",[False,True])
def test_document_project_squatting(rig,indexed):
    from covearchive.archive import plan
    source=scan(rig); live=replace(source.repository,project_id=6); saved=replace(source.repository,project_id=5)
    release=source.releases[0]; planned=plan.plan_archive([release],archive_root=str(rig.root),repository=saved)
    rec=planned.index.releases[0]; doc=m.build_release_manifest(saved,release,rec)
    with store.winfs.ArchiveRoot.open(str(rig.root)) as root:
        with store.ensure_project(root,live) as project:
            with store.project_writer(project):
                with store.ensure_release(project,rec.folder,release.id) as directory: store.commit_release(directory,doc)
                if indexed: index_path(rig).write_text(m.dumps_index(replace(planned.index,repository=live)),encoding="utf-8")
            before=snapshot(rig.root)
            with pytest.raises(d.DownloadError,match="repository_mismatch"): d._state(project,live,release.id,rec.folder)
            assert snapshot(rig.root)==before
    result=d.archive_asset(live,release,release.assets[0],str(rig.root))
    assert result.code=="repository_mismatch" and snapshot(rig.root)==before
    assert not list(rig.root.rglob("*.zip")) and not list(rig.root.rglob("*.part"))


def test_long_project_casing_reuses_archive(rig,monkeypatch):
    # Isolate casing from the long workspace-root path budget.
    monkeypatch.setattr(naming,'MAX_PATH_BUDGET',512)
    name='a'*121+'.abcdefghijklmnop'; repo=RepositoryTarget('o',name,'gitlab','gitlab.com',5)
    rig.api.project.update(id=5,path_with_namespace='o/'+name)
    rig.api.releases[0]['assets']['links'][0]['url']=gl.build_package_url(5,'glab','1.130.0','app.zip')
    with gl.GitLabReleaseClient() as client: source=client.discover_releases(repo)
    release=source.releases[0]; asset=release.assets[0]
    first=d.archive_asset(source.repository,release,asset,str(rig.root)); assert first.status.value=='ARCHIVED'
    before=snapshot(rig.root); rig.log.clear(); upper=replace(repo,repo=name.upper())
    again=d.archive_asset(upper,release,asset,str(rig.root))
    assert again.status.value=='VERIFIED_EXISTING' and snapshot(rig.root)==before and not bytes_requests(rig)
    changed=d.archive_asset(replace(upper,project_id=6),release,asset,str(rig.root))
    assert changed.code=='repository_mismatch' and snapshot(rig.root)==before and not bytes_requests(rig)
