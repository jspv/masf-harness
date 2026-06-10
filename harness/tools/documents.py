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
from ..status import report_progress


def _docling_convert(source: str, ocr: bool = False) -> str:
    """Convert a local path or URL to markdown (tables preserved) via Docling. Lazy import:
    Docling is an optional, heavy dependency, so it is only imported when actually used.

    OCR defaults off: born-digital documents get their tables/structure from the layout and
    TableFormer models, so OCR only adds latency and extra model downloads. Pass ``ocr=True``
    for scanned/image documents.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pipeline = PdfPipelineOptions(do_ocr=ocr)
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline)}
    )
    return converter.convert(source).document.export_to_markdown()


def read_document(session: Session, source: str,
                  convert: Callable[[str], str] | None = None) -> dict:
    """Convert ``source`` (a workspace path or http(s) URL) to a clean-markdown handle.

    Returns the handle summary on success, or ``{"error", "source"}`` on any failure.
    ``convert`` is an injectable converter (defaults to Docling, honoring
    ``config.documents.ocr``) for testing.
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

    if convert is None:
        ocr = session.config.documents.ocr
        def convert(src: str) -> str:  # default converter honors the session's OCR setting
            return _docling_convert(src, ocr=ocr)

    report_progress(f"converting {source} via Docling", tool="read_document")
    try:
        markdown = convert(target)
    except ImportError:
        return {"error": "document ingestion unavailable: install the 'docling' extra "
                         "(e.g. `uv sync --extra docling`) to enable Docling", "source": source}
    except Exception as e:  # noqa: BLE001 - unsupported/corrupt file etc. -> structured error
        return {"error": f"could not read document: {e}", "source": source}

    handle = session.store.put(markdown, source=f"read_document({source})", kind="text")
    return handle.summary()


def prefetch_models(downloader: Callable[..., object] | None = None, *, ocr: bool = False) -> None:
    """Pre-download Docling's models so the first ``read_document`` call doesn't pay for it.

    Fetches the layout + TableFormer (and related) models by default; OCR models are pulled
    only when ``ocr=True``, matching the OCR-off conversion default. ``downloader`` is
    injectable for testing; by default it lazily imports Docling's model downloader.
    """
    if downloader is None:
        from docling.utils.model_downloader import download_models as downloader

    downloader(progress=True, with_rapidocr=ocr)


def _prefetch_main() -> None:
    """Console-script entry (``harness-prefetch-docling``): warm the Docling model cache."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="harness-prefetch-docling",
        description="Pre-download Docling models so the first read_document call is fast.",
    )
    parser.add_argument("--ocr", action="store_true",
                        help="also fetch OCR models (needed only for scanned/image documents)")
    args = parser.parse_args()
    prefetch_models(ocr=args.ocr)
    print("Docling models prefetched.")
