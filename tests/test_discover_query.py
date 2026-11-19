"""Search text is data, never GitHub query syntax."""

from dataclasses import FrozenInstanceError
from urllib.parse import parse_qsl, urlsplit

import pytest

from covearchive.discover.query import SEARCH_ENDPOINT, SearchQueryError, build_search_query


CLAUSES = {
    "all": '(recomp OR recompilation OR decompilation OR "PC port" OR "source port" OR emulator)',
    "recomps": '(recomp OR recompilation OR recompiled OR "static recompilation")',
    "ports": '("PC port" OR "source port" OR "native port" OR "game port")',
    "decomps": '(decompilation OR decomp OR decompiled)',
    "emulators": '(emulator OR emulation)',
}


def test_title_search_is_immutable_and_public():
    query = build_search_query("  Zelda 64  ")
    assert query.text == "Zelda 64"
    assert (query.kind, query.sort, query.page, query.per_page) == ("all", "relevance", 1, 30)
    assert query.q == f"Zelda 64 {CLAUSES['all']} in:name,description,topics is:public"
    assert tuple(query.params) == (("q", query.q), ("per_page", 30), ("page", 1))
    with pytest.raises(FrozenInstanceError):
        query.q = "is:private"
    with pytest.raises(TypeError):
        query.params[0] = ("q", "is:private")


@pytest.mark.parametrize("text,cleaned", [
    ("user:foo org:bar repo:a/b", "user foo org bar repo a b"),
    ("fork:true is:private in:readme stars:>1 language:C++", "fork true is private in readme stars 1 language C"),
    ('ＵＳＥＲ：foo ｉｓ：private ＂OR＂ （repo：a／b）', "USER foo is private repo a b"),
    ('"a" (b) -term * > c', "a b term c"),
    ("AND OR NOT Zelda AND OR NOT", "Zelda"),
    ("__Zelda__ ..N64.. --Recomp--", "Zelda N64 Recomp"),
    ("é\u00a0東京\u2009N64.recomp-test_1", "é 東京 N64.recomp-test_1"),
    ("ＡＮＤ ＯＲ ＮＯＴ mario", "mario"),
])
def test_qualifiers_and_operators_are_neutralized(text, cleaned):
    query = build_search_query(text)
    assert query.text == cleaned
    assert query.q == f"{cleaned} {CLAUSES['all']} in:name,description,topics is:public"
    assert sum(token in {"AND", "OR", "NOT"} for token in query.q.split()) == 5
    assert query.q.endswith(" is:public") and "fork:" not in query.q


@pytest.mark.parametrize("kind,clause", CLAUSES.items())
def test_each_kind_has_exact_fixed_clause(kind, clause):
    query = build_search_query("Mario", kind)
    assert query.q == f"Mario {clause} in:name,description,topics is:public"
    assert len(query.q) <= 256
    assert sum(word in {"AND", "OR", "NOT"} for word in query.q.split()) <= 5


@pytest.mark.parametrize("sort,extra", [
    ("relevance", []), ("stars", [("sort", "stars"), ("order", "desc")]),
    ("updated", [("sort", "updated"), ("order", "desc")]),
])
def test_exact_url_encoding_and_parameter_order(sort, extra):
    query = build_search_query("Zélda 64", "emulators", sort, page=2, per_page=10)
    encoded = ("Z%C3%A9lda%2064%20%28emulator%20OR%20emulation%29%20"
               "in%3Aname%2Cdescription%2Ctopics%20is%3Apublic")
    middle = "" if sort == "relevance" else f"&sort={sort}&order=desc"
    assert query.url == f"{SEARCH_ENDPOINT}?q={encoded}{middle}&per_page=10&page=2"
    assert tuple(query.params) == tuple([("q", query.q)] + extra + [("per_page", 10), ("page", 2)])
    assert parse_qsl(urlsplit(query.url).query) == [(k, str(v)) for k, v in query.params]


@pytest.mark.parametrize("text,code", [
    ("", "empty"), ("   ", "empty"), ("***", "no_terms"),
    ("AND OR NOT", "no_terms"), ("._-", "no_terms"),
    ("x" * 257, "too_long"), ("x" * 101, "too_long"),
    (" " * 257, "too_long"), (None, "type"), (12, "type"),
    ("x\u202ey", "control_char"), ("x\u200by", "control_char"),
    ("x\x00y", "control_char"), ("\tMario", "control_char"),
    ("Mario\n", "control_char"), ("x\ud800", "control_char"),
    ("x\ue000", "control_char"), ("x\u0378", "control_char"),
    ("x\u2028", "control_char"), ("x\u2029", "control_char"),
])
def test_bad_text_has_stable_error_code(text, code):
    with pytest.raises(SearchQueryError) as caught:
        build_search_query(text)
    assert caught.value.code == code
    assert isinstance(caught.value, ValueError)


@pytest.mark.parametrize("options,code", [
    ({"kind": "port"}, "kind"), ({"kind": []}, "kind"),
    ({"sort": "forks"}, "sort"), ({"sort": None}, "sort"),
    ({"page": True}, "page"), ({"page": 0}, "page"),
    ({"page": 1.0}, "page"), ({"page": "1"}, "page"),
    ({"per_page": False}, "per_page"), ({"per_page": 0}, "per_page"),
    ({"per_page": 101}, "per_page"), ({"per_page": 1.0}, "per_page"),
    ({"page": 34, "per_page": 30}, "page"),
    ({"page": 11, "per_page": 100}, "page"),
])
def test_invalid_options(options, code):
    with pytest.raises(SearchQueryError) as caught:
        build_search_query("Mario", **options)
    assert caught.value.code == code


@pytest.mark.parametrize("page,per_page", [(1000, 1), (10, 100), (33, 30), (1, 1)])
def test_result_cap_boundaries(page, per_page):
    query = build_search_query("Mario", page=page, per_page=per_page)
    assert (query.page, query.per_page) == (page, per_page)


def test_maximum_cleaned_text_and_raw_boundary():
    assert build_search_query("x" * 100).text == "x" * 100
    assert build_search_query(" " * 255 + "x").text == "x"


def test_defensive_clause_limits(monkeypatch):
    from covearchive.discover import query

    monkeypatch.setitem(query._KIND_CLAUSES, "all", "x" * 240)
    with pytest.raises(SearchQueryError, match="query_too_long"):
        build_search_query("Mario")
    monkeypatch.setitem(query._KIND_CLAUSES, "all", "(a OR b OR c OR d OR e OR f OR g)")
    with pytest.raises(SearchQueryError, match="too_many_operators"):
        build_search_query("Mario")
