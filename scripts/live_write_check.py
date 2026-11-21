"""Live write checks: the tools that write, run against a real Joplin (#81).

The unit tests use a fake Joplin. This checks the real one, and the Joplin
behaviours the write tools rely on. It WRITES to the Joplin the env file names, so
it refuses to start without ``--yes-write-to``. Everything happens in a temporary
notebook that it creates and permanently deletes at the end (also on failure); it
compares the vault's notebook and note counts before and after. It prints counts,
ids and ok/FAIL, never a note's title or text.

Run it from the checkout, in the project's environment (it drives the tools
through FastMCP's in-memory client):

    uv run python scripts/live_write_check.py --yes-write-to .env [--wait-history 12]

The env file needs JOPLIN_API_TOKEN (and JOPLIN_BASE_URL if the port isn't the
default). Other SBJ_* settings in it are ignored: the checks set their own. With
``--wait-history N`` it also waits up to N minutes for Joplin to collect the
change into its note history (Joplin does that every few minutes).

Not collected by pytest and not run in CI. Run it before a release that adds or
changes a write tool (CONTRIBUTING.md). Each run on a synced vault shows the
temporary notebook on the other devices for a few minutes.
"""

import argparse
import asyncio
import hashlib
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

RESULTS: list[bool] = []
FIELDS = "id,title,body,parent_id,is_todo,author,source_url,created_time,user_created_time"


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append(ok)
    print(f"{'ok  ' if ok else 'FAIL'}  {label}" + (f": {detail}" if detail else ""))


def info(label: str) -> None:
    print(f"info  {label}")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Raw:
    """Joplin's Data API directly, for setting up, looking, and cleaning up."""

    def __init__(self, base: str, token: str) -> None:
        self.http = httpx.AsyncClient(
            base_url=base, params={"token": token}, trust_env=False, timeout=30
        )

    async def all(self, path: str, **params: Any) -> list[dict[str, Any]]:
        items, page = [], 1
        while True:
            response = await self.http.get(path, params={**params, "page": page, "limit": 100})
            response.raise_for_status()
            data = response.json()
            items += data.get("items", [])
            if not data.get("has_more"):
                return items
            page += 1

    async def counts(self) -> tuple[int, int, int]:
        folders = await self.all("/folders", fields="id")
        notes = await self.all("/notes", fields="id")
        everything = await self.all("/notes", fields="id", include_deleted=1)
        return len(folders), len(notes), len(everything)

    async def note(self, note_id: str, fields: str = FIELDS) -> dict[str, Any]:
        response = await self.http.get(f"/notes/{note_id}", params={"fields": fields})
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result


async def run(env_file: Path, wait_history: int) -> None:
    values = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    token = values.get("JOPLIN_API_TOKEN", "")
    base = values.get("JOPLIN_BASE_URL", "http://localhost:41184").rstrip("/")
    if not token:
        sys.exit(f"{env_file} sets no JOPLIN_API_TOKEN")
    raw = Raw(base, token)
    before = await raw.counts()
    info(f"vault before: {before[0]} notebooks, {before[1]} notes, {before[2]} with the trash")

    response = await raw.http.post(
        "/folders", json={"title": f"sbj-live-check-{uuid.uuid4().hex[:8]}"}
    )
    response.raise_for_status()
    folder = response.json()["id"]
    info(f"temporary notebook {folder}")
    undo = tempfile.mkdtemp(prefix="sbj-live-undo-")
    made: set[str] = set()

    for name in list(os.environ):
        if name.startswith("SBJ_"):
            del os.environ[name]
    os.environ.update(
        {
            "JOPLIN_API_TOKEN": token,
            "JOPLIN_BASE_URL": base,
            "SBJ_WRITE_NOTEBOOKS": folder,
            "SBJ_EDIT_NOTEBOOKS": folder,
            "SBJ_REPLACE": "yes",
            "SBJ_CREATE_NOTES_MAX": "3",
            "SBJ_UNDO_DIR": undo,
            "SBJ_SEMANTIC_AUTO_SYNC": "false",
        }
    )
    from fastmcp import Client

    from second_brain_joplin.cli import run_restore
    from second_brain_joplin.config import Settings
    from second_brain_joplin.server import create_server

    async def call(client: Client[Any], tool: str, args: dict[str, Any]) -> Any:
        return (await client.call_tool(tool, args)).structured_content

    async def refused(client: Client[Any], tool: str, args: dict[str, Any]) -> str:
        result = await client.call_tool(tool, args, raise_on_error=False)
        return "" if not result.is_error else " ".join(str(c) for c in result.content)

    try:
        async with Client(create_server()) as client:
            body = "Line one.\n\n# Log\n\n- first\n"
            created = await call(
                client,
                "joplin_create_note",
                {"title": "live check", "body": body, "notebook": folder},
            )
            note_id = created["id"]
            made.add(note_id)
            stored = await raw.note(note_id)
            check(
                stored["body"] == body and created["body_sha256"] == sha(body),
                "create: the note holds what was sent",
                note_id,
            )

            read = await call(client, "joplin_read", {"note_id": note_id})
            check(read["body_sha256"] == sha(body), "read: body_sha256 is the stored text's")

            first = await raw.note(note_id, FIELDS + ",updated_time,user_updated_time")
            edited = await call(
                client,
                "joplin_edit_note",
                {
                    "note_id": note_id,
                    "title": "live check",
                    "expected_sha256": read["body_sha256"],
                    "edits": [{"old": "Line one.", "new": "Line one, changed."}],
                    "append": {"text": "- second", "section": "Log"},
                },
            )
            after = await raw.note(note_id, FIELDS + ",updated_time,user_updated_time")
            expected = "Line one, changed.\n\n# Log\n\n- first\n- second\n"
            check(
                after["body"] == expected and edited["new_sha256"] == sha(expected),
                "edit: the text is exactly as intended",
            )
            unchanged = all(after[f] == first[f] for f in FIELDS.split(",") if f != "body")
            check(
                unchanged,
                "edit: PUT left every other property alone "
                "(title, notebook, author, source, times created)",
            )
            moved = after["updated_time"] > first["updated_time"]
            user_moved = after["user_updated_time"] > first["user_updated_time"]
            info(
                f"edit: updated_time {'moved' if moved else 'did not move'}, "
                f"user_updated_time {'moved' if user_moved else 'did not move'}"
            )
            check(
                len(list(Path(undo).glob("*.json"))) == 1,
                "edit: one copy kept in the journal before the change",
            )

            again = await call(
                client,
                "joplin_edit_note",
                {
                    "note_id": note_id,
                    "title": "live check",
                    "expected_sha256": read["body_sha256"],
                    "edits": [{"old": "Line one.", "new": "Line one, changed."}],
                    "append": {"text": "- second", "section": "Log"},
                },
            )
            check(again["already_applied"] is True, "edit: the same call again changes nothing")

            put = await raw.http.put(f"/notes/{note_id}", json={"author": "sbj-live-check"})
            took = (await raw.note(note_id))["author"] == "sbj-live-check"
            info(
                f"PUT with an extra property (author): Joplin "
                f"{'applied it' if took else 'ignored it'} (HTTP {put.status_code})"
            )
            await raw.http.put(f"/notes/{note_id}", json={"author": first.get("author", "")})

            replaced_body = "Rebuilt whole.\n"
            current = (await call(client, "joplin_read", {"note_id": note_id}))["body_sha256"]
            replaced = await call(
                client,
                "joplin_replace_note",
                {
                    "note_id": note_id,
                    "title": "live check",
                    "body": replaced_body,
                    "expected_sha256": current,
                },
            )
            check(
                (await raw.note(note_id))["body"] == replaced_body
                and not replaced["already_replaced"],
                "replace: the whole text replaced",
            )

            settings = Settings.from_env()
            await run_restore(settings, note_id, 1, False)
            check(
                (await raw.note(note_id))["body"] == expected,
                "restore: the copy from before the replace is back",
            )

            batch = await call(
                client,
                "joplin_create_notes",
                {
                    "notes": [
                        {"title": f"batch {i}", "body": f"batch body {i}", "notebook": folder}
                        for i in range(2)
                    ]
                },
            )
            made.update(n["id"] for n in batch["notes"] if n["id"])
            check(
                [n["outcome"] for n in batch["notes"]] == ["created", "created"],
                "create_notes: two notes in one call",
            )

            listing = await call(client, "joplin_list_notes", {"notebook": folder})
            check(
                listing["total"] == 3,
                "list_notes: the notebook's three notes",
                str(listing["total"]),
            )
            many = await call(client, "joplin_read_notes", {"note_ids": sorted(made) + ["0" * 32]})
            check(
                len(many["notes"]) == 3 and many["not_found"] == ["0" * 32],
                "read_notes: three found, one not",
            )

            marker = f"zq{uuid.uuid4().hex[:10]}"
            searched_id = (
                await call(
                    client,
                    "joplin_create_note",
                    {
                        "title": "search",
                        "body": f"# Find\n\nword {marker} here\n",
                        "notebook": folder,
                    },
                )
            )["id"]
            made.add(searched_id)
            # Joplin indexes a new note for search a few seconds after saving it.
            hit = None
            for _ in range(30):
                hits = await call(client, "joplin_search", {"query": marker})
                listed = hits["result"] if isinstance(hits, dict) else []
                hit = next((h for h in listed if h["id"] == searched_id), None)
                if hit is not None:
                    break
                await asyncio.sleep(2)
            check(
                hit is not None and hit["section"] == "1" and marker in (hit["snippet"] or ""),
                "search: snippet and section of the match",
            )

            conflict = await raw.http.post(
                "/notes",
                json={"title": "conflict", "body": "x", "parent_id": folder, "is_conflict": 1},
            )
            conflict_id = conflict.json().get("id")
            if conflict_id:
                made.add(conflict_id)
                kept = (await raw.note(conflict_id, "id,is_conflict")).get("is_conflict")
                info(f"POST with is_conflict=1: Joplin {'kept it' if kept else 'dropped it'}")
                if kept:
                    message = await refused(
                        client,
                        "joplin_edit_note",
                        {
                            "note_id": conflict_id,
                            "title": "conflict",
                            "expected_sha256": sha("x"),
                            "append": {"text": "y"},
                        },
                    )
                    check("conflict copy" in message, "edit: a conflict copy is refused")

            trash_id = (
                await call(
                    client,
                    "joplin_create_note",
                    {"title": "trash", "body": "t", "notebook": folder},
                )
            )["id"]
            made.add(trash_id)
            await raw.http.delete(f"/notes/{trash_id}")
            deleted = (await raw.note(trash_id, "id,deleted_time")).get("deleted_time", 0)
            check(bool(deleted), "DELETE without permanent=1 moves a note to the trash")
            message = await refused(
                client,
                "joplin_edit_note",
                {
                    "note_id": trash_id,
                    "title": "trash",
                    "expected_sha256": sha("t"),
                    "append": {"text": "y"},
                },
            )
            check("trash" in message, "edit: a note in the trash is refused")

            deadline = time.monotonic() + wait_history * 60
            while True:
                revisions = await raw.all(
                    "/revisions", fields="id,item_id", order_by="created_time", order_dir="DESC"
                )
                found = sum(1 for r in revisions if r.get("item_id") == note_id)
                if found or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(30)
            info(
                f"note history: {found} entries for the changed note"
                + (
                    ""
                    if wait_history
                    else " right after the changes "
                    "(use --wait-history to wait for Joplin to collect them)"
                )
            )
            info(
                "not checked here: a change to a copy behind another device, and an "
                "editor open on the note (both need a second device or a person)"
            )
    finally:
        for nid in made:
            await raw.http.delete(f"/notes/{nid}", params={"permanent": 1})
        await raw.http.delete(f"/folders/{folder}", params={"permanent": 1})
        after_counts = await raw.counts()
        check(
            after_counts == before,
            "the vault's counts are as they were",
            f"{after_counts[0]} notebooks, {after_counts[1]} notes",
        )
        await raw.http.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--yes-write-to",
        type=Path,
        required=True,
        metavar="ENV_FILE",
        help="the env file of the Joplin to write to",
    )
    parser.add_argument("--wait-history", type=int, default=0, metavar="MINUTES")
    args = parser.parse_args()
    if not args.yes_write_to.is_file():
        sys.exit(f"{args.yes_write_to} is not a file")
    asyncio.run(run(args.yes_write_to, args.wait_history))
    failed = RESULTS.count(False)
    print(f"{len(RESULTS) - failed} ok, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
