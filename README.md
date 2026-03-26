# 🎯 TradeSight — Self-Hosted AI Trading Strategy Lab

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Flask](https://img.shields.io/badge/flask-2.3+-green.svg)](https://flask.palletsprojects.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests: 169/169](https://img.shields.io/badge/tests-169%2F169%20passing-brightgreen.svg)]()
[![Paper Trading](https://img.shields.io/badge/mode-paper%20trading-orange.svg)]()

**Build, test, and evolve trading strategies with AI — entirely on your own machine. No cloud subscription. No data leaks. No monthly fees.**

TradeSight is a self-hosted Python app that runs AI-powered strategy tournaments overnight, backtests technical indicators, and executes paper trades via Alpaca — all from a local web dashboard.

---

## 🤔 Who Is This For?

- **Algorithmic trading hobbyists** who want to test strategies without risking real money
- **Python developers** exploring quantitative finance and AI-driven decision systems
- **Privacy-conscious traders** who don't want their strategies on someone else's server
- **Makers** building autonomous financial agents

---

## ✨ Features

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

## 📈 Paper Trading Log — March 2026

Real system activity on paper money (transparent dev journal):

| Date | Strategy | Action | Result |
|---|---|---|---|
| Mar 2026 | RSI Mean Reversion | 3 trades | -$113.96 realized |
| Mar 16 | MACD Crossover | Tournament winner | Score: 0.72 |
| Mar 12 | RSI Mean Reversion | Tournament winner | Score: 0.62 |

> ⚠️ Early results reflect the initial RSI strategy before multi-indicator confluence filtering was added. Confluence (MACD + RSI + Bollinger alignment) is in active development.

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

## 🔗 Related Projects

| Project | Description |
|---|---|
| [BillingWatch](https://github.com/rmbell09-lang/BillingWatch) | Self-hosted billing anomaly detection — catch Stripe issues before they cost you |

---

## 📄 License

MIT — free to use, modify, and build on.

---

## ⭐ If This Helped You

Star the repo — it helps other Python traders find it.

Got broken AI-generated code? → [Vibe Code Rescue](https://rmbell09-lang.github.io/tradesight/vibe-code-rescue.html)
