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


PROJECT_ROOT = Path(__file__).resolve().parent
DASHBOARD_URL = "http://localhost:5000"


def main():
    print("🎯 TradeSight - Trading Intelligence Platform")
    print("=" * 50)
    print("🚀 Starting dashboard...")

    os.chdir(PROJECT_ROOT)
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

    print(f"📁 Project folder: {PROJECT_ROOT}")
    print(f"🌐 Dashboard will be at: {DASHBOARD_URL}")
    print("💡 Browser will open automatically")
    print("⚠️  Keep this window open while using TradeSight")
    print("")

    # Auto-open browser
    def open_browser():
        try:
            webbrowser.open(DASHBOARD_URL)
            print("✅ Browser opened")
        except Exception:
            print(f"ℹ️  Please manually open: {DASHBOARD_URL}")

    Timer(3.0, open_browser).start()

    # Start Flask app
    try:
        from web.dashboard import app
        print("⚡ Web server starting...")
        app.run(host="127.0.0.1", port=5000, debug=False)
    except Exception as e:
        print(f"❌ Error: {e}")
        print(f"💡 Try running from terminal: cd {PROJECT_ROOT} && python3 web/dashboard.py")
        input("\nPress Enter to close...")

if __name__ == "__main__":
    main()
