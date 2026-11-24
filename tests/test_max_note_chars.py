"""SBJ_MAX_NOTE_CHARS (#114): the longest note the write tools accept, as a setting.

The default is the limit before the setting existed. Raised, every write tool
accepts a note up to the new limit and refuses one over it with nothing written;
what one call may carry follows the same setting. A note longer than one page of
joplin_read reads back whole in pages, and by its outline and a section.
"""

import hashlib
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from fakes import FakeJoplin
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from second_brain_core import env as core_env
from second_brain_core.remote import HttpOptions

from second_brain_joplin import cli
from second_brain_joplin.config import (
    DEFAULT_MAX_NOTE_CHARS,
    MAX_NOTE_CHARS_CEILING,
    MAX_NOTE_CHARS_FLOOR,
    Settings,
)
from second_brain_joplin.models import READ_MAX_CHARS
from second_brain_joplin.server import create_server

RAISED = 250_000
# Between the old limit and the raised one, and longer than one page of joplin_read.
BETWEEN = 180_000
WRITE_TOOLS = (
    "joplin_create_note",
    "joplin_create_notes",
    "joplin_edit_note",
    "joplin_replace_note",
)
OLD = "# Plan\n\nShort for now.\n"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def names(limit: int, message: str) -> bool:
    """Whether a refusal names ``limit``: ours with commas, the schema's without."""
    return f"{limit:,}" in message or f"at most {limit} characters" in message


def long_note(chars: int) -> str:
    """A Markdown note of exactly ``chars`` characters with three sections."""
    head = "# One\n\nfirst\n\n# Two\n\nsecond\n\n## Two point one\n\n"
    tail = "\n\n# Three\n\nthird\n"
    filler = "x" * (chars - len(head) - len(tail))
    text = head + filler + tail
    assert len(text) == chars
    return text


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("SBJ_NOTEBOOKS", "SBJ_EXCLUDE_NOTEBOOKS", "SBJ_ASK_EVERY_TIME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SBJ_WRITE_NOTEBOOKS", "Projects")
    monkeypatch.setenv("SBJ_EDIT_NOTEBOOKS", "Projects")
    monkeypatch.setenv("SBJ_REPLACE", "true")
    monkeypatch.setenv("SBJ_CREATE_NOTES_MAX", "5")
    monkeypatch.setenv("SBJ_UNDO_DIR", str(tmp_path / "undo"))
    monkeypatch.setenv("SBJ_MAX_NOTE_CHARS", "")
    monkeypatch.delenv("SBJ_MAX_NOTE_CHARS")
    monkeypatch.setattr(core_env, "load_dotenv", lambda *a, **k: False)


@pytest.fixture
def raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBJ_MAX_NOTE_CHARS", str(RAISED))


@pytest.fixture
def joplin() -> Iterator[FakeJoplin]:
    with respx.mock:
        fake = FakeJoplin()
        fake.ids = {"projects": fake.notebook("Projects")}
        fake.note_id = fake.note("Plan", OLD, fake.ids["projects"])
        fake.install()
        yield fake


async def _call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async with Client(create_server()) as client:
        result = await client.call_tool(tool, args)
    content: dict[str, Any] = result.structured_content
    return content


async def _refused(tool: str, args: dict[str, Any]) -> str:
    with pytest.raises(ToolError) as raised:
        await _call(tool, args)
    return str(raised.value)


def _replace_args(joplin: FakeJoplin, body: str) -> dict[str, Any]:
    current = joplin.notes[joplin.note_id]["body"]
    return {
        "note_id": joplin.note_id,
        "title": "Plan",
        "body": body,
        "expected_sha256": sha(current),
    }


def _append_args(joplin: FakeJoplin, text: str) -> dict[str, Any]:
    current = joplin.notes[joplin.note_id]["body"]
    return {
        "note_id": joplin.note_id,
        "title": "Plan",
        "expected_sha256": sha(current),
        "append": {"text": text},
    }


# -- the setting -------------------------------------------------------------------


def test_the_default_is_the_old_limit() -> None:
    assert Settings.from_env().max_note_chars == DEFAULT_MAX_NOTE_CHARS == 100_000
    assert Settings().max_note_chars == DEFAULT_MAX_NOTE_CHARS


@pytest.mark.parametrize("raw", ["1000", "250000", " 1000000 "])
def test_a_number_in_range_is_read(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("SBJ_MAX_NOTE_CHARS", raw)
    settings = Settings.from_env()
    assert settings.max_note_chars == int(raw)
    assert settings.unreadable_numbers == ()


@pytest.mark.parametrize("raw", ["lots", "999", "0", "-5", "1000001", "250,000", "2.5e5"])
def test_anything_else_keeps_the_default_and_is_reported(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("SBJ_MAX_NOTE_CHARS", raw)
    settings = Settings.from_env()
    assert settings.max_note_chars == DEFAULT_MAX_NOTE_CHARS
    assert "SBJ_MAX_NOTE_CHARS" in settings.unreadable_numbers


def test_the_bounds_are_as_documented() -> None:
    assert (MAX_NOTE_CHARS_FLOOR, MAX_NOTE_CHARS_CEILING) == (1_000, 1_000_000)


@pytest.mark.parametrize(
    "argv",
    [["serve"], ["serve", "--transport", "http", "--no-auth"], ["notebooks"]],
    ids=["stdio", "http", "notebooks"],
)
def test_an_unreadable_value_stops_the_commands_before_joplin_is_asked(
    argv: list[str],
    joplin: FakeJoplin,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    started: list[object] = []
    monkeypatch.setattr(FastMCP, "run", lambda *a, **k: started.append(a))
    monkeypatch.setattr(cli, "run_http", lambda *a: started.append(a))
    monkeypatch.setenv("SBJ_MAX_NOTE_CHARS", "250,000")
    joplin.requests.clear()
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert (
        "SBJ_MAX_NOTE_CHARS must be a whole number from 1,000 to 1,000,000, written "
        "without commas, got '250,000'"
    ) in capsys.readouterr().err
    assert started == [] and joplin.requests == []


# -- the default is unchanged ------------------------------------------------------


async def test_by_default_every_write_tool_refuses_a_note_over_100_000(joplin: FakeJoplin) -> None:
    over = "x" * (DEFAULT_MAX_NOTE_CHARS + 1)
    notes_before = dict(joplin.notes)
    limit = DEFAULT_MAX_NOTE_CHARS
    assert names(
        limit,
        await _refused(
            "joplin_create_note", {"title": "Big", "body": over, "notebook": "Projects"}
        ),
    )
    assert names(
        limit,
        await _refused(
            "joplin_create_notes",
            {"notes": [{"title": "Big", "body": over, "notebook": "Projects"}]},
        ),
    )
    assert names(limit, await _refused("joplin_replace_note", _replace_args(joplin, over)))
    assert "100,000" in await _refused(
        "joplin_edit_note", _append_args(joplin, "x" * (DEFAULT_MAX_NOTE_CHARS - len(OLD)))
    )
    assert joplin.notes == notes_before and joplin.puts == []


async def test_each_tool_says_the_limit_in_force(monkeypatch: pytest.MonkeyPatch) -> None:
    for value, said in (("", "100,000"), (str(RAISED), "250,000")):
        monkeypatch.setenv("SBJ_MAX_NOTE_CHARS", value)
        async with Client(create_server()) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
        for name in WRITE_TOOLS:
            assert f"up to {said} characters" in (tools[name].description or ""), name
        limit = int(said.replace(",", ""))
        for name, param in (("joplin_create_note", "body"), ("joplin_replace_note", "body")):
            assert tools[name].input_schema["properties"][param]["maxLength"] == limit


# -- raised ------------------------------------------------------------------------


@pytest.mark.usefixtures("raised")
async def test_create_accepts_a_note_between_the_limits_and_refuses_one_over(
    joplin: FakeJoplin,
) -> None:
    body = long_note(BETWEEN)
    made = await _call(
        "joplin_create_note", {"title": "Long", "body": body, "notebook": "Projects"}
    )
    assert joplin.notes[made["id"]]["body"] == body
    assert made["chars"] == BETWEEN and made["body_sha256"] == sha(body)

    count = len(joplin.notes)
    message = await _refused(
        "joplin_create_note",
        {"title": "Longer", "body": "x" * (RAISED + 1), "notebook": "Projects"},
    )
    assert names(RAISED, message) and len(joplin.notes) == count


@pytest.mark.usefixtures("raised")
async def test_create_notes_follows_the_setting_for_one_call(joplin: FakeJoplin) -> None:
    half = "y" * (BETWEEN // 2)
    made = await _call(
        "joplin_create_notes",
        {"notes": [{"title": f"N{i}", "body": half, "notebook": "Projects"} for i in (1, 2)]},
    )
    assert [n["outcome"] for n in made["notes"]] == ["created", "created"]

    count = len(joplin.notes)
    big = [{"title": f"M{i}", "body": "z" * 130_000, "notebook": "Projects"} for i in (1, 2)]
    message = await _refused("joplin_create_notes", {"notes": big})
    assert "260,000 characters together, over the limit of 250,000" in message
    assert len(joplin.notes) == count


@pytest.mark.usefixtures("raised")
async def test_replace_accepts_a_note_between_the_limits_and_refuses_one_over(
    joplin: FakeJoplin,
) -> None:
    over = await _refused("joplin_replace_note", _replace_args(joplin, "x" * (RAISED + 1)))
    assert names(RAISED, over) and joplin.puts == []

    body = long_note(BETWEEN)
    result = await _call("joplin_replace_note", _replace_args(joplin, body))
    assert joplin.notes[joplin.note_id]["body"] == body
    assert result["new_chars"] == BETWEEN


@pytest.mark.usefixtures("raised")
async def test_edit_accepts_a_note_between_the_limits_and_refuses_one_over(
    joplin: FakeJoplin,
) -> None:
    grown = await _call("joplin_edit_note", _append_args(joplin, "w" * (BETWEEN - len(OLD))))
    assert grown["new_chars"] >= BETWEEN
    size = len(joplin.notes[joplin.note_id]["body"])
    puts = len(joplin.puts)

    # Small in itself, but it would leave the note over the raised limit.
    message = await _refused("joplin_edit_note", _append_args(joplin, "v" * (RAISED - size + 10)))
    assert "over the limit of 250,000, so nothing was written" in message
    assert len(joplin.notes[joplin.note_id]["body"]) == size and len(joplin.puts) == puts


@pytest.mark.usefixtures("raised")
async def test_what_one_edit_call_carries_follows_the_setting(joplin: FakeJoplin) -> None:
    joplin.requests.clear()
    message = await _refused("joplin_edit_note", _append_args(joplin, "u" * (RAISED + 1)))
    assert "over the limit of 250,000 for one call" in message
    assert joplin.requests == []  # refused before Joplin is asked


# -- reading a note longer than one page -------------------------------------------


async def _read(note_id: str, **args: Any) -> dict[str, Any]:
    return await _call("joplin_read", {"note_id": note_id, **args})


@pytest.mark.usefixtures("raised")
async def test_a_note_longer_than_a_page_reads_back_whole_in_pages(joplin: FakeJoplin) -> None:
    body = long_note(BETWEEN)
    assert BETWEEN > READ_MAX_CHARS
    made = await _call(
        "joplin_create_note", {"title": "Long", "body": body, "notebook": "Projects"}
    )

    pages: list[str] = []
    offset: int | None = 0
    while offset is not None:
        page = await _read(made["id"], offset=offset, max_chars=READ_MAX_CHARS)
        assert len(page["body"]) <= READ_MAX_CHARS
        assert page["total_chars"] == BETWEEN
        pages.append(page["body"])
        offset = page["next_offset"]
    assert len(pages) == 2
    joined = "".join(pages)
    assert sha(joined) == page["body_sha256"] == made["body_sha256"]


@pytest.mark.usefixtures("raised")
async def test_a_long_notes_outline_and_one_section_read_as_for_a_short_note(
    joplin: FakeJoplin,
) -> None:
    body = long_note(BETWEEN)
    made = await _call(
        "joplin_create_note", {"title": "Long", "body": body, "notebook": "Projects"}
    )

    outline = await _read(made["id"], outline=True)
    assert outline["is_outline"] is True and outline["next_offset"] is None
    headings = [line.split("\t")[2] for line in outline["body"].splitlines()]
    assert headings == ["One", "Two", "Two point one", "Three"]

    three = await _read(made["id"], section="Three")
    assert three["body"] == "# Three\n\nthird\n"
    assert three["section_path"] == "3" and three["next_offset"] is None
    assert three["body_sha256"] == made["body_sha256"]  # the whole note's, as always


# -- what the operator is told -----------------------------------------------------


def test_the_notebooks_command_says_a_limit_that_is_not_the_default(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["notebooks"])
    assert "Longest note" not in capsys.readouterr().out
    monkeypatch.setenv("SBJ_MAX_NOTE_CHARS", str(RAISED))
    cli.main(["notebooks"])
    assert (
        "Longest note the write tools accept: 250,000 characters "
        "(SBJ_MAX_NOTE_CHARS; the default is 100,000)."
    ) in capsys.readouterr().out.splitlines()


def test_no_limit_line_on_a_read_only_server(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in (
        "SBJ_WRITE_NOTEBOOKS",
        "SBJ_EDIT_NOTEBOOKS",
        "SBJ_REPLACE",
        "SBJ_CREATE_NOTES_MAX",
    ):
        monkeypatch.delenv(name)
    monkeypatch.setenv("SBJ_MAX_NOTE_CHARS", str(RAISED))
    cli.main(["notebooks"])
    assert "Longest note" not in capsys.readouterr().out


def test_http_mode_logs_a_limit_that_is_not_the_default(
    joplin: FakeJoplin,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[HttpOptions] = []
    monkeypatch.setattr(cli, "run_http", lambda _server, options: calls.append(options))
    caplog.set_level(logging.INFO, logger="second_brain_joplin")
    cli.main(["serve", "--transport", "http", "--no-auth"])
    assert "longest note" not in caplog.text
    monkeypatch.setenv("SBJ_MAX_NOTE_CHARS", str(RAISED))
    cli.main(["serve", "--transport", "http", "--no-auth"])
    assert len(calls) == 2
    assert "longest note the write tools accept: 250,000 characters" in caplog.text
