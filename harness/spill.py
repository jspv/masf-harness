"""Spill: turn oversized/structured tool results into handles via MAF result_parser.

A plugged-in capability (a plain tool function or an MCP server tool) returns its raw
Python value; MAF calls our ``result_parser`` on it *before serialization*. We write an
oversized/structured value to the handle store and return the lightweight handle summary
the model sees; small values defer to MAF's default parsing. The harness's own built-in
tools are NOT given this parser -- they already manage their own output.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from agent_framework import FunctionTool
from agent_framework._types import Content

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
    if isinstance(result, (bytes, bytearray)):
        return len(result) > threshold_bytes
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


def make_spill_parser(session: Session, tool_name: str) -> Callable[[Any], list[Content]]:
    """A MAF ``result_parser``: spill oversized returns, else default-parse."""
    def parse(result: Any) -> list[Content]:
        return FunctionTool.parse_result(_maybe_spill(session, tool_name, result))
    return parse


def spill_tool(session: Session, fn: Callable) -> FunctionTool:
    """Wrap a plain developer callable as a FunctionTool whose return is spilled."""
    name = getattr(fn, "__name__", "tool")
    return FunctionTool(
        func=fn,
        name=name,
        description=(fn.__doc__ or "").strip(),
        result_parser=make_spill_parser(session, name),
    )


def looks_like_mcp(tool: Any) -> bool:
    """Duck-typed MCP detection: connectable, closeable, exposes ``.functions``.

    Avoids coupling to MAF's MCP class names and lets tests use a fake. Plain callables
    have no ``.functions`` attribute, so there are no false positives.
    """
    return (
        callable(getattr(tool, "connect", None))
        and callable(getattr(tool, "close", None))
        and hasattr(tool, "functions")
    )


# ---------------------------------------------------------------------------
# Backward-compatibility shim — agent.py still imports this until Task 4/8.
# ---------------------------------------------------------------------------

def wrap_external_tools(session: Session, tools: list | None) -> list:
    """Deprecated shim: wraps plain callables with functools.wraps-based spill.

    Kept so agent.py can import without changes until it is migrated to
    ``spill_tool`` / ``create_agent`` in later tasks.
    """
    import functools
    import inspect

    def _wrap(fn: Callable) -> Callable:
        name = getattr(fn, "__name__", "tool")
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:  # type: ignore[return]
                return _maybe_spill(session, name, await fn(*args, **kwargs))
            return awrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _maybe_spill(session, name, fn(*args, **kwargs))
        return wrapper

    return [_wrap(t) for t in (tools or [])]
