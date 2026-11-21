"""joplin_search (#97): where the match is, Joplin's search words, and the notebook filter."""

from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fakes import FakeJoplin
from fastmcp import Client
from fastmcp.exceptions import ToolError

from second_brain_joplin.models import MAX_SEARCH_PAGES
from second_brain_joplin.search import first_match, keywords, like, notebook_terms, snippet
from second_brain_joplin.server import create_server

BODY = "Intro.\n\n# Plan\n\nWe buy the apple trees in spring.\n\n## Budget\n\nApple cost: 40.\n"


@pytest.fixture(autouse=True)
def _no_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SBJ_NOTEBOOKS", "SBJ_EXCLUDE_NOTEBOOKS", "SBJ_WRITE_NOTEBOOKS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def joplin() -> Iterator[FakeJoplin]:
    """Parent ─ Kid, and Other; a note in each."""
    with respx.mock:
        fake = FakeJoplin()
        parent = fake.notebook("Parent")
        kid = fake.notebook("Kid", parent=parent)
        other = fake.notebook("Other")
        fake.ids = {"parent": parent, "kid": kid, "other": other}
        fake.note("In parent", "apple parent", parent)
        fake.note("In kid", BODY, kid, updated_time=1_700_000_000_000)
        fake.note("In other", "apple other", other)
        fake.install()
        yield fake


async def _search(query: str, **args: Any) -> list[dict[str, Any]]:
    async with Client(create_server()) as client:
        result = await client.call_tool("joplin_search", {"query": query, **args})
    return list(result.structured_content["result"])


def _titles(results: list[dict[str, Any]]) -> set[str]:
    return {r["title"] for r in results}


# -- where the match is --------------------------------------------------------------


async def test_a_hit_carries_its_notebook_time_snippet_and_section(joplin: FakeJoplin) -> None:
    [hit] = await _search("trees")
    assert hit["notebook_id"] == joplin.ids["kid"]
    assert hit["updated_time"].startswith("2023-11-14")
    assert "apple trees in spring" in hit["snippet"]
    assert (hit["section"], hit["section_heading"]) == ("1", "Plan")
    assert hit["excerpt"].startswith("Intro.")  # kept as it was


async def test_the_section_is_the_innermost_one(joplin: FakeJoplin) -> None:
    [hit] = await _search("cost")
    assert (hit["section"], hit["section_heading"]) == ("1.1", "Budget")


async def test_a_filter_only_query_has_no_snippet_or_section(joplin: FakeJoplin) -> None:
    hits = await _search('notebook:"Kid"')
    assert _titles(hits) == {"In kid"}
    assert (hits[0]["snippet"], hits[0]["section"], hits[0]["section_heading"]) == (
        None,
        None,
        None,
    )


async def test_a_section_from_search_is_one_joplin_read_accepts(joplin: FakeJoplin) -> None:
    [hit] = await _search("cost")
    async with Client(create_server()) as client:
        note = await client.call_tool(
            "joplin_read", {"note_id": hit["id"], "section": hit["section"]}
        )
        by_heading = await client.call_tool(
            "joplin_read", {"note_id": hit["id"], "section": hit["section_heading"]}
        )
    assert note.structured_content["body"].startswith("## Budget")
    assert by_heading.structured_content["body"] == note.structured_content["body"]


async def test_the_limit_now_goes_to_100(joplin: FakeJoplin) -> None:
    for i in range(120):
        joplin.note(f"n{i}", "pear", joplin.ids["other"])
    assert len(await _search("pear", limit=100)) == 100


async def test_the_description_lists_joplins_search_words() -> None:
    async with Client(create_server()) as client:
        [tool] = [t for t in await client.list_tools() if t.name == "joplin_search"]
    for word in ("notebook:", "tag:", "type:todo", "iscompleted:", "created:", "-"):
        assert word in (tool.description or "")


# -- Joplin's notebook: word under the filter ------------------------------------------


async def test_without_a_filter_notebook_terms_go_to_joplin_as_they_are(joplin: FakeJoplin) -> None:
    hits = await _search('apple notebook:"Parent"')
    assert _titles(hits) == {"In parent", "In kid"}  # Joplin takes in sub-notebooks
    assert joplin.queries == ['apple notebook:"Parent"']


@pytest.mark.parametrize(
    "query",
    [
        'apple notebook:"Parent"',
        'apple notebook:"Par%"',
        "apple notebook:par*",
        'apple notebook:"Nope"',
    ],
    ids=["hidden parent", "LIKE pattern", "wildcard", "missing"],
)
async def test_a_hidden_notebook_name_answers_like_a_missing_one(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    monkeypatch.setenv("SBJ_NOTEBOOKS", "Kid")  # Parent is hidden, its child Kid visible
    assert await _search(query) == []
    assert joplin.count("/search") == 0  # the same requests for every one of them
    assert joplin.count("/folders") == 1


async def test_a_visible_notebook_name_still_works_under_the_filter(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBJ_NOTEBOOKS", "Kid")
    assert _titles(await _search('apple notebook:"Kid"')) == {"In kid"}
    assert _titles(await _search("apple notebook:K*")) == {"In kid"}


async def test_a_hidden_notebook_with_the_same_title_adds_nothing(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    twin = joplin.notebook("Kid")  # a second "Kid", top level
    joplin.note("In hidden twin", "apple twin", twin)
    monkeypatch.setenv("SBJ_EXCLUDE_NOTEBOOKS", twin)
    assert _titles(await _search('apple notebook:"Kid"')) == {"In kid"}


async def test_a_pattern_that_also_matches_a_hidden_parent_keeps_only_visible_matches(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`%` matches Parent (hidden) and Kid and Other (visible): Kid's notes come by
    their own match, never as Parent's sub-notebook."""
    monkeypatch.setenv("SBJ_NOTEBOOKS", "Kid,Other")
    assert _titles(await _search("apple notebook:%")) == {"In kid", "In other"}


async def test_a_negated_hidden_notebook_excludes_nothing(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBJ_NOTEBOOKS", "Kid,Other")
    hidden = await _search('apple -notebook:"Parent"')
    missing = await _search('apple -notebook:"Nope"')
    assert _titles(hidden) == _titles(missing) == {"In kid", "In other"}
    assert joplin.queries == ["apple", "apple"]  # the -notebook: term never reached Joplin


async def test_a_negated_visible_notebook_is_left_out(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBJ_NOTEBOOKS", "Kid,Other")
    assert _titles(await _search('apple -notebook:"Kid"')) == {"In other"}


async def test_a_query_of_only_a_negated_notebook_is_refused_under_a_filter(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBJ_NOTEBOOKS", "Kid")
    async with Client(create_server()) as client:
        with pytest.raises(ToolError, match="besides -notebook:"):
            await client.call_tool("joplin_search", {"query": '-notebook:"Kid"'})


async def test_no_snippet_comes_from_a_hidden_note(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBJ_EXCLUDE_NOTEBOOKS", "Other")
    hits = await _search("apple")
    assert "other" not in " ".join(str(h) for h in hits).lower()


async def test_a_search_looks_at_no_more_than_the_page_cap(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(MAX_SEARCH_PAGES * 100 + 250):
        joplin.note(f"h{i}", "plum", joplin.ids["other"])
    monkeypatch.setenv("SBJ_EXCLUDE_NOTEBOOKS", "Other")  # every hit is hidden
    assert await _search("plum") == []
    assert joplin.count("/search") == MAX_SEARCH_PAGES


# -- the pieces --------------------------------------------------------------------


def test_notebook_terms_are_split_out_and_negated_ones_removed() -> None:
    terms = notebook_terms('apple Notebook:"A B" -notebook:C  tag:x')
    assert terms.include == ["A B"]
    assert terms.exclude == ["C"]
    assert terms.query == 'apple Notebook:"A B" tag:x'


def test_like_is_joplins_like() -> None:
    assert like("par%").fullmatch("Parent")
    assert like("P_rent").fullmatch("parent")
    assert like("Par*").fullmatch("PARENT")
    assert not like("Par").fullmatch("Parent")
    assert like("a.b").fullmatch("a.b") and not like("a.b").fullmatch("axb")


def test_keywords_leave_out_filters_and_negated_words() -> None:
    assert keywords('apple "big tree" -pear tag:x notebook:"A B" app*') == [
        "big tree",
        "apple",
        "app",
    ]
    assert keywords('notebook:"Kid"') == []


def test_first_match_and_snippet() -> None:
    body = "x" * 500 + "Needle here" + "y" * 500
    at = first_match(body, ["needle"])
    assert at == 500
    text = snippet(body, at)
    assert "Needle here" in text and text.startswith("…") and text.endswith("…")
    assert len(text) <= 202
    assert snippet("short   text\nhere", 0) == "short text here"
    assert first_match("abc", ["zz"]) is None
