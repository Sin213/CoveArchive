"""Bound JSON nesting before parsing, independently of the Python JSON scanner."""

import re

MAX_JSON_DEPTH = 64
# Consume whole strings, including escaped characters and unfinished strings.
# Possessive runs avoid backtracking storage for large, escape-heavy strings.
_TOKENS = re.compile(r'"[^"\\]*+(?:\\.[^"\\]*+)*+(?:"|\\?\Z)|[\[\]{}]', re.DOTALL)


class JsonNestingError(RecursionError):
    """JSON containers exceed the supported nesting depth."""


def check_json_depth(text: str) -> None:
    """Check pre-decoded JSON in linear time; leave syntax validation to json.loads."""
    depth = 0
    for token in _TOKENS.finditer(text):
        char = text[token.start()]
        if char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise JsonNestingError("JSON nesting limit exceeded.")
        elif char in "]}":
            depth -= 1
