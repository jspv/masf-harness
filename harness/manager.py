"""SessionManager: an in-process registry of continuous Conversations, keyed by id.

Open-or-create by id (e.g. an AG-UI threadId), get, close, lazy TTL expiry, and a sweep the host
can call on its own cadence. Each conversation gets an isolated per-id root so concurrent
conversations never collide. The ConversationStore seam lets a disk-backed impl drop in later.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from .conversation import Conversation


class ConversationStore(Protocol):
    def get(self, conv_id: str) -> Conversation | None: ...
    def put(self, conv_id: str, conv: Conversation) -> None: ...
    def pop(self, conv_id: str) -> Conversation | None: ...
    def items(self) -> list[tuple[str, Conversation]]: ...


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._d: dict[str, Conversation] = {}

    def get(self, conv_id: str) -> Conversation | None:
        return self._d.get(conv_id)

    def put(self, conv_id: str, conv: Conversation) -> None:
        self._d[conv_id] = conv

    def pop(self, conv_id: str) -> Conversation | None:
        return self._d.pop(conv_id, None)

    def items(self) -> list[tuple[str, Conversation]]:
        return list(self._d.items())


def _conv_root(config: Any, conv_id: str) -> Path:
    """Isolated per-conversation root. Uses config.root_dir as a base, else ./.harness/sessions."""
    base = Path(config.root_dir) if config.root_dir else (Path.cwd() / ".harness" / "sessions")
    return base / conv_id


class SessionManager:
    def __init__(self, harness: Any, *, idle_ttl_s: float | None = None,
                 store: ConversationStore | None = None) -> None:
        self._harness = harness
        self._ttl = idle_ttl_s
        self._store: ConversationStore = store or InMemoryConversationStore()
        self._lock = asyncio.Lock()

    def _expired(self, conv: Conversation) -> bool:
        return self._ttl is not None and (time.monotonic() - conv.last_activity) > self._ttl

    async def aopen(self, session_id: str | None = None, *, tools: list | None = None,
                    bundles: tuple[str, ...] | None = None) -> Conversation:
        """Reuse the live conversation for ``session_id`` (if any, not expired), else create one."""
        async with self._lock:
            if session_id is not None:
                existing = self._store.get(session_id)
                if existing is not None and not self._expired(existing):
                    return existing
                if existing is not None:               # expired -> reap before recreating
                    self._store.pop(session_id)
                    await existing.aclose()
            conv_id = session_id or uuid.uuid4().hex
            config = dataclasses.replace(
                self._harness.config, root_dir=str(_conv_root(self._harness.config, conv_id)))
            conv = await Conversation.acreate(
                id=conv_id, config=config, client=self._harness._make_client(),
                tools=self._harness._tools + (tools or []),
                bundles=bundles if bundles is not None else self._harness._bundles,
                reap_on_close=True,
            )
            self._store.put(conv_id, conv)
            return conv

    def get(self, session_id: str) -> Conversation | None:
        conv = self._store.get(session_id)
        if conv is None or self._expired(conv):
            return None
        return conv

    async def close(self, session_id: str) -> None:
        conv = self._store.pop(session_id)
        if conv is not None:
            await conv.aclose()

    async def sweep(self) -> None:
        for conv_id, conv in self._store.items():
            if self._expired(conv):
                self._store.pop(conv_id)
                await conv.aclose()

    async def aclose(self) -> None:
        for conv_id, conv in self._store.items():
            self._store.pop(conv_id)
            await conv.aclose()
