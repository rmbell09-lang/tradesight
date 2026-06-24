#!/bin/bash

# TradeSight Nightly Strategy Improvement
# Runs automated strategy tournaments and generates performance reports
# Designed to be called by cron for overnight execution

set -e

# Configuration
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_PATH="${PYTHON_PATH:-/usr/bin/python3}"
LOG_FILE="$PROJECT_DIR/logs/cron_$(date +%Y%m%d).log"
LOCK_FILE="$PROJECT_DIR/logs/strategy_automation.lock"

mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/reports" "$PROJECT_DIR/data"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOG_FILE"
}

cleanup() {
    if [ -f "$LOCK_FILE" ]; then
        rm -f "$LOCK_FILE"
        log "Lock file removed"
    fi
}

trap cleanup EXIT

if [ -f "$LOCK_FILE" ]; then
    log "ERROR: Lock file exists - another instance may be running"
    exit 1
fi

echo "$$" > "$LOCK_FILE"
log "Created lock file with PID $$"

cd "$PROJECT_DIR" || {
    log "ERROR: Cannot change to project directory: $PROJECT_DIR"
    exit 1
}

log "=== TradeSight Nightly Strategy Development Started ==="
log "Project Directory: $PROJECT_DIR"
log "Python Path: $PYTHON_PATH"

if ! "$PYTHON_PATH" -c "import sys; sys.path.insert(0, 'src'); from automation.strategy_automation import StrategyAutomation" 2>/dev/null; then
    log "ERROR: Python dependencies not available"
    exit 1
fi

log "Starting overnight tournament session..."
"$PYTHON_PATH" src/automation/strategy_automation.py 2>&1 | tee -a "$LOG_FILE"

if [ ${PIPESTATUS[0]} -eq 0 ]; then
    log "✅ Overnight strategy development completed successfully"

    log "Generating final daily report..."
    "$PYTHON_PATH" src/automation/strategy_automation.py report >> "$LOG_FILE" 2>&1

    LATEST_REPORT="$PROJECT_DIR/reports/daily_report_$(date +%Y%m%d).txt"

    if [ -f "$LATEST_REPORT" ]; then
        log "Daily report generated: $LATEST_REPORT"

        if grep -q "Sessions completed:" "$LATEST_REPORT"; then
            SESSIONS=$(grep "Sessions completed:" "$LATEST_REPORT" | cut -d: -f2 | xargs)
            log "Sessions completed today: $SESSIONS"
        fi

        if grep -q "Winner:" "$LATEST_REPORT"; then
            WINNERS=$(grep "Winner:" "$LATEST_REPORT" | head -3)
            log "Recent winners:"
            echo "$WINNERS" | while read line; do
                log "  $line"
            done
        fi
    fi

    log "Cleaning up old logs..."
    find "$PROJECT_DIR/logs" -name "*.log" -type f -mtime +14 -delete 2>/dev/null || true
    find "$PROJECT_DIR/reports" -name "*.txt" -type f -mtime +14 -delete 2>/dev/null || true

    log "✅ Nightly strategy development cycle complete"
    exit 0
else
    log "❌ ERROR: Overnight strategy development failed"
    exit 1
fi