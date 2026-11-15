"""Select native absolute roots in shared cross-platform fixtures."""
from pathlib import PureWindowsPath, PurePosixPath
import sys

NativePath = PureWindowsPath if sys.platform == "win32" else PurePosixPath


def native_root(windows):
    return windows if sys.platform == "win32" else "/" + windows[3:]
