"""A Session bundles one run's root directory, handle store, and sandbox."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import HarnessConfig
from .handles import HandleStore
from .sandbox import LocalSubprocessSandbox


@dataclass
class Session:
    root: Path
    store: HandleStore
    sandbox: LocalSubprocessSandbox
    config: HarnessConfig

    @classmethod
    def create(cls, config: HarnessConfig) -> "Session":
        root = _resolve_root(config)
        root.mkdir(parents=True, exist_ok=True)
        store = HandleStore(root)
        sandbox = LocalSubprocessSandbox(root=root, store=store, config=config.sandbox)
        return cls(root=root, store=store, sandbox=sandbox, config=config)

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
