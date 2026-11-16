"""GitLab byte-transfer boundary over the existing fake adapter."""
from dataclasses import replace
import io
import pytest
from covearchive.transfer import resolve as r
from covearchive.github.client import DirectSession
from covearchive.github.urls import RepositoryTarget
from test_transfer_resolve import ScriptAdapter, public_dns, asset, BODY, SECRET

REPO=RepositoryTarget("g/sub","p","gitlab","gitlab.com",5)
URL="https://gitlab.com/api/v4/projects/5/packages/generic/pkg/1/app.zip"
def selected(**changes):
    return replace(asset(),browser_download_url=URL,sha256="a"*64,file_id=9,**changes)
class Raw(io.BytesIO):
    def read(self,amount,decode_content=False): return super().read(amount)
class Adapter(ScriptAdapter):
    def send(self,*args,**kw):
        response=super().send(*args,**kw); response.raw=Raw(response.raw.getvalue()); return response
def setup(script):
    session=DirectSession(); adapter=Adapter(script); session.mount("https://",adapter)
    return session,adapter
def consume(session,a=None,**kw):
    with r.open_asset_stream(REPO,a or selected(),session=session,resolver=public_dns,**kw) as chunks:
        return b"".join(chunks)

@pytest.mark.parametrize("status,headers,body,code",[(200,{},BODY,None),(200,{"x-checksum-sha256":"A"*64},BODY,None),
    (200,{"x-checksum-sha256":"b"*64},BODY,"asset_changed"),(200,{"Content-Length":"1"},BODY,"size_mismatch"),
    (200,{},BODY[:-1],"partial_transfer"),(200,{},BODY+b"x","size_mismatch"),(404,{},b"","upstream_removed"),
    (429,{"Retry-After":"17"},b"","rate_limited"),(200,{"Content-Encoding":"gzip"},BODY,"unexpected_encoding"),
    (200,{"Content-Range":"secret"},BODY,"unexpected_status"),(200,{"Content-Type":"text/html"},BODY,"unexpected_content")])
def test_gitlab_transfer(status,headers,body,code):
    session,adapter=setup([(status,headers,body)])
    if code:
        with pytest.raises(r.DownloadError) as caught: consume(session)
        assert caught.value.code==code and SECRET not in str(caught.value) and URL not in str(caught.value)
        if code=="rate_limited": assert caught.value.retry_after_seconds==17
    else: assert consume(session)==BODY
    request=adapter.calls[0][0]
    assert request.url==URL and "X-GitHub-Api-Version" not in request.headers
    assert request.headers["Accept-Encoding"]=="identity" and request.headers["Accept"]=="application/octet-stream"

@pytest.mark.parametrize("status",[301,302,303,304,307,308])
@pytest.mark.parametrize("location",[URL,"https://evil.com/?"+SECRET])
def test_gitlab_redirect(status,location):
    session,adapter=setup([(status,{"Location":location},b"")])
    with pytest.raises(r.DownloadError) as caught: consume(session)
    assert caught.value.code=="unsafe_redirect" and len(adapter.calls)==1

@pytest.mark.parametrize("patch",[dict(browser_download_url=URL.replace("/5/","/6/")),dict(name="other.zip"),dict(sha256=None),dict(file_id=None),dict(file_id=True),dict(state="external"),dict(sha256="bad")])
def test_gitlab_identity(patch):
    session,adapter=setup([])
    with pytest.raises(r.DownloadError) as caught: consume(session,replace(selected(),**patch))
    assert caught.value.code=="invalid_asset_identity" and not adapter.calls

def test_gitlab_retry_cancel_dns():
    session,adapter=setup([(503,{},b""),(200,{},BODY)])
    assert consume(session,sleep=lambda _:None)==BODY and len(adapter.calls)==2
    session,adapter=setup([(200,{},b"x"*10000)]); stop=[False]
    with r.open_asset_stream(REPO,replace(selected(),size=10000),session=session,resolver=public_dns,cancel=lambda:stop[0]) as chunks:
        next(chunks); stop[0]=True
        with pytest.raises(r.DownloadError,match="cancelled"): next(chunks)
    session,adapter=setup([])
    with pytest.raises(r.DownloadError,match="unsafe_redirect"):
        with r.open_asset_stream(REPO,selected(),session=session,resolver=lambda *a:[(2,1,6,"",("127.0.0.1",443))]): pass
    assert not adapter.calls
