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
from contextlib import closing
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List, Tuple
import uuid

logger = logging.getLogger(__name__)

TRADE_FEEDBACK_COLUMNS = {
    'notional': 'REAL DEFAULT 0.0',
    'risk_dollars': 'REAL DEFAULT 0.0',
    'r_multiple': 'REAL DEFAULT 0.0',
    'max_adverse_pct': 'REAL DEFAULT 0.0',
    'max_favorable_pct': 'REAL DEFAULT 0.0',
    'missing_exit_reason': 'INTEGER DEFAULT 0',
    'regime_source': "TEXT DEFAULT 'unknown'",
}


class FeedbackTracker:
    """Tracks paper trading outcomes against parameter sets for adaptive optimization."""

    def __init__(self, base_dir: str = None):
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent.parent
        self.db_path = self.base_dir / 'data' / 'feedback.db'
        self.db_path.parent.mkdir(exist_ok=True)
        self._init_db()

    def _init_db(self):
        with closing(sqlite3.connect(self.db_path)) as connection, connection as conn:
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
            conn.execute('''
                CREATE TABLE IF NOT EXISTS trade_feedback (
                    source TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    params_hash TEXT,
                    params_json TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    side TEXT DEFAULT 'long',
                    entry_price REAL DEFAULT 0.0,
                    exit_price REAL DEFAULT 0.0,
                    quantity REAL DEFAULT 0.0,
                    pnl_dollars REAL DEFAULT 0.0,
                    pnl_pct REAL DEFAULT 0.0,
                    exit_reason TEXT DEFAULT '',
                    market_regime TEXT DEFAULT 'unknown',
                    opened_at TEXT,
                    closed_at TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (source, source_id)
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_trade_feedback_symbol_strategy ON trade_feedback(symbol, strategy)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_trade_feedback_closed_at ON trade_feedback(closed_at)')
            self._ensure_trade_feedback_columns(conn)
            conn.commit()
        logger.info(f"Feedback DB ready: {self.db_path}")

    def _ensure_trade_feedback_columns(self, conn):
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(trade_feedback)").fetchall()
        }
        for column, ddl in TRADE_FEEDBACK_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE trade_feedback ADD COLUMN {column} {ddl}")

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

        with closing(sqlite3.connect(self.db_path)) as connection, connection as conn:
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
        merged = {}
        with closing(sqlite3.connect(self.db_path)) as connection, connection as conn:
            rows = conn.execute('''
                SELECT params_hash, params_json, times_used, avg_pnl,
                       win_sessions, loss_sessions, last_pnl
                FROM param_performance
                WHERE times_used >= ?
                ORDER BY avg_pnl DESC
            ''', (min_uses,)).fetchall()
            trade_rows = conn.execute('''
                SELECT params_hash, params_json, COUNT(*) as trades,
                       AVG(pnl_pct) as avg_pnl_pct,
                       SUM(CASE WHEN pnl_dollars > 0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN pnl_dollars <= 0 THEN 1 ELSE 0 END) as losses,
                       SUM(pnl_dollars) as total_pnl_dollars
                FROM trade_feedback
                WHERE params_hash IS NOT NULL
                GROUP BY params_hash, params_json
                HAVING COUNT(*) >= ?
            ''', (min_uses,)).fetchall()

        for row in rows:
            params = json.loads(row[1])
            win_rate = row[4] / (row[4] + row[5]) if (row[4] + row[5]) > 0 else 0
            merged[row[0]] = {
                'hash': row[0],
                'params': params,
                'times_used': row[2],
                'avg_pnl': row[3],
                'win_rate': win_rate,
                'last_pnl': row[6],
                'score': row[3] * (0.5 + 0.5 * win_rate),  # blended score
                'source': 'session',
            }

        for row in trade_rows:
            params = json.loads(row[1])
            trades = int(row[2] or 0)
            wins = int(row[4] or 0)
            losses = int(row[5] or 0)
            win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0
            avg_pnl = float(row[3] or 0.0)
            score = avg_pnl * (0.5 + 0.5 * win_rate)
            existing = merged.get(row[0])
            if not existing or trades >= existing.get('times_used', 0) or score > existing.get('score', 0):
                merged[row[0]] = {
                    'hash': row[0],
                    'params': params,
                    'times_used': trades,
                    'avg_pnl': avg_pnl,
                    'win_rate': win_rate,
                    'last_pnl': avg_pnl,
                    'score': score,
                    'source': 'trade_feedback',
                    'total_pnl_dollars': float(row[6] or 0.0),
                }
        return sorted(merged.values(), key=lambda x: x['avg_pnl'], reverse=True)

    def get_top_params(self, n: int = 5) -> List[Dict]:
        """Return top N parameter sets by blended score."""
        scores = self.get_param_scores(min_uses=1)
        return sorted(scores, key=lambda x: x['score'], reverse=True)[:n]

    def record_closed_trade(self, params: Dict, symbol: str, strategy: str,
                            side: str, entry_price: float, exit_price: float,
                            quantity: float, pnl_dollars: float,
                            exit_reason: str = '', opened_at: str = None,
                            closed_at: str = None, market_regime: str = 'unknown',
                            risk_dollars: float = 0.0,
                            max_adverse_pct: float = 0.0,
                            max_favorable_pct: float = 0.0,
                            regime_source: str = 'unknown',
                            source: str = 'positions', source_id: str = None) -> bool:
        """Persist one closed trade outcome for execution-time learning.

        This table is intentionally trade-level instead of session-level. The
        trader can use it to learn that a specific symbol/strategy/exit pattern
        is working or failing, and the same shape works for paper and future
        real broker fills.
        """
        if not symbol or not strategy:
            return False
        params = params or {}
        params_hash = self._hash_params(params)
        source_id = str(source_id or f"{symbol}:{strategy}:{closed_at or datetime.now().isoformat()}")
        try:
            entry_price = float(entry_price or 0.0)
            exit_price = float(exit_price or 0.0)
            quantity = float(quantity or 0.0)
            pnl_dollars = float(pnl_dollars or 0.0)
            entry_value = abs(entry_price * quantity)
            pnl_pct = (pnl_dollars / entry_value * 100.0) if entry_value > 0 else 0.0
            notional = entry_value
            risk_dollars = abs(float(risk_dollars or 0.0))
            r_multiple = (pnl_dollars / risk_dollars) if risk_dollars > 0 else 0.0
            max_adverse_pct = float(max_adverse_pct or 0.0)
            max_favorable_pct = float(max_favorable_pct or 0.0)
            missing_exit_reason = 0 if str(exit_reason or '').strip() else 1
            exit_reason = self._normalize_exit_reason(exit_reason, pnl_dollars)
            market_regime, regime_source = self.classify_market_regime(
                market_regime=market_regime,
                regime_source=regime_source,
            )
            with closing(sqlite3.connect(self.db_path)) as connection, connection as conn:
                conn.execute('''
                    INSERT OR REPLACE INTO trade_feedback
                    (source, source_id, params_hash, params_json, symbol, strategy, side,
                     entry_price, exit_price, quantity, pnl_dollars, pnl_pct, exit_reason,
                     market_regime, opened_at, closed_at, notional, risk_dollars,
                     r_multiple, max_adverse_pct, max_favorable_pct, missing_exit_reason,
                     regime_source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ''', (
                    source, source_id, params_hash, json.dumps(params, sort_keys=True),
                    symbol, strategy, side or 'long', entry_price, exit_price,
                    quantity, pnl_dollars, pnl_pct, exit_reason or '',
                    market_regime or 'unknown', opened_at, closed_at, notional,
                    risk_dollars, r_multiple, max_adverse_pct, max_favorable_pct,
                    missing_exit_reason, regime_source or 'unknown',
                ))
                conn.commit()
            return True
        except Exception as exc:
            logger.warning("Trade feedback record failed for %s/%s: %s", symbol, strategy, exc)
            return False

    def _normalize_exit_reason(self, exit_reason: str, pnl_dollars: float) -> str:
        reason = str(exit_reason or '').strip()
        if reason:
            return reason
        if pnl_dollars > 0:
            return 'unlabeled_profit_exit'
        if pnl_dollars < 0:
            return 'unlabeled_loss_exit'
        return 'unlabeled_flat_exit'

    def classify_market_regime(self, market_regime: str = 'unknown',
                               regime_source: str = 'unknown',
                               context: Dict = None) -> Tuple[str, str]:
        """Small deterministic regime hook for callers with market context."""
        regime = str(market_regime or '').strip().lower()
        if regime and regime != 'unknown':
            return regime, regime_source or 'provided'
        context = context or {}
        volatility = context.get('volatility_pct')
        trend = context.get('trend_pct')
        try:
            volatility = float(volatility)
            trend = float(trend)
        except (TypeError, ValueError):
            return 'unknown', regime_source or 'unknown'
        if volatility >= 2.5:
            return 'high_volatility', 'deterministic_context'
        if trend >= 1.0:
            return 'bullish_trend', 'deterministic_context'
        if trend <= -1.0:
            return 'bearish_trend', 'deterministic_context'
        return 'sideways', 'deterministic_context'

    def ingest_positions_db(self, positions_db: Path, params: Dict,
                            source: str = 'positions_backfill',
                            limit: int = 10000) -> int:
        """Backfill closed trades from positions.db into trade_feedback.

        Historical position rows do not always know the exact parameter set used,
        so params is stored as the current learning context while symbol/strategy
        statistics still remain valid and immediately useful.
        """
        positions_db = Path(positions_db)
        if not positions_db.exists():
            return 0
        inserted = 0
        with closing(sqlite3.connect(positions_db)) as _resource, _resource as src:
            rows = src.execute('''
                SELECT id, symbol, strategy, side, entry_price, exit_price, quantity,
                       realized_pnl, COALESCE(exit_reason, ''), entry_time, exit_time
                FROM positions
                WHERE status='closed'
                  AND entry_price IS NOT NULL
                  AND quantity IS NOT NULL
                  AND realized_pnl IS NOT NULL
                ORDER BY COALESCE(exit_time, updated_at, created_at) DESC
                LIMIT ?
            ''', (int(limit),)).fetchall()
        for row in rows:
            (
                pos_id, symbol, strategy, side, entry_price, exit_price,
                quantity, pnl_dollars, exit_reason, opened_at, closed_at,
            ) = row
            if self.record_closed_trade(
                params=params,
                symbol=symbol,
                strategy=strategy,
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                pnl_dollars=pnl_dollars,
                exit_reason=exit_reason,
                opened_at=opened_at,
                closed_at=closed_at,
                source=source,
                source_id=str(pos_id),
            ):
                inserted += 1
        return inserted

    def get_execution_adjustment(self, symbol: str, strategy: str,
                                 min_trades: int = 3) -> Dict:
        """Return conservative confidence/size adjustment from trade outcomes."""
        default = {
            'tradeable': True,
            'confidence_multiplier': 1.0,
            'size_multiplier': 1.0,
            'reason': 'insufficient trade-level feedback',
            'sample_size': 0,
            'win_rate': None,
            'avg_pnl_pct': None,
            'total_pnl_dollars': None,
            'sample_weight': 0.0,
            'avg_r_multiple': None,
        }
        with closing(sqlite3.connect(self.db_path)) as connection, connection as conn:
            rows = conn.execute('''
                SELECT pnl_dollars, pnl_pct, COALESCE(r_multiple, 0.0)
                FROM trade_feedback
                WHERE symbol=? AND strategy=?
                ORDER BY COALESCE(closed_at, created_at) DESC
                LIMIT 30
            ''', (symbol, strategy)).fetchall()
            if len(rows) < min_trades:
                rows = conn.execute('''
                    SELECT pnl_dollars, pnl_pct, COALESCE(r_multiple, 0.0)
                    FROM trade_feedback
                    WHERE symbol=?
                    ORDER BY COALESCE(closed_at, created_at) DESC
                    LIMIT 30
                ''', (symbol,)).fetchall()
                scope = 'symbol'
            else:
                scope = 'symbol_strategy'

        if len(rows) < min_trades:
            return default

        pnl_dollars = [float(r[0] or 0.0) for r in rows]
        pnl_pct = [float(r[1] or 0.0) for r in rows]
        r_values = [float(r[2] or 0.0) for r in rows]
        wins = sum(1 for p in pnl_dollars if p > 0)
        losses = sum(1 for p in pnl_dollars if p <= 0)
        sample = len(rows)
        win_rate = wins / sample if sample else 0.0
        avg_pnl_pct = sum(pnl_pct) / sample if sample else 0.0
        total_pnl = sum(pnl_dollars)
        avg_r_multiple = sum(r_values) / sample if sample else 0.0
        sample_weight = min(1.0, sample / 10.0)

        adjustment = {
            **default,
            'sample_size': sample,
            'win_rate': round(win_rate, 4),
            'avg_pnl_pct': round(avg_pnl_pct, 4),
            'total_pnl_dollars': round(total_pnl, 4),
            'sample_weight': round(sample_weight, 4),
            'avg_r_multiple': round(avg_r_multiple, 4),
            'reason': (
                f"{scope}: n={sample}, win_rate={win_rate:.0%}, "
                f"avg_pnl={avg_pnl_pct:.2f}%, avg_R={avg_r_multiple:.2f}, "
                f"total=${total_pnl:.2f}"
            ),
        }

        if sample >= 5 and total_pnl < 0 and avg_pnl_pct < -1.0 and win_rate < 0.45:
            adjustment.update({
                'tradeable': False,
                'confidence_multiplier': 0.0,
                'size_multiplier': 0.0,
                'reason': 'blocked by learned negative edge: ' + adjustment['reason'],
            })
        elif total_pnl < 0 or avg_pnl_pct < 0:
            adjustment.update({
                'confidence_multiplier': 0.85,
                'size_multiplier': 0.50,
                'reason': 'reduced by learned weak edge: ' + adjustment['reason'],
            })
        elif sample >= 8 and win_rate >= 0.60 and avg_pnl_pct > 0.75:
            adjustment.update({
                'confidence_multiplier': 1.08,
                'size_multiplier': 1.20,
                'reason': 'boosted by learned positive edge: ' + adjustment['reason'],
            })
        elif sample >= 5 and win_rate >= 0.50 and avg_pnl_pct > 0:
            adjustment.update({
                'confidence_multiplier': 1.03,
                'size_multiplier': 1.05,
                'reason': 'slightly boosted by learned positive edge: ' + adjustment['reason'],
            })
        elif win_rate >= 0.50 and avg_pnl_pct > 0:
            adjustment.update({
                'reason': 'observed positive edge but sample too small for boost: ' + adjustment['reason'],
            })
        return adjustment

    def generate_daily_playbook(self, min_trades: int = 3) -> Dict:
        """Summarize learned pairs for daily paper/shadow routing."""
        playbook = {
            'generated_at': datetime.now().isoformat(),
            'preferred_pairs': [],
            'blocked_pairs': [],
            'reduced_pairs': [],
            'explore_pairs': [],
        }
        with closing(sqlite3.connect(self.db_path)) as connection, connection as conn:
            rows = conn.execute('''
                SELECT symbol, strategy, COUNT(*) as trades,
                       SUM(pnl_dollars) as total_pnl,
                       AVG(pnl_pct) as avg_pnl_pct,
                       SUM(CASE WHEN pnl_dollars > 0 THEN 1 ELSE 0 END) as wins
                FROM trade_feedback
                GROUP BY symbol, strategy
                ORDER BY trades DESC, total_pnl DESC
            ''').fetchall()
        for symbol, strategy, trades, total_pnl, avg_pnl_pct, wins in rows:
            trades = int(trades or 0)
            wins = int(wins or 0)
            win_rate = wins / trades if trades else 0.0
            item = {
                'symbol': symbol,
                'strategy': strategy,
                'trades': trades,
                'total_pnl_dollars': round(float(total_pnl or 0.0), 4),
                'avg_pnl_pct': round(float(avg_pnl_pct or 0.0), 4),
                'win_rate': round(win_rate, 4),
            }
            if trades < min_trades:
                playbook['explore_pairs'].append(item)
            elif item['total_pnl_dollars'] < 0 and item['avg_pnl_pct'] < -1.0 and win_rate < 0.45:
                playbook['blocked_pairs'].append(item)
            elif item['total_pnl_dollars'] < 0 or item['avg_pnl_pct'] < 0:
                playbook['reduced_pairs'].append(item)
            elif trades >= 5 and win_rate >= 0.50 and item['avg_pnl_pct'] > 0:
                playbook['preferred_pairs'].append(item)
            else:
                playbook['explore_pairs'].append(item)
        return playbook

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
        with closing(sqlite3.connect(self.db_path)) as connection, connection as conn:
            total_sessions = conn.execute('SELECT COUNT(*) FROM session_log').fetchone()[0]
            total_params = conn.execute('SELECT COUNT(*) FROM param_performance').fetchone()[0]
            best = conn.execute(
                'SELECT params_json, avg_pnl, times_used FROM param_performance ORDER BY avg_pnl DESC LIMIT 1'
            ).fetchone()

        lines = [
            f"Feedback DB: {total_sessions} sessions, {total_params} unique param sets",
        ]
        try:
            with closing(sqlite3.connect(self.db_path)) as connection, connection as conn:
                trade_count = conn.execute('SELECT COUNT(*) FROM trade_feedback').fetchone()[0]
            lines.append(f"Trade-level feedback: {trade_count} closed trades")
        except Exception:
            pass
        if best:
            p = json.loads(best[0])
            lines.append(f"Best params (avg {best[1]:.2f}% over {best[2]} sessions): "
                         f"oversold={p.get('oversold')}, overbought={p.get('overbought')}, "
                         f"size={p.get('position_size')}, sl={p.get('stop_loss_pct')}, "
                         f"tp={p.get('take_profit_pct')}")
        return '\n'.join(lines)
