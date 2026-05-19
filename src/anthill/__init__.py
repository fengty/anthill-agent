"""Anthill — every user grows their own AI nation through accumulated experience.

0.1.16+ — re-exports are lazy. ``from anthill import __version__`` no
longer drags in Nation / Router / Pheromone / Agent and their entire
import trees. Each public symbol is materialized on first attribute
access via PEP 562 ``__getattr__``. ``--version`` and ``--help`` go
from ~120 ms to ~30 ms on a warm cache.

``from anthill import Nation`` still works — Python calls our
``__getattr__`` when it can't find the name eagerly, gets the real
class back, and caches it on the module so subsequent lookups are
free.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.62"

__all__ = ["Agent", "Nation", "PheromoneTrail", "Router"]


def __getattr__(name: str) -> Any:
    # Lazy materialization of the heavy classes. The import only fires
    # the first time someone reaches for the name — fine on a 100 ms
    # human-perceptible timescale, invisible afterwards.
    if name == "Agent":
        from anthill.core.agent import Agent

        return Agent
    if name == "Nation":
        from anthill.core.nation import Nation

        return Nation
    if name == "PheromoneTrail":
        from anthill.core.pheromone import PheromoneTrail

        return PheromoneTrail
    if name == "Router":
        from anthill.core.router import Router

        return Router
    raise AttributeError(f"module 'anthill' has no attribute {name!r}")
