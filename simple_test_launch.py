#!/usr/bin/env python3

import os
import sys
import webbrowser
from pathlib import Path
from threading import Timer

project_root = Path(__file__).resolve().parent
os.chdir(project_root)
sys.path.insert(0, str(project_root / "src"))

print("🎯 Starting TradeSight Dashboard...")
print("🌐 Dashboard will be available at: http://localhost:5000")
print("💡 Keep this window open while using TradeSight")
print("")


def open_browser():
    try:
        webbrowser.open("http://localhost:5000")
        print("✅ Browser opened automatically")
    except Exception:
        print("ℹ️  Please manually open: http://localhost:5000")


Timer(2.0, open_browser).start()

try:
    from web.dashboard import app

    app.run(host="127.0.0.1", port=5000, debug=False)
except Exception as e:
    print(f"❌ Error starting dashboard: {e}")
    input("Press Enter to close...")