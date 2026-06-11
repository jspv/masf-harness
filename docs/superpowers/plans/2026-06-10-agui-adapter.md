# AG-UI Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose a harness run as an AG-UI event stream (for CopilotKit and other AG-UI clients): reuse the official `agent-framework-ag-ui` converter for streamed text / tool calls / frontend tools / state / HITL, and overlay the harness's own `StatusBus` (built-in + MCP status) as `CUSTOM` events.

**Architecture:** `harness/agui.py` provides a pure, thread-safe `merge_status` overlay (testable without any AG-UI import), a `status_to_agui` mapping, and `agui_event_stream` which runs `AgentFrameworkAgent(agent, **kwargs).run(input_data)` and merges the bus. `Harness.agui_stream` wraps the session/agent lifecycle. `agent-framework-ag-ui` is an optional `agui` extra; the harness core never imports it. (Spec: `docs/superpowers/specs/2026-06-10-agui-adapter-design.md`; mechanism + frontend-tool + MCP-overlay all verified by spikes.)

**Tech Stack:** Python 3.12, `agent-framework-ag-ui` (optional extra, pulls `ag-ui-protocol`/`fastapi`/`uvicorn`), Microsoft Agent Framework 1.8.1, pytest. Offline tests for the overlay; live tests gated behind `HARNESS_LIVE_AGUI=1`.

---

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `harness/agui.py` | **Create** | `status_to_agui` (lazy ag_ui import); `merge_status(events, bus, to_event)` (thread-safe overlay, no AG-UI import); `agui_event_stream(agent, bus, input_data, **kwargs)` (lazy `agent-framework-ag-ui`) |
| `harness/api.py` | Modify | `Harness.agui_stream(input_data, *, tools=None, **agui_kwargs)` async generator |
| `pyproject.toml` | Modify | `[project.optional-dependencies] agui = ["agent-framework-ag-ui>=1.0.0rc4"]` |
| `examples/agui_server.py` | **Create** | Runnable FastAPI/SSE endpoint for CopilotKit |
| `tests/test_agui.py` | **Create** | Offline unit tests (overlay + mapping + missing-extra) |
| `tests/test_agui_live.py` | **Create** | Gated live tests (end-to-end, frontend tool, MCP status overlay) |
| `README.md` | Modify | Document live status updates (built-in + MCP) and AG-UI integration |

**Design note (why `merge_status` takes a `to_event` param):** keeping the event-mapper injectable lets the overlay's concurrency/threading logic — the only real engineering here — be unit-tested with plain Python objects and **no `ag_ui` dependency**, so it runs in default CI. `status_to_agui` (which needs `ag_ui`) gets its own tiny gated test.

---

## Task 1: `harness/agui.py` — the status overlay + mapping

**Files:**
- Create: `harness/agui.py`
- Test: `tests/test_agui.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agui.py`:

```python
import asyncio
import importlib.util
import threading

import pytest

from harness.agui import merge_status, status_to_agui
from harness.status import StatusBus, StatusEvent

_HAS_AGUI = importlib.util.find_spec("ag_ui") is not None


def _tag(status_event):
    # a fake to_event mapper so the overlay is testable without ag_ui
    return ("status", status_event.message)


def test_merge_status_yields_source_events_in_order():
    bus = StatusBus()

    async def source():
        yield "A"
        yield "B"

    out = []

    async def run():
        async for ev in merge_status(source(), bus, to_event=_tag):
            out.append(ev)

    asyncio.run(run())
    assert [e for e in out if isinstance(e, str)] == ["A", "B"]


def test_merge_status_interleaves_bus_events():
    bus = StatusBus()

    async def source():
        bus.emit(StatusEvent(tool="t", message="mid-1"))
        yield "A"
        await asyncio.sleep(0)                      # let the scheduled put run
        bus.emit(StatusEvent(tool="t", message="mid-2"))
        yield "B"
        await asyncio.sleep(0)

    out = []

    async def run():
        async for ev in merge_status(source(), bus, to_event=_tag):
            out.append(ev)

    asyncio.run(run())
    statuses = [e for e in out if isinstance(e, tuple)]
    assert ("status", "mid-1") in statuses
    assert ("status", "mid-2") in statuses


def test_merge_status_handles_emit_from_worker_thread():
    bus = StatusBus()

    async def source():
        t = threading.Thread(target=lambda: bus.emit(StatusEvent(tool="bg", message="from-thread")))
        t.start()
        t.join()
        yield "X"
        await asyncio.sleep(0)

    out = []

    async def run():
        async for ev in merge_status(source(), bus, to_event=_tag):
            out.append(ev)

    asyncio.run(run())
    assert ("status", "from-thread") in out          # cross-thread marshaling worked


def test_merge_status_unsubscribes_on_exit():
    bus = StatusBus()

    async def source():
        yield "A"

    async def run():
        async for _ in merge_status(source(), bus, to_event=_tag):
            pass

    asyncio.run(run())
    assert bus._subscribers == []                    # sink removed in finally


@pytest.mark.skipif(not _HAS_AGUI, reason="needs the agui extra (ag-ui-protocol)")
def test_status_to_agui_maps_fields():
    ev = status_to_agui(StatusEvent(tool="run_python", message="running", current=1, total=3))
    assert ev.name == "harness.status"
    assert ev.value == {"tool": "run_python", "message": "running", "current": 1, "total": 3}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_agui.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.agui'`.

- [ ] **Step 3: Write the implementation**

Create `harness/agui.py`:

```python
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
    queue: asyncio.Queue = asyncio.Queue()

    def _sink(status_event: StatusEvent) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, to_event(status_event))

    unsubscribe = bus.subscribe(_sink)
    try:
        async for event in events:
            while not queue.empty():
                yield queue.get_nowait()
            yield event
        while not queue.empty():
            yield queue.get_nowait()
    finally:
        unsubscribe()


async def agui_event_stream(agent: Any, bus: StatusBus, input_data: dict,
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_agui.py -q`
Expected: PASS (6 passed if the agui extra is installed; 5 passed + 1 skipped otherwise).

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check harness/agui.py tests/test_agui.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add harness/agui.py tests/test_agui.py
git commit -m "feat(agui): status overlay (merge_status) + StatusEvent->CustomEvent mapping"
```

---

## Task 2: `agui_event_stream` wiring + `Harness.agui_stream` + the `agui` extra

**Files:**
- Modify: `harness/api.py`, `pyproject.toml`
- Test: `tests/test_agui.py` (append), `tests/test_agui_live.py` (create)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agui.py` (offline missing-extra test):

```python
import sys


def test_agui_event_stream_errors_clearly_without_the_extra(monkeypatch):
    # Simulate the extra being absent even though it's installed in this env.
    monkeypatch.setitem(sys.modules, "agent_framework_ag_ui", None)
    from harness.agui import agui_event_stream
    from harness.status import StatusBus

    async def run():
        gen = agui_event_stream(object(), StatusBus(), {"messages": []})
        await gen.__anext__()

    with pytest.raises(RuntimeError, match="install the 'agui' extra"):
        asyncio.run(run())
```

Create `tests/test_agui_live.py` (gated; runs only with the extra + a real key + the env flag):

```python
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_RUN = os.environ.get("HARNESS_LIVE_AGUI") == "1"
_HAS_AGUI = importlib.util.find_spec("agent_framework_ag_ui") is not None
pytestmark = pytest.mark.skipif(
    not (_RUN and _HAS_AGUI),
    reason="set HARNESS_LIVE_AGUI=1 and install the 'agui' extra to run AG-UI live tests",
)

_FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp_progress_server.py")


def _event_types(events):
    return [type(e).__name__ for e in events]


def _collect(input_data, **harness_kwargs):
    import asyncio

    from harness import Harness, HarnessConfig

    async def run():
        h = Harness(HarnessConfig(), **harness_kwargs)
        return [ev async for ev in h.agui_stream(input_data)]

    return asyncio.run(run())


def test_agui_stream_emits_lifecycle_toolcall_and_status(tmp_path):
    events = _collect(
        {"messages": [{"role": "user", "content": "Use run_python to compute 6*7 and report it."}],
         "threadId": "t", "runId": "r"},
        bundles=("code",),
    )
    types = _event_types(events)
    assert "RunStartedEvent" in types and "RunFinishedEvent" in types
    assert "ToolCallStartEvent" in types                       # built-in run_python surfaced
    statuses = [e for e in events if type(e).__name__ == "CustomEvent" and getattr(e, "name", "") == "harness.status"]
    assert any(s.value.get("tool") == "run_python" for s in statuses)


def test_agui_stream_calls_a_frontend_tool(tmp_path):
    events = _collect(
        {"messages": [{"role": "user", "content": "Render a bar chart of [1,2,3] with show_chart."}],
         "threadId": "t", "runId": "r",
         "tools": [{"name": "show_chart", "description": "Render a bar chart",
                    "parameters": {"type": "object",
                                   "properties": {"values": {"type": "array", "items": {"type": "number"}}},
                                   "required": ["values"]}}]},
        bundles=(),
    )
    names = [getattr(e, "tool_call_name", None) for e in events]
    assert "show_chart" in names                                # request-defined tool was called


def test_agui_stream_overlays_mcp_status(tmp_path):
    from agent_framework import MCPStdioTool

    mcp = MCPStdioTool(name="statusfix", command=sys.executable, args=[_FIXTURE])
    events = _collect(
        {"messages": [{"role": "user", "content": "Call the slow tool with n=3."}],
         "threadId": "t", "runId": "r"},
        tools=[mcp], bundles=(),
    )
    statuses = [e for e in events if type(e).__name__ == "CustomEvent" and getattr(e, "name", "") == "harness.status"]
    assert any(s.value.get("tool") == "mcp:statusfix" for s in statuses)   # MCP logging overlaid
    assert any(s.value.get("tool") == "slow" and s.value.get("current") is not None for s in statuses)  # MCP progress
```

- [ ] **Step 2: Run the offline test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_agui.py -k without_the_extra -q`
Expected: FAIL — `Harness`/`agui_event_stream` flow not wired (or `agui_stream` missing). (The live tests are skipped.)

- [ ] **Step 3: Add `Harness.agui_stream` to `harness/api.py`**

Add this method to the `Harness` class, immediately after `asolve` (before the instance `solve`):

```python
    async def agui_stream(self, input_data: dict, *, tools: list | None = None,
                          **agui_kwargs: Any):
        """Yield AG-UI events for one request (messages/state/tools in ``input_data``).

        Streams the agent's text + tool calls (via the official AG-UI converter) with the
        harness's StatusBus overlaid as ``harness.status`` CustomEvents. ``**agui_kwargs`` pass
        to ``AgentFrameworkAgent`` (e.g. ``state_schema``, ``require_confirmation``). Requires
        the ``agui`` extra.
        """
        from .agui import agui_event_stream

        async with Session.create(self.config) as session:
            agent = await session.create_agent(
                self._make_client(),
                agent_instructions=None,
                tools=self._tools + (tools or []),
                bundles=self._bundles,
            )
            async for event in agui_event_stream(agent, session.status_bus, input_data, **agui_kwargs):
                yield event
```

- [ ] **Step 4: Add the optional dependency extra**

In `pyproject.toml`, under the existing `[project.optional-dependencies]` table (which already has `docling`), add:

```toml
agui = ["agent-framework-ag-ui>=1.0.0rc4"]
```

- [ ] **Step 5: Run the offline tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_agui.py -q`
Expected: PASS (offline tests; the `_HAS_AGUI` mapping test runs if the extra is installed).

- [ ] **Step 6: Verify the gated live tests are skipped by default + full suite + lint**

Run: `.venv/bin/python -m pytest tests/test_agui_live.py -q`
Expected: `3 skipped` (no `HARNESS_LIVE_AGUI`).
Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/api.py tests/test_agui.py tests/test_agui_live.py`
Expected: all pass; `All checks passed!`.

- [ ] **Step 7: (Optional, manual) run the live tests once**

If `OPENAI_API_KEY` is set and the agui extra installed:
`HARNESS_LIVE_AGUI=1 .venv/bin/python -m pytest tests/test_agui_live.py -q` → 3 passed. (Not part of CI.)

- [ ] **Step 8: Commit**

```bash
git add harness/api.py pyproject.toml tests/test_agui.py tests/test_agui_live.py
git commit -m "feat(agui): Harness.agui_stream + agui extra + gated live tests"
```

---

## Task 3: Runnable CopilotKit example

**Files:**
- Create: `examples/agui_server.py`

- [ ] **Step 1: Create the example**

Create `examples/agui_server.py`:

```python
"""Serve the harness to an AG-UI client (e.g. CopilotKit) over SSE.

Run with the agui extra installed:
    uv sync --prerelease=allow --extra agui
    uv run uvicorn examples.agui_server:app --reload
Point your AG-UI client's runtime URL at  http://localhost:8000/agent
"""

from __future__ import annotations

from ag_ui.encoder import EventEncoder
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from harness import Harness

app = FastAPI()
harness = Harness()


@app.post("/agent")
async def agent(request: Request) -> StreamingResponse:
    input_data = await request.json()          # AG-UI RunAgentInput (messages, threadId, runId, tools, state)
    encoder = EventEncoder()

    async def sse():
        async for event in harness.agui_stream(input_data):
            yield encoder.encode(event)         # -> "data: {json}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")
```

- [ ] **Step 2: Syntax-check it (no import — fastapi/ag_ui may be absent in core CI)**

Run: `.venv/bin/python -m py_compile examples/agui_server.py`
Expected: no output (compiles).

- [ ] **Step 3: Lint**

Run: `.venv/bin/ruff check examples/agui_server.py`
Expected: `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git add examples/agui_server.py
git commit -m "docs(agui): runnable CopilotKit SSE example server"
```

---

## Task 4: Documentation — live status updates + AG-UI

**Files:**
- Modify: `README.md`

Document the full feature set cleanly (status updates across built-in/dev/MCP tools, and AG-UI/CopilotKit). READ `README.md` first to confirm anchors; current relevant headings: `## Install` (has the `--extra docling` block), `### Library`, `### MCP servers`, `## Tool surface`, `## Project layout` (a code block listing modules), `## Status & roadmap`.

- [ ] **Step 1: Add a "Live status updates" subsection after the `### MCP servers` section**

Insert this new subsection immediately before `## Tool surface`:

```markdown
### Live status updates

Tools report what they're doing while they run. Pass an `on_status` callback to receive
`StatusEvent`s as they happen — from the built-in tools, your own tools (via `report_progress`),
and MCP servers (their logging + progress notifications are captured automatically):

​```python
from harness import Harness, report_progress

def crunch(n: int) -> str:
    """Your tool can report progress."""
    for i in range(n):
        report_progress(f"processed {i + 1}/{n}", current=i + 1, total=n, tool="crunch")
    return "done"

def show(event):
    bar = f" [{event.current}/{event.total}]" if event.current is not None else ""
    print(f"→ {event.tool}: {event.message}{bar}")

Harness(tools=[crunch], on_status=show).solve("crunch 5 items")
​```

The CLI exposes the same feed with `-v`/`--verbose` (printed to stderr). MCP-server status is
captured with no extra wiring: a server's `notifications/message` arrive tagged
`mcp:<server>`, and its `notifications/progress` arrive tagged with the calling tool's name and
a `current`/`total`.
```

(Note: the `​` zero-width characters above are only to escape the nested fences in this plan —
write the block with ordinary triple backticks.)

- [ ] **Step 2: Add an "AG-UI / CopilotKit" section after `### Live status updates`**

Insert this section (still before `## Tool surface`):

```markdown
### AG-UI / CopilotKit

A harness run can drive an [AG-UI](https://docs.ag-ui.com/) client such as
[CopilotKit](https://docs.copilotkit.ai/): streamed answer text, live tool-call visibility, and
the status feed above — all as a stream of AG-UI events. Install the optional extra:

​```bash
uv sync --prerelease=allow --extra agui
​```

`Harness.agui_stream(input_data)` yields AG-UI events for one AG-UI request; encode them as SSE
and return them from your endpoint:

​```python
from ag_ui.encoder import EventEncoder
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from harness import Harness

app, harness = FastAPI(), Harness()

@app.post("/agent")
async def agent(request: Request):
    input_data = await request.json()                 # AG-UI RunAgentInput
    encoder = EventEncoder()
    async def sse():
        async for event in harness.agui_stream(input_data):
            yield encoder.encode(event)
    return StreamingResponse(sse(), media_type="text/event-stream")
​```

Point your AG-UI client at this endpoint. The harness reuses the official
`agent-framework-ag-ui` converter, so **frontend/generative-UI tools** (defined in the request),
**shared state** (`state_schema`/`predict_state_config`, forwarded as keyword args to
`agui_stream`), **human-in-the-loop**, and **multi-turn history** all work — with the harness's
own progress feed overlaid as `harness.status` `CUSTOM` events. A runnable version is in
`examples/agui_server.py`.
```

(Same zero-width-fence note as Step 1 — use ordinary backticks.)

- [ ] **Step 3: Add `agui.py` to the Project layout block**

In the `## Project layout` code block, add this line right after the `spill.py` line:

```
  agui.py        AG-UI event stream (status overlay over agent-framework-ag-ui)
```

- [ ] **Step 4: Update Status & roadmap**

In `## Status & roadmap`, update the Implemented sentence to append the new capabilities. Replace the existing sentence that begins `Implemented:` so it ends with:

```markdown
, **live status updates** (built-in, developer, and MCP tools → an `on_status` feed / `--verbose`), and an **AG-UI / CopilotKit** integration (`Harness.agui_stream`).
```

- [ ] **Step 5: Verify the README renders (no broken fences) and commit**

Run: `.venv/bin/python -c "open('README.md').read(); print('ok')"`
Then:
```bash
git add README.md
git commit -m "docs(readme): document live status updates and AG-UI/CopilotKit integration"
```

---

## Self-Review (completed during plan authoring)

**Spec coverage** (`2026-06-10-agui-adapter-design.md`):
- `status_to_agui` → `CustomEvent(name="harness.status", …)` — Task 1. ✓
- `merge_status` overlay with cross-thread `call_soon_threadsafe` marshaling + unsubscribe — Task 1 (impl + 4 offline tests incl. worker-thread + unsubscribe). ✓
- `agui_event_stream` lazy-imports the extra, forwards `**agui_kwargs`, errors clearly if missing — Task 1 (impl) + Task 2 (missing-extra test). ✓
- `Harness.agui_stream(input_data, *, tools, **agui_kwargs)` session/agent lifecycle — Task 2. ✓
- Optional `agui` extra; core never imports it — Task 2 (pyproject; lazy imports). ✓
- Faithful passthrough: frontend tools, shared state, HITL, multi-turn — Task 2 live tests (frontend tool) + `**agui_kwargs`/`input_data` passthrough; documented in Task 4. ✓
- MCP status overlaid into AG-UI — Task 2 `test_agui_stream_overlays_mcp_status`. ✓
- `examples/agui_server.py` — Task 3. ✓
- Offline overlay tests + gated live tests — Tasks 1-2. ✓
- Full feature documentation — Task 4 (status updates incl. MCP, AG-UI). ✓

**Placeholder scan:** none. (The zero-width characters in Task 4 are explicitly fence-escapes with a written instruction to use ordinary backticks — not placeholders.)

**Type/name consistency:** `status_to_agui`, `merge_status(events, bus, to_event=status_to_agui)`, `agui_event_stream(agent, bus, input_data, **agui_kwargs)`, `Harness.agui_stream(input_data, *, tools, **agui_kwargs)`, the `harness.status` CustomEvent name, and the `agui` extra name are consistent across all tasks and the spec.

**Deferred (per spec):** the harness does not implement state/HITL logic itself (forwards the wrapper's knobs); the FastAPI server stays an example, not core.
