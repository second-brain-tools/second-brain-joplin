"""The journal: a copy of a note as it was, kept on the server's disk before any change.

Every tool that changes a note writes an entry here *before* it sends the change to
Joplin: the note's title, body and notebook as they were, and what the change will
make of them (lengths and checksums, not the new text). If the entry can't be
written and synced to disk, the change is refused. ``second-brain-joplin restore``
puts a kept copy back.

Joplin's own note history is not relied on: a change made through the Data API is
collected into it only every few minutes, and the text from before a change is
kept as an entry of its own only when the note was last changed long enough ago.

The same entries count the day's changes for ``SBJ_CHANGES_PER_DAY``, so a restart
doesn't reset the limit, and list what was changed for ``joplin_changes``.

**What is on disk.** One JSON file per change, in ``SBJ_UNDO_DIR`` (mode 0700 where
the system has modes; each file 0600). Note text is kept there for
``SBJ_UNDO_DAYS`` days (90 by default), within ``SBJ_UNDO_MAX_MB`` (500 by
default): the oldest files go first, never one from the last day. The journal
can't be switched off.
"""

import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DAY = 86_400


class JournalError(Exception):
    """The journal can't be written: the change it guards must not be sent.

    ``str()`` is a fixed sentence, safe for a client; the reason (a path, an OS
    error) is in ``detail``, for the log.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            "The server couldn't keep a copy of the note before changing it, so "
            "nothing was changed. Its operator can see why in the log."
        )
        self.detail = detail


@dataclass(frozen=True)
class Entry:
    """One change, as the journal holds it."""

    path: Path
    at: float  # seconds since the epoch
    tool: str
    caller: str
    note_id: str
    outcome: str  # "sent" until the answer is known; then "applied", "failed" or "unconfirmed"
    old_title: str
    old_body: str
    old_notebook: str
    old_chars: int
    new_chars: int
    old_sha256: str
    new_sha256: str

    @property
    def counts(self) -> bool:
        """Whether it counts toward the daily limit: anything not known to have failed."""
        return self.outcome != "failed"


class Journal:
    def __init__(
        self,
        directory: str | Path,
        *,
        days: int,
        max_mb: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.directory = Path(directory)
        self.days = days
        self.max_bytes = max_mb * 1024 * 1024
        self.clock = clock

    # -- writing ---------------------------------------------------------------

    def _ready(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.directory, 0o700)

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        """Write ``data`` to ``path`` through a temporary file, synced, then renamed."""
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            if os.name == "posix":
                dir_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def record(
        self,
        *,
        tool: str,
        caller: str,
        note: dict[str, Any],
        new_chars: int,
        old_sha256: str,
        new_sha256: str,
    ) -> Path:
        """Keep the note as it is now, before a change. Raises :class:`JournalError`."""
        at = self.clock()
        stamp = f"{int(at * 1_000_000):020d}"
        # The random part keeps two copies made in the same microsecond apart.
        path = self.directory / f"{stamp}-{note['id']}-{tool}-{uuid.uuid4().hex[:12]}.json"
        body = str(note.get("body", ""))
        data = {
            "at": at,
            "tool": tool,
            "caller": caller,
            "note_id": note["id"],
            "outcome": "sent",
            "old_title": str(note.get("title", "")),
            "old_body": body,
            "old_notebook": str(note.get("parent_id", "")),
            "old_chars": len(body),
            "new_chars": new_chars,
            "old_sha256": old_sha256,
            "new_sha256": new_sha256,
        }
        try:
            self._ready()
            self._write(path, data)
        except OSError as exc:
            raise JournalError(f"{type(exc).__name__}: {exc}") from exc
        return path

    def settle(self, path: Path, outcome: str) -> None:
        """Record how a change ended. A failure here loses only the outcome, never the copy."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["outcome"] = outcome
            self._write(path, data)
        except (OSError, ValueError):
            pass

    # -- reading ---------------------------------------------------------------

    def entries(self, *, since: float = 0.0) -> list[Entry]:
        """Every readable entry from ``since`` on, oldest first."""
        if not self.directory.is_dir():
            return []
        found = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                entry = Entry(path=path, **{k: data[k] for k in _FIELDS})
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if entry.at >= since:
                found.append(entry)
        return found

    def copies_of(self, note_id: str) -> list[Entry]:
        """The kept copies of one note, newest first."""
        return [e for e in reversed(self.entries()) if e.note_id == note_id]

    def day_start(self) -> float:
        """When today began, in the server's own time zone."""
        now = time.localtime(self.clock())
        return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))

    def changes_today(self) -> int:
        """How many changes count toward today's limit."""
        return sum(1 for e in self.entries(since=self.day_start()) if e.counts)

    def next_day(self) -> float:
        """When the daily limit resets: the next local midnight."""
        start = time.localtime(self.day_start() + DAY + 3600)  # past a DST change
        return time.mktime((start.tm_year, start.tm_mon, start.tm_mday, 0, 0, 0, 0, 0, -1))

    # -- keeping it small --------------------------------------------------------

    def prune(self) -> int:
        """Drop entries past the keep time, then the oldest beyond the size cap.

        Never one from the last day. Returns how many files went.
        """
        if not self.directory.is_dir():
            return 0
        now = self.clock()
        files = sorted(self.directory.glob("*.json"))
        removed = 0
        kept: list[tuple[Path, int, float]] = []
        for path in files:
            try:
                at = int(path.name.split("-", 1)[0]) / 1_000_000
                size = path.stat().st_size
            except (ValueError, OSError):
                continue
            if now - at > self.days * DAY:
                path.unlink(missing_ok=True)
                removed += 1
            else:
                kept.append((path, size, at))
        total = sum(size for _, size, _ in kept)
        for path, size, at in kept:  # oldest first
            if total <= self.max_bytes or now - at < DAY:
                break
            path.unlink(missing_ok=True)
            total -= size
            removed += 1
        return removed


_FIELDS = (
    "at",
    "tool",
    "caller",
    "note_id",
    "outcome",
    "old_title",
    "old_body",
    "old_notebook",
    "old_chars",
    "new_chars",
    "old_sha256",
    "new_sha256",
)
