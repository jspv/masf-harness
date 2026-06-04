"""A Session bundles one run's root directory, handle store, and sandbox."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import bundles as _bundles
from .config import HarnessConfig
from .handles import HandleStore
from .sandbox import LocalSubprocessSandbox


@dataclass
class Session:
    root: Path
    store: HandleStore
    sandbox: LocalSubprocessSandbox
    config: HarnessConfig
    _mcp_connected: list[Any] = field(default_factory=list, init=False, repr=False)

    @classmethod
    def create(cls, config: HarnessConfig) -> "Session":
        root = _resolve_root(config)
        root.mkdir(parents=True, exist_ok=True)
        store = HandleStore(root)
        sandbox = LocalSubprocessSandbox(root=root, store=store, config=config.sandbox)
        return cls(root=root, store=store, sandbox=sandbox, config=config)

    @property
    def handles(self) -> dict[str, Any]:
        """Handle summaries produced during the run, by id."""
        return self.store.manifest()

    @property
    def artifacts(self) -> list[str]:
        """User-meaningful files under root, excluding handle storage and scratch."""
        out: list[str] = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.root)
            top = rel.parts[0]
            if top in ("handles", ".scripts") or top.startswith("_"):
                continue
            out.append(rel.as_posix())
        return out

    async def aclose(self) -> None:
        """Close every connected MCP server, then honor the cleanup policy."""
        for tool in self._mcp_connected:
            try:
                await tool.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        self._mcp_connected.clear()
        if self.config.cleanup and self.root.exists():
            shutil.rmtree(self.root)

    def tools(self, *bundles: str) -> list:
        """The built-in tool callables for the selected bundles (default: all)."""
        from .tools.registry import build_tools  # local import avoids circular dependency
        wanted = _bundles.tool_names_for(bundles)
        return [t for t in build_tools(self) if t.__name__ in wanted]

    def harness_instructions(self, *bundles: str) -> str:
        """The operating-manual text (core + selected bundles)."""
        return _bundles.instructions_for(bundles)

    async def __aenter__(self) -> "Session":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)


def _resolve_root(config: HarnessConfig) -> Path:
    if config.root_dir is not None:
        return Path(config.root_dir).resolve()
    base = Path.cwd() / ".harness" / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    existing = [int(p.name) for p in base.iterdir() if p.name.isdigit()]
    next_id = (max(existing) + 1) if existing else 1
    return (base / str(next_id)).resolve()
