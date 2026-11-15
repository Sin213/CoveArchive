"""Interpreter-independent nesting bounds for pre-decoded JSON."""

import json
from time import perf_counter

import pytest

from covearchive.jsondepth import MAX_JSON_DEPTH, JsonNestingError, check_json_depth


@pytest.mark.parametrize("kind", ["array", "object", "mixed"])
@pytest.mark.parametrize("extra", [0, 1])
def test_nesting_limit(kind, extra):
    depth = MAX_JSON_DEPTH + extra
    opens = ["[" if kind == "array" or kind == "mixed" and i % 2 else '{"a":'
             for i in range(depth)]
    text = "".join(opens) + "0" + "".join("]" if part == "[" else "}" for part in reversed(opens))
    if extra:
        with pytest.raises(JsonNestingError): check_json_depth(text)
    else:
        check_json_depth(text)
        json.loads(text)


@pytest.mark.parametrize("value", ["[]{}" * 1000, '"' + "[" * 1000,
    "\\" + "{" * 1000, '\\"' + "[" * 1000, "\\\\", "\\u005b", "\u005b"])
def test_string_brackets_and_escapes(value):
    text = "[" * MAX_JSON_DEPTH + json.dumps(value) + "]" * MAX_JSON_DEPTH
    check_json_depth(text)
    json.loads(text)


def test_unicode_escape_and_deep_nesting_after_string():
    check_json_depth(r'"\u005b\u005d\u007b\u007d"')
    text = '[' + json.dumps('[]{}\\"' * 1000) + ',' + '[' * MAX_JSON_DEPTH + '0' + ']' * (MAX_JSON_DEPTH + 1)
    with pytest.raises(JsonNestingError): check_json_depth(text)


@pytest.mark.parametrize("text", ['', '{', '[}', ']}', '"', '"\\', '"\\"[[[',
    '"\\uZZZZ[{}]', '"unterminated' + '[' * 10000, '[1,,2]', '"line\n[break'])
def test_malformed_input_leaves_syntax_to_decoder(text):
    check_json_depth(text)
    with pytest.raises(ValueError): json.loads(text)


def test_huge_depth_exits_early():
    text = '[' * 1_000_000 + '0' + ']' * 1_000_000
    start = perf_counter()
    with pytest.raises(JsonNestingError) as exc: check_json_depth(text)
    assert isinstance(exc.value, RecursionError)
    assert perf_counter() - start < 1
