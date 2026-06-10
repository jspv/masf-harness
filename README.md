# harness

A reusable Python substrate for building **autonomous data-gathering and data-integration agents** — a single-agent-loop ("coding agent") harness on the [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) (MAF).

You give it a task and a set of tools (or MCP servers). It fetches data from links and tools, keeps large datasets **out of the model's context** as typed *handles*, and uses a **sandboxed Python environment** to write and run code that analyzes that data — returning a trustworthy answer plus a re-openable audit trail of every script and artifact.

This is **v1: the general substrate**. Specific problems plug in later as user-supplied tools / MCP servers.

## The core idea: references, not payloads

The central design move is keeping large data out of context. When a tool, MCP server, or URL returns something big or structured, a **"spill" middleware** intercepts it *before it reaches the model* and writes it to disk as a typed **handle** (JSON, text, or a dataframe stored as Parquet). The model only ever sees a compact summary:

```json
{ "handle": "h1", "kind": "dataframe", "source": "query_sales(region='EU')",
  "schema": {"date": "date", "units": "int", "revenue": "float"},
  "n_rows": 480000, "n_cols": 3, "bytes": 18402211,
  "path": "handles/h1.parquet",
  "preview": "date,units,revenue\n2025-01-01,12,840.0\n... (5 of 480000 rows)" }
```

~200 tokens stand in for 18 MB. To actually work with the data, the agent is given a **computer**: it writes `.py` scripts and runs them in a sandbox that speaks the same handle store (`load(id)` / `save(id, obj)`). Only the small value it deliberately prints/returns flows back into context; derived data becomes new handles.

## Install

Requires **Python 3.12** and [`uv`](https://docs.astral.sh/uv/). `agent-framework-core` is a prerelease, so allow prereleases when syncing:

```bash
uv sync --prerelease=allow
```

Document ingestion (`read_document`) uses [Docling](https://github.com/DS4SD/docling), an optional heavy dependency that downloads models on first use. Enable it with:

```bash
uv sync --prerelease=allow --extra docs
```

Create a `.env` in the project root:

```bash
OPENAI_API_KEY=sk-...        # required
TAVILY_API_KEY=tvly-...      # optional — enables web_search / web_extract
```

## Quickstart

### CLI

```bash
uv run harness "What is the total EU revenue in 2025, excluding invalid rows?"
```

Flags:

| Flag | Default | Meaning |
|---|---|---|
| `--model` | `gpt-5-mini` | Model name (any provider via the MAF OpenAI client) |
| `--root` | a fresh session dir | Workspace root (the confinement boundary) |
| `-v`, `--verbose` | off | Print each tool call (and `run_python` code) as it happens |

The CLI prints the answer and leaves `[session: …]` — the directory holding every script, handle, and artifact for inspection.

### Library

```python
from harness import Harness, HarnessConfig, solve

# One-shot convenience
result = solve("Summarize the latest figures from https://example.com/report.pdf")
print(result.final_text)

# Reusable harness with your own tools
def query_sales(region: str) -> list[dict]:
    """Return sales rows for a region."""
    ...

h = Harness(HarnessConfig(model="gpt-5-mini"))
result = h.solve(
    "Total EU revenue in 2025, excluding invalid rows?",
    tools=[query_sales],          # plain Python callables; auto-wrapped + spill-handled
)

result.final_text   # the answer
result.handles      # dict of handle summaries produced during the run
result.files        # user-meaningful files written under the session root
result.session_dir  # full audit trail (scripts, handles, transcript)
result.error        # None, or a string if the run failed (e.g. context overflow)
```

Your tools are wrapped as MAF agent tools automatically, and their returns pass through the same spill middleware — so tool-produced and code-produced data are identical kinds of handle.

### MCP servers

Pass a MAF `MCPTool` — stdio, Streamable-HTTP, or WebSocket — in the same `tools=[…]` list. The harness connects each server, **owns its lifecycle** (closing it when the run ends, even on error), and attaches the same spill handling to every tool the server exposes — so a large MCP return (a long message list, a big query result) lands as a clean typed handle instead of flooding context.

```python
from agent_framework import MCPStdioTool
from harness import Harness, HarnessConfig

# A stdio MCP server, launched however your other MCP clients launch it.
# (If the server resolves its config/credentials relative to a directory, launch it
#  from there, e.g. command="sh", args=["-c", "cd /path/to/server && exec uv run its-cmd"].)
mcp = MCPStdioTool(name="msgraph", command="uv", args=["run", "msgraph-mcp"])

h = Harness(HarnessConfig(model="gpt-5-mini"))
result = h.solve("List the subjects of my 5 most recent emails.", tools=[mcp])
print(result.final_text)
```

A remote server works the same way:

```python
from agent_framework import MCPStreamableHTTPTool

mcp = MCPStreamableHTTPTool(
    name="my-service",
    url="https://example.com/mcp",
    header_provider=lambda _: {"Authorization": f"Bearer {token}"},  # if it needs auth
)
result = h.solve("…", tools=[mcp])
```

MCP support needs the `mcp` SDK, which is a declared dependency, so `uv sync` already installs it.

**On result size.** Spilling is lossless — a large page is preserved whole as a handle, so an MCP server's pagination cursor survives and the agent can fetch the next page. The two ends of the spill-over zone are configurable (`spill_threshold_bytes` … `max_spill_bytes`): a well-behaved server that paginates returns bounded pages that flow through cleanly, while an unbounded dump past `max_spill_bytes` raises `SpillLimitExceeded` rather than silently filling disk — nudging the source toward server-side pagination/filtering.

## Tool surface

The agent gets nine root-confined tools. Domain data sources are *your* tools/MCP servers, auto-handled by spill.

| Tool | Purpose |
|---|---|
| `write_file(path, content)` | Write a file (root-jailed) |
| `read_file(path, offset=0, limit=2000)` | Bounded, paginated text |
| `list_files(path=".")` | Directory listing |
| `search(pattern, …)` | ripgrep over files/handles, with a Python-regex fallback |
| `run_python(code=None, path=None, args=[])` | Run a script in the sandbox → `{stdout, result, error, exit_code, new_handles}` |
| `fetch_url(url)` | Fetch a URL → typed handle; HTML is cleaned to Markdown (trafilatura) |
| `inspect_handle(id, …)` | Deeper on-demand look at a handle (fuller schema, more preview, optional stats) |
| `web_search(query, max_results=5)` | Tavily web search (needs `TAVILY_API_KEY`) |
| `web_extract(url)` | Tavily clean-content extraction |
| `read_document(source)` | A workspace path or URL → clean markdown handle (tables preserved) via Docling; needs the `docs` extra |

The loop follows the **search → read → analyze** triad: `search` to locate, `read_file` to load the right slice, `run_python` to analyze.

## Confinement & security

Every session has one **root directory**; everything — handles, agent-written scripts, reads/writes, the sandbox `cwd` — lives under it.

- **Layer 1 — Tool path-jail (guaranteed).** All model-supplied paths route through one chokepoint, `safe_path(root, p)`, which resolves symlinks *before* checking and rejects any path outside the root (blocks `..`, absolute paths, symlink escapes). It's the most heavily tested code in the project.
- **Layer 2 — Executed code (best-effort at the local tier).** `run_python` runs in a subprocess with `cwd=root`, a scrubbed environment, `resource` rlimits (CPU, memory, file size) and a wall-clock timeout. An **optional** OS jail (`sandbox-exec` on macOS, `bwrap`/`firejail` on Linux) is available via `SandboxConfig.confine_os`.

> ⚠️ At the local tier, isolation of *arbitrary executed code* is best-effort. Airtight isolation arrives when the sandbox tier moves to a container/micro-VM — and because everything sits behind the `SandboxExecutor` interface, that swap changes zero harness code.

## Configuration

`HarnessConfig` (see `harness/config.py`) is a plain dataclass:

| Field | Default | Notes |
|---|---|---|
| `model` | `"gpt-5-mini"` | |
| `spill_threshold_bytes` | `8192` | Lower edge of the spill-over zone: a tool return over this becomes a handle |
| `max_spill_bytes` | `100 MiB` | Upper edge: a return larger than this is **rejected loudly** (`SpillLimitExceeded`), never silently stored |
| `max_context_window_tokens` | `128_000` | Fed to MAF compaction |
| `max_output_tokens` | `4096` | |
| `root_dir` | `None` | `None` → a session dir under `./.harness/sessions/` |
| `sandbox` | `SandboxConfig()` | timeout, rlimits, `confine_os`, preinstalled libs |
| `fetch` | `FetchConfig()` | `max_bytes`, timeout, allowed URL schemes |
| `search` | `SearchConfig()` | Tavily provider, key, `max_results` |

## Development

```bash
uv run pytest          # full suite — no network or API keys required
uv run ruff check .
```

Testing is a first-class principle: **tests are written before implementation for every unit** (TDD). The fast suite runs deterministically against a stub chat client (no API cost); live tests are gated behind `HARNESS_LIVE=1`. `safe_path` and the spill logic are held to the highest bar.

## Project layout

```
harness/
  config.py      HarnessConfig / SandboxConfig / FetchConfig / SearchConfig
  paths.py       safe_path() — the single security chokepoint
  handles.py     Handle + HandleStore (json / text / dataframe persistence)
  sandbox.py     SandboxExecutor protocol + LocalSubprocessSandbox
  session.py     Session — root dir + store + sandbox for one run
  spill.py       large/structured tool & MCP returns → handles (MAF result_parser)
  bundles.py     capability bundles (code / files / web) + their instructions
  tools/         the agent's tools (files, search, fetch, code, inspect, web)
  runtime/       in-sandbox helpers (load/save/emit)
  api.py         Harness / solve() / Result
  cli.py         thin streaming CLI
docs/superpowers/  design specs + phased implementation plans
evals/             eval harness
tests/             mirror of the package (unit + integration + security tests)
```

## Status & roadmap

Implemented: foundation (handles, sandbox, path-jail), the agent loop + tool surface, the `Harness`/`solve()` API + CLI, web research (Tavily search/extract, Markdown fetch), and **document ingestion** (`read_document` via Docling — PDF/spreadsheet → Markdown with tables).

Planned (documented under `docs/superpowers/`):

- **Container / micro-VM sandbox tier** — airtight code isolation behind the existing `SandboxExecutor` interface.
- **MAF skills + memory providers** (v1.1) and a MAF Workflow durability / HITL outer shell.

Out of scope for now: headless-browser/JS rendering, a full search-provider abstraction, and alternative document backends.
