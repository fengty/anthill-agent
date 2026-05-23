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

        choice = data["choices"][0]
        text = choice["message"]["content"]
        usage = data.get("usage", {})
        return ModelResponse(
            text=text,
            model=self.model,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency_ms,
            raw=data,
            finish_reason=choice.get("finish_reason"),
        )

    async def complete_with_messages(
        self,
        messages: list,
        *,
        system: str | None = None,
        tools: list | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> ModelResponse:
        """0.2.29 — multi-turn + native tool_use for OpenAI-compatible hosts.

        Wire format (OpenAI / DeepSeek / Minimax / Moonshot / Qwen):
          - tools as `tools` array of {type: function, function: {...}}
          - tool calls in response.choices[0].message.tool_calls
            (each has id, function.name, function.arguments JSON-encoded)
          - tool results submitted as messages with role="tool",
            tool_call_id=..., content=...

        Anthropic backend ALSO routes here for now (it supports the
        OpenAI-compatible chat/completions endpoint via the
        anthropic-compat layer), but the proper Anthropic
        Messages-API path is 0.2.30 work — for now Anthropic users
        fall through to the OpenAI-style call which their endpoint
        accepts.
        """
        from anthill.core.tools_protocol import ToolCall, ToolSpec

        # Always prepend system as the first message when provided.
        msgs: list[dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = [
                t.to_openai_format() if isinstance(t, ToolSpec) else t
                for t in tools
            ]
            # Let the model decide whether to call. We don't force
            # tool use; the brief tool-use-enforcement prompt does
            # that job at the language level.
            payload["tool_choice"] = "auto"
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

        choice = data["choices"][0]
        msg = choice.get("message", {}) or {}
        text = msg.get("content") or ""
        usage = data.get("usage", {}) or {}

        # Parse tool_calls if present.
        tool_calls_list: list[ToolCall] = []
        for tc in (msg.get("tool_calls") or []):
            try:
                fn = tc.get("function", {}) or {}
                raw_args = fn.get("arguments", "") or "{}"
                # arguments is a JSON-encoded STRING per OpenAI spec.
                # Some hosts (deepseek) sometimes return a dict; handle both.
                if isinstance(raw_args, str):
                    args = json.loads(raw_args) if raw_args.strip() else {}
                else:
                    args = dict(raw_args)
            except (ValueError, TypeError):
                args = {}
            tool_calls_list.append(
                ToolCall(
                    id=tc.get("id") or f"call_{len(tool_calls_list)}",
                    name=fn.get("name", ""),
                    arguments=args,
                )
            )

        return ModelResponse(
            text=text,
            model=self.model,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency_ms,
            raw=data,
            finish_reason=choice.get("finish_reason"),
            tool_calls=tool_calls_list,
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
        finish_reason: str | None = None
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
                        # finish_reason arrives on the last content chunk.
                        fr = choice.get("finish_reason")
                        if fr:
                            finish_reason = fr
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
            finish_reason=finish_reason,
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
        finish_reason: str | None = None
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
                        # And stop_reason lands on the same event.
                        stop = (parsed.get("delta") or {}).get("stop_reason")
                        if stop:
                            finish_reason = "length" if stop == "max_tokens" else stop
                    elif event_type == "message_stop":
                        break
        yield StreamChunk(
            done=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=finish_reason,
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
        # Anthropic uses `stop_reason` instead of OpenAI's `finish_reason`.
        # Map `max_tokens` → `length` so downstream truncation detection
        # in ModelResponse.truncated stays provider-agnostic.
        anthropic_stop = data.get("stop_reason")
        if anthropic_stop == "max_tokens":
            finish_reason = "length"
        else:
            finish_reason = anthropic_stop
        return ModelResponse(
            text=text,
            model=self.model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            latency_ms=latency_ms,
            raw=data,
            finish_reason=finish_reason,
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
