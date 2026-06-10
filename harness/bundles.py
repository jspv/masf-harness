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
    "web": ("fetch_url", "web_search", "web_extract", "read_document"),
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
        "web_extract for clean content. Fetched bodies are stored as handles. "
        "Use read_document to turn a PDF/Office/spreadsheet file (a workspace path or an "
        "http(s) URL) into a clean markdown handle with tables preserved."
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
