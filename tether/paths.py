"""Single chokepoint that confines every model-supplied path to the session root."""

from __future__ import annotations

from pathlib import Path


class PathEscapesRootError(ValueError):
    """Raised when a candidate path resolves outside the session root."""


def safe_path(root: Path | str, candidate: Path | str) -> Path:
    """Resolve ``candidate`` against ``root`` and guarantee it stays inside it.

    ``.resolve()`` normalizes ``..`` and follows symlinks, so a symlink inside
    the root that points outside is caught here rather than exploited.

    An empty/whitespace-only ``candidate`` is rejected: a file tool receiving it
    signals an upstream bug, not a request for the root. Note: a symlink *loop*
    inside the root is not rejected (it stays inside root, so it is not an escape);
    the OS raises ``OSError`` (ELOOP) when the returned path is later opened.
    """
    if not str(candidate).strip():
        raise PathEscapesRootError("empty candidate path")
    root_resolved = Path(root).resolve()
    p = Path(candidate)
    if not p.is_absolute():
        p = root_resolved / p
    resolved = p.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathEscapesRootError(f"path escapes root: {candidate!r}")
    return resolved
