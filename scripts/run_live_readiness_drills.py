#!/usr/bin/env python3
"""Run bounded paper-safety and service-restart drills and write a receipt."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from urllib.request import urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "state" / "live-failure-drills.json"
ET = ZoneInfo("America/New_York")


def fetch(path: str) -> dict:
    with urlopen(f"http://127.0.0.1:5001{path}", timeout=10) as response:
        return json.load(response)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous-started-at", required=True)
    args = parser.parse_args(argv)
    focused = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_accounting_truth.py",
            "tests/test_live_safety.py",
            "tests/test_operator_guard.py",
            "-q",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    health = fetch("/health")
    readiness = fetch("/api/live-readiness")
    checks = {
        "focused_safety_tests": focused.returncode == 0,
        "service_restart_recovered": bool(
            health.get("ok")
            and health.get("started_at")
            and health.get("started_at") != args.previous_started_at
        ),
        "supported_runtime_after_restart": bool((health.get("runtime") or {}).get("supported")),
        "paper_mode_after_restart": health.get("trading_mode") == "PAPER_ONLY",
        "live_execution_still_compiled_out": readiness.get("live_execution_compiled_in") is False,
        "live_trading_still_denied": readiness.get("live_trading_allowed") is False,
    }
    passed = all(checks.values())
    receipt = {
        "schema": "tradesight_live_failure_drills.v1",
        "observed_at_et": datetime.now(ET).isoformat(),
        "status": "PASSED" if passed else "FAILED",
        "all_required_passed": passed,
        "checks": checks,
        "focused_test_returncode": focused.returncode,
        "focused_test_summary": focused.stdout.strip().splitlines()[-1] if focused.stdout.strip() else "",
        "previous_started_at": args.previous_started_at,
        "current_started_at": health.get("started_at"),
        "runtime": health.get("runtime"),
        "live_trading_allowed": readiness.get("live_trading_allowed"),
    }
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
