"""The abstract provider interface.

Keep this surface as small as possible. The colony only needs one operation:
give me text for a prompt, with usage metadata so we can score it later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ModelResponse:
    """Uniform response shape across providers."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    raw: dict | None = None  # type: ignore[type-arg]


class ModelProvider(ABC):
    """Anything that can answer a prompt with text."""

    name: str  # short identifier used in config

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> ModelResponse:
        """Return the model's completion for a prompt."""
