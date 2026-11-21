"""joplin_replace_note: rewrite the whole text of a note (#80).

For a short note rebuilt whole each time (a brief, a "where things stand" page);
joplin_edit_note is the better tool when most of a note stays. Registered only when
``SBJ_REPLACE`` is on as well as ``SBJ_EDIT_NOTEBOOKS``: it is the bluntest change,
so an operator can allow edits without it. It goes through the safety net of
changes.py like every change; on top of that the new text must not be empty, and
a call whose text the note already holds changes nothing (``already_replaced``),
however long ago it was first sent.
"""

from typing import Annotated

from fastmcp import Context, FastMCP
from pydantic import Field

from ..changes import ChangeRequest, change_note, sha256
from ..context import app_context
from ..models import MAX_NOTE_CHARS, MAX_TITLE_CHARS, ChangedNote
from .annotations import ASK_EVERY_TIME, REPLACES_NOTE
from .edit import Checksum
from .read import NoteId


class ReplacedNote(ChangedNote):
    """The result of joplin_replace_note."""

    already_replaced: bool = False


def register(mcp: FastMCP, *, ask_every_time: bool = True) -> None:
    @mcp.tool(annotations=REPLACES_NOTE, meta=ASK_EVERY_TIME if ask_every_time else None)
    async def joplin_replace_note(
        ctx: Context,
        note_id: NoteId,
        title: Annotated[
            str,
            Field(
                max_length=MAX_TITLE_CHARS,
                description=(
                    "The note's current title, exactly as joplin_read gave it "
                    "(empty for a note with no title)."
                ),
            ),
        ],
        body: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_NOTE_CHARS,
                description="The note's whole new text, in markdown.",
            ),
        ],
        expected_sha256: Checksum,
    ) -> ReplacedNote:
        """Replace the whole text of a note: for a short note rebuilt each time.

        For a long note, or a change to part of one, use joplin_edit_note. First
        read the note with joplin_read: give its ``title`` and ``body_sha256`` (as
        ``expected_sha256``). Only the text changes; the title, notebook and tags
        stay. Every attachment reference (``:/<id>``) of the old text must be in
        the new. The server keeps a copy of the note as it was.
        """
        result = await change_note(
            app_context(ctx),
            ChangeRequest(
                tool="joplin_replace_note",
                note_ref=note_id,
                title=title,
                expected_sha256=expected_sha256,
                make_body=lambda _old: body,
                request_parts=(sha256(body),),
                allow_empty=False,
                target_body=body,
            ),
        )
        data = result.model_dump()
        return ReplacedNote(**data, already_replaced=result.already_applied)
