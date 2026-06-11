"""AG-UI adapter: run the harness as an AG-UI event stream, overlaying StatusBus events.

The official ``agent-framework-ag-ui`` package already maps a streaming MAF agent to AG-UI
events (text, tool calls, frontend tools, state, HITL). We reuse it and *overlay* the harness's
own ``StatusBus`` -- mid-tool progress and MCP logging/progress that the official converter does
not carry -- as ``CustomEvent``s. ``agent-framework-ag-ui`` is an optional extra; this module
imports it (and ``ag_ui``) lazily so the harness core never depends on it.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable

from .status import StatusBus, StatusEvent


def status_to_agui(event: StatusEvent) -> Any:
    """Map a StatusEvent to an AG-UI ``CustomEvent(name="harness.status", value=...)``.

    Lazily imports ``ag_ui`` so importing this module does not require the agui extra.
    """
    from ag_ui.core import CustomEvent

    return CustomEvent(
        name="harness.status",
        value={"tool": event.tool, "message": event.message,
               "current": event.current, "total": event.total},
    )


async def merge_status(events: AsyncIterator[Any], bus: StatusBus,
                       to_event: Callable[[StatusEvent], Any] = status_to_agui) -> AsyncIterator[Any]:
    """Yield ``events``, interleaving ``bus``'s StatusEvents (mapped via ``to_event``).

    StatusEvents may be emitted from a worker thread (sync tools run via ``asyncio.to_thread``),
    so the subscriber marshals onto the loop with ``call_soon_threadsafe`` (an ``asyncio.Queue``
    is not thread-safe). Queued status events are flushed at each source-event boundary and once
    the source ends. The subscriber is always removed on exit.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()

    def _sink(status_event: StatusEvent) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, to_event(status_event))

    unsubscribe = bus.subscribe(_sink)
    try:
        async for event in events:
            while not queue.empty():
                yield queue.get_nowait()
            yield event
        await asyncio.sleep(0)          # let any last call_soon_threadsafe put land
        while not queue.empty():
            yield queue.get_nowait()
    finally:
        unsubscribe()


async def agui_event_stream(agent: Any, bus: StatusBus, input_data: dict[str, Any],
                            **agui_kwargs: Any) -> AsyncIterator[Any]:
    """Run ``agent`` via ``AgentFrameworkAgent(**agui_kwargs)`` and overlay ``bus``'s status.

    ``**agui_kwargs`` (e.g. ``state_schema``, ``predict_state_config``, ``require_confirmation``,
    ``name``, ``description``) pass straight to ``AgentFrameworkAgent``, and ``input_data``
    (messages, ``tools``, ``state``) passes straight to its ``.run`` -- so frontend tools, shared
    state, HITL, and multi-turn all work. Raises a clear error if the agui extra is missing.
    """
    try:
        from agent_framework_ag_ui import AgentFrameworkAgent
    except ImportError as e:
        raise RuntimeError(
            "AG-UI support unavailable: install the 'agui' extra (e.g. `uv sync --extra agui`)"
        ) from e

    afa = AgentFrameworkAgent(agent, **agui_kwargs)
    async for event in merge_status(afa.run(input_data), bus, status_to_agui):
        yield event
