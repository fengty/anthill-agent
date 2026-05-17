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


# 0.1.19 — model IDs verified against each provider's official docs
# in May 2026. Deprecated / retired ids are NOT carried as fallbacks —
# they will simply fail the API call when used. The catalog refresh
# command (`anthill model catalog refresh`) always wins over this
# static list, so users on the bleeding edge of new releases don't
# need to wait for a package update.
PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "deepseek": ProviderPreset(
        name="deepseek",
        description="DeepSeek (cheap, 1M context, OpenAI-compatible)",
        default_model="deepseek-v4-pro",
        key_prompt="DeepSeek API key (sk-...)",
        # Per api-docs.deepseek.com (May 2026). deepseek-chat /
        # deepseek-reasoner are retiring 2026-07-24 and intentionally
        # omitted here so the picker doesn't steer users at a
        # countdown-to-broken default.
        known_models=(
            "deepseek-v4-pro",
            "deepseek-v4-flash",
        ),
    ),
    "minimax": ProviderPreset(
        name="minimax",
        description="MiniMax (M2.7 — Chinese strong, multilingual)",
        default_model="MiniMax-M2.7",
        key_prompt="MiniMax API key",
        # Per platform.minimax.io (May 2026). The legacy M2 / abab6.5
        # ids are intentionally dropped — superseded by M2.x.
        known_models=(
            "MiniMax-M2.7",
            "MiniMax-M2.7-highspeed",
            "MiniMax-M2.5",
            "MiniMax-M2.1",
        ),
    ),
    "openai": ProviderPreset(
        name="openai",
        description="OpenAI (GPT-5.5 / 5.4 / o-series)",
        default_model="gpt-5.5",
        key_prompt="OpenAI API key (sk-...)",
        # Per developers.openai.com/api/docs/models/all (May 2026).
        # GPT-4o family / o1-mini / o1-preview omitted — superseded
        # by GPT-5.x and o3.
        known_models=(
            "gpt-5.5",
            "gpt-5.5-pro",
            "gpt-5.4",
            "gpt-5.4-pro",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
            "gpt-5.3-codex",
            "o3",
            "o3-pro",
        ),
    ),
    "anthropic": ProviderPreset(
        name="anthropic",
        description="Anthropic (Claude 4.x — Opus / Sonnet / Haiku)",
        default_model="claude-opus-4-7",
        key_prompt="Anthropic API key (sk-ant-...)",
        # Per platform.claude.com/docs/.../models/overview (May 2026).
        # Recent legacy ids (4.6 / 4.5 / 4.1) kept since the docs still
        # mark them active. claude-3-5-* dropped — superseded.
        known_models=(
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
            "claude-opus-4-6",
            "claude-sonnet-4-5",
            "claude-opus-4-5",
            "claude-opus-4-1",
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
