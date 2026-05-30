"""Spill: convert oversized external-tool results into handles.

External (user-supplied) tools may return large raw data. We wrap each one so that an
oversized/structured return is written to the handle store and replaced with a
lightweight handle summary BEFORE it reaches the model. Operating on the tool's raw
Python return value (rather than via function middleware, which only sees MAF's
already-serialized result) keeps the spilled object faithful.

The harness's own built-in tools are NOT wrapped -- they already manage their own
output (read_file is bounded, search is capped, fetch_url/inspect_handle return
summaries, run_python returns a control dict whose result/new_handles the loop needs).

(MCP-server tool returns are not Python callables we can wrap here; spilling those is
future work once MCP is wired in.)
"""

from __future__ import annotations

import functools
import inspect
import json
from typing import Any, Callable

from .session import Session


def _is_handle_summary(obj: Any) -> bool:
    return isinstance(obj, dict) and {"id", "kind", "path"} <= obj.keys()


def _should_spill(result: Any, threshold_bytes: int) -> bool:
    try:
        import pandas as pd
        if isinstance(result, pd.DataFrame):
            return True
    except ImportError:
        pass
    if isinstance(result, str):
        return len(result.encode()) > threshold_bytes
    if isinstance(result, (dict, list)):
        if _is_handle_summary(result):
            return False
        return len(json.dumps(result, default=str).encode()) > threshold_bytes
    return False


def _maybe_spill(session: Session, tool_name: str, result: Any) -> Any:
    if _should_spill(result, session.config.spill_threshold_bytes):
        return session.store.put(result, source=f"tool:{tool_name}").summary()
    return result


def wrap_external_tool(session: Session, fn: Callable) -> Callable:
    """Wrap a user tool so an oversized return is spilled to a handle summary.

    ``functools.wraps`` preserves name/docstring/signature so MAF still generates the
    correct tool schema. Async tools are supported.
    """
    name = getattr(fn, "__name__", "tool")
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            return _maybe_spill(session, name, await fn(*args, **kwargs))
        return awrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return _maybe_spill(session, name, fn(*args, **kwargs))
    return wrapper


def wrap_external_tools(session: Session, tools: list | None) -> list:
    """Wrap each external tool with the spill behavior."""
    return [wrap_external_tool(session, t) for t in (tools or [])]
