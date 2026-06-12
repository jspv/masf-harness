"""Conversation: a stateful, multi-turn handle over a persistent workspace.

It keeps one workspace ``Session`` open across turns (so handles persist) and reuses one MAF
``AgentSession`` (so conversation history threads, MAF-managed). ``aask`` runs one turn under a
single-flight lock; ``aclose`` tears down MCP/bus and reaps the workspace unless ``reap_on_close``
is False. Async-first: a persistent agent + MCP connections require one stable event loop.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from typing import TYPE_CHECKING, Any

from .config import TetherConfig
from .session import Session

if TYPE_CHECKING:
    from .api import Result


class Conversation:
    def __init__(self, conv_id: str, session: Session, agent: Any, agent_session: Any,
                 *, reap_on_close: bool = True) -> None:
        self.id = conv_id
        self.session = session
        self.agent = agent
        self.agent_session = agent_session
        self.reap_on_close = reap_on_close
        self.last_activity = time.monotonic()
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def acreate(cls, *, id: str, config: TetherConfig, client: Any,
                      tools: list | None = None, bundles: tuple[str, ...] = ("code", "files", "web"),
                      reap_on_close: bool = True) -> "Conversation":
        """Open the workspace, build the agent once, start a MAF conversation thread."""
        session = Session.create(config)
        await session.__aenter__()
        try:
            agent = await session.create_agent(client, agent_instructions=None,
                                                tools=tools or [], bundles=bundles)
            agent_session = agent.create_session(session_id=id)
        except BaseException:
            await session.__aexit__(None, None, None)
            raise
        return cls(id, session, agent, agent_session, reap_on_close=reap_on_close)

    async def aask(self, question: str) -> "Result":
        """Run one turn (serialized per conversation). Returns a Result; errors are captured."""
        from .api import Result

        async with self._lock:
            final_text, error = "", None
            try:
                response = await self.agent.run(question, session=self.agent_session)
                final_text = response.text
            except Exception as e:  # noqa: BLE001 - surface, don't crash the conversation
                error = f"{type(e).__name__}: {e}"
            self.last_activity = time.monotonic()
            return Result(final_text=final_text, handles=dict(self.session.handles),
                          files=self.session.artifacts, session_dir=self.session.root, error=error)

    async def aclose(self) -> None:
        """Tear down the workspace (MCP + bus), then reap the root unless retained. Idempotent."""
        if self._closed:
            return
        self._closed = True   # mark closed up front: a teardown failure isn't retried
        try:
            await self.session.__aexit__(None, None, None)
        finally:
            if self.reap_on_close and self.session.root.exists():
                shutil.rmtree(self.session.root, ignore_errors=True)
