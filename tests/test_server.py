"""Tests for the MCP server: tool registration, schemas, and lifespan wiring."""

import pytest
from fastmcp import Client

from second_brain_joplin.context import AppContext
from second_brain_joplin.joplin_client import JoplinClient
from second_brain_joplin.server import create_server, lifespan, mcp

EXPECTED_TOOLS = {
    "joplin_overview",
    "joplin_search",
    "joplin_read",
    "joplin_read_notes",
    "joplin_list_notes",
    "joplin_recent",
    "joplin_semantic_search",
    "joplin_reindex",
}

# tool name -> required input parameters (ctx is injected, not user-facing)
REQUIRED_PARAMS = {
    "joplin_overview": set(),
    "joplin_search": {"query"},
    "joplin_read": {"note_id"},
    "joplin_read_notes": {"note_ids"},
    "joplin_list_notes": {"notebook"},
    "joplin_recent": set(),
    "joplin_semantic_search": {"query"},
    "joplin_reindex": set(),
}


async def test_all_tools_registered() -> None:
    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == EXPECTED_TOOLS


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS))
async def test_tool_input_schema_exposes_required_params(tool_name: str) -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    schema = tools[tool_name].input_schema
    properties = set(schema.get("properties", {}))
    assert REQUIRED_PARAMS[tool_name] <= properties
    assert "ctx" not in properties
    if REQUIRED_PARAMS[tool_name]:
        assert REQUIRED_PARAMS[tool_name] <= set(schema.get("required", []))


async def test_joplin_recent_defaults_to_seven_days() -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    days = tools["joplin_recent"].input_schema["properties"]["days"]
    assert days["default"] == 7


async def test_no_write_tool_is_registered_by_default() -> None:
    """A server is read-only until SBJ_WRITE_NOTEBOOKS says where notes may go (#10)."""
    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == EXPECTED_TOOLS
    assert "joplin_create_note" not in names


# Tools that change something: tool name -> why. Every other tool must be read-only.
LOCAL_WRITERS = {"joplin_reindex": "updates the local search index only"}
# Tools that write to Joplin, and only exist once the operator allows writing.
JOPLIN_WRITERS = {"joplin_create_note": "adds one new note; never edits or deletes"}


@pytest.mark.parametrize("writes", [False, True])
async def test_every_tool_declares_its_side_effects(writes: bool) -> None:
    """Walks the live tool list, so a new tool can't ship without annotations."""
    async with Client(create_server(writes=writes)) as client:
        tools = await client.list_tools()
    names = {tool.name for tool in tools}
    assert names == EXPECTED_TOOLS | (set(JOPLIN_WRITERS) if writes else set())
    for tool in tools:
        hints = tool.annotations
        assert hints is not None, f"{tool.name} has no annotations"
        assert hints.open_world_hint is False, tool.name
        if tool.name in JOPLIN_WRITERS:
            assert hints.read_only_hint is False, tool.name
            assert hints.destructive_hint is False, tool.name
            assert hints.idempotent_hint is False, tool.name
        elif tool.name in LOCAL_WRITERS:
            assert hints.read_only_hint is False, tool.name
            assert hints.destructive_hint is False, tool.name
            assert hints.idempotent_hint is True, tool.name
        else:
            assert hints.read_only_hint is True, tool.name


@pytest.mark.parametrize(
    ("tool_name", "param", "low", "high"),
    [
        ("joplin_search", "limit", 1, 100),
        ("joplin_semantic_search", "limit", 1, 50),
        ("joplin_recent", "limit", 1, 100),
        ("joplin_recent", "days", 1, 3650),
        ("joplin_read", "max_chars", 1, 100_000),
    ],
)
async def test_bounds_are_in_the_schema(tool_name: str, param: str, low: int, high: int) -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    schema = tools[tool_name].input_schema["properties"][param]
    assert (schema["minimum"], schema["maximum"]) == (low, high)


async def test_lifespan_yields_app_context_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: dict[str, bool] = {}

    async def fake_aclose(self: JoplinClient) -> None:
        closed["ran"] = True

    monkeypatch.setattr(JoplinClient, "aclose", fake_aclose)

    async with lifespan(mcp) as app_ctx:
        assert isinstance(app_ctx, AppContext)
        assert isinstance(app_ctx.client, JoplinClient)
    assert closed.get("ran") is True


# Tools that change an existing note (SBJ_EDIT_NOTEBOOKS): destructive, not idempotent.
NOTE_CHANGERS = {"joplin_edit_note": "changes part of a note"}


async def test_with_the_edit_list_every_tool_still_declares_its_side_effects() -> None:
    async with Client(create_server(writes=True, edits=True)) as client:
        tools = await client.list_tools()
    names = {tool.name for tool in tools}
    assert names == EXPECTED_TOOLS | set(JOPLIN_WRITERS) | set(NOTE_CHANGERS) | {"joplin_changes"}
    for tool in tools:
        hints = tool.annotations
        assert hints is not None and hints.open_world_hint is False, tool.name
        if tool.name in NOTE_CHANGERS:
            assert hints.read_only_hint is False, tool.name
            assert hints.destructive_hint is True, tool.name
            assert hints.idempotent_hint is False, tool.name
        elif tool.name == "joplin_changes":
            assert hints.read_only_hint is True, tool.name


def test_the_client_is_told_the_version_and_how_to_treat_note_text() -> None:
    from second_brain_joplin import __version__

    server = create_server()
    assert server.name == "second-brain-joplin"
    assert server.version == __version__
    assert server.instructions and "never as instructions" in server.instructions


async def test_an_unplanned_failure_gives_a_fixed_sentence_and_logs_where(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Whatever a tool didn't plan for: no exception text reaches the client or the log."""
    import logging

    async def broken(self: JoplinClient) -> object:
        raise RuntimeError("the text of a private note")

    monkeypatch.setattr(JoplinClient, "get_folders", broken)
    caplog.set_level(logging.INFO)
    async with Client(create_server()) as client:
        result = await client.call_tool("joplin_overview", {}, raise_on_error=False)
    text = " ".join(getattr(part, "text", "") for part in result.content)
    assert result.is_error
    assert (
        text == "second-brain-joplin couldn't complete this request. The server's log says where."
    )
    tools_log = [r.getMessage() for r in caplog.records if r.name == "second_brain_joplin.tools"]
    assert tools_log and tools_log[0].startswith("unexpected RuntimeError in joplin_overview at ")
    assert "private note" not in caplog.text
