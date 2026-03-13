#!/usr/bin/env python3
"""
TradeSight Feedback Tracker

Logs paper trading results against the parameter sets that generated them.
The overnight optimizer reads this data to weight future parameter searches
toward historically profitable combos.

Schema:
  param_performance table:
    - params_hash: SHA1 of the parameter dict (unique key)
    - params_json: full param set as JSON
    - times_used: how many sessions used these params
    - total_pnl: cumulative P&L across all sessions
    - avg_pnl: average P&L per session
    - win_sessions: sessions with positive P&L
    - loss_sessions: sessions with negative P&L
    - last_used: timestamp
    - last_pnl: most recent session P&L

  session_log table:
    - session_id: UUID
    - params_hash: FK to param_performance
    - date: trading date
    - pnl: session P&L
    - trades_opened: count
    - trades_closed: count
    - win_rate: closed trades win rate
    - market_regime: trending/choppy/volatile (future use)
"""

import hashlib
import json
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List, Tuple
import uuid

logger = logging.getLogger(__name__)


class FeedbackTracker:
    """Tracks paper trading outcomes against parameter sets for adaptive optimization."""

    def __init__(self, base_dir: str = None):
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).parent.parent
        self.db_path = self.base_dir / 'data' / 'feedback.db'
        self.db_path.parent.mkdir(exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS param_performance (
                    params_hash TEXT PRIMARY KEY,
                    params_json TEXT NOT NULL,
                    times_used INTEGER DEFAULT 0,
                    total_pnl REAL DEFAULT 0.0,
                    avg_pnl REAL DEFAULT 0.0,
                    win_sessions INTEGER DEFAULT 0,
                    loss_sessions INTEGER DEFAULT 0,
                    last_used TEXT,
                    last_pnl REAL DEFAULT 0.0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS session_log (
                    session_id TEXT PRIMARY KEY,
                    params_hash TEXT,
                    date TEXT,
                    pnl REAL,
                    trades_opened INTEGER DEFAULT 0,
                    trades_closed INTEGER DEFAULT 0,
                    win_rate REAL DEFAULT 0.0,
                    market_regime TEXT DEFAULT 'unknown',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (params_hash) REFERENCES param_performance(params_hash)
                )
            ''')
            conn.commit()
        logger.info(f"Feedback DB ready: {self.db_path}")

    def _hash_params(self, params: Dict) -> str:
        """Stable hash of a parameter dict."""
        canonical = json.dumps(params, sort_keys=True)
        return hashlib.sha1(canonical.encode()).hexdigest()[:16]

    def log_session(self, params: Dict, pnl: float, trades_opened: int = 0,
                    trades_closed: int = 0, win_rate: float = 0.0,
                    market_regime: str = 'unknown') -> str:
        """
        Log a completed paper trading session.
        Returns the session_id.
        """
        params_hash = self._hash_params(params)
        session_id = str(uuid.uuid4())[:8]
        date = datetime.now().strftime('%Y-%m-%d')

        with sqlite3.connect(self.db_path) as conn:
            # Upsert param_performance
            existing = conn.execute(
                'SELECT times_used, total_pnl, win_sessions, loss_sessions FROM param_performance WHERE params_hash = ?',
                (params_hash,)
            ).fetchone()

            if existing:
                times_used = existing[0] + 1
                total_pnl = existing[1] + pnl
                win_sessions = existing[2] + (1 if pnl > 0 else 0)
                loss_sessions = existing[3] + (1 if pnl <= 0 else 0)
                avg_pnl = total_pnl / times_used
                conn.execute('''
                    UPDATE param_performance
                    SET times_used=?, total_pnl=?, avg_pnl=?, win_sessions=?,
                        loss_sessions=?, last_used=?, last_pnl=?
                    WHERE params_hash=?
                ''', (times_used, total_pnl, avg_pnl, win_sessions,
                      loss_sessions, date, pnl, params_hash))
            else:
                conn.execute('''
                    INSERT INTO param_performance
                    (params_hash, params_json, times_used, total_pnl, avg_pnl,
                     win_sessions, loss_sessions, last_used, last_pnl)
                    VALUES (?,?,1,?,?,?,?,?,?)
                ''', (params_hash, json.dumps(params), pnl, pnl,
                      1 if pnl > 0 else 0, 0 if pnl > 0 else 1, date, pnl))

            # Log session
            conn.execute('''
                INSERT INTO session_log
                (session_id, params_hash, date, pnl, trades_opened, trades_closed, win_rate, market_regime)
                VALUES (?,?,?,?,?,?,?,?)
            ''', (session_id, params_hash, date, pnl, trades_opened,
                  trades_closed, win_rate, market_regime))
            conn.commit()

        logger.info(f"Logged session {session_id}: params={params_hash}, P&L={pnl:.2f}%")
        return session_id

    def get_param_scores(self, min_uses: int = 2) -> List[Dict]:
        """
        Return all parameter sets with at least min_uses sessions,
        sorted by avg_pnl descending. Used by the optimizer for weighting.
        """
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute('''
                SELECT params_hash, params_json, times_used, avg_pnl,
                       win_sessions, loss_sessions, last_pnl
                FROM param_performance
                WHERE times_used >= ?
                ORDER BY avg_pnl DESC
            ''', (min_uses,)).fetchall()

        results = []
        for row in rows:
            params = json.loads(row[1])
            win_rate = row[4] / (row[4] + row[5]) if (row[4] + row[5]) > 0 else 0
            results.append({
                'hash': row[0],
                'params': params,
                'times_used': row[2],
                'avg_pnl': row[3],
                'win_rate': win_rate,
                'last_pnl': row[6],
                'score': row[3] * (0.5 + 0.5 * win_rate)  # blended score
            })
        return results

    def get_top_params(self, n: int = 5) -> List[Dict]:
        """Return top N parameter sets by blended score."""
        scores = self.get_param_scores(min_uses=1)
        return sorted(scores, key=lambda x: x['score'], reverse=True)[:n]

    def get_neighborhood_params(self, params: Dict, radius: int = 2) -> List[Dict]:
        """
        Generate parameter variants near a known-good set.
        Used to explore around proven params instead of full grid search.
        """
        variants = []
        base = params.copy()

        oversold_vals = [max(20, base.get('oversold', 30) + d) for d in range(-radius*2, radius*2+1, 2)]
        overbought_vals = [min(80, base.get('overbought', 70) + d) for d in range(-radius*2, radius*2+1, 2)]

        for os_val in oversold_vals:
            for ob_val in overbought_vals:
                if ob_val <= os_val + 20:
                    continue
                v = base.copy()
                v['oversold'] = os_val
                v['overbought'] = ob_val
                variants.append(v)

        return variants

    def summary(self) -> str:
        """Human-readable summary of feedback data."""
        with sqlite3.connect(self.db_path) as conn:
            total_sessions = conn.execute('SELECT COUNT(*) FROM session_log').fetchone()[0]
            total_params = conn.execute('SELECT COUNT(*) FROM param_performance').fetchone()[0]
            best = conn.execute(
                'SELECT params_json, avg_pnl, times_used FROM param_performance ORDER BY avg_pnl DESC LIMIT 1'
            ).fetchone()

        lines = [
            f"Feedback DB: {total_sessions} sessions, {total_params} unique param sets",
        ]
        if best:
            p = json.loads(best[0])
            lines.append(f"Best params (avg {best[1]:.2f}% over {best[2]} sessions): "
                         f"oversold={p.get('oversold')}, overbought={p.get('overbought')}, "
                         f"size={p.get('position_size')}, sl={p.get('stop_loss_pct')}, "
                         f"tp={p.get('take_profit_pct')}")
        return '\n'.join(lines)
