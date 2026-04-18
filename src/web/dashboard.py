#!/usr/bin/env python3
"""
TradeSight Performance Dashboard (Task 21)

Web dashboard showing trading performance, equity curve, win rates.
Runs on port 5001 (same as existing TradeSight Flask app if any).
"""

import json
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, render_template_string

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / 'data'

app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>TradeSight Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, system-ui, sans-serif; background: #0d1117; color: #c9d1d9; }
        .header { padding: 20px 30px; background: #161b22; border-bottom: 1px solid #30363d; }
        .header h1 { font-size: 24px; color: #58a6ff; }
        .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; padding: 20px 30px; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; }
        .card h3 { font-size: 13px; color: #8b949e; text-transform: uppercase; margin-bottom: 8px; }
        .card .value { font-size: 28px; font-weight: 700; }
        .card .value.positive { color: #3fb950; }
        .card .value.negative { color: #f85149; }
        .chart-container { grid-column: span 2; min-height: 300px; }
        .full-width { grid-column: span 4; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #21262d; }
        th { color: #8b949e; font-size: 12px; text-transform: uppercase; }
        td { font-size: 14px; }
        .pnl-pos { color: #3fb950; }
        .pnl-neg { color: #f85149; }
        .regime-badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; }
        .regime-low_vol { background: #1f6feb33; color: #58a6ff; }
        .regime-normal { background: #3fb95033; color: #3fb950; }
        .regime-high_vol { background: #d2992233; color: #d29922; }
        .regime-extreme { background: #f8514933; color: #f85149; }
    </style>
</head>
<body>
    <div class="header">
        <h1>📈 TradeSight Dashboard</h1>
    </div>
    <div class="grid" id="dashboard">
        <div class="card"><h3>Portfolio Value</h3><div class="value" id="portfolio-value">-</div></div>
        <div class="card"><h3>Total P&L</h3><div class="value" id="total-pnl">-</div></div>
        <div class="card"><h3>Win Rate</h3><div class="value" id="win-rate">-</div></div>
        <div class="card"><h3>Open Positions</h3><div class="value" id="open-positions">-</div></div>
        
        <div class="card chart-container">
            <h3>Equity Curve (30d)</h3>
            <canvas id="equity-chart"></canvas>
        </div>
        <div class="card chart-container">
            <h3>Win Rate by Strategy</h3>
            <canvas id="strategy-chart"></canvas>
        </div>
        
        <div class="card full-width">
            <h3>Recent Trades</h3>
            <table>
                <thead><tr><th>Symbol</th><th>Strategy</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Reason</th><th>Time</th></tr></thead>
                <tbody id="trades-table"></tbody>
            </table>
        </div>
    </div>
    <script>
    async function load() {
        const [perf, trades] = await Promise.all([
            fetch('/api/performance').then(r => r.json()),
            fetch('/api/trades').then(r => r.json())
        ]);
        
        document.getElementById('portfolio-value').textContent = '$' + perf.portfolio_value.toFixed(2);
        const pnlEl = document.getElementById('total-pnl');
        pnlEl.textContent = (perf.total_pnl >= 0 ? '+$' : '-$') + Math.abs(perf.total_pnl).toFixed(2);
        pnlEl.className = 'value ' + (perf.total_pnl >= 0 ? 'positive' : 'negative');
        document.getElementById('win-rate').textContent = perf.win_rate.toFixed(1) + '%';
        document.getElementById('open-positions').textContent = perf.open_positions;
        
        // Equity chart
        if (perf.equity_curve && perf.equity_curve.length) {
            new Chart(document.getElementById('equity-chart'), {
                type: 'line',
                data: {
                    labels: perf.equity_curve.map(e => e.date),
                    datasets: [{
                        label: 'Portfolio Value',
                        data: perf.equity_curve.map(e => e.value),
                        borderColor: '#58a6ff',
                        backgroundColor: '#58a6ff22',
                        fill: true, tension: 0.3, pointRadius: 0
                    }]
                },
                options: { scales: { x: { display: false }, y: { grid: { color: '#21262d' } } } }
            });
        }
        
        // Strategy chart
        if (perf.by_strategy && perf.by_strategy.length) {
            new Chart(document.getElementById('strategy-chart'), {
                type: 'bar',
                data: {
                    labels: perf.by_strategy.map(s => s.strategy),
                    datasets: [{
                        label: 'Win Rate %',
                        data: perf.by_strategy.map(s => s.win_rate),
                        backgroundColor: perf.by_strategy.map(s => s.win_rate >= 50 ? '#3fb950' : '#f85149')
                    }]
                },
                options: { scales: { y: { max: 100, grid: { color: '#21262d' } } } }
            });
        }
        
        // Trades table
        const tbody = document.getElementById('trades-table');
        tbody.innerHTML = trades.map(t => `
            <tr>
                <td><strong>${t.symbol}</strong></td>
                <td>${t.strategy}</td>
                <td>${t.side}</td>
                <td>$${(t.entry_price||0).toFixed(2)}</td>
                <td>${t.exit_price ? '$'+t.exit_price.toFixed(2) : '-'}</td>
                <td class="${(t.pnl||0) >= 0 ? 'pnl-pos' : 'pnl-neg'}">
                    ${(t.pnl||0) >= 0 ? '+' : ''}$${(t.pnl||0).toFixed(2)}
                </td>
                <td>${t.exit_reason || t.entry_reason || '-'}</td>
                <td>${t.time || '-'}</td>
            </tr>
        `).join('');
    }
    load();
    setInterval(load, 60000); // Refresh every minute
    </script>
</body>
</html>
"""


@app.route('/')
@app.route('/dashboard')
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@app.route('/api/performance')
def api_performance():
    """Portfolio performance summary."""
    try:
        db_path = DATA_DIR / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            # Portfolio value from latest snapshot or positions
            open_positions = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE status='open'"
            ).fetchone()[0]
            
            # Total realized P&L
            total_pnl = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions WHERE status='closed'"
            ).fetchone()[0]
            
            # Win rate
            total_closed = conn.execute("SELECT COUNT(*) FROM positions WHERE status='closed'").fetchone()[0]
            wins = conn.execute("SELECT COUNT(*) FROM positions WHERE status='closed' AND realized_pnl > 0").fetchone()[0]
            win_rate = (wins / total_closed * 100) if total_closed > 0 else 0
            
            # By strategy
            by_strategy = []
            strategies = conn.execute(
                "SELECT DISTINCT strategy FROM positions WHERE status='closed'"
            ).fetchall()
            for (strat,) in strategies:
                s_total = conn.execute(
                    "SELECT COUNT(*) FROM positions WHERE strategy=? AND status='closed'", (strat,)
                ).fetchone()[0]
                s_wins = conn.execute(
                    "SELECT COUNT(*) FROM positions WHERE strategy=? AND status='closed' AND realized_pnl > 0", (strat,)
                ).fetchone()[0]
                s_pnl = conn.execute(
                    "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions WHERE strategy=? AND status='closed'", (strat,)
                ).fetchone()[0]
                by_strategy.append({
                    'strategy': strat,
                    'trades': s_total,
                    'wins': s_wins,
                    'win_rate': round(s_wins / s_total * 100, 1) if s_total > 0 else 0,
                    'total_pnl': round(s_pnl, 2)
                })
            
            # Equity curve from snapshots
            equity_curve = []
            try:
                snapshots = conn.execute(
                    "SELECT date(entry_time), SUM(realized_pnl) "
                    "FROM positions WHERE status='closed' "
                    "GROUP BY date(entry_time) ORDER BY date(entry_time)"
                ).fetchall()
                running = 500.0  # initial balance
                for date_str, daily_pnl in snapshots:
                    running += (daily_pnl or 0)
                    equity_curve.append({'date': date_str, 'value': round(running, 2)})
            except Exception as e:
                logger.warning(f"Failed to build equity curve: {e}")
            
            # Balance sync
            portfolio_value = 500.0 + total_pnl
            try:
                bal = conn.execute(
                    "SELECT balance FROM balance_sync ORDER BY synced_at DESC LIMIT 1"
                ).fetchone()
                if bal:
                    portfolio_value = bal[0]
            except Exception as e:
                logger.warning(f"Failed to read balance_sync: {e}")
            
            return jsonify({
                'portfolio_value': round(portfolio_value, 2),
                'total_pnl': round(total_pnl, 2),
                'win_rate': round(win_rate, 1),
                'open_positions': open_positions,
                'total_trades': total_closed,
                'by_strategy': by_strategy,
                'equity_curve': equity_curve,
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/trades')
def api_trades():
    """Recent trades list."""
    try:
        db_path = DATA_DIR / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            # Check if journal columns exist
            cols = [row[1] for row in conn.execute("PRAGMA table_info(positions)").fetchall()]
            has_journal = 'entry_reason' in cols
            
            if has_journal:
                rows = conn.execute(
                    "SELECT symbol, strategy, side, entry_price, exit_price, "
                    "realized_pnl, entry_reason, exit_reason, "
                    "COALESCE(exit_time, entry_time) as time "
                    "FROM positions ORDER BY COALESCE(exit_time, entry_time) DESC LIMIT 50"
                ).fetchall()
                return jsonify([{
                    'symbol': r[0], 'strategy': r[1], 'side': r[2],
                    'entry_price': r[3], 'exit_price': r[4], 'pnl': r[5],
                    'entry_reason': r[6], 'exit_reason': r[7], 'time': r[8]
                } for r in rows])
            else:
                rows = conn.execute(
                    "SELECT symbol, strategy, side, entry_price, exit_price, "
                    "realized_pnl, COALESCE(exit_time, entry_time) as time "
                    "FROM positions ORDER BY COALESCE(exit_time, entry_time) DESC LIMIT 50"
                ).fetchall()
                return jsonify([{
                    'symbol': r[0], 'strategy': r[1], 'side': r[2],
                    'entry_price': r[3], 'exit_price': r[4], 'pnl': r[5],
                    'entry_reason': '', 'exit_reason': '', 'time': r[6]
                } for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio')
def api_portfolio():
    """Current open positions."""
    try:
        db_path = DATA_DIR / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, strategy, side, quantity, entry_price, "
                "current_price, entry_time FROM positions WHERE status='open'"
            ).fetchall()
            return jsonify([{
                'symbol': r[0], 'strategy': r[1], 'side': r[2],
                'quantity': r[3], 'entry_price': r[4],
                'current_price': r[5], 'entry_time': r[6],
                'unrealized_pnl': round((r[5] - r[4]) * r[3], 2) if r[4] and r[5] else 0
            } for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)
