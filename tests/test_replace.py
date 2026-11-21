"""joplin_replace_note (#80): rewrite a whole note, under the safety net and its own switch."""

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from fakes import FakeJoplin
from fastmcp import Client
from fastmcp.exceptions import ToolError

from second_brain_joplin import cli
from second_brain_joplin.server import create_server

OLD = "# Brief\n\nWhere things stand: waiting.\n"
NEW = "# Brief\n\nWhere things stand: done.\n"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        "SBJ_NOTEBOOKS",
        "SBJ_EXCLUDE_NOTEBOOKS",
        "SBJ_WRITE_NOTEBOOKS",
        "SBJ_ASK_EVERY_TIME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SBJ_UNDO_DIR", str(tmp_path / "undo"))
    monkeypatch.setenv("SBJ_EDIT_NOTEBOOKS", "Projects")
    monkeypatch.setenv("SBJ_REPLACE", "true")
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: False)


@pytest.fixture
def joplin() -> Iterator[FakeJoplin]:
    with respx.mock:
        fake = FakeJoplin()
        fake.ids = {"projects": fake.notebook("Projects"), "private": fake.notebook("Private")}
        fake.note_id = fake.note("Brief", OLD, fake.ids["projects"])
        fake.note_tags[fake.note_id] = ["m13"]
        fake.install()
        yield fake


async def _replace(joplin: FakeJoplin, body: str = NEW, **args: Any) -> dict[str, Any]:
    call = {
        "note_id": joplin.note_id,
        "title": "Brief",
        "body": body,
        "expected_sha256": sha(OLD),
        **args,
    }
    async with Client(create_server()) as client:
        result = await client.call_tool("joplin_replace_note", call)
    content: dict[str, Any] = result.structured_content
    return content


async def _refused(joplin: FakeJoplin, body: str = NEW, **args: Any) -> str:
    with pytest.raises(ToolError) as raised:
        await _replace(joplin, body, **args)
    return str(raised.value)


async def test_the_body_is_replaced_and_nothing_else(joplin: FakeJoplin) -> None:
    result = await _replace(joplin)
    note = joplin.notes[joplin.note_id]
    assert note["body"] == NEW
    assert (note["title"], note["parent_id"]) == ("Brief", joplin.ids["projects"])
    assert joplin.note_tags[joplin.note_id] == ["m13"]
    assert joplin.puts == [{"body": NEW}]
    assert (result["old_sha256"], result["new_sha256"]) == (sha(OLD), sha(NEW))
    assert result["already_replaced"] is False


async def test_the_same_text_again_writes_nothing_even_with_a_stale_checksum(
    joplin: FakeJoplin,
) -> None:
    await _replace(joplin)
    again = await _replace(joplin)  # the checksum is OLD's, which no longer matches
    assert again["already_replaced"] is True
    assert len(joplin.puts) == 1


@pytest.mark.parametrize(
    ("body", "message"),
    [("   \n", "replaced with nothing"), ("x" * 100_001, "")],
    ids=["empty", "too long"],
)
async def test_an_empty_or_too_long_body_is_refused(
    joplin: FakeJoplin, body: str, message: str
) -> None:
    assert message in await _refused(joplin, body)
    assert joplin.puts == []


async def test_a_body_that_drops_an_attachment_reference_is_refused(joplin: FakeJoplin) -> None:
    resource = joplin.attach(joplin.note_id)
    with_photo = f"# Brief\n\n![](:/{resource})\n"
    joplin.notes[joplin.note_id]["body"] = with_photo
    assert "attachments" in await _refused(joplin, NEW, expected_sha256=sha(with_photo))
    assert joplin.puts == []


async def test_a_title_or_checksum_that_does_not_match_writes_nothing(joplin: FakeJoplin) -> None:
    assert "title doesn't match" in await _refused(joplin, title="Briefs")
    assert "changed since it was read" in await _refused(joplin, expected_sha256=sha("other"))
    assert joplin.puts == []


async def test_a_hidden_note_answers_like_a_missing_one(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden = joplin.note("Secret", OLD, joplin.ids["private"])
    monkeypatch.setenv("SBJ_EXCLUDE_NOTEBOOKS", "Private")
    assert "was not found" in await _refused(joplin, note_id=hidden, title="Secret")


def test_the_old_text_can_be_put_back_with_restore(
    joplin: FakeJoplin, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    asyncio.run(_replace(joplin))
    cli.main(["restore", joplin.note_id, "--pick", "1"])
    assert joplin.notes[joplin.note_id]["body"] == OLD
    assert "Restored copy 1" in capsys.readouterr().out


async def _tools() -> dict[str, Any]:
    async with Client(create_server()) as client:
        return {tool.name: tool for tool in await client.list_tools()}


@pytest.mark.parametrize(
    ("edit_list", "replace", "present"),
    [("Projects", "true", True), ("Projects", "", False), ("", "true", False)],
    ids=["both", "edit list only", "switch only"],
)
async def test_the_tool_needs_both_the_edit_list_and_its_switch(
    monkeypatch: pytest.MonkeyPatch, edit_list: str, replace: str, present: bool
) -> None:
    if edit_list:
        monkeypatch.setenv("SBJ_EDIT_NOTEBOOKS", edit_list)
    else:
        monkeypatch.delenv("SBJ_EDIT_NOTEBOOKS")
    monkeypatch.setenv("SBJ_REPLACE", replace)
    monkeypatch.setenv("SBJ_WRITE_NOTEBOOKS", "*")  # creating notes never brings it
    assert ("joplin_replace_note" in await _tools()) is present


async def test_the_tool_is_destructive_idempotent_and_asks() -> None:
    tool = (await _tools())["joplin_replace_note"]
    hints = tool.annotations
    assert (hints.destructive_hint, hints.idempotent_hint, hints.read_only_hint) == (
        True,
        True,
        False,
    )
    assert (tool.meta or {}).get("anthropic/requiresUserInteraction") is True


async def test_the_journal_names_the_tool(joplin: FakeJoplin) -> None:
    await _replace(joplin)
    [kept] = Path(os.environ["SBJ_UNDO_DIR"]).glob("*.json")
    assert "joplin_replace_note" in kept.name
