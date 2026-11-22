# Remote access

`second-brain-joplin` normally runs over **stdio**, started by an MCP client on the
same machine as Joplin Desktop. Claude's **custom connectors** (claude.ai, Claude
Desktop, the mobile apps, Cowork, Claude Code) work differently: they call an MCP
server over HTTPS **from Anthropic's cloud**, not from your device. HTTP mode lets
this server be one of those connectors.

```
Claude (Anthropic's cloud)
        │  HTTPS, OAuth bearer token
        ▼
your tunnel or reverse proxy (public URL, TLS)
        │
        ▼
second-brain-joplin serve --transport http   (127.0.0.1:8775)
        │  HTTP + Joplin token, never leaves the machine
        ▼
Joplin Desktop (localhost:41184)
```

The server stays bound to loopback. Something else publishes it: a Cloudflare
Tunnel, Tailscale Funnel, or a reverse proxy with TLS. Joplin's own token never
leaves the machine.

## Running in HTTP mode

```bash
second-brain-joplin serve --transport http --public-url https://joplin.example.com
```

Every option falls back to an environment variable, then to its default:

| Flag | Env var | Default | Notes |
|---|---|---|---|
| `--transport {stdio,http}` | `SBJ_TRANSPORT` | `stdio` | stdio setups are unchanged |
| `--host` | `SBJ_HOST` | `127.0.0.1` | Loopback; the tunnel reaches it locally |
| `--port` | `SBJ_PORT` | `8775` | |
| `--path` | `SBJ_HTTP_PATH` | `/mcp` | The MCP endpoint |
| `--public-url` | `SBJ_PUBLIC_URL` | none | The server's public origin (`https://host`, no path); sign-in needs it |
| `--env-file PATH` | | none | Loads `KEY=value` lines first; variables already set to a value win. For launchd and other service managers that can't load an env file |
| `--no-auth` | | off | No sign-in, for a local test only (below) |

The HTTP-only flags are refused with `--transport stdio`, so a typo can't
silently run the wrong mode.

### How it behaves

- **Stateless, plain JSON.** Each request stands alone and gets a JSON response.
  A restart never breaks Claude's session, and there is no long-lived event stream
  for a proxy to cut (Cloudflare drops a response that sends nothing for 125
  seconds). None of the tools sends server-initiated messages, which is all a
  stateful session would add.
- **Host check.** A request whose `Host` header isn't a loopback name or the
  public URL's host is answered `421` before it reaches MCP. This blocks DNS
  rebinding against the loopback port.
- **`/healthz`** answers `{"server": true, "joplin": true}` with `200`, or
  `"joplin": false` with `503` when Joplin Desktop isn't answering. It carries no
  note data, version or configuration, because it is never behind sign-in.
  Keep it off the public internet anyway (see your tunnel's firewall rules).

### Start-up refusals

HTTP mode refuses to start rather than expose your notes by mistake:

- **No sign-in configured** and no `--no-auth`: refused.
- **`--no-auth`** is accepted only on a loopback address and without a public URL.
  A tunnel reaches a loopback server just as well as a public one, so loopback alone
  is not a reason to skip sign-in.
- A public URL that isn't `https://host` (a path, a query, plain `http` off
  loopback) is refused, as is a port outside 1 to 65535 or a path without a
  leading `/`.

For a quick local test with no sign-in:

```bash
second-brain-joplin serve --transport http --no-auth
curl -s http://127.0.0.1:8775/healthz
```

## Sign-in

Claude's custom connectors sign in with OAuth. HTTP mode uses FastMCP's OAuth
proxy with **GitHub** as the identity provider: the proxy offers Claude the
client registration it expects (Dynamic Client Registration, or a Client ID
Metadata Document), sends you to GitHub to sign in, and then hands Claude a token
of its own. GitHub sign-in alone would admit any GitHub account, so the server
also checks every request against a list of **numeric GitHub user ids**. Ids, not
logins: a login can be renamed and then registered by someone else.

### Set it up

1. **Create a GitHub OAuth App** on your own account (GitHub → Settings →
   Developer settings → OAuth Apps → New OAuth App):
   - Homepage URL: your public URL, e.g. `https://joplin.example.com`
   - Authorization callback URL: `https://joplin.example.com/auth/callback`
   Then generate a client secret.
2. **Find your GitHub id:** `curl -s https://api.github.com/users/<login> | jq .id`.
3. **Set all four variables** (an env file passed with `--env-file`, mode 600, is
   a good home for them):

   | Variable | What |
   |---|---|
   | `SBJ_GITHUB_CLIENT_ID` | The OAuth App's client id |
   | `SBJ_GITHUB_CLIENT_SECRET` | The OAuth App's client secret |
   | `SBJ_JWT_SIGNING_KEY` | 32+ random characters: `openssl rand -base64 32` |
   | `SBJ_ALLOWED_GITHUB_IDS` | Your numeric id; several, comma-separated |

   Optional: `SBJ_OAUTH_REDIRECT_URIS`, the OAuth clients allowed to register
   (default: claude.ai's callback only, `https://claude.ai/api/mcp/auth_callback`).
4. **Start the server** with `--public-url` (or `SBJ_PUBLIC_URL`); sign-in needs it.

Set all four or none: some but not all is refused at start-up, naming what's
missing (never a value).

### What it does

- **Anyone not listed gets 401** before any MCP message is read. A second check
  runs for every tool, so a mistake in one layer doesn't open the tools.
- **Scope `read:user`:** the server only ever learns your public profile.
  (FastMCP's default, `user`, could also write it.)
- **GitHub is asked once per five minutes per token**, not on every request.
- **Registered clients survive restarts.** FastMCP keeps them in an encrypted
  store on disk (under your user data directory), keyed from
  `SBJ_JWT_SIGNING_KEY`. Rotating the GitHub client secret changes nothing for
  Claude; rotating the signing key signs every client out.
- **Consent page:** after GitHub, the server asks you to approve the client, every
  time. **Approve only a sign-in you started yourself.** claude.ai's callback is
  the same for every claude.ai user, so an authorize link someone else sends you
  could connect your notes to *their* Claude account if you approve it.

### Turning access off

The token Claude holds lasts a year (GitHub OAuth Apps issue no refresh token).
To cut access at once, do any one of:

- revoke the grant at GitHub: Settings → Applications → Authorized OAuth Apps;
- rotate `SBJ_JWT_SIGNING_KEY` and restart: every issued token stops working;
- stop the tunnel, or the server.

## What a remote client can do

Unless you turn note creation on ([Creating notes](#creating-notes)) or
changing notes ([Changing notes](#changing-notes)), every tool is read-only
toward Joplin, the same over HTTP as over stdio:

- **No write tool is registered** until you list where notes may go or which
  notes may change. A client can't call, or even see, a tool that isn't there.
- **Annotations** mark every tool read-only, except `joplin_reindex`, which
  updates the local search index only (never Joplin, never destructive),
  `joplin_create_note` and `joplin_create_notes`, which are marked as writes
  that add and never destroy, and `joplin_edit_note` and `joplin_replace_note`,
  which are marked destructive, because a change can remove text.
- **Bounded arguments:** `limit` 1–100 for the keyword search, 1–50 for search
  by meaning, 1–100 for `joplin_recent` (`days` 1–3650) and `joplin_list_notes`,
  1–20 ids for `joplin_read_notes`, and for `joplin_changes` `days` 1–90 and
  `limit` 1–200. Anything outside is refused before Joplin is called.
- **Long notes are read in pages.** Claude caps a tool result near 150,000
  characters, so `joplin_read` returns at most `max_chars` (default 50,000, up to
  100,000) from `offset`, with `total_chars` and `next_offset` for the rest.
- **Only the notebooks you choose**, if you set a notebook filter (next section).
  Without one, every notebook is readable.

## Choosing which notebooks are exposed

By default every notebook is readable. Over HTTP, whatever Claude reads travels
to Anthropic, so decide which notebooks that should cover. Two variables set a
filter. They apply to every tool, on stdio as well as HTTP:

| Variable | Effect |
|---|---|
| `SBJ_NOTEBOOKS` | Only these notebooks are visible |
| `SBJ_EXCLUDE_NOTEBOOKS` | These notebooks are hidden |

Each is a comma-separated list of notebook ids or titles, and an entry covers the
notebook and every notebook under it. Hiding wins: with both set, a notebook is
visible when the first list covers it and the second doesn't. Prefer ids. An id
survives a rename; a title has to match exactly and belong to one notebook only.

To find the ids, and to check a filter before a client sees through it:

```bash
second-brain-joplin notebooks
```

```
Notebook filter: 3 of 4 notebooks visible.
Note creation: allowed in the 1 marked writable.
Changing notes: off (SBJ_EDIT_NOTEBOOKS is not set).
writable  c7d1e5a09b3f4c2d8e6f1a0b9c8d7e6f  Inbox (4)
hidden    5a1f0c2e9b7d4f63a8e1c0d2b4f6a7c9  Journal (212)
visible   9c40d1e7a3b24f58b6c9e0f1a2d3b4c5  Work (57)
visible   0b7e3f9a1c2d4e5f8a7b6c5d4e3f2a1b    Projects (31)
```

It lists every notebook with its id and note count (hidden ones too: this is
your own terminal), marked under the settings it was run with: `hidden`,
`visible`, `writable` where a note may also be created
([Creating notes](#creating-notes)), `editable` where notes may be changed
([Changing notes](#changing-notes)), or `writable+editable`. Add `--env-file` to read the settings from
the server's env file.

What a filter changes:

- **`joplin_overview`** lists visible notebooks only. A visible notebook under a
  hidden one moves to the top of the tree, so the hidden parent's name isn't shown.
- **`joplin_list_notes`** answers for a hidden notebook exactly as for one that
  doesn't exist.
- **`joplin_search` and `joplin_recent`** leave hidden notes out, and keep reading
  Joplin's results until `limit` is filled (a search, for 20 pages at most). A
  search's `notebook:` and `-notebook:` words match visible notebooks only, so a
  hidden notebook answers exactly as one that doesn't exist, even when a
  sub-notebook of it is visible.
- **`joplin_read` and `joplin_read_notes`** answer for a hidden note exactly as
  for a note that doesn't exist. A note's notebook path starts at the topmost
  visible notebook, so a hidden parent's name isn't shown. A note's attachments
  are the ones its own text refers to: leaving out one that a hidden note also
  uses would show that it does. A locked note that the filter hides answers as
  missing too, though Joplin refuses its text with "locked".
- **Semantic search** never indexes a hidden note, and drops one that becomes
  hidden (it moved, its notebook moved, or you changed the filter) at the next
  sync. What a search returns doesn't rest on that, though: every search asks
  Joplin which notes are visible now and returns only those, whatever the index
  file holds.
- **It follows Joplin as it changes.** The filter is checked against Joplin's
  notebooks on every call: a new sub-notebook of a visible notebook is visible at
  once, and a note moved into a hidden notebook is gone at the next call.

A filter that doesn't match Joplin's notebooks never falls back to showing
everything:

- **At start-up**, `serve` refuses to start, naming the entry: an id no notebook
  has, a title no notebook has, a title two notebooks share, or a notebook one
  list shows and the other hides. If Joplin isn't answering at that moment there
  is nothing to check against, so the server starts and checks on every call.
- **A variable that is set but lists nothing** (`SBJ_NOTEBOOKS=`) is refused
  too. "Only these: none" and "no filter" are opposite readings of it, and the
  second would show everything. Remove the variable to have no filter.
- **With no filter set**, HTTP mode says so in its log at start-up ("no notebook
  filter is set: every notebook is readable"), so a misspelt variable name
  doesn't pass for a filter.
- **While running** (you renamed or deleted a notebook the filter names), every
  tool refuses with a message saying the filter needs fixing. The client isn't
  told which entry, since that can be a hidden notebook's title. The server log
  and `second-brain-joplin notebooks` say.

What it doesn't do:

- It filters by notebook, not by content. A visible note that quotes or links a
  hidden one still shows what it says itself.
- A note that is in no notebook is hidden once a filter is set. With only
  `SBJ_EXCLUDE_NOTEBOOKS` set, so is a notebook whose parents can't be traced
  to the top (its parent is missing from Joplin's list): it might sit under a
  hidden one.
- A search whose matches are mostly in hidden notebooks reads through them
  before it answers, so it takes longer than the same search without a filter.
- The settings are read when the server starts, so restart it after changing
  them.
- **The index file is shared** by everything that runs on the machine with the
  same `SBJ_INDEX_DIR`. `second-brain-joplin reindex` run without the server's
  settings has no filter and writes every note into it. Searches still return
  only visible notes, and the server's next sync removes the rest, but to keep
  hidden text out of the file run it as
  `second-brain-joplin reindex --env-file <the server's env file>`. The command
  says whether a filter was set.

## Creating notes

The server is read-only until you say where notes may go:

| Variable | Effect |
|---|---|
| `SBJ_WRITE_NOTEBOOKS` | Notes may be created in these notebooks: ids or titles, comma-separated, each with its sub-notebooks. `*` means every visible notebook |

With it set, one more tool appears: `joplin_create_note(title, body, notebook)`.
It adds one new markdown note to a notebook on the list, named by its exact
title or its id. That is all it can do. It can't change, move or delete a note
(changing one takes `SBJ_EDIT_NOTEBOOKS`: [Changing notes](#changing-notes)),
and it can't write to a notebook the filter hides. Without the variable the tool
isn't registered at all.

With `SBJ_CREATE_NOTES_MAX` set too, `joplin_create_notes` creates up to that
many notes (20 at most, and 100,000 characters in all) in one call. One approval
then covers every note in the call, so set it only if that is what you want.
Every note is checked before any is written; writing stops at the first one
that fails, and the answer says which exist.

A notebook made for the purpose is a good first setting, for example
`SBJ_WRITE_NOTEBOOKS=Inbox`. `second-brain-joplin notebooks` marks the notebooks
that can receive notes `writable`.

### Who confirms each note

You do, in your client, when the client asks. The server can't put its own
question to you on every client (claude.ai's connector documentation doesn't
list that part of MCP as supported), so the check on each note is the client's
approval prompt for the call (with `joplin_create_notes`, one prompt covers all
the notes of a call). That is why the call
carries the whole note in readable form: the notebook by name, the title, the
text. What you approve is what gets written.

Whether a client asks is up to the client. The server does what it can to make
Claude's clients ask: it marks every write tool (`joplin_create_note`,
`joplin_create_notes`, `joplin_edit_note` and `joplin_replace_note`) with
`"anthropic/requiresUserInteraction": true`, which Claude Code documents as the
way for a tool to require approval on every call. The table shows what each
client did when tested in November 2026, with that mark and with the mark left
off ([below](#a-client-whose-prompt-cant-be-answered)). In Claude's apps the
tool was on "Needs approval" throughout.

| Client | With the mark, which is the default | With `SBJ_ASK_EVERY_TIME=false` |
|---|---|---|
| Claude Code | Asks on every call, with no "don't ask again", whatever allow rules are in force. Where it can't put the prompt to a person (`dontAsk` mode, a run with `--permission-prompt-tool`) it refuses the call | Not tested. Its ordinary permission rules apply |
| A chat on claude.ai | Asked before each note, in Manual and in Auto mode. Not tested with "Always allow" | Not tested |
| Claude Duet | Shows a prompt with no way to approve it, so no note can be created | Created the note without asking, on the web and in the phone app |
| Any other client | Not known. Find out how yours treats a tool that isn't read-only before you turn note creation on | Not known |

The table was made with `joplin_create_note`. `joplin_edit_note` carries the
same mark under the same setting; expect the same, and check it with your
client before relying on it.

**"Needs approval" is not that check.** Claude's apps give each connector tool
a permission, under **Customize → Connectors**, on the connector's
**Permissions** tab. "Needs approval" makes Claude ask in Manual mode. Of Auto
mode, which Anthropic's help describes as rolling out as the default on most
plans, the same page says that "tools set to 'Needs approval' can run without
asking you"
([Choose how Claude checks with you](https://support.claude.com/en/articles/13345190)).
The permission that asks before every use in every mode is "Always ask", where
your account has it. "Always allow" means Claude never asks.

#### A client whose prompt can't be answered

Set `SBJ_ASK_EVERY_TIME=false` and the server leaves the mark off. It is there
for a client such as Claude Duet, which can't create a note while the tool
carries the mark. Know what you give up:

- **It applies to every client of that server,** not only the one you did it
  for.
- **Without the mark, a client treats the write tools like any other tool
  that writes.** Claude Code follows its ordinary permission rules, so an allow
  rule or "don't ask again" stops it asking. Claude Duet created notes without
  asking at all when tested. Claude's apps in Auto mode can do the same, unless
  the tool is set to "Always ask".
- **What the server enforces stays as it is** ([below](#what-the-server-enforces)):
  the listed notebooks only, the size limits, a record of every note, and no
  second copy from a repeat. A note you didn't ask for can land in a notebook
  you listed, and with `SBJ_EDIT_NOTEBOOKS` a note there can be changed without
  asking you (a copy is still kept first). Nothing is moved or deleted.

Only `0`, `false`, `no` or `off` turns it off. A value that is neither a yes nor
a no stops the server from starting, so a typo can't pass for either. With note
creation on, HTTP mode says in its log at start-up that the setting is off, and
`second-brain-joplin notebooks` says so too.

**The change counts once Claude has read the tool list again.** Restart the
server, then refresh the connector's tool list in Claude: **Refresh tools
list** in the connector's three-dot menu
([A new tool or setting doesn't show up in Claude](troubleshooting.md#http-mode)).
A new chat is not enough: after the restart, new Duet chats went on showing the
prompt that can't be answered until the list was refreshed. Only that direction
was tested. Assume the same in the other: when you take the setting out again,
refresh the list and check that the prompt is back.

### What the server enforces

- **Where.** Only the listed notebooks, and never a hidden one. A request for any
  other notebook gets one answer, whether that notebook is hidden, doesn't exist
  or just isn't listed, followed by the notebooks that can receive notes.
- **How much.** A title of up to 300 characters, a body of up to 100,000.
- **A record.** Each attempt is logged with the note's id, its notebook's id, the
  caller and the client, before the request goes out and again with its outcome
  (`created`, `create failed`, or `create unconfirmed`). Never the title or text:

  ```
  2026-11-12 16:40:02,113 INFO second_brain_joplin.audit: note create sent id=4be0…a1 notebook=c7d1…6f caller=65598 client=Claude
  2026-11-12 16:40:02,131 INFO second_brain_joplin.audit: note created id=4be0…a1 notebook=c7d1…6f caller=65598 client=Claude
  ```
- **An honest answer when Joplin fails.** The tool chooses the note's id itself,
  so it can look afterwards. One of three things is reported: nothing was
  created (Joplin was never reached, or answered no); the note was created (even
  if Joplin's answer got lost on the way back); or it can't be known yet (the
  request went out, no answer came, and the note isn't there so far).
- **No second copy from a repeat.** Requests get repeated: a client that timed
  out, Claude trying once more. For ten minutes the tool remembers which note it
  sent, and the same request again (same notebook, title and text) returns that
  note with `already_created: true` instead of making another.

A wrong `SBJ_WRITE_NOTEBOOKS` (a title no notebook has, a notebook the filter
hides) stops `serve` from starting, as a wrong filter does. If it stops matching
later, `joplin_create_note` refuses and the read tools carry on.

Notes hold text from anywhere: a clipped web page, a pasted email. Text written
to steer an AI could ask it to write more notes. The approval prompt is where you
catch that, which is the reason to use a client that asks, and to leave
`SBJ_ASK_EVERY_TIME` alone unless you need it off.

## Changing notes

The tools that change a note (`joplin_edit_note`, and with `SBJ_REPLACE` the one
that rewrites a whole note) exist only when `SBJ_EDIT_NOTEBOOKS` lists the
notebooks whose notes may change. `SBJ_WRITE_NOTEBOOKS` alone never allows a
change: it has always meant "a new note, and nothing else". A client may have
no prompt at all (an always-on agent, or `SBJ_ASK_EVERY_TIME=false`), so the
server's own rules are what make a change safe:

- **Where.** Only notes in a notebook on the edit list, and its sub-notebooks,
  and never one the filter hides. A hidden note gets the answer a missing one
  gets, before any other check can answer differently.
- **Which version.** The call carries the checksum of the text it read
  (`body_sha256` from `joplin_read`). If the note changed since, nothing is
  written, so an edit you made in Joplin is never overwritten unseen.
- **Which note.** The call names the note's current title as well as its id,
  so a prompt, where the client shows one, says which note will change.
- **Which notes never.** A note in the trash, a conflict copy, an encrypted
  note and one that isn't Markdown (a clipped web page) are refused. So is a
  change that would remove a reference (`:/<id>`) to one of the note's
  attachments: Joplin deletes an attachment no note refers to.
- **A copy first.** Before anything is sent to Joplin, the note's title, text
  and notebook are saved on the server's disk
  ([`SBJ_UNDO_DIR`](configuration.md#changing-notes), 90 days). If that copy
  can't be written, nothing is changed. `second-brain-joplin restore` puts a
  copy back ([the command](configuration.md#second-brain-joplin-restore)).
- **A daily limit.** `SBJ_CHANGES_PER_DAY` (100) changes a day, all tools and
  restores together, counted from the copies so a restart doesn't reset it. A
  change counts unless it is known to have failed; new notes don't count. It
  resets at the server's local midnight.
- **Only what it means to change.** Joplin's API changes whatever properties a
  request carries, so each request carries one: the text, or the title.
- **One at a time.** Two changes to one note run one after the other, and the
  second sees what the first did.
- **No second change from a repeat.** The same call again within ten minutes,
  while the note still holds what the first left, gets the first answer with
  `already_applied: true`.
- **An honest answer when Joplin fails.** If Joplin's answer is lost, the note
  is read again: the change arrived, it didn't, or it can't be told yet.
- **A record.** Each change is logged with the tool, the note's id, its
  notebook's id, the caller and the client, before it goes out and with its outcome. Never
  the title or text. `joplin_changes` lists what was changed, with no text.

Joplin's own note history isn't relied on for undoing a change: a change made
through Joplin's API reaches it only every few minutes, and the text from before
a change is kept as an entry of its own only when the note had been left alone
for a while. (Checked on Joplin 3.7.24 with
[the live write checks](../CONTRIBUTING.md#live-write-checks): a change made
through the API did get one history entry within twelve minutes.)

What the live write checks found on Joplin 3.7.24, and the server relies on:

- Joplin's `PUT /notes/:id` changes exactly the properties it is sent, and
  applies any it is sent (an extra `author` was saved). That is why each request
  carries one property and nothing else. The note's title, notebook, author,
  source and creation times were the same after an edit; `updated_time` and
  `user_updated_time` moved.
- `DELETE /notes/:id` without `permanent=1` moves a note to the trash, where it
  keeps its id and gets a `deleted_time`; a change to it is refused.
- A note created through the API with `is_conflict` set is not a conflict copy:
  Joplin drops the flag. Conflict copies come only from sync.
- Not checked, because it needs a second device or a person: a change made
  while the server's Joplin is behind another device (expected: one version
  goes to Joplin's Conflicts notebook after the tool has said it worked), and an
  editor open on the note when it changes.

## Limits and logs

- **Rate limit:** `SBJ_RATE_LIMIT_PER_MINUTE` tool calls per minute per GitHub
  account (default 60, 0 turns it off), as a token bucket that allows bursts of
  half that. Listing tools and protocol requests aren't counted. Over the limit,
  the call is refused with "Rate limit exceeded".
- **Semantic search** runs at most two queries at a time, so a burst can't tie
  up the embedding model.
- **Logs** go to stderr, one line per tool call: the tool, the caller's GitHub
  id, the client, the outcome and the time taken. A failed call names what failed
  by its class name, such as `outcome=error:JoplinNotFoundError` or
  `outcome=error:JoplinUnavailableError` (Joplin not answering):

  ```
  2026-11-11 16:40:02,113 INFO second_brain_joplin.audit: tool=joplin_search caller=65598 client=Claude outcome=ok ms=41
  ```

  **`client=` is what the client says about itself, and proves nothing.** It is
  the name the client registered under when it signed in, else the name it gives
  in the MCP handshake (or in each request, on protocols without one), else the
  first word of its `User-Agent`, else `unknown`. It keeps letters, digits and
  `._/-` only, at most 40 characters, so it can't break or forge a line. It tells
  your clients apart in the log (a desktop app, a phone, a coding agent); the
  server decides nothing by it.

  Never the arguments (a search query is note content, and so is a new note's
  text), never results, never tokens. Four loggers that would leak are held back
  in HTTP mode: httpx's request log (the Joplin token travels in the URL's query
  string, the only way Joplin's API accepts it), uvicorn's access log (the
  sign-in callback URL carries the OAuth code), FastMCP's debug log (it prints
  each call's arguments, so it stays at INFO even if you ask for DEBUG), and
  FastMCP's "Error calling tool" line, which loses its traceback (that carries the
  exception's text).
- **A failure nobody planned for** gets one fixed sentence ("second-brain-joplin
  couldn't complete this request. The server's log says where."), and the log
  gets the exception's class and the file and line where it surfaced, never its
  text: `unexpected KeyError in joplin_read at tools/read.py:312 in joplin_read`.

## Indexing

A remote tool call has to finish quickly: claude.ai stops waiting after 240
seconds, and Cloudflare drops a response that sends nothing for 125. A first
full index of a large vault (model download plus every note) can take longer, so
indexing stays out of the request path:

- **Build it first, on the server's machine:**
  `second-brain-joplin reindex --env-file <the server's env file>` (add `--full`
  to rebuild from scratch). It has no time limit. The env file matters if you
  set a notebook filter: without it the command indexes every note.
- **Background sync:** in HTTP mode the server syncs once at start-up and then
  every `SBJ_SYNC_INTERVAL_MINUTES` (default 15, 0 turns it off). Runs never
  overlap, a failed run is logged and the next one tries again, and if semantic
  search isn't installed the loop stops quietly.
- **Over HTTP**, `joplin_reindex(full=True)` is refused with a pointer to the
  command above; incremental syncs (`full=false`) still work.

## Publishing it

The server only listens on loopback; something with TLS has to publish it at
the public URL. Three ways that work:

- **Cloudflare Tunnel** (recommended). `cloudflared` on the server's machine
  dials out, so no port is opened. Point a public hostname at
  `http://127.0.0.1:8775`, and run `cloudflared tunnel run --token-file <file>`
  as a service. Don't put Cloudflare Access in front: Claude is a machine client
  and can't complete its login. Don't use Quick Tunnels: their hostname changes
  on every start. On the Free plan, turn **Bot Fight Mode off** for the zone: it
  challenges automated clients, which Claude's calls are, and no rule can exempt
  them.
- **Tailscale Funnel**: `tailscale funnel --bg 8775` publishes it at your
  machine's `ts.net` name with a certificate, no domain needed. No firewall in
  front, and Funnel is beta.
- **A reverse proxy** (Caddy, nginx) on a host with a public address and TLS.

### A firewall in front (recommended)

Claude's requests come from Anthropic's published outbound ranges,
`160.79.104.0/21` and `153.61.208.0/20`
([Anthropic's IP addresses](https://platform.claude.com/docs/en/api/ip-addresses)).
Only three paths have to be reachable from anywhere, because your own browser
opens them while you sign in: `/authorize`, `/auth/callback` and `/consent`.
A rule that blocks everything else unless it comes from those two ranges keeps
`/mcp`, `/token`, `/register`, `/.well-known/*` and `/healthz` closed to the rest
of the internet. As a Cloudflare custom rule (action Block):

```
(http.host eq "joplin.example.com")
and not (http.request.uri.path in {"/authorize" "/auth/callback" "/consent"})
and not (ip.src in {160.79.104.0/21 153.61.208.0/20})
```

### Keeping it running

Run it under your service manager, with the secrets in a mode-600 env file.
A user LaunchAgent on macOS (`~/Library/LaunchAgents/example.second-brain-joplin.plist`;
Joplin Desktop runs in your login session, so a per-user agent is the right fit):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>example.second-brain-joplin</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/.local/bin/second-brain-joplin</string>
    <string>serve</string>
    <string>--transport</string><string>http</string>
    <string>--env-file</string><string>/Users/you/.config/second-brain-joplin/serve.env</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/Users/you/Library/Logs/second-brain-joplin.log</string>
</dict>
</plist>
```

Or a systemd user unit on Linux (`~/.config/systemd/user/second-brain-joplin.service`):

```ini
[Unit]
Description=second-brain-joplin MCP server (HTTP mode)

[Service]
ExecStart=%h/.local/bin/second-brain-joplin serve --transport http --env-file %h/.config/second-brain-joplin/serve.env
Restart=on-failure

[Install]
WantedBy=default.target
```

Install the command with
`uv tool install --python 3.13 "second-brain-joplin[semantic]"` (the semantic
stack has no build for Python 3.15 yet: [Which Python](install.md#which-python)),
and build the index once with
`second-brain-joplin reindex --env-file <that env file>` before the first start.

## Adding it to Claude

1. On claude.ai: **Customize → Connectors → Add custom connector**.
2. Name it, and enter the URL: your public URL plus the path,
   `https://joplin.example.com/mcp`.
3. Claude finds the sign-in on its own. Sign in with GitHub, then approve the
   consent page.
4. Optional: on the connector's **Permissions** tab, choose for each tool how
   Claude checks with you ([Who confirms each note](#who-confirms-each-note)
   says what the choices do for `joplin_create_note`).

Connectors you add on claude.ai also work in Claude Desktop, the mobile apps and
Cowork. Free accounts can add one custom connector.

## Environment variables at a glance

| Variable | Default | |
|---|---|---|
| `SBJ_TRANSPORT` | `stdio` | `http` for HTTP mode |
| `SBJ_HOST` / `SBJ_PORT` / `SBJ_HTTP_PATH` | `127.0.0.1` / `8775` / `/mcp` | Where it listens |
| `SBJ_PUBLIC_URL` | none | The public origin; required with sign-in |
| `SBJ_GITHUB_CLIENT_ID` / `SBJ_GITHUB_CLIENT_SECRET` | none | The GitHub OAuth App |
| `SBJ_JWT_SIGNING_KEY` | none | 32+ characters |
| `SBJ_ALLOWED_GITHUB_IDS` | none | Numeric GitHub ids allowed in |
| `SBJ_OAUTH_REDIRECT_URIS` | claude.ai's callback | OAuth clients allowed to register |
| `SBJ_NOTEBOOKS` | none | Only these notebooks (ids or titles, comma-separated) are visible |
| `SBJ_EXCLUDE_NOTEBOOKS` | none | These notebooks are hidden |
| `SBJ_WRITE_NOTEBOOKS` | none (read-only) | Notes may be created in these notebooks; `*` = every visible one |
| `SBJ_EDIT_NOTEBOOKS` | none (no changes) | Notes in these notebooks may be changed; `*` = every visible one |
| `SBJ_REPLACE` | `false` | `true` adds the tool that rewrites a whole note |
| `SBJ_UNDO_DIR` / `SBJ_UNDO_DAYS` / `SBJ_UNDO_MAX_MB` | see [configuration](configuration.md#changing-notes) / `90` / `500` | The copies kept before each change |
| `SBJ_CHANGES_PER_DAY` | `100` | The most changes in a day |
| `SBJ_CREATE_NOTES_MAX` | `0` (off) | Up to this many notes (at most 20) in one call of `joplin_create_notes` |
| `SBJ_ASK_EVERY_TIME` | `true` | `false` leaves off the mark that asks Claude's clients to check with you before each note or change |
| `SBJ_JOPLIN_TIMEOUT` / `SBJ_JOPLIN_CONNECT_TIMEOUT` | `10` / `3` | Seconds Joplin has to answer, and to accept the connection |
| `SBJ_RATE_LIMIT_PER_MINUTE` | `60` | Tool calls per caller; 0 = off |
| `SBJ_SYNC_INTERVAL_MINUTES` | `15` | Background index sync; 0 = off |

Plus the usual `JOPLIN_API_TOKEN`, `JOPLIN_BASE_URL` and the `SBJ_*` semantic
settings. [configuration.md](configuration.md) lists every setting, and
[troubleshooting.md](troubleshooting.md#http-mode) covers what can go wrong.
