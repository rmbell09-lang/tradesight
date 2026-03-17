# TradeSight Paper Trader Audit — Mar 14, 2026 (7:10 PM ET)

## Status: Running but NOT trading

### Cron: Active
- `/Volumes/Crucial X10/TradeSight/run_scan.sh` — every 5 min
- Sessions logged: 09:35, 13:00, 15:30 on Mar 14

### Root Cause: No tournament winners
Every session logs:
- "No recent tournament winners found"
- "No winning strategies found"

### Result
- 0 trades logged since RSI tuning (Mar 14)
- positions.db: 0 rows in `positions`, 0 rows in `portfolio_history` (src/data)
- Portfolio stuck at $500 cash, $0 P&L
- DEMO mode active (no Alpaca API keys in keychain)

### What needs to happen
The strategy tournament needs to run and produce winners before the paper trader will execute any trades. Either:
1. Run a strategy tournament manually
2. Fix tournament winner detection pipeline
3. Or inject a seed strategy so paper trader has something to trade
