"""joplin_edit_note: change part of a note, or add to it (#83, which covers #70).

Registered only with ``SBJ_EDIT_NOTEBOOKS``. Every call goes through the safety net
in changes.py (the edit list, the kept copy, the daily limit, the checksum, the
title); this module says only how the new text is made from the old:

- **edits**: pairs of ``old`` text, copied exactly from the note, and its ``new``
  text. Each ``old`` must appear exactly once in the text as it stands when that
  edit is applied (overlapping occurrences count), and they are applied in order.
  One that doesn't match refuses the whole call, naming which and why.
- **append**: text added at the end of a section (after its last line of text,
  before the next heading of the same or a higher level; sections by the rules of
  markdown.py), or at the end of the note.
- **prepend**: text added at the very start of the note.

Inserted text takes the note's own line endings. Everything else in the note stays
as it was, byte for byte.
"""

import json
from typing import Annotated

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from ..changes import ChangeRequest, change_note
from ..context import app_context
from ..failure import guarded
from ..markdown import SectionError, find_section
from ..models import MAX_TITLE_CHARS, ChangedNote
from .annotations import ASK_EVERY_TIME, CHANGES_NOTE, noting
from .read import NoteId

MAX_EDITS = 50
Checksum = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{64}$",
        description="body_sha256 from the joplin_read of the note this change is made on.",
    ),
]


class TextEdit(BaseModel):
    """Replace ``old`` (exact text from the note, found once) with ``new``."""

    old: Annotated[str, Field(min_length=1, description="Text copied exactly from the note.")]
    new: Annotated[str, Field(description="What it becomes; empty removes it.")]


class Append(BaseModel):
    """Add ``text`` at the end of a section, or of the note."""

    text: Annotated[str, Field(min_length=1)]
    section: Annotated[
        str | None,
        Field(
            max_length=300,
            description=(
                "The section to add it to: a heading's exact text (it must appear once) "
                "or a path from joplin_read's outline. Omitted: the end of the note."
            ),
        ),
    ] = None


class Prepend(BaseModel):
    """Add ``text`` at the very start of the note."""

    text: Annotated[str, Field(min_length=1)]


class EditedNote(ChangedNote):
    """The result of joplin_edit_note."""

    edits_applied: int = 0


def occurrences(text: str, old: str) -> int:
    """How many times ``old`` appears in ``text``, overlapping ones included."""
    count, start = 0, 0
    while (found := text.find(old, start)) >= 0:
        count += 1
        start = found + 1
    return count


def line_ending(body: str) -> str:
    """The note's own line ending: CRLF if it has any, else LF."""
    return "\r\n" if "\r\n" in body else "\n"


def in_style(text: str, newline: str) -> str:
    """``text`` with its line endings turned into ``newline``."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def apply_edits(body: str, edits: list[TextEdit]) -> str:
    """Apply each edit in turn: ``old`` must name one place in the note.

    It is looked for as written, and in the note's own line endings (a model often
    sends ``\n`` for a note written with ``\r\n``); between the two it must appear
    exactly once. Text inserted for a match in the note's style takes that style.
    """
    for number, edit in enumerate(edits, start=1):
        newline = line_ending(body)
        styled = in_style(edit.old, newline)
        as_written = occurrences(body, edit.old)
        in_note_style = occurrences(body, styled) if styled != edit.old else 0
        found = as_written + in_note_style
        old, new = (edit.old, edit.new) if as_written else (styled, in_style(edit.new, newline))
        if found == 0:
            raise ToolError(
                f"Edit {number}: its old text isn't in the note (after the edits before it), "
                "so nothing was changed. Copy it exactly from the note, spaces and line breaks "
                "included."
            )
        if found > 1:
            raise ToolError(
                f"Edit {number}: its old text appears {found} times, so nothing was changed. "
                "Include more of the text around it, so that it appears once."
            )
        body = body.replace(old, new, 1)
    return body


def apply_append(body: str, append: Append) -> str:
    newline = line_ending(body)
    text = in_style(append.text, newline)
    if append.section is None:
        if not body:
            return text
        separator = "" if body.endswith(("\n", "\r")) else newline
        added = body + separator + text
        if body.endswith(("\n", "\r")) and not text.endswith(newline):
            added += newline
        return added
    try:
        section = find_section(body, append.section)
    except SectionError as exc:
        raise ToolError(f"{exc} Nothing was changed.") from exc
    part = body[section.start : section.end]
    # After the section's last line of text: the end of that line, before the blank
    # lines that end the section. A scan, not a regular expression, so a long run
    # of blank lines can't make it slow.
    content = len(part.rstrip(" \t\r\n"))
    breaks = [i for i in (part.find("\r", content), part.find("\n", content)) if i >= 0]
    at = section.start + (min(breaks) if breaks else len(part))
    return body[:at] + newline + text.rstrip("\r\n") + body[at:]


def apply_prepend(body: str, prepend: Prepend) -> str:
    newline = line_ending(body)
    text = in_style(prepend.text, newline)
    if body and not text.endswith(newline):
        text += newline
    return text + body


def register(mcp: FastMCP, *, max_chars: int, ask_every_time: bool = True) -> None:
    """Register joplin_edit_note; ``max_chars`` is ``SBJ_MAX_NOTE_CHARS``.

    It bounds both the note an edit leaves and what one call carries (the text of
    every ``old``, ``new``, ``append`` and ``prepend`` together).
    """

    @mcp.tool(annotations=CHANGES_NOTE, meta=ASK_EVERY_TIME if ask_every_time else None)
    @noting(
        f"The note may be up to {max_chars:,} characters after the change, and the "
        f"text in one call up to {max_chars:,} characters together."
    )
    @guarded
    async def joplin_edit_note(
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
        expected_sha256: Checksum,
        edits: Annotated[
            list[TextEdit] | None,
            Field(
                max_length=MAX_EDITS,
                description=f"Up to {MAX_EDITS} replacements, applied in order.",
            ),
        ] = None,
        append: Append | None = None,
        prepend: Prepend | None = None,
    ) -> EditedNote:
        """Change part of a note, or add text to a section or to its start or end.

        First read the note with joplin_read: give its ``title`` and
        ``body_sha256`` (as ``expected_sha256``) here, so a note changed since
        is never overwritten. Each ``edits`` entry replaces ``old``, copied
        exactly from the note and found there once, with ``new``. ``append``
        adds text at the end of a section (a heading's text, or a path from the
        outline) or of the note; ``prepend`` adds it at the start. Nothing else
        in the note changes. If any part doesn't fit, nothing is written. The
        server keeps a copy of the note as it was.
        """
        if not (edits or append or prepend):
            raise ToolError("Give at least one of edits, append or prepend.")
        size = sum(len(e.old) + len(e.new) for e in edits or [])
        size += len(append.text) if append else 0
        size += len(prepend.text) if prepend else 0
        if size > max_chars:
            raise ToolError(
                f"The edits hold {size:,} characters, over the limit of {max_chars:,} for "
                "one call, so nothing was written."
            )

        def make_body(body: str) -> str:
            if edits:
                body = apply_edits(body, edits)
            if append:
                body = apply_append(body, append)
            if prepend:
                body = apply_prepend(body, prepend)
            return body

        described = json.dumps(
            {
                "edits": [e.model_dump() for e in edits or []],
                "append": append.model_dump() if append else None,
                "prepend": prepend.model_dump() if prepend else None,
            },
            sort_keys=True,
        )
        result = await change_note(
            app_context(ctx),
            ChangeRequest(
                tool="joplin_edit_note",
                note_ref=note_id,
                title=title,
                expected_sha256=expected_sha256,
                make_body=make_body,
                max_chars=max_chars,
                request_parts=(described,),
            ),
        )
        return EditedNote(**result.model_dump(), edits_applied=len(edits or []))
