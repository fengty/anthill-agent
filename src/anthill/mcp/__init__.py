"""MCP (Model Context Protocol) integration.

Two halves:

    server  — expose Anthill's plugin registry as MCP tools so any
              MCP-compatible client (Claude Desktop, Cursor, etc.)
              can call them.

    client  — register a remote MCP server as a plugin source so its
              tools appear in Anthill's plugin registry like native ones.

We implement the protocol manually (HTTP+JSON) rather than depending on
the official `mcp` SDK. The reasons:

    1. Zero extra dependency. MCP's transport is plain JSON-RPC; we
       already speak HTTP via FastAPI/httpx.
    2. Forward-compatibility. The spec is moving fast (HTTP transport,
       stdio transport, SSE transport). Hand-rolling lets us follow it.
    3. Testability. No subprocess management, no spec-version pinning.

This is a v0.1.x first cut: tools/list and tools/call only. resources/*,
prompts/*, sampling/* are spec-compliant TODOs for v0.2.
"""

from anthill.mcp.server import build_mcp_app
from anthill.mcp.client import McpClient, McpRemotePlugin

__all__ = ["build_mcp_app", "McpClient", "McpRemotePlugin"]
