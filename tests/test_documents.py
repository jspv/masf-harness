from harness import HarnessConfig, Session
from harness.tools.documents import read_document


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


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
    assert "error" in out and "docs" in out["error"].lower()
    assert not sess.handles
