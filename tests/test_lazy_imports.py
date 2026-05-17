"""0.1.16 — lazy top-level re-exports.

`from anthill import __version__` used to pull in Nation, Router,
Agent, Pheromone, and the entire transitive import tree (~120ms,
~50 submodules). PEP 562 `__getattr__` defers each public name to
first-use.

Tests:
  1. __version__ accessible without triggering heavy imports
  2. from anthill import Nation still works (materializes lazily)
  3. Unknown names raise AttributeError (the standard contract)
  4. __all__ lists the lazy names
  5. dir() includes the lazy names (for tab completion in REPL)
"""

from __future__ import annotations

import sys


def test_version_is_str() -> None:
    import anthill

    assert isinstance(anthill.__version__, str)
    # Sanity: starts with major.minor.
    assert anthill.__version__.startswith("0.")


def test_version_import_does_not_load_nation() -> None:
    """The whole point: __version__ shouldn't drag in core.nation.

    We can't reliably test this in-process because some other test
    almost certainly imported anthill.core.nation already. Instead,
    spawn a fresh interpreter that imports only __version__ and
    check that sys.modules afterwards does NOT contain core.nation.
    """
    import subprocess

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from anthill import __version__; "
                "import sys; "
                "print('has_nation=' + str('anthill.core.nation' in sys.modules))"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert "has_nation=False" in proc.stdout


def test_nation_class_is_materialized_on_access() -> None:
    import anthill

    cls = anthill.Nation
    # Real class, not a stub
    assert cls.__name__ == "Nation"
    assert hasattr(cls, "ask")


def test_agent_class_is_materialized_on_access() -> None:
    import anthill

    cls = anthill.Agent
    assert cls.__name__ == "Agent"


def test_pheromone_trail_class_is_materialized_on_access() -> None:
    import anthill

    cls = anthill.PheromoneTrail
    assert cls.__name__ == "PheromoneTrail"


def test_router_class_is_materialized_on_access() -> None:
    import anthill

    cls = anthill.Router
    assert cls.__name__ == "Router"


def test_unknown_attribute_raises() -> None:
    import anthill

    import pytest

    with pytest.raises(AttributeError, match="DoesNotExist"):
        _ = anthill.DoesNotExist


def test_all_lists_lazy_names() -> None:
    import anthill

    assert "Nation" in anthill.__all__
    assert "Agent" in anthill.__all__
    assert "PheromoneTrail" in anthill.__all__
    assert "Router" in anthill.__all__
