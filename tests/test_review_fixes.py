"""Regressions for the review of 1.2 before release: each test names the problem it pins."""

import hashlib
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from fakes import FakeJoplin
from fastmcp import Client
from fastmcp.exceptions import ToolError
from second_brain_core import env as core_env

from second_brain_joplin import cli
from second_brain_joplin.search import first_match, like, notebook_terms, terms
from second_brain_joplin.server import create_server
from second_brain_joplin.tools.edit import Append, TextEdit, apply_append, apply_edits


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("SBJ_NOTEBOOKS", "SBJ_EXCLUDE_NOTEBOOKS", "SBJ_REPLACE", "SBJ_ASK_EVERY_TIME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SBJ_UNDO_DIR", str(tmp_path / "undo"))
    monkeypatch.setenv("SBJ_EDIT_NOTEBOOKS", "Kid")
    monkeypatch.setenv("SBJ_WRITE_NOTEBOOKS", "Kid")
    monkeypatch.setattr(core_env, "load_dotenv", lambda *a, **k: False)


@pytest.fixture
def joplin() -> Iterator[FakeJoplin]:
    """Secret (hidden by the tests that set a filter) ─ Kid; Other."""
    with respx.mock:
        fake = FakeJoplin()
        secret = fake.notebook("Secret")
        kid = fake.notebook("Kid", parent=secret)
        fake.ids = {"secret": secret, "kid": kid, "other": fake.notebook("Other")}
        fake.note_id = fake.note("Kid note", "apple kid", kid)
        fake.note("Secret note", "apple secret", secret)
        fake.install()
        yield fake


async def _call(tool: str, args: dict[str, Any]) -> Any:
    async with Client(create_server()) as client:
        return (await client.call_tool(tool, args)).structured_content


async def _refused(tool: str, args: dict[str, Any]) -> str:
    async with Client(create_server()) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool(tool, args)
    return str(raised.value)


# 1: a notebook: term right after a closing quote was not seen ----


@pytest.mark.parametrize(
    "query",
    [
        '"apple"notebook:Secr%',
        '"apple"NoteBook:"Secret"',
        '"apple"-notebook:Secret',
        "apple notebook:Secr%",
    ],
)
async def test_a_notebook_term_after_a_quote_is_read_as_joplin_reads_it(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    monkeypatch.setenv("SBJ_NOTEBOOKS", "Kid")
    missing = query.replace("Secr", "Zzzz")
    assert await _call("joplin_search", {"query": query}) == await _call(
        "joplin_search", {"query": missing}
    )


async def test_notebook_inside_a_phrase_is_refused_under_a_filter(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBJ_NOTEBOOKS", "Kid")
    assert "must be a search word of its own" in await _refused(
        "joplin_search", {"query": '"x notebook:Secret"'}
    )
    monkeypatch.delenv("SBJ_NOTEBOOKS")  # without a filter it is only a phrase
    await _call("joplin_search", {"query": '"x notebook:Secret"'})


def test_the_tokenizer_splits_as_joplins_getterms() -> None:
    split = [
        (t.name, t.value, t.negated)
        for t in terms('"a b"notebook:X -Tag:y sourceurl:https://h/p:1 plain')
    ]
    assert split == [
        ("_", '"a b"', False),
        ("notebook", "X", False),
        ("tag", "y", True),
        ("sourceurl", "https://h/p:1", False),
        ("_", "plain", False),
    ]
    found = notebook_terms('"a"-notebook:"B c" word')
    assert (found.exclude, found.query, found.unread) == (["B c"], '"a" word', False)


# 2: a hidden locked note answered "locked" ----


async def test_a_hidden_locked_note_answers_like_a_missing_one(
    joplin: FakeJoplin, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden = joplin.note("Locked", "secret", joplin.ids["other"])
    joplin.notes[hidden]["is_locked"] = 1
    monkeypatch.setenv("SBJ_EXCLUDE_NOTEBOOKS", "Other")
    missing = "f" * 32
    for tool, args in (
        ("joplin_read", {"note_id": hidden}),
        (
            "joplin_edit_note",
            {
                "note_id": hidden,
                "title": "Locked",
                "expected_sha256": sha("secret"),
                "append": {"text": "x"},
            },
        ),
    ):
        assert await _refused(tool, args) == await _refused(tool, {**args, "note_id": missing})
    many = await _call("joplin_read_notes", {"note_ids": [hidden, missing]})
    assert many["not_found"] == [hidden, missing]


async def test_a_visible_locked_note_says_it_is_locked(joplin: FakeJoplin) -> None:
    joplin.notes[joplin.note_id]["is_locked"] = 1
    assert "locked in Joplin" in await _refused("joplin_read", {"note_id": joplin.note_id})
    assert (await _call("joplin_read_notes", {"note_ids": [joplin.note_id]}))["not_found"] == [
        joplin.note_id
    ]


# 3, 4: append on many CRLF blank lines; edits on mixed line endings ------------------------------


def test_append_after_many_crlf_blank_lines_is_quick_and_in_place() -> None:
    body = "# A\r\n" + "text\r\n" + "\r\n" * 60 + "more\r\n# B\r\n"
    started = time.perf_counter()
    result = apply_append(body, Append(text="added", section="A"))
    assert time.perf_counter() - started < 0.5
    assert result == body.replace("more\r\n", "more\r\nadded\r\n", 1)


def test_append_keeps_trailing_spaces_of_the_last_line() -> None:
    assert (
        apply_append("# A\ntext  \n\n# B\n", Append(text="x", section="A"))
        == "# A\ntext  \nx\n\n# B\n"
    )


def test_an_edit_in_a_note_with_mixed_line_endings_must_name_one_place() -> None:
    with pytest.raises(ToolError, match="appears 2 times"):
        apply_edits("x a\nb y\r\nz a\r\nb", [TextEdit(old="a\nb", new="Q")])
    assert (
        apply_edits("one\ntwo\r\nthree", [TextEdit(old="one\ntwo", new="1\n2")]) == "1\n2\r\nthree"
    )
    assert apply_edits("a\rb\rc", [TextEdit(old="a\rb", new="ab")]) == "ab\rc"


# 5: LIKE folds ASCII case only, as SQLite's ----


def test_like_folds_ascii_case_only() -> None:
    assert like("é%").fullmatch("école") and not like("é%").fullmatch("École")
    assert like("e%").fullmatch("Ecole")


# 7: a retry after a lost answer ----


async def test_a_retry_after_an_unconfirmed_change_that_landed_is_answered_not_refused(
    joplin: FakeJoplin,
) -> None:
    call = {
        "note_id": joplin.note_id,
        "title": "Kid note",
        "expected_sha256": sha("apple kid"),
        "append": {"text": "pear"},
    }
    joplin.put_saves_nothing_and_times_out = True
    async with Client(create_server()) as client:
        with pytest.raises(ToolError, match="may or may not"):
            await client.call_tool("joplin_edit_note", call)
        joplin.notes[joplin.note_id]["body"] = joplin.puts[0]["body"]  # it landed after all
        joplin.put_saves_nothing_and_times_out = False
        again = (await client.call_tool("joplin_edit_note", call)).structured_content
    assert again["already_applied"] is True and len(joplin.puts) == 1


async def test_a_change_known_to_have_failed_can_be_sent_again(joplin: FakeJoplin) -> None:
    call = {
        "note_id": joplin.note_id,
        "title": "Kid note",
        "expected_sha256": sha("apple kid"),
        "append": {"text": "pear"},
    }
    joplin.put_refused = True
    async with Client(create_server()) as client:
        with pytest.raises(ToolError):
            await client.call_tool("joplin_edit_note", call)
        joplin.put_refused = False
        again = (await client.call_tool("joplin_edit_note", call)).structured_content
    assert again["already_applied"] is False and joplin.notes[joplin.note_id]["body"].endswith(
        "pear"
    )


# 8: an attachment's bare id left as plain text is still dropped ----


async def test_turning_an_attachment_link_into_plain_text_is_refused(joplin: FakeJoplin) -> None:
    resource = joplin.attach(joplin.note_id)
    body = f"![photo](:/{resource})\n"
    joplin.notes[joplin.note_id]["body"] = body
    message = await _refused(
        "joplin_edit_note",
        {
            "note_id": joplin.note_id,
            "title": "Kid note",
            "expected_sha256": sha(body),
            "edits": [{"old": f"![photo](:/{resource})", "new": f"photo {resource}"}],
        },
    )
    assert "attachments" in message


# 9: a note with no title ----


async def test_a_note_with_no_title_can_be_edited(joplin: FakeJoplin) -> None:
    untitled = joplin.note("", "body", joplin.ids["kid"])
    result = await _call(
        "joplin_edit_note",
        {"note_id": untitled, "title": "", "expected_sha256": sha("body"), "append": {"text": "x"}},
    )
    assert result["new_chars"] == len("body\nx")


# 10: restore --pick with nothing to restore ----


def test_restore_pick_with_no_copies_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["restore", "d" * 32, "--pick", "1"])
    assert exited.value.code == 2 and "no kept copies" in capsys.readouterr().err


# 11: a repeat that can't be checked is unconfirmed, not failed ----


async def test_a_repeat_whose_earlier_note_cannot_be_looked_up_is_unconfirmed(
    joplin: FakeJoplin,
) -> None:
    joplin.post_saves_late = True
    args = {"title": "N", "body": "b", "notebook": "Kid"}
    async with Client(create_server()) as client:
        with pytest.raises(ToolError, match="may or may not"):
            await client.call_tool("joplin_create_note", args)
        joplin.post_saves_late = False
        original = joplin._note

        def lookup_fails(request: Any) -> Any:
            if request.url.path.endswith(joplin.posts[0]["id"]):
                raise __import__("httpx").ReadError("reset")
            return original(request)

        respx.get(url__regex=rf"{joplin.base_url}/notes/[^/?]+(?:\?|$)").mock(
            side_effect=lookup_fails
        )
        with pytest.raises(ToolError, match="may or may not"):
            await client.call_tool("joplin_create_note", args)
    assert len(joplin.posts) == 1  # no second copy sent


# 13: offsets come from the text itself ----


def test_the_match_is_found_where_it_is_even_after_letters_whose_lower_case_is_longer() -> None:
    body = "İİİİ then the needle"
    assert first_match(body, ["needle"]) == body.index("needle")
