[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/rmbell09-lang/tradesight/blob/main/intro_demo.ipynb)
[![PyPI version](https://badge.fury.io/py/tradesight.svg)](https://pypi.org/project/tradesight/)

# 🎯 TradeSight — Python Algorithmic Trading & Backtesting Strategy Lab

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Flask](https://img.shields.io/badge/flask-2.3+-green.svg)](https://flask.palletsprojects.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests: 169/169](https://img.shields.io/badge/tests-169%2F169%20passing-brightgreen.svg)]()
[![Paper Trading](https://img.shields.io/badge/mode-paper%20trading-orange.svg)]()
[![Live P&L](https://img.shields.io/badge/live%20P%26L-%2B6.43%25%20%28%2B%2432.15%29-brightgreen.svg)]()
[![Portfolio](https://img.shields.io/badge/portfolio-%24532-blue.svg)]()
[![GitHub Stars](https://img.shields.io/github/stars/rmbell09-lang/tradesight?style=social)](https://github.com/rmbell09-lang/tradesight)
[![Strategies](https://img.shields.io/badge/strategies-9-brightgreen.svg)](https://github.com/rmbell09-lang/tradesight#strategies)

**Build, test, and evolve trading strategies with AI — entirely on your own machine. No cloud subscription. No data leaks. No monthly fees.**

TradeSight is a self-hosted Python app that runs AI-powered strategy tournaments overnight, backtests technical indicators, and executes paper trades via Alpaca — all from a local web dashboard.

---

## 🤔 Who Is This For?

- **Algorithmic trading hobbyists** who want to test strategies without risking real money
- **Python developers** exploring quantitative finance and AI-driven decision systems
- **Privacy-conscious traders** who don't want their strategies on someone else's server
- **Makers** building autonomous financial agents

---

## ✨ Features <a name=strategies></a>

| Feature | Description |
|---|---|
| 🧬 **AI Strategy Tournaments** | Automated overnight evolution of trading strategies — the best wins, rest are retired |
| 📊 **15+ Technical Indicators** | MACD, RSI, Bollinger Bands, EMA crossovers, ATR, volume analysis, and more |
| 💸 **Paper Trading** | Connect Alpaca paper account — trade with fake money, track real P&L |
| 🔍 **Multi-Market Scanner** | Scan stocks + Polymarket prediction markets for signals simultaneously |
| 🌐 **Web Dashboard** | Real-time Flask interface — positions, signals, tournament results, logs |
| ⏰ **Cron Automation** | Overnight strategy improvement runs automatically — wake up to new results |
| 🔒 **100% Local** | Runs on your machine. Your strategies stay yours. |

---

## 🚀 Quick Start

### Requirements
- Python 3.11+
- macOS or Linux (Windows via WSL)
- [Alpaca paper trading account](https://alpaca.markets/) (free, optional — demo mode works without it)

### Install

**macOS (Homebrew):**
```bash
brew tap rmbell09-lang/tradesight
brew install tradesight
```

**From source:**
```bash
git clone https://github.com/rmbell09-lang/tradesight.git
cd tradesight
pip install -r requirements.txt
```

### Run

```bash
python START_TRADESIGHT.py
```

Dashboard opens at **http://localhost:5000**

### Demo Mode (No API Keys Required)
TradeSight runs fully in demo mode with simulated market data — no Alpaca account needed to explore.

### Live Paper Trading (Optional)
1. Create a free [Alpaca paper account](https://alpaca.markets/)
2. Add your API keys to `config/api_keys.json`:
```json
{
  "alpaca_key": "YOUR_KEY",
  "alpaca_secret": "YOUR_SECRET",
  "paper": true
}
```

---

## 📸 Dashboard

```
┌─────────────────────────────────────────────────┐
│  TradeSight Dashboard          [Localhost:5000]  │
├──────────┬──────────┬───────────┬───────────────┤
│ Markets  │Tournaments│  Trading  │   Settings    │
├──────────┴──────────┴───────────┴───────────────┤
│  Active Signals: 3    Open Positions: 2          │
│  Best Strategy: MACD Crossover (score: 0.72)     │
│  Paper P&L: -$113.96  (initial RSI strategy)     │
│  Next Tournament: Tonight @ 2:00 AM              │
└─────────────────────────────────────────────────┘
```

---

## 🧪 Test Results

```
169/169 tests passing ✅
```

```bash
python -m pytest tests/ -v
```

---

## 🏗️ Architecture

```
tradesight/
├── src/
│   ├── scanner.py          # Multi-market signal scanner
│   ├── strategy_lab/       # AI tournament engine
│   ├── trading/            # Alpaca paper trade executor
│   ├── indicators/         # 15+ technical indicators
│   └── automation/         # Overnight cron jobs
├── web/                    # Flask dashboard
├── config/                 # API keys + settings
├── data/                   # Price history cache
└── tests/                  # 169 unit tests
```

---

## 📈 Live Paper Trading — April 2026

Real money-at-risk (on paper). Updated automatically. System has been live since March 2026.

**Current Portfolio:** $532 total value | **+6.43% total return** (+$32.15)

| Strategy | Trades | P&L | Win Rate |
|---|---|---|---|
| MACD Crossover | 1 | +$17.89 | 100% ✅ |
| VWAP Reversion | 1 | -$4.75 | 0% |
| RSI Mean Reversion | 5 | -$13.77 | 20% |

**Recent Closed Trades (Last 7 Days):**
| Symbol | Strategy | Entry | Exit | P&L |
|---|---|---|---|---|
| JPM | MACD Crossover | $242.94 | $303.24 | +$17.89 ✅ |
| SPY | RSI Mean Reversion | $596.36 | $649.79 | +$6.06 ✅ |
| AMZN | VWAP Reversion | $223.25 | $209.69 | -$4.75 |
| QQQ | RSI Mean Reversion | $590.06 | $565.22 | -$6.56 |
| GOOG | RSI Mean Reversion | $297.77 | $276.47 | -$13.28 |

> This is a live paper trading account connected to Alpaca. All prices are real market data; no real money at risk. System runs overnight tournaments to evolve strategy parameters automatically. Early RSI underperformance drove a regime-filter upgrade now in production.

---

## 🗺️ Roadmap

- [x] Multi-indicator technical analysis (15+ indicators)
- [x] AI strategy tournament engine
- [x] Alpaca paper trading integration
- [x] Real-time web dashboard
- [x] Overnight automation (cron)
- [ ] Phase 1: Active stop-loss + take-profit execution
- [ ] Phase 1: Trailing stop with high-water mark
- [ ] Phase 2: Confluence strategy (multi-indicator entry gates)
- [ ] Phase 2: Market regime detection (bull/bear/sideways filter)
- [ ] Phase 3: Monte Carlo simulation for strategy validation

---


---

## 💰 Support Development

TradeSight is MIT-licensed and free to use. If it saved you time or you want the packaged strategy lab with setup guide and pre-tuned parameters:

**[Get TradeSight Strategy Lab on Gumroad →](https://qcautonomous.gumroad.com/l/zpkutz)** — $49 one-time

Includes: packaged download, setup walkthrough, pre-configured Alpaca integration, and strategy parameter reference.

## 🔗 Related Projects & Alternatives

TradeSight is similar to — but different from — these popular Python trading tools:

| Project | What It Does | How TradeSight Differs |
|---|---|---|
| [backtrader](https://github.com/mementum/backtrader) | Python backtesting framework | TradeSight adds AI strategy evolution + live paper trading web dashboard |
| [freqtrade](https://github.com/freqtrade/freqtrade) | Crypto trading bot | TradeSight focuses on **stocks** (Alpaca API) with overnight strategy tournaments |
| [vectorbt](https://github.com/polakowo/vectorbt) | Vectorized backtesting in notebooks | TradeSight is a self-hosted web app — no notebook required |
| [zipline-reloaded](https://github.com/stefan-jansen/zipline-reloaded) | Algorithmic trading library | TradeSight is a full app, not a library — no Python trading experience needed |
| [Jesse](https://github.com/jesse-ai/jesse) | Crypto strategy framework | TradeSight is for **stocks + prediction markets**, not crypto |
| [nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | High-performance trading platform | TradeSight is simpler, self-hosted, built for hobbyists not HFT |
| [BillingWatch](https://github.com/rmbell09-lang/BillingWatch) | Self-hosted billing anomaly detection | Same maker — catch Stripe issues before they cost you |

> **Also useful for:** python trading bot · algorithmic trading python · paper trading software · free backtesting · stock trading python · quantitative finance · algo trading strategy tester · self-hosted trading platform · automated trading system · backtesting framework

---

## 📄 License

MIT — free to use, modify, and build on.

---

## ⭐ If This Helped You

Star the repo — it helps other Python traders find it.

Got broken AI-generated code? → [Vibe Code Rescue](https://rmbell09-lang.github.io/tradesight/vibe-code-rescue.html)
