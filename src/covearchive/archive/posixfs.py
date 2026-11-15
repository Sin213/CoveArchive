"""Linux dirfd-anchored archive primitives.

Ancestors above the authorized root resolve once; pinned ancestry is checked
for substitution. Binary publication links the verified open fd via
/proc/self/fd and linkat(AT_SYMLINK_FOLLOW), atomically without replacement.
A hardlink-capable local filesystem and working proc fd links are required.
An interrupted publication's second link is accepted only as a same-inode
reserved temp sibling, which recovery removes. New files are 0600, dirs 0700.
Linux has no handle-based unlink: replacement after the last identity check
can cause temp cleanup to unlink a same-uid writer's replacement. Metadata
rename/replace has the existing same-uid pathname races; binaries never use it.
Closed files remain mutable. File fsync precedes publication; directory fsync
is best effort. No power-loss durability or immutable-file guarantee is made.
"""
import ctypes as C
from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import PurePosixPath
import re
import stat
import sys
import uuid

from . import naming
from .fscommon import (ArchiveError, ArchiveConflictError, ArchiveBusyError,
                       ArchiveUnsafePathError, ArchiveCorruptError, TEMP_PATTERN,
                       _METADATA, _component)

_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_libc = C.CDLL(None, use_errno=True)
_linkat = getattr(_libc, "linkat", None)
if _linkat is not None:
    _linkat.argtypes = [C.c_int, C.c_char_p, C.c_int, C.c_char_p, C.c_int]
    _linkat.restype = C.c_int
_renameat2 = getattr(_libc, "renameat2", None)
if _renameat2 is not None:
    _renameat2.argtypes = [C.c_int, C.c_char_p, C.c_int, C.c_char_p, C.c_uint]
    _renameat2.restype = C.c_int


def _platform():
    if sys.platform != "linux":
        raise ArchiveError("unsupported_platform")


def _error(exc):
    if exc.errno == errno.ENOENT:
        raise ArchiveError("missing", status=exc.errno) from exc
    if exc.errno == errno.EEXIST:
        raise ArchiveConflictError("exists", status=exc.errno) from exc
    if exc.errno in (errno.ENOTDIR, errno.EISDIR):
        raise ArchiveUnsafePathError("not_regular_file", status=exc.errno) from exc
    if exc.errno == errno.ELOOP:
        raise ArchiveUnsafePathError("unsafe_path", status=exc.errno) from exc
    raise ArchiveError("os", status=exc.errno) from exc


def _verify_type(fd, directory=False, *, parent_fd=None, name=None):
    info = os.fstat(fd)
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ArchiveUnsafePathError("not_regular_file")
    if not directory and info.st_nlink != 1:
        if (info.st_nlink != 2 or parent_fd is None or name is None
                or not (TEMP_PATTERN.fullmatch(name) or _temp_sibling(parent_fd, name, info))):
            # A publisher/recovery process may have removed its temp alias
            # between fstat and the sibling scan. Accept the now single link.
            if info.st_nlink == 2:
                current = os.fstat(fd)
                if current.st_nlink == 1:
                    return current
            raise ArchiveUnsafePathError("multiple_links")
    return info


def _identity(info):
    return info.st_dev, info.st_ino


def _temp_sibling(directory_fd, name, info):
    if name in _METADATA or name == ".covearchive.lock" or any(
            name.startswith(m + ".corrupt-") for m in _METADATA):
        return False
    for sibling in os.listdir(directory_fd):
        if not TEMP_PATTERN.fullmatch(sibling):
            continue
        try:
            candidate = os.stat(sibling, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(candidate.st_mode) and _identity(candidate) == _identity(info):
            return True
    return False


def _unlink_owned(directory_fd, name, identity):
    """Best effort; same-uid replacement can still race the stat/unlink pair."""
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(current) != identity or not stat.S_ISREG(current.st_mode):
            return False
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return False
    return True


def _sync_dir(fd):
    try:
        os.fsync(fd)
    except OSError:
        pass


def _noreplace(source_fd, source, destination_fd, destination):
    """Metadata-only no-replace rename, including corrupt-manifest quarantine."""
    if _renameat2 is None:
        raise ArchiveError("noreplace_unsupported")
    if _renameat2(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), 1):
        code = C.get_errno()
        if code in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise ArchiveError("noreplace_unsupported", status=code)
        _error(OSError(code, os.strerror(code)))


def _publish_fd(fd, destination_fd, name):
    if _linkat is None:
        raise ArchiveError("noreplace_unsupported")
    if _linkat(-100, os.fsencode(f"/proc/self/fd/{fd}"), destination_fd, os.fsencode(name), 0x400):
        code = C.get_errno()
        if code in (errno.EPERM, errno.EACCES, errno.EOPNOTSUPP, errno.EMLINK,
                    errno.ENOENT, errno.ENOTDIR, errno.EXDEV, errno.ENOSYS, errno.EINVAL):
            raise ArchiveError("noreplace_unsupported", status=code)
        _error(OSError(code, os.strerror(code)))


@dataclass(frozen=True)
class FileInfo:
    volume_serial: int
    file_id: int
    links: int
    size: int
    is_directory: bool
    is_reparse: bool = False


class Handle:
    def __init__(self, fd, *, parent=None, name=None, temporary=False):
        self._handle = fd
        self.parent, self.name, self._temporary = parent, name, temporary
        self._children = set()
        self._identity = _identity(os.fstat(fd))
        if parent is not None:
            parent._children.add(self)

    def _get(self):
        if self._handle is None:
            raise ArchiveError("closed")
        if self.parent is not None:
            parent_fd = self.parent._get()
            try:
                current = os.stat(self.name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise ArchiveUnsafePathError("directory_changed") from exc
            if _identity(current) != self._identity or stat.S_ISLNK(current.st_mode):
                raise ArchiveUnsafePathError("directory_changed")
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
        try:
            for child in tuple(self._children):
                try:
                    child.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
        finally:
            try:
                if self._temporary:
                    self.delete_temp()
            finally:
                os.close(self._handle)
                self._handle = None
                if self.parent is not None:
                    self.parent._children.discard(self)
        if first_error is not None:
            raise first_error

    def info(self):
        info = _verify_type(self._get(), isinstance(self, Directory),
                            parent_fd=self.parent._get() if self.parent is not None else None, name=self.name)
        # The shared engine counts external aliases. A validated recovery temp
        # is not an external alias; raw fstat still exposes both native links.
        links = (1 if info.st_nlink == 2 and not isinstance(self, Directory)
                 and not self._temporary else info.st_nlink)
        return FileInfo(info.st_dev, info.st_ino, links, info.st_size,
                        stat.S_ISDIR(info.st_mode))

    def unlinked(self):
        # Metadata replacement intentionally unlinks the held old inode.
        return os.fstat(self._handle).st_nlink == 0

    def disk_name(self):
        self._get()
        return self.name

    def read(self, count=1024 * 1024):
        if type(count) is not int or not 0 <= count <= 1024 * 1024:
            raise ValueError("read count must be between zero and one MiB")
        try:
            return os.read(self._get(), count)
        except OSError as exc:
            _error(exc)

    def read_bounded(self, maximum):
        if type(maximum) is not int or maximum < 0:
            raise ValueError("maximum must be nonnegative")
        if self.info().size > maximum:
            raise ArchiveError("oversized")
        chunks, used = [], 0
        while chunk := self.read(min(1024 * 1024, maximum - used + 1)):
            used += len(chunk)
            if used > maximum:
                raise ArchiveError("oversized")
            chunks.append(chunk)
        return b"".join(chunks)

    def write(self, data):
        view, used = memoryview(data).cast("B"), 0
        try:
            while used < len(view):
                actual = os.write(self._get(), view[used:])
                if not actual:
                    raise ArchiveError("short_write")
                used += actual
        except OSError as exc:
            _error(exc)
        return used

    def rewind(self):
        os.lseek(self._get(), 0, os.SEEK_SET)

    def flush(self):
        try:
            os.fsync(self._get())
        except OSError as exc:
            _error(exc)

    def rename(self, directory, name, *, replace=False, metadata=False):
        _component(name, metadata=metadata, role=directory.role)
        if replace and (not metadata or name not in _METADATA):
            raise ArchiveUnsafePathError("replace_forbidden")
        if metadata and name not in _METADATA and not any(
                re.fullmatch(re.escape(m) + r"\.corrupt-[1-9][0-9]*", name) for m in _METADATA):
            raise ArchiveUnsafePathError("reserved_name")
        if not metadata and not self._temporary:
            raise ArchiveUnsafePathError("not_temp")
        self.info()
        if not metadata and os.fstat(self._handle).st_nlink != 1:
            raise ArchiveUnsafePathError("multiple_links")
        source_fd, destination_fd = self.parent._get(), directory._get()
        try:
            if not metadata:
                _publish_fd(self._handle, destination_fd, name)
                try:
                    _unlink_owned(source_fd, self.name, self._identity)
                except OSError:
                    # Publication already succeeded. A retained same-inode temp
                    # is recoverable; a substituted basename is never required.
                    pass
            elif replace:
                os.rename(self.name, name, src_dir_fd=source_fd, dst_dir_fd=destination_fd)
            else:
                _noreplace(source_fd, self.name, destination_fd, name)
        except OSError as exc:
            _error(exc)
        self.parent._children.discard(self)
        self.parent, self.name, self._temporary = directory, name, False
        directory._children.add(self)
        self._get()
        _sync_dir(destination_fd)

    def delete_temp(self):
        if not self._temporary or not TEMP_PATTERN.fullmatch(self.name or ""):
            raise ArchiveUnsafePathError("not_temp")
        if self._handle is None:
            raise ArchiveError("closed")
        parent_fd = self.parent._get()
        info = os.fstat(self._handle)
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 0:
            self._temporary = False
            return  # A foreign unlink already removed our only name.
        _verify_type(self._handle, parent_fd=parent_fd, name=self.name)
        try:
            _unlink_owned(parent_fd, self.name, self._identity)
        except OSError as exc:
            _error(exc)
        self._temporary = False


class Directory(Handle):
    role = "archive"

    def _open(self, name, *, directory=False, create=False, temporary=False, metadata=False):
        _component(name, metadata=metadata, temporary=temporary, role=self.role, directory=directory)
        parent_fd = self._get()
        flags = _FLAGS | (os.O_DIRECTORY if directory else os.O_RDONLY)
        try:
            if directory and create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            if temporary:
                flags = _FLAGS | os.O_RDWR | os.O_CREAT | os.O_EXCL if create else _FLAGS | os.O_RDWR
            fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            _error(exc)
        try:
            _verify_type(fd, directory, parent_fd=parent_fd, name=name)
            if temporary:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ArchiveBusyError("busy") from exc
            opened = (Directory if directory else Handle)(fd, parent=self, name=name)
        except BaseException:
            os.close(fd)
            raise
        try:
            opened._get()
            opened._temporary = temporary
            return opened
        except BaseException:
            opened.close()
            raise

    def open_dir(self, name, *, create=False, fold_case=False):
        if not fold_case:
            return self._open(name, directory=True, create=create)
        _component(name, directory=True)
        lock_fd = None
        try:
            # A fresh open description prevents unlocking any ancestor handle's
            # flock. This short critical section never nests directory locks.
            lock_fd = os.open(".", _FLAGS | os.O_DIRECTORY, dir_fd=self._get())
            _verify_type(lock_fd, True)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if name.isascii():
                matches = [entry for entry in self.names() if entry.isascii() and entry.casefold() == name.casefold()]
                if len(matches) > 1:
                    raise ArchiveConflictError("case_alias")
                if matches:
                    name = matches[0]
            return self._open(name, directory=True, create=create)
        except OSError as exc:
            _error(exc)
        finally:
            if lock_fd is not None:
                # Closing this dedicated fd releases flock, including on errors.
                os.close(lock_fd)

    def open_file(self, name, *, metadata=False, delete=False, hold=False):
        return self._open(name, metadata=metadata)

    def create_temp(self, *, token=None):
        token = uuid.uuid4().hex if token is None else token
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ArchiveUnsafePathError("component")
        return self._open(".covearchive-tmp-" + token + ".part", temporary=True, create=True)

    def open_temp(self, name):
        if not isinstance(name, str) or not TEMP_PATTERN.fullmatch(name):
            raise ArchiveUnsafePathError("not_temp")
        return self._open(name, temporary=True)

    def open_lock(self):
        try:
            fd = os.open(".covearchive.lock", _FLAGS | os.O_RDWR | os.O_CREAT, 0o600, dir_fd=self._get())
        except OSError as exc:
            _error(exc)
        try:
            _verify_type(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ArchiveBusyError("busy") from exc
            opened = Handle(fd, parent=self, name=".covearchive.lock")
        except BaseException:
            os.close(fd)
            raise
        try:
            opened._get()
            return opened
        except BaseException:
            opened.close()
            raise

    def names(self):
        try:
            return tuple(os.listdir(self._get()))
        except OSError as exc:
            _error(exc)


class ArchiveRoot(Directory):
    @classmethod
    def open(cls, path):
        _platform()
        try:
            naming.validate_root(path)
        except naming.NamingError as exc:
            raise ArchiveUnsafePathError("root") from exc
        path = PurePosixPath(path)
        parent = None
        try:
            # Only ancestors above the authorized root may resolve symlinks.
            parent_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            parent = Directory(parent_fd)
            fd = os.open(path.name or ".", _FLAGS | os.O_DIRECTORY, dir_fd=parent_fd)
            try:
                info = _verify_type(fd, True)
                root = cls(fd, parent=parent, name=path.name or ".")
                # Root owns this private parent descriptor, not vice versa.
                parent._children.discard(root)
            except BaseException:
                os.close(fd)
                raise
            root._root_parent = parent
            root.identity = _identity(info)
            try:
                root._get()
            except BaseException:
                root.close()
                raise
            return root
        except BaseException as exc:
            if parent is not None:
                parent.close()
            if isinstance(exc, OSError):
                _error(exc)
            raise

    def close(self):
        parent = getattr(self, "_root_parent", None)
        try:
            super().close()
        finally:
            if parent is not None:
                self._root_parent = None
                parent.close()
