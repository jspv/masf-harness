"""Web research tools backed by Tavily (search + extract).

Failures return structured error dicts (never raise into the agent loop), matching the
fetch_url convention. A missing TAVILY_API_KEY returns a clear, actionable message.
"""

from __future__ import annotations

import httpx

from ..session import Session

_SEARCH_URL = "https://api.tavily.com/search"
_EXTRACT_URL = "https://api.tavily.com/extract"
_SNIPPET_CHARS = 500


def web_search(session: Session, query: str, max_results: int = 5,
               client: httpx.Client | None = None) -> dict:
    """Search the web. Returns {"answer": str|None, "results": [{title, url, content, score}]}.
    `content` is a clipped extracted snippet. Use the urls with web_extract/fetch_url for more."""
    cfg = session.config.search
    if not cfg.api_key:
        return {"error": "web search unavailable: set TAVILY_API_KEY in .env"}
    owns = client is None
    client = client or httpx.Client(timeout=cfg.timeout_s)
    try:
        try:
            resp = client.post(_SEARCH_URL, json={
                "api_key": cfg.api_key, "query": query,
                "max_results": max_results, "include_answer": True,
                "search_depth": "basic",
            })
        except httpx.HTTPError as e:
            return {"error": f"search request failed: {e}"}
        if resp.is_error:
            return {"error": f"search HTTP {resp.status_code}", "status": resp.status_code}
        data = resp.json()
        results = [{
            "title": r.get("title"),
            "url": r.get("url"),
            "content": (r.get("content") or "")[:_SNIPPET_CHARS],
            "score": r.get("score"),
        } for r in data.get("results", [])]
        return {"answer": data.get("answer"), "results": results}
    finally:
        if owns:
            client.close()
