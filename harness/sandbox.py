"""Local subprocess sandbox: runs agent scripts in a child process, root-confined.

NOTE: ``run_script`` / ``run_code`` are sequential and NOT re-entrant per root.
Each run uses per-run-unique control files (keyed by pid + run counter), so
sequential runs never clobber each other's scratch, but a single sandbox instance
is not designed to be driven concurrently from multiple threads against one root.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import SandboxConfig
from .handles import HandleStore
from .paths import safe_path

_RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
_SCRIPTS_DIR = ".scripts"


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


class LocalSubprocessSandbox:
    def __init__(self, root: Path | str, store: HandleStore,
                 config: SandboxConfig | None = None) -> None:
        self.root = Path(root).resolve()
        self.store = store
        self.config = config or SandboxConfig()
        self._run_counter = 0

    def run_code(self, code: str, args: list[str] | None = None) -> ExecResult:
        scripts = self.root / _SCRIPTS_DIR
        scripts.mkdir(exist_ok=True)
        # Collision-free across instances/processes; scripts persist as debuggable artifacts.
        fd, abspath = tempfile.mkstemp(prefix="inline_", suffix=".py", dir=scripts)
        os.close(fd)
        Path(abspath).write_text(code, encoding="utf-8")
        rel = str(Path(abspath).relative_to(self.root))
        return self.run_script(rel, args)

    def run_script(self, path: str, args: list[str] | None = None) -> ExecResult:
        script = safe_path(self.root, path)  # raises PathEscapesRootError if outside
        argv = [str(a) for a in (args or [])]  # coerce so non-str args fail clearly, not opaquely

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

            env = {
                "PATH": _minimal_path(),
                "HOME": str(self.root),    # keep ~/-relative scratch inside the root
                "TMPDIR": str(self.root),
                "HARNESS_ROOT": str(self.root),
                "HARNESS_REGISTRY": str(registry_file),
                "HARNESS_NEW_HANDLES": str(new_handles_file),
                "HARNESS_EMIT": str(emit_file),
                "PYTHONPATH": str(_RUNTIME_DIR),
            }

            killed_by = None
            try:
                proc = subprocess.run(
                    [sys.executable, str(script), *argv],
                    cwd=self.root, env=env, capture_output=True, text=True,
                    timeout=self.config.timeout_s, preexec_fn=self._limits(),
                )
                stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
            except subprocess.TimeoutExpired as e:
                killed_by = "timeout"
                stdout = _as_text(e.stdout)
                stderr = _as_text(e.stderr) + "\nharness: killed (timeout)"
                exit_code = -1

            # Read the emit payload defensively: a misbehaving script must yield an
            # ExecResult, never make run_script raise.
            result = None
            emit_error = None
            if exit_code == 0 and emit_file.exists():
                try:
                    result = json.loads(emit_file.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    emit_error = f"harness: malformed emit payload: {e}"

            new_handles = self._ingest_new_handles(new_handles_file)

            base_error = (stderr.strip() or None) if exit_code != 0 else None
            error = "\n".join(p for p in (base_error, emit_error) if p) or None

            return ExecResult(stdout=stdout, stderr=stderr, result=result, error=error,
                              exit_code=exit_code, new_handles=new_handles,
                              killed_by=killed_by)
        finally:
            for f in (new_handles_file, emit_file, registry_file):
                f.unlink(missing_ok=True)

    def _ingest_new_handles(self, new_handles_file: Path) -> list[str]:
        """Register handles the child wrote. Tolerant: a corrupt line is skipped, not
        fatal, so one bad record can't abort ingestion or leave the store inconsistent."""
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

    def _limits(self):
        cfg = self.config

        def set_limits() -> None:
            mem = cfg.max_memory_mb * 1024 * 1024
            fsize = cfg.max_file_size_mb * 1024 * 1024
            for res_id, limit in (
                (resource.RLIMIT_AS, mem),      # may be rejected on macOS — best-effort
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
