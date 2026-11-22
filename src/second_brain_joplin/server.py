"""MCP server construction and lifecycle."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from second_brain_core.audit import RegisteredClients
from second_brain_core.health import make_healthz
from second_brain_core.remote import build_middleware

from . import __version__
from .background import sync_forever
from .config import Settings
from .context import AppContext
from .identity import INSTRUCTIONS, NAMED_CAUSES, SERVER
from .joplin_client import JoplinClient
from .tools import register_tools

Lifespan = Callable[[FastMCP], AbstractAsyncContextManager[AppContext]]


def make_lifespan(*, remote: bool = False, sync_interval_minutes: int = 0) -> Lifespan:
    """A lifespan owning the shared app context, plus HTTP mode's background sync.

    The yielded context is reachable from tools via ``ctx.lifespan_context``; the
    semantic service is built lazily on first use, so start-up never waits on a
    model. With ``sync_interval_minutes`` above 0 a background task keeps the index
    fresh (background.py) and is cancelled when the server stops.
    """

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
        settings = Settings.from_env()
        client = JoplinClient(settings)
        app = AppContext(client=client, settings=settings, remote=remote)
        task = (
            asyncio.create_task(sync_forever(app, sync_interval_minutes * 60))
            if sync_interval_minutes > 0
            else None
        )
        try:
            yield app
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await client.aclose()

    return lifespan


# stdio: no background work, and a full reindex is allowed.
lifespan = make_lifespan()


async def joplin_answers() -> bool:
    """Whether Joplin answers, asked with a short-lived client of its own.

    Its own client keeps ``/healthz`` independent of the MCP lifespan, and of the
    tools' hold after a timeout: it asks Joplin every time.
    """
    client = JoplinClient(Settings.from_env())
    try:
        return await client.ping()
    finally:
        await client.aclose()


# Two booleans, 503 when Joplin is down (second_brain_core.health). Not cached.
healthz = make_healthz(joplin_answers, "joplin")


def create_server(
    *,
    auth: AuthProvider | None = None,
    allowed_ids: frozenset[str] | None = None,
    rate_limit_per_minute: int = 0,
    remote: bool = False,
    sync_interval_minutes: int = 0,
    writes: bool | None = None,
    edits: bool | None = None,
    replace_whole: bool | None = None,
    create_notes_max: int | None = None,
    ask_every_time: bool | None = None,
) -> FastMCP:
    """Build the MCP server with its tools and the ``/healthz`` route.

    With ``auth`` (HTTP mode), ``allowed_ids`` adds the per-tool allowlist check as
    a second layer behind the token verifier. ``rate_limit_per_minute`` (0 = off)
    limits each caller. Every tool call is logged without its content
    (``second_brain_core.audit``); with sign-in, the client is named from the
    sign-in's registrations.
    ``remote`` and ``sync_interval_minutes`` are HTTP mode's: see make_lifespan.

    ``writes`` says whether the write tool is registered, ``edits`` whether the
    tools that change notes are; None reads each from the environment as it is now
    (``SBJ_WRITE_NOTEBOOKS``, ``SBJ_EDIT_NOTEBOOKS``), so build the server after any
    env file is loaded. ``ask_every_time`` says whether that tool is
    marked to be asked about on every call; None reads ``SBJ_ASK_EVERY_TIME`` the
    same way, which is on unless it plainly says no.
    """
    clients = auth if isinstance(auth, RegisteredClients) else None
    middleware = build_middleware(
        SERVER,
        allowed_ids=allowed_ids,
        rate_limit_per_minute=rate_limit_per_minute,
        named_causes=NAMED_CAUSES,
        signed_in=auth is not None,
        clients=clients,
    )
    server_lifespan = (
        make_lifespan(remote=remote, sync_interval_minutes=sync_interval_minutes)
        if remote or sync_interval_minutes
        else lifespan
    )
    server = FastMCP(
        SERVER.name,
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=server_lifespan,
        auth=auth,
        middleware=middleware,
        # An exception that escapes a tool anyway reaches the client as FastMCP's
        # generic sentence, never as its own text (every tool is also guarded).
        mask_error_details=True,
    )
    settings = Settings.from_env()
    if writes is None:
        writes = settings.writes_enabled
    if edits is None:
        edits = settings.edits_enabled
    if replace_whole is None:
        replace_whole = settings.replace
    if create_notes_max is None:
        create_notes_max = settings.create_notes_max
    if ask_every_time is None:
        ask_every_time = settings.ask_every_time
    register_tools(
        server,
        writes=writes,
        edits=edits,
        replace_whole=replace_whole,
        create_notes_max=create_notes_max,
        ask_every_time=ask_every_time,
    )
    server.custom_route("/healthz", methods=["GET"])(healthz)
    return server


# The server as the environment describes it at import time. `serve` builds its
# own after loading any env file, so that file can switch the write tool on.
mcp = create_server()
