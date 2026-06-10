"""Thin CLI over Harness.solve()."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from .api import Harness
from .config import HarnessConfig
from .status import StatusEvent


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="Run the data-integration harness.")
    p.add_argument("problem", help="The task to solve.")
    p.add_argument("--model", default="gpt-5-mini", help="Model name.")
    p.add_argument("--root", default=None, help="Workspace root dir (default: a fresh session dir).")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Print live tool status to stderr as the task runs.")
    return p


def make_status_printer(write: Callable[[str], None] | None = None):
    """A status sink for --verbose: formats each StatusEvent to a line."""
    if write is None:
        def write(line: str) -> None:
            print(line, file=sys.stderr)

    def on_status(event: StatusEvent) -> None:
        progress = ""
        if event.current is not None and event.total is not None:
            progress = f" [{event.current:g}/{event.total:g}]"
        write(f"→ {event.tool}: {event.message}{progress}")

    return on_status


def run_cli(argv: list[str] | None = None, client=None) -> int:
    args = build_parser().parse_args(argv)
    on_status = make_status_printer() if args.verbose else None
    cfg = HarnessConfig(model=args.model,
                        root_dir=Path(args.root) if args.root else None)
    result = Harness(cfg, client=client).solve(args.problem, on_status=on_status)
    if result.final_text:
        print(result.final_text)
    if result.error:
        print(f"\n[run did not complete: {result.error}]")
    print(f"\n[session: {result.session_dir}]")
    return 1 if result.error else 0


def main() -> None:
    raise SystemExit(run_cli())
