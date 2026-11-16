"""Manual, immutable history bundles. No checkout or downloaded code execution.

Cancellation is honored until manifest publication begins; that commit completes.
The private URL/protocol hooks exist solely for offline tests.
Progress callback failures abort as internal_error after normal cleanup.
"""
from contextlib import contextmanager, ExitStack, closing
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time

from covearchive.github.urls import RepositoryTarget
from covearchive.providers import validate_repository, canonical_url
from covearchive.jsondepth import check_json_depth
from . import store, naming


@dataclass(frozen=True)
class Limits:
    ls_remote_seconds: float = 120
    fetch_seconds: float = 1800
    bundle_seconds: float = 600
    verify_seconds: float = 600
    overall_seconds: float = 3600
    max_refs: int = 20_000
    max_stdout_bytes: int = 16 * 1024 * 1024
    max_stderr_bytes: int = 1024 * 1024
    max_working_bytes: int = 8 * 1024**3
    min_free_bytes: int = 64 * 1024**2


class HistoryError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__("Git history operation failed.")


@dataclass(frozen=True)
class HistoryResult:
    status: str
    snapshot: object = None
    ref_count: int = 0
    size: int = 0
    damaged: tuple = ()


@dataclass(frozen=True)
class Snapshot:
    path: Path
    status: str
    manifest: object = None


# Private, monkeypatch-only test seam. No runtime setting relaxes production policy.
_test_protocols = ()
_spawn = subprocess.Popen
_state = threading.local()
_snapshot_pattern = re.compile(r"snapshot-\d{8}T\d{6}Z--[0-9a-f]{12}\Z")
MAX_MANIFEST_BYTES = 16 * 1024**2
_sha = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_hash = re.compile(r"[0-9a-f]{64}\Z")
# Credential/config channels (GIT_*, SSH_*, XDG_*, APPDATA, CURL_HOME) are not
# inherited. CA paths remain explicit trust anchors; anonymous proxies remain
# usable, but proxy URLs containing userinfo are dropped. HOME lookups are private.
_env_keys = set("PATH SYSTEMROOT COMSPEC PATHEXT WINDIR TEMP TMP HTTPS_PROXY HTTP_PROXY NO_PROXY ALL_PROXY SSL_CERT_FILE SSL_CERT_DIR CURL_CA_BUNDLE".split())


def _validate(repository):
    try:
        return validate_repository(repository)
    except (ValueError, TypeError, AttributeError):
        raise HistoryError("invalid_repository") from None


def _remote_url(repository):
    return canonical_url(_validate(repository)) + ".git"


def _git_executable():
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not folder or not Path(folder).is_absolute(): continue
        for name in (("git.exe", "git.com") if os.name == "nt" else ("git",)):
            path = Path(folder) / name
            if path.is_file() and os.access(path, os.X_OK): return str(path.resolve())
    raise HistoryError("git_missing")


def _environment(home):
    env = {k: v for k, v in os.environ.items() if k.upper() in _env_keys}
    env = {k:v for k,v in env.items() if not (k.upper().endswith("_PROXY") and "@" in v)}
    for key in ("HOME","USERPROFILE","CURL_HOME","APPDATA","LOCALAPPDATA","XDG_CONFIG_HOME","XDG_CACHE_HOME","XDG_DATA_HOME","XDG_STATE_HOME"):
        env[key] = str(home)
    if os.name == "nt": env.update(HOMEDRIVE=home.drive, HOMEPATH=str(home)[len(home.drive):])
    env["PATH"] = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if p and Path(p).is_absolute())
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
        GIT_ALLOW_PROTOCOL=":".join(("https", *_test_protocols)), GIT_PROTOCOL_FROM_USER="0",
        GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0", LANG="C", LC_ALL="C",
        NoDefaultCurrentDirectoryInExePath="1")
    env["GIT_CEILING_DIRECTORIES"] = str(home.resolve().parent)
    return env


def _config():
    values = ["credential.helper=", "core.hooksPath=" + os.devnull, "core.fsmonitor=false",
        "core.symlinks=false", "protocol.allow=never", "protocol.https.allow=always",
        "http.followRedirects=false", "http.lowSpeedLimit=1024", "http.lowSpeedTime=120",
        "fetch.fsckObjects=true", "transfer.fsckObjects=true", "fetch.recurseSubmodules=false",
        "submodule.recurse=false", "gc.auto=0", "maintenance.auto=false",
        "core.logAllRefUpdates=false", "http.sslVerify=true"]
    values += [f"protocol.{p}.allow=always" for p in _test_protocols]
    return [part for value in values for part in ("-c", value)]


def _cancelled(cancel):
    return bool(cancel and (cancel() if callable(cancel) else cancel.is_set()))


def _check(cancel):
    if _cancelled(cancel): raise HistoryError("cancelled")
    if time.monotonic() >= getattr(_state, "deadline", float("inf")): raise HistoryError("timeout")


def _size(path):
    total = 0
    for entry in Path(path).rglob("*"):
        _check(getattr(_state, "cancel", None))
        try:
            if entry.is_file(): total += entry.stat().st_size
        except FileNotFoundError:
            pass  # Git atomically replaces/removes pack and reftable temporaries.
    return total


def _rmtree(path):
    # Cleanup must finish even after cancellation/deadline; never abandon temp packs.
    def writable(function, name, exc):
        os.chmod(name, stat.S_IWRITE | stat.S_IREAD); function(name)
    shutil.rmtree(path, onexc=writable)


@contextmanager
def _temporary():
    path = Path(tempfile.mkdtemp(prefix="covearchive-history-"))
    try: yield path
    finally: _rmtree(path)


class _WindowsJob:
    """Own the complete child tree. Popen has a tiny pre-assignment race.

    Assignment follows Popen immediately, before pipe readers or polling. Failure
    kills/reaps the parent and fails closed rather than proceeding unsupervised.
    """
    def __init__(self):
        import ctypes as C
        from ctypes import wintypes as W
        class Basic(C.Structure):
            _fields_ = [("ProcessTime", C.c_int64), ("JobTime", C.c_int64),
                ("LimitFlags", W.DWORD), ("MinimumWorkingSet", C.c_size_t),
                ("MaximumWorkingSet", C.c_size_t), ("ActiveProcesses", W.DWORD),
                ("Affinity", C.c_size_t), ("Priority", W.DWORD), ("Scheduling", W.DWORD)]
        class IO(C.Structure):
            _fields_ = [(name, C.c_uint64) for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]
        class Extended(C.Structure):
            _fields_ = [("Basic", Basic), ("IO", IO), ("ProcessMemory", C.c_size_t),
                ("JobMemory", C.c_size_t), ("PeakProcessMemory", C.c_size_t), ("PeakJobMemory", C.c_size_t)]
        self.api = C.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([C.c_void_p, W.LPCWSTR], W.HANDLE),
            "SetInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
            "AssignProcessToJobObject": ([W.HANDLE, W.HANDLE], W.BOOL),
            "TerminateJobObject": ([W.HANDLE, W.UINT], W.BOOL),
            "CloseHandle": ([W.HANDLE], W.BOOL)}
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name); function.argtypes = args; function.restype = result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle: raise HistoryError("internal_error")
        limits = Extended(); limits.Basic.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits)):
            self.close(); raise HistoryError("internal_error")

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            process.kill(); process.wait(timeout=10)
            raise HistoryError("internal_error")

    def terminate(self):
        if not self.api.TerminateJobObject(self.handle, 1): raise HistoryError("internal_error")

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.api.CloseHandle(handle): raise HistoryError("internal_error")


def _kill_tree(process, job=None):
    if job is not None: job.terminate()
    else:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    if process.poll() is None: process.kill()
    process.wait(timeout=10)


def _private_git(cwd, arguments):
    # No discovery: cwd starts empty (neither .git nor a bare repository).
    # Explicit --git-dir overrides the ceiling and always names our fresh mirror/
    # scratch. Init also receives its absolute destination as --git-dir.
    if any(cwd.iterdir()): raise HistoryError("unsafe_path")
    if any(arg.startswith("--git-dir=") for arg in arguments): return []
    if "init" in arguments: return ["--git-dir=" + str(Path(arguments[-1]).resolve())]
    control = cwd/"control.git"
    for name in ("objects", "refs"): (control/name).mkdir(parents=True)
    (control/"HEAD").write_text("ref: refs/heads/unborn\n")
    (control/"config").write_text("[core]\n bare = true\n")
    return ["--git-dir=" + str(control)]


# Config sources: system/global are null; HOME/XDG/AppData/CURL_HOME are private.
# Local/worktree config is only our empty control repo or freshly initialized
# mirror/scratch (empty template, no reuse, no commondir/alternates/worktrees).
# No accessible config contains include/includeIf, so chains cannot start.
# The allowlist drops ALL ambient GIT_*: CONFIG[_PARAMETERS/_COUNT/KEY_n/VALUE_n],
# DIR/WORK_TREE/COMMON_DIR, OBJECT_DIRECTORY/ALTERNATE_OBJECT_DIRECTORIES,
# NAMESPACE/REPLACE_REF_BASE, discovery/config redirections and SSH/askpass hooks.
# Explicit -c options are ours. PATH selects absolute Git; TEMP/TMP only locate
# empty cwd; proxy/CA variables cannot introduce Git config (proxy userinfo stripped).
def _run_git(arguments, *, cancel=None, limits=Limits()):
    _check(cancel)
    executable = getattr(_state, "executable", None) or _git_executable()
    command = next((a for a in arguments if a in ("ls-remote", "fetch", "bundle")), "other")
    seconds = {"ls-remote": limits.ls_remote_seconds, "fetch": limits.fetch_seconds,
        "bundle": limits.verify_seconds if "verify" in arguments else limits.bundle_seconds}.get(command, limits.verify_seconds)
    deadline = min(time.monotonic() + seconds, getattr(_state, "deadline", time.monotonic() + limits.overall_seconds))
    with _temporary() as cwd, ExitStack() as cleanup:
        isolation = _private_git(cwd, arguments)
        options = dict(cwd=str(cwd), env=_environment(cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        if os.name == "nt": options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else: options["start_new_session"] = True
        job = _WindowsJob() if os.name == "nt" else None
        if job is not None: cleanup.callback(job.close)
        try: process = _spawn([executable, *isolation, *_config(), *arguments], **options)
        except FileNotFoundError: raise HistoryError("git_missing") from None
        if job is not None:
            try: job.assign(process)
            except BaseException:
                process.stdout.close(); process.stderr.close()
                raise
        buffers = [bytearray(), bytearray()]; overflow = threading.Event()
        def drain(pipe, target, cap):
            try:
                while data := pipe.read(8192):
                    remaining = max(0, cap - len(target))
                    target.extend(data[:remaining])
                    if len(data) > remaining: overflow.set()
            finally: pipe.close()
        readers = [threading.Thread(target=drain, args=(pipe, buffers[i], cap)) for i, (pipe, cap) in enumerate(
            [(process.stdout, limits.max_stdout_bytes), (process.stderr, limits.max_stderr_bytes)])]
        for reader in readers: reader.start()
        next_size_poll = 0
        try:
            while process.poll() is None or any(t.is_alive() for t in readers):
                _check(cancel)
                if overflow.is_set(): raise HistoryError("too_large")
                if time.monotonic() >= deadline: raise HistoryError("timeout")
                if time.monotonic() >= next_size_poll:
                    if _size(getattr(_state, "work", cwd)) > limits.max_working_bytes: raise HistoryError("too_large")
                    next_size_poll = time.monotonic() + .5
                time.sleep(.03)
            if overflow.is_set(): raise HistoryError("too_large")
            process.wait()
        except BaseException:
            _kill_tree(process, job)
            raise
        finally:
            for reader in readers: reader.join(timeout=10)
        if process.returncode:
            detail = bytes(buffers[1]).decode("utf-8", "replace").lower()
            if "redirect" in detail or re.search(r"requested url returned error: 30[12378]\b", detail): code = "redirect"
            elif any(s in detail for s in ("no space", "disk full")): code = "disk_full"
            elif any(s in detail for s in ("not found", "does not appear to be a git repository", "could not read from remote")): code = "not_found"
            elif any(s in detail for s in ("fsck", "bad object", "missing tree", "pack has bad", "unpacker", "corrupt", "invalid object")): code = "invalid_objects"
            elif "ref-format" in detail or "unknown option" in detail: code = "git_unsupported"
            else: code = "network" if command in ("ls-remote", "fetch") else "invalid_objects"
            raise HistoryError(code)
        _check(cancel)
        if time.monotonic() >= deadline: raise HistoryError("timeout")
        if _size(getattr(_state, "work", cwd)) > limits.max_working_bytes: raise HistoryError("too_large")
        _check(cancel)
        if time.monotonic() >= deadline: raise HistoryError("timeout")
        return bytes(buffers[0])


def _digest(refs, head):
    raw = "".join(name + "\0" + sha + "\n" for name, sha in sorted(refs.items()))
    return hashlib.sha256((raw + "HEAD\0" + (head or "") + "\n").encode()).hexdigest()


def _git_lines(raw):
    """Git uses LF framing. Non-UTF-8 ref names are explicitly unsupported."""
    try: return [line for line in raw.decode("utf-8", "strict").split("\n") if line]
    except UnicodeDecodeError: raise HistoryError("invalid_objects") from None


def _refs(raw, limits, remote=False):
    refs = {}; head = None
    for line in _git_lines(raw):
        fields = line.split("\t") if remote else line.split(" ", 1)
        if len(fields) != 2: continue
        sha, name = fields
        if remote and name == "HEAD" and sha.startswith("ref: "):
            head = sha[5:]; continue
        if name.endswith("^{}") or not name.startswith(("refs/heads/", "refs/tags/")): continue
        if not _sha.fullmatch(sha): raise HistoryError("invalid_objects")
        refs[name] = sha
        if len(refs) > limits.max_refs: raise HistoryError("too_many_refs")
    return refs, head if head in refs else None


def _free(path, limits, extra=0):
    if shutil.disk_usage(path).free < limits.min_free_bytes + extra: raise HistoryError("disk_full")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError("duplicate")
        result[key] = value
    return result


def _parse_manifest(raw):
    try:
        if len(raw) > MAX_MANIFEST_BYTES: raise ValueError()
        text = raw.decode("utf-8"); check_json_depth(text)
        value = json.loads(text, object_pairs_hook=_pairs)
        keys = {"schema", "kind", "repository", "source_url", "created_utc", "git_version", "head", "refs", "refs_digest", "bundle", "notes"}
        if set(value) != keys or type(value["schema"]) is not int or value["schema"] != 1 or value["kind"] != "git-history": raise ValueError()
        identity = value["repository"]
        if set(identity) != {"provider", "host", "owner", "repo"}: raise ValueError()
        repository = _validate(RepositoryTarget(**identity))
        if value["source_url"] != canonical_url(repository): raise ValueError()
        if not isinstance(value["refs"], list) or not value["refs"] or len(value["refs"]) > 20000: raise ValueError()
        refs = {}
        for row in value["refs"]:
            if set(row) != {"name", "sha"} or not isinstance(row["name"], str) or not row["name"].startswith(("refs/heads/", "refs/tags/")) or not _sha.fullmatch(row["sha"]): raise ValueError()
            if row["name"] in refs: raise ValueError()
            refs[row["name"]] = row["sha"]
        if list(refs) != sorted(refs) or value["head"] is not None and value["head"] not in refs: raise ValueError()
        if value["refs_digest"] != _digest(refs, value["head"]): raise ValueError()
        bundle = value["bundle"]
        if set(bundle) != {"name", "size", "sha256"} or bundle["name"] != "history.bundle" or type(bundle["size"]) is not int or not 0 < bundle["size"] <= 2**53-1 or not _hash.fullmatch(bundle["sha256"]): raise ValueError()
        if value["notes"] != {"lfs_contents_included": False, "submodule_contents_included": False}: raise ValueError()
        if not isinstance(value["git_version"], str) or len(value["git_version"]) > 200: raise ValueError()
        stamp = datetime.strptime(value["created_utc"], "%Y-%m-%dT%H:%M:%SZ")
        if stamp.strftime("%Y-%m-%dT%H:%M:%SZ") != value["created_utc"]: raise ValueError()
        return value
    except HistoryError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, store.ArchiveError):
        raise HistoryError("corrupt") from None


def _same_repository(identity, repository):
    # Match fs fold_case=True: ASCII spelling folds; non-ASCII stays exact.
    same = lambda a,b: a.casefold()==b.casefold() if a.isascii() and b.isascii() else a==b
    return (identity["provider"]==repository.provider and identity["host"]==repository.host
        and same(identity["owner"],repository.owner) and same(identity["repo"],repository.repo))


def _bind_snapshot(doc, name, repository, limits):
    if repository is not None and not _same_repository(doc["repository"], repository): raise HistoryError("corrupt")
    expected = "snapshot-"+doc["created_utc"].replace("-", "").replace(":", "")+"--"+doc["refs_digest"][:12]
    if name != expected: raise HistoryError("corrupt")
    if len(doc["refs"]) > limits.max_refs: raise HistoryError("corrupt")
    return doc


def _manifest(directory, *, cancel=None, repository=None, snapshot_name=None, limits=Limits()):
    try:
        with directory.open_file("history.json") as file:
            info = file.info()
            if info.links != 1 or info.size > MAX_MANIFEST_BYTES: raise HistoryError("corrupt")
            parts = []; size = 0
            while True:
                _check(cancel)
                chunk = file.read(1024 * 1024)
                if not chunk: break
                size += len(chunk)
                if size > MAX_MANIFEST_BYTES: raise HistoryError("corrupt")
                parts.append(chunk)
            if file.info().size != size or info.size != size: raise HistoryError("corrupt")
            raw = b"".join(parts)
    except store.ArchiveError: raise HistoryError("corrupt") from None
    return _bind_snapshot(_parse_manifest(raw), snapshot_name or directory.name, repository, limits)


def _encode_manifest(doc):
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > MAX_MANIFEST_BYTES: raise HistoryError("too_large")
    try: _parse_manifest(raw)
    except (HistoryError, ValueError, TypeError): raise HistoryError("invalid_objects") from None
    return raw


def _verify(directory, doc, *, cancel=None, limits=Limits()):
    """Hash one pinned handle, checking cancellation/deadline between MiB reads."""
    deadline = min(time.monotonic() + limits.verify_seconds, getattr(_state, "deadline", float("inf")))
    def check():
        _check(cancel)
        if time.monotonic() >= deadline: raise HistoryError("timeout")
    b = doc["bundle"]; digest = hashlib.sha256(); size = 0
    try:
        check()
        with directory.open_file(b["name"]) as file:
            info = file.info()
            if info.links != 1 or info.size != b["size"]: raise HistoryError("corrupt")
            while True:
                check(); chunk = file.read(1024 * 1024); check()
                if not chunk: break
                size += len(chunk); digest.update(chunk)
                if size > b["size"]: raise HistoryError("corrupt")
            if size != b["size"] or file.info().size != size or digest.hexdigest() != b["sha256"]:
                raise HistoryError("corrupt")
        check()
    except store.ArchiveError: raise HistoryError("corrupt") from None


def _bundle_info(directory, doc):
    with directory.open_file("history.bundle") as file:
        info = file.info()
        if info.links != 1 or info.size != doc["bundle"]["size"]: raise HistoryError("corrupt")


def _validate_root(path):
    try:
        with store.ArchiveRoot.open(str(path)): pass
    except (store.ArchiveError, ValueError, TypeError, OSError):
        raise HistoryError("unsafe_path") from None


def _directory_path(directory):
    parts = []
    while not isinstance(directory, store.ArchiveRoot):
        parts.append(directory.name); directory = directory.parent
    # ArchiveRoot retains its absolute root basename through its pinned parent.
    return Path(getattr(_state, "root_path")).joinpath(*reversed(parts))


def _statuses(history, path, *, repository, verify=False, check_bundle=True, recover=False, cancel=None, limits=Limits()):
    result = []
    for name in sorted(history.names()):
        _check(cancel)
        if not name.startswith("snapshot-"): continue
        doc = None
        try:
            with history.open_dir(name) as directory:
                if "history.json" not in directory.names():
                    if recover: store.recover(directory)
                    _check(cancel); result.append(Snapshot(path/name, "incomplete")); continue
                doc = _manifest(directory, cancel=cancel, repository=repository, limits=limits)
                if check_bundle:
                    _verify(directory, doc, cancel=cancel, limits=limits) if verify else _bundle_info(directory, doc)
                result.append(Snapshot(path/name, "valid", doc))
        except HistoryError as error:
            if error.code in ("cancelled", "timeout"): raise
            result.append(Snapshot(path/name, "corrupt", doc))
        except store.ArchiveError: result.append(Snapshot(path/name, "corrupt", doc))
    return result


def _translate(error):
    if isinstance(error, HistoryError): return error
    disk_codes = (39,112,1295,0xc000007f,0xc0000802) if os.name=="nt" else (errno.ENOSPC,errno.EDQUOT)
    if isinstance(error, OSError) and (error.errno in (errno.ENOSPC,errno.EDQUOT) or getattr(error, "winerror", None) in (39,112,1295)): return HistoryError("disk_full")
    if isinstance(error, store.ArchiveError) and error.code=="os" and error.status in disk_codes: return HistoryError("disk_full")
    if isinstance(error, store.ArchiveUnsafePathError): return HistoryError("unsafe_path")
    if isinstance(error, store.ArchiveConflictError): return HistoryError("conflict")
    if isinstance(error, store.ArchiveError): return HistoryError("busy" if error.code == "busy" else "unsafe_path")
    return HistoryError("internal_error")


def archive_history(repository, archive_root, *, cancel=None, progress=None, limits=Limits()):
    repository = _validate(repository)
    _validate_root(archive_root)
    previous = dict(vars(_state))
    try:
        _state.cancel = cancel
        _state.deadline = time.monotonic() + limits.overall_seconds
        _state.executable = _git_executable(); _state.root_path = Path(archive_root)
        url = _remote_url(repository)
        lines = _git_lines(_run_git(["--version"], cancel=cancel, limits=limits))
        if len(lines)!=1: raise HistoryError("git_unsupported")
        version = lines[0]
        match = re.search(r"(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (2, 45): raise HistoryError("git_unsupported")
        if progress: progress("checking")
        advertised, head = _refs(_run_git(["ls-remote", "--symref", "--", url, "HEAD", "refs/heads/*", "refs/tags/*"], cancel=cancel, limits=limits), limits, True)
        if not advertised: raise HistoryError("empty_repository")
        _free(archive_root, limits)
        with store.ArchiveRoot.open(str(archive_root)) as root, store.ensure_project(root, repository) as project:
            with project.open_dir("git-history", create=True) as history, history.open_lock():
                history_path = _directory_path(history)
                store.recover(history)
                snapshots = _statuses(history, history_path, repository=repository, check_bundle=False, recover=True, cancel=cancel, limits=limits)
                valid = [s for s in snapshots if s.status == "valid"]
                damaged = tuple(s.path for s in snapshots if s.status == "corrupt")
                if valid and valid[-1].manifest["refs_digest"] == _digest(advertised, head):
                    latest = valid[-1]
                    try:
                        with history.open_dir(latest.path.name) as directory: _verify(directory, latest.manifest, cancel=cancel, limits=limits)
                    except HistoryError as error:
                        if error.code in ("cancelled", "timeout"): raise
                        damaged += (latest.path,)
                    except store.ArchiveError:
                        damaged += (latest.path,)
                    else:
                        _check(cancel)
                        return HistoryResult("UNCHANGED", latest.path, len(advertised), latest.manifest["bundle"]["size"], damaged)
                with _temporary() as work:
                    _state.work = work; _free(work, limits)
                    mirror = work/"mirror"; scratch = work/"verify"; bundle = work/"history.bundle"
                    run = lambda args: _run_git(args, cancel=cancel, limits=limits)
                    run(["init", "--bare", "--template=", "--ref-format=reftable", str(mirror)])
                    prefix = ["--git-dir=" + str(mirror)]
                    if progress: progress("downloading")
                    run([*prefix, "fetch", "--no-tags", "--no-recurse-submodules", "--", url, "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"])
                    refs, _ = _refs(run([*prefix, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/", "refs/tags/"]), limits)
                    if not refs: raise HistoryError("empty_repository")
                    head = head if head in refs else None
                    run([*prefix, "symbolic-ref", "HEAD", head or "refs/covearchive/unborn"])
                    # fetch.fsckObjects validates received objects/connectivity;
                    # bundle verification below checks the self-contained result.
                    if progress: progress("verifying")
                    # Fresh mirror: only explicit fetched namespaces exist. The fallback
                    # HEAD is outside those namespaces, so it is guaranteed unborn.
                    run([*prefix, "bundle", "create", str(bundle), "--all"])
                    actual = set(_git_lines(run(["bundle", "list-heads", str(bundle)])))
                    expected = {sha + " " + name for name, sha in refs.items()}
                    if head: expected.add(refs[head] + " HEAD")
                    if actual != expected: raise HistoryError("invalid_objects")
                    run(["init", "--bare", "--template=", "--ref-format=reftable", str(scratch)])
                    run(["--git-dir=" + str(scratch), "bundle", "verify", str(bundle)])
                    _check(cancel); _free(archive_root, limits, bundle.stat().st_size)
                    digest = _digest(refs, head)
                    # A damaged/incomplete directory is never reused, even in this second.
                    now = datetime.now(timezone.utc)
                    for snapshot in valid:
                        try: stamp = datetime.strptime(snapshot.path.name[9:25], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                        except ValueError: continue
                        if now.strftime("%Y%m%dT%H%M%SZ") <= stamp.strftime("%Y%m%dT%H%M%SZ"): now = stamp + timedelta(seconds=1)
                    while True:
                        name = "snapshot-" + now.strftime("%Y%m%dT%H%M%SZ") + "--" + digest[:12]
                        if name not in history.names(): break
                        now += timedelta(seconds=1)
                    doc = dict(schema=1, kind="git-history", repository=dict(provider=repository.provider,host=repository.host,owner=repository.owner,repo=repository.repo),
                        source_url=canonical_url(repository), created_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"), git_version=version, head=head,
                        refs=[dict(name=n,sha=s) for n,s in sorted(refs.items())], refs_digest=digest,
                        bundle=dict(name="history.bundle",size=2**53-1,sha256="0"*64),
                        notes=dict(lfs_contents_included=False,submodule_contents_included=False))
                    _encode_manifest(doc)  # Worst-width bundle fields: validate before any publication.
                    if progress: progress("publishing")
                    if _size(work) > limits.max_working_bytes: raise HistoryError("too_large")
                    with history.open_dir(name, create=True) as destination:
                        def chunks():
                            with bundle.open("rb") as file:
                                while data := file.read(1024*1024):
                                    _check(cancel); yield data
                        with closing(chunks()) as stream:
                            published = store.write_asset(destination, "history.bundle", stream, bundle.stat().st_size)
                        doc["bundle"].update(size=published.size,sha256=published.local_sha256)
                        raw = _encode_manifest(doc)
                        _check(cancel)
                        store.write_asset(destination, "history.json", [raw], len(raw))
                    return HistoryResult("ARCHIVED", history_path/name, len(refs), published.size, damaged)
    except Exception as error:
        raise _translate(error) from None
    finally:
        vars(_state).clear(); vars(_state).update(previous)


def history_status(repository, archive_root, *, verify=False, cancel=None, limits=Limits()):
    repository = _validate(repository)
    _validate_root(archive_root)
    previous = dict(vars(_state))
    try:
        _state.deadline = time.monotonic() + min(limits.overall_seconds, limits.verify_seconds)
        _check(cancel)
        with ExitStack() as stack:
            directory = stack.enter_context(store.ArchiveRoot.open(str(archive_root))); path = Path(archive_root)
            for component in (*naming.project_components(repository), "git-history"):
                directory = stack.enter_context(directory.open_dir(component, fold_case=True)); path /= directory.name
            return _statuses(directory, path, repository=repository, verify=verify, cancel=cancel, limits=limits)
    except store.ArchiveError as error:
        if error.code == "missing": return []
        raise _translate(error) from None
    finally:
        vars(_state).clear(); vars(_state).update(previous)


def verify_snapshot(snapshot, *, repository=None, cancel=None, limits=Limits()):
    if repository is not None: repository = _validate(repository)
    previous = dict(vars(_state))
    try:
        _state.deadline = time.monotonic() + min(limits.overall_seconds, limits.verify_seconds)
        _check(cancel)
        with store.ArchiveRoot.open(str(snapshot)) as directory:
            doc = _manifest(directory, cancel=cancel, repository=repository, snapshot_name=Path(snapshot).name, limits=limits)
            _verify(directory, doc, cancel=cancel, limits=limits)
        return True
    except Exception as error:
        raise _translate(error) from None
    finally:
        vars(_state).clear(); vars(_state).update(previous)
