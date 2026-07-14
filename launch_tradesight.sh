#!/bin/bash
# TradeSight Launcher Script

echo "🎯 Starting TradeSight Trading Intelligence Platform..."
echo "========================================"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Activate any virtual environment if needed (optional)
# source venv/bin/activate 2>/dev/null || true

# Start the web dashboard
echo "📊 Launching TradeSight Dashboard..."
echo "🌐 Opening http://localhost:5000"
echo "💡 Press Ctrl+C to stop TradeSight"
echo ""

# Open browser (optional - remove if you don't want auto-open)
sleep 2 && open "http://localhost:5000" 2>/dev/null &

# Start the web server
python3 web/dashboard.py
