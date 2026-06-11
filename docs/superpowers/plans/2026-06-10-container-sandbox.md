# Container Sandbox Tier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run `run_python` code in a hardened OCI container (Podman/Docker) behind the existing `SandboxExecutor` interface, selectable by config — network off by default, packages via a baked image + a provisioned layer.

**Architecture:** Extract the host-side control-file orchestration out of `LocalSubprocessSandbox` into a shared base parameterized by a `_launch()` seam; `LocalSubprocessSandbox` keeps subprocess+rlimits, a new `ContainerSandbox` builds a hardened `<runtime> run …` argv that bind-mounts the session root → `/workspace` and the harness `runtime/` → `/runtime` (read-only). `Session.create` picks the backend; `local` stays default. (Spec: `docs/superpowers/specs/2026-06-10-container-sandbox-design.md`.)

**Tech Stack:** Python 3.12, Podman/Docker (driven via CLI through `subprocess` — no new Python dep), pytest. Pure logic (argv builder, runtime detection, image/layer hashing) unit-tested offline; real-container behavior gated behind runtime availability.

---

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `harness/config.py` | Modify | `SandboxConfig`: add `backend`, `container_runtime`, `network`, `pip_packages`, `max_cpus`; remove `confine_os` |
| `harness/sandbox.py` | Modify | Extract `_OrchestratedSandbox` base (control-file orchestration + `_launch` seam); keep `LocalSubprocessSandbox` over it |
| `harness/container_runtime.py` | **Create** | `detect_runtime`, `image_tag`, `image_exists`/`ensure_image`, `layer_dir`/`ensure_layer` |
| `harness/runtime/Containerfile` | **Create** | `python:3.12-slim` + `pip install` the `preinstalled` set |
| `harness/sandbox_container.py` | **Create** | `ContainerSandbox(_OrchestratedSandbox)` + `_build_run_argv` |
| `harness/session.py` | Modify | Select backend from `config.sandbox.backend`; type the field `SandboxExecutor` |
| `harness/__init__.py`, `pyproject.toml` | Modify | Export `ContainerSandbox`; add `harness-build-sandbox` console script |
| `tests/test_config.py`, `tests/test_container_runtime.py`, `tests/test_sandbox_container.py`, `tests/test_session.py` | Create/Modify | Offline unit tests |
| `tests/test_container_live.py` | **Create** | Gated real-container integration tests |
| `README.md` | Modify | Document the container tier |

Key existing types (do not change): `ExecResult(stdout, stderr, result, error, exit_code, new_handles, killed_by)` and the `SandboxExecutor` Protocol (`run_script`/`run_code → ExecResult`) in `harness/sandbox.py`.

---

## Task 1: Config — backend + container fields

**Files:** Modify `harness/config.py`; Test `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_sandbox_backend_defaults_and_container_fields():
    cfg = HarnessConfig()
    assert cfg.sandbox.backend == "local"            # default backend
    assert cfg.sandbox.network is False              # network off by default
    assert cfg.sandbox.pip_packages == ()
    assert cfg.sandbox.container_runtime is None      # auto-detect
    assert cfg.sandbox.max_cpus == 2.0
    assert not hasattr(cfg.sandbox, "confine_os")     # removed
```

- [ ] **Step 2: Run it, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config.py -k backend -q`
Expected: FAIL — `backend` attribute missing.

- [ ] **Step 3: Edit `harness/config.py`**

Add `Literal` to the typing import at the top of the file:

```python
from typing import Literal
```

Replace the `SandboxConfig` dataclass body with:

```python
@dataclass
class SandboxConfig:
    timeout_s: float = 30.0
    max_memory_mb: int = 1024
    max_file_size_mb: int = 512        # enforced by the local tier only (no container equivalent)
    backend: Literal["local", "container"] = "local"
    container_runtime: str | None = None   # None -> auto-detect podman, then docker
    network: bool = False                  # sandbox network off by default; opt-in to enable
    pip_packages: tuple[str, ...] = ()     # provisioned into a mounted layer (network only there)
    max_cpus: float = 2.0
    preinstalled: tuple[str, ...] = ("pandas", "pyarrow", "numpy", "httpx")
```

- [ ] **Step 4: Confirm `confine_os` is referenced nowhere else, then run tests**

Run: `grep -rn "confine_os" harness/ tests/` → expect no matches.
Run: `.venv/bin/python -m pytest tests/test_config.py -q && .venv/bin/ruff check harness/config.py tests/test_config.py`
Expected: pass; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add harness/config.py tests/test_config.py
git commit -m "feat(sandbox): add container backend config fields; drop aspirational confine_os"
```

---

## Task 2: Refactor — shared orchestration base (behavior-preserving)

**Files:** Modify `harness/sandbox.py`; Test `tests/test_sandbox.py` (existing tests must stay green)

- [ ] **Step 1: Confirm the existing sandbox suite is green (baseline)**

Run: `.venv/bin/python -m pytest tests/test_sandbox.py -q`
Expected: pass. (This task must keep it green — it is a refactor.)

- [ ] **Step 2: Rewrite `harness/sandbox.py`**

Replace the module from the `ExecResult` dataclass downward with the version below. It extracts the control-file orchestration into `_OrchestratedSandbox` with a `_launch(ctx) -> _LaunchResult` seam; `LocalSubprocessSandbox` subclasses it and implements `_launch` with the exact same subprocess+rlimit behavior as before. Keep the module docstring and imports at the top; add `from dataclasses import dataclass, field` is already present.

```python
@dataclass
class ExecResult:
    stdout: str
    stderr: str
    result: Any | None
    error: str | None
    exit_code: int
    new_handles: list[str] = field(default_factory=list)
    killed_by: str | None = None


class SandboxExecutor(Protocol):
    """Swappable execution backend. Local now; container/remote later."""

    def run_script(self, path: str, args: list[str] | None = None) -> ExecResult: ...
    def run_code(self, code: str, args: list[str] | None = None) -> ExecResult: ...


@dataclass
class _LaunchResult:
    """What a backend's _launch returns: raw process outcome, pre-parse."""
    stdout: str
    stderr: str
    exit_code: int
    killed_by: str | None = None


@dataclass
class _RunContext:
    """Everything a backend needs to launch one run. Control-file paths are HOST paths
    (the parent reads/writes them); a backend translates them to its own namespace."""
    script_rel: str          # user script path relative to root
    argv: list[str]          # user args (already str-coerced)
    root: Path
    registry_file: Path      # host path
    new_handles_file: Path   # host path
    emit_file: Path          # host path
    config: SandboxConfig


class _OrchestratedSandbox:
    """Shared control-file orchestration. Subclasses implement ``_launch``.

    Sequential and NOT re-entrant per root: each run uses per-run-unique control files
    (keyed by pid + run counter), so sequential runs never clobber each other.
    """

    def __init__(self, root: Path | str, store: HandleStore,
                 config: SandboxConfig | None = None) -> None:
        self.root = Path(root).resolve()
        self.store = store
        self.config = config or SandboxConfig()
        self._run_counter = 0

    def run_code(self, code: str, args: list[str] | None = None) -> ExecResult:
        scripts = self.root / _SCRIPTS_DIR
        scripts.mkdir(exist_ok=True)
        fd, abspath = tempfile.mkstemp(prefix="inline_", suffix=".py", dir=scripts)
        os.close(fd)
        Path(abspath).write_text(code, encoding="utf-8")
        rel = str(Path(abspath).relative_to(self.root))
        return self.run_script(rel, args)

    def run_script(self, path: str, args: list[str] | None = None) -> ExecResult:
        script = safe_path(self.root, path)  # raises PathEscapesRootError if outside
        argv = [str(a) for a in (args or [])]

        self._run_counter += 1
        token = f"{os.getpid()}_{self._run_counter}"
        new_handles_file = self.root / f"_new_handles_{token}.jsonl"
        emit_file = self.root / f"_emit_{token}.json"
        registry_file = self.root / f"_registry_{token}.json"

        try:
            new_handles_file.write_text("", encoding="utf-8")
            registry = {hid: {"kind": h.kind, "path": h.path}
                        for hid, h in self.store.manifest_handles().items()}
            registry_file.write_text(json.dumps(registry), encoding="utf-8")

            ctx = _RunContext(
                script_rel=str(script.relative_to(self.root)), argv=argv, root=self.root,
                registry_file=registry_file, new_handles_file=new_handles_file,
                emit_file=emit_file, config=self.config,
            )
            launched = self._launch(ctx)

            result = None
            emit_error = None
            if launched.exit_code == 0 and emit_file.exists():
                try:
                    result = json.loads(emit_file.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    emit_error = f"harness: malformed emit payload: {e}"

            if (result is None and emit_error is None and launched.exit_code == 0
                    and launched.stdout.strip()):
                result = launched.stdout.strip()

            new_handles = self._ingest_new_handles(new_handles_file)
            base_error = (launched.stderr.strip() or None) if launched.exit_code != 0 else None
            error = "\n".join(p for p in (base_error, emit_error) if p) or None

            return ExecResult(stdout=launched.stdout, stderr=launched.stderr, result=result,
                              error=error, exit_code=launched.exit_code,
                              new_handles=new_handles, killed_by=launched.killed_by)
        finally:
            for f in (new_handles_file, emit_file, registry_file):
                f.unlink(missing_ok=True)

    def _ingest_new_handles(self, new_handles_file: Path) -> list[str]:
        ids: list[str] = []
        if not new_handles_file.exists():
            return ids
        for line in new_handles_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                self.store.register(rec)
                ids.append(rec["id"])
            except (json.JSONDecodeError, ValueError, KeyError):
                continue
        return ids

    def _launch(self, ctx: _RunContext) -> _LaunchResult:
        raise NotImplementedError


class LocalSubprocessSandbox(_OrchestratedSandbox):
    """Runs the script in a scrubbed-env child process with rlimits + a wall-clock timeout."""

    def _launch(self, ctx: _RunContext) -> _LaunchResult:
        script_abs = ctx.root / ctx.script_rel
        env = {
            "PATH": _minimal_path(),
            "HOME": str(ctx.root),
            "TMPDIR": str(ctx.root),
            "HARNESS_ROOT": str(ctx.root),
            "HARNESS_REGISTRY": str(ctx.registry_file),
            "HARNESS_NEW_HANDLES": str(ctx.new_handles_file),
            "HARNESS_EMIT": str(ctx.emit_file),
            "PYTHONPATH": str(_RUNTIME_DIR),
        }
        try:
            proc = subprocess.run(
                [sys.executable, str(_RUNNER), str(script_abs), *ctx.argv],
                cwd=ctx.root, env=env, capture_output=True, text=True,
                timeout=ctx.config.timeout_s, preexec_fn=self._limits(),
            )
            return _LaunchResult(proc.stdout, proc.stderr, proc.returncode)
        except subprocess.TimeoutExpired as e:
            return _LaunchResult(_as_text(e.stdout),
                                 _as_text(e.stderr) + "\nharness: killed (timeout)",
                                 -1, killed_by="timeout")

    def _limits(self):
        cfg = self.config

        def set_limits() -> None:
            mem = cfg.max_memory_mb * 1024 * 1024
            fsize = cfg.max_file_size_mb * 1024 * 1024
            for res_id, limit in (
                (resource.RLIMIT_AS, mem),
                (resource.RLIMIT_FSIZE, fsize),
                (resource.RLIMIT_CORE, 0),
            ):
                try:
                    resource.setrlimit(res_id, (limit, limit))
                except (ValueError, OSError):
                    pass

        return set_limits


def _minimal_path() -> str:
    return "/usr/bin:/bin:/usr/local/bin"


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
```

- [ ] **Step 3: Run the existing sandbox suite — it must stay green**

Run: `.venv/bin/python -m pytest tests/test_sandbox.py -q`
Expected: all pass (the refactor preserves behavior).

- [ ] **Step 4: Full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/sandbox.py`
Expected: pass; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add harness/sandbox.py
git commit -m "refactor(sandbox): extract shared orchestration base with a _launch seam"
```

---

## Task 3: `container_runtime.py` — detection, image, package layer

**Files:** Create `harness/container_runtime.py`, `harness/runtime/Containerfile`; Test `tests/test_container_runtime.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_container_runtime.py`:

```python
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
```

- [ ] **Step 2: Run, verify fail**

Run: `.venv/bin/python -m pytest tests/test_container_runtime.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.container_runtime'`.

- [ ] **Step 3: Create `harness/runtime/Containerfile`**

```dockerfile
# Base sandbox image: Python + the preinstalled libraries.
# The harness runtime/ helpers are NOT baked in -- they are mounted read-only at /runtime,
# so the image never drifts from the installed harness version.
FROM python:3.12-slim
ARG PREINSTALLED="pandas pyarrow numpy httpx"
RUN pip install --no-cache-dir ${PREINSTALLED}
```

- [ ] **Step 4: Create `harness/container_runtime.py`**

```python
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
```

- [ ] **Step 5: Run tests + lint**

Run: `.venv/bin/python -m pytest tests/test_container_runtime.py -q`
Expected: 7 passed.
Run: `.venv/bin/ruff check harness/container_runtime.py tests/test_container_runtime.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add harness/container_runtime.py harness/runtime/Containerfile tests/test_container_runtime.py
git commit -m "feat(sandbox): container runtime detection, image build, package layer provisioning"
```

---

## Task 4: `ContainerSandbox` — the hardened run

**Files:** Create `harness/sandbox_container.py`; Test `tests/test_sandbox_container.py`

- [ ] **Step 1: Write the failing tests** (offline — they test the argv builder, not a real container)

Create `tests/test_sandbox_container.py`:

```python
from pathlib import Path

from harness.config import SandboxConfig
from harness.sandbox import _RunContext
from harness.sandbox_container import ContainerSandbox


def _ctx(tmp_path, config):
    return _RunContext(
        script_rel=".scripts/inline_x.py", argv=["EU", "2025"], root=tmp_path,
        registry_file=tmp_path / "_registry_t.json",
        new_handles_file=tmp_path / "_new_handles_t.jsonl",
        emit_file=tmp_path / "_emit_t.json", config=config,
    )


def _sandbox(tmp_path, config):
    # inject a fake runtime so construction needs no podman/docker installed
    return ContainerSandbox(root=tmp_path, store=None, config=config, runtime="podman")


def test_run_argv_blocks_network_by_default(tmp_path):
    cfg = SandboxConfig(backend="container")
    sb = _sandbox(tmp_path, cfg)
    argv = sb._build_run_argv(_ctx(tmp_path, cfg), "harness-sandbox:abc", layer=None)
    assert argv[:2] == ["podman", "run"]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv and "--cap-drop" in argv
    assert "no-new-privileges" in " ".join(argv)


def test_run_argv_enables_network_when_configured(tmp_path):
    cfg = SandboxConfig(backend="container", network=True)
    sb = _sandbox(tmp_path, cfg)
    argv = sb._build_run_argv(_ctx(tmp_path, cfg), "harness-sandbox:abc", layer=None)
    assert "none" not in [argv[i + 1] for i, a in enumerate(argv) if a == "--network"] or "--network" not in argv


def test_run_argv_mounts_root_and_runtime_and_translates_paths(tmp_path):
    cfg = SandboxConfig(backend="container")
    sb = _sandbox(tmp_path, cfg)
    argv = sb._build_run_argv(_ctx(tmp_path, cfg), "harness-sandbox:abc", layer=None)
    joined = " ".join(argv)
    assert f"{tmp_path}:/workspace:rw" in joined            # root mount
    assert ":/runtime:ro" in joined                         # runtime mount (read-only)
    assert "HARNESS_ROOT=/workspace" in joined              # path translated into the container
    assert "HARNESS_EMIT=/workspace/_emit_t.json" in joined
    assert "PYTHONPATH=/runtime" in joined
    # the command runs the runner with the relative script path under /workspace
    assert argv[-4:] == ["python", "/runtime/_runner.py", ".scripts/inline_x.py", "EU"] or \
        argv[-5:] == ["python", "/runtime/_runner.py", ".scripts/inline_x.py", "EU", "2025"]


def test_run_argv_mounts_layer_and_extends_pythonpath(tmp_path):
    cfg = SandboxConfig(backend="container", pip_packages=("rich",))
    sb = _sandbox(tmp_path, cfg)
    layer = tmp_path / "layer"
    argv = sb._build_run_argv(_ctx(tmp_path, cfg), "harness-sandbox:abc", layer=layer)
    joined = " ".join(argv)
    assert f"{layer}:/pkgs:ro" in joined
    assert "PYTHONPATH=/runtime:/pkgs" in joined


def test_run_argv_applies_resource_limits(tmp_path):
    cfg = SandboxConfig(backend="container", max_memory_mb=512, max_cpus=1.5)
    sb = _sandbox(tmp_path, cfg)
    argv = sb._build_run_argv(_ctx(tmp_path, cfg), "harness-sandbox:abc", layer=None)
    assert "--memory" in argv and argv[argv.index("--memory") + 1] == "512m"
    assert "--cpus" in argv and argv[argv.index("--cpus") + 1] == "1.5"
    assert "--pids-limit" in argv
```

- [ ] **Step 2: Run, verify fail**

Run: `.venv/bin/python -m pytest tests/test_sandbox_container.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Create `harness/sandbox_container.py`**

```python
"""ContainerSandbox: run agent code in a hardened OCI container (podman/docker).

Reuses the shared orchestration in ``_OrchestratedSandbox`` (control files, ExecResult parsing)
and only differs in ``_launch``: it bind-mounts the session root at /workspace and the harness
runtime/ at /runtime (read-only), translates the control-file paths into the container, and runs
``python /runtime/_runner.py <script-rel> <args>`` with network off (by default), a read-only
root filesystem, dropped capabilities, and memory/cpu/pid limits.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import SandboxConfig
from .sandbox import _LaunchResult, _OrchestratedSandbox, _RunContext, _as_text

_RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
_PIDS_LIMIT = 256


class ContainerSandbox(_OrchestratedSandbox):
    def __init__(self, root: Path | str, store, config: SandboxConfig | None = None,
                 runtime: str | None = None) -> None:
        super().__init__(root, store, config)
        if runtime is not None:
            self._runtime = runtime
        else:
            from .container_runtime import detect_runtime
            self._runtime = detect_runtime(self.config.container_runtime)

    def _build_run_argv(self, ctx: _RunContext, tag: str, layer: Path | None) -> list[str]:
        cfg = self.config
        argv = [
            self._runtime, "run", "--rm",
            "--read-only", "--tmpfs", "/tmp",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", str(_PIDS_LIMIT),
            "--memory", f"{cfg.max_memory_mb}m", "--cpus", str(cfg.max_cpus),
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{ctx.root}:/workspace:rw",
            "-v", f"{_RUNTIME_DIR}:/runtime:ro",
            "-w", "/workspace",
        ]
        if not cfg.network:
            argv += ["--network", "none"]
        pythonpath = "/runtime"
        if layer is not None:
            argv += ["-v", f"{layer}:/pkgs:ro"]
            pythonpath = "/runtime:/pkgs"
        env = {
            "HARNESS_ROOT": "/workspace",
            "HARNESS_REGISTRY": f"/workspace/{ctx.registry_file.name}",
            "HARNESS_NEW_HANDLES": f"/workspace/{ctx.new_handles_file.name}",
            "HARNESS_EMIT": f"/workspace/{ctx.emit_file.name}",
            "PYTHONPATH": pythonpath,
            "HOME": "/workspace",
            "TMPDIR": "/tmp",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        for key, value in env.items():
            argv += ["-e", f"{key}={value}"]
        argv += [tag, "python", "/runtime/_runner.py", ctx.script_rel, *ctx.argv]
        return argv

    def _launch(self, ctx: _RunContext) -> _LaunchResult:
        from .container_runtime import ensure_image, ensure_layer, image_tag

        tag = image_tag(self.config.preinstalled)
        ensure_image(self._runtime, tag, self.config)
        layer = ensure_layer(self._runtime, self.config) if self.config.pip_packages else None
        argv = self._build_run_argv(ctx, tag, layer)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=ctx.config.timeout_s)
            killed_by = "killed" if proc.returncode == 137 else None
            return _LaunchResult(proc.stdout, proc.stderr, proc.returncode, killed_by=killed_by)
        except subprocess.TimeoutExpired as e:
            return _LaunchResult(_as_text(e.stdout),
                                 _as_text(e.stderr) + "\nharness: killed (timeout)",
                                 -1, killed_by="timeout")
```

- [ ] **Step 4: Run tests + lint**

Run: `.venv/bin/python -m pytest tests/test_sandbox_container.py -q`
Expected: pass.
Run: `.venv/bin/ruff check harness/sandbox_container.py tests/test_sandbox_container.py`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add harness/sandbox_container.py tests/test_sandbox_container.py
git commit -m "feat(sandbox): ContainerSandbox with hardened run-argv builder"
```

---

## Task 5: Wire the backend into `Session` + export + console script

**Files:** Modify `harness/session.py`, `harness/__init__.py`, `pyproject.toml`; Test `tests/test_session.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_session.py`:

```python
def test_session_selects_container_backend(tmp_path, monkeypatch):
    from harness.config import HarnessConfig, SandboxConfig
    from harness.sandbox import LocalSubprocessSandbox
    from harness.sandbox_container import ContainerSandbox

    # default -> local
    s_local = Session.create(HarnessConfig(root_dir=tmp_path / "a"))
    assert isinstance(s_local.sandbox, LocalSubprocessSandbox)

    # backend="container" -> ContainerSandbox (inject runtime via env so no podman/docker needed)
    import harness.container_runtime as cr
    monkeypatch.setattr(cr, "detect_runtime", lambda override, **kw: "podman")
    cfg = HarnessConfig(root_dir=tmp_path / "b", sandbox=SandboxConfig(backend="container"))
    s_cont = Session.create(cfg)
    assert isinstance(s_cont.sandbox, ContainerSandbox)
```

- [ ] **Step 2: Run, verify fail**

Run: `.venv/bin/python -m pytest tests/test_session.py -k container_backend -q`
Expected: FAIL — `Session.create` always builds a `LocalSubprocessSandbox`.

- [ ] **Step 3: Edit `harness/session.py`**

Change the `sandbox` field type annotation (around line 21) from:
```python
    sandbox: LocalSubprocessSandbox
```
to:
```python
    sandbox: SandboxExecutor
```

Change the imports near the top (the existing `from .sandbox import LocalSubprocessSandbox`) to:
```python
from .sandbox import LocalSubprocessSandbox, SandboxExecutor
```

In `Session.create`, replace the sandbox construction line:
```python
        sandbox = LocalSubprocessSandbox(root=root, store=store, config=config.sandbox)
```
with:
```python
        sandbox = _build_sandbox(root, store, config.sandbox)
```

Add this module-level helper at the end of `harness/session.py` (after `_resolve_root`):
```python
def _build_sandbox(root, store, sandbox_config):
    """Pick the sandbox backend from config (default: local)."""
    if sandbox_config.backend == "container":
        from .sandbox_container import ContainerSandbox  # local import: optional backend
        return ContainerSandbox(root=root, store=store, config=sandbox_config)
    return LocalSubprocessSandbox(root=root, store=store, config=sandbox_config)
```

- [ ] **Step 4: Export `ContainerSandbox` from the package**

In `harness/__init__.py`, change the sandbox import line to add `ContainerSandbox`:
```python
from .sandbox import ExecResult, LocalSubprocessSandbox, SandboxExecutor
from .sandbox_container import ContainerSandbox
```
and add `"ContainerSandbox",` to `__all__` next to `"LocalSubprocessSandbox"`.

- [ ] **Step 5: Add the `harness-build-sandbox` console script**

In `pyproject.toml`, under `[project.scripts]` (which already has `harness` and `harness-prefetch-docling`), add:
```toml
harness-build-sandbox = "harness.container_runtime:_build_sandbox_main"
```

Add this entry point to `harness/container_runtime.py` (at the end):
```python
def _build_sandbox_main() -> None:
    """Console-script entry (``harness-build-sandbox``): pre-build the sandbox image."""
    from .config import SandboxConfig

    config = SandboxConfig()
    runtime = detect_runtime(config.container_runtime)
    tag = image_tag(config.preinstalled)
    ensure_image(runtime, tag, config)
    print(f"sandbox image ready: {tag} (via {runtime})")
```

- [ ] **Step 6: Run tests + lint + sync (so the console script registers)**

Run: `.venv/bin/python -m pytest tests/test_session.py -q`
Expected: pass.
Run: `uv sync --prerelease=allow 2>&1 | tail -2` (registers `harness-build-sandbox`).
Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check harness/session.py harness/__init__.py harness/container_runtime.py tests/test_session.py`
Expected: all pass; `All checks passed!`. Confirm `.venv/bin/python -c "from harness import ContainerSandbox; print('ok')"`.

- [ ] **Step 7: Commit**

```bash
git add harness/session.py harness/__init__.py harness/container_runtime.py pyproject.toml uv.lock tests/test_session.py
git commit -m "feat(sandbox): select backend in Session.create; export ContainerSandbox; build command"
```

---

## Task 6: Gated real-container integration tests

**Files:** Create `tests/test_container_live.py`

These run only when a container runtime is available (skipped in default CI). They build the real image and exercise the real isolation. Container flags can be finicky across runtimes/hosts — if a test fails on a real runtime, fix the production `_build_run_argv`/`container_runtime` accordingly (the offline unit tests in Tasks 3–4 are the stable contract) and report; do NOT weaken these assertions.

- [ ] **Step 1: Create `tests/test_container_live.py`**

```python
import shutil

import pytest

from harness import HarnessConfig
from harness.config import SandboxConfig
from harness.handles import HandleStore
from harness.sandbox import LocalSubprocessSandbox
from harness.sandbox_container import ContainerSandbox

_RUNTIME = "podman" if shutil.which("podman") else ("docker" if shutil.which("docker") else None)
pytestmark = pytest.mark.skipif(_RUNTIME is None, reason="no podman/docker runtime available")


def _container(tmp_path, **cfg):
    store = HandleStore(tmp_path)
    return ContainerSandbox(root=tmp_path, store=store,
                            config=SandboxConfig(backend="container", **cfg)), store


def test_runs_code_and_captures_result(tmp_path):
    sb, _ = _container(tmp_path)
    res = sb.run_code("from harness_sandbox import emit\nemit(6 * 7)\n")
    assert res.error is None
    assert res.result == 42


def test_network_is_blocked_by_default(tmp_path):
    sb, _ = _container(tmp_path)
    res = sb.run_code(
        "import socket\n"
        "socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "from harness_sandbox import emit\nemit('reached')\n"
    )
    assert res.result != "reached"          # the connection must fail
    assert res.exit_code != 0


def test_network_can_be_enabled(tmp_path):
    sb, _ = _container(tmp_path, network=True)
    res = sb.run_code("from harness_sandbox import emit\nemit('ok')\n")  # trivial; just proves run works with net on
    assert res.result == "ok"


def test_saved_handle_is_ingested(tmp_path):
    sb, _ = _container(tmp_path)
    res = sb.run_code("from harness_sandbox import save\nsave('h1', {'x': 1})\n")
    assert "h1" in res.new_handles


def test_pip_packages_are_importable(tmp_path):
    sb, _ = _container(tmp_path, pip_packages=("six",))   # tiny, pure-python
    res = sb.run_code("import six\nfrom harness_sandbox import emit\nemit(six.__name__)\n")
    assert res.result == "six"


def test_local_and_container_parity(tmp_path):
    code = "x = sum(range(10))\nfrom harness_sandbox import emit\nemit(x)\n"
    cstore = HandleStore(tmp_path / "c")
    (tmp_path / "c").mkdir()
    cont = ContainerSandbox(root=tmp_path / "c", store=cstore,
                            config=SandboxConfig(backend="container"))
    lstore = HandleStore(tmp_path / "l")
    (tmp_path / "l").mkdir()
    loc = LocalSubprocessSandbox(root=tmp_path / "l", store=lstore, config=SandboxConfig())
    assert cont.run_code(code).result == loc.run_code(code).result == 45
```

- [ ] **Step 2: Confirm skipped by default (if no runtime) OR run for real (if available)**

Run: `.venv/bin/python -m pytest tests/test_container_live.py -q`
Expected: if no runtime → all skipped; if podman/docker present → they run. (First run builds the image — may take a minute.)

- [ ] **Step 3: Full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check tests/test_container_live.py`
Expected: pass (live tests skipped or passed); `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git add tests/test_container_live.py
git commit -m "test(sandbox): gated real-container integration tests (run, network-off, handles, layer, parity)"
```

---

## Task 7: Documentation

**Files:** Modify `README.md`

- [ ] **Step 1: Replace the "Confinement & security" Layer-2 bullet + warning**

READ `README.md`. In `## Confinement & security`, the bullet beginning `- **Layer 2 — Executed code (best-effort at the local tier).**` and the following `> ⚠️` blockquote describe the local tier and the future container swap. Replace that blockquote with an accurate description of the now-available container tier (keep the Layer-2 bullet itself). New blockquote text:

```markdown
> The **container tier** provides real isolation: set `HarnessConfig.sandbox.backend = "container"` to run `run_python` in a hardened Podman/Docker container — network off by default, read-only root filesystem, dropped capabilities, non-root, and memory/cpu/pid limits — behind the same `SandboxExecutor` interface (no other harness code changes). See [Sandbox tiers](#sandbox-tiers).
```

- [ ] **Step 2: Add a "Sandbox tiers" subsection at the end of "Confinement & security"**

```markdown
### Sandbox tiers

`run_python` executes model-authored code. Two backends, chosen by `HarnessConfig.sandbox.backend`:

- **`local`** (default) — a scrubbed-env subprocess with `resource` rlimits, a wall-clock timeout, and the path-jail. Fast, no dependencies; best-effort isolation.
- **`container`** — runs the code in a hardened OCI container (Podman preferred, Docker supported; auto-detected). Network **off by default**, read-only root filesystem, `--cap-drop ALL`, non-root, and memory/cpu/pid limits. The session root is bind-mounted to `/workspace`, so handles round-trip exactly as in the local tier.

​```python
from harness import Harness, HarnessConfig
from harness.config import SandboxConfig

cfg = HarnessConfig(sandbox=SandboxConfig(
    backend="container",
    network=False,                 # opt-in with True if sandbox code must reach the network
    pip_packages=("rich",),        # provisioned into a mounted layer (network only during provisioning)
))
Harness(cfg).solve("…")
​```

The image (Python + `preinstalled` libraries) is **built automatically on first use** and cached; run `harness-build-sandbox` to pre-build it in CI/deploy. Notes: on macOS the runtime runs in a Linux VM, so the session root must sit under a VM-shared path (the default `~/.harness/...` is); the container tier does not enforce `max_file_size_mb` (memory/pid/cpu/network are enforced instead).
```

(Use ordinary triple backticks; the `​` zero-width markers above are only to escape the nested fences in this plan.)

- [ ] **Step 3: Update Project layout + Status & roadmap**

In `## Project layout`, after the `sandbox.py` line add:
```
  sandbox_container.py  hardened OCI-container backend (podman/docker)
  container_runtime.py  runtime detection, image build, package layer
```

In `## Status & roadmap`: move the container item from Planned to Implemented. Append to the `Implemented:` sentence: `, and a **container sandbox tier** (hardened Podman/Docker isolation behind `SandboxExecutor`)`. Delete the `- **Container / micro-VM sandbox tier**` bullet from the Planned list (or, if it mentions micro-VM specifically, narrow it to: `- **Micro-VM sandbox tier** — gVisor / Firecracker, behind the same `SandboxExecutor` interface.`).

- [ ] **Step 4: Verify + commit**

Run: `.venv/bin/python -c "open('README.md').read(); print('ok')"`
```bash
git add README.md
git commit -m "docs(readme): document the container sandbox tier"
```

---

## Self-Review (completed during plan authoring)

**Spec coverage** (`2026-06-10-container-sandbox-design.md`):
- `ContainerSandbox` behind `SandboxExecutor` — Task 4. ✓
- Shared-orchestration refactor (`_OrchestratedSandbox` + `_launch` seam) — Task 2. ✓
- Config: add `backend`/`container_runtime`/`network`/`pip_packages`/`max_cpus`, remove `confine_os` — Task 1. ✓
- Podman/Docker auto-detect (override) — Task 3 (`detect_runtime`). ✓
- Bind-mount root → `/workspace`, runtime → `/runtime` ro, path translation — Task 4 (`_build_run_argv`). ✓
- Network off by default + opt-in — Task 4 (argv) + Task 6 (live). ✓
- Image: Containerfile, tag by hash, auto-build, `harness-build-sandbox` — Tasks 3 & 5. ✓
- Package layer (`pip_packages`, network only during provisioning) — Task 3 (`ensure_layer`) + Task 4 (mount). ✓
- Hardening flags — Task 4. ✓
- Backend selection in `Session.create`, `local` default — Task 5. ✓
- `killed_by` (timeout / killed) — Task 4. ✓
- Tests: offline unit (argv, detection, hashing, refactor stays green) + gated live (run, network-off, handle ingest, layer, parity) — Tasks 1–6. ✓
- Known limits documented (no FSIZE in container; macOS VM path) — Task 7. ✓

**Placeholder scan:** none. The Task-6 note ("fix `_build_run_argv` if a real-runtime test fails") is guidance for genuinely environment-dependent container behavior, not a deferred design decision — the argv contract is fully specified and unit-tested in Task 4.

**Type/name consistency:** `_OrchestratedSandbox`, `_LaunchResult`, `_RunContext`, `_launch`, `ContainerSandbox._build_run_argv(ctx, tag, layer)`, `detect_runtime(override, which=)`, `image_tag(preinstalled)`, `ensure_image(runtime, tag, config, run=)`, `layer_dir(config, base=)`, `ensure_layer(runtime, config, base=, run=)`, and `SandboxConfig.{backend,container_runtime,network,pip_packages,max_cpus}` are used identically across all tasks and match the spec.

**Deferred (per spec):** gVisor/Firecracker micro-VM tiers; publishing a prebuilt image; per-file-size enforcement in the container tier.
