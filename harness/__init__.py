"""Data-integration agent harness."""

from .api import Harness, Result, solve
from .config import FetchConfig, HarnessConfig, SandboxConfig
from .handles import Handle, HandleStore
from .paths import PathEscapesRootError, safe_path
from .sandbox import ExecResult, LocalSubprocessSandbox, SandboxExecutor
from .session import Session
from .tools.registry import build_tools

__version__ = "0.1.0"

__all__ = [
    "Harness", "Result", "solve",
    "FetchConfig", "HarnessConfig", "SandboxConfig",
    "Handle", "HandleStore",
    "PathEscapesRootError", "safe_path",
    "ExecResult", "LocalSubprocessSandbox", "SandboxExecutor",
    "Session",
    "build_tools",
]
