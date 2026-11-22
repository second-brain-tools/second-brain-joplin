"""Search by meaning over Joplin notes: the Joplin side of second-brain-core's engine.

The engine (``second_brain_core.semantic``) keeps the index in step and searches
it. This module tells it about Joplin: :class:`JoplinSource` lists the notes the
notebook filter lets through with their versions, and reads one note's title and
text, asking the filter again, since a note may have moved since the listing.

The notebook filter (scope.py) decides what the index may hold: a note the filter
hides is never embedded, and one that becomes hidden (it moved, its notebook moved,
or the filter changed) is dropped by the next sync, before anything new is
embedded. What a search returns does not rest on the index being clean, though:
with a filter set, every search asks Joplin which notes are visible now and returns
only those, whatever the index holds.
"""

from second_brain_core.semantic.engine import Engine
from second_brain_core.semantic.types import (
    Embedder,
    ItemText,
    Skip,
    SyncStats,
    VectorIndex,
)

from ..config import Settings
from ..joplin_client import JoplinClient
from ..models import SemanticResult, excerpt
from ..scope import current_scope


class JoplinSource:
    """Joplin's notes, as the engine sees them: visible ones, with ``updated_time``."""

    def __init__(self, client: JoplinClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    @property
    def filter_active(self) -> bool:
        return self._settings.notebook_filter_set

    async def list_versions(self) -> dict[str, str]:
        scope = await current_scope(self._client, self._settings)
        notes = await self._client.list_note_versions()
        return {note["id"]: str(note["updated_time"]) for note in notes if scope.allows_note(note)}

    async def get_item(self, item_id: str) -> ItemText | Skip:
        note = await self._client.get_note(item_id)
        # The listing said this note was visible; it may have moved since. What is
        # embedded is decided by the notebook the text was actually read from, under
        # the filter as it is now (no scope is kept between calls: a search may list
        # while a sync reads). Without a filter this asks Joplin nothing more.
        scope = await current_scope(self._client, self._settings)
        if not scope.allows_note(note):
            return Skip.HIDDEN
        # Synced in encrypted, and not decrypted yet: its title and text read as
        # empty. Not marked, since decrypting keeps its version: looked at again.
        if note.get("encryption_applied"):
            return Skip.NOT_READY
        return ItemText(title=str(note.get("title", "")), text=str(note.get("body", "")))


class SemanticSearchService:
    """Keeps the embedding index in sync with Joplin and answers semantic queries."""

    def __init__(
        self,
        client: JoplinClient,
        embedder: Embedder,
        index: VectorIndex,
        settings: Settings,
    ) -> None:
        self.engine = Engine(
            JoplinSource(client, settings),
            embedder,
            index,
            auto_sync=settings.semantic_auto_sync,
        )

    async def sync(self, *, full: bool = False) -> SyncStats:
        """Incrementally re-embed changed notes (or rebuild everything)."""
        return await self.engine.sync(full=full)

    async def prune(self) -> int:
        """Drop indexed notes that are hidden or gone, embedding nothing."""
        return await self.engine.prune()

    async def search(self, query: str, limit: int = 10) -> list[SemanticResult]:
        """Return up to ``limit`` notes most semantically similar to ``query``."""
        return [
            SemanticResult(
                id=hit.item_id,
                title=hit.title,
                excerpt=excerpt(hit.text),
                score=hit.score,
                chunk_index=hit.chunk_index,
            )
            for hit in await self.engine.search(query, limit)
        ]
