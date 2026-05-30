import json
import os
import subprocess
import sys
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parent.parent / "harness" / "runtime"


def _run_child(tmp_path, body: str, registry: dict | None = None, args: list[str] | None = None):
    """Run a small script in a child process with the helper env wired up."""
    root = tmp_path
    (root / "handles").mkdir(exist_ok=True)
    new_handles = root / "_new_handles.jsonl"
    emit = root / "_emit.json"
    registry_path = root / "_registry.json"
    registry_path.write_text(json.dumps(registry or {}))

    script = root / "script.py"
    script.write_text(body)

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HARNESS_ROOT": str(root),
        "HARNESS_NEW_HANDLES": str(new_handles),
        "HARNESS_EMIT": str(emit),
        "HARNESS_REGISTRY": str(registry_path),
        "PYTHONPATH": str(RUNTIME_DIR),
    }
    proc = subprocess.run(
        [sys.executable, str(script), *(args or [])],
        cwd=root, env=env, capture_output=True, text=True, timeout=30,
    )
    return proc, new_handles, emit


def test_helper_emit_writes_payload(tmp_path):
    proc, _, emit = _run_child(
        tmp_path,
        "from harness_sandbox import emit\nemit({'total': 42})\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(emit.read_text()) == {"total": 42}


def test_helper_save_records_new_handle(tmp_path):
    proc, new_handles, _ = _run_child(
        tmp_path,
        "from harness_sandbox import save\nsave('h5', 'derived text')\n",
    )
    assert proc.returncode == 0, proc.stderr
    line = json.loads(new_handles.read_text().strip())
    assert line["id"] == "h5"
    assert line["kind"] == "text"
    assert (tmp_path / line["path"]).read_text() == "derived text"


def test_helper_load_reads_existing_text_handle(tmp_path):
    (tmp_path / "handles").mkdir(exist_ok=True)
    (tmp_path / "handles" / "h1.txt").write_text("input data")
    registry = {"h1": {"kind": "text", "path": "handles/h1.txt"}}
    proc, _, emit = _run_child(
        tmp_path,
        "from harness_sandbox import load, emit\nemit(load('h1'))\n",
        registry=registry,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(emit.read_text()) == "input data"


def test_helper_passes_argv(tmp_path):
    proc, _, emit = _run_child(
        tmp_path,
        "import sys\nfrom harness_sandbox import emit\nemit(sys.argv[1:])\n",
        args=["EU", "2025"],
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(emit.read_text()) == ["EU", "2025"]


import pytest

from harness.config import SandboxConfig
from harness.handles import HandleStore
from harness.sandbox import ExecResult, LocalSubprocessSandbox
from harness.paths import PathEscapesRootError


def _sandbox(tmp_path):
    store = HandleStore(tmp_path)
    return LocalSubprocessSandbox(root=tmp_path, store=store, config=SandboxConfig()), store


def test_run_script_captures_emit_result(tmp_path):
    sb, _ = _sandbox(tmp_path)
    (tmp_path / "s.py").write_text(
        "from harness_sandbox import emit\nemit({'ok': True})\n"
    )
    res = sb.run_script("s.py")
    assert isinstance(res, ExecResult)
    assert res.exit_code == 0
    assert res.result == {"ok": True}
    assert res.error is None


def test_run_script_captures_stdout(tmp_path):
    sb, _ = _sandbox(tmp_path)
    (tmp_path / "s.py").write_text("print('hello from child')\n")
    res = sb.run_script("s.py")
    assert "hello from child" in res.stdout


def test_run_script_reports_new_handles_and_registers_them(tmp_path):
    sb, store = _sandbox(tmp_path)
    (tmp_path / "s.py").write_text(
        "from harness_sandbox import save\nsave('h1', {'derived': 1})\n"
    )
    res = sb.run_script("s.py")
    assert res.new_handles == ["h1"]
    assert store.get("h1") == {"derived": 1}  # parent ingested it


def test_run_script_can_load_existing_handle(tmp_path):
    sb, store = _sandbox(tmp_path)
    store.put({"input": 99}, source="seed", id="h1")
    (tmp_path / "s.py").write_text(
        "from harness_sandbox import load, emit\nemit(load('h1'))\n"
    )
    res = sb.run_script("s.py")
    assert res.result == {"input": 99}


def test_run_script_passes_args(tmp_path):
    sb, _ = _sandbox(tmp_path)
    (tmp_path / "s.py").write_text(
        "import sys\nfrom harness_sandbox import emit\nemit(sys.argv[1:])\n"
    )
    res = sb.run_script("s.py", args=["EU", "2025"])
    assert res.result == ["EU", "2025"]


def test_run_script_captures_exception_as_error(tmp_path):
    sb, _ = _sandbox(tmp_path)
    (tmp_path / "s.py").write_text("raise ValueError('boom')\n")
    res = sb.run_script("s.py")
    assert res.exit_code != 0
    assert "ValueError: boom" in res.error


def test_run_script_times_out(tmp_path):
    store = HandleStore(tmp_path)
    sb = LocalSubprocessSandbox(root=tmp_path, store=store,
                                config=SandboxConfig(timeout_s=0.5))
    (tmp_path / "s.py").write_text("import time\ntime.sleep(5)\n")
    res = sb.run_script("s.py")
    assert res.killed_by == "timeout"
    assert res.exit_code != 0


def test_run_script_rejects_path_outside_root(tmp_path):
    sb, _ = _sandbox(tmp_path)
    with pytest.raises(PathEscapesRootError):
        sb.run_script("../evil.py")


def test_run_code_convenience_writes_and_runs_inline(tmp_path):
    sb, _ = _sandbox(tmp_path)
    res = sb.run_code("from harness_sandbox import emit\nemit(7)\n")
    assert res.result == 7


# --- robustness: a misbehaving child must yield an ExecResult, never raise ---

def test_malformed_emit_payload_is_graceful(tmp_path):
    sb, _ = _sandbox(tmp_path)
    # Child exits 0 but writes garbage directly to the emit file.
    (tmp_path / "s.py").write_text(
        "import os\nopen(os.environ['HARNESS_EMIT'], 'w').write('{not json')\n"
    )
    res = sb.run_script("s.py")  # must not raise
    assert res.exit_code == 0
    assert res.result is None
    assert "malformed emit" in res.error


def test_corrupt_handle_line_is_skipped_not_fatal(tmp_path):
    sb, store = _sandbox(tmp_path)
    # One good handle, then a corrupt jsonl line appended directly.
    (tmp_path / "s.py").write_text(
        "import os\n"
        "from harness_sandbox import save\n"
        "save('h1', {'ok': 1})\n"
        "open(os.environ['HARNESS_NEW_HANDLES'], 'a').write('{bad json\\n')\n"
    )
    res = sb.run_script("s.py")  # must not raise
    assert res.new_handles == ["h1"]      # good one reported
    assert store.get("h1") == {"ok": 1}   # and registered
    assert res.exit_code == 0


def test_dataframe_roundtrip_through_sandbox(tmp_path):
    import pandas as pd

    sb, store = _sandbox(tmp_path)
    store.put(pd.DataFrame({"revenue": [120, 0, 210, 0, 95]}), source="seed", id="h1")
    (tmp_path / "s.py").write_text(
        "from harness_sandbox import load, save, emit\n"
        "df = load('h1')\n"
        "clean = df[df.revenue > 0]\n"
        "save('h2', clean)\n"
        "emit({'total': int(clean.revenue.sum()), 'dropped': int((df.revenue <= 0).sum())})\n"
    )
    res = sb.run_script("s.py")
    assert res.result == {"total": 425, "dropped": 2}
    assert res.new_handles == ["h2"]
    assert list(store.get("h2").revenue) == [120, 210, 95]


def test_emits_nothing_yields_none_result_and_no_error(tmp_path):
    sb, _ = _sandbox(tmp_path)
    (tmp_path / "s.py").write_text("x = 1 + 1\n")
    res = sb.run_script("s.py")
    assert res.exit_code == 0
    assert res.result is None
    assert res.error is None


def test_stderr_on_success_is_captured_without_setting_error(tmp_path):
    sb, _ = _sandbox(tmp_path)
    (tmp_path / "s.py").write_text("import sys\nsys.stderr.write('just a warning')\n")
    res = sb.run_script("s.py")
    assert res.exit_code == 0
    assert "just a warning" in res.stderr
    assert res.error is None


def test_inline_scripts_do_not_collide_across_instances(tmp_path):
    store = HandleStore(tmp_path)
    sb_a = LocalSubprocessSandbox(root=tmp_path, store=store, config=SandboxConfig())
    sb_b = LocalSubprocessSandbox(root=tmp_path, store=store, config=SandboxConfig())
    res_a = sb_a.run_code("from harness_sandbox import emit\nemit('A')\n")
    res_b = sb_b.run_code("from harness_sandbox import emit\nemit('B')\n")
    assert (res_a.result, res_b.result) == ("A", "B")


def test_dataframe_preview_matches_between_parent_and_child(tmp_path):
    # The child's save() duplicates HandleStore's dataframe preview logic; they must
    # produce an identical preview for the same frame (guards against contract drift).
    import pandas as pd

    df = pd.DataFrame({"a": list(range(10))})  # >5 rows -> truncation suffix expected

    parent_store = HandleStore(tmp_path / "p")
    parent_handle = parent_store.put(df, source="s")

    child_store = HandleStore(tmp_path / "c")
    sb = LocalSubprocessSandbox(root=tmp_path / "c", store=child_store, config=SandboxConfig())
    child_store.put(df, source="seed", id="hin")
    (tmp_path / "c" / "s.py").write_text(
        "from harness_sandbox import load, save\nsave('hout', load('hin'))\n"
    )
    sb.run_script("s.py")
    child_handle = child_store.manifest_handles()["hout"]

    assert child_handle.preview == parent_handle.preview
    assert "5 of 10 rows" in child_handle.preview  # suffix present on both sides
