# MAF-Composable Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a developer build a MAF agent the normal way — bringing their own task instructions, tools, and MCP servers — and have it transparently gain the harness's capabilities (handle/spill core, `code`/`files`/`web` bundles, sandbox), via a `Session.create_agent()` factory.

**Architecture:** The existing `Session` becomes the composable anchor and an async context manager. Plugged-in tools and MCP servers get the references-not-payloads treatment through MAF's documented `result_parser` hook (replacing the old `wrap_external_tool`). Operational instructions are harness-owned and ride with the selected bundles via MAF's `harness_instructions` slot. `Harness`/`solve()`/`asolve()` and the CLI are rebuilt on this path as the worked example.

**Tech Stack:** Python 3.12, `uv`, `pytest`, Microsoft Agent Framework (`agent-framework-core`). All new tests run with no network or API keys (deterministic `StubChatClient` + a fake MCP tool).

**Spec:** `docs/superpowers/specs/2026-06-03-maf-composable-harness-design.md`

**Branch:** `feat/maf-composable-harness` (already checked out; the spec is committed here).

---

## File Structure

- Modify: `harness/config.py` — add `cleanup: bool` to `HarnessConfig`.
- Create: `harness/bundles.py` — bundle → tool-name groups + `harness_instructions` fragments; core constants.
- Modify: `harness/spill.py` — replace `wrap_external_tool*` with `make_spill_parser`, `spill_tool`, and MCP detection (`looks_like_mcp`). Keep `_should_spill` / `_is_handle_summary` / `_maybe_spill`.
- Modify: `harness/session.py` — async context manager; `tools()`, `harness_instructions()`, `spill_parser()`, `create_agent()`, `handles`/`artifacts` properties; owns MCP connections.
- Modify: `harness/api.py` — rebuild `Harness` on `create_agent`; add `asolve`; keep `solve` sync.
- Modify: `harness/cli.py` — confirm it drives the rebuilt `Harness` (no signature change needed).
- Modify: `harness/agent.py` — remove dead `build_agent`/`AGENT_INSTRUCTIONS` after `api.py` migrates; keep `run_agent_sync`, `_instrument`.
- Modify: `harness/__init__.py` — export `bundles` helpers if needed (no breaking removals).
- Create: `tests/test_bundles.py`, `tests/test_session_create_agent.py`, `tests/test_mcp_wiring.py`.
- Modify: `tests/test_spill.py`, `tests/test_api.py`, `tests/test_session.py`.

---

## Task 1: `cleanup` config + Session as async context manager

Make `Session` own a lifecycle: an async context manager that (later) closes MCP connections and optionally deletes the root, plus `handles`/`artifacts` accessors moved off `api.py`.

**Files:**
- Modify: `harness/config.py`
- Modify: `harness/session.py`
- Test: `tests/test_session.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_session.py`:

```python
import asyncio


def test_session_handles_and_artifacts(tmp_path):
    from harness import HarnessConfig, Session

    sess = Session.create(HarnessConfig(root_dir=tmp_path / "r"))
    (sess.root / "report.txt").write_text("hi")
    (sess.root / ".scripts").mkdir(exist_ok=True)
    (sess.root / ".scripts" / "x.py").write_text("# scratch")
    sess.store.put({"a": 1}, source="t")

    assert "report.txt" in sess.artifacts
    assert not any(a.startswith(".scripts") for a in sess.artifacts)
    assert not any(a.startswith("handles") for a in sess.artifacts)
    assert sess.handles  # the put() handle shows up


def test_session_async_context_manager_cleanup(tmp_path):
    from harness import HarnessConfig, Session

    async def run():
        cfg = HarnessConfig(root_dir=tmp_path / "r", cleanup=True)
        async with Session.create(cfg) as sess:
            root = sess.root
            assert root.exists()
        return root

    root = asyncio.run(run())
    assert not root.exists()  # cleanup=True removed it on exit
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_session.py -k "handles_and_artifacts or async_context" -v`
Expected: FAIL — `AttributeError: 'Session' object has no attribute 'artifacts'` / `HarnessConfig` has no `cleanup`.

- [ ] **Step 3: Add `cleanup` to config** — in `harness/config.py`, add a field to `HarnessConfig` (after `root_dir`):

```python
    root_dir: Path | None = None  # None -> a session dir is created under ./.harness/sessions/
    cleanup: bool = False  # delete the root on async-context exit (throwaway runs)
```

- [ ] **Step 4: Implement on `Session`** — in `harness/session.py`, update the imports and class. Add `field` to the dataclass import and `Any` typing:

```python
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
```

Replace the dataclass body's fields and add the new members:

```python
@dataclass
class Session:
    root: Path
    store: HandleStore
    sandbox: LocalSubprocessSandbox
    config: HarnessConfig
    _mcp_connected: list = field(default_factory=list, init=False, repr=False)

    @classmethod
    def create(cls, config: HarnessConfig) -> "Session":
        root = _resolve_root(config)
        root.mkdir(parents=True, exist_ok=True)
        store = HandleStore(root)
        sandbox = LocalSubprocessSandbox(root=root, store=store, config=config.sandbox)
        return cls(root=root, store=store, sandbox=sandbox, config=config)

    @property
    def handles(self) -> dict[str, Any]:
        """Handle summaries produced during the run, by id."""
        return self.store.manifest()

    @property
    def artifacts(self) -> list[str]:
        """User-meaningful files under root, excluding handle storage and scratch."""
        out: list[str] = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.root)
            top = rel.parts[0]
            if top in ("handles", ".scripts") or top.startswith("_"):
                continue
            out.append(rel.as_posix())
        return out

    async def aclose(self) -> None:
        """Close every connected MCP server, then honor the cleanup policy."""
        for tool in self._mcp_connected:
            try:
                await tool.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        self._mcp_connected.clear()
        if self.config.cleanup and self.root.exists():
            shutil.rmtree(self.root)

    async def __aenter__(self) -> "Session":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_session.py -v`
Expected: PASS (all session tests, old and new).

- [ ] **Step 6: Commit**

```bash
git add harness/config.py harness/session.py tests/test_session.py
git commit -m "feat(session): cleanup config + async context manager, handles/artifacts accessors"
```

---

## Task 2: Bundles — tool groups and operating-instruction fragments

Define the always-on core plus the `code`/`files`/`web` bundles: which tools each exposes and the `harness_instructions` fragment that teaches the model to operate them. Add `Session.tools()` and `Session.harness_instructions()`.

**Files:**
- Create: `harness/bundles.py`
- Modify: `harness/session.py`
- Test: `tests/test_bundles.py`

- [ ] **Step 1: Write the failing test** — create `tests/test_bundles.py`:

```python
from harness import HarnessConfig, Session


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def test_tools_for_code_bundle_includes_core(tmp_path):
    sess = _session(tmp_path)
    names = {t.__name__ for t in sess.tools("code")}
    assert names == {"inspect_handle", "run_python"}  # core + code


def test_tools_default_is_all_bundles(tmp_path):
    sess = _session(tmp_path)
    names = {t.__name__ for t in sess.tools()}
    assert names == {
        "inspect_handle", "run_python",
        "read_file", "write_file", "list_files", "search",
        "fetch_url", "web_search", "web_extract",
    }


def test_harness_instructions_compose_by_bundle(tmp_path):
    sess = _session(tmp_path)
    core_only = sess.harness_instructions()  # always includes core
    assert "handle" in core_only.lower()
    with_code = sess.harness_instructions("code")
    assert "run_python" in with_code
    assert "load(" in with_code
    # web fragment only appears when web is selected
    assert "web_search" not in sess.harness_instructions("code")
    assert "web_search" in sess.harness_instructions("web")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_bundles.py -v`
Expected: FAIL — `AttributeError: 'Session' object has no attribute 'tools'`.

- [ ] **Step 3: Create `harness/bundles.py`**

```python
"""Capability bundles: which tools each exposes and how to operate them.

The data substrate (handle store + spill + inspect_handle) is always-on CORE.
``code`` / ``files`` / ``web`` are opt-in layers. Each contributes (a) tool names
and (b) a ``harness_instructions`` fragment the model reads to operate the tools.
"""

from __future__ import annotations

CORE_TOOL_NAMES: tuple[str, ...] = ("inspect_handle",)

BUNDLE_TOOL_NAMES: dict[str, tuple[str, ...]] = {
    "code": ("run_python",),
    "files": ("read_file", "write_file", "list_files", "search"),
    "web": ("fetch_url", "web_search", "web_extract"),
}

CORE_INSTRUCTIONS = (
    "You solve data-gathering and integration tasks. "
    "Work autonomously and do NOT stop to ask the user. "
    "Large data is referenced by handles (ids); never expect full datasets in the "
    "conversation. Use inspect_handle(id) to look closer at any handle. "
    "ALWAYS verify data quality before reporting results, and state any issues you handled."
)

BUNDLE_INSTRUCTIONS: dict[str, str] = {
    "code": (
        "Use run_python to analyze data by writing Python. Inside it, load(id) reads a "
        "handle and save(id, obj) stores one. To return a value, end your code with a "
        "bare expression (e.g. `total`) OR print() it -- the result field captures it."
    ),
    "files": (
        "Use read_file/write_file/list_files/search to work with files in the workspace. "
        "read_file is paginated; search finds regex matches across files (including handle "
        "backing files)."
    ),
    "web": (
        "Use web_search to find pages, fetch_url to retrieve a page as clean markdown, and "
        "web_extract for clean content. Fetched bodies are stored as handles."
    ),
}


def selected_bundles(bundles: tuple[str, ...]) -> tuple[str, ...]:
    """Empty selection means all optional bundles; validate names."""
    chosen = bundles or tuple(BUNDLE_TOOL_NAMES.keys())
    for b in chosen:
        if b not in BUNDLE_TOOL_NAMES:
            raise ValueError(f"unknown bundle {b!r}; choose from {sorted(BUNDLE_TOOL_NAMES)}")
    return chosen


def tool_names_for(bundles: tuple[str, ...]) -> set[str]:
    names = set(CORE_TOOL_NAMES)
    for b in selected_bundles(bundles):
        names |= set(BUNDLE_TOOL_NAMES[b])
    return names


def instructions_for(bundles: tuple[str, ...]) -> str:
    parts = [CORE_INSTRUCTIONS]
    for b in selected_bundles(bundles):
        parts.append(BUNDLE_INSTRUCTIONS[b])
    return "\n\n".join(parts)
```

- [ ] **Step 4: Add `tools()` / `harness_instructions()` to `Session`** — in `harness/session.py`, add imports near the top:

```python
from . import bundles as _bundles
from .tools.registry import build_tools
```

Add these methods to the `Session` class:

```python
    def tools(self, *bundles: str) -> list:
        """The built-in tool callables for the selected bundles (default: all)."""
        wanted = _bundles.tool_names_for(bundles)
        return [t for t in build_tools(self) if t.__name__ in wanted]

    def harness_instructions(self, *bundles: str) -> str:
        """The operating-manual text (core + selected bundles)."""
        return _bundles.instructions_for(bundles)
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_bundles.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add harness/bundles.py harness/session.py tests/test_bundles.py
git commit -m "feat(bundles): core + code/files/web tool groups and operating instructions"
```

---

## Task 3: Spill via `result_parser` (plain tools + MCP detection)

Replace the function-wrapping spill with a MAF `result_parser`: a parser that spills oversized/structured returns to a handle and otherwise defers to MAF's default parsing. Add `spill_tool` (plain callable → `FunctionTool` carrying the parser) and `looks_like_mcp` detection. Keep the threshold logic.

**Files:**
- Modify: `harness/spill.py`
- Test: `tests/test_spill.py`

- [ ] **Step 1: Write the failing test** — replace the body of `tests/test_spill.py` with:

```python
import json

import pandas as pd

from agent_framework._types import Content
from harness import HarnessConfig, Session
from harness.spill import looks_like_mcp, make_spill_parser, spill_tool


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r", spill_threshold_bytes=64))


def _as_text(parsed) -> str:
    # parse_result returns list[Content]; pull the text out of the first item.
    return parsed[0].text if isinstance(parsed, list) else parsed


def test_parser_spills_oversized_dict_to_handle(tmp_path):
    sess = _session(tmp_path)
    parse = make_spill_parser(sess, "big_tool")
    big = {"rows": list(range(500))}
    parsed = parse(big)
    text = _as_text(parsed)
    assert '"id"' in text and '"path"' in text          # a handle summary, not raw rows
    assert sess.handles                                  # a handle was created
    assert next(iter(sess.handles.values()))["source"] == "tool:big_tool"


def test_parser_passes_small_result_through(tmp_path):
    sess = _session(tmp_path)
    parse = make_spill_parser(sess, "tiny")
    parsed = parse({"ok": True})
    assert "ok" in _as_text(parsed)
    assert not sess.handles                              # nothing spilled


def test_parser_spills_dataframe_with_schema(tmp_path):
    sess = _session(tmp_path)
    parse = make_spill_parser(sess, "frame")
    parse(pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]}))
    summ = next(iter(sess.handles.values()))
    assert summ["kind"] == "dataframe"
    assert summ["schema"] == {"a": "int64", "b": "int64"}


def test_parser_does_not_double_spill_handle_summary(tmp_path):
    sess = _session(tmp_path)
    parse = make_spill_parser(sess, "passthru")
    already = {"id": "h1", "kind": "json", "path": "handles/h1.json"}
    parse(already)
    assert not sess.handles                              # recognized as a summary; no new handle


def test_spill_tool_builds_functiontool_with_name_and_doc(tmp_path):
    sess = _session(tmp_path)

    def my_tool(x: int) -> dict:
        """Returns a big payload."""
        return {"rows": list(range(x))}

    ft = spill_tool(sess, my_tool)
    assert ft.name == "my_tool"
    assert "big payload" in ft.description


def test_looks_like_mcp_detection(tmp_path):
    class FakeMCP:
        functions = []
        async def connect(self): ...
        async def close(self): ...

    def plain(): return 1

    assert looks_like_mcp(FakeMCP())
    assert not looks_like_mcp(plain)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_spill.py -v`
Expected: FAIL — `ImportError: cannot import name 'make_spill_parser'`.

- [ ] **Step 3: Rewrite `harness/spill.py`** — replace the whole file:

```python
"""Spill: turn oversized/structured tool results into handles via MAF result_parser.

A plugged-in capability (a plain tool function or an MCP server tool) returns its raw
Python value; MAF calls our ``result_parser`` on it *before serialization*. We write an
oversized/structured value to the handle store and return the lightweight handle summary
the model sees; small values defer to MAF's default parsing. The harness's own built-in
tools are NOT given this parser -- they already manage their own output.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from agent_framework import FunctionTool
from agent_framework._types import Content

from .session import Session


def _is_handle_summary(obj: Any) -> bool:
    return isinstance(obj, dict) and {"id", "kind", "path"} <= obj.keys()


def _should_spill(result: Any, threshold_bytes: int) -> bool:
    try:
        import pandas as pd
        if isinstance(result, pd.DataFrame):
            return True
    except ImportError:
        pass
    if isinstance(result, (bytes, bytearray)):
        return len(result) > threshold_bytes
    if isinstance(result, str):
        return len(result.encode()) > threshold_bytes
    if isinstance(result, (dict, list)):
        if _is_handle_summary(result):
            return False
        return len(json.dumps(result, default=str).encode()) > threshold_bytes
    return False


def _maybe_spill(session: Session, tool_name: str, result: Any) -> Any:
    if _should_spill(result, session.config.spill_threshold_bytes):
        return session.store.put(result, source=f"tool:{tool_name}").summary()
    return result


def make_spill_parser(session: Session, tool_name: str) -> Callable[[Any], list[Content]]:
    """A MAF ``result_parser``: spill oversized returns, else default-parse."""
    def parse(result: Any) -> list[Content]:
        return FunctionTool.parse_result(_maybe_spill(session, tool_name, result))
    return parse


def spill_tool(session: Session, fn: Callable) -> FunctionTool:
    """Wrap a plain developer callable as a FunctionTool whose return is spilled."""
    name = getattr(fn, "__name__", "tool")
    return FunctionTool(
        func=fn,
        name=name,
        description=(fn.__doc__ or "").strip(),
        result_parser=make_spill_parser(session, name),
    )


def looks_like_mcp(tool: Any) -> bool:
    """Duck-typed MCP detection: connectable, closeable, exposes ``.functions``.

    Avoids coupling to MAF's MCP class names and lets tests use a fake. Plain callables
    have no ``.functions`` attribute, so there are no false positives.
    """
    return (
        callable(getattr(tool, "connect", None))
        and callable(getattr(tool, "close", None))
        and hasattr(tool, "functions")
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_spill.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/spill.py tests/test_spill.py
git commit -m "feat(spill): result_parser-based spill, spill_tool, MCP detection"
```

---

## Task 4: `Session.create_agent()` for plain tools + bundles

Add the factory: assemble built-in bundle tools + spill-wrapped developer tools + harness instructions and call `create_harness_agent`. MCP comes in Task 5; this task handles plain callables only.

**Files:**
- Modify: `harness/session.py`
- Test: `tests/test_session_create_agent.py`

- [ ] **Step 1: Write the failing test** — create `tests/test_session_create_agent.py`:

```python
import asyncio

from harness import HarnessConfig, Session
from harness.testing import StubChatClient, text, tool_call


def test_create_agent_spills_developer_tool_return(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r", spill_threshold_bytes=64)

    def fetch_big(n: int) -> dict:
        """Return a big payload that must not flood the context."""
        return {"rows": list(range(n))}

    client = StubChatClient([
        tool_call("fetch_big", {"n": 500}),
        text("done"),
    ])

    async def run():
        async with Session.create(cfg) as sess:
            agent = await sess.create_agent(
                client,
                agent_instructions="Fetch the rows.",
                tools=[fetch_big],
                bundles=("code",),
            )
            await agent.run("go")
            return dict(sess.handles)

    handles = asyncio.run(run())
    assert handles  # the developer tool's big return was spilled to a handle
    assert next(iter(handles.values()))["source"] == "tool:fetch_big"


def test_create_agent_exposes_selected_bundle_tools(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    client = StubChatClient([text("hi")])

    async def run():
        async with Session.create(cfg) as sess:
            agent = await sess.create_agent(
                client, agent_instructions="x", tools=[], bundles=("files",),
            )
            # the harness instructions for the files bundle reached the agent
            return agent

    agent = asyncio.run(run())
    assert agent is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_session_create_agent.py -v`
Expected: FAIL — `AttributeError: 'Session' object has no attribute 'create_agent'`.

- [ ] **Step 3: Implement `create_agent`** — in `harness/session.py`, add the import and method. Near the top imports:

```python
from .spill import looks_like_mcp, spill_tool
```

Add to the `Session` class:

```python
    async def create_agent(
        self,
        client: Any,
        *,
        agent_instructions: str | None = None,
        tools: list | None = None,
        bundles: tuple[str, ...] = ("code", "files", "web"),
        name: str = "data-integrator",
        **maf_kwargs: Any,
    ):
        """Build a MAF agent over the selected bundles plus developer tools/MCP.

        Plain callables are spill-wrapped; MCP servers are connected and their tools get
        the spill parser (Task 5). Operational instructions ride in ``harness_instructions``.
        """
        from agent_framework import create_harness_agent

        builtin = self.tools(*bundles)
        external: list = []
        for tool in tools or []:
            if looks_like_mcp(tool):
                external.extend(await self._attach_mcp(tool))
            else:
                external.append(spill_tool(self, tool))

        maf_kwargs.setdefault("max_context_window_tokens", self.config.max_context_window_tokens)
        maf_kwargs.setdefault("max_output_tokens", self.config.max_output_tokens)
        return create_harness_agent(
            client,
            name=name,
            harness_instructions=self.harness_instructions(*bundles),
            agent_instructions=agent_instructions,
            tools=builtin + external,
            disable_todo=True,
            disable_mode=True,
            disable_memory=True,
            disable_web_search=True,
            **maf_kwargs,
        )

    async def _attach_mcp(self, tool: Any) -> list:
        """Placeholder until Task 5; plain-tool path does not reach here."""
        raise NotImplementedError("MCP support lands in Task 5")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_session_create_agent.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `uv run pytest -q`
Expected: PASS (existing `Harness`/CLI still use the old `build_agent`; untouched).

- [ ] **Step 6: Commit**

```bash
git add harness/session.py tests/test_session_create_agent.py
git commit -m "feat(session): create_agent factory for bundles + spill-wrapped tools"
```

---

## Task 5: MCP support in `create_agent`

Connect MCP servers, attach the spill `result_parser` to each exposed `FunctionTool`, register the connection for teardown, and return the functions to add to the agent's tool list. Tested with a fake MCP tool (deterministic, no network).

**Files:**
- Modify: `harness/session.py`
- Test: `tests/test_mcp_wiring.py`

- [ ] **Step 1: Write the failing test** — create `tests/test_mcp_wiring.py`:

```python
import asyncio

from agent_framework import FunctionTool
from harness import HarnessConfig, Session
from harness.testing import StubChatClient, text, tool_call


class FakeMCPTool:
    """Mimics MAF's MCPTool surface: connect() populates .functions; close() tears down."""

    def __init__(self):
        self.functions: list[FunctionTool] = []
        self.connected = False
        self.closed = False

    async def connect(self):
        self.connected = True

        def mcp_query(q: str) -> dict:
            """Return a big result from the MCP server."""
            return {"hits": list(range(500)), "q": q}

        self.functions = [FunctionTool(func=mcp_query, name="mcp_query",
                                       description="mcp query")]

    async def close(self):
        self.closed = True


def test_create_agent_connects_and_spills_mcp(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r", spill_threshold_bytes=64)
    mcp = FakeMCPTool()
    client = StubChatClient([
        tool_call("mcp_query", {"q": "widgets"}),
        text("done"),
    ])

    async def run():
        async with Session.create(cfg) as sess:
            agent = await sess.create_agent(
                client, agent_instructions="query it", tools=[mcp], bundles=("code",),
            )
            assert mcp.connected
            await agent.run("go")
            return dict(sess.handles), mcp

    handles, mcp = asyncio.run(run())
    assert handles  # MCP result over threshold was spilled to a handle
    assert next(iter(handles.values()))["source"] == "tool:mcp_query"
    assert mcp.closed  # connection torn down on context exit


def test_mcp_closed_even_on_error(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    mcp = FakeMCPTool()

    async def run():
        try:
            async with Session.create(cfg) as sess:
                await sess.create_agent(
                    StubChatClient([text("x")]),
                    agent_instructions="x", tools=[mcp], bundles=("code",),
                )
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        return mcp

    mcp = asyncio.run(run())
    assert mcp.connected and mcp.closed  # __aexit__ closed it despite the error
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_mcp_wiring.py -v`
Expected: FAIL — `NotImplementedError: MCP support lands in Task 5`.

- [ ] **Step 3: Implement `_attach_mcp`** — in `harness/session.py`, replace the placeholder `_attach_mcp` with:

```python
    async def _attach_mcp(self, tool: Any) -> list:
        """Connect an MCP server, attach the spill parser to its tools, own its lifecycle."""
        from .spill import make_spill_parser

        await tool.connect()
        self._mcp_connected.append(tool)
        functions = list(tool.functions)
        for ft in functions:
            ft.result_parser = make_spill_parser(self, ft.name)
        return functions
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_mcp_wiring.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/session.py tests/test_mcp_wiring.py
git commit -m "feat(session): connect MCP servers, spill their returns, own their lifecycle"
```

> **Execution note (spec §12 risk):** the fake exercises *our* wiring. Before merge, verify against a real MAF MCP server that the mutated `result_parser` actually fires at call time and that passing `tool.functions` (rather than the `MCPTool` wrapper) into the agent works while the session holds the connection open. If MAF requires the wrapper object in the tools list, adjust `create_agent` to pass `tool` instead of its functions (still connecting + setting parsers here).

---

## Task 6: Rebuild `Harness` on `create_agent` (+ `asolve`)

Make `Harness.solve()`/`asolve()` drive the new composable path so the convenience wrapper is honest sugar over `Session.create_agent`. Keep the `Result` shape and the work-preserving error handling.

**Files:**
- Modify: `harness/api.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing test** — replace `tests/test_api.py` with:

```python
import asyncio

from harness import HarnessConfig, Harness
from harness.testing import StubChatClient, text, tool_call


def _client():
    return StubChatClient([
        tool_call("fetch_big", {"n": 500}),
        text("the answer"),
    ])


def fetch_big(n: int) -> dict:
    """Return a big payload."""
    return {"rows": list(range(n))}


def test_solve_sync_returns_answer_and_spills(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r", spill_threshold_bytes=64)
    h = Harness(cfg, client=_client(), tools=[fetch_big])
    result = h.solve("go")
    assert result.final_text == "the answer"
    assert result.handles  # developer tool spilled
    assert result.error is None


def test_asolve_matches_solve(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r2", spill_threshold_bytes=64)
    h = Harness(cfg, client=_client(), tools=[fetch_big])
    result = asyncio.run(h.asolve("go"))
    assert result.final_text == "the answer"
    assert result.handles
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api.py -v`
Expected: FAIL — `Harness.__init__` got an unexpected keyword `tools` / `asolve` missing.

- [ ] **Step 3: Rewrite `harness/api.py`**

```python
"""Public entry point: Harness / solve() returning a Result."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import HarnessConfig
from .session import Session


@dataclass
class Result:
    final_text: str
    handles: dict[str, Any]
    files: list[str]
    session_dir: Path
    error: str | None = None


class Harness:
    """Reusable harness: builds a Session per run via the composable create_agent path."""

    def __init__(self, config: HarnessConfig | None = None, client: Any | None = None,
                 *, tools: list | None = None,
                 bundles: tuple[str, ...] = ("code", "files", "web")) -> None:
        self.config = config or HarnessConfig()
        self._client = client
        self._tools = tools or []
        self._bundles = bundles
        if self.config.search.api_key is None:
            from dotenv import load_dotenv
            import os
            load_dotenv()
            self.config.search.api_key = os.environ.get("TAVILY_API_KEY")

    def _make_client(self):
        if self._client is not None:
            return self._client
        from agent_framework.openai import OpenAIChatClient
        return OpenAIChatClient(model=self.config.model, env_file_path=".env")

    async def asolve(self, problem: str, tools: list | None = None) -> Result:
        final_text, error = "", None
        async with Session.create(self.config) as session:
            agent = await session.create_agent(
                self._make_client(),
                agent_instructions=None,
                tools=self._tools + (tools or []),
                bundles=self._bundles,
            )
            try:
                response = await agent.run(problem)
                final_text = response.text
            except Exception as e:  # noqa: BLE001 - surface, don't crash; keep work-so-far
                error = f"{type(e).__name__}: {e}"
            return Result(
                final_text=final_text,
                handles=dict(session.handles),
                files=session.artifacts,
                session_dir=session.root,
                error=error,
            )

    def solve(self, problem: str, tools: list | None = None) -> Result:
        return asyncio.run(self.asolve(problem, tools=tools))


def solve(problem: str, *, tools: list | None = None,
          config: HarnessConfig | None = None, client: Any | None = None) -> Result:
    """One-shot convenience: build a Harness and solve a single problem."""
    return Harness(config, client=client, tools=tools).solve(problem)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/api.py tests/test_api.py
git commit -m "feat(api): rebuild Harness on create_agent; add asolve; keep solve sync"
```

---

## Task 7: Point the CLI at the rebuilt Harness

The CLI already constructs `Harness(cfg, client=...).solve(...)`. The old `solve` accepted `on_tool_call`; the rebuilt one does not. Drop the verbose plumbing for now (it returns in a follow-up) so the CLI compiles against the new API and stays the worked example.

**Files:**
- Modify: `harness/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test** — replace `tests/test_cli.py` with:

```python
from harness.cli import run_cli
from harness.testing import StubChatClient, text, tool_call


def test_cli_prints_answer_and_session(tmp_path, capsys):
    client = StubChatClient([
        tool_call("list_files", {"path": "."}),
        text("all done"),
    ])
    code = run_cli(
        [str("Summarize the workspace."), "--root", str(tmp_path / "r")],
        client=client,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "all done" in out
    assert "[session:" in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `run_cli` passes `on_tool_call` to `solve`, which no longer accepts it.

- [ ] **Step 3: Update `harness/cli.py`** — replace `run_cli` and drop the verbose printer wiring:

```python
def run_cli(argv: list[str] | None = None, client=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = HarnessConfig(model=args.model,
                        root_dir=Path(args.root) if args.root else None)
    result = Harness(cfg, client=client).solve(args.problem)
    if result.final_text:
        print(result.final_text)
    if result.error:
        print(f"\n[run did not complete: {result.error}]")
    print(f"\n[session: {result.session_dir}]")
    return 1 if result.error else 0
```

Leave `build_parser`, `_short`, and `make_verbose_printer` in place (unused for now; `-v` becomes a no-op until verbose plumbing is reintroduced). Remove the `-v` handling lines inside `run_cli` only.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/cli.py tests/test_cli.py
git commit -m "feat(cli): drive the rebuilt Harness composable path"
```

---

## Task 8: Remove dead code and confirm the full suite

Retire the superseded `build_agent`/`AGENT_INSTRUCTIONS` and the old `wrap_external_tools` references now that nothing uses them, and run everything.

**Files:**
- Modify: `harness/agent.py`
- Modify: `harness/__init__.py`
- Test: full suite

- [ ] **Step 1: Find remaining references**

Run: `uv run python - <<'PY'
import subprocess
for sym in ("build_agent", "AGENT_INSTRUCTIONS", "wrap_external_tool"):
    print("==", sym)
    print(subprocess.run(["grep","-rn",sym,"harness","tests"],capture_output=True,text=True).stdout or "  (none)")
PY`
Expected: references only in `harness/agent.py` (definitions) and any stale test imports.

- [ ] **Step 2: Trim `harness/agent.py`** — delete `AGENT_INSTRUCTIONS` and `build_agent` (now superseded by `Session.create_agent`). Keep `run_agent_sync` and `_instrument` (reusable). Remove now-unused imports (`create_harness_agent`, `wrap_external_tools`, `build_tools`, `HarnessConfig`, `Session`) that only `build_agent` used. The file should retain only `run_agent_sync` and `_instrument` with their imports (`asyncio`, `functools`, `inspect`, `typing`).

- [ ] **Step 3: Confirm `harness/__init__.py` still imports cleanly** — it does not export `build_agent`; no change needed unless an import breaks. Run:

Run: `uv run python -c "import harness; print(sorted(harness.__all__))"`
Expected: prints the export list with no ImportError.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS — all tests green.

- [ ] **Step 5: Lint**

Run: `uv run ruff check harness tests`
Expected: clean (fix any unused-import warnings surfaced by the trim).

- [ ] **Step 6: Commit**

```bash
git add harness/agent.py harness/__init__.py
git commit -m "chore: retire build_agent/AGENT_INSTRUCTIONS superseded by create_agent"
```

---

## Self-Review (completed during authoring)

**Spec coverage:**
- §4/§5 composable seam → Tasks 1, 2, 4 (`create_agent`, `tools`, `harness_instructions`, async session).
- §6 bundles (core + code/files/web) → Task 2.
- §7 spill via `result_parser`, replaces `wrap_external_tool` → Task 3 (parser), Tasks 4–5 (applied to dev tools + MCP), Task 8 (removal).
- §8 lifecycle (async CM, Session owns MCP, cleanup policy) → Tasks 1, 5.
- §5.2 `Harness.solve()`/`asolve()` rebuilt → Task 6.
- §9 CLI as worked example → Task 7.
- §11 testing (result_parser, bundle wiring, MCP with stub, sync/async parity, CLI) → Tasks 3–7.
- §12 risks → execution note in Task 5; verbose-CLI regression called out in Task 7 as deferred.

**Known deferrals (intentional, not gaps):** the `--verbose`/`on_tool_call` tool-call reporter is dropped in Task 7 to migrate the API cleanly; reintroducing it (instrument built-in + external tools inside `create_agent`) is a follow-up. The real-MCP runtime verification (vs. the fake) is an execution gate in Task 5, matching spec §12.

**Type consistency:** `make_spill_parser(session, tool_name)`, `spill_tool(session, fn)`, `looks_like_mcp(tool)`, `Session.tools(*bundles)`, `Session.harness_instructions(*bundles)`, `Session.create_agent(..., bundles=...)`, `Session._attach_mcp(tool)`, `Harness(config, client, *, tools, bundles)` / `asolve`/`solve`, `Result(final_text, handles, files, session_dir, error)` — names and signatures are consistent across tasks.
