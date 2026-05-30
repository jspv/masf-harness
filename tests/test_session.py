from pathlib import Path

from harness.config import HarnessConfig
from harness.session import Session


def test_session_creates_root_under_default_location(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sess = Session.create(HarnessConfig())
    assert sess.root.exists()
    assert sess.root.is_dir()
    assert ".harness/sessions" in str(sess.root)
    sess.cleanup()


def test_session_uses_explicit_root_dir(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "myroot")
    sess = Session.create(cfg)
    assert sess.root == (tmp_path / "myroot").resolve()
    assert sess.root.exists()


def test_session_wires_store_and_sandbox_to_same_root(tmp_path):
    sess = Session.create(HarnessConfig(root_dir=tmp_path / "r"))
    assert sess.store.root == sess.root
    assert sess.sandbox.root == sess.root


def test_session_end_to_end_handle_then_analyze(tmp_path):
    sess = Session.create(HarnessConfig(root_dir=tmp_path / "r"))
    h = sess.store.put({"values": [120, 95, 0, 210]}, source="tool:read")
    code = (
        "from harness_sandbox import load, emit\n"
        f"d = load('{h.id}')\n"
        "vals = [v for v in d['values'] if v > 0]\n"
        "emit({'total': sum(vals), 'dropped': len(d['values']) - len(vals)})\n"
    )
    res = sess.sandbox.run_code(code)
    assert res.result == {"total": 425, "dropped": 1}


def test_cleanup_removes_root_when_owned(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    sess = Session.create(cfg)
    assert sess.root.exists()
    sess.cleanup()
    assert not sess.root.exists()
