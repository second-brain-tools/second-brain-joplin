"""MCP tool registration.

The read and semantic tools are always registered and are read-only toward
Joplin. ``joplin_create_note`` is registered only when the operator has listed
where notes may go (``SBJ_WRITE_NOTEBOOKS``), and the tools that change notes only
when they have listed whose notes may change (``SBJ_EDIT_NOTEBOOKS``); otherwise
the server is read-only and a client can't even see a write tool.
"""

from fastmcp import FastMCP

from . import changes, edit, read, semantic, write


def register_tools(
    mcp: FastMCP, *, writes: bool = False, edits: bool = False, ask_every_time: bool = True
) -> None:
    """Register the MCP tools on the given FastMCP server; the write tools on request.

    ``writes`` adds note creation (``SBJ_WRITE_NOTEBOOKS``); ``edits`` the tools that
    change notes and ``joplin_changes`` (``SBJ_EDIT_NOTEBOOKS``). ``ask_every_time``
    marks every write tool: see :func:`write.register`.
    """
    read.register(mcp)
    semantic.register(mcp)
    if writes:
        write.register(mcp, ask_every_time=ask_every_time)
    if edits:
        changes.register(mcp)
        edit.register(mcp, ask_every_time=ask_every_time)
