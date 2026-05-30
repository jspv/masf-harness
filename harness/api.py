"""Public entry point: Harness / solve() returning a Result."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import build_agent, run_agent_sync
from .config import HarnessConfig
from .session import Session


@dataclass
class Result:
    final_text: str
    handles: dict[str, Any]
    files: list[str]
    session_dir: Path


class Harness:
    """A reusable harness: holds config + a Session and runs the agent on tasks."""

    def __init__(self, config: HarnessConfig | None = None, client: Any | None = None) -> None:
        self.config = config or HarnessConfig()
        self._client = client
        self.session = Session.create(self.config)

    def _make_client(self):
        if self._client is not None:
            return self._client
        from agent_framework.openai import OpenAIChatClient
        return OpenAIChatClient(model=self.config.model, env_file_path=".env")

    def solve(self, problem: str, tools: list | None = None, on_tool_call=None) -> Result:
        agent = build_agent(self.session, self.config, self._make_client(),
                            extra_tools=tools, on_tool_call=on_tool_call)
        resp = run_agent_sync(agent, problem)
        return Result(
            final_text=resp.text,
            handles=self.session.store.manifest(),
            files=self._user_files(),
            session_dir=self.session.root,
        )

    def _user_files(self) -> list[str]:
        """User-meaningful files written during the run, excluding internal scratch
        (handle storage, inline scripts, control files)."""
        out: list[str] = []
        for p in sorted(self.session.root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.session.root)
            top = rel.parts[0]
            if top in ("handles", ".scripts") or top.startswith("_"):
                continue
            out.append(rel.as_posix())
        return out


def solve(problem: str, *, tools: list | None = None,
          config: HarnessConfig | None = None, client: Any | None = None,
          on_tool_call=None) -> Result:
    """One-shot convenience: build a Harness and solve a single problem."""
    return Harness(config, client=client).solve(problem, tools=tools, on_tool_call=on_tool_call)
