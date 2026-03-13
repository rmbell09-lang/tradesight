#!/usr/bin/env python3
"""
TradeSight Morning Report
Reads last 24h optimization results + paper P&L + opportunities.
Outputs a clean text summary to reports/daily_YYYYMMDD.txt
Also writes to /tmp/tradesight-daily-report.txt for heartbeat pickup.

Usage:
    python3 scripts/morning_report.py
    python3 scripts/morning_report.py --quiet  # suppress stdout
"""

import json
import sys
import glob
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
REPORTS_DIR = PROJECT_DIR / "reports"
DATA_DIR = PROJECT_DIR / "data"
HEARTBEAT_FILE = Path("/tmp/tradesight-daily-report.txt")

QUIET = "--quiet" in sys.argv


def log(msg):
    if not QUIET:
        print(msg)


def load_recent_optimizations(hours=24):
    """Load optimization JSON reports from the last N hours."""
    cutoff = datetime.now() - timedelta(hours=hours)
    results = []

    for fpath in sorted(REPORTS_DIR.glob("optimization_*.json")):
        # Parse timestamp from filename: optimization_YYYYMMDD_HHMMSS.json
        try:
            parts = fpath.stem.split("_")
            ts_str = parts[1] + "_" + parts[2]
            ts = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            if ts >= cutoff:
                with open(fpath) as f:
                    data = json.load(f)
                data["_file"] = fpath.name
                data["_ts"] = ts
                results.append(data)
        except (IndexError, ValueError, json.JSONDecodeError):
            continue

    return sorted(results, key=lambda x: x["_ts"], reverse=True)


def load_paper_pnl():
    """Try to read paper trading P&L from known data locations."""
    candidates = [
        DATA_DIR / "paper_trades.json",
        DATA_DIR / "pnl.json",
        PROJECT_DIR / "data" / "paper_pnl.json",
    ]
    for c in candidates:
        if c.exists():
            try:
                with open(c) as f:
                    return json.load(f)
            except Exception:
                pass
    return None


def format_report(opt_results, pnl_data):
    today = datetime.now().strftime("%A, %B %-d, %Y")
    now = datetime.now().strftime("%I:%M %p")

    lines = [
        "=" * 60,
        f"  TRADESIGHT DAILY REPORT",
        f"  {today} — Generated {now}",
        "=" * 60,
        "",
    ]

    # Optimization results
    if opt_results:
        lines.append(f"OVERNIGHT OPTIMIZATION ({len(opt_results)} run(s) in last 24h)")
        lines.append("-" * 40)
        for r in opt_results[:3]:  # top 3
            winner = r.get("winner", "Unknown")
            baseline_pnl = r.get("baseline", {}).get("pnl_pct", 0)
            opt_pnl = r.get("optimized", {}).get("pnl_pct", 0)
            improvement = r.get("improvement", {}).get("pnl_pct", 0)
            sharpe = r.get("optimized", {}).get("sharpe", 0)
            win_rate = r.get("optimized", {}).get("win_rate", 0)
            ts = r.get("_ts", datetime.now()).strftime("%m/%d %H:%M")

            lines.append(f"  [{ts}] Winner: {winner}")
            lines.append(f"    P&L: {baseline_pnl:.1f}% → {opt_pnl:.1f}% (+{improvement:.1f}%)")
            lines.append(f"    Sharpe: {sharpe:.2f} | Win Rate: {win_rate:.0f}%")

            # Best params
            best_params = r.get("optimized", {}).get("parameters", {})
            if best_params:
                lines.append(f"    Best params: {json.dumps(best_params, separators=(',', ':'))}")
            lines.append("")
    else:
        lines.append("OVERNIGHT OPTIMIZATION")
        lines.append("-" * 40)
        lines.append("  No optimization runs in the last 24 hours.")
        lines.append("")

    # Paper P&L
    lines.append("PAPER TRADING P&L")
    lines.append("-" * 40)
    if pnl_data:
        if isinstance(pnl_data, dict):
            total_pnl = pnl_data.get("total_pnl", pnl_data.get("total", "N/A"))
            trades = pnl_data.get("total_trades", pnl_data.get("trades", "N/A"))
            lines.append(f"  Total P&L: {total_pnl}")
            lines.append(f"  Total Trades: {trades}")
            if "win_rate" in pnl_data:
                lines.append(f"  Win Rate: {pnl_data['win_rate']:.1f}%")
        else:
            lines.append(f"  {pnl_data}")
    else:
        lines.append("  No paper trading data found.")
        lines.append("  (Run overnight_strategy_evolution.py to generate paper trades)")
    lines.append("")

    # Opportunities (if any flagged)
    opp_file = DATA_DIR / "opportunities.json"
    if opp_file.exists():
        try:
            with open(opp_file) as f:
                opps = json.load(f)
            if opps:
                lines.append("TOP OPPORTUNITIES")
                lines.append("-" * 40)
                for opp in opps[:5]:
                    symbol = opp.get("symbol", opp.get("market", "?"))
                    score = opp.get("score", opp.get("confidence", "?"))
                    sig = opp.get("signal", opp.get("type", ""))
                    lines.append(f"  {symbol}: score={score} {sig}")
                lines.append("")
        except Exception:
            pass

    # Status
    lines.append("STATUS")
    lines.append("-" * 40)
    if opt_results:
        last_run = opt_results[0]["_ts"].strftime("%m/%d at %I:%M %p")
        lines.append(f"  Last optimization: {last_run}")
    lines.append(f"  Report generated: {datetime.now().strftime('%m/%d/%Y %I:%M %p')}")
    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)


def main():
    log("TradeSight Morning Report — generating...")

    opt_results = load_recent_optimizations(hours=24)
    log(f"  Found {len(opt_results)} optimization run(s) in last 24h")

    pnl_data = load_paper_pnl()
    log(f"  Paper P&L data: {'found' if pnl_data else 'not found'}")

    report = format_report(opt_results, pnl_data)

    # Save to dated file
    REPORTS_DIR.mkdir(exist_ok=True)
    today_str = datetime.now().strftime("%Y%m%d")
    out_path = REPORTS_DIR / f"daily_{today_str}.txt"
    out_path.write_text(report)
    log(f"  Saved: {out_path}")

    # Write to heartbeat pickup location
    HEARTBEAT_FILE.write_text(report)
    log(f"  Heartbeat file: {HEARTBEAT_FILE}")

    if not QUIET:
        print()
        print(report)

    return out_path


if __name__ == "__main__":
    main()
