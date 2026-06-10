"""read_document: convert a PDF/Office/spreadsheet to clean markdown (with tables) via Docling.

The source may be a workspace-relative path (resolved + jailed by safe_path) or an http(s)
URL (passed straight to Docling, which fetches it). The markdown is stored as a text handle
and only the summary is returned, keeping large document text out of the model's context.

Following the fetch_url/web convention, every failure -- a path escaping the root, an unknown
scheme, an unsupported/corrupt file, or Docling not being installed -- is returned as a
structured {"error", "source"} dict rather than raised into the agent loop. The `convert`
seam is injectable so unit tests never need real Docling (heavy, downloads models).
"""

from __future__ import annotations

from typing import Callable
from urllib.parse import urlparse

from ..paths import PathEscapesRootError, safe_path
from ..session import Session


def _docling_convert(source: str) -> str:
    """Convert a local path or URL to markdown (tables preserved) via Docling. Lazy import:
    Docling is an optional, heavy dependency, so it is only imported when actually used."""
    from docling.document_converter import DocumentConverter

    result = DocumentConverter().convert(source)
    return result.document.export_to_markdown()


def read_document(session: Session, source: str,
                  convert: Callable[[str], str] | None = None) -> dict:
    """Convert ``source`` (a workspace path or http(s) URL) to a clean-markdown handle.

    Returns the handle summary on success, or ``{"error", "source"}`` on any failure.
    ``convert`` is an injectable converter (defaults to Docling) for testing.
    """
    scheme = urlparse(source).scheme
    if scheme in ("http", "https"):
        target = source
    elif scheme == "":
        try:
            target = str(safe_path(session.root, source))
        except PathEscapesRootError:
            return {"error": f"path escapes the workspace root: {source!r}", "source": source}
    else:
        return {"error": f"unsupported source scheme {scheme!r}; pass a workspace path or an "
                         "http(s) URL", "source": source}

    do_convert = convert or _docling_convert
    try:
        markdown = do_convert(target)
    except ImportError:
        return {"error": "document ingestion unavailable: install the 'docs' extra "
                         "(e.g. `uv sync --extra docs`) to enable Docling", "source": source}
    except Exception as e:  # noqa: BLE001 - unsupported/corrupt file etc. -> structured error
        return {"error": f"could not read document: {e}", "source": source}

    handle = session.store.put(markdown, source=f"read_document({source})", kind="text")
    return handle.summary()
