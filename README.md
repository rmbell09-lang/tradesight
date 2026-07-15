# TradeSight

TradeSight is a self-hosted strategy-research and **paper-trading** application. It evaluates strategy families against historical data, preserves champion/challenger evidence, and connects to an Alpaca paper account for broker-simulated orders and reconciliation.

TradeSight is research software. It is not financial advice, does not promise returns, and does not contain an enabled live-money execution path.

## What is real in the current release

- Alpaca market data is labeled `REAL` only after source verification.
- Orders, positions, and fills from the Alpaca paper endpoint are labeled `PAPER`.
- Broker/local accounting is reconciled from a defined paper-account epoch.
- Historical local P&L that lacks complete broker proof is preserved as `LEGACY / UNVERIFIED` and excluded from trusted performance.
- Strategy Lab reads the persisted optimizer, tournament database, champion, challengers, and rejection evidence.
- The Live Readiness screen shows mandatory evidence gates but cannot activate live trading.
- Polymarket records are archived and hidden when stale; the scanner is not currently maintained.

## Requirements

- Python 3.11 or newer
- macOS or Linux
- Alpaca **paper** credentials for broker-backed market and trading evidence

## Supported installation

Extract the verified TradeSight source release, then run:

```bash
python3 install_tradesight.py
./launch_tradesight.sh
```

The installer:

- selects Python 3.11 or newer;
- creates an isolated `.runtime` environment;
- installs declared runtime dependencies;
- installs the local extracted source without cloning another repository; and
- verifies the CLI in `PAPER_ONLY` mode.

Release verification may use `--no-register` so an extracted test copy cannot
replace the canonical CLI pointer.

The dashboard opens at `http://127.0.0.1:5001`.

For development from the canonical source tree:

```bash
python3 install_tradesight.py
.runtime/bin/python -m pip install -r requirements-dev.txt
.runtime/bin/python -m pytest tests -q
.runtime/bin/tradesight --no-browser
```

## Data labels

- `REAL`: verified external market evidence.
- `PAPER`: broker-simulated trading evidence.
- `DEMO`: explicit development-only synthetic data.
- `STALE`: retained data too old to support a current decision.
- `UNVERIFIED`: local evidence that has not met broker-proof requirements.

TradeSight never silently substitutes generated prices for unavailable market data.

## Strategy lifecycle

`Candidate → Backtest → Out-of-sample → Shadow paper → Qualified → Champion → Retired`

Daily evaluation continues with new real data. Full searches are evidence-triggered. A challenger cannot promote automatically and must pass out-of-sample, walk-forward, multiple-testing, Monte Carlo, and forward-paper evidence gates.

## Live-money status

This release is technically locked to paper trading. The readiness dashboard requires, among other evidence:

- 30 verified accounting sessions with zero unexplained differences;
- 60 forward-paper sessions and 100 broker-confirmed forward trades;
- a frozen strategy with a passing qualification receipt;
- protected controls, working outbound safety alerts, and passed failure drills; and
- the operator's explicit strategy approval and hard per-trade, daily, and total-pilot loss limits.

Passing those gates would only support a separate canary-build review. It would not automatically enable trading or guarantee profitability.

## Release integrity

Build a local release archive with:

```bash
.runtime/bin/python scripts/build_release.py
.runtime/bin/python scripts/verify_release.py dist/releases/tradesight-1.2.0-paper-only.zip
```

The builder uses an allowlist, rejects runtime/private state and common credential patterns, writes a SHA-256 manifest, and never publishes the archive.

## Tests

Run the complete canonical suite instead of relying on a hardcoded README count:

```bash
.runtime/bin/python -m pytest tests -q
```

The dashboard displays the latest test receipt and marks it stale when it ages out.

## Safety boundaries

- Paper endpoint only.
- No live activation endpoint.
- No automatic strategy promotion.
- No automatic scaling.
- New paper entries fail closed on stale signals or unverified accounting.
- Exits remain available during operational suspensions.

## License

MIT. Third-party data and brokerage services retain their own terms.
