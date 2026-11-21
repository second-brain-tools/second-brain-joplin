"""Structured response models returned by the MCP tools.

Returning pydantic models (rather than plain dicts) gives FastMCP a rich output
schema for each tool automatically.
"""

from datetime import UTC, datetime

from pydantic import BaseModel

EXCERPT_LENGTH = 200

# Bounds on what one tool call may ask for. Claude caps a tool result near 150,000
# characters, so a single page of a note stays well under that.
MAX_SEARCH_LIMIT = 100
MAX_SEMANTIC_LIMIT = 50
# A keyword search looks at no more than this many pages of Joplin's answer (100 hits
# each), however few of them the notebook filter lets through.
MAX_SEARCH_PAGES = 20
MAX_RECENT_LIMIT = 100
MAX_LIST_LIMIT = 100
MAX_RECENT_DAYS = 3650
READ_DEFAULT_CHARS = 50_000
READ_MAX_CHARS = 100_000
# joplin_read_notes: at most this many notes, sharing this many characters of text.
READ_NOTES_MAX = 20
READ_NOTES_BUDGET = 40_000
# Bounds on a note the write tool may create: one page of joplin_read holds it.
MAX_TITLE_CHARS = 300
MAX_NOTE_CHARS = 100_000


def epoch_ms_to_iso(value: int) -> str:
    """Convert a Joplin epoch-millisecond timestamp to an ISO-8601 string."""
    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()


def excerpt(body: str) -> str:
    """Collapse whitespace and truncate ``body`` to a short single-line excerpt."""
    text = " ".join((body or "").split())
    if len(text) <= EXCERPT_LENGTH:
        return text
    return text[:EXCERPT_LENGTH].rstrip() + "…"


class Attachment(BaseModel):
    """A file attached to a note (a Joplin resource): what it is, not its content."""

    id: str
    title: str
    mime: str
    size: int


class Note(BaseModel):
    """A Joplin note, or one page of a long one, or of one section, or its outline.

    ``body`` holds the characters from ``offset`` of what was asked for: the whole
    body, one section (``section_path``, ``section_heading``) or the outline
    (``is_outline``). ``total_chars`` is that text's length, and ``next_offset`` is
    where the next page starts, or None when this page reaches the end.
    """

    id: str
    title: str
    body: str
    parent_id: str
    created_time: str
    updated_time: str
    offset: int = 0
    total_chars: int = 0
    next_offset: int | None = None
    section_path: str | None = None
    section_heading: str | None = None
    is_outline: bool = False
    notebook: str | None = None
    notebook_path: list[str] = []
    tags: list[str] = []
    is_todo: bool = False
    todo_due: str | None = None
    todo_completed: str | None = None
    attachments: list[Attachment] = []
    body_sha256: str = ""


class NotesRead(BaseModel):
    """Several notes read in one call, sharing one budget of characters.

    A note cut short by the budget has ``next_offset`` set: read the rest with
    ``joplin_read``. ``not_found`` lists the ids that are missing or hidden, alike.
    """

    notes: list[Note]
    not_found: list[str]
    budget: int


class RecentNote(BaseModel):
    """A lightweight note entry for the recent-notes listing."""

    id: str
    title: str
    updated_time: str


class SearchResult(BaseModel):
    """A search hit: the note, the start of its text, and where the match is.

    ``snippet``, ``section`` and ``section_heading`` are null when the query has
    no plain word to find in the body (a filter-only query such as ``tag:x``).
    """

    id: str
    title: str
    excerpt: str
    notebook_id: str = ""
    updated_time: str | None = None
    snippet: str | None = None
    section: str | None = None
    section_heading: str | None = None


class ChangedNote(BaseModel):
    """What a tool that changes a note did: enough to check it without reading it again."""

    id: str
    title: str
    notebook_id: str
    notebook: str
    old_chars: int
    new_chars: int
    old_sha256: str
    new_sha256: str
    updated_time: str | None = None
    # The same request was answered earlier, and the note still holds its result:
    # nothing was written this time.
    already_applied: bool = False


class Change(BaseModel):
    """One change the server made, as joplin_changes lists it. Never any text."""

    at: str
    tool: str
    note_id: str
    title: str | None  # the note's title now; null when the note is gone
    old_chars: int
    new_chars: int
    outcome: str


class ListedNote(BaseModel):
    """A note in a notebook's listing."""

    id: str
    title: str
    updated_time: str
    is_todo: bool


class NoteListing(BaseModel):
    """One page of a notebook's own notes. ``next_offset`` is null on the last page."""

    notebook_id: str
    notebook: str
    notes: list[ListedNote]
    total: int
    next_offset: int | None = None


class Notebook(BaseModel):
    """A notebook (Joplin folder) with its note count and nested children."""

    id: str
    name: str
    note_count: int
    children: list["Notebook"] = []


class SemanticResult(BaseModel):
    """A semantic-search hit: note identity, snippet, and similarity score."""

    id: str
    title: str
    excerpt: str
    score: float
    chunk_index: int


class ReindexResult(BaseModel):
    """Summary of a semantic index sync/rebuild."""

    added: int
    updated: int
    deleted: int
    total: int
    model: str


class CreatedNote(BaseModel):
    """A note the write tool created: where it is and what it is called.

    ``already_created`` is True when an identical request made this note in the
    last few minutes: nothing new was written, and this is that note.
    """

    id: str
    title: str
    notebook_id: str
    notebook: str
    created_time: str | None = None
    already_created: bool = False
