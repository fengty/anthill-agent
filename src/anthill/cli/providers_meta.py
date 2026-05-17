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
    # Known-good model ids for this provider. Empty tuple means
    # "we don't track an allow-list" (e.g. custom OpenAI-compatible).
    # Setup wizard warns when user types something not in this list.
    known_models: tuple[str, ...] = ()


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "deepseek": ProviderPreset(
        name="deepseek",
        description="DeepSeek (cheap, OpenAI-compatible)",
        default_model="deepseek-chat",
        key_prompt="DeepSeek API key (sk-...)",
        known_models=("deepseek-chat", "deepseek-reasoner"),
    ),
    "minimax": ProviderPreset(
        name="minimax",
        description="MiniMax (M2 series, multilingual)",
        default_model="MiniMax-M2-Stable",
        key_prompt="MiniMax API key",
        known_models=(
            "MiniMax-M2-Stable",
            "MiniMax-M2",
            "abab6.5s-chat",
        ),
    ),
    "openai": ProviderPreset(
        name="openai",
        description="OpenAI (GPT-5, GPT-4o)",
        default_model="gpt-4o-mini",
        key_prompt="OpenAI API key (sk-...)",
        known_models=(
            "gpt-4o-mini",
            "gpt-4o",
            "gpt-4-turbo",
            "gpt-5",
            "o1-mini",
            "o1-preview",
        ),
    ),
    "anthropic": ProviderPreset(
        name="anthropic",
        description="Anthropic (Claude family)",
        default_model="claude-sonnet-4-5",
        key_prompt="Anthropic API key (sk-ant-...)",
        known_models=(
            "claude-sonnet-4-5",
            "claude-opus-4-5",
            "claude-haiku-4-5",
            "claude-3-5-sonnet-latest",
            "claude-3-5-haiku-latest",
        ),
    ),
    "custom": ProviderPreset(
        name="custom",
        description="Any OpenAI-compatible endpoint",
        default_model="your-model-id",
        key_prompt="API key",
        needs_base_url=True,
    ),
}
