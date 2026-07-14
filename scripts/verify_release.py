#!/usr/bin/env python3
"""Verify a built release can be extracted and launched in paper-only mode."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)
    archive = args.archive.resolve()
    manifest_path = archive.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != manifest["sha256"]:
        raise SystemExit("archive checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="tradesight-release-") as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            if any("/.env" in name or name.endswith(".db") or "/state/" in name for name in names):
                raise SystemExit("release contains runtime/private state")
            zf.extractall(root)
        project = next(path for path in root.iterdir() if path.is_dir())
        dry = subprocess.run(
            [args.python, "install_tradesight.py", "--dry-run"],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        )
        plan = json.loads(dry.stdout)
        if plan["mode"] != "PAPER_ONLY" or plan["port"] != 5001 or plan["clone_source"]:
            raise SystemExit("installer plan violated release policy")
    print(json.dumps({"ok": True, "archive": str(archive), "sha256": digest, "install_plan": plan}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
