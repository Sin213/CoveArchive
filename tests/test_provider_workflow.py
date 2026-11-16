"""Offline provider pipeline: real queues, transfers, manifests and native files."""
from dataclasses import replace
from types import SimpleNamespace as NS
from urllib.parse import urlsplit
import hashlib
import io
import json
import socket
import sqlite3
import threading
import pytest
from covearchive.archive import download as d, manifest as m
from covearchive.github import client as g
from covearchive.github.select import select_releases
from covearchive.github.urls import RepositoryTarget
from covearchive.forgejo import ForgejoReleaseClient
from covearchive.queue import QueueStore, QueueRunner, JobState as S
from covearchive.transfer import resolve
from test_transfer_resolve import ScriptAdapter, public_dns, BODY
from test_forgejo import release as cb_release, asset as cb_asset
from test_github_client import release as gh_release, asset as gh_asset
from test_queue_store import legacy_queue

CB=RepositoryTarget('Owner','Repo','codeberg','codeberg.org'); GH=RepositoryTarget('Owner','Repo')
UUID='bbde8b93-f9d8-4323-a4c6-3fbd119e5fd4'
def snapshot(root): return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
def project(rig,repo=CB): return rig.root.joinpath(*(() if repo==GH else ('codeberg.org',)),'Owner','Repo')
def scan(wire,repo=CB):
    with (g.GitHubReleaseClient() if repo==GH else ForgejoReleaseClient(resolver=public_dns)) as client: return client.discover_releases(repo)
def enqueue(rig,repo=CB,store=None,asset_id=7):
    catalog=scan(rig.wire,repo); matches=select_releases(catalog).releases[0]
    chosen=next(x.asset for x in matches.matches if x.asset.id==asset_id)
    return (store or rig.store).enqueue(catalog.repository,matches.release,chosen,str(rig.root))
def run(store):
    with QueueRunner(store) as runner: return runner.run_pending()

@pytest.fixture
def wire(monkeypatch):
    state=NS(log=[],mode='ok',barrier=None)
    class Raw(io.BytesIO):
        def read(self,n=-1,decode_content=False): return super().read(n)
    class Adapter(ScriptAdapter):
        def send(self,request,**options):
            state.log.append(request.url); parts=urlsplit(request.url); path=parts.path
            assert parts.hostname in ('codeberg.org','api.github.com'), 'unvalidated host reached transport'
            headers={'Content-Type':'application/json'}; status=200
            if path=='/api/v1/repos/Owner/Repo/releases':
                assets=[cb_asset(7,size=len(BODY))]+([cb_asset(8,name='removed.zip',size=len(BODY))] if state.mode=='removed' else [])
                body=[cb_release(10,assets=assets)]; headers.update({'X-Total-Count':'1','Link':f'<https://codeberg.org{path}?page=1&limit=50>; rel="last"'})
            elif path=='/repos/Owner/Repo/releases': body=[gh_release(10,assets=[gh_asset(7,size=len(BODY))])]
            elif path.startswith('/api/v1/repos/Owner/Repo/releases/10/assets/'):
                status=404 if state.mode=='removed' else 200
                body=dict(id=7,size=len(BODY),name='app.zip',type='attachment',uuid=UUID,browser_download_url='https://evil.example/secret')
            elif path in ('/attachments/'+UUID,'/repos/Owner/Repo/releases/assets/7'):
                if state.barrier: state.barrier.wait(5)
                body=BODY; headers={'Content-Type':'application/octet-stream'}
                if state.mode=='hostile': status=302; headers={'Location':'https://evil.example/secret'}
            else: pytest.fail('unexpected network path: '+path)
            body=body if isinstance(body,bytes) else json.dumps(body).encode(); headers['Content-Length']=str(len(body))
            self.script=[(status,headers,body)]; response=super().send(request,**options)
            response.raw=Raw(response.raw.getvalue()); return response
    class Session(g.DirectSession):
        def __init__(self): super().__init__(); self.mount('https://',Adapter([]))
    monkeypatch.setattr(g,'DirectSession',Session); monkeypatch.setattr(resolve,'DirectSession',Session)
    monkeypatch.setattr(d,'open_asset_stream',lambda *a,**kw:resolve.open_asset_stream(*a,**kw,resolver=public_dns))
    def offline(*a,**k): pytest.fail('real network forbidden')
    monkeypatch.setattr(socket.socket,'connect',offline); monkeypatch.setattr(socket,'getaddrinfo',public_dns)
    return state

@pytest.fixture
def rig(tmp_path,wire):
    root=tmp_path/'archive'; root.mkdir(); path=tmp_path/'queue.db'
    return NS(root=root,path=path,store=QueueStore(path),wire=wire)

@pytest.mark.parametrize('repo',[CB,GH])
def test_scan_select_real_clients(wire,repo):
    catalog=scan(wire,repo); selected=select_releases(catalog).releases[0]
    assert catalog.complete and catalog.repository==repo and selected.release.id==10
    assert [(x.asset.id,x.asset.size) for x in selected.matches]==[(7,len(BODY))]

@pytest.mark.parametrize('mode,code',[('removed','upstream_removed'),('hostile','unknown_cdn_host')])
def test_wire_failure_boundary(wire,mode,code):
    wire.mode=mode; release=scan(wire).releases[0]
    with pytest.raises(resolve.DownloadError) as caught:
        with resolve.open_asset_stream(CB,release.assets[0],release=release,resolver=public_dns) as chunks: list(chunks)
    assert caught.value.code==code and UUID not in str(caught.value)
    assert {urlsplit(url).hostname for url in wire.log}=={'codeberg.org'}

def test_codeberg_scan_queue_archive(rig):
    job=enqueue(rig).job; assert run(rig.store).completed==1
    job=rig.store.get(job.job_id); base=project(rig); folder=base/'release-v10--10'
    assert job.state==S.COMPLETED and (job.provider,job.host)==('codeberg','codeberg.org')
    assert (folder/'app.zip').read_bytes()==BODY and job.integrity=='LOCAL_HASH_ONLY'
    for path in (base/'archive-index.json',folder/'release.json'):
        data=json.loads(path.read_text()); assert (data['schema_version'],data['provider'],data['repository']['host'])==(2,'codeberg','codeberg.org')
    m.check_consistent(m.loads_index((base/'archive-index.json').read_text()),m.loads_release_manifest((folder/'release.json').read_text()))

def test_reopen_reenqueue_verifies_real_bytes_without_download(rig):
    enqueue(rig); assert run(rig.store).completed==1
    store=QueueStore(rig.path); assert enqueue(rig,store=store).outcome=='requeued'; rig.wire.log.clear()
    assert run(store).completed==1 and store.list_jobs()[0].result_status=='VERIFIED_EXISTING'
    assert not any('/attachments/' in url for url in rig.wire.log)
    path=project(rig)/'release-v10--10'/'app.zip'; path.write_bytes(b'x'*len(BODY))
    enqueue(rig,store=store); rig.wire.log.clear(); assert run(store).failed==1
    assert store.list_jobs()[0].result_status=='MODIFIED' and not any('/attachments/' in url for url in rig.wire.log)

def test_identical_provider_ids_never_collide_or_serialize(rig):
    first,second=enqueue(rig,GH).job,enqueue(rig).job
    assert first.job_id!=second.job_id and first.project_key!=second.project_key
    rig.wire.barrier=threading.Barrier(2); assert run(rig.store).completed==2; rig.wire.barrier=None
    for repo,provider in ((GH,'github'),(CB,'codeberg')):
        base=project(rig,repo); assert (base/'release-v10--10'/'app.zip').read_bytes()==BODY
        assert json.loads((base/'archive-index.json').read_text())['provider']==provider
    for repo,other in ((GH,CB),(CB,GH)):
        before=snapshot(project(rig,other)); assert enqueue(rig,repo).outcome=='requeued'
        assert run(rig.store).completed==1 and snapshot(project(rig,other))==before

def test_removed_upstream_preserves_existing_archive(rig):
    enqueue(rig); assert run(rig.store).completed==1
    before=snapshot(rig.root); rig.wire.mode='removed'; job=enqueue(rig,asset_id=8).job
    assert run(rig.store).failed==1 and rig.store.get(job.job_id).last_error_code=='upstream_removed'
    assert snapshot(rig.root)==before and not list(rig.root.rglob('*.part'))

def test_hostile_attachment_redirect_never_leaves_codeberg(rig):
    rig.wire.mode='hostile'; job=enqueue(rig).job; assert run(rig.store).failed==1
    assert rig.store.get(job.job_id).last_error_code=='unknown_cdn_host'
    assert {urlsplit(url).hostname for url in rig.wire.log}=={'codeberg.org'}
    assert not any(list(rig.root.rglob(pattern)) for pattern in ('*.zip','*.part','release.json','archive-index.json'))
    assert UUID.encode() not in rig.path.read_bytes() and b'evil.example' not in rig.path.read_bytes()

def test_migrating_restored_queue_never_starts_work(tmp_path,wire):
    path=tmp_path/'v1.db'
    with sqlite3.connect(path) as c:
        legacy_queue(c); c.execute("DELETE FROM jobs WHERE state!='QUEUED'")
    store=QueueStore(path); assert len(store.list_jobs())==1 and store.list_jobs()[0].state==S.QUEUED
    assert not wire.log
    with sqlite3.connect(path) as c: assert c.execute('PRAGMA user_version').fetchone()[0]==2
