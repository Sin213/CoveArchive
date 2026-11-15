"""Filename rule semantics and bounded input validation."""
from dataclasses import FrozenInstanceError
import pytest
from covearchive.build_filters import BuildRules, BuildFilterError, allowed, parse_patterns, validate_patterns

@pytest.mark.parametrize("includes,excludes,name,expected", [
    ((), (), "any.zip", True), (("*win64*", "*windows*"), (), "APP.WINDOWS.zip", True),
    (("*win*",), ("*debug*",), "win-debug.zip", False), (("*win*",), (), "darwin.zip", True),
    (("[ab]?.zip",), (), "A1.ZIP", True), (("[!a]*",), (), "abc", False),
    (("*\u00c4*",), (), "x\u00e4.zip", False), (("*\u00c4*",), (), "x\u00c4.zip", True),
    (("a[,]b.zip",), (), "a,b.zip", True), (("*.zip",), (), "a.tar.gz", False),
])
def test_matching(includes, excludes, name, expected):
    assert allowed(BuildRules(includes, excludes), name) is expected

@pytest.mark.parametrize("patterns", [("",), ("x"*129,), tuple(str(i) for i in range(17)),
    ("a/b",), ("a\\b",), ("a\n",), ("a\x00",), ("a\x7f",), ("*"*9,), (1,), "*.zip"])
def test_validation(patterns):
    with pytest.raises(BuildFilterError) as caught: validate_patterns(patterns)
    assert caught.value.code and str(caught.value)

def test_parsing_and_immutable_snapshot():
    assert parse_patterns(" *.zip, ,*.zip, a[,]b.zip, *.tar.gz ") == ("*.zip", "a[,]b.zip", "*.tar.gz")
    source=["*.zip"]; rules=BuildRules(source, []); source.clear()
    assert rules.include == ("*.zip",) and allowed(rules, "a.zip")
    with pytest.raises(FrozenInstanceError): rules.include=()
    with pytest.raises(BuildFilterError): BuildRules((), ("bad/path",))


@pytest.mark.parametrize("text", ["ok, bad\n", "ok, bad\x00", "ok, bad\t", "x"*129,
    ",".join(str(i) for i in range(17))])
def test_parser_rejects_invalid_input_without_losing_valid_rules(text):
    previous=BuildRules(("*.zip",), ())
    with pytest.raises(BuildFilterError): parse_patterns(text)
    assert previous.include == ("*.zip",) and allowed(previous, "a.zip")


def test_boundary_patterns_and_original_extensions():
    rules=BuildRules(tuple("x"*126+str(i).zfill(2) for i in range(16)), ("*"*8,))
    assert len(rules.include)==16 and len(rules.include[0])==128
    assert not allowed(rules, rules.include[0])
    assert parse_patterns("a[,]b, foo,]") == ("a[,]b", "foo", "]")
