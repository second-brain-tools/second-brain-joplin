"""The vector stores, tested against one set of checks.

Each check runs on the in-memory fake the service tests use, on the sqlite-vec
store (the default; a dev dependency, so CI runs it) and on the Chroma store
(skipped unless ``chromadb`` is installed). The stores take ready-made vectors,
so no embedding model is downloaded.
"""

import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import FakeVectorIndex

from second_brain_joplin.semantic.types import ChunkRecord, VectorIndex

MODEL = "test-model"

Opener = Callable[[Path], VectorIndex]


CAN_LOAD_EXTENSIONS = hasattr(sqlite3.Connection, "enable_load_extension")


def _sqlite(index_dir: Path) -> VectorIndex:
    pytest.importorskip("sqlite_vec")
    if not CAN_LOAD_EXTENSIONS:
        pytest.skip("this Python can't load SQLite extensions (GitHub's macOS runner)")
    from second_brain_joplin.semantic.backends import _SqliteVecIndex

    return _SqliteVecIndex(str(index_dir), MODEL)


def _chroma(index_dir: Path) -> VectorIndex:
    pytest.importorskip("chromadb")
    from second_brain_joplin.semantic.backends import _ChromaVectorIndex

    return _ChromaVectorIndex(str(index_dir), MODEL)


def _fake(_index_dir: Path) -> VectorIndex:
    return FakeVectorIndex()


STORES = [
    pytest.param(_fake, id="fake"),
    pytest.param(_sqlite, id="sqlite-vec"),
    pytest.param(_chroma, id="chroma"),
]
PERSISTENT = [STORES[1], STORES[2]]


def _record(note_id: str, index: int, vector: list[float], version: str = "100") -> ChunkRecord:
    return ChunkRecord(
        chunk_id=f"{note_id}::{index}",
        note_id=note_id,
        title=f"title of {note_id}",
        updated_time=version,
        chunk_index=index,
        text=f"text {index} of {note_id}",
        embedding=vector,
    )


@pytest.fixture(params=STORES)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> VectorIndex:
    opener: Opener = request.param
    index = opener(tmp_path)
    index.reset(MODEL)
    return index


def test_a_fresh_store_is_empty(store: VectorIndex) -> None:
    assert store.model_name == MODEL
    assert store.note_versions() == {}
    assert store.empty_versions() == {}
    assert store.query([1.0, 0.0, 0.0, 0.0], 5) == []


def test_upsert_then_query_returns_the_nearest_first(store: VectorIndex) -> None:
    store.upsert(
        [
            _record("a", 0, [1.0, 0.0, 0.0, 0.0]),
            _record("a", 1, [0.0, 1.0, 0.0, 0.0]),
            _record("b", 0, [0.0, 0.0, 1.0, 0.0], version="200"),
        ]
    )
    assert store.note_versions() == {"a": "100", "b": "200"}
    hits = store.query([0.0, 0.0, 1.0, 0.0], 2)
    assert [(hit.note_id, hit.chunk_index) for hit in hits][0] == ("b", 0)
    assert hits[0].title == "title of b"
    assert hits[0].text == "text 0 of b"
    assert hits[0].score >= hits[1].score


def test_delete_notes_removes_every_chunk_of_a_note(store: VectorIndex) -> None:
    store.upsert([_record("a", 0, [1.0, 0.0, 0.0, 0.0]), _record("a", 1, [0.0, 1.0, 0.0, 0.0])])
    store.upsert([_record("b", 0, [0.0, 0.0, 1.0, 0.0])])
    store.delete_notes(["a"])
    assert store.note_versions() == {"b": "100"}
    assert {hit.note_id for hit in store.query([1.0, 0.0, 0.0, 0.0], 5)} == {"b"}
    store.delete_notes([])  # nothing to do, no error


def test_a_mark_holds_a_version_apart_from_the_chunks(store: VectorIndex) -> None:
    store.mark_empty("e", "100")
    assert store.empty_versions() == {"e": "100"}
    assert store.note_versions() == {}
    store.mark_empty("e", "200")  # a newer version replaces the old
    assert store.empty_versions() == {"e": "200"}


def test_a_mark_is_never_a_search_result(store: VectorIndex) -> None:
    store.mark_empty("e", "100")
    store.upsert([_record("a", 0, [1.0, 0.0, 0.0, 0.0])])
    assert {hit.note_id for hit in store.query([1.0, 0.0, 0.0, 0.0], 5)} == {"a"}


def test_upsert_of_a_note_clears_its_mark(store: VectorIndex) -> None:
    store.mark_empty("e", "100")
    store.upsert([_record("e", 0, [1.0, 0.0, 0.0, 0.0], version="200")])
    assert store.empty_versions() == {}
    assert store.note_versions() == {"e": "200"}


def test_delete_notes_clears_marks_too(store: VectorIndex) -> None:
    store.mark_empty("e", "100")
    store.mark_empty("f", "100")
    store.delete_notes(["e"])
    assert store.empty_versions() == {"f": "100"}


def test_reset_clears_chunks_and_marks_and_records_the_model(store: VectorIndex) -> None:
    store.upsert([_record("a", 0, [1.0, 0.0, 0.0, 0.0])])
    store.mark_empty("e", "100")
    store.reset("another-model")
    assert store.model_name == "another-model"
    assert store.note_versions() == {}
    assert store.empty_versions() == {}
    assert store.query([1.0, 0.0, 0.0, 0.0], 5) == []


@pytest.mark.parametrize("opener", PERSISTENT)
def test_chunks_and_marks_survive_reopening_the_index(opener: Opener, tmp_path: Path) -> None:
    first = opener(tmp_path)
    first.reset(MODEL)
    first.upsert([_record("a", 0, [1.0, 0.0, 0.0, 0.0])])
    first.mark_empty("e", "100")

    again = opener(tmp_path)
    assert again.model_name == MODEL
    assert again.note_versions() == {"a": "100"}
    assert again.empty_versions() == {"e": "100"}


def test_an_index_made_before_the_marks_existed_still_opens(tmp_path: Path) -> None:
    """mercury's index file predates the mark table: it must work without a rebuild."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    if not CAN_LOAD_EXTENSIONS:
        pytest.skip("this Python can't load SQLite extensions (GitHub's macOS runner)")
    conn = sqlite3.connect(str(tmp_path / "index.db"))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta(key, value) VALUES('model', ?)", (MODEL,))
    conn.execute(
        "CREATE TABLE chunks(rowid INTEGER PRIMARY KEY, chunk_id TEXT UNIQUE, note_id TEXT, "
        "updated_time TEXT, title TEXT, chunk_index INTEGER, text TEXT)"
    )
    conn.execute("CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[4])")
    conn.execute(
        "INSERT INTO chunks(chunk_id, note_id, updated_time, title, chunk_index, text) "
        "VALUES('a::0', 'a', '100', 'title of a', 0, 'text 0 of a')"
    )
    conn.execute(
        "INSERT INTO vec_chunks(rowid, embedding) VALUES(1, ?)",
        (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
    )
    conn.commit()
    conn.close()

    index = _sqlite(tmp_path)

    assert index.model_name == MODEL
    assert index.note_versions() == {"a": "100"}
    assert index.empty_versions() == {}
    index.mark_empty("e", "100")
    assert index.empty_versions() == {"e": "100"}
    assert [hit.note_id for hit in index.query([1.0, 0.0, 0.0, 0.0], 1)] == ["a"]


def test_a_python_that_cannot_load_extensions_gets_a_clear_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("sqlite_vec")
    from second_brain_joplin.errors import SemanticUnavailableError
    from second_brain_joplin.semantic import backends
    from second_brain_joplin.semantic.backends import _SqliteVecIndex

    class _NoExtensions:  # a Connection type built without extension loading
        pass

    monkeypatch.setattr(backends, "sqlite3", SimpleNamespace(Connection=_NoExtensions))
    with pytest.raises(SemanticUnavailableError, match="can load SQLite extensions"):
        _SqliteVecIndex(str(tmp_path), MODEL)
