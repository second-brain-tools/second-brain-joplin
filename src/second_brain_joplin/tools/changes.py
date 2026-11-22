"""joplin_changes: what the tools that change notes did, from the journal (#82).

Registered with the tools that change notes. It reads only the journal's metadata:
when, which tool, which note, the lengths before and after and how it ended, and
the note's title as it is now. Never any text, and never a note the notebook
filter hides.
"""

import asyncio
from typing import Annotated

from fastmcp import Context, FastMCP
from pydantic import Field

from ..context import app_context
from ..errors import JoplinError, JoplinNotFoundError, NotebookFilterError
from ..failure import guarded, tool_error
from ..journal import DAY
from ..models import Change, epoch_ms_to_iso
from ..scope import scope_of
from .annotations import READ_ONLY

MAX_CHANGES_DAYS = 90
MAX_CHANGES_LIMIT = 200


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @guarded
    async def joplin_changes(
        ctx: Context,
        days: Annotated[int, Field(ge=1, le=MAX_CHANGES_DAYS)] = 7,
        limit: Annotated[int, Field(ge=1, le=MAX_CHANGES_LIMIT)] = 50,
    ) -> list[Change]:
        """List the changes this server made to notes, newest first.

        Each has when, which tool, the note's id and its title now, its length
        before and after, and how it ended: ``applied``; ``failed`` (nothing
        changed); ``unconfirmed`` or ``sent`` (Joplin's answer never came back:
        read the note to see). No note text. Useful at the start of a session, to
        see what was changed.
        """
        app = app_context(ctx)
        journal = app.journal()
        since = journal.clock() - days * DAY
        entries = list(reversed(await asyncio.to_thread(journal.entries, since=since)))
        out: list[Change] = []
        try:
            scope = scope_of(app.settings, await app.client.get_folders())
            for entry in entries:
                if len(out) >= limit:
                    break
                try:
                    # No body: Joplin refuses a locked note's, and the title is all needed.
                    note = await app.client.get_note(entry.note_id, fields="id,title,parent_id")
                except JoplinNotFoundError:
                    if not scope.allows(entry.old_notebook):
                        continue
                    title = None
                else:
                    if not scope.allows_note(note):
                        continue
                    title = str(note.get("title", ""))
                out.append(
                    Change(
                        at=epoch_ms_to_iso(int(entry.at * 1000)),
                        tool=entry.tool,
                        note_id=entry.note_id,
                        title=title,
                        old_chars=entry.old_chars,
                        new_chars=entry.new_chars,
                        outcome=entry.outcome,
                    )
                )
        except (JoplinError, NotebookFilterError) as exc:
            raise tool_error(exc) from exc
        return out
