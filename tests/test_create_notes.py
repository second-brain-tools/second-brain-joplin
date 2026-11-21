"""joplin_create_notes (#69): several new notes in one call."""

import hashlib
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fakes import FakeJoplin
from fastmcp import Client
from fastmcp.exceptions import ToolError

from second_brain_joplin.config import Settings
from second_brain_joplin.server import create_server


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SBJ_NOTEBOOKS",
        "SBJ_EXCLUDE_NOTEBOOKS",
        "SBJ_EDIT_NOTEBOOKS",
        "SBJ_ASK_EVERY_TIME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SBJ_WRITE_NOTEBOOKS", "Inbox,Projects")
    monkeypatch.setenv("SBJ_CREATE_NOTES_MAX", "5")


@pytest.fixture
def joplin() -> Iterator[FakeJoplin]:
    with respx.mock:
        fake = FakeJoplin()
        fake.ids = {name: fake.notebook(name) for name in ("Inbox", "Projects", "Journal")}
        fake.install()
        yield fake


def _items(*specs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"title": t, "body": f"text of {t}", "notebook": nb} for t, nb in specs]


async def _create(notes: list[dict[str, str]]) -> list[dict[str, Any]]:
    async with Client(create_server()) as client:
        result = await client.call_tool("joplin_create_notes", {"notes": notes})
    return list(result.structured_content["notes"])


async def _refused(notes: list[dict[str, str]]) -> str:
    async with Client(create_server()) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool("joplin_create_notes", {"notes": notes})
    return str(raised.value)


async def test_one_call_creates_each_note_in_its_notebook(joplin: FakeJoplin) -> None:
    results = await _create(_items(("A", "Inbox"), ("B", "Projects"), ("C", "Inbox")))
    assert [(r["title"], r["notebook"], r["outcome"]) for r in results] == [
        ("A", "Inbox", "created"),
        ("B", "Projects", "created"),
        ("C", "Inbox", "created"),
    ]
    made = {n["title"]: n["parent_id"] for n in joplin.notes.values()}
    assert made == {"A": joplin.ids["Inbox"], "B": joplin.ids["Projects"], "C": joplin.ids["Inbox"]}
    assert results[0]["chars"] == len("text of A")
    assert results[0]["body_sha256"] == hashlib.sha256(b"text of A").hexdigest()
    assert all(r["id"] in joplin.notes for r in results)


async def test_a_bad_item_refuses_the_whole_call_before_anything_is_written(
    joplin: FakeJoplin,
) -> None:
    message = await _refused(_items(("A", "Inbox"), ("B", "Journal")))
    assert message.startswith("Note 2:") and "Nothing was created" in message
    assert joplin.posts == []
    assert "Note 1:" in await _refused([{"title": "   ", "body": "x", "notebook": "Inbox"}])


async def test_too_many_notes_or_too_much_text_is_refused_before_joplin_is_asked(
    joplin: FakeJoplin,
) -> None:
    assert "at most 5 notes" in await _refused(_items(*[(f"n{i}", "Inbox") for i in range(6)]))
    big = [{"title": f"n{i}", "body": "x" * 30_000, "notebook": "Inbox"} for i in range(4)]
    assert "over the limit" in await _refused(big)
    assert joplin.requests == []


async def test_the_same_note_twice_in_one_call_is_refused(joplin: FakeJoplin) -> None:
    message = await _refused(_items(("A", "Inbox"), ("B", "Inbox"), ("A", "Inbox")))
    assert "Notes 1 and 3 are the same note" in message and joplin.posts == []


async def test_a_failure_part_way_stops_and_says_which_notes_exist(joplin: FakeJoplin) -> None:
    calls = {"n": 0}
    original = joplin._create

    def second_fails(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(500)
        return original(request)

    respx.post(f"{joplin.base_url}/notes").mock(side_effect=second_fails)
    results = await _create(_items(("A", "Inbox"), ("B", "Inbox"), ("C", "Inbox")))
    assert [r["outcome"] for r in results] == ["created", "failed", "not_attempted"]
    assert "HTTP 500" in results[1]["reason"]
    assert results[2]["id"] is None
    assert {n["title"] for n in joplin.notes.values()} == {"A"}


async def test_a_call_that_creates_nothing_fails_as_one_note_would(joplin: FakeJoplin) -> None:
    joplin.post_status = 500
    assert "HTTP 500" in await _refused(_items(("A", "Inbox"), ("B", "Inbox")))


async def test_an_unconfirmed_note_stops_the_call_with_its_id(joplin: FakeJoplin) -> None:
    joplin.post_saves_late = True
    results = await _create(_items(("A", "Inbox"), ("B", "Inbox")))
    assert [r["outcome"] for r in results] == ["unconfirmed", "not_attempted"]
    assert results[0]["id"] and "may or may not" in results[0]["reason"]


async def test_sending_the_same_call_again_creates_only_what_is_missing(joplin: FakeJoplin) -> None:
    calls = {"n": 0}
    original = joplin._create

    def second_fails_once(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500) if calls["n"] == 2 else original(request)

    respx.post(f"{joplin.base_url}/notes").mock(side_effect=second_fails_once)
    notes = _items(("A", "Inbox"), ("B", "Inbox"))
    async with Client(create_server()) as client:
        first = (await client.call_tool("joplin_create_notes", {"notes": notes})).structured_content
        again = (await client.call_tool("joplin_create_notes", {"notes": notes})).structured_content
    assert [r["outcome"] for r in first["notes"]] == ["created", "failed"]
    assert [r["outcome"] for r in again["notes"]] == ["already_created", "created"]
    assert sorted(n["title"] for n in joplin.notes.values()) == ["A", "B"]


async def test_each_note_has_its_own_audit_lines_and_no_text(
    joplin: FakeJoplin, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="second_brain_joplin.audit")
    await _create(_items(("Secret title", "Inbox"), ("Other", "Inbox")))
    lines = [r.getMessage() for r in caplog.records if r.name == "second_brain_joplin.audit"]
    assert sum("note created" in line for line in lines) == 2
    assert "Secret title" not in " ".join(lines) and "text of" not in " ".join(lines)


async def _tools() -> dict[str, Any]:
    async with Client(create_server()) as client:
        return {tool.name: tool for tool in await client.list_tools()}


@pytest.mark.parametrize(
    ("max_notes", "write", "present"),
    [("5", "Inbox", True), ("", "Inbox", False), ("0", "Inbox", False), ("5", "", False)],
    ids=["both", "unset", "zero", "no write list"],
)
async def test_the_tool_needs_its_setting_and_the_write_list(
    monkeypatch: pytest.MonkeyPatch, max_notes: str, write: str, present: bool
) -> None:
    monkeypatch.setenv("SBJ_CREATE_NOTES_MAX", max_notes)
    if write:
        monkeypatch.setenv("SBJ_WRITE_NOTEBOOKS", write)
    else:
        monkeypatch.delenv("SBJ_WRITE_NOTEBOOKS")
    assert ("joplin_create_notes" in await _tools()) is present


async def test_the_tool_is_marked_like_joplin_create_note() -> None:
    tools = await _tools()
    one, many = tools["joplin_create_note"], tools["joplin_create_notes"]
    assert many.annotations == one.annotations
    assert many.meta == one.meta


@pytest.mark.parametrize("raw", ["21", "-1", "many"])
def test_the_setting_is_a_whole_number_from_0_to_20(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("SBJ_CREATE_NOTES_MAX", raw)
    settings = Settings.from_env()
    assert settings.create_notes_max == 0
    assert "SBJ_CREATE_NOTES_MAX" in settings.unreadable_numbers


async def test_joplin_down_refuses_before_any_note(joplin: FakeJoplin) -> None:
    joplin.down = True
    assert "unreachable" in await _refused(_items(("A", "Inbox")))
