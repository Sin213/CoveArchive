"""Pure string-level naming only, NOT filesystem containment.

Reparse points, junctions, symlinks, directory swaps, TOCTOU, existing-file
replacement and concurrent writers require filesystem checks in CA-S5.
"""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from pathlib import PurePath, PureWindowsPath, PurePosixPath
import os
import re
import sys
import unicodedata


INDEX_FILENAME = "archive-index.json"
RELEASE_FILENAME = "release.json"
MAX_PATH_BUDGET = 240
MAX_COMPONENT_LENGTH = 120
MAX_RELEASE_FOLDER_LENGTH = 64
MIN_FILENAME_LENGTH = 48
_FORBIDDEN = frozenset('<>:"/\\|?*')
_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_DEVICES = frozenset({"con", "prn", "aux", "nul", "conin$", "conout$"} | {
    prefix + digit for prefix in ("com", "lpt")
    for digit in (*map(str, range(10)), "\u00b9", "\u00b2", "\u00b3")})
_COMPOUND = (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst", ".tar.lz", ".tar.z")


class NamingError(ValueError):
    """Naming failure with a stable code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class PathBudgetError(NamingError):
    """Root or path budget failure."""


def _units(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _take(text, budget):
    used = 0
    for i, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > budget:
            return text[:i]
    return text


def _hash(text):
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:8]


def _replace(text):
    return "".join("_" if c in _FORBIDDEN or unicodedata.category(c) in _CATEGORIES
                   or (unicodedata.category(c) == "Zs" and c != " ") else c for c in text)


def _extension(name):
    for ext in _COMPOUND:
        if len(name) > len(ext) and name[-len(ext):].lower() == ext:
            return name[:-len(ext)], name[-len(ext):]
    match = re.fullmatch(r"(.+)\.([A-Za-z0-9]{1,16})", name)
    return (match[1], "." + match[2]) if match else (name, "")


def _suffix(name, suffix, limit):
    stem, ext = _extension(name)
    stem = _take(stem, limit - _units(suffix + ext))
    if not stem:
        raise NamingError("budget")
    return stem + suffix + ext


def _asset_suffix(name, suffix, limit):
    if sys.platform == "win32":
        return _suffix(name, suffix, limit)
    stem, ext = _extension(name)
    budget = limit - len(os.fsencode(suffix + ext))
    used, end = 0, 0
    for char in stem:
        used += len(os.fsencode(char))
        if used > budget: break
        end += 1
    if not end:
        raise NamingError("budget")
    return stem[:end] + suffix + ext


def sanitize_component(name: str, *, limit=MAX_COMPONENT_LENGTH) -> str:
    if not isinstance(name, str):
        raise TypeError("Component must be a string.")
    if type(limit) is not int or limit < 1:
        raise ValueError("Component limit must be a positive integer.")
    cleaned = _replace(name.strip()).rstrip(". ") or "unnamed"
    # Check before truncation, including compatibility dots in the NFKC form.
    for form in (cleaned, unicodedata.normalize("NFKC", cleaned)):
        base = form.split(".", 1)[0].rstrip(" ")
        if base.casefold() in _DEVICES:
            position = len(cleaned.split(".", 1)[0].rstrip(" "))
            if form != cleaned:
                position = next((i for i in range(1, len(cleaned) + 1)
                    if unicodedata.normalize("NFKC", cleaned[:i]).rstrip(" ") == base), 0)
            cleaned = cleaned[:position] + "_" + cleaned[position:]
            break
    return _suffix(cleaned, "--" + _hash(name), limit) if _units(cleaned) > limit else cleaned


def is_safe_component(name: str, *, limit=MAX_COMPONENT_LENGTH) -> bool:
    return isinstance(name, str) and sanitize_component(name, limit=limit) == name


def name_key(name: str) -> str:
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).casefold())


def release_folder_name(tag: str, release_id: int) -> str:
    if not isinstance(tag, str):
        raise TypeError("Tag must be a string.")
    if type(release_id) is not int:
        raise TypeError("Release id must be an integer.")
    if release_id < 1:
        raise ValueError("Release id must be positive.")
    suffix = "--" + str(release_id)
    budget = MAX_RELEASE_FOLDER_LENGTH - _units("release-" + suffix)
    if budget < 1:
        raise NamingError("budget")
    part = _replace(tag.strip()).strip(". ") or "untagged"
    part = _take(part, budget).strip(". ") or _take("untagged", budget)
    if not part:
        part = "_"
    # The maximal trailing digit run is the id: different ids cannot collide.
    return "release-" + part + suffix


def is_release_folder(folder: str, release_id: int) -> bool:
    return (type(release_id) is int and release_id >= 1 and isinstance(folder, str)
            and folder.startswith("release-") and folder.endswith("--" + str(release_id))
            and is_safe_component(folder, limit=MAX_RELEASE_FOLDER_LENGTH))


def project_folders(owner: str, repo: str) -> tuple[str, str]:
    folders = []
    for original in (owner, repo):
        cleaned = sanitize_component(original)
        if cleaned != original or "+" in original:
            # '+' is outside both GitHub charsets: changed/unchanged namespaces are disjoint.
            # Unvalidated '+' inputs must also leave the unchanged namespace.
            digest = hashlib.sha256(original.casefold().encode("utf-8", "surrogatepass")).hexdigest()[:12]
            cleaned = _take(cleaned, MAX_COMPONENT_LENGTH - 13) + "+" + digest
        folders.append(cleaned)
    return tuple(folders)


@dataclass(frozen=True)
class AssetName:
    asset_id: int
    original_name: str
    saved_name: str
    disambiguated: bool


def allocate_asset_names(assets: Mapping[int, str], *,
                         taken: Mapping[int, str] | None = None,
                         limit=MAX_COMPONENT_LENGTH) -> dict[int, AssetName]:
    bases = {i: sanitize_component(assets[i], limit=limit) for i in sorted(assets)}
    if sys.platform != "win32":
        bases = {i: (_asset_suffix(base, "--" + _hash(assets[i]), limit)
                     if len(os.fsencode(base)) > limit else base) for i, base in bases.items()}
    groups = Counter(name_key(base) for base in bases.values())
    claimed = {name_key(name) for name in (taken or {}).values()} | {name_key(RELEASE_FILENAME)}
    result = {}
    # Reserve all bare singleton names before considering any id suffix.
    for i, base in bases.items():
        key = name_key(base)
        if groups[key] == 1 and key not in claimed:
            result[i] = AssetName(i, assets[i], base, False)
            claimed.add(key)
    for i, base in bases.items():
        if i in result:
            continue
        suffix, attempt = "--" + str(i), 1
        candidate = _asset_suffix(base, suffix, limit)
        while name_key(candidate) in claimed:
            attempt += 1
            candidate = _asset_suffix(base, suffix + "-" + str(attempt), limit)
        result[i] = AssetName(i, assets[i], candidate, True)
        claimed.add(name_key(candidate))
    return {i: result[i] for i in sorted(result)}


def validate_root(root: str | PurePath) -> int:
    if not isinstance(root, (str, PureWindowsPath if sys.platform == "win32" else PurePosixPath)):
        raise PathBudgetError("root")
    text = str(root)
    if sys.platform != "win32":
        path = PurePosixPath(text)
        if (not text.startswith("/") or text.startswith("//") or "\0" in text
                or "\\" in text or str(path) != text or ".." in path.parts):
            raise PathBudgetError("root")
        try:
            text.encode("utf-8", "strict")
            lengths = [len(os.fsencode(p)) for p in path.parts[1:]]
            length = len(os.fsencode(text))
        except UnicodeError as exc:
            raise PathBudgetError("root") from exc
        if any(n > 255 for n in lengths) or length >= 4096:
            raise PathBudgetError("budget")
        return length
    windows = text.replace("/", "\\")
    path = PureWindowsPath(windows)
    if (not path.is_absolute() or windows.startswith(("\\\\?\\", "\\\\.\\"))
            or ".." in windows.split("\\")):
        raise PathBudgetError("root")
    for i, char in enumerate(text):
        if (char in '<>"|?*' or unicodedata.category(char) == "Cc"
                or (char == ":" and not (i == 1 and len(path.drive) == 2))):
            raise PathBudgetError("root")
    return _units(text.rstrip("\\"))


def filename_limit(root: str | PurePath, owner_dir: str, repo_dir: str,
                   folder: str) -> int:
    if sys.platform != "win32":
        prefix = validate_root(root) + sum(len(os.fsencode(c)) for c in (owner_dir, repo_dir, folder)) + 4
        limit = min(MAX_COMPONENT_LENGTH, 255, 4095 - prefix)
        if limit < MIN_FILENAME_LENGTH:
            raise PathBudgetError("budget")
        return limit
    prefix = validate_root(root) + sum(_units(c) for c in (owner_dir, repo_dir, folder)) + 4
    limit = min(MAX_COMPONENT_LENGTH, MAX_PATH_BUDGET - prefix)
    if limit < MIN_FILENAME_LENGTH:
        raise PathBudgetError("budget")
    return limit


def check_path_budget(root: str | PurePath, owner_dir: str, repo_dir: str,
                      folder: str, filename: str) -> None:
    root_length = validate_root(root)
    if sys.platform != "win32":
        lengths = [len(os.fsencode(c)) for c in (owner_dir, repo_dir, folder, filename)]
        if max(lengths) > 255 or root_length + sum(lengths) + 4 >= 4096:
            raise PathBudgetError("budget")
        return
    lengths = [_units(c) for c in (owner_dir, repo_dir, folder, filename)]
    if max(lengths) > MAX_COMPONENT_LENGTH or root_length + sum(lengths) + 4 > MAX_PATH_BUDGET:
        raise PathBudgetError("budget")
