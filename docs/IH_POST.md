# TradeSight — IndieHackers Post

## Title
I built an AI that evolves trading strategies overnight while I sleep

## Body

Hey IH 👋

I'm a solo dev who got tired of paying $200/month for trading signals that were basically "RSI crossed 30, buy now." So I built my own system.

**TradeSight** runs tournament-style strategy evolution overnight. It takes a base trading strategy, generates hundreds of variations, backtests them against real market data, kills the losers, and promotes the winners. I wake up to the best performer with actual P&L numbers.

### How it works

1. You define a base strategy (e.g., RSI Mean Reversion)
2. TradeSight generates 100+ variations (different thresholds, timeouts, position sizes)
3. Each variation is backtested against historical data
4. Losers get eliminated, winners breed new variations
5. Morning report shows you the champion with full stats

The RSI Mean Reversion strategy that came out of this process hit 89.4% P&L with a 2.53 Sharpe ratio in backtesting. Not a guarantee of future returns, but way better than manual chart-staring.

### What's in the box

- Full Python source (MIT license)
- Web dashboard at localhost:5001
- Polymarket + US equities scanner
- 15+ technical indicators applied automatically
- Paper trading via Alpaca (free account)
- Overnight automation via cron
- 94 tests passing
- Demo mode — works without any API keys

### The stack

Python, Flask, SQLite. No Docker, no Redis, no cloud dependencies. Runs on a Mac Mini.

### Revenue model

One-time purchase at $49. No subscriptions. Currently raising to $79 when v2 ships (crypto scanning + push alerts). Early buyers get v2 free.

### What I learned building this

- Strategy evolution is just genetic algorithms applied to trading parameters. The concept isn't new, but packaging it for solo traders is.
- Paper trading is non-negotiable for trust. Nobody should go live without proving the strategy works with fake money first.
- Self-hosted matters for trading tools. Your edge disappears the moment it's on someone else's server.

Would love feedback from anyone who's built trading tools or used algorithmic strategies. What would you want to see in v2?

**Link:** https://qcautonomous.gumroad.com/l/zpkutz

