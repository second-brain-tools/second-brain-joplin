"""In-memory fakes for tests: the semantic Protocols, and Joplin's Data API.

Deterministic, dependency-free stand-ins for the real embedder and vector store
so the orchestration can be exercised without the optional ML/vector libs, and
a small Joplin (notebooks, notes, search, paging) served through respx.
"""

import hashlib
import json
import math
import re
from collections.abc import Callable
from typing import Any

import httpx
import respx

from second_brain_joplin.semantic.types import ChunkRecord, QueryHit

_DIM = 16


def _stable_hash(token: str) -> int:
    return int.from_bytes(hashlib.md5(token.encode()).digest()[:4], "big")


def _embed(text: str) -> list[float]:
    """Bag-of-tokens hashed into a fixed-dim, L2-normalized vector."""
    vec = [0.0] * _DIM
    for token in text.lower().split():
        vec[_stable_hash(token) % _DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class FakeEmbedder:
    """Deterministic embedder over token hashes."""

    def __init__(self, model_name: str = "fake-model") -> None:
        self.model_name = model_name
        self.dim = _DIM
        self.doc_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.doc_calls += 1
        return [_embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return _embed(text)


class FakeVectorIndex:
    """In-memory vector store keyed by chunk id."""

    def __init__(self) -> None:
        self.model_name = ""
        self._records: dict[str, ChunkRecord] = {}
        self._empty: dict[str, str] = {}

    def note_versions(self) -> dict[str, str]:
        return {r.note_id: r.updated_time for r in self._records.values()}

    def empty_versions(self) -> dict[str, str]:
        return dict(self._empty)

    def mark_empty(self, note_id: str, version: str) -> None:
        self._empty[note_id] = version

    def stored_text(self) -> str:
        """Everything a reader of the index could find in it: titles and chunk text."""
        return "\n".join(f"{r.title}\n{r.text}" for r in self._records.values())

    def upsert(self, records: list[ChunkRecord]) -> None:
        for record in records:
            self._records[record.chunk_id] = record
            self._empty.pop(record.note_id, None)

    def delete_notes(self, note_ids: list[str]) -> None:
        drop = set(note_ids)
        self._records = {cid: r for cid, r in self._records.items() if r.note_id not in drop}
        self._empty = {nid: v for nid, v in self._empty.items() if nid not in drop}

    def query(self, embedding: list[float], k: int) -> list[QueryHit]:
        scored = [
            QueryHit(
                note_id=r.note_id,
                title=r.title,
                text=r.text,
                chunk_index=r.chunk_index,
                score=_cosine(embedding, r.embedding),
            )
            for r in self._records.values()
        ]
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[:k]

    def reset(self, model_name: str) -> None:
        self.model_name = model_name
        self._records = {}
        self._empty = {}


class SharedVectorIndex(FakeVectorIndex):
    """An index another process writes to: ``intruders`` appear just before each query.

    The real index is a file any process on the machine can sync into with its own
    settings, so a search can't assume it holds only what this process put there.
    """

    def __init__(self) -> None:
        super().__init__()
        self.intruders: list[ChunkRecord] = []

    def query(self, embedding: list[float], k: int) -> list[QueryHit]:
        self.upsert(self.intruders)
        return super().query(embedding, k)


class FakeJoplin:
    """An in-memory Joplin Data API behind respx: notebooks, notes, search, paging.

    Mirrors what the real API was seen to do (Joplin 3.7): ``/folders`` is a flat
    paginated list carrying ``parent_id``, listings honour ``fields``, ``page`` and
    ``order_by=updated_time``, and a missing note answers 404. ``POST /notes``
    creates a note under the id the request supplies, as Joplin's API reference
    describes, and answers with the note. Ids are 32 hexadecimal characters,
    handed out in order. Call :meth:`install` inside a ``respx.mock`` block.
    """

    def __init__(self, base_url: str = "http://localhost:41184", page_size: int = 100) -> None:
        self.base_url = base_url
        self.page_size = page_size
        self.folders: dict[str, dict[str, Any]] = {}
        self.notes: dict[str, dict[str, Any]] = {}
        self.requests: list[str] = []  # the path of every request, in order
        self.queries: list[str] = []  # the query of every /search request
        self.pings = 0  # requests to /ping, kept apart from `requests`
        self.down = False
        # Accept connections and answer nothing: every request times out.
        self.hung = False
        # /ping alone gets no answer (whatever the other routes do).
        self.ping_silent = False
        # Called with a note's id just before that note is served on its own, so
        # a test can change the vault between a listing and the read that follows.
        self.before_note: Callable[[str], None] | None = None
        self.note_status: int | None = None  # answer single-note reads with this
        self.posts: list[dict[str, Any]] = []  # the JSON body of every POST /notes
        self.post_status: int | None = None  # refuse POST /notes with this, creating nothing
        # Create the note, then lose the answer: the client sees a timeout.
        self.post_loses_response = False
        self.post_ignores_id = False  # hand out an id of Joplin's own instead
        self.down_after_post = False  # go down right after the next POST
        self.post_refused = False  # the connection for a POST is refused: nothing is sent
        # The client times out while Joplin is still saving: the note isn't there
        # until land_late() is called.
        self.post_saves_late = False
        self.post_errors_after_saving = False  # the note is saved, the answer is HTTP 500
        self._late: list[dict[str, Any]] = []
        self._next_id = 1

    def _new_id(self) -> str:
        new_id = f"{self._next_id:032x}"
        self._next_id += 1
        return new_id

    def notebook(self, title: str, parent: str = "") -> str:
        """Add a notebook (under ``parent``, an id) and return its id."""
        notebook_id = self._new_id()
        self.folders[notebook_id] = {"id": notebook_id, "title": title, "parent_id": parent}
        return notebook_id

    def note(self, title: str, body: str, notebook: str, updated_time: int = 100) -> str:
        """Add a note to the notebook with id ``notebook`` and return its id."""
        note_id = self._new_id()
        self.notes[note_id] = {
            "id": note_id,
            "title": title,
            "body": body,
            "parent_id": notebook,
            "created_time": 0,
            "updated_time": updated_time,
        }
        return note_id

    def move(self, note_id: str, notebook: str) -> None:
        """Move a note to another notebook, leaving its ``updated_time`` alone."""
        self.notes[note_id]["parent_id"] = notebook

    def land_late(self) -> None:
        """Finish saving the notes whose POST timed out under ``post_saves_late``."""
        for note in self._late:
            self.notes[note["id"]] = note
        self._late.clear()

    def count(self, path: str) -> int:
        """How many requests were made to exactly ``path``."""
        return self.requests.count(path)

    def install(self) -> None:
        base = re.escape(self.base_url)
        respx.get(f"{self.base_url}/folders").mock(side_effect=self._folders)
        respx.get(f"{self.base_url}/notes").mock(side_effect=self._notes)
        respx.get(f"{self.base_url}/search").mock(side_effect=self._search)
        respx.post(f"{self.base_url}/notes").mock(side_effect=self._create)
        respx.get(url__regex=rf"{base}/notes/[^/?]+").mock(side_effect=self._note)
        respx.get(f"{self.base_url}/ping").mock(side_effect=self._ping)

    def _ping(self, _request: httpx.Request) -> httpx.Response:
        self.pings += 1
        if self.down:
            raise httpx.ConnectError("Joplin is down")
        if self.hung or self.ping_silent:
            raise httpx.ReadTimeout("no answer")
        return httpx.Response(200, text="JoplinClipperServer")

    def _page(self, request: httpx.Request, rows: list[dict[str, Any]]) -> httpx.Response:
        self.requests.append(request.url.path)
        if self.down:
            raise httpx.ConnectError("Joplin is down")
        if self.hung:
            raise httpx.ReadTimeout("no answer")
        fields = request.url.params.get("fields", "id,parent_id,title").split(",")
        page = int(request.url.params.get("page", "1"))
        start = (page - 1) * self.page_size
        chunk = rows[start : start + self.page_size]
        return httpx.Response(
            200,
            json={
                "items": [{name: row[name] for name in fields} for row in chunk],
                "has_more": start + self.page_size < len(rows),
            },
        )

    def _folders(self, request: httpx.Request) -> httpx.Response:
        return self._page(request, list(self.folders.values()))

    def _notes(self, request: httpx.Request) -> httpx.Response:
        rows = list(self.notes.values())
        if request.url.params.get("order_by") == "updated_time":
            descending = request.url.params.get("order_dir") == "DESC"
            rows.sort(key=lambda note: note["updated_time"], reverse=descending)
        return self._page(request, rows)

    def _search(self, request: httpx.Request) -> httpx.Response:
        """Joplin's search, as far as the tests need it.

        ``notebook:`` and ``-notebook:`` match notebook titles with SQL ``LIKE``
        (``*``, ``%`` and ``_`` wildcards, either case) and take in every
        sub-notebook, across the whole vault, as Joplin 3.7 does. Every other
        word must appear in the title or the body.
        """
        query = request.url.params.get("query", "")
        self.queries.append(query)
        include: list[str] = []
        exclude: list[str] = []

        def term(match: re.Match[str]) -> str:
            value = match.group(2).strip('"')
            (exclude if match.group(1) else include).append(value)
            return " "

        words = (
            re.sub(r'(?<!\S)(-?)notebook:("[^"]*"|\S+)', term, query, flags=re.I).lower().split()
        )

        def matched(patterns: list[str]) -> set[str]:
            regexes = [
                re.compile(
                    "".join(".*" if c in "%*" else "." if c == "_" else re.escape(c) for c in p),
                    re.I,
                )
                for p in patterns
            ]
            roots = {
                f
                for f, row in self.folders.items()
                if any(r.fullmatch(row["title"]) for r in regexes)
            }
            found = set(roots)
            grew = True
            while grew:
                grew = False
                for f, row in self.folders.items():
                    if f not in found and row["parent_id"] in found:
                        found.add(f)
                        grew = True
            return found

        inside = matched(include) if include else None
        outside = matched(exclude)
        rows = [
            note
            for note in self.notes.values()
            if (inside is None or note["parent_id"] in inside)
            and note["parent_id"] not in outside
            and all(w in note["title"].lower() or w in note["body"].lower() for w in words)
        ]
        return self._page(request, rows)

    def _note(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if self.down:
            raise httpx.ConnectError("Joplin is down")
        if self.hung:
            raise httpx.ReadTimeout("no answer")
        note_id = request.url.path.rsplit("/", 1)[-1]
        if self.note_status is not None:
            return httpx.Response(self.note_status)
        if self.before_note is not None:
            self.before_note(note_id)
        note = self.notes.get(note_id)
        if note is None:
            return httpx.Response(404)
        return httpx.Response(200, json=note)

    def _create(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"POST {request.url.path}")
        if self.down or self.post_refused:
            raise httpx.ConnectError("connection refused")
        if self.hung:
            raise httpx.ReadTimeout("no answer")
        payload = json.loads(request.content)
        self.posts.append(payload)
        if self.post_status is not None:
            return httpx.Response(self.post_status)
        note_id = self._new_id() if self.post_ignores_id else payload["id"]
        note = {
            "id": note_id,
            "title": payload.get("title", ""),
            "body": payload.get("body", ""),
            "parent_id": payload.get("parent_id", ""),
            "created_time": 1_700_000_000_000,
            "updated_time": 1_700_000_000_000,
        }
        if self.post_saves_late:
            self._late.append(note)
            raise httpx.ReadTimeout("still saving")
        self.notes[note_id] = note
        if self.down_after_post:
            self.down = True
        if self.post_loses_response:
            raise httpx.ReadTimeout("the answer never arrived")
        if self.post_errors_after_saving:
            return httpx.Response(500)
        return httpx.Response(200, json=note)
