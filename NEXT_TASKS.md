# TradeSight Improvement Queue
## Created: March 30, 2026 — from full audit session with Ray
## Status: Items 1-12 DONE (commit be28e6e). Items 13-30 queued.

## Priority Order (do these first)

### 13. Per-cluster optimization [HIGH]
- Clusters exist in `data/symbol_clusters.json` (ETFs, mega-tech, defensive, financials)
- Optimizer still runs one param set for all 20 stocks — make it optimize per cluster
- ETFs want tight stops, tech wants wide stops, defensives are in between
- File: `scripts/overnight_strategy_evolution.py`
- Effort: Medium | Impact: High

### 14. Multi-timeframe confirmation [HIGH]
- Dont enter on 1H RSI signal alone. Check if daily trend agrees
- 1H oversold in a daily downtrend = trap (GOOG lost 7% this way)
- Fetch both 1H and 1Day data in `generate_trading_signals()`
- Add daily SMA50 trend check: only buy if daily SMA50 is flat or rising
- Files: `src/trading/paper_trader.py`, `src/data/alpaca_client.py`
- Effort: Medium | Impact: High

### 15. Walk-forward optimization [HIGH]
- Current optimizer: single train/test split (70/30)
- Walk-forward: train months 1-6 test month 7, train 2-7 test 8, etc.
- Proves strategy works across multiple time windows, not just one lucky split
- File: `scripts/overnight_strategy_evolution.py`, `src/strategy_lab/backtest.py`
- Effort: Medium | Impact: High

### 16. Add more strategies [HIGH]
- Only 3 strategies now (RSI, MACD, Bollinger) — most common on the planet, zero edge
- Add: VWAP reversion (institutional favorite)
- Add: Opening range breakout (first 30 min high/low)
- Add: Sector rotation (XLK vs XLF vs XLE relative strength)
- Add: Mean reversion pairs (long one, short correlated)
- Files: `src/strategy_lab/backtest.py` (strategy functions), `src/trading/paper_trader.py` (signal generation)
- Effort: High | Impact: High

### 17. Market regime detection [HIGH]
- Trade differently in calm vs volatile markets
- Low VIX / low realized vol = mean reversion works. High VIX = trend following
- Measure: 20-day realized volatility of SPY, or fetch VIX directly
- Switch strategy weights based on regime
- Files: `src/trading/paper_trader.py`, new `src/indicators/regime_detector.py`
- Effort: Medium | Impact: High

## Medium Priority

### 18. Better data feed
- IEX is free tier, 15-min delayed. Switch to SIP if plan supports it
- Add Yahoo Finance as backup validation
- Hard-reject demo/synthetic data in paper trader (not just optimizer)
- Effort: Low | Impact: Medium

### 19. Pre-market gap detection
- Major moves happen before 9:30 AM
- Add 9:15 AM scan that checks for overnight gaps > 3%
- Gaps down: review stop losses. Gaps up: consider taking profit early
- Effort: Medium | Impact: Medium

### 20. Kelly criterion position sizing
- Instead of flat 35%, size by: f = (win_rate * avg_win - (1-win_rate) * avg_loss) / avg_win
- Calm stock (KO) = bigger position. Volatile stock (META) = smaller
- Needs enough trade history per symbol to calculate (use backtest results as prior)
- Effort: Medium | Impact: Medium

### 21. Performance dashboard
- Web page: win rate by strategy, win rate by symbol, equity curve, drawdown chart
- Average hold time, best/worst trades, Sharpe ratio over time
- Can reuse existing Flask app on port 5001
- Effort: Medium | Impact: Medium

### 22. Sector exposure limits
- Track total sector exposure across all open positions
- If >50% in one sector, block new entries in that sector
- Uses existing correlation_groups from config
- Effort: Low | Impact: Medium

### 23. Intraday 5-minute timeframes
- Current: 1H bars. 5-min bars catch faster mean reversions on mega-caps
- Requires more frequent scanning (every 5 min vs 15 min)
- May need plist update for scan frequency
- Effort: Medium | Impact: Medium

### 24. Earnings calendar filter
- Never enter position 3 days before earnings (its a coin flip)
- Free API: Alpha Vantage earnings calendar or scrape Yahoo Finance
- Check before every buy signal
- Effort: Low | Impact: Medium

## Lower Priority (Polish)

### 25. Trade journal with auto-notes
- Log WHY each trade was entered: "RSI 28 + high volume + daily uptrend"
- Log exit reason: "stop loss", "trailing stop", "take profit", "aged out"
- Over time shows which entry combinations work
- Effort: Low | Impact: Low-Medium

### 26. Slippage modeling in backtests
- Current backtester assumes perfect fills
- Add 0.05-0.1% slippage per trade for realistic results
- File: `src/strategy_lab/backtest.py` _execute_signal and _close_position
- Effort: Low | Impact: Low

### 27. Max drawdown circuit breaker
- If portfolio drops 15% from peak, stop ALL new trades for 48 hours
- Prevents compounding losses during bad streaks
- Check in scan_and_trade() before generating signals
- Effort: Low | Impact: Low-Medium

### 28. Alpaca WebSocket for real-time
- Instead of polling every 15 min, use WebSocket stream
- Stops trigger instantly instead of waiting for next scan
- Major architecture change — long-running process instead of cron
- Effort: High | Impact: Medium

### 29. Stress test against 2020-2022
- Backtest against March 2020 crash and 2022 bear market
- If strategy survives those, it survives most things
- Just need to extend historical data window to 4+ years
- Effort: Low | Impact: Low-Medium

### 30. Paper-to-live transition report
- When paper trading hits targets (60 days profitable, Sharpe > 1.5, win rate > 55%)
- Auto-generate "go live" report: risk assessment, recommended starting capital
- Gate: NEVER go live without Rays explicit approval
- Effort: Low | Impact: Low (for now)

## Done (March 30, 2026 — commit be28e6e)
- [x] 1. Stale position sync exit prices
- [x] 2. Per-trade feedback loop
- [x] 3. Champion backtest score
- [x] 4. Per-symbol OOS filtering
- [x] 5. ATR-based dynamic stops
- [x] 6. Volume scoring
- [x] 7. Correlation guard
- [x] 8. Symbol clusters
- [x] 9. Expanded optimizer grid
- [x] 10. Data source validation
- [x] 11. Performance report fix
- [x] 12. Backfill NULL exit prices
