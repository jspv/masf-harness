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

    # rg exit codes: 0 = match, 1 = no match, >=2 = error (e.g. invalid regex).
    # Surface errors the same way the Python fallback does, instead of silently
    # returning [] only when rg happens to be installed.
    if proc.returncode >= 2:
        raise ValueError(proc.stderr.strip() or "ripgrep search failed")

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
        # One hit per matching line (first submatch), with a CHARACTER column, to
        # match the Python fallback exactly. rg reports a byte offset, so convert it.
        line_text = data["lines"]["text"]
        sub = data["submatches"][0]
        char_col = len(line_text.encode("utf-8")[:sub["start"]].decode("utf-8", errors="ignore"))
        hits.append({
            "file": rel,
            "line": data["line_number"],
            "col": char_col,
            "text": line_text.rstrip("\n"),
        })
    return hits


def _search_python(root: Path, base: Path, pattern: str, ignore_case: bool,
                   max_matches: int, glob: str | None = None) -> list[dict]:
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:  # match the rg path: invalid regex -> ValueError
        raise ValueError(f"invalid search pattern: {e}") from e
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
