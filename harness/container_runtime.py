"""Container runtime helpers: detect podman/docker, build the sandbox image, provision a
package layer. Driven via the runtime CLI (subprocess) -- no Python container SDK dependency.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .config import SandboxConfig

_RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
_CONTAINERFILE = _RUNTIME_DIR / "Containerfile"


def detect_runtime(override: str | None, which: Callable[[str], str | None] = shutil.which) -> str:
    """Return the container runtime binary name. Prefers ``override``, then podman, then docker."""
    if override:
        if which(override):
            return override
        raise RuntimeError(f"container runtime {override!r} not found on PATH")
    for candidate in ("podman", "docker"):
        if which(candidate):
            return candidate
    raise RuntimeError(
        "no container runtime found: install podman or docker, or set "
        "HarnessConfig.sandbox.backend='local'"
    )


def _py_tag() -> str:
    return f"py{sys.version_info.major}{sys.version_info.minor}"


def image_tag(preinstalled: tuple[str, ...]) -> str:
    """Stable image tag keyed by the Python version + the (order-independent) preinstalled set."""
    digest = hashlib.sha256((_py_tag() + "|" + ",".join(sorted(preinstalled))).encode()).hexdigest()
    return f"harness-sandbox:{digest[:12]}"


def image_exists(runtime: str, tag: str, run: Callable = subprocess.run) -> bool:
    # `image inspect` works on both podman and docker (unlike podman-only `image exists`).
    return run([runtime, "image", "inspect", tag], capture_output=True).returncode == 0


def ensure_image(runtime: str, tag: str, config: SandboxConfig,
                 run: Callable = subprocess.run) -> None:
    """Build the sandbox image if it isn't present. Raises with the build stderr on failure."""
    if image_exists(runtime, tag, run):
        return
    build = [runtime, "build", "-t", tag,
             "--build-arg", f"PREINSTALLED={' '.join(config.preinstalled)}",
             "-f", str(_CONTAINERFILE), str(_RUNTIME_DIR)]
    proc = run(build, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"failed to build sandbox image {tag}:\n{proc.stderr}")


def layer_dir(config: SandboxConfig, base: Path | None = None) -> Path:
    """Host cache dir for the provisioned package layer, keyed by packages + Python version."""
    base = base or (Path.home() / ".harness" / "pkgcache")
    digest = hashlib.sha256(
        (_py_tag() + "|" + ",".join(sorted(config.pip_packages))).encode()
    ).hexdigest()
    return base / digest[:12]


def ensure_layer(runtime: str, config: SandboxConfig, base: Path | None = None,
                 run: Callable = subprocess.run) -> Path:
    """Provision ``config.pip_packages`` into a mounted layer (network ON, provisioning only).

    Cached by package set; provisions once. Returns the host layer dir.
    """
    target = layer_dir(config, base)
    if target.exists() and any(target.iterdir()):
        return target
    target.mkdir(parents=True, exist_ok=True)
    tag = image_tag(config.preinstalled)
    ensure_image(runtime, tag, config, run)
    cmd = [runtime, "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
           "-v", f"{target}:/layer:rw", tag,
           "pip", "install", "--no-cache-dir", "--target", "/layer", *config.pip_packages]
    proc = run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"failed to provision pip_packages into {target}:\n{proc.stderr}")
    return target
