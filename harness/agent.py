"""build_agent: assemble the harness agent from a Session, config, and a chat client."""

from __future__ import annotations

import asyncio

from agent_framework import create_harness_agent

from .config import HarnessConfig
from .session import Session
from .spill import make_spill_middleware
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
    "conversation -- load and analyze them by writing Python via run_python "
    "(use load(id)/save(id, obj)/emit(obj)). "
    "ALWAYS verify data quality before reporting results, and state any issues you handled."
)


def build_agent(session: Session, config: HarnessConfig, client, extra_tools: list | None = None):
    """Build a harness Agent over the session's tools (plus any ``extra_tools``) with the
    spill middleware installed."""
    return create_harness_agent(
        client,
        name="data-integrator",
        agent_instructions=AGENT_INSTRUCTIONS,
        tools=build_tools(session) + list(extra_tools or []),
        middleware=[make_spill_middleware(session)],
        max_context_window_tokens=config.max_context_window_tokens,
        max_output_tokens=config.max_output_tokens,
        disable_todo=True,
        disable_mode=True,
        disable_memory=True,
        disable_web_search=True,
    )
