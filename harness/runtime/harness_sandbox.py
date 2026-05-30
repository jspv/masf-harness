"""Injected into the sandbox subprocess as the top-level module ``harness_sandbox``.

Communicates with the parent harness only via env vars and files:
  HARNESS_ROOT         session root directory
  HARNESS_REGISTRY     json file: { handle_id: {kind, path} } for existing handles
  HARNESS_NEW_HANDLES  jsonl file this module appends new-handle records to
  HARNESS_EMIT         json file this module writes the emit() payload to
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_ROOT = Path(os.environ["HARNESS_ROOT"])
_REGISTRY = json.loads(Path(os.environ["HARNESS_REGISTRY"]).read_text(encoding="utf-8"))
_NEW = Path(os.environ["HARNESS_NEW_HANDLES"])
_EMIT = Path(os.environ["HARNESS_EMIT"])

_PREVIEW_CHARS = 800
_PREVIEW_ROWS = 5


def load(handle_id: str) -> Any:
    meta = _REGISTRY[handle_id]
    path = _ROOT / meta["path"]
    kind = meta["kind"]
    if kind == "dataframe":
        import pandas as pd
        return pd.read_parquet(path)
    if kind == "json":
        return json.loads(path.read_text(encoding="utf-8"))
    return path.read_text(encoding="utf-8")


def save(handle_id: str, obj: Any, source: str = "run_python") -> str:
    import pandas as pd

    if isinstance(obj, pd.DataFrame):
        rel = f"handles/{handle_id}.parquet"
        obj.to_parquet(_ROOT / rel)
        # Preview must match HandleStore._write_dataframe exactly (kept in sync by hand;
        # the child cannot import the harness package). See tests for parity check.
        preview = obj.head(_PREVIEW_ROWS).to_csv(index=False)
        if len(obj) > _PREVIEW_ROWS:
            preview += f"... ({_PREVIEW_ROWS} of {len(obj)} rows)"
        rec = {"id": handle_id, "kind": "dataframe", "path": rel, "source": source,
               "bytes": (_ROOT / rel).stat().st_size, "preview": preview,
               "schema": {c: str(t) for c, t in obj.dtypes.items()},
               "n_rows": int(len(obj)), "n_cols": int(obj.shape[1])}
    elif isinstance(obj, (dict, list)):
        rel = f"handles/{handle_id}.json"
        text = json.dumps(obj, default=str)
        (_ROOT / rel).write_text(text, encoding="utf-8")
        rec = {"id": handle_id, "kind": "json", "path": rel, "source": source,
               "bytes": len(text.encode()), "preview": text[:_PREVIEW_CHARS]}
    else:
        rel = f"handles/{handle_id}.txt"
        text = str(obj)
        (_ROOT / rel).write_text(text, encoding="utf-8")
        rec = {"id": handle_id, "kind": "text", "path": rel, "source": source,
               "bytes": len(text.encode()), "preview": text[:_PREVIEW_CHARS]}

    with _NEW.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return handle_id


def emit(obj: Any) -> None:
    _EMIT.write_text(json.dumps(obj, default=str), encoding="utf-8")
