# Session Lifecycle — Design

- **Date:** 2026-06-11
- **Status:** Approved design (brainstormed).
- **Motivation:** The harness must serve both **one-shot** and **continuous (multi-turn)**
  interactions, from any frontend (AG-UI host, a looping terminal app, another agent). Today a
  `Session` is welded to a single call — `solve`/`agui_stream` each do
  `Session.create(...)` → run → `aclose()` — so a continuous conversation cannot keep its
  workspace (handles, sandbox files) across turns, and one-shots leave their dirs behind. This
  feature decouples session lifecycle from a single call and adds explicit one-shot (ephemeral,
  self-cleaning) vs continuous (held until closed or expired) modes.

## Facts the design must satisfy

- One-shot and continuous interactions, both first-class.
- Frontend-agnostic: AG-UI, CLI/terminal loop, and other agents drive the same primitives.
- One-shots clean up after themselves.
- Continuous persists until told (`close()`) or expired (optional TTL).

## How MAF already does multi-turn (we align, not invent)

MAF's native multi-turn model: **one agent + one reused `AgentSession`**. `Agent.run(messages,
*, session: AgentSession)` accumulates the conversation across turns; the agent's
`HistoryProvider` owns where history lives (in-memory by default, or service-side when the chat
client stores it). `AgentSession` (`{session_id, service_session_id}`) is `to_dict`/`from_dict`
serializable. So **MAF owns conversation history** — the harness must NOT reimplement it. The
harness owns only the *workspace* half (root dir + `HandleStore`), which MAF knows nothing about.

## The unifying model

One-shot and continuous are the **same machinery**, differing only in lifetime + cleanup:

- A **`Conversation`** bundles a workspace `Session` + the agent (built once) + one MAF
  `AgentSession`. A turn is `agent.run(question, session=agent_session)`.
- **One-shot** = an *ephemeral* `Conversation`: open → one `ask` → reap the workspace.
- **Continuous** = a *registered* `Conversation`: open/resume by id, kept until `close()` or TTL.

## Architecture

```
Harness
  ├─ SessionManager            # in-process registry of live Conversations by id (+ lazy TTL)
  │     └─ ConversationStore   # interface; in-memory impl now, disk-backed later (deferred)
  └─ solve()/open()/agui_stream()  # all go through the manager

Conversation (one per conversation id)
  ├─ Session        # workspace: root, HandleStore, sandbox, StatusBus, MCP  (existing class)
  ├─ agent          # built once (Session.create_agent)
  ├─ AgentSession   # MAF conversation thread (history lives here / in the HistoryProvider)
  └─ ask(question) -> Result   # single-flight; agent.run(question, session=agent_session)
```

### Concepts (each focused)
- **`Session`** (existing, role unchanged) — the workspace half.
- **`Conversation`** (new, thin) — stateful multi-turn handle; owns a `Session` + agent + MAF
  `AgentSession`; `ask`/`aclose`; single-flight per conversation.
- **`SessionManager`** (new) — registry: `open(id=None)`, `get(id)`, `close(id)`, `sweep()`;
  lazy TTL; behind a `ConversationStore` interface (in-memory impl; disk-backed deferred).

## Components / files

| Unit | Create/Modify | Responsibility |
|---|---|---|
| `harness/conversation.py` | **Create** | `Conversation`: holds Session + agent + AgentSession; `aask`/`aclose` (async-first; sync deferred); single-flight lock; reaps workspace on close |
| `harness/manager.py` | **Create** | `SessionManager` + `ConversationStore` (in-memory): open-or-create, get, close, lazy-TTL `sweep` |
| `harness/handles.py` | Modify | Persist `handles/_manifest.json` on `put`/`register`; rehydrate on init (resume manifest + id counter) |
| `harness/api.py` | Modify | `Harness.open()`/`aopen()`; `solve(keep=False)` ephemeral default; `agui_stream` resolves `threadId` via the manager |
| `harness/config.py` | Modify | `HarnessConfig.idle_ttl_s: float | None = None` (continuous-session TTL; `None` = never). Ephemerality is the `solve(keep=...)` param, not a config knob. |

## Handle persistence + rehydration

A `Conversation` reuses **one** `Session`/`HandleStore` for all its turns, so within a process
turn 2 sees turn 1's handles automatically. We still make the store **persist + rehydrate** so
the model is correct and the future disk-resumable store has its seam:

- `HandleStore` writes `handles/_manifest.json` (`{id: summary}`, from `manifest()`) after every
  `put`/`register`.
- On `__init__`, if `_manifest.json` exists, load each record via the existing `register()` (which
  rebuilds the `Handle` and advances the id counter) — so ids continue (`h4`, not a re-used `h1`)
  and `load(id)`/`inspect_handle` see prior handles. Backing files (`handles/h1.json`…) already
  persist; this restores the in-memory manifest.

## API surface (frontend-agnostic)

```python
# One-shot (ephemeral; self-cleaning by default)
result = harness.solve("question")              # open ephemeral -> ask -> reap workspace
result = harness.solve("question", keep=True)    # opt out: retain the audit trail

# Continuous (held until closed / expired) — async (a persistent agent + MCP need one stable loop)
conv = await harness.aopen(session_id=None)      # generates an id, or pass your own (e.g. threadId)
r1 = await conv.aask("load sales.csv and summarize")
r2 = await conv.aask("now filter to EU")         # sees r1's handles + conversation history
await conv.aclose()                              # reap workspace; drop from registry

# AG-UI: threadId -> registry Conversation (callers unchanged)
async for ev in harness.agui_stream(input_data): ...
```

`solve()`/`asolve()` (one-shot) stay sync+async; `solve` becomes a thin wrapper:
`open(ephemeral)` → `ask` → `close` (reap unless `keep=True`). **Continuous is async-first in v1**
(`aopen`/`aask`/`aclose`): the AG-UI host and an async terminal loop both have a single stable
event loop, which the persistent agent + MCP connections require. A **sync** `conv.ask()`
convenience (which needs a per-conversation background loop) is deferred — see Out of scope.

## Lifecycle & cleanup

- **Ephemeral (one-shot):** workspace reaped on completion. The `Result` (final_text + handle
  **summaries** + error) is returned *before* reaping; afterward `session_dir`/`files` no longer
  exist (use `keep=True` to retain). **This flips today's `solve` default to self-cleaning.**
- **Continuous:** persists until `conv.close()` **or** TTL. **TTL is opt-in**
  (`SessionConfig.idle_ttl_s`, default `None` = never expire). Each `ask` stamps last-activity;
  expiry is **lazy** — an `open`/`get` whose target is past TTL reaps it and treats it as absent —
  plus an optional `SessionManager.sweep()` the host may call on its own timer. No mandatory
  background thread (works for both a server and a terminal loop).
- **Manager close-all:** `SessionManager.aclose()` closes every live `Conversation` (best-effort);
  a host calls it on shutdown.

## Concurrency

Each `Conversation` holds a **single-flight async lock**: a concurrent `ask` on the same
conversation **serializes** (waits its turn) — required because the sandbox is sequential per
root and turns are inherently serial. Different conversations are independent. `SessionManager`
mutations (open/close) are guarded so concurrent `open(id)` for the same id returns the same
`Conversation` (open-or-create is atomic).

## Where conversation history lives (per frontend)

The **workspace** (the `Session` + `HandleStore`, keyed by id in the `SessionManager`) is the one
thing persisted across turns for *every* frontend. Conversation *history*, however, is owned by
MAF in both paths — just reached differently:

- **Direct `ask()` path** (terminal loop, another agent): the `Conversation` holds a MAF
  `AgentSession`, and each turn is `agent.run(question, session=agent_session)` — MAF accumulates
  history server-side. The caller sends only the new turn.
- **AG-UI path** (`agui_stream`): the official `AgentFrameworkAgent` already owns history from the
  request — CopilotKit replays the full `input_data["messages"]` each turn, which MAF threads. We
  do **not** layer a second history source on top; we simply give that path the **persistent
  workspace** for the thread.

This avoids double-counting history: each path has exactly one history owner, and both share the
persistent workspace.

## AG-UI mapping

`agui_stream` stops creating a throwaway `Session` per call. It resolves `input_data["threadId"]`
→ `SessionManager.open(threadId)` to obtain the **persistent workspace `Session`** for that thread,
builds/uses the agent over it, runs the turn through `AgentFrameworkAgent` (which owns history via
the replayed messages), and yields events. So handles and sandbox files persist across turns while
history stays with the official wrapper. The thread's workspace is reaped only by an explicit
lifecycle call or TTL — never at the end of a request.

## Error handling

- A failed turn keeps the conversation alive (the existing non-fatal `Result.error` contract); it
  does not close or corrupt the session.
- `close()`/reap is idempotent and best-effort (mirrors today's `aclose`): MCP teardown, then
  workspace removal; exceptions are swallowed so cleanup always completes.
- `open(id)` of an existing live id returns the existing `Conversation` (does not rebuild).

## Testing (offline — no model/network/container)

- **Handle rehydration:** `put` handles → drop the store → new `HandleStore(same root)` restores
  the manifest and the id counter (next id continues; `load`/`inspect_handle` see prior handles).
- **Conversation multi-turn:** `StubChatClient`-driven turns — turn 2 can `load` a handle saved in
  turn 1; conversation history threads via the reused MAF `AgentSession`.
- **One-shot ephemeral:** `solve()` reaps its session dir; `solve(keep=True)` retains it.
- **Continuous:** `open` → `ask` → dir persists; `close` reaps it and removes from the registry.
- **Lazy TTL:** with `idle_ttl_s` set, an `open`/`get` past the TTL reaps and re-creates; `sweep()`
  reaps idle conversations.
- **Single-flight:** two overlapping `aask` calls on one conversation serialize (observable
  ordering), and distinct conversations run independently.
- **SessionManager:** `open(id)` twice returns the same object; `close` is idempotent.
- **AG-UI:** two `agui_stream` calls with the same `threadId` share a workspace (handle from turn 1
  visible in turn 2) — via `StubChatClient` where possible, else gated.

## Scope / phasing

Cohesive but sizable; the plan phases it: (1) handle manifest persistence + rehydration;
(2) `Conversation` + single-flight; (3) `SessionManager` + lazy TTL; (4) `solve` ephemeral default
+ `Harness.open`; (5) `agui_stream` via the manager; (6) docs.

## Out of scope (deferred)
- **Sync continuous (`conv.ask()`/`harness.open()`):** needs a per-conversation background event
  loop so the persistent agent + MCP stay on one loop across sync calls. Both v1 continuous
  frontends are async, so this is a fast-follow, not v1. (One-shot `solve()` stays sync.)
- Disk-backed / distributed `ConversationStore` and cross-process resume (the pluggable option;
  the manifest persistence + store interface are the seams it will use).
- A mandatory background sweeper thread (hosts call `sweep()` on their own cadence).
- Returning live file contents from an ephemeral one-shot (use `keep=True` for the audit trail).
