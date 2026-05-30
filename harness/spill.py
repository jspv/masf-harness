"""Spill middleware: convert oversized/structured tool results into handles.

After a tool runs, if its result is a DataFrame or a json/text payload larger than
the configured threshold, write it to the handle store and replace the model-visible
result with the lightweight handle summary. Small scalars and results that are already
handle summaries pass through untouched.
"""

from __future__ import annotations

import json
from typing import Any

from agent_framework import function_middleware

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


def make_spill_middleware(session: Session):
    """Return a function middleware bound to ``session`` that spills large tool results."""
    threshold = session.config.spill_threshold_bytes

    @function_middleware
    async def spill_middleware(context, call_next) -> None:
        await call_next()
        result = context.result
        if _is_handle_summary(result):
            return
        if _should_spill(result, threshold):
            handle = session.store.put(result, source=f"tool:{context.function.name}")
            context.result = handle.summary()

    return spill_middleware
