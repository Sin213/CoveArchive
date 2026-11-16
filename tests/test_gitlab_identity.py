"""GitLab foundation identities, metadata and migration boundaries."""
from dataclasses import replace
import hashlib
import json
import sqlite3
import pytest
from covearchive import providers
from covearchive.github.urls import RepositoryTarget
from covearchive.github.models import Release, ReleaseAsset
from covearchive.archive import manifest as m, naming, plan, store, download
from covearchive.queue import store as q
from covearchive.queue.models import QueueBusyError, QueueVersionError, InvalidJobError


def target(project_id=5):
    return replace(RepositoryTarget("group/sub", "project", "gitlab", "gitlab.com"), project_id=project_id)


def release(tag="007", file_id=1):
    a=ReleaseAsset(7,"a.zip",0,"uploaded","","",None,None,None,"","",".zip")
    a=replace(a,file_id=file_id)
    return Release(tag,tag,None,False,False,None,"",None,None,(a,),None,None)


def test_nested_url():
    r=providers.parse_repository_url("https://gitlab.com/group/sub/project/-/releases/?x=1#x")
    assert r.repo_key=="gitlab.com/group/sub/project"
    assert providers.project_components(target())==("gitlab.com","group","sub","project")
    assert providers.validate_repository(target()).project_id==5
    assert providers.display_name("gitlab")=="GitLab"


@pytest.mark.parametrize("suffix",["a/-bad","a/p.git","a/p.atom","a/../p","a//p","a/p/-/releases/tag/x","a/"+"p"*256])
def test_bad_paths(suffix):
    with pytest.raises(ValueError): providers.parse_repository_url("https://gitlab.com/"+suffix)


def test_folder_digest_and_manifest():
    r=release(); folder=naming.release_folder_name(r.tag_name,r.id)
    n=int.from_bytes(hashlib.sha256(b"007").digest()[:8],"big")|2**63
    assert folder==f"release-007--{n}"
    record=m.ReleaseRecord(r.id,r.tag_name,folder,())
    index=m.ProjectIndex(target(),(record,))
    encoded=m.dumps_index(index)
    assert json.loads(encoded)["schema_version"]==3
    assert m.loads_index(encoded)==index
    doc=m.build_release_manifest(target(),r,record)
    assert m.loads_release_manifest(m.dumps_release_manifest(doc))==doc
    with pytest.raises(m.ManifestError): m.ProjectIndex(target(None),())
    with pytest.raises(m.ManifestError): m.ProjectIndex(replace(RepositoryTarget("a","b"),project_id=5),())
    with pytest.raises(m.ManifestError): m.ProjectIndex(target(),(m.ReleaseRecord(5,"5","release-5--5",()),))


@pytest.mark.parametrize("tag",["1.0","007","1e3","a|b","  x"])
def test_queue_exact_tags(tmp_path,tag):
    db=q.QueueStore(tmp_path/"q.db"); r=release(tag)
    first=db.enqueue(target(),r,r.assets[0],str(tmp_path)).job
    assert first.release_id is None and first.release_key==tag and first.project_id==5
    assert db.get(first.job_id)==first
    r2=release(tag,2)
    second=db.enqueue(target(),r2,r2.assets[0],str(tmp_path))
    assert second.outcome=="created" and second.job.identity!=first.identity
    assert db.enqueue(target(6),r,r.assets[0],str(tmp_path)).outcome=="created"
    assert db.get(first.job_id)==first


def test_plan_mixed_and_squatting(tmp_path):
    r=release(); p=plan.plan_archive([r],archive_root=str(tmp_path),repository=target())
    assert tuple(p.project_dir.parts)==("gitlab.com","group","sub","project")
    with pytest.raises(plan.PlanError):
        plan.plan_archive([r,replace(r,id=5,assets=(replace(r.assets[0],id=8),))],archive_root=str(tmp_path),repository=target())
    with pytest.raises(plan.PlanError):
        plan.plan_archive([r],archive_root=str(tmp_path),repository=target(6),index=p.index)


@pytest.mark.parametrize("version",[1,2])
def test_migrations(tmp_path,version):
    path=tmp_path/"old.db"
    with sqlite3.connect(path) as c:
        c.execute(q._SCHEMA_V1 if version==1 else q._SCHEMA)
        c.execute(q._COOLDOWNS)
        c.execute("INSERT INTO jobs(seq,job_id,identity,provider,state) VALUES (42,'job','identity','github','QUEUED')")
        c.execute("UPDATE sqlite_sequence SET seq=99 WHERE name='jobs'")
        c.execute("INSERT INTO cooldowns VALUES ('global',123)")
        c.execute(f"PRAGMA application_id={q.APPLICATION_ID}")
        c.execute(f"PRAGMA user_version={version}")
    before=path.read_bytes()
    with q.RunnerLock(path):
        with pytest.raises(QueueBusyError): q.QueueStore(path)
    assert path.read_bytes()==before
    q.QueueStore(path)
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()==(3,)
        assert c.execute("SELECT seq,job_id,identity,provider,state,host,project_id,release_key,file_id FROM jobs").fetchone()==(42,'job','identity','github','QUEUED','github.com' if version==1 else None,None,None,None)
        assert c.execute("SELECT seq FROM sqlite_sequence WHERE name='jobs'").fetchone()==(99,)
        assert c.execute("SELECT * FROM cooldowns").fetchall()==[('global',123.0)]
        c.execute("PRAGMA user_version=4")
    with pytest.raises(QueueVersionError): q.QueueStore(path)


def test_string_folder_without_models():
    tag="v1/odd"
    folder=naming.release_folder_name(tag,tag)
    assert naming.is_release_folder(folder,tag)
    assert not naming.is_release_folder(folder,tag+"x")


def test_v3_decode_and_key_type():
    data={"schema_version":3,"provider":"gitlab","repository":{"host":"gitlab.com","project_id":5,"owner":"g/sub","name":"p","key":"gitlab.com/g/sub/p"},"releases":[]}
    assert m.loads_index(json.dumps(data)).repository.project_id==5


@pytest.mark.parametrize("url",["gitlab.com\t/a/p", "https://gitlab.com./a/p", "https://gitlab.com:443/a/p", "https://x@gitlab.com/a/p", "https://www.gitlab.com/a/p", "https://gitlab.com/a/%2fp", "https://gitlab.com/a/%252E", "https://gitlab.com/a/"+"/".join(["p"]*21)])
def test_hostile_urls(url):
    with pytest.raises(ValueError): providers.parse_repository_url(url)


def test_nested_sanitization():
    repo=replace(target(),owner="CON/sub.")
    parts=naming.project_components(repo)
    assert parts[0]=="gitlab.com" and len(parts)==4
    assert parts[1:3]==tuple(naming.project_folders(p,p)[0] for p in ("CON","sub."))
    assert all("+" in p for p in parts[1:3])


def test_wrong_persisted_keys():
    for repo,key,tag in ((target(),5,"5"),(RepositoryTarget("g","p"),"5","5")):
        data=json.loads(m.dumps_index(m.ProjectIndex(repo,())))
        data["releases"]=[dict(release_id=key,tag=tag,folder="release-5--5",assets=[])]
        with pytest.raises(m.ManifestError): m.loads_index(json.dumps(data))
    with pytest.raises(InvalidJobError):
        q.QueueStore._validate(object(),target(None),release(),release().assets[0],"C:/Archive")


def test_folder_collision(tmp_path,monkeypatch):
    class Digest:
        def digest(self): return b"x"*32
    monkeypatch.setattr(naming.hashlib,"sha256",lambda *a:Digest())
    a=release("a/b"); b=replace(release("a:b"),assets=(replace(a.assets[0],id=8),))
    with pytest.raises(plan.PlanError,match="unrepresentable"):
        plan.plan_archive([a,b],archive_root=str(tmp_path),repository=target())
    assert list(tmp_path.iterdir())==[]


def test_recovery_and_squatting(tmp_path,monkeypatch):
    from contextlib import contextmanager
    r=release(); repo=target()
    p=plan.plan_archive([r],archive_root=str(tmp_path),repository=repo)
    rec=p.index.releases[0]; doc=m.build_release_manifest(repo,r,rec)
    with store.winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.ensure_project(root,repo) as project:
            with store.project_writer(project):
                with store.ensure_release(project,rec.folder,r.id) as directory:
                    store.commit_release(directory,doc)
            state=store.load_project_state(project)
            assert state.unindexed==(doc,)
            assert not any(issue=="consistency" for _,issue in state.issues)
    @contextmanager
    def stream(*a,**kw): yield iter(())
    monkeypatch.setattr(download,"open_asset_stream",stream)
    result=download.archive_asset(repo,r,r.assets[0],str(tmp_path))
    assert result.status.value=="ARCHIVED" and result.change=="MANIFEST_RECOVERY"
    index=tmp_path.joinpath(*naming.project_components(repo),naming.INDEX_FILENAME)
    before=index.read_bytes(); files=sorted(str(p) for p in tmp_path.rglob("*") if p.is_file())
    result=download.archive_asset(target(6),r,r.assets[0],str(tmp_path))
    assert result.code=="repository_mismatch" and index.read_bytes()==before
    assert sorted(str(p) for p in tmp_path.rglob("*") if p.is_file())==files
    with store.winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.ensure_project(root,repo) as project:
            with store.project_writer(project):
                with pytest.raises(store.ArchiveConflictError,match="identity"):
                    store.commit_index(project,replace(p.index,repository=target(6)),hashlib.sha256(before).hexdigest())


def test_mixed_unindexed(tmp_path):
    repo=target(); r=release()
    docs=[m.build_release_manifest(repo,r,m.ReleaseRecord(r.id,r.tag_name,naming.release_folder_name(r.id,r.id),()))]
    gh=RepositoryTarget("g","p"); old=replace(r,id=5,tag_name="5")
    docs.append(m.build_release_manifest(gh,old,m.ReleaseRecord(5,"5","release-5--5",())))
    with store.winfs.ArchiveRoot.open(str(tmp_path)) as root:
        with store.ensure_project(root,repo) as project:
            with store.project_writer(project):
                for doc in docs:
                    with store.ensure_release(project,doc.folder,doc.release_id) as directory:
                        store.commit_release(directory,doc)
            assert len(store.load_project_state(project).unindexed)==2


def test_all_tag_identities(tmp_path):
    db=q.QueueStore(tmp_path/"q.db"); jobs=[]
    for tag in ("1.0","007","1e3","a|b","  x"):
        r=release(tag); jobs.append(db.enqueue(target(),r,r.assets[0],str(tmp_path)).job)
    assert len({j.identity for j in jobs})==5
    assert [j.release_key for j in db.list_jobs()]==["1.0","007","1e3","a|b","  x"]
    r=release("https://hidden?token=abc")
    job=db.enqueue(target(),r,r.assets[0],str(tmp_path)).job
    assert job.tag_name=="[redacted]" and job.release_key==r.id


def test_v2_rollback(tmp_path,monkeypatch):
    path=tmp_path/"v2.db"
    with sqlite3.connect(path) as c:
        c.execute(q._SCHEMA); c.execute(q._COOLDOWNS)
        c.execute("INSERT INTO jobs (job_id,identity,state) VALUES ('j','i','QUEUED')")
        c.execute("PRAGMA user_version=2"); c.execute(f"PRAGMA application_id={q.APPLICATION_ID}")
    before=path.read_bytes(); original=q.QueueStore._migration_snapshot; calls=[]
    def snapshot(self,*args):
        calls.append(1)
        return original(self,*args) if len(calls)==1 else (0,b"bad")
    monkeypatch.setattr(q.QueueStore,"_migration_snapshot",snapshot)
    from covearchive.queue.models import QueueCorruptError
    with pytest.raises(QueueCorruptError): q.QueueStore(path)
    assert path.read_bytes()==before


@pytest.mark.parametrize("text",[" https://gitlab.com/a/b ","\tGitLab.COM/a/b\n","https://GitLab.COM/a/b"])
def test_gitlab_input_leniency(text):
    assert providers.parse_repository_url(text)==RepositoryTarget("a","b","gitlab","gitlab.com")


@pytest.mark.parametrize("route",["issues","releases/tag/v1","releases/latest","tree/main"])
def test_gitlab_unsupported_route(route):
    with pytest.raises(providers.RepositoryUrlError,match="Invalid") as caught:
        providers.parse_repository_url("https://gitlab.com/a/b/-/"+route)
    assert caught.value.code=="unsupported"


def test_excluded_mixed_keys(tmp_path):
    good=replace(release(),id=5,tag_name="v5")
    excluded=replace(release(),draft=True,assets=())
    p=plan.plan_archive([good,excluded],archive_root=str(tmp_path),repository=RepositoryTarget("a","b"))
    assert len(p.releases)==1 and len(p.excluded)==1


def test_missing_project_ids(tmp_path):
    r=release(); rec=m.ReleaseRecord(r.id,r.tag_name,naming.release_folder_name(r.id,r.id),())
    with pytest.raises(m.ManifestError,match="identity"):
        m.build_release_manifest(target(None),r,rec)
    with pytest.raises(InvalidJobError):
        q.QueueStore(tmp_path/"q.db").enqueue(target(None),r,r.assets[0],str(tmp_path))
    for provider,host in (("github","github.com"),("codeberg","codeberg.org")):
        bad=RepositoryTarget("a","b",provider,host,5)
        with pytest.raises(providers.RepositoryUrlError): providers.validate_repository(bad)


@pytest.mark.parametrize("provider,host,index_golden,release_golden",[
    ('github', 'github.com', '{\n  "schema_version": 1,\n  "provider": "github",\n  "repository": {\n    "owner": "A",\n    "name": "B",\n    "key": "a/b"\n  },\n  "releases": [\n    {\n      "release_id": 5,\n      "tag": "v5",\n      "folder": "release-v5--5",\n      "assets": []\n    }\n  ]\n}\n', '{\n  "schema_version": 1,\n  "provider": "github",\n  "repository": {\n    "owner": "A",\n    "name": "B",\n    "key": "a/b"\n  },\n  "release": {\n    "release_id": 5,\n    "tag": "v5",\n    "folder": "release-v5--5",\n    "title": null,\n    "published_at": null,\n    "prerelease": false,\n    "source_url": "https://github.com/A/B/releases/tag/v5"\n  },\n  "assets": []\n}\n'),
    ('codeberg', 'codeberg.org', '{\n  "schema_version": 2,\n  "provider": "codeberg",\n  "repository": {\n    "host": "codeberg.org",\n    "owner": "A",\n    "name": "B",\n    "key": "codeberg.org/a/b"\n  },\n  "releases": [\n    {\n      "release_id": 5,\n      "tag": "v5",\n      "folder": "release-v5--5",\n      "assets": []\n    }\n  ]\n}\n', '{\n  "schema_version": 2,\n  "provider": "codeberg",\n  "repository": {\n    "host": "codeberg.org",\n    "owner": "A",\n    "name": "B",\n    "key": "codeberg.org/a/b"\n  },\n  "release": {\n    "release_id": 5,\n    "tag": "v5",\n    "folder": "release-v5--5",\n    "title": null,\n    "published_at": null,\n    "prerelease": false,\n    "source_url": "https://codeberg.org/A/B/releases/tag/v5"\n  },\n  "assets": []\n}\n'),
])
def test_manifests_golden_bytes(provider,host,index_golden,release_golden):
    repo=RepositoryTarget("A","B",provider,host); rec=m.ReleaseRecord(5,"v5","release-v5--5",())
    index=m.ProjectIndex(repo,(rec,))
    doc=m.build_release_manifest(repo,replace(release(),id=5,tag_name="v5"),rec)
    assert m.dumps_index(index)==index_golden and m.loads_index(index_golden)==index
    assert m.dumps_release_manifest(doc)==release_golden and m.loads_release_manifest(release_golden)==doc


def test_sanitized_component_separation():
    for original in ("CON","sub.","NUL"):
        repo=replace(target(),owner=original+"/nest",repo=original)
        parts=naming.project_components(repo)
        clean=naming.sanitize_component(original)
        plain=naming.project_components(replace(repo,owner=clean+"/nest",repo=clean))
        assert parts[1]!=plain[1] and parts[-1]!=plain[-1]
        assert "+" in parts[1] and "+" in parts[-1]


@pytest.mark.parametrize("version",[1,2])
def test_all_migration_fields(tmp_path,version):
    from test_queue_store import legacy_queue
    with sqlite3.connect(":memory:") as old:
        columns,rows=legacy_queue(old)
        cooldowns=old.execute("SELECT * FROM cooldowns").fetchall()
    path=tmp_path/"legacy.db"
    with sqlite3.connect(path) as c:
        c.execute(q._SCHEMA_V1 if version==1 else q._SCHEMA); c.execute(q._COOLDOWNS)
        c.executemany("INSERT INTO jobs ("+columns+") VALUES ("+",".join("?" for _ in rows[0])+")",rows)
        if version==2: c.execute("UPDATE jobs SET host='github.com'")
        c.executemany("INSERT INTO cooldowns VALUES (?,?)",cooldowns)
        c.execute("UPDATE sqlite_sequence SET seq=99 WHERE name='jobs'")
        c.execute(f"PRAGMA user_version={version}"); c.execute(f"PRAGMA application_id={q.APPLICATION_ID}")
    q.QueueStore(path)
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT "+columns+" FROM jobs ORDER BY seq").fetchall()==rows
        assert c.execute("SELECT * FROM cooldowns").fetchall()==cooldowns
        assert c.execute("SELECT seq FROM sqlite_sequence WHERE name='jobs'").fetchone()==(99,)
        assert set(c.execute("SELECT host,project_id,release_key,file_id FROM jobs"))=={('github.com',None,None,None)}


def test_cross_provider_identities(tmp_path):
    db=q.QueueStore(tmp_path/"ids.db"); r=replace(release(),id=5,tag_name="5")
    jobs=[]
    for provider,host in (("github","github.com"),("codeberg","codeberg.org")):
        repo=RepositoryTarget("group","project",provider,host)
        jobs.append(db.enqueue(repo,r,r.assets[0],str(tmp_path)).job)
    repo=replace(target(),owner="group"); r=replace(r,id="5")
    jobs.append(db.enqueue(repo,r,r.assets[0],str(tmp_path)).job)
    assert len({j.identity for j in jobs})==3
