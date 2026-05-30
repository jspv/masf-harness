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
