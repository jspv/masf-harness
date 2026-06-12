import tether.tools.documents as docmod
from tether import TetherConfig, Session
from tether.config import DocumentConfig
from tether.tools.documents import prefetch_models, read_document


def _session(tmp_path):
    return Session.create(TetherConfig(root_dir=tmp_path / "r"))


def test_path_source_converts_to_markdown_handle(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    summary = read_document(sess, "report.pdf", convert=lambda src: "# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert summary["kind"] == "text"
    assert "Title" in summary["preview"]
    assert summary["source"] == "read_document(report.pdf)"
    hid = summary["id"]
    assert "| a | b |" in sess.store.get(hid)


def test_path_is_resolved_under_root_before_conversion(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "sub").mkdir()
    (sess.root / "sub" / "doc.docx").write_bytes(b"x")
    seen = {}

    def fake_convert(src):
        seen["src"] = src
        return "# ok"

    read_document(sess, "sub/doc.docx", convert=fake_convert)
    assert seen["src"] == str(sess.root / "sub" / "doc.docx")


def test_url_source_is_passed_through_unchanged(tmp_path):
    sess = _session(tmp_path)
    seen = {}

    def fake_convert(src):
        seen["src"] = src
        return "# remote"

    summary = read_document(sess, "https://example.com/a.pdf", convert=fake_convert)
    assert seen["src"] == "https://example.com/a.pdf"
    assert summary["kind"] == "text"


def test_path_escape_returns_structured_error(tmp_path):
    sess = _session(tmp_path)
    out = read_document(sess, "../../etc/passwd", convert=lambda src: "nope")
    assert "error" in out and "escape" in out["error"].lower()
    assert out["source"] == "../../etc/passwd"
    assert not sess.handles


def test_unknown_scheme_returns_structured_error(tmp_path):
    sess = _session(tmp_path)
    out = read_document(sess, "ftp://host/f.pdf", convert=lambda src: "nope")
    assert "error" in out and "scheme" in out["error"].lower()
    assert not sess.handles


def test_conversion_failure_returns_structured_error(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "broken.pdf").write_bytes(b"x")

    def boom(src):
        raise ValueError("corrupt pdf")

    out = read_document(sess, "broken.pdf", convert=boom)
    assert "error" in out and "could not read document" in out["error"].lower()
    assert "corrupt pdf" in out["error"]
    assert out["source"] == "broken.pdf"
    assert not sess.handles


def test_missing_docling_returns_actionable_error(tmp_path):
    sess = _session(tmp_path)
    (sess.root / "x.pdf").write_bytes(b"x")

    def not_installed(src):
        raise ModuleNotFoundError("No module named 'docling'")

    out = read_document(sess, "x.pdf", convert=not_installed)
    assert "error" in out and "docling" in out["error"].lower()
    assert "--extra docling" in out["error"]
    assert not sess.handles


def test_default_converter_uses_config_ocr_off_by_default(tmp_path, monkeypatch):
    sess = _session(tmp_path)                                   # default DocumentConfig: ocr=False
    (sess.root / "f.pdf").write_bytes(b"x")
    captured = {}
    monkeypatch.setattr(docmod, "_docling_convert",
                        lambda src, ocr: captured.update(src=src, ocr=ocr) or "# md")
    out = read_document(sess, "f.pdf")                          # no injected converter -> default path
    assert out["kind"] == "text"
    assert captured["ocr"] is False
    assert captured["src"] == str(sess.root / "f.pdf")


def test_default_converter_enables_ocr_when_configured(tmp_path, monkeypatch):
    sess = Session.create(TetherConfig(root_dir=tmp_path / "r", documents=DocumentConfig(ocr=True)))
    (sess.root / "f.pdf").write_bytes(b"x")
    captured = {}
    monkeypatch.setattr(docmod, "_docling_convert",
                        lambda src, ocr: captured.update(ocr=ocr) or "# md")
    read_document(sess, "f.pdf")
    assert captured["ocr"] is True


def test_prefetch_models_skips_ocr_models_by_default():
    calls = {}
    prefetch_models(downloader=lambda **kw: calls.update(kw))
    assert calls["with_rapidocr"] is False                      # matches the OCR-off default


def test_prefetch_models_includes_ocr_models_when_requested():
    calls = {}
    prefetch_models(downloader=lambda **kw: calls.update(kw), ocr=True)
    assert calls["with_rapidocr"] is True
