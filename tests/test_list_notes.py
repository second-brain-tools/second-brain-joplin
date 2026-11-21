"""joplin_list_notes (#84): the notes in one notebook, a page at a time."""

from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fakes import FakeJoplin
from fastmcp import Client
from fastmcp.exceptions import ToolError

from second_brain_joplin.server import create_server


@pytest.fixture(autouse=True)
def _no_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SBJ_NOTEBOOKS", "SBJ_EXCLUDE_NOTEBOOKS", "SBJ_WRITE_NOTEBOOKS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def joplin() -> Iterator[FakeJoplin]:
    with respx.mock:
        fake = FakeJoplin()
        fake.install()
        yield fake


async def _list(**args: Any) -> dict[str, Any]:
    async with Client(create_server()) as client:
        result = await client.call_tool("joplin_list_notes", args)
    content: dict[str, Any] = result.structured_content
    return content


async def _refused(**args: Any) -> str:
    async with Client(create_server()) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool("joplin_list_notes", args)
    return str(raised.value)


async def test_a_large_notebook_lists_in_full_by_following_next_offset(joplin: FakeJoplin) -> None:
    inbox = joplin.notebook("Inbox")
    made = {joplin.note(f"n{i}", "", inbox, updated_time=1000 + i) for i in range(230)}
    seen: list[str] = []
    offset: int | None = 0
    while offset is not None:
        page = await _list(notebook="Inbox", limit=100, offset=offset)
        assert page["total"] == 230
        seen += [n["id"] for n in page["notes"]]
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == 230
    assert set(seen) == made


async def test_newest_first_by_default_and_by_title_on_request(joplin: FakeJoplin) -> None:
    inbox = joplin.notebook("Inbox")
    joplin.note("banana", "", inbox, updated_time=3)
    joplin.note("Apple", "", inbox, updated_time=1)
    joplin.note("cherry", "", inbox, updated_time=2)
    assert [n["title"] for n in (await _list(notebook="Inbox"))["notes"]] == [
        "banana",
        "cherry",
        "Apple",
    ]
    titles = [n["title"] for n in (await _list(notebook="Inbox", order="title"))["notes"]]
    assert titles == ["Apple", "banana", "cherry"]


async def test_the_trash_conflict_copies_and_sub_notebooks_are_left_out(joplin: FakeJoplin) -> None:
    inbox = joplin.notebook("Inbox")
    sub = joplin.notebook("Sub", parent=inbox)
    keep = joplin.note("keep", "", inbox)
    trashed = joplin.note("trashed", "", inbox)
    joplin.notes[trashed]["deleted_time"] = 5
    conflict = joplin.note("conflict", "", inbox)
    joplin.notes[conflict]["is_conflict"] = 1
    joplin.note("in sub", "", sub)
    todo = joplin.note("todo", "", inbox)
    joplin.notes[todo]["is_todo"] = 1
    page = await _list(notebook=inbox)
    assert {n["id"] for n in page["notes"]} == {keep, todo}
    assert page["total"] == 2
    assert {n["title"]: n["is_todo"] for n in page["notes"]} == {"keep": False, "todo": True}
    assert (page["notebook_id"], page["notebook"]) == (inbox, "Inbox")


async def test_a_hidden_notebook_answers_like_a_missing_one(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = joplin.notebook("Private")
    joplin.note("secret", "", private)
    joplin.notebook("Inbox")
    monkeypatch.setenv("SBJ_EXCLUDE_NOTEBOOKS", "Private")
    by_title, by_id, missing = (
        await _refused(notebook="Private"),
        await _refused(notebook=private),
        await _refused(notebook="Nope"),
    )
    assert by_title == by_id == missing == "No notebook has that title or id."
    assert joplin.count(f"/folders/{private}/notes") == 0


async def test_a_shared_title_is_refused_and_the_id_works(joplin: FakeJoplin) -> None:
    first = joplin.notebook("Ideas")
    joplin.notebook("Ideas")
    joplin.note("x", "", first)
    assert "use the id" in await _refused(notebook="Ideas")
    assert (await _list(notebook=first))["total"] == 1


async def test_a_title_a_hidden_notebook_shares_is_not_ambiguous(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    visible = joplin.notebook("Ideas")
    hidden = joplin.notebook("Ideas")
    monkeypatch.setenv("SBJ_EXCLUDE_NOTEBOOKS", hidden)
    assert (await _list(notebook="Ideas"))["notebook_id"] == visible


async def test_limit_bounds(joplin: FakeJoplin) -> None:
    joplin.notebook("Inbox")
    for limit in (0, 101):
        await _refused(notebook="Inbox", limit=limit)
    assert (await _list(notebook="Inbox", limit=100))["notes"] == []
