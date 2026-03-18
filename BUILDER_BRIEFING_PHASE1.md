# TradeSight Phase 1 Builder Briefing
Audit Date: 2026-03-17 17:11 ET
Author: Lucky (task-runner audit)
Purpose: Gap analysis for nightly builder — current paper_trader.py vs Phase 1 requirements

---

## Summary

Phase 1 = Exit Logic. All 4 tasks are MISSING or incomplete. The paper trader currently:
- Watches positions drop with no stop loss
- Never trails winners
- Accepts unconfirmed 'accepted' orders as fills
- Has zero safety limits on drawdown

These are the highest-priority gaps (NEXT_TASKS.md Priority 9-10).

---

## Task 1.1: Active Stop Loss + Take Profit — MISSING (0%)

What should exist: _check_stop_loss_take_profit() called at START of every scan_and_trade().

What actually exists:
- position_manager.py line 85-86: stop_loss_percent=0.05, take_profit_percent=0.06 defined but NEVER READ
- Champion params include SL=5pct, TP=6pct — loaded into self.active_params at init but paper_trader never uses them
- scan_and_trade() has NO call to any stop/TP check before generating new signals

Files to modify:
- src/trading/paper_trader.py — add _check_stop_loss_take_profit(), call at top of scan_and_trade()
- Use self.alpaca.get_quote(symbol) (already available) + self.active_params (already loaded)

---

## Task 1.2: Trailing Stop — MISSING (0%)

What should exist: Per-position high-water mark tracking + trailing stop logic.

What actually exists:
- positions table schema: NO high_water_mark column. No trailing logic anywhere.
- position_manager.py update_positions(price_data) updates current_price + unrealized_pnl only

Files to modify:
- src/trading/position_manager.py — add high_water_mark column migration, update HWM on price updates
- src/trading/paper_trader.py — add trailing stop check after SL/TP check

DB migration needed: ALTER TABLE positions ADD COLUMN high_water_mark REAL DEFAULT 0.0;

---

## Task 1.3: Order Fill Verification — PARTIAL (30%)

What should exist: Poll for fill confirmation after accepted status; cancel after 30s.

What actually exists:
- _execute_buy_order() line 381: accepts status in [filled, accepted] — proceeds immediately on accepted
- _execute_sell_order() line 427: same issue
- fill_price = order_result.get(fill_price) or price — uses signal price as fallback if None

Files to modify:
- src/trading/paper_trader.py — both _execute_buy_order() and _execute_sell_order()
- Need: poll self.alpaca.get_order(order_id) with 5s intervals up to 30s
- Check: src/data/alpaca_client.py — verify if get_order/cancel_order methods exist

---

## Task 1.4: Circuit Breaker / Kill Switch — MISSING (0%)

What should exist: _check_circuit_breaker() with SQLite-persisted state.

What actually exists: Zero. No daily loss tracking, no consecutive loss counter, no kill switch.

Files to modify:
- src/trading/paper_trader.py — add _check_circuit_breaker(), call before scan_and_trade() proceeds

New SQLite table needed (circuit_breaker_state):
  - date TEXT
  - daily_loss_pct REAL DEFAULT 0.0
  - weekly_loss_pct REAL DEFAULT 0.0
  - consecutive_losses INTEGER DEFAULT 0
  - position_size_multiplier REAL DEFAULT 1.0
  - trading_halted INTEGER DEFAULT 0
  - halt_reason TEXT
  - updated_at TEXT

Logic:
1. Daily loss > 2pct equity -> trading_halted=1, reason=daily_loss_limit
2. Weekly loss > 5pct equity -> halt for week
3. 3 consecutive losses -> position_size_multiplier=0.5 (not full halt)
4. Single trade loss > 3pct -> WARNING log only
5. Check runs at START of each session, before SL/TP, before signals

---

## Execution Order for Builder

1. Task 1.1 (SL/TP) — highest impact, simplest, no schema changes
2. Task 1.4 (Circuit Breaker) — independent, new DB table
3. Task 1.2 (Trailing Stop) — needs positions schema migration + HWM
4. Task 1.3 (Fill Verification) — check AlpacaClient API first

---

## Key Files Reference

- src/trading/paper_trader.py    — Main orchestrator (all 4 tasks)
- src/trading/position_manager.py — Position tracking (Task 1.2 schema+HWM)
- src/data/alpaca_client.py       — Alpaca API wrapper (Task 1.3 get_order/cancel)

---

## Current Working State (do NOT break)

- _sync_with_alpaca() — works
- active_params loading from ChampionTracker — works (oversold=40, overbought=65, SL=5pct, TP=6pct)
- Orphan position guard — works
- RSI Mean Reversion + MACD Crossover signal generation — works
- scan_and_trade() base flow — works (SL/TP + circuit breaker need to be inserted at top)
