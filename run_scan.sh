#!/bin/bash
# TradeSight Scanner - Automated Market Scan
# Runs every 5 minutes via cron

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p data

python3 src/scanner.py >> data/scan.log 2>&1

tail -1000 data/scan.log > data/scan.log.tmp && mv data/scan.log.tmp data/scan.log