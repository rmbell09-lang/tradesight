#!/usr/bin/env python3
"""
TradeSight Trade Logger

Logs every individual trade (open + close) to a SQLite DB for granular analysis.
Session-level P&L is useful but hides patterns. Trade-level data answers:
  - Are we exiting winners too early (take profit too tight)?
  - Are stops triggering too often (stop loss too tight)?
  - Which symbols are profitable vs losers?
  - What's our average hold time?
  - Does performance degrade intraday (morning vs afternoon)?
"""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class TradeLogger:
    """Logs individual trades for granular performance analysis."""

    def __init__(self, base_dir: str = None):
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).parent.parent
        self.db_path = self.base_dir / 'data' / 'trades.db'
        self.db_path.parent.mkdir(exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    strategy TEXT,
                    params_hash TEXT,
                    side TEXT,
                    quantity REAL,
                    entry_price REAL,
                    exit_price REAL,
                    entry_time TEXT,
                    exit_time TEXT,
                    hold_minutes INTEGER,
                    pnl_dollars REAL,
                    pnl_pct REAL,
                    exit_reason TEXT,
                    market_session TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS open_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    strategy TEXT,
                    params_hash TEXT,
                    side TEXT,
                    quantity REAL,
                    entry_price REAL,
                    entry_time TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()

    def _market_session(self) -> str:
        """Classify time as morning/midday/afternoon."""
        hour = datetime.now().hour
        if hour < 11:
            return 'morning'
        elif hour < 13:
            return 'midday'
        else:
            return 'afternoon'

    def log_open(self, symbol: str, strategy: str, side: str,
                 quantity: float, entry_price: float,
                 params_hash: str = '') -> int:
        """Log trade open. Returns open_trade_id."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute('''
                INSERT INTO open_trades
                (symbol, strategy, params_hash, side, quantity, entry_price, entry_time)
                VALUES (?,?,?,?,?,?,?)
            ''', (symbol, strategy, params_hash, side, quantity,
                  entry_price, datetime.now().isoformat()))
            conn.commit()
            open_id = cur.lastrowid
        logger.info(f"Trade opened: {side} {quantity} {symbol} @ ${entry_price:.2f} [{strategy}]")
        return open_id

    def log_close(self, symbol: str, strategy: str, exit_price: float,
                  exit_reason: str = 'signal', params_hash: str = ''):
        """Log trade close. Matches against most recent open trade for this symbol+strategy."""
        with sqlite3.connect(self.db_path) as conn:
            open_trade = conn.execute('''
                SELECT id, side, quantity, entry_price, entry_time
                FROM open_trades
                WHERE symbol = ? AND strategy = ?
                ORDER BY entry_time DESC LIMIT 1
            ''', (symbol, strategy)).fetchone()

            if not open_trade:
                logger.warning(f"No open trade found for {symbol}/{strategy} — skipping close log")
                return

            ot_id, side, quantity, entry_price, entry_time_str = open_trade

            # Calculate P&L
            if side == 'long':
                pnl_dollars = (exit_price - entry_price) * quantity
            else:
                pnl_dollars = (entry_price - exit_price) * quantity
            pnl_pct = ((exit_price - entry_price) / entry_price * 100) if side == 'long' else ((entry_price - exit_price) / entry_price * 100)

            # Hold time
            try:
                entry_time = datetime.fromisoformat(entry_time_str)
                hold_minutes = int((datetime.now() - entry_time).total_seconds() / 60)
            except Exception as e:
                logger.debug(f"Failed to parse entry_time for {symbol}/{strategy}: {e}")
                hold_minutes = 0

            conn.execute('''
                INSERT INTO trades
                (symbol, strategy, params_hash, side, quantity, entry_price, exit_price,
                 entry_time, exit_time, hold_minutes, pnl_dollars, pnl_pct,
                 exit_reason, market_session)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (symbol, strategy, params_hash, side, quantity, entry_price, exit_price,
                  entry_time_str, datetime.now().isoformat(), hold_minutes,
                  round(pnl_dollars, 4), round(pnl_pct, 4),
                  exit_reason, self._market_session()))

            conn.execute('DELETE FROM open_trades WHERE id = ?', (ot_id,))
            conn.commit()

        outcome = 'WIN' if pnl_dollars > 0 else 'LOSS'
        logger.info(f"Trade closed: {symbol} [{exit_reason}] P&L=${pnl_dollars:.2f} ({pnl_pct:.2f}%) {outcome}")

    def get_analysis(self, days: int = 30) -> Dict:
        """Return structured analysis of recent trades."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute('''
                SELECT symbol, strategy, side, pnl_dollars, pnl_pct,
                       hold_minutes, exit_reason, market_session
                FROM trades
                WHERE created_at > datetime('now', ?)
            ''', (f'-{days} days',)).fetchall()

        if not rows:
            return {'total_trades': 0, 'message': 'No closed trades yet'}

        total = len(rows)
        wins = sum(1 for r in rows if r[3] > 0)
        total_pnl = sum(r[3] for r in rows)
        avg_pnl_pct = sum(r[4] for r in rows) / total
        avg_hold = sum(r[5] for r in rows) / total

        # By exit reason
        by_reason = {}
        for r in rows:
            reason = r[6]
            if reason not in by_reason:
                by_reason[reason] = {'count': 0, 'wins': 0, 'pnl': 0}
            by_reason[reason]['count'] += 1
            if r[3] > 0:
                by_reason[reason]['wins'] += 1
            by_reason[reason]['pnl'] += r[3]

        # By symbol
        by_symbol = {}
        for r in rows:
            sym = r[0]
            if sym not in by_symbol:
                by_symbol[sym] = {'count': 0, 'pnl': 0}
            by_symbol[sym]['count'] += 1
            by_symbol[sym]['pnl'] += r[3]

        # By session
        by_session = {}
        for r in rows:
            sess = r[7]
            if sess not in by_session:
                by_session[sess] = {'count': 0, 'wins': 0}
            by_session[sess]['count'] += 1
            if r[3] > 0:
                by_session[sess]['wins'] += 1

        return {
            'total_trades': total,
            'win_rate': round(wins / total * 100, 1),
            'total_pnl_dollars': round(total_pnl, 2),
            'avg_pnl_pct': round(avg_pnl_pct, 2),
            'avg_hold_minutes': round(avg_hold),
            'by_exit_reason': {k: {
                'count': v['count'],
                'win_rate': round(v['wins'] / v['count'] * 100, 1),
                'total_pnl': round(v['pnl'], 2)
            } for k, v in by_reason.items()},
            'by_symbol': {k: {
                'count': v['count'],
                'total_pnl': round(v['pnl'], 2)
            } for k, v in sorted(by_symbol.items(), key=lambda x: -x[1]['pnl'])},
            'by_session': {k: {
                'count': v['count'],
                'win_rate': round(v['wins'] / v['count'] * 100, 1) if v['count'] else 0
            } for k, v in by_session.items()}
        }

    def report(self, days: int = 30) -> str:
        """Human-readable trade analysis report."""
        a = self.get_analysis(days)
        if a.get('total_trades', 0) == 0:
            return "No closed trades yet."

        lines = [
            f"📊 Trade Analysis (last {days} days)",
            f"Total trades: {a['total_trades']} | Win rate: {a['win_rate']}%",
            f"Total P&L: ${a['total_pnl_dollars']:.2f} | Avg per trade: {a['avg_pnl_pct']:.2f}%",
            f"Avg hold time: {a['avg_hold_minutes']} min",
            "",
            "Exit reasons:"
        ]
        for reason, stats in a.get('by_exit_reason', {}).items():
            lines.append(f"  {reason}: {stats['count']} trades, {stats['win_rate']}% win, ${stats['total_pnl']:.2f}")

        lines.append("\nBy symbol:")
        for sym, stats in list(a.get('by_symbol', {}).items())[:5]:
            lines.append(f"  {sym}: {stats['count']} trades, ${stats['total_pnl']:.2f}")

        lines.append("\nBy session:")
        for sess, stats in a.get('by_session', {}).items():
            lines.append(f"  {sess}: {stats['count']} trades, {stats['win_rate']}% win")

        return '\n'.join(lines)
