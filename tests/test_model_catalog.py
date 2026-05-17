"""0.1.9 — refreshable model catalog.

Closes the loop "model ids change upstream all the time" by pulling
the live list from each provider's ``/v1/models`` endpoint and caching
it at ``~/.anthill/model_catalog.json``. The setup wizard's picker
merges this with the static defaults in PROVIDER_PRESETS so users
who refresh see new ids without needing a package update.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_load_catalog_missing_file_returns_empty(tmp_path: Path) -> None:
    from anthill.cli.model_catalog import load_catalog

    assert load_catalog(tmp_path) == {}


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    from anthill.cli.model_catalog import (
        ProviderCatalog,
        load_catalog,
        save_catalog,
    )

    catalog = {
        "deepseek": ProviderCatalog(
            fetched_at=1.0,
            models=("deepseek-chat", "deepseek-reasoner"),
        ),
    }
    save_catalog(tmp_path, catalog)
    reloaded = load_catalog(tmp_path)
    assert "deepseek" in reloaded
    assert reloaded["deepseek"].models == ("deepseek-chat", "deepseek-reasoner")
    assert reloaded["deepseek"].fetched_at == 1.0


def test_load_catalog_handles_corrupt_json(tmp_path: Path) -> None:
    """A broken cache file shouldn't blow up the wizard — degrade to empty."""
    from anthill.cli.model_catalog import CATALOG_FILENAME, load_catalog

    (tmp_path / CATALOG_FILENAME).write_text("not json at all {{{")
    assert load_catalog(tmp_path) == {}


def test_model_ids_for_provider_static_only(tmp_path: Path) -> None:
    """No cache yet ⇒ returns the static known_models tuple."""
    from anthill.cli.model_catalog import model_ids_for_provider

    ids = model_ids_for_provider("deepseek", tmp_path)
    assert "deepseek-chat" in ids
    assert "deepseek-reasoner" in ids


def test_model_ids_for_provider_live_wins_order(tmp_path: Path) -> None:
    """Live ids come first; any static-only ids still surface at the end."""
    from anthill.cli.model_catalog import (
        ProviderCatalog,
        model_ids_for_provider,
        save_catalog,
    )

    # Live cache only knows about one id; static knows two.
    save_catalog(
        tmp_path,
        {
            "deepseek": ProviderCatalog(
                fetched_at=1.0,
                models=("deepseek-future-2027",),
            ),
        },
    )
    ids = model_ids_for_provider("deepseek", tmp_path)
    assert ids[0] == "deepseek-future-2027"
    # The static-only ids "deepseek-chat" / "deepseek-reasoner" still
    # appear so the user has a known-good fallback.
    assert "deepseek-chat" in ids


def test_model_ids_for_unknown_provider_returns_empty(tmp_path: Path) -> None:
    from anthill.cli.model_catalog import model_ids_for_provider

    assert model_ids_for_provider("nonexistent", tmp_path) == ()


@pytest.mark.asyncio
async def test_fetch_models_parses_openai_compat(monkeypatch) -> None:
    """OpenAI-compatible /v1/models response shape is parsed correctly."""
    import httpx

    from anthill.cli.model_catalog import _fetch_models

    class FakeResp:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {
                "data": [
                    {"id": "deepseek-chat"},
                    {"id": "deepseek-reasoner"},
                    {"not_id": "ignored"},  # malformed entry filtered out
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, headers=None):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    models = await _fetch_models("deepseek", "sk-fake", None)
    assert models == ["deepseek-chat", "deepseek-reasoner"]


@pytest.mark.asyncio
async def test_fetch_models_returns_empty_on_http_error(monkeypatch) -> None:
    """Network/auth errors degrade silently — refresh is best-effort."""
    import httpx

    from anthill.cli.model_catalog import _fetch_models

    class BrokenClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, headers=None):
            raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "AsyncClient", BrokenClient)
    assert await _fetch_models("deepseek", "sk-fake", None) == []
