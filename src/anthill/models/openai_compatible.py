"""Generic OpenAI-compatible provider.

Handles three closely-related shapes from a single class:

  - OpenAI itself                       (provider_name='openai')
  - Anthropic Messages API              (provider_name='anthropic',
                                         adds x-api-key + version header)
  - Any custom OpenAI-compatible host   (provider_name='custom')

The class is intentionally minimal — adapter-level features unique to
a specific vendor (tool calling, vision, streaming) belong in a
dedicated subclass when we need them. For text-only chat completions
this is enough.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

import httpx

from anthill.models.base import ModelProvider, ModelResponse, StreamChunk


class OpenAICompatibleProvider(ModelProvider):
    """Single class handling the three common OpenAI-shaped APIs."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        provider_name: str = "custom",
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.provider_name = provider_name.lower()
        self.timeout = timeout
        # Required by Plugin/ModelProvider naming.
        self.name = provider_name

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> ModelResponse:
        if self.provider_name == "anthropic":
            return await self._anthropic(prompt, system, max_tokens, temperature)
        return await self._openai_style(prompt, system, max_tokens, temperature)

    async def _openai_style(
        self,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float,
    ) -> ModelResponse:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        latency_ms = (time.perf_counter() - start) * 1000

        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return ModelResponse(
            text=text,
            model=self.model,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency_ms,
            raw=data,
        )

    async def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> AsyncIterator[StreamChunk]:
        """Yield incremental text via SSE.

        Both OpenAI-style and Anthropic-style backends speak SSE, but
        with different event shapes. We dispatch on ``provider_name``
        and run a single shared SSE reader. The terminal chunk always
        carries the cumulative input/output token counts when the
        backend reports them — Anthropic reports per-block usage,
        OpenAI sends a final ``[DONE]`` with optional ``usage``.
        """
        if self.provider_name == "anthropic":
            iterator = self._stream_anthropic(prompt, system, max_tokens, temperature)
        else:
            iterator = self._stream_openai(prompt, system, max_tokens, temperature)
        async for chunk in iterator:
            yield chunk

    async def _stream_openai(
        self,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[StreamChunk]:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            # Some OpenAI-compat hosts only report usage when asked.
            "stream_options": {"include_usage": True},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        url = f"{self.base_url}/chat/completions"

        input_tokens = 0
        output_tokens = 0
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", url, json=payload, headers=headers
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    parsed = _parse_sse_line(line)
                    if parsed is None:
                        continue
                    if parsed == "[DONE]":
                        break
                    # Each event payload is a chat.completion.chunk.
                    delta = ""
                    try:
                        choice = parsed["choices"][0]
                        delta = choice.get("delta", {}).get("content") or ""
                    except (KeyError, IndexError, TypeError):
                        delta = ""
                    usage = parsed.get("usage") if isinstance(parsed, dict) else None
                    if isinstance(usage, dict):
                        input_tokens = usage.get("prompt_tokens", input_tokens)
                        output_tokens = usage.get("completion_tokens", output_tokens)
                    if delta:
                        yield StreamChunk(delta=delta)
        yield StreamChunk(
            done=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def _stream_anthropic(
        self,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[StreamChunk]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        if system:
            payload["system"] = system

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        url = f"{self.base_url}/messages"

        input_tokens = 0
        output_tokens = 0
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", url, json=payload, headers=headers
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    parsed = _parse_sse_line(line)
                    if parsed is None or parsed == "[DONE]":
                        continue
                    if not isinstance(parsed, dict):
                        continue
                    event_type = parsed.get("type")
                    if event_type == "content_block_delta":
                        delta_obj = parsed.get("delta") or {}
                        text = delta_obj.get("text") or ""
                        if text:
                            yield StreamChunk(delta=text)
                    elif event_type == "message_start":
                        usage = (parsed.get("message") or {}).get("usage") or {}
                        input_tokens = usage.get("input_tokens", input_tokens)
                        output_tokens = usage.get("output_tokens", output_tokens)
                    elif event_type == "message_delta":
                        usage = parsed.get("usage") or {}
                        # Anthropic streams cumulative output_tokens here.
                        output_tokens = usage.get("output_tokens", output_tokens)
                    elif event_type == "message_stop":
                        break
        yield StreamChunk(
            done=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def _anthropic(
        self,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        url = f"{self.base_url}/messages"

        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        latency_ms = (time.perf_counter() - start) * 1000

        # Anthropic returns content as a list of blocks; we collapse.
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        usage = data.get("usage", {})
        return ModelResponse(
            text=text,
            model=self.model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            latency_ms=latency_ms,
            raw=data,
        )


def _parse_sse_line(line: str) -> Any:
    """Decode one SSE ``data: ...`` line.

    Returns:
      - ``None`` if the line is empty, a comment, or a non-data field
      - The string ``"[DONE]"`` for the OpenAI terminator
      - The decoded JSON object otherwise

    SSE parsing only cares about ``data:`` fields for our use case;
    ``event:``, ``id:``, ``retry:`` etc. are ignored — Anthropic
    duplicates the event type inside the JSON payload as ``type`` so
    we don't lose information.
    """
    if not line:
        return None
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload:
        return None
    if payload == "[DONE]":
        return "[DONE]"
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None
