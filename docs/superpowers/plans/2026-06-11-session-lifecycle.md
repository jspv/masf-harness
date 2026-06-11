# Session Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decouple session lifecycle from a single call: one-shot `solve()` becomes ephemeral (self-cleaning); continuous interactions get a persistent `Conversation` (workspace + reused MAF `AgentSession`) held by a `SessionManager` until closed or expired.

**Architecture:** A `Conversation` bundles one workspace `Session` (kept open across turns) + the agent + a MAF `AgentSession`; `aask()` is `agent.run(q, session=agent_session)` (MAF threads history) and reuses the store so handles persist. A `SessionManager` registers continuous conversations by id (isolated per-id roots), with lazy TTL. `solve` opens an ephemeral conversation and reaps it; `agui_stream` resolves `threadId` to a persistent conversation. (Spec: `docs/superpowers/specs/2026-06-11-session-lifecycle-design.md`. Verified by spike: the harness agent threads history across `run(session=…)`.)

**Tech Stack:** Python 3.12 (`asyncio`, `dataclasses`, `uuid`), Microsoft Agent Framework (`AgentSession`), pytest with `StubChatClient` (offline). Continuous is async-first in v1.

---

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `harness/handles.py` | Modify | Persist `handles/_manifest.json` on `put`/`register`; rehydrate (manifest + id counter) on init |
| `harness/conversation.py` | **Create** | `Conversation`: workspace Session + agent + MAF AgentSession; `acreate`/`aask`/`aclose`; single-flight; reap-on-close |
| `harness/manager.py` | **Create** | `ConversationStore` (interface + in-memory) + `SessionManager` (open-or-create by id, get, close, lazy TTL, sweep, isolated per-id roots) |
| `harness/config.py` | Modify | `HarnessConfig.idle_ttl_s: float | None = None` |
| `harness/api.py` | Modify | `asolve(keep=False)` ephemeral default + `solve(keep=…)`; `Harness.aopen()`; `agui_stream` via the manager |
| `harness/__init__.py` | Modify | Export `Conversation`, `SessionManager` |
| `tests/test_handles.py`, `tests/test_conversation.py`, `tests/test_manager.py`, `tests/test_api.py`, `tests/test_agui.py` | Create/Modify | Offline tests |
| `README.md` | Modify | Document one-shot vs continuous lifecycle |

Existing types reused: `Result(final_text, handles, files, session_dir, error)` (`api.py`); `Session` (`session.py`); `HandleStore.register/put/manifest` (`handles.py`).

---

## Task 1: Handle manifest persistence + rehydration

**Files:** Modify `harness/handles.py`; Test `tests/test_handles.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_handles.py`:

```python
def test_handle_store_rehydrates_manifest_and_counter(tmp_path):
    from harness.handles import HandleStore
    s1 = HandleStore(tmp_path)
    h1 = s1.put({"a": 1}, source="t")
    h2 = s1.put("hello", source="t")
    assert (h1.id, h2.id) == ("h1", "h2")

    s2 = HandleStore(tmp_path)                       # new store, same root -> rehydrate
    assert set(s2.manifest().keys()) == {"h1", "h2"}
    assert s2.get("h1") == {"a": 1}                  # backing file still readable
    assert s2.put("again", source="t").id == "h3"    # id counter resumed (no h1 collision)


def test_rehydrate_skips_corrupt_record(tmp_path):
    import json
    from harness.handles import HandleStore
    HandleStore(tmp_path).put({"a": 1}, source="t")   # valid h1, persists manifest
    mf = tmp_path / "handles" / "_manifest.json"
    data = json.loads(mf.read_text())
    data["bad"] = {"id": "bad"}                        # missing required Handle fields
    mf.write_text(json.dumps(data))
    s2 = HandleStore(tmp_path)                         # must not raise
    assert "h1" in s2.manifest() and "bad" not in s2.manifest()
```

- [ ] **Step 2: Run, verify fail**

Run: `.venv/bin/python -m pytest tests/test_handles.py -k "rehydrate or manifest_and_counter" -q`
Expected: FAIL — the second `HandleStore` starts empty (no rehydration yet).

- [ ] **Step 3: Implement in `harness/handles.py`**

In `HandleStore.__init__`, after `self._counter = 0`, add a load call:
```python
        self._load_manifest()
```

Add `self._save_manifest()` as the last line of `put()` before `return handle`:
```python
        self._handles[hid] = handle
        self._save_manifest()
        return handle
```

Replace the existing `register` method with a save-wrapping public method plus a private no-save worker (the load path must not re-save). Replace:
```python
    def register(self, record: dict[str, Any]) -> Handle:
        """Register a handle whose file already exists (e.g. written by the sandbox child).

        The ``path`` is supplied by the lower-trust child over the jsonl channel, so it
        is run through ``safe_path``: a record pointing outside the root is rejected here
        rather than letting the trusted parent later read an arbitrary file.
        """
        try:
            handle = Handle(**record)
        except TypeError as e:  # cross-process contract boundary — give a useful message
            raise ValueError(f"invalid handle record {record!r}: {e}") from e
        try:
            safe_path(self.root, handle.path)
        except PathEscapesRootError as e:
            raise ValueError(f"handle record path escapes root: {record!r}") from e
        self._handles[handle.id] = handle
        self._advance_counter(handle.id)
        return handle
```
with:
```python
    def _register_record(self, record: dict[str, Any]) -> Handle:
        """Register a handle whose file already exists (sandbox child, or manifest rehydration).

        The ``path`` is supplied by lower-trust input, so it is run through ``safe_path``: a
        record pointing outside the root is rejected here rather than read later.
        """
        try:
            handle = Handle(**record)
        except TypeError as e:  # contract boundary — give a useful message
            raise ValueError(f"invalid handle record {record!r}: {e}") from e
        try:
            safe_path(self.root, handle.path)
        except PathEscapesRootError as e:
            raise ValueError(f"handle record path escapes root: {record!r}") from e
        self._handles[handle.id] = handle
        self._advance_counter(handle.id)
        return handle

    def register(self, record: dict[str, Any]) -> Handle:
        """Register a handle whose file already exists; persists the manifest."""
        handle = self._register_record(record)
        self._save_manifest()
        return handle

    @property
    def _manifest_file(self) -> Path:
        return self.dir / "_manifest.json"

    def _save_manifest(self) -> None:
        """Persist {id: summary} atomically so a new HandleStore on this root can rehydrate."""
        tmp = self.dir / "_manifest.json.tmp"
        tmp.write_text(json.dumps(self.manifest()), encoding="utf-8")
        tmp.replace(self._manifest_file)

    def _load_manifest(self) -> None:
        """Restore handles + the id counter from a prior session on this root. Tolerant:
        a corrupt record is skipped, not fatal."""
        if not self._manifest_file.exists():
            return
        try:
            records = json.loads(self._manifest_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for record in records.values():
            try:
                self._register_record(record)
            except (ValueError, KeyError):
                continue
```

- [ ] **Step 4: Run tests + lint + full suite**

Run: `.venv/bin/python -m pytest tests/test_handles.py -q`
Expected: pass.
Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/handles.py tests/test_handles.py`
Expected: all pass (existing sandbox/handle tests stay green — `register` is behavior-compatible); `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add harness/handles.py tests/test_handles.py
git commit -m "feat(handles): persist + rehydrate the handle manifest (id counter resumes)"
```

---

## Task 2: `Conversation` — a stateful multi-turn handle

**Files:** Create `harness/conversation.py`; Test `tests/test_conversation.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_conversation.py`:

```python
import asyncio

from harness import HarnessConfig
from harness.conversation import Conversation
from harness.testing import StubChatClient, text, tool_call


def _save(hid, value):
    return tool_call("run_python", {"code": f"from harness_sandbox import save\nsave({hid!r}, {value!r})\n"})


def test_conversation_persists_workspace_across_turns(tmp_path):
    # StubChatClient consumes its script linearly across run() calls, so 4 steps = 2 turns.
    client = StubChatClient([
        _save("h1", {"x": 1}), text("saved"),
        tool_call("run_python", {"code": "from harness_sandbox import load, save\nsave('h2', load('h1'))\n"}),
        text("done"),
    ])

    async def run():
        conv = await Conversation.acreate(id="c1", config=HarnessConfig(root_dir=tmp_path / "r"),
                                          client=client, tools=[], bundles=("code",))
        r1 = await conv.aask("save it")
        r2 = await conv.aask("reload it")            # turn 2 reuses the same workspace
        # turn 2 could load h1 (saved in turn 1) and write h2 from it -> proves persistence
        loaded_ok = "h2" in r2.handles and conv.session.store.get("h2") == {"x": 1}
        await conv.aclose()
        return r1, r2, loaded_ok, conv

    r1, r2, loaded_ok, conv = asyncio.run(run())
    assert "h1" in r1.handles
    assert loaded_ok                                 # turn 2 read turn-1's handle from the persistent store
    assert not (tmp_path / "r").exists()             # reaped on close (reap_on_close default True)


def test_conversation_keep_on_close(tmp_path):
    client = StubChatClient([text("hi")])

    async def run():
        conv = await Conversation.acreate(id="c2", config=HarnessConfig(root_dir=tmp_path / "k"),
                                          client=client, tools=[], bundles=(), reap_on_close=False)
        await conv.aask("hello")
        root = conv.session.root
        await conv.aclose()
        return root

    root = asyncio.run(run())
    assert root.exists()                             # retained when reap_on_close=False


def test_conversation_error_is_non_fatal(tmp_path):
    class _Boom(StubChatClient):
        async def _inner_get_response(self, *, messages, stream, options, **kwargs):
            raise RuntimeError("model boom")

    async def run():
        conv = await Conversation.acreate(id="c3", config=HarnessConfig(root_dir=tmp_path / "e"),
                                          client=_Boom([text("x")]), tools=[], bundles=())
        r = await conv.aask("go")                    # error captured, conversation survives
        ok = r.error is not None and "RuntimeError" in r.error
        await conv.aclose()
        return ok

    assert asyncio.run(run())
```

- [ ] **Step 2: Run, verify fail**

Run: `.venv/bin/python -m pytest tests/test_conversation.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.conversation'`.

- [ ] **Step 3: Create `harness/conversation.py`**

```python
"""Conversation: a stateful, multi-turn handle over a persistent workspace.

It keeps one workspace ``Session`` open across turns (so handles persist) and reuses one MAF
``AgentSession`` (so conversation history threads, MAF-managed). ``aask`` runs one turn under a
single-flight lock; ``aclose`` tears down MCP/bus and reaps the workspace unless ``reap_on_close``
is False. Async-first: a persistent agent + MCP connections require one stable event loop.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from typing import Any

from .config import HarnessConfig
from .session import Session


class Conversation:
    def __init__(self, conv_id: str, session: Session, agent: Any, agent_session: Any,
                 *, reap_on_close: bool = True) -> None:
        self.id = conv_id
        self.session = session
        self.agent = agent
        self.agent_session = agent_session
        self.reap_on_close = reap_on_close
        self.last_activity = time.monotonic()
        self._lock = asyncio.Lock()

    @classmethod
    async def acreate(cls, *, id: str, config: HarnessConfig, client: Any,
                      tools: list | None = None, bundles: tuple[str, ...] = ("code", "files", "web"),
                      reap_on_close: bool = True) -> "Conversation":
        """Open the workspace, build the agent once, start a MAF conversation thread."""
        session = Session.create(config)
        await session.__aenter__()
        try:
            agent = await session.create_agent(client, agent_instructions=None,
                                                tools=tools or [], bundles=bundles)
            agent_session = agent.create_session(session_id=id)
        except BaseException:
            await session.__aexit__(None, None, None)
            raise
        return cls(id, session, agent, agent_session, reap_on_close=reap_on_close)

    async def aask(self, question: str) -> Any:
        """Run one turn (serialized per conversation). Returns a Result; errors are captured."""
        from .api import Result

        async with self._lock:
            final_text, error = "", None
            try:
                response = await self.agent.run(question, session=self.agent_session)
                final_text = response.text
            except Exception as e:  # noqa: BLE001 - surface, don't crash the conversation
                error = f"{type(e).__name__}: {e}"
            self.last_activity = time.monotonic()
            return Result(final_text=final_text, handles=dict(self.session.handles),
                          files=self.session.artifacts, session_dir=self.session.root, error=error)

    async def aclose(self) -> None:
        """Tear down the workspace (MCP + bus), then reap the root unless retained. Idempotent."""
        try:
            await self.session.__aexit__(None, None, None)
        finally:
            if self.reap_on_close and self.session.root.exists():
                shutil.rmtree(self.session.root, ignore_errors=True)
```

- [ ] **Step 4: Run tests + lint + full suite**

Run: `.venv/bin/python -m pytest tests/test_conversation.py -q`
Expected: 3 passed.
Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/conversation.py tests/test_conversation.py`
Expected: all pass; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add harness/conversation.py tests/test_conversation.py
git commit -m "feat(conversation): persistent multi-turn Conversation (workspace + MAF AgentSession)"
```

---

## Task 3: `SessionManager` — registry + lazy TTL

**Files:** Create `harness/manager.py`; Test `tests/test_manager.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_manager.py`:

```python
import asyncio

from harness import HarnessConfig, Harness
from harness.manager import SessionManager
from harness.testing import StubChatClient, text


def _harness(tmp_path):
    return Harness(HarnessConfig(root_dir=tmp_path / "base"), client=StubChatClient([text("x")]))


def test_open_is_reuse_or_create_and_isolated_roots(tmp_path):
    async def run():
        m = SessionManager(_harness(tmp_path))
        a = await m.aopen("t1")
        a2 = await m.aopen("t1")          # same id -> same Conversation
        b = await m.aopen("t2")           # different id -> different Conversation + root
        assert a is a2
        assert b is not a
        assert a.session.root != b.session.root      # isolated per-id roots
        await m.aclose()
        return a, b

    a, b = asyncio.run(run())


def test_get_and_close_are_idempotent(tmp_path):
    async def run():
        m = SessionManager(_harness(tmp_path))
        c = await m.aopen("t1")
        assert m.get("t1") is c
        await m.close("t1")
        assert m.get("t1") is None
        await m.close("t1")               # idempotent: no error
    asyncio.run(run())


def test_lazy_ttl_expiry(tmp_path):
    async def run():
        m = SessionManager(_harness(tmp_path), idle_ttl_s=60)
        c1 = await m.aopen("t1")
        c1.last_activity -= 1000          # simulate idle past the TTL
        assert m.get("t1") is None        # lazy expiry on get
        c2 = await m.aopen("t1")          # open re-creates a fresh conversation
        assert c2 is not c1
        await m.aclose()
    asyncio.run(run())


def test_sweep_reaps_idle(tmp_path):
    async def run():
        m = SessionManager(_harness(tmp_path), idle_ttl_s=60)
        c = await m.aopen("t1")
        c.last_activity -= 1000
        await m.sweep()
        assert m.get("t1") is None
    asyncio.run(run())
```

- [ ] **Step 2: Run, verify fail**

Run: `.venv/bin/python -m pytest tests/test_manager.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.manager'`.

- [ ] **Step 3: Create `harness/manager.py`**

```python
"""SessionManager: an in-process registry of continuous Conversations, keyed by id.

Open-or-create by id (e.g. an AG-UI threadId), get, close, lazy TTL expiry, and a sweep the host
can call on its own cadence. Each conversation gets an isolated per-id root so concurrent
conversations never collide. The ConversationStore seam lets a disk-backed impl drop in later.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from .conversation import Conversation


class ConversationStore(Protocol):
    def get(self, conv_id: str) -> Conversation | None: ...
    def put(self, conv_id: str, conv: Conversation) -> None: ...
    def pop(self, conv_id: str) -> Conversation | None: ...
    def items(self) -> list[tuple[str, Conversation]]: ...


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._d: dict[str, Conversation] = {}

    def get(self, conv_id: str) -> Conversation | None:
        return self._d.get(conv_id)

    def put(self, conv_id: str, conv: Conversation) -> None:
        self._d[conv_id] = conv

    def pop(self, conv_id: str) -> Conversation | None:
        return self._d.pop(conv_id, None)

    def items(self) -> list[tuple[str, Conversation]]:
        return list(self._d.items())


def _conv_root(config: Any, conv_id: str) -> Path:
    """Isolated per-conversation root. Uses config.root_dir as a base, else ./.harness/sessions."""
    base = Path(config.root_dir) if config.root_dir else (Path.cwd() / ".harness" / "sessions")
    return base / conv_id


class SessionManager:
    def __init__(self, harness: Any, *, idle_ttl_s: float | None = None,
                 store: ConversationStore | None = None) -> None:
        self._harness = harness
        self._ttl = idle_ttl_s
        self._store: ConversationStore = store or InMemoryConversationStore()
        self._lock = asyncio.Lock()

    def _expired(self, conv: Conversation) -> bool:
        return self._ttl is not None and (time.monotonic() - conv.last_activity) > self._ttl

    async def aopen(self, session_id: str | None = None, *, tools: list | None = None,
                    bundles: tuple[str, ...] | None = None) -> Conversation:
        """Reuse the live conversation for ``session_id`` (if any, not expired), else create one."""
        async with self._lock:
            if session_id is not None:
                existing = self._store.get(session_id)
                if existing is not None and not self._expired(existing):
                    return existing
                if existing is not None:               # expired -> reap before recreating
                    self._store.pop(session_id)
                    await existing.aclose()
            conv_id = session_id or uuid.uuid4().hex
            config = dataclasses.replace(
                self._harness.config, root_dir=str(_conv_root(self._harness.config, conv_id)))
            conv = await Conversation.acreate(
                id=conv_id, config=config, client=self._harness._make_client(),
                tools=self._harness._tools + (tools or []),
                bundles=bundles if bundles is not None else self._harness._bundles,
                reap_on_close=True,
            )
            self._store.put(conv_id, conv)
            return conv

    def get(self, session_id: str) -> Conversation | None:
        conv = self._store.get(session_id)
        if conv is None or self._expired(conv):
            return None
        return conv

    async def close(self, session_id: str) -> None:
        conv = self._store.pop(session_id)
        if conv is not None:
            await conv.aclose()

    async def sweep(self) -> None:
        for conv_id, conv in self._store.items():
            if self._expired(conv):
                self._store.pop(conv_id)
                await conv.aclose()

    async def aclose(self) -> None:
        for conv_id, conv in self._store.items():
            self._store.pop(conv_id)
            await conv.aclose()
```

- [ ] **Step 4: Run tests + lint + full suite**

Run: `.venv/bin/python -m pytest tests/test_manager.py -q`
Expected: 4 passed.
Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/manager.py tests/test_manager.py`
Expected: all pass; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add harness/manager.py tests/test_manager.py
git commit -m "feat(manager): SessionManager registry with isolated per-id roots + lazy TTL"
```

---

## Task 4: `Harness` wiring — ephemeral one-shot + `aopen`

**Files:** Modify `harness/config.py`, `harness/api.py`, `harness/__init__.py`; Test `tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api.py`:

```python
def test_solve_is_ephemeral_by_default(tmp_path):
    client = StubChatClient([text("the answer")])
    h = Harness(HarnessConfig(root_dir=tmp_path / "r"), client=client)
    result = h.solve("go")
    assert result.final_text == "the answer"
    assert not (tmp_path / "r").exists()             # one-shot reaped its workspace


def test_solve_keep_retains_workspace(tmp_path):
    client = StubChatClient([text("the answer")])
    h = Harness(HarnessConfig(root_dir=tmp_path / "k"), client=client)
    result = h.solve("go", keep=True)
    assert (tmp_path / "k").exists()                 # audit trail retained
    assert result.session_dir == tmp_path / "k"


def test_aopen_returns_persistent_conversation(tmp_path):
    import asyncio

    async def run():
        h = Harness(HarnessConfig(root_dir=tmp_path / "base"), client=StubChatClient([text("x")]))
        conv = await h.aopen("t1")
        same = await h.aopen("t1")
        assert conv is same                          # open-or-create via the manager
        await h.aclose_sessions()
        return conv

    asyncio.run(run())
```

- [ ] **Step 2: Run, verify fail**

Run: `.venv/bin/python -m pytest tests/test_api.py -k "ephemeral or keep_retains or aopen" -q`
Expected: FAIL — `solve` keeps the dir; `aopen`/`aclose_sessions` don't exist.

- [ ] **Step 3: Add `idle_ttl_s` to `harness/config.py`**

In the `HarnessConfig` dataclass, add this field after `cleanup`:
```python
    idle_ttl_s: float | None = None  # continuous-session idle TTL (None = never expire)
```

- [ ] **Step 4: Wire the manager + ephemeral one-shot into `harness/api.py`**

In `Harness.__init__`, after `self._on_status = on_status`, add a lazily-created manager holder:
```python
        self._manager = None
```

Add a manager accessor + lifecycle helpers as methods on `Harness` (place after `_make_client`):
```python
    def _sessions(self):
        if self._manager is None:
            from .manager import SessionManager
            self._manager = SessionManager(self, idle_ttl_s=self.config.idle_ttl_s)
        return self._manager

    async def aopen(self, session_id: str | None = None, *, tools: list | None = None):
        """Open (or resume) a persistent continuous Conversation by id."""
        return await self._sessions().aopen(session_id, tools=tools)

    async def aclose_sessions(self) -> None:
        """Close every live continuous Conversation (host shutdown)."""
        if self._manager is not None:
            await self._manager.aclose()

    async def sweep_sessions(self) -> None:
        """Reap idle continuous Conversations past their TTL."""
        if self._manager is not None:
            await self._manager.sweep()
```

Rewrite `asolve` to run a one-shot through an ephemeral `Conversation` (reaped unless `keep`):
```python
    async def asolve(self, problem: str, tools: list | None = None, *,
                     on_status: _StatusSink | None = None, keep: bool = False) -> Result:
        from .conversation import Conversation

        sink = on_status if on_status is not None else self._on_status
        conv = await Conversation.acreate(
            id="oneshot", config=self.config, client=self._make_client(),
            tools=self._tools + (tools or []), bundles=self._bundles, reap_on_close=not keep)
        if sink is not None:
            conv.session.subscribe(sink)
        try:
            return await conv.aask(problem)
        finally:
            await conv.aclose()
```

Update `solve` to thread `keep`:
```python
    def solve(self, problem: str, tools: list | None = None, *,
              on_status: _StatusSink | None = None, keep: bool = False) -> Result:
        return asyncio.run(self.asolve(problem, tools=tools, on_status=on_status, keep=keep))
```

And the module-level `solve`:
```python
def solve(problem: str, *, tools: list | None = None,
          config: HarnessConfig | None = None, client: Any | None = None,
          on_status: _StatusSink | None = None, keep: bool = False) -> Result:
    """One-shot convenience: build a Harness and solve a single problem (ephemeral unless keep)."""
    return Harness(config, client=client, tools=tools, on_status=on_status).solve(problem, keep=keep)
```

- [ ] **Step 5: Export from the package**

In `harness/__init__.py`, add after the `from .session import Session` line:
```python
from .conversation import Conversation
from .manager import SessionManager
```
and add `"Conversation", "SessionManager",` to `__all__`.

- [ ] **Step 6: Run tests + lint + full suite**

Run: `.venv/bin/python -m pytest tests/test_api.py -q`
Expected: pass (incl. the existing solve/asolve/on_status tests — `asolve` still returns the same `Result` shape).
Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/api.py harness/config.py harness/__init__.py tests/test_api.py`
Expected: all pass; `All checks passed!`. Confirm `.venv/bin/python -c "from harness import Conversation, SessionManager; print('ok')"`.

- [ ] **Step 7: Commit**

```bash
git add harness/api.py harness/config.py harness/__init__.py tests/test_api.py
git commit -m "feat(api): ephemeral one-shot solve (keep opt-out) + Harness.aopen/sweep/close sessions"
```

---

## Task 5: `agui_stream` via the manager (persistent per-thread workspace)

**Files:** Modify `harness/api.py`; Test `tests/test_agui.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agui.py` (offline — drives the agent via `StubChatClient`, asserts the *workspace* persists across two `agui_stream` calls with the same threadId; AG-UI history stays with the wrapper):

```python
def test_agui_stream_reuses_workspace_per_thread(tmp_path):
    import asyncio

    from harness import Harness, HarnessConfig
    from harness.testing import StubChatClient, text, tool_call

    client = StubChatClient([
        tool_call("run_python", {"code": "from harness_sandbox import save\nsave('h1', {'x': 1})\n"}),
        text("saved"),
        tool_call("run_python", {"code": "from harness_sandbox import load, save\nsave('h2', load('h1'))\n"}),
        text("done"),
    ])
    h = Harness(HarnessConfig(root_dir=tmp_path / "base"), client=client, bundles=("code",))

    async def run():
        async for _ in h.agui_stream({"messages": [{"role": "user", "content": "save"}], "threadId": "t1"}):
            pass
        async for _ in h.agui_stream({"messages": [{"role": "user", "content": "reload"}], "threadId": "t1"}):
            pass
        conv = h._sessions().get("t1")               # same workspace reused across both calls
        ok = conv is not None and "h1" in conv.session.handles and conv.session.store.get("h2") == {"x": 1}
        await h.aclose_sessions()
        return ok

    assert asyncio.run(run())
```

- [ ] **Step 2: Run, verify fail**

Run: `.venv/bin/python -m pytest tests/test_agui.py -k reuses_workspace -q`
Expected: FAIL — `agui_stream` builds a throwaway Session per call, so `h2`/`load('h1')` can't see turn-1's handle, and `_sessions().get("t1")` is `None`.

- [ ] **Step 3: Rewrite `agui_stream` in `harness/api.py`**

Replace the body of `agui_stream` (the `async with Session.create(...)` block) so it resolves the threadId to a persistent `Conversation` and reuses its workspace + agent:
```python
    async def agui_stream(self, input_data: dict, *, tools: list | None = None,
                          **agui_kwargs: Any) -> AsyncIterator[Any]:
        """Yield AG-UI events for one request, reusing a persistent workspace per ``threadId``.

        The workspace (handles, sandbox files) persists across turns; conversation history stays
        with the official AG-UI wrapper (CopilotKit replays ``messages``). The thread's workspace
        is reaped only by ``close``/TTL, never at end-of-request. Requires the ``agui`` extra.
        """
        from .agui import agui_event_stream

        thread_id = input_data.get("threadId") or input_data.get("thread_id")
        conv = await self._sessions().aopen(thread_id, tools=tools)
        async for event in agui_event_stream(conv.agent, conv.session.status_bus, input_data,
                                             **agui_kwargs):
            yield event
```

- [ ] **Step 4: Run tests + lint + full suite**

Run: `.venv/bin/python -m pytest tests/test_agui.py -q`
Expected: pass (the existing `agui` offline tests + the new one; gated live tests skipped).
Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/api.py tests/test_agui.py`
Expected: all pass; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add harness/api.py tests/test_agui.py
git commit -m "feat(agui): agui_stream reuses a persistent workspace per threadId via SessionManager"
```

---

## Task 6: Documentation

**Files:** Modify `README.md`

- [ ] **Step 1: Add a "Sessions: one-shot vs continuous" subsection**

READ `README.md`. Insert a new `### Sessions: one-shot vs continuous` subsection at the end of the `### Library` section (just before `### MCP servers`). Use ordinary triple backticks. Content:

A short intro: the harness serves both one-shot and continuous interactions. Then:

```markdown
- **One-shot** (`solve`) is ephemeral — it runs and **cleans up its workspace** when done. Pass `keep=True` to retain the audit trail (`result.session_dir`).
- **Continuous** keeps a persistent workspace (handles + sandbox files) and conversation history across turns, until you close it (or an optional idle TTL expires).
```

Then a python fenced block:

```python
import asyncio
from harness import Harness, HarnessConfig

async def main():
    h = Harness(HarnessConfig(idle_ttl_s=1800))     # idle conversations expire after 30 min (optional)
    conv = await h.aopen("thread-42")                # open or resume by id (e.g. an AG-UI threadId)
    print((await conv.aask("load sales.csv and summarize")).final_text)
    print((await conv.aask("now filter to EU")).final_text)   # sees the prior turn's handles + history
    await conv.aclose()                              # reap this conversation's workspace
    await h.aclose_sessions()                        # (host shutdown) close any remaining
    # await h.sweep_sessions()  # reap idle conversations on your own cadence

asyncio.run(main())
```

Then a closing line: AG-UI hosts get this automatically — `agui_stream` maps each request's `threadId` to a persistent conversation, so handles and files persist across turns (history stays in the AG-UI message replay).

- [ ] **Step 2: Update Project layout + Status & roadmap**

In `## Project layout`, after the `session.py` line add:
```
  conversation.py  persistent multi-turn Conversation (workspace + MAF AgentSession)
  manager.py       SessionManager: continuous-session registry + lazy TTL
```

In `## Status & roadmap`, append to the `Implemented:` sentence: `, and a **session lifecycle** model (ephemeral one-shot `solve`; persistent continuous `aopen`/`aask`/`aclose` with optional idle TTL)`.

- [ ] **Step 3: Verify + commit**

Run: `.venv/bin/python -c "open('README.md').read(); print('ok')"`
```bash
git add README.md
git commit -m "docs(readme): document one-shot (ephemeral) vs continuous session lifecycle"
```

---

## Self-Review (completed during plan authoring)

**Spec coverage** (`2026-06-11-session-lifecycle-design.md`):
- Handle manifest persistence + rehydration (id counter resumes) — Task 1. ✓
- `Conversation` (Session + agent + MAF AgentSession; `aask`/`aclose`; single-flight; reap-on-close) — Task 2. ✓
- `SessionManager` + `ConversationStore` (in-memory), open-or-create, get, close, lazy TTL, sweep, isolated per-id roots — Task 3. ✓
- One-shot ephemeral default + `keep` opt-out; `Harness.aopen`/`aclose_sessions`/`sweep_sessions`; `idle_ttl_s` config — Task 4. ✓
- `agui_stream` via the manager (persistent workspace per threadId; history with the wrapper) — Task 5. ✓
- Async-first continuous (sync `ask` deferred per spec) — `aask`/`aclose`/`aopen` throughout. ✓
- MAF-native history (verified spike) — `agent.create_session` + `agent.run(session=…)` in Task 2. ✓
- Single-flight serialization — `asyncio.Lock` in `Conversation` (Task 2). ✓
- Tests: handle rehydration, multi-turn workspace persistence, ephemeral reap / keep, open-or-create, lazy TTL, sweep, agui per-thread reuse — Tasks 1–5. ✓
- Docs — Task 6. ✓

**Placeholder scan:** none — every step has concrete code/commands.

**Type/name consistency:** `Conversation.acreate(id=, config=, client=, tools=, bundles=, reap_on_close=)`, `aask`/`aclose`, `SessionManager.aopen(session_id, tools=, bundles=)`/`get`/`close`/`sweep`/`aclose`, `_conv_root`, `Harness.aopen`/`aclose_sessions`/`sweep_sessions`/`solve(keep=)`/`asolve(keep=)`, `HarnessConfig.idle_ttl_s`, and the reused `Result` shape are consistent across all tasks and match the spec.

**Deferred (per spec):** sync `conv.ask()` (background-loop); disk-backed/distributed `ConversationStore` + cross-process resume; a mandatory background sweeper.
