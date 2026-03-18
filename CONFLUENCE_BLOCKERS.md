# TradeSight Confluence Strategy — Blocker Analysis
Generated: 2026-03-18 by Jinx (task-1773848588178)

## Context
scanner.py currently only detects arbitrage + high-opportunity scores via price/volume/spread.
No multi-indicator logic exists. These are the 3 code gaps blocking Confluence Strategy (RSI + MACD + Bollinger).

---

## Blocker 1: No Indicator Integration in scanner.py
**File:**  (entire file — no indicator imports)
**Issue:** Scanner has zero integration with . It computes everything inline from raw price data. No RSI, MACD, or Bollinger Band values are ever calculated or stored.
**Fix:** Add import + calls to  in  and  — pass price history to get RSI/MACD/BB signals before scoring.

---

## Blocker 2: No Multi-Signal Confluence Logic
**File:**  —  (lines ~170-200) and  (lines ~240-290)
**Issue:** Opportunity scoring is additive (volume + liquidity + price balance + spread). No mechanism to require ALL indicators to confirm before firing an alert — which is the core of a Confluence Strategy.
**Fix:** Add a  method that takes computed RSI/MACD/BB values and returns True only when all three confirm (e.g., RSI > 50, MACD bullish crossover, price above BB midline).

---

## Blocker 3: No Historical Price Time Series Storage
**File:**  —  (lines ~33-80),  (lines ~215-240)
**Issue:**  table stores single-point snapshots, but RSI/MACD/BB require N periods of history (typically 14 for RSI, 26 for MACD slow). The schema doesn't order-sort snapshots per market for window calculations.
**Fix:** Add a query helper  that pulls last N price_snapshots ordered by timestamp — pass this array to technical_indicators.py before scoring.

---

## Recommended Build Order (for nightly builder)
1. Implement  (unblocks everything else)
2. Wire  into 
3. Add  gate before 

## Related Tasks in MC Backlog
- #891: [BUILDER] TradeSight: Confluence Strategy (PRIMARY)
- #887: [BUILDER] TradeSight Phase 1: Trailing Stops  
- #888: [BUILDER] TradeSight Phase 1: Order Fill Verification
