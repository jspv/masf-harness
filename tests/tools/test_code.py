import pytest

from harness import HarnessConfig, Session
from harness.tools.code import run_python


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def test_run_python_inline_code_returns_result_dict(tmp_path):
    sess = _session(tmp_path)
    out = run_python(sess, code="from harness_sandbox import emit\nemit(21 * 2)\n")
    assert out["result"] == 42
    assert out["exit_code"] == 0
    assert out["error"] is None
    assert out["new_handles"] == []


def test_run_python_script_file_with_args(tmp_path):
    sess = _session(tmp_path)
    from harness.tools.files import write_file
    write_file(sess, "s.py", "import sys\nfrom harness_sandbox import emit\nemit(sys.argv[1:])\n")
    out = run_python(sess, path="s.py", args=["EU", "2025"])
    assert out["result"] == ["EU", "2025"]


def test_run_python_reports_new_handles(tmp_path):
    sess = _session(tmp_path)
    out = run_python(sess, code="from harness_sandbox import save\nsave('h1', {'x': 1})\n")
    assert out["new_handles"] == ["h1"]
    assert sess.store.get("h1") == {"x": 1}


def test_run_python_error_is_reported_not_raised(tmp_path):
    sess = _session(tmp_path)
    out = run_python(sess, code="raise ValueError('boom')\n")
    assert out["exit_code"] != 0
    assert "ValueError: boom" in out["error"]


def test_run_python_requires_code_or_path(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(ValueError, match="code.*or.*path"):
        run_python(sess)
