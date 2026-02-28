#!/bin/bash
# TradeSight Scanner - Automated Market Scan
# Runs every 5 minutes via cron

cd "/Volumes/Crucial X10/TradeSight"
python3 src/scanner.py >> data/scan.log 2>&1

# Keep only last 1000 lines of log to prevent unlimited growth
tail -1000 data/scan.log > data/scan.log.tmp && mv data/scan.log.tmp data/scan.log