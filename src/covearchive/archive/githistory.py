"""Manual immutable bundles and incremental mirrors; no checkout/code execution.

Cancellation is honored until manifest publication begins; that commit completes.
The private URL/protocol hooks exist solely for offline tests.
Progress callback failures abort as internal_error after normal cleanup.
"""
from contextlib import contextmanager, ExitStack, closing
from dataclasses import dataclass, replace
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
import uuid

from covearchive.github.urls import RepositoryTarget
from covearchive.providers import validate_repository, canonical_url
from covearchive.jsondepth import check_json_depth
from . import store, naming, gitsecurity


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
    max_mirror_entries: int = 1_000_000
    max_mirror_metadata_bytes: int = 64 * 1024**2
    # Read access must survive an update crossing its smaller ingestion budget.
    # These independent hard limits still bound verification/export work.
    max_mirror_read_bytes: int = 64 * 1024**3
    max_mirror_read_entries: int = 4_000_000
    max_captures: int = 10_000


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
_test_windows_acl_reader = None
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


def _working_size(default):
    path = getattr(_state, "work", default)
    extra = getattr(_state, "extra_work", None)
    return _size(path) + (_size(extra) if extra is not None and extra != path else 0)


def _check_working(default, limits):
    if _working_size(default) > limits.max_working_bytes: raise HistoryError("too_large")
    repository = getattr(_state, "mirror_fetch_path", None)
    if repository is not None:
        for count, _ in enumerate(repository.rglob("*"), 1):
            _check(getattr(_state, "cancel", None))
            if count > limits.max_mirror_entries: raise HistoryError("too_large")


def _rmtree(path):
    # Cleanup must finish even after cancellation/deadline; never abandon temp packs.
    def writable(function, name, exc):
        os.chmod(name, stat.S_IWRITE | stat.S_IREAD); function(name)
    deadline = time.monotonic() + 3
    while True:
        try:
            shutil.rmtree(path, onexc=writable)
            return
        except OSError as exc:
            # Dying children, antivirus, and indexers can briefly hold Windows handles.
            if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 145) or not Path(path).exists():
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(.05, remaining))


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
        # Give Git's signal handlers a brief opportunity to release its locks.
        # Kill the entire group afterward even if the parent already exited.
        try: os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: process.wait(timeout=.3)
        except subprocess.TimeoutExpired: pass
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    if process.poll() is None: process.kill()
    process.wait(timeout=10)


def _private_git(cwd, arguments):
    # No discovery: cwd starts empty (neither .git nor a bare repository).
    # Explicit --git-dir names a fresh scratch repo or an allowlisted persistent
    # mirror. Init also receives its absolute destination as --git-dir.
    if any(cwd.iterdir()): raise HistoryError("unsafe_path")
    if any(arg.startswith("--git-dir=") for arg in arguments): return []
    if "init" in arguments: return ["--git-dir=" + str(Path(arguments[-1]).resolve())]
    control = cwd/"control.git"
    for name in ("objects", "refs"): (control/name).mkdir(parents=True)
    (control/"HEAD").write_text("ref: refs/heads/unborn\n")
    (control/"config").write_text("[core]\n bare = true\n")
    return ["--git-dir=" + str(control)]


# Config sources: system/global are null; HOME/XDG/AppData/CURL_HOME are private.
# Local config is our empty control repo, fresh scratch, or a persistent mirror
# whose exact config and structure have passed _trust_repository before Git.
# No accessible config contains include/includeIf, so chains cannot start.
# The allowlist drops ALL ambient GIT_*: CONFIG[_PARAMETERS/_COUNT/KEY_n/VALUE_n],
# DIR/WORK_TREE/COMMON_DIR, OBJECT_DIRECTORY/ALTERNATE_OBJECT_DIRECTORIES,
# NAMESPACE/REPLACE_REF_BASE, discovery/config redirections and SSH/askpass hooks.
# Explicit -c options are ours. PATH selects absolute Git; TEMP/TMP only locate
# empty cwd; proxy/CA variables cannot introduce Git config (proxy userinfo stripped).
def _run_git(arguments, *, cancel=None, limits=Limits(), input_data=None, truncate_stderr=False):
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
        if input_data is not None:
            if len(input_data) > limits.max_stdout_bytes: raise HistoryError("too_large")
            stream = cleanup.enter_context(tempfile.TemporaryFile())
            stream.write(input_data); stream.seek(0); options["stdin"] = stream
        if os.name == "nt": options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else: options.update(start_new_session=True, umask=0o077)
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
        def drain(pipe, target, cap, allow_extra=False):
            try:
                while data := pipe.read(8192):
                    remaining = max(0, cap - len(target))
                    target.extend(data[:remaining])
                    if len(data) > remaining and not allow_extra: overflow.set()
            finally: pipe.close()
        readers = [threading.Thread(target=drain, args=(pipe, buffers[i], cap, i == 1 and truncate_stderr)) for i, (pipe, cap) in enumerate(
            [(process.stdout, limits.max_stdout_bytes), (process.stderr, limits.max_stderr_bytes)])]
        for reader in readers: reader.start()
        next_size_poll = 0; quiescent = False
        try:
            while process.poll() is None or any(t.is_alive() for t in readers):
                _check(cancel)
                if overflow.is_set(): raise HistoryError("too_large")
                if time.monotonic() >= deadline: raise HistoryError("timeout")
                if time.monotonic() >= next_size_poll:
                    _check_working(cwd, limits)
                    next_size_poll = time.monotonic() + .5
                time.sleep(.03)
            if overflow.is_set(): raise HistoryError("too_large")
            process.wait()
            quiescent = True
        except BaseException:
            _kill_tree(process, job)
            quiescent = True
            raise
        finally:
            for reader in readers: reader.join(timeout=10)
            _state.git_quiescent = quiescent and not any(reader.is_alive() for reader in readers)
        if process.returncode:
            detail = bytes(buffers[1]).decode("utf-8", "replace").lower()
            if "redirect" in detail or re.search(r"requested url returned error: 30[12378]\b", detail): code = "redirect"
            elif any(s in detail for s in ("no space", "disk full")): code = "disk_full"
            elif any(s in detail for s in ("not found", "does not appear to be a git repository", "could not read from remote")): code = "not_found"
            elif any(s in detail for s in ("ref-format", "unknown option", "unknown repository extension", "unknown ref storage", "unsupported repository version")): code = "git_unsupported"
            elif any(s in detail for s in ("fsck", "bad object", "missing tree", "pack has bad", "unpacker", "corrupt", "invalid object")): code = "invalid_objects"
            else: code = "network" if command in ("ls-remote", "fetch") else "invalid_objects"
            raise HistoryError(code)
        _check(cancel)
        if time.monotonic() >= deadline: raise HistoryError("timeout")
        _check_working(cwd, limits)
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


# Persistent repositories are untrusted input on every reopen. No config parser
# (and especially no Git config command) runs before this exact byte comparison.
_MIRROR_CONFIG = b"[core]\n\trepositoryformatversion = 1\n\tbare = true\n[extensions]\n\trefstorage = reftable\n"
_capture_pattern = re.compile(r"capture-[0-9a-f]{32}\.json\Z")


@dataclass(frozen=True)
class MirrorResult:
    status: str
    path: Path
    capture: object = None
    ref_count: int = 0
    created_utc: object = None


def _read_mirror_file(directory, name):
    with directory.open_file(name, strict_links=True) as file:
        if file.info().size > MAX_MANIFEST_BYTES: raise HistoryError("corrupt")
        chunks = []; size = 0
        while data := file.read(1024 * 1024):
            _check(getattr(_state, "cancel", None))
            size += len(data)
            if size > MAX_MANIFEST_BYTES: raise HistoryError("corrupt")
            chunks.append(data)
        if size != file.info().size: raise HistoryError("corrupt")
        return b"".join(chunks)


def _mirror_json(raw):
    try:
        text = raw.decode("utf-8"); check_json_depth(text)
        return json.loads(text, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError): raise HistoryError("corrupt") from None


def _mirror_identity(repository):
    return dict(provider=repository.provider, host=repository.host, owner=repository.owner, repo=repository.repo)


def _mirror_identity_matches(identity, repository):
    return (isinstance(identity, dict) and set(identity) == {"provider", "host", "owner", "repo"}
        and all(isinstance(value, str) for value in identity.values()) and _same_repository(identity, repository))


def _capture_document(raw, name, repository, limits):
    doc = _mirror_json(raw)
    try:
        if set(doc) != {"schema", "kind", "repository", "capture", "created_utc", "head", "refs", "refs_digest"}: raise ValueError()
        if type(doc["schema"]) is not int or doc["schema"] != 1 or doc["kind"] != "git-mirror-capture": raise ValueError()
        if not _mirror_identity_matches(doc["repository"], repository): raise ValueError()
        if doc["capture"] != name or not _capture_pattern.fullmatch(name): raise ValueError()
        stamp = datetime.strptime(doc["created_utc"], "%Y-%m-%dT%H:%M:%S.%fZ")
        if stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != doc["created_utc"]: raise ValueError()
        refs = doc["refs"]
        if not isinstance(refs, dict) or len(refs) > limits.max_refs: raise ValueError()
        for ref, sha in refs.items():
            # Git has validated the names at fetch; reject framing/control bytes
            # before using a manifest to construct commands or update-ref input.
            if not ref.startswith(("refs/heads/", "refs/tags/")) or any(ord(c) < 33 or ord(c) == 127 for c in ref): raise ValueError()
            if not isinstance(sha, str) or not _sha.fullmatch(sha): raise ValueError()
        if doc["head"] is not None and doc["head"] not in refs: raise ValueError()
        if doc["refs_digest"] != _digest(refs, doc["head"]): raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError): raise HistoryError("corrupt") from None
    return doc


def _trust_ancestors(path, archive_root):
    """Exclude foreign-user pathname substitution, including above the root.

    System-root-owned ancestors (e.g. sticky /tmp) are trusted; the authorized
    archive and everything below it must belong to this user. Same-user races
    remain outside the adapter's boundary.
    """
    for current in (path, *path.parents):
        if current == archive_root or archive_root in current.parents: _trust_acl(current)
    if os.name == "nt": return  # NTFS handles pin names; ACLs above protect contents.
    system_owner = Path(path.anchor).lstat().st_uid  # Root can be UID-mapped in a sandbox.
    for current in reversed((path, *path.parents)):
        info = current.lstat()
        within = current == archive_root or archive_root in current.parents
        owners = {os.getuid()} if within else {0, system_owner, os.getuid()}
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            raise HistoryError("unsafe_path")


def _trust_acl(path):
    try: gitsecurity.check_path(path, reader=_test_windows_acl_reader)
    except gitsecurity.SecurityError as error: raise HistoryError(error.code) from None


def _trust_metadata(path, *, directory=False, recovery_alias=False):
    info = path.lstat()
    if directory:
        if not stat.S_ISDIR(info.st_mode): raise HistoryError("unsafe_path")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink not in ((1, 2) if recovery_alias else (1,)):
        raise HistoryError("unsafe_path")
    if os.name != "nt" and (info.st_uid != os.getuid()
            or info.st_mode & 0o022 and not (directory and info.st_mode & stat.S_ISVTX)):
        raise HistoryError("unsafe_path")
    _trust_acl(path)
    return info


def _require_mirror_git(cancel, limits):
    _state.executable = _git_executable()
    lines = _git_lines(_run_git(["--version"], cancel=cancel, limits=limits))
    match = re.search(r"(\d+)\.(\d+)", lines[0]) if len(lines) == 1 else None
    if not match or tuple(map(int, match.groups())) < (2, 45): raise HistoryError("git_unsupported")


def _trust_repository(directory, path, *, cancel, limits):
    """Allow only our bare reftable layout, owned regular files, no redirects.

    Pinned archive handles reject links/reparse points; lstat additionally checks
    ownership and POSIX write permissions. Same-user concurrent replacement is
    outside the filesystem adapter's security boundary, as for archive bundles.
    """
    total = 0; entries = 0; table_names = set()
    def walk(current, relative, depth=0):
        nonlocal total, entries, table_names
        _check(cancel)
        if depth > 4: raise HistoryError("corrupt")
        info = (path / relative).lstat()
        _trust_acl(path / relative)
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o022): raise HistoryError("corrupt")
        names = current.names(limit=max(0, limits.max_mirror_entries - entries))
        entries += len(names)
        if entries > limits.max_mirror_entries: raise HistoryError("too_large")
        if relative.parts == ("reftable",): table_names = set(names)
        for name in names:
            _check(cancel)
            item = relative / name; parts = item.parts
            info = (path / item).lstat()
            _trust_acl(path / item)
            if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o022): raise HistoryError("corrupt")
            is_dir = stat.S_ISDIR(info.st_mode)
            allowed = False
            if not relative.parts:
                allowed = (is_dir and name in {"objects", "refs", "reftable"}) or (not is_dir and name in {"config", "HEAD"})
            elif parts[0] == "objects":
                if len(parts) == 2: allowed = is_dir and (name in {"info", "pack"} or re.fullmatch(r"[0-9a-f]{2}", name))
                elif len(parts) == 3 and parts[1] == "pack": allowed = not is_dir and bool(re.fullmatch(r"(?:pack-[0-9a-f]{40,64}\.(?:pack|idx|rev|keep)|tmp_pack_[A-Za-z0-9]+)", name))
                elif len(parts) == 3 and re.fullmatch(r"[0-9a-f]{2}", parts[1]): allowed = not is_dir and bool(re.fullmatch(r"[0-9a-f]{38}|[0-9a-f]{62}|tmp_obj_[A-Za-z0-9]+", name))
            elif parts[0] == "refs": allowed = len(parts) == 2 and name == "heads" and not is_dir
            elif parts[0] == "reftable": allowed = len(parts) == 2 and not is_dir and (name == "tables.list" or bool(re.fullmatch(r"0x[0-9a-f]+-0x[0-9a-f]+-[0-9a-f]+\.ref", name)))
            if not allowed or stat.S_ISLNK(info.st_mode): raise HistoryError("corrupt")
            if is_dir:
                with current.open_dir(name) as child: walk(child, item, depth + 1)
            else:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: raise HistoryError("corrupt")
                if not getattr(_state, "mirror_export_only", False) or parts[0] != "objects": total += info.st_size
                if total > limits.max_working_bytes: raise HistoryError("too_large")
        current.info()
    walk(directory, Path())
    if set(directory.names()) != {"objects", "refs", "reftable", "config", "HEAD"}: raise HistoryError("corrupt")
    if _read_mirror_file(directory, "config") != _MIRROR_CONFIG: raise HistoryError("corrupt")
    if _read_mirror_file(directory, "HEAD") != b"ref: refs/heads/.invalid\n": raise HistoryError("corrupt")
    with directory.open_dir("refs") as refs:
        if _read_mirror_file(refs, "heads") != b"this repository uses the reftable format\n": raise HistoryError("corrupt")
    with directory.open_dir("reftable") as tables:
        listed = _read_mirror_file(tables, "tables.list").decode("ascii").splitlines()
        if len(listed) > limits.max_mirror_entries: raise HistoryError("too_large")
        if not listed: raise HistoryError("corrupt")
        seen = set()
        for name in listed:
            _check(cancel)
            if name in seen or name not in table_names or not re.fullmatch(r"0x[0-9a-f]+-0x[0-9a-f]+-[0-9a-f]+\.ref", name): raise HistoryError("corrupt")
            seen.add(name)


def _cleanup_fetch_transients(repo, path, before, limits, *, objects_before=None):
    """After a reaped fetch only: remove new unlisted Git lock/temp files.

    Cleanup intentionally ignores the expired operation's cancel/deadline,
    but enumeration and input remain bounded. Never remove ref tables, objects,
    listed files, pre-existing entries, links, directories or foreign-owned files.
    """
    with repo.open_dir("reftable") as tables:
        with tables.open_file("tables.list", strict_links=True) as file:
            listed = set(file.read_bounded(MAX_MANIFEST_BYTES).decode("ascii").splitlines())
        names = tables.names(limit=limits.max_mirror_entries)
        if len(names) > limits.max_mirror_entries: raise HistoryError("too_large")
        for name in set(names) - before - listed:
            if not re.fullmatch(r"(?:tables\.list|0x[0-9a-f]+-0x[0-9a-f]+-[0-9a-f]+\.ref)(?:\.lock|\.temp\.[A-Za-z0-9._-]+)", name): continue
            target = path / "reftable" / name
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: continue
            if os.name != "nt" and info.st_uid != os.getuid(): continue
            with tables.open_file(name, strict_links=True) as file:
                file.info()  # Use the adapter's native checks on POSIX and NTFS.
            tables.info(); target.unlink()
    if objects_before is not None:
        with repo.open_dir("objects") as objects:
            names = objects.names(limit=limits.max_mirror_read_entries)
            if len(names) > limits.max_mirror_read_entries: raise HistoryError("too_large")
            for name in set(names) - objects_before:
                if not re.fullmatch(r"(?:tmp_objdir-incoming-|incoming-|tmp_objdir-)[A-Za-z0-9]+", name): continue
                target = path / "objects" / name
                info = target.lstat()
                if not stat.S_ISDIR(info.st_mode): continue
                entries = [0]
                def owned(current, location, depth=0):
                    if depth > 4: return False
                    info = location.lstat()
                    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o022): return False
                    names = current.names(limit=max(0, limits.max_mirror_read_entries - entries[0]))
                    entries[0] += len(names)
                    if entries[0] > limits.max_mirror_read_entries: raise HistoryError("too_large")
                    for child in names:
                        child_path = location / child; info = child_path.lstat()
                        if os.name != "nt" and info.st_uid != os.getuid(): return False
                        if stat.S_ISDIR(info.st_mode):
                            with current.open_dir(child) as directory:
                                if not owned(directory, child_path, depth + 1): return False
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                            with current.open_file(child, strict_links=True) as file: file.info()
                        else: return False
                    current.info()
                    return True
                with objects.open_dir(name) as quarantine:
                    safe = owned(quarantine, target)
                if safe:
                    objects.info(); _rmtree(target)


def _mirror_documents(mirror, repository, limits, selected=None):
    if set(mirror.names()) - {"repository.git", "identity.json", "captures", ".covearchive.lock"}: raise HistoryError("corrupt")
    metadata_path = _directory_path(mirror)
    _trust_metadata(metadata_path / "identity.json")
    if ".covearchive.lock" in mirror.names(): _trust_metadata(metadata_path / ".covearchive.lock")
    identity = _mirror_json(_read_mirror_file(mirror, "identity.json"))
    if not _mirror_identity_matches(identity, repository): raise HistoryError("corrupt")
    documents = []; size = 0
    with mirror.open_dir("captures") as captures:
        _trust_metadata(metadata_path / "captures", directory=True)
        names = captures.names(limit=limits.max_mirror_entries)
        if len(names) > limits.max_mirror_entries: raise HistoryError("too_large")
        infos = {}
        for name in names:
            _check(getattr(_state, "cancel", None))
            if not (_capture_pattern.fullmatch(name) or store.winfs.TEMP_PATTERN.fullmatch(name)): raise HistoryError("corrupt")
            infos[name] = _trust_metadata(metadata_path / "captures" / name, recovery_alias=os.name != "nt")
        # The adapter's interrupted link publication is the only temporary
        # two-link exception. Prove the exact owned record/temp pair first;
        # recovery removes its temp, then every read requires one link.
        aliases = {}
        for name, info in infos.items():
            if info.st_nlink == 2: aliases.setdefault((info.st_dev, info.st_ino), []).append(name)
        for pair in aliases.values():
            if len(pair) != 2 or sum(bool(_capture_pattern.fullmatch(name)) for name in pair) != 1:
                raise HistoryError("unsafe_path")
        store.recover(captures)
        if selected is not None:
            if not isinstance(selected, str) or not _capture_pattern.fullmatch(selected) or selected not in infos: raise HistoryError("missing_capture")
            try:
                _trust_metadata(metadata_path / "captures" / selected)
                raw = _read_mirror_file(captures, selected)
            except store.ArchiveError as error:
                if error.code == "missing": raise HistoryError("missing_capture") from None
                raise
            if len(raw) > limits.max_mirror_metadata_bytes: raise HistoryError("too_large")
            return [_capture_document(raw, selected, repository, limits)]
        for name in captures.names():
            _check(getattr(_state, "cancel", None))
            if name.startswith(".covearchive-tmp-") and name.endswith(".part"): continue
            if not _capture_pattern.fullmatch(name): raise HistoryError("corrupt")
            _trust_metadata(metadata_path / "captures" / name)
            raw = _read_mirror_file(captures, name); size += len(raw)
            if size > limits.max_mirror_metadata_bytes: raise HistoryError("too_large")
            documents.append(_capture_document(raw, name, repository, limits))
            if len(documents) > limits.max_captures: raise HistoryError("too_large")
    return sorted(documents, key=lambda d: (d["created_utc"], d["capture"]))


def _capture_ref(name, ref):
    return "refs/covearchive/captures/" + name[8:-5] + "/" + ref[5:]


def _verify_captures(run, documents, *, check_objects=True):
    pattern = "refs/covearchive/captures/" + "[0-9a-f]" * 32
    if check_objects and run(["for-each-ref", "--count=1", "--exclude=" + pattern + "/heads/**",
            "--exclude=" + pattern + "/tags/**", "--format=%(refname)"]): raise HistoryError("corrupt")
    refs = {}
    for doc in documents:
        _check(getattr(_state, "cancel", None))
        prefix = "refs/covearchive/captures/" + doc["capture"][8:-5] + "/"
        actual = {}
        for line in _git_lines(run(["for-each-ref", "--format=%(objectname) %(refname)", prefix])):
            _check(getattr(_state, "cancel", None))
            sha, name = line.split(" ", 1); actual[name] = sha
        expected = {_capture_ref(doc["capture"], ref): sha for ref, sha in doc["refs"].items()}
        if actual != expected: raise HistoryError("corrupt")
        refs.update(actual)
    if check_objects: run(["fsck", "--full", "--strict", "--no-reflogs", "--no-dangling"], truncate_stderr=True)
    return refs


def _initialize_mirror(mirror, repository, run):
    # Caller supplies a private staging sibling, never the permanent mirror.
    with _temporary() as work:
        fresh = work / "repository.git"
        run(["init", "--bare", "--template=", "--ref-format=reftable", str(fresh)])
        (fresh / "config").write_bytes(_MIRROR_CONFIG)
        def copy(source, destination):
            for entry in source.iterdir():
                if entry.is_dir():
                    with destination.open_dir(entry.name, create=True) as child: copy(entry, child)
                else:
                    data = entry.read_bytes(); store.write_asset(destination, entry.name, [data], len(data))
        with mirror.open_dir("repository.git", create=True) as repo: copy(fresh, repo)
    with mirror.open_dir("captures", create=True): pass
    raw = json.dumps(_mirror_identity(repository), sort_keys=True).encode()
    store.write_asset(mirror, "identity.json", [raw], len(raw))


def _check_directory_binding(directory, path, *, code="unsafe_path"):
    # Use the backend's native identity on both POSIX and Windows, not a
    # pathname-only check or incompatible stat/native Windows file IDs.
    try:
        with store.ArchiveRoot.open(str(path)) as checked:
            directory.info()
            if directory.identity != checked.identity: raise HistoryError(code)
    except store.ArchiveError:
        raise HistoryError(code) from None


@contextmanager
def _mirror_root(archive_root):
    with ExitStack() as stack:
        try:
            # Pin before resolving: even an ancestor retarget during resolve
            # must agree with the handle on which the operation will run.
            root = stack.enter_context(store.ArchiveRoot.open(str(archive_root)))
            path = Path(archive_root).resolve(strict=True)
            _trust_ancestors(path, path)
            _check_directory_binding(root, path)
        except HistoryError: raise
        except (store.ArchiveError, OSError, RuntimeError, TypeError, ValueError):
            raise HistoryError("unsafe_path") from None
        yield path, root


@contextmanager
def _mirror_context(repository, archive_root, *, cancel, limits, create=False, selected=None, for_export=False, bound_root=None):
    repository = _validate(repository)
    previous = dict(vars(_state)); exists = False
    try:
        _state.cancel = cancel
        _state.deadline = time.monotonic() + limits.overall_seconds
        _state.mirror_export_only = for_export
        _state.extra_work = None
        _check(cancel)
        with ExitStack() as stack:
            _state.root_path, project = bound_root if bound_root is not None else stack.enter_context(_mirror_root(archive_root))
            if bound_root is not None: _check_directory_binding(project, _state.root_path)
            if create: project = stack.enter_context(store.ensure_project(project, repository))
            else:
                for component in naming.project_components(repository):
                    project = stack.enter_context(project.open_dir(component, fold_case=True))
            with project.open_dir("git-history", create=create) as history, history.open_lock():
                history_path = _directory_path(history); path = history_path / "mirror"
                _trust_ancestors(history_path, _state.root_path)
                _trust_metadata(history_path / ".covearchive.lock")
                exists = "mirror" in history.names()
                empty = False
                if exists:
                    with history.open_dir("mirror") as probe:
                        _trust_ancestors(path, _state.root_path)
                        empty = not probe.names(limit=0)
                if (not exists or empty) and create:
                    _require_mirror_git(cancel, limits); _free(_state.root_path, limits)
                    staging = Path(tempfile.mkdtemp(prefix="mirror-init-", dir=history_path))
                    try:
                        _state.work = staging
                        with history.open_dir(staging.name) as prepared:
                            _initialize_mirror(prepared, repository, lambda args: _run_git(args, cancel=cancel, limits=limits))
                        _check(cancel); history.info(); _trust_ancestors(history_path, _state.root_path)
                        if empty: path.rmdir()  # Only an empty, owned uninitialized directory.
                        os.rename(staging, path)
                        exists = True
                    finally:
                        if staging.exists(): _rmtree(staging)
                if not exists or empty and not create: raise HistoryError("missing_mirror")
                with history.open_dir("mirror") as mirror:
                    _state.work = path if create else stack.enter_context(_temporary())
                    _trust_ancestors(path, _state.root_path)
                    documents = _mirror_documents(mirror, repository, limits, selected)
                    with mirror.open_dir("repository.git") as repo:
                        trusted = False
                        def run(args, **kwargs):
                            nonlocal trusted
                            mirror.info(); history.info()
                            _trust_ancestors(path, _state.root_path)
                            if not trusted:
                                _trust_repository(repo, path / "repository.git", cancel=cancel, limits=limits)
                                trusted = True
                            before = None; objects_before = None
                            if "fetch" in args:
                                with repo.open_dir("reftable") as tables: before = set(tables.names(limit=limits.max_mirror_entries))
                                with repo.open_dir("objects") as objects: objects_before = set(objects.names(limit=limits.max_mirror_entries))
                                _state.mirror_fetch_path = path / "repository.git"
                                _state.git_quiescent = False
                            try:
                                return _run_git(["--git-dir=" + str(path / "repository.git"), "-c", "core.fsync=all", *args], cancel=cancel, limits=limits, **kwargs)
                            except HistoryError as error:
                                if before is not None and getattr(_state, "git_quiescent", False):
                                    mirror.info(); history.info(); _trust_ancestors(path, _state.root_path)
                                    _cleanup_fetch_transients(repo, path / "repository.git", before, limits, objects_before=objects_before)
                                raise
                            finally:
                                if before is not None:
                                    _state.mirror_fetch_path = None; trusted = False
                        # Trust validation precedes even --version / ls-remote.
                        _trust_repository(repo, path / "repository.git", cancel=cancel, limits=limits)
                        trusted = True
                        _require_mirror_git(cancel, limits)
                        yield mirror, path, documents, run
    except Exception as error:
        if isinstance(error, (FileNotFoundError, UnicodeError, ValueError)): raise HistoryError("corrupt") from None
        if isinstance(error, store.ArchiveError) and error.code == "missing": raise HistoryError("corrupt" if exists or create else "missing_mirror") from None
        raise _translate(error) from None
    finally:
        vars(_state).clear(); vars(_state).update(previous)


def _mirror_result(status, path, document):
    return MirrorResult(status, path, path / "captures" / document["capture"] if document else None,
        len(document["refs"]) if document else 0, document["created_utc"] if document else None)


def update_mirror(repository, archive_root, *, cancel=None, progress=None, limits=Limits()):
    """Incrementally fetch into a NEW ref namespace; never prune or force refs."""
    repository = _validate(repository)
    # Reserve read/export headroom before admitting more permanent data.
    if (limits.max_working_bytes * 4 > limits.max_mirror_read_bytes
            or limits.max_mirror_entries + 512 > limits.max_mirror_read_entries
            or limits.max_captures > limits.max_mirror_read_entries): raise HistoryError("too_large")
    with _mirror_context(repository, archive_root, cancel=cancel, limits=limits, create=True) as (mirror, path, documents, run):
        if progress: progress("checking")
        retained = _verify_captures(run, documents[-1:])
        advertised, head = _refs(_run_git(["ls-remote", "--symref", "--", _remote_url(repository), "HEAD", "refs/heads/*", "refs/tags/*"], cancel=cancel, limits=limits), limits, True)
        if documents and documents[-1]["refs_digest"] == _digest(advertised, head):
            return _mirror_result("UNCHANGED", path, documents[-1])
        if not advertised and not documents: raise HistoryError("empty_repository")
        if len(documents) >= limits.max_captures: raise HistoryError("too_large")
        _free(path, limits)
        name = "capture-" + uuid.uuid4().hex + ".json"
        prefix = "refs/covearchive/captures/" + name[8:-5] + "/"
        if any(ref.startswith(prefix) for ref in retained) or any(doc["capture"] == name for doc in documents): raise HistoryError("conflict")
        if run(["for-each-ref", "--count=1", "--format=%(refname)", prefix]): raise HistoryError("conflict")
        now = datetime.now(timezone.utc)
        if documents:
            prior = datetime.strptime(documents[-1]["created_utc"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            now = max(now, prior + timedelta(microseconds=1))
        def capture_doc(refs, default):
            return dict(schema=1, kind="git-mirror-capture", repository=_mirror_identity(repository), capture=name,
                created_utc=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), refs=refs, head=default, refs_digest=_digest(refs, default))
        # SHA-256-width IDs and fixed export diagnostics reserve a read budget
        # before fetching. Explicit source refspecs prevent a remote ref-add race
        # from expanding this admitted capture; existing tips may still move.
        predicted = json.dumps(capture_doc({ref: "f" * 64 for ref in advertised}, head), sort_keys=True, separators=(",", ":")).encode()
        refspecs = "".join(ref + ":" + _capture_ref(name, ref) + "\n" for ref in advertised).encode()
        retained_bytes = sum(len(_capture_ref(name, ref).encode()) + 66 for ref in advertised)
        if (any(len(ref.encode()) > 1024 for ref in advertised)
                or len(predicted) > MAX_MANIFEST_BYTES or retained_bytes + 2048 > limits.max_stdout_bytes
                or len(refspecs) > limits.max_stdout_bytes
                or _size(path / "captures") + len(predicted) > limits.max_mirror_metadata_bytes): raise HistoryError("too_large")
        refs = {}
        if advertised:
            if progress: progress("downloading")
            run(["-c", "fetch.unpackLimit=1", "fetch", "--stdin", "--atomic", "--no-tags", "--no-recurse-submodules",
                 "--no-auto-maintenance", "--no-write-fetch-head", "--", _remote_url(repository)], input_data=refspecs)
            raw = run(["for-each-ref", "--format=%(objectname) %(refname)", prefix])
            for line in _git_lines(raw):
                sha, ref = line.split(" ", 1); refs["refs/" + ref[len(prefix):]] = sha
            if len(refs) > limits.max_refs: raise HistoryError("too_many_refs")
            if not refs: raise HistoryError("empty_repository")
        head = head if head in refs else None
        if progress: progress("verifying")
        doc = capture_doc(refs, head)
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > MAX_MANIFEST_BYTES: raise HistoryError("too_large")
        _capture_document(raw, name, repository, limits)
        _verify_captures(run, [doc])
        _check(cancel); _free(path, limits, len(raw))
        if progress: progress("publishing")
        # A single immutable manifest is the commit marker. Orphan namespaces
        # from interruption are deliberately retained and remain negotiable.
        with mirror.open_dir("captures") as captures:
            if _size(path / "captures") + len(raw) > limits.max_mirror_metadata_bytes: raise HistoryError("too_large")
            store.write_asset(captures, name, [raw], len(raw))
        return _mirror_result("UPDATED", path, doc)


def _mirror_read_limits(limits):
    return replace(limits, max_working_bytes=limits.max_mirror_read_bytes,
        max_mirror_entries=limits.max_mirror_read_entries)


def verify_mirror(repository, archive_root, *, cancel=None, progress=None, limits=Limits()):
    limits = _mirror_read_limits(limits)
    with _mirror_context(repository, archive_root, cancel=cancel, limits=limits) as (_, path, documents, run):
        if progress: progress("verifying")
        _verify_captures(run, documents)
        return _mirror_result("VERIFIED", path, documents[-1] if documents else None)


def _export_destination(repository, archive_root, destination, *, root, stack):
    """Resolve overlap before repository locks, recovery, Git or scratch creation."""
    repository = _validate(repository)
    try:
        requested = Path(destination).absolute()
        try: pinned_parent = stack.enter_context(store.ArchiveRoot.open(str(requested.parent)))
        except store.ArchiveError: raise HistoryError("unsafe_destination") from None
        parent = requested.parent.resolve(strict=True)
        _check_directory_binding(pinned_parent, parent, code="unsafe_destination")
        resolved = (parent / requested.name).resolve(strict=False)
        with ExitStack() as directories:
            directory = root
            mirror = archive_root
            for component in (*naming.project_components(repository), "git-history", "mirror"):
                directory = directories.enter_context(directory.open_dir(component, fold_case=True))
                mirror /= directory.name
            exists = True
            for ancestor in (resolved, *resolved.parents):
                try:
                    if os.path.samefile(ancestor, mirror): raise HistoryError("unsafe_destination")
                except FileNotFoundError:
                    if ancestor != resolved: raise
                    exists = False  # Only the new destination leaf may be absent.
            if exists:
                for ancestor in (mirror, *mirror.parents):
                    if os.path.samefile(ancestor, resolved): raise HistoryError("unsafe_destination")
            _check_directory_binding(pinned_parent, parent, code="unsafe_destination")
            return parent / requested.name, pinned_parent
    except HistoryError: raise
    except store.ArchiveError as error:
        raise HistoryError("missing_mirror" if error.code == "missing" else "unsafe_destination") from None
    except (OSError, RuntimeError, TypeError, ValueError):
        raise HistoryError("unsafe_destination") from None


@contextmanager
def _mirror_export_context(repository, archive_root, destination):
    with _mirror_root(archive_root) as bound_root, ExitStack() as stack:
        destination, parent = _export_destination(repository, bound_root[0], destination, root=bound_root[1], stack=stack)
        yield bound_root, destination, parent


def export_mirror(repository, archive_root, destination, *, capture=None, cancel=None, limits=Limits()):
    """Export a self-contained bundle with original names, or an empty bare repo.

    destination is a new directory below an existing safe parent. Restore with
    `git clone --bare --ref-format=reftable <returned path> restored.git`;
    pass --branch from capture.json to disambiguate shared default-branch tips.
    """
    repository = _validate(repository)
    limits = _mirror_read_limits(limits)
    with _mirror_export_context(repository, archive_root, destination) as (bound_root, destination, parent), _mirror_context(
            repository, archive_root, cancel=cancel, limits=limits, selected=capture, for_export=True, bound_root=bound_root) as (_, path, documents, run):
        doc = next((d for d in documents if d["capture"] == capture), None) if capture else (documents[-1] if documents else None)
        if doc is None: raise HistoryError("missing_capture")
        _verify_captures(run, [doc], check_objects=False)
        with parent:
            _check_directory_binding(parent, destination.parent, code="unsafe_destination")
            if destination.name in parent.names(): raise HistoryError("conflict")
            with _temporary() as work:
                # Bound selected export scratch, not unrelated retained data.
                _state.work = work
                _state.extra_work = None
                _free(work, limits); _free(destination.parent, limits)
                scratch = work / "restore.git"; bundle = work / "history.bundle"
                plain = lambda args, **kw: _run_git(args, cancel=cancel, limits=limits, **kw)
                plain(["init", "--bare", "--template=", "--ref-format=reftable", str(scratch)])
                prefix = ["--git-dir=" + str(scratch)]
                if doc["refs"]:
                    retained = work / "retained.bundle"
                    run(["bundle", "create", str(retained), "--glob=refs/covearchive/captures/" + doc["capture"][8:-5] + "/*"])
                    plain([*prefix, "bundle", "unbundle", str(retained)])
                    commands = "".join("create " + ref + " " + sha + "\n" for ref, sha in sorted(doc["refs"].items())).encode()
                    plain([*prefix, "update-ref", "--stdin"], input_data=commands)
                unborn = "refs/heads/covearchive-unborn-" + uuid.uuid4().hex
                while unborn in doc["refs"]:
                    _check(cancel); unborn += "-unborn"
                plain([*prefix, "symbolic-ref", "HEAD", doc["head"] or unborn])
                plain([*prefix, "fsck", "--full", "--strict"], truncate_stderr=True)
                if doc["refs"]:
                    plain([*prefix, "bundle", "create", str(bundle), "--all"])
                    plain(["bundle", "verify", str(bundle)])
                _check(cancel)
                _check_directory_binding(parent, destination.parent, code="unsafe_destination")
                with parent.open_dir(destination.name, create=True) as output:
                    if doc["refs"]:
                        with bundle.open("rb") as file:
                            def chunks():
                                while data := file.read(1024 * 1024):
                                    _check(cancel); yield data
                            store.write_asset(output, "history.bundle", chunks(), bundle.stat().st_size)
                    else:
                        # Git cannot represent an empty repository in a bundle.
                        def copy_empty(source, target):
                            for entry in source.iterdir():
                                if entry.is_dir():
                                    with target.open_dir(entry.name, create=True) as child: copy_empty(entry, child)
                                else:
                                    raw = entry.read_bytes(); store.write_asset(target, entry.name, [raw], len(raw))
                        with output.open_dir("empty.git", create=True) as empty: copy_empty(scratch, empty)
                    raw = json.dumps(doc, sort_keys=True).encode()
                    store.write_asset(output, "capture.json", [raw], len(raw))
                    raw = ((doc["head"] or unborn) + "\n").encode()
                    store.write_asset(output, "restore-head.txt", [raw], len(raw))
                return destination / ("history.bundle" if doc["refs"] else "empty.git")
