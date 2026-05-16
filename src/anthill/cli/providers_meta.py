"""Static metadata about supported providers — used by setup wizard
and `anthill model add`.

This file is the single source of truth for: which providers exist,
what their default model is, what to prompt for a key, whether they
need a custom base_url.

The runtime adapter that actually calls the provider's API lives in
src/anthill/models/. The two layers are kept separate so adding a new
provider here does not force a code change to the adapter (and vice
versa).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
    name: str
    description: str
    default_model: str
    key_prompt: str
    needs_base_url: bool = False


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "deepseek": ProviderPreset(
        name="deepseek",
        description="DeepSeek (cheap, OpenAI-compatible)",
        default_model="deepseek-chat",
        key_prompt="DeepSeek API key (sk-...)",
    ),
    "minimax": ProviderPreset(
        name="minimax",
        description="MiniMax (M2 series, multilingual)",
        default_model="MiniMax-M2-Stable",
        key_prompt="MiniMax API key",
    ),
    "openai": ProviderPreset(
        name="openai",
        description="OpenAI (GPT-5, GPT-4o)",
        default_model="gpt-4o-mini",
        key_prompt="OpenAI API key (sk-...)",
    ),
    "anthropic": ProviderPreset(
        name="anthropic",
        description="Anthropic (Claude family)",
        default_model="claude-sonnet-4-5",
        key_prompt="Anthropic API key (sk-ant-...)",
    ),
    "custom": ProviderPreset(
        name="custom",
        description="Any OpenAI-compatible endpoint",
        default_model="your-model-id",
        key_prompt="API key",
        needs_base_url=True,
    ),
}
