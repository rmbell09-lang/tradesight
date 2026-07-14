#!/usr/bin/env python3
"""
TradeSight Paper Trading Orchestrator

Takes winning strategies from tournaments and executes them in live paper trading.
Integrates with Alpaca Markets for real market data and paper trading execution.
"""

import os
import sys
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import pandas as pd
import time
import threading

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from data.alpaca_client import AlpacaClient
from trading.position_manager import PositionManager, PortfolioState
from trading.accounting_truth import load_epoch, reconcile_accounting
from automation.strategy_automation import StrategyAutomation
from strategy_lab.tournament import get_builtin_strategies
from indicators.technical_indicators import TechnicalIndicators

# Earnings calendar filter (Task 24)
try:
    from data.earnings_calendar import is_near_earnings
    _EARNINGS_AVAILABLE = True
except ImportError:
    _EARNINGS_AVAILABLE = False

# Market regime detector (Task 17)
try:
    from indicators.regime_detector import RegimeDetector, MarketRegime, fetch_vix
    _REGIME_AVAILABLE = True
except ImportError:
    _REGIME_AVAILABLE = False

# Feedback tracker — imported lazily to avoid circular imports
try:
    from trading.feedback_tracker import FeedbackTracker
    _FEEDBACK_AVAILABLE = True
except ImportError:
    _FEEDBACK_AVAILABLE = False


# AlertManager — push notifications on trades
try:
    from alerts.alert_manager import AlertManager
    from alerts.alert_types import AlertType
    _ALERTS_AVAILABLE = True
except ImportError:
    _ALERTS_AVAILABLE = False

# Champion tracker — load optimizer-winning params
try:
    from trading.champion_tracker import ChampionTracker
    _CHAMPION_AVAILABLE = True
except ImportError:
    _CHAMPION_AVAILABLE = False


class ExponentialBackoffWebSocketSupervisor:
    """Supervises a websocket-style connection with exponential backoff reconnect."""

    def __init__(self, connect_once, logger, initial_backoff: float = 1.0,
                 max_backoff: float = 60.0, sleeper=time.sleep):
        self.connect_once = connect_once
        self.logger = logger
        self.initial_backoff = max(0.1, float(initial_backoff))
        self.max_backoff = max(self.initial_backoff, float(max_backoff))
        self.sleeper = sleeper

    def run(self, stop_event: threading.Event):
        """Run until stop_event is set.

        connect_once() should block while connected and raise on drop/failure.
        """
        delay = self.initial_backoff
        while not stop_event.is_set():
            try:
                self.connect_once(stop_event=stop_event)
                if stop_event.is_set():
                    break
                # Unexpected clean return: treat as dropped connection
                raise ConnectionError('WebSocket loop exited unexpectedly')
            except Exception as exc:
                if stop_event.is_set():
                    break
                self.logger.warning(
                    f'[WS] Connection dropped: {exc}. Reconnecting in {delay:.1f}s')
                self.sleeper(delay)
                delay = min(delay * 2, self.max_backoff)



class PaperTrader:
    """Orchestrates paper trading with tournament-winning strategies"""
    
    def __init__(self, base_dir: str = None, alpaca_api_key: str = None, 
                 alpaca_secret: str = None, initial_balance: float = 500.0):
        resolved_base = Path(base_dir).resolve() if base_dir else Path(__file__).resolve().parent.parent.parent
        # Normalize callers that pass project/src instead of project root.
        # Using different base dirs creates split SQLite state (data/positions.db vs src/data/positions.db)
        # and can bypass per-symbol entry checks across runs.
        if resolved_base.name == "src" and (resolved_base / "trading").exists():
            resolved_base = resolved_base.parent
        self.base_dir = resolved_base
        self.data_dir = self.base_dir / "data"
        self.logs_dir = self.base_dir / "logs"
        
        # Ensure directories exist
        for dir_path in [self.data_dir, self.logs_dir]:
            dir_path.mkdir(exist_ok=True)
        
        # Initialize components
        self.position_manager = PositionManager(base_dir=self.base_dir, initial_balance=initial_balance)
        self.automation = StrategyAutomation(base_dir=self.base_dir)
        
        # Active params — loaded from ChampionTracker (optimizer winning params)
        self.active_params: Dict = {}
        if _CHAMPION_AVAILABLE:
            try:
                # Use the resolved project root for this PaperTrader instance.
                # Tests and alternate checkouts pass their own base_dir; loading
                # the live repo champion there leaks real learning into isolated runs.
                _ct = ChampionTracker(base_dir=str(self.base_dir))
                _champ = _ct.get_champion()
                if _champ and _champ.get('params'):
                    self.active_params = _champ['params']
                    import logging
                    logging.getLogger('PaperTrader').info(f'Loaded champion params: {self.active_params}')
            except Exception as _e:
                import logging
                logging.getLogger('PaperTrader').warning(f'Could not load champion params: {_e}')
        
        # Feedback tracker
        if _FEEDBACK_AVAILABLE:
            # Use the same project root as the trader so feedback matches this instance.
            self.feedback = FeedbackTracker(base_dir=str(self.base_dir))
        else:
            self.feedback = None
        

        # Alert manager — dispatches email/webhook notifications
        if _ALERTS_AVAILABLE:
            try:
                import sys, os
                sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
                from config import ALERTS_CONFIG
                self.alert_manager = AlertManager(
                    config=ALERTS_CONFIG,
                    data_dir=str(self.data_dir)
                )
            except Exception as _ae:
                import logging
                logging.getLogger('PaperTrader').warning(f'AlertManager init failed: {_ae}')
                self.alert_manager = None
        else:
            self.alert_manager = None

        # Initialize Alpaca client. If keys were not passed explicitly, load the
        # shared TradeSight config/keychain path. This prevents direct PaperTrader
        # usage from silently falling into demo quotes while reading the real
        # positions DB.
        if not alpaca_api_key or not alpaca_secret:
            # Only autoload credentials for the real TradeSight project checkout.
            # Unit tests create temporary base_dir values and expect demo/mocked
            # clients; loading real keychain creds there makes tests hit Alpaca.
            should_autoload_creds = (self.base_dir / '.env').exists() or self.base_dir.name == 'TradeSight'
            if should_autoload_creds:
                try:
                    try:
                        from src import config as _ts_config
                    except Exception:
                        import config as _ts_config
                    alpaca_api_key = alpaca_api_key or getattr(_ts_config, 'ALPACA_API_KEY', None)
                    alpaca_secret = alpaca_secret or getattr(_ts_config, 'ALPACA_SECRET_KEY', None)
                except Exception as _cfg_e:
                    logging.getLogger('PaperTrader').warning(f'Could not load Alpaca keys from config/keychain: {_cfg_e}')

        if alpaca_api_key and alpaca_secret:
            self.alpaca = AlpacaClient(api_key=alpaca_api_key, secret_key=alpaca_secret, paper=True)
        else:
            self.alpaca = AlpacaClient()  # Demo mode (safe only for empty/synthetic state)
        
        # Load per-symbol OOS performance (from optimizer)
        self._symbol_performance = {}
        try:
            perf_file = self.base_dir / 'data' / 'symbol_performance.json'
            if perf_file.exists():
                with open(perf_file) as f:
                    self._symbol_performance = json.load(f)
                logging.getLogger('PaperTrader').info(
                    'Loaded symbol performance: %d symbols, %d tradeable' % (
                        len(self._symbol_performance),
                        sum(1 for v in self._symbol_performance.values() if v.get('tradeable', True))))
        except Exception as _spe:
            logging.getLogger('PaperTrader').warning('Could not load symbol performance: %s' % str(_spe))
        
        # Setup logging
        self._setup_logging()

        # WebSocket trade-update monitor state
        self._ws_stop_event = None
        self._ws_thread = None
        self._ws_supervisor = None

        # Load per-cluster params
        self._cluster_params = self._load_clusters()

        # Earnings calendar filter (Task 24)
        try:
            from data.earnings_calendar import is_near_earnings
            _EARNINGS_AVAILABLE = True
        except ImportError:
            _EARNINGS_AVAILABLE = False

        # Market regime detector (Task 17)
        self._regime_detector = RegimeDetector() if _REGIME_AVAILABLE else None
        self._current_regime = MarketRegime.UNKNOWN if _REGIME_AVAILABLE else None
        self._regime_details = {}
        self._portfolio_peak = None
        self._circuit_breaker_until = None
        self._daily_loss_limit_reached = False  # Task 890 — daily loss circuit breaker
        self._cb_date = None  # tracks calendar day for daily-loss reset
        
        # Trading parameters
        self.config = {
            # SWING TRADE WATCHLIST - 20 mega/large-cap, high-liquidity stocks
            # Broad enough for good data collection, no high-beta names that
            # destroy mean reversion (removed TSLA, ADBE, AMD, BA - too volatile)
            # PDT avoided via min_hold_hours, not small watchlist
            'trading_symbols': [
                'SPY', 'QQQ',                      # Broad market ETFs
                'AAPL', 'MSFT', 'GOOGL', 'AMZN',  # Tech mega-cap
                'META',                             # Tech (stable post-2024)
                'JPM', 'BAC', 'V', 'MA',           # Financials
                'JNJ', 'PFE',                       # Healthcare
                'XOM', 'CVX',                       # Energy
                'WMT', 'COST', 'HD',               # Consumer/Retail
                'KO', 'DIS',                        # Consumer staples + media
            ],
            'min_strategy_confidence': 0.55,  # Signal confidence bar for individual entries
            # Tournament scores are composite return/risk scores, not signal confidence.
            # Recent real tournaments score around 0.01-0.03, so using min_strategy_confidence
            # here incorrectly forced fallback strategies every scan. Still require
            # a positive edge so tiny/zero-score winners do not trade by default.
            'min_tournament_winner_score': 0.01,
            'max_concurrent_trades': 5,       # 5 positions for more data (fractional shares)
            'trade_frequency_hours': 4,       # Check for signals every 4 hours
            'position_hold_days': 5,          # Hold positions 1-5 days (swing trade)
            'symbol_cooldown_hours': 24,      # Block same-symbol churn after any close
            'loss_cooldown_days': 7,          # Longer block after losing protective exits
            'hwm_max_quote_jump_pct': 0.08,   # Ignore one-tick HWM jumps above 8%
            'hwm_max_entry_jump_pct': 0.25,   # Ignore HWM quotes >25% above entry
            'accounting_divergence_warn_usd': 2.0,
            'min_hold_hours': 24,             # Optional minimum hold before discretionary closes
            'enforce_min_hold_hours': not getattr(self.alpaca, 'paper', True),
            'max_unrealized_gain_pct': 0.20,  # Auto-close at +20% unrealized gain
            'rebalance_frequency_days': 7,    # Rebalance weekly
            # Correlation groups — max 2 positions per group
            'correlation_groups': {
                'broad_market': ['SPY', 'QQQ'],
                'tech': ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META'],
                'financials': ['JPM', 'BAC', 'V', 'MA'],
                'healthcare': ['JNJ', 'PFE'],
                'energy': ['XOM', 'CVX'],
                'consumer': ['WMT', 'COST', 'HD', 'KO', 'DIS'],
            },
            'max_per_correlation_group': 2,
            'daily_loss_limit': 15.0,  # Task 890 — block new entries if daily P&L <= -$15
            'gap_warning_threshold': 0.03,  # warn/manage open positions on >3% overnight gap
            'gap_take_profit_threshold': 0.05,  # lock gains when gap-up profit is >=5%
            'gap_take_profit_when_at_capacity': True,  # free a slot only when saturated
        }

        self._refresh_learning_feedback()
    
    def _setup_logging(self):
        """Setup logging for paper trading"""
        log_file = self.logs_dir / f"paper_trader_{datetime.now().strftime('%Y%m%d')}.log"
        
        self.logger = logging.getLogger('PaperTrader')
        if not self.logger.handlers:  # Avoid duplicate handlers
            handler = logging.FileHandler(log_file)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)

    def _load_clusters(self) -> Dict:
        """Load symbol clusters with per-cluster params from symbol_clusters.json"""
        clusters = {}
        try:
            cluster_file = self.base_dir / 'data' / 'symbol_clusters.json'
            if cluster_file.exists():
                with open(cluster_file) as f:
                    raw = json.load(f)
                # Build symbol -> cluster params mapping
                for cluster_name, cluster_data in raw.items():
                    for sym in cluster_data.get('symbols', []):
                        clusters[sym] = cluster_data.get('default_params', {})
                logging.getLogger('PaperTrader').info(
                    'Loaded cluster params for %d symbols across %d clusters' % (
                        len(clusters), len(raw)))
        except Exception as e:
            logging.getLogger('PaperTrader').warning('Could not load clusters: %s' % str(e))
        return clusters

    def _refresh_learning_feedback(self) -> int:
        """Backfill closed paper trades into the trade-level learning table."""
        if not self.feedback:
            return 0
        try:
            params = dict(self.active_params or {})
            count = self.feedback.ingest_positions_db(
                self.position_manager.data_dir / 'positions.db',
                params=params,
                source='positions_backfill',
            )
            if count:
                logging.getLogger('PaperTrader').info(
                    '[Learning] Trade feedback backfill ready: %d closed position rows', count
                )
            return count
        except Exception as exc:
            logging.getLogger('PaperTrader').warning('[Learning] Feedback backfill failed: %s', exc)
            return 0

    def _get_learning_adjustment(self, symbol: str, strategy: str) -> Dict:
        """Read execution-time learning for a symbol/strategy pair."""
        if not self.feedback:
            return {
                'tradeable': True,
                'confidence_multiplier': 1.0,
                'size_multiplier': 1.0,
                'reason': 'feedback tracker unavailable',
            }
        try:
            return self.feedback.get_execution_adjustment(symbol, strategy)
        except Exception as exc:
            self.logger.warning('[Learning] Adjustment unavailable for %s/%s: %s', symbol, strategy, exc)
            return {
                'tradeable': True,
                'confidence_multiplier': 1.0,
                'size_multiplier': 1.0,
                'reason': 'feedback adjustment error',
            }

    def _get_params_for_symbol(self, symbol: str) -> Dict:
        """Get trading params for a symbol: cluster-specific if available, else active_params"""
        cluster_params = self._cluster_params.get(symbol)
        if cluster_params:
            # Merge: cluster params override active_params for keys that exist
            merged = dict(self.active_params)
            merged.update(cluster_params)
            return merged
        return dict(self.active_params)

    def _check_sector_exposure(self, symbol: str) -> bool:
        """Check if adding a position in this symbol would breach sector exposure limits.
        
        Returns True if the trade is ALLOWED, False if blocked.
        Max 50% of portfolio value in any single sector.
        """
        max_sector_pct = 0.50  # 50% max per sector
        corr_groups = self.config.get('correlation_groups', {})
        
        # Find which sector this symbol belongs to
        target_sector = None
        for group_name, group_symbols in corr_groups.items():
            if symbol in group_symbols:
                target_sector = group_name
                break
        
        if not target_sector:
            return True  # Unknown sector — allow
        
        try:
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                # Get total portfolio value
                portfolio = self.position_manager.get_portfolio_state()
                total_value = portfolio.total_value or self.initial_balance if hasattr(self, 'initial_balance') else 500
                
                if total_value <= 0:
                    return True
                
                # Get sector symbols
                sector_symbols = corr_groups[target_sector]
                placeholders = ','.join('?' for _ in sector_symbols)
                
                # Sum current exposure in this sector
                sector_value = conn.execute(
                    f"SELECT COALESCE(SUM(current_price * quantity), 0) "
                    f"FROM positions WHERE symbol IN ({placeholders}) AND status='open'",
                    sector_symbols
                ).fetchone()[0]
                
                sector_pct = sector_value / total_value
                
                if sector_pct >= max_sector_pct:
                    self.logger.info(
                        f"[SectorLimit] Blocking {symbol}: sector '{target_sector}' "
                        f"at {sector_pct*100:.1f}% (limit={max_sector_pct*100:.0f}%)")
                    return False
                
                return True
                
        except Exception as e:
            self.logger.warning(f"[SectorLimit] Check failed for {symbol}: {e}")
            return True  # Allow on error (don't block trading due to DB issues)
    
    def get_latest_tournament_winners(self, days: int = 7) -> List[Tuple[str, float]]:
        """Get winning strategies from recent tournaments"""
        try:
            db_path = self.automation.data_dir / 'tournament_history.db'
            
            if not db_path.exists():
                self.logger.warning("No tournament history found")
                return []
            
            with sqlite3.connect(db_path) as conn:
                # Get recent tournament winners
                winners = conn.execute('''
                    SELECT winner, winner_avg_score, start_time
                    FROM tournament_sessions 
                    WHERE status = 'completed' 
                    AND date(start_time) >= date('now', '-{} days')
                    ORDER BY winner_avg_score DESC
                    LIMIT 10
                '''.format(days)).fetchall()
                
                if not winners:
                    self.logger.info("No recent tournament winners found")
                    return []
                
                # Tournament winner_avg_score is a composite backtest score, not a live
                # signal confidence. Keep the filter separate so valid real tournament
                # winners are not accidentally discarded and replaced with fallback params.
                min_tournament_score = float(self.config.get('min_tournament_winner_score', 0.0))
                seen_strategies = set()
                qualified_winners = []
                rejected = []

                for winner, score, start_time in winners:
                    score = float(score or 0.0)
                    if winner in seen_strategies:
                        continue
                    if score >= min_tournament_score:
                        qualified_winners.append((winner, score))
                        seen_strategies.add(winner)
                    else:
                        rejected.append((winner, score))

                self.logger.info(
                    f"Found {len(qualified_winners)} qualified strategies from recent tournaments "
                    f"(tournament-sourced, min_score={min_tournament_score:.4f}, rejected={len(rejected)})"
                )
                return qualified_winners
                
        except Exception as e:
            self.logger.error(f"Failed to get tournament winners: {e}")
            return []

    def _is_final_exit_status(self, status: str) -> bool:
        """Return True only for statuses that mean the broker exit is filled/closed."""
        return str(status or '').lower() in {
            'filled', 'closed', 'done_for_day', 'calculated'
        }

    def _is_pending_exit_status(self, status: str) -> bool:
        return str(status or '').lower() in {
            'new', 'accepted', 'accepted_for_bidding', 'pending_new',
            'partially_filled', 'pending_cancel', 'pending_replace'
        }

    def _mark_positions_exit_submitted(self, symbol: str, strategy: str,
                                       exit_order_id: str, exit_fill_status: str,
                                       exit_reason: str = ''):
        """Persist an accepted-but-not-filled exit without closing the position."""
        try:
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE positions SET exit_order_id=?, exit_fill_status=?, "
                    "exit_reason=COALESCE(NULLIF(exit_reason, ''), ?), "
                    "updated_at=CURRENT_TIMESTAMP "
                    "WHERE symbol=? AND strategy=? AND status='open'",
                    (exit_order_id, exit_fill_status, exit_reason, symbol, strategy)
                )
                conn.commit()
        except Exception as exc:
            self.logger.warning(
                "[ExitReconcile] Could not mark pending exit for %s/%s: %s",
                symbol, strategy, exc,
            )

    def _has_pending_exit(self, symbol: str, strategy: str = None) -> bool:
        """Avoid submitting duplicate closes while a broker exit order is pending."""
        try:
            db_path = self.position_manager.data_dir / 'positions.db'
            params = [symbol]
            strategy_clause = ''
            if strategy:
                strategy_clause = ' AND strategy=?'
                params.append(strategy)
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT exit_fill_status FROM positions "
                    "WHERE symbol=? AND status='open' "
                    "AND exit_order_id IS NOT NULL" + strategy_clause +
                    " ORDER BY updated_at DESC LIMIT 1",
                    params
                ).fetchone()
            return bool(row and self._is_pending_exit_status(row[0]))
        except Exception as exc:
            self.logger.warning("[ExitReconcile] Pending-exit check failed for %s: %s", symbol, exc)
            return False

    def _is_symbol_in_cooldown(self, symbol: str) -> bool:
        """Block churny same-symbol re-entry after recent closes."""
        try:
            now = datetime.now()
            normal_cutoff = (now - timedelta(
                hours=float(self.config.get('symbol_cooldown_hours', 24))
            )).isoformat()
            loss_cutoff = (now - timedelta(
                days=float(self.config.get('loss_cooldown_days', 7))
            )).isoformat()
            db_path = self.position_manager.data_dir / "positions.db"
            with sqlite3.connect(db_path) as conn:
                recent_close = conn.execute(
                    "SELECT exit_reason, realized_pnl, exit_time FROM positions "
                    "WHERE symbol=? AND status='closed' AND exit_time >= ? "
                    "ORDER BY exit_time DESC LIMIT 1",
                    (symbol, normal_cutoff)
                ).fetchone()
                recent_loss = conn.execute(
                    "SELECT exit_reason, realized_pnl, exit_time FROM positions "
                    "WHERE symbol=? AND status='closed' AND exit_time >= ? "
                    "AND UPPER(COALESCE(exit_reason, '')) IN ('STOP_LOSS', 'TRAILING_STOP') "
                    "AND COALESCE(realized_pnl, 0) <= 0 "
                    "ORDER BY exit_time DESC LIMIT 1",
                    (symbol, loss_cutoff)
                ).fetchone()
            if recent_loss:
                self.logger.info(
                    "[SymbolCooldown] Skipping buy for %s: losing %s close within %.0f days",
                    symbol, recent_loss[0], float(self.config.get('loss_cooldown_days', 7)),
                )
                return True
            if recent_close:
                self.logger.info(
                    "[SymbolCooldown] Skipping buy for %s: closed within %.0f hours",
                    symbol, float(self.config.get('symbol_cooldown_hours', 24)),
                )
                return True
            return False
        except Exception as exc:
            self.logger.warning("[SymbolCooldown] Could not check cooldown for %s: %s", symbol, exc)
            return False

    def _reconcile_trade_logger_close(self, symbol: str, strategy: str, side: str,
                                      quantity: float, entry_price: float,
                                      entry_time: str, exit_price: float,
                                      exit_time: str, exit_reason: str):
        trade_logger = getattr(self.position_manager, 'trade_logger', None)
        if not trade_logger:
            return
        try:
            if hasattr(trade_logger, 'log_position_close'):
                trade_logger.log_position_close(
                    symbol=symbol,
                    strategy=strategy,
                    side=side,
                    quantity=quantity,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    entry_time=entry_time,
                    exit_time=exit_time,
                    exit_reason=exit_reason or 'broker_reconciled',
                )
            else:
                trade_logger.log_close(
                    symbol=symbol,
                    strategy=strategy,
                    exit_price=exit_price,
                    exit_reason=exit_reason or 'broker_reconciled',
                )
        except Exception as exc:
            self.logger.warning("[TradeLogger] Close reconciliation failed for %s: %s", symbol, exc)

    def _close_position_rows_with_fill(self, rows, fill_price: float,
                                       exit_status: str, exit_order_id: str = None,
                                       exit_reason: str = None):
        """Close local position rows using a broker-confirmed fill price."""
        if not rows or not fill_price or fill_price <= 0:
            return 0
        db_path = self.position_manager.data_dir / "positions.db"
        closed_count = 0
        exit_time = datetime.now().isoformat()
        with sqlite3.connect(db_path) as conn:
            for row in rows:
                pos_id, symbol, strategy, side, qty, entry_price, entry_time, existing_reason = row
                pnl = (fill_price - entry_price) * qty if side == "long" else (entry_price - fill_price) * qty
                reason = exit_reason or existing_reason or 'broker_reconciled'
                conn.execute(
                    "UPDATE positions SET exit_time=?, exit_price=?, realized_pnl=?, "
                    "status='closed', exit_order_id=COALESCE(?, exit_order_id), "
                    "exit_fill_status=?, exit_reason=?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=?",
                    (exit_time, fill_price, pnl, exit_order_id, exit_status, reason, pos_id)
                )
                self._reconcile_trade_logger_close(
                    symbol=symbol,
                    strategy=strategy,
                    side=side,
                    quantity=qty,
                    entry_price=entry_price,
                    entry_time=entry_time,
                    exit_price=fill_price,
                    exit_time=exit_time,
                    exit_reason=reason,
                )
                closed_count += 1
            conn.commit()
        return closed_count

    def _reconcile_exit_fills(self, remote_symbols: set = None) -> int:
        """Update pending/local exit rows from broker order state and positions reality."""
        if getattr(self.alpaca, 'demo_mode', False):
            return 0
        try:
            db_path = self.position_manager.data_dir / "positions.db"
            with sqlite3.connect(db_path) as conn:
                pending_rows = conn.execute(
                    "SELECT id, symbol, strategy, side, quantity, entry_price, entry_time, "
                    "COALESCE(exit_reason, ''), exit_order_id, COALESCE(exit_fill_status, '') "
                    "FROM positions "
                    "WHERE exit_order_id IS NOT NULL "
                    "AND (status='open' OR LOWER(COALESCE(exit_fill_status, '')) NOT IN ('filled', 'closed'))"
                ).fetchall()

            reconciled = 0
            for row in pending_rows:
                (
                    pos_id, symbol, strategy, side, qty, entry_price, entry_time,
                    exit_reason, exit_order_id, exit_fill_status,
                ) = row
                order = {}
                if exit_order_id and not str(exit_order_id).startswith('alpaca_close_position:'):
                    order = self.alpaca.get_order(exit_order_id) or {}

                order_status = str(order.get('status') or exit_fill_status or '').lower()
                fill_price = (
                    order.get('filled_avg_price')
                    or order.get('avg_fill_price')
                    or order.get('fill_price')
                    or None
                )
                try:
                    fill_price = float(fill_price) if fill_price else None
                except Exception:
                    fill_price = None

                broker_position_gone = remote_symbols is not None and symbol not in remote_symbols
                if self._is_final_exit_status(order_status) and fill_price and fill_price > 0:
                    rows = [(pos_id, symbol, strategy, side, qty, entry_price, entry_time, exit_reason)]
                    reconciled += self._close_position_rows_with_fill(
                        rows, fill_price, order_status, exit_order_id, exit_reason
                    )
                elif broker_position_gone:
                    if not fill_price:
                        try:
                            quote = self.alpaca.get_quote(symbol)
                            fill_price = float(quote.last) if quote and quote.last else None
                        except Exception:
                            fill_price = None
                    if fill_price and fill_price > 0:
                        rows = [(pos_id, symbol, strategy, side, qty, entry_price, entry_time, exit_reason)]
                        reconciled += self._close_position_rows_with_fill(
                            rows,
                            fill_price,
                            'broker_reconciled_closed',
                            exit_order_id,
                            exit_reason or 'broker_reconciled',
                        )
                else:
                    with sqlite3.connect(db_path) as conn:
                        conn.execute(
                            "UPDATE positions SET exit_fill_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (order_status or exit_fill_status, pos_id)
                        )
                        conn.commit()

            if reconciled:
                self.logger.info("[ExitReconcile] Broker-reconciled %d pending exit row(s)", reconciled)
            return reconciled
        except Exception as exc:
            self.logger.error("[ExitReconcile] Failed: %s", exc)
            return 0

    def _accounting_divergence(self) -> Optional[Dict]:
        """Compare local position P&L against broker equity truth."""
        try:
            state = self.position_manager.get_portfolio_state()
            if state.balance_synced_at is None:
                return None
            db_path = self.position_manager.data_dir / "positions.db"
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(realized_pnl), 0), COALESCE(SUM(CASE WHEN status='open' THEN unrealized_pnl ELSE 0 END), 0) "
                    "FROM positions"
                ).fetchone()
            local_pnl = float((row[0] or 0) + (row[1] or 0))
            broker_pnl = float(state.total_value - self.position_manager.config['initial_balance'])
            diff = local_pnl - broker_pnl
            threshold = max(
                float(self.config.get('accounting_divergence_warn_usd', 2.0)),
                abs(float(state.total_value or 0)) * 0.005,
            )
            return {
                'local_pnl': local_pnl,
                'broker_pnl': broker_pnl,
                'diff': diff,
                'threshold': threshold,
                'warn': abs(diff) > threshold,
                'synced_at': state.balance_synced_at,
            }
        except Exception as exc:
            self.logger.warning("[Accounting] Divergence check failed: %s", exc)
            return None
    
    def generate_trading_signals(self, symbol: str, strategy_name: str) -> Optional[Dict]:
        """Generate trading signals for a symbol using a specific strategy.
        
        Multi-timeframe: fetches both 1H (signal) and 1Day (trend) data.
        Daily trend is passed to strategy logic for confirmation.
        """
        try:
            # Get 1H market data (primary signal timeframe)
            data = self.alpaca.get_historical_data(symbol, days=500, timeframe='1Hour')
            if data is None or len(data) < 20:
                self.logger.warning(f"Insufficient data for {symbol}")
                return None
            
            # Data source validation (Task 18): reject demo/synthetic data in paper trader
            data_source = getattr(data, 'attrs', {}).get('data_source', 'real')
            if data_source in ('demo_mode', 'demo_fallback'):
                self.logger.warning(
                    f"[DataGuard] Rejecting {symbol}: data source is '{data_source}'. "
                    f"Reason: {getattr(data, 'attrs', {}).get('fallback_reason', 'N/A')}")
                return None
            
            # Get daily data for trend confirmation (Task 14)
            daily_trend = 'unknown'
            try:
                daily_data = self.alpaca.get_historical_data(symbol, days=200, timeframe='1Day')
                if daily_data is not None and len(daily_data) >= 50:
                    daily_sma50 = daily_data['close'].rolling(50).mean()
                    daily_sma20 = daily_data['close'].rolling(20).mean()
                    latest_price = float(daily_data['close'].iloc[-1])
                    latest_sma50 = float(daily_sma50.iloc[-1]) if not pd.isna(daily_sma50.iloc[-1]) else None
                    latest_sma20 = float(daily_sma20.iloc[-1]) if not pd.isna(daily_sma20.iloc[-1]) else None
                    
                    if latest_sma50:
                        # Check SMA50 slope (rising/flat/falling)
                        prev_sma50 = float(daily_sma50.iloc[-5]) if not pd.isna(daily_sma50.iloc[-5]) else latest_sma50
                        sma50_slope = (latest_sma50 - prev_sma50) / prev_sma50
                        
                        if latest_price > latest_sma50 and sma50_slope > -0.001:
                            daily_trend = 'bullish'
                        elif latest_price < latest_sma50 * 0.97:
                            daily_trend = 'bearish'
                        else:
                            daily_trend = 'neutral'
                        
                        self.logger.debug(
                            f"[MTF] {symbol}: daily trend={daily_trend}, "
                            f"price=${latest_price:.2f}, SMA50=${latest_sma50:.2f}, "
                            f"slope={sma50_slope*100:.3f}%")
            except Exception as _mte:
                self.logger.debug(f"[MTF] Daily data fetch failed for {symbol}: {_mte}")
            
            # Calculate technical indicators using module-level import
            indicators = TechnicalIndicators()
            indicators.data = data
            
            # Calculate all indicators
            indicators_data = indicators.calculate_all(data)
            
            # Apply strategy-specific logic with daily trend context
            signal = self._apply_strategy_logic(strategy_name, data, indicators_data, 
                                                symbol=symbol, daily_trend=daily_trend)
            
            if signal:
                signal['symbol'] = symbol
                signal['strategy'] = strategy_name
                signal['timestamp'] = datetime.now().isoformat()
                signal['current_price'] = float(data.iloc[-1]['close'])
                learning = self._get_learning_adjustment(symbol, strategy_name)
                signal['learning_adjustment'] = learning
                if signal.get('action') == 'buy' and not learning.get('tradeable', True):
                    self.logger.info(
                        '[Learning] Blocking %s/%s before entry: %s',
                        symbol, strategy_name, learning.get('reason')
                    )
                    return None
                multiplier = float(learning.get('confidence_multiplier', 1.0) or 1.0)
                original_conf = float(signal.get('confidence', 0.0) or 0.0)
                if signal.get('action') == 'buy':
                    signal['confidence'] = max(0.0, min(0.99, original_conf * multiplier))
                if signal.get('action') == 'buy' and multiplier != 1.0:
                    signal['reason'] = signal.get('reason', '') + (
                        ' [learning: conf %.2f→%.2f, size x%.2f]' % (
                            original_conf,
                            signal['confidence'],
                            float(learning.get('size_multiplier', 1.0) or 1.0),
                        )
                    )
                
            return signal
            
        except Exception as e:
            self.logger.error(f"Failed to generate signal for {symbol} using {strategy_name}: {e}")
            return None
    
    def _apply_strategy_logic(self, strategy_name: str, data: pd.DataFrame, 
                            indicators_data: Dict, symbol: str = '',
                            daily_trend: str = 'unknown') -> Optional[Dict]:
        """Apply specific strategy logic to generate buy/sell signals.
        
        Args:
            daily_trend: 'bullish', 'bearish', 'neutral', or 'unknown' from daily timeframe
        """
        
        # Get the latest values
        current_price = float(data.iloc[-1]['close'])
        prev_price = float(data.iloc[-2]['close'])
        
        signal = None
        
        if strategy_name == 'MACD Crossover':
            # Compute MACD directly from raw price data (avoids format mismatch with indicators_data)
            try:
                import talib as _talib
                import numpy as _np
                close_arr = data['close'].astype(float).values
                if len(close_arr) >= 35:
                    macd_line, signal_line, histogram = _talib.MACD(close_arr, fastperiod=12, slowperiod=26, signalperiod=9)
                    valid_hist = [(i, v) for i, v in enumerate(histogram) if not _np.isnan(v)]
                    if len(valid_hist) >= 2:
                        prev_histogram = valid_hist[-2][1]
                        current_histogram = valid_hist[-1][1]
                        if current_histogram > 0 and prev_histogram <= 0:
                            signal = {
                                'action': 'buy',
                                'side': 'long',
                                'confidence': 0.75,
                                'reason': f'MACD bullish crossover (hist: {current_histogram:.4f})'
                            }
                        elif current_histogram < 0 and prev_histogram >= 0:
                            signal = {
                                'action': 'sell',
                                'side': 'long',
                                'confidence': 0.70,
                                'reason': f'MACD bearish crossover (hist: {current_histogram:.4f})'
                            }
            except Exception as macd_err:
                self.logger.debug(f"MACD signal calc failed for {symbol}: {macd_err}")
        
        elif strategy_name == 'RSI Mean Reversion':
            # Get RSI data from indicators_data dict
            indicators_dict = indicators_data.get("indicators", {})
            rsi_value = indicators_dict.get("rsi", None)
            
            if rsi_value is not None:
                current_rsi = rsi_value if isinstance(rsi_value, (int, float)) else rsi_value.iloc[-1] if isinstance(rsi_value, pd.DataFrame) else None
                
                if current_rsi is not None:
                    # RSI thresholds from per-symbol params (cluster or champion)
                    _sym_params = self._get_params_for_symbol(symbol)
                    oversold_thresh = _sym_params.get("oversold", 33)
                    overbought_thresh = _sym_params.get("overbought", 70)
                    if current_rsi < oversold_thresh:
                        # MULTI-TIMEFRAME FILTER (Task 14): reject buys in daily downtrends
                        if daily_trend == 'bearish':
                            self.logger.info(
                                f"[MTF] Skipping RSI buy for {symbol}: daily trend is bearish")
                            return None
                        
                        # TREND REGIME FILTER: only buy if price is near or above its 50-bar SMA
                        # Prevents buying sustained downtrends (e.g. ADBE -41%)
                        sma50 = indicators_dict.get("sma50") or indicators_dict.get("sma_50")
                        in_downtrend = False
                        if sma50 is not None:
                            sma50_val = float(sma50) if isinstance(sma50, (int, float)) else (sma50.iloc[-1] if hasattr(sma50, "iloc") else None)
                            current_price = indicators_dict.get("current_price") or indicators_dict.get("close")
                            if sma50_val and current_price:
                                price_val = float(current_price) if isinstance(current_price, (int, float)) else (current_price.iloc[-1] if hasattr(current_price, "iloc") else None)
                                if price_val and price_val < sma50_val * 0.97:
                                    in_downtrend = True
                                    self.logger.info(f"[TrendFilter] Skipping RSI buy for {symbol}: price {price_val:.2f} < SMA50 {sma50_val:.2f} * 0.97 (downtrend)")
                        if not in_downtrend:
                            # VOLUME CONFIRMATION: adjust confidence based on volume
                            # High volume = more conviction (capitulation). Low = drift.
                            vol_boost = 0.0
                            vol_tag = ""
                            try:
                                if len(data) > 20:
                                    recent_vol = float(data.iloc[-1].get('volume', 0) if hasattr(data.iloc[-1], 'get') else data['volume'].iloc[-1])
                                    avg_vol = float(data['volume'].tail(20).mean())
                                    if avg_vol > 0:
                                        vol_ratio = recent_vol / avg_vol
                                        if vol_ratio >= 1.5:
                                            vol_boost = 0.05  # High volume = capitulation
                                            vol_tag = ", high vol"
                                        elif vol_ratio >= 1.2:
                                            vol_boost = 0.02
                                            vol_tag = ", vol confirmed"
                                        elif vol_ratio < 0.5:
                                            vol_boost = -0.10  # Very low = reduce confidence
                                            vol_tag = ", LOW vol"
                            except Exception as vol_err:
                                self.logger.debug(f"Volume adjustment unavailable for {symbol}: {vol_err}")
                            
                            base_conf = min(0.80, (oversold_thresh - current_rsi) / oversold_thresh + 0.60)
                            adj_conf = max(0.50, min(0.85, base_conf + vol_boost))
                            signal = {
                                "action": "buy",
                                "side": "long",
                                "confidence": adj_conf,
                                "reason": "RSI oversold: %.1f%s" % (current_rsi, vol_tag)
                            }
                    # Overbought condition
                    elif current_rsi > overbought_thresh:
                        signal = {
                            "action": "sell",
                            "side": "long",
                            "confidence": min(0.80, (current_rsi - overbought_thresh) / overbought_thresh + 0.60),
                            "reason": f"RSI overbought: {current_rsi:.1f}"
                        }
        
        elif strategy_name == 'Bollinger Bounce':
            # Bollinger data may come in either shape:
            # 1) Legacy/DataFrame: indicators[bollinger_bands] with upper_band/lower_band columns
            # 2) Current dict from TechnicalIndicators.calculate_all(): indicators[bollinger]
            indicators_dict = indicators_data.get("indicators", {})

            upper_band = float('inf')
            lower_band = float('-inf')

            bb_dict = indicators_dict.get("bollinger")
            if isinstance(bb_dict, dict):
                upper_band = bb_dict.get('upper', upper_band)
                lower_band = bb_dict.get('lower', lower_band)

            bb_data = indicators_dict.get("bollinger_bands")
            if isinstance(bb_data, pd.DataFrame) and len(bb_data) >= 1:
                current_bb = bb_data.iloc[-1]
                upper_band = current_bb.get('upper_band', upper_band) if hasattr(current_bb, 'get') else current_bb['upper_band']
                lower_band = current_bb.get('lower_band', lower_band) if hasattr(current_bb, 'get') else current_bb['lower_band']

            # Normalize values and ignore malformed bands
            try:
                upper_band = float(upper_band)
                lower_band = float(lower_band)
            except Exception:
                upper_band = float('inf')
                lower_band = float('-inf')

            # Price near lower band (buy signal)
            if pd.notna(lower_band) and current_price <= lower_band * 1.02:
                signal = {
                    'action': 'buy',
                    'side': 'long',
                    'confidence': 0.72,
                    'reason': 'Price near Bollinger lower band'
                }
            # Price near upper band (sell signal)
            elif pd.notna(upper_band) and current_price >= upper_band * 0.98:
                signal = {
                    'action': 'sell',
                    'side': 'long',
                    'confidence': 0.68,
                    'reason': 'Price near Bollinger upper band'
                }

        elif strategy_name == 'VWAP Reversion':
            try:
                if len(data) >= 20:
                    # Calculate VWAP (cumulative volume-weighted price for today's session)
                    # For 1H bars, use rolling 20-bar VWAP as proxy
                    typical_price = (data['high'] + data['low'] + data['close']) / 3
                    cumvol = data['volume'].rolling(20).sum()
                    cumtp = (typical_price * data['volume']).rolling(20).sum()
                    vwap = cumtp / cumvol
                    
                    current_vwap = float(vwap.iloc[-1])
                    if current_vwap > 0 and not pd.isna(current_vwap):
                        deviation = (current_price - current_vwap) / current_vwap
                        
                        # Buy when price drops 1%+ below VWAP
                        if deviation < -0.01:
                            vol_ratio = 1.0
                            try:
                                recent_vol = float(data['volume'].iloc[-1])
                                avg_vol = float(data['volume'].tail(20).mean())
                                vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0
                            except Exception as vol_err:
                                self.logger.debug(f"VWAP volume ratio unavailable for {symbol}: {vol_err}")
                            
                            # Higher confidence with bigger deviation and higher volume
                            conf = min(0.80, 0.60 + abs(deviation) * 5)
                            if vol_ratio >= 1.5:
                                conf = min(0.85, conf + 0.05)
                            
                            signal = {
                                'action': 'buy',
                                'side': 'long',
                                'confidence': conf,
                                'reason': 'VWAP reversion: price %.1f%% below VWAP $%.2f' % (deviation*100, current_vwap)
                            }
                        # Sell when price rises 1%+ above VWAP
                        elif deviation > 0.01:
                            signal = {
                                'action': 'sell',
                                'side': 'long',
                                'confidence': min(0.75, 0.55 + abs(deviation) * 5),
                                'reason': 'VWAP reversion: price %.1f%% above VWAP $%.2f' % (deviation*100, current_vwap)
                            }
            except Exception as _ve:
                self.logger.debug(f"VWAP calculation failed for {symbol}: {_ve}")

        elif strategy_name == 'Opening Range Breakout':
            # Task 16b: ORB — breakout above/below first period's range
            try:
                if len(data) >= 10:
                    # Approximate: use first bar of day as "opening range"
                    # For 1H bars, compare current bar to recent range
                    recent_high = float(data['high'].tail(5).max())
                    recent_low = float(data['low'].tail(5).min())
                    range_size = recent_high - recent_low
                    
                    if range_size > 0:
                        # Breakout above range
                        if current_price > recent_high and current_price > prev_price:
                            conf = min(0.75, 0.55 + (current_price - recent_high) / range_size * 0.3)
                            signal = {
                                'action': 'buy',
                                'side': 'long',
                                'confidence': conf,
                                'reason': 'ORB breakout above $%.2f (range=$%.2f)' % (recent_high, range_size)
                            }
                        # Breakdown below range
                        elif current_price < recent_low and current_price < prev_price:
                            conf = min(0.70, 0.50 + (recent_low - current_price) / range_size * 0.3)
                            signal = {
                                'action': 'sell',
                                'side': 'long',
                                'confidence': conf,
                                'reason': 'ORB breakdown below $%.2f (range=$%.2f)' % (recent_low, range_size)
                            }
            except Exception as _oe:
                self.logger.debug(f"ORB calculation failed for {symbol}: {_oe}")

        elif strategy_name == 'Mean Reversion Pairs':
            # Task 16d: Pairs trading — trade ratio deviation in correlated pairs
            try:
                # Find the paired symbol from correlation groups
                corr_groups = self.config.get('correlation_groups', {})
                paired_symbol = None
                for group_name, group_symbols in corr_groups.items():
                    if symbol in group_symbols:
                        # Pick the first other symbol in the group
                        others = [s for s in group_symbols if s != symbol]
                        if others:
                            paired_symbol = others[0]
                        break
                
                if paired_symbol:
                    pair_data = self.alpaca.get_historical_data(paired_symbol, days=100, timeframe='1Hour')
                    if pair_data is not None and len(pair_data) >= 20:
                        # Align data by index
                        common_idx = data.index.intersection(pair_data.index)
                        if len(common_idx) >= 20:
                            sym_prices = data.loc[common_idx, 'close']
                            pair_prices = pair_data.loc[common_idx, 'close']
                            
                            # Calculate price ratio
                            ratio = sym_prices / pair_prices
                            ratio_mean = ratio.rolling(20).mean()
                            ratio_std = ratio.rolling(20).std()
                            
                            current_ratio = float(ratio.iloc[-1])
                            mean_ratio = float(ratio_mean.iloc[-1])
                            std_ratio = float(ratio_std.iloc[-1])
                            
                            if std_ratio > 0 and not pd.isna(mean_ratio):
                                z_score = (current_ratio - mean_ratio) / std_ratio
                                
                                # Buy when underperforming (z < -2)
                                if z_score < -2.0:
                                    signal = {
                                        'action': 'buy',
                                        'side': 'long',
                                        'confidence': min(0.75, 0.55 + abs(z_score) * 0.05),
                                        'reason': 'Pairs: %s/%s z=%.2f (underperforming)' % (symbol, paired_symbol, z_score)
                                    }
                                # Sell when overperforming (z > 2)
                                elif z_score > 2.0:
                                    signal = {
                                        'action': 'sell',
                                        'side': 'long',
                                        'confidence': min(0.70, 0.50 + abs(z_score) * 0.05),
                                        'reason': 'Pairs: %s/%s z=%.2f (overperforming)' % (symbol, paired_symbol, z_score)
                                    }
            except Exception as _pe:
                self.logger.debug(f"Pairs calculation failed for {symbol}: {_pe}")

        elif strategy_name == 'Confluence':
            # Task 1303: Multi-indicator confluence strategy
            # Uses TechnicalIndicators.calculate_all() confluence_score + signal agreement
            try:
                conf_score = indicators_data.get('confluence_score', 0.0)
                signals = indicators_data.get('signals', {})

                # Count agreeing signals (same approach as scanner.detect_confluence)
                positive = sum([
                    1 if signals.get('rsi', 0) >= 0 else 0,
                    1 if signals.get('macd', 0) > 0 else 0,
                    1 if float(signals.get('bollinger', 0)) > 0 else 0,
                    1 if signals.get('supertrend', 0) > 0 else 0,
                    1 if signals.get('vwap', 0) > 0 else 0,
                ])
                negative = sum([
                    1 if signals.get('rsi', 0) < 0 else 0,
                    1 if signals.get('macd', 0) < 0 else 0,
                    1 if float(signals.get('bollinger', 0)) < 0 else 0,
                    1 if signals.get('supertrend', 0) < 0 else 0,
                    1 if signals.get('vwap', 0) < 0 else 0,
                ])

                rsi_val = indicators_data.get('indicators', {}).get('rsi', 50)

                # BUY: confluence_score > 0.2 AND at least 3/5 signals positive
                if conf_score > 0.2 and positive >= 3:
                    # Multi-timeframe filter: skip buys in daily downtrends
                    if daily_trend == 'bearish':
                        self.logger.info(
                            f"[MTF] Skipping Confluence buy for {symbol}: daily trend bearish")
                    else:
                        # Confidence scales with confluence score and signal agreement
                        base_conf = 0.55 + conf_score * 0.25 + (positive - 3) * 0.05
                        signal = {
                            'action': 'buy',
                            'side': 'long',
                            'confidence': min(0.85, base_conf),
                            'reason': (f"Confluence bullish: score={conf_score:.3f}, "
                                      f"{positive}/5 signals positive, RSI={rsi_val:.1f}")
                        }

                # SELL: confluence_score < -0.2 AND at least 3/5 signals negative
                elif conf_score < -0.2 and negative >= 3:
                    base_conf = 0.55 + abs(conf_score) * 0.25 + (negative - 3) * 0.05
                    signal = {
                        'action': 'sell',
                        'side': 'long',
                        'confidence': min(0.80, base_conf),
                        'reason': (f"Confluence bearish: score={conf_score:.3f}, "
                                  f"{negative}/5 signals negative, RSI={rsi_val:.1f}")
                    }
            except Exception as _ce:
                self.logger.debug(f"Confluence calculation failed for {symbol}: {_ce}")

                # Add more strategy implementations as needed...
        
        return signal
    
    def execute_signal(self, signal: Dict) -> bool:
        """Execute a trading signal"""
        # Task 890: daily loss circuit breaker guard
        if getattr(self, '_daily_loss_limit_reached', False):
            self.logger.info('[CircuitBreaker] Daily loss limit active — execute_signal blocked.')
            return False
        try:
            symbol = signal['symbol']
            strategy = signal['strategy']
            action = signal['action']
            side = signal['side']
            current_price = signal['current_price']
            confidence = signal['confidence']
            learning = signal.get('learning_adjustment') or self._get_learning_adjustment(symbol, strategy)
            if action == 'buy' and not learning.get('tradeable', True):
                self.logger.info(
                    '[Learning] Skipping buy for %s/%s: %s',
                    symbol, strategy, learning.get('reason')
                )
                return False
            
            # Per-symbol position limit: skip buy if symbol already has an open position
            # (prevents buying the same stock repeatedly after losses, e.g. ADBE x3)
            if action == 'buy':
                try:
                    db_path = self.position_manager.data_dir / "positions.db"
                    with sqlite3.connect(db_path) as _pc:
                        existing = _pc.execute(
                            "SELECT COUNT(*) FROM positions WHERE symbol=? AND status='open'",
                            (symbol,)
                        ).fetchone()[0]
                    if existing > 0:
                        self.logger.info(
                            f"[PositionLimit] Skipping buy for {symbol}: already has {existing} open position(s)"
                        )
                        return False
                except Exception as _ple:
                    self.logger.warning(f"[PositionLimit] Could not check positions for {symbol}: {_ple}")

                if self._has_pending_exit(symbol):
                    self.logger.info(
                        f"[ExitReconcile] Skipping buy for {symbol}: broker exit order still pending"
                    )
                    return False

                if self._is_symbol_in_cooldown(symbol):
                    return False

            # 7-day stop-loss cooldown: block re-entry if symbol hit a stop-loss in the last 7 days
            # Prevents ADBE-style re-entry loops after getting stopped out
            if action == 'buy':
                try:
                    from datetime import datetime, timedelta
                    db_path = self.position_manager.data_dir / "positions.db"
                    cooldown_cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
                    with sqlite3.connect(db_path) as _cd:
                        sl_count = _cd.execute(
                            "SELECT COUNT(*) FROM positions "
                            "WHERE symbol=? AND status='closed' AND exit_reason='STOP_LOSS' "
                            "AND exit_time >= ?",
                            (symbol, cooldown_cutoff)
                        ).fetchone()[0]
                    if sl_count > 0:
                        self.logger.info(
                            f"[StopLossCooldown] Skipping buy for {symbol}: "
                            f"stop-loss triggered within last 7 days ({sl_count} time(s))"
                        )
                        return False
                except Exception as _cde:
                    self.logger.warning(f"[StopLossCooldown] Could not check cooldown for {symbol}: {_cde}")

            # Minimum cash reserve guard: if buying power falls below 15% of equity, block new buys
            if action == 'buy' and getattr(self, '_pdt_protection_active', False):
                self.logger.info(
                    "[PDT-PROTECT] Skipping buy for %s: Alpaca day-trade protection active (daytrade_count=%s)" % (
                        symbol, getattr(self, '_daytrade_count', 'unknown')
                    )
                )
                return False

            if action == "buy" and getattr(self, "_alpaca_synced", False) and hasattr(self, "_real_buying_power") and hasattr(self, "_real_equity"):
                min_cash_reserve = max(0.0, float(self._real_equity) * 0.15)
                if float(self._real_buying_power) < min_cash_reserve:
                    self.logger.info(
                        "[CashReserveGuard] Skipping buy for %s: buying_power=%.2f < 15%% equity reserve (%.2f)" % (
                            symbol, float(self._real_buying_power), min_cash_reserve
                        )
                    )
                    return False

            # Calculate position size
            quantity = self.position_manager.calculate_position_size(symbol, strategy, current_price)
            size_multiplier = float(learning.get('size_multiplier', 1.0) or 1.0)
            if action == 'buy' and size_multiplier != 1.0:
                adjusted_quantity = round(quantity * size_multiplier, 6)
                self.logger.info(
                    '[Learning] Position size adjusted for %s/%s: %.6f -> %.6f (%s)',
                    symbol, strategy, quantity, adjusted_quantity, learning.get('reason')
                )
                quantity = adjusted_quantity

            # Hard cap: each position must be <= 15% of real equity when Alpaca is synced
            if action == "buy" and getattr(self, "_alpaca_synced", False) and hasattr(self, "_real_equity") and current_price > 0:
                max_by_equity = (float(self._real_equity) * 0.15) / current_price
                if quantity > max_by_equity:
                    self.logger.info(
                        "[PositionCap] Capping %s qty from %.4f to %.4f (15%% of real equity %.2f)" % (
                            symbol, quantity, max_by_equity, float(self._real_equity)
                        )
                    )
                    quantity = round(max_by_equity, 6)

            # Cap by real Alpaca buying power (prevents insufficient buying power errors)
            if getattr(self, "_alpaca_synced", False) and hasattr(self, "_real_buying_power"):
                max_affordable = self._real_buying_power / current_price * 0.95  # 5% safety margin
                if quantity > max_affordable:
                    self.logger.info(
                        "Capping %s qty from %.4f to %.4f (real buying power: %.2f)" % (
                            symbol, quantity, max_affordable, float(self._real_buying_power)
                        )
                    )
                    quantity = round(max_affordable, 6)

            if quantity <= 0:
                self.logger.info(f"No position size available for {symbol} {strategy}")
                return False

            # Alpaca minimum order: notional must be >= $1
            notional = quantity * current_price
            if notional < 1.0:
                self.logger.info(f"Order too small for {symbol}: ${notional:.2f} notional < $1.00 minimum. Skipping.")
                return False
            
            # Execute the trade
            if action == 'buy':
                success = self._execute_buy_order(symbol, strategy, side, quantity, current_price,
                                                   entry_reason=signal.get('reason', ''))
            else:  # sell/close
                success = self._execute_sell_order(symbol, strategy, current_price)
            

            if success:
                self.logger.info(f"Executed {action} order: {quantity} {symbol} @ ${current_price:.2f} (Strategy: {strategy}, Confidence: {confidence:.2f})")
                # Fire trade alert
                if self.alert_manager:
                    try:
                        self.alert_manager.fire(
                            AlertType.TRADE_EXECUTED,
                            symbol=symbol,
                            action=action,
                            quantity=quantity,
                            price=round(current_price, 2),
                            strategy=strategy,
                            confidence=confidence,
                        )
                    except Exception as _ae:
                        self.logger.warning(f"Alert dispatch failed (non-fatal): {_ae}")
            
            return success
            
        except Exception as e:
            self.logger.error(f"Failed to execute signal: {e}")
            return False
    
    def _execute_buy_order(self, symbol: str, strategy: str, side: str, 
                          quantity: float, price: float, entry_reason: str = '') -> bool:
        """Execute a buy order with trade journal entry"""
        try:
            # Place order with Alpaca (or simulate in demo mode)
            order_result = self.alpaca.place_paper_trade(
                symbol=symbol,
                quantity=round(quantity, 6),  # fractional shares supported
                side='buy'
            )
            
            if order_result and order_result.get('status') in ['filled', 'accepted']:
                # Record position
                fill_price = order_result.get('fill_price') or price
                self.logger.info(
                    f"[BuyOrder] Order accepted: {symbol} qty={quantity:.4f} "
                    f"@ ${fill_price:.2f} status={order_result.get('status')}"
                )
                success = self.position_manager.open_position(
                    symbol=symbol,
                    strategy=strategy,
                    side=side,
                    quantity=quantity,
                    entry_price=fill_price,
                    entry_order_id=(
                        order_result.get('order_id')
                        or order_result.get('id')
                        or order_result.get('client_order_id')
                    ),
                    entry_fill_status=order_result.get('status', 'filled')
                )
                if not success:
                    self.logger.error(f"[BuyOrder] position_manager.open_position FAILED for {symbol}")
                else:
                    # Trade journal: save entry reason (Task 25)
                    try:
                        db_path = self.position_manager.data_dir / "positions.db"
                        with sqlite3.connect(db_path) as _jc:
                            _jc.execute(
                                "UPDATE positions SET entry_reason=? "
                                "WHERE symbol=? AND strategy=? AND status='open' "
                                "ORDER BY entry_time DESC LIMIT 1",
                                (entry_reason, symbol, strategy))
                            _jc.commit()
                    except Exception as journal_err:
                        self.logger.warning(f"Failed to persist entry_reason for {symbol}: {journal_err}")
                return success
            
            # Log WHY the order failed
            if order_result:
                err = order_result.get('error', 'unknown')
                status = order_result.get('status', 'no_status')
                status_code = order_result.get('status_code', '?')
                self.logger.error(
                    f"[BuyOrder] FAILED {symbol}: status={status}, "
                    f"http={status_code}, error={err}"
                )
            else:
                self.logger.error(f"[BuyOrder] FAILED {symbol}: no order_result returned")
            return False
            
        except Exception as e:
            self.logger.error(f"Failed to execute buy order: {e}")
            return False
    
    def _execute_sell_order(self, symbol: str, strategy: str, price: float,
                          force: bool = False) -> bool:
        """
        Execute a sell order (close position). Respects min_hold_hours to avoid PDT.
        Uses DELETE /v2/positions/{symbol} (close_full_position) for reliability with fractional shares.
        Closes ALL open local DB positions for this symbol+strategy.
        """
        try:
            db_path = self.position_manager.data_dir / "positions.db"

            # Optional hold-time guard: keep disabled for paper trading so exits can actually execute.
            min_hold = self.config.get('min_hold_hours', 24)
            enforce_min_hold = self.config.get('enforce_min_hold_hours', False)
            if enforce_min_hold and min_hold > 0 and not force:
                try:
                    with sqlite3.connect(db_path) as conn:
                        recent_entry = conn.execute(
                            "SELECT entry_time FROM positions WHERE symbol=? AND strategy=? AND status='open' "
                            "ORDER BY entry_time DESC LIMIT 1",
                            (symbol, strategy)
                        ).fetchone()
                        if recent_entry and recent_entry[0] and isinstance(recent_entry[0], str):
                            entry_dt = datetime.fromisoformat(recent_entry[0])
                            hours_held = (datetime.now() - entry_dt).total_seconds() / 3600
                            if hours_held < min_hold:
                                self.logger.info(
                                    f"[PDT-GUARD] Skipping close {symbol} ({strategy}) - "
                                    f"held {hours_held:.1f}h < {min_hold}h minimum"
                                )
                                self._last_close_blocked = (symbol, strategy, 'PDT_GUARD')
                                return False
                except (TypeError, ValueError) as e:
                    self.logger.debug(f"[PDT-GUARD] Could not check hold time: {e}")

            with sqlite3.connect(db_path) as conn:
                open_count = conn.execute(
                    "SELECT COUNT(*) FROM positions WHERE symbol=? AND strategy=? AND status=?",
                    (symbol, strategy, "open")
                ).fetchone()[0]

            if open_count == 0:
                self.logger.debug(f"No open position to close for {symbol} {strategy}")
                return False

            if self._has_pending_exit(symbol, strategy):
                self.logger.info(
                    f"[ExitReconcile] Close already pending for {symbol} ({strategy}); not submitting duplicate exit"
                )
                return True

            # Use DELETE /v2/positions/{symbol} — closes full Alpaca position, handles fractional shares
            order_result = self.alpaca.close_full_position(symbol)

            if order_result and "error" not in order_result:
                fill_price = order_result.get("fill_price") or price
                exit_order_id = order_result.get('order_id') or order_result.get('id') or order_result.get('client_order_id')
                exit_fill_status = order_result.get('status') or ('closed' if order_result.get('broker_verified') else None)

                if not self._is_final_exit_status(exit_fill_status):
                    if exit_order_id:
                        self._mark_positions_exit_submitted(
                            symbol=symbol,
                            strategy=strategy,
                            exit_order_id=exit_order_id,
                            exit_fill_status=exit_fill_status or 'pending_new',
                        )
                        self.logger.warning(
                            f"[ExitReconcile] Exit submitted for {symbol} but not filled yet "
                            f"(order_id={exit_order_id}, status={exit_fill_status}); local position stays open."
                        )
                        self._last_close_blocked = (symbol, strategy, 'EXIT_PENDING')
                        return False
                    if not order_result.get('broker_verified'):
                        self.logger.error(
                            f"[ExitReconcile] Refusing to close {symbol}: broker did not return final fill/close status "
                            f"or an order id (status={exit_fill_status})"
                        )
                        return False

                # GUARD 1: zero/null price — Alpaca occasionally returns 0 (data error)
                if not fill_price or fill_price <= 0:
                    self.logger.error(
                        f"[PriceGuard] Rejecting close for {symbol}: fill_price={fill_price} is zero/null. "
                        f"Position stays open. Check Alpaca data feed."
                    )
                    return False

                # GUARD 2: sanity check — reject if fill deviates >25% from any open entry price.
                # This catches stale/cached prices from Alpaca paper trading (e.g. ADBE $425→$262).
                try:
                    with sqlite3.connect(db_path) as _g:
                        entry_prices = [r[0] for r in _g.execute(
                            "SELECT entry_price FROM positions WHERE symbol=? AND strategy=? AND status='open'",
                            (symbol, strategy)
                        ).fetchall()]
                    if entry_prices:
                        avg_entry = sum(entry_prices) / len(entry_prices)
                        deviation = abs(fill_price - avg_entry) / avg_entry
                        if deviation > 0.25:
                            self.logger.error(
                                f"[PriceGuard] Rejecting close for {symbol}: fill_price=${fill_price:.2f} "
                                f"deviates {deviation*100:.1f}% from avg entry ${avg_entry:.2f} (>25% threshold). "
                                f"Likely stale Alpaca price. Position stays open."
                            )
                            return False
                except Exception as _ge:
                    self.logger.warning(f"[PriceGuard] Could not validate price for {symbol}: {_ge}")

                self.logger.info(f"Alpaca position closed: {symbol} fill_price={fill_price}")
                if not exit_order_id and order_result.get('broker_verified'):
                    # Alpaca DELETE /v2/positions/{symbol} can successfully close a
                    # paper position while returning no order id. This is still a
                    # broker-confirmed close because the API call succeeded; persist
                    # a stable synthetic reference so reports/risk accounting do not
                    # misclassify the close as local/demo-only.
                    exit_order_id = f"alpaca_close_position:{symbol}:{datetime.now().isoformat()}"

                # Close ALL open DB positions for this symbol+strategy
                with sqlite3.connect(db_path) as conn:
                    open_positions = conn.execute(
                        "SELECT id, symbol, strategy, side, quantity, entry_price, entry_time, COALESCE(exit_reason, '') "
                        "FROM positions "
                        "WHERE symbol=? AND strategy=? AND status=?",
                        (symbol, strategy, "open")
                    ).fetchall()
                    for pos_id, pos_symbol, pos_strategy, side, qty, entry_price, entry_time, exit_reason in open_positions:
                        pnl = (fill_price - entry_price) * qty if side == "long" else (entry_price - fill_price) * qty
                        exit_time = datetime.now().isoformat()
                        conn.execute(
                            "UPDATE positions SET exit_time=?, exit_price=?, realized_pnl=?, "
                            "status=?, exit_order_id=?, exit_fill_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (
                                exit_time,
                                fill_price,
                                pnl,
                                "closed",
                                exit_order_id,
                                exit_fill_status,
                                pos_id,
                            )
                        )
                        self._reconcile_trade_logger_close(
                            symbol=pos_symbol,
                            strategy=pos_strategy,
                            side=side,
                            quantity=qty,
                            entry_price=entry_price,
                            entry_time=entry_time,
                            exit_price=fill_price,
                            exit_time=exit_time,
                            exit_reason=exit_reason or 'broker_close',
                        )
                    conn.commit()
                    self.logger.info(f"Closed {len(open_positions)} local DB position(s) for {symbol} ({strategy})")
                return True
            else:
                err = order_result.get("error", "unknown") if order_result else "no response"
                status_code = order_result.get("status_code", "?") if order_result else "?"
                self.logger.error(f"close_full_position failed for {symbol}: HTTP {status_code} - {err}")
                return False

        except Exception as e:
            self.logger.error(f"Failed to execute sell order for {symbol}: {e}", exc_info=True)
            return False

    def _check_daily_loss_limit(self) -> bool:
        """
        Task 890 — Daily loss circuit breaker.
        Returns True (and sets flag) if today's realized P&L has hit the configured limit.
        Resets automatically at midnight ET.
        Does NOT block _check_stop_loss_take_profit — existing positions keep their protection.
        """
        # Midnight reset
        today = datetime.now().date()
        if self._cb_date is not None and self._cb_date != today:
            self._daily_loss_limit_reached = False
            self._cb_date = today

        limit = self.config.get('daily_loss_limit', 15.0)
        try:
            today_start = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(str(db_path)) as conn:
                # Exclude placeholder/demo exits from daily circuit-breaker P&L.
                # 1) Demo-mode closures typically have no order_id (status may be 'closed' or NULL)
                # 2) Price-sanity guard: long exit < 50% of entry, short exit > 150% of entry
                row = conn.execute(
                    '''
                    SELECT SUM(realized_pnl)
                    FROM positions
                    WHERE status='closed'
                      AND exit_time >= ?
                      AND NOT (
                            (exit_order_id IS NULL AND exit_fill_status IS NULL)
                            OR
                            (exit_order_id IS NULL AND LOWER(COALESCE(exit_fill_status, '')) = 'closed')
                            OR
                            (
                                entry_price > 0 AND exit_price IS NOT NULL AND (
                                    (LOWER(COALESCE(side, 'long')) = 'long' AND exit_price < entry_price * 0.5)
                                    OR
                                    (LOWER(COALESCE(side, 'long')) = 'short' AND exit_price > entry_price * 1.5)
                                )
                            )
                      )
                    ''',
                    (today_start,)
                ).fetchone()

                excluded = conn.execute(
                    '''
                    SELECT symbol, side, entry_price, exit_price, realized_pnl, exit_order_id, exit_fill_status
                    FROM positions
                    WHERE status='closed'
                      AND exit_time >= ?
                      AND (
                            (exit_order_id IS NULL AND exit_fill_status IS NULL)
                            OR
                            (exit_order_id IS NULL AND LOWER(COALESCE(exit_fill_status, '')) = 'closed')
                            OR
                            (
                                entry_price > 0 AND exit_price IS NOT NULL AND (
                                    (LOWER(COALESCE(side, 'long')) = 'long' AND exit_price < entry_price * 0.5)
                                    OR
                                    (LOWER(COALESCE(side, 'long')) = 'short' AND exit_price > entry_price * 1.5)
                                )
                            )
                      )
                    ''',
                    (today_start,)
                ).fetchall()

            for symbol, side, entry_price, exit_price, realized_pnl, exit_order_id, exit_fill_status in excluded:
                entry_str = f'{entry_price:.2f}' if entry_price is not None else 'None'
                exit_str = f'{exit_price:.2f}' if exit_price is not None else 'None'
                self.logger.warning(
                    '[CircuitBreaker] Excluding demo/suspicious close from daily P&L: '
                    f'{symbol} {side} entry={entry_str} exit={exit_str} pnl={realized_pnl:.2f} '
                    f'order_id={exit_order_id} fill_status={exit_fill_status}'
                )

            daily_pnl = float(row[0]) if row and row[0] is not None else 0.0
            was_reached = self._daily_loss_limit_reached
            self._daily_loss_limit_reached = daily_pnl <= -limit
            self._cb_date = today

            if self._daily_loss_limit_reached:
                if not was_reached:
                    self.logger.warning(
                        f'[CircuitBreaker] TRIGGERED: daily_pnl={daily_pnl:.2f} <= -{limit:.2f}. '
                        f'No new entries for the rest of today.'
                    )
                    if self.alert_manager:
                        try:
                            self.alert_manager.fire(
                                AlertType.TRADE_EXECUTED,
                                symbol='PORTFOLIO', action='DAILY_LOSS_LIMIT',
                                quantity=0, price=round(abs(daily_pnl), 2),
                                strategy='circuit_breaker', confidence=daily_pnl / -limit
                            )
                        except Exception as _gge:
                            self.logger.debug(f"Circuit-breaker alert dispatch failed: {_gge}")
                return True

            if was_reached:
                self.logger.info(
                    f'[CircuitBreaker] RESET: daily_pnl={daily_pnl:.2f} above -{limit:.2f}. Entries re-enabled.'
                )
        except Exception as e:
            self.logger.error(f'[CircuitBreaker] _check_daily_loss_limit failed: {e}')
        return False

    def _check_stop_loss_take_profit(self):
        """
        Check all open positions against stop loss, take profit, and trailing stop.
        Called at the START of every scan_and_trade() before generating new signals.

        Trailing stop logic (Task 887):
        - trailing_stop_pct (default 3%) tracks from high water mark (HWM)
        - Activates AFTER position is up >= 2% (trailing_activation_pct)
        - When active, REPLACES fixed take profit (let winners run)
        - HWM also updated in position_manager.update_positions()
        """
        # Default SL/TP from active params — overridden per-symbol in the loop
        raw_sl    = self.active_params.get('stop_loss_pct',     0.05)
        raw_tp    = self.active_params.get('take_profit_pct',   0.06)
        raw_trail = self.active_params.get('trailing_stop_pct', 0.03)
        # Normalise: 5.0 -> 0.05, 0.05 -> 0.05
        stop_loss_pct      = raw_sl    / 100.0 if raw_sl    > 1.0 else raw_sl
        take_profit_pct    = raw_tp    / 100.0 if raw_tp    > 1.0 else raw_tp
        trailing_stop_pct  = raw_trail / 100.0 if raw_trail > 1.0 else raw_trail
        trailing_activation_pct = 0.02  # 2% gain to activate trailing stop

        self.logger.info(
            f"[SL/TP/Trail] SL={stop_loss_pct*100:.1f}% TP={take_profit_pct*100:.1f}% "
            f"Trail={trailing_stop_pct*100:.1f}% (activates at +{trailing_activation_pct*100:.0f}%)"
        )

        try:
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                open_positions = conn.execute(
                    "SELECT symbol, strategy, side, quantity, entry_price, "
                    "COALESCE(high_water_mark, entry_price), "
                    "COALESCE(trailing_stop_active, 0) "
                    "FROM positions WHERE status = 'open' "
                    "AND NOT (exit_order_id IS NOT NULL "
                    "AND LOWER(COALESCE(exit_fill_status, '')) IN "
                    "('new','accepted','accepted_for_bidding','pending_new','partially_filled','pending_cancel','pending_replace'))"
                ).fetchall()
        except Exception as e:
            self.logger.error(f"[SL/TP] Failed to fetch open positions: {e}")
            return

        if not open_positions:
            self.logger.info("[SL/TP] No open positions to check.")
            return

        for symbol, strategy, side, quantity, entry_price, high_water_mark, trailing_active in open_positions:
            try:
                # Per-symbol params from cluster config
                _sym_p = self._get_params_for_symbol(symbol)
                _raw_sl = _sym_p.get('stop_loss_pct', raw_sl if isinstance(raw_sl, float) and raw_sl <= 1 else 0.05)
                _raw_tp = _sym_p.get('take_profit_pct', raw_tp if isinstance(raw_tp, float) and raw_tp <= 1 else 0.06)
                _raw_trail = _sym_p.get('trailing_stop_pct', raw_trail if isinstance(raw_trail, float) and raw_trail <= 1 else 0.03)
                stop_loss_pct = _raw_sl / 100.0 if _raw_sl > 1.0 else _raw_sl
                take_profit_pct = _raw_tp / 100.0 if _raw_tp > 1.0 else _raw_tp
                trailing_stop_pct = _raw_trail / 100.0 if _raw_trail > 1.0 else _raw_trail

                quote = self.alpaca.get_quote(symbol)
                if quote is None:
                    self.logger.warning(f"[SL/TP] No quote for {symbol} - skipping")
                    continue

                current_price = float(quote.last)

                if side == 'long':
                    pnl_pct = (current_price - entry_price) / entry_price
                else:
                    pnl_pct = (entry_price - current_price) / entry_price

                # --- Update high water mark if price rose (long only) ---
                if side == 'long' and current_price > (high_water_mark or 0):
                    max_hwm_jump_pct = float(self.config.get('hwm_max_quote_jump_pct', 0.08))
                    max_entry_jump_pct = float(self.config.get('hwm_max_entry_jump_pct', 0.25))
                    sane_vs_hwm = (
                        not high_water_mark
                        or current_price <= float(high_water_mark) * (1.0 + max_hwm_jump_pct)
                    )
                    sane_vs_entry = (
                        not entry_price
                        or current_price <= float(entry_price) * (1.0 + max_entry_jump_pct)
                    )
                    if sane_vs_hwm and sane_vs_entry:
                        try:
                            with sqlite3.connect(self.position_manager.data_dir / 'positions.db') as c2:
                                c2.execute(
                                    "UPDATE positions SET high_water_mark=? "
                                    "WHERE symbol=? AND strategy=? AND status='open'",
                                    (current_price, symbol, strategy)
                                )
                                c2.commit()
                            self.logger.debug(f"[Trail] HWM {symbol}: ${high_water_mark:.2f} -> ${current_price:.2f}")
                            high_water_mark = current_price
                        except Exception as he:
                            self.logger.warning(f"[Trail] HWM update failed {symbol}: {he}")
                    else:
                        self.logger.warning(
                            f"[Trail] Ignoring suspicious HWM jump for {symbol}: "
                            f"entry=${entry_price:.2f} old_hwm=${high_water_mark:.2f} quote=${current_price:.2f}"
                        )

                # --- Activate trailing stop when gain >= 2% (long only) ---
                if side == 'long' and not trailing_active and pnl_pct >= trailing_activation_pct:
                    try:
                        with sqlite3.connect(self.position_manager.data_dir / 'positions.db') as c3:
                            c3.execute(
                                "UPDATE positions SET trailing_stop_active=1 "
                                "WHERE symbol=? AND strategy=? AND status='open'",
                                (symbol, strategy)
                            )
                            c3.commit()
                        trailing_active = 1
                        self.logger.info(
                            f"[Trail] ACTIVATED {symbol} ({strategy}) | "
                            f"PnL={pnl_pct*100:.2f}% >= {trailing_activation_pct*100:.0f}% | "
                            f"HWM=${high_water_mark:.2f} Trail={trailing_stop_pct*100:.1f}%"
                        )
                    except Exception as ae:
                        self.logger.warning(f"[Trail] Activation failed {symbol}: {ae}")

                # --- Determine trigger ---
                trigger = None

                if pnl_pct <= -stop_loss_pct:
                    trigger = 'STOP_LOSS'
                elif trailing_active and side == 'long':
                    # Trailing stop replaces fixed TP
                    hwm = high_water_mark or entry_price
                    trail_floor = hwm * (1.0 - trailing_stop_pct)
                    if current_price <= trail_floor:
                        locked_pct = (hwm - entry_price) / entry_price
                        trigger = 'TRAILING_STOP'
                        self.logger.info(
                            f"[Trail] TRAILING_STOP {symbol} | "
                            f"HWM=${hwm:.2f} Floor=${trail_floor:.2f} "
                            f"Current=${current_price:.2f} | "
                            f"Locked={locked_pct*100:.2f}% PnL={pnl_pct*100:.2f}%"
                        )
                    else:
                        self.logger.debug(
                            f"[Trail] {symbol} trailing - "
                            f"price=${current_price:.2f} floor=${trail_floor:.2f} pnl={pnl_pct*100:.2f}%"
                        )
                elif pnl_pct >= take_profit_pct:
                    trigger = 'TAKE_PROFIT'

                # MAX UNREALIZED GAIN — hard cap to prevent giving back huge gains
                if not trigger and pnl_pct >= self.config.get('max_unrealized_gain_pct', 0.20):
                    trigger = 'MAX_GAIN'
                    self.logger.info(
                        f"[MaxGain] {symbol} ({strategy}) hit +{pnl_pct*100:.1f}% "
                        f"(cap={self.config.get('max_unrealized_gain_pct', 0.20)*100:.0f}%) — forcing close"
                    )

                if trigger:
                    self.logger.warning(
                        f"[SL/TP] {trigger}: {symbol} ({strategy}) | "
                        f"Entry=${entry_price:.2f} Current=${current_price:.2f} "
                        f"PnL={pnl_pct*100:.2f}% | Closing."
                    )

                    emergency_sl_override = (
                        trigger == 'STOP_LOSS' and
                        pnl_pct <= -(2.0 * stop_loss_pct)
                    )
                    if emergency_sl_override:
                        self.logger.warning(
                            "[PDT-OVERRIDE] Emergency stop-loss: loss exceeds 2x SL threshold"
                        )

                    if emergency_sl_override:
                        closed = self._execute_sell_order(
                            symbol,
                            strategy,
                            current_price,
                            force=True
                        )
                    else:
                        closed = self._execute_sell_order(symbol, strategy, current_price)
                    # Trade journal: save exit reason (Task 25)
                    if closed:
                        try:
                            _db = self.position_manager.data_dir / 'positions.db'
                            with sqlite3.connect(_db) as _jc2:
                                _jc2.execute(
                                    "UPDATE positions SET exit_reason=? "
                                    "WHERE symbol=? AND strategy=? AND status='closed' "
                                    "ORDER BY exit_time DESC LIMIT 1",
                                    (trigger, symbol, strategy))
                                _jc2.commit()
                        except Exception as exit_reason_err:
                            self.logger.warning(f"Failed to persist exit_reason for {symbol}: {exit_reason_err}")
                    if closed:
                        self.logger.info(
                            f"[SL/TP] Closed {symbol} ({strategy}) @ ${current_price:.2f} "
                            f"| PnL={pnl_pct*100:.2f}% | Trigger={trigger}"
                        )
                        if self.alert_manager:
                            try:
                                from alerts.alert_types import AlertType
                                self.alert_manager.fire(
                                    AlertType.TRADE_EXECUTED,
                                    symbol=symbol, action='sell',
                                    quantity=quantity, price=round(current_price, 2),
                                    strategy=strategy, confidence=1.0,
                                )
                            except Exception as ale:
                                self.logger.warning(f"[SL/TP] Alert failed: {ale}")
                    else:
                        if getattr(self, '_last_close_blocked', None) == (symbol, strategy, 'PDT_GUARD'):
                            self.logger.info(f"[SL/TP] Close deferred by PDT guard: {symbol} ({strategy})")
                            self._last_close_blocked = None
                        elif getattr(self, '_last_close_blocked', None) == (symbol, strategy, 'EXIT_PENDING'):
                            self.logger.info(f"[SL/TP] Close submitted and pending broker fill: {symbol} ({strategy})")
                            self._last_close_blocked = None
                        else:
                            self.logger.error(f"[SL/TP] Close failed: {symbol} ({strategy})")
                else:
                    self.logger.debug(
                        f"[SL/TP] {symbol} ({strategy}): ${current_price:.2f} "
                        f"pnl={pnl_pct*100:.2f}% - no trigger"
                    )

            except Exception as e:
                self.logger.error(f"[SL/TP] Error {symbol} ({strategy}): {e}")

    def _check_premarket_gaps(self):
        """
        Check/manage large overnight gaps on open positions (Task 19).
        Called at start of scan_and_trade.

        Conservative policy:
        - Gap down on a long: warn loudly; the normal SL/trailing-stop pass runs next.
        - Gap up on a long: if the account is already at max position capacity and
          unrealized gain is >= gap_take_profit_threshold, close the full paper
          position to lock the gain and free one slot. Otherwise log the hold reason.
        """
        try:
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                open_positions = conn.execute(
                    "SELECT symbol, strategy, entry_price, side, quantity FROM positions WHERE status='open'"
                ).fetchall()

            if not open_positions:
                return

            at_capacity = len(open_positions) >= int(self.config.get('max_concurrent_trades', 5))
            gap_threshold = float(self.config.get('gap_warning_threshold', 0.03))
            take_profit_threshold = float(self.config.get('gap_take_profit_threshold', 0.05))
            require_capacity = bool(self.config.get('gap_take_profit_when_at_capacity', True))

            for symbol, strategy, entry_price, side, quantity in open_positions:
                try:
                    quote = self.alpaca.get_quote(symbol)
                    if not quote:
                        continue

                    current_price = float(quote.last)

                    # Get previous close for gap calculation
                    hist = self.alpaca.get_historical_data(symbol, days=5, timeframe='1Day')
                    if hist is None or len(hist) < 2:
                        continue

                    prev_close = float(hist['close'].iloc[-2])
                    if prev_close <= 0 or not entry_price:
                        continue
                    gap_pct = (current_price - prev_close) / prev_close

                    if abs(gap_pct) > gap_threshold:
                        direction = "UP" if gap_pct > 0 else "DOWN"
                        self.logger.warning(
                            f"[Gap] {symbol} gapped {direction} {gap_pct*100:.1f}%: "
                            f"prev_close=${prev_close:.2f} → current=${current_price:.2f}")

                        # Gap down on long position: SL/trailing pass follows immediately.
                        if gap_pct < -gap_threshold and side == 'long':
                            self.logger.info(
                                f"[Gap] {symbol} gap down — SL/trailing-stop check will manage exit risk next")

                        # Gap up on long position: lock gains only when the portfolio is saturated.
                        elif gap_pct > gap_threshold and side == 'long':
                            unrealized = (current_price - entry_price) * quantity
                            unrealized_pct = (current_price - entry_price) / entry_price
                            should_take_profit = (
                                unrealized_pct >= take_profit_threshold
                                and (at_capacity or not require_capacity)
                            )
                            if should_take_profit:
                                self.logger.info(
                                    f"[Gap] {symbol} gap-up profit action: unrealized=${unrealized:.2f} "
                                    f"({unrealized_pct*100:.1f}%) >= {take_profit_threshold*100:.1f}% "
                                    f"and at_capacity={at_capacity}; closing to lock gain/free capacity")
                                self._execute_sell_order(symbol, strategy, current_price, force=True)
                            else:
                                self.logger.info(
                                    f"[Gap] {symbol} gap up — holding: unrealized=${unrealized:.2f} "
                                    f"({unrealized_pct*100:.1f}%), threshold={take_profit_threshold*100:.1f}%, "
                                    f"at_capacity={at_capacity}")

                except Exception as _ge:
                    self.logger.debug(f"[Gap] Check failed for {symbol}: {_ge}")

        except Exception as e:
            self.logger.warning(f"[Gap] Pre-market gap check failed: {e}")

    def scan_and_trade(self):
        """Main trading loop: scan for signals and execute trades"""
        try:
            self.logger.info("Starting trading scan...")

            # Safety: never apply demo quotes to existing real/paper positions.
            # Demo quote generation can be wildly different from current market prices
            # and would trigger bogus stop-loss/gap exits.
            if getattr(self.alpaca, 'demo_mode', False):
                try:
                    db_path = self.position_manager.data_dir / 'positions.db'
                    with sqlite3.connect(db_path) as conn:
                        open_count = conn.execute(
                            "SELECT COUNT(*) FROM positions WHERE status='open'"
                        ).fetchone()[0]
                    if open_count:
                        self.logger.error(
                            f"[DATA-GUARD] Alpaca client is in demo_mode with {open_count} open position(s); "
                            "aborting scan to avoid demo prices acting on live paper state."
                        )
                        return
                except Exception as _dg_e:
                    self.logger.error(f"[DATA-GUARD] Could not verify open positions in demo_mode: {_dg_e}")
                    return

            # PRE-MARKET GAP DETECTION (Task 19)
            self._check_premarket_gaps()

            # DETECT MARKET REGIME (Task 17) — before making any trading decisions
            if self._regime_detector:
                try:
                    spy_data = self.alpaca.get_historical_data('SPY', days=60, timeframe='1Day')
                    vix_val = fetch_vix() if _REGIME_AVAILABLE else None
                    self._current_regime, self._regime_details = self._regime_detector.detect_regime(
                        spy_data=spy_data, vix_value=vix_val)
                    self.logger.info(
                        f"[Regime] Current: {self._current_regime.value} | "
                        f"Details: {self._regime_details}")
                except Exception as _re:
                    self.logger.warning(f"[Regime] Detection failed: {_re}")
                    self._current_regime = MarketRegime.NORMAL if _REGIME_AVAILABLE else None

            # CHECK STOP LOSS / TAKE PROFIT FIRST — before any new signals
            self._check_stop_loss_take_profit()

            # DAILY LOSS CIRCUIT BREAKER (Task 890) — block new entries if daily P&L too negative
            if self._check_daily_loss_limit():
                self.logger.warning('[CircuitBreaker] Daily loss limit reached -- no new entries today.')
                return
            
            # Get winning strategies
            winning_strategies = self.get_latest_tournament_winners()
            
            if not winning_strategies:
                # FALLBACK: Use built-in strategies with champion params when no tournament winners
                self.logger.info("No recent tournament winners — using fallback strategies with champion params")
                winning_strategies = [
                    # RSI Mean Reversion DISABLED: 0% win rate over 4 trades (Mar 31). Re-enable after 30-day data review.
                    ('Confluence', 0.65),
                    ('MACD Crossover', 0.60),
                    ('Bollinger Bounce', 0.55),
                    ('VWAP Reversion', 0.55),
                    ('Opening Range Breakout', 0.50),
                    ('Mean Reversion Pairs', 0.50),
                ]
            
            # Refresh current prices before capacity/drawdown decisions. The old path
            # returned immediately when max_concurrent_trades was reached, leaving
            # existing position values stale during saturated sessions.
            price_data = {}
            for symbol in self.config['trading_symbols']:
                try:
                    quote = self.alpaca.get_quote(symbol)
                    if quote:
                        price_data[symbol] = quote.last
                except Exception as e:
                    self.logger.warning(f"Failed to get quote for {symbol}: {e}")

            if price_data:
                self.position_manager.update_positions(price_data)

            # Check portfolio state after management/price refresh
            portfolio_state = self.position_manager.get_portfolio_state()
            self.logger.info(f"Portfolio: ${portfolio_state.total_value:,.2f}, {portfolio_state.position_count} positions")
            
            # MAX DRAWDOWN CIRCUIT BREAKER (Task 27)
            current_value = portfolio_state.total_value
            if self._portfolio_peak is None or current_value > self._portfolio_peak:
                self._portfolio_peak = current_value
            
            if self._portfolio_peak and self._portfolio_peak > 0:
                drawdown = (self._portfolio_peak - current_value) / self._portfolio_peak
                if drawdown > 0.15:  # 15% drawdown threshold
                    if self._circuit_breaker_until is None:
                        self._circuit_breaker_until = datetime.now() + timedelta(hours=48)
                        self.logger.warning(
                            f"[CIRCUIT BREAKER] Portfolio drawdown {drawdown*100:.1f}% exceeds 15%! "
                            f"Peak=${self._portfolio_peak:.2f} Current=${current_value:.2f}. "
                            f"NEW TRADES BLOCKED until {self._circuit_breaker_until.isoformat()}")
                        if self.alert_manager:
                            try:
                                self.alert_manager.fire(
                                    AlertType.TRADE_EXECUTED,
                                    symbol='PORTFOLIO', action='CIRCUIT_BREAKER',
                                    quantity=0, price=round(current_value, 2),
                                    strategy='drawdown_guard', confidence=drawdown)
                            except Exception as close_gap_err:
                                self.logger.debug(f"Gap close attempt failed for {symbol}: {close_gap_err}")
            
            if self._circuit_breaker_until and datetime.now() < self._circuit_breaker_until:
                self.logger.warning(
                    f"[CIRCUIT BREAKER] Active until {self._circuit_breaker_until.isoformat()}. "
                    f"No new trades. Existing SL/TP still monitored.")
                return
            elif self._circuit_breaker_until:
                self.logger.info("[CIRCUIT BREAKER] Cooldown expired. Resuming trading.")
                self._circuit_breaker_until = None
            
            if portfolio_state.position_count >= self.config['max_concurrent_trades']:
                self.logger.info(
                    "Maximum concurrent trades reached after risk management/price refresh; "
                    "new entries blocked, existing positions already checked"
                )
                return
            
            # Generate and execute signals
            signals_executed = 0
            
            for strategy_name, score in winning_strategies:
                if signals_executed >= (self.config['max_concurrent_trades'] - portfolio_state.position_count):
                    break
                    
                for symbol in self.config['trading_symbols']:
                    # Check if we already have a position for this strategy+symbol (local DB)
                    existing_positions = self._check_existing_position(symbol, strategy_name)
                    if existing_positions:
                        continue
                    
                    # Correlation guard: max positions per correlated group
                    corr_groups = self.config.get('correlation_groups', {})
                    max_per_group = self.config.get('max_per_correlation_group', 2)
                    skip_corr = False
                    for group_name, group_symbols in corr_groups.items():
                        if symbol in group_symbols:
                            # Count open positions in this group
                            try:
                                db_path = self.position_manager.data_dir / 'positions.db'
                                with sqlite3.connect(db_path) as _cconn:
                                    placeholders = ','.join('?' for _ in group_symbols)
                                    group_count = _cconn.execute(
                                        "SELECT COUNT(*) FROM positions WHERE symbol IN (%s) AND status='open'" % placeholders,
                                        group_symbols
                                    ).fetchone()[0]
                                if group_count >= max_per_group:
                                    self.logger.debug(
                                        "[CorrGuard] Skipping %s: group '%s' has %d/%d positions" % (
                                            symbol, group_name, group_count, max_per_group))
                                    skip_corr = True
                            except Exception as _cge:
                                pass
                            break
                    if skip_corr:
                        continue

                    # Earnings filter (Task 24): don't enter near earnings
                    if _EARNINGS_AVAILABLE:
                        try:
                            cache_dir = str(self.data_dir)
                            if is_near_earnings(symbol, days_buffer=3, cache_dir=cache_dir):
                                self.logger.debug(f"[Earnings] Skipping {symbol}: too close to earnings")
                                continue
                        except Exception as _ef:
                            pass  # Don't block trading on earnings check failure

                    # Sector exposure limit (Task 22)
                    if not self._check_sector_exposure(symbol):
                        continue

                    # Also block if Alpaca already holds this symbol (orphan guard)
                    alpaca_positions = getattr(self, "_alpaca_positions", set())
                    if symbol in alpaca_positions:
                        self.logger.debug("Skipping %s: Alpaca already holds position (orphan guard)" % symbol)
                        continue
                    
                    # Skip symbols that lose money in OOS validation
                    sym_perf = self._symbol_performance.get(symbol, {})
                    if sym_perf and not sym_perf.get('tradeable', True):
                        self.logger.debug(
                            "Skipping %s: OOS P&L=%.1f%% (not tradeable)" % (
                                symbol, sym_perf.get('oos_pnl_pct', 0)))
                        continue
                    
                    # Generate signal
                    signal = self.generate_trading_signals(symbol, strategy_name)
                    
                    # Apply regime-based weight to confidence (Task 17)
                    if signal and self._regime_detector and self._current_regime:
                        regime_weight = self._regime_detector.get_strategy_weight(
                            self._current_regime, strategy_name)
                        original_conf = signal.get('confidence', 0)
                        signal['confidence'] = original_conf * regime_weight
                        if regime_weight != 1.0:
                            signal['reason'] = signal.get('reason', '') + (
                                f" [regime:{self._current_regime.value} w={regime_weight:.1f}]")
                            self.logger.debug(
                                f"[Regime] {symbol} {strategy_name}: conf {original_conf:.2f} "
                                f"→ {signal['confidence']:.2f} (regime={self._current_regime.value})")

                    if signal and signal.get('confidence', 0) >= self.config['min_strategy_confidence']:
                        success = self.execute_signal(signal)
                        if success:
                            signals_executed += 1
                            break  # Only one trade per strategy per scan
            
            # Save portfolio snapshot
            self.position_manager.save_portfolio_snapshot()
            
            self.logger.info(f"Trading scan completed. Executed {signals_executed} signals.")
            
        except Exception as e:
            self.logger.error(f"Trading scan failed: {e}")
    
    def _check_existing_position(self, symbol: str, strategy: str) -> bool:
        """Check if we already have a position for this symbol+strategy"""
        try:
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                position = conn.execute('''
                    SELECT id FROM positions 
                    WHERE symbol = ? AND strategy = ? AND status = 'open'
                    LIMIT 1
                ''', (symbol, strategy)).fetchone()
                
                return position is not None
                
        except Exception as e:
            self.logger.error(f"Failed to check existing position: {e}")
            return False
    
    def close_aged_positions(self):
        """Close positions that have been held too long"""
        try:
            cutoff_date = (datetime.now() - timedelta(days=self.config['position_hold_days'])).isoformat()
            
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                aged_positions = conn.execute('''
                    SELECT symbol, strategy FROM positions 
                    WHERE status = 'open' AND entry_time < ?
                ''', (cutoff_date,)).fetchall()
                
                for symbol, strategy in aged_positions:
                    try:
                        # Get current price
                        quote = self.alpaca.get_quote(symbol)
                        if quote:
                            current_price = quote.last
                            success = self._execute_sell_order(symbol, strategy, current_price)
                            if success:
                                self.logger.info(f"Closed aged position: {symbol} {strategy}")
                    except Exception as e:
                        self.logger.error(f"Failed to close aged position {symbol} {strategy}: {e}")
            
        except Exception as e:
            self.logger.error(f"Failed to close aged positions: {e}")
    
    def generate_trading_report(self) -> str:
        """Generate comprehensive trading performance report"""
        try:
            # Get portfolio state
            portfolio_state = self.position_manager.get_portfolio_state()
            
            # Get position manager report
            position_report = self.position_manager.get_performance_report(days=30)
            
            # Get recent tournament data
            winning_strategies = self.get_latest_tournament_winners(days=7)
            
            # Build comprehensive report
            report_lines = []
            report_lines.append("🤖 TradeSight Automated Trading Report")
            report_lines.append("=" * 60)
            report_lines.append(f"Report Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            report_lines.append("")
            
            # Portfolio summary
            report_lines.append("💼 Portfolio Summary")
            report_lines.append("-" * 20)
            report_lines.append(f"Total Value: ${portfolio_state.total_value:,.2f}")
            report_lines.append(f"Available Cash: ${portfolio_state.available_cash:,.2f}")
            report_lines.append(f"Positions Value: ${portfolio_state.total_positions_value:,.2f}")
            report_lines.append(f"Total P&L: ${portfolio_state.total_pnl:,.2f}")
            report_lines.append(f"Active Positions: {portfolio_state.position_count}")
            report_lines.append(f"Active Strategies: {', '.join(portfolio_state.strategies_active)}")
            divergence = self._accounting_divergence()
            if divergence and divergence.get('warn'):
                self.logger.warning(
                    "[Accounting] Local P&L diverges from Alpaca equity: local=$%.2f broker=$%.2f diff=$%.2f",
                    divergence['local_pnl'],
                    divergence['broker_pnl'],
                    divergence['diff'],
                )
                report_lines.append("")
                report_lines.append("⚠️ Accounting Reconciliation Warning")
                report_lines.append("-" * 40)
                report_lines.append(
                    "Local positions P&L differs from Alpaca equity by $%.2f "
                    "(local=$%.2f, broker=$%.2f, synced=%s). "
                    "Treat broker equity as source of truth until reconciled." % (
                        divergence['diff'],
                        divergence['local_pnl'],
                        divergence['broker_pnl'],
                        divergence.get('synced_at') or 'unknown',
                    )
                )
            report_lines.append("")
            
            # Tournament winners
            if winning_strategies:
                report_lines.append("🏆 Active Tournament Winners (Last 7 Days)")
                report_lines.append("-" * 40)
                for strategy, score in winning_strategies:
                    report_lines.append(f"{strategy}: {score:.3f} confidence")
                report_lines.append("")
            
            # Position performance
            report_lines.append(position_report)
            
            # Recent closed trades from positions DB (last 7 days).
            # Separate broker-verified closes from local/unverified closes so the
            # report does not imply suspicious/demo rows are clean broker fills.
            try:
                db_path = self.position_manager.data_dir / 'positions.db'
                cutoff = (datetime.now() - timedelta(days=7)).isoformat()
                with sqlite3.connect(db_path) as conn:
                    closed = conn.execute(
                        "SELECT symbol, side, entry_price, exit_price, realized_pnl, strategy, exit_time, "
                        "exit_order_id, exit_fill_status "
                        "FROM positions WHERE status='closed' AND exit_time > ? "
                        "ORDER BY exit_time DESC LIMIT 20",
                        (cutoff,)
                    ).fetchall()
                if closed:
                    verified = []
                    unverified = []
                    for row in closed:
                        exit_order_id = row[7]
                        exit_fill_status = (row[8] or '').lower()
                        if exit_order_id and exit_fill_status in ('filled', 'closed'):
                            verified.append(row)
                        else:
                            unverified.append(row)

                    def _append_closed_rows(title, rows, suffix=''):
                        if not rows:
                            return
                        report_lines.append("")
                        report_lines.append(title)
                        report_lines.append("-" * 40)
                        for sym, side, entry, exit_p, pnl, strat, exit_t, order_id, fill_status in rows[:10]:
                            exit_str = "$%.2f" % exit_p if exit_p else "N/A"
                            pnl_str = "$%.2f" % pnl if pnl else "$0.00"
                            report_lines.append(
                                "%s %s: entry=$%.2f exit=%s P&L=%s (%s)%s" % (
                                    sym, side, entry or 0, exit_str, pnl_str, strat, suffix))

                    _append_closed_rows("📉 Broker-Verified Closed Trades (Last 7 Days)", verified)
                    _append_closed_rows(
                        "⚠️ Local/Unverified Closed Trades (excluded from risk limits)",
                        unverified,
                        " [missing broker exit fill]",
                    )
            except Exception as _rpe:
                self.logger.warning("Could not fetch closed trades for report: %s" % str(_rpe))
            
            return "\n".join(report_lines)
            
        except Exception as e:
            self.logger.error(f"Failed to generate trading report: {e}")
            return "Failed to generate trading report"
    

    def _sync_with_alpaca(self):
        """Sync local state with Alpaca reality - call at start of every session."""
        try:
            if getattr(self.alpaca, 'demo_mode', False):
                # No real broker truth is available in demo mode. Do not overwrite
                # broker-synced cash/equity, and never close local live/paper
                # positions just because the demo Alpaca client has no positions.
                self.logger.warning("[DemoGuard] Alpaca credentials unavailable; skipping broker sync/stale-position reconciliation")
                self._alpaca_synced = False
                self._alpaca_positions = set()
                return

            account = self.alpaca.get_account()
            if not account:
                self.logger.warning("Could not fetch Alpaca account - skipping sync")
                return
            
            real_equity = float(account.get("equity", 0))
            real_cash = float(account.get("buying_power", 0))
            real_positions_value = float(account.get("long_market_value", 0))
            self._daytrade_count = int(float(account.get('daytrade_count', 0) or 0))
            self._pattern_day_trader = bool(account.get('pattern_day_trader', False))
            self._pdt_protection_active = (not self._pattern_day_trader and self._daytrade_count >= 3)
            self.config['enforce_min_hold_hours'] = self._pdt_protection_active
            
            self.logger.info("Alpaca account: equity=$%.2f, buying_power=$%.2f, positions=$%.2f" % (real_equity, real_cash, real_positions_value))
            if self._pdt_protection_active:
                self.logger.warning(
                    "[PDT-PROTECT] Alpaca day-trade protection active (daytrade_count=%d). "
                    "New buys blocked and same-day closes deferred." % self._daytrade_count
                )
            
            # Check for orphan positions (in Alpaca but not in our DB)
            remote_positions = self.alpaca.get_remote_positions()
            local_state = self.position_manager.get_portfolio_state()

            # Build set of symbols Alpaca currently holds
            remote_symbols = {rp.get("symbol", "") for rp in remote_positions if rp.get("symbol")}

            # Detect orphan positions regardless of local DB count.
            # Bug fix: previously only triggered when local DB had 0 positions,
            # meaning stale local positions blocked orphan detection for new symbols.
            if remote_positions:
                with sqlite3.connect(self.position_manager.data_dir / 'positions.db') as _conn:
                    local_open = set(
                        row[0] for row in _conn.execute(
                            "SELECT DISTINCT symbol FROM positions WHERE status=\'open\'"
                        ).fetchall()
                    )
                orphan_symbols = remote_symbols - local_open
                if orphan_symbols:
                    self.logger.warning(
                        "ORPHAN POSITIONS DETECTED: Alpaca has %d position(s) not in local DB: %s. "
                        "Buying power is $%.2f. Syncing now." % (
                            len(orphan_symbols), sorted(orphan_symbols), real_cash
                        )
                    )

            # Store real buying power for position sizing
            self._real_buying_power = real_cash
            self._real_equity = real_equity
            self._alpaca_synced = True

            # Persist buying power to DB so get_portfolio_state() can use real balance
            self.position_manager.persist_balance_sync(real_cash, equity=real_equity, positions_value=real_positions_value)

            # Update pending exit orders before stale-position cleanup so filled
            # broker closes get true fill prices and clear trades.db/open_trades.
            self._reconcile_exit_fills(remote_symbols)

            # Always run orphan sync (not just when local DB is empty).
            # _sync_orphan_positions handles per-symbol dedup internally.
            if remote_positions:
                self._sync_orphan_positions(remote_positions)

            # Close stale local positions that Alpaca has already exited.
            self._close_stale_positions(remote_symbols)

            # Refresh the broker-verified accounting receipt after local/broker
            # position repair. If no epoch exists yet, normal paper operation is
            # unchanged and the dashboard continues to fail closed.
            if load_epoch(self.position_manager.base_dir / 'state'):
                accounting = reconcile_accounting(
                    self.position_manager.base_dir,
                    account,
                    remote_positions,
                    order_fetcher=self.alpaca.get_order,
                )
                if accounting.get('status') != 'VERIFIED':
                    self.logger.warning(
                        '[AccountingTruth] Reconciliation status=%s blockers=%s',
                        accounting.get('status'),
                        (accounting.get('reconciliation') or {}).get('blockers'),
                    )

            # Track which symbols Alpaca already has positions in
            self._alpaca_positions = remote_symbols
            for rp in remote_positions:
                sym = rp.get("symbol", "")
                if sym:
                    self.logger.info("Alpaca has existing position: %s (qty=%s)" % (sym, rp.get("qty", "?")))
            
        except Exception as e:
            self.logger.error("Alpaca sync failed: %s" % str(e))
            self._alpaca_synced = False

    def _sync_orphan_positions(self, remote_positions):
        """Import orphan Alpaca positions into local DB so SL/TP monitoring works."""
        if not remote_positions:
            return
        try:
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                local_symbols = set(
                    row[0] for row in conn.execute(
                        "SELECT DISTINCT symbol FROM positions WHERE status='open'"
                    ).fetchall()
                )
                for rp in remote_positions:
                    sym = rp.get('symbol', '')
                    if sym and sym not in local_symbols:
                        qty = float(rp.get('qty', 0))
                        avg_entry = float(rp.get('avg_entry_price', 0))
                        market_value = float(rp.get('market_value', 0))
                        side = rp.get('side', 'long')
                        self.logger.info(
                            f"[OrphanSync] Importing {sym}: qty={qty}, "
                            f"entry=${avg_entry:.2f}, side={side}"
                        )
                        current_val = float(rp.get('current_price', 0)) or avg_entry
                        conn.execute(
                            "INSERT INTO positions "
                            "(symbol, strategy, side, quantity, entry_price, "
                            "current_price, entry_time, status, high_water_mark) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                            (sym, 'RSI Mean Reversion', side, qty, avg_entry,
                             current_val, datetime.now().isoformat(), avg_entry)
                        )
                        local_symbols.add(sym)
                conn.commit()
        except Exception as e:
            self.logger.error(f"[OrphanSync] Failed: {e}")

    def _close_stale_positions(self, remote_symbols: set):
        """Mark local 'open' positions as closed if Alpaca no longer holds them.

        This handles the case where Alpaca closes a position externally
        (stop-loss triggered, manual close, etc.) but local DB still shows open.
        Stale open positions block orphan detection and skew portfolio reporting.
        Now fetches current price for proper P&L calculation instead of NULL exit.
        """
        try:
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                local_open = conn.execute(
                    "SELECT id, symbol, strategy, entry_price, quantity, side, entry_time, COALESCE(exit_reason, '') "
                    "FROM positions WHERE status='open'"
                ).fetchall()
                stale = [(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7])
                         for row in local_open if row[1] not in remote_symbols]
                if stale:
                    for pos_id, sym, strategy, entry_price, quantity, side, entry_time, exit_reason in stale:
                        # Fetch current price for proper exit price
                        exit_price = None
                        try:
                            quote = self.alpaca.get_quote(sym)
                            if quote and quote.last and quote.last > 0:
                                exit_price = float(quote.last)
                        except Exception as qe:
                            self.logger.warning(
                                "[StaleSync] Could not get quote for %s: %s" % (sym, qe))
                        
                        # Calculate realized P&L
                        realized_pnl = 0.0
                        if exit_price and entry_price and quantity:
                            if side == 'long':
                                realized_pnl = (exit_price - entry_price) * quantity
                            else:
                                realized_pnl = (entry_price - exit_price) * quantity
                        
                        exit_str = "$%.2f" % exit_price if exit_price else "unknown"
                        self.logger.warning(
                            "[StaleSync] Closing local position %s (id=%d) — "
                            "no longer in Alpaca. Exit=%s PnL=$%.2f" % (
                                sym, pos_id, exit_str, realized_pnl))
                        
                        conn.execute(
                            "UPDATE positions SET status='closed', exit_time=?, "
                            "exit_price=?, realized_pnl=?, exit_fill_status=?, "
                            "exit_reason=COALESCE(NULLIF(exit_reason, ''), ?), updated_at=? WHERE id=?",
                            (datetime.now().isoformat(), exit_price, realized_pnl,
                             'broker_reconciled_closed', exit_reason or 'broker_reconciled',
                             datetime.now().isoformat(), pos_id)
                        )
                        if exit_price and exit_price > 0:
                            self._reconcile_trade_logger_close(
                                symbol=sym,
                                strategy=strategy,
                                side=side,
                                quantity=quantity,
                                entry_price=entry_price,
                                entry_time=entry_time,
                                exit_price=exit_price,
                                exit_time=datetime.now().isoformat(),
                                exit_reason=exit_reason or 'broker_reconciled',
                            )
                    conn.commit()
                    self.logger.info("[StaleSync] Closed %d stale position(s): %s" % (
                        len(stale), [s[1] for s in stale]
                    ))
        except Exception as e:
            self.logger.error("[StaleSync] Failed: %s" % str(e))

    def _trade_updates_connect_once(self, stop_event: threading.Event):
        """Connect to Alpaca trade_updates stream and block until disconnected.

        Raises on auth/subscribe failure or connection drop so supervisor can reconnect.
        """
        if self.alpaca.demo_mode:
            raise RuntimeError('demo mode: websocket disabled')

        # Lazy import so unit tests don't require websocket-client installed.
        import websocket

        ws_url = 'wss://paper-api.alpaca.markets/stream'
        ws = websocket.create_connection(ws_url, timeout=20)
        ws.settimeout(20)
        try:
            ws.send(json.dumps({
                'action': 'auth',
                'key': self.alpaca.api_key,
                'secret': self.alpaca.secret_key,
            }))
            auth_resp = json.loads(ws.recv())
            auth_payload = auth_resp[0] if isinstance(auth_resp, list) and auth_resp else auth_resp
            auth_stream = auth_payload.get('stream') if isinstance(auth_payload, dict) else None
            auth_data = auth_payload.get('data', {}) if isinstance(auth_payload, dict) else {}
            auth_status = str(auth_data.get('status', '')).lower() if isinstance(auth_data, dict) else ''
            if auth_stream != 'authorization' or auth_status != 'authorized':
                raise RuntimeError(f'websocket auth failed: {auth_resp}')

            ws.send(json.dumps({'action': 'listen', 'data': {'streams': ['trade_updates']}}))
            listen_resp = json.loads(ws.recv())
            payload = listen_resp[0] if isinstance(listen_resp, list) and listen_resp else listen_resp
            streams = payload.get('data', {}).get('streams', []) if isinstance(payload, dict) else []
            if 'trade_updates' not in streams:
                raise RuntimeError(f'trade_updates subscribe failed: {listen_resp}')

            self.logger.info('[WS] Connected to Alpaca trade_updates stream')

            while not stop_event.is_set():
                try:
                    raw = ws.recv()
                except Exception as recv_err:
                    # Alpaca trade_updates can be idle for long stretches.
                    # websocket-client raises a timeout when no frame arrives before
                    # ws.settimeout(), but that is not a dropped connection. Treat
                    # idle read timeouts as healthy silence instead of reconnecting
                    # every 20 seconds and filling logs with false warnings.
                    err_name = recv_err.__class__.__name__.lower()
                    err_text = str(recv_err).lower()
                    if 'timeout' in err_name or 'timed out' in err_text:
                        continue
                    raise
                if raw is None:
                    raise ConnectionError('empty websocket frame')
                event_msg = json.loads(raw)
                packets = event_msg if isinstance(event_msg, list) else [event_msg]
                for packet in packets:
                    if packet.get('stream') != 'trade_updates':
                        continue
                    data = packet.get('data', {})
                    event = data.get('event')
                    if event in ('fill', 'partial_fill'):
                        order = data.get('order', {})
                        self.logger.info(
                            '[WS] %s %s qty=%s avg=%s side=%s',
                            event,
                            order.get('symbol', '?'),
                            order.get('filled_qty', '?'),
                            order.get('filled_avg_price', '?'),
                            order.get('side', '?'),
                        )
        finally:
            try:
                ws.close()
            except Exception as stop_err:
                self.logger.warning(f"Failed to stop trade update monitor cleanly: {stop_err}")

    def _start_trade_updates_monitor(self):
        """Start background websocket supervisor with exponential backoff."""
        if self.alpaca.demo_mode:
            self.logger.info('[WS] Demo mode: skipping trade_updates monitor')
            return
        if self._ws_thread and self._ws_thread.is_alive():
            return

        self._ws_stop_event = threading.Event()
        self._ws_supervisor = ExponentialBackoffWebSocketSupervisor(
            connect_once=self._trade_updates_connect_once,
            logger=self.logger,
            initial_backoff=1.0,
            max_backoff=60.0,
        )
        self._ws_thread = threading.Thread(
            target=self._ws_supervisor.run,
            kwargs={'stop_event': self._ws_stop_event},
            daemon=True,
            name='alpaca-trade-updates-ws',
        )
        self._ws_thread.start()
        self.logger.info('[WS] trade_updates monitor started')

    def _stop_trade_updates_monitor(self):
        """Stop websocket supervisor thread if running."""
        if self._ws_stop_event:
            self._ws_stop_event.set()
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=3)
        self._ws_thread = None
        self._ws_stop_event = None


    def _append_trade_logger_analysis(self, report: str, days: int = 30) -> str:
        """Append trade_logger analysis only when it has real closed trades.

        The positions DB is the source used by the portfolio report. The separate
        trade_logger DB can legitimately be empty; appending its "No closed trades
        yet" message after positions DB closed trades makes the report contradict
        itself.
        """
        try:
            trade_logger = getattr(self.position_manager, 'trade_logger', None)
            if not trade_logger:
                return report
            analysis = trade_logger.get_analysis(days=days)
            if int(analysis.get('total_trades') or 0) <= 0:
                return report
            return report + "\n\n" + trade_logger.report(days=days)
        except Exception as te:
            self.logger.warning(f"Trade report failed (non-fatal): {te}")
            return report


    def run_trading_session(self):
        """Run a complete trading session"""
        try:
            self.logger.info("=== Starting TradeSight Paper Trading Session ===")
            self._start_trade_updates_monitor()
            self._refresh_learning_feedback()
            
            # Sync with Alpaca before doing anything
            self._sync_with_alpaca()
            
            # Main trading logic
            self.scan_and_trade()
            
            # Close aged positions
            self.close_aged_positions()

            # One more lightweight broker reconciliation after any exits submitted
            # during this run so reports do not freeze pending_new as a fake close.
            if not getattr(self.alpaca, 'demo_mode', False):
                try:
                    remote_positions = self.alpaca.get_remote_positions()
                    remote_symbols = {rp.get("symbol", "") for rp in remote_positions if rp.get("symbol")}
                    self._reconcile_exit_fills(remote_symbols)
                except Exception as reconcile_err:
                    self.logger.warning(f"[ExitReconcile] End-of-run reconcile skipped: {reconcile_err}")
            
            # Generate report
            report = self.generate_trading_report()

            # Append trade-level analysis before saving so the on-disk report
            # matches the returned report and Mission Control evidence.
            report = self._append_trade_logger_analysis(report, days=30)
            
            # Save report
            report_file = self.logs_dir / f"trading_report_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
            with open(report_file, 'w') as f:
                f.write(report)
            
            # --- FEEDBACK LOOP (per-trade, not per-scan) ---
            # Only log feedback when trades were actually CLOSED in this session.
            # Previous bug: logged cumulative portfolio P&L on every 15-min scan,
            # producing garbage data (same P&L repeated 26x/day).
            if self.feedback and self.active_params:
                try:
                    db_path = self.position_manager.data_dir / 'positions.db'
                    # Find trades closed in the last 20 minutes (this session window)
                    cutoff = (datetime.now() - timedelta(minutes=20)).isoformat()
                    with sqlite3.connect(db_path) as conn:
                        closed_this_session = conn.execute(
                            "SELECT id, symbol, strategy, side, entry_price, exit_price, realized_pnl, "
                            "quantity, COALESCE(exit_reason, ''), entry_time, exit_time "
                            "FROM positions WHERE status='closed' AND exit_time > ?",
                            (cutoff,)
                        ).fetchall()
                    
                    if closed_this_session:
                        total_pnl = sum(r[6] or 0 for r in closed_this_session)
                        wins = sum(1 for r in closed_this_session if (r[6] or 0) > 0)
                        total = len(closed_this_session)
                        win_rate = wins / total if total > 0 else 0.0
                        # Calculate P&L as percentage of total position value
                        total_entry_value = sum((r[4] or 0) * (r[7] or 0) for r in closed_this_session)
                        pnl_pct = (total_pnl / max(total_entry_value, 1)) * 100
                        recorded = 0
                        for row in closed_this_session:
                            (
                                pos_id, symbol, strategy, side, entry_price, exit_price,
                                realized_pnl, quantity, exit_reason, entry_time, exit_time,
                            ) = row
                            if self.feedback.record_closed_trade(
                                params=self.active_params,
                                symbol=symbol,
                                strategy=strategy,
                                side=side,
                                entry_price=entry_price,
                                exit_price=exit_price,
                                quantity=quantity,
                                pnl_dollars=realized_pnl,
                                exit_reason=exit_reason,
                                opened_at=entry_time,
                                closed_at=exit_time,
                                market_regime=(
                                    self._current_regime.value
                                    if getattr(self, '_current_regime', None) is not None
                                    and hasattr(self._current_regime, 'value')
                                    else 'unknown'
                                ),
                                source='positions',
                                source_id=str(pos_id),
                            ):
                                recorded += 1
                        
                        self.feedback.log_session(
                            params=self.active_params,
                            pnl=pnl_pct,
                            trades_opened=0,
                            trades_closed=total,
                            win_rate=win_rate
                        )
                        symbols = [r[1] for r in closed_this_session]
                        self.logger.info(
                            "Feedback logged: %d closed trades (%d trade rows), P&L=%.2f%%, "
                            "WR=%.0f%%, symbols=%s" % (total, recorded, pnl_pct, win_rate * 100, symbols))
                    else:
                        self.logger.info("Feedback: no trades closed this session — skipping log")
                except Exception as fe:
                    self.logger.warning("Feedback logging failed (non-fatal): %s" % str(fe))

            self.logger.info(f"Trading session completed. Report saved to {report_file}")
            return report

        except Exception as e:
            self.logger.error(f"Trading session failed: {e}")
            return f"Trading session failed: {e}"
        finally:
            self._stop_trade_updates_monitor()


def run_paper_trader_test():
    """Test paper trader functionality"""
    import tempfile
    
    # Create temporary directory
    temp_dir = tempfile.mkdtemp()
    
    try:
        # Initialize paper trader
        import config; trader = PaperTrader(base_dir=temp_dir, alpaca_api_key=config.ALPACA_API_KEY, alpaca_secret=config.ALPACA_SECRET_KEY)
        
        print("✅ Paper trader initialized")
        
        # Test signal generation (demo mode)
        signal = trader.generate_trading_signals('AAPL', 'MACD Crossover')
        if signal:
            print(f"✅ Generated signal: {signal}")
        
        # Test trading session
        report = trader.run_trading_session()
        print("✅ Trading session completed")
        print("\nTrading Report:")
        print(report)
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
    
    finally:
        # Cleanup
        import shutil
        shutil.rmtree(temp_dir)
        print("✅ Test cleanup completed")


if __name__ == '__main__':
    run_paper_trader_test()
