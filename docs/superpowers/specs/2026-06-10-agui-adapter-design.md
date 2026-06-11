# AG-UI Adapter — Design (Tool Status Updates, Phase 3)

- **Date:** 2026-06-10
- **Status:** Approved design (brainstormed). Follow-on to the tool-status-updates feature
  (Phases 1-2). The deferred "AG-UI adapter sink" from `2026-06-10-tool-status-updates-design.md`.
- **Motivation:** Expose a harness run to an AG-UI client (e.g. CopilotKit) so a connected UI
  can render the run live: streamed answer text, tool-call visibility, and the harness's own
  mid-tool / MCP progress feed. The harness already has a `StatusBus` (Phases 1-2); this phase
  emits a full AG-UI event stream and overlays the status events onto it.

## Spike findings (the design rests on these, all verified on the installed stack)

- **The harness agent streams.** `agent.run(prompt, stream=True)` returns a `ResponseStream`
  of `AgentResponseUpdate`s (verified: 17 incremental updates, token-streamed text).
- **The official `agent-framework-ag-ui` already maps MAF → AG-UI, completely.**
  `AgentFrameworkAgent(agent, require_confirmation=False).run(input_data)` is a public async
  generator that internally calls `agent.run(messages, stream=True, …)` and yields AG-UI
  `BaseEvent`s. On a real tool-using task it produced the full sequence:
  `RunStarted → TextMessageStart → ToolCallStart(run_python) → ToolCallArgs×N (streamed args)
  → ToolCallEnd → ToolCallResult → TextMessage(answer, streamed) → MessagesSnapshot →
  RunFinished` (plus `CustomEvent name="usage"`). Hand-rolling this (Approach A′) was rejected:
  it would reimplement tool-arg streaming, snapshots, usage, lifecycle, and HITL/state that the
  maintained package already does correctly.
- **Our `StatusBus` fires concurrently during that run** (verified: `run_python: running script
  in sandbox` was captured by a session subscriber while `AgentFrameworkAgent.run` streamed).
  So the harness-specific value is **overlaying** our status onto the official AG-UI stream —
  the official converter shows tool-call *boundaries* but not mid-execution progress (Docling
  conversion, fetch, spill, MCP logging/progress).
- **SSE encoding is in the lightweight protocol package.** `ag_ui.encoder.EventEncoder().encode(event)`
  yields `data: {json}\n\n`, so the transport needs no extra harness code.
- **CopilotKit's richer features come "for free" through the official wrapper — verified.**
  `AgentFrameworkAgent` already implements frontend/generative-UI tools, shared state, HITL, and
  multi-turn, and they work *through* the harness agent because it is a standard streaming MAF
  agent:
  - **Frontend tools (proven by spike):** a tool defined only in the AG-UI request
    (`input_data["tools"]`, not registered in the harness) was *called by the harness agent* —
    `ToolCallStart(show_chart)` + streamed args. The wrapper converts and merges request tools
    into the agent's per-run `tools=` (`_agent_run.py:832` → `merge_tools` →
    `agent.run(messages, tools=merged, stream=True)`).
  - **Shared state:** `state_schema` / `predict_state_config` / `state_update`, with
    `STATE_SNAPSHOT`/`STATE_DELTA` emitted from `input_data["state"]`.
  - **HITL:** an approval registry (`_pending_approvals`) + predictive-state confirmation.
  - **Multi-turn:** CopilotKit sends the full message history in `input_data["messages"]`.
  - Corroborated by CopilotKit's MAF-Python AG-UI docs (shared state via `useCoAgent`, HITL,
    generative UI).

## Scope

The adapter is a **faithful, thin passthrough to `AgentFrameworkAgent`** that adds the status
overlay and session lifecycle, and **strips nothing** — so the wrapper's full feature set
(streaming text, tool calls, frontend tools, shared state, HITL, multi-turn) is available to the
developer.

**In scope (v1):**
- An optional `agui` dependency extra (`agent-framework-ag-ui`).
- `harness/agui.py`: the `StatusEvent → CustomEvent` mapping, a generic **status-overlay merge**
  of two async event sources, and an `agui_event_stream` helper that wraps `AgentFrameworkAgent`
  and applies the overlay. It **forwards** the wrapper's knobs — `state_schema`,
  `predict_state_config`, `require_confirmation`, `name`, `description` — so shared state / HITL
  are enable-able by the developer; and passes `input_data` straight through so request-defined
  frontend tools, prior `state`, and message history reach the agent.
- `Harness.agui_stream(input_data, **agui_kwargs)`: a convenience async generator that builds the
  session + agent and yields the merged AG-UI event stream (mirrors `asolve`'s session lifecycle),
  forwarding `**agui_kwargs` to `agui_event_stream`.
- `examples/agui_server.py`: a runnable ~12-line FastAPI/SSE endpoint for CopilotKit.

**Not in scope (by nature, not by restriction):**
- The harness does **not implement** shared-state or HITL *logic* itself — it forwards the
  wrapper's parameters so the developer configures them (a `state_schema`, approval-mode tools).
  An autonomous harness with default tools simply won't exercise them unless the developer opts in.
- Shipping the FastAPI server as core (it stays an example; transport is the developer's).
- Generative-UI **rendering** (a frontend concern); the harness only emits the events.

## Architecture

```
Harness.agui_stream(input_data, **agui_kwargs)
  └─ async with Session.create(config)            # owns the StatusBus, bound for the run
       agent = session.create_agent(client, …)    # the streaming harness agent
       agui_event_stream(agent, session.status_bus, input_data, **agui_kwargs)
         └─ AgentFrameworkAgent(agent, **agui_kwargs).run(input_data)   # official → AG-UI events
            merged with session.status_bus  →  one AsyncIterator[BaseEvent]
  developer encodes each event via ag_ui EventEncoder → SSE → CopilotKit
```

The reused official mapping does the heavy lifting; the harness adds (a) the status overlay and
(b) session/agent lifecycle. Everything downstream of the merged event stream (HTTP, SSE, CORS,
auth, hosting) is the developer's — shown by the example, not owned by the harness.

### Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `harness/agui.py` (new) | `status_to_agui(event)`; `merge_status(events, bus)` (the overlay); `agui_event_stream(agent, bus, input_data, **agui_kwargs)` (lazy-imports `agent-framework-ag-ui`, forwards `**agui_kwargs` to `AgentFrameworkAgent`) | `status`, `ag-ui-protocol`, `agent-framework-ag-ui` (extra) |
| `harness/api.py` (modify) | `Harness.agui_stream(input_data, *, tools=None, **agui_kwargs)` — session+agent lifecycle, delegates to `agui_event_stream` | `agui`, `session` |
| `pyproject.toml` (modify) | `[project.optional-dependencies] agui = ["agent-framework-ag-ui>=1.0.0rc4"]` | — |
| `examples/agui_server.py` (new) | Runnable FastAPI/SSE endpoint for CopilotKit | `Harness.agui_stream`, `ag_ui.encoder`, fastapi (dev-installed) |

## Public API

```python
# harness/agui.py
def status_to_agui(event: StatusEvent) -> CustomEvent:
    """StatusEvent -> AG-UI CustomEvent(name="harness.status", value={tool,message,current,total})."""

async def merge_status(events: AsyncIterator[BaseEvent], bus: StatusBus) -> AsyncIterator[BaseEvent]:
    """Yield `events`, interleaving the bus's StatusEvents (as CustomEvents) between them."""

async def agui_event_stream(agent: Any, bus: StatusBus, input_data: dict,
                            **agui_kwargs: Any) -> AsyncIterator[BaseEvent]:
    """Run `agent` via AgentFrameworkAgent(**agui_kwargs) and overlay `bus`'s status.
    Lazy-imports the extra; `**agui_kwargs` (e.g. state_schema, predict_state_config,
    require_confirmation, name, description) pass straight to AgentFrameworkAgent."""
```

```python
# Harness
async def agui_stream(self, input_data: dict, *, tools: list | None = None,
                      **agui_kwargs: Any) -> AsyncIterator[BaseEvent]: ...
```

## The status overlay (the one piece of real engineering)

`AgentFrameworkAgent.run(input_data)` is an async generator; our status events fire on the bus
*while we await its next event*, and — critically — a status event may be emitted from a
**worker thread** (sync tools run via `asyncio.to_thread`). So `merge_status` must marshal
cross-thread safely:

- capture `loop = asyncio.get_running_loop()`;
- subscribe a sink that does `loop.call_soon_threadsafe(queue.put_nowait, status_to_agui(event))`
  (an `asyncio.Queue` is **not** thread-safe; `call_soon_threadsafe` is the correct bridge);
- iterate the AG-UI events; **before** each yielded event, drain whatever status `CustomEvent`s
  are already queued; after the source ends, drain the remainder;
- always unsubscribe in a `finally`.

This interleaves status at AG-UI event boundaries (a slight, acceptable latency for a live feed)
without a second event loop or a thread-unsafe hand-off.

## Data flow (one request)

1. CopilotKit POSTs an AG-UI `RunAgentInput` (`messages`, `threadId`, `runId`, …) to the
   developer's endpoint.
2. The endpoint calls `harness.agui_stream(input_data)`.
3. `agui_stream` opens a `Session` (binding the `StatusBus`), builds the streaming agent, and
   delegates to `agui_event_stream`, which runs `AgentFrameworkAgent(...).run(input_data)` and
   merges the status overlay.
4. The endpoint encodes each yielded `BaseEvent` with `ag_ui.encoder.EventEncoder` → SSE.
5. CopilotKit renders: streamed answer, tool calls, and the `harness.status` progress feed.

## Error handling

- **Missing extra:** `agui_event_stream` lazy-imports `agent_framework_ag_ui`; on `ImportError`
  it raises a clear, actionable error ("install the 'agui' extra: `uv sync --extra agui`").
- **Run failure:** `AgentFrameworkAgent` emits its own `RunErrorEvent`; the overlay passes it
  through. The status sink never raises into the run (`StatusBus.emit` already swallows
  subscriber errors), and `merge_status` unsubscribes in `finally`.
- The harness core never imports `agent-framework-ag-ui` (lazy), so the extra stays optional.

## Testing

- **Offline unit tests (no model, no network):**
  - `status_to_agui`: maps fields to a `CustomEvent(name="harness.status", value=…)`.
  - `merge_status`: a hand-built async generator of AG-UI events + a `StatusBus`; emitting a
    status mid-iteration yields a `harness.status` `CustomEvent` interleaved at the next
    boundary; a status emitted **from a worker thread** still appears (exercises the
    `call_soon_threadsafe` bridge); unsubscribes on exit.
- **Gated live tests** (`HARNESS_LIVE_AGUI=1`, real model + key; skipped by default like the
  other `HARNESS_LIVE*` tests):
  - `Harness.agui_stream` on a tool-using prompt (built-in `run_python`) yields a sequence
    containing `RunStartedEvent` … `RunFinishedEvent`, at least one `ToolCall*` event, and at
    least one `harness.status` `CustomEvent`.
  - **Frontend-tool passthrough:** a tool defined only in `input_data["tools"]` (not registered
    in the harness) is *called by the agent* (a `ToolCallStartEvent` with that tool's name) —
    locking in the proven request-tool merge so a future regression is caught.
- The example server is manually runnable; not part of CI.
