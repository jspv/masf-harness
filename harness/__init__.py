"""Data-integration agent harness."""

from .api import Harness, Result, solve
from .config import FetchConfig, HarnessConfig, SandboxConfig
from .handles import Handle, HandleStore
from .paths import PathEscapesRootError, safe_path
from .sandbox import ExecResult, LocalSubprocessSandbox, SandboxExecutor
from .sandbox_container import ContainerSandbox
from .session import Session
from .conversation import Conversation
from .manager import SessionManager
from .status import StatusBus, StatusEvent, report_progress
from .tools.registry import build_tools

__version__ = "0.1.0"

__all__ = [
    "Harness", "Result", "solve",
    "FetchConfig", "HarnessConfig", "SandboxConfig",
    "Handle", "HandleStore",
    "PathEscapesRootError", "safe_path",
    "ExecResult", "LocalSubprocessSandbox", "SandboxExecutor",
    "ContainerSandbox",
    "Session",
    "Conversation", "SessionManager",
    "StatusBus", "StatusEvent", "report_progress",
    "build_tools",
]
