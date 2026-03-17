# TradeSight RSI Signal Audit — 2026-03-14

## Result: RSI Signals DO Trigger ✅ (with caveat)

### What Was Tested
- `src/indicators/technical_indicators.py` — core indicator engine
- `src/scanners/stock_opportunities.py` — opportunity scanner
- Latest optimization report: `reports/optimization_20260313_200027.json`

### Findings

**Core Engine (`technical_indicators.py`)**
- RSI computes correctly via `talib.RSI()`
- Declining prices → RSI=0.00, signal=-1 (oversold) ✅
- Rising prices → RSI=100.00, signal=1 (overbought) ✅
- Signal triggers confirmed working

**Stock Scanner (`stock_opportunities.py`)**
- Independent RSI calculation (rolling 14-period)
- Declining prices → RSI near 0, triggers `rsi_oversold` ✅
- Scanner does trigger on symbols

### Gap Found ⚠️

Optimization tuned thresholds to **oversold=33, overbought=65** (Sharpe 2.65, PnL +1778%)
but both files still use **hardcoded 30/70**.

This means the scanner misses signals between 30-33 (oversold) and 65-70 (overbought).
The tuned parameters were NOT applied back to the scanner code post-optimization.

### Recommendation
Create task: Apply tuned RSI thresholds (33/65) from optimization report to both
`technical_indicators.py` and `stock_opportunities.py`
