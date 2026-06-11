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
  of `AgentResponseUpdate`s (verified: 17 incremental updates, token-streamed text). (This
  corrects the Phase-1 spike's "non-streaming" claim — see "Corrections" below.)
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

## Scope

**In scope (v1):**
- An optional `agui` dependency extra (`agent-framework-ag-ui`).
- `harness/agui.py`: the `StatusEvent → CustomEvent` mapping, a generic **status-overlay merge**
  of two async event sources, and an `agui_event_stream` helper that wraps
  `AgentFrameworkAgent` and applies the overlay.
- `Harness.agui_stream(input_data)`: a convenience async generator that builds the session +
  agent and yields the merged AG-UI event stream (mirrors `asolve`'s session lifecycle).
- `examples/agui_server.py`: a runnable ~12-line FastAPI/SSE endpoint for CopilotKit.

**Out of scope (noted for later):**
- CopilotKit **shared state** (`useCoAgent` / `STATE_SNAPSHOT`), **frontend/generative-UI
  tools**, and **human-in-the-loop** approval. The harness is an autonomous one-shot solver, so
  v1 sets `require_confirmation=False` (tools auto-run) and does not wire interactive state.
- Multi-turn conversation history beyond the latest user message.
- Shipping the FastAPI server as core (it stays an example; transport is the developer's).

## Architecture

```
Harness.agui_stream(input_data)
  └─ async with Session.create(config)            # owns the StatusBus, bound for the run
       agent = session.create_agent(client, …)    # the streaming harness agent
       agui_event_stream(agent, session.status_bus, input_data)
         └─ AgentFrameworkAgent(agent, require_confirmation=False).run(input_data)   # official → AG-UI events
            merged with session.status_bus  →  one AsyncIterator[BaseEvent]
  developer encodes each event via ag_ui EventEncoder → SSE → CopilotKit
```

The reused official mapping does the heavy lifting; the harness adds (a) the status overlay and
(b) session/agent lifecycle. Everything downstream of the merged event stream (HTTP, SSE, CORS,
auth, hosting) is the developer's — shown by the example, not owned by the harness.

### Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `harness/agui.py` (new) | `status_to_agui(event)`; `merge_status(events, bus)` (the overlay); `agui_event_stream(agent, bus, input_data, *, require_confirmation=False)` (lazy-imports `agent-framework-ag-ui`) | `status`, `ag-ui-protocol`, `agent-framework-ag-ui` (extra) |
| `harness/api.py` (modify) | `Harness.agui_stream(input_data, tools=None)` — session+agent lifecycle, delegates to `agui_event_stream` | `agui`, `session` |
| `pyproject.toml` (modify) | `[project.optional-dependencies] agui = ["agent-framework-ag-ui>=1.0.0rc4"]` | — |
| `examples/agui_server.py` (new) | Runnable FastAPI/SSE endpoint for CopilotKit | `Harness.agui_stream`, `ag_ui.encoder`, fastapi (dev-installed) |

## Public API

```python
# harness/agui.py
def status_to_agui(event: StatusEvent) -> CustomEvent:
    """StatusEvent -> AG-UI CustomEvent(name="harness.status", value={tool,message,current,total})."""

async def merge_status(events: AsyncIterator[BaseEvent], bus: StatusBus) -> AsyncIterator[BaseEvent]:
    """Yield `events`, interleaving the bus's StatusEvents (as CustomEvents) between them."""

async def agui_event_stream(agent: Any, bus: StatusBus, input_data: dict, *,
                            require_confirmation: bool = False) -> AsyncIterator[BaseEvent]:
    """Run `agent` via AgentFrameworkAgent and overlay `bus`'s status. Lazy-imports the extra."""
```

```python
# Harness
async def agui_stream(self, input_data: dict, tools: list | None = None) -> AsyncIterator[BaseEvent]: ...
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
- **Gated live test** (`HARNESS_LIVE_AGUI=1`, real model + key; skipped by default like the
  other `HARNESS_LIVE*` tests): `Harness.agui_stream` on a tool-using prompt yields a sequence
  containing `RunStartedEvent` … `RunFinishedEvent`, at least one `ToolCall*` event, and at
  least one `harness.status` `CustomEvent`.
- The example server is manually runnable; not part of CI.

## Corrections to prior specs (separate cleanup, not built here)

The Phase-1 and Phase-2 specs state the harness agent is non-streaming and use that to justify
the side-band `StatusBus`. The agent **does** stream (`run(stream=True)`). The side-band bus is
still correct, but for the accurate reason: **mid-tool and MCP progress are not part of MAF's
response stream** (which carries text deltas + tool-call lifecycle, not mid-execution custom
progress). Those two specs' "research findings" should be corrected to say so. Tracked as a
docs follow-up, independent of this implementation.
