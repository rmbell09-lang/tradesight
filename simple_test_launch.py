#!/usr/bin/env python3
import os
import sys
import webbrowser
import time
from threading import Timer

# Change to TradeSight directory
os.chdir("/Volumes/Crucial X10/TradeSight")
sys.path.insert(0, "src")

print("🎯 Starting TradeSight Dashboard...")
print("🌐 Dashboard will be available at: http://localhost:5000")
print("💡 Keep this window open while using TradeSight")
print("")

# Auto-open browser after 2 seconds
def open_browser():
    try:
        webbrowser.open("http://localhost:5000")
        print("✅ Browser opened automatically")
    except:
        print("ℹ️  Please manually open: http://localhost:5000")

Timer(2.0, open_browser).start()

# Import and start dashboard
try:
    from web.dashboard import app
    app.run(host="127.0.0.1", port=5000, debug=False)
except Exception as e:
    print(f"❌ Error starting dashboard: {e}")
    input("Press Enter to close...")
