"""The safety net for every tool that changes a note (#82): changes.py, journal.py, restore."""

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from fakes import FakeJoplin
from fastmcp import Client
from fastmcp.exceptions import ToolError
from second_brain_core import env as core_env

from second_brain_joplin import cli
from second_brain_joplin.changes import ChangeRequest, change_note, sha256
from second_brain_joplin.config import Settings
from second_brain_joplin.context import AppContext
from second_brain_joplin.joplin_client import JoplinClient
from second_brain_joplin.journal import DAY, Journal, JournalError
from second_brain_joplin.server import create_server

BODY = "# Plan\n\nOld text.\n"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("SBJ_NOTEBOOKS", "SBJ_EXCLUDE_NOTEBOOKS", "SBJ_WRITE_NOTEBOOKS", "SBJ_REPLACE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SBJ_UNDO_DIR", str(tmp_path / "undo"))
    monkeypatch.setenv("SBJ_EDIT_NOTEBOOKS", "Projects")
    monkeypatch.setattr(core_env, "load_dotenv", lambda *a, **k: False)


@pytest.fixture
def joplin() -> Iterator[FakeJoplin]:
    """Projects (editable) ─ Sub, Archive (not editable), Private."""
    with respx.mock:
        fake = FakeJoplin()
        projects = fake.notebook("Projects")
        sub = fake.notebook("Sub", parent=projects)
        archive = fake.notebook("Archive")
        private = fake.notebook("Private")
        fake.ids = {"projects": projects, "sub": sub, "archive": archive, "private": private}
        fake.note_id = fake.note("Plan", BODY, projects)
        fake.install()
        yield fake


def _app() -> AppContext:
    settings = Settings.from_env()
    return AppContext(client=JoplinClient(settings), settings=settings)


def _request(
    note_id: str,
    *,
    title: str = "Plan",
    expected: str | None = None,
    make: Callable[[str], str] = lambda body: body + "New line.\n",
    parts: tuple[str, ...] = ("append New line.",),
    tool: str = "test_change",
    allow_empty: bool = True,
) -> ChangeRequest:
    return ChangeRequest(
        tool=tool,
        note_ref=note_id,
        title=title,
        expected_sha256=expected if expected is not None else sha256(BODY),
        make_body=make,
        request_parts=parts,
        allow_empty=allow_empty,
    )


def _adding(text: str) -> Callable[[str], str]:
    return lambda body: body + text


async def _refused(app: AppContext, request: ChangeRequest) -> str:
    with pytest.raises(ToolError) as raised:
        await change_note(app, request)
    return str(raised.value)


def _journal_files() -> list[Path]:
    return sorted(Path(os.environ["SBJ_UNDO_DIR"]).glob("*.json"))


# -- a change that goes through --------------------------------------------------------


async def test_a_change_is_kept_first_then_sent_as_the_body_alone(joplin: FakeJoplin) -> None:
    seen_before_put: list[list[dict[str, Any]]] = []
    joplin.before_put = lambda _id, _payload: seen_before_put.append(
        [json.loads(path.read_text()) for path in _journal_files()]
    )
    result = await change_note(_app(), _request(joplin.note_id))

    assert joplin.puts == [{"body": BODY + "New line.\n"}]  # one property, nothing else
    assert len(seen_before_put[0]) == 1  # the copy was on disk before the PUT
    kept = seen_before_put[0][0]
    assert (kept["old_body"], kept["old_title"], kept["outcome"]) == (BODY, "Plan", "sent")
    assert json.loads(_journal_files()[0].read_text())["outcome"] == "applied"
    assert (result.old_chars, result.new_chars) == (len(BODY), len(BODY) + 10)
    assert result.new_sha256 == hashlib.sha256((BODY + "New line.\n").encode()).hexdigest()
    assert result.notebook == "Projects" and result.updated_time is not None
    assert result.already_applied is False


async def test_the_journal_is_readable_only_by_its_owner(joplin: FakeJoplin) -> None:
    if os.name != "posix":
        pytest.skip("modes are POSIX's")
    await change_note(_app(), _request(joplin.note_id))
    directory = Path(os.environ["SBJ_UNDO_DIR"])
    assert directory.stat().st_mode & 0o777 == 0o700
    assert _journal_files()[0].stat().st_mode & 0o777 == 0o600


async def test_a_sub_notebook_of_an_editable_one_is_editable(joplin: FakeJoplin) -> None:
    note = joplin.note("Deep", BODY, joplin.ids["sub"])
    result = await change_note(_app(), _request(note, title="Deep"))
    assert result.notebook == "Sub"


# -- the checks, in order, each writing nothing -------------------------------------------


async def test_a_hidden_note_answers_like_a_missing_one_whatever_else_is_wrong(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden = joplin.note("Secret", BODY, joplin.ids["private"])
    monkeypatch.setenv("SBJ_EXCLUDE_NOTEBOOKS", "Private")
    app = _app()
    answers = {
        await _refused(app, _request(hidden, title="wrong", expected="0" * 64)),
        await _refused(app, _request(hidden, title="Secret")),
        await _refused(app, _request("f" * 32)),
    }
    assert answers == {"The requested Joplin resource was not found."}
    assert joplin.puts == [] and _journal_files() == []


async def test_a_note_off_the_edit_list_is_refused_naming_the_list(joplin: FakeJoplin) -> None:
    note = joplin.note("Old", BODY, joplin.ids["archive"])
    message = await _refused(_app(), _request(note, title="Old"))
    assert "can't be changed" in message or "isn't one whose notes can be changed" in message
    assert "'Projects'" in message and "'Sub'" in message and "Archive" not in message
    assert joplin.puts == []


async def test_a_title_that_does_not_match_is_refused(joplin: FakeJoplin) -> None:
    assert "title doesn't match" in await _refused(_app(), _request(joplin.note_id, title="Plan!"))
    assert joplin.puts == [] and _journal_files() == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("deleted_time", 5, "trash"),
        ("is_conflict", 1, "conflict copy"),
        ("encryption_applied", 1, "decrypted"),
        ("markup_language", 2, "isn't Markdown"),
    ],
)
async def test_notes_no_tool_changes(
    joplin: FakeJoplin, field: str, value: int, message: str
) -> None:
    joplin.notes[joplin.note_id][field] = value
    assert message in await _refused(_app(), _request(joplin.note_id))
    assert joplin.puts == []


async def test_a_checksum_that_is_not_the_current_one_is_refused(joplin: FakeJoplin) -> None:
    message = await _refused(_app(), _request(joplin.note_id, expected=sha256("older text")))
    assert "changed since it was read" in message
    assert joplin.puts == []


async def test_a_change_that_changes_nothing_is_refused(joplin: FakeJoplin) -> None:
    assert "leave the note as it is" in await _refused(
        _app(), _request(joplin.note_id, make=lambda b: b)
    )


async def test_an_empty_body_is_refused_where_the_tool_says_so(joplin: FakeJoplin) -> None:
    message = await _refused(
        _app(), _request(joplin.note_id, make=lambda b: "  ", allow_empty=False)
    )
    assert "replaced with nothing" in message


async def test_a_body_over_the_limit_is_refused(joplin: FakeJoplin) -> None:
    message = await _refused(_app(), _request(joplin.note_id, make=lambda b: "x" * 100_001))
    assert "over the limit" in message


async def test_dropping_an_attachment_reference_is_refused_but_a_note_link_may_go(
    joplin: FakeJoplin,
) -> None:
    note_id = joplin.note_id
    resource = joplin.attach(note_id)
    linked_note = "c" * 32
    body = f"# Plan\n\n![photo](:/{resource})\n\nSee [other](:/{linked_note}).\n"
    joplin.notes[note_id]["body"] = body
    app = _app()
    message = await _refused(
        app,
        _request(note_id, expected=sha256(body), make=lambda b: b.replace(f"(:/{resource})", "()")),
    )
    assert "attachments" in message
    result = await change_note(
        app,
        _request(
            note_id,
            expected=sha256(body),
            make=lambda b: b.replace(f"[other](:/{linked_note})", "other"),
        ),
    )
    assert result.new_chars < len(body)


async def test_an_unwritable_journal_refuses_the_change_and_sends_nothing(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("SBJ_UNDO_DIR", str(blocker / "undo"))
    message = await _refused(_app(), _request(joplin.note_id))
    assert "couldn't keep a copy" in message and "nothing was changed" in message
    assert str(tmp_path) not in message  # the path is for the log, not the client
    assert joplin.puts == []


# -- the daily limit ---------------------------------------------------------------------


async def test_the_daily_limit_refuses_and_survives_a_restart(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBJ_CHANGES_PER_DAY", "2")
    note_id = joplin.note_id
    body = BODY
    for word in ("one", "two"):
        await change_note(
            _app(),
            _request(note_id, expected=sha256(body), make=_adding(word), parts=(word,)),
        )
        body = joplin.notes[note_id]["body"]
    message = await _refused(
        _app(), _request(note_id, expected=sha256(body), make=lambda b: b + "3", parts=("3",))
    )
    assert "limit of 2 changes a day is reached" in message and "resets at" in message
    assert len(joplin.puts) == 2


async def test_a_change_that_failed_does_not_count(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBJ_CHANGES_PER_DAY", "1")
    joplin.put_refused = True
    await _refused(_app(), _request(joplin.note_id))
    joplin.put_refused = False
    await change_note(_app(), _request(joplin.note_id))
    assert json.loads(_journal_files()[0].read_text())["outcome"] == "failed"


# -- repeats, lost answers, one change at a time ---------------------------------------------


async def test_the_same_call_twice_changes_the_note_once(joplin: FakeJoplin) -> None:
    app = _app()
    first = await change_note(app, _request(joplin.note_id))
    again = await change_note(app, _request(joplin.note_id))
    assert again.already_applied is True
    assert again.new_sha256 == first.new_sha256
    assert len(joplin.puts) == 1 and len(_journal_files()) == 1


async def test_a_repeat_after_someone_changed_the_note_is_checked_again(joplin: FakeJoplin) -> None:
    app = _app()
    await change_note(app, _request(joplin.note_id))
    joplin.notes[joplin.note_id]["body"] = "Edited in Joplin.\n"
    assert "changed since it was read" in await _refused(app, _request(joplin.note_id))


async def test_a_lost_answer_after_the_change_landed_is_a_success(joplin: FakeJoplin) -> None:
    joplin.put_loses_response = True
    result = await change_note(_app(), _request(joplin.note_id))
    assert result.new_chars == len(BODY) + 10
    assert json.loads(_journal_files()[0].read_text())["outcome"] == "applied"


async def test_a_lost_answer_with_nothing_landed_is_a_maybe(joplin: FakeJoplin) -> None:
    joplin.put_saves_nothing_and_times_out = True
    message = await _refused(_app(), _request(joplin.note_id))
    assert "may or may not have been made" in message
    assert json.loads(_journal_files()[0].read_text())["outcome"] == "unconfirmed"


async def test_a_refused_connection_changed_nothing(joplin: FakeJoplin) -> None:
    joplin.put_refused = True
    message = await _refused(_app(), _request(joplin.note_id))
    assert "unreachable" in message and "may or may not" not in message


async def test_an_error_answer_with_nothing_changed_is_a_failure(joplin: FakeJoplin) -> None:
    joplin.put_status = 500
    message = await _refused(_app(), _request(joplin.note_id))
    assert "HTTP 500" in message and "may or may not" not in message
    assert json.loads(_journal_files()[0].read_text())["outcome"] == "failed"


async def test_two_changes_to_one_note_run_one_after_the_other(joplin: FakeJoplin) -> None:
    app = _app()
    first = _request(joplin.note_id, make=lambda b: b + "A", parts=("A",))
    second = _request(joplin.note_id, make=lambda b: b + "B", parts=("B",))
    results = await asyncio.gather(
        change_note(app, first), change_note(app, second), return_exceptions=True
    )
    ok = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, ToolError)]
    assert len(ok) == 1 and len(refused) == 1
    assert "changed since it was read" in str(refused[0])  # it saw the first one's result
    assert len(joplin.puts) == 1


# -- the journal on its own -------------------------------------------------------------


def _note(body: str = BODY, note_id: str = "a" * 32) -> dict[str, Any]:
    return {"id": note_id, "title": "T", "body": body, "parent_id": "p" * 32}


def test_the_journal_keeps_copies_newest_first(tmp_path: Path) -> None:
    clock = [1000.0]
    journal = Journal(tmp_path / "j", days=90, max_mb=500, clock=lambda: clock[0])
    for text in ("one", "two", "three"):
        journal.record(
            tool="t", caller="c", note=_note(text), new_chars=1, old_sha256="o", new_sha256="n"
        )
        clock[0] += 1
    assert [e.old_body for e in journal.copies_of("a" * 32)] == ["three", "two", "one"]
    assert journal.copies_of("b" * 32) == []


def test_old_copies_go_after_the_keep_time(tmp_path: Path) -> None:
    clock = [1_000_000.0]
    journal = Journal(tmp_path / "j", days=2, max_mb=500, clock=lambda: clock[0])
    journal.record(
        tool="t", caller="c", note=_note("old"), new_chars=1, old_sha256="o", new_sha256="n"
    )
    clock[0] += 3 * DAY
    journal.record(
        tool="t", caller="c", note=_note("new"), new_chars=1, old_sha256="o", new_sha256="n"
    )
    assert journal.prune() == 1
    assert [e.old_body for e in journal.entries()] == ["new"]


def test_the_size_cap_drops_the_oldest_but_never_the_last_day(tmp_path: Path) -> None:
    clock = [1_000_000.0]
    journal = Journal(tmp_path / "j", days=90, max_mb=1, clock=lambda: clock[0])
    big = "x" * 600_000
    journal.record(
        tool="t", caller="c", note=_note(big), new_chars=1, old_sha256="o", new_sha256="n"
    )
    clock[0] += 2 * DAY
    journal.record(
        tool="t", caller="c", note=_note(big), new_chars=1, old_sha256="o", new_sha256="n"
    )
    journal.record(
        tool="t", caller="c", note=_note(big), new_chars=1, old_sha256="o", new_sha256="n"
    )
    assert journal.prune() == 1  # the old one; the two from today stay though over the cap
    assert len(journal.entries()) == 2


def test_the_day_resets_at_local_midnight(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "j", days=90, max_mb=500, clock=time.time)
    start, nxt = journal.day_start(), journal.next_day()
    assert time.localtime(start)[3:6] == (0, 0, 0) and time.localtime(nxt)[3:6] == (0, 0, 0)
    assert 22 * 3600 < nxt - start < 26 * 3600


def test_a_journal_that_cannot_be_written_says_why_in_its_detail(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x")
    journal = Journal(tmp_path / "file" / "j", days=90, max_mb=500)
    with pytest.raises(JournalError) as raised:
        journal.record(
            tool="t", caller="c", note=_note(), new_chars=1, old_sha256="o", new_sha256="n"
        )
    assert raised.value.detail and str(tmp_path) not in str(raised.value)


# -- joplin_changes ------------------------------------------------------------------------


async def test_joplin_changes_lists_changes_without_text_and_respects_the_filter(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    note_id = joplin.note_id
    gone = joplin.note("Gone", "Bye.\n", joplin.ids["projects"])
    await change_note(_app(), _request(note_id))
    await change_note(_app(), _request(gone, title="Gone", expected=sha256("Bye.\n"), parts=("g",)))
    del joplin.notes[gone]
    async with Client(create_server()) as client:
        listed = (await client.call_tool("joplin_changes", {})).structured_content["result"]
    assert [(c["note_id"], c["title"], c["outcome"]) for c in listed] == [
        (gone, None, "applied"),
        (note_id, "Plan", "applied"),
    ]
    assert "Old text" not in json.dumps(listed) and "New line" not in json.dumps(listed)
    monkeypatch.setenv("SBJ_EXCLUDE_NOTEBOOKS", "Sub")  # a filter that still shows Projects
    joplin.move(note_id, joplin.ids["sub"])
    async with Client(create_server()) as client:
        listed = (await client.call_tool("joplin_changes", {})).structured_content["result"]
    assert [c["note_id"] for c in listed] == [gone]


async def test_joplin_changes_exists_only_with_the_edit_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def names() -> set[str]:
        async with Client(create_server()) as client:
            return {t.name for t in await client.list_tools()}

    assert "joplin_changes" in await names()
    monkeypatch.delenv("SBJ_EDIT_NOTEBOOKS")
    assert "joplin_changes" not in await names()
    monkeypatch.setenv("SBJ_WRITE_NOTEBOOKS", "Projects")  # creating notes is not changing them
    assert "joplin_changes" not in await names()


# -- restore ----------------------------------------------------------------------------------


def test_restore_lists_copies_and_puts_one_back(
    joplin: FakeJoplin, capsys: pytest.CaptureFixture[str]
) -> None:
    note_id = joplin.note_id
    asyncio.run(change_note(_app(), _request(note_id)))
    joplin.notes[note_id]["title"] = "Plan renamed"
    cli.main(["restore", note_id])
    listing = capsys.readouterr().out
    assert "  1  " in listing and "before test_change" in listing and "Old text" not in listing
    cli.main(["restore", note_id, "--pick", "1"])
    assert joplin.notes[note_id]["body"] == BODY
    assert joplin.notes[note_id]["title"] == "Plan"
    assert joplin.puts[-2:] == [{"body": BODY}, {"title": "Plan"}]  # each property on its own
    newest = Journal(os.environ["SBJ_UNDO_DIR"], days=90, max_mb=500).copies_of(note_id)[0]
    assert (newest.tool, newest.old_body) == ("restore", BODY + "New line.\n")  # undoable too


def test_restore_refuses_to_drop_an_attachment_without_force(
    joplin: FakeJoplin, capsys: pytest.CaptureFixture[str]
) -> None:
    note_id = joplin.note_id
    asyncio.run(change_note(_app(), _request(note_id)))
    joplin.attach(note_id)  # attached after the copy was kept
    with pytest.raises(SystemExit):
        cli.main(["restore", note_id, "--pick", "1"])
    assert "--force" in capsys.readouterr().err
    cli.main(["restore", note_id, "--pick", "1", "--force"])
    assert joplin.notes[note_id]["body"] == BODY


def test_restore_with_no_copies_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["restore", "d" * 32])
    assert "No kept copies" in capsys.readouterr().out


# -- settings -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["SBJ_UNDO_DAYS", "SBJ_UNDO_MAX_MB", "SBJ_CHANGES_PER_DAY"])
@pytest.mark.parametrize("raw", ["0", "-1", "lots"])
def test_journal_and_limit_settings_must_be_whole_numbers_of_at_least_one(
    monkeypatch: pytest.MonkeyPatch, name: str, raw: str
) -> None:
    monkeypatch.setenv(name, raw)
    settings = Settings.from_env()
    assert name in settings.unreadable_numbers
    assert (settings.undo_days, settings.undo_max_mb, settings.changes_per_day) == (90, 500, 100)


@pytest.mark.parametrize(
    ("raw", "on", "unreadable"),
    [("", False, False), ("yes", True, False), ("no", False, False), ("maybe", False, True)],
)
def test_the_replace_switch_is_off_unless_it_plainly_says_yes(
    monkeypatch: pytest.MonkeyPatch, raw: str, on: bool, unreadable: bool
) -> None:
    monkeypatch.setenv("SBJ_REPLACE", raw)
    settings = Settings.from_env()
    assert settings.replace is on
    assert ("SBJ_REPLACE" in settings.unreadable_switches) is unreadable


async def test_a_hidden_notebook_on_the_edit_list_is_a_settings_error(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBJ_EXCLUDE_NOTEBOOKS", "Projects")
    monkeypatch.setenv("SBJ_EDIT_NOTEBOOKS", "Projects")
    assert "notebook settings" in await _refused(_app(), _request(joplin.note_id))


async def test_a_token_refused_on_the_write_changed_nothing(joplin: FakeJoplin) -> None:
    joplin.put_status = 403
    message = await _refused(_app(), _request(joplin.note_id))
    assert "rejected the request" in message
    assert json.loads(_journal_files()[0].read_text())["outcome"] == "failed"


async def test_an_error_answer_then_silence_is_a_maybe(joplin: FakeJoplin) -> None:
    joplin.put_status = 500

    def go_down(_id: str, _payload: dict[str, Any]) -> None:
        joplin.down = True  # the read that would check is refused too

    joplin.before_put = go_down
    message = await _refused(_app(), _request(joplin.note_id))
    assert "may or may not have been made" in message
    assert json.loads(_journal_files()[0].read_text())["outcome"] == "unconfirmed"


async def test_an_error_answer_with_the_change_landed_is_a_success(joplin: FakeJoplin) -> None:
    def land_then_fail(note_id: str, payload: dict[str, Any]) -> None:
        joplin.notes[note_id].update(payload)  # saved, then the answer is an error

    joplin.put_status = 500
    joplin.before_put = land_then_fail
    result = await change_note(_app(), _request(joplin.note_id))
    assert result.updated_time is None  # no answer to take it from
    assert json.loads(_journal_files()[0].read_text())["outcome"] == "applied"


async def test_joplin_down_before_the_checks_is_reported(joplin: FakeJoplin) -> None:
    joplin.down = True
    assert "unreachable" in await _refused(_app(), _request(joplin.note_id))


async def test_joplin_changes_stops_at_its_limit_and_skips_a_gone_note_from_a_hidden_notebook(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    note_id = joplin.note_id
    body = BODY
    for word in ("a", "b", "c"):
        await change_note(
            _app(), _request(note_id, expected=sha256(body), make=_adding(word), parts=(word,))
        )
        body = joplin.notes[note_id]["body"]
    async with Client(create_server()) as client:
        listed = (await client.call_tool("joplin_changes", {"limit": 2})).structured_content[
            "result"
        ]
    assert len(listed) == 2
    del joplin.notes[note_id]
    monkeypatch.setenv("SBJ_EXCLUDE_NOTEBOOKS", "Projects")
    monkeypatch.setenv("SBJ_EDIT_NOTEBOOKS", "Archive")
    async with Client(create_server()) as client:
        assert (await client.call_tool("joplin_changes", {})).structured_content["result"] == []
        joplin.down = True
        with pytest.raises(ToolError, match="unreachable"):
            await client.call_tool("joplin_changes", {})
