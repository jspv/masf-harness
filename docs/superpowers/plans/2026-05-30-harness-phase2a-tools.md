# Harness Phase 2a — Tool Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the agent's tool surface as Session-bound Python functions — `read_file`, `write_file`, `list_files`, `search`, `fetch_url`, `run_python`, `inspect_handle` — each root-confined and returning model-friendly values, all testable without an LLM.

**Architecture:** Each tool is a plain function taking a `Session` (from Phase 1) as its first argument and the model-facing parameters after. File tools route every path through `safe_path`. `search` shells out to ripgrep with a pure-Python regex fallback. `fetch_url` uses httpx (injectable for tests) and spills the body to a handle. `run_python` wraps the Phase 1 sandbox. `inspect_handle` gives a deeper on-demand view of a handle. Wrapping these as MAF agent tools + the spill middleware + `Harness`/`solve()` + CLI is Phase 2b.

**Tech Stack:** Python 3.12, `uv`, `pytest`, `httpx` (with `MockTransport` for tests), ripgrep (`rg`, with Python fallback). No LLM/API/network in any test.

**Builds on (Phase 1, on `main`):** `harness.Session` (`.root`, `.store`, `.sandbox`, `.config`), `harness.HandleStore` (`put`/`get`/`summary`/`manifest_handles`), `harness.safe_path`/`PathEscapesRootError`, `harness.LocalSubprocessSandbox` (`run_code`/`run_script` → `ExecResult`), `harness.HarnessConfig`/`FetchConfig`.

---

## File Structure

- Create: `harness/tools/__init__.py` — re-exports the tool functions
- Create: `harness/tools/files.py` — `read_file`, `write_file`, `list_files`
- Create: `harness/tools/search.py` — `search` (ripgrep + fallback) + `Match`
- Create: `harness/tools/fetch.py` — `fetch_url`
- Create: `harness/tools/code.py` — `run_python`
- Create: `harness/tools/inspect.py` — `inspect_handle`
- Create tests: `tests/tools/test_files.py`, `test_search.py`, `test_fetch.py`, `test_code.py`, `test_inspect.py`

**Tool return conventions (consistent across the surface):**
- `read_file` → `str` (the requested slice)
- `write_file` → `str` (a short confirmation)
- `list_files` → `list[str]` (root-relative POSIX paths)
- `search` → `list[dict]` (`{"file", "line", "col", "text"}`)
- `fetch_url` → `dict` (a Handle summary)
- `run_python` → `dict` (the `ExecResult` as a dict)
- `inspect_handle` → `dict`

---

## Task 1: Tools package + file tools (`read_file`, `write_file`, `list_files`)

**Files:**
- Create: `harness/tools/__init__.py`
- Create: `harness/tools/files.py`
- Create: `tests/tools/__init__.py` (empty), `tests/tools/test_files.py`

- [ ] **Step 1: Write the failing tests** — create `tests/tools/__init__.py` (empty) and `tests/tools/test_files.py`:

```python
import pytest

from harness import HarnessConfig, PathEscapesRootError, Session
from harness.tools.files import list_files, read_file, write_file


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def test_write_then_read_roundtrip(tmp_path):
    sess = _session(tmp_path)
    msg = write_file(sess, "notes/a.txt", "hello\nworld\n")
    assert "a.txt" in msg
    assert read_file(sess, "notes/a.txt") == "hello\nworld\n"


def test_read_is_bounded_by_limit_and_offset(tmp_path):
    sess = _session(tmp_path)
    write_file(sess, "big.txt", "".join(f"line{i}\n" for i in range(100)))
    out = read_file(sess, "big.txt", offset=10, limit=3)
    assert out == "line10\nline11\nline12\n"


def test_write_rejects_path_outside_root(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(PathEscapesRootError):
        write_file(sess, "../escape.txt", "x")


def test_read_rejects_path_outside_root(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(PathEscapesRootError):
        read_file(sess, "/etc/passwd")


def test_list_files_returns_relative_paths(tmp_path):
    sess = _session(tmp_path)
    write_file(sess, "a.txt", "1")
    write_file(sess, "sub/b.txt", "2")
    listing = set(list_files(sess, "."))
    assert "a.txt" in listing
    assert "sub/b.txt" in listing


def test_list_files_rejects_path_outside_root(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(PathEscapesRootError):
        list_files(sess, "..")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/tools/test_files.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.tools'`

- [ ] **Step 3: Create the package marker** — create `harness/tools/__init__.py`:

```python
"""Agent tool surface: Session-bound functions the agent calls."""

from .files import list_files, read_file, write_file

__all__ = ["list_files", "read_file", "write_file"]
```

- [ ] **Step 4: Implement the file tools** — create `harness/tools/files.py`:

```python
"""File tools, all confined to the session root via safe_path."""

from __future__ import annotations

from pathlib import Path

from ..paths import safe_path
from ..session import Session

_DEFAULT_LIMIT = 2000


def read_file(session: Session, path: str, offset: int = 0, limit: int = _DEFAULT_LIMIT) -> str:
    """Read up to ``limit`` lines starting at line ``offset`` from a file under the root."""
    target = safe_path(session.root, path)
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(lines[offset:offset + limit])


def write_file(session: Session, path: str, content: str) -> str:
    """Write ``content`` to a file under the root, creating parent dirs. Returns a confirmation."""
    target = safe_path(session.root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    rel = target.relative_to(session.root).as_posix()
    return f"wrote {len(content.encode())} bytes to {rel}"


def list_files(session: Session, path: str = ".") -> list[str]:
    """List files (recursively) under ``path`` as root-relative POSIX paths."""
    base = safe_path(session.root, path)
    if base.is_file():
        return [base.relative_to(session.root).as_posix()]
    out: list[str] = []
    for p in sorted(base.rglob("*")):
        if p.is_file():
            out.append(p.relative_to(session.root).as_posix())
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/tools/test_files.py -v`
Expected: PASS (6 passed)

- [ ] **Step 6: Commit**

```bash
git add harness/tools/__init__.py harness/tools/files.py tests/tools/
git commit -m "feat(tools): add root-confined read_file/write_file/list_files"
```

---

## Task 2: `search` tool (ripgrep + Python fallback)

**Files:**
- Create: `harness/tools/search.py`
- Modify: `harness/tools/__init__.py` (export `search`)
- Test: `tests/tools/test_search.py`

- [ ] **Step 1: Write the failing tests** — create `tests/tools/test_search.py`:

```python
import pytest

from harness import HarnessConfig, PathEscapesRootError, Session
from harness.tools.files import write_file
from harness.tools.search import _search_python, search


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def _seed(sess):
    write_file(sess, "a.txt", "alpha\nbeta\nGAMMA gamma\n")
    write_file(sess, "sub/b.txt", "delta\nalpha again\n")


def test_search_finds_matches_across_files(tmp_path):
    sess = _session(tmp_path)
    _seed(sess)
    hits = search(sess, "alpha")
    files = {h["file"] for h in hits}
    assert files == {"a.txt", "sub/b.txt"}
    assert all("line" in h and "text" in h for h in hits)


def test_search_can_target_a_single_file(tmp_path):
    sess = _session(tmp_path)
    _seed(sess)
    hits = search(sess, "alpha", path="a.txt")
    assert {h["file"] for h in hits} == {"a.txt"}
    assert hits[0]["line"] == 1


def test_search_ignore_case(tmp_path):
    sess = _session(tmp_path)
    _seed(sess)
    assert search(sess, "gamma", ignore_case=False) and all(
        h["text"].find("gamma") >= 0 or h["text"].find("GAMMA") >= 0 for h in search(sess, "gamma", ignore_case=True)
    )
    insensitive = search(sess, "gamma", ignore_case=True)
    assert any("GAMMA" in h["text"] for h in insensitive)


def test_search_respects_max_matches(tmp_path):
    sess = _session(tmp_path)
    write_file(sess, "many.txt", "x\n" * 50)
    hits = search(sess, "x", max_matches=5)
    assert len(hits) == 5


def test_search_rejects_path_outside_root(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(PathEscapesRootError):
        search(sess, "x", path="..")


def test_python_fallback_matches_directly(tmp_path):
    # Exercise the fallback regardless of whether rg is installed.
    sess = _session(tmp_path)
    _seed(sess)
    hits = _search_python(sess.root, sess.root, "alpha", ignore_case=False, max_matches=100)
    assert {h["file"] for h in hits} == {"a.txt", "sub/b.txt"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/tools/test_search.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.tools.search'`

- [ ] **Step 3: Implement** — create `harness/tools/search.py`:

```python
"""Text search over the session root: ripgrep when available, Python regex fallback."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from ..paths import safe_path
from ..session import Session


def search(session: Session, pattern: str, path: str = ".", glob: str | None = None,
           ignore_case: bool = False, max_matches: int = 100) -> list[dict]:
    """Search for ``pattern`` (a regex) in files under ``path`` (a file or folder).

    Returns up to ``max_matches`` hits as ``{"file", "line", "col", "text"}`` with
    ``file`` relative to the session root. Uses ripgrep if installed, else a Python scan.
    """
    base = safe_path(session.root, path)
    rg = shutil.which("rg")
    if rg:
        return _search_rg(rg, session.root, base, pattern, glob, ignore_case, max_matches)
    return _search_python(session.root, base, pattern, ignore_case, max_matches, glob)


def _search_rg(rg: str, root: Path, base: Path, pattern: str, glob: str | None,
               ignore_case: bool, max_matches: int) -> list[dict]:
    cmd = [rg, "--json", "--no-heading"]
    if ignore_case:
        cmd.append("-i")
    if glob:
        cmd += ["-g", glob]
    cmd += [pattern, str(base)]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    import json

    hits: list[dict] = []
    for line in proc.stdout.splitlines():
        if len(hits) >= max_matches:
            break
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") != "match":
            continue
        data = evt["data"]
        abs_path = Path(data["path"]["text"])
        rel = abs_path.relative_to(root).as_posix() if abs_path.is_absolute() else (root / abs_path).relative_to(root).as_posix()
        for sub in data["submatches"]:
            if len(hits) >= max_matches:
                break
            hits.append({
                "file": rel,
                "line": data["line_number"],
                "col": sub["start"],
                "text": data["lines"]["text"].rstrip("\n"),
            })
    return hits


def _search_python(root: Path, base: Path, pattern: str, ignore_case: bool,
                   max_matches: int, glob: str | None = None) -> list[dict]:
    flags = re.IGNORECASE if ignore_case else 0
    rx = re.compile(pattern, flags)
    files = [base] if base.is_file() else sorted(
        p for p in base.rglob(glob or "*") if p.is_file()
    )
    hits: list[dict] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            m = rx.search(line)
            if m:
                hits.append({
                    "file": f.relative_to(root).as_posix(),
                    "line": lineno,
                    "col": m.start(),
                    "text": line,
                })
                if len(hits) >= max_matches:
                    return hits
    return hits
```

- [ ] **Step 4: Export `search`** — update `harness/tools/__init__.py`:

```python
"""Agent tool surface: Session-bound functions the agent calls."""

from .files import list_files, read_file, write_file
from .search import search

__all__ = ["list_files", "read_file", "write_file", "search"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/tools/test_search.py -v`
Expected: PASS (6 passed)

- [ ] **Step 6: Commit**

```bash
git add harness/tools/search.py harness/tools/__init__.py tests/tools/test_search.py
git commit -m "feat(tools): add ripgrep-backed search with Python fallback"
```

---

## Task 3: `fetch_url` tool

**Files:**
- Create: `harness/tools/fetch.py`
- Modify: `harness/tools/__init__.py` (export `fetch_url`)
- Test: `tests/tools/test_fetch.py`

- [ ] **Step 1: Write the failing tests** — create `tests/tools/test_fetch.py`:

```python
import httpx
import pytest

from harness import HarnessConfig, Session
from harness.tools.fetch import fetch_url


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_json_returns_json_handle(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(200, json={"hello": "world"})

    summary = fetch_url(sess, "https://example.com/data", client=_client(handler))
    assert summary["kind"] == "json"
    assert sess.store.get(summary["id"]) == {"hello": "world"}


def test_fetch_text_returns_text_handle(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(200, text="plain body", headers={"content-type": "text/plain"})

    summary = fetch_url(sess, "https://example.com/page", client=_client(handler))
    assert summary["kind"] == "text"
    assert sess.store.get(summary["id"]) == "plain body"


def test_fetch_rejects_disallowed_scheme(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(ValueError, match="scheme"):
        fetch_url(sess, "file:///etc/passwd")


def test_fetch_rejects_body_over_max_bytes(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    cfg.fetch.max_bytes = 5
    sess = Session.create(cfg)

    def handler(request):
        return httpx.Response(200, text="this is way too long", headers={"content-type": "text/plain"})

    with pytest.raises(ValueError, match="exceeds"):
        fetch_url(sess, "https://example.com/big", client=_client(handler))


def test_fetch_raises_on_http_error_status(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(404, text="nope")

    with pytest.raises(httpx.HTTPStatusError):
        fetch_url(sess, "https://example.com/missing", client=_client(handler))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/tools/test_fetch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.tools.fetch'`

- [ ] **Step 3: Implement** — create `harness/tools/fetch.py`:

```python
"""fetch_url: pull a link, spill its body to a typed handle, return the handle summary."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from ..session import Session


def fetch_url(session: Session, url: str, max_bytes: int | None = None,
              client: httpx.Client | None = None) -> dict:
    """Fetch ``url`` and store its body as a handle (json if JSON content-type, else text).

    Returns the handle summary. Enforces the session's allowed schemes and byte cap.
    ``client`` is injectable for testing; a real httpx.Client is used by default.
    """
    cfg = session.config.fetch
    limit = max_bytes if max_bytes is not None else cfg.max_bytes

    scheme = urlparse(url).scheme
    if scheme not in cfg.allowed_schemes:
        raise ValueError(f"scheme {scheme!r} not allowed (allowed: {cfg.allowed_schemes})")

    owns_client = client is None
    client = client or httpx.Client(timeout=cfg.timeout_s)
    try:
        resp = client.get(url)
        resp.raise_for_status()
        body = resp.content
        if len(body) > limit:
            raise ValueError(f"response body ({len(body)} bytes) exceeds max_bytes ({limit})")
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            handle = session.store.put(resp.json(), source=f"fetch_url({url})", kind="json")
        else:
            handle = session.store.put(resp.text, source=f"fetch_url({url})", kind="text")
        return handle.summary()
    finally:
        if owns_client:
            client.close()
```

- [ ] **Step 4: Export `fetch_url`** — update `harness/tools/__init__.py`:

```python
"""Agent tool surface: Session-bound functions the agent calls."""

from .fetch import fetch_url
from .files import list_files, read_file, write_file
from .search import search

__all__ = ["list_files", "read_file", "write_file", "search", "fetch_url"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/tools/test_fetch.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add harness/tools/fetch.py harness/tools/__init__.py tests/tools/test_fetch.py
git commit -m "feat(tools): add fetch_url that spills response bodies to handles"
```

---

## Task 4: `run_python` tool

**Files:**
- Create: `harness/tools/code.py`
- Modify: `harness/tools/__init__.py` (export `run_python`)
- Test: `tests/tools/test_code.py`

- [ ] **Step 1: Write the failing tests** — create `tests/tools/test_code.py`:

```python
import pytest

from harness import HarnessConfig, Session
from harness.tools.code import run_python


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def test_run_python_inline_code_returns_result_dict(tmp_path):
    sess = _session(tmp_path)
    out = run_python(sess, code="from harness_sandbox import emit\nemit(21 * 2)\n")
    assert out["result"] == 42
    assert out["exit_code"] == 0
    assert out["error"] is None
    assert out["new_handles"] == []


def test_run_python_script_file_with_args(tmp_path):
    sess = _session(tmp_path)
    from harness.tools.files import write_file
    write_file(sess, "s.py", "import sys\nfrom harness_sandbox import emit\nemit(sys.argv[1:])\n")
    out = run_python(sess, path="s.py", args=["EU", "2025"])
    assert out["result"] == ["EU", "2025"]


def test_run_python_reports_new_handles(tmp_path):
    sess = _session(tmp_path)
    out = run_python(sess, code="from harness_sandbox import save\nsave('h1', {'x': 1})\n")
    assert out["new_handles"] == ["h1"]
    assert sess.store.get("h1") == {"x": 1}


def test_run_python_error_is_reported_not_raised(tmp_path):
    sess = _session(tmp_path)
    out = run_python(sess, code="raise ValueError('boom')\n")
    assert out["exit_code"] != 0
    assert "ValueError: boom" in out["error"]


def test_run_python_requires_code_or_path(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(ValueError, match="code.*or.*path"):
        run_python(sess)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/tools/test_code.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.tools.code'`

- [ ] **Step 3: Implement** — create `harness/tools/code.py`:

```python
"""run_python: execute agent-authored Python in the sandbox and return a result dict."""

from __future__ import annotations

from dataclasses import asdict

from ..session import Session


def run_python(session: Session, code: str | None = None, path: str | None = None,
               args: list[str] | None = None) -> dict:
    """Run Python in the sandbox. Provide ``path`` (a script file under the root) or
    ``code`` (inline, written to a scratch script then run). Scripts may use the injected
    ``load(id)`` / ``save(id, obj)`` / ``emit(obj)`` helpers. Returns the ExecResult fields:
    ``stdout, stderr, result, error, exit_code, new_handles, killed_by``.
    """
    if path is not None:
        res = session.sandbox.run_script(path, args)
    elif code is not None:
        res = session.sandbox.run_code(code, args)
    else:
        raise ValueError("run_python requires either code= or path=")
    return asdict(res)
```

- [ ] **Step 4: Export `run_python`** — update `harness/tools/__init__.py`:

```python
"""Agent tool surface: Session-bound functions the agent calls."""

from .code import run_python
from .fetch import fetch_url
from .files import list_files, read_file, write_file
from .search import search

__all__ = ["list_files", "read_file", "write_file", "search", "fetch_url", "run_python"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/tools/test_code.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add harness/tools/code.py harness/tools/__init__.py tests/tools/test_code.py
git commit -m "feat(tools): add run_python wrapping the sandbox executor"
```

---

## Task 5: `inspect_handle` tool

**Files:**
- Create: `harness/tools/inspect.py`
- Modify: `harness/tools/__init__.py` (export `inspect_handle`)
- Test: `tests/tools/test_inspect.py`

- [ ] **Step 1: Write the failing tests** — create `tests/tools/test_inspect.py`:

```python
import pandas as pd
import pytest

from harness import HarnessConfig, Session
from harness.tools.inspect import inspect_handle


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def test_inspect_text_handle_returns_more_preview(tmp_path):
    sess = _session(tmp_path)
    h = sess.store.put("line\n" * 100, source="s")
    out = inspect_handle(sess, h.id, rows=10)
    assert out["kind"] == "text"
    assert out["preview"].count("line") == 10  # more than the stored summary preview


def test_inspect_dataframe_returns_head_rows(tmp_path):
    sess = _session(tmp_path)
    h = sess.store.put(pd.DataFrame({"a": range(50)}), source="s")
    out = inspect_handle(sess, h.id, rows=7)
    assert out["kind"] == "dataframe"
    assert out["n_rows"] == 50
    assert len(out["head"]) == 7


def test_inspect_dataframe_with_stats(tmp_path):
    sess = _session(tmp_path)
    h = sess.store.put(pd.DataFrame({"a": [1, 2, 3, 4]}), source="s")
    out = inspect_handle(sess, h.id, stats=True)
    assert "describe" in out
    assert out["describe"]["a"]["max"] == 4


def test_inspect_unknown_handle_raises(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(KeyError):
        inspect_handle(sess, "nope")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/tools/test_inspect.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.tools.inspect'`

- [ ] **Step 3: Implement** — create `harness/tools/inspect.py`:

```python
"""inspect_handle: on-demand deeper look at a stored handle (more preview / stats)."""

from __future__ import annotations

from ..session import Session


def inspect_handle(session: Session, handle_id: str, rows: int = 20,
                   stats: bool = False) -> dict:
    """Return a deeper view of a handle than its summary: more preview lines for text/json,
    and head rows (plus optional describe() stats) for dataframes."""
    handle = session.store.manifest_handles()[handle_id]  # raises KeyError if unknown
    obj = session.store.get(handle_id)
    out = handle.summary()
    if handle.kind == "dataframe":
        out["head"] = obj.head(rows).to_dict(orient="records")
        if stats:
            out["describe"] = obj.describe().to_dict()
    else:
        text = obj if isinstance(obj, str) else _json_text(obj)
        out["preview"] = "\n".join(text.splitlines()[:rows])
    return out


def _json_text(obj) -> str:
    import json

    return json.dumps(obj, indent=2, default=str)
```

- [ ] **Step 4: Export `inspect_handle`** — update `harness/tools/__init__.py`:

```python
"""Agent tool surface: Session-bound functions the agent calls."""

from .code import run_python
from .fetch import fetch_url
from .files import list_files, read_file, write_file
from .inspect import inspect_handle
from .search import search

__all__ = [
    "list_files", "read_file", "write_file", "search",
    "fetch_url", "run_python", "inspect_handle",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/tools/test_inspect.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Run the full suite + commit**

Run: `uv run pytest -q`
Expected: PASS (all Phase 1 + Phase 2a tests; ~75 passed)

```bash
git add harness/tools/inspect.py harness/tools/__init__.py tests/tools/test_inspect.py
git commit -m "feat(tools): add inspect_handle for deeper on-demand handle views"
```

---

## Definition of done (Phase 2a)

- `uv run pytest` green; the full tool surface (`read_file`, `write_file`, `list_files`, `search`, `fetch_url`, `run_python`, `inspect_handle`) implemented and tested without any LLM, API, or live network.
- Every file/search/script path is confined to the session root via `safe_path`.
- `fetch_url` spills bodies to handles; `run_python` wraps the sandbox; `inspect_handle` gives deeper views.
- Ready for Phase 2b: wrap these as MAF agent tools (with introspectable signatures/docstrings), add the spill middleware, build the agent on `create_harness_agent`, and expose `Harness`/`solve()` + CLI.
```
