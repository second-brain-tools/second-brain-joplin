"""Command-line entry point for the Joplin MCP server."""

import argparse
import asyncio
import logging
import os
import time
from pathlib import Path

from fastmcp.exceptions import ToolError
from second_brain_core.audit import configure_logging
from second_brain_core.auth import AuthSettings, build_auth
from second_brain_core.env import load_configuration
from second_brain_core.errors import ServeConfigError
from second_brain_core.remote import (
    TRANSPORTS,
    choose_transport,
    http_options,
    refuse_http_flags,
    run_http,
    run_stdio,
    validate,
)
from second_brain_core.semantic.types import SyncStats

from . import __version__
from .changes import references, refuse_kind, sha256
from .config import (
    CONNECT_TIMEOUT_VAR,
    CREATE_NOTES_CEILING,
    CREATE_NOTES_MAX_VAR,
    TIMEOUT_VAR,
    Settings,
)
from .context import AppContext
from .errors import JoplinError, NotebookFilterError, SemanticUnavailableError
from .identity import SERVER
from .joplin_client import JoplinClient, JsonDict
from .journal import Journal, JournalError
from .remote import sync_interval_minutes
from .scope import (
    NotebookScope,
    WriteTargets,
    check_at_startup,
    edit_targets_of,
    scope_of,
    write_targets_of,
)
from .server import create_server

logger = logging.getLogger("second_brain_joplin.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="second-brain-joplin",
        description="MCP server for Joplin knowledge bases.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    serve = subparsers.add_parser(
        "serve",
        help="Start the MCP server (stdio by default, or streamable HTTP).",
        description=(
            "Start the MCP server. stdio is for a client on this machine (Claude Code); "
            "http is for a remote client such as a Claude custom connector "
            "(docs/remote-access.md). Every option falls back to the SBJ_* variable named."
        ),
    )
    serve.add_argument(
        "--transport", choices=TRANSPORTS, help="stdio (default) or http  [SBJ_TRANSPORT]"
    )
    serve.add_argument("--host", help="HTTP: address to bind (default 127.0.0.1)  [SBJ_HOST]")
    serve.add_argument(
        "--port", type=int, help="HTTP: port to listen on (default 8775)  [SBJ_PORT]"
    )
    serve.add_argument("--path", help="HTTP: MCP endpoint path (default /mcp)  [SBJ_HTTP_PATH]")
    serve.add_argument(
        "--public-url",
        help="HTTP: the server's public origin, e.g. https://joplin.example.com  [SBJ_PUBLIC_URL]",
    )
    serve.add_argument(
        "--no-auth",
        action="store_true",
        help="HTTP: run without sign-in, on a loopback address only (local testing)",
    )
    serve.add_argument(
        "--env-file",
        type=Path,
        help="load settings and secrets from this file first (variables already set win)",
    )
    reindex = subparsers.add_parser(
        "reindex",
        help="Build or update the semantic index, with no time limit.",
        description=(
            "Sync the semantic index against Joplin from the command line. Use it for a "
            "first build or a full rebuild, which can take longer than a remote tool "
            "call may run. Needs the [semantic] extra and Joplin Desktop running."
        ),
    )
    reindex.add_argument("--full", action="store_true", help="re-embed every note from scratch")
    reindex.add_argument(
        "--env-file", type=Path, help="load settings from this file first (set variables win)"
    )
    notebooks = subparsers.add_parser(
        "notebooks",
        help="List the notebooks with their ids, and which are visible or writable.",
        description=(
            "Print every notebook with its id and note count, marked hidden, visible or "
            "writable under SBJ_NOTEBOOKS, SBJ_EXCLUDE_NOTEBOOKS and SBJ_WRITE_NOTEBOOKS. "
            "Use it to find the ids for those variables, and to check them before a "
            "client sees through them. With note creation on, it also says when "
            "SBJ_ASK_EVERY_TIME is off. "
            "Needs Joplin Desktop running."
        ),
    )
    notebooks.add_argument(
        "--env-file", type=Path, help="load settings from this file first (set variables win)"
    )
    restore = subparsers.add_parser(
        "restore",
        help="List a note's kept copies, or put one back.",
        description=(
            "Before every change, the server keeps a copy of the note as it was "
            "(SBJ_UNDO_DIR). With a note's id alone this lists those copies, newest "
            "first, without their text; --pick N puts copy N back (its text, and its "
            "title if that changed). The restore is kept as a change of its own, so it "
            "can be undone the same way. Needs Joplin Desktop running."
        ),
    )
    restore.add_argument("note_id", help="the note's 32-character id")
    restore.add_argument("--pick", type=int, metavar="N", help="put copy N of the list back")
    restore.add_argument(
        "--force",
        action="store_true",
        help="restore even if the copy doesn't refer to an attachment the note now has",
    )
    restore.add_argument(
        "--env-file", type=Path, help="load settings from this file first (set variables win)"
    )
    return parser


async def run_restore(settings: Settings, note_id: str, pick: int | None, force: bool) -> str:
    """List a note's kept copies, or put one back. Returns what to print."""
    journal = Journal(settings.undo_dir, days=settings.undo_days, max_mb=settings.undo_max_mb)
    copies = journal.copies_of(note_id)
    if not copies:
        if pick is not None:
            raise ServeConfigError(f"no kept copies of {note_id} in {settings.undo_dir}")
        return f"No kept copies of {note_id} in {settings.undo_dir}."
    if pick is None:
        lines = [f"Kept copies of {note_id}, newest first (in {settings.undo_dir}):"]
        for number, entry in enumerate(copies, start=1):
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.at))
            lines.append(
                f"  {number:>3}  {when}  before {entry.tool}  {entry.old_chars:>7} characters"
                f"  ({entry.outcome})"
            )
        lines.append("Put one back with --pick N.")
        return "\n".join(lines)
    if not 1 <= pick <= len(copies):
        raise ServeConfigError(f"--pick takes 1 to {len(copies)}")
    chosen = copies[pick - 1]
    client = JoplinClient(settings)
    try:
        note = await client.get_note(note_id)
        try:
            refuse_kind(note)
        except ToolError as exc:
            raise ServeConfigError(str(exc)) from exc
        attached = [r["id"] for r in await client.get_note_resources(note_id)]
        dropped = [r for r in attached if r.lower() not in references(chosen.old_body)]
        if dropped and not force:
            raise ServeConfigError(
                f"copy {pick} doesn't refer to {len(dropped)} attachment(s) the note has now, "
                "and Joplin deletes an attachment no note refers to; add --force to restore anyway"
            )
        body = str(note.get("body", ""))
        kept = journal.record(
            tool="restore",
            caller="command line",
            note=note,
            new_chars=len(chosen.old_body),
            old_sha256=sha256(body),
            new_sha256=sha256(chosen.old_body),
        )
        try:
            if chosen.old_body != body:
                await client.update_note(note_id, body=chosen.old_body)
            if chosen.old_title and chosen.old_title != note.get("title", ""):
                await client.update_note(note_id, title=chosen.old_title)
        except JoplinError:
            journal.settle(kept, "unconfirmed")
            raise
        journal.settle(kept, "applied")
    finally:
        await client.aclose()
    return (
        f"Restored copy {pick} of {note_id} ({chosen.old_chars} characters). "
        "What it replaced is kept as the newest copy."
    )


def restore(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    refuse_unreadable_settings(settings)
    try:
        print(asyncio.run(run_restore(settings, args.note_id.lower(), args.pick, args.force)))
    except JournalError as exc:
        raise ServeConfigError(f"the journal couldn't be written: {exc.detail}") from exc


async def run_reindex(full: bool) -> SyncStats:
    """Sync the index once, outside any server, and return what changed."""
    settings = Settings.from_env()
    client = JoplinClient(settings)
    try:
        return await AppContext(client=client, settings=settings).semantic().sync(full=full)
    finally:
        await client.aclose()


def reindex(args: argparse.Namespace) -> None:
    refuse_unreadable_settings(Settings.from_env())
    stats = asyncio.run(run_reindex(args.full))
    print(
        f"added={stats.added} updated={stats.updated} deleted={stats.deleted} "
        f"total={stats.total} model={stats.model_name}"
    )
    # The index is shared with the server. Run without the server's settings, this
    # command has no filter and indexes everything, so say which it was.
    if Settings.from_env().notebook_filter_set:
        print("Notebook filter: set; only visible notes are indexed.")
    else:
        print(
            "Notebook filter: not set; every note is indexed. If the server has one, "
            "run this with the server's --env-file."
        )


def notebook_lines(
    folders: list[JsonDict],
    notes_index: list[JsonDict],
    scope: NotebookScope | None,
    targets: WriteTargets | None = None,
    editable: WriteTargets | None = None,
) -> list[str]:
    """One line per notebook, as an indented tree: mark, id, title and note count.

    Every notebook is listed, hidden ones too: this is the operator's own view.
    The mark is ``writable`` (visible, and a note may be created there),
    ``editable`` (its notes may be changed), ``writable+editable``, ``visible`` or
    ``hidden``. ``scope`` is None when the settings don't resolve, and the mark is
    then ``?``.
    """
    width = 17 if editable is not None and editable.titles else 8
    counts: dict[str, int] = {}
    for note in notes_index:
        parent = note.get("parent_id", "")
        counts[parent] = counts.get(parent, 0) + 1
    ids = {folder["id"] for folder in folders}
    children_of: dict[str, list[JsonDict]] = {}
    for folder in sorted(folders, key=lambda f: str(f.get("title", "")).casefold()):
        parent = folder.get("parent_id", "")
        children_of.setdefault(parent if parent in ids else "", []).append(folder)

    lines: list[str] = []
    listed: set[str] = set()

    def walk(parent: str, depth: int) -> None:
        for folder in children_of.get(parent, []):
            if folder["id"] in listed:  # notebooks that contain each other
                continue
            listed.add(folder["id"])
            writable = targets is not None and targets.title_of(folder["id"]) is not None
            changeable = editable is not None and editable.title_of(folder["id"]) is not None
            if scope is None:
                mark = "?"
            elif writable and changeable:
                mark = "writable+editable"
            elif writable:
                mark = "writable"
            elif changeable:
                mark = "editable"
            else:
                mark = "visible" if scope.allows(folder["id"]) else "hidden"
            title = folder.get("title", "")
            count = counts.get(folder["id"], 0)
            lines.append(f"{mark:<{width}}  {folder['id']}  {'  ' * depth}{title} ({count})")
            walk(folder["id"], depth + 1)

    walk("", 0)
    # Whatever the walk from the top didn't reach (a loop of notebooks) is listed
    # too: this command must show everything a client could be shown.
    for folder in folders:
        if folder["id"] not in listed:
            walk(folder.get("parent_id", ""), 0)
    return lines


async def fetch_notebooks(settings: Settings) -> tuple[list[JsonDict], list[JsonDict]]:
    """Joplin's notebooks and its note index (ids and notebooks only, no content)."""
    client = JoplinClient(settings)
    try:
        return await client.get_folders(), await client.get_notes_index()
    finally:
        await client.aclose()


def _number_problem(name: str) -> str:
    """What a number setting that can't be read should have been."""
    got = os.environ.get(name, "")
    if name in (TIMEOUT_VAR, CONNECT_TIMEOUT_VAR):
        return f"{name} must be a number of seconds above 0, got {got!r}"
    if name == CREATE_NOTES_MAX_VAR:
        return f"{name} must be a whole number from 0 to {CREATE_NOTES_CEILING}, got {got!r}"
    return f"{name} must be a whole number of at least 1, got {got!r}"


def refuse_unreadable_settings(settings: Settings) -> None:
    """Refuse a setting whose value can't be read: a switch or a number of seconds.

    The setting itself keeps its safe value (config.py): a switch stays on, a
    timeout keeps its default, so a typo exposes nothing. Refusing is so the
    operator finds out that the value didn't do what they meant.
    """
    problems = [
        f"{name} must be true or false, got {os.environ.get(name, '')!r}"
        for name in settings.unreadable_switches
    ] + [_number_problem(name) for name in settings.unreadable_numbers]
    if problems:
        raise ServeConfigError("; ".join(problems))


def notebooks(_args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    refuse_unreadable_settings(settings)
    folders, notes_index = asyncio.run(fetch_notebooks(settings))
    try:
        scope = scope_of(settings, folders)
        targets = write_targets_of(settings, folders)
        editable = edit_targets_of(settings, folders)
    except NotebookFilterError:
        # Show the tree anyway: its ids are what fixes the settings.
        print("\n".join(notebook_lines(folders, notes_index, None)))
        raise
    if scope.visible is None:
        print(f"No notebook filter is set: all {len(folders)} notebooks are visible.")
    else:
        print(f"Notebook filter: {len(scope.visible)} of {len(folders)} notebooks visible.")
    if settings.writes_enabled:
        print(f"Note creation: allowed in the {len(targets.titles)} marked writable.")
        if not settings.ask_every_time:
            print("Asking before each note: left to the client (SBJ_ASK_EVERY_TIME is off).")
    else:
        print("Note creation: off (SBJ_WRITE_NOTEBOOKS is not set).")
    if settings.edits_enabled:
        replace = ", replacing whole notes too" if settings.replace else ""
        print(f"Changing notes: allowed in the {len(editable.titles)} marked editable{replace}.")
    else:
        print("Changing notes: off (SBJ_EDIT_NOTEBOOKS is not set).")
    print("\n".join(notebook_lines(folders, notes_index, scope, targets, editable)))


def serve(args: argparse.Namespace) -> None:
    transport = choose_transport(SERVER, args.transport)
    refuse_unreadable_settings(Settings.from_env())
    if transport == "stdio":
        refuse_http_flags(
            {
                "--host": args.host,
                "--port": args.port,
                "--path": args.path,
                "--public-url": args.public_url,
                "--no-auth": args.no_auth or None,
            }
        )
        asyncio.run(check_at_startup(Settings.from_env()))
        # Never FastMCP's own FASTMCP_TRANSPORT: run_stdio names the transport.
        run_stdio(create_server())
        return
    options = http_options(
        SERVER,
        host=args.host,
        port=args.port,
        path=args.path,
        public_url=args.public_url,
        no_auth=args.no_auth,
    )
    sync_interval = sync_interval_minutes()
    auth_settings = AuthSettings.from_env(SERVER)
    validate(SERVER, options, auth_configured=auth_settings is not None)
    configure_logging(SERVER)
    settings = Settings.from_env()
    asyncio.run(check_at_startup(settings))
    if settings.writes_enabled and not settings.ask_every_time:
        # Said out loud, like a missing notebook filter: the operator turned a
        # protection off, and the log should show that it was on purpose.
        logger.info(
            "note creation: SBJ_ASK_EVERY_TIME is off, so whether you are asked before "
            "each note is up to the client"
        )
    auth = None
    if auth_settings is not None:
        assert options.public_url is not None  # validate() guarantees it with sign-in
        auth = build_auth(SERVER, auth_settings, options.public_url)
    server = create_server(
        auth=auth,
        allowed_ids=auth_settings.allowed_ids if auth_settings else None,
        rate_limit_per_minute=options.rate_limit_per_minute,
        remote=True,
        sync_interval_minutes=sync_interval,
    )
    run_http(server, options)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return
    try:
        # The named file is the configuration: a checkout's .env is not read as
        # well, where it would fill in whatever the file leaves out.
        load_configuration(SERVER, getattr(args, "env_file", None))
        if args.command == "serve":
            serve(args)
        elif args.command == "reindex":
            reindex(args)
        elif args.command == "notebooks":
            notebooks(args)
        elif args.command == "restore":
            restore(args)
    except ServeConfigError as exc:
        parser.exit(2, f"{parser.prog}: error: {exc}\n")
    except NotebookFilterError as exc:
        # A configuration error like the ones above. The detail names the entry at
        # fault; this is the operator's terminal, so it may.
        parser.exit(2, f"{parser.prog}: error: {exc.detail}\n")
    except (SemanticUnavailableError, JoplinError) as exc:
        parser.exit(1, f"{parser.prog}: error: {exc}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
