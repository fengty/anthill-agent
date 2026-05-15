"""DeepSeek provider.

DeepSeek exposes an OpenAI-compatible Chat Completions API, so the wire format
is the same shape as OpenAI's. We use httpx directly to avoid pulling in the
OpenAI SDK as a hard dependency.
"""

from __future__ import annotations

import os
import time

import httpx

from anthill.models.base import ModelProvider, ModelResponse


class DeepSeekProvider(ModelProvider):
    """DeepSeek Chat completions.

    Models: "deepseek-chat" (V3), "deepseek-reasoner" (R1).
    """

    name = "deepseek"
    base_url = "https://api.deepseek.com/v1"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "deepseek-chat",
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key or os.getenv("ANTHILL_DEEPSEEK_KEY") or os.getenv("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "DeepSeek API key not found. Set ANTHILL_DEEPSEEK_KEY or pass api_key=..."
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
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
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
