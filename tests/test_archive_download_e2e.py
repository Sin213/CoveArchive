"""Real NTFS, streaming, hostile hops and hard process-exit recovery."""
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import traceback
import threading
import time
from contextlib import contextmanager
import http.server

import pytest
from covearchive.archive import download as d, manifest as m, store, winfs
from covearchive.github.client import DirectSession
from covearchive.transfer import resolve as r
from test_transfer_resolve import fixture_server, asset, BODY, TARGET, SECRET, ScriptAdapter
from test_archive_download import release, project, folder, no_temps

DATA=bytes(range(256))*1024
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def rig(tmp_path):
    with fixture_server() as server:
        server.routes["/cdn"]=(200,{"Content-Length":str(len(DATA))},DATA)
        yield tmp_path,server

def selected(publisher=False):
    return replace(asset(len(DATA)),sha256=hashlib.sha256(DATA).hexdigest() if publisher else None)

def archive(rig, publisher=False, **options):
    root,server=rig; item=selected(publisher)
    return d.archive_asset(TARGET,release(item),item,str(root),policy=r.TransferPolicy.for_loopback_test(server.origin),**options)

def private(text, server):
    assert not any(marker in text for marker in ("sig=","se=","sp=",SECRET,server.origin,"/cdn?"+SECRET))

def consistent(root):
    index=m.loads_index((project(root)/"archive-index.json").read_text())
    manifest=m.loads_release_manifest((folder(root)/"release.json").read_text())
    m.check_consistent(index,manifest)
    assert len(index.releases)==1 and len(manifest.assets)==1
    assert {p.name for p in folder(root).iterdir()}=={"app.zip","release.json"}
    assert (folder(root)/"app.zip").read_bytes()==DATA
    no_temps(root)

CRASH=r"""
import sys,os,hashlib
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from covearchive.archive import store,winfs
from test_archive_download_e2e import DATA,selected,release,TARGET,r,d
root,origin,point,publisher=sys.argv[3:]
item=selected(publisher=='True')
write,rename=winfs.Handle.write,winfs.Handle.rename
if point=='mid':
    def crash_write(handle,data):
        result=write(handle,data)
        if handle.name.endswith('.part'): os._exit(71)
        return result
    winfs.Handle.write=crash_write
elif point in ('before','after'):
    def crash_rename(handle,directory,name,**kwargs):
        if name=='app.zip':
            assert handle.info().size==len(DATA)
            if point=='before': os._exit(71)
            rename(handle,directory,name,**kwargs); os._exit(71)
        return rename(handle,directory,name,**kwargs)
    winfs.Handle.rename=crash_rename
else:
    store.commit_index=lambda *a,**k: os._exit(71)
d.archive_asset(TARGET,release(item),item,root,policy=r.TransferPolicy.for_loopback_test(origin))
raise AssertionError('crash hook was not reached')
"""

@pytest.mark.parametrize("point",["mid","before","after","index"])
@pytest.mark.parametrize("publisher",[False,True])
def test_D18_D20_hard_exit_restart(rig,point,publisher):
    root,server=rig
    child=subprocess.Popen([sys.executable,"-I","-B","-c",CRASH,str(ROOT/"src"),str(ROOT/"tests"),
        str(root),server.origin,point,str(publisher)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        stdout,stderr=child.communicate(timeout=20)
        assert child.returncode==71,stdout+stderr
    finally:
        if child.poll() is None: child.kill(); child.wait(timeout=5)
        assert child.poll() is not None
    path=folder(root)/"app.zip"
    published=path.exists(); identity=path.stat().st_ino if published else None
    assert published==(point in ("after","index"))
    assert not (project(root)/"archive-index.json").exists()
    orphans=list(root.rglob("*.part"))
    if point in ("mid","before"):
        assert len(orphans)==1
        if point=="before": assert hashlib.sha256(orphans[0].read_bytes()).digest()==hashlib.sha256(DATA).digest()
        orphans[0].write_bytes(b"poisoned orphan must never be published")
    if point=="index": assert (folder(root)/"release.json").exists()
    server.requests.clear(); result=archive(rig,publisher)
    assert result.status.value==("ALREADY_ARCHIVED" if published else "ARCHIVED")
    if point=="index": assert result.change=="MANIFEST_RECOVERY"
    if published: assert path.stat().st_ino==identity
    assert len(server.requests)==(0 if publisher and published else 2)
    consistent(root)
    assert archive(rig,publisher).status.value=="VERIFIED_EXISTING"

@pytest.mark.parametrize("position",["owner","repo","release","destination"])
def test_D23_junction_fails_closed(rig,position):
    root,server=rig; outside=root/"untouched"; outside.mkdir(); (outside/"sentinel").write_bytes(b"safe")
    link={"owner":root/"Owner","repo":project(root),"release":folder(root),"destination":folder(root)/"app.zip"}[position]
    link.parent.mkdir(parents=True,exist_ok=True)
    made=subprocess.run(["cmd","/c","mklink","/J",str(link),str(outside)],capture_output=True,text=True,timeout=15)
    assert made.returncode==0,made.stdout+made.stderr
    try:
        result=archive(rig)
        assert result.status.value=="FAILED" and result.code=="unsafe_path"
        assert {p.name:p.read_bytes() for p in outside.iterdir()}=={"sentinel":b"safe"}
        no_temps(root)
    finally: link.rmdir()

@pytest.mark.parametrize("when",["before","mid","after"])
def test_D24_cancellation_real_stream(rig,monkeypatch,when):
    root,server=rig; cancelled=[when=="before"]; written=[]
    original_write,original_rename=winfs.Handle.write,winfs.Handle.rename
    def write(handle,data):
        result=original_write(handle,data)
        if handle.name.endswith(".part"):
            written.append(len(data))
            if when=="mid": cancelled[0]=True
        return result
    def rename(handle,directory,name,**options):
        result=original_rename(handle,directory,name,**options)
        if name=="app.zip" and when=="after": cancelled[0]=True
        return result
    monkeypatch.setattr(winfs.Handle,"write",write); monkeypatch.setattr(winfs.Handle,"rename",rename)
    result=archive(rig,cancel=lambda:cancelled[0])
    assert result.status.value==("ARCHIVED" if when=="after" else "CANCELLED")
    if when=="after": consistent(root)
    else:
        assert not (folder(root)/"app.zip").exists() and not (project(root)/"archive-index.json").exists()
        no_temps(root)
        if when=="before": assert not server.requests and not written
        else: assert len(written)==1 and 0<written[0]<=65536

@pytest.mark.parametrize("status,headers,body,code",[(302,{},b"","unsafe_redirect"),
    (304,{},b"","unexpected_status"),(200,{"Content-Encoding":"gzip"},DATA,"unexpected_encoding"),
    (200,{"Content-Length":"bogus"},DATA,"size_mismatch"),(206,{},DATA,"unexpected_status"),
    (200,{},DATA[:-1],"partial_transfer"),(200,{"Content-Type":"text/html"},DATA,"unexpected_content")],ids=["location","status304","encoding","length","partial206","truncated","html"])
def test_D26_bad_responses_no_publication(rig,status,headers,body,code,caplog):
    root,server=rig; server.routes["/cdn"]=(status,headers,body)
    result=archive(rig)
    assert result.code==code and not (folder(root)/"app.zip").exists()
    assert not (project(root)/"archive-index.json").exists(); no_temps(root)
    private(repr(result)+caplog.text,server)

def test_D27_D28_redirect_changes_between_runs(rig):
    root,server=rig; assert archive(rig).status.value=="ARCHIVED"
    (folder(root)/"app.zip").unlink(); server.requests.clear()
    server.routes["/cdn"]=(302,{"Location":"http://127.0.0.2:1/?"+SECRET},b"")
    result=archive(rig)
    assert result.code=="unsafe_redirect" and len(server.requests)==2
    assert not (folder(root)/"app.zip").exists(); no_temps(root)

def test_D29_D30_multichunk_expiry_proxy_and_privacy(rig,monkeypatch,caplog):
    root,server=rig; calls=[]
    caplog.set_level(logging.DEBUG)
    def expired(path):
        calls.append(path)
        return (403,{},b"") if len(calls)==1 else (200,{},DATA)
    server.routes["/cdn"]=expired
    for key in ("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","NO_PROXY"):
        monkeypatch.setenv(key,"http://127.0.0.1:1")
    result=archive(rig,True)
    assert result.status.value=="ARCHIVED" and len(server.requests)==4
    assert result.local_sha256==hashlib.sha256((folder(root)/"app.zip").read_bytes()).hexdigest()
    assert result.integrity=="PUBLISHER_VERIFIED"; consistent(root)
    text=repr(result)+caplog.text+(folder(root)/"release.json").read_text()+(project(root)/"archive-index.json").read_text()
    private(text,server)
    session=DirectSession(); adapter=ScriptAdapter([(302,{"Location":"/cdn#"+SECRET},b"")]); session.mount("http://",adapter)
    with pytest.raises(r.DownloadError) as caught:
        with r.open_asset_stream(TARGET,selected(),policy=r.TransferPolicy.for_loopback_test(server.origin),session=session):
            pytest.fail("entered malformed redirect")
    private("".join(traceback.format_exception(caught.value))+repr(caught.value),server)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("status,headers",[(200,{"Content-Type":"text/html"}),(302,{}),(300,{"Location":"/cdn"})])
def test_D26_malformed_api_response(rig,status,headers):
    root,server=rig
    server.routes["/repos/Owner/Repo/releases/assets/7"]=(status,headers,b"bad API response")
    result=archive(rig)
    assert result.status.value=="FAILED" and len(server.requests)==1
    assert not (folder(root)/"app.zip").exists() and not (project(root)/"archive-index.json").exists()
    no_temps(root)


@pytest.mark.parametrize("location",["https://[sig=private&se=secret&sp=read]/","https://[broken/","https://api.github.com:bad/a","https://api.github.com:99999/a","https://\u00e9.invalid/a"])
def test_repair_C3_malformed_redirects_typed_private(rig,location,caplog):
    root,server=rig
    for public in ("stream","archive"):
        session=DirectSession(); adapter=ScriptAdapter([(302,{"Location":location},b"")]); session.mount("http://",adapter)
        if public=="stream":
            with pytest.raises(r.DownloadError) as caught:
                with r.open_asset_stream(TARGET,selected(),policy=r.TransferPolicy.for_loopback_test(server.origin),session=session): pytest.fail("entered")
            assert caught.value.code=="unsafe_redirect"
            private("".join(traceback.format_exception(caught.value))+caplog.text,server)
        else:
            result=archive(rig,session=session)
            assert result.code=="unsafe_redirect"; private(repr(result)+caplog.text,server)
        assert adapter.closed and not (folder(root)/"app.zip").exists(); no_temps(root)

@contextmanager
def trickle_server(headers_delay=0):
    stop=threading.Event()
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            if self.path.startswith('/repos/'):
                self.send_response(302); self.send_header('Location','/cdn'); self.end_headers(); return
            if stop.wait(headers_delay): return
            self.send_response(200); self.send_header('Content-Length','20'); self.end_headers()
            for _ in range(20):
                try: self.wfile.write(b'x'); self.wfile.flush()
                except (ConnectionError,OSError): break
                if stop.wait(.15): break
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler); server.daemon_threads=False
    thread=threading.Thread(target=server.serve_forever); thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}'
    finally:
        stop.set(); server.shutdown(); server.server_close(); thread.join(3); assert not thread.is_alive()

@pytest.mark.parametrize("cancelled",[False,True])
def test_repair_C4_trickle_returns_control_promptly(tmp_path,cancelled):
    with trickle_server() as origin:
        flag=threading.Event(); timer=threading.Timer(.25,flag.set)
        timer.start(); started=time.monotonic(); item=asset(20)
        try:
            result=d.archive_asset(TARGET,release(item),item,str(tmp_path),policy=r.TransferPolicy.for_loopback_test(origin,deadline_seconds=.35,timeout=(1,1)),cancel=flag.is_set if cancelled else None)
            elapsed=time.monotonic()-started
            assert elapsed<1.5 and result.code==('cancelled' if cancelled else 'timeout')
            assert not (folder(tmp_path)/'app.zip').exists() and not (project(tmp_path)/'archive-index.json').exists(); no_temps(tmp_path)
        finally: timer.cancel(); timer.join(2); assert not timer.is_alive()


def test_repair_C4_header_timeout_does_not_sleep_past_deadline(tmp_path):
    with trickle_server(headers_delay=1) as origin:
        started=time.monotonic(); item=asset(20)
        result=d.archive_asset(TARGET,release(item),item,str(tmp_path),policy=r.TransferPolicy.for_loopback_test(origin,deadline_seconds=.2,timeout=(1,1)))
        assert result.code=='timeout' and time.monotonic()-started<.8
        no_temps(tmp_path)


@pytest.mark.parametrize('case',['success','length','gzip','oversize','truncated','html','range','206'])
def test_postfinal_F3_direct_api_binary_checks(rig,case):
    root,server=rig; headers={'Content-Type':'application/octet-stream'}; body=DATA; status=200
    if case=='length': headers['Content-Length']='1'
    if case=='gzip': headers['Content-Encoding']='gzip'
    if case=='oversize': body+=b'x'
    if case=='truncated': headers['Content-Length']=str(len(DATA)); body=DATA[:-1]
    if case=='html': headers['Content-Type']='text/html'
    if case=='range': headers['Content-Range']='bytes 0-1/2'
    if case=='206': status=206
    server.routes['/repos/Owner/Repo/releases/assets/7']=(status,headers,body)
    result=archive(rig)
    expected={'success':None,'length':'size_mismatch','gzip':'unexpected_encoding','oversize':'size_mismatch','truncated':'partial_transfer','html':'unexpected_content','range':'unexpected_status','206':'unexpected_status'}[case]
    assert result.code==expected
    if case=='success': consistent(root); assert result.local_sha256==hashlib.sha256(DATA).hexdigest()
    else: assert not (folder(root)/'app.zip').exists() and not (project(root)/'archive-index.json').exists()
    no_temps(root)

def test_postfinal_F4_real_download_without_read1(rig,monkeypatch):
    response_type=r.requests.packages.urllib3.response.HTTPResponse
    monkeypatch.delattr(response_type,'read1')
    result=archive(rig)
    assert result.status.value=='ARCHIVED'; consistent(rig[0])

@pytest.mark.parametrize('method',['read1','read'])
@pytest.mark.parametrize('error',[AttributeError,TypeError])
def test_postfinal_F4_body_version_failures_are_private(rig,monkeypatch,error,method):
    response_type=r.requests.packages.urllib3.response.HTTPResponse
    def broken(*args,**kwargs): raise error('sig=private&se=secret&sp=read')
    if method=='read': monkeypatch.setattr(response_type,'read1',None)
    monkeypatch.setattr(response_type,method,broken)
    result=archive(rig)
    assert result.status.value=='FAILED' and result.code=='partial_transfer'
    private(repr(result),rig[1]); no_temps(rig[0])
    assert not (folder(rig[0])/'app.zip').exists()


RETRY_MATRIX=r"""
import sys,os,json
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from test_archive_download_e2e import d,r,store,winfs,selected,release,TARGET
from dataclasses import replace
root,origin,context,point,kind=sys.argv[3:]
flag=[False]; item=selected()
if context=='indexed': item=replace(item,id=8,name='second.zip')
source=release(assets=(selected(),item)) if context=='indexed' else release(item)
def stop():
    if kind=='crash': os._exit(72)
    if kind=='cancel': flag[0]=True; return
    raise winfs.ArchiveError('os')
ensure,write,rename,commit,commit_index=store.ensure_release,winfs.Handle.write,winfs.Handle.rename,store.commit_release,store.commit_index
if point=='ensure':
    def ensured(*args):
        directory=ensure(*args); stop(); return directory
    store.ensure_release=ensured
elif point=='download':
    if kind=='cancel':
        count=[0]
        def written(handle,data):
            actual=write(handle,data); count[0]+=actual
            if count[0]>=item.size: stop()
            return actual
        winfs.Handle.write=written
    else:
        def renamed(handle,directory,name,**options):
            if name==item.name: stop()
            return rename(handle,directory,name,**options)
        winfs.Handle.rename=renamed
elif point=='publish':
    def committing(*args):
        if os.path.isfile(os.path.join(root,'Owner','Repo',args[1].folder,item.name)): stop()
        return commit(*args)
    store.commit_release=committing
else:
    def indexing(*args): stop(); return commit_index(*args)
    store.commit_index=indexing
result=d.archive_asset(TARGET,source,item,root,policy=r.TransferPolicy.for_loopback_test(origin),cancel=lambda:flag[0])
print(json.dumps({'status':result.status.value,'code':result.code}))
"""

@pytest.mark.parametrize('context',['new','indexed','restore'])
@pytest.mark.parametrize('point',['ensure','download','publish','index'])
@pytest.mark.parametrize('kind',['failure','cancel','crash'])
def test_pf2_retry_matrix(rig,context,point,kind):
    root,server=rig; item=selected(); sibling=None
    if context!='new':
        archive(rig)
        if context=='restore':
            for path in folder(root).iterdir(): path.unlink()
            folder(root).rmdir()
        else:
            sibling=(folder(root)/'app.zip').stat().st_ino
            item=replace(item,id=8,name='second.zip')
            server.routes['/repos/Owner/Repo/releases/assets/8']=(302,{'Location':'/cdn'},b'')
    source=release(assets=(selected(),item)) if context=='indexed' else release(item)
    command=[sys.executable,'-I','-B','-c',RETRY_MATRIX,str(ROOT/'src'),str(ROOT/'tests'),str(root),server.origin,context,point,kind]
    child=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        stdout,stderr=child.communicate(timeout=20)
        assert child.returncode==(72 if kind=='crash' else 0),stdout+stderr
        if kind!='crash':
            status=json.loads(stdout)['status']
            assert status==('ARCHIVED' if kind=='cancel' and point in ('publish','index') else 'CANCELLED' if kind=='cancel' else 'FAILED')
    finally:
        if child.poll() is None: child.kill(); child.wait(5)
        assert child.poll() is not None
    path=folder(root)/item.name; existed=path.exists(); identity=path.stat().st_ino if existed else None
    for orphan in root.rglob('*.part'): orphan.write_bytes(b'never publish stale bytes')
    server.requests.clear()
    result=d.archive_asset(TARGET,source,item,str(root),policy=r.TransferPolicy.for_loopback_test(server.origin))
    assert result.status.value in ('ARCHIVED','ALREADY_ARCHIVED','VERIFIED_EXISTING'),result
    assert path.read_bytes()==DATA
    if existed: assert path.stat().st_ino==identity
    if sibling is not None: assert (folder(root)/'app.zip').stat().st_ino==sibling
    verified_evidence=existed and (context=='restore' or kind=='cancel' and point in ('publish','index'))
    assert len(server.requests)==(0 if verified_evidence else 2)
    index=m.loads_index((project(root)/'archive-index.json').read_text()); manifest=m.loads_release_manifest((folder(root)/'release.json').read_text())
    m.check_consistent(index,manifest)
    assert {a.asset_id for a in manifest.assets}==({7,8} if context=='indexed' else {7})
    assert {p.name for p in folder(root).iterdir()}==({'app.zip','second.zip','release.json'} if context=='indexed' else {'app.zip','release.json'})
    no_temps(root)
    server.requests.clear()
    assert d.archive_asset(TARGET,source,item,str(root),policy=r.TransferPolicy.for_loopback_test(server.origin)).status.value=='VERIFIED_EXISTING'
    assert not server.requests


@pytest.mark.parametrize('api',['stream','archive'])
@pytest.mark.parametrize('mode',['bracket','port','unicode','tls','timeout','connection','request','read','identity'])
def test_pf3_typed_errors_have_no_hidden_context(rig,monkeypatch,caplog,api,mode):
    import requests
    root,server=rig; seen=[]; init=r.DownloadError.__init__; session=DirectSession()
    marker=server.origin+'/?'+SECRET
    def capture(self,*args,**kwargs): init(self,*args,**kwargs); seen.append(self)
    monkeypatch.setattr(r.DownloadError,'__init__',capture)
    locations={'bracket':'https://[sig=private&se=secret&sp=read]/','port':'https://objects.githubusercontent.com:bad/?'+SECRET,'unicode':'https://?.invalid/?'+SECRET}
    errors={'tls':requests.exceptions.SSLError,'timeout':requests.Timeout,'connection':requests.ConnectionError,'request':requests.RequestException}
    script=[(302,{'Location':locations[mode]},b'')] if mode in locations else (
        [errors[mode](marker)]*3 if mode in errors else [(200,{},DATA)])
    adapter=ScriptAdapter(script); session.mount('http://',adapter)
    if mode=='read':
        class Raw:
            def read(self,*a,**k): raise AttributeError(marker)
            def close(self): pass
        send=adapter.send
        def broken(*a,**k):
            response=send(*a,**k); response.raw=Raw(); return response
        monkeypatch.setattr(adapter,'send',broken)
    target=replace(TARGET,repo='[sig=private&se=secret&sp=read]') if mode=='identity' else TARGET
    options=dict(policy=r.TransferPolicy.for_loopback_test(server.origin),session=session,sleep=lambda _:None)
    if api=='stream':
        with pytest.raises(r.DownloadError) as caught:
            with r.open_asset_stream(target,selected(),**options) as chunks: b''.join(chunks)
        assert caught.value in seen
    else:
        result=d.archive_asset(target,release(selected()),selected(),str(root),**options)
        assert result.status.value=='FAILED'; private(repr(result),server); no_temps(root)
        assert not list(root.rglob('release.json'))
    assert seen
    for error in seen:
        assert error.__cause__ is None and error.__context__ is None
        pending=[error]; visited=set()
        while pending:
            link=pending.pop()
            if id(link) in visited: continue
            visited.add(id(link)); private(str(link)+repr(link),server)
            pending.extend(e for e in (link.__cause__,link.__context__) if e is not None)
    private(caplog.text,server)


PF4_CHILD = r"""
import sys,os,json
sys.path[:0]=sys.argv[1:3]
from test_archive_download_e2e import selected,release,d,r,store,winfs,TARGET
from dataclasses import replace
root,origin,selection,point,kind=sys.argv[3:]
a=selected(); b=replace(a,id=8,name='second.zip'); item=b if selection=='new' else a
flag=[False]
def stop():
    if kind=='crash': os._exit(73)
    if kind=='cancel': flag[0]=True; return
    raise winfs.ArchiveError('os')
ensure,commit,index=store.ensure_release,store.commit_release,store.commit_index
if point=='ensure':
    def ensured(*args):
        directory=ensure(*args); stop(); return directory
    store.ensure_release=ensured
elif point=='publish':
    def committing(directory,document):
        if any(x.asset_id==item.id and x.status=='completed' for x in document.assets): stop()
        return commit(directory,document)
    store.commit_release=committing
else:
    def indexing(*args): stop(); return index(*args)
    store.commit_index=indexing
result=d.archive_asset(TARGET,release(assets=(a,b)),item,root,policy=r.TransferPolicy.for_loopback_test(origin),cancel=lambda:flag[0])
print(json.dumps({'status':result.status.value,'code':result.code}))
"""

@pytest.mark.parametrize('state',['missing','ahead','unindexed'])
@pytest.mark.parametrize('selection',['new','existing'])
@pytest.mark.parametrize('point',['ensure','publish','index'])
@pytest.mark.parametrize('retry',['same','sibling'])
def test_pf4_recovery_matrix(rig,state,selection,point,retry):
    _pf4_matrix(rig,state,selection,point,retry,'crash')

@pytest.mark.parametrize('point',['ensure','publish','index'])
@pytest.mark.parametrize('retry',['same','sibling'])
@pytest.mark.parametrize('kind',['failure','cancel'])
def test_pf4_missing_new_failure_cancel(rig,point,retry,kind):
    _pf4_matrix(rig,'missing','new',point,retry,kind)

def _pf4_matrix(rig,state,selection,point,retry,kind):
    root,server=rig; a=selected(); b=replace(a,id=8,name='second.zip'); c=replace(a,id=9,name='third.zip')
    source=release(assets=(a,b,c)); policy=r.TransferPolicy.for_loopback_test(server.origin)
    def run(item): return d.archive_asset(TARGET,source,item,str(root),policy=policy)
    for item in (b,c): server.routes[f'/repos/Owner/Repo/releases/assets/{item.id}']=(302,{'Location':'/cdn'},b'')
    assert run(a).status.value=='ARCHIVED'
    index=project(root)/'archive-index.json'; manifest=folder(root)/'release.json'
    if state=='missing': manifest.unlink()
    elif state=='unindexed': index.unlink()
    else:
        assert run(c).status.value=='ARCHIVED'
        data=json.loads(index.read_text()); data['releases'][0]['assets']=[x for x in data['releases'][0]['assets'] if x['asset_id']!=9]
        index.write_text(json.dumps(data))
    a_identity=(folder(root)/a.name).stat().st_ino
    command=[sys.executable,'-I','-B','-c',PF4_CHILD,str(ROOT/'src'),str(ROOT/'tests'),str(root),server.origin,selection,point,kind]
    child=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        stdout,stderr=child.communicate(timeout=20)
        assert child.returncode==(73 if kind=='crash' else 0),stdout+stderr
        if kind!='crash': assert json.loads(stdout)['status']==('FAILED' if kind=='failure' else 'CANCELLED' if point=='ensure' else 'ARCHIVED')
    finally:
        if child.poll() is None: child.kill(); child.wait(5)
        assert child.poll() is not None
    before={p.name:p.stat().st_ino for p in folder(root).glob('*.zip')}
    item=b if selection=='new' else a; target=item if retry=='same' else a if selection=='new' else b
    server.requests.clear(); result=run(target)
    assert result.status.value in ('ARCHIVED','ALREADY_ARCHIVED','VERIFIED_EXISTING'),result
    if state in ('missing','ahead') and target==a: assert not server.requests
    m.check_consistent(m.loads_index(index.read_text()),m.loads_release_manifest(manifest.read_text()))
    # Retry each identity again: claims must be re-established without losing siblings.
    assert run(item).status.value in ('ARCHIVED','ALREADY_ARCHIVED','VERIFIED_EXISTING')
    assert run(a).status.value in ('ALREADY_ARCHIVED','VERIFIED_EXISTING')
    docs=m.loads_index(index.read_text()),m.loads_release_manifest(manifest.read_text()); m.check_consistent(*docs)
    expected={7,8} if selection=='new' or target==b else {7}
    if state=='ahead': expected.add(9)
    assert {x.asset_id for x in docs[1].assets}==expected
    assert (folder(root)/a.name).stat().st_ino==a_identity
    for name,identity in before.items(): assert (folder(root)/name).stat().st_ino==identity and (folder(root)/name).read_bytes()==DATA
    assert {p.name for p in folder(root).iterdir()}=={x.saved_name for x in docs[1].assets}|{'release.json'}
    no_temps(root)
