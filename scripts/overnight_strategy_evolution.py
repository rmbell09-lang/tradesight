#!/usr/bin/env python3
"""
Overnight Strategy Evolution - Parameter Optimization v2

Runs after market close, takes the latest tournament winner,
and optimizes its parameters. Results ready by morning.

v2 changes: Expanded parameter search space to include:
- Stop loss percentage (was hardcoded at 7%)
- Take profit percentage (was hardcoded at 8%)
- Max holding period in bars (new - forces exit after N bars)

Usage: python3 overnight_strategy_evolution.py
Schedule: Add to cron: 0 20 * * * cd ~/Projects/TradeSight && python3 scripts/overnight_strategy_evolution.py
"""

import os
import sys
import json
import logging
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Callable, List
from itertools import product
import pandas as pd
import numpy as np
try:
    import yfinance as yf
    _YFINANCE_AVAILABLE = True
except ImportError:
    _YFINANCE_AVAILABLE = False

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from strategy_lab.backtest import BacktestEngine, rsi_mean_reversion, simple_ma_crossover
from strategy_lab.tournament import get_builtin_strategies
from strategy_lab.ai_engine import create_test_data
from data.alpaca_client import AlpacaClient

try:
    from indicators.regime_detector import RegimeDetector, MarketRegime
    _REGIME_AVAILABLE = True
except ImportError:
    _REGIME_AVAILABLE = False

# Feedback tracker for adaptive parameter weighting
try:
    from trading.feedback_tracker import FeedbackTracker
    _FEEDBACK_AVAILABLE = True
except ImportError:
    _FEEDBACK_AVAILABLE = False

try:
    from trading.champion_tracker import ChampionTracker
    _CHAMPION_AVAILABLE = True
except ImportError:
    _CHAMPION_AVAILABLE = False

PROJECT_ROOT = Path(__file__).parent.parent
OPTIMIZER_TARGET = 'RSI Mean Reversion'
SUPPORTED_OPTIMIZER_TARGETS = ('RSI Mean Reversion', 'MACD Crossover', 'Momentum Breakout', 'Bollinger Bounce')
DEFAULT_RSI_PARAMS = {
    'oversold': 30,
    'overbought': 65,
    'position_size': 0.15,
    'stop_loss_pct': 0.05,
    'take_profit_pct': 0.12,
    'max_holding_bars': 0,
    'use_atr': True,
    'trend_buffer': 0.97,
    'volume_min_ratio': 0.0,
}
RSI_PARAM_KEYS = tuple(DEFAULT_RSI_PARAMS.keys())
DEFAULT_MACD_PARAMS = {
    'position_size': 0.40,
    'stop_loss_pct': 0.04,
    'take_profit_pct': 0.08,
    'max_holding_bars': 0,
    'histogram_min': 0.0,
}
MACD_PARAM_KEYS = tuple(DEFAULT_MACD_PARAMS.keys())
DEFAULT_MOMENTUM_PARAMS = {
    'lookback_bars': 10,
    'entry_momentum': 0.03,
    'exit_momentum': -0.02,
    'rsi_ceiling': 70,
    'position_size': 0.50,
    'stop_loss_pct': 0.05,
    'take_profit_pct': 0.10,
    'max_holding_bars': 0,
}
MOMENTUM_PARAM_KEYS = tuple(DEFAULT_MOMENTUM_PARAMS.keys())
DEFAULT_BOLLINGER_PARAMS = {
    'position_size': 0.15,
    'stop_loss_pct': 0.03,
    'take_profit_pct': 0.08,
    'max_holding_bars': 10,
    'exit_band': 'middle',
}
BOLLINGER_PARAM_KEYS = tuple(DEFAULT_BOLLINGER_PARAMS.keys())
STRATEGY_DEFAULTS = {
    'RSI Mean Reversion': DEFAULT_RSI_PARAMS,
    'MACD Crossover': DEFAULT_MACD_PARAMS,
    'Momentum Breakout': DEFAULT_MOMENTUM_PARAMS,
    'Bollinger Bounce': DEFAULT_BOLLINGER_PARAMS,
}
STRATEGY_PARAM_KEYS = {
    'RSI Mean Reversion': RSI_PARAM_KEYS,
    'MACD Crossover': MACD_PARAM_KEYS,
    'Momentum Breakout': MOMENTUM_PARAM_KEYS,
    'Bollinger Bounce': BOLLINGER_PARAM_KEYS,
}
EVIDENCE_SYMBOLS = ('SPY', 'QQQ', 'AAPL', 'JPM')
MIN_NEW_BARS_PER_SYMBOL = 20
MIN_GROWTH_SYMBOLS = 3
MIN_CREDIBLE_CLOSED_TRADES = 20
MAX_FULL_SEARCH_AGE_DAYS = 14

# Setup logging
log_dir = PROJECT_ROOT / 'logs'
log_dir.mkdir(exist_ok=True)

log_file = log_dir / f"overnight_evolution_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('OvernightEvolution')


def load_alpaca_credentials(env: Optional[Dict[str, str]] = None) -> tuple:
    """Load Alpaca credentials from explicit env, then config/Keychain, then process env."""
    if env is not None:
        return env.get('ALPACA_API_KEY', ''), env.get('ALPACA_SECRET_KEY', ''), 'environment'

    try:
        import config as _ts_config
        api_key = getattr(_ts_config, 'ALPACA_API_KEY', '') or ''
        secret_key = getattr(_ts_config, 'ALPACA_SECRET_KEY', '') or ''
        if api_key and secret_key:
            return api_key, secret_key, 'TradeSight config'
    except Exception:
        pass

    return os.environ.get('ALPACA_API_KEY', ''), os.environ.get('ALPACA_SECRET_KEY', ''), 'environment'


def optimizer_env_status(env: Optional[Dict[str, str]] = None) -> Dict:
    """Report whether the runtime has the credentials needed for a real optimizer run."""
    alpaca_key, alpaca_secret, credential_source = load_alpaca_credentials(env)
    return {
        'alpaca_key_present': bool(alpaca_key),
        'alpaca_secret_present': bool(alpaca_secret),
        'credential_source': credential_source,
        'yfinance_available': bool(_YFINANCE_AVAILABLE),
        'feedback_tracker_available': bool(_FEEDBACK_AVAILABLE),
        'champion_tracker_available': bool(_CHAMPION_AVAILABLE),
    }


def require_real_market_data_env(env: Optional[Dict[str, str]] = None) -> None:
    """Fail before the overnight job can silently fall through to weak fallback data."""
    status = optimizer_env_status(env)
    if not status['alpaca_key_present'] or not status['alpaca_secret_present']:
        raise RuntimeError(
            'ALPACA_API_KEY and ALPACA_SECRET_KEY must both be set. '
            'Store them in TradeSight Keychain or pass an explicit env for tests.'
        )


def normalize_rsi_params(params: Optional[Dict]) -> Dict:
    """Return a complete RSI optimizer parameter set with explicit defaults."""
    normalized = dict(DEFAULT_RSI_PARAMS)
    if params:
        normalized.update(params)
    normalized['oversold'] = int(normalized['oversold'])
    normalized['overbought'] = int(normalized['overbought'])
    normalized['position_size'] = float(normalized['position_size'])
    normalized['stop_loss_pct'] = float(normalized['stop_loss_pct'])
    normalized['take_profit_pct'] = float(normalized['take_profit_pct'])
    normalized['max_holding_bars'] = int(normalized['max_holding_bars'])
    normalized['use_atr'] = bool(normalized.get('use_atr', True))
    normalized['trend_buffer'] = float(normalized.get('trend_buffer', 0.97))
    normalized['volume_min_ratio'] = float(normalized.get('volume_min_ratio', 0.0))
    return normalized


def normalize_macd_params(params: Optional[Dict]) -> Dict:
    normalized = dict(DEFAULT_MACD_PARAMS)
    if params:
        normalized.update({k: v for k, v in params.items() if k in DEFAULT_MACD_PARAMS})
    normalized['position_size'] = float(normalized['position_size'])
    normalized['stop_loss_pct'] = float(normalized['stop_loss_pct'])
    normalized['take_profit_pct'] = float(normalized['take_profit_pct'])
    normalized['max_holding_bars'] = int(normalized['max_holding_bars'])
    normalized['histogram_min'] = float(normalized['histogram_min'])
    return normalized


def normalize_momentum_params(params: Optional[Dict]) -> Dict:
    normalized = dict(DEFAULT_MOMENTUM_PARAMS)
    if params:
        normalized.update({k: v for k, v in params.items() if k in DEFAULT_MOMENTUM_PARAMS})
    normalized['lookback_bars'] = int(normalized['lookback_bars'])
    normalized['entry_momentum'] = float(normalized['entry_momentum'])
    normalized['exit_momentum'] = float(normalized['exit_momentum'])
    normalized['rsi_ceiling'] = int(normalized['rsi_ceiling'])
    normalized['position_size'] = float(normalized['position_size'])
    normalized['stop_loss_pct'] = float(normalized['stop_loss_pct'])
    normalized['take_profit_pct'] = float(normalized['take_profit_pct'])
    normalized['max_holding_bars'] = int(normalized['max_holding_bars'])
    return normalized


def normalize_bollinger_params(params: Optional[Dict]) -> Dict:
    normalized = dict(DEFAULT_BOLLINGER_PARAMS)
    if params:
        normalized.update({k: v for k, v in params.items() if k in DEFAULT_BOLLINGER_PARAMS})
    normalized['position_size'] = float(normalized['position_size'])
    normalized['stop_loss_pct'] = float(normalized['stop_loss_pct'])
    normalized['take_profit_pct'] = float(normalized['take_profit_pct'])
    normalized['max_holding_bars'] = int(normalized['max_holding_bars'])
    normalized['exit_band'] = str(normalized['exit_band'])
    return normalized


def optimizer_target_for_winner(winner_name: Optional[str]) -> str:
    """Tune the actual tournament winner when this optimizer supports that family."""
    return winner_name if winner_name in SUPPORTED_OPTIMIZER_TARGETS else OPTIMIZER_TARGET


def normalize_strategy_params(strategy_name: str, params: Optional[Dict]) -> Dict:
    if strategy_name == 'MACD Crossover':
        return normalize_macd_params(params)
    if strategy_name == 'Momentum Breakout':
        return normalize_momentum_params(params)
    if strategy_name == 'Bollinger Bounce':
        return normalize_bollinger_params(params)
    return normalize_rsi_params(params)


def validate_strategy_params(strategy_name: str, params: Dict) -> List[str]:
    if strategy_name == 'MACD Crossover':
        reasons = []
        if not 0.01 <= params['position_size'] <= 0.50:
            reasons.append('position_size must be between 0.01 and 0.50')
        if not 0.01 <= params['stop_loss_pct'] <= 0.20:
            reasons.append('stop_loss_pct must be between 0.01 and 0.20')
        if not 0.02 <= params['take_profit_pct'] <= 0.30:
            reasons.append('take_profit_pct must be between 0.02 and 0.30')
        if params['take_profit_pct'] < params['stop_loss_pct'] * 1.5:
            reasons.append('take_profit_pct must be at least 1.5x stop_loss_pct')
        if not 0 <= params['max_holding_bars'] <= 200:
            reasons.append('max_holding_bars must be between 0 and 200')
        if not 0.0 <= params['histogram_min'] <= 2.0:
            reasons.append('histogram_min must be between 0.0 and 2.0')
        return reasons
    if strategy_name == 'Momentum Breakout':
        reasons = []
        if not 5 <= params['lookback_bars'] <= 40:
            reasons.append('lookback_bars must be between 5 and 40')
        if not 0.005 <= params['entry_momentum'] <= 0.12:
            reasons.append('entry_momentum must be between 0.5% and 12%')
        if not -0.10 <= params['exit_momentum'] < 0:
            reasons.append('exit_momentum must be negative and no lower than -10%')
        if not 50 <= params['rsi_ceiling'] <= 85:
            reasons.append('rsi_ceiling must be between 50 and 85')
        if not 0.01 <= params['position_size'] <= 0.50:
            reasons.append('position_size must be between 0.01 and 0.50')
        if not 0.01 <= params['stop_loss_pct'] <= 0.20:
            reasons.append('stop_loss_pct must be between 0.01 and 0.20')
        if not 0.02 <= params['take_profit_pct'] <= 0.30:
            reasons.append('take_profit_pct must be between 0.02 and 0.30')
        if params['take_profit_pct'] < params['stop_loss_pct'] * 1.5:
            reasons.append('take_profit_pct must be at least 1.5x stop_loss_pct')
        if not 0 <= params['max_holding_bars'] <= 200:
            reasons.append('max_holding_bars must be between 0 and 200')
        return reasons
    if strategy_name == 'Bollinger Bounce':
        reasons = []
        if not 0.01 <= params['position_size'] <= 0.50:
            reasons.append('position_size must be between 0.01 and 0.50')
        if not 0.01 <= params['stop_loss_pct'] <= 0.20:
            reasons.append('stop_loss_pct must be between 0.01 and 0.20')
        if not 0.02 <= params['take_profit_pct'] <= 0.30:
            reasons.append('take_profit_pct must be between 0.02 and 0.30')
        if not 0 <= params['max_holding_bars'] <= 200:
            reasons.append('max_holding_bars must be between 0 and 200')
        if params['exit_band'] not in {'middle', 'upper'}:
            reasons.append('exit_band must be middle or upper')
        return reasons
    return validate_rsi_params(params)


def validate_rsi_params(params: Dict) -> List[str]:
    """Guard against impossible or dangerously loose optimizer seeds."""
    reasons = []
    if not 10 <= params['oversold'] < params['overbought'] <= 90:
        reasons.append('RSI thresholds must satisfy 10 <= oversold < overbought <= 90')
    if not 0.01 <= params['position_size'] <= 0.50:
        reasons.append('position_size must be between 0.01 and 0.50')
    if not 0.01 <= params['stop_loss_pct'] <= 0.20:
        reasons.append('stop_loss_pct must be between 0.01 and 0.20')
    if not 0.02 <= params['take_profit_pct'] <= 0.30:
        reasons.append('take_profit_pct must be between 0.02 and 0.30')
    if params['take_profit_pct'] < params['stop_loss_pct'] * 1.5:
        reasons.append('take_profit_pct must be at least 1.5x stop_loss_pct')
    if not 0 <= params['max_holding_bars'] <= 200:
        reasons.append('max_holding_bars must be between 0 and 200')
    if not 0.85 <= params['trend_buffer'] <= 1.05:
        reasons.append('trend_buffer must be between 0.85 and 1.05')
    if not 0.0 <= params['volume_min_ratio'] <= 2.0:
        reasons.append('volume_min_ratio must be between 0.0 and 2.0')
    return reasons


def build_optimizer_preflight(winner: Optional[Dict] = None,
                              env: Optional[Dict[str, str]] = None) -> Dict:
    """Compact debug report for the optimizer setup before a long overnight run."""
    champion_path = PROJECT_ROOT / 'data' / 'champion.json'
    raw_params = {}
    if champion_path.exists():
        try:
            with open(champion_path) as f:
                raw_params = (json.load(f) or {}).get('params') or {}
        except Exception as e:
            raw_params = {'_load_error': str(e)}

    if winner is None:
        winner = {
            'name': OPTIMIZER_TARGET,
            'base_params': raw_params if '_load_error' not in raw_params else {},
        }

    target_name = optimizer_target_for_winner(winner.get('name'))
    seed_params = winner.get('base_params') or raw_params
    base_params = normalize_strategy_params(target_name, seed_params)
    env_status = optimizer_env_status(env)
    expected_keys = STRATEGY_PARAM_KEYS.get(target_name, RSI_PARAM_KEYS)
    missing_schema_keys = [k for k in expected_keys if k not in seed_params]
    param_errors = validate_strategy_params(target_name, base_params)
    blocking = []
    if not env_status['alpaca_key_present'] or not env_status['alpaca_secret_present']:
        blocking.append('missing Alpaca credentials for real 1H market data')
    if param_errors:
        blocking.extend(param_errors)
    if raw_params.get('_load_error'):
        blocking.append('champion.json could not be loaded')

    return {
        'ok': not blocking,
        'blocking': blocking,
        'warnings': (
            [f"champion params missing explicit keys: {', '.join(missing_schema_keys)}"]
            if missing_schema_keys and raw_params and '_load_error' not in raw_params
            else []
        ),
        'project_root': str(PROJECT_ROOT),
        'optimizer_target': target_name,
        'tournament_winner': winner.get('name'),
        'env': env_status,
        'champion_path': str(champion_path),
        'champion_path_exists': champion_path.exists(),
        'base_params': base_params,
        'param_schema_keys': list(expected_keys),
    }


class ParameterTuner:
    """Optimize strategy parameters through backtesting"""
    
    def __init__(self, training_data: pd.DataFrame, base_params: Optional[Dict] = None,
                 strategy_name: str = OPTIMIZER_TARGET):
        self.training_data = training_data
        self.strategy_name = optimizer_target_for_winner(strategy_name)
        self.base_params = normalize_strategy_params(self.strategy_name, base_params or {})
        self.backtest_engine = BacktestEngine(initial_balance=500.0, slippage_pct=0.0005)
        self.results = []
    
    def create_rsi_variant(self, oversold: int, overbought: int, size: float,
                           stop_loss_pct: float, take_profit_pct: float,
                           max_holding_bars: int, use_atr: bool = True,
                           trend_buffer: float = 0.97, volume_min_ratio: float = 0.0):
        """
        Create RSI Mean Reversion variant with fully configurable parameters.
        
        Args:
            oversold: RSI level to trigger buy (e.g. 28)
            overbought: RSI level to trigger sell/close (e.g. 68)
            size: Position size fraction (e.g. 0.7)
            stop_loss_pct: Stop loss as decimal below entry (e.g. 0.07 = 7%)
            take_profit_pct: Take profit as decimal above entry (e.g. 0.10 = 10%)
            max_holding_bars: Max bars to hold before forced exit (0 = no limit)
        """
        def rsi_variant(data, index, positions):
            if index < 50:
                return None
            
            current = data.iloc[index]
            
            # --- EXIT LOGIC ---
            # Max holding period: force close if held too long
            if positions and max_holding_bars > 0:
                for pos in positions:
                    entry_idx = pos.get('entry_index', index)
                    if (index - entry_idx) >= max_holding_bars:
                        return {'action': 'close'}
            
            # RSI overbought exit
            if current.get('rsi', 50) > overbought and positions:
                return {'action': 'close'}
            
            # --- ENTRY LOGIC ---
            if current.get('rsi', 50) < oversold and not positions:
                # TREND REGIME FILTER: only buy if price is above 50-bar SMA
                # Prevents buying falling knives in sustained downtrends
                sma50 = current.get('sma_50')
                price = current['close']
                if sma50 is not None and not pd.isna(sma50) and price < sma50 * trend_buffer:
                    return None  # In downtrend — skip RSI oversold signal
                
                # VOLUME CONFIRMATION: optionally require at least N% of average volume.
                # This is now part of the optimizer search instead of a dead/pass branch.
                vol = current.get('volume', 0)
                vol_sma = current.get('volume_sma_20', 0)
                if volume_min_ratio > 0 and vol_sma and vol_sma > 0 and vol < vol_sma * volume_min_ratio:
                    return None
                
                # ATR-BASED DYNAMIC STOPS: adapt to each stock's volatility
                atr = current.get('atr_14', None)
                entry_price = price
                if use_atr and atr and not pd.isna(atr) and atr > 0:
                    # Use ATR for stops: SL = 2x ATR below, TP = 3x ATR above
                    # But cap by the percentage params to avoid insane values
                    atr_sl = min(2.0 * atr / entry_price, stop_loss_pct)
                    atr_tp = min(3.0 * atr / entry_price, take_profit_pct)
                    sl_price = entry_price * (1.0 - max(atr_sl, 0.02))
                    tp_price = entry_price * (1.0 + max(atr_tp, 0.03))
                else:
                    sl_price = entry_price * (1.0 - stop_loss_pct)
                    tp_price = entry_price * (1.0 + take_profit_pct)
                
                return {
                    'action': 'buy',
                    'size': size,
                    'stop_loss': sl_price,
                    'take_profit': tp_price
                }
            
            return None
        
        return rsi_variant

    def create_rsi_variant_from_params(self, params: Dict):
        """Build the RSI variant from the exact persisted/selected parameter set."""
        params = normalize_rsi_params(params)
        return self.create_rsi_variant(
            params['oversold'],
            params['overbought'],
            params['position_size'],
            params['stop_loss_pct'],
            params['take_profit_pct'],
            params['max_holding_bars'],
            use_atr=params['use_atr'],
            trend_buffer=params['trend_buffer'],
            volume_min_ratio=params['volume_min_ratio'],
        )

    def create_macd_variant_from_params(self, params: Dict):
        """Build a MACD crossover variant with tunable risk and trigger strictness."""
        params = normalize_macd_params(params)

        def macd_variant(data, index, positions):
            if index < 50:
                return None
            current = data.iloc[index]
            prev = data.iloc[index - 1]

            if positions and params['max_holding_bars'] > 0:
                for pos in positions:
                    entry_idx = pos.get('entry_index', index)
                    if (index - entry_idx) >= params['max_holding_bars']:
                        return {'action': 'close'}

            hist = current.get('macd', 0) - current.get('macd_signal', 0)
            prev_hist = prev.get('macd', 0) - prev.get('macd_signal', 0)
            if (
                current.get('macd', 0) > current.get('macd_signal', 0)
                and prev.get('macd', 0) <= prev.get('macd_signal', 0)
                and hist >= params['histogram_min']
                and not positions
            ):
                return {
                    'action': 'buy',
                    'size': params['position_size'],
                    'stop_loss': current['close'] * (1.0 - params['stop_loss_pct']),
                    'take_profit': current['close'] * (1.0 + params['take_profit_pct']),
                }

            if (
                positions
                and current.get('macd', 0) < current.get('macd_signal', 0)
                and prev.get('macd', 0) >= prev.get('macd_signal', 0)
            ) or (positions and prev_hist > 0 and hist < 0):
                return {'action': 'close'}
            return None

        return macd_variant

    def create_momentum_variant_from_params(self, params: Dict):
        """Build a tunable momentum-breakout variant."""
        params = normalize_momentum_params(params)

        def momentum_variant(data, index, positions):
            lookback = params['lookback_bars']
            if index < max(50, lookback + 1):
                return None
            current = data.iloc[index]
            prior_close = data.iloc[index - lookback]['close']
            if not prior_close:
                return None
            momentum = (current['close'] - prior_close) / prior_close

            if positions and params['max_holding_bars'] > 0:
                for pos in positions:
                    entry_idx = pos.get('entry_index', index)
                    if (index - entry_idx) >= params['max_holding_bars']:
                        return {'action': 'close'}

            if (
                momentum > params['entry_momentum']
                and current.get('rsi', 50) < params['rsi_ceiling']
                and not positions
            ):
                return {
                    'action': 'buy',
                    'size': params['position_size'],
                    'stop_loss': current['close'] * (1.0 - params['stop_loss_pct']),
                    'take_profit': current['close'] * (1.0 + params['take_profit_pct']),
                }

            if momentum < params['exit_momentum'] and positions:
                return {'action': 'close'}
            return None

        return momentum_variant

    def create_bollinger_variant_from_params(self, params: Dict):
        """Build a tunable Bollinger bounce candidate around the real tournament family."""
        params = normalize_bollinger_params(params)

        def bollinger_variant(data, index, positions):
            if index < 50:
                return None
            current = data.iloc[index]

            if positions and params['max_holding_bars'] > 0:
                for pos in positions:
                    entry_idx = pos.get('entry_index', index)
                    if (index - entry_idx) >= params['max_holding_bars']:
                        return {'action': 'close'}

            exit_level = current.get('bb_middle') if params['exit_band'] == 'middle' else current.get('bb_upper')
            if positions and exit_level is not None and not pd.isna(exit_level) and current['close'] >= exit_level:
                return {'action': 'close'}

            lower = current.get('bb_lower')
            if not positions and lower is not None and not pd.isna(lower) and current['close'] <= lower:
                take_profit = current['close'] * (1.0 + params['take_profit_pct'])
                middle = current.get('bb_middle')
                if middle is not None and not pd.isna(middle) and middle > current['close']:
                    take_profit = min(take_profit, float(middle))
                return {
                    'action': 'buy',
                    'size': params['position_size'],
                    'stop_loss': current['close'] * (1.0 - params['stop_loss_pct']),
                    'take_profit': take_profit,
                }
            return None

        return bollinger_variant

    def create_variant_from_params(self, params: Dict):
        if self.strategy_name == 'MACD Crossover':
            return self.create_macd_variant_from_params(params)
        if self.strategy_name == 'Momentum Breakout':
            return self.create_momentum_variant_from_params(params)
        if self.strategy_name == 'Bollinger Bounce':
            return self.create_bollinger_variant_from_params(params)
        return self.create_rsi_variant_from_params(params)

    def _score_candidate_params(self, params: Dict) -> Optional[Dict]:
        scoring_datasets = getattr(self, 'scoring_datasets', {'primary': self.training_data})
        variant = self.create_variant_from_params(params)
        per_symbol_metrics = {}
        for ds_name, ds in scoring_datasets.items():
            backtest_results = self.backtest_engine.run_backtest(
                ds,
                variant,
                asset_name=f"{ds_name}_{self.strategy_name}_candidate"
            )
            per_symbol_metrics[ds_name] = backtest_results['metrics']

        metrics_list = list(per_symbol_metrics.values())
        summary = summarize_backtest_metrics(per_symbol_metrics)
        if summary['pnl_pct'] < 0 or summary['profitable_symbols'] < max(1, int(len(metrics_list) * 0.50)):
            return None

        pnl_score = min(summary['pnl_pct'] / 10.0, 2.0)
        sharpe_score = max(min(summary['sharpe'] / 2.0, 2.0), -1.0)
        win_rate_score = summary['win_rate'] / 100.0
        trade_score = min(summary['total_trades'] / max(20.0, len(metrics_list) * 4.0), 1.0)
        drawdown_penalty = min(max(summary['max_drawdown_pct'], 0) / 20.0, 1.5)
        concentration_penalty = summary['negative_symbols'] / max(len(metrics_list), 1)
        low_trade_penalty = 0.25 if summary['total_trades'] < max(5, len(metrics_list)) else 0.0
        composite_score = (
            pnl_score * 0.25 +
            sharpe_score * 0.30 +
            win_rate_score * 0.15 +
            trade_score * 0.15 -
            drawdown_penalty * 0.10 -
            concentration_penalty * 0.20 -
            low_trade_penalty
        )

        result = {
            **params,
            'pnl_pct': summary['pnl_pct'],
            'sharpe': summary['sharpe'],
            'win_rate': summary['win_rate'],
            'max_drawdown_pct': summary['max_drawdown_pct'],
            'total_trades': summary['total_trades'],
            'profitable_symbols': summary['profitable_symbols'],
            'negative_symbols': summary['negative_symbols'],
            'worst_symbol_pnl': summary['worst_symbol_pnl'],
            'symbols_scored': list(scoring_datasets.keys()),
            'per_symbol_metrics': compact_per_symbol_metrics(per_symbol_metrics),
            'composite_score': composite_score,
        }
        return result

    def _family_grid_candidates(self) -> List[Dict]:
        bp = self.base_params or {}
        if self.strategy_name == 'MACD Crossover':
            values = {
                'position_size': sorted(set([round(bp.get('position_size', 0.30), 2)])),
                'stop_loss_pct': sorted(set([round(bp.get('stop_loss_pct', 0.04), 2), 0.05])),
                'take_profit_pct': sorted(set([round(bp.get('take_profit_pct', 0.08), 2), 0.10])),
                'max_holding_bars': sorted(set([int(bp.get('max_holding_bars', 0)), 10])),
                'histogram_min': sorted(set([round(bp.get('histogram_min', 0.0), 3), 0.05])),
            }
        elif self.strategy_name == 'Momentum Breakout':
            values = {
                'lookback_bars': sorted(set([int(bp.get('lookback_bars', 10)), 8, 15])),
                'entry_momentum': sorted(set([round(bp.get('entry_momentum', 0.03), 3), 0.02, 0.04])),
                'exit_momentum': sorted(set([round(bp.get('exit_momentum', -0.02), 3), -0.03])),
                'rsi_ceiling': sorted(set([int(bp.get('rsi_ceiling', 70)), 75])),
                'position_size': sorted(set([round(bp.get('position_size', 0.30), 2)])),
                'stop_loss_pct': sorted(set([round(bp.get('stop_loss_pct', 0.05), 2), 0.06])),
                'take_profit_pct': sorted(set([round(bp.get('take_profit_pct', 0.10), 2), 0.12])),
                'max_holding_bars': sorted(set([int(bp.get('max_holding_bars', 0)), 10])),
            }
        elif self.strategy_name == 'Bollinger Bounce':
            values = {
                'position_size': sorted(set([round(bp.get('position_size', 0.15), 2), 0.10, 0.20])),
                'stop_loss_pct': sorted(set([round(bp.get('stop_loss_pct', 0.03), 2), 0.05])),
                'take_profit_pct': sorted(set([round(bp.get('take_profit_pct', 0.08), 2), 0.10])),
                'max_holding_bars': sorted(set([int(bp.get('max_holding_bars', 10)), 0, 20])),
                'exit_band': sorted(set([str(bp.get('exit_band', 'middle')), 'upper'])),
            }
        else:
            return []

        keys = list(values.keys())
        return [dict(zip(keys, combo)) for combo in product(*(values[k] for k in keys))]

    def test_family_parameter_grid(self, base_name: str) -> List[Dict]:
        logger.info(f"Testing family-specific parameter grid for {self.strategy_name}...")
        candidates = self._family_grid_candidates()
        results = []
        rejected_schema = 0
        rejected_negative = 0
        for idx, params in enumerate(candidates, 1):
            reasons = validate_strategy_params(self.strategy_name, params)
            if reasons:
                rejected_schema += 1
                continue
            scored = self._score_candidate_params(params)
            if scored is None:
                rejected_negative += 1
                continue
            results.append(scored)
            if idx % 100 == 0:
                logger.info(f"  Tested {idx}/{len(candidates)} combinations...")

        results.sort(key=lambda x: x['composite_score'], reverse=True)
        self.grid_stats = {
            'total_grid_combinations': len(candidates),
            'loop_iterations': len(candidates),
            'evaluated_after_expectancy_filter': len(candidates) - rejected_schema,
            'rejected_expectancy': rejected_schema,
            'rejected_negative_pnl': rejected_negative,
            'profitable_candidates': len(results),
        }
        logger.info(
            f"Grid stats: total={len(candidates)}, rejected_schema={rejected_schema}, "
            f"rejected_negative={rejected_negative}, profitable_candidates={len(results)}"
        )
        return results
    
    def test_parameter_grid(self, base_name: str) -> List[Dict]:
        """
        Test expanded grid of RSI parameters including risk controls plus ATR,
        trend, and volume filters. Scores robustness across multiple symbols.
        """
        if self.strategy_name != 'RSI Mean Reversion':
            return self.test_family_parameter_grid(base_name)

        logger.info("Testing expanded parameter grid for RSI Mean Reversion...")
        logger.info("Variables: RSI thresholds, position size, SL/TP, max hold, ATR stops, trend buffer, volume floor")

        results = []
        evaluated_count = 0
        rejected_negative_count = 0
        rejected_expectancy_count = 0

        bp = self.base_params or {}
        def unique_sorted(vals):
            return sorted(set(vals))

        base_oversold = int(bp.get('oversold', 30))
        base_overbought = int(bp.get('overbought', 70))
        base_size = float(bp.get('position_size', 0.15))
        base_sl = float(bp.get('stop_loss_pct', 0.05))
        base_tp = float(bp.get('take_profit_pct', 0.08))
        base_hold = int(bp.get('max_holding_bars', 0))

        oversold_values = unique_sorted([max(18, base_oversold - 5), base_oversold, min(40, base_oversold + 5)])
        overbought_values = unique_sorted([max(55, base_overbought - 5), base_overbought, min(82, base_overbought + 5)])
        size_values = unique_sorted([round(base_size, 2)])
        stop_loss_values = unique_sorted([round(base_sl, 2), min(0.10, round(base_sl + 0.03, 2))])
        take_profit_values = unique_sorted([round(base_tp, 2), min(0.15, round(base_tp + 0.04, 2))])
        holding_bars_values = unique_sorted([0, 10 if base_hold != 10 else 20])
        use_atr_values = [bool(bp.get('use_atr', True))]
        trend_buffer_values = [float(bp.get('trend_buffer', 0.97))]
        volume_min_ratio_values = unique_sorted([float(bp.get('volume_min_ratio', 0.0)), 0.8])

        total = (len(oversold_values) * len(overbought_values) * len(size_values) *
                 len(stop_loss_values) * len(take_profit_values) * len(holding_bars_values) *
                 len(use_atr_values) * len(trend_buffer_values) * len(volume_min_ratio_values))
        logger.info(f"Total combinations to test: {total}")

        test_count = 0
        scoring_datasets = getattr(self, 'scoring_datasets', {'primary': self.training_data})
        for oversold in oversold_values:
            for overbought in overbought_values:
                for size in size_values:
                    for sl_pct in stop_loss_values:
                        for tp_pct in take_profit_values:
                            for hold_bars in holding_bars_values:
                                for use_atr in use_atr_values:
                                    for trend_buffer in trend_buffer_values:
                                        for volume_min_ratio in volume_min_ratio_values:
                                            test_count += 1
                                            if tp_pct < sl_pct * 1.5:
                                                rejected_expectancy_count += 1
                                                continue

                                            evaluated_count += 1
                                            variant = self.create_rsi_variant(
                                                oversold, overbought, size,
                                                sl_pct, tp_pct, hold_bars,
                                                use_atr=use_atr,
                                                trend_buffer=trend_buffer,
                                                volume_min_ratio=volume_min_ratio,
                                            )

                                            per_symbol_metrics = {}
                                            for ds_name, ds in scoring_datasets.items():
                                                backtest_results = self.backtest_engine.run_backtest(
                                                    ds,
                                                    variant,
                                                    asset_name=(
                                                        f"{ds_name}_RSI_OS{oversold}_OB{overbought}_"
                                                        f"S{size:.2f}_SL{sl_pct:.2f}_TP{tp_pct:.2f}_"
                                                        f"H{hold_bars}_ATR{int(use_atr)}_TB{trend_buffer:.2f}_VOL{volume_min_ratio:.1f}"
                                                    )
                                                )
                                                per_symbol_metrics[ds_name] = backtest_results['metrics']

                                            metrics_list = list(per_symbol_metrics.values())
                                            avg_pnl = sum(m['total_pnl_pct'] for m in metrics_list) / len(metrics_list)
                                            avg_sharpe = sum(m['sharpe_ratio'] for m in metrics_list) / len(metrics_list)
                                            avg_win_rate = sum(m['win_rate'] for m in metrics_list) / len(metrics_list)
                                            avg_drawdown = sum(m.get('max_drawdown_pct', m.get('max_drawdown', 0)) for m in metrics_list) / len(metrics_list)
                                            total_trades = sum(max(m['total_trades'], 0) for m in metrics_list)
                                            negative_symbols = sum(1 for m in metrics_list if m['total_pnl_pct'] < 0)
                                            profitable_symbols = len(metrics_list) - negative_symbols
                                            worst_symbol_pnl = min(m['total_pnl_pct'] for m in metrics_list)

                                            if avg_pnl < 0 or profitable_symbols < max(1, int(len(metrics_list) * 0.50)):
                                                rejected_negative_count += 1
                                                continue

                                            pnl_score = min(avg_pnl / 10.0, 2.0)
                                            sharpe_score = max(min(avg_sharpe / 2.0, 2.0), -1.0)
                                            win_rate_score = avg_win_rate / 100.0
                                            trade_score = min(total_trades / max(20.0, len(metrics_list) * 4.0), 1.0)
                                            drawdown_penalty = min(max(avg_drawdown, 0) / 20.0, 1.5)
                                            concentration_penalty = negative_symbols / max(len(metrics_list), 1)
                                            low_trade_penalty = 0.25 if total_trades < max(5, len(metrics_list)) else 0.0

                                            composite_score = (
                                                pnl_score * 0.25 +
                                                sharpe_score * 0.30 +
                                                win_rate_score * 0.15 +
                                                trade_score * 0.15 -
                                                drawdown_penalty * 0.10 -
                                                concentration_penalty * 0.20 -
                                                low_trade_penalty
                                            )

                                            results.append({
                                                'oversold': oversold,
                                                'overbought': overbought,
                                                'position_size': size,
                                                'stop_loss_pct': sl_pct,
                                                'take_profit_pct': tp_pct,
                                                'max_holding_bars': hold_bars,
                                                'use_atr': bool(use_atr),
                                                'trend_buffer': float(trend_buffer),
                                                'volume_min_ratio': float(volume_min_ratio),
                                                'pnl_pct': avg_pnl,
                                                'sharpe': avg_sharpe,
                                                'win_rate': avg_win_rate,
                                                'max_drawdown_pct': avg_drawdown,
                                                'total_trades': total_trades,
                                                'profitable_symbols': profitable_symbols,
                                                'negative_symbols': negative_symbols,
                                                'worst_symbol_pnl': worst_symbol_pnl,
                                                'symbols_scored': list(scoring_datasets.keys()),
                                                'per_symbol_metrics': {
                                                    k: {
                                                        'pnl_pct': float(v['total_pnl_pct']),
                                                        'sharpe': float(v['sharpe_ratio']),
                                                        'win_rate': float(v['win_rate']),
                                                        'max_drawdown_pct': float(v.get('max_drawdown_pct', v.get('max_drawdown', 0))),
                                                        'trades': int(v['total_trades']),
                                                    }
                                                    for k, v in per_symbol_metrics.items()
                                                },
                                                'composite_score': composite_score,
                                            })

                                            if test_count % 100 == 0:
                                                logger.info(f"  Tested {test_count}/{total} combinations...")

        results.sort(key=lambda x: x['composite_score'], reverse=True)
        self.grid_stats = {
            'total_grid_combinations': total,
            'loop_iterations': test_count,
            'evaluated_after_expectancy_filter': evaluated_count,
            'rejected_expectancy': rejected_expectancy_count,
            'rejected_negative_pnl': rejected_negative_count,
            'profitable_candidates': len(results),
        }
        logger.info(
            f"Grid stats: total={total}, evaluated={evaluated_count}, "
            f"rejected_expectancy={rejected_expectancy_count}, "
            f"rejected_negative={rejected_negative_count}, profitable_candidates={len(results)}"
        )

        if _FEEDBACK_AVAILABLE:
            try:
                feedback = FeedbackTracker(base_dir=str(Path(__file__).parent.parent))
                top_live = feedback.get_top_params(n=20)
                if top_live:
                    logger.info(f"Feedback: {len(top_live)} proven param sets found, boosting scores")
                    for result in results:
                        for live in top_live:
                            lp = live['params']
                            same_os = result['oversold'] == lp.get('oversold')
                            same_ob = result['overbought'] == lp.get('overbought')
                            same_sz = abs(result['position_size'] - lp.get('position_size', 0)) < 0.01
                            same_sl = abs(result['stop_loss_pct'] - lp.get('stop_loss_pct', 0)) < 0.01
                            same_tp = abs(result['take_profit_pct'] - lp.get('take_profit_pct', 0)) < 0.01
                            if same_os and same_ob and same_sz and same_sl and same_tp:
                                boost = min(0.15, live['score'] * 0.1)
                                result['composite_score'] += boost
                                result['feedback_boost'] = round(boost, 4)
                                result['live_avg_pnl'] = round(live['avg_pnl'], 2)
                                result['live_sessions'] = live['times_used']
                                break
                    results.sort(key=lambda x: x['composite_score'], reverse=True)
                    top = results[0]
                    logger.info(
                        f"Top after boost: OS={top['oversold']} OB={top['overbought']} "
                        f"boost={top.get('feedback_boost', 0):.4f} "
                        f"live_pnl={top.get('live_avg_pnl', 'N/A')}"
                    )
                else:
                    logger.info("Feedback: no live data yet, using backtest scores only")
            except Exception as fe:
                logger.warning(f"Feedback boost skipped: {fe}")

        return results

    def cross_validate(self, best_params, datasets):
        """
        True walk-forward + cross-symbol validation.
        
        For each symbol: split data 70/30 (time-based). The optimizer only saw the
        first 70% (training). We validate on the last 30% (unseen future data).
        This is genuine out-of-sample testing — not just running on different tickers
        over the same time period (which can still overfit to a market regime).
        
        Also runs on other symbols for cross-asset robustness.
        """
        cv_results = {}
        for sym, df in datasets.items():
            try:
                # Time-split: first 70% = in-sample (optimizer saw this),
                # last 30% = out-of-sample (optimizer never saw this)
                split_idx = int(len(df) * 0.70)
                oos_data = df.iloc[split_idx:].copy()  # Out-of-sample slice
                
                if len(oos_data) < 50:
                    logger.warning(f"  CV {sym}: skipped — OOS slice too short ({len(oos_data)} bars)")
                    continue
                
                variant = self.create_variant_from_params(best_params)
                bt = BacktestEngine(initial_balance=500.0)
                res = bt.run_backtest(oos_data, variant, asset_name=f"OOS_{sym}")
                m = res['metrics']
                cv_results[sym] = {
                    'pnl_pct': float(m['total_pnl_pct']),
                    'sharpe': float(m['sharpe_ratio']),
                    'win_rate': float(m['win_rate']),
                    'trades': int(m['total_trades']),
                    'oos_bars': len(oos_data),
                    'note': 'out-of-sample (last 30% of data)'
                }
                logger.info(
                    f"  OOS {sym}: PnL={m['total_pnl_pct']:.2f}% "                    f"Sharpe={m['sharpe_ratio']:.4f} WR={m['win_rate']:.2f}% "                    f"Trades={m['total_trades']} ({len(oos_data)} OOS bars)"
                )
            except Exception as e:
                logger.warning(f"  CV {sym}: failed — {e}")
        return cv_results

    def walk_forward_validate(self, full_data: pd.DataFrame, best_params: Dict,
                              n_windows: int = 4, train_pct: float = 0.70) -> Dict:
        """
        Walk-forward validation: split data into N rolling windows,
        test best params on each OOS slice.
        
        Returns dict with per-window results and stability score.
        """
        logger.info(f"Walk-forward validation: {n_windows} windows, {train_pct*100:.0f}% train")
        
        total_bars = len(full_data)
        window_size = total_bars // n_windows
        results = []
        
        for i in range(n_windows):
            start = i * (window_size // 2)  # 50% overlap for more windows
            end = min(start + window_size, total_bars)
            
            if end - start < 100:
                continue
            
            window_data = full_data.iloc[start:end].copy()
            split_idx = int(len(window_data) * train_pct)
            oos_data = window_data.iloc[split_idx:].copy()
            
            if len(oos_data) < 50:
                continue
            
            try:
                variant = self.create_variant_from_params(best_params)
                bt = BacktestEngine(initial_balance=500.0, slippage_pct=0.0005)
                res = bt.run_backtest(oos_data, variant, asset_name=f"WF_window_{i}")
                m = res['metrics']
                
                results.append({
                    'window': i,
                    'oos_bars': len(oos_data),
                    'pnl_pct': float(m['total_pnl_pct']),
                    'sharpe': float(m['sharpe_ratio']),
                    'win_rate': float(m['win_rate']),
                    'trades': int(m['total_trades']),
                })
                logger.info(
                    f"  WF window {i}: PnL={m['total_pnl_pct']:.2f}% "
                    f"Sharpe={m['sharpe_ratio']:.4f} Trades={m['total_trades']}")
            except Exception as e:
                logger.warning(f"  WF window {i} failed: {e}")
        
        if not results:
            return {'stable': False, 'windows': [], 'reason': 'No valid windows'}
        
        # Stability score: what fraction of windows are profitable?
        profitable_windows = sum(1 for r in results if r['pnl_pct'] > 0)
        stability = profitable_windows / len(results)
        avg_pnl = sum(r['pnl_pct'] for r in results) / len(results)
        avg_sharpe = sum(r['sharpe'] for r in results) / len(results)
        
        stable = stability >= 0.60  # At least 60% of windows profitable
        
        logger.info(
            f"Walk-forward: {profitable_windows}/{len(results)} profitable "
            f"(stability={stability:.0%}), avg PnL={avg_pnl:.2f}%, "
            f"avg Sharpe={avg_sharpe:.4f}, STABLE={stable}")
        
        return {
            'stable': stable,
            'stability_pct': round(stability * 100, 1),
            'profitable_windows': profitable_windows,
            'total_windows': len(results),
            'avg_pnl': round(avg_pnl, 2),
            'avg_sharpe': round(avg_sharpe, 4),
            'windows': results,
        }



def select_optimizer_scoring_datasets(training_datasets: Dict[str, pd.DataFrame], max_symbols: int = 10,
                                      as_of: Optional[datetime] = None) -> Dict[str, pd.DataFrame]:
    """Pick a diversified rotating subset for grid scoring so optimization is not SPY-only or fixed-basket overfit."""
    preferred = [
        'SPY', 'QQQ',                       # Broad market ETFs
        'AAPL', 'MSFT', 'GOOGL', 'AMZN',   # Tech
        'JPM', 'BAC', 'V',                 # Financials
        'JNJ', 'PFE',                      # Healthcare
        'XOM', 'CVX',                      # Energy
        'WMT', 'COST', 'HD', 'KO', 'DIS',  # Consumer
        'META', 'MA',
    ]
    available_preferred = [s for s in preferred if s in training_datasets]
    if available_preferred:
        as_of = as_of or datetime.now()
        offset = as_of.toordinal() % len(available_preferred)
        rotated = available_preferred[offset:] + available_preferred[:offset]
        # Keep SPY/QQQ anchored when present, rotate the rest.
        anchored = [s for s in ['SPY', 'QQQ'] if s in available_preferred]
        ordered = anchored + [s for s in rotated if s not in anchored]
    else:
        ordered = list(training_datasets.keys())

    selected = {}
    for sym in ordered:
        if sym in training_datasets and len(training_datasets[sym]) >= 200:
            selected[sym] = training_datasets[sym]
        if len(selected) >= max_symbols:
            break
    if len(selected) < min(4, len(training_datasets)):
        for sym, df in training_datasets.items():
            if sym not in selected and len(df) >= 200:
                selected[sym] = df
            if len(selected) >= max_symbols:
                break
    return selected or training_datasets


def summarize_backtest_metrics(per_symbol_metrics: Dict[str, Dict]) -> Dict:
    """Average per-symbol backtest metrics for fair baseline/challenger comparisons."""
    metrics_list = list(per_symbol_metrics.values())
    if not metrics_list:
        return {
            'pnl_pct': 0.0,
            'sharpe': 0.0,
            'win_rate': 0.0,
            'max_drawdown_pct': 0.0,
            'total_trades': 0,
            'profitable_symbols': 0,
            'negative_symbols': 0,
            'worst_symbol_pnl': 0.0,
        }
    pnl_values = [float(m['total_pnl_pct']) for m in metrics_list]
    return {
        'pnl_pct': sum(pnl_values) / len(metrics_list),
        'sharpe': sum(float(m['sharpe_ratio']) for m in metrics_list) / len(metrics_list),
        'win_rate': sum(float(m['win_rate']) for m in metrics_list) / len(metrics_list),
        'max_drawdown_pct': (
            sum(float(m.get('max_drawdown_pct', m.get('max_drawdown', 0))) for m in metrics_list)
            / len(metrics_list)
        ),
        'total_trades': sum(max(int(m['total_trades']), 0) for m in metrics_list),
        'profitable_symbols': sum(1 for pnl in pnl_values if pnl >= 0),
        'negative_symbols': sum(1 for pnl in pnl_values if pnl < 0),
        'worst_symbol_pnl': min(pnl_values),
    }


def compact_per_symbol_metrics(per_symbol_metrics: Dict[str, Dict]) -> Dict:
    """Persist a compact, JSON-safe view of each symbol's backtest result."""
    return {
        k: {
            'pnl_pct': float(v['total_pnl_pct']),
            'sharpe': float(v['sharpe_ratio']),
            'win_rate': float(v['win_rate']),
            'max_drawdown_pct': float(v.get('max_drawdown_pct', v.get('max_drawdown', 0))),
            'trades': int(v['total_trades']),
        }
        for k, v in per_symbol_metrics.items()
    }


def summarize_data_quality(training_datasets: Dict[str, pd.DataFrame], data_source: str) -> Dict:
    """Separate market-data quality from strategy scoring."""
    symbols = {}
    rejected_sources = []
    for sym, df in training_datasets.items():
        source = getattr(df, 'attrs', {}).get('data_source', data_source)
        bars = int(len(df))
        latest_bar = pd.Timestamp(df.index[-1]).isoformat() if bars else None
        symbols[sym] = {'bars': bars, 'source': source, 'latest_bar': latest_bar}
        if source in ('demo_mode', 'demo_fallback', 'synthetic') or sym.upper().startswith('SYNTHETIC'):
            rejected_sources.append({'symbol': sym, 'source': source})
    usable = [v for v in symbols.values() if v['source'] not in ('demo_mode', 'demo_fallback', 'synthetic') and v['bars'] >= 200]
    return {
        'data_source': data_source,
        'symbols_loaded': len(symbols),
        'symbols_with_200_plus_bars': len(usable),
        'min_bars': min((v['bars'] for v in symbols.values()), default=0),
        'max_bars': max((v['bars'] for v in symbols.values()), default=0),
        'rejected_sources': rejected_sources,
        'promotion_data_ok': len(rejected_sources) == 0 and len(usable) >= 4 and data_source not in ('synthetic', 'demo_fallback', 'demo_mode'),
        'symbols': symbols,
    }


def build_overfit_diagnostics(results: Dict) -> Dict:
    """Promotion diagnostics for multiple-testing and OOS fragility."""
    opt = results.get('optimized', {}) or {}
    cv = results.get('cross_validation', {}) or {}
    search = results.get('search_space', {}) or {}
    trials = max(int(search.get('total_grid_combinations') or search.get('total_combinations') or 1), 1)
    trades = max(int(opt.get('total_trades', 0)), 1)
    sharpe = float(opt.get('sharpe', 0.0))
    baseline_sharpe = float((results.get('baseline') or {}).get('sharpe', 0.0))
    improvement = sharpe - baseline_sharpe
    # Conservative proxy: more trials need a larger Sharpe improvement.
    multiple_testing_penalty = float(np.sqrt(np.log(max(trials, 2))) / np.sqrt(trades))
    deflated_sharpe_margin = improvement - multiple_testing_penalty

    pnl_values = [float(v.get('pnl_pct', 0.0)) for v in cv.values()]
    monte_carlo = {
        'runs': 0,
        'p05_avg_pnl': 0.0,
        'median_avg_pnl': 0.0,
        'pass': False,
        'reason': 'no OOS symbol results',
    }
    if pnl_values:
        rng = np.random.default_rng(42)
        samples = []
        values = np.array(pnl_values, dtype=float)
        for _ in range(1000):
            samples.append(float(rng.choice(values, size=len(values), replace=True).mean()))
        p05 = float(np.percentile(samples, 5))
        median = float(np.percentile(samples, 50))
        monte_carlo = {
            'runs': 1000,
            'p05_avg_pnl': round(p05, 4),
            'median_avg_pnl': round(median, 4),
            'pass': p05 > -0.25 and median > 0,
            'reason': '5th percentile OOS avg PnL must be > -0.25% and median > 0',
        }

    return {
        'trial_count': trials,
        'trade_count': trades,
        'sharpe_improvement': round(improvement, 4),
        'multiple_testing_penalty': round(multiple_testing_penalty, 4),
        'deflated_sharpe_margin': round(deflated_sharpe_margin, 4),
        'deflated_sharpe_pass': deflated_sharpe_margin > 0,
        'monte_carlo': monte_carlo,
    }


def build_regime_report(training_datasets: Dict[str, pd.DataFrame], strategy_name: str) -> Dict:
    if not _REGIME_AVAILABLE:
        return {'available': False, 'pass': True, 'reason': 'regime detector unavailable'}
    spy_data = training_datasets.get('SPY')
    detector = RegimeDetector()
    regime, details = detector.detect_regime(spy_data=spy_data)
    weight = detector.get_strategy_weight(regime, strategy_name)
    return {
        'available': True,
        'regime': getattr(regime, 'value', str(regime)),
        'details': details,
        'strategy_weight': float(weight),
        'pass': weight >= 0.75,
        'reason': 'strategy regime weight must be >= 0.75 for promotion',
    }


def latest_full_optimizer_report() -> tuple:
    """Return the newest real full-search report, ignoring daily evaluation receipts."""
    report_dir = PROJECT_ROOT / 'reports'
    reports = sorted(report_dir.glob('optimization_*.json'))
    full_reports = []
    for report in reports:
        try:
            with open(report) as f:
                data = json.load(f)
            if not data.get('skipped_reason') and data.get('optimized'):
                full_reports.append((report, data))
        except Exception:
            continue
    if not full_reports:
        return None, {}, None

    latest_path, latest = full_reports[-1]
    ts = latest.get('timestamp')
    try:
        latest_dt = datetime.fromisoformat(ts)
    except Exception:
        latest_dt = datetime.fromtimestamp(latest_path.stat().st_mtime)
    return latest_path, latest, latest_dt


def count_credible_closed_trades_since(since: Optional[datetime], db_path: Optional[Path] = None) -> int:
    """Count broker-filled paper closes only; legacy placeholder rows do not open the gate."""
    if since is None:
        return 0
    import sqlite3

    path = db_path or (PROJECT_ROOT / 'data' / 'positions.db')
    if not path.is_file():
        return 0
    try:
        with sqlite3.connect(str(path)) as conn:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(positions)').fetchall()}
            required = {'status', 'exit_time', 'exit_order_id', 'exit_fill_status'}
            if not required.issubset(columns):
                return 0
            row = conn.execute(
                "SELECT COUNT(*) FROM positions "
                "WHERE status='closed' AND exit_time > ? "
                "AND COALESCE(exit_order_id, '') != '' "
                "AND LOWER(COALESCE(exit_fill_status, '')) IN ('filled', 'partially_filled')",
                (since.isoformat(),),
            ).fetchone()
            return int(row[0] if row else 0)
    except Exception as exc:
        logger.warning(f"Credible closed-trade evidence unavailable: {exc}")
        return 0


def evaluate_optimizer_evidence(
    latest_report: Dict,
    latest_timestamp: Optional[datetime],
    current_symbol_bars: Dict[str, int],
    credible_closed_trades: int,
    current_regime: Optional[str],
    now: Optional[datetime] = None,
    fresh_symbol_bars: Optional[Dict[str, int]] = None,
) -> Dict:
    """Open a full search only on new independent evidence, not a calendar rerun."""
    now = now or datetime.now()
    if not latest_report or latest_timestamp is None:
        return {
            'run': True,
            'reasons': ['no prior full optimizer report found'],
            'age_days': None,
            'new_bars': {},
            'growth_symbols': 0,
            'credible_closed_trades': int(credible_closed_trades),
            'regime_changed': False,
            'thresholds': {
                'new_bars_per_symbol': MIN_NEW_BARS_PER_SYMBOL,
                'growth_symbols': MIN_GROWTH_SYMBOLS,
                'credible_closed_trades': MIN_CREDIBLE_CLOSED_TRADES,
                'stale_failsafe_days': MAX_FULL_SEARCH_AGE_DAYS,
            },
        }

    previous_symbols = ((latest_report.get('data_quality') or {}).get('symbols') or {})
    new_bars = dict(fresh_symbol_bars or {})
    if not new_bars:
        new_bars = {
            symbol: max(0, int(count) - int((previous_symbols.get(symbol) or {}).get('bars', count)))
            for symbol, count in current_symbol_bars.items()
        }
    growth_symbols = sum(1 for count in new_bars.values() if count >= MIN_NEW_BARS_PER_SYMBOL)
    previous_regime = str((latest_report.get('regime_report') or {}).get('regime') or '') or None
    regime_changed = bool(current_regime and previous_regime and current_regime != previous_regime)
    age_days = (now - latest_timestamp).total_seconds() / 86400.0
    reasons: List[str] = []
    if growth_symbols >= MIN_GROWTH_SYMBOLS:
        reasons.append(
            f'{growth_symbols} evidence symbols added at least {MIN_NEW_BARS_PER_SYMBOL} real 1H bars'
        )
    if credible_closed_trades >= MIN_CREDIBLE_CLOSED_TRADES:
        reasons.append(f'{credible_closed_trades} credible paper trades closed since last full search')
    if regime_changed:
        reasons.append(f'market regime changed from {previous_regime} to {current_regime}')
    if age_days >= MAX_FULL_SEARCH_AGE_DAYS:
        reasons.append(f'full-search fail-safe age reached {age_days:.2f} days')

    return {
        'run': bool(reasons),
        'reasons': reasons or ['daily evidence collected; full-search thresholds not reached'],
        'age_days': round(age_days, 3),
        'new_bars': new_bars,
        'growth_symbols': growth_symbols,
        'credible_closed_trades': int(credible_closed_trades),
        'previous_regime': previous_regime,
        'current_regime': current_regime,
        'regime_changed': regime_changed,
        'thresholds': {
            'new_bars_per_symbol': MIN_NEW_BARS_PER_SYMBOL,
            'growth_symbols': MIN_GROWTH_SYMBOLS,
            'credible_closed_trades': MIN_CREDIBLE_CLOSED_TRADES,
            'stale_failsafe_days': MAX_FULL_SEARCH_AGE_DAYS,
        },
    }


def collect_daily_market_evidence(symbols=EVIDENCE_SYMBOLS, since: Optional[datetime] = None) -> tuple:
    """Fetch a bounded real-data sample for bar growth and regime evidence."""
    alpaca_key, alpaca_secret, credential_source = load_alpaca_credentials()
    client = AlpacaClient(api_key=alpaca_key, secret_key=alpaca_secret, paper=True)
    datasets: Dict[str, pd.DataFrame] = {}
    summary: Dict[str, Dict] = {}
    for symbol in symbols:
        try:
            df = client.get_historical_data(symbol, days=730, timeframe='1Hour')
            source = getattr(df, 'attrs', {}).get('data_source') if df is not None else None
            if df is None or len(df) < 400 or source in ('demo_mode', 'demo_fallback'):
                summary[symbol] = {'ok': False, 'bars': len(df) if df is not None else 0, 'source': source}
                continue
            df.attrs['data_source'] = 'alpaca_1h'
            datasets[symbol] = df
            fresh_bars = 0
            if since is not None:
                cutoff = pd.Timestamp(since)
                if cutoff.tzinfo is None:
                    cutoff = cutoff.tz_localize('America/New_York')
                cutoff = cutoff.tz_convert('UTC')
                index_utc = pd.to_datetime(df.index, utc=True)
                fresh_bars = int((index_utc > cutoff).sum())
            summary[symbol] = {
                'ok': True,
                'bars': len(df),
                'source': 'alpaca_1h',
                'latest_bar': pd.Timestamp(df.index[-1]).isoformat(),
                'fresh_bars_since_last_full_search': fresh_bars,
            }
        except Exception as exc:
            summary[symbol] = {'ok': False, 'bars': 0, 'source': 'error', 'error': str(exc)}
    return {
        'credential_source': credential_source,
        'symbols_requested': list(symbols),
        'symbols_verified': len(datasets),
        'symbols': summary,
    }, datasets


def run_daily_strategy_tournament() -> Dict:
    """Generate and freeze a daily strategy-family candidate without promoting it."""
    logger.info('Daily evaluation: running strategy-family tournament on real historical data...')
    try:
        import datetime as _dt
        from automation.strategy_automation import StrategyAutomation

        automation = StrategyAutomation()
        session_id = 'daily_' + _dt.datetime.now().strftime('%Y%m%d_%H%M%S')
        result = automation.run_tournament_session(session_id)
        automation.store_session_results(result)
        return {
            'ok': not bool(result.get('error')),
            'session_id': session_id,
            'winner': result.get('winner'),
            'winner_avg_score': result.get('winner_avg_score'),
            'data_source': result.get('data_source', 'unknown'),
            'error': result.get('error'),
            'incubator_status': 'frozen_for_forward_evidence',
            'auto_promotion': False,
        }
    except Exception as exc:
        logger.warning(f'Daily strategy tournament failed: {exc}')
        return {
            'ok': False,
            'error': str(exc),
            'incubator_status': 'last_known_candidate_retained',
            'auto_promotion': False,
        }


def run_daily_optimizer_evaluation(now: Optional[datetime] = None) -> tuple:
    """Always collect daily evidence; return a full-search gate and frozen candidate."""
    now = now or datetime.now()
    tournament = run_daily_strategy_tournament()
    winner = get_latest_tournament_winner()
    latest_path, latest, latest_dt = latest_full_optimizer_report()
    market, datasets = collect_daily_market_evidence(since=latest_dt)
    strategy_name = optimizer_target_for_winner((winner or {}).get('name'))
    regime = build_regime_report(datasets, strategy_name) if datasets else {
        'available': False,
        'regime': None,
        'pass': False,
        'reason': 'daily evidence market sample unavailable',
    }
    credible_closed = count_credible_closed_trades_since(latest_dt)
    current_bars = {
        symbol: int(item.get('bars', 0))
        for symbol, item in market.get('symbols', {}).items()
        if item.get('ok') is True
    }
    fresh_bars = {
        symbol: int(item.get('fresh_bars_since_last_full_search', 0))
        for symbol, item in market.get('symbols', {}).items()
        if item.get('ok') is True
    }
    gate = evaluate_optimizer_evidence(
        latest,
        latest_dt,
        current_bars,
        credible_closed,
        regime.get('regime'),
        now=now,
        fresh_symbol_bars=fresh_bars,
    )
    if latest_path:
        gate['latest_full_report'] = str(latest_path)
        gate['latest_full_timestamp'] = latest.get('timestamp')
    receipt = {
        'schema': 'tradesight_daily_optimizer_evaluation.v1',
        'timestamp': now.isoformat(),
        'market_evidence': market,
        'tournament': tournament,
        'candidate': {
            'tournament_winner': (winner or {}).get('name'),
            'optimizer_target': strategy_name,
            'status': 'frozen_for_forward_evidence',
            'auto_promotion': False,
        },
        'regime': regime,
        'full_search_gate': gate,
        'live_trading_allowed': False,
    }
    return receipt, winner


def build_promotion_quality_gate(results: Dict) -> Dict:
    """Default is no promotion unless the challenger clearly wins on robust OOS evidence."""
    if 'error' in results:
        return {'eligible': False, 'reasons': [results.get('error', 'optimizer error')]}

    reasons = []
    data_quality = results.get('data_quality', {})
    cv = results.get('cross_validation', {}) or {}
    wf = results.get('walk_forward', {}) or {}
    opt = results.get('optimized', {}) or {}
    improvement = results.get('improvement', {}) or {}
    overfit = results.get('overfit_diagnostics') or build_overfit_diagnostics(results)
    regime = results.get('regime_report') or {}

    if not data_quality.get('promotion_data_ok'):
        reasons.append('market data quality gate failed')
    if improvement.get('pnl_pct', 0) <= 0:
        reasons.append('challenger did not beat baseline PnL')
    if improvement.get('sharpe', 0) <= 0:
        reasons.append('challenger did not beat baseline Sharpe')
    if not wf.get('stable', False):
        reasons.append('walk-forward stability gate failed')
    if cv:
        cv_values = list(cv.values())
        cv_avg_pnl = sum(r.get('pnl_pct', 0) for r in cv_values) / len(cv_values)
        cv_avg_sharpe = sum(r.get('sharpe', 0) for r in cv_values) / len(cv_values)
        profitable = sum(1 for r in cv_values if r.get('pnl_pct', 0) > 0)
        if cv_avg_pnl <= 0:
            reasons.append('OOS average PnL is not positive')
        if cv_avg_sharpe <= 0.25:
            reasons.append('OOS average Sharpe below 0.25')
        if profitable / max(len(cv_values), 1) < 0.60:
            reasons.append('fewer than 60% of OOS symbols profitable')
    else:
        reasons.append('cross-validation did not run')
    if opt.get('total_trades', 0) < max(10, len(opt.get('symbols_scored', [])) * 2):
        reasons.append('not enough trades for reliable statistics')
    if opt.get('max_drawdown_pct', 0) > 20:
        reasons.append('average drawdown above 20%')
    if not overfit.get('deflated_sharpe_pass', False):
        reasons.append('deflated Sharpe proxy did not clear multiple-testing penalty')
    if not (overfit.get('monte_carlo') or {}).get('pass', False):
        reasons.append('Monte Carlo OOS robustness gate failed')
    if regime and not regime.get('pass', True):
        reasons.append('current regime does not favor this strategy enough for promotion')

    return {
        'eligible': len(reasons) == 0,
        'reasons': reasons or ['challenger passed all promotion gates'],
        'thresholds': {
            'data': 'no demo/synthetic data; >=4 symbols with >=200 bars',
            'walk_forward': 'stable=True',
            'oos': 'avg PnL > 0, avg Sharpe > 0.25, >=60% symbols profitable',
            'baseline': 'PnL and Sharpe both improve',
            'risk': 'avg drawdown <= 20%, enough trades',
            'overfit': 'deflated Sharpe margin > 0 and Monte Carlo OOS robustness passes',
            'regime': 'strategy regime weight >= 0.75',
        }
    }


def fetch_yfinance_1h(symbol: str) -> Optional[pd.DataFrame]:
    """
    Fetch 2 years of 1H bars from Yahoo Finance (free, no API key needed).
    Returns DataFrame with columns: open, high, low, close, volume
    yfinance supports up to 730 days of 1H history.
    """
    if not _YFINANCE_AVAILABLE:
        return None
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = yf.download(symbol, period='2y', interval='1h',
                             progress=False, auto_adjust=True)
        if df is None or len(df) < 50:
            return None
        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        df.index = pd.to_datetime(df.index)
        return df
    except Exception as e:
        logger.warning(f"  yfinance fetch failed for {symbol}: {e}")
        return None


def get_latest_tournament_winner() -> Optional[Dict]:
    """Get the most recently won strategy from tournament history DB, with hardcoded fallback."""
    db_path = Path(__file__).parent.parent / "data" / "tournament_history.db"
    
    # Load champion params as seed — optimizer searches AROUND current champion
    # so we converge rather than always restarting from hardcoded defaults
    champion_base = normalize_rsi_params(DEFAULT_RSI_PARAMS)
    champion_path = Path(__file__).parent.parent / 'data' / 'champion.json'
    if champion_path.exists():
        try:
            import json as _json
            with open(champion_path) as _f:
                _champ = _json.load(_f)
            if _champ.get('params'):
                champion_base = normalize_rsi_params(_champ['params'])
                logger.info(f"Seeding optimizer from champion params: {champion_base}")
        except Exception as _ce:
            logger.warning(f"Could not load champion params for seeding: {_ce}")

    # Try to read from actual tournament results
    if db_path.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                "SELECT winner, winner_avg_score FROM tournament_sessions "
                "WHERE status = 'completed' ORDER BY start_time DESC LIMIT 1"
            ).fetchone()
            conn.close()
            
            if row and row[0]:
                target_name = optimizer_target_for_winner(row[0])
                base_seed = champion_base if target_name == 'RSI Mean Reversion' else normalize_strategy_params(target_name, {})
                logger.info(f"Latest tournament winner from DB: {row[0]} (score: {row[1]:.4f})")
                return {
                    'name': row[0],
                    'score': row[1],
                    'base_params': base_seed
                }
        except Exception as e:
            logger.warning(f"Could not read tournament DB: {e}")
    
    # Fallback to champion-seeded defaults
    winner = {
        'name': 'RSI Mean Reversion',
        'score': 0.3655,
        'base_params': champion_base  # always seed from champion, never hardcoded
    }
    
    logger.info(f"Using default tournament winner: {winner['name']} (no DB history found)")
    return winner


def optimize_winner_strategy(winner: Dict) -> Dict:
    """
    Optimize the supported parameter family that actually won the overnight tournament.
    """
    
    target_name = optimizer_target_for_winner(winner.get('name'))
    if winner.get('name') != target_name:
        logger.warning(
            "Tournament winner '%s' is not directly tuneable by this optimizer; optimizing %s instead.",
            winner.get('name'),
            target_name,
        )

    logger.info(f"Starting family-specific parameter optimization of {target_name}...")
    
    # Try to fetch REAL market data from Alpaca, fall back to synthetic
    alpaca_key, alpaca_secret, credential_source = load_alpaca_credentials()
    logger.info(f"Alpaca credentials loaded from {credential_source}")
    
    training_datasets = {}
    data_source = 'synthetic'
    
    # SYMBOLS: matches paper trader watchlist for cross-validation
    symbols = [
        'SPY', 'QQQ',                       # Broad market ETFs
        'AAPL', 'MSFT', 'GOOGL', 'AMZN',   # Tech mega-cap
        'META',                              # Tech
        'JPM', 'BAC', 'V', 'MA',            # Financials
        'JNJ', 'PFE',                        # Healthcare
        'XOM', 'CVX',                        # Energy
        'WMT', 'COST', 'HD',                 # Consumer/Retail
        'KO', 'DIS',                         # Consumer staples + media
    ]

    # PRIMARY: Alpaca 1H when credentials are present.
    # Strategy is designed for 1H bars and launchd injects Alpaca keys via app-launcher.sh.
    min_intraday_bars = 400  # ~8 trading weeks of 1H bars; below this OOS gets too sparse

    if alpaca_key and alpaca_secret:
        logger.info("Fetching 1H bars from Alpaca (primary)...")
        try:
            client = AlpacaClient(api_key=alpaca_key, secret_key=alpaca_secret, paper=True)
            for sym in symbols:
                try:
                    df = client.get_historical_data(sym, days=730, timeframe='1Hour')
                    source = getattr(df, 'attrs', {}).get('data_source') if df is not None else None
                    if source in ('demo_mode', 'demo_fallback'):
                        logger.warning(
                            f"  Alpaca {sym}: rejected {source} data ({getattr(df, 'attrs', {}).get('fallback_reason', 'no reason')})"
                        )
                        continue
                    if df is not None and len(df) >= min_intraday_bars:
                        df.attrs['data_source'] = 'alpaca_1h'
                        training_datasets[sym] = df
                        logger.info(f"  Alpaca {sym}: {len(df)} 1H bars")
                    elif df is not None:
                        logger.warning(
                            f"  Alpaca {sym}: only {len(df)} 1H bars (< {min_intraday_bars}) — skipping as too sparse for optimization"
                        )
                except Exception as e:
                    logger.warning(f"  Alpaca {sym} failed: {e}")
            if len(training_datasets) >= 3:
                data_source = 'alpaca_1h'
                logger.info(f"Using Alpaca 1H data ({len(training_datasets)} symbols, strategy designed for 1H)")
            elif training_datasets:
                logger.warning(
                    f"Alpaca produced only {len(training_datasets)} sufficiently-long symbol(s); trying Yahoo Finance for broader validation"
                )
                training_datasets = {}
            else:
                logger.warning("Alpaca 1H history was too short for reliable optimization; trying Yahoo Finance next")
        except Exception as e:
            logger.warning(f"Alpaca connection failed: {e}")

    # FALLBACK: yfinance 1H if Alpaca unavailable or failed
    if not training_datasets and _YFINANCE_AVAILABLE:
        logger.warning("Alpaca unavailable — falling back to Yahoo Finance 1H bars")
        for sym in symbols:
            df = fetch_yfinance_1h(sym)
            if df is not None and len(df) >= 50:
                training_datasets[sym] = df
                logger.info(f"  yfinance {sym}: {len(df)} 1H bars")
        if training_datasets:
            data_source = 'yfinance_1h'
            logger.info(f"Using yfinance 1H data ({len(training_datasets)} symbols, strategy designed for 1H)")

    if not training_datasets:
        logger.warning("No market data from any source — using synthetic")
    
    # Fallback to synthetic if no real data
    if not training_datasets:
        training_datasets['SYNTHETIC'] = create_test_data(days=500)
        logger.info(f"Created synthetic training data: {len(training_datasets['SYNTHETIC'])} bars")
    
    # Use SPY as primary (broadest market), or first available
    primary_symbol = 'SPY' if 'SPY' in training_datasets else list(training_datasets.keys())[0]
    full_primary_data = training_datasets[primary_symbol]
    
    # WALK-FORWARD SPLIT: optimizer trains on first 70% only.
    # Last 30% is reserved for out-of-sample validation in cross_validate().
    # This enforces true data separation — the optimizer NEVER sees the test period.
    train_split = int(len(full_primary_data) * 0.70)
    training_data = full_primary_data.iloc[:train_split].copy()
    data_quality = summarize_data_quality(training_datasets, data_source)
    finalist_scoring_datasets = {
        sym: df.iloc[:int(len(df) * 0.70)].copy()
        for sym, df in select_optimizer_scoring_datasets(training_datasets, max_symbols=10).items()
        if len(df) >= 200
    }
    if not finalist_scoring_datasets:
        finalist_scoring_datasets = {primary_symbol: training_data}
    scout_scoring_datasets = {
        sym: df.iloc[:int(len(df) * 0.70)].copy()
        for sym, df in select_optimizer_scoring_datasets(training_datasets, max_symbols=5).items()
        if len(df) >= 200
    } or {primary_symbol: training_data}
    scoring_datasets = finalist_scoring_datasets
    
    logger.info(
        f"Primary optimization symbol: {primary_symbol} | "        f"Total bars: {len(full_primary_data)} | "        f"Training (70%): {len(training_data)} bars | "        f"OOS holdout (30%): {len(full_primary_data) - train_split} bars | "        f"Source: {data_source}"
    )
    logger.info(
        f"Data quality: symbols={data_quality['symbols_loaded']}, "        f"usable={data_quality['symbols_with_200_plus_bars']}, "        f"promotion_data_ok={data_quality['promotion_data_ok']}"
    )
    logger.info(f"Optimizer scout symbols: {', '.join(scout_scoring_datasets.keys())}")
    logger.info(f"Optimizer finalist scoring symbols: {', '.join(finalist_scoring_datasets.keys())}")
    
    # Baseline backtest with the current champion parameter set. Score it across
    # the exact same symbol basket as the challenger so "improvement" is fair.
    baseline_params = normalize_strategy_params(target_name, winner.get('base_params', {}))
    baseline_tuner = ParameterTuner(training_data, baseline_params, strategy_name=target_name)
    baseline_per_symbol_metrics = {}
    for ds_name, ds in scoring_datasets.items():
        baseline_variant = baseline_tuner.create_variant_from_params(baseline_params)
        baseline_results = baseline_tuner.backtest_engine.run_backtest(
            ds,
            baseline_variant,
            asset_name=f"{ds_name}_{target_name}_baseline"
        )
        baseline_per_symbol_metrics[ds_name] = baseline_results['metrics']
    baseline_metrics = summarize_backtest_metrics(baseline_per_symbol_metrics)
    baseline_pnl = baseline_metrics['pnl_pct']
    baseline_sharpe = baseline_metrics['sharpe']

    logger.info(f"Baseline {target_name}:")
    logger.info(f"  PnL: {baseline_pnl:.2f}%")
    logger.info(f"  Sharpe: {baseline_sharpe:.4f}")
    logger.info(f"  Win Rate: {baseline_metrics['win_rate']:.2f}%")
    logger.info(f"  Symbols: {', '.join(scoring_datasets.keys())}")
    
    # Run expanded parameter optimization
    tuner = ParameterTuner(training_data, baseline_params, strategy_name=target_name)
    tuner.scoring_datasets = scout_scoring_datasets
    optimized_results = tuner.test_parameter_grid(target_name)
    scout_grid_stats = dict(getattr(tuner, 'grid_stats', {'profitable_candidates': len(optimized_results)}))
    if optimized_results:
        logger.info("Re-scoring top scout finalists across broader finalist basket...")
        finalist_results = []
        tuner.scoring_datasets = finalist_scoring_datasets
        for candidate in optimized_results[:25]:
            candidate_params = normalize_strategy_params(
                target_name,
                {k: candidate[k] for k in STRATEGY_PARAM_KEYS.get(target_name, RSI_PARAM_KEYS) if k in candidate}
            )
            rescored = tuner._score_candidate_params(candidate_params)
            if rescored:
                finalist_results.append(rescored)
        if finalist_results:
            finalist_results.sort(key=lambda x: x['composite_score'], reverse=True)
            optimized_results = finalist_results
            tuner.grid_stats = {
                **scout_grid_stats,
                'scout_symbols': list(scout_scoring_datasets.keys()),
                'finalist_symbols': list(finalist_scoring_datasets.keys()),
                'finalists_rescored': len(finalist_results),
            }
        else:
            logger.warning("No scout finalists survived broader finalist basket; keeping scout ranking for diagnostics")
            tuner.grid_stats = {
                **scout_grid_stats,
                'scout_symbols': list(scout_scoring_datasets.keys()),
                'finalist_symbols': list(finalist_scoring_datasets.keys()),
                'finalists_rescored': 0,
            }
    cv_results = {}
    wf_results = {'stable': False, 'windows': [], 'reason': 'Cross-symbol / walk-forward validation not run'}
    
    if optimized_results:
        best = optimized_results[0]
        param_keys = STRATEGY_PARAM_KEYS.get(target_name, RSI_PARAM_KEYS)
        best_params = normalize_strategy_params(target_name, {k: best[k] for k in param_keys if k in best})
        
        improvement_pnl    = best['pnl_pct'] - baseline_pnl
        improvement_sharpe = best['sharpe'] - baseline_sharpe
        
        logger.info(f"Best Optimized Parameters:")
        for key in param_keys:
            logger.info(f"  {key}: {best_params.get(key)}  (was {baseline_params.get(key)})")
        
        logger.info(f"Optimized Performance:")
        logger.info(f"  PnL: {best['pnl_pct']:.2f}% (improvement: {improvement_pnl:+.2f}%)")
        logger.info(f"  Sharpe: {best['sharpe']:.4f} (improvement: {improvement_sharpe:+.4f})")
        logger.info(f"  Win Rate: {best['win_rate']:.2f}%")
        
        # Cross-validate best params against all symbols
        if len(training_datasets) > 1:
            logger.info("")
            logger.info(f"Walk-forward OOS validation across {len(training_datasets)} symbols (last 30% of each, unseen by optimizer)...")
            cv_results = tuner.cross_validate(best_params, training_datasets)
            if cv_results:
                avg_pnl = sum(r["pnl_pct"] for r in cv_results.values()) / len(cv_results)
                avg_sharpe = sum(r["sharpe"] for r in cv_results.values()) / len(cv_results)
                avg_trades = sum(r.get("trades",0) for r in cv_results.values()) / len(cv_results)
                logger.info(f"OOS avg: PnL={avg_pnl:.2f}% Sharpe={avg_sharpe:.4f} Trades/symbol={avg_trades:.1f}")
                logger.info("NOTE: OOS PnL is the honest number. Lower than training = overfit.")
            # cv_results kept for report (was set to None - bug fixed)
            
            # Walk-forward validation (Task 15) — tests stability across rolling windows
            logger.info("")
            logger.info("Walk-forward validation (Task 15)...")
            wf_results = tuner.walk_forward_validate(full_primary_data, best_params)
            if not wf_results.get('stable', False):
                logger.warning(
                    "WALK-FORWARD UNSTABLE: only %d/%d windows profitable. "
                    "Params may be overfit." % (
                        wf_results.get('profitable_windows', 0),
                        wf_results.get('total_windows', 0)))
        
        return {
            'winner': target_name,  # strategy actually being optimized
            'tournament_winner': winner['name'],  # what won the nightly tournament
            'optimizer_target': target_name,
            'version': 'v5-daily-evidence-triggered-search',
            'baseline': {
                'pnl_pct': float(baseline_pnl),
                'sharpe': float(baseline_sharpe),
                'win_rate': float(baseline_metrics['win_rate']),
                'parameters': baseline_params,
                'max_drawdown_pct': float(baseline_metrics['max_drawdown_pct']),
                'total_trades': int(baseline_metrics['total_trades']),
                'profitable_symbols': int(baseline_metrics['profitable_symbols']),
                'negative_symbols': int(baseline_metrics['negative_symbols']),
                'worst_symbol_pnl': float(baseline_metrics['worst_symbol_pnl']),
                'symbols_scored': list(scoring_datasets.keys()),
                'per_symbol_metrics': compact_per_symbol_metrics(baseline_per_symbol_metrics),
            },
            'optimized': {
                'pnl_pct': float(best['pnl_pct']),
                'sharpe': float(best['sharpe']),
                'win_rate': float(best['win_rate']),
                'parameters': best_params,
                'composite_score': float(best['composite_score']),
                'max_drawdown_pct': float(best.get('max_drawdown_pct', 0)),
                'total_trades': int(best.get('total_trades', 0)),
                'profitable_symbols': int(best.get('profitable_symbols', 0)),
                'negative_symbols': int(best.get('negative_symbols', 0)),
                'worst_symbol_pnl': float(best.get('worst_symbol_pnl', 0)),
                'symbols_scored': best.get('symbols_scored', []),
                'per_symbol_metrics': best.get('per_symbol_metrics', {})
            },
            'improvement': {
                'pnl_pct': float(improvement_pnl),
                'sharpe': float(improvement_sharpe)
            },
            'top_5_variants': [
                {
                    'parameters': normalize_strategy_params(target_name, {k: r[k] for k in param_keys if k in r}),
                    'pnl_pct': float(r['pnl_pct']),
                    'sharpe': float(r['sharpe']),
                    'win_rate': float(r['win_rate']),
                    'score': float(r['composite_score']),
                    'max_drawdown_pct': float(r.get('max_drawdown_pct', 0)),
                    'total_trades': int(r.get('total_trades', 0)),
                    'profitable_symbols': int(r.get('profitable_symbols', 0)),
                    'negative_symbols': int(r.get('negative_symbols', 0)),
                    'worst_symbol_pnl': float(r.get('worst_symbol_pnl', 0)),
                    'symbols_scored': r.get('symbols_scored', [])
                }
                for r in optimized_results[:5]
            ],
            'search_space': {
                **getattr(tuner, 'grid_stats', {'profitable_candidates': len(optimized_results)}),
                # Backward-compatible key: historically meant profitable candidates, not full grid size.
                'total_combinations': len(optimized_results),
                'parameters_varied': list(param_keys)
            },
            'data_quality': data_quality,
            'data_source': data_source,
            'symbols_tested': list(training_datasets.keys()),
            'symbols_scored': list(scoring_datasets.keys()),
            'primary_symbol': primary_symbol,
            'cross_validation': cv_results,
            'walk_forward': wf_results,
            'regime_report': build_regime_report(training_datasets, target_name),
            'promotion_quality_gate': build_promotion_quality_gate({
                'data_quality': data_quality,
                'optimized': {
                    'total_trades': int(best.get('total_trades', 0)),
                    'symbols_scored': best.get('symbols_scored', []),
                    'max_drawdown_pct': float(best.get('max_drawdown_pct', 0)),
                },
                'improvement': {
                    'pnl_pct': float(improvement_pnl),
                    'sharpe': float(improvement_sharpe),
                },
                'cross_validation': cv_results,
                'walk_forward': wf_results,
                'overfit_diagnostics': build_overfit_diagnostics({
                    'baseline': {'sharpe': float(baseline_sharpe)},
                    'optimized': {
                        'sharpe': float(best['sharpe']),
                        'total_trades': int(best.get('total_trades', 0)),
                    },
                    'cross_validation': cv_results,
                    'search_space': {
                        **getattr(tuner, 'grid_stats', {'profitable_candidates': len(optimized_results)}),
                        'total_combinations': len(optimized_results),
                    },
                }),
            }),
            'timestamp': datetime.now().isoformat()
        }
    else:
        logger.warning("No optimized variants generated")
        return {
            'winner': winner['name'],
            'error': 'No optimized variants generated',
            'timestamp': datetime.now().isoformat()
        }


def save_optimization_report(results: Dict) -> Path:
    """Save optimization results to JSON report"""
    report_dir = Path(__file__).parent.parent / 'reports'
    report_dir.mkdir(exist_ok=True)
    
    report_file = report_dir / f"optimization_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    with open(report_file, 'w') as f:
        json.dump(results, f, indent=2)
    
    # Save per-symbol OOS performance for paper trader filtering
    cv = results.get('cross_validation', {})
    if cv:
        symbol_perf = {}
        for sym, metrics in cv.items():
            oos_pnl = metrics.get('pnl_pct', 0)
            oos_sharpe = metrics.get('sharpe', 0)
            oos_trades = metrics.get('trades', 0)
            symbol_perf[sym] = {
                'oos_pnl_pct': oos_pnl,
                'oos_sharpe': oos_sharpe,
                'oos_win_rate': metrics.get('win_rate', 0),
                'oos_trades': oos_trades,
                'tradeable': oos_pnl > 0 and oos_sharpe > 0 and oos_trades >= 1,
                'tradeable_rule': 'oos_pnl_pct > 0 and oos_sharpe > 0 and oos_trades >= 1',
                'updated': datetime.now().isoformat()
            }
        perf_file = Path(__file__).parent.parent / 'data' / 'symbol_performance.json'
        with open(perf_file, 'w') as f:
            json.dump(symbol_perf, f, indent=2)
        logger.info("Symbol performance saved: %d symbols (%d tradeable)" % (
            len(symbol_perf), sum(1 for v in symbol_perf.values() if v['tradeable'])))
    
    logger.info(f"Report saved: {report_file}")
    return report_file


def print_summary(results: Dict):
    """Pretty-print optimization results"""
    print("\n" + "="*75)
    print("🧠 OVERNIGHT STRATEGY OPTIMIZATION v2 - RESULTS")
    print("="*75)
    
    if 'error' in results:
        print(f"\n❌ Error: {results['error']}")
        return
    
    winner    = results['winner']
    baseline  = results['baseline']
    optimized = results['optimized']
    improvement = results['improvement']
    search    = results.get('search_space', {})
    
    print(f"\n📊 Strategy: {winner}  [{results.get('version', 'v1')}]")
    print(f"   Timestamp: {results['timestamp']}")
    tested = search.get('total_grid_combinations', search.get('total_combinations', '?'))
    survivors = search.get('total_combinations')
    print(f"   Combinations tested: {tested}")
    if survivors is not None and survivors != tested:
        print(f"   Profitable candidates: {survivors}")
    print(f"   Parameters varied: {', '.join(search.get('parameters_varied', []))}")
    
    bp = baseline['parameters']
    print(f"\n📈 Baseline Parameters & Performance:")
    print(f"   Params: {json.dumps(bp, sort_keys=True)}")
    print(f"   PnL:       {baseline['pnl_pct']:>7.2f}%")
    print(f"   Sharpe:    {baseline['sharpe']:>7.4f}")
    print(f"   Win Rate:  {baseline['win_rate']:>7.2f}%")
    
    op = optimized['parameters']
    print(f"\n✨ Optimized Parameters & Performance:")
    print(f"   Params: {json.dumps(op, sort_keys=True)}")
    print(f"   PnL:       {optimized['pnl_pct']:>7.2f}%")
    print(f"   Sharpe:    {optimized['sharpe']:>7.4f}")
    print(f"   Win Rate:  {optimized['win_rate']:>7.2f}%")
    print(f"   Score:     {optimized['composite_score']:>7.4f}")
    
    print(f"\n🚀 Improvement:")
    print(f"   PnL:    {improvement['pnl_pct']:>+7.2f}%")
    print(f"   Sharpe: {improvement['sharpe']:>+7.4f}")
    
    promotion_gate = results.get('promotion_quality_gate') or {}
    if promotion_gate.get('eligible') and not str(results.get('champion_decision', '')).startswith('NO'):
        print("\n✅ PROMOTION ELIGIBLE: challenger cleared every robustness gate")
    else:
        reasons = '; '.join(promotion_gate.get('reasons', [])) or results.get('champion_decision', 'not promotion eligible')
        print(f"\n⛔ NO PROMOTION: {reasons}")
    
    if 'top_5_variants' in results:
        print(f"\n📋 Top 5 Parameter Combinations:")
        for i, variant in enumerate(results['top_5_variants'], 1):
            p = variant['parameters']
            print(f"   {i}. {json.dumps(p, sort_keys=True)} → PnL:{variant['pnl_pct']:>7.2f}% Score:{variant['score']:.4f}")
    
    print("\n" + "="*75)


def print_preflight(preflight: Dict):
    """Human-readable optimizer setup report."""
    print("\n" + "="*75)
    print("TRADE SIGHT OPTIMIZER PREFLIGHT")
    print("="*75)
    print(f"Status: {'OK' if preflight['ok'] else 'BLOCKED'}")
    print(f"Target: {preflight['optimizer_target']}")
    print(f"Champion path: {preflight['champion_path']}")
    print(
        "Alpaca env: "
        f"key={'yes' if preflight['env']['alpaca_key_present'] else 'no'}, "
        f"secret={'yes' if preflight['env']['alpaca_secret_present'] else 'no'}"
    )
    print(f"yfinance fallback installed: {'yes' if preflight['env']['yfinance_available'] else 'no'}")
    print(f"Champion tracker installed: {'yes' if preflight['env']['champion_tracker_available'] else 'no'}")
    print(f"Feedback tracker installed: {'yes' if preflight['env']['feedback_tracker_available'] else 'no'}")
    print(f"Base params: {json.dumps(preflight['base_params'], sort_keys=True)}")
    if preflight['warnings']:
        print("\nWarnings:")
        for warning in preflight['warnings']:
            print(f"  - {warning}")
    if preflight['blocking']:
        print("\nBlocking:")
        for reason in preflight['blocking']:
            print(f"  - {reason}")
    print("="*75)


def main(argv: Optional[List[str]] = None):
    """Main overnight optimization routine"""
    parser = argparse.ArgumentParser(description='TradeSight overnight family-aware optimizer')
    parser.add_argument(
        '--preflight',
        action='store_true',
        help='check optimizer setup, champion params, and runtime env without fetching market data',
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='emit JSON for --preflight',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='run the full optimizer even if the scheduled cadence guard would skip',
    )
    parser.add_argument(
        '--daily-evaluation-only',
        action='store_true',
        help='collect the daily tournament/data/regime receipt but never open a full search',
    )
    args = parser.parse_args(argv)

    logger.info("="*75)
    logger.info("🌙 OVERNIGHT STRATEGY OPTIMIZATION v2 - STARTING")
    logger.info("="*75)
    
    try:
        if args.preflight:
            winner = get_latest_tournament_winner()
            preflight = build_optimizer_preflight(winner)
            if args.json:
                print(json.dumps(preflight, indent=2, sort_keys=True))
            else:
                print_preflight(preflight)
            return bool(preflight['ok'])

        require_real_market_data_env()

        daily_evaluation, winner = run_daily_optimizer_evaluation()
        evidence_gate = daily_evaluation['full_search_gate']
        if args.force:
            evidence_gate = dict(evidence_gate)
            evidence_gate['run'] = True
            evidence_gate['reasons'] = list(evidence_gate.get('reasons', [])) + ['manual --force override']
            daily_evaluation['full_search_gate'] = evidence_gate
        if args.daily_evaluation_only or not evidence_gate.get('run', False):
            decision = (
                'DAILY EVALUATION ONLY: full search suppressed for verification'
                if args.daily_evaluation_only
                else 'NO FULL OPTIMIZATION: evidence thresholds not reached'
            )
            logger.info("Daily evaluation complete: %s", '; '.join(evidence_gate.get('reasons', [])))
            skipped = {
                'version': 'v5-daily-evidence-triggered-search',
                'timestamp': datetime.now().isoformat(),
                'daily_evaluation': daily_evaluation,
                'full_search_decision': decision,
                'champion_decision': 'NO PROMOTION: daily candidate remains frozen for forward evidence',
            }
            report_file = save_optimization_report(skipped)
            logger.info(f"Daily evaluation report: {report_file}")
            return True

        if not winner:
            logger.error("No tournament winner found")
            return False
        
        results = optimize_winner_strategy(winner)
        results['daily_evaluation'] = daily_evaluation
        results['full_search_trigger'] = evidence_gate
        # Champion/Challenger evaluation. Default is NO PROMOTION unless the quality gate passes.
        if 'error' not in results:
            results['overfit_diagnostics'] = build_overfit_diagnostics(results)
        promotion_gate = build_promotion_quality_gate(results)
        results['promotion_quality_gate'] = promotion_gate
        if not promotion_gate.get('eligible'):
            logger.warning("Promotion gate rejected challenger: %s" % '; '.join(promotion_gate.get('reasons', [])))
            try:
                champion = ChampionTracker(base_dir=str(Path(__file__).parent.parent)) if _CHAMPION_AVAILABLE else None
                current = champion.get_champion()['params'] if champion and champion.get_champion() else None
            except Exception:
                current = None
            results['champion_decision'] = 'NO PROMOTION: ' + '; '.join(promotion_gate.get('reasons', []))
            results['active_params'] = current
        elif results.get('optimizer_target') != 'RSI Mean Reversion':
            results['champion_decision'] = (
                f"NO AUTO PROMOTION: {results.get('optimizer_target')} candidate passed optimizer gates, "
                "but ChampionTracker/data/champion.json is still RSI-parameter shaped"
            )
            results['active_params'] = None
            logger.warning(results['champion_decision'])
        elif _CHAMPION_AVAILABLE and _FEEDBACK_AVAILABLE:
            try:
                from trading.feedback_tracker import FeedbackTracker
                feedback = FeedbackTracker(base_dir=str(Path(__file__).parent.parent))
                champion = ChampionTracker(base_dir=str(Path(__file__).parent.parent))
                
                # Safely extract optimized parameters
                opt = results.get('optimized', {})
                opt_params = opt.get('parameters', {})
                
                if not opt_params or 'oversold' not in opt_params:
                    raise ValueError(f"Optimized results missing expected parameters. Keys: {list(opt_params.keys()) if opt_params else 'none'}")
                
                best_params = {
                    'oversold': opt_params['oversold'],
                    'overbought': opt_params['overbought'],
                    'position_size': opt_params['position_size'],
                    'stop_loss_pct': opt_params['stop_loss_pct'],
                    'take_profit_pct': opt_params['take_profit_pct'],
                    'max_holding_bars': opt_params['max_holding_bars'],
                    'use_atr': opt_params.get('use_atr', False),
                    'trend_buffer': opt_params.get('trend_buffer', 0.97),
                    'volume_min_ratio': opt_params.get('volume_min_ratio', 0.0),
                }
                best_score = opt.get('composite_score', 0)
                active_params, decision = champion.evaluate_challenger(
                    challenger_params=best_params,
                    challenger_backtest_score=best_score,
                    feedback_tracker=feedback
                )
                results['champion_decision'] = decision
                results['active_params'] = active_params
                logger.info(f"Champion decision: {decision}")
                logger.info(f"Champion status: {champion.status()}")
            except Exception as ce:
                logger.warning(f"Champion tracker failed (non-fatal): {ce}")

        # DATA SOURCE VALIDATION: refuse to promote from synthetic data
        if results.get('data_source', 'synthetic') in ('synthetic', 'demo_fallback'):
            logger.error(
                "DATA SOURCE GUARD: Optimization ran on %s data — "
                "refusing to update champion. Fix data feed." % results.get('data_source'))
            results['champion_decision'] = 'REJECTED: synthetic/demo data'
            results['active_params'] = None
        
        report_file = save_optimization_report(results)
        print_summary(results)
        
        logger.info("🌙 OVERNIGHT OPTIMIZATION v2 - COMPLETE")
        logger.info(f"Report: {report_file}")
        
        return True
        
    except Exception as e:
        logger.error(f"Optimization failed: {e}", exc_info=True)
        print(f"\n❌ Error: {e}")
        return False


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
