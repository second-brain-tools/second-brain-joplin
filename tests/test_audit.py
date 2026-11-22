"""What HTTP mode logs for this server, and never logs.

The audit line, the rate limit and the logging set-up are second-brain-core's and
tested there. These run them on this server: the Joplin token, note content, and
this server's own failures named in the log.
"""

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import StaticTokenVerifier
from fastmcp.utilities.tests import ASGIServer, asgi_server
from second_brain_core.audit import NO_DEBUG_LOGGERS, QUIET_LOGGERS, outcome_name
from second_brain_core.audit import configure_logging as core_configure_logging
from second_brain_core.remote import HttpOptions, http_app_kwargs

from second_brain_joplin.errors import (
    JoplinUnavailableError,
    NotebookFilterError,
    SemanticUnavailableError,
)
from second_brain_joplin.identity import NAMED_CAUSES, SERVER
from second_brain_joplin.server import create_server


def configure_logging() -> None:
    core_configure_logging(SERVER)


BASE_URL = "http://localhost:41184"
JOPLIN_TOKEN = "joplin-token-SECRET-1"
NOTE_TEXT = "note-body-SECRET-2"
QUERY = "query-SECRET-3"
TOKENS = {
    "john": {"client_id": "c", "scopes": [], "sub": "65598"},
    "other": {"client_id": "c", "scopes": [], "sub": "2"},
}


@pytest.fixture(autouse=True)
def _restore_loggers() -> Any:
    names = ("second_brain_joplin", *QUIET_LOGGERS, *NO_DEBUG_LOGGERS)
    saved = {n: (logging.getLogger(n).level, list(logging.getLogger(n).handlers)) for n in names}
    yield
    for name, (level, handlers) in saved.items():
        logging.getLogger(name).setLevel(level)
        logging.getLogger(name).handlers[:] = handlers


@asynccontextmanager
async def serving(rate: int = 0) -> AsyncIterator[ASGIServer]:
    server = create_server(
        auth=StaticTokenVerifier(tokens=TOKENS),
        allowed_ids=frozenset({"65598", "2"}),
        rate_limit_per_minute=rate,
    )
    options = HttpOptions(host="127.0.0.1", port=8775, public_url="https://joplin.example.com")
    kwargs = http_app_kwargs(options)
    path = kwargs.pop("path")
    async with asgi_server(server, path=path, **kwargs) as running:
        yield running


def _mock_search() -> None:
    respx.get(f"{BASE_URL}/search").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "n1", "title": "T", "body": NOTE_TEXT}], "has_more": False}
        )
    )


async def _search_as_john(running: ASGIServer) -> None:
    async with running.client(auth="john") as client:
        await client.call_tool("joplin_search", {"query": QUERY})


@respx.mock
async def test_fastmcp_at_debug_would_print_tool_arguments() -> None:
    """The control: at DEBUG, FastMCP logs each call with its arguments, which are
    note content. configure_logging keeps its loggers out of DEBUG."""
    _mock_search()
    printed: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            printed.append(record.getMessage())

    fastmcp_logger = logging.getLogger("fastmcp")
    fastmcp_logger.addHandler(Collect(level=logging.DEBUG))

    async def search() -> None:
        async with Client(create_server()) as client:
            await client.call_tool("joplin_search", {"query": QUERY})

    fastmcp_logger.setLevel(logging.DEBUG)
    await search()
    assert any(QUERY in line for line in printed)  # the leak is real

    printed.clear()
    configure_logging()
    await search()
    assert not any(QUERY in line for line in printed)


@respx.mock
async def test_unpinned_httpx_would_print_the_joplin_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The control: this is the leak configure_logging closes."""
    monkeypatch.setenv("JOPLIN_API_TOKEN", JOPLIN_TOKEN)
    _mock_search()
    caplog.set_level(logging.INFO)
    async with serving() as running:
        await _search_as_john(running)
    assert JOPLIN_TOKEN in caplog.text


@respx.mock
async def test_logs_hold_the_call_but_no_content_or_credentials(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("JOPLIN_API_TOKEN", JOPLIN_TOKEN)
    _mock_search()
    configure_logging()
    caplog.set_level(logging.INFO)
    async with serving() as running:
        await _search_as_john(running)
    text = caplog.text
    assert re.search(r"tool=joplin_search caller=65598 client=\S+ outcome=ok", text)
    for secret in (JOPLIN_TOKEN, NOTE_TEXT, QUERY, "Bearer john"):
        assert secret not in text, secret


@respx.mock
async def test_failed_calls_are_logged_by_type_not_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.get(f"{BASE_URL}/notes/{QUERY}").mock(return_value=httpx.Response(404))
    configure_logging()
    caplog.set_level(logging.INFO, logger="second_brain_joplin")
    async with serving() as running, running.client(auth="john") as client:
        result = await client.call_tool("joplin_read", {"note_id": QUERY}, raise_on_error=False)
    assert result.is_error
    audit = [r.getMessage() for r in caplog.records if r.name == "second_brain_joplin.audit"]
    assert audit and re.match(r"tool=joplin_read caller=65598 client=\S+ outcome=error", audit[0])
    assert QUERY not in " ".join(audit)


@respx.mock
async def test_a_failure_of_ours_is_logged_by_its_own_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing = "f" * 32
    respx.get(f"{BASE_URL}/folders").mock(
        return_value=httpx.Response(200, json={"items": [], "has_more": False})
    )
    respx.get(f"{BASE_URL}/notes/{missing}").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE_URL}/search").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get(f"{BASE_URL}/ping").mock(side_effect=httpx.ReadTimeout("slow"))
    configure_logging()
    caplog.set_level(logging.INFO, logger="second_brain_joplin")
    async with serving() as running, running.client(auth="john") as client:
        await client.call_tool("joplin_read", {"note_id": missing}, raise_on_error=False)
        for _ in range(2):  # the timeout, then a call refused by the hold
            await client.call_tool("joplin_search", {"query": QUERY}, raise_on_error=False)
    audit = [r.getMessage() for r in caplog.records if r.name == "second_brain_joplin.audit"]
    outcomes = [line.split(" outcome=")[1].split(" ")[0] for line in audit]
    assert outcomes == [
        "error:JoplinNotFoundError",
        "error:JoplinUnavailableError",
        "error:JoplinUnavailableError",
    ]
    assert QUERY not in " ".join(audit)


def test_outcome_names_only_this_packages_causes() -> None:
    def raised_from(cause: BaseException | None) -> ToolError:
        error = ToolError("a message that may quote the caller")
        error.__cause__ = cause
        return error

    named = NAMED_CAUSES
    assert outcome_name(raised_from(JoplinUnavailableError("x")), named) == "JoplinUnavailableError"
    assert outcome_name(raised_from(NotebookFilterError("detail")), named) == "NotebookFilterError"
    unavailable = raised_from(SemanticUnavailableError("x"))
    assert outcome_name(unavailable, named) == "SemanticUnavailableError"
    assert outcome_name(raised_from(None), named) == "ToolError"
    assert outcome_name(raised_from(KeyError("x")), named) == "ToolError"  # not ours: not named
    assert outcome_name(ValueError("x"), named) == "ValueError"
