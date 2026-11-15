"""Bounded shell-style filename rules; never filesystem globs or regex.

Use [,] for a literal comma. Matching is ASCII-insensitive only; *win*
therefore also matches darwin and does not identify an operating system.
"""
from dataclasses import dataclass
from fnmatch import fnmatchcase
import unicodedata

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

class BuildFilterError(ValueError):
    """An actionable rule validation failure with a stable code."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def validate_patterns(patterns):
    if not isinstance(patterns, (list, tuple)):
        raise BuildFilterError("patterns", "Provide a list of filename patterns.")
    if len(patterns) > 16:
        raise BuildFilterError("count", "Use at most 16 patterns in each include/exclude list.")
    for pattern in patterns:
        if not isinstance(pattern, str) or not 1 <= len(pattern) <= 128:
            raise BuildFilterError("length", "Each filename pattern must contain 1 to 128 characters.")
        if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in pattern):
            raise BuildFilterError("control", "Remove control characters from filename patterns.")
        if "/" in pattern or "\\" in pattern:
            raise BuildFilterError("path", "Match filenames only; remove path separators.")
        if pattern.count("*") > 8:
            raise BuildFilterError("wildcards", "Use at most 8 stars in each filename pattern.")
    return tuple(patterns)


def parse_patterns(text):
    if not isinstance(text, str):
        raise BuildFilterError("text", "Enter comma-separated filename patterns.")
    # Protect the documented literal-comma spelling before separating patterns.
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in text):
        raise BuildFilterError("control", "Remove control characters from filename patterns.")
    parts = [p.replace("\x00", "[,]") for p in text.replace("[,]", "\x00").split(",")]
    return validate_patterns(tuple(dict.fromkeys(p.strip() for p in parts if p.strip())))


@dataclass(frozen=True)
class BuildRules:
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "include", validate_patterns(self.include))
        object.__setattr__(self, "exclude", validate_patterns(self.exclude))


def allowed(rules, filename):
    name = filename.translate(_ASCII_LOWER)
    def matches(pattern): return fnmatchcase(name, pattern.translate(_ASCII_LOWER))
    return (not rules.include or any(map(matches, rules.include))) and not any(map(matches, rules.exclude))
