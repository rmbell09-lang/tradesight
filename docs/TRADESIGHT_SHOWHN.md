# TradeSight — Show HN Post

*Written by Lucky — March 16, 2026*

---

## TITLE

```
Show HN: I built a self-hosted AI that evolves trading strategies overnight
```

## BODY

```
I got tired of paying $200/month for trading signal services that were basically just "RSI crossed 30." So I built TradeSight — a self-hosted system that runs tournament-style strategy evolution while I sleep.

Here's how it works:

1. You define a base strategy (e.g., RSI Mean Reversion)
2. TradeSight generates 100+ parameter variations
3. Each variation is backtested against historical data
4. Losers get eliminated, winners "breed" new variations
5. You wake up to the champion with full stats (win rate, Sharpe ratio, P&L)

The best RSI Mean Reversion strategy it's evolved so far: 89.4% P&L with a 2.53 Sharpe ratio. Not a live trading guarantee, but much better than manual chart-staring.

**What's included:**
- Full Python source (MIT license)
- Web dashboard at localhost:5001 (markets, tournaments, paper trades)
- Polymarket prediction markets + US equities scanner
- 15+ technical indicators applied automatically
- Paper trading via Alpaca's free API (fake money, real P&L tracking)
- Overnight automation via cron
- 94 tests passing
- Demo mode — runs without any API keys

**Stack:** Python, Flask, SQLite. No Docker, no Redis, no cloud. Runs fine on a Mac Mini.

I'm selling it as a one-time purchase ($49) because I hate subscriptions. Currently at v1; v2 adds crypto scanning and push alerts — early buyers get it free.

Happy to answer questions about the strategy evolution algorithm, the backtest methodology, or anything else.

GitHub: [not yet public — distributed as a zip via Gumroad]
Gumroad: https://qcautonomous.gumroad.com/l/zpkutz
```

---

## POSTING CHECKLIST

- [ ] Title must start with "Show HN:" — no variations
- [ ] HN URL: https://news.ycombinator.com/submit
- [ ] Post Monday–Thursday, 9–11 AM ET (peak US traffic)
- [ ] Must link to a live URL — Gumroad listing counts
- [ ] Respond to ALL early comments within first 2 hours (critical for ranking)
- [ ] Do NOT upvote your own post or ask others to
- [ ] Do NOT post the same URL twice — HN flags duplicate domains

## RESPONSE PLAYBOOK

**"Why not open source it?"**
> MIT license on the source — it IS open source. Gumroad is just the distribution channel. $49 saves you the time to wire it all together.

**"Can't you just use QuantConnect / Backtrader / etc?"**
> Sure. TradeSight wraps the evolutionary tournament logic + paper trading + dashboard in a ready-to-run package. It's an appliance, not a framework.

**"89.4% P&L sounds too good to be true"**
> Backtesting numbers always look great — that's survivorship bias baked into the methodology. The value is the tournament system finding *relatively* better strategies, not the absolute number. Paper trade it before going live.

**"What data source do you use?"**
> yfinance for historical equities data, Polymarket's public API for prediction markets. Free and public.

**"Is this legal?"**
> Fully. Automated trading is legal for retail investors. Paper trading is just simulation. Always check your broker's ToS before going live.

---

## NOTES

- Show HN posts live or die in the first 2 hours — monitor and respond fast
- Technical audience: lead with the algorithm, not the product
- Don't oversell the P&L — HN readers will crucify you for it
- Acknowledge limitations upfront (backtesting ≠ live trading)
- The "self-hosted, you own the code" angle plays well on HN
