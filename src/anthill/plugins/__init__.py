"""Plugins — capabilities the nation can call on top of pure LLM inference.

A plugin is a callable with a known name and a typed input/output. The
Plugin Registry lets the nation discover what tools it has and dispatch
to them by name from inside a worker prompt.

The first set of built-in plugins covers the two most common needs:
fetching web pages and searching the web. These are the difference
between a 'nation that talks' and a 'nation that knows what's happening
in the world right now.'

Why not MCP? MCP is the obvious choice and we will adopt it in v0.2.
Right now, a small Python registry is sufficient and avoids the extra
process / network surface during early iteration.
"""

from anthill.plugins.base import Plugin, PluginRegistry, PluginResult
from anthill.plugins.registry import default_registry

__all__ = ["Plugin", "PluginRegistry", "PluginResult", "default_registry"]
