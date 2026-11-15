"""Bounded, versioned local preferences with atomic, non-destructive writes."""
import json
import os
from pathlib import Path
import re

from PySide6.QtCore import QIODevice, QSaveFile
from covearchive.archive.naming import validate_root
from covearchive.build_filters import BuildRules
from covearchive.formats import SUPPORTED_EXTENSIONS

MAX_BYTES = 256 * 1024
_READ_ONLY = "Preferences are read-only until the file is fixed or moved."


def _defaults():
    return {"version": 1, "archive_root": None,
            "window": {"width": 1180, "height": 720, "tab": "browse"}, "repositories": {}}


def _keys(obj, expected):
    if not isinstance(obj, dict) or set(obj) != set(expected):
        raise ValueError("Invalid preference fields.")


def _validated(data):
    _keys(data, ("version", "archive_root", "window", "repositories"))
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("Unsupported preference version.")
    root = data["archive_root"]
    if root is not None:
        if not isinstance(root, str) or len(root) > 4096:
            raise ValueError("Invalid archive root.")
        validate_root(root)
    window = data["window"]
    _keys(window, ("width", "height", "tab"))
    if (type(window["width"]) is not int or type(window["height"]) is not int
            or window["tab"] not in ("browse", "queue")):
        raise ValueError("Invalid window preferences.")
    repos = data["repositories"]
    if not isinstance(repos, dict) or len(repos) > 500:
        raise ValueError("Use at most 500 saved repositories.")
    normalized = {}
    for key, rule in repos.items():
        if (not isinstance(key, str) or re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?/[a-z0-9._-]{1,100}", key) is None
                or key.split("/")[1] in (".", "..")):
            raise ValueError("Invalid normalized repository key.")
        _keys(rule, ("include", "exclude", "extensions", "prereleases"))
        rules = BuildRules(rule["include"], rule["exclude"])
        extensions = rule["extensions"]
        if (not isinstance(extensions, list) or len(extensions) > len(SUPPORTED_EXTENSIONS)
                or any(not isinstance(ext, str) or ext not in SUPPORTED_EXTENSIONS for ext in extensions)
                or len(set(extensions)) != len(extensions) or type(rule["prereleases"]) is not bool):
            raise ValueError("Invalid formats or prerelease choice.")
        normalized[key] = {"include": list(rules.include), "exclude": list(rules.exclude),
                           "extensions": list(extensions), "prereleases": rule["prereleases"]}
    return {"version": 1, "archive_root": root, "repositories": normalized,
            "window": {"width": max(820, min(3840, window["width"])),
                       "height": max(520, min(2160, window["height"])), "tab": window["tab"]}}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError("Duplicate preference field.")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("Non-finite JSON number.")


class PreferenceStore:
    """load -> (dict, problem or None, writable); save -> problem or None.

    A failed load latches read-only for this instance. No defaults are written
    automatically, and save always loads first to protect an existing file.
    """
    def __init__(self, path=None):
        local = os.environ.get("LOCALAPPDATA")
        self.path = Path(path) if path is not None else (Path(local)/"CoveArchive"/"preferences.json" if local else None)
        self.writable = self.path is not None
        self.problem = None if self.writable else "LOCALAPPDATA is unset; preferences are in-memory only."
        self._loaded = False
        self._seen = None
        self._preferences = _defaults()

    def load(self):
        if not self._loaded:
            self._loaded = True
            if self.path is not None:
                try:
                    with self.path.open("rb") as stream: raw = stream.read(MAX_BYTES + 1)
                    self._seen = raw
                    if len(raw) > MAX_BYTES: raise ValueError("Preferences exceed 256 KiB.")
                    self._preferences = _validated(json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                                                              parse_constant=_nonfinite))
                except FileNotFoundError:
                    pass
                except (OSError, ValueError, TypeError, RecursionError, OverflowError):
                    self.writable = False
                    self.problem = f"Could not load preferences at {self.path}. {_READ_ONLY}"
        # JSON round-trip returns an independent snapshot with no shared lists.
        return json.loads(json.dumps(self._preferences)), self.problem, self.writable

    def save(self, preferences):
        self.load()
        if not self.writable: return self.problem or _READ_ONLY
        try:
            with self.path.open("rb") as stream: current = stream.read(MAX_BYTES + 1)
        except FileNotFoundError: current = None
        except OSError: return "Could not read preferences to verify them; nothing was saved, try again"
        # Narrows the external-writer race; this is not atomic compare-and-replace.
        if current != self._seen:
            self.writable = False
            self.problem = "Preferences changed on disk; restart CoveArchive to reload; the file was not overwritten."
            return self.problem
        try:
            validated = _validated(preferences)
            payload = (json.dumps(validated, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
            if len(payload) > MAX_BYTES: raise ValueError("Preferences exceed 256 KiB.")
        except (ValueError, TypeError, RecursionError, OverflowError):
            return "Invalid preferences; check filename rules, formats and archive root."
        output = None
        try:
            os.makedirs(self.path.parent, exist_ok=True)
            output = QSaveFile(str(self.path))
            output.setDirectWriteFallback(False)
            if not output.open(QIODevice.WriteOnly): return "Could not open preferences for atomic saving."
            if output.write(payload) != len(payload):
                output.cancelWriting()
                return "Could not write preferences; the previous file was preserved."
            if not output.commit(): return "Could not commit preferences; the previous file was preserved."
        except OSError:
            if output is not None: output.cancelWriting()
            return "Could not save preferences; check the folder permissions."
        self._preferences = validated
        self._seen = payload
        return None
