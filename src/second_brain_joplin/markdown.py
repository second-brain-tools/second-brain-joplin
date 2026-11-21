"""Headings and sections of a note's Markdown: the one place that decides what counts.

Reading by section (``joplin_read``), the section a search hit sits in
(``joplin_search``) and where an edit appends (``joplin_edit_note``) all use these
rules, so a section found one way is the section the others mean.

- **A heading** is an ATX heading (``#`` to ``######``, then a space or the end of
  the line, at most three spaces in), outside a fenced code block (```` ``` ```` or
  ``~~~``). Setext headings (text underlined with ``===`` or ``---``) don't count:
  Joplin's own outline counts them, this server doesn't.
- **A section** runs from its heading's line to the next heading of the same or a
  higher level, so it includes its subsections. **Section 0** is the text before
  the first heading, when there is any.
- **Paths are positional:** ``1``, ``1.2``, ``1.2.1``, by nesting, whatever the
  levels (a ``###`` straight under a ``#`` is ``1.1``).
- Offsets are into the text as stored: ``\\r\\n`` and ``\\n`` line endings both work.

Pure and standard-library only.
"""

import re
from dataclasses import dataclass

_LINE = re.compile(r"[^\r\n]*(?:\r\n|\n|\r)?")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_PATH = re.compile(r"^\d+(?:\.\d+)*$")


@dataclass(frozen=True)
class Section:
    """One section: where it starts and ends in the body, and what heads it."""

    path: str  # "0" for the text before the first heading
    level: int  # 0 for section 0, else 1–6
    heading: str  # whitespace collapsed; "" for section 0
    start: int  # offset of the heading line (0 for section 0)
    end: int  # offset just past the section, subsections included


class SectionError(ValueError):
    """An address that names no section, or more than one. The message says which."""


def _lines(body: str) -> list[tuple[int, str]]:
    """(offset, line without its ending) for each line of ``body``."""
    out = []
    for match in _LINE.finditer(body):
        if match.start() == len(body) and match.group() == "":
            break
        out.append((match.start(), match.group().rstrip("\r\n")))
    return out


def _clean(text: str) -> str:
    return " ".join(text.split())


def sections(body: str) -> list[Section]:
    """Every section of ``body`` in document order, section 0 first when it exists."""
    found: list[tuple[int, int, str]] = []  # (offset, level, heading)
    fence: tuple[str, int] | None = None  # (char, length) of the open fence
    for offset, line in _lines(body):
        opener = _FENCE.match(line)
        if fence is not None:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= fence[1]:
                if not opener.group(2).strip():
                    fence = None
            continue
        if opener and not (opener.group(1)[0] == "`" and "`" in opener.group(2)):
            fence = (opener.group(1)[0], len(opener.group(1)))
            continue
        atx = _ATX.match(line)
        if atx:
            found.append((offset, len(atx.group(1)), _clean(atx.group(2) or "")))

    result: list[Section] = []
    first = found[0][0] if found else len(body)
    if body[:first].strip():
        result.append(Section("0", 0, "", 0, first))

    stack: list[tuple[int, str, list[int]]] = []  # (level, path, [children counted])
    top_count = [0]
    for index, (offset, level, heading) in enumerate(found):
        while stack and stack[-1][0] >= level:
            stack.pop()
        counter = stack[-1][2] if stack else top_count
        counter[0] += 1
        path = f"{stack[-1][1]}.{counter[0]}" if stack else str(counter[0])
        end = len(body)
        for later_offset, later_level, _ in found[index + 1 :]:
            if later_level <= level:
                end = later_offset
                break
        result.append(Section(path, level, heading, offset, end))
        stack.append((level, path, [0]))
    return result


def find_section(body: str, address: str) -> Section:
    """The section an address names: a path (``1.2``, ``0``) or a heading's exact text.

    Heading text is compared with whitespace collapsed, and must name exactly one
    heading. Raises :class:`SectionError` otherwise, naming what there is.
    """
    all_sections = sections(body)
    wanted = _clean(address)
    if _PATH.match(wanted):
        for section in all_sections:
            if section.path == wanted:
                return section
        raise SectionError(f"The note has no section {wanted}. {_listing(all_sections)}")
    matches = [s for s in all_sections if s.level and s.heading == wanted]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SectionError(f"The note has no heading {wanted!r}. {_listing(all_sections)}")
    paths = ", ".join(s.path for s in matches)
    raise SectionError(
        f"The heading {wanted!r} appears {len(matches)} times (sections {paths}); "
        "name the section by its path instead."
    )


def _listing(all_sections: list[Section], at_most: int = 30) -> str:
    headed = [s for s in all_sections if s.level]
    if not headed:
        return "It has no headings."
    shown = "; ".join(f"{s.path} {s.heading}" for s in headed[:at_most])
    more = f"; and {len(headed) - at_most} more" if len(headed) > at_most else ""
    return f"Its sections: {shown}{more}."


def outline(body: str) -> str:
    """One line per section: ``path<TAB>characters<TAB>heading``."""
    return "".join(f"{s.path}\t{s.end - s.start}\t{s.heading}\n" for s in sections(body))


def section_at(body: str, offset: int) -> Section | None:
    """The innermost section holding ``offset``, or None when the note has no sections."""
    best: Section | None = None
    for section in sections(body):
        if section.start <= offset < section.end or (
            offset == len(body) and section.end == len(body)
        ):
            if best is None or section.level > best.level:
                best = section
    return best
