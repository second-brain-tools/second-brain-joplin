"""Guard that packaging metadata stays wired to the single version source."""

import subprocess
import sys
from importlib.metadata import metadata, version

import pytest

from second_brain_joplin import __version__


def test_installed_version_matches_dunder() -> None:
    assert version("second-brain-joplin") == __version__


def test_license_metadata_names_the_agpl_and_nothing_else() -> None:
    """One license statement, as an SPDX expression.

    A leftover ``License ::`` classifier would be a second statement of the
    license beside the expression, and the old one named MIT.
    """
    meta = metadata("second-brain-joplin")
    assert meta["License-Expression"] == "AGPL-3.0-or-later"
    assert [c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")] == []


@pytest.mark.parametrize(
    "module",
    ["second_brain_joplin.cli", "second_brain_joplin.changes", "second_brain_joplin.server"],
)
def test_each_entry_point_imports_on_its_own(module: str) -> None:
    """In a fresh interpreter, as `second-brain-joplin` starts: a suite's import order
    can hide an import cycle."""
    done = subprocess.run(
        [sys.executable, "-c", f"import {module}"], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr[-2000:]
