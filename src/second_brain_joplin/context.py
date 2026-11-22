"""Application context handed to MCP tools via the server lifespan.

Holds the shared Joplin client and settings, and lazily builds the semantic
search service on first use. The optional ML/vector backends are imported only
inside :meth:`AppContext.semantic` (second-brain-core's ``semantic.embedders`` and
``semantic.stores``), so importing this module — and therefore starting the
server on a base install — never requires the extras.
"""

import asyncio
from dataclasses import dataclass, field
from typing import cast

from fastmcp import Context

from .config import Settings
from .joplin_client import JoplinClient
from .journal import Journal
from .models import ChangedNote
from .recent import Recent, RecentNotes
from .scope import NotebookScope, WriteTargets, current_scope, current_write_targets
from .semantic.service import SemanticSearchService


@dataclass
class AppContext:
    """Per-server shared state reachable from tools via ``ctx.lifespan_context``."""

    client: JoplinClient
    settings: Settings
    # True in HTTP mode: tools that would run past a remote client's time limit
    # (a full reindex) refuse there and point at the CLI instead.
    remote: bool = False
    # What the write tool sent lately, so a repeated request isn't a second note.
    recent_notes: RecentNotes = field(default_factory=RecentNotes, repr=False)
    # What the tools that change a note answered lately, so a repeat changes nothing.
    recent_changes: Recent[ChangedNote] = field(default_factory=Recent, repr=False)
    _semantic: SemanticSearchService | None = field(default=None, init=False, repr=False)
    _journal: Journal | None = field(default=None, init=False, repr=False)
    _note_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)

    def journal(self) -> Journal:
        """The journal of kept copies (journal.py), from the settings."""
        if self._journal is None:
            self._journal = Journal(
                self.settings.undo_dir,
                days=self.settings.undo_days,
                max_mb=self.settings.undo_max_mb,
            )
        return self._journal

    def note_lock(self, note_id: str) -> asyncio.Lock:
        """One lock per note: two changes to the same note run one after the other."""
        return self._note_locks.setdefault(note_id, asyncio.Lock())

    async def scope(self) -> NotebookScope:
        """The notebooks a tool may see, resolved against Joplin now (scope.py).

        Raises :class:`NotebookFilterError` when the configured filter doesn't
        resolve; a tool must then refuse rather than show anything.
        """
        return await current_scope(self.client, self.settings)

    async def write_targets(self) -> WriteTargets:
        """The notebooks a note may be created in, resolved against Joplin now.

        Empty when the server is read-only. Raises :class:`NotebookFilterError`
        when the notebook settings don't resolve; the write tool must then refuse.
        """
        return await current_write_targets(self.client, self.settings)

    def semantic(self) -> SemanticSearchService:
        """Return the semantic service, building it (and loading the model) once.

        Raises :class:`SemanticUnavailableError` if the semantic extra is missing
        or misconfigured.
        """
        if self._semantic is None:
            from second_brain_core.semantic import embedders, stores

            from .identity import SERVER

            embedder = embedders.build_embedder(
                SERVER, self.settings.embedding_backend, self.settings.embedding_model
            )
            index = stores.build_vector_index(
                SERVER, self.settings.vector_store, self.settings.semantic_index_dir
            )
            self._semantic = SemanticSearchService(self.client, embedder, index, self.settings)
        return self._semantic


def app_context(ctx: Context) -> AppContext:
    """Fetch the :class:`AppContext` from a FastMCP tool context."""
    return cast(AppContext, ctx.lifespan_context)
