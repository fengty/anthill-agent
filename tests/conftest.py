"""Shared pytest fixtures for the Anthill test suite.

pytest auto-discovers fixtures defined here for every test under
`tests/`. Centralizing the recurring two means individual test files
no longer redefine them, and any future tweak (e.g. always setting
ANTHILL_HOME to a tmp dir for the whole session) lands in one place.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _anthill_home_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolate every test from the user's real ~/.anthill state.

    autouse=True means every test gets this without opting in. The
    cost is tiny (env-var swap) and the alternative — every test
    forgetting once and accidentally writing to ~/.anthill — is the
    kind of bug you only catch by burning your real nation.

    Test files that ALSO defined a local `_isolate` autouse fixture
    are unaffected — pytest just runs both. The duplicate
    definitions are now safe to delete from individual files at
    leisure.
    """
    monkeypatch.setenv("ANTHILL_HOME", str(tmp_path))


@pytest.fixture
def workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Sandbox directory for plugins that read/write user files.

    Plugins like file_read / file_write / pdf_read consult
    ANTHILL_PLUGIN_WORKSPACE to know where they're allowed to touch
    the filesystem. Tests that exercise those plugins use this
    fixture instead of redefining their own.
    """
    monkeypatch.setenv("ANTHILL_PLUGIN_WORKSPACE", str(tmp_path))
    return tmp_path
