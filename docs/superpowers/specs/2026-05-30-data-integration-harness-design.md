# Data-Integration Harness — Design Spec

- **Date:** 2026-05-30
- **Status:** Approved design, pre-implementation
- **Author:** jspv
- **Related:** `spike.py` (MAF feasibility spike)

## 1. Goal

Build a reusable Python **substrate** — a single-agent-loop harness (the modern
coding-agent pattern) on Microsoft Agent Framework (MAF) — that solves a variety of
data-gathering and
data-integration problems. Tools/MCP servers feed it datasets (JSON, large text
blobs, dataframes); it fetches data from links; it has a file sandbox and a Python
execution sandbox so it can write and run code to analyze the data, returning
trustworthy answers and artifacts.

This spec covers **v1: the general substrate**, not any one problem. Specific
problems plug in later as tools/MCP servers.

## 2. Context & feasibility (already established)

A spike (`spike.py`) confirmed against the installed `agent-framework-core==1.7.0`:

- MAF ships a native single-agent harness: `create_harness_agent()` with a
  single dynamic loop, compaction strategies, todo, memory, skills, `max_iterations`
  (default 100), streaming, and middleware.
- OpenTelemetry works out of the box (GenAI semantic conventions: token usage,
  spans, trace IDs).
- The full autonomous gather→act→verify chain runs end-to-end with a competent,
  cheap model (`gpt-4o-mini`); small local models (gpt-oss:20b, qwen3-coder:30b via
  Ollama) are unreliable at multi-step tool chaining. **Model strength, not the
  framework, is the determining factor.**

Setup: Python 3.12 (3.14 too new), `uv add --prerelease=allow agent-framework-core
agent-framework-openai`. Run via `OpenAIChatClient(model=..., env_file_path=".env")`.

## 3. Key decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Anchor | **General substrate first** | Foundation a variety of problems plug into |
| Data flow | **Typed handles + sandbox** | Large data never enters context; references-not-payloads model, generalized to any dataset |
| Sandbox isolation (v1) | **Local subprocess + workdir**, behind a swappable interface | Zero infra; upgrade to container/remote later without harness changes |
| Entry point | **Library core + thin CLI** | Embeddable; CLI for interactive testing |
| Core loop | **Build on MAF `create_harness_agent`** (Approach A) | Leans on spike-proven built-ins; keeps loop dynamic |
| Code execution model | **Agent writes `.py` files and runs them with args** | Debuggable, reusable, composes with `write_file`; "give the agent a computer" |
| Confinement | **One root dir; tools strictly jailed; code best-effort + opt-in OS jail** | Strong now, airtight when we move to a container |

**Future (explicitly out of v1):** container/remote sandbox tier (planned next),
MAF skills + memory providers, a MAF Workflow durability/HITL outer shell.

## 4. Architecture — components

Python package `harness` (renameable). Eight focused, independently testable units:

| Unit | Responsibility | Depends on |
|---|---|---|
| `config` | Typed `HarnessConfig` (model, thresholds, sandbox, limits, root_dir) + `.env` | — |
| `session` | One run's `root_dir` + sandbox + handle store; lifecycle (create/cleanup) | config, sandbox, handles |
| `handles` | `Handle` model + `HandleStore` persisting json/text/dataframe under root | config |
| `sandbox` | `SandboxExecutor` interface + `LocalSubprocessSandbox`; runs script files with args, injects `load`/`save`/`emit` helper | handles |
| `spill` | MAF function middleware: intercepts tool/MCP returns; large/structured → handle | handles |
| `tools` | The agent's 8 tools (below) | session |
| `agent` | `build_agent(session, config)` over `create_harness_agent`; wires prompt, tools, middleware, compaction | tools, spill |
| `api` + `cli` | `Harness` / `solve()`; thin streaming CLI | agent, session |

**Boundaries:** `sandbox` and `handles` are LLM-agnostic and usable standalone.
`spill` is the *only* auto-creator of handles from tool output (single place to
reason about what enters context). User tools/MCP plug in at `api` and get spill
treatment transparently. `cli` depends only on `api`.

## 5. Data flow

**Bootstrap.** `solve()` creates a `Session`: a `root_dir` (default
`./.harness/sessions/<id>/`), a `HandleStore` rooted there, a `LocalSubprocessSandbox`
bound to that root + venv. User tools/MCP are registered, each wrapped with `spill`.
`build_agent()` builds the MAF harness agent; we call `agent.run(problem, stream=True)`.

**The handle (what the model sees instead of raw data):**
```json
{ "handle": "h1", "kind": "dataframe", "source": "query_sales(region='EU')",
  "schema": {"date":"date","units":"int","revenue":"float"},
  "n_rows": 480000, "n_cols": 3, "bytes": 18402211,
  "path": "handles/h1.parquet",
  "preview": "date,units,revenue\n2025-01-01,12,840.0\n... (5 of 480000 rows)" }
```
~200 tokens stands in for 18 MB.

**Loop (worked example — "total EU revenue, excluding bad rows"):**
1. Model calls `query_sales(region="EU")` → 480k rows.
2. `spill` middleware intercepts the return *before context*: over threshold →
   write `handles/h1.parquet`, infer schema+preview, register `h1`, and replace the
   model-visible result with the handle summary. (Small returns pass through.)
3. Model reasons over the handle, writes a script via `write_file("analyze.py", ...)`,
   then `run_python(path="analyze.py", args=["EU"])`:
   ```python
   import sys
   from harness_sandbox import load, save, emit
   df = load("h1")
   clean = df[df.revenue > 0]
   save("h2", clean)
   emit({"total": float(clean.revenue.sum()), "dropped": int((df.revenue <= 0).sum())})
   ```
4. Sandbox runs the file (venv, `cwd=root`, timeout, rlimits); returns
   `{stdout, result(emit), error, exit_code, new_handles:["h2"]}`. Only the small
   `emit` payload re-enters context; the cleaned 18 MB stays on disk as `h2`.
5. Verify: the system prompt steers a sanity-check (row reconciliation, dropped
   count) before answering.
6. Answer: total + note on dropped rows. `Result` carries `final_text`, `handles`,
   `files`, `usage`, `session_dir`.

**Invariants:**
- Nothing large enters context. Only paths in: a bounded handle summary, or a
  `emit()`/`print` the model deliberately sized. `read_file`/`fetch_url` are bounded.
- Handles are immutable, content-addressed by id; derived data → new handles.
- The sandbox is the only execution surface; it speaks the same `load`/`save` store
  the middleware uses, so tool-produced and code-produced data are identical kinds.
- Failures are structured data the model adapts to, never loop-killing crashes.

## 6. Tool surface (8 sharp tools)

| Tool | Signature | Returns to context |
|---|---|---|
| `write_file` | `write_file(path, content)` | Confirmation (root-jailed) |
| `read_file` | `read_file(path, offset=0, limit=2000)` | Bounded, paginated text |
| `list_files` | `list_files(path=".")` | Directory listing |
| `search` | `search(pattern, path=".", glob=None, ignore_case=False, max_matches=100)` | `file:line:byte-offset:snippet` (ripgrep; Python-regex fallback). `path` may be a file or folder, recursive within it. Covers handle backing files. |
| `run_python` | `run_python(path=None, args=[], code=None)` | `{stdout, result, error, exit_code, new_handles}`. Canonical: run a script file with argv. `code=` is a convenience that writes a temp script then runs it. |
| `fetch_url` | `fetch_url(url, max_bytes=…)` | A typed handle (content-type → json/text); body spilled, never inlined |
| `inspect_handle` | `inspect_handle(id, rows=20, stats=False)` | Deeper on-demand look: fuller schema, more preview, optional describe/value-counts |

The agent loop follows the established search→read→analyze triad: **`search` to locate → `read_file` to
load the right slice → `run_python` to analyze.** Domain data sources are
user-supplied tools/MCP servers, auto-handled by `spill`.

## 7. Public API & CLI

```python
from harness import Harness, HarnessConfig, solve

h = Harness(HarnessConfig(model="gpt-4o-mini"))
result = h.solve(
    "Total EU revenue in 2025, excluding invalid rows?",
    tools=[query_sales],
    mcp_servers=["mcp://internal-data"],
)
result.final_text     # answer
result.handles        # dict[str, Handle]
result.files          # artifacts in the workdir
result.usage          # tokens/cost (OTel)
result.session_dir    # full audit trail

solve("...", tools=[...], model="gpt-4o-mini")          # one-shot convenience

async for event in h.asolve("...", tools=[...]):        # streaming: tool calls, tokens, handle-created
    ...
```

CLI (thin wrapper over `asolve`):
```
harness "Total EU revenue in 2025?" --tool mymod:query_sales --model gpt-4o-mini
```
Streams tool calls + tokens; prints the answer; leaves `session_dir` to inspect.

## 8. Confinement & security model

Every session has one **root directory** (`HarnessConfig.root_dir`, default the
session workdir). Everything — handles, agent-written scripts, reads/writes, the
sandbox `cwd` — lives under it. The agent may work in the root and any **subfolder**,
never above.

**Layer 1 — Tool path-jail (guaranteed).** All model-supplied paths (`read_file`,
`write_file`, `list_files`, `search`, `run_python` script path) pass through one
chokepoint `safe_path(root, p)` that: resolves with `realpath` (symlinks followed
*before* the check, so symlink escapes are caught), rejects any resolved path outside
`root` (blocks `..`, absolute paths, symlink escapes). Single auditable location.

**Layer 2 — Executed code (best-effort at local tier).** `run_python` runs arbitrary
agent Python; the tool path-jail cannot stop direct `open()`/`os.system()`. At this
tier we enforce: subprocess `cwd=root` + scrubbed env; `resource.setrlimit` caps (CPU,
memory, file size, no core) + wall-clock timeout; **optional** OS wrapper
(`sandbox-exec` on macOS / `bwrap`/`firejail` on Linux) via `sandbox.confine_os` that
restricts fs writes to root and denies network even for arbitrary code (auto-detects
the tool; off → best-effort only).

**Airtight** isolation of arbitrary code arrives when the sandbox tier moves to a
container/micro-VM — and because everything sits behind `SandboxExecutor`, that swap
changes zero harness code. **Container is the planned next step after v1.**

## 9. Configuration (`HarnessConfig`)

- `model`, `model_settings` (temperature, max tokens), provider via MAF client
- `spill_threshold_bytes` (~8 KB default) — when a tool return becomes a handle
- `max_context_window_tokens` — fed to MAF compaction
- `root_dir` + cleanup policy — the confinement boundary
- `sandbox`: `kind="local"`, python timeout, mem/CPU/file rlimits, `confine_os` flag,
  preinstalled libs (`pandas`, `pyarrow`, `numpy`, `httpx`)
- `fetch`: `max_bytes`, timeout, allowed URL schemes
- built-ins: keep MAF `todo` + `compaction`; disable `mode`/`memory`/`skills`/
  `web_search` in v1 (we provide `fetch_url`; skills/memory are a v1.1 lever)

## 10. Error handling

Failures are data, not crashes:
- `run_python` exceptions/timeout/rlimit-kill → `{error, exit_code, killed_by}`; loop
  continues so the model fixes its script and re-runs.
- `safe_path` violations → clear `"path escapes root"` message to the model.
- `fetch_url` failures, `load(id)` on missing handle → structured errors.
- Loop ceiling: MAF `max_iterations` (default 100, configurable); on hit → partial
  `Result` flagged `incomplete`, never a hang.
- Provider errors (rate-limit/network) → bounded retry with backoff.

## 11. Testing strategy & quality bar (first-class, throughout)

**TDD: tests are written before implementation for every unit.** Strong automated
testing is a core principle of this project, not a final step.

- **Unit tests:**
  - `handles`: round-trip json/text/dataframe (parquet); schema/preview generation.
  - **`safe_path`: the most heavily tested code in the system** — exhaustive
    traversal/symlink/absolute-path cases; security-critical; must fail closed.
  - `spill`: threshold logic, structured-vs-scalar detection, summary shape.
  - `sandbox`: `run_script` stdout/`emit`/`new_handles` diff; timeout; rlimit kill;
    error capture; arg passing.
  - each tool against a fake `Session`.
- **Integration tests:** end-to-end `solve()` against a **stub model client** that
  emits a scripted tool-call sequence — exercises the full loop/spill/sandbox wiring
  deterministically, **no API cost**. Plus one opt-in live smoke test against
  `gpt-4o-mini`.
- **Security tests:** explicit "agent attempts to escape root" scenarios must fail.
- **CI-ready:** fast unit/integration suite runnable without network or API keys;
  live tests gated behind an env flag.
- Coverage tracked; the confinement boundary and spill logic held to the highest bar.

## 12. Observability

- Build on MAF OTel (verified): `gen_ai.*` spans/metrics out of the box.
- Add custom spans: tool calls (name, handle in/out), sandbox runs (duration, exit,
  rlimit hits), spill events (bytes → handle id).
- `session_dir` is the durable audit trail: every script, every handle (with
  provenance), the transcript, and `manifest.json`. Sessions are re-openable.
- Streaming `on_event` hook surfaces tool calls / handle-created / tokens to the CLI;
  `Result.usage` from OTel.

## 13. Risks & open questions

- **Local-tier code isolation is best-effort.** Accepted for v1; container is next.
- **Dataframe transport across the subprocess boundary** uses parquet files via the
  handle store (not in-memory hand-off); fine for the handle model, validate perf on
  large frames.
- **Spill threshold tuning** (size + structured detection) will need iteration to
  avoid both context bloat and over-spilling trivial results.
- **MCP return shapes** vary; the spill middleware must handle text/json/binary
  content blocks robustly.
- **Model dependency:** reliable multi-step autonomy needs a competent model; document
  the floor.
