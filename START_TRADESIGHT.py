#!/usr/bin/env python3
"""
TradeSight Quick Launcher
Double-click this file to start TradeSight
"""

import os
import sys
import webbrowser
from pathlib import Path
from threading import Timer


def main():
    print("🎯 TradeSight - Trading Intelligence Platform")
    print("=" * 50)
    print("🚀 Starting dashboard...")

    # Use the directory where this file exists, not a hardcoded local path
    project_root = Path(__file__).resolve().parent
    os.chdir(project_root)
    sys.path.insert(0, str(project_root / "src"))

    print("🌐 Dashboard will be at: http://localhost:5000")
    print("💡 Browser will open automatically")
    print("⚠️  Keep this window open while using TradeSight")
    print("")

    def open_browser():
        try:
            webbrowser.open("http://localhost:5000")
            print("✅ Browser opened")
        except Exception:
            print("ℹ️  Please manually open: http://localhost:5000")

    Timer(3.0, open_browser).start()

    try:
        from web.dashboard import app

        print("⚡ Web server starting...")
        app.run(host="127.0.0.1", port=5000, debug=False)
    except Exception as e:
        print(f"❌ Error: {e}")
        print(f"💡 Try running from terminal: cd {project_root} && python3 web/dashboard.py")
        input("\nPress Enter to close...")


if __name__ == "__main__":
    main()