"""Thin CLI over Harness.solve()."""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path
from typing import Any

from .api import Harness
from .config import HarnessConfig


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="Run the data-integration harness.")
    p.add_argument("problem", help="The task to solve.")
    p.add_argument("--model", default="gpt-5-mini", help="Model name.")
    p.add_argument("--root", default=None, help="Workspace root dir (default: a fresh session dir).")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Print each tool call (and run_python code) as it happens.")
    return p


def _short(value: Any, limit: int = 300) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"… ({len(text)} chars)"


def make_verbose_printer(write=print):
    """A tool-call reporter for --verbose: shows each call, the run_python code, and the result."""
    def on_tool_call(name: str, kwargs: dict, result: Any) -> None:
        arg_str = ", ".join(f"{k}={_short(v, 80)}" for k, v in kwargs.items() if k != "code")
        write(f"\n→ {name}({arg_str})")
        code = kwargs.get("code")
        if code:
            write(textwrap.indent(code.rstrip(), "    "))
        write(f"← {_short(result)}")
    return on_tool_call


def run_cli(argv: list[str] | None = None, client=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = HarnessConfig(model=args.model,
                        root_dir=Path(args.root) if args.root else None)
    result = Harness(cfg, client=client).solve(args.problem)
    if result.final_text:
        print(result.final_text)
    if result.error:
        print(f"\n[run did not complete: {result.error}]")
    print(f"\n[session: {result.session_dir}]")
    return 1 if result.error else 0


def main() -> None:
    raise SystemExit(run_cli())
