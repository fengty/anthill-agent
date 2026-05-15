"""Model providers — the layer between the colony and any LLM API.

Anthill is model-agnostic by design. The pheromone mechanism doesn't care
which model is behind which agent; it only cares about success scores.

Provider registry is intentionally tiny. Adding a new model means:
    1. implement ModelProvider
    2. register it in registry.py
"""

from anthill.models.base import ModelProvider, ModelResponse
from anthill.models.registry import get_provider, register_provider

__all__ = ["ModelProvider", "ModelResponse", "get_provider", "register_provider"]
