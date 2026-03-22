# TradeSight Phase 1 — Nightly Builder Pre-Flight
Date: 2026-03-20
Author: Lucky (task-runner, task 1055)

---

## TASK 886 — Dynamic Balance Sync: ❌ NO-GO

**Blocker:** portfolio_history table is MISSING  and  columns.

Schema check (src/data/positions.db):
- portfolio_history columns: id, timestamp, total_value, available_cash, total_positions_value, unrealized_pnl, realized_pnl, total_pnl, position_count, strategies_active
- NO buying_power ❌
- NO balance_synced_at ❌

Code status (paper_trader.py):
- _real_buying_power IS set in _alpaca_sync() (line 805) and used for position sizing (lines 344-347)
- BUT never persisted to any DB table

Builder must do BEFORE this task can run:
1. ALTER TABLE portfolio_history ADD COLUMN buying_power REAL DEFAULT 0.0
2. ALTER TABLE portfolio_history ADD COLUMN balance_synced_at TEXT
3. After each _alpaca_sync(), write _real_buying_power to latest portfolio_history row
4. Update get_portfolio_state() to use persisted buying_power (fallback to available_cash)

---

## TASK 891 — Confluence Strategy: ✅ GO

**All hard prerequisites present:**
- price_snapshots table: EXISTS in scanner.py SQLite (lines 62+, cols: market_id, timestamp, price_yes, price_no, volume_24h, best_bid, best_ask, spread) ✓
- technical_indicators.py: EXISTS at src/indicators/technical_indicators.py ✓
- score_market_opportunity(): EXISTS in scanner.py (line 161) — entry point for wiring ✓

**What's missing (builder work, not blockers):**
- get_price_history() helper: NOT in scanner.py yet (needs to be added)
- technical_indicators NOT imported in scanner.py
- detect_confluence() gate: NOT implemented

**Build order (from CONFLUENCE_BLOCKERS.md):**
1. Add get_price_history(market_id, n=30) to scanner.py — queries price_snapshots ordered by timestamp DESC, returns list
2. Import technical_indicators.py, wire into score_market_opportunity()
3. Add detect_confluence() gate before store_opportunity() call

Estimated effort: 3 focused functions, ~60 lines total. No schema migration needed.

---

## BONUS FINDINGS

**Task 887 (Trailing Stop) — schema already done:**
- positions table: high_water_mark REAL ✓ and trailing_stop_active INTEGER DEFAULT 0 ✓
- Builder can skip schema migration step, go straight to logic implementation

**Task 890 (Circuit Breaker) — schema NOT present:**
- circuit_breaker_state table: NOT FOUND in src/data/positions.db
- Builder must CREATE TABLE circuit_breaker_state before implementing logic

---

## Builder Priority Recommendation

| Task | Status | Builder Action |
|------|--------|---------------|
| 891 Confluence | ✅ GO | Start here — clean implementation |
| 887 Trailing Stop | ✅ GO | Schema done, just code logic |
| 890 Circuit Breaker | ✅ GO | New table + logic (~60 lines) |
| 886 Balance Sync | ❌ NO-GO | Schema migration first, then re-assess |
