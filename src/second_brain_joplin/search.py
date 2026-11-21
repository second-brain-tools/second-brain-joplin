"""What a search query asks for: Joplin's `notebook:` terms, and the words to anchor a snippet on.

**Why the server reads `notebook:` itself.** Joplin's search matches `notebook:"Name"`
(and `-notebook:`) against every notebook title with SQL `LIKE` (`*`, `%` and `_` are
wildcards, letters match either case) and takes in every sub-notebook of a match, across
the whole vault. Seen on Joplin 3.7.24 (2026-11-21): `notebook:"<a parent>"` returned
the notes of its sub-notebooks, and a three-letter prefix with `%` returned the same.
So with a notebook filter that shows only a sub-notebook, searching for the hidden
parent's name would return the visible child's notes and confirm that the parent
exists. With a filter set, the server therefore matches these terms against the
*visible* notebooks only:

- a `notebook:` term that matches no visible notebook gives no results, at once, as a
  name that matches nothing does; otherwise hits are kept only in the visible notebooks
  it matched and their visible sub-notebooks;
- a `-notebook:` term is taken out of the query Joplin gets and applied here, to the
  visible notebooks it matches, so a hidden notebook excludes nothing.

`tag:` needs none of this: Joplin applies it note by note, and every hit is checked
against the filter before it is returned.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

_NOTEBOOK_TERM = re.compile(r'(?<!\S)(-?)notebook:("[^"]*"?|\S+)', re.IGNORECASE)
_FILTER_TERM = re.compile(r'(?<!\S)-?[A-Za-z]+:("[^"]*"?|\S+)')
_PHRASE = re.compile(r'"([^"]+)"')

SNIPPET_LENGTH = 200


@dataclass
class NotebookTerms:
    """The `notebook:` terms of a query, and the query to send Joplin."""

    query: str  # the query with the `-notebook:` terms taken out
    include: list[str] = field(default_factory=list)  # patterns, quotes trimmed
    exclude: list[str] = field(default_factory=list)


def _trim(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value.strip('"')


def notebook_terms(query: str) -> NotebookTerms:
    """Split out the query's `notebook:` and `-notebook:` terms."""
    terms = NotebookTerms(query=query)
    kept: list[str] = []
    last = 0
    for match in _NOTEBOOK_TERM.finditer(query):
        value = _trim(match.group(2))
        if match.group(1):
            terms.exclude.append(value)
            kept.append(query[last : match.start()])
            last = match.end()
        else:
            terms.include.append(value)
    kept.append(query[last:])
    terms.query = " ".join("".join(kept).split())
    return terms


def like(pattern: str) -> re.Pattern[str]:
    """SQL `LIKE` as Joplin uses it: `%` and `*` any run, `_` one character, either case."""
    parts = []
    for char in pattern:
        if char in "%*":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(parts), re.IGNORECASE | re.DOTALL)


def matching(patterns: Iterable[str], titles: dict[str, str]) -> set[str]:
    """The ids (of ``titles``: id → title) whose title one of ``patterns`` matches."""
    compiled = [like(p) for p in patterns]
    return {nid for nid, title in titles.items() if any(c.fullmatch(title) for c in compiled)}


def keywords(query: str) -> list[str]:
    """The plain words and phrases of a query, lower case, for anchoring a snippet.

    Filter terms (`name:value`) and negated words are left out; `*` is dropped.
    """
    rest = _FILTER_TERM.sub(" ", query)
    words = [p.lower() for p in _PHRASE.findall(rest)]
    rest = _PHRASE.sub(" ", rest)
    for token in rest.split():
        if token.startswith("-"):
            continue
        token = token.replace("*", "").strip("\"'").lower()
        if token:
            words.append(token)
    return words


def first_match(body: str, words: Sequence[str]) -> int | None:
    """Where the earliest of ``words`` first occurs in ``body``, ignoring case."""
    lowered = body.lower()
    found = [i for i in (lowered.find(w) for w in words if w) if i >= 0]
    return min(found) if found else None


def snippet(body: str, at: int, length: int = SNIPPET_LENGTH) -> str:
    """About ``length`` characters of ``body`` around offset ``at``, on one line."""
    start = max(0, at - length // 3)
    end = min(len(body), start + length)
    start = max(0, end - length)
    text = " ".join(body[start:end].split())
    return ("…" if start > 0 else "") + text + ("…" if end < len(body) else "")
