import asyncio

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


def test_session_handles_and_artifacts(tmp_path):
    from harness import HarnessConfig, Session

    sess = Session.create(HarnessConfig(root_dir=tmp_path / "r"))
    (sess.root / "report.txt").write_text("hi")
    (sess.root / ".scripts").mkdir(exist_ok=True)
    (sess.root / ".scripts" / "x.py").write_text("# scratch")
    sess.store.put({"a": 1}, source="t")

    assert "report.txt" in sess.artifacts
    assert not any(a.startswith(".scripts") for a in sess.artifacts)
    assert not any(a.startswith("handles") for a in sess.artifacts)
    assert sess.handles  # the put() handle shows up


def test_session_async_context_manager_cleanup(tmp_path):
    from harness import HarnessConfig, Session

    async def run():
        cfg = HarnessConfig(root_dir=tmp_path / "r", cleanup=True)
        async with Session.create(cfg) as sess:
            root = sess.root
            assert root.exists()
        return root

    root = asyncio.run(run())
    assert not root.exists()  # cleanup=True removed it on exit


def test_session_async_context_manager_no_cleanup_by_default(tmp_path):
    from harness import HarnessConfig, Session

    async def run():
        cfg = HarnessConfig(root_dir=tmp_path / "r")  # cleanup defaults to False
        async with Session.create(cfg) as sess:
            root = sess.root
            assert root.exists()
        return root

    root = asyncio.run(run())
    assert root.exists()  # root survives because cleanup=False
