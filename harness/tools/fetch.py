"""fetch_url: pull a link, spill its body to a typed handle, return the handle summary.

Network/HTTP failures are returned as structured error dicts (not raised) so the agent
can adapt -- e.g. follow a different link or report the problem -- rather than dead-ending
on a "function failed". Redirects are followed and a browser User-Agent is sent so
ordinary anti-bot pages don't 403 a bare client.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from ..config import FetchConfig
from ..session import Session

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _default_client(cfg: FetchConfig) -> httpx.Client:
    return httpx.Client(
        timeout=cfg.timeout_s,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    )


def fetch_url(session: Session, url: str, max_bytes: int | None = None,
              raw: bool = False, client: httpx.Client | None = None) -> dict:
    """Fetch ``url`` and store its body as a handle (json if JSON content-type, else text).

    HTML responses are converted to clean markdown via trafilatura (stripping nav, footer,
    ads, and other boilerplate) unless ``raw=True`` is passed, in which case the raw HTML
    text is stored unchanged.

    Returns the handle summary on success, or ``{"error", "status", "url"}`` on an HTTP
    error / network failure. Follows redirects and sends a browser User-Agent. Enforces
    the session's allowed schemes and byte cap. ``client`` is injectable for testing.
    """
    cfg = session.config.fetch
    limit = max_bytes if max_bytes is not None else cfg.max_bytes

    scheme = urlparse(url).scheme
    if scheme not in cfg.allowed_schemes:
        raise ValueError(f"scheme {scheme!r} not allowed (allowed: {cfg.allowed_schemes})")

    owns_client = client is None
    client = client or _default_client(cfg)
    try:
        try:
            resp = client.get(url)
        except httpx.HTTPError as e:
            return {"error": f"request failed: {e}", "status": None, "url": url}

        if resp.is_error:  # 4xx/5xx -> structured error the model can adapt to
            return {"error": f"HTTP {resp.status_code} for {resp.url}",
                    "status": resp.status_code, "url": str(resp.url)}

        body = resp.content
        truncated = len(body) > limit
        content_type = resp.headers.get("content-type", "")
        text = body[:limit].decode(resp.encoding or "utf-8", errors="replace")

        if "json" in content_type and not truncated:
            handle = session.store.put(resp.json(), source=f"fetch_url({url})", kind="json")
        elif "html" in content_type and not raw:
            import trafilatura
            md = trafilatura.extract(text, output_format="markdown")
            stored = md if md else text  # fall back to raw text if extraction yields nothing
            handle = session.store.put(stored, source=f"fetch_url({url})", kind="text")
        else:
            handle = session.store.put(text, source=f"fetch_url({url})", kind="text")

        summary = handle.summary()
        if truncated:
            summary["truncated"] = True
            summary["full_bytes"] = len(body)
        return summary
    finally:
        if owns_client:
            client.close()
