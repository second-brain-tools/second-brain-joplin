"""Turning a failure inside a tool into the error an MCP client sees.

second-brain-core's rules (``second_brain_core.failure``), bound to this server: a
planned failure carries its own sentence, a notebook filter that doesn't resolve
tells the client nothing more, and anything unplanned gets a fixed sentence.
"""

from fastmcp.exceptions import ToolError
from second_brain_core import failure

from .identity import SERVER

# Under @mcp.tool on every tool: nothing unplanned escapes it.
guarded = failure.guarded(SERVER)


def tool_error(exc: Exception) -> ToolError:
    """The ``ToolError`` for a planned failure (see ``second_brain_core.failure``)."""
    return failure.tool_error(SERVER, exc)
