"""build_agent: assemble the harness agent from a Session, config, and a chat client."""

from __future__ import annotations

import asyncio
import functools
import inspect
from typing import Any, Callable

from agent_framework import create_harness_agent

from .config import HarnessConfig
from .session import Session
from .spill import wrap_external_tools
from .tools.registry import build_tools


def run_agent_sync(agent, prompt: str):
    """Run ``agent.run(prompt)`` synchronously, telemetry-safe.

    MAF's observability layer sets a ContextVar token when ``agent.run()`` is invoked
    and resets it as the coroutine completes. Passing ``agent.run(...)`` straight to
    ``asyncio.run`` evaluates it in the *outer* context, so the token is set there but
    reset inside the Task's copied context -> ValueError. Calling ``agent.run`` *inside*
    the coroutine keeps set and reset in the same context, so instrumentation stays on.
    """
    async def _run():
        return await agent.run(prompt)

    return asyncio.run(_run())

AGENT_INSTRUCTIONS = (
    "You solve data-gathering and integration tasks. "
    "IMPORTANT: Work autonomously and do NOT stop to ask the user. "
    "Large data is referenced by handles (ids); never expect full datasets in the "
    "conversation -- load and analyze them by writing Python via run_python. "
    "In run_python you can use load(id) to read a handle and save(id, obj) to store one. "
    "To return a value, just end your code with a bare expression (e.g. `total`) OR "
    "print() it -- the run_python `result` field captures it; you do NOT need emit(). "
    "ALWAYS verify data quality before reporting results, and state any issues you handled."
)


def _instrument(fn: Callable, on_tool_call: Callable[[str, dict, Any], None]) -> Callable:
    """Wrap a tool so each call is reported to ``on_tool_call(name, kwargs, result)``."""
    name = getattr(fn, "__name__", "tool")
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                result = await fn(*args, **kwargs)
            except Exception as e:  # report the failure, then let it propagate
                on_tool_call(name, kwargs, e)
                raise
            on_tool_call(name, kwargs, result)
            return result
        return awrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            result = fn(*args, **kwargs)
        except Exception as e:  # report the failure, then let it propagate
            on_tool_call(name, kwargs, e)
            raise
        on_tool_call(name, kwargs, result)
        return result
    return wrapper


def build_agent(session: Session, config: HarnessConfig, client,
                extra_tools: list | None = None,
                on_tool_call: Callable[[str, dict, Any], None] | None = None):
    """Build a harness Agent over the session's tools (plus any ``extra_tools``).

    External tools are spill-wrapped (oversized raw returns become handles). If
    ``on_tool_call`` is given, every tool is also instrumented to report each call
    (used for --verbose live visibility).
    """
    tools = build_tools(session) + wrap_external_tools(session, extra_tools)
    if on_tool_call is not None:
        tools = [_instrument(t, on_tool_call) for t in tools]
    return create_harness_agent(
        client,
        name="data-integrator",
        agent_instructions=AGENT_INSTRUCTIONS,
        tools=tools,
        max_context_window_tokens=config.max_context_window_tokens,
        max_output_tokens=config.max_output_tokens,
        disable_todo=True,
        disable_mode=True,
        disable_memory=True,
        disable_web_search=True,
    )
