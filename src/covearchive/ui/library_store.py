"""Bounded library snapshots with latched failures and atomic writes."""
import os
from pathlib import Path
import sys
from covearchive.library import model as m
from covearchive.ui.preferences import PreferenceStore

_READ_ONLY = "Library is read-only until the file is fixed or moved, then restart CoveArchive."
QSaveFile = None  # Loading a Library must not initialize Qt.


class LibraryStore:
    """load -> (Library, problem, writable); save -> problem or None; no implicit writes."""
    def __init__(self, path=None):
        if path is None:
            base = PreferenceStore().path
            path = base.with_name("library.json") if base is not None else None
        self.path = Path(path) if path is not None else None
        self.writable = self.path is not None
        self.problem = None if self.writable else "LOCALAPPDATA is unset; library is in-memory only."
        self._loaded, self._seen, self._library = False, None, m.Library()

    def load(self):
        if not self._loaded:
            self._loaded = True
            if self.path is not None:
                try:
                    with self.path.open("rb") as stream: raw = stream.read(m.MAX_BYTES + 1)
                    self._seen = raw
                    self._library = m.load_library(raw)
                except FileNotFoundError:
                    pass
                except (OSError, ValueError, TypeError, RecursionError, OverflowError) as exc:
                    self.writable = False
                    reason = "Newer version of library" if isinstance(exc, m.LibraryError) and exc.code == "newer_version" else "Could not load library"
                    if isinstance(exc, m.LibraryError) and exc.code == "size": reason = "Library exceeds 8 MiB"
                    self.problem = f"{reason} at {self.path}. {_READ_ONLY}"
        return self._library.clone(), self.problem, self.writable

    def save(self, library):
        self.load()
        if not self.writable: return self.problem or _READ_ONLY
        try:
            with self.path.open("rb") as stream: current = stream.read(m.MAX_BYTES + 1)
        except FileNotFoundError: current = None
        except OSError: return "Could not read library to verify it; nothing was saved."
        # Compare again before writing; this narrows, but cannot eliminate, writer races.
        if current != self._seen:
            self.writable = False
            self.problem = "Library changed on disk; restart CoveArchive to reload; the file was not overwritten."
            return self.problem
        try:
            payload = m.dump_library(library)
            validated = m.load_library(payload)
        except (ValueError, TypeError, RecursionError, OverflowError):
            return "Invalid library; nothing was saved."
        problem = atomic_write(self.path, payload)
        if problem: return problem
        self._library, self._seen = validated, payload
        return None


def atomic_write(path, payload):
    """Write bytes atomically without falling back to a destructive direct write."""
    from PySide6.QtCore import QIODevice, QSaveFile as SaveFile
    factory = QSaveFile or SaveFile
    output, path = None, Path(path)
    try:
        if sys.platform == "win32":
            os.makedirs(path.parent, exist_ok=True)
        else:
            missing, directory = [], path.parent
            while not directory.exists():
                missing.append(directory); directory = directory.parent
            for directory in reversed(missing): directory.mkdir(mode=0o700, exist_ok=True)
        output = factory(str(path))
        output.setDirectWriteFallback(False)
        if not output.open(QIODevice.WriteOnly):
            problem = "Could not open library for atomic saving."
        elif output.write(payload) != len(payload):
            problem = "Could not write library; the previous file was preserved."
        elif not output.commit():
            problem = "Could not commit library; the previous file was preserved."
        else: return None
        output.cancelWriting()
        return problem
    except (OSError, RuntimeError):
        if output is not None: output.cancelWriting()
        return "Could not save library; check the folder permissions."
