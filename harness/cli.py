"""Thin CLI over Harness.solve()."""

from __future__ import annotations

import argparse
from pathlib import Path

from .api import Harness
from .config import HarnessConfig


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="Run the data-integration harness.")
    p.add_argument("problem", help="The task to solve.")
    p.add_argument("--model", default="gpt-4o-mini", help="Model name.")
    p.add_argument("--root", default=None, help="Workspace root dir (default: a fresh session dir).")
    return p


def run_cli(argv: list[str] | None = None, client=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = HarnessConfig(model=args.model,
                        root_dir=Path(args.root) if args.root else None)
    result = Harness(cfg, client=client).solve(args.problem)
    print(result.final_text)
    print(f"\n[session: {result.session_dir}]")
    return 0


def main() -> None:
    raise SystemExit(run_cli())
