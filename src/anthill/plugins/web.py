"""Web plugins — fetch and search.

`web_fetch`  pulls a URL via httpx, strips HTML to readable text.
`web_search` calls a search API.

Search requires an API key in 2026: every free HTML endpoint
(DuckDuckGo, Bing, Baidu) has either gone JavaScript-rendered or
captcha-walls anonymous traffic. Anthill supports any of:

    BOCHA_API_KEY     — Bocha web-search API (bochaai.com)
    TAVILY_API_KEY    — Tavily search, generous free tier (tavily.com)
    SERPER_API_KEY    — Google results via serper.dev
    BRAVE_API_KEY     — Brave Search API

Order of precedence above. The first key found wins. With none of
them, web_search refuses with a clear message rather than pretending
to scrape.
"""

from __future__ import annotations

import os
import re
from typing import Any

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
    description = (
        "Search the web (requires BOCHA/TAVILY/SERPER/BRAVE_API_KEY)."
    )

    async def call(self, *, query: str, top_k: int = 5, **_: Any) -> PluginResult:
        if not query.strip():
            return PluginResult(output=[], ok=False, error="empty query")

        bocha_key = os.getenv("BOCHA_API_KEY") or os.getenv("ANTHILL_BOCHA_KEY")
        tavily_key = os.getenv("TAVILY_API_KEY") or os.getenv("ANTHILL_TAVILY_KEY")
        serper_key = os.getenv("SERPER_API_KEY") or os.getenv("ANTHILL_SERPER_KEY")
        brave_key = os.getenv("BRAVE_API_KEY") or os.getenv("ANTHILL_BRAVE_KEY")

        if bocha_key:
            return await self._bocha(query, top_k, bocha_key)
        if tavily_key:
            return await self._tavily(query, top_k, tavily_key)
        if serper_key:
            return await self._serper(query, top_k, serper_key)
        if brave_key:
            return await self._brave(query, top_k, brave_key)

        return PluginResult(
            output=[],
            ok=False,
            error=(
                "web_search needs an API key. Free HTML scraping no longer "
                "works in 2026 (DuckDuckGo, Bing, Baidu all block or "
                "require JS rendering). Set one of:\n"
                "  export BOCHA_API_KEY=sk-...      (bochaai.com)\n"
                "  export TAVILY_API_KEY=tvly-...   (tavily.com, free tier)\n"
                "  export SERPER_API_KEY=...        (serper.dev, Google)\n"
                "  export BRAVE_API_KEY=...         (Brave Search)"
            ),
        )

    @staticmethod
    async def _bocha(query: str, top_k: int, api_key: str) -> PluginResult:
        """Bocha web-search API. Clean JSON, strong CJK results.

        Docs: https://api.bochaai.com/v1/web-search
        Returns up to 50 results per call. We slice to top_k.
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://api.bochaai.com/v1/web-search",
                    json={"query": query, "count": min(top_k, 50), "summary": False},
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                data = response.json()
                # Bocha wraps results under data.webPages.value.
                pages = (
                    data.get("data", {}).get("webPages", {}).get("value", [])
                    if isinstance(data.get("data"), dict)
                    else data.get("webPages", {}).get("value", [])
                )
                results = [
                    {
                        "title": p.get("name"),
                        "url": p.get("url"),
                        "snippet": p.get("snippet") or p.get("summary"),
                    }
                    for p in pages[:top_k]
                ]
                return PluginResult(output=results, metadata={"engine": "bocha"})
        except Exception as e:  # noqa: BLE001
            return PluginResult(
                output=[],
                ok=False,
                error=f"{type(e).__name__}: {e}" if str(e) else type(e).__name__,
            )

    @staticmethod
    async def _serper(query: str, top_k: int, api_key: str) -> PluginResult:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://google.serper.dev/search",
                    json={"q": query, "num": top_k},
                    headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
                results = [
                    {"title": r.get("title"), "url": r.get("link"), "snippet": r.get("snippet")}
                    for r in data.get("organic", [])[:top_k]
                ]
                return PluginResult(output=results, metadata={"engine": "serper"})
        except Exception as e:  # noqa: BLE001
            return PluginResult(
                output=[],
                ok=False,
                error=f"{type(e).__name__}: {e}" if str(e) else type(e).__name__,
            )

    @staticmethod
    async def _brave(query: str, top_k: int, api_key: str) -> PluginResult:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": top_k},
                    headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
                web_results = data.get("web", {}).get("results", [])
                results = [
                    {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("description")}
                    for r in web_results[:top_k]
                ]
                return PluginResult(output=results, metadata={"engine": "brave"})
        except Exception as e:  # noqa: BLE001
            return PluginResult(
                output=[],
                ok=False,
                error=f"{type(e).__name__}: {e}" if str(e) else type(e).__name__,
            )

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

