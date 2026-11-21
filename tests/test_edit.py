"""joplin_edit_note (#83, covering #70): the edits, append and prepend, through the tool."""

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from fakes import FakeJoplin
from fastmcp import Client
from fastmcp.exceptions import ToolError

from second_brain_joplin.server import create_server
from second_brain_joplin.tools.edit import (
    Append,
    Prepend,
    TextEdit,
    apply_append,
    apply_edits,
    apply_prepend,
    occurrences,
)

NOTE = (
    "# Plan\n\nBuy the barn.\n\n## Budget\n\nMoney: 40.\n\n"
    "### Detail\n\nFine print.\n\n# Log\n\n- one\n"
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("SBJ_NOTEBOOKS", "SBJ_EXCLUDE_NOTEBOOKS", "SBJ_WRITE_NOTEBOOKS", "SBJ_REPLACE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SBJ_UNDO_DIR", str(tmp_path / "undo"))
    monkeypatch.setenv("SBJ_EDIT_NOTEBOOKS", "Projects")
    monkeypatch.delenv("SBJ_ASK_EVERY_TIME", raising=False)


@pytest.fixture
def joplin() -> Iterator[FakeJoplin]:
    with respx.mock:
        fake = FakeJoplin()
        fake.ids = {"projects": fake.notebook("Projects"), "private": fake.notebook("Private")}
        fake.note_id = fake.note("Plan", NOTE, fake.ids["projects"])
        fake.install()
        yield fake


async def _edit(joplin: FakeJoplin, **args: Any) -> dict[str, Any]:
    body = joplin.notes[joplin.note_id]["body"]
    call = {"note_id": joplin.note_id, "title": "Plan", "expected_sha256": sha(body), **args}
    async with Client(create_server()) as client:
        result = await client.call_tool("joplin_edit_note", call)
    content: dict[str, Any] = result.structured_content
    return content


async def _refused(joplin: FakeJoplin, **args: Any) -> str:
    with pytest.raises(ToolError) as raised:
        await _edit(joplin, **args)
    return str(raised.value)


def _body(joplin: FakeJoplin) -> str:
    return str(joplin.notes[joplin.note_id]["body"])


# -- edits -----------------------------------------------------------------------------


async def test_an_edit_changes_exactly_the_matched_text(joplin: FakeJoplin) -> None:
    result = await _edit(joplin, edits=[{"old": "Money: 40.", "new": "Money: 45."}])
    assert _body(joplin) == NOTE.replace("Money: 40.", "Money: 45.")
    assert result["edits_applied"] == 1
    assert (result["old_sha256"], result["new_sha256"]) == (sha(NOTE), sha(_body(joplin)))
    assert (result["old_chars"], result["new_chars"]) == (len(NOTE), len(NOTE))
    assert joplin.puts == [{"body": _body(joplin)}]


async def test_edits_apply_in_order_and_a_later_one_may_match_what_an_earlier_made(
    joplin: FakeJoplin,
) -> None:
    await _edit(
        joplin,
        edits=[
            {"old": "Buy the barn.", "new": "Buy the red barn."},
            {"old": "red barn", "new": "blue barn"},
        ],
    )
    assert "Buy the blue barn." in _body(joplin)


async def test_text_found_twice_is_refused_and_nothing_is_written(joplin: FakeJoplin) -> None:
    message = await _refused(
        joplin, edits=[{"old": "Money", "new": "Cash"}, {"old": "#", "new": "##"}]
    )
    assert message.startswith("Edit 2: its old text appears")
    assert joplin.puts == [] and _body(joplin) == NOTE
    assert not list(Path(os.environ["SBJ_UNDO_DIR"]).glob("*.json"))


async def test_text_not_found_names_the_edit(joplin: FakeJoplin) -> None:
    assert (await _refused(joplin, edits=[{"old": "Nowhere", "new": "x"}])).startswith("Edit 1:")


async def test_an_edit_can_remove_text(joplin: FakeJoplin) -> None:
    await _edit(joplin, edits=[{"old": "### Detail\n\nFine print.\n\n", "new": ""}])
    assert "Fine print" not in _body(joplin) and "# Log" in _body(joplin)


async def test_windows_line_endings_are_kept_and_matched(joplin: FakeJoplin) -> None:
    crlf = NOTE.replace("\n", "\r\n")
    joplin.notes[joplin.note_id]["body"] = crlf
    await _edit(
        joplin, edits=[{"old": "Buy the barn.\n\n## Budget", "new": "Buy two barns.\n\n## Budget"}]
    )
    assert _body(joplin) == crlf.replace("Buy the barn.", "Buy two barns.")
    assert "\n" not in _body(joplin).replace("\r\n", "")


def test_overlapping_occurrences_count() -> None:
    assert occurrences("aaa", "aa") == 2
    with pytest.raises(ToolError, match="appears 2 times"):
        apply_edits("aaa", [TextEdit(old="aa", new="b")])


# -- append and prepend -------------------------------------------------------------------


async def test_append_goes_at_the_end_of_a_section_before_the_next_heading(
    joplin: FakeJoplin,
) -> None:
    await _edit(joplin, append={"text": "- two", "section": "Log"})
    assert _body(joplin) == NOTE.replace("- one\n", "- one\n- two\n")
    await _edit(joplin, append={"text": "Second line.", "section": "Budget"})
    assert "Fine print.\nSecond line.\n\n# Log" in _body(joplin)  # after its subsection, before Log


async def test_append_by_path(joplin: FakeJoplin) -> None:
    await _edit(joplin, append={"text": "Added.", "section": "1.1.1"})
    assert "Fine print.\nAdded.\n\n# Log" in _body(joplin)


async def test_append_to_a_missing_or_repeated_heading_is_refused(joplin: FakeJoplin) -> None:
    assert "no heading 'Nope'" in await _refused(joplin, append={"text": "x", "section": "Nope"})
    joplin.notes[joplin.note_id]["body"] = "# A\n\n# A\n"
    assert "appears 2 times" in await _refused(joplin, append={"text": "x", "section": "A"})
    assert joplin.puts == []


async def test_a_heading_in_a_code_block_is_not_a_section(joplin: FakeJoplin) -> None:
    joplin.notes[joplin.note_id]["body"] = "# Real\n\n```\n# Fake\n```\n"
    assert "no heading 'Fake'" in await _refused(joplin, append={"text": "x", "section": "Fake"})
    await _edit(joplin, append={"text": "x", "section": "Real"})
    assert _body(joplin) == "# Real\n\n```\n# Fake\n```\nx\n"


def test_append_at_the_end_of_the_note() -> None:
    assert apply_append("a\n", Append(text="b")) == "a\nb\n"
    assert apply_append("a", Append(text="b")) == "a\nb"
    assert apply_append("", Append(text="b")) == "b"
    assert apply_append("a\r\n", Append(text="b\nc")) == "a\r\nb\r\nc\r\n"


def test_append_to_a_section_at_the_very_end_with_no_trailing_newline() -> None:
    assert apply_append("# A\ntext", Append(text="more", section="A")) == "# A\ntext\nmore"


def test_prepend_goes_first() -> None:
    assert apply_prepend("# Plan\n", Prepend(text="Draft.")) == "Draft.\n# Plan\n"
    assert apply_prepend("", Prepend(text="Only")) == "Only"
    assert apply_prepend("x\r\n", Prepend(text="a\nb")) == "a\r\nb\r\nx\r\n"


async def test_edits_append_and_prepend_together(joplin: FakeJoplin) -> None:
    result = await _edit(
        joplin,
        edits=[{"old": "40", "new": "41"}],
        append={"text": "- two", "section": "Log"},
        prepend={"text": "Status: draft"},
    )
    assert _body(joplin).startswith("Status: draft\n# Plan")
    assert "Money: 41." in _body(joplin) and _body(joplin).endswith("- one\n- two\n")
    assert result["edits_applied"] == 1


# -- the call ------------------------------------------------------------------------------


async def test_nothing_to_do_is_refused(joplin: FakeJoplin) -> None:
    assert "at least one of" in await _refused(joplin)
    assert "at least one of" in await _refused(joplin, edits=[])


async def test_too_much_text_is_refused_before_joplin_is_asked(joplin: FakeJoplin) -> None:
    joplin.requests.clear()
    message = await _refused(joplin, append={"text": "x" * 100_001})
    assert "over the limit" in message and joplin.requests == []


async def test_a_wrong_checksum_or_title_writes_nothing(joplin: FakeJoplin) -> None:
    assert "changed since it was read" in await _refused(
        joplin, edits=[{"old": "40", "new": "41"}], expected_sha256=sha("not it")
    )
    assert "title doesn't match" in await _refused(
        joplin, edits=[{"old": "40", "new": "41"}], title="Plan 2"
    )
    assert joplin.puts == []


async def test_the_same_call_twice_edits_once(joplin: FakeJoplin) -> None:
    call = {
        "note_id": joplin.note_id,
        "title": "Plan",
        "expected_sha256": sha(NOTE),
        "edits": [{"old": "40", "new": "41"}],
    }
    async with Client(create_server()) as client:
        first = (await client.call_tool("joplin_edit_note", call)).structured_content
        again = (await client.call_tool("joplin_edit_note", call)).structured_content
    assert (first["already_applied"], again["already_applied"]) == (False, True)
    assert len(joplin.puts) == 1


async def test_a_hidden_note_answers_like_a_missing_one(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden = joplin.note("Secret", "x", joplin.ids["private"])
    monkeypatch.setenv("SBJ_EXCLUDE_NOTEBOOKS", "Private")
    async with Client(create_server()) as client:
        with pytest.raises(ToolError, match="was not found"):
            await client.call_tool(
                "joplin_edit_note",
                {
                    "note_id": hidden,
                    "title": "Secret",
                    "expected_sha256": sha("x"),
                    "append": {"text": "y"},
                },
            )


async def test_the_journal_holds_the_text_from_before(joplin: FakeJoplin) -> None:
    await _edit(joplin, edits=[{"old": "40", "new": "41"}])
    [kept] = Path(os.environ["SBJ_UNDO_DIR"]).glob("*.json")
    data = json.loads(kept.read_text())
    assert (data["tool"], data["old_body"]) == ("joplin_edit_note", NOTE)


# -- registration and marks --------------------------------------------------------------


async def _tools() -> dict[str, Any]:
    async with Client(create_server()) as client:
        return {tool.name: tool for tool in await client.list_tools()}


async def test_the_tool_exists_only_with_the_edit_list(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "joplin_edit_note" in await _tools()
    monkeypatch.delenv("SBJ_EDIT_NOTEBOOKS")
    monkeypatch.setenv("SBJ_WRITE_NOTEBOOKS", "*")
    assert "joplin_edit_note" not in await _tools()


async def test_the_tool_is_destructive_and_asks_unless_told_not_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = (await _tools())["joplin_edit_note"]
    assert tool.annotations.destructive_hint is True and tool.annotations.read_only_hint is False
    assert (tool.meta or {}).get("anthropic/requiresUserInteraction") is True
    monkeypatch.setenv("SBJ_ASK_EVERY_TIME", "false")
    tool = (await _tools())["joplin_edit_note"]
    assert "anthropic/requiresUserInteraction" not in (tool.meta or {})


async def test_create_note_returns_the_length_and_checksum(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBJ_WRITE_NOTEBOOKS", "Projects")
    async with Client(create_server()) as client:
        made = (
            await client.call_tool(
                "joplin_create_note", {"title": "N", "body": "héllo", "notebook": "Projects"}
            )
        ).structured_content
    assert (made["chars"], made["body_sha256"]) == (5, sha("héllo"))
