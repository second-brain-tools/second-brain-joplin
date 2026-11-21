"""Read-only MCP tools backed by the Joplin Data API."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..context import app_context
from ..errors import JoplinError, JoplinNotFoundError, NotebookFilterError
from ..joplin_client import JoplinClient, JsonDict
from ..markdown import SectionError, find_section, section_at
from ..markdown import outline as _outline
from ..models import (
    MAX_LIST_LIMIT,
    MAX_RECENT_DAYS,
    MAX_RECENT_LIMIT,
    MAX_SEARCH_LIMIT,
    MAX_SEARCH_PAGES,
    READ_DEFAULT_CHARS,
    READ_MAX_CHARS,
    READ_NOTES_BUDGET,
    READ_NOTES_MAX,
    Attachment,
    ListedNote,
    Note,
    Notebook,
    NoteListing,
    NotesRead,
    RecentNote,
    SearchResult,
    epoch_ms_to_iso,
    excerpt,
)
from ..scope import (
    ID_RE,
    NOTE_REF_PATTERN,
    UNFILTERED,
    NotebookScope,
    note_id_of,
    scope_of,
    subtrees,
)
from ..search import first_match, keywords, matching, notebook_terms, snippet
from .annotations import READ_ONLY
from .failure import tool_error

SearchLimit = Annotated[int, Field(ge=1, le=MAX_SEARCH_LIMIT)]
NoteId = Annotated[
    str,
    Field(
        pattern=NOTE_REF_PATTERN,
        description=(
            "A note's 32-character id; a link to it (':/<id>' or "
            "'joplin://x-callback-url/openNote?id=<id>') works too."
        ),
    ),
]


def _build_overview(
    folders: list[JsonDict], notes_index: list[JsonDict], scope: NotebookScope
) -> list[Notebook]:
    """Assemble the tree of visible notebooks with per-notebook note counts.

    ``folders`` and ``notes_index`` are raw Joplin dicts. A notebook whose parent
    isn't shown (a root, or a visible notebook under one the filter hides) sits
    at the top of the tree, so a hidden parent's name never appears.
    """
    shown = [folder for folder in folders if scope.allows(folder["id"])]
    shown_ids = {folder["id"] for folder in shown}

    counts: dict[str, int] = {}
    for note in notes_index:
        parent = note.get("parent_id", "")
        counts[parent] = counts.get(parent, 0) + 1

    children_of: dict[str, list[JsonDict]] = {}
    for folder in shown:
        children_of.setdefault(folder.get("parent_id", ""), []).append(folder)

    def build(folder: JsonDict) -> Notebook:
        folder_id = folder["id"]
        return Notebook(
            id=folder_id,
            name=folder.get("title", ""),
            note_count=counts.get(folder_id, 0),
            children=[build(child) for child in children_of.get(folder_id, [])],
        )

    return [build(folder) for folder in shown if folder.get("parent_id", "") not in shown_ids]


NO_SUCH_NOTEBOOK = "No notebook has that title or id."


def _visible_notebook(wanted: str, visible: dict[str, str]) -> str:
    """The id of the visible notebook ``wanted`` names, by id or exact title.

    A hidden notebook gets the same answer as one that doesn't exist. A title two
    visible notebooks share is refused: the id is needed then.
    """
    wanted = wanted.strip()
    if ID_RE.fullmatch(wanted):
        if wanted.lower() in visible:
            return wanted.lower()
        raise JoplinNotFoundError(NO_SUCH_NOTEBOOK)
    matches = [notebook_id for notebook_id, title in visible.items() if title == wanted]
    if not matches:
        raise JoplinNotFoundError(NO_SUCH_NOTEBOOK)
    if len(matches) > 1:
        ids = ", ".join(sorted(matches))
        raise JoplinNotFoundError(f"Two or more notebooks have that title; use the id ({ids}).")
    return matches[0]


def share_budget(lengths: list[int], budget: int) -> list[int]:
    """How many characters each text gets of ``budget``: the short ones in full,
    the rest split evenly over the longer ones."""
    shares = [0] * len(lengths)
    left = budget
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    for position, index in enumerate(order):
        fair = left // (len(order) - position)
        shares[index] = min(lengths[index], fair)
        left -= shares[index]
    return shares


def _time_or_none(value: object) -> str | None:
    return epoch_ms_to_iso(value) if isinstance(value, int) and value > 0 else None


def notebook_path(folders: list[JsonDict], scope: NotebookScope, notebook_id: str) -> list[str]:
    """Names from the topmost *visible* notebook down to ``notebook_id``.

    The walk up stops at a hidden notebook, so a hidden parent's name never appears.
    """
    by_id = {f["id"]: f for f in folders}
    path: list[str] = []
    seen: set[str] = set()
    current = notebook_id
    while current and current in by_id and current not in seen and scope.allows(current):
        seen.add(current)
        path.append(by_id[current].get("title", ""))
        current = by_id[current].get("parent_id", "")
    return list(reversed(path))


async def _details(
    client: JoplinClient, scope: NotebookScope, folders: list[JsonDict], note: JsonDict
) -> dict[str, Any]:
    """The facts about a visible note beyond its text: notebook, tags, to-do, attachments.

    Tags are one list for the whole vault, so they are given only as properties of
    this note. An attachment can be referred to by any note, including one a caller
    wrote itself, so with a filter set it is listed only when no hidden note refers
    to it: otherwise it is left out, as if it didn't exist.
    """
    path = notebook_path(folders, scope, note.get("parent_id", ""))
    tags = sorted(t.get("title", "") for t in await client.get_note_tags(note["id"]))
    attachments = []
    for resource in await client.get_note_resources(note["id"]):
        if scope.visible is not None:
            users = await client.get_resource_notes(resource["id"])
            if not all(scope.allows_note(user) for user in users):
                continue
        attachments.append(
            Attachment(
                id=resource["id"],
                title=resource.get("title", ""),
                mime=resource.get("mime", ""),
                size=int(resource.get("size", 0) or 0),
            )
        )
    return {
        "notebook": path[-1] if path else None,
        "notebook_path": path,
        "tags": tags,
        "is_todo": bool(note.get("is_todo")),
        "todo_due": _time_or_none(note.get("todo_due")),
        "todo_completed": _time_or_none(note.get("todo_completed")),
        "attachments": attachments,
        "body_sha256": hashlib.sha256(note.get("body", "").encode()).hexdigest(),
    }


def _search_result(hit: JsonDict, words: list[str]) -> SearchResult:
    body: str = hit.get("body", "")
    at = first_match(body, words)
    found = section_at(body, at) if at is not None else None
    updated = hit.get("updated_time")
    return SearchResult(
        id=hit["id"],
        title=hit.get("title", ""),
        excerpt=excerpt(body),
        notebook_id=hit.get("parent_id", ""),
        updated_time=epoch_ms_to_iso(updated) if isinstance(updated, int) else None,
        snippet=snippet(body, at) if at is not None else None,
        section=found.path if found else None,
        section_heading=found.heading if found else None,
    )


def register(mcp: FastMCP) -> None:
    """Register the read tools on the given FastMCP server."""

    @mcp.tool(annotations=READ_ONLY)
    async def joplin_overview(ctx: Context) -> list[Notebook]:
        """List the Joplin notebooks as a tree with note counts."""
        app = app_context(ctx)
        try:
            folders = await app.client.get_folders()
            scope = scope_of(app.settings, folders)
            notes_index = await app.client.get_notes_index()
        except (JoplinError, NotebookFilterError) as exc:
            raise tool_error(exc) from exc
        return _build_overview(folders, notes_index, scope)

    @mcp.tool(annotations=READ_ONLY)
    async def joplin_search(
        ctx: Context, query: str, limit: SearchLimit = 10
    ) -> list[SearchResult]:
        """Search notes by keyword, with Joplin's search words.

        Each hit has its notebook, when it last changed, a snippet around the
        first word that matched, and the section that holds it (``section`` is a
        path ``joplin_read`` accepts). The snippet often answers the question
        without reading the note.

        Plain words must all match (``any:1 a b``: any of them); ``"a phrase"``;
        ``word*`` for a prefix; ``-word`` to leave out. Filters, combined with
        spaces, ``-`` to negate: ``notebook:"Name"`` (with its sub-notebooks),
        ``tag:name``, ``title:word``, ``body:word``, ``type:todo`` or
        ``type:note``, ``iscompleted:0`` or ``1``, ``created:20261101`` or
        ``created:day-7`` (also ``week-``, ``month-``, ``year-``), ``updated:…``,
        ``due:…``, ``sourceurl:…``, ``resource:image/png``. Examples:
        ``notebook:"Work" budget``, ``tag:idea -tag:archived``,
        ``type:todo iscompleted:0``.
        """
        app = app_context(ctx)
        terms = notebook_terms(query)
        try:
            if app.settings.notebook_filter_set:
                folders = await app.client.get_folders()
                scope = scope_of(app.settings, folders)
            else:
                folders, scope = [], UNFILTERED
            keep = scope.allows_note
            sent = query
            if scope.visible is not None and (terms.include or terms.exclude):
                visible = {f["id"]: f.get("title", "") for f in folders if scope.allows(f["id"])}

                def under(ids: set[str]) -> set[str]:
                    return {n for n in subtrees(folders, ids) if n in visible}

                allowed = under(matching(terms.include, visible)) if terms.include else None
                if allowed is not None and not allowed:
                    return []  # as for a name that matches no notebook at all
                excluded = under(matching(terms.exclude, visible))
                sent = terms.query
                if not sent:
                    raise ToolError("A search needs words or filters besides -notebook:.")

                def keep(note: JsonDict) -> bool:
                    parent = note.get("parent_id", "")
                    return (
                        scope.allows_note(note)
                        and (allowed is None or parent in allowed)
                        and parent not in excluded
                    )

            hits = await app.client.search(sent, limit, keep=keep, max_pages=MAX_SEARCH_PAGES)
        except (JoplinError, NotebookFilterError) as exc:
            raise tool_error(exc) from exc
        words = keywords(query)
        return [_search_result(hit, words) for hit in hits]

    @mcp.tool(annotations=READ_ONLY)
    async def joplin_read(
        ctx: Context,
        note_id: NoteId,
        offset: Annotated[int, Field(ge=0)] = 0,
        max_chars: Annotated[int, Field(ge=1, le=READ_MAX_CHARS)] = READ_DEFAULT_CHARS,
        outline: Annotated[
            bool,
            Field(
                description=(
                    "Return the note's headings instead of its text: one line per section, "
                    "'path<TAB>characters<TAB>heading'. Can't be combined with section."
                )
            ),
        ] = False,
        section: Annotated[
            str | None,
            Field(
                max_length=300,
                description=(
                    "Return only this section, subsections included: its path from the "
                    "outline ('1.2'; '0' is the text before the first heading) or its "
                    "heading's exact text, which must appear once."
                ),
            ),
        ] = None,
    ) -> Note:
        """Read a note's markdown by its ID, a page at a time for long notes.

        Returns up to ``max_chars`` characters starting at ``offset``. When
        ``next_offset`` is set, call again with ``offset=next_offset`` for the rest;
        ``total_chars`` is the length of what is being read. For a long note, ask
        for its ``outline`` first, then read one ``section``. Only ``#`` headings
        outside code blocks count as headings.
        """
        if outline and section is not None:
            raise ToolError("Ask for the outline or for one section, not both.")
        app = app_context(ctx)
        try:
            folders = await app.client.get_folders()
            scope = scope_of(app.settings, folders)
            note = await app.client.get_note(note_id_of(note_id))
            if not scope.allows_note(note):
                # The same answer as a note that doesn't exist.
                raise JoplinNotFoundError()
            details = await _details(app.client, scope, folders, note)
        except (JoplinError, NotebookFilterError) as exc:
            raise tool_error(exc) from exc
        body: str = note.get("body", "")
        text = body
        section_path: str | None = None
        section_heading: str | None = None
        if outline:
            text = _outline(body)
        elif section is not None:
            try:
                found = find_section(body, section)
            except SectionError as exc:
                raise ToolError(str(exc)) from exc
            text = body[found.start : found.end]
            section_path, section_heading = found.path, found.heading
        end = offset + max_chars
        return Note(
            id=note["id"],
            title=note.get("title", ""),
            body=text[offset:end],
            parent_id=note.get("parent_id", ""),
            created_time=epoch_ms_to_iso(note["created_time"]),
            updated_time=epoch_ms_to_iso(note["updated_time"]),
            offset=offset,
            total_chars=len(text),
            next_offset=end if end < len(text) else None,
            section_path=section_path,
            section_heading=section_heading,
            is_outline=outline,
            **details,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def joplin_read_notes(
        ctx: Context,
        note_ids: Annotated[
            list[NoteId],
            Field(
                min_length=1,
                max_length=READ_NOTES_MAX,
                description=f"Up to {READ_NOTES_MAX} note ids (or links to notes).",
            ),
        ],
    ) -> NotesRead:
        """Read several notes in one call, for example the results of a search.

        Each note comes as ``joplin_read`` gives it. Their texts share one budget
        of characters: short notes take what they need, and the rest is split
        over the longer ones. A note cut short has ``next_offset`` set: read the
        rest, or one section of it, with ``joplin_read``. Ids that don't resolve
        are listed in ``not_found``.
        """
        app = app_context(ctx)
        wanted = list(dict.fromkeys(note_id_of(ref) for ref in note_ids))
        found: list[tuple[JsonDict, dict[str, Any]]] = []
        not_found: list[str] = []
        try:
            folders = await app.client.get_folders()
            scope = scope_of(app.settings, folders)
            for wanted_id in wanted:
                try:
                    note = await app.client.get_note(wanted_id)
                except JoplinNotFoundError:
                    not_found.append(wanted_id)
                    continue
                if not scope.allows_note(note):
                    not_found.append(wanted_id)  # the same answer as a missing note
                    continue
                found.append((note, await _details(app.client, scope, folders, note)))
        except (JoplinError, NotebookFilterError) as exc:
            raise tool_error(exc) from exc
        shares = share_budget([len(n.get("body", "")) for n, _ in found], READ_NOTES_BUDGET)
        notes = []
        for (note, details), take in zip(found, shares, strict=True):
            body = note.get("body", "")
            notes.append(
                Note(
                    id=note["id"],
                    title=note.get("title", ""),
                    body=body[:take],
                    parent_id=note.get("parent_id", ""),
                    created_time=epoch_ms_to_iso(note["created_time"]),
                    updated_time=epoch_ms_to_iso(note["updated_time"]),
                    offset=0,
                    total_chars=len(body),
                    next_offset=take if take < len(body) else None,
                    **details,
                )
            )
        return NotesRead(notes=notes, not_found=not_found, budget=READ_NOTES_BUDGET)

    @mcp.tool(annotations=READ_ONLY)
    async def joplin_list_notes(
        ctx: Context,
        notebook: Annotated[
            str,
            Field(
                min_length=1,
                max_length=300,
                description="The notebook: its exact title, or its id.",
            ),
        ],
        limit: Annotated[int, Field(ge=1, le=MAX_LIST_LIMIT)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
        order: Annotated[
            Literal["updated", "title"],
            Field(description="'updated' (newest first) or 'title' (A to Z)."),
        ] = "updated",
    ) -> NoteListing:
        """List the notes in one notebook, a page at a time.

        Only the notebook's own notes: ``joplin_overview`` shows its
        sub-notebooks. Notes in the trash and conflict copies are left out. When
        ``next_offset`` is set, call again with ``offset=next_offset``.
        """
        app = app_context(ctx)
        try:
            folders = await app.client.get_folders()
            scope = scope_of(app.settings, folders)
            visible = {f["id"]: f.get("title", "") for f in folders if scope.allows(f["id"])}
            folder_id = _visible_notebook(notebook, visible)
            rows = await app.client.get_folder_notes(folder_id)
        except (JoplinError, NotebookFilterError) as exc:
            raise tool_error(exc) from exc
        notes = [
            row
            for row in rows
            if not row.get("is_conflict")
            and not row.get("deleted_time")
            and row.get("parent_id", folder_id) == folder_id
        ]
        if order == "title":
            notes.sort(key=lambda row: (str(row.get("title", "")).casefold(), row["id"]))
        else:
            notes.sort(key=lambda row: (-int(row.get("updated_time", 0)), row["id"]))
        page = notes[offset : offset + limit]
        end = offset + len(page)
        return NoteListing(
            notebook_id=folder_id,
            notebook=visible[folder_id],
            notes=[
                ListedNote(
                    id=row["id"],
                    title=row.get("title", ""),
                    updated_time=epoch_ms_to_iso(int(row.get("updated_time", 0))),
                    is_todo=bool(row.get("is_todo")),
                )
                for row in page
            ],
            total=len(notes),
            next_offset=end if end < len(notes) else None,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def joplin_recent(
        ctx: Context,
        days: Annotated[int, Field(ge=1, le=MAX_RECENT_DAYS)] = 7,
        limit: Annotated[int, Field(ge=1, le=MAX_RECENT_LIMIT)] = 20,
    ) -> list[RecentNote]:
        """List notes modified in the last N days, most recent first."""
        app = app_context(ctx)
        cutoff_ms = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)
        try:
            scope = await app.scope()
            notes = await app.client.get_recent(limit, since_ms=cutoff_ms, keep=scope.allows_note)
        except (JoplinError, NotebookFilterError) as exc:
            raise tool_error(exc) from exc
        return [
            RecentNote(
                id=note["id"],
                title=note.get("title", ""),
                updated_time=epoch_ms_to_iso(note["updated_time"]),
            )
            for note in notes
        ]
