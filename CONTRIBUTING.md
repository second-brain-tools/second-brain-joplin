# Contributing

Thanks for your interest in second-brain-joplin.

## Where the code lives

Sign-in, the audit log, HTTP serving, the settings files and the search engine are
shared with the other Second Brain servers in
[second-brain-core](https://github.com/second-brain-tools/second-brain-core). **Fix
shared code there**, release it, then move this package's pin; this repository
holds what is Joplin's (the Joplin client, the notebook filter, the tools). To try
an unreleased core change here, point uv at your checkout for the moment, and
don't commit it:

```toml
[tool.uv.sources]
second-brain-core = { path = "../second-brain-core", editable = true }
```

## Development setup

We use [uv](https://docs.astral.sh/uv/) as the primary toolchain:

```bash
git clone https://github.com/second-brain-tools/second-brain-joplin
cd second-brain-joplin
uv sync
uv run pre-commit install   # enable lint/format on commit
```

<details>
<summary>pip fallback</summary>

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
```
</details>

## Running tests

```bash
uv run pytest
```

Coverage is printed on every run. The **90% coverage gate is enforced in CI**
(not in `addopts`, so running a subset of tests locally won't spuriously fail).
Reproduce the gate locally with:

```bash
uv run pytest --cov-fail-under=90
```

### Semantic search & optional extras

The semantic-search stack (embedding model + vector store) lives behind optional
extras (`[semantic]`, `[semantic-chroma]`, `[semantic-st]`, each pulling in the
same extra of second-brain-core), so a plain `uv sync` and the test suite need
**none** of them. The engine, the stores and the embedders are
[second-brain-core](https://github.com/second-brain-tools/second-brain-core)'s and
tested there; this repository's tests drive the Joplin side
(`src/second_brain_joplin/semantic/service.py`) through core's in-memory fakes,
re-exported in `tests/fakes.py`. To exercise semantic search for real, install a
backend combo and run `joplin_reindex`:

```bash
uv sync --extra semantic                 # sqlite-vec + FastEmbed (light default)
# or: uv sync --extra semantic --extra semantic-st      # bge-m3 via sentence-transformers
# or: uv sync --extra semantic --extra semantic-chroma  # ChromaDB vector store
```

### Live write checks

The unit tests use a fake Joplin. Before a release that adds or changes a tool
that writes, run the write checks against a real Joplin:

```bash
uv run python scripts/live_write_check.py --yes-write-to .env [--wait-history 12]
```

It writes to the Joplin the env file names (`JOPLIN_API_TOKEN`, and
`JOPLIN_BASE_URL` if needed), so it refuses to run without `--yes-write-to`.
Everything happens in a temporary notebook it creates and permanently deletes;
it compares the vault's notebook and note counts before and after, and prints
counts, ids and ok/FAIL, never a note's text. `--wait-history N` also waits up to
N minutes for Joplin to record the change in its note history. On a synced vault
the temporary notebook shows on the other devices for a few minutes. It isn't
collected by pytest or run in CI. What it found is in the docs beside each tool.

## Linting and type checking

```bash
uvx pre-commit run --all-files   # ruff lint + format + actionlint (the authoritative gate)
uv run mypy                      # strict static type checking (src, tests, scripts)
```

`.pre-commit-config.yaml` is the single source of truth for lint tooling and
versions — CI runs the exact same `pre-commit run --all-files`. It also runs
`actionlint` over `.github/workflows/*.yml`, so workflow-syntax regressions are
caught locally. mypy runs in `strict` mode over `src`, `tests` and `scripts`
(configured in `pyproject.toml`).

## How CI works

All quality gates live in one reusable workflow,
[`.github/workflows/checks.yml`](.github/workflows/checks.yml) — **lint**,
**typecheck**, a **test** matrix (Python 3.11 to 3.15 on Linux, plus a run each
on macOS and Windows), and a **build** job that builds the package and runs
`twine check`. The built wheel is then smoke-tested on all three systems (in the
build job on Linux, and at the end of the macOS and Windows test jobs): it is
started with `uvx` and spoken to over MCP, on stdio and HTTP
([`scripts/smoke.py`](scripts/smoke.py); run it yourself with `uv build` and
then `uv run scripts/smoke.py --from dist/second_brain_joplin-<version>-py3-none-any.whl`). Both
[`ci.yml`](.github/workflows/ci.yml) (on push/PR to `main`) and
[`release.yml`](.github/workflows/release.yml) (before publishing to PyPI) call
it, so nothing merges or ships without passing the same checks. Run `uv sync
--locked` locally to match CI's locked environment.

A separate [`labeler.yml`](.github/workflows/labeler.yml) workflow applies
`component:*` labels to PRs from the file paths changed — advisory only, not a
merge gate.

## Running the server locally

```bash
cp .env.example .env
# Edit .env with your Joplin API token; it is loaded automatically via python-dotenv
uv run second-brain-joplin serve
```

## Issue triage checklist

See [docs/project-management.md](docs/project-management.md) for the full model
(kind of work, labels, milestones). Every triaged issue should have:

1. **Kind** — Epic (via the `epic` label) / Feature / Task / Spike / Bug (via the
   issue-form template; personal accounts have no org-level issue types)
2. **Milestone** — which release it targets (or `backlog` label if unscheduled)
3. One or more **`component:*`** labels
4. **Parent Epic** set as a sub-issue where relevant

## Label conventions

| Prefix | Values | Applied by |
|---|---|---|
| `component:` | `joplin-api`, `mcp`, `search`, `docs`, `ci`, `templates` | Maintainer (issues); [labeler bot](.github/workflows/labeler.yml) (PRs) |
| meta | `epic`, `backlog`, `needs-triage`, `blocked`, `good first issue`, `help wanted` | Maintainer |

## License of contributions

The server is licensed under the GNU Affero General Public License, version 3
or later ([LICENSE](LICENSE)), and the note templates under CC0 1.0
([templates/LICENSE](templates/LICENSE)). A contribution is accepted under the
license of the part it changes: by opening a pull request you agree to that.
There is no contributor agreement to sign. A sign-off (`git commit -s`) says
you have the right to submit the change under those terms
([Developer Certificate of Origin](https://developercertificate.org/)).

## Commit style

No strict convention yet — use a short imperative summary line. DCO sign-off (`git commit -s`) is encouraged but not enforced at this stage.

## Pull requests

- Target `main`.
- Keep PRs focused — one logical change per PR.
- Add or update tests for any new behavior.
- CI must pass before merge.
