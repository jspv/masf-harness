"""Typed configuration for the harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SandboxConfig:
    timeout_s: float = 30.0
    max_memory_mb: int = 1024
    max_file_size_mb: int = 512
    confine_os: bool = False  # opt-in OS-level jail (sandbox-exec / bwrap); Phase 2+
    preinstalled: tuple[str, ...] = ("pandas", "pyarrow", "numpy", "httpx")


@dataclass
class FetchConfig:
    max_bytes: int = 10_000_000
    timeout_s: float = 30.0
    allowed_schemes: tuple[str, ...] = ("http", "https")


@dataclass
class SearchConfig:
    provider: str = "tavily"
    api_key: str | None = None
    max_results: int = 5
    timeout_s: float = 20.0


@dataclass
class HarnessConfig:
    model: str = "gpt-5-mini"
    spill_threshold_bytes: int = 8192
    max_context_window_tokens: int = 128_000
    max_output_tokens: int = 4096
    root_dir: Path | None = None  # None -> a session dir is created under ./.harness/sessions/
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
