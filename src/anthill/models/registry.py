"""Provider registry — name to instance factory.

Lets users select a model from config without importing provider classes directly.
"""

from __future__ import annotations

from typing import Callable

from anthill.models.base import ModelProvider
from anthill.models.deepseek import DeepSeekProvider
from anthill.models.minimax import MiniMaxProvider

_REGISTRY: dict[str, Callable[[], ModelProvider]] = {
    "deepseek": lambda: DeepSeekProvider(),
    "deepseek-chat": lambda: DeepSeekProvider(model="deepseek-chat"),
    "deepseek-reasoner": lambda: DeepSeekProvider(model="deepseek-reasoner"),
    "minimax": lambda: MiniMaxProvider(),
    "minimax-m2-stable": lambda: MiniMaxProvider(model="MiniMax-M2-Stable"),
    "minimax-m2": lambda: MiniMaxProvider(model="MiniMax-M2"),
    "minimax-m2.5": lambda: MiniMaxProvider(model="MiniMax-M2.5"),
}


def register_provider(name: str, factory: Callable[[], ModelProvider]) -> None:
    """Register a new provider factory under a short name."""
    _REGISTRY[name] = factory


def get_provider(name: str) -> ModelProvider:
    """Resolve a provider by name. Raises KeyError if unknown."""
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"Unknown model '{name}'. Known: {known}")
    return _REGISTRY[name]()


def known_providers() -> list[str]:
    return sorted(_REGISTRY)
