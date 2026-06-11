# Container Sandbox Tier — Design

- **Date:** 2026-06-10
- **Status:** Approved design (brainstormed).
- **Motivation:** `run_python` executes model-authored code. The local tier
  (`LocalSubprocessSandbox`) is best-effort only — a scrubbed-env subprocess with `resource`
  rlimits, a wall-clock timeout, and the `safe_path` jail; it shares the host kernel,
  filesystem (beyond the root), and network. (The `SandboxConfig.confine_os` field is
  aspirational — referenced nowhere.) This phase adds a real isolation tier that runs the code
  in an OCI container, behind the existing `SandboxExecutor` interface, so swapping it in is a
  config change — not a harness-code change.

## Decisions (resolved in brainstorm)

| Decision | Choice |
|---|---|
| Backend | OCI **container** now (Podman **or** Docker, auto-detected); gVisor / Firecracker micro-VM deferred as later swaps behind the same interface |
| Network | **Off by default** (`--network none`); opt-in via config. `run_python` analyzes handles; real I/O stays with the audited `fetch_url`/`web_*`/MCP tools |
| Packages | **Both**: `preinstalled` baked into the image, **plus** a managed provisioning layer (`pip_packages` installed with network *only during provisioning*, then mounted read-only and run network-off). Network-opt-in remains for ad-hoc cases |
| Image | Ship a `Containerfile`; **auto-build on first use**, cached by tag; plus an explicit `harness-build-sandbox` pre-build command |
| Default backend | `local` (no container runtime required for the common case) |

## Architecture

A new `ContainerSandbox` implements the `SandboxExecutor` protocol (`run_script(path, args)` /
`run_code(code, args)` → `ExecResult`). `Session.create` picks the backend from
`config.sandbox.backend`; `local` stays the default. The container backend preserves the *exact*
file-based contract the runtime helpers already use:

- bind-mount the session **root → `/workspace`** (read-write);
- run `python /runtime/_runner.py <script-rel> <args>` with `cwd=/workspace`;
- exchange data through the mount: the parent writes the **registry** and an empty
  **new-handles** file before the run and reads the **emit** file after; the child reads/writes
  handle files and these control files under the root, exactly as today;
- `HARNESS_ROOT=/workspace`; control-file env vars use `/workspace/...` paths; handle paths in
  the registry are already root-relative, so they resolve unchanged inside the container.

### Targeted refactor (DRY, part of this work)

`LocalSubprocessSandbox` currently mixes **orchestration** (write control files → launch child →
parse `ExecResult` → cleanup) with **launching** (`subprocess.run` + `preexec_fn` rlimits). The
orchestration is identical for both backends, so it is extracted into a shared helper
parameterized by a `launch(argv, env, cwd, timeout_s) -> (stdout, stderr, exit_code, killed_by)`
callable:

- `LocalSubprocessSandbox.launch` = `subprocess.run` with the rlimit `preexec_fn` (unchanged
  behavior).
- `ContainerSandbox.launch` = build the `podman/docker run …` argv and `subprocess.run` it.

Both backends become thin; the well-tested control-file path lives in one place.

## Components

| Unit | Responsibility | New dep |
|---|---|---|
| `harness/sandbox.py` (modify) | Extract the shared orchestration helper; keep `LocalSubprocessSandbox` as a `launch` provider over it | — |
| `harness/sandbox_container.py` (new) | `ContainerSandbox` (`SandboxExecutor`): build the hardened `run` argv (mounts, env, limits), provide `launch`, surface `killed_by` (`timeout`/`oom`) | — (shells out to the CLI) |
| `harness/container_runtime.py` (new) | Detect `podman`/`docker` (config override); build image (tagged by hash), ensure/auto-build, build the provisioning layer, expose paths | — |
| `harness/runtime/Containerfile` (new) | `python:3.12-slim`, a non-root user, `pip install` the `preinstalled` set; runtime helpers are **mounted**, not baked | — |
| `harness/config.py` (modify) | `SandboxConfig`: add `backend`, `container_runtime`, `network`, `pip_packages`, `max_cpus`; remove `confine_os` | — |
| `harness/session.py` (modify) | Construct the backend from `config.sandbox.backend`; type the field as `SandboxExecutor` | — |
| `pyproject.toml` (modify) | `harness-build-sandbox` console script | — |

No new Python dependency: the runtime is driven via its CLI (`subprocess`), consistent with how
the harness already shells out.

## Config (`SandboxConfig`)

```python
backend: Literal["local", "container"] = "local"
container_runtime: str | None = None        # None -> auto-detect podman, then docker
network: bool = False                       # off by default; opt-in to enable
pip_packages: tuple[str, ...] = ()          # managed provisioning layer (network only there)
max_cpus: float = 2.0
# kept: timeout_s, max_memory_mb, max_file_size_mb (local-tier only — see Known limits), preinstalled
# removed: confine_os (aspirational; superseded by backend)
```

## Image and package provisioning

- **Base image.** Built from `Containerfile` with `preinstalled` baked in. Tag =
  `harness-sandbox:<hash>` where the hash covers the Python version + sorted `preinstalled`.
  Auto-built on first container run if `<runtime> image exists` is false; reused thereafter.
  `harness-build-sandbox` builds it explicitly (CI/deploy pre-warm; mirrors
  `harness-prefetch-docling`). Build runs with network (it installs packages).
- **Runtime mount.** The harness `runtime/` directory is mounted read-only at `/runtime` and put
  on `PYTHONPATH`, so the image carries only Python + `preinstalled` and never drifts from the
  installed harness version.
- **Package layer (`pip_packages`).** A host cache dir keyed by hash(`pip_packages` + Python
  version), e.g. `~/.harness/pkgcache/<hash>/`. If absent, a one-shot **provisioning container**
  (network **on**) runs `pip install --target=/layer <pip_packages>` into the mounted cache dir.
  Code runs then mount the layer read-only at `/pkgs`, append it to `PYTHONPATH`, **network
  off**. Cached across sessions, so provisioning happens once per package set.

## The hardened `run` invocation

`<runtime> run --rm` plus:
- `--network none` (omitted only when `network=True`);
- `--read-only` root filesystem + `--tmpfs /tmp` (writable scratch);
- `--cap-drop ALL`, `--security-opt no-new-privileges`;
- non-root `--user` (the image's non-root UID);
- `--pids-limit <N>`, `--memory <max_memory_mb>m`, `--cpus <max_cpus>`;
- mounts: `-v <root>:/workspace:rw`, `-v <runtime_dir>:/runtime:ro`, and (if `pip_packages`)
  `-v <layer>:/pkgs:ro`;
- env: `HARNESS_ROOT=/workspace`, the three control-file paths under `/workspace`,
  `PYTHONPATH=/runtime[:/pkgs]`, minimal `PATH`, `HOME=/workspace`, `TMPDIR=/tmp`;
- `python /runtime/_runner.py <script-rel> <args>`.

The wall-clock timeout is enforced by the parent (`subprocess.run(timeout=...)` on the `run`
process); `--rm` plus killing the foreground `run` process tears the container down. A parent
timeout maps to `killed_by="timeout"`. A container killed by the runtime (exit 137 / SIGKILL —
typically the memory cap under `--memory`) maps to `killed_by="killed"` (best-effort: exit code
alone can't always distinguish OOM from other kills without an extra `inspect` call).

## Data flow (one `run_python`)

1. `Session` was built with `ContainerSandbox` because `config.sandbox.backend == "container"`.
2. The shared orchestrator writes the registry + empty new-handles file under the root (as today).
3. `ContainerSandbox.launch` ensures the image exists (auto-build if not) and the package layer
   exists (provision if `pip_packages` and not cached), then runs the hardened `run` argv.
4. The child executes `_runner.py`, reads/writes handles + control files under `/workspace`,
   writes `emit`, appends new-handle records — identical to the local tier.
5. The orchestrator parses stdout/emit/new-handles into an `ExecResult` and cleans up control
   files — shared with the local backend.

## Error handling

- **Never raises into `run_python`** — every failure becomes an `ExecResult` with `error` set
  (the existing contract).
- **Runtime missing:** a clear error naming the fix — install Podman/Docker or set
  `backend="local"`.
- **Image build / provisioning failure:** surfaced as an error with the runtime's stderr; not a
  crash.
- **Timeout / kill:** `ExecResult(killed_by="timeout")` on a parent wall-clock timeout;
  `killed_by="killed"` on a runtime kill (exit 137, typically the memory cap).

## Testing

- **Offline unit tests (CI, no container runtime):**
  - The `run`-argv builder: asserts `--network none` by default (and its omission when
    `network=True`), the three mounts, env path translation (`HARNESS_ROOT=/workspace`,
    control-file paths under `/workspace`, `PYTHONPATH`), and the limit flags.
  - Image-tag hashing (stable, changes with Python version / `preinstalled`).
  - Package-layer cache keying.
  - Runtime detection (podman preferred, docker fallback, override, none-found error) via an
    injected "which" lookup.
  - The shared orchestrator still drives `LocalSubprocessSandbox` correctly (existing sandbox
    tests stay green — proves the refactor is behavior-preserving).
- **Gated integration tests** (skipped unless a podman/docker runtime is available):
  - build the image, `run_python("emit(6*7)")` → `result == 42`;
  - a script that opens a socket fails (network blocked by default), and succeeds when
    `network=True`;
  - a `save()`-d handle inside the container is ingested by the parent;
  - `pip_packages=["<small pure-python pkg>"]` makes that package importable in the sandbox;
  - parity: the same network-free script through `local` and `container` yields equal
    `ExecResult` (stdout/result/new_handles).

## Known limits (documented)

- `RLIMIT_FSIZE` (per-file size cap, from `max_file_size_mb`) has no clean container equivalent
  and is **not enforced** in the container tier; memory, pids, cpu, and network are enforced
  instead. `max_file_size_mb` is kept in config and still applies to the local tier.
- On macOS, Podman/Docker run inside a Linux VM, so the session root must sit under a
  VM-shared path. The default root (`~/.harness/sessions/...`) is under `$HOME`, which the
  machine shares; a custom `root_dir` outside shared paths won't mount.

## Out of scope (noted for later)
- gVisor (`runsc`) and Firecracker/Kata micro-VM tiers — later swaps behind `SandboxExecutor`.
- Publishing a prebuilt image (the image is built locally to honor `preinstalled`).
- Per-file-size enforcement in the container tier.
