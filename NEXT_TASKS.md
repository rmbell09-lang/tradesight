# TradeSight Build Queue — March 17, 2026 (v2)

## Strategy Decision (Ray approved)
**Two-track system, not four separate strategies:**
1. **Confluence** — primary strategy. Combines all 7 indicators (RSI, MACD, Bollinger, VWAP,
   SuperTrend, Ichimoku, Volume) into one score. Why use ingredients separately when you
   have the recipe?
2. **RSI Mean Reversion** — contrarian secondary. Catches reversals that Confluence misses
   because Confluence requires indicator agreement, but the best mean reversion trades
   happen when most indicators look bearish.

Standalone MACD and Bollinger are dropped from live trading (already baked into Confluence).
They remain in the tournament system for benchmarking.

**Priority order: Exits > Entries.** Stop loss + trailing stop implementation matters more
than any strategy improvement. A mediocre strategy with great exits beats a great strategy
with no exits.

## Current State
- Account: ~$493 Alpaca paper (ADBE + JPM positions from orphan bug)
- First paper trade: ADBE 0.126 shares @ $425.48 (RSI Mean Reversion)
- Orphan guard deployed. Alpaca balance sync working.
- Champion params: oversold=40, overbought=65, position_size=0.8, SL=5%, TP=6%
- Confluence score engine EXISTS in technical_indicators.py but unused

---

## Phase 1: EXIT LOGIC — The Real Money [BUILDER]
These are worth 50%+ of the difference between profit and loss.

### Task 1.1: Active Stop Loss + Take Profit Execution
**Priority:** 10 | **Complexity:** MEDIUM
**Files:** src/trading/paper_trader.py, src/trading/position_manager.py

Currently stop_loss_pct (5%) and take_profit_pct (6%) are in config but NEVER CHECKED.
A stock could drop 20% and the app just watches.

Add _check_stop_loss_take_profit() method to paper_trader.py:
1. Called at START of every scan_and_trade() before generating new signals
2. For each open position (from BOTH local DB AND Alpaca):
   - Get current price via self.alpaca.get_quote(symbol)
   - Calculate PnL pct from entry price
   - If loss >= stop_loss_pct then close position immediately
   - If gain >= take_profit_pct then close position immediately
3. Log every stop/TP trigger with entry price, exit price, PnL
4. Use champion params stop_loss_pct and take_profit_pct (not hardcoded)

**Verify:** Open a position, manually check that SL/TP thresholds trigger closes.

### Task 1.2: Trailing Stop Implementation
**Priority:** 10 | **Complexity:** MEDIUM
**Files:** src/trading/position_manager.py, src/trading/paper_trader.py

Fixed take profit (6%) caps winners. Trailing stop lets winners run.
1. Add trailing_stop_pct to champion params (default 3%)
2. Track high_water_mark for each position in positions DB (new column)
3. On each price update, if current_price > high_water_mark then update HWM
4. If current_price drops trailing_stop_pct below HWM then close position
5. Trailing stop only activates AFTER position is up >= 2% (dont trail from entry)
6. When trailing stop is active, it REPLACES the fixed take profit
   (let the winner run, trail protects the gain)

**Verify:** Simulate a position that goes up 8% then drops 3% — should close at +5%.

### Task 1.3: Order Fill Verification
**Priority:** 9 | **Complexity:** LOW
**File:** src/trading/paper_trader.py

_execute_buy_order accepts status "accepted" but that doesnt mean filled.
1. After placing order, if status is "accepted" (not "filled"):
   - Wait 5 seconds, poll GET /v2/orders/order_id for actual fill status
   - Only record position if status becomes "filled"
   - If still not filled after 30s, cancel the order
2. Ensure fill_price is NEVER None when recording position
3. Log order lifecycle: placed then accepted then filled/cancelled

**Verify:** Check that positions are only recorded after confirmed fill.

### Task 1.4: Circuit Breaker / Kill Switch
**Priority:** 9 | **Complexity:** MEDIUM
**Files:** src/trading/paper_trader.py (new method _check_circuit_breaker)

Hard safety limits:
1. Daily loss > 2% of equity then stop trading for the day
2. Weekly loss > 5% then stop trading for the week
3. 3 consecutive losses then reduce position size by 50% until a win
4. Single trade loss > 3% then log WARNING (review signal quality)
5. Store circuit breaker state in SQLite (survives restarts)
6. Log all breaker trips with timestamp and reason

**Verify:** Simulate losses exceeding thresholds, confirm trading halts.

---

## Phase 2: ENTRY LOGIC — Better Signals [BUILDER]

### Task 2.1: Dynamic Balance Sync (not hardcoded $500)
**Priority:** 9 | **Complexity:** MEDIUM
**File:** src/trading/position_manager.py

Instead of initial_balance: 500.0 hardcoded, the position manager should:
1. Accept initial_balance as a constructor parameter
2. PaperTrader.__init__ should pass the real Alpaca equity from _sync_with_alpaca
3. All position sizing math uses real balance, not hardcoded
4. Strategy allocation adjusts to account size (bigger account = more diversification)
5. If Alpaca sync fails, fall back to config value with WARNING log

**Verify:** Change Alpaca paper balance, confirm position sizing adjusts automatically.

### Task 2.2: Confluence Strategy (PRIMARY — the big one)
**Priority:** 9 | **Complexity:** HIGH
**Files:** src/trading/paper_trader.py, src/strategy_lab/tournament.py

The technical indicators engine already computes a confluence score (-1.0 to +1.0) combining
RSI + MACD + Bollinger + VWAP + SuperTrend + Ichimoku + Volume. This becomes our primary
strategy.

Build Confluence Score strategy:
1. Add to _apply_strategy_logic:
   - Buy when confluence_score > +0.5 AND volume relative > 1.2 (confirms conviction)
   - Sell/close when confluence_score < -0.3 OR stop loss/trailing stop hit
   - Confidence = abs(confluence_score) (natural 0-1 range)
2. Register in get_builtin_strategies() for tournament use
3. Runs alongside RSI Mean Reversion (2-track system)

Two-track allocation:
- Confluence: up to 50% of portfolio (primary)
- RSI Mean Reversion: up to 30% of portfolio (contrarian)
- Max 3 concurrent positions total
- Same stock cant be held by both strategies

**Verify:**
- Run tournament: Confluence vs RSI vs MACD vs Bollinger
- Confirm Confluence generates different signals than individual indicators
- Check that two-track positions dont exceed limits

### Task 2.3: Remove Short Selling
**Priority:** 8 | **Complexity:** LOW
**File:** src/trading/paper_trader.py

On a small account, shorting is reckless (unlimited loss, margin).
1. In execute_signal(): if action == sell and no existing position then skip
2. Only allow sells that CLOSE existing long positions
3. Log skipped short signals for analysis
4. Add config flag allow_shorting: False (enable later for larger accounts)

**Verify:** Confirm bearish signals are logged but not executed as shorts.

### Task 2.4: Time-of-Day Filter
**Priority:** 7 | **Complexity:** LOW
**File:** src/trading/paper_trader.py

Add time awareness to signal generation:
1. Skip signals during first 30 min (9:30-10:00 ET) — too much noise
2. Skip signals during lunch (11:30-1:00 ET) — choppy
3. Best windows: 10:00-11:30 and 2:00-3:30 ET
4. Make configurable via trading_windows in config
5. Log skipped signals with "outside trading window" reason

**Verify:** Confirm signals only fire during approved windows.

### Task 2.5: Regime Detection
**Priority:** 7 | **Complexity:** MEDIUM
**Files:** src/indicators/technical_indicators.py, src/trading/paper_trader.py

Add market regime classification:
1. Calculate ADX (trend strength) — already have talib
2. Classify regime:
   - ADX > 25 + price > SMA50 = TRENDING_UP: favor Confluence
   - ADX > 25 + price < SMA50 = TRENDING_DOWN: favor cash (no shorts)
   - ADX < 20 = RANGING: favor RSI Mean Reversion
3. Active strategy selection adjusts based on regime
4. Log regime at start of each scan

**Verify:** Check regime classification across different market conditions in backtest.

---

## Phase 3: DATA QUALITY

### Task 3.1: Fix Monte Carlo Simulation
**Priority:** 6 | **Complexity:** MEDIUM
**File:** src/strategy_lab/backtester.py

Current Monte Carlo shuffles OHLCV bars randomly — destroys temporal structure.
Fix: shuffle TRADE OUTCOMES, not price bars.
1. Run strategy normally to get trade list
2. Randomly resample trade PnL values (with replacement)
3. Build equity curves from resampled trades
4. Calculate distribution of outcomes

### Task 3.2: Skip 0-Trade Feedback Logging
**Priority:** 6 | **Complexity:** LOW
**File:** src/trading/paper_trader.py

Sessions with 0 trades should NOT log 0% PnL to feedback DB.
Check trades_opened > 0 before logging.

### Task 3.3: Sector Correlation Guard
**Priority:** 5 | **Complexity:** MEDIUM
**File:** src/trading/paper_trader.py

Prevent double-exposure: define sector map, block same-sector positions.

### Task 3.4: Slippage + Spread Modeling
**Priority:** 5 | **Complexity:** MEDIUM
**File:** src/strategy_lab/backtest.py
Add configurable slippage (0.05%) and bid-ask spread (0.02%) to backtests.

---

## Phase 4: LIVE-READINESS

### Task 4.1: Paper-to-Live Transition Checklist
**Priority:** 5 | **Complexity:** LOW
Create docs/LIVE_READINESS.md:
- 30 days profitable paper trading
- Circuit breaker tested
- Stop loss verified with real fills
- Max drawdown < 10% over 30 days
- Win rate > 45%, profit factor > 1.2
- Ray reviewed and approved

---

## Execution Notes
- Phase 1 (exits) is worth more than everything else combined — do first
- Phase 2 Task 2.2 (Confluence) is the highest-value strategy change
- Nightly builder picks up [BUILDER] tasks
- Every task: code change + test + git commit + log to MC
- MACD and Bollinger remain in tournament for benchmarking but not live trading
