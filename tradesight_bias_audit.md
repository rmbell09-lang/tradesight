# TradeSight Backtesting — Lookahead Bias Audit

Date: 2026-03-14
Files audited: src/strategy_lab/backtest.py, src/strategy_lab/backtester.py

---

## Summary

Cross-validation PnL is unrealistic due to 4 confirmed issues, ranging from a critical entry-timing bias to flawed Monte Carlo methodology.

---

## Issue 1 — CRITICAL: Signal-at-Close / Entry-at-Close Bias

Location: BacktestEngine.run_backtest() -> _execute_signal() -> _open_position()

Problem:
The strategy function is called with data.iloc[i] — which INCLUDES bar i's closing price — and the entry is immediately filled at that same bar's close. This means the backtest "knows" the close of bar i before trading it.

In reality: if bar i's close triggers a signal, the earliest you can trade is bar i+1's open.

Effect: Inflates PnL. Strategy gets filled at the exact candle that caused the signal — often the highest point of a bullish bar or lowest of a bearish bar. This alone can explain unrealistically high PnL figures.

Fix: Delay entry by one bar — use data.iloc[i+1]["open"] as entry price, or generate the signal at bar i but execute on bar i+1.

---

## Issue 2 — HIGH: Monte Carlo Shuffles Bars Instead of Trades

Location: MultiAssetBacktester.monte_carlo_simulation()

Problem:
The MC simulation randomly shuffles OHLCV bars (data.sample(frac=1)), then restores the original timestamps. This destroys the time-series structure:
- Rolling indicators (SMA, RSI, MACD, BB) computed on shuffled prices produce garbage values
- The strategy is effectively tested on random noise, not alternative market paths
- Results from this MC are meaningless for robustness assessment

Correct approach: Run the backtest ONCE, collect the ordered list of trade P&Ls, then bootstrap THOSE returns:
  - Sample trade_pnls with replacement N times
  - Compute cumulative equity and drawdowns per simulation
  - This tests whether the strategy is robust to different trade sequences

---

## Issue 3 — MEDIUM: No Indicator Warm-Up at Walk-Forward Fold Boundaries

Location: MultiAssetBacktester.walk_forward_validation() + BacktestEngine._add_indicators()

Problem:
Each fold's test slice is passed to run_backtest() independently, which calls _add_indicators() on ONLY that slice. So SMA_20 at the first bar of the test fold is computed from the first 20 bars of the test slice — completely ignoring the training history.

Result: All indicators are wrong/warm-up artifacts for the first ~50 bars of every test fold. Walk-forward test scores are computed with corrupted indicator state at every fold boundary.

Fix: Compute indicators on the full fold data, then slice only the execution loop to the test period.

---

## Issue 4 — LOW-MEDIUM: Stop/TP Checked Against Close, Not Intrabar High/Low

Location: BacktestEngine._update_positions()

Problem:
Stop-loss and take-profit triggers are only evaluated against bar["close"]. In reality, price moves through SL/TP levels intrabar. Using close means:
- A stop that was hit on a wick but closed above it is never triggered (overestimates PnL)
- A take profit hit mid-bar that reversed is credited as NOT triggered (underestimates wins)

The net effect on PnL depends on the strategy, but it's a systematic distortion of realistic results.

Fix: Check SL against bar["low"] (for longs) / bar["high"] (for shorts), and TP vice versa.

---

## Issue 5 — LOW: Walk-Forward Uses Independent Folds, Not Expanding Window

Location: MultiAssetBacktester.walk_forward_validation()

Problem:
Current implementation creates N equally-sized non-overlapping folds (fold 0 = bars 0–200, fold 1 = bars 200–400, etc.). Each fold's training set is the same size. Classic walk-forward uses an expanding training window so the model sees more history with each fold, which is how real optimization and live deployment works.

Fix: Expand the training window with each fold — train on all data up to test_start, test on the next window of bars.

---

## Priority Order for Fixes

1. CRITICAL — Entry-at-close bias (effort: low, 1-line fix in _open_position)
2. HIGH — MC boots bars not trades (effort: medium, rewrite monte_carlo_simulation)
3. MEDIUM — Indicator warm-up at fold boundaries (effort: medium)
4. MEDIUM — SL/TP intrabar check (effort: low, swap close for high/low)
5. LOW — Expanding walk-forward window (effort: medium)

Fixing issues 1 + 4 alone will likely bring PnL figures back to realistic levels. Issue 2 will make the MC actually informative.
