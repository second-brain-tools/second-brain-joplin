"""HTTP mode's one setting of this server's own: the background index sync.

Everything else about HTTP mode (the options, the guardrails, the run call) is
second-brain-core's (``second_brain_core.remote``).
"""

import os
from collections.abc import Mapping

from second_brain_core.errors import ServeConfigError
from second_brain_core.remote import env_number

from .identity import SERVER

# Minutes between background index syncs in HTTP mode (0 = off).
DEFAULT_SYNC_INTERVAL = 15
SYNC_INTERVAL_VAR = SERVER.var("SYNC_INTERVAL_MINUTES")


def sync_interval_minutes(environ: Mapping[str, str] | None = None) -> int:
    """``SBJ_SYNC_INTERVAL_MINUTES``: 15 when unset, 0 for off; refused when negative."""
    env = os.environ if environ is None else environ
    value = env_number(env, SYNC_INTERVAL_VAR)
    if value is None:
        return DEFAULT_SYNC_INTERVAL
    if value < 0:
        raise ServeConfigError(f"{SYNC_INTERVAL_VAR} must be 0 (off) or more")
    return value
