"""Match supported release-asset suffixes using ASCII case conversion only."""

from collections.abc import Iterable
from types import MappingProxyType


DEFAULT_EXTENSIONS = (".appimage", ".tar.gz", ".tar", ".dmg", ".zip", ".7z")
OPT_IN_EXTENSIONS = (".exe", ".msi")
SUPPORTED_EXTENSIONS = DEFAULT_EXTENSIONS + OPT_IN_EXTENSIONS
FORMAT_LABELS = MappingProxyType(
    {
        ".appimage": "AppImage",
        ".tar.gz": "TAR.GZ",
        ".tar": "TAR",
        ".dmg": "DMG",
        ".zip": "ZIP",
        ".7z": "7z",
        ".exe": "EXE",
        ".msi": "MSI",
    }
)

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def match_extension(filename: str, enabled: Iterable[str] | None = None) -> str | None:
    """Resolve the actual suffix before applying the validated format selection."""
    if not isinstance(filename, str):
        raise TypeError("Filename must be a string.")

    selected = None
    if enabled is not None:
        if isinstance(enabled, (str, bytes)):
            raise TypeError("Enabled formats must be an iterable of extensions.")
        selected = set()
        for extension in enabled:
            if not isinstance(extension, str) or extension not in SUPPORTED_EXTENSIONS:
                raise ValueError("Unknown enabled extension.")
            selected.add(extension)

    name_lower = filename.translate(_ASCII_LOWER)
    # The full registry is ordered longest first, independently of enabled.
    for extension in SUPPORTED_EXTENSIONS:
        if name_lower.endswith(extension):
            if len(filename) == len(extension):
                return None
            if selected is not None and extension not in selected:
                return None
            return extension
    return None
