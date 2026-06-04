# MAF-Composable Harness — Design Spec

- **Date:** 2026-06-03
- **Status:** Approved design, pre-implementation
- **Author:** jspv
- **Related:** `docs/superpowers/specs/2026-05-30-data-integration-harness-design.md` (the substrate this builds on), `harness/agent.py`, `harness/spill.py`, `harness/session.py`

## 1. Goal

Today the harness is a closed front door: `Harness`/`solve()` hard-code the agent
instructions, the tool set, and the MAF `disable_*` flags. You cannot bring your own
agent, tools, or MCP servers and have them *use* the harness.

This spec inverts the exposure. A developer should be able to **build a Microsoft
Agent Framework (MAF) agent the normal MAF way — supplying their own task
instructions, tools, and MCP servers — and have that agent transparently gain the
harness's broader capabilities**: the handle/spill data substrate, a sandboxed
`run_python`, file tools, and web tools, all confined to one session root with a
durable audit trail.

The mental model is the modern coding-agent pattern: the agent is a competent
*operator of its own computer*. It already knows how to use files, run commands
without flooding the context, and manage large data by reference. The developer
supplies only **the problem to solve** and **optional extra capabilities**; they never
learn the word "handle."

## 2. Key decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Integration boundary | **Composable toolkit (B) with a convenience wrapper (A) on top** | MAF-native and reusable across agent types; the wrapper is thin sugar over the same path |
| Primary composable seam | **`session.create_agent()` factory** | One call mirroring MAF's agent construction, with all operational wiring injected internally; nothing to forget, no magic splat |
| Opt-in granularity | **Capability bundles** (`code`, `files`, `web`) | Mix-and-match capability layers; the data substrate is core, not a bundle |
| Data substrate | **Always-on core**, not a toggle | Handle store + spill + `inspect_handle` *is* the harness's reason to exist; costs nothing when unused |
| Plugged-in tool/MCP spill | **`result_parser` on every `FunctionTool` (MAF's documented hook)** | MAF-blessed, future-compatible, sees the *faithful raw return value* before serialization (verified) |
| Operating instructions | **Harness-owned, injected via MAF `harness_instructions`** | The handle/`load`/`save`/`run_python` protocol rides with the bundles; the developer writes only `agent_instructions` (the task) |
| MCP connection lifecycle | **Session owns it** (async context manager) | MCP tools must be connected to expose `.functions` for `result_parser`, and stay open across the run; the session connects and closes them |
| Sync/async | **Async-first composable path; `Harness.solve()` and `Harness.asolve()`** | The composable path is `async with` + `await create_agent`; the wrapper offers both sync and async ergonomics |
| Existing API | **`Harness`/`solve()`/CLI rebuilt on the new toolkit** | The CLI becomes the canonical worked example; if the public path is awkward, the CLI feels it first |

**Future (explicitly out of v1):** developer-defined custom bundles; treating
non-harness MAF agent types as the primary target (the raw pieces below enable it, but
it is not the focus).

## 3. Verified MAF facts (the design hinges on these)

Confirmed against the installed `agent-framework-core` in `.venv`:

- **Two instruction slots.** `create_harness_agent(harness_instructions=…,
  agent_instructions=…)` assembles them as `f"{harness}\n\n{agent}"`
  (`_harness/_agent.py:_assemble_instructions`). `harness_instructions` overrides the
  operational layer; `agent_instructions` is the task appended after. This is exactly
  the operator-vs-task split we want.
- **`result_parser` sees the raw object.** `FunctionTool.invoke`
  (`_tools.py`) computes `result = await func(...)` and then `parsed =
  result_parser(result)` *before* any serialization. Signature
  `Callable[[Any], str | list[Content]]`. So a tool returning a `DataFrame` reaches our
  parser as a `DataFrame`; the parser returns the handle-summary string the model sees.
- **MCP tools are `FunctionTool`s.** An MCP server (`MCPStdioTool` /
  `MCPStreamableHTTPTool` / `MCPWebsocketTool`) exposes its tools as `FunctionTool`
  instances via `.functions` **after connecting**, and MAF documents setting
  `result_parser` on those instances.
- **MCP lifecycle.** `MCPTool` is an async context manager (`__aenter__`/`__aexit__`,
  `connect()`, `close()`); `.functions` is only populated after `connect()`.

## 4. Architecture

The anchor remains the existing **`Session`** (`harness/session.py`): one root dir →
one `HandleStore` → one sandbox cwd → the audit trail. We extend it with the
composable seam rather than introducing a parallel concept.

```
Session (root dir + HandleStore + sandbox + audit trail; async context manager)
 ├── core (always on): HandleStore + spill result_parser + inspect_handle
 ├── bundles (opt-in): code | files | web   → tools + harness_instructions fragments
 ├── create_agent(client, agent_instructions, tools, bundles, **maf_passthrough)
 │     → connects MCP servers, attaches spill result_parser to every plugged-in
 │       FunctionTool, assembles harness_instructions, calls create_harness_agent,
 │       returns a normal MAF Agent
 ├── raw pieces (power users): tools(*bundles), harness_instructions(*bundles),
 │     spill_parser(), context(*bundles)
 ├── handles / artifacts (post-run accessors)
 └── __aexit__: close all MCP connections (always); persist or delete root (config)

Harness (convenience wrapper A): owns a Session + event loop
 ├── solve(task)   — sync
 └── asolve(task)  — async
CLI: rebuilt on Harness; the canonical worked example
```

**Boundaries.** The data substrate (handles + spill) and the sandbox remain
LLM-agnostic. `create_agent` is the single place that wires plugged-in capabilities
to the session (connect MCP, attach `result_parser`, assemble instructions). The
convenience wrapper depends only on the session seam; the CLI depends only on the
wrapper.

## 5. Public API

### 5.1 Composable path (primary)

```python
from agent_framework.openai import OpenAIChatClient
from harness import Session, HarnessConfig

client = OpenAIChatClient(model="gpt-5-mini", env_file_path=".env")

async with Session.create(HarnessConfig()) as session:     # root + store + sandbox
    agent = await session.create_agent(                    # connects MCP, attaches spill
        client,
        agent_instructions="Reconcile our EU sales and flag anomalies.",  # the task
        tools=[my_query_tool, my_mcp_server],              # extra capabilities (fn + MCP)
        bundles=("code", "files", "web"),                  # optional layers; default: all
        # MAF passthrough: max_context_window_tokens, max_output_tokens, name, ...
    )
    result = await agent.run(task)
    session.handles      # dict[str, handle summary] — everything spilled
    session.artifacts    # list[str] — user-meaningful files under root
# ← MCP connections closed here; handles/scripts/transcript persist as the audit trail
```

The developer writes `agent_instructions` (their problem) and brings tools/MCP. They
never write the handle protocol: `create_agent` fills MAF's `harness_instructions`
slot with the operating manual for exactly the bundles in play, attaches the spill
`result_parser` to every plugged-in tool, and connects MCP servers.

### 5.2 Convenience wrapper (sugar)

```python
from harness import Harness

h = Harness(tools=[my_query_tool, my_mcp_server])   # all bundles default-on
result = h.solve("Reconcile our EU sales and flag anomalies.")     # sync
result = await h.asolve("Reconcile our EU sales and flag anomalies.")  # async
```

`Harness` manages the event loop and the `async with` internally, rebuilt on the
`session.create_agent()` path. `solve`/`asolve` return the existing `Result`
(`final_text`, `handles`, `files`, `session_dir`, `error`).

### 5.3 Raw pieces (power users, non-primary)

For hand-wiring into any MAF agent (including non-harness agent types):

```python
session.tools(*bundles)                # the bundle tool callables (already spill-bound)
session.harness_instructions(*bundles) # the operating-manual text for those bundles
session.spill_parser()                 # the result_parser to attach to your own tools
session.context(*bundles)              # context providers, if any
```

## 6. Capability bundles

**Core (always on, not a toggle):** `HandleStore` + spill `result_parser` +
`inspect_handle`. This is "references not payloads." It has no cost when unused (no
oversized returns → no handles created).

| Bundle | Tools | Notes |
|---|---|---|
| `code` | `run_python` (sandbox + `load`/`save` runtime) | Uses the core handle store for `load(id)`/`save(id)`. |
| `files` | `read_file`, `write_file`, `list_files`, `search` | Operates under root, including handle backing files. |
| `web` | `fetch_url`, `web_search`, `web_extract` | `web_search`/`web_extract` need `TAVILY_API_KEY`; degrade to a structured error without it. |

No hard dependencies among optional bundles. `web` output (a fetched-page handle) is
most useful consumed via `files` or `code`, but the agent can always peek with core's
`inspect_handle`. `bundles=("code",)` or `bundles=("files","web")` are both valid.
`bundles` defaults to all optional bundles on.

Each bundle contributes (a) its tool callables and (b) an `harness_instructions`
fragment describing how to operate those tools. `create_agent` concatenates the
fragments for the selected bundles (plus the always-on core fragment) into the
`harness_instructions` it passes to MAF.

## 7. Spill via `result_parser`

`create_agent` turns **every plugged-in capability** into a `FunctionTool` carrying a
spill `result_parser` bound to the session's `HandleStore`:

- **Plain dev function** → wrapped as `FunctionTool(fn, result_parser=spill)`.
- **MCP server** → connected, then `result_parser=spill` set on each of its
  `.functions`.

The spill parser receives the raw return value and:

1. Returns it **unchanged** (as its string/Content form) if it is small / already a
   handle summary (`_is_handle_summary`) — no spill.
2. Otherwise writes it to the `HandleStore` (DataFrame → Parquet with schema+preview;
   oversized dict/str → JSON/text; binary → byte handle) and returns the handle-summary
   **string** the model sees.

The harness's own built-in tools (`run_python`, `read_file`, `search`,
`fetch_url`, `inspect_handle`, …) deliberately **do not** get the spill parser — they
already manage their own output, and `run_python`'s `result`/`new_handles` control
dict must reach the loop intact. One handle store underneath means the model sees a
uniform handle-summary shape regardless of source.

This **replaces** the current `wrap_external_tool` / `wrap_external_tools` mechanism
in `harness/spill.py`. The threshold and shape logic (`_should_spill`,
`_is_handle_summary`, `_maybe_spill`) is retained, repointed at the `result_parser`
seam.

## 8. Lifecycle & confinement

- **`Session.create(config)`** stays synchronous (makes the root dir, `HandleStore`,
  sandbox).
- **`create_agent()` is async** — it may connect MCP servers (required to expose
  `.functions`).
- **`Session` is an async context manager.** It owns every connected MCP server;
  `__aexit__` closes them all, even on error.
- **Cleanup policy (config).** By default the root **persists** (audit trail); MCP
  connections **always** close. A `cleanup=True` option deletes the root on exit for
  throwaway runs.
- **No MCP passed?** `create_agent` simply has nothing to connect; the `async with`
  idiom is unchanged.
- **Confinement** is inherited from the existing substrate (single root, `safe_path`
  tool path-jail, sandboxed `run_python`); this spec adds no new escape surface. MCP
  servers are external processes/endpoints the developer chose to trust.

## 9. CLI

The CLI is rebuilt on `Harness` and serves as the canonical example: it constructs the
agent the developer way (`Session` → `create_agent`) and hands it the task. Existing
flags (`--model`, `--root`, `--verbose`) are preserved.

## 10. Error handling

- **Missing `TAVILY_API_KEY`** with the `web` bundle → `web_search`/`web_extract`
  return a structured error (unchanged behavior); the rest of the harness is
  unaffected.
- **MCP connect failure** in `create_agent` → raise a clear error naming the server;
  any already-connected servers are closed before propagating.
- **Agent/provider failure** during the run → the convenience wrapper preserves
  work-so-far in `Result` with `error` set (unchanged behavior). The composable path
  surfaces the exception to the caller (who owns the `async with`).
- **Spill parser failure** on a tool result → fall back to MAF's default parsing
  (`str(result)`), matching `FunctionTool.invoke`'s own fallback, so a bad spill never
  kills the loop.

## 11. Testing strategy

Extends the project's TDD bar; the fast suite needs no network or API keys.

- **`result_parser` spill (unit):** raw object → handle-summary string; faithful
  `DataFrame` → Parquet handle w/ schema+preview; oversized dict/str → JSON/text handle;
  binary → byte handle; no-op when already a handle summary; never attached to built-in
  control tools (`run_python` result/new_handles intact).
- **Bundle wiring (unit):** `create_agent(bundles=…)` exposes exactly the expected tool
  set; core (`inspect_handle` + spill) always present; `harness_instructions` assembled
  from the selected bundles' fragments.
- **MCP (integration, no network):** an in-process stub MCP server — assert
  `create_agent` connects it, attaches `result_parser` to each `.functions` entry,
  spills an oversized return, and that `__aexit__` closes the connection even on error.
  One optional real-MCP smoke test gated behind an env flag.
- **`create_agent` end-to-end (deterministic):** the existing `StubChatClient` drives a
  scripted sequence calling *both* a plain dev tool and the stub MCP tool; assert both
  spill into the same store and the model sees only summaries.
- **Sync/async parity:** `Harness.solve()` and `Harness.asolve()` produce equivalent
  `Result`s over the same stub; CLI smoke test.
- **Unchanged:** live tests stay gated behind `HARNESS_LIVE=1`.

## 12. Risks & open questions

- **MCP `result_parser` timing.** We rely on `.functions` being populated and mutable
  after `connect()` and on MAF invoking those `FunctionTool` instances (so our parser
  fires). The implementation plan must confirm against a live/stub MCP server that the
  attached parser is actually used at call time.
- **MCP result shapes.** MCP returns content blocks (text/json/binary); the spill
  parser must handle each robustly and decide thresholds on the serialized content.
- **`harness_instructions` override vs. MAF defaults.** Supplying `harness_instructions`
  *overrides* MAF's `DEFAULT_HARNESS_INSTRUCTIONS`. Since the harness disables MAF's
  built-in tool surface (todo/mode/memory/web_search) and supplies its own, the bundle
  fragments must cover the operating manual completely; verify nothing essential is lost
  by overriding.
- **Event-loop ownership in `Harness.solve()`.** The sync wrapper must drive the async
  session without conflicting with an already-running loop (telemetry-safe, per the
  existing `run_agent_sync` note in `harness/agent.py`).
