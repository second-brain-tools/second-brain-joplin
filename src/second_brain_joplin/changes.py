"""The safety net every tool that changes a note goes through (#82).

One function, :func:`change_note`, does what is the same for all of them; each tool
supplies only how the new body is made from the old one. In this order:

1. **The notebook filter.** A note it hides gets the answer a missing note gets,
   before any other check can answer differently.
2. **The edit list** (``SBJ_EDIT_NOTEBOOKS``): the note's notebook must be on it.
3. **A repeat.** The same call within ten minutes, while the note still holds what
   the first one left, is answered with the first result and changes nothing.
4. **The title** must be the note's current title: a prompt then names the note.
5. **The kind of note.** Never one in the trash, a conflict copy, an encrypted
   note, or one that isn't Markdown.
6. **The checksum** of the body the caller read must be the body's now, so an edit
   made since is never overwritten unseen.
7. **The change itself**, from the tool. Then: not the same text, not over the
   size limit, and every attachment the note refers to still referred to (Joplin
   deletes an attachment no note refers to).
8. **The daily limit** (``SBJ_CHANGES_PER_DAY``), counted from the journal.
9. **The journal**: the note as it was, on disk, before anything is sent. No
   copy, no change.
10. **The write**: ``PUT`` with the body alone. If the answer is lost, the note is
    read again to say whether the change arrived.

All of it runs under a lock for the note, so two changes to one note never interleave.
Audit lines name the tool, the note's id and the caller: never a title or text.
"""

import asyncio
import functools
import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastmcp.exceptions import ToolError

from .audit import caller_id
from .context import AppContext
from .errors import (
    JoplinAuthError,
    JoplinConnectionError,
    JoplinError,
    NotebookFilterError,
)
from .failure import tool_error
from .journal import JournalError
from .models import MAX_NOTE_CHARS, ChangedNote, epoch_ms_to_iso
from .recent import request_key
from .scope import WriteTargets, edit_targets_of, note_id_of, scope_of, visible_note

audit_logger = logging.getLogger("second_brain_joplin.audit")
logger = logging.getLogger("second_brain_joplin.changes")

REFUSAL_LISTS = 20
MARKDOWN = 1  # Joplin's markup_language for Markdown; 2 is HTML
RESOURCE_REF = r":/([0-9a-fA-F]{32})"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def references(text: str) -> set[str]:
    """The ids a text refers to as ``:/<id>``, the way Joplin links an attachment."""
    return {found.lower() for found in re.findall(RESOURCE_REF, text)}


@dataclass(frozen=True)
class ChangeRequest:
    """What a tool asks of :func:`change_note`."""

    tool: str
    note_ref: str
    title: str
    expected_sha256: str
    # Makes the new body from the current one; raises ToolError to refuse.
    make_body: Callable[[str], str]
    # Everything else that makes this call this call (the edits, the new body), for
    # the repeat memory.
    request_parts: tuple[str, ...]
    allow_empty: bool = True
    # For a tool that sends the whole new text: when the note already holds it,
    # the call is a repeat however long ago it was made, and nothing is written.
    target_body: str | None = None


def _not_editable(targets: WriteTargets) -> ToolError:
    """The one answer for a visible note whose notebook isn't on the edit list."""
    notebooks = sorted(targets.titles.items(), key=lambda item: (item[1].casefold(), item[0]))
    if not notebooks:
        return ToolError(
            "Changing notes is off on this server: no notebook it lists in "
            "SBJ_EDIT_NOTEBOOKS is visible."
        )
    named = "; ".join(f"{title!r} ({nid})" for nid, title in notebooks[:REFUSAL_LISTS])
    more = len(notebooks) - REFUSAL_LISTS
    if more > 0:
        named += f"; and {more} more"
    return ToolError(
        f"This note's notebook isn't one whose notes can be changed. Those are: {named}"
    )


def refuse_kind(note: dict[str, object]) -> None:
    if note.get("deleted_time"):
        raise ToolError("This note is in Joplin's trash, so it can't be changed.")
    if note.get("is_conflict"):
        raise ToolError(
            "This note is a conflict copy, so it can't be changed. Resolve the conflict in Joplin."
        )
    if note.get("encryption_applied"):
        raise ToolError(
            "This note hasn't been decrypted on the server's Joplin, so it can't be changed."
        )
    if int(str(note.get("markup_language") or MARKDOWN)) != MARKDOWN:
        raise ToolError(
            "This note isn't Markdown (a clipped HTML page, say), so it can't be changed."
        )


async def change_note(app: AppContext, request: ChangeRequest) -> ChangedNote:
    """Run one change through every check, keep a copy, and write it. See the module."""
    note_id = note_id_of(request.note_ref)
    key = request_key(
        request.tool, note_id, request.title, request.expected_sha256, *request.request_parts
    )
    async with app.note_lock(note_id):
        try:
            folders = await app.client.get_folders()
            scope = scope_of(app.settings, folders)
            targets = edit_targets_of(app.settings, folders)
            note = await visible_note(app.client, scope, note_id)  # hidden = missing
        except (JoplinError, NotebookFilterError) as exc:
            raise tool_error(exc) from exc
        notebook_id = str(note.get("parent_id", ""))
        notebook_title = targets.title_of(notebook_id)
        if notebook_title is None:
            raise _not_editable(targets)
        body = str(note.get("body", ""))
        current = sha256(body)

        earlier = app.recent_changes.get(key)
        if earlier is not None and earlier.new_sha256 == current:
            return earlier.model_copy(update={"already_applied": True})
        if request.target_body is not None and body == request.target_body:
            return ChangedNote(
                id=note_id,
                title=str(note.get("title", "")),
                notebook_id=notebook_id,
                notebook=notebook_title,
                old_chars=len(body),
                new_chars=len(body),
                old_sha256=current,
                new_sha256=current,
                already_applied=True,
            )

        if str(note.get("title", "")) != request.title:
            raise ToolError(
                "The title doesn't match the note's current title, so nothing was changed. "
                "Read the note again and give its title as it is."
            )
        refuse_kind(note)
        if current != request.expected_sha256:
            raise ToolError(
                "The note has changed since it was read (its text's checksum is different), "
                "so nothing was changed. Read it again, then make the change on what it says now."
            )

        new_body = request.make_body(body)
        if new_body == body:
            raise ToolError("The change would leave the note as it is, so nothing was written.")
        if not new_body.strip() and not request.allow_empty:
            raise ToolError("A note's text can't be replaced with nothing.")
        if len(new_body) > MAX_NOTE_CHARS:
            raise ToolError(
                f"The note would be {len(new_body):,} characters, over the limit of "
                f"{MAX_NOTE_CHARS:,}, so nothing was written."
            )
        try:
            attached = [r["id"] for r in await app.client.get_note_resources(note_id)]
        except JoplinError as exc:
            raise tool_error(exc) from exc
        dropped = [r for r in attached if r.lower() not in references(new_body)]
        if dropped:
            raise ToolError(
                f"The change would remove {len(dropped)} reference(s) to the note's attachments "
                "(:/<id>), and Joplin deletes an attachment no note refers to, so nothing was "
                "written. Keep every :/<id> of an attachment in the text."
            )

        journal = app.journal()
        limit = app.settings.changes_per_day
        if await asyncio.to_thread(journal.changes_today) >= limit:
            resets = time.strftime("%Y-%m-%d %H:%M", time.localtime(journal.next_day()))
            raise ToolError(
                f"The server's limit of {limit} changes a day is reached, so nothing was "
                f"changed. It resets at {resets} (the server's time)."
            )
        new_sha = sha256(new_body)
        who = caller_id()

        def record(what: str, level: int = logging.INFO) -> None:
            audit_logger.log(
                level,
                "%s %s id=%s notebook=%s caller=%s",
                request.tool,
                what,
                note_id,
                notebook_id,
                who,
            )

        try:
            entry = await asyncio.to_thread(
                functools.partial(
                    journal.record,
                    tool=request.tool,
                    caller=who,
                    note=note,
                    new_chars=len(new_body),
                    old_sha256=current,
                    new_sha256=new_sha,
                )
            )
        except JournalError as exc:
            logger.warning("the journal couldn't be written: %s", exc.detail)
            raise ToolError(str(exc)) from exc

        result = ChangedNote(
            id=note_id,
            title=request.title,
            notebook_id=notebook_id,
            notebook=notebook_title,
            old_chars=len(body),
            new_chars=len(new_body),
            old_sha256=current,
            new_sha256=new_sha,
        )
        # Remembered before the write: a retry after an answer that got lost (or a
        # call the client gave up on) finds it, and, while the note holds the new
        # text, is answered with this instead of failing the checksum. Forgotten
        # below when the write is known to have failed.
        app.recent_changes.put(key, result)
        record("sent")
        try:
            answer = await app.client.update_note(note_id, body=new_body)
        except JoplinConnectionError as exc:
            if not exc.maybe_sent:
                journal.settle(entry, "failed")
                app.recent_changes.forget(key)
                record("failed")
                raise tool_error(exc) from exc
            outcome = await _arrived(app, note_id, new_sha)
            if outcome is not True:
                journal.settle(entry, "unconfirmed")
                record("unconfirmed", logging.WARNING)
                raise ToolError(
                    f"{exc} The change may or may not have been made: read the note to see, "
                    "and if it wasn't, make it again (a repeat of this request won't make "
                    "it twice)."
                ) from exc
            answer = {}
        except JoplinAuthError as exc:
            journal.settle(entry, "failed")
            app.recent_changes.forget(key)
            record("failed")
            raise tool_error(exc) from exc
        except JoplinError as exc:  # Joplin answered, and the answer was an error
            outcome = await _arrived(app, note_id, new_sha)
            if outcome is None:
                journal.settle(entry, "unconfirmed")
                record("unconfirmed", logging.WARNING)
                raise ToolError(
                    f"{exc} The change may or may not have been made: read the note to see."
                ) from exc
            if outcome is False:
                journal.settle(entry, "failed")
                app.recent_changes.forget(key)
                record("failed")
                raise tool_error(exc) from exc
            answer = {}
        journal.settle(entry, "applied")
        record("applied")
        await asyncio.to_thread(journal.prune)
        updated = answer.get("updated_time")
        if isinstance(updated, int):
            result = result.model_copy(update={"updated_time": epoch_ms_to_iso(updated)})
        app.recent_changes.put(key, result)
        return result


async def _arrived(app: AppContext, note_id: str, new_sha: str) -> bool | None:
    """Whether the note now holds the new text; None if Joplin won't say."""
    try:
        note = await app.client.get_note(note_id)
    except JoplinError:
        return None
    return sha256(str(note.get("body", ""))) == new_sha
