import pytest

from harness.config import SandboxConfig
from harness.container_runtime import (
    detect_runtime,
    ensure_image,
    image_tag,
    layer_dir,
)


def test_detect_prefers_podman_then_docker():
    assert detect_runtime(None, which=lambda c: c if c == "podman" else None) == "podman"
    assert detect_runtime(None, which=lambda c: c if c == "docker" else None) == "docker"


def test_detect_honors_override():
    assert detect_runtime("docker", which=lambda c: "/usr/bin/docker") == "docker"


def test_detect_raises_when_none_found():
    with pytest.raises(RuntimeError, match="no container runtime"):
        detect_runtime(None, which=lambda c: None)


def test_detect_raises_when_override_missing():
    with pytest.raises(RuntimeError, match="not found"):
        detect_runtime("nope", which=lambda c: None)


def test_image_tag_is_stable_and_depends_on_preinstalled():
    a = image_tag(("pandas", "numpy"))
    assert a == image_tag(("numpy", "pandas"))        # order-independent
    assert a.startswith("harness-sandbox:")
    assert a != image_tag(("pandas",))                # different set -> different tag


def test_layer_dir_keys_on_packages(tmp_path):
    cfg1 = SandboxConfig(pip_packages=("rich",))
    cfg2 = SandboxConfig(pip_packages=("rich", "tabulate"))
    d1 = layer_dir(cfg1, base=tmp_path)
    assert d1 == layer_dir(cfg1, base=tmp_path)        # stable
    assert d1 != layer_dir(cfg2, base=tmp_path)        # different set -> different dir
    assert d1.parent == tmp_path


def test_ensure_image_builds_when_absent():
    calls = []

    class _Proc:
        def __init__(self, code, out="", err=""):
            self.returncode, self.stdout, self.stderr = code, out, err

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return _Proc(1)                            # not present -> triggers build
        return _Proc(0)

    ensure_image("podman", "harness-sandbox:abc", SandboxConfig(), run=fake_run)
    assert any(a[1] == "build" and "-t" in a and "harness-sandbox:abc" in a for a in calls)


def test_ensure_image_skips_build_when_present():
    class _Proc:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(argv, **kw):
        assert argv[1] != "build", "should not build when image exists"
        return _Proc()

    ensure_image("podman", "harness-sandbox:abc", SandboxConfig(), run=fake_run)
