"""Provider registry — three resolution paths, in priority order.

  1. User config.       'work-deepseek' in ~/.anthill/config.toml wins
                        first. Reads provider + model + key from the
                        ModelEntry, no env required.
  2. Built-in alias.    'deepseek-chat', 'minimax-m2-stable', etc map
                        to a hardcoded constructor that respects env
                        vars (backward compatibility).
  3. Custom factory.    register_provider(name, fn) lets tests and
                        downstream code inject their own.

The legacy aliases stay because old code (spawn() in the demo, the
test scripts) still uses provider names like 'deepseek-chat' directly.
v0.3 may deprecate them.
"""

from __future__ import annotations

from typing import Callable

from anthill.models.base import ModelProvider
from anthill.models.deepseek import DeepSeekProvider
from anthill.models.minimax import MiniMaxProvider
from anthill.models.openai_compatible import OpenAICompatibleProvider


# --- Built-in aliases (legacy path, env-var based) --------------------

_REGISTRY: dict[str, Callable[[], ModelProvider]] = {
    "deepseek": lambda: DeepSeekProvider(),
    "deepseek-chat": lambda: DeepSeekProvider(model="deepseek-chat"),
    "deepseek-reasoner": lambda: DeepSeekProvider(model="deepseek-reasoner"),
    "minimax": lambda: MiniMaxProvider(),
    "minimax-m2-stable": lambda: MiniMaxProvider(model="MiniMax-M2-Stable"),
    "minimax-m2": lambda: MiniMaxProvider(model="MiniMax-M2"),
    "minimax-m2.5": lambda: MiniMaxProvider(model="MiniMax-M2.5"),
}


def register_provider(name: str, factory: Callable[[], ModelProvider]) -> None:
    """Register a new provider factory under a short name."""
    _REGISTRY[name] = factory


def known_providers() -> list[str]:
    return sorted(_REGISTRY)


# --- New path: resolve a model name through user config ---------------

def _from_user_config(name: str) -> ModelProvider | None:
    """If `name` matches a ModelEntry in ~/.anthill/config.toml, build
    a provider from it (key sourced from secrets.toml). Returns None
    if not found so the caller can fall back to the alias path."""
    from anthill.core.userconfig import load_config, secret_for

    cfg = load_config()
    entry = cfg.find_model(name)
    if entry is None:
        return None

    api_key = secret_for(entry.secret_ref) or ""
    if not api_key:
        raise RuntimeError(
            f"Model '{name}' has no API key. Either run "
            f"`anthill model add` again or edit ~/.anthill/secrets.toml."
        )

    provider = entry.provider.lower()
    if provider == "deepseek":
        return DeepSeekProvider(
            api_key=api_key,
            model=entry.model,
        )
    if provider == "minimax":
        # MiniMax wants a group id in addition to the key. Stored as
        # an extra. Fall back to env for backward compat.
        group_id = entry.extra.get("group_id")
        return MiniMaxProvider(
            api_key=api_key,
            group_id=group_id,
            model=entry.model,
        )
    if provider in _OPENAI_COMPAT_PROVIDERS or provider == "custom":
        # OpenAI-compatible covers a growing set of providers — OpenAI
        # itself, Anthropic (via the OpenAI-compat base_url), and the
        # 5 added in 0.1.20 (google/xai/moonshot/qwen/zhipu). All
        # speak the same /chat/completions shape; only the base URL
        # changes.
        base_url = entry.base_url or _DEFAULT_BASE_URLS.get(provider)
        if not base_url:
            raise RuntimeError(
                f"Provider '{provider}' needs base_url. "
                f"Run `anthill model add` or edit config.toml."
            )
        return OpenAICompatibleProvider(
            api_key=api_key,
            model=entry.model,
            base_url=base_url,
            provider_name=provider,
        )

    raise RuntimeError(f"unknown provider '{entry.provider}' for model '{name}'")


_OPENAI_COMPAT_PROVIDERS = frozenset({
    "openai", "anthropic",
    # 0.1.20 — additional mainstream providers via OpenAI-compatible mode.
    "google", "xai", "moonshot", "qwen", "zhipu",
})

_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
    "xai": "https://api.x.ai/v1",
    "moonshot": "https://api.moonshot.ai/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
}


# --- Public lookup: try config first, then aliases --------------------

def get_provider(name: str) -> ModelProvider:
    """Resolve a model name to a ModelProvider.

    Lookup order:
        1. User-config alias (anthill model add)
        2. Built-in legacy alias (env-driven)
        3. KeyError with a helpful list

    Raises KeyError if nothing matches, RuntimeError if config matched
    but the configured key is missing or invalid.
    """
    via_config = _from_user_config(name)
    if via_config is not None:
        return via_config

    if name in _REGISTRY:
        return _REGISTRY[name]()

    known_set = set(_REGISTRY)
    try:
        from anthill.core.userconfig import load_config
        known_set.update(m.name for m in load_config().models)
    except Exception:  # noqa: BLE001
        pass
    known = ", ".join(sorted(known_set))
    raise KeyError(f"Unknown model '{name}'. Known: {known or '(none)'}")
