#!/bin/bash
# TradeSight Launcher Script

set -e

echo "🎯 Starting TradeSight Trading Intelligence Platform..."
echo "========================================"

# Navigate to repo directory where this script lives
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "📊 Launching TradeSight Dashboard..."
echo "🌐 Opening http://localhost:5000"
echo "💡 Press Ctrl+C to stop TradeSight"
echo ""

# Open browser cross-platform
sleep 2 && {
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://localhost:5000"
  elif command -v open >/dev/null 2>&1; then
    open "http://localhost:5000"
  fi
} >/dev/null 2>&1 &

python3 web/dashboard.py