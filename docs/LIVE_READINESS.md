# TradeSight — Live Trading Readiness Checklist

Use this checklist before transitioning from paper trading to live capital.
**All items must be ✅ before going live. Ray must sign off on the final review.**

---

## 1. Performance Benchmarks (30-Day Paper Trading)

- [ ] **30 consecutive days** of profitable paper trading completed
- [ ] **Win rate ≥ 45%** over the trailing 30 days
- [ ] **Profit factor ≥ 1.2** (gross profit / gross loss)
- [ ] **Max drawdown < 10%** over the 30-day window
- [ ] **Sharpe ratio ≥ 1.0** (annualized, risk-free = 0)
- [ ] **Average trade count ≥ 3/day** (system is generating signals consistently)

## 2. Risk Controls

- [ ] **Circuit breaker tested** — confirm daily loss limit halts all trading
- [ ] **Stop losses verified** with simulated fill slippage (0.05% + 0.02% spread)
- [ ] **Position sizing** caps enforced (max % NAV per trade, max open positions)
- [ ] **Sector correlation guard** active — no double-exposure to same sector
- [ ] **Regime detection** working — strategy selection adjusts to TRENDING vs RANGING

## 3. Trading Windows

- [ ] Signals confirmed to skip first 30 min (9:30–10:00 ET)
- [ ] Signals confirmed to skip lunch window (11:30–1:00 ET)
- [ ] Active windows verified: 10:00–11:30 and 2:00–3:30 ET

## 4. Data & Execution Quality

- [ ] **Slippage model validated** — backtest results include 0.05% slippage + 0.02% spread
- [ ] **Monte Carlo simulation passing** — shuffles trade outcomes (not price bars)
- [ ] **No 0-trade sessions logging 0% PnL** to feedback DB (noise contamination fixed)
- [ ] Data feed latency acceptable (< 5s for quotes)
- [ ] No known data quality issues (gaps, stale prices, bad ticks)

## 5. Broker & Infrastructure

- [ ] Broker API credentials configured and tested
- [ ] Order submission tested (paper orders routed correctly)
- [ ] Rate limits understood and respected
- [ ] Fallback / kill switch documented and tested
- [ ] Logging to disk working (every trade, every signal, every error)

## 6. Monitoring

- [ ] Alerts set for: drawdown threshold, circuit breaker trigger, API errors
- [ ] Dashboard (or log tail) accessible during market hours
- [ ] Emergency shutdown procedure documented

## 7. Ray's Sign-Off

- [ ] Ray has reviewed performance logs and agrees metrics are solid
- [ ] Ray has set initial live capital limit (start small: 00–000 max)
- [ ] Ray has confirmed go-live date

---

## Slippage Model (Pending — BUILDER Task)

> ⚠️ **Not yet implemented.** Backtest currently assumes perfect fills.
> Task: Add configurable slippage (0.05%) and bid-ask spread (0.02%) to .
> Until this is done, live performance will likely underperform backtests.

---

## Notes

- Phase 1 exits + Phase 2 Confluence strategy must be shipping and stable before this checklist matters
- Circuit breaker = hard daily loss cap (e.g., -2% NAV stops all trading for the day)
- Start live with minimum position sizes. Scale only after 30 more live days of profitability.

---

*Last updated: 2026-03-17*
