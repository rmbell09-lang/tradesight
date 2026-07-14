#!/usr/bin/env python3
"""Fail closed when TradeSight is run from the wrong source tree."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "ops" / "canonical_source.json"


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def main() -> int:
    manifest = json.loads(MANIFEST_PATH.read_text())
    expected_root = Path(manifest["canonical_path"]).resolve()
    origin = git("config", "--get", "remote.origin.url")
    branch = git("branch", "--show-current")
    head = git("rev-parse", "HEAD")

    checks = {
        "canonical_path": ROOT == expected_root,
        "canonical_branch": branch == manifest["canonical_branch"],
        "remote_url": origin == manifest["remote_url"],
        "remote_has_no_userinfo": not (
            origin.startswith(("http://", "https://"))
            and "@" in origin.split("//", 1)[1].split("/", 1)[0]
        ),
        "paper_only_manifest": manifest.get("paper_only") is True,
        "backup_exists": Path(manifest["prechange_backup"]).is_dir(),
        "backup_checksums_exist": (
            Path(manifest["prechange_backup"]) / "SHA256SUMS"
        ).is_file(),
    }

    launch_agents = {}
    for raw_path in manifest.get("launch_agent_files", []):
        path = Path(raw_path)
        try:
            content = path.read_text()
            launch_agents[str(path)] = {
                "exists": True,
                "uses_canonical_path": str(expected_root) in content,
            }
        except OSError:
            launch_agents[str(path)] = {
                "exists": False,
                "uses_canonical_path": False,
            }
    checks["launch_agents_use_canonical_path"] = all(
        item["exists"] and item["uses_canonical_path"]
        for item in launch_agents.values()
    )

    noncanonical = {}
    for raw_path in manifest.get("known_noncanonical_copies", []):
        path = Path(raw_path)
        noncanonical[str(path)] = {
            "exists": path.exists(),
            "is_canonical": path.exists() and path.resolve() == expected_root,
        }
    checks["alternate_copies_not_canonical"] = not any(
        item["is_canonical"] for item in noncanonical.values()
    )

    ok = all(checks.values())
    payload = {
        "schema": "tradesight.canonical_source.v1",
        "ok": ok,
        "root": str(ROOT),
        "branch": branch,
        "head": head,
        "paper_only": True,
        "checks": checks,
        "launch_agents": launch_agents,
        "noncanonical_copies": noncanonical,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
