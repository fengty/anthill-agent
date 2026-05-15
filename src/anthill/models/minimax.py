"""MiniMax provider.

MiniMax's Chat Completion v2 endpoint uses a payload shape close to OpenAI's
but requires a group_id query parameter in addition to the bearer token.
Reference: https://api.minimax.chat/document/guides/chat-model/V2
"""

from __future__ import annotations

import os
import time

import httpx

from anthill.models.base import ModelProvider, ModelResponse


class MiniMaxProvider(ModelProvider):
    """MiniMax abab Chat Completion v2."""

    name = "minimax"
    base_url = "https://api.minimax.chat/v1/text/chatcompletion_v2"

    def __init__(
        self,
        api_key: str | None = None,
        group_id: str | None = None,
        model: str = "abab6.5s-chat",
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key or os.getenv("ANTHILL_MINIMAX_KEY") or os.getenv("MINIMAX_API_KEY")
        self.group_id = group_id or os.getenv("ANTHILL_MINIMAX_GROUP") or os.getenv("MINIMAX_GROUP_ID")
        if not self.api_key:
            raise RuntimeError(
                "MiniMax API key not found. Set ANTHILL_MINIMAX_KEY or pass api_key=..."
            )
        if not self.group_id:
            raise RuntimeError(
                "MiniMax group_id not found. Set ANTHILL_MINIMAX_GROUP or pass group_id=..."
            )
        self.model = model
        self.timeout = timeout

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> ModelResponse:
        messages: list[dict[str, str]] = []
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

        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}?GroupId={self.group_id}",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        latency_ms = (time.perf_counter() - start) * 1000

        # MiniMax v2 returns OpenAI-style choices
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
