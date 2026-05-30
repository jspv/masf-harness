"""inspect_handle: on-demand deeper look at a stored handle (more preview / stats)."""

from __future__ import annotations

from ..session import Session


def inspect_handle(session: Session, handle_id: str, rows: int = 20,
                   stats: bool = False) -> dict:
    """Return a deeper view of a handle than its summary: more preview lines for text/json,
    and head rows (plus optional describe() stats) for dataframes."""
    handle = session.store.manifest_handles()[handle_id]  # raises KeyError if unknown
    obj = session.store.get(handle_id)
    out = handle.summary()
    if handle.kind == "dataframe":
        out["head"] = obj.head(rows).to_dict(orient="records")
        if stats:
            out["describe"] = obj.describe().to_dict()
    else:
        text = obj if isinstance(obj, str) else _json_text(obj)
        out["preview"] = "\n".join(text.splitlines()[:rows])
    return out


def _json_text(obj) -> str:
    import json

    return json.dumps(obj, indent=2, default=str)
