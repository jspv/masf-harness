"""Local subprocess sandbox: runs agent scripts in a child process, root-confined."""

from __future__ import annotations

import json
import resource
import subprocess
import sys
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
        self._inline_counter = 0

    def run_code(self, code: str, args: list[str] | None = None) -> ExecResult:
        self._inline_counter += 1
        scripts = self.root / _SCRIPTS_DIR
        scripts.mkdir(exist_ok=True)
        rel = f"{_SCRIPTS_DIR}/_inline_{self._inline_counter}.py"
        (self.root / rel).write_text(code, encoding="utf-8")
        return self.run_script(rel, args)

    def run_script(self, path: str, args: list[str] | None = None) -> ExecResult:
        script = safe_path(self.root, path)  # raises PathEscapesRootError if outside

        new_handles_file = self.root / "_new_handles.jsonl"
        emit_file = self.root / "_emit.json"
        registry_file = self.root / "_registry.json"
        for f in (new_handles_file, emit_file):
            f.unlink(missing_ok=True)
        new_handles_file.touch()
        registry = {hid: {"kind": h.kind, "path": h.path}
                    for hid, h in self.store.manifest_handles().items()}
        registry_file.write_text(json.dumps(registry), encoding="utf-8")

        env = {
            "PATH": _minimal_path(),
            "HARNESS_ROOT": str(self.root),
            "HARNESS_REGISTRY": str(registry_file),
            "HARNESS_NEW_HANDLES": str(new_handles_file),
            "HARNESS_EMIT": str(emit_file),
            "PYTHONPATH": str(_RUNTIME_DIR),
        }

        killed_by = None
        try:
            proc = subprocess.run(
                [sys.executable, str(script), *(args or [])],
                cwd=self.root, env=env, capture_output=True, text=True,
                timeout=self.config.timeout_s, preexec_fn=self._limits(),
            )
            stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            killed_by = "timeout"
            stdout = e.stdout or ""
            stderr = (e.stderr or "") + "\nharness: killed (timeout)"
            exit_code = -1
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")

        result = None
        if exit_code == 0 and emit_file.exists():
            result = json.loads(emit_file.read_text(encoding="utf-8"))

        new_handles = self._ingest_new_handles(new_handles_file)
        error = stderr.strip() or None if exit_code != 0 else None

        return ExecResult(stdout=stdout, result=result, error=error,
                          exit_code=exit_code, new_handles=new_handles,
                          killed_by=killed_by)

    def _ingest_new_handles(self, new_handles_file: Path) -> list[str]:
        ids: list[str] = []
        if not new_handles_file.exists():
            return ids
        for line in new_handles_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            self.store.register(rec)
            ids.append(rec["id"])
        return ids

    def _limits(self):
        cfg = self.config

        def set_limits() -> None:
            mem = cfg.max_memory_mb * 1024 * 1024
            fsize = cfg.max_file_size_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            except (ValueError, OSError):
                pass
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        return set_limits


def _minimal_path() -> str:
    return "/usr/bin:/bin:/usr/local/bin"
