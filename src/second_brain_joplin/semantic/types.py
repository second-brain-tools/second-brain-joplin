"""Protocols and value objects shared across the semantic-search layer.

Nothing here imports an optional dependency — the concrete embedders and vector
stores in :mod:`.backends` implement these Protocols, so the orchestration in
:mod:`.service` can be exercised with in-memory fakes.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ChunkRecord:
    """One embedded chunk of a note, ready to persist in the vector index."""

    chunk_id: str
    note_id: str
    title: str
    updated_time: str
    chunk_index: int
    text: str
    embedding: list[float]


@dataclass(frozen=True)
class QueryHit:
    """A single nearest-neighbour result from the vector index."""

    note_id: str
    title: str
    text: str
    chunk_index: int
    score: float


@dataclass(frozen=True)
class SyncPlan:
    """The set of notes to (re)embed and to drop, computed from a version diff."""

    changed_ids: list[str]
    deleted_ids: list[str]


@dataclass(frozen=True)
class SyncStats:
    """Outcome of a sync/rebuild pass."""

    added: int
    updated: int
    deleted: int
    total: int
    model_name: str


class Embedder(Protocol):
    """Turns text into vectors. Implemented by the concrete backends."""

    @property
    def model_name(self) -> str: ...
    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class VectorIndex(Protocol):
    """A persistent nearest-neighbour store keyed by note/chunk.

    ``model_name`` reports the embedding model the persisted data is keyed by
    (empty string for a fresh store); :meth:`reset` clears everything and records
    a new model, so the service can rebuild when the configured model changes.

    A note with no text has no chunks, so it is recorded apart, as a mark that
    holds its id and version and nothing else: :meth:`mark_empty` sets one and
    :meth:`empty_versions` lists them. :meth:`note_versions` lists only notes
    with chunks. :meth:`delete_notes` and :meth:`reset` clear marks too, and
    :meth:`upsert` of a note's chunks clears its mark.
    """

    @property
    def model_name(self) -> str: ...

    def note_versions(self) -> dict[str, str]: ...
    def empty_versions(self) -> dict[str, str]: ...
    def mark_empty(self, note_id: str, version: str) -> None: ...
    def upsert(self, records: list[ChunkRecord]) -> None: ...
    def delete_notes(self, note_ids: list[str]) -> None: ...
    def query(self, embedding: list[float], k: int) -> list[QueryHit]: ...
    def reset(self, model_name: str) -> None: ...
