"""Plugin abstract interface + registry.

Keep the surface minimal: name, description, async call. A plugin
returns a structured PluginResult so the caller knows whether the call
succeeded and what the cost was.

Plugins are deliberately *named*, not *typed*. Workers refer to them by
short name in prompts, and the runtime maps those names to call sites.
This matches how a real organisation talks ('go ask the research team')
better than passing function pointers around.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PluginResult:
    """The outcome of calling a plugin."""

    output: Any
    ok: bool = True
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Plugin(ABC):
    """A capability the nation can call by name."""

    name: str  # short snake_case identifier (e.g. "web_search")
    description: str  # one-line, used in tool listings

    @abstractmethod
    async def call(self, **kwargs: Any) -> PluginResult:
        """Execute the plugin. Keyword args are plugin-specific."""


class PluginRegistry:
    """In-memory plugin lookup."""

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    def register(self, plugin: Plugin) -> None:
        if not plugin.name:
            raise ValueError("Plugin must declare a non-empty name.")
        self._plugins[plugin.name] = plugin

    def get(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def list(self) -> list[Plugin]:
        return list(self._plugins.values())

    def names(self) -> list[str]:
        return sorted(self._plugins)

    def describe(self) -> str:
        """Human-readable summary, used in CLI listings and prompts."""
        if not self._plugins:
            return "(no plugins registered)"
        return "\n".join(
            f"  {p.name:<20} {p.description}"
            for p in sorted(self._plugins.values(), key=lambda x: x.name)
        )
