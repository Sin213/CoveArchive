"""Handle-anchored primitives for a local NTFS archive.

Children are opened relative to pinned directory handles, never by joined paths.
No-replace publication and exclusive temporary creation are enforced by NTFS.
Ancestors above the authorized root resolve once; file flushes do not promise
power-loss durability. Closed files remain mutable by other same-user processes.
"""

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import re
import sys
import uuid

from . import naming


class ArchiveError(Exception):
    """Filesystem failure with a stable code and optional OS status."""

    def __init__(self, code, *, status=None):
        self.code = code
        self.status = status
        super().__init__(code if status is None else f"{code} (0x{status:08x})")


class ArchiveConflictError(ArchiveError):
    """An existing object conflicts with the requested operation."""


class ArchiveBusyError(ArchiveError):
    """Another handle excludes this operation."""


class ArchiveUnsafePathError(ArchiveError):
    """A name or opened object is unsafe."""


class ArchiveCorruptError(ArchiveError):
    """Persisted archive metadata is corrupt."""


TEMP_PATTERN = re.compile(r"\.covearchive-tmp-[0-9a-f]{32}\.part\Z")
_METADATA = {naming.RELEASE_FILENAME, naming.INDEX_FILENAME}
_READ, _WRITE, _DELETE, _SYNC = 0x80000000, 0x40000000, 0x10000, 0x100000
_REPARSE, _DIRECTORY = 0x400, 0x10


def _platform():
    if sys.platform != "win32":
        raise ArchiveError("unsupported_platform")


def _component(name, *, metadata=False, temporary=False, role="archive", directory=False):
    if not naming.is_safe_component(name):
        raise ArchiveUnsafePathError("component")
    if directory:
        return
    reserved = (TEMP_PATTERN.fullmatch(name) or
                (role in ("archive", "project") and name.casefold() in (naming.INDEX_FILENAME, ".covearchive.lock")) or
                (role in ("archive", "release") and name.casefold() == naming.RELEASE_FILENAME))
    allowed_metadata = name in _METADATA or any(
        re.fullmatch(re.escape(m) + r"\.corrupt-[1-9][0-9]*", name) for m in _METADATA)
    if reserved and not ((metadata and allowed_metadata)
                         or (temporary and TEMP_PATTERN.fullmatch(name))):
        raise ArchiveUnsafePathError("reserved_name")


def _ntcheck(status):
    status &= 0xffffffff
    if status < 0x80000000:
        return
    if status in (0xc0000034, 0xc000003a, 0xc000000f):
        raise ArchiveError("missing", status=status)
    if status == 0xc0000035:
        raise ArchiveConflictError("exists", status=status)
    if status in (0xc0000043, 0xc0000054):
        raise ArchiveBusyError("busy", status=status)
    if status in (0xc0000103, 0xc00000ba):
        raise ArchiveUnsafePathError("not_regular_file", status=status)
    if status in (0xc000050b, 0xc0000279, 0xc0000280):
        raise ArchiveUnsafePathError("reparse_point", status=status)
    raise ArchiveError("os", status=status)


def _wincheck(ok):
    if not ok:
        error = C.get_last_error()
        if error in (32, 33):
            raise ArchiveBusyError("busy", status=error)
        if error in (2, 3):
            raise ArchiveError("missing", status=error)
        raise ArchiveError("os", status=error)


class _Unicode(C.Structure):
    _fields_ = [("Length", W.USHORT), ("MaximumLength", W.USHORT), ("Buffer", W.LPWSTR)]


class _Attributes(C.Structure):
    _fields_ = [("Length", W.ULONG), ("RootDirectory", W.HANDLE),
                ("ObjectName", C.POINTER(_Unicode)), ("Attributes", W.ULONG),
                ("SecurityDescriptor", C.c_void_p), ("SecurityQualityOfService", C.c_void_p)]


class _IO(C.Structure):
    _fields_ = [("Status", C.c_void_p), ("Information", C.c_size_t)]


class _Information(C.Structure):
    _fields_ = [("attributes", W.DWORD), ("creation", W.FILETIME),
                ("access", W.FILETIME), ("write", W.FILETIME),
                ("volume", W.DWORD), ("size_high", W.DWORD), ("size_low", W.DWORD),
                ("links", W.DWORD), ("id_high", W.DWORD), ("id_low", W.DWORD)]


class _Rename(C.Structure):
    _fields_ = [("Flags", W.DWORD), ("RootDirectory", W.HANDLE),
                ("FileNameLength", W.DWORD), ("FileName", W.WCHAR * 1)]


if sys.platform == "win32":
    _kernel = C.WinDLL("kernel32", use_last_error=True)
    _nt = C.WinDLL("ntdll")

    def _bind(dll, name, args, result):
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, result
        return fn

    _create = _bind(_kernel, "CreateFileW", [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p,
                     W.DWORD, W.DWORD, W.HANDLE], W.HANDLE)
    _close = _bind(_kernel, "CloseHandle", [W.HANDLE], W.BOOL)
    _info = _bind(_kernel, "GetFileInformationByHandle", [W.HANDLE, C.POINTER(_Information)], W.BOOL)
    _flush = _bind(_kernel, "FlushFileBuffers", [W.HANDLE], W.BOOL)
    _read = _bind(_kernel, "ReadFile", [W.HANDLE, C.c_void_p, W.DWORD,
                 C.POINTER(W.DWORD), C.c_void_p], W.BOOL)
    _write = _bind(_kernel, "WriteFile", [W.HANDLE, C.c_void_p, W.DWORD,
                  C.POINTER(W.DWORD), C.c_void_p], W.BOOL)
    _seek = _bind(_kernel, "SetFilePointerEx", [W.HANDLE, C.c_longlong,
                 C.POINTER(C.c_longlong), W.DWORD], W.BOOL)
    _volume = _bind(_kernel, "GetVolumeInformationByHandleW", [W.HANDLE, W.LPWSTR, W.DWORD,
                   C.POINTER(W.DWORD), C.POINTER(W.DWORD), C.POINTER(W.DWORD),
                   W.LPWSTR, W.DWORD], W.BOOL)
    _ntcreate = _bind(_nt, "NtCreateFile", [C.POINTER(W.HANDLE), W.ULONG,
                     C.POINTER(_Attributes), C.POINTER(_IO), C.c_void_p, W.ULONG,
                     W.ULONG, W.ULONG, W.ULONG, C.c_void_p, W.ULONG], W.LONG)
    _ntquery = _bind(_nt, "NtQueryInformationFile", [W.HANDLE, C.POINTER(_IO),
                    C.c_void_p, W.ULONG, W.ULONG], W.LONG)
    _ntset = _bind(_nt, "NtSetInformationFile", [W.HANDLE, C.POINTER(_IO),
                  C.c_void_p, W.ULONG, W.ULONG], W.LONG)
    _ntvolume = _bind(_nt, "NtQueryVolumeInformationFile", [W.HANDLE, C.POINTER(_IO),
                     C.c_void_p, W.ULONG, W.ULONG], W.LONG)
    _ntdirectory = _bind(_nt, "NtQueryDirectoryFile", [W.HANDLE, W.HANDLE, C.c_void_p,
                        C.c_void_p, C.POINTER(_IO), C.c_void_p, W.ULONG, W.ULONG,
                        C.c_ubyte, C.c_void_p, C.c_ubyte], W.LONG)


@dataclass(frozen=True)
class FileInfo:
    attributes: int
    volume_serial: int
    file_id: int
    links: int
    size: int

    @property
    def is_directory(self):
        return bool(self.attributes & _DIRECTORY)

    @property
    def is_reparse(self):
        return bool(self.attributes & _REPARSE)


class Handle:
    """An owned handle; closing a parent first closes all its descendants."""

    def __init__(self, handle, *, parent=None, name=None, temporary=False):
        self._handle = handle
        self.parent, self.name = parent, name
        self._temporary = temporary
        self._children = set()
        if parent is not None:
            parent._children.add(self)

    def _get(self):
        _platform()
        if self._handle is None:
            raise ArchiveError("closed")
        return self._handle

    def __enter__(self):
        self._get()
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._handle is None:
            return
        first_error = None
        for child in tuple(self._children):
            try:
                child.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        try:
            if self._temporary:
                self.delete_temp()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        try:
            _wincheck(_close(self._handle))
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        finally:
            self._handle = None
            if self.parent is not None:
                self.parent._children.discard(self)
        if first_error is not None:
            raise first_error

    def info(self):
        result = _Information()
        _wincheck(_info(self._get(), C.byref(result)))
        return FileInfo(result.attributes, result.volume,
                        result.id_high << 32 | result.id_low, result.links,
                        result.size_high << 32 | result.size_low)

    def unlinked(self):
        data, io = C.create_string_buffer(24), _IO()
        _ntcheck(_ntquery(self._get(), C.byref(io), data, len(data), 5))
        return W.ULONG.from_buffer(data, 16).value == 0

    def disk_name(self):
        data, io = C.create_string_buffer(65536), _IO()
        _ntcheck(_ntquery(self._get(), C.byref(io), data, len(data), 48))
        length = W.ULONG.from_buffer(data).value
        return data.raw[4:4 + length].decode("utf-16-le", "surrogatepass").rsplit("\\", 1)[-1]

    def read(self, count=1024 * 1024):
        if type(count) is not int or not 0 <= count <= 1024 * 1024:
            raise ValueError("read count must be between zero and one MiB")
        data, actual = C.create_string_buffer(count), W.DWORD()
        _wincheck(_read(self._get(), data, count, C.byref(actual), None))
        return data.raw[:actual.value]

    def read_bounded(self, maximum):
        if type(maximum) is not int or maximum < 0:
            raise ValueError("maximum must be nonnegative")
        if self.info().size > maximum:
            raise ArchiveError("oversized")
        parts, used = [], 0
        while chunk := self.read(min(1024 * 1024, maximum - used + 1)):
            used += len(chunk)
            if used > maximum:
                raise ArchiveError("oversized")
            parts.append(chunk)
        return b"".join(parts)

    def write(self, data):
        view = memoryview(data).cast("B")
        offset = 0
        while offset < len(view):
            piece = bytes(view[offset:offset + 1024 * 1024])
            actual = W.DWORD()
            _wincheck(_write(self._get(), piece, len(piece), C.byref(actual), None))
            if not actual.value:
                raise ArchiveError("short_write")
            offset += actual.value
        return offset

    def rewind(self):
        _wincheck(_seek(self._get(), 0, None, 0))

    def flush(self):
        _wincheck(_flush(self._get()))

    def rename(self, directory, name, *, replace=False, metadata=False):
        _component(name, metadata=metadata, role=directory.role)
        if replace and (not metadata or name not in _METADATA):
            raise ArchiveUnsafePathError("replace_forbidden")
        encoded = name.encode("utf-16-le", "surrogatepass")
        size = _Rename.FileName.offset + len(encoded)
        data = C.create_string_buffer(max(size, C.sizeof(_Rename)))
        fields = _Rename.from_buffer(data)
        fields.Flags = 3 if replace else 0
        fields.RootDirectory = directory._get()
        fields.FileNameLength = len(encoded)
        C.memmove(C.addressof(data) + _Rename.FileName.offset, encoded, len(encoded))
        io = _IO()
        _ntcheck(_ntset(self._get(), C.byref(io), data, len(data), 65))
        self.name = name
        self._temporary = False

    def delete_temp(self):
        if not self._temporary or not TEMP_PATTERN.fullmatch(self.name or ""):
            raise ArchiveUnsafePathError("not_temp")
        info = self.info()
        if info.is_reparse or info.is_directory or info.links != 1:
            raise ArchiveUnsafePathError("not_regular_file")
        io, disposition = _IO(), C.c_ubyte(1)
        _ntcheck(_ntset(self._get(), C.byref(io), C.byref(disposition), 1, 13))
        self._temporary = False


class Directory(Handle):
    role = "archive"

    def _open(self, name, *, directory=False, disposition=1, access=_READ | _SYNC,
              share=1, metadata=False, temporary=False):
        _component(name, metadata=metadata, temporary=temporary, role=self.role, directory=directory)
        text = C.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le", "surrogatepass"))
        string = _Unicode(length, length + 2, C.cast(text, W.LPWSTR))
        attrs = _Attributes(C.sizeof(_Attributes), self._get(), C.pointer(string), 0x40, None, None)
        result, io = W.HANDLE(), _IO()
        options = 0x20 | 0x200000 | (1 if directory else 0)
        _ntcheck(_ntcreate(C.byref(result), access, C.byref(attrs), C.byref(io), None,
                          0, share, disposition, options, None, 0))
        cls = Directory if directory else Handle
        opened = cls(result.value, parent=self, name=name)
        try:
            info = opened.info()
            if info.is_reparse:
                raise ArchiveUnsafePathError("reparse_point")
            if info.is_directory != directory:
                raise ArchiveUnsafePathError("not_regular_file")
            if opened.disk_name() != name:
                raise ArchiveConflictError("case_alias")
            opened._temporary = temporary
            return opened
        except BaseException:
            opened.close()
            raise

    def open_dir(self, name, *, create=False, fold_case=False):
        if fold_case:
            _component(name, directory=True)
            if name.isascii():
                matches = [entry for entry in self.names()
                           if entry.isascii() and entry.casefold() == name.casefold()]
                if len(matches) > 1:
                    raise ArchiveConflictError("case_alias")
                if matches:
                    name = matches[0]
        return self._open(name, directory=True, disposition=3 if create else 1,
                          access=0x100081, share=3)

    def open_file(self, name, *, metadata=False, delete=False, hold=False):
        return self._open(name, metadata=metadata, share=5 if hold else 1,
                          access=_READ | _SYNC | (_DELETE if delete or hold else 0))

    def create_temp(self, *, token=None):
        token = uuid.uuid4().hex if token is None else token
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ArchiveUnsafePathError("component")
        return self._open(".covearchive-tmp-" + token + ".part", disposition=2,
                          access=_READ | _WRITE | _DELETE | _SYNC, share=0, temporary=True)

    def open_temp(self, name):
        if not isinstance(name, str) or not TEMP_PATTERN.fullmatch(name):
            raise ArchiveUnsafePathError("not_temp")
        return self._open(name, access=_READ | _DELETE | _SYNC, share=0, temporary=True)

    def open_lock(self):
        # The lock is the sole non-metadata reserved name accepted here.
        return self._open_lock()

    def _open_lock(self):
        name = ".covearchive.lock"
        text = C.create_unicode_buffer(name)
        string = _Unicode(len(name) * 2, (len(name) + 1) * 2, C.cast(text, W.LPWSTR))
        attrs = _Attributes(C.sizeof(_Attributes), self._get(), C.pointer(string), 0x40, None, None)
        result, io = W.HANDLE(), _IO()
        _ntcheck(_ntcreate(C.byref(result), _READ | _WRITE | _SYNC, C.byref(attrs),
                          C.byref(io), None, 0, 0, 3, 0x200020, None, 0))
        opened = Handle(result.value, parent=self, name=name)
        try:
            info = opened.info()
            if info.is_reparse:
                raise ArchiveUnsafePathError("reparse_point")
            if info.is_directory or info.links != 1:
                raise ArchiveUnsafePathError("not_regular_file")
            if opened.disk_name() != name:
                raise ArchiveConflictError("case_alias")
            return opened
        except BaseException:
            opened.close()
            raise

    def names(self):
        result, restart = [], True
        while True:
            data, io = C.create_string_buffer(65536), _IO()
            status = _ntdirectory(self._get(), None, None, None, C.byref(io), data,
                                  len(data), 12, False, None, restart)
            restart = False
            if status & 0xffffffff == 0x80000006:
                return tuple(result)
            _ntcheck(status)
            offset = 0
            while True:
                next_offset = W.ULONG.from_buffer(data, offset).value
                length = W.ULONG.from_buffer(data, offset + 8).value
                name = data.raw[offset + 12:offset + 12 + length].decode("utf-16-le", "surrogatepass")
                if name not in (".", ".."):
                    result.append(name)
                if not next_offset:
                    break
                offset += next_offset


class ArchiveRoot(Directory):
    """Explicit authorization of an existing, pinned local NTFS directory."""

    @classmethod
    def open(cls, path):
        _platform()
        try:
            naming.validate_root(path)
        except naming.NamingError as exc:
            raise ArchiveUnsafePathError("root") from exc
        if str(path).replace("/", "\\").startswith("\\\\"):
            raise ArchiveUnsafePathError("remote_volume")
        raw = _create(str(path), 0x100081, 3, None, 3, 0x02200000, None)
        if raw == C.c_void_p(-1).value:
            _wincheck(False)
        root = cls(raw)
        try:
            info = root.info()
            if info.is_reparse:
                raise ArchiveUnsafePathError("reparse_point")
            if not info.is_directory:
                raise ArchiveUnsafePathError("not_directory")
            filesystem = C.create_unicode_buffer(256)
            serial, maximum, flags = W.DWORD(), W.DWORD(), W.DWORD()
            _wincheck(_volume(root._get(), None, 0, C.byref(serial), C.byref(maximum),
                              C.byref(flags), filesystem, len(filesystem)))
            if filesystem.value != "NTFS":
                raise ArchiveUnsafePathError("filesystem")
            device, io = (W.ULONG * 2)(), _IO()
            _ntcheck(_ntvolume(root._get(), C.byref(io), device, C.sizeof(device), 4))
            if device[0] != 7 or device[1] & 0x10:
                raise ArchiveUnsafePathError("remote_volume")
            root.identity = (info.volume_serial, info.file_id)
            return root
        except BaseException:
            root.close()
            raise
