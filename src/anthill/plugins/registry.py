"""The shared default registry, pre-populated with built-in plugins."""

from __future__ import annotations

from anthill.plugins.base import PluginRegistry
from anthill.plugins.web import WebFetchPlugin, WebSearchPlugin


def _build_default() -> PluginRegistry:
    reg = PluginRegistry()
    reg.register(WebFetchPlugin())
    reg.register(WebSearchPlugin())
    return reg


default_registry = _build_default()
