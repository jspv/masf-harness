# MCP Status Capture — Design (Tool Status Updates, Phase 2)

- **Date:** 2026-06-10
- **Status:** Approved design (brainstormed). Phase 2 of the tool-status-updates feature
  (`2026-06-10-tool-status-updates-design.md`).
- **Motivation:** Phase 1 gave the harness a status channel (`StatusBus` + `report_progress`)
  that the harness's own built-in tools feed. MCP-server tools — which are exactly the long
  operations where mid-run status matters most — produce no status, because MAF surfaces
  neither MCP logging nor MCP progress notifications to the harness. Phase 2 captures both and
  routes them into the same channel, so MCP-tool activity shows up on the same `on_status` /
  `--verbose` sinks as everything else.

## Spike findings (proven end-to-end on MAF 1.8.1 + mcp SDK)

A spike connected a real local FastMCP server (a tool calling `ctx.info(...)` and
`ctx.report_progress(i, n, msg)`) through MAF's `MCPStdioTool` and captured both signals:

- **Logging notifications** (`notifications/message`): MAF exposes `MCPTool.logging_callback`
  (an instance method passed to the underlying `ClientSession` at connect). Wrapping it before
  `connect()` yields `LoggingMessageNotificationParams` (`.level`, `.data`).
- **Progress notifications** (`notifications/progress`): MAF passes **no** `progress_callback`,
  but in the mcp SDK a `ProgressNotification` falls through to `_handle_incoming` →
  `MCPTool.message_handler` **regardless of whether a callback is registered** (mcp
  `shared/session.py:426-427`). Wrapping `message_handler` before `connect()` yields the
  `ProgressNotification` (`.params.progress/.total/.message/.progressToken`).
- **Servers only emit progress if a `progressToken` is on the request.** MAF doesn't set one.
  Injecting `_tool_call_meta_by_name[name]["progressToken"] = <token>` after connect makes MAF
  send `_meta.progressToken`, and the server then emits progress. Verified: with the token,
  progress flows; without it, nothing (only logging, which needs no token).
- Both wrappers must **chain to the originals** to preserve MAF's behavior (notably the
  `notifications/tools/list_changed` reload handled inside `message_handler`).

## Architecture

All of this lives in the existing `Session._attach_mcp` seam (`harness/session.py`) — the one
place the harness already connects an MCP server, owns its lifecycle, and sets the spill
parser on its functions. Phase 2 adds, in that same method, the wiring that translates the
server's logging and progress notifications into the **same `StatusBus` the Session owns**
(`session.status_bus`), reusing the Phase 1 `StatusEvent`.

**No new public API.** A developer still just passes a MAF MCP tool to
`Harness(tools=[...])` / `solve(..., tools=[...])`; its status now flows to the same
`on_status` callback and the same `--verbose` printer as the built-in tools. Capture is
**always-on** and best-effort.

### Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `harness/mcp_status.py` (new) | Pure translation + wiring helpers: build the chaining `logging_callback`/`message_handler` wrappers that emit `StatusEvent`s to a bus; inject the progress token; feature-detect the MAF seams | `status`, stdlib (mcp notifications are duck-typed, not imported) |
| `harness/session.py` (modify) | In `_attach_mcp`: install the wrappers before `connect()`, inject the token after, keep the token→tool map | `mcp_status` |

`mcp_status.py` is kept separate from `session.py` so the (MAF-internal-coupled, therefore
most fragile) logic has one clear home with focused tests, and `session.py` stays a thin
orchestration layer.

## Mechanism (in `_attach_mcp`)

For each MCP tool the harness attaches:

1. **Before `connect()`** — if the tool exposes them, replace `tool.logging_callback` and
   `tool.message_handler` with chaining wrappers (closing over `session` and a server label):
   - **logging wrapper:** emit `StatusEvent(tool=f"mcp:{server}", message=str(params.data))`
     to `session.status_bus`, then `await original(params)`.
   - **message wrapper:** if the message is a `ProgressNotification`, look up the tool name for
     `params.progressToken` in the token map and emit
     `StatusEvent(tool=<tool or f"mcp:{server}">, message=params.message or "", current=params.progress, total=params.total)`;
     then `await original(message)` (always, for any message type).
2. **`await tool.connect()`** (existing line).
3. **After connect** — if `tool` has `_tool_call_meta_by_name`, for each function name set a
   stable token (e.g. `f"harness:{server}:{name}"`), merging into any existing per-tool meta,
   and record `token → name` in the map used by the message wrapper.

Emitting **directly to `session.status_bus`** (not via the `report_progress` contextvar) is
deliberate: MCP notifications are processed on the ClientSession's receive-loop task, and a
direct bus reference avoids any dependence on contextvar propagation into that task.

### Attribution
- **Progress** → the specific tool name, resolved from the per-tool `progressToken` via the
  token map (falls back to `mcp:{server}` if unknown).
- **Logging** → `mcp:{server}` — logging notifications are session-level in the protocol and
  carry no tool identity.

(`server` is the MAF tool's `.name`, the label the developer gave the MCP server.)

## Robustness — the central risk

This depends on MAF-**private** seams: the `logging_callback` / `message_handler` instance
methods and the `_tool_call_meta_by_name` dict. These can change across MAF releases. Mitigation:

- **Feature-detect every seam** with `hasattr`/`getattr`. If `logging_callback` is missing, skip
  logging capture; if `message_handler` or `_tool_call_meta_by_name` is missing, skip progress
  capture. Missing seams degrade to "no MCP status," never an error.
- Capture is **best-effort**: wrappers always delegate to the original handler (try/finally), so
  a translation bug can never break MAF's own notification handling or the tool call. `bus.emit`
  already swallows subscriber exceptions.
- The behavior is **pinned by a real-MCP gate test** (a committed FastMCP fixture) so a future
  MAF bump that moves a seam is caught by CI, not in production.

## Error handling

- Wrapper translation is wrapped so any exception is swallowed and the original handler still
  runs (status is best-effort).
- Token injection is guarded by the `_tool_call_meta_by_name` feature check; failure to inject
  simply means no progress (logging still works).
- All existing `_attach_mcp` behavior (connect-failure naming, lifecycle registration, spill
  parser attachment) is unchanged.

## Testing (CI stays offline — local subprocess MCP server, no network/API)

- **Fixture:** commit a tiny FastMCP server under `tests/fixtures/` (a `slow(n)` tool that
  calls `ctx.info(...)` and `ctx.report_progress(i, n, msg)`), launched as a stdio subprocess.
- **End-to-end gate test:** attach it through the harness (`Session` + a real
  `MCPStdioTool`), drive the tool, and assert a subscriber receives **both** a logging
  `StatusEvent` (`tool == "mcp:<server>"`) and a progress `StatusEvent` with
  `current`/`total` set and `tool ==` the emitting tool name. Skipped only if the MAF seams
  are absent (the future-MAF guard), never gated on network.
- **Unit tests** for the translation helpers in `mcp_status.py`: feeding a fake
  `LoggingMessageNotificationParams` and a fake `ProgressNotification` (or duck-typed stand-ins)
  through the wrappers produces the expected `StatusEvent`s and always calls the chained
  original; an unknown progress token falls back to `mcp:<server>`.
- **Graceful-degradation test:** a fake MCP tool lacking the seams attaches without error and
  emits no status.

## Out of scope (noted for later)
- Per-call progress-token correlation (v1 uses a stable per-tool token; fine for the
  single-agent sequential loop).
- The AG-UI adapter sink (separate, consumes the same bus).
- Capturing other MCP notification types (resources/prompts list-changed) as status.
