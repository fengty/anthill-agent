"""Live model catalog — refreshable list of model ids per provider.

Two sources of truth, merged at read time:

1. **Static defaults** in ``providers_meta.PROVIDER_PRESETS``.
   These ship with the package and cover the common case for users
   who never run ``anthill model catalog refresh``.

2. **Live cache** at ``~/.anthill/model_catalog.json``.
   Populated by ``anthill model catalog refresh`` which calls each
   configured provider's ``/v1/models`` endpoint. This is the
   "iterate later" half of the design — model ids change frequently
   upstream and we don't want to ship a new patch every time
   DeepSeek renames something.

The wizard / model-add picker reads the union: a user who refreshed
yesterday sees yesterday's live ids; a user who never refreshed
still sees a sane default list.

The cache schema is intentionally minimal:

    {
      "fetched_at": 1716000000.0,
      "providers": {
        "deepseek": {
          "fetched_at": 1716000000.0,
          "models": ["deepseek-chat", "deepseek-reasoner"]
        },
        ...
      }
    }

Fields are namespaced so adding more (capabilities, context windows,
prices) later doesn't break old readers.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anthill.cli.providers_meta import PROVIDER_PRESETS


CATALOG_FILENAME = "model_catalog.json"


@dataclass
class ProviderCatalog:
    fetched_at: float
    models: tuple[str, ...]


def _catalog_path(home: Path) -> Path:
    return home / CATALOG_FILENAME


def load_catalog(home: Path) -> dict[str, ProviderCatalog]:
    """Read the live cache. Missing file / parse errors return empty dict."""
    path = _catalog_path(home)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, ProviderCatalog] = {}
    for provider, payload in (data.get("providers") or {}).items():
        models = payload.get("models") or []
        if isinstance(models, list):
            out[provider] = ProviderCatalog(
                fetched_at=float(payload.get("fetched_at", 0.0)),
                models=tuple(m for m in models if isinstance(m, str)),
            )
    return out


def save_catalog(home: Path, catalog: dict[str, ProviderCatalog]) -> None:
    """Write the live cache atomically."""
    path = _catalog_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": time.time(),
        "providers": {
            p: {"fetched_at": c.fetched_at, "models": list(c.models)}
            for p, c in catalog.items()
        },
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def model_ids_for_provider(provider: str, home: Path) -> tuple[str, ...]:
    """The picker's source of truth: union of static + live, live wins on order."""
    static = PROVIDER_PRESETS[provider].known_models if provider in PROVIDER_PRESETS else ()
    cache = load_catalog(home).get(provider)
    if cache is None:
        return static
    # Live first, then any static ids the live list missed (keeps a
    # known-good fallback even if the upstream API hides something).
    seen = set(cache.models)
    extra = tuple(m for m in static if m not in seen)
    return cache.models + extra


# ---------------------------------------------------------------------------
# Refresh — talks to each provider's /v1/models endpoint.
# ---------------------------------------------------------------------------

_PROVIDER_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "minimax": "https://api.minimax.chat/v1",
    # 0.1.20 — additional mainstream providers via OpenAI-compatible
    # endpoints. Each exposes /chat/completions + /models the same way.
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
    "xai": "https://api.x.ai/v1",
    "moonshot": "https://api.moonshot.ai/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
}


async def _fetch_models(provider: str, api_key: str, base_url: str | None) -> list[str]:
    """Probe one provider's models endpoint. Returns [] on any failure."""
    import httpx

    base = base_url or _PROVIDER_BASE_URLS.get(provider)
    if base is None:
        return []

    url = f"{base}/models"
    if provider == "anthropic":
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    else:
        headers = {"authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data: Any = response.json()
    except Exception:  # noqa: BLE001 — refresh is best-effort
        return []

    # Both Anthropic and OpenAI-compat use { "data": [{"id": ...}, ...] }.
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [
        item["id"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


async def refresh_all(home: Path) -> dict[str, ProviderCatalog]:
    """Refresh the catalog for every configured model and return the new cache.

    For each provider that the user has at least one model configured for,
    we pick the first such model's API key and call its ``/v1/models``
    endpoint. Results are merged into the existing cache so a temporarily
    failing provider doesn't wipe out yesterday's good data.
    """
    from anthill.core.userconfig import load_config, load_secrets

    cfg = load_config()
    secrets = load_secrets()

    # One representative (key, base_url) per provider.
    seen: dict[str, tuple[str, str | None]] = {}
    for entry in cfg.models:
        if entry.provider in seen:
            continue
        api_key = secrets.get(entry.secret_ref)
        if not api_key:
            continue
        seen[entry.provider] = (api_key, entry.base_url)

    catalog = load_catalog(home)
    now = time.time()
    for provider, (api_key, base_url) in seen.items():
        models = await _fetch_models(provider, api_key, base_url)
        if models:
            catalog[provider] = ProviderCatalog(fetched_at=now, models=tuple(models))
    save_catalog(home, catalog)
    return catalog
