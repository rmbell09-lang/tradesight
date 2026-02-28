#!/bin/bash

# TradeSight Nightly Strategy Improvement
# Runs automated strategy tournaments and generates performance reports
# Designed to be called by cron for overnight execution

set -e  # Exit on any error

# Configuration
PROJECT_DIR="/Volumes/Crucial X10/TradeSight"
PYTHON_PATH="/usr/bin/python3"
LOG_FILE="$PROJECT_DIR/logs/cron_$(date +%Y%m%d).log"
LOCK_FILE="$PROJECT_DIR/logs/strategy_automation.lock"

# Ensure directories exist
mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/reports" "$PROJECT_DIR/data"

# Function to log messages
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" | tee -a "$LOG_FILE"
}

# Function to cleanup on exit
cleanup() {
    if [ -f "$LOCK_FILE" ]; then
        rm -f "$LOCK_FILE"
        log "Lock file removed"
    fi
}

# Set cleanup trap
trap cleanup EXIT

# Check for existing lock (prevent concurrent runs)
if [ -f "$LOCK_FILE" ]; then
    log "ERROR: Lock file exists - another instance may be running"
    exit 1
fi

# Create lock file
echo "$$" > "$LOCK_FILE"
log "Created lock file with PID $$"

# Change to project directory
cd "$PROJECT_DIR" || {
    log "ERROR: Cannot change to project directory: $PROJECT_DIR"
    exit 1
}

log "=== TradeSight Nightly Strategy Development Started ==="
log "Project Directory: $PROJECT_DIR"
log "Python Path: $PYTHON_PATH"

# Check if Python and required modules are available
if ! "$PYTHON_PATH" -c "import sys, os; sys.path.insert(0, 'src'); from automation.strategy_automation import StrategyAutomation" 2>/dev/null; then
    log "ERROR: Python dependencies not available"
    exit 1
fi

# Run the overnight strategy development session
log "Starting overnight tournament session..."
"$PYTHON_PATH" src/automation/strategy_automation.py 2>&1 | tee -a "$LOG_FILE"

# Check if the session completed successfully
if [ ${PIPESTATUS[0]} -eq 0 ]; then
    log "✅ Overnight strategy development completed successfully"
    
    # Generate standalone daily report
    log "Generating final daily report..."
    "$PYTHON_PATH" src/automation/strategy_automation.py report >> "$LOG_FILE" 2>&1
    
    # Check for any critical findings in reports
    LATEST_REPORT="$PROJECT_DIR/reports/daily_report_$(date +%Y%m%d).txt"
    if [ -f "$LATEST_REPORT" ]; then
        log "Daily report generated: $LATEST_REPORT"
        
        # Extract key metrics for cron log
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
    
    # Cleanup old logs (keep last 14 days)
    log "Cleaning up old logs..."
    find "$PROJECT_DIR/logs" -name "*.log" -type f -mtime +14 -delete 2>/dev/null || true
    find "$PROJECT_DIR/reports" -name "*.txt" -type f -mtime +14 -delete 2>/dev/null || true
    
    log "✅ Nightly strategy development cycle complete"
    exit 0
else
    log "❌ ERROR: Overnight strategy development failed with exit code $?"
    exit 1
fi