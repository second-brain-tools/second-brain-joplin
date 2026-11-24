"""MCP tool registration.

The read and semantic tools are always registered and are read-only toward
Joplin. ``joplin_create_note`` is registered only when the operator has listed
where notes may go (``SBJ_WRITE_NOTEBOOKS``), and the tools that change notes only
when they have listed whose notes may change (``SBJ_EDIT_NOTEBOOKS``); otherwise
the server is read-only and a client can't even see a write tool.
"""

from fastmcp import FastMCP

from ..config import DEFAULT_MAX_NOTE_CHARS
from . import changes, edit, read, replace, semantic, write


def register_tools(
    mcp: FastMCP,
    *,
    writes: bool = False,
    edits: bool = False,
    replace_whole: bool = False,
    create_notes_max: int = 0,
    max_note_chars: int = DEFAULT_MAX_NOTE_CHARS,
    ask_every_time: bool = True,
) -> None:
    """Register the MCP tools on the given FastMCP server; the write tools on request.

    ``writes`` adds note creation (``SBJ_WRITE_NOTEBOOKS``); ``edits`` the tools that
    change notes and ``joplin_changes`` (``SBJ_EDIT_NOTEBOOKS``), and with
    ``replace_whole`` (``SBJ_REPLACE``) also the one that rewrites a whole note.
    ``create_notes_max`` (``SBJ_CREATE_NOTES_MAX``) adds joplin_create_notes, with
    ``writes`` only. ``max_note_chars`` (``SBJ_MAX_NOTE_CHARS``) is the longest note
    every write tool accepts, and the most one call may carry. ``ask_every_time``
    marks every write tool: see :func:`write.register`.
    """
    read.register(mcp)
    semantic.register(mcp)
    if writes:
        write.register(mcp, max_chars=max_note_chars, ask_every_time=ask_every_time)
        if create_notes_max:
            write.register_many(
                mcp,
                max_notes=create_notes_max,
                max_chars=max_note_chars,
                ask_every_time=ask_every_time,
            )
    if edits:
        changes.register(mcp)
        edit.register(mcp, max_chars=max_note_chars, ask_every_time=ask_every_time)
        if replace_whole:
            replace.register(mcp, max_chars=max_note_chars, ask_every_time=ask_every_time)
