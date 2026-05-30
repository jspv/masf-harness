# Web Research Capability — Design Spec (Phase 2d)

- **Date:** 2026-05-30
- **Status:** Spec — needs a provider-choice brainstorm before implementation
- **Trigger:** Self-eval (`evals/run_evals.py`) + the user's pricing/current-events tasks. The
  harness is robust and strong at compute / data-integration / clean-API fetch (8/10 eval
  tasks pass), but **fails every open-web research task** (t07 OpenAI pricing, t08 "current
  CEO", and the earlier pricing run). A frontier-model Claude Code run on the same pricing
  question succeeded — because it has two tools we lack.

## Problem

The harness is **blind and dumb about the open web**:
1. **No web search.** The agent can only fetch URLs it already knows/guesses. When the right
   page 403s or redirects, it has no way to *discover* an alternative (e.g. the parseable
   `developers.openai.com/api/docs/pricing` docs page). It guesses a few URLs and gives up
   or falls back to stale model knowledge.
2. **`fetch_url` returns raw HTML.** Modern marketing/pricing pages are JS-rendered and
   bot-protected; the raw HTML is minified soup with the real content rendered client-side.
   The agent then thrashes trying to regex/search pages (t08 took 29 search calls), and even
   when it fetches a page the numbers often aren't in the HTML at all.

By contrast, Claude Code's `WebSearch` discovers the right docs URLs, and its `WebFetch`
returns HTML converted to markdown + LLM-extracted content. Those two capabilities are the
gap. (Model strength is a secondary factor — but no model can use a search tool that doesn't
exist or un-minify HTML our fetch never cleaned.)

## Goal

Give the agent the two missing web capabilities, as Session-bound tools registered in
`build_tools`, returning handle/summary outputs consistent with the existing surface:

### Component 1 — `web_search` tool
`web_search(query: str, max_results: int = 5) -> list[dict]` → ranked results as
`[{title, url, snippet}]`. This unlocks the "discover the right URL" move.

- **Pluggable provider** behind a small interface (so we can swap without touching the tool):
  candidates — **Tavily** (LLM-oriented, returns clean snippets), **Brave Search API**,
  **SerpAPI/Bing**, or **DuckDuckGo** (no key, lower quality). Provider + API key live in a
  new `SearchConfig` on `HarnessConfig` (key from `.env`).
- Implemented over `httpx` (provider REST API) with an **injectable client** for tests
  (`MockTransport`), mirroring `fetch_url`. No live calls in CI; one opt-in live test.
- Network/HTTP failures return a structured error dict (same convention as `fetch_url`).

### Component 2 — smart fetch (HTML → clean text/markdown)
Make fetched pages **digestible** instead of raw soup. Two sub-options (pick in brainstorm):
- **(a) Enhance `fetch_url`:** when content-type is HTML, convert to markdown/clean text
  (e.g. `trafilatura` for main-content extraction, or `readability-lxml` + `markdownify`)
  before storing the handle. Store both? Default to extracted text; keep raw available via a
  flag. Dramatically smaller, parseable handles.
- **(b) Separate `fetch_page(url, extract: str | None)` tool:** fetches + converts, and if
  `extract` (a question) is given, runs a cheap-model extraction pass returning just the
  relevant content (closest to Claude Code's `WebFetch`). Reuses the harness's own model.

JS-rendered pages remain a hard limit unless we add a headless-browser/render backend
(out of scope here; note it). Markdown extraction still helps server-rendered docs pages —
which is exactly where the working data lives (the docs-page pivot).

## Non-goals (for this phase)
- Headless-browser / JS rendering (separate, heavier capability — note for later).
- A general scraping framework. Keep to: search → fetch-clean → analyze-in-sandbox.

## Design notes / fit with existing harness
- Both tools are plain `session`-bound functions added to `harness/tools/` and registered in
  `build_tools` (and exempt from spill if they return summaries, like `fetch_url`).
- Outputs stay context-safe: `web_search` returns a small ranked list; smart fetch stores a
  (now much smaller) handle. The agent then uses `run_python`/`search`/`inspect_handle` over
  the clean text — its existing strengths.
- New dependencies: a search-provider key (`.env`) + an extraction lib (`trafilatura` or
  `readability-lxml`+`markdownify`). These are the reason a **provider/lib brainstorm** is
  needed before building.

## Open decisions (resolve in brainstorm before implementation)
1. **Search provider:** Tavily vs Brave vs SerpAPI/Bing vs DuckDuckGo (key availability,
   cost, snippet quality). The user has keys for which?
2. **Smart fetch shape:** enhance `fetch_url` (a) vs a separate `fetch_page` with optional
   LLM extraction (b). (b) matches Claude Code's WebFetch but costs an extra model call.
3. **Extraction lib:** `trafilatura` (best main-content extraction) vs `readability` +
   `markdownify` (lighter). 

## Testing strategy (when built)
- Unit: `web_search` against a `MockTransport` provider response; smart-fetch HTML→markdown
  on canned HTML (assert the soup shrinks and key text survives); structured-error paths.
- Integration: a stub-model agent run where `web_search` returns a canned result the model
  then `fetch_page`s — deterministic, no network.
- One opt-in live test (`HARNESS_LIVE=1`) re-running the OpenAI/Anthropic pricing task;
  success = real per-token numbers in the answer.
- Regression guard already added from this eval: every tool must be exercised through
  `build_tools` (not just its impl), since that path is what the agent actually uses.

## Eval evidence (2026-05-30)
`evals/run_evals.py`, gpt-4o-mini: 8/10 tasks correct (JSON-API fetch, compute, data
integration, CSV, multi-fetch compare, clean-text fetch, file output). The 2 failures
(t07 pricing, t08 current-events) are 100% open-web research and motivated this spec. Two
bugs were found and fixed during the eval (search broken via the package re-export; search
hit text unbounded → context overflow) — see commit history.
