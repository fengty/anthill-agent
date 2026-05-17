"""Anthill — every user grows their own AI nation through accumulated experience."""

__version__ = "0.1.5"

from anthill.core.agent import Agent
from anthill.core.nation import Nation
from anthill.core.pheromone import PheromoneTrail
from anthill.core.router import Router

__all__ = ["Agent", "Nation", "PheromoneTrail", "Router"]
