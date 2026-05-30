# Harness Phase 2d-web — Web Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the agent open-web research: a Tavily-backed `web_search`, a Tavily `web_extract`, and a `fetch_url` that returns clean trafilatura markdown instead of raw HTML — fixing the eval tasks (t07/t08) that fail today.

**Architecture:** Two new Session-bound tools in `harness/tools/web.py` (Tavily `/search` and `/extract` over `httpx`, injectable client for tests, structured-error returns), plus a `fetch_url` change to convert HTML→markdown via `trafilatura`. Tools register in `build_tools`. Tavily key resolves from `TAVILY_API_KEY` in `.env` (handled by `Harness`). Document ingestion (Docling `read_document`) is the separate 2d-docs plan.

**Tech Stack:** Python 3.12, `httpx` (already present), `trafilatura` (already added), `pytest` with `httpx.MockTransport` for deterministic no-network tests.

**Spec:** `docs/superpowers/specs/2026-05-30-web-and-document-ingestion-design.md`

**Builds on (Phase 1/2):** `harness.Session` (`.store`, `.config`), `HarnessConfig`, `HandleStore.put(obj, source, kind=...)` → `Handle.summary()`, `harness.tools.registry.build_tools`, `harness.api.Harness`. Tavily verified (REST: `POST https://api.tavily.com/search` and `/extract`, key in JSON body). trafilatura verified: `trafilatura.extract(html, output_format="markdown")` returns clean markdown, dropping nav/footer.

---

## File Structure

- Modify: `harness/config.py` — add `SearchConfig` + `HarnessConfig.search`
- Create: `harness/tools/web.py` — `web_search`, `web_extract` (+ Tavily helpers)
- Modify: `harness/tools/fetch.py` — HTML→markdown via trafilatura + `raw` flag
- Modify: `harness/tools/registry.py` — register `web_search`, `web_extract`
- Modify: `harness/api.py` — resolve `TAVILY_API_KEY` from `.env` into `config.search`
- Create tests: `tests/test_search_config.py`, `tests/tools/test_web.py`; modify `tests/tools/test_fetch.py`, `tests/test_registry.py`

---

## Task 1: `SearchConfig` on `HarnessConfig`

**Files:**
- Modify: `harness/config.py`
- Test: `tests/test_search_config.py`

- [ ] **Step 1: Write the failing test** — create `tests/test_search_config.py`:

```python
from harness.config import HarnessConfig, SearchConfig


def test_search_config_defaults():
    cfg = HarnessConfig()
    assert isinstance(cfg.search, SearchConfig)
    assert cfg.search.provider == "tavily"
    assert cfg.search.api_key is None
    assert cfg.search.max_results == 5
    assert cfg.search.timeout_s == 20.0


def test_search_config_independent_between_instances():
    assert HarnessConfig().search is not HarnessConfig().search
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_search_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'SearchConfig'`

- [ ] **Step 3: Implement** — in `harness/config.py`, add the `SearchConfig` dataclass (after `FetchConfig`) and a `search` field on `HarnessConfig`.

Add this dataclass:
```python
@dataclass
class SearchConfig:
    provider: str = "tavily"
    api_key: str | None = None
    max_results: int = 5
    timeout_s: float = 20.0
```

Add to `HarnessConfig` (alongside the existing `sandbox`/`fetch` fields):
```python
    search: SearchConfig = field(default_factory=SearchConfig)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_search_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add harness/config.py tests/test_search_config.py
git commit -m "feat(web): add SearchConfig (tavily) to HarnessConfig"
```

---

## Task 2: `web_search` (Tavily /search)

**Files:**
- Create: `harness/tools/web.py`
- Test: `tests/tools/test_web.py`

- [ ] **Step 1: Write the failing tests** — create `tests/tools/test_web.py`:

```python
import httpx

from harness import HarnessConfig, Session
from harness.tools.web import web_search


def _session(tmp_path, api_key="test-key"):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    cfg.search.api_key = api_key
    return Session.create(cfg)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_web_search_returns_answer_and_results(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        assert request.url.path == "/search"
        return httpx.Response(200, json={
            "answer": "Flagship input is $5/M, output $25/M.",
            "results": [
                {"title": "Pricing", "url": "https://x.com/p", "content": "five dollars", "score": 0.9},
                {"title": "Docs", "url": "https://x.com/d", "content": "twenty five", "score": 0.8},
            ],
        })

    out = web_search(sess, "model pricing", client=_client(handler))
    assert out["answer"].startswith("Flagship")
    assert len(out["results"]) == 2
    assert out["results"][0]["url"] == "https://x.com/p"
    assert out["results"][0]["title"] == "Pricing"


def test_web_search_clips_long_snippets(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(200, json={"answer": None,
            "results": [{"title": "T", "url": "https://x", "content": "z" * 5000, "score": 1.0}]})

    out = web_search(sess, "q", client=_client(handler))
    assert len(out["results"][0]["content"]) <= 500


def test_web_search_missing_key_returns_error(tmp_path):
    sess = _session(tmp_path, api_key=None)
    out = web_search(sess, "q")
    assert "error" in out
    assert "TAVILY_API_KEY" in out["error"]


def test_web_search_http_error_is_structured(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(401, text="unauthorized")

    out = web_search(sess, "q", client=_client(handler))
    assert out["status"] == 401
    assert "error" in out
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/tools/test_web.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.tools.web'`

- [ ] **Step 3: Implement** — create `harness/tools/web.py`:

```python
"""Web research tools backed by Tavily (search + extract).

Failures return structured error dicts (never raise into the agent loop), matching the
fetch_url convention. A missing TAVILY_API_KEY returns a clear, actionable message.
"""

from __future__ import annotations

import httpx

from ..session import Session

_SEARCH_URL = "https://api.tavily.com/search"
_EXTRACT_URL = "https://api.tavily.com/extract"
_SNIPPET_CHARS = 500


def web_search(session: Session, query: str, max_results: int = 5,
               client: httpx.Client | None = None) -> dict:
    """Search the web. Returns {"answer": str|None, "results": [{title, url, content, score}]}.
    `content` is a clipped extracted snippet. Use the urls with web_extract/fetch_url for more."""
    cfg = session.config.search
    if not cfg.api_key:
        return {"error": "web search unavailable: set TAVILY_API_KEY in .env"}
    owns = client is None
    client = client or httpx.Client(timeout=cfg.timeout_s)
    try:
        try:
            resp = client.post(_SEARCH_URL, json={
                "api_key": cfg.api_key, "query": query,
                "max_results": max_results, "include_answer": True,
                "search_depth": "basic",
            })
        except httpx.HTTPError as e:
            return {"error": f"search request failed: {e}"}
        if resp.is_error:
            return {"error": f"search HTTP {resp.status_code}", "status": resp.status_code}
        data = resp.json()
        results = [{
            "title": r.get("title"),
            "url": r.get("url"),
            "content": (r.get("content") or "")[:_SNIPPET_CHARS],
            "score": r.get("score"),
        } for r in data.get("results", [])]
        return {"answer": data.get("answer"), "results": results}
    finally:
        if owns:
            client.close()
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/tools/test_web.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add harness/tools/web.py tests/tools/test_web.py
git commit -m "feat(web): add web_search (Tavily) tool"
```

---

## Task 3: `web_extract` (Tavily /extract → handle)

**Files:**
- Modify: `harness/tools/web.py`
- Modify: `tests/tools/test_web.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/tools/test_web.py`:

```python
from harness.tools.web import web_extract


def test_web_extract_stores_markdown_handle(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        assert request.url.path == "/extract"
        return httpx.Response(200, json={
            "results": [{"url": "https://x.com/p", "raw_content": "# Pricing\n\n$5 per million"}],
            "failed_results": [],
        })

    out = web_extract(sess, "https://x.com/p", client=_client(handler))
    assert out["kind"] == "text"
    assert sess.store.get(out["id"]) == "# Pricing\n\n$5 per million"


def test_web_extract_no_content_returns_error(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(200, json={"results": [], "failed_results": [{"url": "https://x"}]})

    out = web_extract(sess, "https://x", client=_client(handler))
    assert "error" in out
    assert out["url"] == "https://x"


def test_web_extract_missing_key_returns_error(tmp_path):
    sess = _session(tmp_path, api_key=None)
    out = web_extract(sess, "https://x")
    assert "error" in out and "TAVILY_API_KEY" in out["error"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/tools/test_web.py -v`
Expected: FAIL — `ImportError: cannot import name 'web_extract'`

- [ ] **Step 3: Implement** — append to `harness/tools/web.py`:

```python
def web_extract(session: Session, url: str, client: httpx.Client | None = None) -> dict:
    """Fetch a URL's clean content via Tavily /extract, store it as a markdown handle, and
    return the handle summary. Returns a structured error on failure."""
    cfg = session.config.search
    if not cfg.api_key:
        return {"error": "web extract unavailable: set TAVILY_API_KEY in .env"}
    owns = client is None
    client = client or httpx.Client(timeout=cfg.timeout_s)
    try:
        try:
            resp = client.post(_EXTRACT_URL, json={"api_key": cfg.api_key, "urls": [url]})
        except httpx.HTTPError as e:
            return {"error": f"extract request failed: {e}", "url": url}
        if resp.is_error:
            return {"error": f"extract HTTP {resp.status_code}", "status": resp.status_code, "url": url}
        results = resp.json().get("results", [])
        if not results:
            return {"error": "no content extracted", "url": url}
        content = results[0].get("raw_content") or results[0].get("content") or ""
        handle = session.store.put(content, source=f"web_extract({url})", kind="text")
        return handle.summary()
    finally:
        if owns:
            client.close()
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/tools/test_web.py -v`
Expected: PASS (7 passed total in the file)

- [ ] **Step 5: Commit**

```bash
git add harness/tools/web.py tests/tools/test_web.py
git commit -m "feat(web): add web_extract (Tavily) -> markdown handle"
```

---

## Task 4: `fetch_url` → trafilatura HTML→markdown

**Files:**
- Modify: `harness/tools/fetch.py`
- Modify: `tests/tools/test_fetch.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/tools/test_fetch.py`:

```python
_HTML_PAGE = (
    "<html><head><title>T</title></head><body>"
    "<nav>Home About Contact Login Signup</nav>"
    "<article><h1>Pricing Details</h1>"
    "<p>Our flagship model costs 5 dollars per million input tokens.</p>"
    "<p>Output tokens are billed at 25 dollars per million.</p>"
    "<p>Batch processing gives a 50 percent discount on all usage.</p></article>"
    "<footer>Copyright 2026. Cookie banner here. Terms of service.</footer></body></html>"
)


def test_fetch_html_is_converted_to_clean_markdown(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(200, text=_HTML_PAGE, headers={"content-type": "text/html"})

    summary = fetch_url(sess, "https://example.com/pricing", client=_client(handler))
    body = sess.store.get(summary["id"])
    assert "flagship model costs 5 dollars" in body   # main content kept
    assert "Cookie banner" not in body                # footer dropped
    assert "Signup" not in body                       # nav dropped
    assert "<article>" not in body                    # markdown, not raw HTML


def test_fetch_html_raw_flag_keeps_raw(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(200, text=_HTML_PAGE, headers={"content-type": "text/html"})

    summary = fetch_url(sess, "https://example.com/pricing", raw=True, client=_client(handler))
    assert "<article>" in sess.store.get(summary["id"])  # raw HTML preserved


def test_fetch_non_html_text_unchanged(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(200, text="just plain text", headers={"content-type": "text/plain"})

    summary = fetch_url(sess, "https://example.com/p.txt", client=_client(handler))
    assert sess.store.get(summary["id"]) == "just plain text"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/tools/test_fetch.py -v`
Expected: FAIL — `fetch_url() got an unexpected keyword argument 'raw'`

- [ ] **Step 3: Implement** — modify `harness/tools/fetch.py`. Change the signature to add `raw: bool = False`, and replace the body-handling block.

Change the signature line:
```python
def fetch_url(session: Session, url: str, max_bytes: int | None = None,
              raw: bool = False, client: httpx.Client | None = None) -> dict:
```

Replace the existing block that starts at `body = resp.content` and ends at `return summary` with:
```python
        body = resp.content
        truncated = len(body) > limit
        content_type = resp.headers.get("content-type", "")
        text = body[:limit].decode(resp.encoding or "utf-8", errors="replace")

        if "json" in content_type and not truncated:
            handle = session.store.put(resp.json(), source=f"fetch_url({url})", kind="json")
        elif "html" in content_type and not raw:
            import trafilatura
            md = trafilatura.extract(text, output_format="markdown")
            stored = md if md else text  # fall back to raw text if extraction yields nothing
            handle = session.store.put(stored, source=f"fetch_url({url})", kind="text")
        else:
            handle = session.store.put(text, source=f"fetch_url({url})", kind="text")

        summary = handle.summary()
        if truncated:
            summary["truncated"] = True
            summary["full_bytes"] = len(body)
        return summary
```

Also update the docstring to mention HTML is returned as clean markdown unless `raw=True`.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/tools/test_fetch.py -v`
Expected: PASS (all fetch tests, including the 3 new ones)

If `test_fetch_html_is_converted_to_clean_markdown` fails because trafilatura returned `None` (too little content), make the `<article>` content longer/more paragraph-rich — do NOT weaken the content/boilerplate assertions.

- [ ] **Step 5: Commit**

```bash
git add harness/tools/fetch.py tests/tools/test_fetch.py
git commit -m "feat(web): fetch_url returns clean trafilatura markdown for HTML (raw= opt-out)"
```

---

## Task 5: register web tools + resolve `TAVILY_API_KEY`

**Files:**
- Modify: `harness/tools/registry.py`
- Modify: `harness/api.py`
- Modify: `tests/test_registry.py`
- Test: `tests/test_api.py` (add one)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_registry.py`:
```python
def test_build_tools_includes_web_tools(tmp_path):
    names = {t.__name__ for t in build_tools(_session(tmp_path))}
    assert {"web_search", "web_extract"} <= names
```

ALSO update the existing `test_build_tools_returns_expected_named_callables` in
`tests/test_registry.py` — it asserts the name set *exactly* equals the 7 built-ins, so add
the two web tools to its expected set:
```python
    assert names == {
        "read_file", "write_file", "list_files", "search",
        "fetch_url", "run_python", "inspect_handle",
        "web_search", "web_extract",
    }
```

Append to `tests/test_api.py`:
```python
def test_harness_resolves_tavily_key_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "env-key-123")
    from harness import Harness, HarnessConfig
    h = Harness(HarnessConfig(root_dir=tmp_path / "r"))
    assert h.config.search.api_key == "env-key-123"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_registry.py::test_build_tools_includes_web_tools tests/test_api.py::test_harness_resolves_tavily_key_from_env -v`
Expected: FAIL (web tools not registered; key not resolved)

- [ ] **Step 3: Implement**

In `harness/tools/registry.py`, add the import near the other tool imports:
```python
from .web import web_extract as _web_extract, web_search as _web_search
```
Inside `build_tools`, add two closures (before the `return`):
```python
    def web_search(query: str, max_results: int = 5) -> dict:
        """Search the web. Returns an answer + ranked results [{title,url,content,score}].
        Use the result urls with fetch_url or web_extract to read a page."""
        return _web_search(session, query, max_results)

    def web_extract(url: str) -> dict:
        """Fetch a URL's clean content via the search provider; returns a markdown handle."""
        return _web_extract(session, url)
```
And add them to the returned list:
```python
    return [read_file, write_file, list_files, search, fetch_url, run_python,
            inspect_handle, web_search, web_extract]
```

In `harness/api.py`, resolve the key in `Harness.__init__`. After `self.session = Session.create(self.config)`, add:
```python
        if self.config.search.api_key is None:
            from dotenv import load_dotenv
            load_dotenv()
            import os
            self.config.search.api_key = os.environ.get("TAVILY_API_KEY")
```
(`python-dotenv` is already a dependency via agent-framework-core.)

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_registry.py tests/test_api.py -v`
Expected: PASS (including the 2 new tests)

- [ ] **Step 5: Run the full suite + commit**

Run: `uv run pytest -q`
Expected: all pass (no regressions).

```bash
git add harness/tools/registry.py harness/api.py tests/test_registry.py tests/test_api.py
git commit -m "feat(web): register web_search/web_extract; resolve TAVILY_API_KEY from .env"
```

---

## Task 6: opt-in live test

**Files:**
- Test: `tests/test_live_web.py`

- [ ] **Step 1: Write the gated live test** — create `tests/test_live_web.py`:

```python
import os

import pytest

from harness import Harness, HarnessConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE") != "1",
    reason="set HARNESS_LIVE=1 (plus OPENAI_API_KEY and TAVILY_API_KEY in .env) for the live web test",
)


def test_live_web_research_pricing(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r", model="gpt-4o-mini")
    result = Harness(cfg).solve(
        "What are the current OpenAI API prices for their flagship model "
        "(input/output per million tokens)? Use web search and cite a source URL."
    )
    assert result.error is None
    # Some dollar figure per million tokens should appear.
    assert "$" in result.final_text or "per million" in result.final_text.lower()
```

- [ ] **Step 2: Confirm it skips without the flag**

Run: `uv run pytest tests/test_live_web.py -v`
Expected: 1 skipped.

- [ ] **Step 3: (Optional, manual) run it live**

Run: `HARNESS_LIVE=1 uv run pytest tests/test_live_web.py -v` (requires `TAVILY_API_KEY` + `OPENAI_API_KEY` in `.env`). Expected: PASS with real pricing in the answer. If it fails, that's a model/provider observation to note — do not weaken the unit tests.

- [ ] **Step 4: Commit**

```bash
git add tests/test_live_web.py
git commit -m "test(web): add opt-in live web-research test (HARNESS_LIVE=1)"
```

---

## Definition of done (2d-web)

- `uv run pytest` green (no network/API in CI; live test skipped).
- `web_search` + `web_extract` registered and working against mocked Tavily; `fetch_url`
  returns clean trafilatura markdown for HTML (`raw=True` to opt out).
- `TAVILY_API_KEY` resolves from `.env`; a missing key yields a clear model-readable error.
- The agent can now discover sources and read clean content — ready to retest eval t07/t08.
- Next: 2d-docs (`read_document` via Docling).
