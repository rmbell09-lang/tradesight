#!/usr/bin/env python3
"""
TradeSight Quick Launcher
Double-click this file to start TradeSight
"""

import os
import sys
import webbrowser
import time
from threading import Timer

def main():
    print("🎯 TradeSight - Trading Intelligence Platform")
    print("=" * 50)
    print("🚀 Starting dashboard...")
    
    # Change to TradeSight directory
    os.chdir("/Volumes/Crucial X10/TradeSight")
    sys.path.insert(0, "src")
    
    print("🌐 Dashboard will be at: http://localhost:5000")
    print("💡 Browser will open automatically")
    print("⚠️  Keep this window open while using TradeSight")
    print("")
    
    # Auto-open browser
    def open_browser():
        try:
            webbrowser.open("http://localhost:5000")
            print("✅ Browser opened")
        except Exception as e:
            print(f"ℹ️  Please manually open: http://localhost:5000")
    
    Timer(3.0, open_browser).start()
    
    # Start Flask app
    try:
        from web.dashboard import app
        print("⚡ Web server starting...")
        app.run(host="127.0.0.1", port=5000, debug=False)
    except Exception as e:
        print(f"❌ Error: {e}")
        print("💡 Try running from terminal: cd /Volumes/Crucial X10/TradeSight && python3 web/dashboard.py")
        input("\nPress Enter to close...")

if __name__ == "__main__":
    main()
