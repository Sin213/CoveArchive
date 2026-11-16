"""Hostile inputs and process boundaries; no public network access."""
from dataclasses import replace
import errno
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

from covearchive.archive import githistory as gh, store
from covearchive.github.urls import RepositoryTarget
from test_git_history import REPO, local, archive, git, snapshot_bytes


@pytest.mark.parametrize("repository", [
    "http://github.com/o/r", "https://user:secret@github.com/o/r",
    "ssh://github.com/o/r", "file:///tmp/repo", "git://github.com/o/r",
    "ext::command", "--upload-pack=evil", "https://github.com:443/o/r",
    "https://gıthub.com/o/r", "https://github.com/o/r?token=secret",
    RepositoryTarget("-evil", "repo"), RepositoryTarget("owner", "../repo"),
    RepositoryTarget("owner", "repo", "github", "evil.example"),
    RepositoryTarget("owner", "repo", "github", "github.com:443"),
    RepositoryTarget("owner", "repo", "github", "user@github.com"),
])
def test_invalid_identity_rejected_before_spawn(tmp_path, monkeypatch, repository):
    monkeypatch.setattr(gh, "_spawn", lambda *a, **kw: pytest.fail("spawned for invalid identity"))
    with pytest.raises(gh.HistoryError) as error: gh.archive_history(repository, tmp_path)
    assert error.value.code == "invalid_repository"
    assert "secret" not in str(error.value) and "://" not in str(error.value)
    assert list(tmp_path.iterdir()) == []


def test_git_calls_are_hardened_and_private(local, monkeypatch):
    calls = []; spawn = gh._spawn
    def record(args, **kw):
        calls.append((args, kw)); return spawn(args, **kw)
    monkeypatch.setattr(gh, "_spawn", record)
    archive(local)
    assert calls
    required = ["credential.helper=", "core.hooksPath=" + os.devnull,
        "core.fsmonitor=false", "core.symlinks=false", "http.followRedirects=false",
        "http.sslVerify=true", "fetch.fsckObjects=true", "transfer.fsckObjects=true",
        "fetch.recurseSubmodules=false", "submodule.recurse=false", "gc.auto=0",
        "maintenance.auto=false", "core.logAllRefUpdates=false",
        "http.lowSpeedLimit=1024", "http.lowSpeedTime=120"]
    for args, kw in calls:
        assert isinstance(args, list) and Path(args[0]).is_absolute()
        assert all(item in args for item in required)
        assert kw.get("shell", False) is False and kw["stdin"] == subprocess.DEVNULL
        assert Path(kw["cwd"]).resolve() != local[1].resolve()
        assert local[1].resolve() not in Path(kw["cwd"]).resolve().parents
        assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kw["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert "GIT_ASKPASS" not in kw["env"] and "SSH_ASKPASS" not in kw["env"]
    fetch = next(args for args, kw in calls if "fetch" in args)
    assert "--no-tags" in fetch and "--no-recurse-submodules" in fetch
    assert fetch.index("--") < fetch.index(local[0].as_uri())
    assert "+refs/heads/*:refs/heads/*" in fetch and "+refs/tags/*:refs/tags/*" in fetch
    assert not any(set(args) & {"checkout", "submodule", "lfs", "reset"} for args, kw in calls)


def test_hostile_environment_and_global_config(local, monkeypatch, tmp_path):
    marker = tmp_path / "executed"; config = tmp_path / "hostile.gitconfig"
    config.write_text('[credential]\n helper = !echo BAD\n[core]\n hooksPath = hostile\n[url "ext::evil"]\n insteadOf = file://\n')
    poison = {"GIT_CONFIG_GLOBAL": str(config), "GIT_DIR": str(tmp_path / "evil"),
        "GIT_SSH_COMMAND": "echo BAD", "GIT_EXEC_PATH": "evil", "GIT_TRACE": str(marker),
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "evil",
        "GIT_ASKPASS": "evil", "SSH_ASKPASS": "evil", "UNRELATED_SECRET": "secret"}
    for key, value in poison.items(): monkeypatch.setenv(key, value)
    calls = []; spawn = gh._spawn
    def record(args, **kw): calls.append(kw["env"]); return spawn(args, **kw)
    monkeypatch.setattr(gh, "_spawn", record)
    assert archive(local).status == "ARCHIVED"
    assert not marker.exists() and calls
    for env in calls:
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert not (set(poison) - {"GIT_CONFIG_GLOBAL"}) & set(env)
        for key, value in {"GIT_CONFIG_NOSYSTEM": "1", "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0", "GCM_INTERACTIVE": "never",
            "NoDefaultCurrentDirectoryInExePath": "1"}.items(): assert env[key] == value


def test_git_resolution_excludes_relative_path_and_cwd(tmp_path, monkeypatch):
    real = Path(shutil.which("git")).resolve(); fake = tmp_path / real.name
    fake.write_bytes(b"not an executable")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "relative", str(real.parent)]))
    assert Path(gh._git_executable()).resolve() == real


@pytest.mark.parametrize("limit,value,code", [("max_refs", 1, "too_many_refs"),
    ("max_working_bytes", 1, "too_large"), ("max_stdout_bytes", 1, "too_large"),
    ("min_free_bytes", 2**63, "disk_full")])
def test_limits_preserve_archive(local, limit, value, code):
    # The zero-byte git-history lock is infrastructure, not archived content.
    def content():
        return {p: b for p, b in snapshot_bytes(local[1]).items()
                if not (Path(p).parts == ("Owner", "Repo", "git-history", ".covearchive.lock") and b == b"")}
    before = content()
    with pytest.raises(gh.HistoryError) as error:
        archive(local, limits=replace(gh.Limits(), **{limit: value}))
    assert error.value.code == code and content() == before


def test_empty_repository_writes_nothing(local):
    git(local[0], "update-ref", "-d", "refs/heads/main")
    git(local[0], "update-ref", "-d", "refs/tags/v1")
    with pytest.raises(gh.HistoryError) as error: archive(local)
    assert error.value.code == "empty_repository" and list(local[1].iterdir()) == []


def test_hooks_submodules_lfs_are_inert(local, tmp_path):
    marker = tmp_path / "executed"; hooks = local[0] / "hooks"; hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-upload-pack"; hook.write_text("#!/bin/sh\necho BAD > '" + str(marker) + "'\n"); hook.chmod(0o755)
    modules = b'[submodule "evil"]\n path = evil\n url = ext::evil\n'
    pointer = b'version https://git-lfs.github.com/spec/v1\noid sha256:' + b'0' * 64 + b'\nsize 123\n'
    a = git(local[0], "hash-object", "-w", "--stdin", data=modules)
    b = git(local[0], "hash-object", "-w", "--stdin", data=pointer)
    tree = git(local[0], "mktree", data=(f"100644 blob {a}\t.gitmodules\n100644 blob {b}\tpointer\n160000 commit {local[2]}\tevil\n").encode())
    commit = git(local[0], "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit-tree", tree, data=b"hostile\n")
    git(local[0], "update-ref", "refs/heads/main", commit)
    result = archive(local)
    assert result.status == "ARCHIVED" and not marker.exists()
    assert not list(local[1].rglob(".gitmodules")) and not list(local[1].rglob("pointer"))


def test_disk_full_during_publication_preserves_previous(local, monkeypatch):
    first = archive(local); before = snapshot_bytes(first.snapshot)
    git(local[0], "update-ref", "refs/heads/main", local[3](local[2]))
    def full(*args, **kw): raise OSError(errno.ENOSPC, "disk full")
    monkeypatch.setattr(store, "write_asset", full)
    with pytest.raises(gh.HistoryError) as error: archive(local)
    assert error.value.code == "disk_full" and snapshot_bytes(first.snapshot) == before


def test_bundle_validation_precedes_publication(local, monkeypatch):
    spawn = gh._spawn; calls = []
    def tamper(args, **kw):
        calls.append(args)
        if "list-heads" in args:
            return spawn([sys.executable, "-c", "print('0'*40+' refs/heads/fake')"], **kw)
        return spawn(args, **kw)
    monkeypatch.setattr(gh, "_spawn", tamper)
    with pytest.raises(gh.HistoryError) as error: archive(local)
    assert error.value.code == "invalid_objects"
    assert not list(local[1].rglob("history.json"))


def test_concurrent_history_busy_does_not_lock_releases(local):
    result = archive(local)
    with store.ArchiveRoot.open(str(local[1])) as root:
        with store.ensure_project(root, REPO) as project:
            with project.open_dir("git-history") as history:
                with history.open_lock():
                    with pytest.raises(gh.HistoryError) as error: archive(local)
                    assert error.value.code == "busy"
                    with store.project_writer(project): pass


@pytest.mark.parametrize("reason", ["cancel", "timeout"])
def test_runner_kills_child_tree_and_cleans_private_cwd(tmp_path, monkeypatch, reason):
    # Substitute only process creation; the real supervision/pipe/drain/kill code runs.
    script = tmp_path / "hang.py"; marker = tmp_path / "child-survived"; ready = tmp_path / "child-started"
    script.write_text("import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c',"
        + repr("import time,pathlib; pathlib.Path(" + repr(str(ready)) + ").write_text('ready'); time.sleep(2); pathlib.Path(" + repr(str(marker)) + ").write_text('alive')")
        + "])\nprint('started',flush=True)\ntime.sleep(30)\n")
    spawned = []; watchers = []; spawn = gh._spawn; cancel = threading.Event()
    def fake(args, **kw):
        process = spawn([sys.executable, str(script)], **kw); spawned.append((process, Path(kw["cwd"])))
        if reason == "cancel":
            def after_child_started():
                deadline = time.monotonic() + 2
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline: time.sleep(.01)
                cancel.set()
            watcher = threading.Thread(target=after_child_started); watchers.append(watcher); watcher.start()
        return process
    monkeypatch.setattr(gh, "_spawn", fake)
    try:
        with pytest.raises(gh.HistoryError) as error:
            gh._run_git(["ls-remote", "--", "https://github.com/Owner/Repo.git"], cancel=cancel.is_set,
                        limits=replace(gh.Limits(), ls_remote_seconds=1, overall_seconds=1.5))
        assert error.value.code == ("cancelled" if reason == "cancel" else "timeout")
        assert ready.exists(), "test must observe the descendant before killing the tree"
        assert spawned and all(p.poll() is not None and not cwd.exists() for p, cwd in spawned)
        time.sleep(2.2)
        assert not marker.exists(), "descendant outlived cancellation"
    finally:
        for process, cwd in spawned:
            if process.poll() is None: process.kill()
            process.wait(timeout=5)
        for watcher in watchers:
            watcher.join(timeout=3); assert not watcher.is_alive()


def test_redirect_rejected_local_server(local, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(301); self.send_header("Location", "http://127.0.0.1:1/forbidden"); self.end_headers()
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever); thread.start()
    monkeypatch.setattr(gh, "_remote_url", lambda repo: f"http://127.0.0.1:{server.server_port}/repo.git")
    monkeypatch.setattr(gh, "_test_protocols", ("http",))
    try:
        with pytest.raises(gh.HistoryError) as error: archive(local)
        assert error.value.code == "redirect" and list(local[1].iterdir()) == []
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize("damage", ["missing_committer", "missing_tree"])
def test_fsck_bad_objects_rejected_without_publication(local, damage, monkeypatch):
    commands = []; run = gh._run_git
    def recorded(args, **kwargs): commands.append(args); return run(args, **kwargs)
    monkeypatch.setattr(gh, "_run_git", recorded)
    tree = git(local[0], "mktree", data=b"") if damage == "missing_committer" else "0" * 40
    raw = f"tree {tree}\nauthor Test <test@example.invalid> 1000000000 +0000\n"
    if damage == "missing_tree": raw += "committer Test <test@example.invalid> 1000000000 +0000\n"
    bad = git(local[0], "hash-object", "--literally", "-t", "commit", "-w", "--stdin", data=(raw + "\nmalformed\n").encode())
    git(local[0], "update-ref", "refs/heads/bad", bad)
    check = git(local[0], "fsck", "--full", check=False)
    assert check.returncode != 0, "fixture must contain an actual fsck violation"
    with pytest.raises(gh.HistoryError) as error: archive(local)
    assert error.value.code == "invalid_objects"
    assert "fetch" in commands[-1], "fetch.fsckObjects must reject the bad objects on receipt"
    assert not list(local[1].rglob("history.json"))


def test_size_poll_throttled_and_readonly_cleanup(tmp_path, monkeypatch):
    import stat
    tree = tmp_path/"readonly"; (tree/"objects"/"pack").mkdir(parents=True)
    pack = tree/"objects"/"pack"/"x.pack"; pack.write_bytes(b"pack"); pack.chmod(stat.S_IREAD)
    gh._rmtree(tree); assert not tree.exists()
    times = []; spawn = gh._spawn
    def slow(args, **kw): return spawn([sys.executable,"-c","import time; time.sleep(1.1)"], **kw)
    monkeypatch.setattr(gh,"_spawn",slow)
    monkeypatch.setattr(gh,"_size",lambda path: times.append(time.monotonic()) or 0)
    gh._run_git(["--version"])
    # The final completion check is unconditional; only in-flight polls are throttled.
    assert 2 <= len(times[:-1]) <= 3 and all(b-a >= .49 for a,b in zip(times[:-1],times[1:-1]))


def test_advertised_pull_ref_volume_does_not_consume_ref_cap(local):
    commands = "".join(f"update refs/pull/{i}/head {local[2]}\n" for i in range(20_100))
    commands += f"update refs/merge-requests/1/head {local[2]}\nupdate refs/notes/test {local[2]}\n"
    git(local[0], "update-ref", "--stdin", data=commands.encode())
    result = archive(local, limits=replace(gh.Limits(), max_refs=4))
    assert result.status == "ARCHIVED" and result.ref_count == 2
    assert archive(local, limits=replace(gh.Limits(), max_refs=4)).status == "UNCHANGED"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_runner_output_is_bounded(tmp_path, monkeypatch, stream):
    spawn = gh._spawn; children = []
    def flood(args, **kw):
        process = spawn([sys.executable, "-c", f"import sys; sys.{stream}.write('x'*2000000); sys.{stream}.flush()"], **kw)
        children.append(process); return process
    monkeypatch.setattr(gh, "_spawn", flood)
    try:
        with pytest.raises(gh.HistoryError) as error:
            gh._run_git(["ls-remote", "--", "https://github.com/Owner/Repo.git"],
                limits=replace(gh.Limits(), max_stdout_bytes=100, max_stderr_bytes=100))
        assert error.value.code == "too_large"
        assert children and all(p.poll() is not None for p in children)
    finally:
        for process in children:
            if process.poll() is None: process.kill()
            process.wait(timeout=5)


def test_working_size_handles_git_removing_a_temporary_file(tmp_path, monkeypatch):
    transient = tmp_path / "pack.tmp"; transient.write_bytes(b"temporary")
    (tmp_path / "retained").write_bytes(b"abc")
    class Entry:
        def __init__(self,path):self.path=path
        def is_file(self):return True
        def stat(self):
            if self.path.name=="pack.tmp":self.path.unlink();raise FileNotFoundError()
            return self.path.stat()
    monkeypatch.setattr(Path,"rglob",lambda self,pattern:iter([Entry(transient),Entry(tmp_path/"retained")]))
    try:size=gh._size(tmp_path)
    except FileNotFoundError:size=None
    assert size==3 and not transient.exists()



def test_publication_never_replaces_existing_snapshot_manifest(local, monkeypatch):
    sentinel = b"existing manifest must never be replaced"; seeded = []
    original = store.Directory.open_dir
    def collision(directory, name, **kwargs):
        opened = original(directory, name, **kwargs)
        if name.startswith("snapshot-") and kwargs.get("create"):
            store.write_asset(opened, "history.json", [sentinel], len(sentinel))
            path = local[1]/"Owner"/"Repo"/"git-history"/name/"history.json"
            assert path.read_bytes() == sentinel
            seeded.append(path)
        return opened
    monkeypatch.setattr(store.Directory, "open_dir", collision)
    code = None
    try: archive(local)
    except gh.HistoryError as error: code = error.code
    assert len(seeded) == 1
    assert seeded[0].read_bytes() == sentinel, "publication replaced an existing snapshot file"
    assert code == "conflict"


def test_ambient_netrc_never_authenticates(local, tmp_path, monkeypatch):
    import base64
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    home=tmp_path/"ambient-home"; home.mkdir(); sentinel="sentinel-user:sentinel-password"
    for name in (".netrc","_netrc"):
        path=home/name; path.write_text("machine 127.0.0.1 login sentinel-user password sentinel-password\n"); path.chmod(0o600)
    for key in ("HOME","USERPROFILE","CURL_HOME","XDG_CONFIG_HOME","APPDATA"): monkeypatch.setenv(key,str(home))
    headers=[]
    class Challenge(BaseHTTPRequestHandler):
        def do_GET(self):
            headers.append(self.headers.get("Authorization")); self.send_response(401)
            self.send_header("WWW-Authenticate",'Basic realm="private"'); self.end_headers()
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Challenge); thread=threading.Thread(target=server.serve_forever); thread.start()
    monkeypatch.setattr(gh,"_remote_url",lambda repo:f"http://127.0.0.1:{server.server_port}/repo.git")
    monkeypatch.setattr(gh,"_test_protocols",("http",))
    try:
        with pytest.raises(gh.HistoryError) as error: archive(local)
        token="Basic "+base64.b64encode(sentinel.encode()).decode()
        assert headers and token not in headers and all(value is None for value in headers)
        assert error.value.code == "network" and not list(local[1].rglob("history.json"))
    finally: server.shutdown(); server.server_close(); thread.join(timeout=3); assert not thread.is_alive()


def test_process_home_and_proxy_credentials_are_isolated(local, monkeypatch):
    for key in ("HTTPS_PROXY","HTTP_PROXY","ALL_PROXY"): monkeypatch.setenv(key,"http://sentinel:password@127.0.0.1:1")
    monkeypatch.setenv("APPDATA","ambient"); monkeypatch.setenv("SSH_AUTH_SOCK","ambient")
    calls=[]; spawn=gh._spawn
    def recorded(args,**kw): calls.append((kw["cwd"],kw["env"])); return spawn(args,**kw)
    monkeypatch.setattr(gh,"_spawn",recorded); assert archive(local).status == "ARCHIVED"
    for cwd,env in calls:
        assert all(env.get(key)==cwd for key in ("HOME","USERPROFILE","APPDATA","CURL_HOME","XDG_CONFIG_HOME"))
        assert not any("PROXY" in key.upper() and "@" in value for key,value in env.items())
        assert "SSH_AUTH_SOCK" not in env


_DISK_CODES = (39,112,1295,0xc000007f,0xc0000802) if os.name=="nt" else (errno.ENOSPC,errno.EDQUOT)
@pytest.mark.parametrize("method", ["write","flush"])
@pytest.mark.parametrize("status", [*_DISK_CODES,13,122 if os.name=="nt" else 112])
def test_adapter_disk_full_classification_and_retention(local, monkeypatch, method, status):
    first=archive(local); saved=snapshot_bytes(first.snapshot)
    git(local[0],"update-ref","refs/heads/main",local[3](local[2]))
    def failed(*args,**kw): raise store.ArchiveError("os",status=status)
    monkeypatch.setattr(store.winfs.Handle,method,failed)
    with pytest.raises(gh.HistoryError) as error: archive(local)
    if error.value.code != ("disk_full" if status in _DISK_CODES else "unsafe_path"):
        print("adapter diagnostic", error.value.code, repr(error.value.__context__))
    assert error.value.code == ("disk_full" if status in _DISK_CODES else "unsafe_path")
    assert snapshot_bytes(first.snapshot)==saved and len(list(local[1].rglob("history.json")))==1


def test_bundle_verify_rejects_matching_heads_with_missing_prerequisite(local, tmp_path, monkeypatch):
    newer=local[3](local[2]); git(local[0],"update-ref","refs/heads/main",newer)
    git(local[0],"update-ref","refs/tags/v1",newer)
    thin=tmp_path/"thin.bundle"
    git(local[0],"bundle","create",str(thin),"--all","^"+local[2])
    heads=git(local[0],"bundle","list-heads",str(thin))
    assert set(heads.split("\n"))=={newer+" HEAD",newer+" refs/heads/main",newer+" refs/tags/v1"}
    scratch=tmp_path/"scratch"; git(tmp_path,"init","--bare",str(scratch))
    assert git(scratch,"bundle","verify",str(thin),check=False).returncode != 0
    run=gh._run_git; injected=[]
    def incremental(args,**kw):
        result=run(args,**kw)
        if "bundle" in args and "create" in args:
            shutil.copyfile(thin,args[args.index("create")+1]); injected.append(True)
        return result
    monkeypatch.setattr(gh,"_run_git",incremental)
    with pytest.raises(gh.HistoryError) as error: archive(local)
    assert injected and error.value.code=="invalid_objects" and not list(local[1].rglob("history.json"))


@pytest.mark.parametrize("operation", ["archive", "ls-remote", "version", "list-heads", "init"])
def test_hostile_ancestor_config_cannot_reach_git(local, tmp_path, monkeypatch, operation):
    import json, tempfile
    work=tmp_path/"hostile"; work.mkdir(); git(work,"init","--template=")
    decoy=tmp_path/"decoy"; git(tmp_path,"clone","--bare","--ref-format=reftable",str(local[0]),str(decoy))
    git(decoy,"update-ref","refs/heads/decoy",local[2])
    marker=tmp_path/"executed"; attack="!echo BAD > '"+marker.as_posix()+"'"
    hooks=tmp_path/"hooks";hooks.mkdir()
    for name in ("reference-transaction","post-checkout"):
        hook=hooks/name;hook.write_text("#!/bin/sh\necho BAD > '"+marker.as_posix()+"'\n");hook.chmod(0o755)
    git(work,"config","url."+decoy.as_uri()+".insteadOf",local[0].as_uri())
    for key,value in [("credential.helper",attack),("http.extraHeader","Authorization: sentinel"),
        ("core.hooksPath",str(hooks)),("core.fsmonitor",attack[1:]),("core.sshCommand",attack[1:])]: git(work,"config",key,value)
    nested=work/"temp";nested.mkdir()
    for key in ("TEMP","TMP","TMPDIR"):monkeypatch.setenv(key,str(nested))
    monkeypatch.setattr(tempfile,"tempdir",str(nested))
    trace=tmp_path/"git.trace"; spawn=gh._spawn
    def recorded(args,**kw):
        kw["env"]["GIT_TRACE"]=str(trace)  # Test-only observation after env isolation.
        return spawn(args,**kw)
    monkeypatch.setattr(gh,"_spawn",recorded)
    expected=git(local[0],"for-each-ref","--format=%(objectname) %(refname)")
    if operation=="archive":
        result=archive(local);doc=json.loads((result.snapshot/"history.json").read_text())
        assert {r["sha"]+" "+r["name"] for r in doc["refs"]}==set(expected.split("\n"))
    elif operation=="ls-remote":
        raw=gh._run_git(["ls-remote","--",local[0].as_uri()])
        assert "refs/heads/decoy" not in raw.decode() and "refs/heads/main" in raw.decode()
    else:
        # config is a direct probe of the same no--git-dir discovery context.
        if operation=="version": args=["--version"]
        elif operation=="init": args=["init","--bare","--template=",str(tmp_path/"new")]
        else:
            bundle=tmp_path/"probe.bundle";git(local[0],"bundle","create",str(bundle),"--all")
            args=["bundle","list-heads",str(bundle)]
        gh._run_git(args)
    assert not marker.exists()
    assert decoy.as_posix().casefold() not in trace.read_text().casefold(), "contacted decoy"
    # Read the active config through the same runner; exit 0 even when no values exist.
    raw=gh._run_git(["config","--list","--local"])
    assert b"insteadOf" not in raw and b"insteadof" not in raw and b"Authorization: sentinel" not in raw


@pytest.mark.parametrize("reason", ["size","deadline","cancel"])
def test_completed_child_still_enforces_limits(tmp_path, monkeypatch, reason):
    import io
    from types import SimpleNamespace
    threads=[];sizes=[];thread=threading.Thread;size=gh._size;cancel=threading.Event()
    def reader(*args,**kw):
        result=thread(*args,**kw);threads.append(result);return result
    def spawn(args,**kw):
        cwd=Path(kw["cwd"]);(cwd/"completed.bin").write_bytes(b"x"*2048 if reason=="size" else b"")
        def poll():
            for item in threads:item.join()  # Drained pipes: supervision loop cannot run.
            if reason=="cancel":cancel.set()
            return 0
        return SimpleNamespace(stdout=io.BytesIO(),stderr=io.BytesIO(),returncode=0,poll=poll,wait=lambda **kw:0)
    monkeypatch.setattr(gh.threading,"Thread",reader);monkeypatch.setattr(gh,"_spawn",spawn)
    monkeypatch.setattr(gh,"_WindowsJob",lambda:SimpleNamespace(assign=lambda p:None,close=lambda:None))
    monkeypatch.setattr(gh,"_size",lambda path:sizes.append(path) or size(path))
    with pytest.raises(gh.HistoryError) as error:
        gh._run_git(["--version"],cancel=cancel.is_set,limits=replace(gh.Limits(),max_working_bytes=1024,verify_seconds=0 if reason=="deadline" else 1))
    assert error.value.code==dict(size="too_large",deadline="timeout",cancel="cancelled")[reason]
    assert len(sizes)==(1 if reason=="size" else 0)


def test_completed_bundle_growth_rejected_before_publication(local, monkeypatch):
    run=gh._run_git;bound=128*1024
    def completed(args,**kw):
        raw=run(args,**kw)
        if "fetch" in args:assert gh._size(gh._state.work)<bound
        if "bundle" in args and "verify" in args:(gh._state.work/"late-growth").write_bytes(b"x"*(bound+1))
        return raw
    monkeypatch.setattr(gh,"_run_git",completed)
    with pytest.raises(gh.HistoryError) as error:archive(local,limits=replace(gh.Limits(),max_working_bytes=bound))
    assert error.value.code=="too_large" and not list(local[1].rglob("snapshot-*"))
