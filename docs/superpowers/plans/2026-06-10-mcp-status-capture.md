# MCP Status Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture MCP-server logging (`notifications/message`) and progress (`notifications/progress`) and route them into the harness's existing `StatusBus`, so MCP-tool status reaches the same `on_status` / `--verbose` sinks as built-in tools.

**Architecture:** A new `harness/mcp_status.py` provides pure wiring helpers — chaining wrappers around MAF's `MCPTool.logging_callback` / `message_handler` that emit `StatusEvent`s, plus a `progressToken` injector into MAF's `_tool_call_meta_by_name` (servers only emit progress when a token is on the request, and MAF sets none). `Session._attach_mcp` installs the wrappers before `connect()` and injects tokens after. Every MAF-internal seam is feature-detected; absence degrades to "no MCP status," never an error. (Spec: `docs/superpowers/specs/2026-06-10-mcp-status-capture-design.md`; mechanism proven by a spike on MAF 1.8.1.)

**Tech Stack:** Python 3.12, Microsoft Agent Framework 1.8.1, the `mcp` SDK (incl. FastMCP for the test fixture), pytest. All tests run offline (a local stdio subprocess MCP server; no network/API).

---

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `harness/mcp_status.py` | **Create** | `install_status_wrappers(bus, tool, server)`, `inject_progress_tokens(tool, server, token_map)`, the wrapper builders, `_is_progress` — decoupled from `Session`, depends only on `harness.status` |
| `harness/session.py` | Modify | `_attach_mcp`: install wrappers before connect, inject tokens after |
| `tests/test_mcp_status.py` | **Create** | Unit tests (fakes) + the `_attach_mcp` integration test |
| `tests/fixtures/mcp_progress_server.py` | **Create** | A tiny FastMCP server emitting logging + progress |
| `tests/test_mcp_status_live.py` | **Create** | End-to-end gate test through a real `MCPStdioTool` |

Key types from `harness/status.py` (Phase 1, already present): `StatusEvent(tool, message, current=None, total=None, seq, timestamp)` and `StatusBus` with `.subscribe(cb)` / `.emit(event)`.

---

## Task 1: `mcp_status.py` — translation + wiring helpers

**Files:**
- Create: `harness/mcp_status.py`
- Test: `tests/test_mcp_status.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_status.py`:

```python
import asyncio

from harness.mcp_status import (
    inject_progress_tokens,
    install_status_wrappers,
)
from harness.status import StatusBus


# --- fakes mimicking MAF's MCPTool surface + mcp notification objects --------

class _Fn:
    def __init__(self, name):
        self.name = name


class _FakeTool:
    def __init__(self, with_seams=True):
        self.name = "srv"
        self.functions = [_Fn("slow")]
        self.log_seen = []
        self.msg_seen = []
        if with_seams:
            self._tool_call_meta_by_name = {}

    async def logging_callback(self, params):
        self.log_seen.append(params)

    async def message_handler(self, message):
        self.msg_seen.append(message)


class _BareTool:
    """No logging_callback / message_handler / _tool_call_meta_by_name."""
    def __init__(self):
        self.name = "bare"
        self.functions = [_Fn("t")]


class _LogParams:
    def __init__(self, data, level="info"):
        self.data = data
        self.level = level


class _ProgressParams:
    def __init__(self, token, progress, total, message):
        self.progressToken = token
        self.progress = progress
        self.total = total
        self.message = message


class _Root:
    def __init__(self, method, params=None):
        self.method = method
        self.params = params


class _Message:
    def __init__(self, root):
        self.root = root


def _bus():
    bus = StatusBus()
    events = []
    bus.subscribe(events.append)
    return bus, events


def test_logging_wrapper_emits_and_chains():
    bus, events = _bus()
    tool = _FakeTool()
    install_status_wrappers(bus, tool, "srv")
    asyncio.run(tool.logging_callback(_LogParams("hello there")))
    assert len(events) == 1
    assert events[0].tool == "mcp:srv"
    assert events[0].message == "hello there"
    assert len(tool.log_seen) == 1                     # original still called


def test_progress_wrapper_emits_with_tool_name_and_chains():
    bus, events = _bus()
    tool = _FakeTool()
    token_map = install_status_wrappers(bus, tool, "srv")
    inject_progress_tokens(tool, "srv", token_map)     # populates token -> "slow"
    token = tool._tool_call_meta_by_name["slow"]["progressToken"]
    msg = _Message(_Root("notifications/progress", _ProgressParams(token, 2, 4, "step 2")))
    asyncio.run(tool.message_handler(msg))
    assert len(events) == 1
    e = events[0]
    assert (e.tool, e.message, e.current, e.total) == ("slow", "step 2", 2, 4)
    assert len(tool.msg_seen) == 1                      # original still called


def test_progress_unknown_token_falls_back_to_server():
    bus, events = _bus()
    tool = _FakeTool()
    token_map = install_status_wrappers(bus, tool, "srv")
    msg = _Message(_Root("notifications/progress", _ProgressParams("nope", 1, 2, "x")))
    asyncio.run(tool.message_handler(msg))
    assert events[0].tool == "mcp:srv"


def test_non_progress_message_does_not_emit_but_chains():
    bus, events = _bus()
    tool = _FakeTool()
    install_status_wrappers(bus, tool, "srv")
    msg = _Message(_Root("notifications/tools/list_changed"))
    asyncio.run(tool.message_handler(msg))
    assert events == []
    assert len(tool.msg_seen) == 1                      # original still called


def test_inject_sets_token_in_meta_and_map():
    tool = _FakeTool()
    token_map = {}
    inject_progress_tokens(tool, "srv", token_map)
    assert tool._tool_call_meta_by_name["slow"]["progressToken"] == "harness:srv:slow"
    assert token_map == {"harness:srv:slow": "slow"}


def test_inject_preserves_existing_meta():
    tool = _FakeTool()
    tool._tool_call_meta_by_name["slow"] = {"keep": 1}
    inject_progress_tokens(tool, "srv", {})
    assert tool._tool_call_meta_by_name["slow"]["keep"] == 1
    assert "progressToken" in tool._tool_call_meta_by_name["slow"]


def test_graceful_degradation_when_seams_absent():
    bus, events = _bus()
    tool = _BareTool()
    token_map = install_status_wrappers(bus, tool, "bare")   # must not raise
    inject_progress_tokens(tool, "bare", token_map)          # must not raise
    assert token_map == {}
    assert not hasattr(tool, "_tool_call_meta_by_name")
    assert not events
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_status.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.mcp_status'`.

- [ ] **Step 3: Write the implementation**

Create `harness/mcp_status.py`:

```python
"""Translate MCP server notifications (logging + progress) into harness StatusEvents.

MAF surfaces neither MCP logging nor MCP progress to the harness, so we hook the (private)
seams its ``MCPTool`` passes to the underlying mcp ``ClientSession``: ``logging_callback`` and
``message_handler``. We wrap them, chaining to the originals to preserve MAF's own behavior
(notably the ``notifications/tools/list_changed`` reload handled inside ``message_handler``).

Servers only emit progress when a ``progressToken`` is on the request, and MAF sets none, so
``inject_progress_tokens`` writes a stable per-tool token into MAF's ``_tool_call_meta_by_name``;
the matching token->tool-name map lets the progress wrapper attribute each event to the right
tool. Every seam is feature-detected -- a future MAF that moves a seam degrades to "no MCP
status", never an error. Emitting is best-effort: a translation error never breaks the wrapped
handler or the tool call.
"""

from __future__ import annotations

from typing import Any, Callable

from .status import StatusBus, StatusEvent


def _is_progress(root: Any) -> bool:
    """Duck-typed check for an mcp ProgressNotification (avoids importing mcp types here)."""
    return getattr(root, "method", None) == "notifications/progress" and hasattr(root, "params")


def _wrap_logging(original: Callable, bus: StatusBus, server: str) -> Callable:
    async def logging_callback(params: Any) -> Any:
        try:
            bus.emit(StatusEvent(tool=f"mcp:{server}", message=str(getattr(params, "data", ""))))
        except Exception:  # noqa: BLE001 - status is best-effort; never break the handler
            pass
        return await original(params)

    return logging_callback


def _wrap_message(original: Callable, bus: StatusBus, server: str,
                  token_map: dict[Any, str]) -> Callable:
    async def message_handler(message: Any) -> Any:
        try:
            root = getattr(message, "root", None)
            if _is_progress(root):
                p = root.params
                tool = token_map.get(p.progressToken, f"mcp:{server}")
                bus.emit(StatusEvent(tool=tool, message=p.message or "",
                                     current=p.progress, total=p.total))
        except Exception:  # noqa: BLE001 - best-effort; never break the handler
            pass
        return await original(message)

    return message_handler


def install_status_wrappers(bus: StatusBus, tool: Any, server: str) -> dict[Any, str]:
    """Wrap the tool's logging/message handlers (before connect) to emit StatusEvents.

    Returns a (mutable) token->tool-name map shared with the message wrapper; it is empty
    until ``inject_progress_tokens`` populates it after connect. Missing seams are skipped.
    """
    token_map: dict[Any, str] = {}
    orig_logging = getattr(tool, "logging_callback", None)
    if callable(orig_logging):
        tool.logging_callback = _wrap_logging(orig_logging, bus, server)
    orig_message = getattr(tool, "message_handler", None)
    if callable(orig_message):
        tool.message_handler = _wrap_message(orig_message, bus, server, token_map)
    return token_map


def inject_progress_tokens(tool: Any, server: str, token_map: dict[Any, str]) -> None:
    """After connect: give each tool a stable progressToken so the server emits progress.

    Writes ``progressToken`` into MAF's per-tool ``_tool_call_meta_by_name`` (preserving any
    existing meta) and records token->name in ``token_map``. No-op if the seam is absent.
    """
    meta_by_name = getattr(tool, "_tool_call_meta_by_name", None)
    if meta_by_name is None:
        return
    for fn in getattr(tool, "functions", []):
        name = getattr(fn, "name", None)
        if not name:
            continue
        token = f"harness:{server}:{name}"
        merged = dict(meta_by_name.get(name) or {})
        merged["progressToken"] = token
        meta_by_name[name] = merged
        token_map[token] = name
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_status.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check harness/mcp_status.py tests/test_mcp_status.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add harness/mcp_status.py tests/test_mcp_status.py
git commit -m "feat(mcp-status): translate MCP logging+progress notifications to StatusEvents"
```

---

## Task 2: Wire status capture into `Session._attach_mcp`

**Files:**
- Modify: `harness/session.py`
- Test: `tests/test_mcp_status.py`

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_mcp_status.py`:

```python
from agent_framework import FunctionTool

from harness import HarnessConfig, Session
from harness.testing import StubChatClient, text


class _SeamMCPTool:
    """A fake MCPTool that exposes the MAF seams, so _attach_mcp wires status into it."""

    def __init__(self):
        self.name = "fakemcp"
        self.functions: list = []
        self.closed = False
        self._tool_call_meta_by_name: dict = {}
        self.orig_logs: list = []

    async def logging_callback(self, params):
        self.orig_logs.append(params)

    async def message_handler(self, message):
        pass

    async def connect(self):
        def slow(n: int) -> str:
            """A slow MCP tool."""
            return "ok"
        self.functions = [FunctionTool(func=slow, name="slow", description="slow")]

    async def close(self):
        self.closed = True


def test_attach_mcp_installs_wrappers_and_injects_token(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    mcp = _SeamMCPTool()

    async def run():
        events = []
        async with Session.create(cfg) as sess:
            sess.subscribe(events.append)
            await sess.create_agent(
                StubChatClient([text("x")]), agent_instructions="x",
                tools=[mcp], bundles=("code",),
            )
            # token injected for the connected tool
            assert mcp._tool_call_meta_by_name["slow"]["progressToken"] == "harness:fakemcp:slow"
            # the wrapped logging handler now emits to the bus AND chains the original
            await mcp.logging_callback(type("P", (), {"data": "hi", "level": "info"})())
            return events, mcp

    events, mcp = asyncio.run(run())
    assert any(e.tool == "mcp:fakemcp" and e.message == "hi" for e in events)
    assert mcp.orig_logs                              # original logging_callback still called
    assert mcp.closed
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mcp_status.py -k attach_mcp -q`
Expected: FAIL — no token injected; the wrapped handler doesn't emit (capture not wired yet).

- [ ] **Step 3: Implement the wiring in `harness/session.py`**

Replace the body of `_attach_mcp` (currently lines ~121-134) with this version, which adds the
two wiring calls around the existing `connect()` and keeps everything else identical:

```python
    async def _attach_mcp(self, tool: Any) -> list:
        """Connect an MCP server, attach the spill parser, capture its status, own its lifecycle."""
        from .mcp_status import inject_progress_tokens, install_status_wrappers
        from .spill import make_spill_parser

        server = getattr(tool, "name", None) or repr(tool)
        # Install before connect so the wrappers reach the underlying MCP ClientSession.
        token_map = install_status_wrappers(self.status_bus, tool, server)
        try:
            await tool.connect()
        except Exception as e:  # noqa: BLE001 - add context naming the server, then re-raise
            raise RuntimeError(f"failed to connect MCP server {tool!r}: {e}") from e
        # Register before the parser loop so aclose() still closes this server if the loop raises.
        self._mcp_connected.append(tool)
        inject_progress_tokens(tool, server, token_map)   # after connect: tools are now loaded
        functions = list(tool.functions)
        for ft in functions:
            ft.result_parser = make_spill_parser(self, ft.name)
        return functions
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_mcp_status.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Full suite + lint (no regressions — existing MCP-wiring tests use a seam-less fake and must still pass via graceful degradation)**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/session.py tests/test_mcp_status.py`
Expected: all pass; `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add harness/session.py tests/test_mcp_status.py
git commit -m "feat(mcp-status): wire MCP status capture into Session._attach_mcp"
```

---

## Task 3: End-to-end gate test against a real MCP server

**Files:**
- Create: `tests/fixtures/mcp_progress_server.py`
- Create: `tests/test_mcp_status_live.py`

- [ ] **Step 1: Create the FastMCP fixture server**

Create `tests/fixtures/mcp_progress_server.py` (run as a stdio subprocess, NOT collected by
pytest — it has no `test_` prefix and lives in `fixtures/`):

```python
"""A tiny FastMCP server for the MCP-status gate test: a tool that logs + reports progress."""
import anyio
from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("statusfixture")


@mcp.tool()
async def slow(n: int, ctx: Context) -> str:
    """Process n items, logging and reporting progress as it goes."""
    for i in range(n):
        await ctx.info(f"processing item {i + 1}/{n}")
        await ctx.report_progress(i + 1, n, f"step {i + 1}")
        await anyio.sleep(0.02)
    return f"done: {n} items"


if __name__ == "__main__":
    mcp.run()
```

- [ ] **Step 2: Write the end-to-end gate test**

Create `tests/test_mcp_status_live.py`:

```python
import sys
from pathlib import Path

from agent_framework import MCPStdioTool

from harness import Harness, HarnessConfig
from harness.testing import StubChatClient, text, tool_call

_FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp_progress_server.py")


def test_mcp_logging_and_progress_reach_on_status(tmp_path):
    events = []
    mcp = MCPStdioTool(name="statusfix", command=sys.executable, args=[_FIXTURE])
    client = StubChatClient([tool_call("slow", {"n": 3}), text("done")])
    h = Harness(HarnessConfig(root_dir=tmp_path / "r"), client=client,
                tools=[mcp], on_status=events.append)
    result = h.solve("go")

    assert result.final_text == "done"
    # logging notifications -> attributed to the server
    logs = [e for e in events if e.tool == "mcp:statusfix"]
    assert any("processing item" in e.message for e in logs)
    # progress notifications -> attributed to the emitting tool, with current/total
    progress = [e for e in events if e.tool == "slow" and e.current is not None]
    assert progress, f"no progress events captured; got {[(e.tool, e.message) for e in events]}"
    assert progress[-1].current == 3 and progress[-1].total == 3
```

- [ ] **Step 3: Run the gate test**

Run: `.venv/bin/python -m pytest tests/test_mcp_status_live.py -q`
Expected: PASS (1 passed). This spawns the fixture as a stdio subprocess — fully offline, no
API key. If it fails with zero progress events, MAF moved a seam (the canary did its job).

- [ ] **Step 4: Full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check tests/test_mcp_status_live.py tests/fixtures/mcp_progress_server.py`
Expected: all pass; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/mcp_progress_server.py tests/test_mcp_status_live.py
git commit -m "test(mcp-status): end-to-end gate — real MCP server logging+progress reach on_status"
```

---

## Self-Review (completed during plan authoring)

**Spec coverage** (`2026-06-10-mcp-status-capture-design.md`):
- Logging capture via wrapped `logging_callback` → `mcp:<server>` event — Task 1 (`_wrap_logging`), Task 3 (live). ✓
- Progress capture via wrapped `message_handler` catching `ProgressNotification` — Task 1 (`_wrap_message`/`_is_progress`), Task 3. ✓
- `progressToken` injection into `_tool_call_meta_by_name` + token→tool map for attribution — Task 1 (`inject_progress_tokens`), Task 2 (wired post-connect). ✓
- Wrappers chain to originals (preserve MAF reload) — Task 1 tests assert `*_seen`/`orig_logs`. ✓
- Wiring in `_attach_mcp`, install before connect / inject after — Task 2. ✓
- Emit directly to `session.status_bus` (no contextvar reliance) — `install_status_wrappers(self.status_bus, ...)` in Task 2. ✓
- Attribution: progress→tool name (token map), logging→`mcp:<server>` — Task 1 tests + Task 3. ✓
- Robustness: feature-detect every seam, degrade silently — Task 1 `test_graceful_degradation_when_seams_absent`, and existing seam-less `FakeMCPTool` tests still green (Task 2 Step 5). ✓
- Best-effort: translation errors swallowed, original always called — `try/except` in both wrappers. ✓
- Always-on for any MCP server — `_attach_mcp` runs unconditionally. ✓
- Offline testing via committed FastMCP fixture — Task 3. ✓

**Placeholder scan:** none — every step has concrete code/commands.

**Type/name consistency:** `install_status_wrappers(bus, tool, server) -> token_map`, `inject_progress_tokens(tool, server, token_map)`, `_is_progress`, token format `f"harness:{server}:{name}"`, event tool labels `f"mcp:{server}"`, and `StatusEvent(tool, message, current, total)` are used identically across Tasks 1–3.

**Deferred (per spec, not in this plan):** per-call progress-token correlation; the AG-UI adapter sink; capturing other MCP notification types.
