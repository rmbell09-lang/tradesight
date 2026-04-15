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
                 alpaca_secret: str = None):
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
        self.position_manager = PositionManager(base_dir=self.base_dir)
        self.automation = StrategyAutomation(base_dir=self.base_dir)
        
        # Active params — loaded from ChampionTracker (optimizer winning params)
        self.active_params: Dict = {}
        if _CHAMPION_AVAILABLE:
            try:
                # .resolve() ensures correct project root regardless of how the module was imported
                _ct = ChampionTracker(base_dir=str(Path(__file__).resolve().parent.parent.parent))
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
            # Use TradeSight root (parent of src/) so feedback DB matches overnight optimizer
            self.feedback = FeedbackTracker(base_dir=str(Path(__file__).resolve().parent.parent.parent))
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

        # Initialize Alpaca client (demo mode if no API keys)
        if alpaca_api_key and alpaca_secret:
            self.alpaca = AlpacaClient(api_key=alpaca_api_key, secret_key=alpaca_secret, paper=True)
        else:
            self.alpaca = AlpacaClient()  # Demo mode
        
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
            'min_strategy_confidence': 0.55,  # Slightly higher bar for fewer, better trades
            'max_concurrent_trades': 5,       # 5 positions for more data (fractional shares)
            'trade_frequency_hours': 4,       # Check for signals every 4 hours
            'position_hold_days': 5,          # Hold positions 1-5 days (swing trade)
            'min_hold_hours': 24,             # MINIMUM 24hr hold - prevents day trades (PDT)
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
        }
    
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
                
                # Filter by confidence threshold and deduplicate
                seen_strategies = set()
                qualified_winners = []
                
                for winner, score, start_time in winners:
                    if score >= self.config['min_strategy_confidence'] and winner not in seen_strategies:
                        qualified_winners.append((winner, score))
                        seen_strategies.add(winner)
                
                self.logger.info(f"Found {len(qualified_winners)} qualified strategies from recent tournaments (tournament-sourced)")
                return qualified_winners
                
        except Exception as e:
            self.logger.error(f"Failed to get tournament winners: {e}")
            return []
    
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
            except Exception:
                pass
        
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
                            except Exception:
                                pass  # Volume data unavailable — no adjustment
                            
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
                            except:
                                pass
                            
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
                    entry_order_id=order_result.get('order_id'),
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
                    except Exception:
                        pass  # Column may not exist yet — added in migration
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

            # PDT GUARD: check minimum hold time before closing
            min_hold = self.config.get('min_hold_hours', 24)
            if not force:
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

            # Use DELETE /v2/positions/{symbol} — closes full Alpaca position, handles fractional shares
            order_result = self.alpaca.close_full_position(symbol)

            if order_result and "error" not in order_result:
                fill_price = order_result.get("fill_price") or price

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

                # Close ALL open DB positions for this symbol+strategy
                with sqlite3.connect(db_path) as conn:
                    open_positions = conn.execute(
                        "SELECT id, side, quantity, entry_price FROM positions "
                        "WHERE symbol=? AND strategy=? AND status=?",
                        (symbol, strategy, "open")
                    ).fetchall()
                    for pos_id, side, qty, entry_price in open_positions:
                        pnl = (fill_price - entry_price) * qty if side == "long" else (entry_price - fill_price) * qty
                        conn.execute(
                            "UPDATE positions SET exit_time=?, exit_price=?, realized_pnl=?, "
                            "status=?, exit_order_id=?, exit_fill_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (
                                datetime.now().isoformat(),
                                fill_price,
                                pnl,
                                "closed",
                                order_result.get('order_id'),
                                order_result.get('status', 'closed'),
                                pos_id,
                            )
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

        if self._daily_loss_limit_reached:
            return True

        limit = self.config.get('daily_loss_limit', 15.0)
        try:
            today_start = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(str(db_path)) as conn:
                row = conn.execute(
                    "SELECT SUM(realized_pnl) FROM positions WHERE status='closed' AND exit_time >= ?",
                    (today_start,)
                ).fetchone()
            daily_pnl = float(row[0]) if row and row[0] is not None else 0.0
            if daily_pnl <= -limit:
                self._daily_loss_limit_reached = True
                self._cb_date = today
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
                    except Exception:
                        pass
                return True
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
                    "FROM positions WHERE status = 'open'"
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
                        except Exception:
                            pass
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
        Check for pre-market gaps on open positions (Task 19).
        Called at start of scan_and_trade.
        
        Gap > 3% up: consider taking partial profit
        Gap > 3% down: tighten stop loss
        """
        try:
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                open_positions = conn.execute(
                    "SELECT symbol, entry_price, side, quantity FROM positions WHERE status='open'"
                ).fetchall()
            
            if not open_positions:
                return
            
            for symbol, entry_price, side, quantity in open_positions:
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
                    gap_pct = (current_price - prev_close) / prev_close
                    
                    if abs(gap_pct) > 0.03:  # 3% gap threshold
                        direction = "UP" if gap_pct > 0 else "DOWN"
                        self.logger.warning(
                            f"[Gap] {symbol} gapped {direction} {gap_pct*100:.1f}%: "
                            f"prev_close=${prev_close:.2f} → current=${current_price:.2f}")
                        
                        # Gap down on long position: tighten stop to -2% from current
                        if gap_pct < -0.03 and side == 'long':
                            self.logger.info(
                                f"[Gap] Tightening stop for {symbol} (gap down on long)")
                        
                        # Gap up on long position: log potential profit taking
                        elif gap_pct > 0.03 and side == 'long':
                            unrealized = (current_price - entry_price) * quantity
                            self.logger.info(
                                f"[Gap] {symbol} gap up — unrealized=${unrealized:.2f}, "
                                f"consider taking partial profit")
                
                except Exception as _ge:
                    self.logger.debug(f"[Gap] Check failed for {symbol}: {_ge}")
        
        except Exception as e:
            self.logger.warning(f"[Gap] Pre-market gap check failed: {e}")

    def scan_and_trade(self):
        """Main trading loop: scan for signals and execute trades"""
        try:
            self.logger.info("Starting trading scan...")

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
                    ('MACD Crossover', 0.60),
                    ('Bollinger Bounce', 0.55),
                    ('VWAP Reversion', 0.55),
                    ('Opening Range Breakout', 0.50),
                    ('Mean Reversion Pairs', 0.50),
                ]
            
            # Check portfolio state
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
                            except Exception:
                                pass
            
            if self._circuit_breaker_until and datetime.now() < self._circuit_breaker_until:
                self.logger.warning(
                    f"[CIRCUIT BREAKER] Active until {self._circuit_breaker_until.isoformat()}. "
                    f"No new trades. Existing SL/TP still monitored.")
                return
            elif self._circuit_breaker_until:
                self.logger.info("[CIRCUIT BREAKER] Cooldown expired. Resuming trading.")
                self._circuit_breaker_until = None
            
            if portfolio_state.position_count >= self.config['max_concurrent_trades']:
                self.logger.info("Maximum concurrent trades reached")
                return
            
            # Get current market prices
            price_data = {}
            for symbol in self.config['trading_symbols']:
                try:
                    quote = self.alpaca.get_quote(symbol)
                    if quote:
                        price_data[symbol] = quote.last
                except Exception as e:
                    self.logger.warning(f"Failed to get quote for {symbol}: {e}")
            
            # Update existing positions
            if price_data:
                self.position_manager.update_positions(price_data)
            
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
            
            # Recent closed trades from DB (last 7 days)
            try:
                db_path = self.position_manager.data_dir / 'positions.db'
                cutoff = (datetime.now() - timedelta(days=7)).isoformat()
                with sqlite3.connect(db_path) as conn:
                    closed = conn.execute(
                        "SELECT symbol, side, entry_price, exit_price, realized_pnl, strategy, exit_time "
                        "FROM positions WHERE status='closed' AND exit_time > ? "
                        "ORDER BY exit_time DESC LIMIT 10",
                        (cutoff,)
                    ).fetchall()
                if closed:
                    report_lines.append("")
                    report_lines.append("📉 Recently Closed Trades (Last 7 Days)")
                    report_lines.append("-" * 40)
                    for sym, side, entry, exit_p, pnl, strat, exit_t in closed:
                        exit_str = "$%.2f" % exit_p if exit_p else "N/A"
                        pnl_str = "$%.2f" % pnl if pnl else "$0.00"
                        report_lines.append(
                            "%s %s: entry=$%.2f exit=%s P&L=%s (%s)" % (
                                sym, side, entry or 0, exit_str, pnl_str, strat))
            except Exception as _rpe:
                self.logger.warning("Could not fetch closed trades for report: %s" % str(_rpe))
            
            return "\n".join(report_lines)
            
        except Exception as e:
            self.logger.error(f"Failed to generate trading report: {e}")
            return "Failed to generate trading report"
    

    def _sync_with_alpaca(self):
        """Sync local state with Alpaca reality - call at start of every session."""
        try:
            account = self.alpaca.get_account()
            if not account:
                self.logger.warning("Could not fetch Alpaca account - skipping sync")
                return
            
            real_equity = float(account.get("equity", 0))
            real_cash = float(account.get("buying_power", 0))
            real_positions_value = float(account.get("long_market_value", 0))
            
            self.logger.info("Alpaca account: equity=$%.2f, buying_power=$%.2f, positions=$%.2f" % (real_equity, real_cash, real_positions_value))
            
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
            self.position_manager.persist_balance_sync(real_cash)

            # Always run orphan sync (not just when local DB is empty).
            # _sync_orphan_positions handles per-symbol dedup internally.
            if remote_positions:
                self._sync_orphan_positions(remote_positions)

            # Close stale local positions that Alpaca has already exited.
            self._close_stale_positions(remote_symbols)

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
                    "SELECT id, symbol, entry_price, quantity, side FROM positions WHERE status='open'"
                ).fetchall()
                stale = [(row[0], row[1], row[2], row[3], row[4])
                         for row in local_open if row[1] not in remote_symbols]
                if stale:
                    for pos_id, sym, entry_price, quantity, side in stale:
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
                            "exit_price=?, realized_pnl=?, updated_at=? WHERE id=?",
                            (datetime.now().isoformat(), exit_price, realized_pnl,
                             datetime.now().isoformat(), pos_id)
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
            auth_stream = auth_resp[0].get('stream') if isinstance(auth_resp, list) and auth_resp else auth_resp.get('stream')
            auth_msg = auth_resp[0].get('data') if isinstance(auth_resp, list) and auth_resp else auth_resp.get('data')
            if auth_stream != 'authorization' or str(auth_msg).lower() != 'authorized':
                raise RuntimeError(f'websocket auth failed: {auth_resp}')

            ws.send(json.dumps({'action': 'listen', 'data': {'streams': ['trade_updates']}}))
            listen_resp = json.loads(ws.recv())
            payload = listen_resp[0] if isinstance(listen_resp, list) and listen_resp else listen_resp
            streams = payload.get('data', {}).get('streams', []) if isinstance(payload, dict) else []
            if 'trade_updates' not in streams:
                raise RuntimeError(f'trade_updates subscribe failed: {listen_resp}')

            self.logger.info('[WS] Connected to Alpaca trade_updates stream')

            while not stop_event.is_set():
                raw = ws.recv()
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
            except Exception:
                pass

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


    def run_trading_session(self):
        """Run a complete trading session"""
        try:
            self.logger.info("=== Starting TradeSight Paper Trading Session ===")
            self._start_trade_updates_monitor()
            
            # Sync with Alpaca before doing anything
            self._sync_with_alpaca()
            
            # Main trading logic
            self.scan_and_trade()
            
            # Close aged positions
            self.close_aged_positions()
            
            # Generate report
            report = self.generate_trading_report()
            
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
                            "SELECT symbol, entry_price, exit_price, realized_pnl, quantity "
                            "FROM positions WHERE status='closed' AND exit_time > ?",
                            (cutoff,)
                        ).fetchall()
                    
                    if closed_this_session:
                        total_pnl = sum(r[3] or 0 for r in closed_this_session)
                        wins = sum(1 for r in closed_this_session if (r[3] or 0) > 0)
                        total = len(closed_this_session)
                        win_rate = wins / total if total > 0 else 0.0
                        # Calculate P&L as percentage of total position value
                        total_entry_value = sum((r[1] or 0) * (r[4] or 0) for r in closed_this_session)
                        pnl_pct = (total_pnl / max(total_entry_value, 1)) * 100
                        
                        self.feedback.log_session(
                            params=self.active_params,
                            pnl=pnl_pct,
                            trades_opened=0,
                            trades_closed=total,
                            win_rate=win_rate
                        )
                        symbols = [r[0] for r in closed_this_session]
                        self.logger.info(
                            "Feedback logged: %d closed trades, P&L=%.2f%%, "
                            "WR=%.0f%%, symbols=%s" % (total, pnl_pct, win_rate * 100, symbols))
                    else:
                        self.logger.info("Feedback: no trades closed this session — skipping log")
                except Exception as fe:
                    self.logger.warning("Feedback logging failed (non-fatal): %s" % str(fe))

            self.logger.info(f"Trading session completed. Report saved to {report_file}")
            # Append trade-level analysis to report
            try:
                if hasattr(self.position_manager, 'trade_logger') and self.position_manager.trade_logger:
                    trade_analysis = self.position_manager.trade_logger.report(days=30)
                    report = report + "\n\n" + trade_analysis
            except Exception as te:
                self.logger.warning(f"Trade report failed (non-fatal): {te}")

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
