#!/usr/bin/env python3
"""Install a supported, isolated TradeSight runtime from an extracted release.

This installer never clones source code, never changes broker credentials, and
never enables live trading. It creates ``.runtime`` beside this file and records
the installation path for the ``tradesight`` command.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Iterable


MINIMUM = (3, 11)
PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = PROJECT_ROOT / ".runtime"
POINTER = Path.home() / ".config" / "tradesight" / "install.json"


def version_of(executable: str) -> tuple[int, int, int] | None:
    probe = subprocess.run(
        [executable, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
        capture_output=True,
        text=True,
    )
    if probe.returncode:
        return None
    try:
        return tuple(int(part) for part in probe.stdout.strip().split("."))  # type: ignore[return-value]
    except ValueError:
        return None


def interpreter_candidates(explicit: str | None = None) -> Iterable[str]:
    if explicit:
        yield explicit
    yield sys.executable
    for name in ("python3.14", "python3.13", "python3.12", "python3.11", "python3"):
        path = shutil.which(name)
        if path:
            yield path
    for path in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if Path(path).is_file():
            yield path


def choose_interpreter(explicit: str | None = None) -> tuple[str, tuple[int, int, int]]:
    seen: set[str] = set()
    for candidate in interpreter_candidates(explicit):
        resolved = str(Path(candidate).expanduser().resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        version = version_of(resolved)
        if version and version >= MINIMUM:
            return resolved, version
    raise RuntimeError("TradeSight requires Python 3.11 or newer; no supported interpreter was found")


def install(args: argparse.Namespace) -> dict:
    required = (PROJECT_ROOT / "web" / "dashboard.py", PROJECT_ROOT / "requirements-runtime.txt")
    if not all(path.is_file() for path in required):
        raise RuntimeError("Run this installer from the extracted TradeSight release root")
    python, version = choose_interpreter(args.python)
    plan = {
        "schema": "tradesight_install.v1",
        "mode": "PAPER_ONLY",
        "project_root": str(PROJECT_ROOT),
        "runtime_dir": str(RUNTIME_DIR),
        "python": python,
        "python_version": ".".join(map(str, version)),
        "port": 5001,
        "clone_source": False,
        "live_trading_enabled": False,
        "register_install": not args.no_register,
    }
    if args.dry_run:
        return plan

    if RUNTIME_DIR.exists() and args.recreate:
        shutil.rmtree(RUNTIME_DIR)
    if not RUNTIME_DIR.exists():
        subprocess.run([python, "-m", "venv", str(RUNTIME_DIR)], check=True)
    runtime_python = RUNTIME_DIR / "bin" / "python"
    if not args.skip_dependencies:
        subprocess.run(
            [str(runtime_python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(PROJECT_ROOT / "requirements-runtime.txt")],
            check=True,
        )
    subprocess.run(
        [str(runtime_python), "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "-e", str(PROJECT_ROOT)],
        check=True,
    )
    if not args.no_register:
        POINTER.parent.mkdir(parents=True, exist_ok=True)
        POINTER.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    check = subprocess.run(
        [str(RUNTIME_DIR / "bin" / "tradesight"), "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if check.returncode:
        raise RuntimeError(check.stderr.strip() or "Installed runtime verification failed")
    plan["verified"] = json.loads(check.stdout)
    return plan


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Install the TradeSight paper-only runtime")
    result.add_argument("--python", help="supported Python interpreter to use")
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--recreate", action="store_true")
    result.add_argument("--no-register", action="store_true", help="verify an extracted copy without changing the installed CLI pointer")
    result.add_argument("--skip-dependencies", action="store_true", help=argparse.SUPPRESS)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        print(json.dumps(install(args), indent=2, sort_keys=True))
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
