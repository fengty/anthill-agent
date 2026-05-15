"""Anthill — multi-agent framework where specialization emerges from pheromone trails."""

__version__ = "0.0.1"

from anthill.core.agent import Agent
from anthill.core.pheromone import PheromoneTrail
from anthill.core.router import Router
from anthill.core.colony import Colony

__all__ = ["Agent", "PheromoneTrail", "Router", "Colony"]
