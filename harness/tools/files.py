"""File tools, all confined to the session root via safe_path."""

from __future__ import annotations

from pathlib import Path

from ..paths import safe_path
from ..session import Session

_DEFAULT_LIMIT = 2000


def read_file(session: Session, path: str, offset: int = 0, limit: int = _DEFAULT_LIMIT) -> str:
    """Read up to ``limit`` lines starting at line ``offset`` from a file under the root."""
    if offset < 0 or limit < 0:
        raise ValueError("offset and limit must be non-negative")
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
