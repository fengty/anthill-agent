"""0.1.20 — mainstream provider lineup tests.

Beyond DeepSeek / OpenAI / Anthropic / MiniMax we added Google Gemini,
xAI Grok, Moonshot Kimi, Alibaba Qwen, Zhipu GLM via OpenAI-compatible
endpoints. Tests check that:

  - PROVIDER_PRESETS has all 9 providers + custom
  - Each preset has a default in its known_models
  - Each non-custom provider has a base URL wired into both the
    catalog refresher and `anthill model test`
  - core/costs.price_for returns a real (non-fallback) price for
    every declared default — proves the pricing table tracks
    providers_meta.py
  - models/registry.get_provider builds an OpenAICompatibleProvider
    for each new provider when given a configured ModelEntry
"""

from __future__ import annotations

from pathlib import Path

import pytest


MAINSTREAM_PROVIDERS = (
    "deepseek", "openai", "anthropic", "minimax",
    "google", "xai", "moonshot", "qwen", "zhipu",
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_HOME", str(tmp_path))


def test_all_mainstream_providers_present() -> None:
    from anthill.cli.providers_meta import PROVIDER_PRESETS

    for p in MAINSTREAM_PROVIDERS:
        assert p in PROVIDER_PRESETS, p
    assert "custom" in PROVIDER_PRESETS


def test_default_model_in_known_list_for_each_provider() -> None:
    """The picker's default must be in the allow-list — otherwise the
    'unknown id' confirm step fires on Enter."""
    from anthill.cli.providers_meta import PROVIDER_PRESETS

    for name, preset in PROVIDER_PRESETS.items():
        if name == "custom":
            continue  # custom has no allow-list by design
        assert preset.default_model in preset.known_models, (
            f"{name}: default '{preset.default_model}' missing from known_models"
        )


def test_base_urls_wired_for_each_provider() -> None:
    """Catalog refresh + `model test` need to know where to call."""
    from anthill.cli.model_catalog import _PROVIDER_BASE_URLS

    for p in MAINSTREAM_PROVIDERS:
        assert p in _PROVIDER_BASE_URLS, p
        assert _PROVIDER_BASE_URLS[p].startswith("https://"), p


def test_pricing_known_for_each_default_model() -> None:
    """price_for returns a *real* price (not the fallback) for every
    default model. Catches "added the provider, forgot the price row."
    """
    from anthill.cli.providers_meta import PROVIDER_PRESETS
    from anthill.core.costs import _DEFAULT_PRICES_USD

    for name, preset in PROVIDER_PRESETS.items():
        if name == "custom":
            continue
        assert preset.default_model in _DEFAULT_PRICES_USD, (
            f"{name}: default '{preset.default_model}' has no pricing row"
        )


@pytest.mark.parametrize(
    "provider,model",
    [
        ("google", "gemini-3.1-pro-preview"),
        ("xai", "grok-4.3"),
        ("moonshot", "kimi-k2.6"),
        ("qwen", "qwen3-max"),
        ("zhipu", "glm-5"),
    ],
)
def test_registry_builds_openai_compat_provider(provider: str, model: str) -> None:
    """get_provider() builds an OpenAICompatibleProvider for each new
    provider, with the correct base URL baked in from config."""
    from anthill.core.userconfig import (
        ModelEntry,
        UserConfig,
        save_config,
        upsert_secret,
    )
    from anthill.models.openai_compatible import OpenAICompatibleProvider
    from anthill.models.registry import get_provider

    upsert_secret(f"model.{provider}-test", "sk-fake")
    save_config(
        UserConfig(
            default_model=f"{provider}-test",
            models=[
                ModelEntry(
                    name=f"{provider}-test",
                    provider=provider,
                    model=model,
                    secret_ref=f"model.{provider}-test",
                )
            ],
        )
    )
    p = get_provider(f"{provider}-test")
    assert isinstance(p, OpenAICompatibleProvider)
    assert p.model == model
    # Base URL gets auto-filled from _DEFAULT_BASE_URLS.
    assert p.base_url.startswith("https://")


def test_qwen_known_models_excludes_dated_snapshots() -> None:
    """The picker offers the evergreen ids; dated snapshots are out —
    users on a pinned snapshot can refresh the live catalog."""
    from anthill.cli.providers_meta import PROVIDER_PRESETS

    for m in PROVIDER_PRESETS["qwen"].known_models:
        assert "-2025-" not in m
        assert "-2026-" not in m


def test_xai_excludes_retired_grok_4() -> None:
    """grok-4, grok-4-fast etc retired 2026-05-15 — must not be offered."""
    from anthill.cli.providers_meta import PROVIDER_PRESETS

    known = PROVIDER_PRESETS["xai"].known_models
    for retired in ("grok-4", "grok-4-fast", "grok-4-1-fast", "grok-code-fast-1"):
        assert retired not in known, f"retired id {retired} still in known_models"
