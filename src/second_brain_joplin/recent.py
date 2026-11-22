"""A short memory of the notes this process asked Joplin to create.

A note is created by one request, and requests get repeated: a client that timed
out sends the call again, a model that saw an error tries once more. Without a
memory, each repeat is a second copy of the note. So the write tool remembers, for
a few minutes, which id it sent for which note (same notebook, title and text),
and looks for that note before making another.

The memory itself is second-brain-core's (``second_brain_core.recent``). Kept in
memory only and never written anywhere: it holds digests and ids, no note text,
and it is fine to lose on a restart.
"""

from second_brain_core.recent import REMEMBER_AT_MOST, REMEMBER_SECONDS, Recent, request_key

__all__ = [
    "REMEMBER_AT_MOST",
    "REMEMBER_SECONDS",
    "Recent",
    "RecentNotes",
    "note_key",
    "request_key",
]


def note_key(notebook_id: str, title: str, body: str) -> str:
    """A digest that is the same for the same note in the same notebook."""
    return request_key(notebook_id, title, body)


class RecentNotes(Recent[str]):
    """Which note id was sent for which new note, for the last few minutes."""
