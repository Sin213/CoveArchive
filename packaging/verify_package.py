"""Read-only, stdlib-only inspection of release trees and archives."""
import argparse
import getpass
import io
import json
import marshal
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile
import zlib

FORBIDDEN = {"tests", ".git", ".env", "preferences.json", "library.json", "evidence"}
SECRET = re.compile(rb"gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
                  rb"AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
                  rb"[\r\n]+[A-Za-z0-9+/=\r\n]{40,}-----END (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----", re.I)


def first_party(name):
    parts = PurePosixPath(name.lower()).parts
    # Install directories and onedir wrappers do not establish package ownership.
    namespace = [p for i, p in enumerate(parts) if not (p == "covearchive" and (
        parts[max(0, i - 1):i] == ("opt",)
        or parts[max(0, i - 2):i] == ("usr", "lib")
        or parts[i + 1:i + 2] == ("_internal",)))]
    return (any(p == "covearchive" or (p.startswith("covearchive.") and not p.endswith((".exe", ".zip", ".pyz")))
                or p.startswith("run_covearchive") for p in namespace)
            or PurePosixPath(name.lower()).suffix in
            {".txt", ".text", ".md", ".rst", ".xml", ".yaml", ".yml", ".json", ".cfg", ".conf", ".ini",
             ".toml", ".spec", ".iss", ".desktop", ".control", ".toc", ".manifest"}
            or PurePosixPath(name.lower()).name == "control")


def build_host_paths():
    root = Path(__file__).resolve().parent.parent
    paths = [root, root / "build/release", root / "build/release/evidence", Path.home(), Path(tempfile.gettempdir())]
    paths += [Path(os.environ[key]) for key in ("USERPROFILE", "HOME") if os.environ.get(key)]
    return {str(path.resolve()).replace("\\", "/").rstrip("/").lower().encode("utf-8") for path in paths}


def build_host_leak(data, paths):
    # Cover plain text, JSON escapes, UTF-16 metadata and marshalled first-party
    # code strings. Third-party binaries and their PDB references are excluded.
    normalized = data.replace(b"\0", b"").replace(b"\\\\", b"\\").replace(b"\\", b"/").lower()
    if any(path and path in normalized for path in paths):
        return True
    user = re.escape(getpass.getuser().lower().encode("utf-8"))
    return re.search(rb"(?:[a-z]:/users/|/home/)" + user + rb"(?:/|[^a-z0-9_.-]|$)", normalized) is not None


def archive_entries(name, data):
    """Inspect embedded zip, PyInstaller CArchive and PYZ without executing code."""
    if data.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                yield name + "/" + member.filename, archive.read(member)
    elif data.startswith(b"PYZ\0"):
        offset = struct.unpack("!I", data[8:12])[0]
        toc = dict(marshal.loads(data[offset:]))
        for module, (_, start, length) in toc.items():
            if not length:  # PYZ namespace packages carry no compressed code.
                continue
            raw = zlib.decompress(data[start:start + length])
            yield name + "/" + module, raw
    else:
        cookie = data.rfind(b"MEI\014\013\012\013\016")
        if cookie < 0:
            return
        _, length, toc_offset, toc_length, _, _ = struct.unpack("!8sIIII64s", data[cookie:cookie + 88])
        start = cookie + 88 - length
        position, end = start + toc_offset, start + toc_offset + toc_length
        while position < end:
            size, offset, packed, _, compressed, kind = struct.unpack("!IIIIBc", data[position:position + 18])
            if size < 18:
                raise ValueError("invalid CArchive entry")
            member = data[position + 18:position + size].rstrip(b"\0").decode("utf-8")
            position += size
            if kind == b"o":
                continue
            raw = data[start + offset:start + offset + packed]
            yield name + "/" + member, zlib.decompress(raw) if compressed else raw


def entries(path):
    if path.is_dir():
        for item in sorted(path.rglob("*")):
            if item.is_symlink():
                # Never follow a package link outside the inspected tree.
                target = item.resolve()
                if not target.is_relative_to(path.resolve()):
                    raise ValueError("package symlink escapes tree")
            yield item.relative_to(path).as_posix(), item.read_bytes() if item.is_file() else b""
    elif path.suffix == ".deb":
        result = subprocess.run(["dpkg-deb", "--fsys-tarfile", str(path)], check=True, capture_output=True)
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            for member in archive:
                source = archive.extractfile(member) if member.isfile() else None
                yield member.name, source.read() if source else b""
    else:
        yield path.name, path.read_bytes()


def verify(path, platform):
    issues, names = [], []
    identifying_paths = build_host_paths()

    def inspect(name, data, depth=0):
        name = name.replace("\\", "/")
        parts = PurePosixPath(name).parts
        lower = name.lower()
        names.append(lower)
        if (any(p.lower() in FORBIDDEN or p.lower().startswith(".tmp-") for p in parts)
                or any(p == ".." for p in parts) or name.startswith("/")
                or lower.endswith((".sqlite3", ".key"))
                or (lower.endswith(".pem") and not lower.endswith("certifi/cacert.pem"))
                or PurePosixPath(lower).name.startswith("queue.")):
            issues.append("forbidden package path: " + name)
        if SECRET.search(data):
            issues.append("forbidden secret: " + name)
        if first_party(name) and build_host_leak(data, identifying_paths):
            issues.append("forbidden build-host path: " + name)
        if depth < 4:
            for member, raw in archive_entries(name, data):
                inspect(member, raw, depth + 1)

    try:
        for name, data in entries(Path(path)):
            inspect(name, data)
    except (OSError, ValueError, EOFError, struct.error, zlib.error, zipfile.BadZipFile,
            tarfile.TarError, subprocess.SubprocessError) as exc:
        issues.append("unreadable package: " + type(exc).__name__)
    for label, suffix in (("package icon/data", "covearchive/assets/covearchive-icon.png"),
                          ("requests CA bundle", "certifi/cacert.pem")):
        if not any(n.endswith(suffix) for n in names):
            issues.append("missing " + label)
    plugins = ("qwindows.dll", "qoffscreen.dll") if platform == "windows" else ("libqxcb.so", "libqoffscreen.so")
    for plugin in plugins:
        if not any("/platforms/" in n and n.endswith(plugin) for n in names):
            issues.append("missing Qt platform plugin: " + plugin)
    if platform == "linux" and not any("qtdbus" in n or "qt6dbus" in n for n in names):
        issues.append("missing QtDBus")
    if platform == "linux" and not any(n.endswith("libxcb-cursor.so.0") for n in names):
        issues.append("missing bundled libxcb-cursor.so.0")
    return sorted(set(issues))


def negative_selftest():
    with tempfile.TemporaryDirectory(prefix="package-verifier-") as directory:
        root = Path(directory)
        (root / "queue.sqlite3").write_bytes(b"planted private database")
        forbidden = any("forbidden package path: queue.sqlite3" in issue for issue in verify(root, "windows"))
        (root / "queue.sqlite3").unlink()
        text = root / "first-party.txt"
        text.write_text(str(Path(__file__).resolve().parent.parent), encoding="utf-8")
        host = any("build-host" in issue for issue in verify(root, "windows"))
        text.write_text(r"C:\Users\qt\work\qtbase_build\Qt6Core.pdb", encoding="utf-8")
        upstream = not any("build-host" in issue for issue in verify(root, "windows"))
        return forbidden and host and upstream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("--platform", choices=("windows", "linux"), default="windows" if sys.platform == "win32" else "linux")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        ok = negative_selftest()
        print(json.dumps({"negative_selftest": ok}))
        return 0 if ok else 1
    if args.path is None:
        parser.error("path required without --self-test")
    issues = verify(args.path, args.platform)
    print(json.dumps({"ok": not issues, "issues": issues}, indent=2))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
