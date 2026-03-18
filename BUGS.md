# TradeSight Paper Trader — Bug Report
**Audited:** 2026-03-17 by Lucky (task-runner)
**Source data:** positions.db, paper_trader_20260317.log, alpaca_client.py, paper_trader.py

---

## BUG-001: Stale current_price in positions.db
**Severity:** High
**Symptom:** ADBE shows entry_price=425.48 but current_price=258.0 — a $167 discrepancy. This is stale/incorrect pricing, not a real market move.
**Root cause:** `update_positions()` in position_manager.py only updates current_price when called with live `price_data` from Alpaca quotes. In DEMO mode (no Alpaca keys), the price fetch path is skipped entirely. The price update loop in paper_trader.py (~lines 463-475) only runs if `self.alpaca` has live connectivity — if that block fails silently, positions never get refreshed.
**Impact:** All unrealized P&L calculations are wrong. Dashboard shows -$21.15 for ADBE but real P&L is unknown.
**Fix needed:** Add fallback price fetching (even in demo mode) using the `_generate_demo_data` method or a separate price refresh cron. Or always call `update_positions()` regardless of Alpaca connectivity status.

---

## BUG-002: JPM Alpaca sync gap — Alpaca position not in local DB
**Severity:** High
**Symptom:** At 15:30 on Mar 17, Alpaca reports 3 open positions (ADBE qty=0.889885, COST qty=0.024093, JPM qty=0.842424) with $491.92 in positions. Local positions.db only has 2 (ADBE + COST). JPM is completely absent from local DB.
**Root cause:** `_sync_with_alpaca()` detects remote positions and logs them, but only warns if remote > 0 AND local == 0. It does NOT import remote positions into the local DB if local already has some positions. The orphan detection condition (`if remote_positions and local_state.position_count == 0`) never triggers for a partial mismatch (remote=3, local=2).
**Timeline:** 13:00 session showed Alpaca at $493.28 equity, 0 positions. By 15:30, Alpaca had 3 positions worth $491.92. JPM was opened between 13:00-15:30, placed on Alpaca but never recorded in local positions.db.
**Impact:** Local DB is out of sync with Alpaca reality. System thinks 2 positions are open; Alpaca has 3. Risk of double-buying JPM. Buying power miscalculated ($0.36 left on Alpaca, system may think more is available).
**Fix needed:** Extend `_sync_with_alpaca()` to import Alpaca positions missing from local DB. If `remote_symbols - local_symbols` is non-empty, insert those positions into positions.db with best-effort entry price from the quote.

---

## BUG-003: Zero closed trades — positions never exit
**Severity:** Medium
**Symptom:** trades.db has 0 records with exit_time set. All trades remain permanently open. The paper trader has never completed a full trade cycle since being stood up.
**Root cause:** Every session log shows "Maximum concurrent trades reached" immediately after portfolio check. The trade logic bails on entry evaluation but the exit evaluation path is either gated behind the same max-positions check or is simply not being reached. `scan_and_trade()` appears to short-circuit before evaluating open positions for exit signals.
**Impact:** No realized P&L ever gets logged. Performance tracking is broken. Strategies cannot learn from completed trades. Paper trader is effectively buy-and-hold forever.
**Fix needed:** Separate the exit evaluation loop from the entry evaluation loop in `scan_and_trade()`. Exiting open positions should always run regardless of max_concurrent_trades. Verify exit signal evaluation is not gated behind the entry guard.

---

## Summary Table
| Bug | Severity | Blocking? |
|-----|----------|-----------|
| BUG-001: Stale pricing in positions.db | High | No (cosmetic P&L error) |
| BUG-002: JPM Alpaca sync gap | High | Yes (incorrect state, risk of double-buy) |
| BUG-003: Zero closed trades | Medium | Yes (trading loop broken) |

**All three bugs need fixing before TradeSight is meaningfully testable as a paper trader.**
