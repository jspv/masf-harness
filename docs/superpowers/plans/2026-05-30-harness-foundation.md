# Harness Foundation (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the LLM-agnostic foundation of the data-integration harness — a root-confined file jail, a typed handle store (JSON/text/dataframe), and a local subprocess Python sandbox that runs agent-written scripts with arguments — all under test-first development.

**Architecture:** Plain Python units with clean interfaces, usable without any LLM. `safe_path` is a single security chokepoint. `HandleStore` persists large data under a session root and hands back lightweight typed handles. `LocalSubprocessSandbox` executes script files in a child process (scrubbed env, `cwd=root`, rlimits, timeout) that talks to the same handle store via an injected `harness_sandbox` runtime helper. `Session` wires them together.

**Tech Stack:** Python 3.12, `uv`, `pytest`, `pandas` + `pyarrow` (dataframe handles), `resource` (POSIX rlimits). No network or API keys needed for any test in this plan.

**Scope note:** This plan is Phase 1 (foundation). The agent layer (spill middleware, agent tools, `Harness`/`solve()`, CLI) is Phase 2, a separate plan that builds on these units. Spec: `docs/superpowers/specs/2026-05-30-data-integration-harness-design.md`.

---

## File Structure

- Create: `harness/__init__.py` — package marker + foundation exports (grows in Phase 2)
- Create: `harness/config.py` — `HarnessConfig`, `SandboxConfig`, `FetchConfig`
- Create: `harness/paths.py` — `safe_path()` + `PathEscapesRootError` (security chokepoint)
- Create: `harness/handles.py` — `Handle`, `HandleStore`
- Create: `harness/runtime/harness_sandbox.py` — helper imported *inside* the subprocess (`load`/`save`/`emit`)
- Create: `harness/sandbox.py` — `SandboxExecutor` protocol, `ExecResult`, `LocalSubprocessSandbox`
- Create: `harness/session.py` — `Session`
- Create: `tests/` mirror: `test_paths.py`, `test_config.py`, `test_handles.py`, `test_sandbox.py`, `test_session.py`
- Modify: `pyproject.toml` — package name, deps, pytest config
- Delete: `main.py` (uv-init placeholder, unused)

---

## Task 0: Project scaffolding

**Files:**
- Modify: `pyproject.toml`
- Create: `harness/__init__.py`
- Delete: `main.py`

- [ ] **Step 1: Replace `pyproject.toml` with the real package config**

```toml
[project]
name = "harness"
version = "0.1.0"
description = "Data-integration agent harness on Microsoft Agent Framework"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "agent-framework-core",
    "agent-framework-openai",
    "pandas>=2.0",
    "pyarrow>=15.0",
    "httpx>=0.27",
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "ruff>=0.6",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"

[tool.hatch.build.targets.wheel]
packages = ["harness"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 2: Create the package marker**

Create `harness/__init__.py`:

```python
"""Data-integration agent harness."""

__version__ = "0.1.0"
```

- [ ] **Step 3: Remove the uv placeholder and a stray README requirement**

Run:
```bash
rm -f main.py
touch README.md
```

- [ ] **Step 4: Sync the environment and confirm pytest runs**

Run:
```bash
uv sync --prerelease=allow
uv run pytest
```
Expected: pytest runs and reports "no tests ran" (exit code 5) — environment is healthy.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: scaffold harness package and test tooling"
```

---

## Task 1: `safe_path` — the security chokepoint (most-tested unit)

**Files:**
- Create: `harness/paths.py`
- Test: `tests/test_paths.py`

- [ ] **Step 1: Write the failing tests** (exhaustive — this is the security boundary)

Create `tests/test_paths.py`:

```python
import os
from pathlib import Path

import pytest

from harness.paths import PathEscapesRootError, safe_path


def test_simple_relative_path_resolves_under_root(tmp_path):
    assert safe_path(tmp_path, "data.csv") == (tmp_path / "data.csv").resolve()


def test_subfolder_is_allowed(tmp_path):
    assert safe_path(tmp_path, "sub/dir/data.csv") == (tmp_path / "sub/dir/data.csv").resolve()


def test_root_itself_is_allowed(tmp_path):
    assert safe_path(tmp_path, ".") == tmp_path.resolve()


def test_dotdot_traversal_is_rejected(tmp_path):
    with pytest.raises(PathEscapesRootError):
        safe_path(tmp_path, "../escape.txt")


def test_nested_dotdot_escaping_root_is_rejected(tmp_path):
    with pytest.raises(PathEscapesRootError):
        safe_path(tmp_path, "sub/../../escape.txt")


def test_absolute_path_outside_root_is_rejected(tmp_path):
    with pytest.raises(PathEscapesRootError):
        safe_path(tmp_path, "/etc/passwd")


def test_symlink_pointing_outside_root_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside_secret"
    outside.mkdir()
    link = tmp_path / "link"
    os.symlink(outside, link)
    with pytest.raises(PathEscapesRootError):
        safe_path(tmp_path, "link/secret.txt")


def test_dotdot_that_stays_within_root_is_allowed(tmp_path):
    # sub/../data.csv normalizes back to root/data.csv — legal
    assert safe_path(tmp_path, "sub/../data.csv") == (tmp_path / "data.csv").resolve()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.paths'`

- [ ] **Step 3: Implement `safe_path`**

Create `harness/paths.py`:

```python
"""Single chokepoint that confines every model-supplied path to the session root."""

from __future__ import annotations

from pathlib import Path


class PathEscapesRootError(ValueError):
    """Raised when a candidate path resolves outside the session root."""


def safe_path(root: Path | str, candidate: Path | str) -> Path:
    """Resolve ``candidate`` against ``root`` and guarantee it stays inside it.

    ``.resolve()`` normalizes ``..`` and follows symlinks, so a symlink inside
    the root that points outside is caught here rather than exploited.
    """
    root_resolved = Path(root).resolve()
    p = Path(candidate)
    if not p.is_absolute():
        p = root_resolved / p
    resolved = p.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathEscapesRootError(f"path escapes root: {candidate!r}")
    return resolved
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_paths.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add harness/paths.py tests/test_paths.py
git commit -m "feat: add root-confining safe_path chokepoint with exhaustive tests"
```

---

## Task 2: Config dataclasses

**Files:**
- Create: `harness/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config.py`:

```python
from harness.config import FetchConfig, HarnessConfig, SandboxConfig


def test_defaults_are_sensible():
    cfg = HarnessConfig()
    assert cfg.model == "gpt-4o-mini"
    assert cfg.spill_threshold_bytes == 8192
    assert isinstance(cfg.sandbox, SandboxConfig)
    assert isinstance(cfg.fetch, FetchConfig)
    assert cfg.sandbox.timeout_s == 30.0
    assert "pandas" in cfg.sandbox.preinstalled
    assert cfg.fetch.allowed_schemes == ("http", "https")


def test_nested_configs_are_independent_between_instances():
    a = HarnessConfig()
    b = HarnessConfig()
    assert a.sandbox is not b.sandbox  # field(default_factory=...) not shared
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.config'`

- [ ] **Step 3: Implement the config dataclasses**

Create `harness/config.py`:

```python
"""Typed configuration for the harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SandboxConfig:
    timeout_s: float = 30.0
    max_memory_mb: int = 1024
    max_file_size_mb: int = 512
    confine_os: bool = False  # opt-in OS-level jail (sandbox-exec / bwrap); Phase 2+
    preinstalled: tuple[str, ...] = ("pandas", "pyarrow", "numpy", "httpx")


@dataclass
class FetchConfig:
    max_bytes: int = 10_000_000
    timeout_s: float = 30.0
    allowed_schemes: tuple[str, ...] = ("http", "https")


@dataclass
class HarnessConfig:
    model: str = "gpt-4o-mini"
    spill_threshold_bytes: int = 8192
    max_context_window_tokens: int = 128_000
    max_output_tokens: int = 4096
    root_dir: Path | None = None  # None -> a session dir is created under ./.harness/sessions/
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add harness/config.py tests/test_config.py
git commit -m "feat: add HarnessConfig/SandboxConfig/FetchConfig"
```

---

## Task 3: `Handle` + `HandleStore`

**Files:**
- Create: `harness/handles.py`
- Test: `tests/test_handles.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_handles.py`:

```python
import pandas as pd

from harness.handles import Handle, HandleStore


def test_put_and_get_json_roundtrip(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put({"a": 1, "b": [1, 2, 3]}, source="tool:x")
    assert h.kind == "json"
    assert store.get(h.id) == {"a": 1, "b": [1, 2, 3]}


def test_put_and_get_text_roundtrip(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put("hello world", source="tool:x")
    assert h.kind == "text"
    assert store.get(h.id) == "hello world"


def test_put_and_get_dataframe_roundtrip(tmp_path):
    store = HandleStore(tmp_path)
    df = pd.DataFrame({"x": [1, 2], "y": [3.0, 4.0]})
    h = store.put(df, source="tool:query")
    assert h.kind == "dataframe"
    assert h.n_rows == 2
    assert h.n_cols == 2
    assert h.schema == {"x": "int64", "y": "float64"}
    pd.testing.assert_frame_equal(store.get(h.id), df)


def test_handle_summary_omits_none_and_includes_preview(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put("abc", source="tool:x")
    summary = h.summary()
    assert summary["id"] == h.id
    assert summary["kind"] == "text"
    assert "preview" in summary
    assert "schema" not in summary  # None fields dropped


def test_ids_are_sequential_and_unique(tmp_path):
    store = HandleStore(tmp_path)
    h1 = store.put("a", source="s")
    h2 = store.put("b", source="s")
    assert (h1.id, h2.id) == ("h1", "h2")


def test_files_are_written_under_root_handles_dir(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put({"a": 1}, source="s")
    assert (tmp_path / h.path).exists()
    assert h.path.startswith("handles/")


def test_register_external_record_round_trips(tmp_path):
    # Simulates the subprocess helper having written a file + metadata record.
    store = HandleStore(tmp_path)
    (tmp_path / "handles").mkdir(exist_ok=True)
    (tmp_path / "handles" / "h7.txt").write_text("from child")
    rec = {"id": "h7", "kind": "text", "path": "handles/h7.txt",
           "source": "run_python", "bytes": 10, "preview": "from child"}
    h = store.register(rec)
    assert h.id == "h7"
    assert store.get("h7") == "from child"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_handles.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.handles'`

- [ ] **Step 3: Implement `Handle` + `HandleStore`**

Create `harness/handles.py`:

```python
"""Typed handles: large data lives on disk; only a lightweight summary enters context."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_PREVIEW_CHARS = 800
_PREVIEW_ROWS = 5


@dataclass
class Handle:
    id: str
    kind: str  # "json" | "text" | "dataframe"
    path: str  # POSIX path relative to the session root
    source: str
    bytes: int
    preview: str
    schema: dict[str, str] | None = None
    n_rows: int | None = None
    n_cols: int | None = None

    def summary(self) -> dict[str, Any]:
        """Context-facing view: drop None fields to keep it compact."""
        return {k: v for k, v in asdict(self).items() if v is not None}


class HandleStore:
    """Persists objects under ``<root>/handles`` and tracks them by id."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.dir = self.root / "handles"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._handles: dict[str, Handle] = {}
        self._counter = 0

    def _new_id(self) -> str:
        self._counter += 1
        return f"h{self._counter}"

    @staticmethod
    def _detect_kind(obj: Any) -> str:
        import pandas as pd

        if isinstance(obj, pd.DataFrame):
            return "dataframe"
        if isinstance(obj, (dict, list)):
            return "json"
        if isinstance(obj, str):
            return "text"
        raise TypeError(f"unsupported handle object type: {type(obj)!r}")

    def put(self, obj: Any, source: str, *, id: str | None = None,
            kind: str | None = None) -> Handle:
        hid = id or self._new_id()
        kind = kind or self._detect_kind(obj)
        if kind == "dataframe":
            handle = self._write_dataframe(hid, obj, source)
        elif kind == "json":
            handle = self._write_json(hid, obj, source)
        elif kind == "text":
            handle = self._write_text(hid, obj, source)
        else:
            raise ValueError(f"unknown handle kind: {kind!r}")
        self._handles[hid] = handle
        return handle

    def _write_dataframe(self, hid: str, df: Any, source: str) -> Handle:
        rel = f"handles/{hid}.parquet"
        path = self.root / rel
        df.to_parquet(path)
        preview = df.head(_PREVIEW_ROWS).to_csv(index=False)
        preview += f"... ({_PREVIEW_ROWS} of {len(df)} rows)" if len(df) > _PREVIEW_ROWS else ""
        return Handle(
            id=hid, kind="dataframe", path=rel, source=source,
            bytes=path.stat().st_size, preview=preview,
            schema={c: str(t) for c, t in df.dtypes.items()},
            n_rows=int(len(df)), n_cols=int(df.shape[1]),
        )

    def _write_json(self, hid: str, obj: Any, source: str) -> Handle:
        rel = f"handles/{hid}.json"
        path = self.root / rel
        text = json.dumps(obj, default=str)
        path.write_text(text)
        return Handle(
            id=hid, kind="json", path=rel, source=source,
            bytes=len(text.encode()), preview=text[:_PREVIEW_CHARS],
        )

    def _write_text(self, hid: str, obj: str, source: str) -> Handle:
        rel = f"handles/{hid}.txt"
        path = self.root / rel
        path.write_text(obj)
        return Handle(
            id=hid, kind="text", path=rel, source=source,
            bytes=len(obj.encode()), preview=obj[:_PREVIEW_CHARS],
        )

    def register(self, record: dict[str, Any]) -> Handle:
        """Register a handle whose file already exists (e.g. written by the sandbox child)."""
        handle = Handle(**record)
        self._handles[handle.id] = handle
        # keep the counter ahead of externally-created ids like "h7"
        if handle.id.startswith("h") and handle.id[1:].isdigit():
            self._counter = max(self._counter, int(handle.id[1:]))
        return handle

    def get(self, handle_id: str) -> Any:
        handle = self._handles[handle_id]
        path = self.root / handle.path
        if handle.kind == "dataframe":
            import pandas as pd
            return pd.read_parquet(path)
        if handle.kind == "json":
            return json.loads(path.read_text())
        return path.read_text()

    def summary(self, handle_id: str) -> dict[str, Any]:
        return self._handles[handle_id].summary()

    def manifest(self) -> dict[str, Any]:
        return {hid: h.summary() for hid, h in self._handles.items()}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_handles.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add harness/handles.py tests/test_handles.py
git commit -m "feat: add Handle + HandleStore with json/text/dataframe roundtrips"
```

---

## Task 4: Sandbox runtime helper (`harness_sandbox`)

This module is imported **inside** the subprocess. It must have zero dependency on the
`harness` package internals — it talks to the parent only through env vars and files.

**Files:**
- Create: `harness/runtime/harness_sandbox.py`
- Create: `harness/runtime/__init__.py` (empty; keeps it importable for tests)
- Test: `tests/test_sandbox.py` (helper-only tests in this task; executor tests in Task 5)

- [ ] **Step 1: Write the failing test** (exercise the helper directly by setting its env)

Create `tests/test_sandbox.py`:

```python
import json
import os
import subprocess
import sys
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parent.parent / "harness" / "runtime"


def _run_child(tmp_path, body: str, registry: dict | None = None, args: list[str] | None = None):
    """Run a small script in a child process with the helper env wired up."""
    root = tmp_path
    (root / "handles").mkdir(exist_ok=True)
    new_handles = root / "_new_handles.jsonl"
    emit = root / "_emit.json"
    registry_path = root / "_registry.json"
    registry_path.write_text(json.dumps(registry or {}))

    script = root / "script.py"
    script.write_text(body)

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HARNESS_ROOT": str(root),
        "HARNESS_NEW_HANDLES": str(new_handles),
        "HARNESS_EMIT": str(emit),
        "HARNESS_REGISTRY": str(registry_path),
        "PYTHONPATH": str(RUNTIME_DIR),
    }
    proc = subprocess.run(
        [sys.executable, str(script), *(args or [])],
        cwd=root, env=env, capture_output=True, text=True, timeout=30,
    )
    return proc, new_handles, emit


def test_helper_emit_writes_payload(tmp_path):
    proc, _, emit = _run_child(
        tmp_path,
        "from harness_sandbox import emit\nemit({'total': 42})\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(emit.read_text()) == {"total": 42}


def test_helper_save_records_new_handle(tmp_path):
    proc, new_handles, _ = _run_child(
        tmp_path,
        "from harness_sandbox import save\nsave('h5', 'derived text')\n",
    )
    assert proc.returncode == 0, proc.stderr
    line = json.loads(new_handles.read_text().strip())
    assert line["id"] == "h5"
    assert line["kind"] == "text"
    assert (tmp_path / line["path"]).read_text() == "derived text"


def test_helper_load_reads_existing_text_handle(tmp_path):
    (tmp_path / "handles").mkdir(exist_ok=True)
    (tmp_path / "handles" / "h1.txt").write_text("input data")
    registry = {"h1": {"kind": "text", "path": "handles/h1.txt"}}
    proc, _, emit = _run_child(
        tmp_path,
        "from harness_sandbox import load, emit\nemit(load('h1'))\n",
        registry=registry,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(emit.read_text()) == "input data"


def test_helper_passes_argv(tmp_path):
    proc, _, emit = _run_child(
        tmp_path,
        "import sys\nfrom harness_sandbox import emit\nemit(sys.argv[1:])\n",
        args=["EU", "2025"],
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(emit.read_text()) == ["EU", "2025"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sandbox.py -v`
Expected: FAIL — child exits non-zero with `ModuleNotFoundError: No module named 'harness_sandbox'`

- [ ] **Step 3: Implement the runtime helper**

Create `harness/runtime/__init__.py` (empty file):

```python
```

Create `harness/runtime/harness_sandbox.py`:

```python
"""Injected into the sandbox subprocess as the top-level module ``harness_sandbox``.

Communicates with the parent harness only via env vars and files:
  HARNESS_ROOT         session root directory
  HARNESS_REGISTRY     json file: { handle_id: {kind, path} } for existing handles
  HARNESS_NEW_HANDLES  jsonl file this module appends new-handle records to
  HARNESS_EMIT         json file this module writes the emit() payload to
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_ROOT = Path(os.environ["HARNESS_ROOT"])
_REGISTRY = json.loads(Path(os.environ["HARNESS_REGISTRY"]).read_text())
_NEW = Path(os.environ["HARNESS_NEW_HANDLES"])
_EMIT = Path(os.environ["HARNESS_EMIT"])

_PREVIEW_CHARS = 800
_PREVIEW_ROWS = 5


def load(handle_id: str) -> Any:
    meta = _REGISTRY[handle_id]
    path = _ROOT / meta["path"]
    kind = meta["kind"]
    if kind == "dataframe":
        import pandas as pd
        return pd.read_parquet(path)
    if kind == "json":
        return json.loads(path.read_text())
    return path.read_text()


def save(handle_id: str, obj: Any, source: str = "run_python") -> str:
    import pandas as pd

    if isinstance(obj, pd.DataFrame):
        rel = f"handles/{handle_id}.parquet"
        obj.to_parquet(_ROOT / rel)
        preview = obj.head(_PREVIEW_ROWS).to_csv(index=False)
        rec = {"id": handle_id, "kind": "dataframe", "path": rel, "source": source,
               "bytes": (_ROOT / rel).stat().st_size, "preview": preview,
               "schema": {c: str(t) for c, t in obj.dtypes.items()},
               "n_rows": int(len(obj)), "n_cols": int(obj.shape[1])}
    elif isinstance(obj, (dict, list)):
        rel = f"handles/{handle_id}.json"
        text = json.dumps(obj, default=str)
        (_ROOT / rel).write_text(text)
        rec = {"id": handle_id, "kind": "json", "path": rel, "source": source,
               "bytes": len(text.encode()), "preview": text[:_PREVIEW_CHARS]}
    else:
        rel = f"handles/{handle_id}.txt"
        text = str(obj)
        (_ROOT / rel).write_text(text)
        rec = {"id": handle_id, "kind": "text", "path": rel, "source": source,
               "bytes": len(text.encode()), "preview": text[:_PREVIEW_CHARS]}

    with _NEW.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return handle_id


def emit(obj: Any) -> None:
    _EMIT.write_text(json.dumps(obj, default=str))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_sandbox.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add harness/runtime/ tests/test_sandbox.py
git commit -m "feat: add harness_sandbox runtime helper (load/save/emit)"
```

---

## Task 5: `LocalSubprocessSandbox` + `ExecResult`

**Files:**
- Create: `harness/sandbox.py`
- Modify: `tests/test_sandbox.py` (append executor tests)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_sandbox.py`)

Append to `tests/test_sandbox.py`:

```python
import pytest

from harness.config import SandboxConfig
from harness.handles import HandleStore
from harness.sandbox import ExecResult, LocalSubprocessSandbox
from harness.paths import PathEscapesRootError


def _sandbox(tmp_path):
    store = HandleStore(tmp_path)
    return LocalSubprocessSandbox(root=tmp_path, store=store, config=SandboxConfig()), store


def test_run_script_captures_emit_result(tmp_path):
    sb, _ = _sandbox(tmp_path)
    (tmp_path / "s.py").write_text(
        "from harness_sandbox import emit\nemit({'ok': True})\n"
    )
    res = sb.run_script("s.py")
    assert isinstance(res, ExecResult)
    assert res.exit_code == 0
    assert res.result == {"ok": True}
    assert res.error is None


def test_run_script_captures_stdout(tmp_path):
    sb, _ = _sandbox(tmp_path)
    (tmp_path / "s.py").write_text("print('hello from child')\n")
    res = sb.run_script("s.py")
    assert "hello from child" in res.stdout


def test_run_script_reports_new_handles_and_registers_them(tmp_path):
    sb, store = _sandbox(tmp_path)
    (tmp_path / "s.py").write_text(
        "from harness_sandbox import save\nsave('h1', {'derived': 1})\n"
    )
    res = sb.run_script("s.py")
    assert res.new_handles == ["h1"]
    assert store.get("h1") == {"derived": 1}  # parent ingested it


def test_run_script_can_load_existing_handle(tmp_path):
    sb, store = _sandbox(tmp_path)
    store.put({"input": 99}, source="seed", id="h1")
    (tmp_path / "s.py").write_text(
        "from harness_sandbox import load, emit\nemit(load('h1'))\n"
    )
    res = sb.run_script("s.py")
    assert res.result == {"input": 99}


def test_run_script_passes_args(tmp_path):
    sb, _ = _sandbox(tmp_path)
    (tmp_path / "s.py").write_text(
        "import sys\nfrom harness_sandbox import emit\nemit(sys.argv[1:])\n"
    )
    res = sb.run_script("s.py", args=["EU", "2025"])
    assert res.result == ["EU", "2025"]


def test_run_script_captures_exception_as_error(tmp_path):
    sb, _ = _sandbox(tmp_path)
    (tmp_path / "s.py").write_text("raise ValueError('boom')\n")
    res = sb.run_script("s.py")
    assert res.exit_code != 0
    assert "ValueError: boom" in res.error


def test_run_script_times_out(tmp_path):
    store = HandleStore(tmp_path)
    sb = LocalSubprocessSandbox(root=tmp_path, store=store,
                                config=SandboxConfig(timeout_s=0.5))
    (tmp_path / "s.py").write_text("import time\ntime.sleep(5)\n")
    res = sb.run_script("s.py")
    assert res.killed_by == "timeout"
    assert res.exit_code != 0


def test_run_script_rejects_path_outside_root(tmp_path):
    sb, _ = _sandbox(tmp_path)
    with pytest.raises(PathEscapesRootError):
        sb.run_script("../evil.py")


def test_run_code_convenience_writes_and_runs_inline(tmp_path):
    sb, _ = _sandbox(tmp_path)
    res = sb.run_code("from harness_sandbox import emit\nemit(7)\n")
    assert res.result == 7
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sandbox.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.sandbox'`

- [ ] **Step 3: Implement the sandbox executor**

Create `harness/sandbox.py`:

```python
"""Local subprocess sandbox: runs agent scripts in a child process, root-confined."""

from __future__ import annotations

import json
import resource
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import SandboxConfig
from .handles import HandleStore
from .paths import safe_path

_RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
_SCRIPTS_DIR = ".scripts"


@dataclass
class ExecResult:
    stdout: str
    result: Any | None
    error: str | None
    exit_code: int
    new_handles: list[str] = field(default_factory=list)
    killed_by: str | None = None


class SandboxExecutor(Protocol):
    """Swappable execution backend. Local now; container/remote later."""

    def run_script(self, path: str, args: list[str] | None = None) -> ExecResult: ...
    def run_code(self, code: str, args: list[str] | None = None) -> ExecResult: ...


class LocalSubprocessSandbox:
    def __init__(self, root: Path | str, store: HandleStore,
                 config: SandboxConfig | None = None) -> None:
        self.root = Path(root).resolve()
        self.store = store
        self.config = config or SandboxConfig()
        self._inline_counter = 0

    def run_code(self, code: str, args: list[str] | None = None) -> ExecResult:
        self._inline_counter += 1
        scripts = self.root / _SCRIPTS_DIR
        scripts.mkdir(exist_ok=True)
        rel = f"{_SCRIPTS_DIR}/_inline_{self._inline_counter}.py"
        (self.root / rel).write_text(code)
        return self.run_script(rel, args)

    def run_script(self, path: str, args: list[str] | None = None) -> ExecResult:
        script = safe_path(self.root, path)  # raises PathEscapesRootError if outside

        new_handles_file = self.root / "_new_handles.jsonl"
        emit_file = self.root / "_emit.json"
        registry_file = self.root / "_registry.json"
        for f in (new_handles_file, emit_file):
            f.unlink(missing_ok=True)
        new_handles_file.touch()
        registry = {hid: {"kind": h.kind, "path": h.path}
                    for hid, h in self.store.manifest_handles().items()}
        registry_file.write_text(json.dumps(registry))

        env = {
            "PATH": _minimal_path(),
            "HARNESS_ROOT": str(self.root),
            "HARNESS_REGISTRY": str(registry_file),
            "HARNESS_NEW_HANDLES": str(new_handles_file),
            "HARNESS_EMIT": str(emit_file),
            "PYTHONPATH": str(_RUNTIME_DIR),
        }

        killed_by = None
        try:
            proc = subprocess.run(
                [sys.executable, str(script), *(args or [])],
                cwd=self.root, env=env, capture_output=True, text=True,
                timeout=self.config.timeout_s, preexec_fn=self._limits(),
            )
            stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            killed_by = "timeout"
            stdout = e.stdout or ""
            stderr = (e.stderr or "") + "\nharness: killed (timeout)"
            exit_code = -1
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")

        result = None
        if exit_code == 0 and emit_file.exists():
            result = json.loads(emit_file.read_text())

        new_handles = self._ingest_new_handles(new_handles_file)
        error = stderr.strip() or None if exit_code != 0 else None

        return ExecResult(stdout=stdout, result=result, error=error,
                          exit_code=exit_code, new_handles=new_handles,
                          killed_by=killed_by)

    def _ingest_new_handles(self, new_handles_file: Path) -> list[str]:
        ids: list[str] = []
        if not new_handles_file.exists():
            return ids
        for line in new_handles_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            self.store.register(rec)
            ids.append(rec["id"])
        return ids

    def _limits(self):
        cfg = self.config

        def set_limits() -> None:
            mem = cfg.max_memory_mb * 1024 * 1024
            fsize = cfg.max_file_size_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            except (ValueError, OSError):
                pass
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        return set_limits


def _minimal_path() -> str:
    return "/usr/bin:/bin:/usr/local/bin"
```

Note: this references `store.manifest_handles()`, added in the next step.

- [ ] **Step 4: Add the `manifest_handles` accessor to `HandleStore`**

Modify `harness/handles.py` — add this method to `HandleStore` (after `manifest`):

```python
    def manifest_handles(self) -> dict[str, Handle]:
        return dict(self._handles)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_sandbox.py -v`
Expected: PASS (all helper + executor tests; 13 passed)

Note on `RLIMIT_AS`: it is wrapped in try/except because on macOS an address-space
limit can spuriously fail; the timeout + FSIZE/CORE limits remain enforced. This is
acceptable for the local tier (the container tier will enforce memory hard).

- [ ] **Step 6: Commit**

```bash
git add harness/sandbox.py harness/handles.py tests/test_sandbox.py
git commit -m "feat: add LocalSubprocessSandbox with rlimits, timeout, handle ingestion"
```

---

## Task 6: `Session` — wires root + store + sandbox

**Files:**
- Create: `harness/session.py`
- Modify: `harness/__init__.py` (export foundation API)
- Test: `tests/test_session.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session.py`:

```python
from pathlib import Path

from harness.config import HarnessConfig
from harness.session import Session


def test_session_creates_root_under_default_location(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sess = Session.create(HarnessConfig())
    assert sess.root.exists()
    assert sess.root.is_dir()
    assert ".harness/sessions" in str(sess.root)
    sess.cleanup()


def test_session_uses_explicit_root_dir(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "myroot")
    sess = Session.create(cfg)
    assert sess.root == (tmp_path / "myroot").resolve()
    assert sess.root.exists()


def test_session_wires_store_and_sandbox_to_same_root(tmp_path):
    sess = Session.create(HarnessConfig(root_dir=tmp_path / "r"))
    assert sess.store.root == sess.root
    assert sess.sandbox.root == sess.root


def test_session_end_to_end_handle_then_analyze(tmp_path):
    sess = Session.create(HarnessConfig(root_dir=tmp_path / "r"))
    h = sess.store.put({"values": [120, 95, 0, 210]}, source="tool:read")
    code = (
        "from harness_sandbox import load, emit\n"
        f"d = load('{h.id}')\n"
        "vals = [v for v in d['values'] if v > 0]\n"
        "emit({'total': sum(vals), 'dropped': len(d['values']) - len(vals)})\n"
    )
    res = sess.sandbox.run_code(code)
    assert res.result == {"total": 425, "dropped": 1}


def test_cleanup_removes_root_when_owned(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    sess = Session.create(cfg)
    assert sess.root.exists()
    sess.cleanup()
    assert not sess.root.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_session.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.session'`

- [ ] **Step 3: Implement `Session`**

Create `harness/session.py`:

```python
"""A Session bundles one run's root directory, handle store, and sandbox."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import HarnessConfig
from .handles import HandleStore
from .sandbox import LocalSubprocessSandbox


@dataclass
class Session:
    root: Path
    store: HandleStore
    sandbox: LocalSubprocessSandbox
    config: HarnessConfig

    @classmethod
    def create(cls, config: HarnessConfig) -> "Session":
        root = _resolve_root(config)
        root.mkdir(parents=True, exist_ok=True)
        store = HandleStore(root)
        sandbox = LocalSubprocessSandbox(root=root, store=store, config=config.sandbox)
        return cls(root=root, store=store, sandbox=sandbox, config=config)

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)


def _resolve_root(config: HarnessConfig) -> Path:
    if config.root_dir is not None:
        return Path(config.root_dir).resolve()
    base = Path.cwd() / ".harness" / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    existing = [int(p.name) for p in base.iterdir() if p.name.isdigit()]
    next_id = (max(existing) + 1) if existing else 1
    return (base / str(next_id)).resolve()
```

- [ ] **Step 4: Export the foundation API**

Replace `harness/__init__.py` with:

```python
"""Data-integration agent harness."""

from .config import FetchConfig, HarnessConfig, SandboxConfig
from .handles import Handle, HandleStore
from .paths import PathEscapesRootError, safe_path
from .sandbox import ExecResult, LocalSubprocessSandbox, SandboxExecutor
from .session import Session

__version__ = "0.1.0"

__all__ = [
    "FetchConfig", "HarnessConfig", "SandboxConfig",
    "Handle", "HandleStore",
    "PathEscapesRootError", "safe_path",
    "ExecResult", "LocalSubprocessSandbox", "SandboxExecutor",
    "Session",
]
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS (all tests across the five test files; ~34 passed)

- [ ] **Step 6: Commit**

```bash
git add harness/session.py harness/__init__.py tests/test_session.py
git commit -m "feat: add Session wiring root/store/sandbox + foundation exports"
```

---

## Definition of done (Phase 1)

- `uv run pytest` is green with the security boundary (`safe_path`) exhaustively covered.
- `from harness import Session, HarnessConfig` gives a working, LLM-free unit: store a dataset → get a handle → run an agent-style script that `load()`s it, analyzes, and `emit()`s a result, all confined to the session root.
- No `agent_framework` / model / network dependency in any test.
- Ready for Phase 2 (spill middleware, agent tools, `Harness`/`solve()`, CLI) to build on top.
```
