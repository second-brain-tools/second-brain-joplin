"""Runtime configuration for the Joplin MCP server."""

import os
from pathlib import Path

from pydantic import BaseModel, field_validator
from second_brain_core.env import env_bool as _env_bool
from second_brain_core.env import env_flag as _env_flag
from second_brain_core.env import env_int
from second_brain_core.env import env_list as _env_list
from second_brain_core.env import env_seconds as _env_seconds
from second_brain_core.env import env_switch as _env_switch
from second_brain_core.env import set_but_blank as _set_but_blank

DEFAULT_BASE_URL = "http://localhost:41184"
DEFAULT_TIMEOUT = 10.0
DEFAULT_CONNECT_TIMEOUT = 3.0
TIMEOUT_VAR = "SBJ_JOPLIN_TIMEOUT"
CONNECT_TIMEOUT_VAR = "SBJ_JOPLIN_CONNECT_TIMEOUT"

# The safety net of the tools that change a note (#82).
DEFAULT_UNDO_DAYS = 90
DEFAULT_UNDO_MAX_MB = 500
DEFAULT_CHANGES_PER_DAY = 100
UNDO_DAYS_VAR = "SBJ_UNDO_DAYS"
UNDO_MAX_MB_VAR = "SBJ_UNDO_MAX_MB"
CHANGES_PER_DAY_VAR = "SBJ_CHANGES_PER_DAY"
REPLACE_VAR = "SBJ_REPLACE"
# joplin_create_notes (#69): the most notes one call may create; 0 leaves the tool out.
CREATE_NOTES_MAX_VAR = "SBJ_CREATE_NOTES_MAX"
CREATE_NOTES_CEILING = 20

DEFAULT_EMBEDDING_BACKEND = "fastembed"
DEFAULT_VECTOR_STORE = "sqlite-vec"


def default_index_dir() -> str:
    """Return the default on-disk location for the semantic index.

    Follows the XDG Base Directory spec: ``$XDG_DATA_HOME/second-brain-joplin/index``
    when set, otherwise ``~/.local/share/second-brain-joplin/index``.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return str(base / "second-brain-joplin" / "index")


ASK_EVERY_TIME_VAR = "SBJ_ASK_EVERY_TIME"


def default_undo_dir() -> str:
    """Where the kept copies of changed notes go: beside the index, readable only by the user."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return str(base / "second-brain-joplin" / "undo")


def _env_count(name: str, default: int) -> int | None:
    """A whole number of at least 1: the default when unset or empty, None otherwise.

    Never 0: the journal can't be turned off (John, 2026-11-21), and a limit of 0
    would only mean "edits on, but none".
    """
    return env_int(name, default, minimum=1)


def _env_up_to(name: str, ceiling: int) -> int | None:
    """A whole number from 0 to ``ceiling``; 0 when unset or empty; None for anything else."""
    return env_int(name, 0, minimum=0, maximum=ceiling)


class Settings(BaseModel):
    """Connection, notebook-filter and semantic-search settings for the server.

    Connection settings are read from ``JOPLIN_*`` env vars; everything else from
    ``SBJ_*``. The token may be empty at startup so that ``serve`` boots without
    Joplin configured; a missing token surfaces as a friendly error on the first
    API call, not at import time.
    """

    joplin_base_url: str = DEFAULT_BASE_URL
    joplin_api_token: str = ""
    # How long a request to Joplin may wait for its answer (SBJ_JOPLIN_TIMEOUT),
    # and for its connection (SBJ_JOPLIN_CONNECT_TIMEOUT), in seconds.
    request_timeout: float = DEFAULT_TIMEOUT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT

    # The notebook filter (scope.py): ids or titles, each covering the notebook and
    # everything under it. Both empty means every notebook is visible.
    notebooks: tuple[str, ...] = ()
    exclude_notebooks: tuple[str, ...] = ()
    # Filter variables that are set but list no notebook. "Only these: none" and
    # "no filter" are opposite readings of that, so it is refused, not guessed.
    blank_filter_vars: tuple[str, ...] = ()
    # Where a note may be created (scope.py); empty means the server is read-only
    # and registers no write tool at all.
    write_notebooks: tuple[str, ...] = ()
    # Notebooks whose notes may be changed (#82): ids or titles, or `*`; empty means
    # no tool that changes a note is registered. Separate from write_notebooks, which
    # has meant "a new note and nothing else" since 0.5.0.
    edit_notebooks: tuple[str, ...] = ()
    # Whether joplin_replace_note (#80), the bluntest change, is registered too.
    replace: bool = False
    # The most notes joplin_create_notes may create in one call; 0 leaves it out.
    create_notes_max: int = 0
    # The journal of old text (journal.py): where, for how long, how big; and the
    # most changes in one day. The journal is always on.
    undo_dir: str = ""
    undo_days: int = DEFAULT_UNDO_DAYS
    undo_max_mb: int = DEFAULT_UNDO_MAX_MB
    changes_per_day: int = DEFAULT_CHANGES_PER_DAY
    # Whether the write tool is marked so that Claude Code asks on every call
    # (tools/annotations.py). On unless SBJ_ASK_EVERY_TIME plainly says no.
    ask_every_time: bool = True
    # Switches set to something that is neither a yes nor a no. They stay on, and
    # `serve` refuses to start until the value is fixed.
    unreadable_switches: tuple[str, ...] = ()
    # Settings of seconds that aren't a positive number. Their default is used, and
    # the commands refuse to start until the value is fixed.
    unreadable_numbers: tuple[str, ...] = ()

    embedding_backend: str = DEFAULT_EMBEDDING_BACKEND
    embedding_model: str = ""
    vector_store: str = DEFAULT_VECTOR_STORE
    semantic_index_dir: str = ""
    semantic_auto_sync: bool = True

    @field_validator("joplin_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def notebook_filter_set(self) -> bool:
        """True when ``SBJ_NOTEBOOKS`` or ``SBJ_EXCLUDE_NOTEBOOKS`` is set at all.

        That includes one set to nothing: resolving the filter then refuses,
        where treating it as "no filter" would show every notebook.
        """
        return bool(self.notebooks or self.exclude_notebooks or self.blank_filter_vars)

    @property
    def edits_enabled(self) -> bool:
        """True when ``SBJ_EDIT_NOTEBOOKS`` names notebooks whose notes may be changed."""
        return bool(self.edit_notebooks)

    @property
    def writes_enabled(self) -> bool:
        """True when ``SBJ_WRITE_NOTEBOOKS`` names anywhere a note may be created."""
        return bool(self.write_notebooks)

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from ``JOPLIN_*`` / ``SBJ_*`` environment variables."""
        ask_every_time = _env_switch(ASK_EVERY_TIME_VAR)
        replace = _env_flag(REPLACE_VAR)
        create_notes_max = _env_up_to(CREATE_NOTES_MAX_VAR, CREATE_NOTES_CEILING)
        timeout = _env_seconds(TIMEOUT_VAR, DEFAULT_TIMEOUT)
        connect_timeout = _env_seconds(CONNECT_TIMEOUT_VAR, DEFAULT_CONNECT_TIMEOUT)
        counts = {
            name: (_env_count(name, default), default)
            for name, default in (
                (UNDO_DAYS_VAR, DEFAULT_UNDO_DAYS),
                (UNDO_MAX_MB_VAR, DEFAULT_UNDO_MAX_MB),
                (CHANGES_PER_DAY_VAR, DEFAULT_CHANGES_PER_DAY),
            )
        }

        def count(name: str) -> int:
            value, default = counts[name]
            return default if value is None else value

        return cls(
            joplin_base_url=os.environ.get("JOPLIN_BASE_URL", DEFAULT_BASE_URL),
            joplin_api_token=os.environ.get("JOPLIN_API_TOKEN", ""),
            request_timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
            connect_timeout=DEFAULT_CONNECT_TIMEOUT if connect_timeout is None else connect_timeout,
            unreadable_numbers=tuple(
                name
                for name, value in ((TIMEOUT_VAR, timeout), (CONNECT_TIMEOUT_VAR, connect_timeout))
                if value is None
            )
            + tuple(name for name, (value, _) in counts.items() if value is None)
            + ((CREATE_NOTES_MAX_VAR,) if create_notes_max is None else ()),
            notebooks=_env_list("SBJ_NOTEBOOKS"),
            exclude_notebooks=_env_list("SBJ_EXCLUDE_NOTEBOOKS"),
            blank_filter_vars=_set_but_blank("SBJ_NOTEBOOKS", "SBJ_EXCLUDE_NOTEBOOKS"),
            write_notebooks=_env_list("SBJ_WRITE_NOTEBOOKS"),
            edit_notebooks=_env_list("SBJ_EDIT_NOTEBOOKS"),
            replace=replace is True,
            create_notes_max=create_notes_max or 0,
            undo_dir=os.environ.get("SBJ_UNDO_DIR", "").strip() or default_undo_dir(),
            undo_days=count(UNDO_DAYS_VAR),
            undo_max_mb=count(UNDO_MAX_MB_VAR),
            changes_per_day=count(CHANGES_PER_DAY_VAR),
            ask_every_time=ask_every_time is not False,
            unreadable_switches=((ASK_EVERY_TIME_VAR,) if ask_every_time is None else ())
            + ((REPLACE_VAR,) if replace is None else ()),
            embedding_backend=os.environ.get("SBJ_EMBEDDING_BACKEND", DEFAULT_EMBEDDING_BACKEND),
            embedding_model=os.environ.get("SBJ_EMBEDDING_MODEL", ""),
            vector_store=os.environ.get("SBJ_VECTOR_STORE", DEFAULT_VECTOR_STORE),
            semantic_index_dir=os.environ.get("SBJ_INDEX_DIR", "") or default_index_dir(),
            semantic_auto_sync=_env_bool("SBJ_SEMANTIC_AUTO_SYNC", True),
        )
