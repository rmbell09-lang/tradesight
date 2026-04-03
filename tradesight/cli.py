#!/usr/bin/env python3
"""
TradeSight CLI — launch the self-hosted trading strategy lab.
"""
import os
import sys
import subprocess
import webbrowser
import time
from pathlib import Path


REPO_URL = "https://github.com/rmbell09-lang/TradeSight"
DEFAULT_DIR = Path.home() / "TradeSight"


def main():
    print("🎯 TradeSight — AI Trading Strategy Lab")
    print("=" * 50)

    tradesight_dir = Path(os.environ.get("TRADESIGHT_DIR", str(DEFAULT_DIR)))

    # Clone if not present
    if not tradesight_dir.exists():
        print(f"📥 Cloning TradeSight to {tradesight_dir} ...")
        result = subprocess.run(
            ["git", "clone", REPO_URL, str(tradesight_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"❌ Clone failed: {result.stderr}")
            print(f"   Manually clone: git clone {REPO_URL}")
            sys.exit(1)
        print("✅ Cloned successfully.")
    else:
        print(f"📂 Found TradeSight at {tradesight_dir}")

    # Install requirements
    req_file = tradesight_dir / "requirements.txt"
    if req_file.exists():
        print("📦 Installing dependencies ...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req_file), "-q"],
            check=True,
        )

    # Launch
    print("🚀 Starting dashboard at http://localhost:5000 ...")
    time.sleep(1)
    webbrowser.open("http://localhost:5000")

    app_script = tradesight_dir / "run_paper_trader.py"
    if not app_script.exists():
        app_script = tradesight_dir / "START_TRADESIGHT.py"

    os.chdir(str(tradesight_dir))
    sys.path.insert(0, str(tradesight_dir / "src"))
    os.execv(sys.executable, [sys.executable, str(app_script)])


if __name__ == "__main__":
    main()
