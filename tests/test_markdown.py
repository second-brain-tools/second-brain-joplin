"""The heading and section rules (markdown.py): the ones reading, search and editing share."""

import pytest

from second_brain_joplin.markdown import SectionError, find_section, outline, section_at, sections

NOTE = """Intro line.

# Plan

Plan text.

## Budget

Money.

### Detail

Fine print.

## Schedule

Dates.

# Log

- one
"""


def _paths(body: str) -> list[tuple[str, int, str]]:
    return [(s.path, s.level, s.heading) for s in sections(body)]


def test_paths_are_positional_and_section_zero_is_the_preamble() -> None:
    assert _paths(NOTE) == [
        ("0", 0, ""),
        ("1", 1, "Plan"),
        ("1.1", 2, "Budget"),
        ("1.1.1", 3, "Detail"),
        ("1.2", 2, "Schedule"),
        ("2", 1, "Log"),
    ]


def test_a_section_runs_to_the_next_heading_of_the_same_or_a_higher_level() -> None:
    plan = find_section(NOTE, "1")
    assert NOTE[plan.start : plan.end].startswith("# Plan")
    assert NOTE[plan.start : plan.end].endswith("Dates.\n\n")  # subsections included
    budget = find_section(NOTE, "Budget")
    assert NOTE[budget.start : budget.end] == "## Budget\n\nMoney.\n\n### Detail\n\nFine print.\n\n"
    log = find_section(NOTE, "2")
    assert log.end == len(NOTE)


def test_the_levels_skipped_still_nest() -> None:
    assert _paths("# A\n### B\n## C\n") == [("1", 1, "A"), ("1.1", 3, "B"), ("1.2", 2, "C")]


def test_no_preamble_no_section_zero_and_a_blank_one_neither() -> None:
    assert _paths("# Only\ntext\n")[0][0] == "1"
    assert _paths("\n  \n# Only\n")[0][0] == "1"


def test_a_note_with_no_headings_is_section_zero_alone() -> None:
    assert _paths("just text\nmore\n") == [("0", 0, "")]
    assert sections("") == []


def test_headings_in_code_blocks_do_not_count() -> None:
    body = "# Real\n```\n# not a heading\n```\n~~~~\n# nor this\n~~~\n# still inside\n~~~~\n# Two\n"
    assert [s.heading for s in sections(body)] == ["Real", "Two"]


def test_a_fence_closes_only_with_its_own_kind_and_length() -> None:
    body = "```\n~~~\n# inside\n``\n# inside\n```\n# Out\n"
    assert [s.heading for s in sections(body) if s.level] == ["Out"]


def test_a_backtick_line_with_a_backtick_in_its_info_is_not_a_fence() -> None:
    assert [s.heading for s in sections("```a`b\n# Real\n") if s.level] == ["Real"]


def test_what_is_and_is_not_an_atx_heading() -> None:
    body = (
        "#NoSpace\n"
        "####### seven\n"
        "    # indented four\n"
        "   # Three spaces in\n"
        "## Closing hashes ##\n"
        "#\n"
        "Setext\n===\n"
        "# Tab\theading  \n"
    )
    assert [s.heading for s in sections(body) if s.level] == [
        "Three spaces in",
        "Closing hashes",
        "",
        "Tab heading",
    ]


def test_windows_line_endings_keep_offsets_into_the_stored_text() -> None:
    body = NOTE.replace("\n", "\r\n")
    budget = find_section(body, "1.1")
    assert body[budget.start : budget.end].startswith("## Budget\r\n")
    assert _paths(body) == _paths(NOTE)


def test_a_heading_text_must_name_exactly_one_section() -> None:
    body = "# Notes\n## Notes\n# Other\n"
    with pytest.raises(SectionError, match=r"appears 2 times \(sections 1, 1.1\)"):
        find_section(body, "Notes")
    assert find_section(body, "1.1").heading == "Notes"


def test_a_missing_section_names_what_there_is() -> None:
    with pytest.raises(SectionError, match=r"no heading 'Nope'\. Its sections: 1 Plan; 1.1 Budget"):
        find_section(NOTE, "Nope")
    with pytest.raises(SectionError, match="no section 9"):
        find_section(NOTE, "9")
    with pytest.raises(SectionError, match="It has no headings"):
        find_section("plain", "Plan")


def test_heading_text_is_matched_with_whitespace_collapsed() -> None:
    assert find_section("#   Big   plan  \n", "Big plan").path == "1"
    assert find_section("# Big plan\n", "  Big   plan ").path == "1"


def test_the_outline_has_one_line_per_section_with_its_size() -> None:
    lines = outline(NOTE).splitlines()
    assert lines[0] == f"0\t{len('Intro line.') + 2}\t"
    assert lines[1].startswith("1\t") and lines[1].endswith("\tPlan")
    assert len(lines) == 6
    assert outline("") == ""


def test_section_at_gives_the_innermost_section() -> None:
    offset = NOTE.index("Fine print")
    assert section_at(NOTE, offset).path == "1.1.1"  # type: ignore[union-attr]
    assert section_at(NOTE, 0).path == "0"  # type: ignore[union-attr]
    assert section_at(NOTE, len(NOTE)).path == "2"  # type: ignore[union-attr]
    assert section_at("", 0) is None
