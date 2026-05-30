"""File tools, all confined to the session root via safe_path."""

from __future__ import annotations

from pathlib import Path

from ..paths import safe_path
from ..session import Session

_DEFAULT_LIMIT = 2000
_MAX_READ_CHARS = 50_000  # hard cap so a single huge line (e.g. minified HTML) can't flood context


def read_file(session: Session, path: str, offset: int = 0, limit: int = _DEFAULT_LIMIT,
              max_chars: int = _MAX_READ_CHARS) -> str:
    """Read up to ``limit`` lines starting at line ``offset`` from a file under the root.

    The result is also capped at ``max_chars`` characters so that a file with very long
    lines (minified HTML/JSON, one-line dumps) cannot blow the model's context window.
    When truncated, a note tells the agent to narrow the read or use ``search``.
    """
    if offset < 0 or limit < 0:
        raise ValueError("offset and limit must be non-negative")
    target = safe_path(session.root, path)
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    text = "".join(lines[offset:offset + limit])
    if len(text) > max_chars:
        text = (text[:max_chars]
                + f"\n... [truncated at {max_chars} chars; narrow the read with offset/limit "
                  "or use search to locate what you need]")
    return text


def write_file(session: Session, path: str, content: str) -> str:
    """Write ``content`` to a file under the root, creating parent dirs. Returns a confirmation."""
    target = safe_path(session.root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    rel = target.relative_to(session.root).as_posix()
    return f"wrote {len(content.encode())} bytes to {rel}"


def list_files(session: Session, path: str = ".") -> list[str]:
    """List files (recursively) under ``path`` as root-relative POSIX paths."""
    base = safe_path(session.root, path or ".")  # "" / None -> the root
    if base.is_file():
        return [base.relative_to(session.root).as_posix()]
    out: list[str] = []
    for p in sorted(base.rglob("*")):
        if p.is_file():
            out.append(p.relative_to(session.root).as_posix())
    return out
