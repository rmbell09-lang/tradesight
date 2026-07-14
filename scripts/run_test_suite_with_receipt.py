#!/usr/bin/env python3
"""Run pytest and write a truthful dashboard receipt from the actual result."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECEIPT_PATH = ROOT / "state" / "test-suite-status.json"


def parse_count(output: str, label: str) -> int:
    matches = re.findall(rf"(\d+)\s+{re.escape(label)}\b", output)
    return int(matches[-1]) if matches else 0


def main() -> int:
    started_at = datetime.now(timezone.utc)
    command = [sys.executable, "-m", "pytest", "-q"]
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print(result.stdout, end="")

    passed = parse_count(result.stdout, "passed")
    failed = parse_count(result.stdout, "failed")
    errors = parse_count(result.stdout, "error") + parse_count(result.stdout, "errors")
    skipped = parse_count(result.stdout, "skipped")
    total = passed + failed + errors + skipped
    receipt = {
        "schema": "tradesight.test_suite.v1",
        "command": command,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "exit_code": result.returncode,
        "passed": passed,
        "failed": failed + errors,
        "skipped": skipped,
        "total": total,
        "verified": result.returncode == 0 and passed > 0,
    }
    RECEIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = RECEIPT_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    temporary.replace(RECEIPT_PATH)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
