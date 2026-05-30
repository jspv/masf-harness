# Harness Phase 2b — Agent Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Phase 1/2a foundation into a runnable agent: wrap the tools as MAF agent tools (binding `session` out of the model-visible signature), add a spill middleware that auto-converts large tool/MCP returns into handles, build the agent on `create_harness_agent`, and expose a `Harness`/`solve()` API + a thin CLI. Tested deterministically with a stub chat client (no API/network).

**Architecture:** `build_tools(session)` returns agent-ready closures (the Phase 2a impls with `session` bound and clean signatures/docstrings). `make_spill_middleware(session)` is a MAF `function_middleware` that, after a tool runs, replaces an oversized/structured `context.result` with a handle summary. `build_agent(session, config, client)` assembles `create_harness_agent`. `Harness`/`solve()` manage a Session and run the agent, returning a `Result`. A `StubChatClient` test helper drives the loop deterministically.

**Tech Stack:** Python 3.12, MAF `agent-framework-core`/`-openai`, `pytest`. Tests use a stub `FunctionInvocationLayer + BaseChatClient`; no API key or network. One opt-in live test (`HARNESS_LIVE=1`) against `gpt-4o-mini`.

**MAF facts (verified by discovery spike, 2026-05-30):**
- Function middleware: `@function_middleware async def mw(context: FunctionInvocationContext, call_next): await call_next(); context.result = ...`. `context.function.name` is the tool name; `context.result` is readable AND writable after `call_next()` (overrides what the model sees). Pass via `create_harness_agent(..., middleware=[mw])`.
- A chat client must subclass `FunctionInvocationLayer` (mixin) AND `BaseChatClient` for tools to auto-execute; only `_inner_get_response(self, *, messages, stream, options, **kwargs)` is abstract.
- Build responses with `ChatResponse(messages=Message(role="assistant", contents=[...]))`; a tool call is `Content("function_call", call_id=..., name=..., arguments={...})`; text is `Content("text", text=...)`. Import `Content` from `agent_framework._types`.
- `create_harness_agent(client, *, tools=[callables], middleware=[...], max_context_window_tokens=int, max_output_tokens=int, disable_todo/disable_mode/disable_memory/disable_web_search=True)`; `await agent.run(prompt)` → response with `.text` and `.messages`.

**Builds on (Phase 1 + 2a, on `main`):** `harness.Session`, `harness.HarnessConfig`, `harness.HandleStore`, and `harness.tools.{files,search,fetch,code,inspect}` impl functions (each takes `session` first).

---

## File Structure

- Create: `harness/tools/registry.py` — `build_tools(session) -> list[callable]`
- Create: `harness/spill.py` — `make_spill_middleware(session)` + `_should_spill`
- Create: `harness/agent.py` — `build_agent(session, config, client) -> Agent`
- Create: `harness/testing.py` — `StubChatClient` (public test helper; also usable by downstream users)
- Create: `harness/api.py` — `Harness`, `solve`, `Result`
- Create: `harness/cli.py` — `main()` CLI entry
- Modify: `harness/__init__.py` — export `Harness`, `solve`, `Result`, `build_tools`
- Modify: `pyproject.toml` — add `[project.scripts] harness = "harness.cli:main"`
- Create tests: `tests/test_registry.py`, `tests/test_spill.py`, `tests/test_agent_loop.py`, `tests/test_api.py`, `tests/test_cli.py`

---

## Task 1: `build_tools(session)` — agent-ready tool wrappers

**Files:**
- Create: `harness/tools/registry.py`
- Test: `tests/test_registry.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_registry.py`:

```python
import inspect

from harness import HarnessConfig, Session
from harness.tools.registry import build_tools


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def test_build_tools_returns_expected_named_callables(tmp_path):
    tools = build_tools(_session(tmp_path))
    names = {t.__name__ for t in tools}
    assert names == {
        "read_file", "write_file", "list_files", "search",
        "fetch_url", "run_python", "inspect_handle",
    }


def test_wrapped_tools_do_not_expose_session_param(tmp_path):
    tools = {t.__name__: t for t in build_tools(_session(tmp_path))}
    assert "session" not in inspect.signature(tools["read_file"]).parameters
    assert list(inspect.signature(tools["read_file"]).parameters)[0] == "path"


def test_wrapped_tools_keep_docstrings(tmp_path):
    tools = {t.__name__: t for t in build_tools(_session(tmp_path))}
    assert tools["search"].__doc__ and "pattern" in tools["search"].__doc__.lower()


def test_wrapped_tools_actually_work(tmp_path):
    sess = _session(tmp_path)
    tools = {t.__name__: t for t in build_tools(sess)}
    tools["write_file"]("a.txt", "hello\n")
    assert tools["read_file"]("a.txt") == "hello\n"
    out = tools["run_python"](code="from harness_sandbox import emit\nemit(5)\n")
    assert out["result"] == 5
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.tools.registry'`

- [ ] **Step 3: Implement** — create `harness/tools/registry.py`:

```python
"""build_tools: wrap the Session-bound tool impls as agent-ready callables.

Each wrapper has a clean model-facing signature (no ``session`` parameter), an
accurate ``__name__``, and a docstring that MAF turns into the tool description.
"""

from __future__ import annotations

from ..session import Session
from . import code as _code
from . import fetch as _fetch
from . import files as _files
from . import inspect as _inspect
from . import search as _search


def build_tools(session: Session) -> list:
    """Return the agent's tool callables, each bound to ``session``."""

    def read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read up to `limit` lines starting at line `offset` from a file in the workspace."""
        return _files.read_file(session, path, offset, limit)

    def write_file(path: str, content: str) -> str:
        """Write `content` to a file in the workspace (creates parent directories)."""
        return _files.write_file(session, path, content)

    def list_files(path: str = ".") -> list[str]:
        """List files under `path` (a file or folder) in the workspace, recursively."""
        return _files.list_files(session, path)

    def search(pattern: str, path: str = ".", glob: str | None = None,
               ignore_case: bool = False, max_matches: int = 100) -> list[dict]:
        """Search files under `path` for the regex `pattern`. Returns file/line/col/text hits."""
        return _search.search(session, pattern, path, glob, ignore_case, max_matches)

    def fetch_url(url: str, max_bytes: int | None = None) -> dict:
        """Fetch `url` and store its body as a handle; returns the handle summary."""
        return _fetch.fetch_url(session, url, max_bytes)

    def run_python(code: str | None = None, path: str | None = None,
                   args: list[str] | None = None) -> dict:
        """Run Python in the sandbox. Give `code` (inline) or `path` (a script file). Scripts
        may use load(id)/save(id, obj)/emit(obj). Returns stdout/result/error/new_handles."""
        return _code.run_python(session, code, path, args)

    def inspect_handle(handle_id: str, rows: int = 20, stats: bool = False) -> dict:
        """Deeper look at a handle: more preview / head rows (and describe() if stats=True)."""
        return _inspect.inspect_handle(session, handle_id, rows, stats)

    return [read_file, write_file, list_files, search, fetch_url, run_python, inspect_handle]
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_registry.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add harness/tools/registry.py tests/test_registry.py
git commit -m "feat(agent): add build_tools wrapping tool impls as agent-ready callables"
```

---

## Task 2: spill middleware

**Files:**
- Create: `harness/spill.py`
- Test: `tests/test_spill.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_spill.py`:

```python
import asyncio
from types import SimpleNamespace

import pandas as pd

from harness import HarnessConfig, Session
from harness.spill import _should_spill, make_spill_middleware


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def _run_mw(mw, function_name, result):
    """Drive the middleware: call_next sets the tool result, then mw may rewrite it."""
    ctx = SimpleNamespace(function=SimpleNamespace(name=function_name), result=None)

    async def call_next():
        ctx.result = result

    asyncio.run(mw(ctx, call_next))
    return ctx.result


def test_large_dict_is_spilled_to_handle_summary(tmp_path):
    sess = _session(tmp_path)
    mw = make_spill_middleware(sess)
    big = {"rows": list(range(5000))}  # well over the default 8 KB threshold
    out = _run_mw(mw, "query_data", big)
    assert out["kind"] == "json"          # replaced with a handle summary
    assert "id" in out
    assert sess.store.get(out["id"]) == big  # full data preserved on disk


def test_dataframe_result_is_always_spilled(tmp_path):
    sess = _session(tmp_path)
    mw = make_spill_middleware(sess)
    df = pd.DataFrame({"a": [1, 2, 3]})
    out = _run_mw(mw, "query_df", df)
    assert out["kind"] == "dataframe"
    pd.testing.assert_frame_equal(sess.store.get(out["id"]), df)


def test_small_result_passes_through_unchanged(tmp_path):
    sess = _session(tmp_path)
    mw = make_spill_middleware(sess)
    out = _run_mw(mw, "add", 42)
    assert out == 42
    assert sess.store.manifest_handles() == {}  # nothing spilled


def test_existing_handle_summary_is_not_respilled(tmp_path):
    sess = _session(tmp_path)
    mw = make_spill_middleware(sess)
    h = sess.store.put({"x": 1}, source="seed")
    summary = h.summary()
    out = _run_mw(mw, "fetch_url", summary)  # already a handle summary
    assert out == summary
    assert len(sess.store.manifest_handles()) == 1  # no new handle created


def test_should_spill_thresholds(tmp_path):
    sess = _session(tmp_path)
    assert _should_spill("x" * 10000, sess.config.spill_threshold_bytes) is True
    assert _should_spill("small", sess.config.spill_threshold_bytes) is False
    assert _should_spill(42, sess.config.spill_threshold_bytes) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_spill.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.spill'`

- [ ] **Step 3: Implement** — create `harness/spill.py`:

```python
"""Spill middleware: convert oversized/structured tool results into handles.

After a tool runs, if its result is a DataFrame or a json/text payload larger than
the configured threshold, write it to the handle store and replace the model-visible
result with the lightweight handle summary. Small scalars and results that are already
handle summaries pass through untouched.
"""

from __future__ import annotations

import json
from typing import Any

from agent_framework import function_middleware

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
    if isinstance(result, str):
        return len(result.encode()) > threshold_bytes
    if isinstance(result, (dict, list)):
        if _is_handle_summary(result):
            return False
        return len(json.dumps(result, default=str).encode()) > threshold_bytes
    return False


def make_spill_middleware(session: Session):
    """Return a function middleware bound to ``session`` that spills large tool results."""
    threshold = session.config.spill_threshold_bytes

    @function_middleware
    async def spill_middleware(context, call_next) -> None:
        await call_next()
        result = context.result
        if _is_handle_summary(result):
            return
        if _should_spill(result, threshold):
            handle = session.store.put(result, source=f"tool:{context.function.name}")
            context.result = handle.summary()

    return spill_middleware
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_spill.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add harness/spill.py tests/test_spill.py
git commit -m "feat(agent): add spill middleware converting large tool results to handles"
```

---

## Task 3: `StubChatClient` + `build_agent` + the agent-loop test

**Files:**
- Create: `harness/testing.py`
- Create: `harness/agent.py`
- Test: `tests/test_agent_loop.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_agent_loop.py`:

```python
from harness import HarnessConfig, Session
from harness.agent import build_agent
from harness.testing import StubChatClient, tool_call, text


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def test_agent_runs_gather_act_verify_over_real_tools(tmp_path):
    sess = _session(tmp_path)
    # Seed a dataset the agent will analyze.
    sess.store.put({"sales": [120, 0, 210, 0, 95]}, source="seed", id="h1")

    # Scripted model: inspect the handle, run python to total the non-zero rows, then answer.
    script = [
        tool_call("inspect_handle", {"handle_id": "h1"}),
        tool_call("run_python", {"code":
            "from harness_sandbox import load, emit\n"
            "d = load('h1')\n"
            "emit(sum(v for v in d['sales'] if v > 0))\n"}),
        text("The total of valid sales is 425."),
    ]
    agent = build_agent(sess, sess.config, StubChatClient(script))
    import asyncio
    resp = asyncio.run(agent.run("Total the valid sales in h1."))
    assert "425" in resp.text


def test_stub_records_tool_results_seen(tmp_path):
    sess = _session(tmp_path)
    client = StubChatClient([tool_call("write_file", {"path": "a.txt", "content": "hi"}),
                             text("done")])
    agent = build_agent(sess, sess.config, client)
    import asyncio
    asyncio.run(agent.run("write a file"))
    assert (sess.root / "a.txt").read_text() == "hi"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_agent_loop.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.testing'`

- [ ] **Step 3: Implement the stub client** — create `harness/testing.py`:

```python
"""StubChatClient: a deterministic chat client for testing the agent loop without an API.

Drive it with a script of `tool_call(...)` / `text(...)` steps; each model turn pops the
next step. A tool_call step makes the agent invoke that tool; a text step ends the run.
"""

from __future__ import annotations

from typing import Any

from agent_framework import BaseChatClient, ChatResponse, FunctionInvocationLayer, Message
from agent_framework._types import Content


def tool_call(name: str, arguments: dict[str, Any] | None = None, call_id: str = "c") -> Content:
    return Content("function_call", call_id=call_id, name=name, arguments=arguments or {})


def text(value: str) -> Content:
    return Content("text", text=value)


class StubChatClient(FunctionInvocationLayer, BaseChatClient):
    """Returns scripted responses, one per model turn."""

    def __init__(self, script: list[Content]) -> None:
        super().__init__()
        self._script = list(script)
        self._turn = 0

    async def _inner_get_response(self, *, messages, stream, options, **kwargs) -> ChatResponse:
        step = self._script[min(self._turn, len(self._script) - 1)]
        self._turn += 1
        return ChatResponse(messages=Message(role="assistant", contents=[step]))
```

- [ ] **Step 4: Implement the agent builder** — create `harness/agent.py`:

```python
"""build_agent: assemble the harness agent from a Session, config, and a chat client."""

from __future__ import annotations

from agent_framework import create_harness_agent

from .config import HarnessConfig
from .session import Session
from .spill import make_spill_middleware
from .tools.registry import build_tools

AGENT_INSTRUCTIONS = (
    "You solve data-gathering and integration tasks. "
    "IMPORTANT: Work autonomously and do NOT stop to ask the user. "
    "Large data is referenced by handles (ids); never expect full datasets in the "
    "conversation -- load and analyze them by writing Python via run_python "
    "(use load(id)/save(id, obj)/emit(obj)). "
    "ALWAYS verify data quality before reporting results, and state any issues you handled."
)


def build_agent(session: Session, config: HarnessConfig, client, extra_tools: list | None = None):
    """Build a harness Agent over the session's tools (plus any ``extra_tools``) with the
    spill middleware installed."""
    return create_harness_agent(
        client,
        name="data-integrator",
        agent_instructions=AGENT_INSTRUCTIONS,
        tools=build_tools(session) + list(extra_tools or []),
        middleware=[make_spill_middleware(session)],
        max_context_window_tokens=config.max_context_window_tokens,
        max_output_tokens=config.max_output_tokens,
        disable_todo=True,
        disable_mode=True,
        disable_memory=True,
        disable_web_search=True,
    )
```

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest tests/test_agent_loop.py -v`
Expected: PASS (2 passed)

If `create_harness_agent` rejects a kwarg or the loop behaves differently than the discovery spike showed, debug honestly against the installed API (`uv run python -c "import inspect, agent_framework as af; print(inspect.signature(af.create_harness_agent))"`). Do NOT weaken the tests.

- [ ] **Step 6: Commit**

```bash
git add harness/testing.py harness/agent.py tests/test_agent_loop.py
git commit -m "feat(agent): add StubChatClient + build_agent; deterministic agent-loop test"
```

---

## Task 4: `Harness` / `solve()` / `Result`

**Files:**
- Create: `harness/api.py`
- Modify: `harness/__init__.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_api.py`:

```python
from harness import Harness, HarnessConfig, Result
from harness.testing import StubChatClient, tool_call, text


def test_solve_returns_result_with_answer_and_session(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    client = StubChatClient([
        tool_call("write_file", {"path": "out.txt", "content": "42"}),
        text("Wrote the answer: 42."),
    ])
    h = Harness(cfg, client=client)
    result = h.solve("write 42 to out.txt")
    assert isinstance(result, Result)
    assert "42" in result.final_text
    assert result.session_dir == h.session.root
    assert (h.session.root / "out.txt").read_text() == "42"


def test_solve_exposes_handles_created_during_the_run(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    client = StubChatClient([
        tool_call("run_python", {"code":
            "from harness_sandbox import save\nsave('out', {'k': 1})\n"}),
        text("Saved handle 'out'."),
    ])
    result = Harness(cfg, client=client).solve("save a handle")
    assert "out" in result.handles
    assert result.handles["out"]["kind"] == "json"


def test_solve_accepts_extra_user_tools(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    calls = {"n": 0}

    def my_source() -> dict:
        "Return some data."
        calls["n"] += 1
        return {"value": 7}

    client = StubChatClient([tool_call("my_source", {}), text("Got it.")])
    Harness(cfg, client=client).solve("call my_source", tools=[my_source])
    assert calls["n"] == 1
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_api.py -v`
Expected: FAIL — `ImportError: cannot import name 'Harness'`

- [ ] **Step 3: Implement** — create `harness/api.py`:

```python
"""Public entry point: Harness / solve() returning a Result."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import build_agent
from .config import HarnessConfig
from .session import Session


@dataclass
class Result:
    final_text: str
    handles: dict[str, Any]
    files: list[str]
    session_dir: Path


class Harness:
    """A reusable harness: holds config + a Session and runs the agent on tasks."""

    def __init__(self, config: HarnessConfig | None = None, client: Any | None = None) -> None:
        self.config = config or HarnessConfig()
        self._client = client
        self.session = Session.create(self.config)

    def _make_client(self):
        if self._client is not None:
            return self._client
        from agent_framework.openai import OpenAIChatClient
        return OpenAIChatClient(model=self.config.model, env_file_path=".env")

    def solve(self, problem: str, tools: list | None = None) -> Result:
        agent = build_agent(self.session, self.config, self._make_client(), extra_tools=tools)
        resp = asyncio.run(agent.run(problem))
        return Result(
            final_text=resp.text,
            handles=self.session.store.manifest(),
            files=[p.relative_to(self.session.root).as_posix()
                   for p in sorted(self.session.root.rglob("*")) if p.is_file()],
            session_dir=self.session.root,
        )


def solve(problem: str, *, tools: list | None = None,
          config: HarnessConfig | None = None, client: Any | None = None) -> Result:
    """One-shot convenience: build a Harness and solve a single problem."""
    return Harness(config, client=client).solve(problem, tools=tools)
```

- [ ] **Step 4: Export the public API** — update `harness/__init__.py` by adding these imports and `__all__` entries (keep all existing Phase 1 exports):

Add after the existing imports:
```python
from .api import Harness, Result, solve
from .tools.registry import build_tools
```
And add to `__all__`: `"Harness", "Result", "solve", "build_tools"`.

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest tests/test_api.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add harness/api.py harness/__init__.py tests/test_api.py
git commit -m "feat(agent): add Harness/solve()/Result public API"
```

---

## Task 5: thin CLI

**Files:**
- Create: `harness/cli.py`
- Modify: `pyproject.toml` (add console script)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_cli.py`:

```python
from harness.cli import build_parser, run_cli
from harness.testing import StubChatClient, tool_call, text


def test_parser_reads_problem_and_model(tmp_path):
    args = build_parser().parse_args(["do a thing", "--model", "gpt-4o-mini"])
    assert args.problem == "do a thing"
    assert args.model == "gpt-4o-mini"


def test_run_cli_prints_answer(tmp_path, capsys):
    client = StubChatClient([tool_call("write_file", {"path": "a.txt", "content": "x"}),
                             text("Done: wrote a.txt.")])
    code = run_cli(["write a file", "--root", str(tmp_path / "r")], client=client)
    assert code == 0
    out = capsys.readouterr().out
    assert "Done: wrote a.txt" in out
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.cli'`

- [ ] **Step 3: Implement** — create `harness/cli.py`:

```python
"""Thin CLI over Harness.solve()."""

from __future__ import annotations

import argparse
from pathlib import Path

from .api import Harness
from .config import HarnessConfig


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="Run the data-integration harness.")
    p.add_argument("problem", help="The task to solve.")
    p.add_argument("--model", default="gpt-4o-mini", help="Model name.")
    p.add_argument("--root", default=None, help="Workspace root dir (default: a fresh session dir).")
    return p


def run_cli(argv: list[str] | None = None, client=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = HarnessConfig(model=args.model,
                        root_dir=Path(args.root) if args.root else None)
    result = Harness(cfg, client=client).solve(args.problem)
    print(result.final_text)
    print(f"\n[session: {result.session_dir}]")
    return 0


def main() -> None:
    raise SystemExit(run_cli())
```

- [ ] **Step 4: Add the console script** — in `pyproject.toml`, add this section (after `[project]` or near the build config):

```toml
[project.scripts]
harness = "harness.cli:main"
```

- [ ] **Step 5: Run to verify they pass + full suite**

Run: `uv run pytest tests/test_cli.py -v` → PASS (2 passed)
Then `uv run pytest -q` → all Phase 1 + 2a + 2b pass (~95 tests).

- [ ] **Step 6: Commit**

```bash
git add harness/cli.py pyproject.toml tests/test_cli.py
git commit -m "feat(agent): add thin CLI over Harness.solve()"
```

---

## Task 6: opt-in live smoke test (gpt-4o-mini)

**Files:**
- Test: `tests/test_live.py`

- [ ] **Step 1: Write the gated live test** — create `tests/test_live.py`:

```python
import os

import pytest

from harness import Harness, HarnessConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE") != "1",
    reason="set HARNESS_LIVE=1 (and OPENAI_API_KEY in .env) to run the live smoke test",
)


def test_live_gather_act_verify(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r", model="gpt-4o-mini")
    h = Harness(cfg)
    h.session.store.put({"sales": [120, 0, 210, 0, 95]}, source="seed", id="h1")
    result = h.solve(
        "Dataset handle 'h1' holds sales numbers. Total the valid (non-zero) sales, "
        "exclude any zero rows, and report the total with a note on what you excluded."
    )
    assert "425" in result.final_text
```

- [ ] **Step 2: Confirm it skips without the flag**

Run: `uv run pytest tests/test_live.py -v`
Expected: SKIPPED (1 skipped)

- [ ] **Step 3: (Optional, manual) run it live**

Run: `HARNESS_LIVE=1 uv run pytest tests/test_live.py -v`
Expected: PASS (uses the real `gpt-4o-mini` via `.env`). If it fails due to model nondeterminism, that's a model-strength observation, not a harness defect — note it; do not weaken other tests.

- [ ] **Step 4: Commit**

```bash
git add tests/test_live.py
git commit -m "test(agent): add opt-in live smoke test (HARNESS_LIVE=1)"
```

---

## Definition of done (Phase 2b)

- `uv run pytest` green (Phase 1 + 2a + 2b, ~95 tests), all without API/network.
- `from harness import Harness, solve` works; `Harness(config, client=stub).solve(problem)` runs the real `create_harness_agent` loop over the real tools, with large tool results auto-spilled to handles.
- The agent never sees full datasets — only handle summaries and the results it deliberately `emit`s.
- A thin `harness "<task>"` CLI runs the loop and prints the answer + session dir.
- One opt-in live test confirms the whole stack against `gpt-4o-mini`.
- This completes the v1 substrate from the design spec; future work (container sandbox tier, MCP server wiring, Workflow durability wrapper, skills/memory) builds on it.
```
