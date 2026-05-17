"""The abstract provider interface.

Keep this surface as small as possible. The colony only needs two
operations:

- ``complete(prompt)`` — give me the whole answer.
- ``stream(prompt)`` — give me the answer in incremental chunks.

The default ``stream()`` implementation falls back to ``complete()``
and emits the whole text as a single chunk. Providers that have real
SSE support override it; callers can rely on the streaming interface
existing on every provider without checking capabilities.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class ModelResponse:
    """Uniform response shape across providers."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    raw: dict | None = None  # type: ignore[type-arg]


@dataclass
class StreamChunk:
    """One incremental piece of a streamed completion.

    ``delta`` is the text fragment to append to whatever's been
    received so far. ``done`` is True only for the terminal chunk,
    which also carries final usage metrics (most providers send these
    in the closing event, not on each token).

    Providers that can't stream emit a single ``StreamChunk(done=True,
    delta=full_text, ...)``. Callers should treat that as legal and
    just render it like any other final chunk.
    """

    delta: str = ""
    done: bool = False
    input_tokens: int = 0
    output_tokens: int = 0


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

    async def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> AsyncIterator[StreamChunk]:
        """Yield the model's completion incrementally.

        Default implementation: call ``complete()`` and emit a single
        terminal chunk with the full text. Providers with native SSE
        support should override this for real-time deltas.

        Implementations MUST yield at least one chunk with ``done=True``
        (the terminal chunk) — callers rely on it to know the stream
        finished cleanly.
        """
        response = await self.complete(
            prompt,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        yield StreamChunk(
            delta=response.text,
            done=True,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
