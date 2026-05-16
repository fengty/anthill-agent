"""Web plugins — fetch and search.

`web_fetch`  pulls a URL via httpx, strips HTML to readable text.
`web_search` queries a search API; we default to DuckDuckGo's HTML
             frontend because it needs no API key. Users with SerpAPI
             or Tavily keys can swap them in via env vars.

Both are intentionally simple — the nation does not need a browser
automation stack to get value out of fetching a page. We can layer a
real browser plugin later if real workflows need JS-rendered sites.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote_plus

import httpx

from anthill.plugins.base import Plugin, PluginResult


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    """Crude HTML → text. Strip tags, collapse whitespace.

    Real readers (e.g. trafilatura) are vastly better, but they pull a
    big dep. For first-pass usage, this is fine.
    """
    text = _TAG_RE.sub(" ", html)
    text = _WS_RE.sub(" ", text)
    return text.strip()


class WebFetchPlugin(Plugin):
    name = "web_fetch"
    description = "Fetch a URL and return its readable text content."

    async def call(self, *, url: str, max_chars: int = 4000, **_: Any) -> PluginResult:
        if not url or not url.startswith(("http://", "https://")):
            return PluginResult(output=None, ok=False, error=f"invalid url: {url!r}")
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(
                    url,
                    headers={"User-Agent": "Anthill/0.1 (web_fetch)"},
                )
                response.raise_for_status()
                text = _strip_html(response.text)
                truncated = text[:max_chars]
                return PluginResult(
                    output=truncated,
                    metadata={
                        "url": str(response.url),
                        "status": response.status_code,
                        "truncated": len(text) > max_chars,
                        "char_count": len(text),
                    },
                )
        except Exception as e:  # noqa: BLE001
            return PluginResult(output=None, ok=False, error=str(e))


class WebSearchPlugin(Plugin):
    name = "web_search"
    description = "Search the web and return top result snippets."

    async def call(self, *, query: str, top_k: int = 5, **_: Any) -> PluginResult:
        if not query.strip():
            return PluginResult(output=[], ok=False, error="empty query")

        # Prefer Tavily if its key is set — it returns clean JSON snippets.
        tavily_key = os.getenv("TAVILY_API_KEY") or os.getenv("ANTHILL_TAVILY_KEY")
        if tavily_key:
            return await self._tavily(query, top_k, tavily_key)
        return await self._duckduckgo(query, top_k)

    @staticmethod
    async def _tavily(query: str, top_k: int, api_key: str) -> PluginResult:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://api.tavily.com/search",
                    json={"api_key": api_key, "query": query, "max_results": top_k},
                )
                response.raise_for_status()
                data = response.json()
                results = [
                    {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content")}
                    for r in data.get("results", [])
                ]
                return PluginResult(output=results, metadata={"engine": "tavily"})
        except Exception as e:  # noqa: BLE001
            return PluginResult(output=[], ok=False, error=str(e))

    @staticmethod
    async def _duckduckgo(query: str, top_k: int) -> PluginResult:
        """Use DuckDuckGo HTML frontend.

        Best-effort: no API key, may rate limit. For serious use, set
        TAVILY_API_KEY.
        """
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Anthill web_search)"},
                )
                response.raise_for_status()
                html = response.text
        except Exception as e:  # noqa: BLE001
            return PluginResult(output=[], ok=False, error=str(e))

        # Crude scrape of result blocks.
        items: list[dict] = []
        for match in re.finditer(
            r'<a class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
            r'.*?<a class="result__snippet"[^>]*>(.*?)</a>',
            html,
            flags=re.DOTALL,
        ):
            url_raw, title_html, snippet_html = match.groups()
            items.append(
                {
                    "url": url_raw,
                    "title": _strip_html(title_html),
                    "snippet": _strip_html(snippet_html),
                }
            )
            if len(items) >= top_k:
                break
        return PluginResult(output=items, metadata={"engine": "duckduckgo"})
