---
title: I Ran a 4-Strategy AI Trading Tournament in Paper Trading — Here's Who Won
tags: python,trading,algorithmic,ai
---

## The Tournament Idea

Every algo trader has opinions about which strategy is better. RSI mean reversion? MACD crossover? Momentum? Everyone argues. Nobody runs the experiment.

So I set up a controlled paper trading tournament inside TradeSight: four strategies, same universe of stocks, same starting capital ($500 each), running in parallel for 30 days. No cherry-picking. No curve-fitting. Just run them and see.

Here's what happened.

---

## The Competitors

**Strategy 1: RSI Mean Reversion**
Buy when RSI < 30 (oversold). Sell when RSI > 70 (overbought) or after 5 days.
Classic momentum fade. Works in range-bound markets. Gets destroyed in trends.

**Strategy 2: MACD Crossover**
Buy when MACD line crosses above signal line. Sell on reverse cross or 10% trailing stop.
Trend-following. Slower to enter, slower to exit. Less noise, more whipsaw.

**Strategy 3: Bollinger Band Squeeze**
Buy when price crosses above lower band after a squeeze (bands tighten). Exit on upper band touch.
Volatility-based. Works well after consolidation periods.

**Strategy 4: AI Confluence (my original)**
Requires 2 of 3 signals aligned: RSI < 35, MACD bullish, price above 20-day SMA. Exits on any signal flipping.
More selective. Should have higher win rate, lower trade count.

---

## Setting Up the Tournament

The core is a shared paper trading engine with isolated strategy contexts:

```python
from tradesight.paper_trader import PaperTrader
from tradesight.strategies import RSIMeanReversion, MACDCrossover, BollingerSqueeze, AIConfluence

UNIVERSE = ["AAPL", "GOOG", "MSFT", "AMZN", "TSLA", "NVDA", "META", "V", "JPM", "ADBE"]
STARTING_CAPITAL = 500.0

tournament = {
    "rsi": PaperTrader(RSIMeanReversion(), capital=STARTING_CAPITAL),
    "macd": PaperTrader(MACDCrossover(), capital=STARTING_CAPITAL),
    "bollinger": PaperTrader(BollingerSqueeze(), capital=STARTING_CAPITAL),
    "confluence": PaperTrader(AIConfluence(), capital=STARTING_CAPITAL),
}

# Each market day, run signals for each strategy
for symbol in UNIVERSE:
    data = fetch_daily(symbol)
    for name, trader in tournament.items():
        trader.evaluate(symbol, data)
```

One critical rule: **no look-ahead bias**. Each strategy only sees data up to the current simulated day. This is the most common mistake in backtesting — and why most backtests look better than live trading.

---

## The Results (30 Days)

| Strategy | Trades | Win Rate | Total P&L | Final Value |
|----------|--------|----------|-----------|-------------|
| RSI Mean Reversion | 9 | 33% | -$146 | $354 |
| MACD Crossover | 4 | 50% | -$32 | $468 |
| Bollinger Squeeze | 6 | 50% | +$14 | $514 |
| AI Confluence | 3 | 67% | +$28 | $528 |

**Winner: AI Confluence** — but barely, and with only 3 trades.

---

## What the Tournament Taught Me

**1. Trade count matters.** RSI took 9 trades, lost on 6 of them, and compounded the losses. MACD's lower trade count reduced drawdown exposure.

**2. Win rate without position sizing is meaningless.** A 67% win rate with 3 trades is statistically weak. I need 20+ trades before trusting a win rate.

**3. Market regime kills everything.** March 2026 was a high-volatility trending month. RSI mean reversion was built for range-bound conditions. It got crushed.

**4. The "best" strategy changes every month.** Bollinger Squeeze was flat or negative in trending months. It would dominate in a consolidating market.

---

## The Honest Takeaway

Nobody wins a trading tournament over 30 days. That's not enough time. But the tournament structure itself is valuable — it forces you to run strategies in parallel without retrofitting results.

The goal isn't to find the "best" strategy. It's to understand *when* each strategy works and build a rotation system around market regimes.

That's what I'm building into TradeSight next: a regime detector that routes signals to the right strategy based on current volatility and trend strength. Tournament results become training data for the router.

---

→ [TradeSight on GitHub](https://github.com/luckyai/tradesight) — open source algo trading framework with paper trading, backtesting, and strategy tournaments built in.
