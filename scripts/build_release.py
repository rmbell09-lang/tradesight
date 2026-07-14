#!/usr/bin/env python3
"""Build a fail-closed TradeSight source release without publishing it."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "ops" / "release_policy.json"
SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [Path(item.decode()) for item in result.stdout.split(b"\0") if item]


def included(path: Path, policy: dict) -> bool:
    value = path.as_posix()
    if path.parts and path.parts[0] in set(policy.get("excluded_roots") or []):
        return False
    if value in policy["allowed_files"]:
        return True
    if not any(value == root or value.startswith(root + "/") for root in policy["allowed_roots"]):
        return False
    wrapped = "/" + value
    return not any(fragment in value or fragment in wrapped for fragment in policy["excluded_fragments"])


def scan_file(path: Path, policy: dict) -> list[str]:
    try:
        text = path.read_text(errors="ignore")
    except OSError as exc:
        return [f"unreadable:{path}:{exc}"]
    findings = [f"secret-pattern:{path}" for pattern in SECRET_PATTERNS if pattern.search(text)]
    if path.as_posix() in {
        "README.md",
        "docs/index.html",
        "docs/GUMROAD_LISTING.md",
        "docs/LIVE_READINESS.md",
    }:
        findings.extend(
            f"prohibited-claim:{path}:{claim}"
            for claim in policy["prohibited_claims"]
            if claim.lower() in text.lower()
        )
    return findings


def main() -> int:
    if sys.version_info < (3, 11):
        print("Release builds require Python 3.11+", file=sys.stderr)
        return 2
    policy = json.loads(POLICY_PATH.read_text())
    files = sorted(path for path in tracked_files() if included(path, policy) and (ROOT / path).is_file())
    findings = [finding for path in files for finding in scan_file(ROOT / path, policy)]
    required = {Path(item) for item in policy["allowed_files"]}
    missing = sorted(str(path) for path in required if path not in files)
    if findings or missing:
        print(json.dumps({"ok": False, "findings": findings, "missing": missing}, indent=2), file=sys.stderr)
        return 1

    version = __import__("tomllib").loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    out_dir = ROOT / "dist" / "releases"
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"tradesight-{version}-paper-only.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(ROOT / path, f"TradeSight-{version}/{path.as_posix()}")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {
        "schema": "tradesight_release_manifest.v1",
        "version": version,
        "mode": "PAPER_ONLY",
        "published": False,
        "archive": str(archive.relative_to(ROOT)),
        "sha256": digest,
        "file_count": len(files),
        "minimum_python": policy["minimum_python"],
        "entrypoint": policy["entrypoint"],
    }
    manifest_path = archive.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    archive.with_suffix(".sha256").write_text(f"{digest}  {archive.name}\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
