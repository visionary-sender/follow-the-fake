"""`ftf analyze` (one input through the pipeline) and `ftf serve` (the Streamlit demo app)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from ftf.pipeline.analyze import INPUT_KINDS, AnalysisInput, analyze, guess_kind

ERROR_EXIT = 1
USAGE_EXIT = 2


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def register_analyze(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "analyze",
        help="analyse one ad, page capture, URL, keyword or base item against the campaign base",
        description=(
            "Run ingest, content features, enrichment and link/assign on one input and print a "
            "stage summary (JSON). Exit 0 for any decision (assign, new, review); 1 if a stage "
            "failed; 2 on bad arguments. The input is copied to a per-run directory and hashed, "
            "never executed."
        ),
    )
    parser.add_argument("input", metavar="INPUT", help="URL, keyword, file, case folder or base incident id")
    parser.add_argument("--kind", choices=INPUT_KINDS, help="input kind (default: guessed from INPUT)")
    parser.add_argument("--base", type=Path, help="campaign base, a contract dataset.json (default: $FTF_BASE, else the fixture)")
    parser.add_argument("--online", action="store_true", help="allow network enrichment and fetching (default: offline; FTF_OFFLINE=1 forces offline)")
    parser.add_argument("--out", type=Path, help="write the result as a contract Dataset (JSON) here")
    parser.set_defaults(func=run_analyze)


def register_serve(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "serve",
        help="start the demo web app (Streamlit; needs the 'app' extra)",
        description="Start the Streamlit demo app. On the workbench VM keep the default address and use an SSH tunnel.",
    )
    parser.add_argument("--port", type=int, default=8501, help="port (default: %(default)s)")
    parser.add_argument("--address", default="127.0.0.1", help="bind address (default: %(default)s)")
    parser.add_argument("--base", type=Path, help="campaign base, a contract dataset.json (default: $FTF_BASE, else the fixture)")
    parser.set_defaults(func=run_serve)


def run_analyze(args: argparse.Namespace) -> int:
    from ftf.app.base import load_base

    try:
        base = load_base(args.base)
    except (OSError, ValueError) as exc:
        print(f"ftf analyze: cannot load the campaign base: {exc}", file=sys.stderr)
        return USAGE_EXIT
    try:
        kind = args.kind or guess_kind(args.input, base)
        path = Path(args.input) if kind not in ("url", "keyword") and Path(args.input).exists() else None
        text = None if path is not None else args.input
        inp = AnalysisInput(kind=kind, path=path, text=text)
    except ValueError as exc:
        print(f"ftf analyze: {exc}", file=sys.stderr)
        return USAGE_EXIT
    offline = not args.online or _env_flag("FTF_OFFLINE")
    result = analyze(inp, base, offline=offline)
    dataset = result.to_dataset()
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(dataset.to_json() + "\n", encoding="utf-8")
    print(json.dumps(result.summary(), indent=2, ensure_ascii=False))
    return 0 if result.ok else ERROR_EXIT


def streamlit_command(port: int, address: str) -> list[str]:
    home = Path(__file__).resolve().parent.parent / "app" / "Home.py"
    return [
        sys.executable, "-m", "streamlit", "run", str(home),
        "--server.port", str(port),
        "--server.address", address,
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]


def run_serve(args: argparse.Namespace) -> int:
    try:
        import streamlit  # noqa: F401
    except ImportError:
        print("ftf serve: Streamlit is not installed; run: pip install -e '.[app]'", file=sys.stderr)
        return USAGE_EXIT
    env = dict(os.environ)
    if args.base is not None:
        if not args.base.is_file():
            print(f"ftf serve: no such base file: {args.base}", file=sys.stderr)
            return USAGE_EXIT
        env["FTF_BASE"] = str(args.base.resolve())
    try:
        return subprocess.call(streamlit_command(args.port, args.address), env=env)
    except KeyboardInterrupt:
        return 0
