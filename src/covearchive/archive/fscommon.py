"""Shared archive errors and component policy; no platform imports."""
import re
from . import naming


class ArchiveError(Exception):
    """Filesystem failure with a stable code and optional OS status."""
    def __init__(self, code, *, status=None):
        self.code, self.status = code, status
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
