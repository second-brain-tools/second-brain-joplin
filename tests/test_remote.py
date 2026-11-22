"""Tests for HTTP mode: options, start-up guardrails, and the app over real HTTP."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import httpx2
import pytest
import respx
from fastmcp import FastMCP
from fastmcp.utilities.tests import ASGIServer, asgi_server
from second_brain_core.errors import ServeConfigError
from second_brain_core.remote import HttpOptions, http_app_kwargs, http_options, run_http

from second_brain_joplin.identity import SERVER
from second_brain_joplin.remote import DEFAULT_SYNC_INTERVAL, sync_interval_minutes
from second_brain_joplin.server import create_server

BASE_URL = "http://localhost:41184"

# -- this server's options ---------------------------------------------------------
# The options and guardrails shared by every server are second-brain-core's, and
# tested there; these are what this server adds to them.


def opts(**kwargs: Any) -> HttpOptions:
    return HttpOptions(**{"host": "127.0.0.1", "port": SERVER.default_port, **kwargs})


def test_this_servers_defaults() -> None:
    options = http_options(SERVER, environ={})
    assert options == opts()
    assert options.port == 8775
    assert http_options(SERVER, environ={"SBJ_PORT": "9001"}).port == 9001


def test_the_sync_interval() -> None:
    assert sync_interval_minutes({}) == DEFAULT_SYNC_INTERVAL == 15
    assert sync_interval_minutes({"SBJ_SYNC_INTERVAL_MINUTES": ""}) == 15
    assert sync_interval_minutes({"SBJ_SYNC_INTERVAL_MINUTES": "0"}) == 0
    assert sync_interval_minutes({"SBJ_SYNC_INTERVAL_MINUTES": "60"}) == 60
    with pytest.raises(ServeConfigError, match="SBJ_SYNC_INTERVAL_MINUTES must be 0"):
        sync_interval_minutes({"SBJ_SYNC_INTERVAL_MINUTES": "-1"})
    with pytest.raises(ServeConfigError, match="SBJ_SYNC_INTERVAL_MINUTES must be a number"):
        sync_interval_minutes({"SBJ_SYNC_INTERVAL_MINUTES": "often"})


# -- the app ------------------------------------------------------------------


def test_app_kwargs_are_stateless_json_with_strict_hosts() -> None:
    kwargs = http_app_kwargs(opts(public_url="https://joplin.example.com"))
    assert kwargs == {
        "path": "/mcp",
        "stateless_http": True,
        "json_response": True,
        "host_origin_protection": True,
        "allowed_hosts": ["joplin.example.com"],
    }
    assert http_app_kwargs(opts())["allowed_hosts"] == []


def test_run_http_passes_the_same_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    server = create_server()
    seen: dict[str, Any] = {}
    monkeypatch.setattr(server, "run", lambda **kwargs: seen.update(kwargs))
    options = opts(host="::1", port=9400, no_auth=True)
    run_http(server, options)
    assert seen == {
        "transport": "http",
        "host": "::1",
        "port": 9400,
        "show_banner": False,
        "uvicorn_config": {"access_log": False},
        **http_app_kwargs(options),
    }


@asynccontextmanager
async def serving(options: HttpOptions, server: FastMCP | None = None) -> AsyncIterator[ASGIServer]:
    kwargs = http_app_kwargs(options)
    path = kwargs.pop("path")
    async with asgi_server(server or create_server(), path=path, **kwargs) as running:
        yield running


def raw_client(running: ASGIServer, host: str | None = None) -> httpx2.AsyncClient:
    """A plain HTTP client into the app, connected as uvicorn would be: on 127.0.0.1.

    ``host`` changes only the Host header. (The ASGI ``server`` address comes from
    the base URL, and the guard always trusts the address it is bound to, so a test
    that put another name there would be testing nothing.)
    """
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=running.app),
        base_url="http://127.0.0.1",
        headers={"Host": host} if host else None,
    )


@respx.mock
async def test_round_trip_over_http() -> None:
    respx.get(f"{BASE_URL}/folders").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "f1", "title": "Inbox", "parent_id": ""}]}
        )
    )
    respx.get(f"{BASE_URL}/notes").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "n1", "parent_id": "f1"}]})
    )
    async with serving(opts(no_auth=True)) as running, running.client() as client:
        names = {tool.name for tool in await client.list_tools()}
        result = await client.call_tool("joplin_overview", {})
    assert "joplin_overview" in names
    assert result.is_error is False


@respx.mock
async def test_stateless_requests_need_no_session() -> None:
    """Two separate clients, no shared session: each request stands alone."""
    async with serving(opts(no_auth=True)) as running:
        for _ in range(2):
            async with running.client() as client:
                assert await client.list_tools()


@respx.mock
async def test_healthz_up() -> None:
    respx.get(f"{BASE_URL}/ping").mock(return_value=httpx.Response(200, text="JoplinClipperServer"))
    async with serving(opts(no_auth=True)) as running, raw_client(running) as http:
        response = await http.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"server": True, "joplin": True}


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
    ids=["refused", "timeout"],
)
@respx.mock
async def test_healthz_joplin_down(failure: Exception) -> None:
    respx.get(f"{BASE_URL}/ping").mock(side_effect=failure)
    async with serving(opts(no_auth=True)) as running, raw_client(running) as http:
        response = await http.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"server": True, "joplin": False}


@pytest.mark.parametrize("raw", ["soon", "0", "-1"])
@respx.mock
async def test_healthz_answers_with_an_unreadable_timeout(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It reads the settings on every request: a bad value must not break it."""
    respx.get(f"{BASE_URL}/ping").mock(return_value=httpx.Response(200, text="JoplinClipperServer"))
    async with serving(opts(no_auth=True)) as running, raw_client(running) as http:
        monkeypatch.setenv("SBJ_JOPLIN_TIMEOUT", raw)
        response = await http.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"server": True, "joplin": True}


@pytest.mark.parametrize("hold", [False, True])
@respx.mock
async def test_healthz_asks_joplin_every_time_whatever_the_tools_learned(hold: bool) -> None:
    """It has a client of its own, so it never answers from the tools' hold."""
    ping = respx.get(f"{BASE_URL}/ping").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get(f"{BASE_URL}/folders").mock(side_effect=httpx.ReadTimeout("slow"))
    async with serving(opts(no_auth=True)) as running:
        if hold:
            async with running.client() as client:
                await client.call_tool("joplin_overview", {}, raise_on_error=False)
        ping.mock(return_value=httpx.Response(200, text="JoplinClipperServer"))
        async with raw_client(running) as http:
            response = await http.get("/healthz")
    assert response.json() == {"server": True, "joplin": True}


@respx.mock
async def test_unknown_host_header_is_refused() -> None:
    respx.get(f"{BASE_URL}/ping").mock(return_value=httpx.Response(200, text="JoplinClipperServer"))
    options = opts(public_url="https://joplin.example.com")
    async with serving(options) as running:
        async with raw_client(running, "evil.example") as http:
            assert (await http.get("/healthz")).status_code == 421
        async with raw_client(running, "joplin.example.com") as http:
            assert (await http.get("/healthz")).status_code == 200
