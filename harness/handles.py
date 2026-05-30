"""Typed handles: large data lives on disk; only a lightweight summary enters context."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .paths import PathEscapesRootError, safe_path

_PREVIEW_CHARS = 800
_PREVIEW_ROWS = 5


@dataclass
class Handle:
    id: str
    kind: str  # "json" | "text" | "dataframe"
    path: str  # POSIX path relative to the session root
    source: str
    bytes: int
    preview: str
    schema: dict[str, str] | None = None
    n_rows: int | None = None
    n_cols: int | None = None

    def summary(self) -> dict[str, Any]:
        """Context-facing view: drop None fields to keep it compact."""
        return {k: v for k, v in asdict(self).items() if v is not None}


class HandleStore:
    """Persists objects under ``<root>/handles`` and tracks them by id."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.dir = self.root / "handles"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._handles: dict[str, Handle] = {}
        self._counter = 0

    def _new_id(self) -> str:
        self._counter += 1
        return f"h{self._counter}"

    @staticmethod
    def _detect_kind(obj: Any) -> str:
        import pandas as pd

        if isinstance(obj, pd.DataFrame):
            return "dataframe"
        if isinstance(obj, (dict, list)):
            return "json"
        if isinstance(obj, str):
            return "text"
        raise TypeError(f"unsupported handle object type: {type(obj)!r}")

    def put(self, obj: Any, source: str, *, id: str | None = None,
            kind: str | None = None) -> Handle:
        hid = id or self._new_id()
        if id is not None:
            self._advance_counter(hid)  # keep auto-ids from colliding with an explicit id
        kind = kind or self._detect_kind(obj)
        if kind == "dataframe":
            handle = self._write_dataframe(hid, obj, source)
        elif kind == "json":
            handle = self._write_json(hid, obj, source)
        elif kind == "text":
            handle = self._write_text(hid, obj, source)
        else:
            raise ValueError(f"unknown handle kind: {kind!r}")
        self._handles[hid] = handle
        return handle

    def _write_dataframe(self, hid: str, df: Any, source: str) -> Handle:
        rel = f"handles/{hid}.parquet"
        path = self.root / rel
        df.to_parquet(path)
        preview = df.head(_PREVIEW_ROWS).to_csv(index=False)
        preview += f"... ({_PREVIEW_ROWS} of {len(df)} rows)" if len(df) > _PREVIEW_ROWS else ""
        return Handle(
            id=hid, kind="dataframe", path=rel, source=source,
            bytes=path.stat().st_size, preview=preview,
            schema={c: str(t) for c, t in df.dtypes.items()},
            n_rows=int(len(df)), n_cols=int(df.shape[1]),
        )

    def _write_json(self, hid: str, obj: Any, source: str) -> Handle:
        # ``default=str`` keeps non-JSON-native types (datetime, Decimal, ...) from
        # crashing serialization, but they round-trip back as strings via get().
        rel = f"handles/{hid}.json"
        path = self.root / rel
        text = json.dumps(obj, default=str)
        path.write_text(text, encoding="utf-8")
        return Handle(
            id=hid, kind="json", path=rel, source=source,
            bytes=len(text.encode()), preview=text[:_PREVIEW_CHARS],
        )

    def _write_text(self, hid: str, obj: str, source: str) -> Handle:
        rel = f"handles/{hid}.txt"
        path = self.root / rel
        path.write_text(obj, encoding="utf-8")
        return Handle(
            id=hid, kind="text", path=rel, source=source,
            bytes=len(obj.encode()), preview=obj[:_PREVIEW_CHARS],
        )

    def _advance_counter(self, hid: str) -> None:
        """Keep the auto-id counter ahead of an externally-supplied ``h<N>`` id."""
        if hid.startswith("h") and hid[1:].isdigit():
            self._counter = max(self._counter, int(hid[1:]))

    def register(self, record: dict[str, Any]) -> Handle:
        """Register a handle whose file already exists (e.g. written by the sandbox child).

        The ``path`` is supplied by the lower-trust child over the jsonl channel, so it
        is run through ``safe_path``: a record pointing outside the root is rejected here
        rather than letting the trusted parent later read an arbitrary file.
        """
        try:
            handle = Handle(**record)
        except TypeError as e:  # cross-process contract boundary — give a useful message
            raise ValueError(f"invalid handle record {record!r}: {e}") from e
        try:
            safe_path(self.root, handle.path)
        except PathEscapesRootError as e:
            raise ValueError(f"handle record path escapes root: {record!r}") from e
        self._handles[handle.id] = handle
        self._advance_counter(handle.id)
        return handle

    def get(self, handle_id: str) -> Any:
        try:
            handle = self._handles[handle_id]
        except KeyError:
            raise KeyError(f"no handle with id {handle_id!r}") from None
        path = safe_path(self.root, handle.path)  # defense-in-depth before any read
        if handle.kind == "dataframe":
            import pandas as pd
            return pd.read_parquet(path)
        if handle.kind == "json":
            return json.loads(path.read_text(encoding="utf-8"))
        return path.read_text(encoding="utf-8")

    def summary(self, handle_id: str) -> dict[str, Any]:
        return self._handles[handle_id].summary()

    def manifest(self) -> dict[str, Any]:
        return {hid: h.summary() for hid, h in self._handles.items()}

    def manifest_handles(self) -> dict[str, Handle]:
        return dict(self._handles)
