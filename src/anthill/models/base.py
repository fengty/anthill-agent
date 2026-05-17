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


# 0.1.26 — bumped from 1024 to 4096. A real user hit the exact bug:
# `深度搜索下ai资深网站` came back truncated mid-sentence on the 6th
# entry because the default cap was 1024. Research / synthesis tasks
# almost always need more room. 4096 is generous for chat-shaped work
# and well below the 1M-token output windows the current frontier
# models advertise — far from a cost risk in practice since judges
# stop the deliberation loop once quality threshold is met.
DEFAULT_MAX_TOKENS = 4096


@dataclass
class ModelResponse:
    """Uniform response shape across providers."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    raw: dict | None = None  # type: ignore[type-arg]
    # 0.1.26 — surfaces why the model stopped. Common values across
    # providers (normalized to lowercase strings):
    #   "stop"   — natural end of generation (good)
    #   "length" — hit max_tokens (output is TRUNCATED)
    #   "tool_use" / "tool_calls" — model wants to call a tool
    #   "content_filter" — provider's safety system blocked
    # None for providers that don't report it; callers should treat
    # None as "stop" optimistically.
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        """True when the provider said it stopped on the max_tokens cap.

        This is the smoking gun for a half-finished research answer
        and the trigger for the 0.1.26 truncation-aware judge.
        """
        if self.finish_reason is None:
            return False
        return self.finish_reason.lower() in ("length", "max_tokens", "max_output_tokens")


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
    # 0.1.26 — same shape as ModelResponse.finish_reason. Only set
    # on the terminal chunk (done=True).
    finish_reason: str | None = None


class ModelProvider(ABC):
    """Anything that can answer a prompt with text."""

    name: str  # short identifier used in config

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.7,
    ) -> ModelResponse:
        """Return the model's completion for a prompt."""

    async def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
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
            finish_reason=response.finish_reason,
        )
