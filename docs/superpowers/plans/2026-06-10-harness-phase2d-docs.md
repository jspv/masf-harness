# Document Ingestion (`read_document` via Docling) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `read_document(source)` tool that converts a PDF / Office / spreadsheet file — given as a workspace path or an `http(s)` URL — into clean markdown **with tables preserved** via Docling, stored as a handle, returning only the handle summary.

**Architecture:** A new `tools/documents.py` impl follows the established Session-bound tool pattern (`fetch.py` / `web.py`): it resolves the source (path-jailed via `safe_path`, or passed through for URLs), calls an **injectable converter seam** (`convert: Callable[[str], str]`) whose default lazily imports Docling, stores the markdown as a `text` handle, and returns structured `{error, source}` dicts on any failure instead of raising into the agent loop. It registers into `build_tools` and joins the `web` capability bundle. Docling is a heavy, model-downloading dependency, so it ships as an **optional `docs` extra** — the tool returns an actionable structured error when it isn't installed.

**Tech Stack:** Python 3.12, Microsoft Agent Framework, Docling (`docling>=2.0`, optional extra), pytest. Unit tests inject a fake converter (no real Docling, no network); a real Docling run is gated behind `HARNESS_LIVE_DOCS=1`.

**Design decisions locked for this plan (from the spec, resolved here where the spec was silent):**
- **Converter seam:** `read_document(session, source, convert=None)` where `convert: Callable[[str], str]` returns markdown. Default `_docling_convert` lazily imports Docling. This is the test seam — unit tests never touch real Docling.
- **Routing:** `urlparse(source).scheme` → `http`/`https` passes the URL straight to the converter (Docling fetches it); empty scheme is a workspace path resolved with `safe_path`; any other scheme is a structured error.
- **Errors never raise into the loop** (per spec): bad path (escape), unknown scheme, conversion failure (unsupported/corrupt), and missing-Docling all return `{"error": ..., "source": source}`.
- **Bundle placement:** `web` (the document-ingestion phase is the research/ingestion phase, and `web` is in the default bundle set, so `read_document` is available by default).
- **Dependency:** optional extra `docs = ["docling>=2.0"]`; lazy import; not in core deps.

---

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `harness/tools/documents.py` | **Create** | `read_document(session, source, convert=None)` + `_docling_convert` seam |
| `tests/test_documents.py` | **Create** | Unit tests via injected converter (routing, path-jail, error paths) |
| `harness/tools/registry.py` | Modify | Import + wrap `read_document`; add to returned list |
| `harness/bundles.py` | Modify | Add `read_document` to the `web` bundle + instruction fragment |
| `tests/test_registry.py` | Modify | Update expected tool-name set; assert `read_document` present |
| `tests/test_bundles.py` | Modify | Update default tool set; assert `read_document` in `web` + instructions |
| `pyproject.toml` | Modify | Add `[project.optional-dependencies] docs = ["docling>=2.0"]` |
| `tests/test_live_documents.py` | **Create** | Real-Docling smoke test gated behind `HARNESS_LIVE_DOCS=1` |
| `README.md` | Modify | Tool-surface row, `docs` extra note, roadmap update |

---

## Task 1: `read_document` implementation + unit tests

**Files:**
- Create: `harness/tools/documents.py`
- Test: `tests/test_documents.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_documents.py`:

```python
import pytest

from harness import HarnessConfig, Session
from harness.tools.documents import read_document


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def test_path_source_converts_to_markdown_handle(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    summary = read_document(sess, "report.pdf", convert=lambda src: "# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert summary["kind"] == "text"
    assert "Title" in summary["preview"]
    assert summary["source"] == "read_document(report.pdf)"
    # the full markdown round-trips out of the handle store
    hid = summary["id"]
    assert "| a | b |" in sess.store.get(hid)


def test_path_is_resolved_under_root_before_conversion(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "sub").mkdir()
    (sess.root / "sub" / "doc.docx").write_bytes(b"x")
    seen = {}

    def fake_convert(src):
        seen["src"] = src
        return "# ok"

    read_document(sess, "sub/doc.docx", convert=fake_convert)
    # converter receives an absolute path inside the root, not the raw relative string
    assert seen["src"] == str(sess.root / "sub" / "doc.docx")


def test_url_source_is_passed_through_unchanged(tmp_path):
    sess = _session(tmp_path)
    seen = {}

    def fake_convert(src):
        seen["src"] = src
        return "# remote"

    summary = read_document(sess, "https://example.com/a.pdf", convert=fake_convert)
    assert seen["src"] == "https://example.com/a.pdf"   # not path-resolved
    assert summary["kind"] == "text"


def test_path_escape_returns_structured_error(tmp_path):
    sess = _session(tmp_path)
    out = read_document(sess, "../../etc/passwd", convert=lambda src: "nope")
    assert "error" in out and "escape" in out["error"].lower()
    assert out["source"] == "../../etc/passwd"
    assert not sess.handles                              # nothing converted or stored


def test_unknown_scheme_returns_structured_error(tmp_path):
    sess = _session(tmp_path)
    out = read_document(sess, "ftp://host/f.pdf", convert=lambda src: "nope")
    assert "error" in out and "scheme" in out["error"].lower()
    assert not sess.handles


def test_conversion_failure_returns_structured_error(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "broken.pdf").write_bytes(b"x")

    def boom(src):
        raise ValueError("corrupt pdf")

    out = read_document(sess, "broken.pdf", convert=boom)
    assert "error" in out and "could not read document" in out["error"].lower()
    assert "corrupt pdf" in out["error"]
    assert out["source"] == "broken.pdf"
    assert not sess.handles


def test_missing_docling_returns_actionable_error(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "x.pdf").write_bytes(b"x")

    def not_installed(src):
        raise ModuleNotFoundError("No module named 'docling'")

    out = read_document(sess, "x.pdf", convert=not_installed)
    assert "error" in out and "docs" in out["error"].lower()      # points at the extra
    assert not sess.handles
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_documents.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.tools.documents'`.

- [ ] **Step 3: Write the implementation**

Create `harness/tools/documents.py`:

```python
"""read_document: convert a PDF/Office/spreadsheet to clean markdown (with tables) via Docling.

The source may be a workspace-relative path (resolved + jailed by safe_path) or an http(s)
URL (passed straight to Docling, which fetches it). The markdown is stored as a text handle
and only the summary is returned, keeping large document text out of the model's context.

Following the fetch_url/web convention, every failure -- a path escaping the root, an unknown
scheme, an unsupported/corrupt file, or Docling not being installed -- is returned as a
structured {"error", "source"} dict rather than raised into the agent loop. The `convert`
seam is injectable so unit tests never need real Docling (heavy, downloads models).
"""

from __future__ import annotations

from typing import Callable
from urllib.parse import urlparse

from ..paths import PathEscapesRootError, safe_path
from ..session import Session


def _docling_convert(source: str) -> str:
    """Convert a local path or URL to markdown (tables preserved) via Docling. Lazy import:
    Docling is an optional, heavy dependency, so it is only imported when actually used."""
    from docling.document_converter import DocumentConverter

    result = DocumentConverter().convert(source)
    return result.document.export_to_markdown()


def read_document(session: Session, source: str,
                  convert: Callable[[str], str] | None = None) -> dict:
    """Convert ``source`` (a workspace path or http(s) URL) to a clean-markdown handle.

    Returns the handle summary on success, or ``{"error", "source"}`` on any failure.
    ``convert`` is an injectable converter (defaults to Docling) for testing.
    """
    scheme = urlparse(source).scheme
    if scheme in ("http", "https"):
        target = source
    elif scheme == "":
        try:
            target = str(safe_path(session.root, source))
        except PathEscapesRootError:
            return {"error": f"path escapes the workspace root: {source!r}", "source": source}
    else:
        return {"error": f"unsupported source scheme {scheme!r}; pass a workspace path or an "
                         "http(s) URL", "source": source}

    do_convert = convert or _docling_convert
    try:
        markdown = do_convert(target)
    except ImportError:
        return {"error": "document ingestion unavailable: install the 'docs' extra "
                         "(e.g. `uv sync --extra docs`) to enable Docling", "source": source}
    except Exception as e:  # noqa: BLE001 - unsupported/corrupt file etc. -> structured error
        return {"error": f"could not read document: {e}", "source": source}

    handle = session.store.put(markdown, source=f"read_document({source})", kind="text")
    return handle.summary()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_documents.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check harness/tools/documents.py tests/test_documents.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add harness/tools/documents.py tests/test_documents.py
git commit -m "feat(documents): read_document converts path/URL to a markdown handle via Docling"
```

---

## Task 2: Register `read_document` + add it to the `web` bundle

**Files:**
- Modify: `harness/tools/registry.py`
- Modify: `harness/bundles.py`
- Test: `tests/test_registry.py`, `tests/test_bundles.py`

- [ ] **Step 1: Update the failing tests first**

In `tests/test_registry.py`, update the expected-names set in `test_build_tools_returns_expected_named_callables` to include `read_document`:

```python
def test_build_tools_returns_expected_named_callables(tmp_path):
    tools = build_tools(_session(tmp_path))
    names = {t.__name__ for t in tools}
    assert names == {
        "read_file", "write_file", "list_files", "search",
        "fetch_url", "run_python", "inspect_handle",
        "web_search", "web_extract", "read_document",
    }
```

And add a focused presence + signature test below `test_build_tools_includes_web_tools`:

```python
def test_build_tools_includes_read_document(tmp_path):
    tools = {t.__name__: t for t in build_tools(_session(tmp_path))}
    assert "read_document" in tools
    params = list(inspect.signature(tools["read_document"]).parameters)
    assert params == ["source"]                 # no session/convert leaked to the model
```

In `tests/test_bundles.py`, update the default-set test and add web-bundle assertions:

```python
def test_tools_default_is_all_bundles(tmp_path):
    sess = _session(tmp_path)
    names = {t.__name__ for t in sess.tools()}
    assert names == {
        "inspect_handle", "run_python",
        "read_file", "write_file", "list_files", "search",
        "fetch_url", "web_search", "web_extract", "read_document",
    }


def test_read_document_is_in_web_bundle(tmp_path):
    sess = _session(tmp_path)
    assert "read_document" in {t.__name__ for t in sess.tools("web")}
    assert "read_document" not in {t.__name__ for t in sess.tools("files")}


def test_web_instructions_mention_read_document(tmp_path):
    sess = _session(tmp_path)
    assert "read_document" in sess.harness_instructions("web")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_registry.py tests/test_bundles.py -q`
Expected: FAIL — assertions on the missing `read_document` name.

- [ ] **Step 3: Register the tool in `registry.py`**

Add the import alongside the other web/fetch imports (after the `web` import line):

```python
from .documents import read_document as _read_document
```

Add the wrapper inside `build_tools` (place it next to `web_extract`):

```python
    def read_document(source: str) -> dict:
        """Convert a document (PDF/Office/spreadsheet) at a workspace path or http(s) URL to a
        clean markdown handle with tables preserved; returns the handle summary."""
        return _read_document(session, source)
```

Add `read_document` to the returned list:

```python
    return [read_file, write_file, list_files, search, fetch_url, run_python, inspect_handle,
            web_search, web_extract, read_document]
```

- [ ] **Step 4: Add it to the `web` bundle in `bundles.py`**

Change the `web` entry in `BUNDLE_TOOL_NAMES`:

```python
    "web": ("fetch_url", "web_search", "web_extract", "read_document"),
```

Extend the `web` entry in `BUNDLE_INSTRUCTIONS`:

```python
    "web": (
        "Use web_search to find pages, fetch_url to retrieve a page as clean markdown, and "
        "web_extract for clean content. Fetched bodies are stored as handles. "
        "Use read_document to turn a PDF/Office/spreadsheet file (a workspace path or an "
        "http(s) URL) into a clean markdown handle with tables preserved."
    ),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_registry.py tests/test_bundles.py -q`
Expected: PASS.

- [ ] **Step 6: Run the full suite + lint (no regressions)**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/ tests/`
Expected: all pass; `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add harness/tools/registry.py harness/bundles.py tests/test_registry.py tests/test_bundles.py
git commit -m "feat(documents): register read_document and add it to the web bundle"
```

---

## Task 3: Declare the optional `docs` extra + real-Docling smoke test

**Files:**
- Modify: `pyproject.toml`
- Test: `tests/test_live_documents.py`

- [ ] **Step 1: Add the optional dependency extra**

In `pyproject.toml`, add a new section immediately after the `dependencies = [...]` block (before `[project.scripts]`):

```toml
[project.optional-dependencies]
docs = ["docling>=2.0"]            # heavy + downloads models; opt-in. Enables read_document.
```

- [ ] **Step 2: Write the gated real-Docling smoke test**

Create `tests/test_live_documents.py`. It exercises the *default* `_docling_convert` path (no injected seam) against a tiny HTML file with a table — proving real Docling runs end-to-end and preserves tables. It is skipped unless `HARNESS_LIVE_DOCS=1` and Docling is importable, so CI stays network/model-free.

```python
import importlib.util
import os

import pytest

from harness import HarnessConfig, Session
from harness.tools.documents import read_document

_RUN = os.environ.get("HARNESS_LIVE_DOCS") == "1"
_HAS_DOCLING = importlib.util.find_spec("docling") is not None

pytestmark = pytest.mark.skipif(
    not (_RUN and _HAS_DOCLING),
    reason="set HARNESS_LIVE_DOCS=1 and install the 'docs' extra to run real Docling",
)


def test_real_docling_converts_table_to_markdown(tmp_path):
    sess = Session.create(HarnessConfig(root_dir=tmp_path / "r"))
    (sess.root / "t.html").write_text(
        "<html><body><h1>Sales</h1>"
        "<table><tr><th>region</th><th>units</th></tr>"
        "<tr><td>EU</td><td>12</td></tr></table></body></html>",
        encoding="utf-8",
    )
    summary = read_document(sess, "t.html")          # default converter -> real Docling
    assert summary["kind"] == "text"
    markdown = sess.store.get(summary["id"])
    assert "region" in markdown and "units" in markdown
    assert "|" in markdown                           # rendered as a markdown table
```

- [ ] **Step 3: Verify the gated test is skipped by default**

Run: `.venv/bin/python -m pytest tests/test_live_documents.py -q`
Expected: `1 skipped` (no `HARNESS_LIVE_DOCS`).

- [ ] **Step 4: Confirm the full suite still passes**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass, with the live-docs test skipped.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/test_live_documents.py
git commit -m "build(documents): add optional docs extra (docling) + gated real-Docling smoke test"
```

---

## Task 4: README documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a `read_document` row to the Tool surface table**

In the Tool surface table, add this row after the `web_extract` row:

```markdown
| `read_document(source)` | A workspace path or URL → clean markdown handle (tables preserved) via Docling; needs the `docs` extra |
```

- [ ] **Step 2: Note the optional extra in the Install section**

After the `uv sync --prerelease=allow` block, add:

```markdown
Document ingestion (`read_document`) uses [Docling](https://github.com/DS4SD/docling), an optional heavy dependency that downloads models on first use. Enable it with:

```bash
uv sync --prerelease=allow --extra docs
```
```

- [ ] **Step 3: Update the roadmap**

In "Status & roadmap", move document ingestion from Planned to Implemented. Change the Implemented sentence to include it:

```markdown
Implemented: foundation (handles, sandbox, path-jail), the agent loop + tool surface, the `Harness`/`solve()` API + CLI, web research (Tavily search/extract, Markdown fetch), and **document ingestion** (`read_document` via Docling — PDF/spreadsheet → Markdown with tables).
```

And delete the now-done bullet from the Planned list:

```markdown
- **Document ingestion** — `read_document` via Docling (PDF/spreadsheet → Markdown *with tables*).
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(readme): document read_document and the docs extra"
```

---

## Self-Review (completed during plan authoring)

**Spec coverage** (`2026-05-30-web-and-document-ingestion-design.md`, 2d-docs scope):
- `read_document(source)` path-or-URL → Docling → markdown(+tables) handle → summary — Task 1. ✓
- `tools/documents.py` new, lazy Docling import — Task 1. ✓
- `tools/registry.py` registers `read_document` — Task 2. ✓
- Structured-error convention, never raises into the loop (bad path / scheme / unsupported / corrupt / missing dep) — Task 1 tests + impl. ✓
- Unit-test routing (path vs URL) + errors via an **injected converter** (no real Docling) — Task 1. ✓
- Real Docling smoke test gated behind an opt-in env flag — Task 3 (`HARNESS_LIVE_DOCS=1`). ✓
- Regression guard: exercise the tool through `build_tools`, not just its impl — Task 2 (`test_build_tools_includes_read_document`). ✓

**Placeholder scan:** none — every code/step block is complete.

**Type/name consistency:** `read_document(session, source, convert=None)` and `_docling_convert(source) -> str` are used identically across Task 1 (impl/tests), Task 2 (registry wrapper `read_document(source)`), and Task 3 (live test calls the default path). Bundle key `web`, tool name `read_document`, handle `kind="text"`, error shape `{"error", "source"}` are consistent throughout.

**Note on bundle choice:** `read_document` is added to `web` (available by default, matches the research/ingestion phase). If a future caller wants document ingestion without web search, splitting a dedicated `docs` bundle is a clean follow-up — out of scope here.
