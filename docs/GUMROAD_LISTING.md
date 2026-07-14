# TradeSight 1.2 — Gumroad listing copy

**Status: APPROVED FOR THE VERIFIED 1.2 PAPER-ONLY RELEASE.**

## Headline

TradeSight — self-hosted strategy research and broker-simulated paper trading

## Short description

Test strategy families, inspect champion/challenger evidence, and track Alpaca paper orders from one local dashboard. TradeSight labels what is real, paper, stale, or unverified and keeps live-money execution disabled.

## What it does

- Evaluates RSI, MACD, momentum, Bollinger, confluence, and other included strategy families against real historical market data.
- Preserves out-of-sample, walk-forward, regime, multiple-testing, and Monte Carlo evidence before any promotion decision.
- Connects to Alpaca's paper endpoint for simulated orders, positions, and fills.
- Reconciles broker and local accounting from a defined paper-account epoch.
- Excludes legacy local profit that lacks complete broker proof from trusted performance.
- Explains recorded trades with available strategy, signal, regime, sizing, risk, and broker evidence.
- Shows a Live Trading Readiness checklist while technically compiling live execution out of the release.

## Honest limitations

- Paper results do not prove live execution or future profit.
- No return, win-rate, income, or performance claim is promised.
- Alpaca paper credentials are needed for broker-backed current data and trading evidence.
- Generated prices are not substituted when market data is unavailable.
- Polymarket data is archived and currently not maintained.
- Live-money trading is not enabled.
- Outbound safety alerts still require operator configuration and a successful test.

## Included

- Full Python source under the repository license.
- Supported Python 3.11+ installer with an isolated runtime.
- Local Flask dashboard at `http://127.0.0.1:5001`.
- Stock scanner using verified Alpaca data.
- Real optimizer/tournament evidence registry.
- Paper-order, fill, position, and accounting views.
- Protected operator controls and immutable security-action receipts.
- Live Trading Readiness evidence gates and paper-only lock.
- Local release manifest and SHA-256 integrity proof.

## Installation

```bash
python3 install_tradesight.py
./launch_tradesight.sh
```

The installer does not clone another copy, does not change brokerage credentials, and does not enable live trading.

## Buyer fit

Good fit for Python developers and algorithmic-trading hobbyists who want an inspectable local research and paper-trading system.

Not a fit for anyone seeking guaranteed returns, automatic live-money activation, a no-setup synthetic demo, or a managed trading service.

## Pricing

Keep the existing listing price unchanged until Ray separately decides otherwise. This file does not authorize a price, listing, account, or publication change.

## Release verification

The customer archive must pass these checks before upload:

1. Build the allowlisted paper-only archive.
2. Verify its SHA-256 manifest.
3. Complete a clean extracted installation using Python 3.11+.
4. Pass the complete test suite and dashboard smoke test.
5. Confirm no credentials, runtime databases, state, reports, or logs are included.
6. Record the uploaded filename and checksum in the release proof.
