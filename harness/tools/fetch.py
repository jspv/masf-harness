"""fetch_url: pull a link, spill its body to a typed handle, return the handle summary."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from ..session import Session


def fetch_url(session: Session, url: str, max_bytes: int | None = None,
              client: httpx.Client | None = None) -> dict:
    """Fetch ``url`` and store its body as a handle (json if JSON content-type, else text).

    Returns the handle summary. Enforces the session's allowed schemes and byte cap.
    ``client`` is injectable for testing; a real httpx.Client is used by default.
    """
    cfg = session.config.fetch
    limit = max_bytes if max_bytes is not None else cfg.max_bytes

    scheme = urlparse(url).scheme
    if scheme not in cfg.allowed_schemes:
        raise ValueError(f"scheme {scheme!r} not allowed (allowed: {cfg.allowed_schemes})")

    owns_client = client is None
    client = client or httpx.Client(timeout=cfg.timeout_s)
    try:
        resp = client.get(url)
        resp.raise_for_status()
        body = resp.content
        if len(body) > limit:
            raise ValueError(f"response body ({len(body)} bytes) exceeds max_bytes ({limit})")
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            handle = session.store.put(resp.json(), source=f"fetch_url({url})", kind="json")
        else:
            handle = session.store.put(resp.text, source=f"fetch_url({url})", kind="text")
        return handle.summary()
    finally:
        if owns_client:
            client.close()
