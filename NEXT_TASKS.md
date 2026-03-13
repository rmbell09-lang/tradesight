# TradeSight Phase 3 + 4 Build Queue

**Project:** TradeSight Trading Intelligence Platform  
**Goal:** Build AI Strategy Lab (Phase 3) + Stock Scanner (Phase 4)  
**Network Requirements:** Stock market APIs (Ray will allowlist via LuLu)

## Priority 1: Phase 3 - AI Strategy Lab (Core Engine)

- [x] **AI Strategy Iteration Engine** - Core backtesting + AI improvement loop
  - Files: `src/strategy_lab/ai_engine.py`, `src/strategy_lab/backtest.py`
  - Spec: Implement Michael Automates workflow - base strategy → AI iterate → multi-asset validation
  - Done when: Can take a simple strategy, run 5+ improvement iterations, validate across 3+ assets

- [x] **Strategy Tournament System** - Evolution approach from Alex Carter research
  - Files: `src/strategy_lab/tournament.py`
  - Spec: Run 10 strategies parallel, kill losers, evolve winners over 2-3 rounds
  - Done when: Tournament can run with mock strategies and report winners

- [x] **Multi-Asset Backtesting** - Anti-overfitting framework
  - Files: `src/strategy_lab/backtester.py`, `src/data/historical.py`
  - Spec: Walk-forward validation, Monte Carlo simulation, cross-asset verification
  - Done when: Backtest results include overfitting detection and bias warnings

## Priority 2: Phase 4 - Stock Scanner (Market Data)

- [x] **Alpaca Integration** - Stock market data + paper trading API
  - Files: `src/data/alpaca_client.py`, `src/scanners/stock_scanner.py`
  - Spec: Real-time + historical stock data, technical indicators, paper trading setup
  - Network: `api.alpaca.markets`, `data.alpaca.markets`
  - Done when: Can fetch S&P 500 data with OHLCV + indicators, paper trading account connected

- [x] **Technical Indicators Engine** - RSI, MACD, Bollinger, MAs, Volume analysis
  - Files: `src/indicators/`, `src/scanners/technical_analysis.py`
  - Spec: All major indicators from trading skill research (25KB knowledge base)
  - Done when: Signal generator produces confluence scores for any stock with 3+ indicators

- [x] **Stock Opportunity Scorer** - Multi-factor scoring like Polymarket scanner
  - Files: `src/scanners/stock_opportunities.py`
  - Spec: Volume, volatility, technical signals, earnings proximity, sector strength
  - Done when: Produces ranked opportunity list for S&P 500 with confidence scores

## Priority 3: Integration & Web Interface

- [x] **Unified Dashboard** - Extend current Polymarket dashboard for all markets
  - Files: `web/dashboard.py`, `web/templates/`
  - Spec: Tabs for Polymarket, Stocks, Strategy Lab with live data
  - Done when: Single dashboard shows opportunities across all market types

- [x] **Strategy Lab Web UI** - Interactive backtesting and tournament management
  - Files: `web/strategy_lab.py`, `web/templates/strategy_lab.html`
  - Spec: Start tournaments, view AI iteration progress, export winning strategies
  - Done when: Can start/stop tournaments and view results via web interface

## Priority 4: Automation & Production

- [x] **Automated Strategy Development** - Overnight AI improvement cron jobs
  - Files: `scripts/nightly_strategy_improvement.sh`, `src/automation/`
  - Spec: Run strategy tournaments overnight, report best performers each morning
  - Done when: Cron job runs 8-hour improvement cycles and emails/logs results

- [x] **Paper Trading Orchestrator** - Execute best strategies with fake money
  - Files: `src/trading/paper_trader.py`, `src/trading/position_manager.py`
  - Spec: Take tournament winners, execute with Alpaca paper trading, track P&L
  - Done when: Top strategies automatically trade with paper money and report results

## Discovered Tasks
(Add new tasks here as they're discovered during development)

## Completed Tasks
(Move completed items here for historical tracking)

---

## Quick Reference

**Key Files:**
- Phase 1: `src/scanner.py` (Polymarket - ✅ Done)
- Phase 3: `src/strategy_lab/` (To build)
- Phase 4: `src/scanners/stock_scanner.py` (To build)
- Dashboard: `web/dashboard.py` (Extend existing)

**External APIs:**
- Polymarket: `gamma-api.polymarket.com` (✅ Working)
- Alpaca: `api.alpaca.markets`, `data.alpaca.markets` (Needs LuLu allowlist)
- Alpha Vantage: `www.alphavantage.co` (Optional backup)

**Test Coverage Target:** 80%+ with integration tests for all API endpoints
**Success Criteria:** AI can improve strategies overnight, stocks + prediction markets unified dashboard
---

## Phase 5 — Next Shippable Features (Ranked by Impact)

*Audit by Lucky — March 9, 2026*

### 1. 🔔 Push Alerts / Notifications (HIGH IMPACT — Est. 4-6 hrs)

**Why:** Strategy results go to log files. You have to actively check the dashboard or hunt through logs. Making TradeSight *pull you* instead of you pulling it is a massive quality-of-life upgrade.

**What to build:**
- `src/alerts/notifier.py` — notification dispatcher
- Alert triggers: overnight optimization complete (with improvement delta), paper trade hits target/stop, high-confidence signal detected (>80% score)
- Delivery: Write to `/tmp/tradesight-alert.json` → hook into Luckys heartbeat check → Lucky forwards to WhatsApp
---

## Phase 5 — Next Shippable Features (Ranked by Impact)

*Audit by Lucky — March 9, 2026*

### 1. Push Alerts / Notifications (HIGH IMPACT — Est. 4-6 hrs)

**Why:** Strategy results go to log files. You have to actively check the dashboard or hunt through logs. Making TradeSight pull you instead of you pulling it is a massive quality-of-life upgrade.

**What to build:**
- `src/alerts/notifier.py` — notification dispatcher
- Alert triggers: overnight optimization complete (with improvement delta), paper trade hits target/stop, high-confidence signal detected (>80% score)
- Delivery: Write to `/tmp/tradesight-alert.json` → hook into Lucky's heartbeat check → Lucky forwards to WhatsApp
- Stretch: Webhook support for any endpoint

**Done when:** Overnight cron fires and Ray gets a WhatsApp message with the optimization result by morning.

---

### 2. Crypto Market Scanner (HIGH IMPACT — Est. 6-8 hrs)

**Why:** Currently covers prediction markets (Polymarket) + equities (Alpaca). Crypto is a massive, 24/7 market with free public APIs — no LuLu allowlist needed for read-only data.

**What to build:**
- `src/scanners/crypto_scanner.py` — uses CoinGecko API (free, no auth required)
- Scan top 100 coins by volume for RSI extremes, breakout patterns, volume spikes
- Add Crypto tab to web dashboard
- Apply same opportunity scoring framework already used for stocks

**Done when:** Dashboard shows crypto opportunities alongside stocks and Polymarket, auto-scanned every hour.

---

### 3. Morning Report / Daily Digest (MEDIUM-HIGH IMPACT — Est. 3-4 hrs)

**Why:** Overnight optimization produces JSON reports that nobody reads. A formatted morning summary that auto-generates at 8 AM (or on demand) turns raw data into actionable intelligence.

**What to build:**
- `scripts/morning_report.py` — reads last 24h of reports, paper trading P&L, best opportunities found
- Output: clean text summary (not JSON) saved to `reports/daily_YYYYMMDD.txt`
- Add to existing overnight cron to run after optimization finishes
- Hook into Lucky's heartbeat: if daily report exists and unseen, deliver it

**Done when:** Ray gets a clean daily summary each morning without logging into the dashboard.

---

*Next: Build in order above. Each feature is self-contained and shippable independently.*
