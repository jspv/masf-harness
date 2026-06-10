# Tool Status Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let tools emit lightweight status/progress updates during execution that the harness observes (and can forward to a UI), via a harness-owned side-band channel.

**Architecture:** A `contextvars`-backed producer (`report_progress()`) feeds a thread-safe `StatusBus` that the `Session` owns and binds for the duration of a run; subscribers (`Harness(on_status=…)`, the CLI `--verbose` printer) receive `StatusEvent`s live. No MAF stream is involved — the harness agent is non-streaming, so this channel is entirely ours. (Spec: `docs/superpowers/specs/2026-06-10-tool-status-updates-design.md`.)

**Tech Stack:** Python 3.12 stdlib (`contextvars`, `threading`, `dataclasses`), Microsoft Agent Framework 1.8.1, pytest. The `agent-framework` 1.8.1 bump is already committed on this branch.

**Note on scope:** v1 = internal-tool status only. MCP status capture and an AG-UI adapter sink are deferred (separate future specs).

---

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `harness/status.py` | **Create** | `StatusEvent`, `StatusBus`, `report_progress`, `bind_bus`, `current_bus` |
| `harness/__init__.py` | Modify | Export `StatusEvent`, `StatusBus`, `report_progress` |
| `harness/session.py` | Modify | Own a `StatusBus`; `subscribe()`; bind the bus across the run via `__aenter__`/`__aexit__` |
| `harness/api.py` | Modify | `Harness(on_status=…)`, `asolve`/`solve`/module `solve` accept `on_status`; subscribe before the run |
| `harness/tools/documents.py`, `code.py`, `fetch.py`, `web.py` | Modify | Built-in tools call `report_progress()` at milestones |
| `harness/spill.py` | Modify | Emit when a tool result spills to a handle |
| `harness/cli.py` | Modify | `--verbose` registers a printer sink; remove the "temporarily unavailable" notice |
| `tests/test_status.py` | **Create** | Unit tests for the bus/producer |
| `tests/test_status_instrumentation.py` | **Create** | Built-in tools emit the expected events |
| `tests/test_api.py`, `tests/test_cli.py` | Modify | `on_status` end-to-end + CLI sink |

---

## Task 1: `status.py` — event, bus, producer

**Files:**
- Create: `harness/status.py`
- Modify: `harness/__init__.py`
- Test: `tests/test_status.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_status.py`:

```python
import threading

from harness.status import StatusBus, StatusEvent, bind_bus, current_bus, report_progress


def test_subscriber_receives_emitted_event():
    bus = StatusBus()
    got = []
    bus.subscribe(got.append)
    bus.emit(StatusEvent(tool="t", message="hi", current=1, total=3))
    assert len(got) == 1
    e = got[0]
    assert (e.tool, e.message, e.current, e.total) == ("t", "hi", 1, 3)
    assert e.seq == 1 and e.timestamp > 0          # bus stamps seq + timestamp


def test_seq_is_monotonic_per_bus():
    bus = StatusBus()
    got = []
    bus.subscribe(got.append)
    bus.emit(StatusEvent(tool="t", message="a"))
    bus.emit(StatusEvent(tool="t", message="b"))
    assert [e.seq for e in got] == [1, 2]


def test_report_progress_is_noop_when_unbound():
    # No bus bound to the contextvar -> silently does nothing.
    assert current_bus() is None
    report_progress("nobody listening")             # must not raise


def test_report_progress_routes_to_bound_bus():
    bus = StatusBus()
    got = []
    bus.subscribe(got.append)
    with bind_bus(bus):
        assert current_bus() is bus
        report_progress("working", current=2, total=4, tool="mytool")
    assert current_bus() is None                     # unbound after the block
    assert (got[0].tool, got[0].message, got[0].current, got[0].total) == ("mytool", "working", 2, 4)


def test_raising_subscriber_does_not_break_emit():
    bus = StatusBus()
    got = []
    bus.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.subscribe(got.append)                        # second subscriber still runs
    bus.emit(StatusEvent(tool="t", message="ok"))
    assert got and got[0].message == "ok"


def test_unsubscribe_handle_stops_delivery():
    bus = StatusBus()
    got = []
    unsub = bus.subscribe(got.append)
    unsub()
    bus.emit(StatusEvent(tool="t", message="x"))
    assert got == []


def test_emit_from_worker_thread_reaches_subscriber():
    bus = StatusBus()
    got = []
    bus.subscribe(got.append)

    def worker():
        with bind_bus(bus):
            report_progress("from thread", tool="bg")

    th = threading.Thread(target=worker)
    th.start()
    th.join()
    assert got and got[0].message == "from thread"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_status.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.status'`.

- [ ] **Step 3: Write the implementation**

Create `harness/status.py`:

```python
"""Tool status updates: a harness-owned side-band channel.

A tool calls ``report_progress(...)`` while it runs; the call routes through a
``contextvars`` lookup to the ``StatusBus`` the active ``Session`` bound for this run, which
fans the event out to subscribers (e.g. a ``Harness(on_status=...)`` callback or the CLI
``--verbose`` printer). Outside a bound run, ``report_progress`` is a no-op, so tools can
call it unconditionally. The harness agent is non-streaming, so this is entirely our own
channel -- no MAF stream is involved.
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

_log = logging.getLogger("harness.status")


@dataclass(frozen=True)
class StatusEvent:
    tool: str                       # emitting tool name, or "harness"
    message: str                    # human-readable status line
    current: float | None = None    # progress numerator (optional)
    total: float | None = None      # progress denominator (optional)
    seq: int = 0                    # monotonic per-bus ordering (filled by the bus)
    timestamp: float = 0.0          # wall-clock, time.time() (filled by the bus)


class StatusBus:
    """Thread-safe fan-out of StatusEvents to subscriber callbacks."""

    def __init__(self) -> None:
        self._subscribers: list[Callable[[StatusEvent], None]] = []
        self._lock = threading.Lock()
        self._seq = 0

    def subscribe(self, callback: Callable[[StatusEvent], None]) -> Callable[[], None]:
        """Register ``callback``; returns a zero-arg handle that unsubscribes it."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def emit(self, event: StatusEvent) -> None:
        """Stamp ``event`` with seq + timestamp and deliver to every subscriber.

        Subscribers are snapshotted under the lock, then called outside it (so a subscriber
        may itself emit without deadlocking). A raising subscriber is swallowed -- status is
        best-effort and must never break the task.
        """
        with self._lock:
            self._seq += 1
            stamped = dataclasses.replace(event, seq=self._seq, timestamp=time.time())
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(stamped)
            except Exception:  # noqa: BLE001 - best-effort; never propagate into the task
                _log.debug("status subscriber raised", exc_info=True)


_current: contextvars.ContextVar[StatusBus | None] = contextvars.ContextVar(
    "harness_status_bus", default=None
)


def current_bus() -> StatusBus | None:
    return _current.get()


@contextmanager
def bind_bus(bus: StatusBus) -> Iterator[None]:
    """Make ``bus`` the target of ``report_progress`` for the duration of the block."""
    token = _current.set(bus)
    try:
        yield
    finally:
        _current.reset(token)


def report_progress(message: str, *, current: float | None = None,
                    total: float | None = None, tool: str = "tool") -> None:
    """Emit a status update from inside a tool. No-op outside a bound run."""
    bus = _current.get()
    if bus is None:
        return
    bus.emit(StatusEvent(tool=tool, message=message, current=current, total=total))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_status.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Export from the package**

In `harness/__init__.py`, add after the `from .session import Session` line:

```python
from .status import StatusBus, StatusEvent, report_progress
```

And add to `__all__` (after `"Session",`):

```python
    "StatusBus", "StatusEvent", "report_progress",
```

- [ ] **Step 6: Verify imports + lint**

Run: `.venv/bin/python -c "from harness import StatusEvent, StatusBus, report_progress; print('ok')"`
Run: `.venv/bin/ruff check harness/status.py tests/test_status.py harness/__init__.py`
Expected: `ok`; `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add harness/status.py harness/__init__.py tests/test_status.py
git commit -m "feat(status): StatusEvent + StatusBus + report_progress side-band channel"
```

---

## Task 2: Session owns the bus and binds it across a run

**Files:**
- Modify: `harness/session.py`
- Test: `tests/test_session.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_session.py`:

```python
import asyncio

from harness import HarnessConfig, Session
from harness.status import report_progress


def test_session_subscribe_receives_events_within_async_context(tmp_path):
    async def run():
        got = []
        async with Session.create(HarnessConfig(root_dir=tmp_path / "r")) as session:
            session.subscribe(got.append)
            report_progress("inside the run", tool="x")   # bus is bound by __aenter__
        return got

    got = asyncio.run(run())
    assert len(got) == 1
    assert got[0].message == "inside the run"


def test_report_progress_is_noop_outside_async_context(tmp_path):
    # Session.create without `async with` does NOT bind the bus.
    session = Session.create(HarnessConfig(root_dir=tmp_path / "r2"))
    got = []
    session.subscribe(got.append)
    report_progress("nobody bound")                       # no-op
    assert got == []


def test_session_unbinds_bus_on_exit(tmp_path):
    from harness.status import current_bus

    async def run():
        async with Session.create(HarnessConfig(root_dir=tmp_path / "r3")):
            assert current_bus() is not None
        assert current_bus() is None

    asyncio.run(run())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_session.py -k "status or noop or unbind or subscribe" -q`
Expected: FAIL — `Session` has no `subscribe`; bus not bound.

- [ ] **Step 3: Implement the Session changes**

In `harness/session.py`, add the import near the top (after `from .sandbox import LocalSubprocessSandbox`):

```python
from .status import StatusBus, bind_bus
```

Add two fields to the `Session` dataclass, right after the existing `_mcp_connected` field:

```python
    status_bus: StatusBus = field(default_factory=StatusBus, init=False, repr=False)
    _status_cm: Any = field(default=None, init=False, repr=False)
```

Add a `subscribe` method (place it just after the `artifacts` property):

```python
    def subscribe(self, callback) -> Callable[[], None]:
        """Register a status subscriber; returns a zero-arg unsubscribe handle."""
        return self.status_bus.subscribe(callback)
```

Add the `Callable` import: change the `from typing import Any` line to:

```python
from typing import Any, Callable
```

Replace the existing `__aenter__` / `__aexit__` with bus binding:

```python
    async def __aenter__(self) -> "Session":
        self._status_cm = bind_bus(self.status_bus)
        self._status_cm.__enter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            await self.aclose()
        finally:
            if self._status_cm is not None:
                self._status_cm.__exit__(None, None, None)
                self._status_cm = None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_session.py -q`
Expected: PASS (all session tests).

- [ ] **Step 5: Full suite + lint (no regressions)**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/session.py tests/test_session.py`
Expected: all pass; `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add harness/session.py tests/test_session.py
git commit -m "feat(status): Session owns the StatusBus and binds it across each run"
```

---

## Task 3: `Harness(on_status=…)` / `solve(on_status=…)`

**Files:**
- Modify: `harness/api.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api.py` (note: `StubChatClient`, `text`, `tool_call` are already imported):

```python
from harness import report_progress


def noisy(n: int) -> str:
    """Emit two progress updates, then return."""
    report_progress("step A", tool="noisy", current=1, total=2)
    report_progress("step B", tool="noisy", current=2, total=2)
    return "ok"


def test_on_status_receives_tool_events_end_to_end(tmp_path):
    events = []
    client = StubChatClient([tool_call("noisy", {"n": 1}), text("done")])
    h = Harness(HarnessConfig(root_dir=tmp_path / "s"), client=client,
                tools=[noisy], on_status=events.append)
    result = h.solve("go")
    assert result.final_text == "done"
    seen = [(e.tool, e.message, e.current, e.total) for e in events]
    assert ("noisy", "step A", 1, 2) in seen
    assert ("noisy", "step B", 2, 2) in seen
    assert [e.seq for e in events] == sorted(e.seq for e in events)   # ordered


def test_on_status_can_be_passed_per_call(tmp_path):
    events = []
    client = StubChatClient([tool_call("noisy", {"n": 1}), text("done")])
    h = Harness(HarnessConfig(root_dir=tmp_path / "s2"), client=client, tools=[noisy])
    h.solve("go", on_status=events.append)                # per-call overrides/sets the sink
    assert any(e.message == "step A" for e in events)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_api.py -k on_status -q`
Expected: FAIL — `Harness.__init__` got an unexpected keyword `on_status`.

- [ ] **Step 3: Implement the api.py changes**

In `harness/api.py`, change `Harness.__init__` to accept and store `on_status`:

```python
    def __init__(self, config: HarnessConfig | None = None, client: Any | None = None,
                 *, tools: list | None = None,
                 bundles: tuple[str, ...] = ("code", "files", "web"),
                 on_status=None) -> None:
        self.config = config or HarnessConfig()
        self._client = client
        self._tools = tools or []
        self._bundles = bundles
        self._on_status = on_status
        if self.config.search.api_key is None:
            import os

            from dotenv import load_dotenv
            load_dotenv()
            self.config.search.api_key = os.environ.get("TAVILY_API_KEY")
```

Change `asolve` to subscribe the sink inside the run, before building/running the agent:

```python
    async def asolve(self, problem: str, tools: list | None = None, *, on_status=None) -> Result:
        final_text, error = "", None
        sink = on_status if on_status is not None else self._on_status
        async with Session.create(self.config) as session:
            if sink is not None:
                session.subscribe(sink)
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
```

Change `solve` (instance) and the module-level `solve` to thread `on_status`:

```python
    def solve(self, problem: str, tools: list | None = None, *, on_status=None) -> Result:
        return asyncio.run(self.asolve(problem, tools=tools, on_status=on_status))
```

```python
def solve(problem: str, *, tools: list | None = None,
          config: HarnessConfig | None = None, client: Any | None = None,
          on_status=None) -> Result:
    """One-shot convenience: build a Harness and solve a single problem."""
    return Harness(config, client=client, tools=tools, on_status=on_status).solve(problem)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_api.py -q`
Expected: PASS (existing + the two new tests).

- [ ] **Step 5: Full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/api.py tests/test_api.py`
Expected: all pass; `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add harness/api.py tests/test_api.py
git commit -m "feat(status): Harness/solve accept on_status; deliver tool events end to end"
```

---

## Task 4: Instrument the local built-in tools + the spill path

**Files:**
- Modify: `harness/tools/documents.py`, `harness/tools/code.py`, `harness/spill.py`
- Test: `tests/test_status_instrumentation.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_status_instrumentation.py`:

```python
from harness import HarnessConfig, Session
from harness.status import StatusBus, bind_bus
from harness.tools.code import run_python
from harness.tools.documents import read_document
from harness.spill import make_spill_parser


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r", spill_threshold_bytes=64))


def _collect():
    bus = StatusBus()
    events = []
    bus.subscribe(events.append)
    return bus, events


def test_read_document_emits_converting_status(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "d.pdf").write_bytes(b"x")
    bus, events = _collect()
    with bind_bus(bus):
        read_document(sess, "d.pdf", convert=lambda src: "# ok")
    assert any(e.tool == "read_document" and "converting" in e.message.lower() for e in events)


def test_run_python_emits_running_status(tmp_path):
    sess = _session(tmp_path)
    bus, events = _collect()
    with bind_bus(bus):
        run_python(sess, code="from harness_sandbox import emit\nemit(1)\n")
    assert any(e.tool == "run_python" for e in events)


def test_spill_emits_stored_status(tmp_path):
    sess = _session(tmp_path)
    parse = make_spill_parser(sess, "big_tool")
    bus, events = _collect()
    with bind_bus(bus):
        parse({"rows": list(range(500))})              # over the 64-byte threshold -> spills
    stored = [e for e in events if e.tool == "big_tool" and "stored" in e.message.lower()]
    assert stored and "h1" in stored[0].message
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_status_instrumentation.py -q`
Expected: FAIL — no status events emitted yet.

- [ ] **Step 3: Instrument `read_document`**

In `harness/tools/documents.py`, add the import (after `from ..session import Session`):

```python
from ..status import report_progress
```

In `read_document`, add the report just before the `try:` that calls `convert(target)` — i.e. immediately after the `if convert is None:` block and before `try:`:

```python
    report_progress(f"converting {source} via Docling", tool="read_document")
    try:
        markdown = convert(target)
```

- [ ] **Step 4: Instrument `run_python`**

In `harness/tools/code.py`, add the import (after `from ..session import Session`):

```python
from ..status import report_progress
```

Add the report as the first statement in the function body (before the `if path is not None:` check):

```python
    report_progress("running script in sandbox", tool="run_python")
    if path is not None:
```

- [ ] **Step 5: Instrument the spill path**

In `harness/spill.py`, add the import (after `from agent_framework._types import Content`):

```python
from .status import report_progress
```

In `_maybe_spill`, replace the final spill return:

```python
    handle = session.store.put(result, source=f"tool:{tool_name}")
    report_progress(f"stored {size} bytes as {handle.id}", tool=tool_name)
    return handle.summary()
```

(The existing line was `return session.store.put(result, source=f"tool:{tool_name}").summary()`; `size` is already computed just above the cap check.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_status_instrumentation.py -q`
Expected: PASS (3 passed).

- [ ] **Step 7: Full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/tools/documents.py harness/tools/code.py harness/spill.py tests/test_status_instrumentation.py`
Expected: all pass; `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add harness/tools/documents.py harness/tools/code.py harness/spill.py tests/test_status_instrumentation.py
git commit -m "feat(status): instrument read_document, run_python, and the spill path"
```

---

## Task 5: Instrument the web/fetch tools

**Files:**
- Modify: `harness/tools/fetch.py`, `harness/tools/web.py`
- Test: `tests/test_status_instrumentation.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_status_instrumentation.py`:

```python
import httpx

from harness.tools.fetch import fetch_url
from harness.tools.web import web_extract, web_search


def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_url_emits_fetching_status(tmp_path):
    sess = _session(tmp_path)
    bus, events = _collect()

    def handler(request):
        return httpx.Response(200, text="hello", headers={"content-type": "text/plain"})

    with bind_bus(bus):
        fetch_url(sess, "https://example.com/x", client=_mock_client(handler))
    assert any(e.tool == "fetch_url" and "example.com" in e.message for e in events)


def test_web_search_emits_searching_status(tmp_path):
    sess = _session(tmp_path)
    sess.config.search.api_key = "test-key"
    bus, events = _collect()

    def handler(request):
        return httpx.Response(200, json={"answer": None, "results": []})

    with bind_bus(bus):
        web_search(sess, "model pricing", client=_mock_client(handler))
    assert any(e.tool == "web_search" and "pricing" in e.message for e in events)


def test_web_extract_emits_extracting_status(tmp_path):
    sess = _session(tmp_path)
    sess.config.search.api_key = "test-key"
    bus, events = _collect()

    def handler(request):
        return httpx.Response(200, json={"results": [{"raw_content": "body", "url": "https://e/x"}]})

    with bind_bus(bus):
        web_extract(sess, "https://e/x", client=_mock_client(handler))
    assert any(e.tool == "web_extract" for e in events)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_status_instrumentation.py -k "fetch or web" -q`
Expected: FAIL — no status events from these tools yet.

- [ ] **Step 3: Instrument `fetch_url`**

In `harness/tools/fetch.py`, add the import (after `from ..session import Session`):

```python
from ..status import report_progress
```

In `fetch_url`, add the report right after the scheme check (after the `raise ValueError(... not allowed ...)` block, before `owns_client = client is None`):

```python
    report_progress(f"fetching {url}", tool="fetch_url")
    owns_client = client is None
```

- [ ] **Step 4: Instrument `web_search` and `web_extract`**

In `harness/tools/web.py`, add the import (after `from ..session import Session`):

```python
from ..status import report_progress
```

In `web_search`, add the report right after the `if not cfg.api_key:` early-return block (before `owns = client is None`):

```python
    report_progress(f"searching: {query}", tool="web_search")
    owns = client is None
```

In `web_extract`, add an equivalent report right after its own `if not cfg.api_key:` early-return block (before its `owns = client is None`):

```python
    report_progress(f"extracting {url}", tool="web_extract")
    owns = client is None
```

(If `web_extract` does not early-return on a missing key, add the report as the first statement after the docstring instead.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_status_instrumentation.py -q`
Expected: PASS (6 passed).

- [ ] **Step 6: Full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/tools/fetch.py harness/tools/web.py tests/test_status_instrumentation.py`
Expected: all pass; `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add harness/tools/fetch.py harness/tools/web.py tests/test_status_instrumentation.py
git commit -m "feat(status): instrument fetch_url, web_search, and web_extract"
```

---

## Task 6: CLI `--verbose` becomes a status printer sink

**Files:**
- Modify: `harness/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py` (it already exercises `run_cli`; reuse its existing imports for `StubChatClient`/`text`/`tool_call`, or add `from harness.testing import StubChatClient, text, tool_call`):

```python
from harness.cli import make_status_printer
from harness.status import StatusEvent


def test_status_printer_formats_event():
    lines = []
    printer = make_status_printer(write=lines.append)
    printer(StatusEvent(tool="read_document", message="converting d.pdf via Docling",
                        current=1, total=4, seq=1, timestamp=1.0))
    assert lines == ["→ read_document: converting d.pdf via Docling [1/4]"]


def test_verbose_prints_tool_status_to_stderr(tmp_path, capsys):
    from harness.cli import run_cli

    client = StubChatClient([
        tool_call("run_python", {"code": "from harness_sandbox import emit\nemit(1)\n"}),
        text("done"),
    ])
    code = run_cli(["go", "-v", "--root", str(tmp_path / "r")], client=client)
    err = capsys.readouterr().err
    assert code == 0
    assert "run_python" in err                            # the instrumented tool reported
    assert "temporarily unavailable" not in err           # old notice is gone
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli.py -k "status or verbose" -q`
Expected: FAIL — `make_status_printer` does not exist; the old `-v` notice is still printed.

- [ ] **Step 3: Rewrite the CLI verbose path**

In `harness/cli.py`, replace the helpers `_short` and `make_verbose_printer` (the deferred tool-call reporter) with a status printer:

```python
def make_status_printer(write=None):
    """A status sink for --verbose: formats each StatusEvent to a line."""
    if write is None:
        def write(line: str) -> None:
            print(line, file=sys.stderr)

    def on_status(event) -> None:
        progress = ""
        if event.current is not None and event.total is not None:
            progress = f" [{event.current:g}/{event.total:g}]"
        write(f"→ {event.tool}: {event.message}{progress}")

    return on_status
```

Change `run_cli` to register the printer instead of the "temporarily unavailable" notice:

```python
def run_cli(argv: list[str] | None = None, client=None) -> int:
    args = build_parser().parse_args(argv)
    on_status = make_status_printer() if args.verbose else None
    cfg = HarnessConfig(model=args.model,
                        root_dir=Path(args.root) if args.root else None)
    result = Harness(cfg, client=client).solve(args.problem, on_status=on_status)
    if result.final_text:
        print(result.final_text)
    if result.error:
        print(f"\n[run did not complete: {result.error}]")
    print(f"\n[session: {result.session_dir}]")
    return 1 if result.error else 0
```

Remove the now-unused `import textwrap` and `from typing import Any` lines if nothing else uses them (ruff will flag unused imports — delete whatever it reports for this file).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/cli.py tests/test_cli.py`
Expected: all pass; `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add harness/cli.py tests/test_cli.py
git commit -m "feat(status): --verbose prints live tool status (replaces the deferred notice)"
```

---

## Self-Review (completed during plan authoring)

**Spec coverage** (`2026-06-10-tool-status-updates-design.md`):
- `StatusEvent` (tool/message/current/total/seq/timestamp) — Task 1. ✓
- `StatusBus` (subscribe→unsubscribe handle; thread-safe emit; seq+timestamp stamping via `dataclasses.replace`; raising-subscriber swallow) — Task 1. ✓
- `report_progress` (contextvar lookup; no-op when unbound; `tool="tool"` default) — Task 1. ✓
- `bind_bus`/`current_bus` — Task 1; used by Session — Task 2. ✓
- Session owns the bus, `subscribe()`, binds across the run via `__aenter__`/`__aexit__` — Task 2. ✓
- `Harness(on_status=…)`, `asolve`/`solve`/module `solve(on_status=…)`, subscribe before run — Task 3. ✓
- Built-in tool instrumentation (read_document, run_python, fetch_url, web_search, web_extract) + spill path — Tasks 4–5. ✓
- CLI `--verbose` printer sink; old notice removed — Task 6. ✓
- Threading: worker-thread emit test (Task 1) + sync tools run in threads (Tasks 3–5 run real tools). ✓
- Error handling: raising subscriber swallowed (Task 1); unbound no-op (Tasks 1–2). ✓
- Offline tests only (StubChatClient, MockTransport, injected converter; no model/network). ✓

**Placeholder scan:** none — every step has concrete code/commands. The one conditional ("If `web_extract` does not early-return…") is a guarded, fully-specified fallback, not a placeholder.

**Type/name consistency:** `StatusEvent`, `StatusBus.subscribe/emit`, `report_progress(message, *, current, total, tool)`, `bind_bus`, `current_bus`, `make_status_printer(write=…)`, and `Harness(..., on_status=…)` / `solve(..., on_status=…)` are used identically across every task and test.

**Deferred (not in this plan, per spec):** MCP status capture (Phase 2) and an AG-UI adapter sink.
