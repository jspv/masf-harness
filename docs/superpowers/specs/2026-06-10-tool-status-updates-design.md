# Tool Status Updates — Design

- **Date:** 2026-06-10
- **Status:** Approved design (brainstormed).
- **Motivation:** When the harness calls tools (its own built-ins, developer-supplied
  callables, and — later — MCP servers), there is currently no way for a tool to tell the
  harness *what it is doing while it runs*. Long operations (Docling conversion, multi-page
  fetches, sandboxed scripts) are opaque until they return. We want tools to emit
  lightweight status/progress updates that the harness can observe (for logging/UX) and
  optionally forward to a connected UI.

## Research findings that shaped this design

A spike against the installed stack (`agent-framework-core` 1.7.0; latest 1.8.1) established:

- **The harness agent is non-streaming.** `create_harness_agent` returns an `Agent` whose
  only public run method is `.run()` — there is no `run_stream`. There is no MAF event
  stream to inject tool-originated events into on our path.
- **MAF exposes no tool-side emit hook.** `FunctionInvocationContext` carries only
  `{function, arguments, kwargs, metadata, result, session}`; `AgentSession` carries only
  `{session_id, to_dict}`. Neither can push an update into a run.
- **MAF does not capture MCP progress.** Its MCP wrapper handles logging notifications but
  not `notifications/progress` (the `mcp` SDK supports `progress_callback`; MAF doesn't
  plumb it).
- **A harness-owned side-band channel works today.** A `contextvars`-backed emitter
  captured live, ordered, mid-tool updates from inside a tool — including a *sync* tool
  offloaded to a worker thread (Python copies the context into the thread) — with no MAF
  coupling.

**Conclusion:** the status channel must be **harness-owned and out-of-band** (not riding a
MAF stream, which does not exist for our agent). Any future UI — including
`agent-framework-ag-ui`, which itself expects a streaming agent — becomes a *subscriber* to
this channel, not the other way around.

## Scope

**This spec covers v1 only: the internal-tool status channel.**

In v1:
- A typed status event, a thread-safe bus, and a `contextvars`-backed producer function.
- `Session` owns the bus and binds the producer for the duration of a run.
- `Harness` / `solve()` accept an `on_status` subscriber; `Session` exposes `subscribe()`.
- The CLI `--verbose` flag becomes the first real consumer (a printer sink), replacing the
  current "temporarily unavailable" notice.
- The harness's own built-in tools are instrumented to emit at high-value points.

Explicitly deferred (each its own later spec/plan):
- **MCP status capture (Phase 2):** capture *both* MCP logging notifications and true
  `notifications/progress`, normalized into the same `StatusEvent`. This is the part that
  pokes at MAF internals (overriding its MCP message/logging handler and setting a
  progress token below MAF's call path).
- **AG-UI adapter sink:** a subscriber that maps `StatusEvent`s to AG-UI `CUSTOM`/`STATE`
  events for a connected UI.
- **Structured `data`/state payloads** on the event (add when the AG-UI mapping needs them).

## Architecture

A single producer→bus→subscribers pipeline, all inside the harness:

```
tool code ──report_progress()──▶ contextvar ──▶ StatusBus.emit() ──▶ each subscriber
 (built-in / developer)                              (on Session)        (on_status, --verbose printer, …)
```

- **Producer side** is decoupled from any session reference via a `contextvars.ContextVar`,
  so *any* callable (built-in or developer-supplied) can emit by importing one function.
- **Consumer side** is a list of subscriber callbacks on a per-`Session` bus. Default is
  silent (no subscribers).
- The two are connected only while a run is executing: the `Session` binds the contextvar to
  its bus around `agent.run()` and unbinds afterward.

### Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `harness/status.py` (new) | `StatusEvent` dataclass; `StatusBus` (subscribe/emit, thread-safe); the `ContextVar`; `report_progress()`; `bind_bus()` context manager; `current_bus()` | stdlib only |
| `harness/session.py` (modify) | Own a `StatusBus`; expose `subscribe()`; bind the bus around the run via `bind_bus()` | `status` |
| `harness/api.py` (modify) | `Harness(..., on_status=...)`, `solve(..., on_status=...)`, module `solve(..., on_status=...)`; subscribe before the run | `status`, `session` |
| `harness/cli.py` (modify) | `--verbose` registers a printer sink (`on_status`) that formats events to stderr; remove the "temporarily unavailable" notice | `status`, `api` |
| `harness/tools/*.py` (modify) | Built-in tools call `report_progress()` at milestones | `status` |

## Public API

```python
# harness/status.py
@dataclass(frozen=True)
class StatusEvent:
    tool: str                       # emitting tool name, or "harness"
    message: str                    # human-readable status line
    current: float | None = None    # progress numerator (optional)
    total: float | None = None      # progress denominator (optional)
    seq: int = 0                    # monotonic per-bus ordering
    timestamp: float = 0.0          # wall-clock (time.time())

def report_progress(message: str, *, current: float | None = None,
                    total: float | None = None, tool: str = "tool") -> None:
    """Emit a status update from inside a tool. No-op if called outside a bound run.
    `tool` defaults to "tool"; the harness's own built-in tools pass their name
    explicitly (auto-deriving the tool name from the active invocation is a later
    enhancement)."""

class StatusBus:
    def subscribe(self, callback: Callable[[StatusEvent], None]) -> Callable[[], None]: ...
    def emit(self, event: StatusEvent) -> None: ...
    # emit() stamps the event before fan-out: event = dataclasses.replace(event,
    # seq=<next per-bus counter>, timestamp=time.time()). StatusEvent is frozen, so the
    # stamped copy (not the caller's original) is what subscribers receive.

# binding (used by Session)
@contextmanager
def bind_bus(bus: StatusBus) -> Iterator[None]: ...
def current_bus() -> StatusBus | None: ...
```

```python
# consumer surfaces
Harness(config, ..., on_status=callback)
solve(problem, ..., on_status=callback)
session.subscribe(callback)                  # returns an unsubscribe handle
```

## Data flow (a run)

1. `Harness.asolve` builds the `Session`; if `on_status` was given, `session.subscribe(on_status)`.
2. Around `agent.run(problem)`, the session does `with bind_bus(self.status_bus):` — the
   contextvar now points at this run's bus.
3. The model calls a tool. Inside it, `report_progress("…", current=i, total=n)` looks up
   `current_bus()` (set via the contextvar, copied into worker threads for sync tools),
   builds a `StatusEvent` (bus assigns `seq`, stamps `timestamp`), and fans out to subscribers.
4. Subscribers (an `on_status` callback, the `--verbose` printer) receive the event live.
5. On run exit the contextvar unbinds; later `report_progress` calls are no-ops again.

## Error handling

- **Emitting never breaks a task.** Each subscriber dispatch is wrapped in try/except; a
  raising subscriber is swallowed (optionally logged via the stdlib logger), and other
  subscribers still run.
- **Unbound emit is a no-op.** `report_progress` outside a bound run (or with no bus)
  returns silently — tools can call it unconditionally.
- **Thread-safety.** `emit()` is lock-protected; subscriber callbacks may be invoked from a
  worker thread (sync tools run in a thread pool). This is documented; subscribers that need
  the main loop are responsible for marshaling.

## Testing (CI stays offline — no model/network/API)

- **`harness/status.py` unit tests:** a bound bus delivers a `StatusEvent` with correct
  `tool`/`message`/`current`/`total`; `seq` increases monotonically; `timestamp` is set;
  `report_progress` outside a bound bus is a no-op; a subscriber that raises is swallowed and
  does not stop other subscribers; an event emitted from a worker thread reaches the
  subscriber; `subscribe()` returns a working unsubscribe handle.
- **Integration:** invoke an instrumented built-in tool wrapper (e.g. `read_document` with an
  injected converter, or `run_python`) under `bind_bus` with a collector subscriber; assert
  the expected events fire — exercised through `build_tools`, not just the impl.
- **CLI:** `--verbose` registers the printer; run with a stub chat client (no API) and assert
  formatted status lines appear on stderr and the old "temporarily unavailable" notice is gone.

## Out of scope (noted for later)
- MCP status capture (Phase 2: logging + progress notifications, normalized).
- AG-UI adapter sink (`StatusEvent` → AG-UI `CUSTOM`/`STATE`).
- Structured `data`/state payloads; async/queue-based delivery; per-subscriber filtering.
