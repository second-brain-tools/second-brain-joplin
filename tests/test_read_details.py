"""joplin_read's facts beyond the text, and joplin_read_notes (#85)."""

import hashlib
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fakes import FakeJoplin
from fastmcp import Client
from fastmcp.exceptions import ToolError

from second_brain_joplin.models import READ_NOTES_BUDGET, READ_NOTES_MAX
from second_brain_joplin.server import create_server
from second_brain_joplin.tools.read import share_budget

DAY_MS = 86_400_000


@pytest.fixture(autouse=True)
def _no_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SBJ_NOTEBOOKS", "SBJ_EXCLUDE_NOTEBOOKS", "SBJ_WRITE_NOTEBOOKS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def joplin() -> Iterator[FakeJoplin]:
    """Projects ─ Barn ─ Kitchen, and Private."""
    with respx.mock:
        fake = FakeJoplin()
        projects = fake.notebook("Projects")
        barn = fake.notebook("Barn", parent=projects)
        kitchen = fake.notebook("Kitchen", parent=barn)
        private = fake.notebook("Private")
        fake.ids = {"projects": projects, "barn": barn, "kitchen": kitchen, "private": private}  # type: ignore[attr-defined]
        fake.install()
        yield fake


async def _read(ref: str) -> dict[str, Any]:
    async with Client(create_server()) as client:
        result = await client.call_tool("joplin_read", {"note_id": ref})
    content: dict[str, Any] = result.structured_content
    return content


async def _read_notes(refs: list[str]) -> dict[str, Any]:
    async with Client(create_server()) as client:
        result = await client.call_tool("joplin_read_notes", {"note_ids": refs})
    content: dict[str, Any] = result.structured_content
    return content


async def test_a_plain_note_says_its_notebook_path_and_checksum(joplin: FakeJoplin) -> None:
    note = joplin.note("Cabinets", "Three quotes.", joplin.ids["kitchen"])  # type: ignore[attr-defined]
    read = await _read(note)
    assert read["notebook"] == "Kitchen"
    assert read["notebook_path"] == ["Projects", "Barn", "Kitchen"]
    assert read["body_sha256"] == hashlib.sha256(b"Three quotes.").hexdigest()
    assert (read["tags"], read["attachments"], read["is_todo"]) == ([], [], False)
    assert (read["todo_due"], read["todo_completed"]) == (None, None)


async def test_the_checksum_is_of_the_whole_body_at_any_offset(joplin: FakeJoplin) -> None:
    body = "x" * 300
    note = joplin.note("Long", body, joplin.ids["barn"])  # type: ignore[attr-defined]
    async with Client(create_server()) as client:
        page = await client.call_tool(
            "joplin_read", {"note_id": note, "offset": 100, "max_chars": 10}
        )
    assert page.structured_content["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()


async def test_tags_and_to_do_fields(joplin: FakeJoplin) -> None:
    open_todo = joplin.note("Call Sam", "", joplin.ids["barn"])  # type: ignore[attr-defined]
    joplin.notes[open_todo].update(is_todo=1, todo_due=1_800_000_000_000, todo_completed=0)
    done = joplin.note("Order wood", "", joplin.ids["barn"])  # type: ignore[attr-defined]
    joplin.notes[done].update(is_todo=1, todo_due=0, todo_completed=1_700_000_000_000)
    joplin.note_tags[open_todo] = ["urgent", "barn"]
    read = await _read(open_todo)
    assert read["is_todo"] is True
    assert read["todo_due"].startswith("2027-01-15")
    assert read["todo_completed"] is None
    assert read["tags"] == ["barn", "urgent"]
    finished = await _read(done)
    assert (finished["todo_due"], finished["todo_completed"][:10]) == (None, "2023-11-14")


async def test_two_attachments_are_listed_without_their_content(joplin: FakeJoplin) -> None:
    note = joplin.note("Photos", "![a](:/x) ![b](:/y)", joplin.ids["barn"])  # type: ignore[attr-defined]
    joplin.attach(note, "front.jpg", "image/jpeg")
    joplin.attach(note, "plan.pdf", "application/pdf")
    read = await _read(note)
    assert [(a["title"], a["mime"], a["size"]) for a in read["attachments"]] == [
        ("front.jpg", "image/jpeg", 1234),
        ("plan.pdf", "application/pdf", 1234),
    ]


async def test_the_path_starts_at_the_topmost_visible_notebook(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = joplin.note("Cabinets", "text", joplin.ids["kitchen"])  # type: ignore[attr-defined]
    monkeypatch.setenv("SBJ_NOTEBOOKS", "Kitchen")
    read = await _read(note)
    assert read["notebook_path"] == ["Kitchen"]
    assert "Projects" not in str(read) and "Barn" not in str(read)


async def test_an_attachment_a_hidden_note_also_uses_is_left_out(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    visible = joplin.note("Visible", "![](:/r)", joplin.ids["barn"])  # type: ignore[attr-defined]
    hidden = joplin.note("Hidden", "![](:/r)", joplin.ids["private"])  # type: ignore[attr-defined]
    shared = joplin.attach(visible, "secret-scan.pdf", "application/pdf")
    joplin.refer(hidden, shared)
    own = joplin.attach(visible, "own.jpg")
    assert {a["id"] for a in (await _read(visible))["attachments"]} == {shared, own}  # no filter
    monkeypatch.setenv("SBJ_EXCLUDE_NOTEBOOKS", "Private")
    assert [a["id"] for a in (await _read(visible))["attachments"]] == [own]


@pytest.mark.parametrize("form", ["{id}", ":/{id}", "joplin://x-callback-url/openNote?id={id}"])
async def test_the_three_forms_of_a_note_id_are_accepted(joplin: FakeJoplin, form: str) -> None:
    note = joplin.note("N", "body", joplin.ids["barn"])  # type: ignore[attr-defined]
    assert (await _read(form.format(id=note)))["id"] == note


@pytest.mark.parametrize(
    "ref", ["joplin://x-callback-url/openFolder?id=" + "a" * 32, ":/" + "a" * 31, "a" * 33]
)
async def test_other_forms_are_refused(joplin: FakeJoplin, ref: str) -> None:
    async with Client(create_server()) as client:
        with pytest.raises(ToolError):
            await client.call_tool("joplin_read", {"note_id": ref})


# -- joplin_read_notes --------------------------------------------------------------


async def test_several_notes_in_one_call_in_the_order_asked(joplin: FakeJoplin) -> None:
    a = joplin.note("A", "alpha", joplin.ids["barn"])  # type: ignore[attr-defined]
    b = joplin.note("B", "beta", joplin.ids["kitchen"])  # type: ignore[attr-defined]
    result = await _read_notes([b, f":/{a}", b])
    assert [n["title"] for n in result["notes"]] == ["B", "A"]  # the repeat once
    assert result["notes"][0]["notebook_path"] == ["Projects", "Barn", "Kitchen"]
    assert result["not_found"] == []
    assert result["budget"] == READ_NOTES_BUDGET


async def test_missing_and_hidden_notes_are_listed_alike(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = joplin.note("Seen", "ok", joplin.ids["barn"])  # type: ignore[attr-defined]
    hidden = joplin.note("Hidden", "secret", joplin.ids["private"])  # type: ignore[attr-defined]
    missing = "e" * 32
    monkeypatch.setenv("SBJ_EXCLUDE_NOTEBOOKS", "Private")
    result = await _read_notes([hidden, seen, missing])
    assert [n["id"] for n in result["notes"]] == [seen]
    assert result["not_found"] == [hidden, missing]
    assert "secret" not in str(result) and "Hidden" not in str(result)


async def test_the_bodies_share_one_budget(joplin: FakeJoplin) -> None:
    short = joplin.note("Short", "s" * 100, joplin.ids["barn"])  # type: ignore[attr-defined]
    long1 = joplin.note("Long1", "a" * 50_000, joplin.ids["barn"])  # type: ignore[attr-defined]
    long2 = joplin.note("Long2", "b" * 50_000, joplin.ids["barn"])  # type: ignore[attr-defined]
    notes = {n["title"]: n for n in (await _read_notes([short, long1, long2]))["notes"]}
    assert len(notes["Short"]["body"]) == 100 and notes["Short"]["next_offset"] is None
    half = (READ_NOTES_BUDGET - 100) // 2
    assert len(notes["Long1"]["body"]) == half and notes["Long1"]["next_offset"] == half
    assert notes["Long1"]["total_chars"] == 50_000
    assert sum(len(n["body"]) for n in notes.values()) <= READ_NOTES_BUDGET


async def test_more_than_the_maximum_is_refused_before_joplin_is_asked(joplin: FakeJoplin) -> None:
    async with Client(create_server()) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "joplin_read_notes", {"note_ids": ["a" * 32] * (READ_NOTES_MAX + 1)}
            )
        with pytest.raises(ToolError):
            await client.call_tool("joplin_read_notes", {"note_ids": []})
    assert joplin.requests == []


def test_share_budget_gives_short_texts_in_full_and_splits_the_rest() -> None:
    assert share_budget([10, 1000, 1000], 210) == [10, 100, 100]
    assert share_budget([5, 5], 100) == [5, 5]
    assert share_budget([], 100) == []
    assert share_budget([7, 9, 8], 7) == [2, 3, 2]  # 7 shared: 2 for the shortest, then 2, then 3


async def test_joplin_down_fails_the_whole_call(joplin: FakeJoplin) -> None:
    note = joplin.note("N", "body", joplin.ids["barn"])  # type: ignore[attr-defined]
    joplin.down = True
    async with Client(create_server()) as client:
        with pytest.raises(ToolError, match="unreachable"):
            await client.call_tool("joplin_read_notes", {"note_ids": [note]})
