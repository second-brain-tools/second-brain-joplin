"""MCP tool annotations shared by the tool modules.

Clients use these hints, e.g. to skip a confirmation prompt for a read-only tool.
"""

import inspect
from collections.abc import Callable
from typing import Any, TypeVar

from mcp.types import ToolAnnotations

F = TypeVar("F", bound=Callable[..., Any])


def noting(sentence: str) -> Callable[[F], F]:
    """Add ``sentence`` to the end of a tool's description (its docstring).

    For what is only known when the tool is registered, such as the limit a
    setting puts on a note (SBJ_MAX_NOTE_CHARS), so a client is told the limit
    that is in force. Put it directly under ``@mcp.tool``.
    """

    def add(fn: F) -> F:
        doc = inspect.cleandoc(fn.__doc__ or "")
        fn.__doc__ = f"{doc}\n\n{sentence}" if doc else sentence
        return fn

    return add


# Reads Joplin, changes nothing, and touches nothing beyond this machine's Joplin.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# Updates the local search index only: never Joplin, never deletes a note, and
# running it twice leaves the same index.
LOCAL_INDEX_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

# Adds one new note to Joplin. It never changes or deletes anything that exists,
# so it is not destructive; calling it twice makes two notes, so not idempotent.
CREATES_NOTE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)

# Changes an existing note. An edit can remove text, so destructive; calling it
# twice is answered from the repeat memory, but a client can't rely on that.
CHANGES_NOTE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

# Rewrites a note's whole text. Destructive; sending the same text again changes
# nothing, so idempotent.
REPLACES_NOTE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)

# Tool metadata Claude Code honours: prompt the user on every call of this tool,
# with no "don't ask again", whatever allow rules are in force and in every
# permission mode that can prompt (Claude Code docs, "Require approval for a
# specific tool"). Where Claude Code can't put the prompt to a person it refuses
# the call instead. That is deliberate there, and it also means a client that
# shows the prompt with no way to approve it (Claude Duet, November 2026) can't
# call the tool at all. So the operator can leave the metadata off with
# SBJ_ASK_EVERY_TIME=false (config.py); the check on each note is then whatever
# the client does for any other tool that writes.
ASK_EVERY_TIME = {"anthropic/requiresUserInteraction": True}
