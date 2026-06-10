"""Public entry point: Harness / solve() returning a Result."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import HarnessConfig
from .session import Session
from .status import StatusEvent

_StatusSink = Callable[[StatusEvent], None]


@dataclass
class Result:
    final_text: str
    handles: dict[str, Any]
    files: list[str]
    session_dir: Path
    error: str | None = None


class Harness:
    """Reusable harness: builds a Session per run via the composable create_agent path."""

    def __init__(self, config: HarnessConfig | None = None, client: Any | None = None,
                 *, tools: list | None = None,
                 bundles: tuple[str, ...] = ("code", "files", "web"),
                 on_status: _StatusSink | None = None) -> None:
        self.config = config or HarnessConfig()
        self._client = client
        self._tools = tools or []
        self._bundles = bundles
        self._on_status = on_status
        if self.config.search.api_key is None:
            import os

            from dotenv import load_dotenv
            load_dotenv()
            self.config.search.api_key = os.environ.get("TAVILY_API_KEY")

    def _make_client(self):
        if self._client is not None:
            return self._client
        from agent_framework.openai import OpenAIChatClient
        return OpenAIChatClient(model=self.config.model, env_file_path=".env")

    async def asolve(self, problem: str, tools: list | None = None, *,
                     on_status: _StatusSink | None = None) -> Result:
        final_text, error = "", None
        sink = on_status if on_status is not None else self._on_status
        async with Session.create(self.config) as session:
            if sink is not None:
                session.subscribe(sink)
            agent = await session.create_agent(
                self._make_client(),
                agent_instructions=None,
                tools=self._tools + (tools or []),
                bundles=self._bundles,
            )
            try:
                response = await agent.run(problem)
                final_text = response.text
            except Exception as e:  # noqa: BLE001 - surface, don't crash; keep work-so-far
                error = f"{type(e).__name__}: {e}"
            return Result(
                final_text=final_text,
                handles=dict(session.handles),
                files=session.artifacts,
                session_dir=session.root,
                error=error,
            )

    def solve(self, problem: str, tools: list | None = None, *,
              on_status: _StatusSink | None = None) -> Result:
        return asyncio.run(self.asolve(problem, tools=tools, on_status=on_status))


def solve(problem: str, *, tools: list | None = None,
          config: HarnessConfig | None = None, client: Any | None = None,
          on_status: _StatusSink | None = None) -> Result:
    """One-shot convenience: build a Harness and solve a single problem."""
    return Harness(config, client=client, tools=tools, on_status=on_status).solve(problem)
