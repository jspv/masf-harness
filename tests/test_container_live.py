import shutil
import uuid
from pathlib import Path

import pytest

from harness.config import SandboxConfig
from harness.handles import HandleStore
from harness.sandbox import LocalSubprocessSandbox
from harness.sandbox_container import ContainerSandbox

_RUNTIME = "podman" if shutil.which("podman") else ("docker" if shutil.which("docker") else None)
pytestmark = pytest.mark.skipif(_RUNTIME is None, reason="no podman/docker runtime available")


@pytest.fixture
def croot():
    # The container runtime (on macOS) shares $HOME but not the system temp, so the bind-mounted
    # session root must live under $HOME. Fresh dir per test; cleaned up after.
    d = Path.home() / ".harness" / "_ctest" / uuid.uuid4().hex
    d.mkdir(parents=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _container(root, **cfg):
    store = HandleStore(root)
    return ContainerSandbox(root=root, store=store,
                            config=SandboxConfig(backend="container", **cfg)), store


def test_runs_code_and_captures_result(croot):
    sb, _ = _container(croot)
    res = sb.run_code("from harness_sandbox import emit\nemit(6 * 7)\n")
    assert res.error is None, res.error
    assert res.result == 42


def test_network_is_blocked_by_default(croot):
    sb, _ = _container(croot)
    res = sb.run_code(
        "import socket\n"
        "socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "from harness_sandbox import emit\nemit('reached')\n"
    )
    assert res.result != "reached"          # the connection must fail
    assert res.exit_code != 0


def test_network_can_be_enabled(croot):
    sb, _ = _container(croot, network=True)
    res = sb.run_code("from harness_sandbox import emit\nemit('ok')\n")
    assert res.result == "ok"


def test_saved_handle_is_ingested(croot):
    sb, _ = _container(croot)
    res = sb.run_code("from harness_sandbox import save\nsave('h1', {'x': 1})\n")
    assert "h1" in res.new_handles


def test_pip_packages_are_importable(croot):
    sb, _ = _container(croot, pip_packages=("six",))   # tiny, pure-python
    res = sb.run_code("import six\nfrom harness_sandbox import emit\nemit(six.__name__)\n")
    assert res.error is None, res.error
    assert res.result == "six"


def test_local_and_container_parity(croot):
    code = "x = sum(range(10))\nfrom harness_sandbox import emit\nemit(x)\n"
    (croot / "c").mkdir()
    (croot / "l").mkdir()
    cont = ContainerSandbox(root=croot / "c", store=HandleStore(croot / "c"),
                            config=SandboxConfig(backend="container"))
    loc = LocalSubprocessSandbox(root=croot / "l", store=HandleStore(croot / "l"),
                                 config=SandboxConfig())
    assert cont.run_code(code).result == loc.run_code(code).result == 45
