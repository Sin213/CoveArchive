"""Trimmed live GitLab shapes over an offline DirectSession adapter."""
import io
import json
import socket
from copy import deepcopy
from dataclasses import replace
from urllib.parse import urlsplit, parse_qs
import pytest
import requests
from covearchive.gitlab import GitLabReleaseClient, parse_package_url, build_package_url
from covearchive.github.client import (DirectSession, InvalidResponseError, IncompleteScanError,
    PaginationError, UnsafeRedirectError, RateLimitError, NotFoundError, GitHubApiError)
from covearchive.github.urls import RepositoryTarget
from test_transfer_resolve import public_dns

ROOT="https://gitlab.com/api/v4/projects/"
TARGET=RepositoryTarget("gitlab-org","cli","gitlab","gitlab.com")
DATE="2026-11-10T13:22:52.624Z"
FILE="glab_1.130.0_windows_amd64.zip"
URL=ROOT+"gitlab-org%2Fcli/packages/generic/glab/1%2E130%2E0/"+FILE
SHA="a"*64


def release(tag="v1.130.0",url=URL):
    return dict(tag_name=tag,name=tag,created_at=DATE,released_at=DATE,upcoming_release=False,
        description="discard",assets=dict(count=2,sources=[dict(format="zip",url="https://ignored")],
        links=[dict(id=14153947,name=FILE,url=url,direct_asset_url="https://ignored",link_type="other")]))


def package(**changes):
    return dict(dict(id=76008474,name="glab",version="1.130.0",package_type="generic",status="default"),**changes)


def file(**changes):
    return dict(dict(id=388296723,package_id=76008474,created_at=DATE,file_name=FILE,size=93,file_sha256=SHA),**changes)


class Wire:
    def __init__(self):
        self.project=dict(id=34675721,path_with_namespace="GitLab-Org/CLI",visibility="public")
        self.releases=[release()]; self.packages=[package()]; self.files=[file()]
        self.calls=[]; self.change=None; self.page_size=100
        self.session=DirectSession(); self.session.mount("https://",self.Adapter(self))
    class Adapter(requests.adapters.BaseAdapter):
        def __init__(self,wire): self.wire=wire
        def send(self,request,**kwargs): return self.wire.send(request,kwargs)
        def close(self): pass
    def send(self,request,options):
        self.calls.append(request)
        assert request.url.startswith(ROOT)
        assert not any(k in request.headers for k in ("Authorization","Cookie","Proxy-Authorization"))
        assert request.headers["Accept"]=="application/json" and options["verify"] is True
        path=urlsplit(request.url).path; query=parse_qs(urlsplit(request.url).query)
        headers={"Content-Type":"application/json; charset=utf-8"}; status=200
        if path.endswith("/package_files"): data=self.files
        elif path.endswith("/packages"): data=self.packages
        elif path.endswith("/releases"): data=self.releases
        elif "/releases/" in path:
            data=self.releases[0] if self.releases else {}; status=200 if self.releases else 404
        else: data=self.project
        if isinstance(data,list):
            number=int(query.get("page",["1"])[0]); total=len(data); pages=max(1,(total+self.page_size-1)//self.page_size)
            data=data[(number-1)*self.page_size:number*self.page_size]
            headers.update({"x-page":str(number),"x-total":str(total),"x-total-pages":str(pages),"x-next-page":str(number+1) if number<pages else "","Link":"deliberately unparsed"})
        if self.change: status,headers,data=self.change(path,status,headers,data)
        response=requests.Response(); response.status_code=status; response.headers.update(headers)
        response.raw=io.BytesIO(json.dumps(data).encode()); response.url=request.url; response.request=request
        return response
    def client(self,**options):
        return GitLabReleaseClient(self.session,resolver=public_dns,clock=lambda:1000,sleep=lambda _:None,**options)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket,"connect",lambda *a,**k:pytest.fail("real network forbidden"))


@pytest.mark.parametrize("count",[1,201])
def test_mapping_pages(count):
    w=Wire(); w.releases=[release("v"+str(i)) for i in range(count)]
    catalog=w.client().discover_releases(TARGET)
    assert len(catalog.releases)==count
    assert catalog.repository==replace(TARGET,owner="GitLab-Org",repo="CLI",project_id=34675721)
    a=catalog.releases[0].assets[0]
    assert (a.id,a.file_id,a.size,a.sha256,a.state)==(14153947,388296723,93,SHA,"uploaded")
    assert a.browser_download_url==ROOT+"34675721/packages/generic/glab/1.130.0/"+FILE
    assert catalog.releases[0].id==catalog.releases[0].tag_name and catalog.releases[0].body is None
    assert catalog.duplicate_release_ids==() and catalog.redirects==0 and catalog.complete
    assert catalog.pages==(count+99)//100 and len(w.calls)==catalog.pages+3


@pytest.mark.parametrize("mode",["missing","total","early","loop","duplicate"])
def test_bad_pagination(mode):
    w=Wire()
    if mode=="duplicate": w.releases=[release(),release()]
    def change(path,status,headers,data):
        if path.endswith("/releases"):
            if mode=="missing": headers.pop("x-total")
            if mode=="total": headers["x-total"]="2"
            if mode=="early": headers.update({"x-total":"101","x-total-pages":"2","x-next-page":""})
            if mode=="loop": headers.update({"x-total":"101","x-total-pages":"2","x-next-page":"1"})
        return status,headers,data
    w.change=change
    with pytest.raises((PaginationError,IncompleteScanError)): w.client().discover_releases(TARGET)


@pytest.mark.parametrize("suffix,state",[("other","external_link"),("plus","unverifiable"),("slash","unverifiable"),("double","unverifiable"),("dot","unverifiable"),("host","external_link"),("upper","external_link"),("trailing","external_link"),("port","external_link"),("user","external_link"),("query","external_link"),("fragment","external_link"),("tab","external_link"),("newline","external_link"),("type","external_link")])
def test_link_grammar(suffix,state):
    urls=dict(other=URL.replace("gitlab-org%2Fcli","7"),plus=URL.replace("/glab/","/g+lab/"),slash=URL.replace(FILE,"a%2fb.zip"),double=URL.replace(FILE,"a%252Ezip"),dot=URL.replace(FILE,".."),host=URL.replace("gitlab.com","evil.com"),upper=URL.replace("gitlab.com","GITLAB.COM"),trailing=URL.replace("gitlab.com","gitlab.com."),port=URL.replace("gitlab.com","gitlab.com:443"),user=URL.replace("gitlab.com","user@gitlab.com"),query=URL+"?x=1",fragment=URL+"#x",tab=URL.replace("gitlab.com","gitlab.com\t"),newline=URL+"\n",type=URL.replace("/generic/","/npm/"))
    w=Wire(); w.releases=[release(url=urls[suffix])]
    result=w.client().discover_releases(TARGET)
    assert len(result.releases)==1 and result.releases[0].assets[0].state==state


@pytest.mark.parametrize("damage",["size","sha","duplicate","package","hidden"])
def test_unverifiable(damage):
    w=Wire()
    if damage=="size": w.files[0].pop("size")
    if damage=="sha": w.files[0].pop("file_sha256")
    if damage=="duplicate": w.files*=2
    if damage=="package": w.packages*=2
    if damage=="hidden": w.packages[0]["status"]="hidden"
    result=w.client().discover_releases(TARGET)
    assert len(result.releases)==1 and result.releases[0].assets[0].state=="unverifiable"


@pytest.mark.parametrize("endpoint",["project","releases","packages","package_files"])
@pytest.mark.parametrize("status",[301,302,303,304,307,308])
def test_zero_redirects(endpoint,status):
    w=Wire()
    def change(path,code,headers,data):
        match=path.endswith("/"+endpoint) if endpoint!="project" else path.endswith("gitlab-org%2Fcli")
        return (status,dict(headers,Location="https://evil.com/secret"),{}) if match else (code,headers,data)
    w.change=change
    with pytest.raises(UnsafeRedirectError): w.client().discover_releases(TARGET)


@pytest.mark.parametrize("retry",[None,"17"])
def test_rate_limit(retry):
    w=Wire()
    w.change=lambda p,s,h,d:(429,dict(h,**({"Retry-After":retry} if retry else {})),{})
    with pytest.raises(RateLimitError) as caught: w.client().discover_releases(TARGET)
    assert caught.value.retry_after_seconds==(17 if retry else 60)
    assert caught.value.kind!="primary"


@pytest.mark.parametrize("damage",["private","internal","path","content","oversize","dns","budget"])
def test_transport_failures(damage):
    w=Wire(); options={}; error=InvalidResponseError
    if damage in ("private","internal"): w.project["visibility"]=damage
    if damage=="path": w.project["path_with_namespace"]="other/repo"
    if damage=="content": w.change=lambda p,s,h,d:(s,{"Content-Type":"text/html"},d)
    if damage=="oversize": options["max_response_bytes"]=10
    if damage=="budget": options["max_requests"]=1; error=IncompleteScanError
    api=w.client(**options)
    if damage=="dns": api.resolver=lambda *a:[(socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1",443))]; error=UnsafeRedirectError
    with pytest.raises(error): api.discover_releases(TARGET)
    if damage=="dns": assert not w.calls


def test_budget_and_exact_packages():
    w=Wire(); w.releases=[release("v"+str(i)) for i in range(169)]
    w.packages=[package(name="glab-extra"),package(),package(status="hidden")]
    result=w.client().discover_releases(TARGET)
    assert len(result.releases)==169 and all(r.assets[0].state=="uploaded" for r in result.releases)
    assert len(w.calls)<=180 and sum(urlsplit(r.url).path.endswith("/packages") for r in w.calls)==1


def test_resolve():
    w=Wire(); repo=replace(TARGET,project_id=34675721)
    r=w.client().resolve_release(repo,"v1.130.0")
    assert r.id=="v1.130.0" and r.assets[0].file_id==388296723 and len(w.calls)==4
    assert w.calls[0].url==ROOT+"34675721"
    query=parse_qs(urlsplit(w.calls[2].url).query)
    assert query["package_name"]==["glab"] and query["package_version"]==["1.130.0"]
    w.files[0]["id"]=9
    assert w.client().resolve_release(repo,r.id).assets[0].file_id==9
    w.project["id"]=1
    with pytest.raises(GitHubApiError) as caught: w.client().resolve_release(repo,r.id)
    assert caught.value.code=="repository_mismatch"


def test_canonical_helper():
    url=ROOT+"34675721/packages/generic/glab/1.130.0/"+FILE
    assert parse_package_url(url)==(34675721,"glab","1.130.0",FILE)
    assert build_package_url(*parse_package_url(url))==url


@pytest.mark.parametrize("url",[URL.replace("gitlab-org%2Fcli","gitlab-org%2fsub%2fcli"),URL.replace("gitlab-org%2Fcli","34675721")])
def test_nested_and_numeric(url):
    w=Wire(); target=replace(TARGET,owner="gitlab-org/sub")
    w.project["path_with_namespace"]="GitLab-Org/Sub/CLI"; w.releases=[release(url=url)]
    catalog=w.client().discover_releases(target)
    assert catalog.repository.owner=="GitLab-Org/Sub" and catalog.releases[0].assets[0].state=="uploaded"
    assert w.calls[0].url==ROOT+"gitlab-org%2Fsub%2Fcli"


def test_unsupported_extension_is_uploaded():
    w=Wire(); w.files[0]["file_name"]="README.txt"; w.releases=[release(url=URL.rsplit("/",1)[0]+"/README.txt")]
    a=w.client().discover_releases(TARGET).releases[0].assets[0]
    assert a.state=="uploaded" and a.name=="README.txt" and a.supported_extension is None


@pytest.mark.parametrize("endpoint",["packages","package_files"])
def test_package_pagination(endpoint):
    w=Wire()
    if endpoint=="packages": w.packages=[package(name="other"+str(i),id=i+1) for i in range(100)]+[package()]
    else: w.files=[file(file_name="other"+str(i)) for i in range(100)]+[file()]
    result=w.client().discover_releases(TARGET)
    assert result.releases[0].assets[0].state=="uploaded"
    assert sum(urlsplit(r.url).path.endswith("/"+endpoint) for r in w.calls)==2


@pytest.mark.parametrize("field,value",[("id",True),("id",0),("size",True),("size",-1),("size",2**53),("file_sha256","A"*64),("file_sha256","bad")])
def test_invalid_file_metadata(field,value):
    w=Wire(); w.files[0][field]=value
    assert w.client().discover_releases(TARGET).releases[0].assets[0].state=="unverifiable"


def test_missing_release():
    w=Wire(); w.releases=[]
    with pytest.raises(NotFoundError): w.client().resolve_release(replace(TARGET,project_id=34675721),"gone")


@pytest.mark.parametrize("key",["x-page","x-next-page","x-total-pages"])
def test_missing_pagination_headers(key):
    w=Wire()
    def change(p,s,h,d):
        if p.endswith("/packages"): h.pop(key)
        return s,h,d
    w.change=change
    with pytest.raises((IncompleteScanError,PaginationError)): w.client().discover_releases(TARGET)


def test_changed_pagination_headers():
    w=Wire(); w.releases=[release("v"+str(i)) for i in range(201)]
    def change(p,s,h,d):
        if h.get("x-page")=="2": h["x-total"]="202"
        return s,h,d
    w.change=change
    with pytest.raises(PaginationError): w.client().discover_releases(TARGET)


def test_dns_rechecked_on_retry():
    w=Wire(); w.change=lambda p,s,h,d:(503,h,{})
    api=w.client(); checks=[]
    def dns(*args):
        checks.append(1)
        return public_dns(*args) if len(checks)==1 else [(socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1",443))]
    api.resolver=dns
    with pytest.raises(UnsafeRedirectError): api.discover_releases(TARGET)
    assert len(w.calls)==1 and len(checks)==2


def test_direct_session_hardening():
    w=Wire(); w.session.trust_env=True; w.session.verify=False; w.session.cert="unused"
    w.session.params={"token":"unused"}; w.session.proxies={"https":"unused"}
    w.session.headers.update({"Authorization":"unused","Cookie":"unused","Proxy-Authorization":"unused","Host":"unused"})
    result=w.client().discover_releases(TARGET)
    assert result.complete and w.session.verify is True and w.session.trust_env is False
    assert not w.session.params and not w.session.proxies and w.session.cert is None
    assert all("token" not in r.url and "Host" not in r.headers for r in w.calls)


def test_encoded_tag_and_future_release():
    w=Wire(); w.releases=[release("a/b +#")]; w.releases[0]["upcoming_release"]=True
    r=w.client().resolve_release(replace(TARGET,project_id=34675721),"a/b +#")
    assert r.prerelease and r.published_at==DATE and r.immutable is None and not r.draft
    assert w.calls[1].url.endswith("/releases/a%2Fb%20%2B%23")


def test_canonical_spelling_must_be_valid():
    w=Wire(); w.project["path_with_namespace"]="\u212a/cli"
    with pytest.raises(InvalidResponseError):
        w.client().discover_releases(replace(TARGET,owner="K"))


@pytest.mark.parametrize("project",["34675721","GITLAB-ORG%2fCLI"])
def test_project_url_spellings(project):
    w=Wire(); w.releases=[release(url=URL.replace("gitlab-org%2Fcli",project))]
    assert w.client().discover_releases(TARGET).releases[0].assets[0].state=="uploaded"


def test_gitlab_limit_headers_are_not_github_primary_limit():
    w=Wire()
    w.change=lambda p,s,h,d:(s,dict(h,**{"ratelimit-remaining":"0","ratelimit-reset":"99999999"}),d)
    assert w.client().discover_releases(TARGET).complete


@pytest.mark.parametrize("missing,code",[("release","release_removed"),("project","not_found")])
def test_release_404_has_distinct_code(missing,code):
    w=Wire(); w.releases=[]
    if missing=="project": w.change=lambda p,s,h,d:(404,h,{})
    with pytest.raises(NotFoundError) as caught: w.client().resolve_release(replace(TARGET,project_id=34675721),'gone')
    assert caught.value.code==code


@pytest.mark.parametrize('escape',['%2F','%2f'])
def test_package_helper_rejects_encoded_separator(escape):
    with pytest.raises(ValueError): parse_package_url(ROOT+'34675721/packages/generic/glab/1/a'+escape+'b.zip')


@pytest.mark.parametrize("path,state",[("other%2Frepo","external_link"),("gitlab-org%2Fother","external_link"),("GITLAB-ORG%2fCLI","uploaded")])
def test_encoded_project_identity(path,state):
    w=Wire(); w.releases=[release(url=URL.replace("gitlab-org%2Fcli",path))]
    a=w.client().discover_releases(TARGET).releases[0].assets[0]
    assert a.state==state
    if state=="uploaded": assert a.browser_download_url==ROOT+"34675721/packages/generic/glab/1.130.0/"+FILE


def test_all_asset_states_wire_roundtrip():
    from datetime import datetime
    from covearchive.scan_worker import encode_success, decode_response
    w=Wire(); urls=[URL,"https://gitlab.com/gitlab-org/cli/uploads/abc/app.zip","https://other.example/app.zip",
        URL.replace(FILE,"duplicate.zip"),URL.replace("/glab/","/g+lab/")]
    w.releases[0]["assets"]["links"]=[dict(id=i+1,name="link"+str(i),url=u) for i,u in enumerate(urls)]
    w.files.extend([file(id=1,file_name="duplicate.zip"),file(id=2,file_name="duplicate.zip")])
    original=w.client().discover_releases(TARGET)
    decoded=decode_response(encode_success("a"*32,original),"a"*32).catalog
    assert decoded==replace(original,endpoint_path="")
    assets=decoded.releases[0].assets
    assert [a.state for a in assets]==["uploaded","external_link","external_link","unverifiable","unverifiable"]
    for a in assets:
        assert datetime.fromisoformat(a.created_at) and datetime.fromisoformat(a.updated_at)
        assert a.created_at==a.updated_at==DATE



@pytest.mark.parametrize("endpoint",["releases","packages","package_files"])
@pytest.mark.parametrize("mode",["short_first","long_last","empty_middle"])
def test_impossible_page_distribution(endpoint,mode):
    w=Wire(); count=201 if mode=="empty_middle" else 150
    rows=[release(str(i)) for i in range(count)] if endpoint=="releases" else [package(id=i+1) for i in range(count)] if endpoint=="packages" else [file(id=i+1) for i in range(count)]
    setattr(w,dict(releases="releases",packages="packages",package_files="files")[endpoint],rows)
    def change(path,status,headers,data):
        if path.endswith("/"+endpoint):
            page=int(headers["x-page"])
            if mode=="short_first": data=rows[:50] if page==1 else rows[50:]
            elif mode=="long_last" and page==2: data=data+[rows[0]]
            elif mode=="empty_middle" and page==2: data=[]
        return status,headers,data
    w.change=change
    path="34675721/"+("packages/76008474/package_files" if endpoint=="package_files" else endpoint)
    with pytest.raises(PaginationError): w.client()._listing(path,(),dict(pages=0,requests=0,redirects=0))


@pytest.mark.parametrize("endpoint",["releases","packages","package_files"])
@pytest.mark.parametrize("damage",["skip","wrong_page"])
def test_listing_page_identity(endpoint,damage):
    w=Wire(); rows=[release(str(i)) for i in range(250)] if endpoint=="releases" else [package(id=i+1) for i in range(250)] if endpoint=="packages" else [file(id=i+1) for i in range(250)]
    setattr(w,"files" if endpoint=="package_files" else endpoint,rows)
    def change(path,status,headers,data):
        if path.endswith("/"+endpoint):
            page=int(headers["x-page"])
            if damage=="skip" and page==1: headers["x-next-page"]="3"
            if damage=="wrong_page" and page==2: headers["x-page"]="1"
        return status,headers,data
    w.change=change; path="34675721/"+("packages/76008474/package_files" if endpoint=="package_files" else endpoint)
    error=PaginationError if damage=="wrong_page" else (PaginationError,IncompleteScanError)
    with pytest.raises(error): w.client()._listing(path,(),dict(pages=0,requests=0,redirects=0))


@pytest.mark.parametrize("damage",["hidden","pending_destruction","missing_status","package_id"])
def test_resolve_package_provenance(damage):
    w=Wire()
    if damage=="package_id": w.files[0]["package_id"]+=1
    elif damage=="missing_status": w.packages[0].pop("status")
    else: w.packages[0]["status"]=damage
    a=w.client().resolve_release(replace(TARGET,project_id=34675721),"v1.130.0").assets[0]
    assert a.state=="unverifiable" and a.file_id is None and a.sha256 is None


@pytest.mark.parametrize("resolve",[False,True])
def test_duplicate_link_ids_rejected(resolve):
    w=Wire(); w.releases[0]["assets"]["links"]*=2
    with pytest.raises(PaginationError):
        if resolve: w.client().resolve_release(replace(TARGET,project_id=34675721),"v1.130.0")
        else: w.client().discover_releases(TARGET)


@pytest.mark.parametrize("tag",["V1.130.0","v1.130","v1.130.0-extra"])
def test_resolve_exact_tag(tag):
    w=Wire(); w.releases=[release(tag)]
    with pytest.raises(InvalidResponseError): w.client().resolve_release(replace(TARGET,project_id=34675721),"v1.130.0")


@pytest.mark.parametrize("url",["https://gitlab.com/evil","http://gitlab.com/api/v4/projects/1","https://gitlab.com.evil.example/api/v4/projects/1"])
def test_transport_prefix_before_get(monkeypatch,url):
    w=Wire(); calls=[]
    def get(*a,**k): calls.append(1); pytest.fail("session.get must not be called")
    monkeypatch.setattr(w.session,"get",get)
    with pytest.raises(UnsafeRedirectError): w.client()._page(url,dict(pages=0,requests=0,redirects=0))
    assert not calls and not w.calls
