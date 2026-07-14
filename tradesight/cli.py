#!/usr/bin/env python3
"""TradeSight CLI — launch the installed paper-trading dashboard.

The CLI never clones another repository and never installs dependencies at
runtime. Installation is an explicit, inspectable step handled by
``install_tradesight.py``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser


DEFAULT_PORT = 5001
INSTALL_POINTER = Path.home() / ".config" / "tradesight" / "install.json"


def _supported_python() -> bool:
    return sys.version_info >= (3, 11)


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    explicit = os.environ.get("TRADESIGHT_HOME") or os.environ.get("TRADESIGHT_DIR")
    if explicit:
        roots.append(Path(explicit).expanduser())
    # A checked-out or extracted release must prove its own files before any
    # machine-wide install pointer is consulted.
    roots.extend((Path.cwd(), Path(__file__).resolve().parents[1]))
    if INSTALL_POINTER.is_file():
        try:
            roots.append(Path(json.loads(INSTALL_POINTER.read_text())["project_root"]).expanduser())
        except (OSError, KeyError, TypeError, ValueError):
            pass
    unique: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def resolve_project_root() -> Path:
    for root in _candidate_roots():
        if (root / "web" / "dashboard.py").is_file() and (root / "src").is_dir():
            return root
    raise RuntimeError(
        "TradeSight application files were not found. Run install_tradesight.py "
        "from the extracted TradeSight release or set TRADESIGHT_HOME."
    )


def _wait_for_health(url: str, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                return response.status == 200
        except Exception:
            time.sleep(0.25)
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the TradeSight paper-only dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TRADESIGHT_PORT", DEFAULT_PORT)))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check", action="store_true", help="verify the install without starting the server")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not _supported_python():
        print("TradeSight requires Python 3.11 or newer.", file=sys.stderr)
        return 2
    try:
        root = resolve_project_root()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.check:
        print(json.dumps({
            "ok": True,
            "mode": "PAPER_ONLY",
            "project_root": str(root),
            "python": sys.version.split()[0],
            "dashboard": str(root / "web" / "dashboard.py"),
            "port": args.port,
        }, sort_keys=True))
        return 0

    env = os.environ.copy()
    env["TRADESIGHT_PORT"] = str(args.port)
    env["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root)))
    command = [sys.executable, str(root / "web" / "dashboard.py")]
    process = subprocess.Popen(command, cwd=root, env=env)
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser and _wait_for_health(url + "/health"):
        webbrowser.open(url)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        return process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
