# Tool reference

Eight tools are always there and never change Joplin (`joplin_reindex` writes
only the local index). Up to five more exist only when you allow them:
`joplin_create_note` and `joplin_create_notes`
([`SBJ_WRITE_NOTEBOOKS`](configuration.md#notebooks), `SBJ_CREATE_NOTES_MAX`),
and `joplin_edit_note`, `joplin_replace_note` and `joplin_changes`
([`SBJ_EDIT_NOTEBOOKS`](configuration.md#notebooks), `SBJ_REPLACE`).

| Tool | What it does | Changes |
|---|---|---|
| [`joplin_overview`](#joplin_overview) | Lists the notebooks as a tree, with note counts | Nothing |
| [`joplin_search`](#joplin_search) | Finds notes by keyword, saying where each match is | Nothing |
| [`joplin_read`](#joplin_read) | Reads one note, a page or a section at a time, or its outline | Nothing |
| [`joplin_read_notes`](#joplin_read_notes) | Reads up to 20 notes at once | Nothing |
| [`joplin_list_notes`](#joplin_list_notes) | Lists the notes in one notebook | Nothing |
| [`joplin_recent`](#joplin_recent) | Lists the notes changed in the last N days | Nothing |
| [`joplin_semantic_search`](#joplin_semantic_search) | Finds notes by meaning | Nothing in Joplin (it may refresh the local index first) |
| [`joplin_reindex`](#joplin_reindex) | Builds or refreshes the semantic index | The local index only, never Joplin |
| [`joplin_create_note`](#joplin_create_note) | Adds one new note to a notebook you allow | A new note, nothing else |
| [`joplin_create_notes`](#joplin_create_notes) | Adds up to 20 new notes in one call | New notes, nothing else |
| [`joplin_edit_note`](#joplin_edit_note) | Changes part of a note, or adds to it | That note, after a copy is kept |
| [`joplin_replace_note`](#joplin_replace_note) | Replaces the whole text of a note | That note, after a copy is kept |
| [`joplin_changes`](#joplin_changes) | Lists the changes the server made | Nothing |

What holds for every tool:

- **Arguments are checked first.** A value outside the range given below is
  refused before Joplin is asked anything.
- **Ids are Joplin's:** 32 hexadecimal characters, for notes and notebooks alike.
- **Times are ISO 8601 in UTC,** for example `2026-11-12T17:09:49.545000+00:00`.
- **Results are structured.** Each tool declares an output schema, and a result
  comes back both as JSON text and as MCP structured content. A tool that
  returns a list wraps it there as `{"result": [...]}`; the examples below show
  the list itself.
- **A notebook filter applies to all of them** when you set one: see
  [With a notebook filter](#with-a-notebook-filter).

The examples use made-up notes.

## `joplin_overview`

Lists the notebooks as a tree with the number of notes in each. It takes no
arguments. Use it to learn the notebooks' names and ids before a search, or
before creating a note.

Returns a list of top-level notebooks, each with:

| Field | Type | |
|---|---|---|
| `id` | string | The notebook's id |
| `name` | string | Its title |
| `note_count` | integer | Notes directly in it, not counting its sub-notebooks |
| `children` | list | Its sub-notebooks, in the same shape |

```json
[
  {
    "id": "c7d1e5a09b3f4c2d8e6f1a0b9c8d7e6f",
    "name": "Inbox",
    "note_count": 4,
    "children": []
  },
  {
    "id": "9c40d1e7a3b24f58b6c9e0f1a2d3b4c5",
    "name": "Work",
    "note_count": 57,
    "children": [
      {
        "id": "0b7e3f9a1c2d4e5f8a7b6c5d4e3f2a1b",
        "name": "Projects",
        "note_count": 31,
        "children": []
      }
    ]
  }
]
```

## `joplin_search`

Finds notes by keyword, using Joplin's own search, and says where in each note
the match is. The query takes
[Joplin's search syntax](https://joplinapp.org/help/apps/search):

- Plain words must all match; `any:1 word1 word2` matches any of them.
  `"exact phrase"`, `word*` for a prefix, `-word` to leave a word out.
- Filters, combined with spaces, each negated with `-`: `notebook:"Name"` (the
  notebook and its sub-notebooks), `tag:name`, `title:word`, `body:word`,
  `type:todo` or `type:note`, `iscompleted:0` or `1`, `created:20261101` or
  `created:day-7` (also `week-`, `month-`, `year-`), `updated:…`, `due:…`,
  `sourceurl:…`, `resource:image/png`.

| Argument | Type | Default | Allowed |
|---|---|---|---|
| `query` | string | required | Any Joplin search query |
| `limit` | integer | `10` | 1 to 100 |

Returns a list of hits, in Joplin's order:

| Field | Type | |
|---|---|---|
| `id` | string | The note's id, for `joplin_read` |
| `title` | string | The note's title |
| `excerpt` | string | The first 200 characters of the note on one line, ending in `…` when cut |
| `notebook_id` | string | The id of the notebook it is in |
| `updated_time` | string | When it was last changed |
| `snippet` | string or null | About 200 characters around the first place a word of the query appears in the text, on one line |
| `section` | string or null | The section that holds that place, as a path `joplin_read` takes (`1.2`; `0` before the first heading) |
| `section_heading` | string or null | That section's heading |

```json
[
  {
    "id": "4be0a1c2d3e4f5061728394a5b6c7d8e",
    "title": "Cabinet quotes",
    "excerpt": "Three quotes so far. - Northside: 8,400 - Hale & Sons: 9,150, installed",
    "notebook_id": "0b7e3f9a1c2d4e5f8a7b6c5d4e3f2a1b",
    "updated_time": "2026-11-09T21:40:27.512000+00:00",
    "snippet": "…- Hale & Sons: 9,150, installed - Westbrook: waiting on a site visit…",
    "section": "1",
    "section_heading": "Quotes"
  }
]
```

`snippet`, `section` and `section_heading` are null when the query has no plain
word to look for in the text, as for `tag:kitchen` alone. A section's path is
what [`joplin_read`](#joplin_read) takes as `section`; the heading's text works
there too when no other heading has it.

A search reads at most 20 pages of Joplin's answer (2,000 notes). With a
notebook filter that hides most of them, a search can therefore return fewer
than `limit` hits; narrow the query with more words or a `notebook:` filter.

To find a note by what it means when you don't know its words, use
[`joplin_semantic_search`](#joplin_semantic_search).

## `joplin_read`

Reads one note's markdown, or its outline, or one section of it. A long text
comes a page at a time.

| Argument | Type | Default | Allowed |
|---|---|---|---|
| `note_id` | string | required | A note's id (32 hexadecimal characters), or a link to it: `:/<id>` as one note links another, or `joplin://x-callback-url/openNote?id=<id>` as Joplin copies it |
| `offset` | integer | `0` | 0 or more: the character to start from |
| `max_chars` | integer | `50000` | 1 to 100,000: the most characters to return |
| `outline` | boolean | `false` | `true`: the note's headings instead of its text |
| `section` | string or null | null | One section, subsections included: its path (`1.2`) or its heading's exact text |

`outline` and `section` can't be used together.

Returns:

| Field | Type | |
|---|---|---|
| `id` | string | The note's id |
| `title` | string | Its title |
| `body` | string | Up to `max_chars` characters of markdown, starting at `offset`: of the whole note, of the section, or of the outline |
| `parent_id` | string | The id of the notebook it is in |
| `created_time` | string | When it was created |
| `updated_time` | string | When it was last changed |
| `offset` | integer | Where this page starts |
| `total_chars` | integer | The length of what is being read: the note, the section or the outline |
| `next_offset` | integer or null | Where the next page starts; null when this page reaches the end |
| `section_path` | string or null | The section read, by its path; null when it wasn't one section |
| `section_heading` | string or null | That section's heading |
| `is_outline` | boolean | `true` when `body` is the outline |
| `notebook` | string or null | The name of the notebook it is in |
| `notebook_path` | list of strings | The notebooks above it and its own, from the topmost visible one down |
| `tags` | list of strings | Its tags' names |
| `is_todo` | boolean | Whether it is a to-do |
| `todo_due` | string or null | A to-do's due time; null when it has none |
| `todo_completed` | string or null | When a to-do was ticked done; null while open |
| `attachments` | list | Its attachments: `id`, `title`, `mime` and `size` in bytes; never their content |
| `body_sha256` | string | The SHA-256 of the note's whole text, whatever page or section was read |

```json
{
  "id": "4be0a1c2d3e4f5061728394a5b6c7d8e",
  "title": "Cabinet quotes",
  "body": "Three quotes so far.\n\n- Northside: 8,400\n- Hale & Sons: 9,150, installed\n",
  "parent_id": "0b7e3f9a1c2d4e5f8a7b6c5d4e3f2a1b",
  "created_time": "2026-10-02T15:04:11+00:00",
  "updated_time": "2026-11-09T21:40:27.512000+00:00",
  "offset": 0,
  "total_chars": 73,
  "next_offset": null
}
```

To read a long note in full, call again with `offset` set to the last
`next_offset` until it comes back null. Claude limits how much one tool result
may hold, which is why a page stops at 100,000 characters.

For a long note, ask for the outline first, then read the one section you need.
The outline is one line per section, `path`, its length in characters and its
heading, separated by tabs:

```
0	48
1	1612	Plan
1.1	420	Budget
1.2	988	Schedule
2	310	Log
```

What counts as a heading and a section:

- **A heading** is a line that starts with one to six `#` followed by a space, a
  tab or the end of the line, at most three spaces in, outside a fenced code block (```` ``` ```` or `~~~`). Text
  underlined with `===` or `---` is not a heading here, although Joplin's own
  editor shows it as one.
- **A section** runs from its heading to the next heading of the same or a
  higher level, so it includes its subsections. Section `0` is the text before
  the first heading, when there is any. A note with no headings is section `0`
  alone.
- **Paths count by nesting**, not by the number of `#`: a `###` straight under a
  `#` is `1.1`.
- **A heading's text must name one section.** A heading that appears twice is
  refused with the paths of both, and a missing one with the list of sections
  the note has. Whitespace inside the heading doesn't matter.

A note that doesn't exist, and a note the notebook filter hides, get the same
answer: `The requested Joplin resource was not found.`

`body_sha256` names the version of the text that was read. The tools that change
a note ask for it back, so a note changed since it was read is never overwritten
unseen.

## `joplin_read_notes`

Reads several notes in one call, for example the results of a search, each as
`joplin_read` gives it. Their texts share one budget of 40,000 characters:
short notes come whole, and what is left is split evenly over the longer ones.

| Argument | Type | Default | Allowed |
|---|---|---|---|
| `note_ids` | list of strings | required | 1 to 20 note ids, in any of the forms `joplin_read` takes |

Returns:

| Field | Type | |
|---|---|---|
| `notes` | list | One `joplin_read` result for each note found, in the order asked, each id once |
| `not_found` | list of strings | The ids that are missing, hidden by the notebook filter, or locked in Joplin, alike |
| `budget` | integer | The characters the bodies shared |

A note cut short has `next_offset` set; read the rest, or one section of it,
with `joplin_read`. `total_chars` is the length of the whole note.

## `joplin_list_notes`

Lists the notes in one notebook, a page at a time.

| Argument | Type | Default | Allowed |
|---|---|---|---|
| `notebook` | string | required | The notebook's exact title, or its id |
| `limit` | integer | `50` | 1 to 100 |
| `offset` | integer | `0` | 0 or more: where the page starts |
| `order` | string | `updated` | `updated` (newest first) or `title` (A to Z) |

Returns:

| Field | Type | |
|---|---|---|
| `notebook_id`, `notebook` | string | The notebook listed |
| `notes` | list | Each note's `id`, `title`, `updated_time` and `is_todo` |
| `total` | integer | How many notes the notebook holds |
| `next_offset` | integer or null | Where the next page starts; null on the last page |

Only the notebook's own notes are listed: [`joplin_overview`](#joplin_overview)
shows its sub-notebooks. Notes in the trash and conflict copies are left out,
and `total` doesn't count them. A title two notebooks share is refused with
their ids; use one of those. A hidden notebook gets the same answer as one that
doesn't exist: `No notebook has that title or id.`

## `joplin_recent`

Lists the notes changed in the last N days, newest first. "Changed" is Joplin's
own updated time, so a new note counts too.

| Argument | Type | Default | Allowed |
|---|---|---|---|
| `days` | integer | `7` | 1 to 3650 |
| `limit` | integer | `20` | 1 to 100 |

Returns a list of:

| Field | Type | |
|---|---|---|
| `id` | string | The note's id |
| `title` | string | Its title |
| `updated_time` | string | When it was last changed |

```json
[
  {
    "id": "a03be000b6aa4b0fa66b9d2070974c55",
    "title": "Call the electrician",
    "updated_time": "2026-11-12T17:09:49.545000+00:00"
  }
]
```

## `joplin_semantic_search`

Finds notes by meaning: a question about "getting the kitchen done" can find a
note that only says "cabinet quotes". It needs the optional `[semantic]` install
and an index of your notes; see [semantic-search.md](semantic-search.md).

| Argument | Type | Default | Allowed |
|---|---|---|---|
| `query` | string | required | Plain language |
| `limit` | integer | `10` | 1 to 50 |

Returns a list of notes, closest first, one entry per note:

| Field | Type | |
|---|---|---|
| `id` | string | The note's id |
| `title` | string | Its title |
| `excerpt` | string | The first 200 characters of the part of the note that matched best. Each part starts with the note's title |
| `score` | number | Closeness, above 0 and at most 1; higher is closer |
| `chunk_index` | integer | Which part of the note that was, counting from 0 |

```json
[
  {
    "id": "4be0a1c2d3e4f5061728394a5b6c7d8e",
    "title": "Cabinet quotes",
    "excerpt": "Cabinet quotes Three quotes so far. - Northside: 8,400 - Hale & Sons: 9,150, installed",
    "score": 0.62,
    "chunk_index": 0
  }
]
```

- Before it searches, it brings the index up to date with the notes that
  changed, unless you turn that off
  ([`SBJ_SEMANTIC_AUTO_SYNC`](configuration.md#semantic-search)). The first
  search after many edits is slower for that reason.
- Scores compare results within one search. They mean nothing across searches
  or across embedding models.
- At most two semantic searches run at a time; more wait their turn.
- Without the `[semantic]` install the tool is still listed, and answers with
  how to install it.

## `joplin_reindex`

Builds or refreshes the semantic index. It reads from Joplin and writes only the
index on the server's disk.

| Argument | Type | Default | |
|---|---|---|---|
| `full` | boolean | `false` | `false`: embed only the notes that changed since the last sync. `true`: throw the index away and embed every note again |

Returns:

| Field | Type | |
|---|---|---|
| `added` | integer | Notes whose text went into the index for the first time: new notes, and notes that had no text before |
| `updated` | integer | Notes in the index that changed: embedded again, or emptied (their text left the index) |
| `deleted` | integer | Notes dropped from the index (deleted in Joplin, or hidden by the filter) |
| `total` | integer | The notes visible in Joplin when the sync ran |
| `model` | string | The embedding model in use |

`added`, `updated` and `deleted` count notes with text. A note with no text has
nothing to embed: it is looked at once for each version of it and counted in
none of them, only in `total`.

```json
{ "added": 3, "updated": 1, "deleted": 0, "total": 158, "model": "BAAI/bge-small-en-v1.5" }
```

- The first build downloads the embedding model and embeds every note, which
  can take minutes. `second-brain-joplin reindex` on the command line does the
  same with no time limit.
- **Over HTTP, `full: true` is refused,** because a full rebuild can outlast a
  remote tool call. Run `second-brain-joplin reindex --full` on the server's
  machine instead ([Indexing](remote-access.md#indexing)).
- If Joplin isn't answering, the index is left as it was.

## `joplin_create_note`

Adds one new markdown note to a notebook. Adding a note is all it does: it can't
change, move or delete one (changing a note is
[`joplin_edit_note`](#joplin_edit_note)'s, with a setting of its own). It is
registered only when
[`SBJ_WRITE_NOTEBOOKS`](configuration.md#notebooks) is set, so on a server
without it a client can't see or call it.

| Argument | Type | Allowed |
|---|---|---|
| `title` | string | 1 to 300 characters. Line breaks and runs of spaces become single spaces |
| `body` | string | Up to 100,000 characters of markdown |
| `notebook` | string | The notebook's exact title, or its id |

Returns:

| Field | Type | |
|---|---|---|
| `id` | string | The new note's id |
| `title` | string | Its title as saved |
| `notebook_id` | string | The id of the notebook it went into |
| `notebook` | string | That notebook's title |
| `created_time` | string or null | When Joplin created it. Null for a repeat (`already_created: true`), and when Joplin's answer was lost or didn't say |
| `already_created` | boolean | `true` when this exact request made the note a moment ago and nothing new was written |
| `chars`, `body_sha256` | integer, string | The text's length and SHA-256, to check it without reading it back |

```json
{
  "id": "a03be000b6aa4b0fa66b9d2070974c55",
  "title": "Call the electrician",
  "notebook_id": "c7d1e5a09b3f4c2d8e6f1a0b9c8d7e6f",
  "notebook": "Inbox",
  "created_time": "2026-11-12T17:09:49.545000+00:00",
  "already_created": false
}
```

- **You approve each note in your client, when the client asks.** The call
  carries the notebook by name, the title and the text, so the approval prompt
  shows what will be written. Which clients ask, and when:
  [Who confirms each note](remote-access.md#who-confirms-each-note).
- **Only listed notebooks.** Any other notebook gets one answer, whether it is
  hidden, missing or just not listed:
  `A note can't be created in that notebook.`, followed by the notebooks that
  can receive notes. Name a notebook by its id when two writable notebooks share
  a title.
- **A repeat doesn't make a second note.** The same notebook, title and text
  sent again within ten minutes returns the note already made, with
  `already_created: true`.
- **When Joplin fails,** the answer tells three cases apart. A plain Joplin
  error means nothing was created. A normal result means the note exists, even
  if Joplin's own answer was lost on the way back. An error ending in "may or
  may not have been created" means it can't be known yet, and trying again in a
  moment is safe.

More in [Creating notes](remote-access.md#creating-notes).

## `joplin_create_notes`

Creates several new notes in one call: up to `SBJ_CREATE_NOTES_MAX` of them (at
most 20), holding up to 100,000 characters together. It exists only when
[`SBJ_CREATE_NOTES_MAX`](configuration.md#notebooks) is set as well as
`SBJ_WRITE_NOTEBOOKS`, and one approval, where the client asks, covers every
note in the call.

| Argument | Type | Default | Allowed |
|---|---|---|---|
| `notes` | list | required | Each `{"title": …, "body": …, "notebook": …}`, with the rules of `joplin_create_note` |

- **Every note is checked before any is written.** One bad notebook or title, or
  the same note twice, refuses the whole call, naming the note.
- **Notes are written in order.** If one fails, writing stops there, and the
  answer says which notes exist. Sending the same call again within ten minutes
  creates only the notes that are missing.
- A call in which nothing was created, and nothing is in doubt, fails with an
  error, as `joplin_create_note` does.

Returns `notes`, one entry for each note in the order given:

| Field | Type | |
|---|---|---|
| `title`, `notebook` | string | The note as asked for |
| `outcome` | string | `created`, `already_created` (a repeat found the note), `failed`, `unconfirmed` (it may or may not have been created) or `not_attempted` (writing stopped before it) |
| `id` | string or null | The note's id, wherever a note exists or may exist |
| `reason` | string or null | Why it failed, or why it can't be told |
| `chars`, `body_sha256` | integer, string | The text's length and checksum, to check a copy without reading it back |

## `joplin_edit_note`

Changes part of a note, or adds text to a section, to its start or to its end.
It exists when [`SBJ_EDIT_NOTEBOOKS`](configuration.md#notebooks) lists the
notebooks whose notes may change, and works only on notes there. Before every
change the server keeps a copy of the note
([what protects a change](remote-access.md#changing-notes)).

| Argument | Type | Default | Allowed |
|---|---|---|---|
| `note_id` | string | required | The note's id, or a link to it |
| `title` | string | required | The note's current title, exactly as `joplin_read` gave it |
| `expected_sha256` | string | required | `body_sha256` from the `joplin_read` the change is based on |
| `edits` | list or null | null | Up to 50 `{"old": …, "new": …}`: `old` copied exactly from the note, `new` what it becomes (empty removes it) |
| `append` | object or null | null | `{"text": …, "section": …}`: the text to add, and where: a heading's exact text or a path from the outline; no `section` means the end of the note |
| `prepend` | object or null | null | `{"text": …}`: the text to add at the very start |

At least one of `edits`, `append` and `prepend` is needed; together they may
hold up to 100,000 characters. The edits are applied in order, then `append`,
then `prepend`.

- **Each `old` must appear exactly once** in the text as it stands when its
  turn comes, copied exactly: spaces, punctuation and line breaks matter
  (overlapping matches count). An `old` may match text an earlier edit wrote.
- **If any part doesn't fit, nothing is written,** and the answer says which
  part and why: `Edit 2: its old text appears 3 times…`.
- **`append` with a `section`** goes at the end of that section, after its last
  line of text and before the next heading of the same or a higher level, so
  after its subsections. The rules for headings and sections are
  [`joplin_read`'s](#joplin_read): `#` headings outside code blocks only.
- **Line endings.** An `old` is looked for as written, and in the note's own
  line endings (Windows' `\r\n` or `\n`), so `\n` finds text in a `\r\n` note;
  across the two it must appear once. Inserted text takes the note's own line
  endings. Everything else in the note stays as it was, byte for byte.
- **A note with no title** is named by `title: ""`.
- The title, notebook, tags and creation date don't change.

Returns:

| Field | Type | |
|---|---|---|
| `id`, `title` | string | The note |
| `notebook_id`, `notebook` | string | Its notebook |
| `old_chars`, `new_chars` | integer | Its length before and after |
| `old_sha256`, `new_sha256` | string | The checksum of its text before and after: `new_sha256` is what to give as `expected_sha256` for the next change |
| `updated_time` | string or null | When Joplin saved the change |
| `edits_applied` | integer | How many `edits` were applied |
| `already_applied` | boolean | `true` when the same call was answered a moment ago and the note still holds its result: nothing was written this time |

```json
{
  "id": "4be0a1c2d3e4f5061728394a5b6c7d8e",
  "title": "Cabinet quotes",
  "notebook_id": "0b7e3f9a1c2d4e5f8a7b6c5d4e3f2a1b",
  "notebook": "Projects",
  "old_chars": 73,
  "new_chars": 112,
  "old_sha256": "9f2c…",
  "new_sha256": "41ab…",
  "updated_time": "2026-11-22T10:14:03.218000+00:00",
  "edits_applied": 1,
  "already_applied": false
}
```

To check a change without reading the note again, compare `new_sha256` with the
text you meant to leave.

## `joplin_replace_note`

Replaces the whole text of a note: for a short note rebuilt each time, such as
a brief or a page that says where things stand. For a long note, or a change to
part of one, [`joplin_edit_note`](#joplin_edit_note) is the better tool. It
exists only when `SBJ_REPLACE` is on as well as `SBJ_EDIT_NOTEBOOKS`, and runs
under the same rules ([what protects a change](remote-access.md#changing-notes)).

| Argument | Type | Default | Allowed |
|---|---|---|---|
| `note_id` | string | required | The note's id, or a link to it |
| `title` | string | required | The note's current title, exactly as `joplin_read` gave it |
| `body` | string | required | The whole new text, 1 to 100,000 characters, not only spaces |
| `expected_sha256` | string | required | `body_sha256` from the `joplin_read` the new text is based on |

Only the text changes: the title, notebook, tags and creation date stay. The new
text must keep every reference to the note's attachments (`:/<id>`). Returns
what [`joplin_edit_note`](#joplin_edit_note) returns, with `already_replaced`
in place of `edits_applied`: `true` when the note already holds exactly this
text, in which case nothing is written, whatever `expected_sha256` says.

## `joplin_changes`

Lists the changes the server made to notes, newest first. It exists when
`SBJ_EDIT_NOTEBOOKS` is set, with the tools that change notes.

| Argument | Type | Default | Allowed |
|---|---|---|---|
| `days` | integer | `7` | 1 to 90 |
| `limit` | integer | `50` | 1 to 200 |

Returns a list:

| Field | Type | |
|---|---|---|
| `at` | string | When |
| `tool` | string | Which tool (`restore` for a restore on the server's machine) |
| `note_id` | string | The note |
| `title` | string or null | The note's title now; null when the note is gone |
| `old_chars`, `new_chars` | integer | Its length before and after |
| `outcome` | string | `applied`, `failed` (nothing changed), `unconfirmed` (it can't be told yet) or `sent` |

It never holds note text. A note the notebook filter hides now is left out,
and so is a deleted note whose notebook is hidden.

## With a notebook filter

[`SBJ_NOTEBOOKS` and `SBJ_EXCLUDE_NOTEBOOKS`](configuration.md#notebooks) limit
which notebooks every tool can see:

| Tool | With a filter |
|---|---|
| `joplin_overview` | Lists visible notebooks only. A visible notebook under a hidden one moves to the top of the tree |
| `joplin_search` | Leaves hidden notes out, and keeps reading until `limit` is filled or 20 pages are read. `notebook:` and `-notebook:` match visible notebooks only: a hidden notebook answers like one that doesn't exist, even when its sub-notebook is visible. The server reads the query as Joplin does (a term starts after a space or a closing quote); a `notebook:` it can't read as a term of its own, such as one inside a phrase, refuses the search |
| `joplin_recent` | Leaves hidden notes out, and keeps reading until `limit` is filled |
| `joplin_list_notes` | Answers for a hidden notebook exactly as for one that doesn't exist |
| `joplin_changes` | Leaves out changes to notes the filter hides now |
| `joplin_read`, `joplin_read_notes` | Answer for a hidden note exactly as for one that doesn't exist, a locked one included. `notebook_path` starts at the topmost visible notebook, so a hidden parent's name never appears. `attachments` are the ones the note's own text refers to |
| `joplin_semantic_search` | Returns only notes that are visible at that moment |
| `joplin_reindex` | Indexes visible notes only, and drops notes that became hidden |
| `joplin_create_note` | Can't write to a hidden notebook, whatever `SBJ_WRITE_NOTEBOOKS` lists |

The full rules are in
[Choosing which notebooks are exposed](remote-access.md#choosing-which-notebooks-are-exposed).

## Errors

A tool that can't do what was asked returns an error with a sentence the
assistant can pass on. These are the main ones the server writes itself; each
says what was wrong, and [troubleshooting.md](troubleshooting.md) covers the
ones that need you.

| The error says | It means |
|---|---|
| `Joplin is unreachable at <address> — is Joplin Desktop running with the Web Clipper service enabled?` | Nothing answered at Joplin's address |
| `Joplin didn't answer at <address> within 10 seconds — is Joplin Desktop running and responsive?` | Joplin didn't answer in time (the number is [`SBJ_JOPLIN_TIMEOUT`](configuration.md#joplin)). For 30 seconds after Joplin was found not answering, calls get this message at once, without asking Joplin |
| `Joplin rejected the request — check that JOPLIN_API_TOKEN is set and matches the token in Joplin's Web Clipper settings.` | The token is missing or wrong |
| `The requested Joplin resource was not found.` | No note has that id, or the filter hides it |
| `Joplin returned an unexpected response (HTTP <status>).` | Joplin answered with an error of its own |
| `The server's notebook settings (…) don't match Joplin's notebooks, so this is refused until they are fixed. …` | A notebook named in `SBJ_NOTEBOOKS`, `SBJ_EXCLUDE_NOTEBOOKS` or `SBJ_WRITE_NOTEBOOKS` was renamed or deleted. The client isn't told which one |
| `Semantic search needs the optional '<package>' dependency. …` | The `[semantic]` install is missing |
| `Unknown SBJ_EMBEDDING_BACKEND …` / `Unknown SBJ_VECTOR_STORE …` | A semantic setting has a value the server doesn't know |
| `A full rebuild can take longer than a remote tool call may run. …` | `joplin_reindex` with `full: true` over HTTP |
| `A note can't be created in that notebook. Notebooks that can receive notes: …` | `joplin_create_note` was given a notebook that isn't on the list |
| `Note creation is off on this server: its operator hasn't listed any notebook that may receive notes (SBJ_WRITE_NOTEBOOKS).` | `SBJ_WRITE_NOTEBOOKS` is set, and no notebook it covers is visible |
| `A note needs a title.` | The title was only spaces |
| `The requested Joplin resource was not found.` (from a change tool) | No note has that id, or the filter hides it |
| `This note's notebook isn't one whose notes can be changed. Those are: …` | The note is outside `SBJ_EDIT_NOTEBOOKS` |
| `The title doesn't match the note's current title, so nothing was changed. …` | Read the note again and give its title as it is |
| `The note has changed since it was read (its text's checksum is different), so nothing was changed. …` | Read it again and make the change on what it says now |
| `Edit N: its old text isn't in the note …` / `Edit N: its old text appears K times …` | That edit's `old` must be copied exactly and found once |
| `This note is in Joplin's trash …`, `… is a conflict copy …`, `… hasn't been decrypted …`, `… isn't Markdown …` | Notes no tool changes |
| `second-brain-joplin couldn't complete this request. The server's log says where.` | A failure nobody planned for (a bug). The log names its class and place, never its text ([troubleshooting.md](troubleshooting.md#a-tool-answers-with-an-error)) |
| `The search index in … belongs to …. Give each server an index directory of its own (SBJ_INDEX_DIR).` | Another Second Brain server keeps its index in that directory |
| `The change would remove N reference(s) to the note's attachments …` | Keep every `:/<id>` of an attachment |
| `The change would leave the note as it is, so nothing was written.` | The edit's `old` and `new` are the same |
| `A note's text can't be replaced with nothing.` | `joplin_replace_note` with a text of only spaces or line breaks (an empty one is refused before that) |
| `With a notebook filter set, notebook: must be a search word of its own, with a space before it.` | A search whose `notebook:` the server can't read as a term (inside a phrase, say) while a filter is set |
| `This note is locked in Joplin, so its text can't be read or changed through Joplin's API. …` | Joplin's note lock protects it; unlock it in Joplin |
| `The server's limit of N changes a day is reached …` | `SBJ_CHANGES_PER_DAY`; it says when it resets |
| `The server couldn't keep a copy of the note before changing it, so nothing was changed. …` | The journal folder can't be written; the server's log says why |
| `… The change may or may not have been made: read the note to see …` | Joplin took the request and didn't answer; a repeat won't change it twice |
| `… The note may or may not have been created: look in Joplin, or try again in a moment (a repeat of this request won't make a second copy).` | Joplin took the request and didn't answer |
| `Rate limit exceeded for client: <id>` | HTTP mode: a burst of more than half [`SBJ_RATE_LIMIT_PER_MINUTE`](configuration.md#http-mode), or more than that many calls a minute kept up. The call is refused before any tool runs |

An argument outside its range is refused with a validation message that names
the argument and the limit, for example
`limit … Input should be less than or equal to 100`.

## What the tools tell the client about themselves

Every tool carries MCP annotations, which a client can use to decide when to ask
you first:

| Tool | `readOnlyHint` | `destructiveHint` | `idempotentHint` | `openWorldHint` |
|---|---|---|---|---|
| The six read tools, `joplin_semantic_search` and `joplin_changes` | `true` | | | `false` |
| `joplin_reindex` | `false` | `false` | `true` | `false` |
| `joplin_create_note`, `joplin_create_notes` | `false` | `false` | `false` | `false` |
| `joplin_edit_note` | `false` | `true` | `false` | `false` |
| `joplin_replace_note` | `false` | `true` | `true` | `false` |

Every write tool (`joplin_create_note`, `joplin_create_notes`,
`joplin_edit_note`, `joplin_replace_note`) also carries
`"anthropic/requiresUserInteraction": true` in its metadata, unless the server runs with
[`SBJ_ASK_EVERY_TIME=false`](configuration.md#notebooks). Claude Code reads that
as: ask on every call, with no "don't ask again", and refuse the call where
nobody can be asked. The two other Claude clients tested act on it too, and not
in the same way: [Who confirms each note](remote-access.md#who-confirms-each-note)
says what each one did.
