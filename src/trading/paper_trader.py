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

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from data.alpaca_client import AlpacaClient
from trading.position_manager import PositionManager, PortfolioState
from automation.strategy_automation import StrategyAutomation
from strategy_lab.tournament import get_builtin_strategies
from indicators.technical_indicators import TechnicalIndicators

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


class PaperTrader:
    """Orchestrates paper trading with tournament-winning strategies"""
    
    def __init__(self, base_dir: str = None, alpaca_api_key: str = None, 
                 alpaca_secret: str = None):
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent.parent
        self.data_dir = self.base_dir / 'data'
        self.logs_dir = self.base_dir / 'logs'
        
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
            'max_per_correlation_group': 2
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
        """Generate trading signals for a symbol using a specific strategy"""
        try:
            # Get market data
            data = self.alpaca.get_historical_data(symbol, days=500, timeframe='1Hour')
            if data is None or len(data) < 20:
                self.logger.warning(f"Insufficient data for {symbol}")
                return None
            
            # Calculate technical indicators using module-level import
            indicators = TechnicalIndicators()
            indicators.data = data
            
            # Calculate all indicators
            indicators_data = indicators.calculate_all(data)
            
            # Apply strategy-specific logic
            signal = self._apply_strategy_logic(strategy_name, data, indicators_data, symbol=symbol)
            
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
                            indicators_data: Dict, symbol: str = '') -> Optional[Dict]:
        """Apply specific strategy logic to generate buy/sell signals"""
        
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
                                'side': 'short',
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
                    # RSI thresholds from champion params
                    oversold_thresh = self.active_params.get("oversold", 33)
                    overbought_thresh = self.active_params.get("overbought", 70)
                    if current_rsi < oversold_thresh:
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
                            "side": "short",
                            "confidence": min(0.80, (current_rsi - overbought_thresh) / overbought_thresh + 0.60),
                            "reason": f"RSI overbought: {current_rsi:.1f}"
                        }
        
        elif strategy_name == 'Bollinger Bounce':
            # Get Bollinger Bands data from indicators_data dict
            indicators_dict = indicators_data.get("indicators", {})
            bb_data = indicators_dict.get("bollinger_bands", pd.DataFrame())
            
            if isinstance(bb_data, pd.DataFrame) and len(bb_data) >= 1:
                current_bb = bb_data.iloc[-1]
                upper_band = current_bb.get('upper_band', float('inf')) if hasattr(current_bb, 'get') else current_bb['upper_band']
                lower_band = current_bb.get('lower_band', float('-inf')) if hasattr(current_bb, 'get') else current_bb['lower_band']
                
                # Price near lower band (buy signal)
                if current_price <= lower_band * 1.02:
                    signal = {
                        'action': 'buy',
                        'side': 'long',
                        'confidence': 0.72,
                        'reason': 'Price near Bollinger lower band'
                    }
                # Price near upper band (sell signal)
                elif current_price >= upper_band * 0.98:
                    signal = {
                        'action': 'sell',
                        'side': 'short',
                        'confidence': 0.68,
                        'reason': 'Price near Bollinger upper band'
                    }
        
        # Add more strategy implementations as needed...
        
        return signal
    
    def execute_signal(self, signal: Dict) -> bool:
        """Execute a trading signal"""
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

            # Calculate position size
            quantity = self.position_manager.calculate_position_size(symbol, strategy, current_price)
            
            # Cap by real Alpaca buying power (prevents "insufficient buying power" errors)
            if getattr(self, "_alpaca_synced", False) and hasattr(self, "_real_buying_power"):
                max_affordable = self._real_buying_power / current_price * 0.95  # 5% safety margin
                if quantity > max_affordable:
                    self.logger.info(f"Capping {symbol} qty from {quantity:.4f} to {max_affordable:.4f} (real buying power: ${self._real_buying_power:.2f})")
                    quantity = round(max_affordable, 6)
            
            if quantity <= 0:
                self.logger.info(f"No position size available for {symbol} {strategy}")
                return False
            
            # Execute the trade
            if action == 'buy':
                success = self._execute_buy_order(symbol, strategy, side, quantity, current_price)
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
                          quantity: float, price: float) -> bool:
        """Execute a buy order"""
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
                    entry_price=fill_price
                )
                if not success:
                    self.logger.error(f"[BuyOrder] position_manager.open_position FAILED for {symbol}")
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
                            "status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (datetime.now().isoformat(), fill_price, pnl, "closed", pos_id)
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
                    closed = self._execute_sell_order(symbol, strategy, current_price)
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
    def scan_and_trade(self):
        """Main trading loop: scan for signals and execute trades"""
        try:
            self.logger.info("Starting trading scan...")

            # CHECK STOP LOSS / TAKE PROFIT FIRST — before any new signals
            self._check_stop_loss_take_profit()
            
            # Get winning strategies
            winning_strategies = self.get_latest_tournament_winners()
            
            if not winning_strategies:
                # FALLBACK: Use built-in strategies with champion params when no tournament winners
                self.logger.info("No recent tournament winners — using fallback strategies with champion params")
                winning_strategies = [
                    ('RSI Mean Reversion', 0.60),
                    ('MACD Crossover', 0.60),
                    ('Bollinger Bounce', 0.55),
                ]
            
            # Check portfolio state
            portfolio_state = self.position_manager.get_portfolio_state()
            self.logger.info(f"Portfolio: ${portfolio_state.total_value:,.2f}, {portfolio_state.position_count} positions")
            
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

    def run_trading_session(self):
        """Run a complete trading session"""
        try:
            self.logger.info("=== Starting TradeSight Paper Trading Session ===")
            
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


def run_paper_trader_test():
    """Test paper trader functionality"""
    import tempfile
    
    # Create temporary directory
    temp_dir = tempfile.mkdtemp()
    
    try:
        # Initialize paper trader
        trader = PaperTrader(base_dir=temp_dir)
        
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
