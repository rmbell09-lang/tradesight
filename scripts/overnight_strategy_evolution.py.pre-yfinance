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
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Callable, List
import pandas as pd
import numpy as np

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from strategy_lab.backtest import BacktestEngine, rsi_mean_reversion, simple_ma_crossover
from strategy_lab.tournament import get_builtin_strategies
from strategy_lab.ai_engine import create_test_data
from data.alpaca_client import AlpacaClient

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

# Setup logging
log_dir = Path(__file__).parent.parent / 'logs'
log_dir.mkdir(exist_ok=True)

log_file = log_dir / f"overnight_evolution_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

# Guard: must have real Alpaca keys (not running via SSH without Keychain context)
_alpaca_key = os.environ.get('ALPACA_API_KEY', '')
if not _alpaca_key:
    print('ERROR: ALPACA_API_KEY not set. Run via launchd (com.luckyai.tradesight-optimizer) for Keychain access.')
    print('Exiting to prevent synthetic data run.')
    sys.exit(1)


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('OvernightEvolution')


class ParameterTuner:
    """Optimize strategy parameters through backtesting"""
    
    def __init__(self, training_data: pd.DataFrame):
        self.training_data = training_data
        self.backtest_engine = BacktestEngine(initial_balance=500.0)
        self.results = []
    
    def create_rsi_variant(self, oversold: int, overbought: int, size: float,
                           stop_loss_pct: float, take_profit_pct: float,
                           max_holding_bars: int):
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
                if sma50 is not None and not pd.isna(sma50) and price < sma50 * 0.97:
                    return None  # In downtrend — skip RSI oversold signal
                entry_price = price
                return {
                    'action': 'buy',
                    'size': size,
                    'stop_loss': entry_price * (1.0 - stop_loss_pct),
                    'take_profit': entry_price * (1.0 + take_profit_pct)
                }
            
            return None
        
        return rsi_variant
    
    def test_parameter_grid(self, base_name: str) -> List[Dict]:
        """
        Test expanded grid of RSI parameters including stop loss, take profit,
        and max holding period. Finds true optimum across full search space.
        """
        logger.info("Testing expanded parameter grid for RSI Mean Reversion...")
        logger.info("Variables: RSI thresholds, position size, stop loss %, take profit %, max holding bars")
        
        results = []
        
        # RSI thresholds
        oversold_values  = [25, 28, 30, 33]        # 4 values
        overbought_values = [65, 68, 72, 75]         # 4 values
        size_values       = [0.25, 0.30, 0.35]       # 3 positions max, so 25-35% each is reasonable
        
        # Risk/reward parameters — TP must be reachable within max_holding_bars on 1H bars.
        # SPY/large-caps move 0.5-2% per day; 10 bars ≈ 1.5 trading days.
        # Old values (6/9/12%) never triggered → dead parameter. Fixed.
        stop_loss_values     = [0.03, 0.05, 0.08]    # 2% removed — too tight for 1H bars, always stops out    # 2%, 5%, 8% stop loss
        take_profit_values   = [0.05, 0.08, 0.12]   # realistic targets: 5%, 8%, 12%   # 1.5%, 3%, 5% take profit (intraday-realistic)
        
        # NEW: max holding period (0 = unlimited)
        holding_bars_values  = [0, 10, 20]            # unlimited, 10 bars, 20 bars
        
        total = (len(oversold_values) * len(overbought_values) * len(size_values) *
                 len(stop_loss_values) * len(take_profit_values) * len(holding_bars_values))
        logger.info(f"Total combinations to test: {total}")
        
        test_count = 0
        for oversold in oversold_values:
            for overbought in overbought_values:
                for size in size_values:
                    for sl_pct in stop_loss_values:
                        for tp_pct in take_profit_values:
                            for hold_bars in holding_bars_values:
                                test_count += 1
                                
                                # FILTER 1: TP must be >= 1.5x SL (minimum positive expectancy)
                                # Prevents negative-expectancy setups even with high win rates
                                if tp_pct < sl_pct * 1.5:
                                    continue
                                
                                variant = self.create_rsi_variant(
                                    oversold, overbought, size,
                                    sl_pct, tp_pct, hold_bars
                                )
                                
                                backtest_results = self.backtest_engine.run_backtest(
                                    self.training_data,
                                    variant,
                                    asset_name=(
                                        f"RSI_OS{oversold}_OB{overbought}_"
                                        f"S{size:.2f}_SL{sl_pct:.2f}_"
                                        f"TP{tp_pct:.2f}_H{hold_bars}"
                                    )
                                )
                                
                                metrics = backtest_results['metrics']
                                
                                # FILTER 2: Hard reject negative PnL strategies
                                # No negative-return strategy can ever be "optimized" — period
                                if metrics['total_pnl_pct'] < 0:
                                    continue
                                
                                # Composite score: balanced across PnL, Sharpe, win rate, and trade count.
                                # PnL is normalized more sensitively now (div 20 vs 50) to reward real gains.
                                # Sharpe ratio is the primary signal — it measures risk-adjusted returns.
                                # Trade count bonus rewards param sets that generate actual signals
                                # (a strategy with 0 trades scores 0 regardless of metrics).
                                trades = max(metrics['total_trades'], 0)
                                pnl_normalized = min(metrics['total_pnl_pct'] / 20.0, 3.0)  # more sensitive — 5% PnL = 0.25 score
                                sharpe_score   = metrics['sharpe_ratio'] / 3.0              # normalize to ~1 at Sharpe=3
                                win_rate_score = metrics['win_rate'] / 100.0
                                trade_bonus    = min(trades / 20.0, 1.0)                    # bonus for 20+ trades
                                
                                # Penalize strategies with fewer than 3 trades (unreliable stats)
                                trade_penalty = 0.0 if trades >= 3 else (0.5 * (3 - trades) / 3.0)
                                
                                composite_score = (
                                    pnl_normalized * 0.30 +   # 30% PnL (capped, normalized)
                                    sharpe_score   * 0.40 +   # 40% Sharpe (risk-adjusted)
                                    win_rate_score * 0.20 +   # 20% win rate
                                    trade_bonus    * 0.10     # 10% trade frequency bonus
                                ) - trade_penalty
                                
                                results.append({
                                    'oversold':       oversold,
                                    'overbought':     overbought,
                                    'position_size':  size,
                                    'stop_loss_pct':  sl_pct,
                                    'take_profit_pct': tp_pct,
                                    'max_holding_bars': hold_bars,
                                    'pnl_pct':        metrics['total_pnl_pct'],
                                    'sharpe':         metrics['sharpe_ratio'],
                                    'win_rate':       metrics['win_rate'],
                                    'composite_score': composite_score
                                })
                                
                                if test_count % 50 == 0:
                                    logger.info(f"  Tested {test_count}/{total} combinations...")
        
        results.sort(key=lambda x: x['composite_score'], reverse=True)
        logger.info(f"Tested {test_count} total parameter combinations")

        # FEEDBACK BOOST: raise scores for params with good live trading history
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
                
                variant = self.create_rsi_variant(
                    best_params['oversold'], best_params['overbought'],
                    best_params['position_size'], best_params['stop_loss_pct'],
                    best_params['take_profit_pct'], best_params['max_holding_bars']
                )
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


def get_latest_tournament_winner() -> Optional[Dict]:
    """Get the most recently won strategy from tournament history DB, with hardcoded fallback."""
    db_path = Path(__file__).parent.parent / "src" / "data" / "tournament_history.db"
    
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
                logger.info(f"Latest tournament winner from DB: {row[0]} (score: {row[1]:.4f})")
                return {
                    'name': row[0],
                    'score': row[1],
                    'base_params': {
                        'oversold': 30,
                        'overbought': 70,
                        'position_size': 0.60,
                        'stop_loss_pct': 0.07,
                        'take_profit_pct': 0.08,
                        'max_holding_bars': 0
                    }
                }
        except Exception as e:
            logger.warning(f"Could not read tournament DB: {e}")
    
    # Fallback to hardcoded default
    winner = {
        'name': 'RSI Mean Reversion',
        'score': 0.3655,
        'base_params': {
            'oversold': 30,
            'overbought': 70,
            'position_size': 0.60,
            'stop_loss_pct': 0.07,
            'take_profit_pct': 0.08,
            'max_holding_bars': 0
        }
    }
    
    logger.info(f"Using default tournament winner: {winner['name']} (no DB history found)")
    return winner


def optimize_winner_strategy(winner: Dict) -> Dict:
    """
    Use expanded parameter tuning to optimize the tournament winner.
    Tests RSI thresholds, position sizing, stop loss, take profit, and holding period.
    """
    
    logger.info(f"Starting expanded parameter optimization of {winner['name']}...")
    
    # Try to fetch REAL market data from Alpaca, fall back to synthetic
    alpaca_key = os.environ.get('ALPACA_API_KEY', '')
    alpaca_secret = os.environ.get('ALPACA_SECRET_KEY', '')
    
    training_datasets = {}
    data_source = 'synthetic'
    
    if alpaca_key and alpaca_secret:
        try:
            client = AlpacaClient(api_key=alpaca_key, secret_key=alpaca_secret, paper=True)
            # Test against multiple real stocks for robustness
            symbols = [
                # SWING TRADE WATCHLIST - matches paper trader's 20-stock list
                # Broad enough for robust cross-validation, no volatile outliers
                'SPY', 'QQQ',                       # Broad market ETFs
                'AAPL', 'MSFT', 'GOOGL', 'AMZN',   # Tech mega-cap
                'META',                              # Tech
                'JPM', 'BAC', 'V', 'MA',            # Financials
                'JNJ', 'PFE',                        # Healthcare
                'XOM', 'CVX',                        # Energy
                'WMT', 'COST', 'HD',                # Consumer/Retail
                'KO', 'DIS',                         # Consumer staples + media
            ]
            for sym in symbols:
                try:
                    # Use 1Day bars for optimizer: IEX free tier only returns ~30 days
                    # of 1H history, making training windows too short for RSI to trigger.
                    # Daily bars give 500 data points (2 years) — deep enough for reliable stats.
                    df = client.get_historical_data(sym, days=500, timeframe='1Day')
                    if df is not None and len(df) >= 50:
                        training_datasets[sym] = df
                        logger.info(f"  Loaded {len(df)} bars of real data for {sym}")
                except Exception as e:
                    logger.warning(f"  Failed to fetch {sym}: {e}")
            
            if training_datasets:
                data_source = 'alpaca'
                logger.info(f"Using REAL market data from Alpaca ({len(training_datasets)} symbols)")
            else:
                logger.warning("Alpaca returned no data — falling back to synthetic")
        except Exception as e:
            logger.warning(f"Alpaca connection failed: {e} — falling back to synthetic")
    else:
        logger.warning("No Alpaca API keys found — using synthetic data")
    
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
    
    logger.info(
        f"Primary optimization symbol: {primary_symbol} | "        f"Total bars: {len(full_primary_data)} | "        f"Training (70%): {len(training_data)} bars | "        f"OOS holdout (30%): {len(full_primary_data) - train_split} bars | "        f"Source: {data_source}"
    )
    
    # Baseline backtest with original strategy
    backtest_engine = BacktestEngine(initial_balance=500.0)
    baseline_results = backtest_engine.run_backtest(
        training_data,
        rsi_mean_reversion,
        asset_name=winner['name']
    )
    baseline_metrics = baseline_results['metrics']
    baseline_pnl    = baseline_metrics['total_pnl_pct']
    baseline_sharpe = baseline_metrics['sharpe_ratio']
    
    logger.info(f"Baseline {winner['name']}:")
    logger.info(f"  PnL: {baseline_pnl:.2f}%")
    logger.info(f"  Sharpe: {baseline_sharpe:.4f}")
    logger.info(f"  Win Rate: {baseline_metrics['win_rate']:.2f}%")
    
    # Run expanded parameter optimization
    tuner = ParameterTuner(training_data)
    optimized_results = tuner.test_parameter_grid(winner['name'])
    
    if optimized_results:
        best = optimized_results[0]
        
        improvement_pnl    = best['pnl_pct'] - baseline_pnl
        improvement_sharpe = best['sharpe'] - baseline_sharpe
        
        logger.info(f"Best Optimized Parameters:")
        logger.info(f"  RSI Oversold:       {best['oversold']}  (was {winner['base_params']['oversold']})")
        logger.info(f"  RSI Overbought:     {best['overbought']}  (was {winner['base_params']['overbought']})")
        logger.info(f"  Position Size:      {best['position_size']:.2f}  (was {winner['base_params']['position_size']:.2f})")
        logger.info(f"  Stop Loss %:        {best['stop_loss_pct']*100:.0f}%  (was {winner['base_params']['stop_loss_pct']*100:.0f}%)")
        logger.info(f"  Take Profit %:      {best['take_profit_pct']*100:.0f}%  (was {winner['base_params']['take_profit_pct']*100:.0f}%)")
        logger.info(f"  Max Holding Bars:   {best['max_holding_bars'] if best['max_holding_bars'] > 0 else 'unlimited'}  (was unlimited)")
        
        logger.info(f"Optimized Performance:")
        logger.info(f"  PnL: {best['pnl_pct']:.2f}% (improvement: {improvement_pnl:+.2f}%)")
        logger.info(f"  Sharpe: {best['sharpe']:.4f} (improvement: {improvement_sharpe:+.4f})")
        logger.info(f"  Win Rate: {best['win_rate']:.2f}%")
        
        # Cross-validate best params against all symbols
        if len(training_datasets) > 1:
            logger.info("")
            logger.info(f"Walk-forward OOS validation across {len(training_datasets)} symbols (last 30% of each, unseen by optimizer)...")
            cv_results = tuner.cross_validate(best, training_datasets)
            if cv_results:
                avg_pnl = sum(r["pnl_pct"] for r in cv_results.values()) / len(cv_results)
                avg_sharpe = sum(r["sharpe"] for r in cv_results.values()) / len(cv_results)
                avg_trades = sum(r.get("trades",0) for r in cv_results.values()) / len(cv_results)
                logger.info(f"OOS avg: PnL={avg_pnl:.2f}% Sharpe={avg_sharpe:.4f} Trades/symbol={avg_trades:.1f}")
                logger.info("NOTE: OOS PnL is the honest number. Lower than training = overfit.")
            # cv_results kept for report (was set to None - bug fixed)
        
        return {
            'winner': winner['name'],
            'version': 'v3-real-data',
            'baseline': {
                'pnl_pct': float(baseline_pnl),
                'sharpe': float(baseline_sharpe),
                'win_rate': float(baseline_metrics['win_rate']),
                'parameters': winner['base_params']
            },
            'optimized': {
                'pnl_pct': float(best['pnl_pct']),
                'sharpe': float(best['sharpe']),
                'win_rate': float(best['win_rate']),
                'parameters': {
                    'oversold':         int(best['oversold']),
                    'overbought':       int(best['overbought']),
                    'position_size':    float(best['position_size']),
                    'stop_loss_pct':    float(best['stop_loss_pct']),
                    'take_profit_pct':  float(best['take_profit_pct']),
                    'max_holding_bars': int(best['max_holding_bars'])
                },
                'composite_score': float(best['composite_score'])
            },
            'improvement': {
                'pnl_pct': float(improvement_pnl),
                'sharpe': float(improvement_sharpe)
            },
            'top_5_variants': [
                {
                    'parameters': {
                        'oversold':         int(r['oversold']),
                        'overbought':       int(r['overbought']),
                        'position_size':    float(r['position_size']),
                        'stop_loss_pct':    float(r['stop_loss_pct']),
                        'take_profit_pct':  float(r['take_profit_pct']),
                        'max_holding_bars': int(r['max_holding_bars'])
                    },
                    'pnl_pct': float(r['pnl_pct']),
                    'sharpe': float(r['sharpe']),
                    'win_rate': float(r['win_rate']),
                    'score': float(r['composite_score'])
                }
                for r in optimized_results[:5]
            ],
            'search_space': {
                'total_combinations': len(optimized_results),
                'parameters_varied': [
                    'oversold', 'overbought', 'position_size',
                    'stop_loss_pct', 'take_profit_pct', 'max_holding_bars'
                ]
            },
            'data_source': data_source,
            'symbols_tested': list(training_datasets.keys()),
            'primary_symbol': primary_symbol,
            'cross_validation': cv_results,
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
    print(f"   Combinations tested: {search.get('total_combinations', '?')}")
    print(f"   Parameters varied: {', '.join(search.get('parameters_varied', []))}")
    
    bp = baseline['parameters']
    print(f"\n📈 Baseline Parameters & Performance:")
    print(f"   RSI:  Oversold={bp.get('oversold',30)}  Overbought={bp.get('overbought',70)}")
    print(f"   Risk: SL={bp.get('stop_loss_pct',0.07)*100:.0f}%  TP={bp.get('take_profit_pct',0.08)*100:.0f}%  MaxHold={'unlimited' if not bp.get('max_holding_bars') else str(bp['max_holding_bars'])+' bars'}")
    print(f"   Size: {bp.get('position_size',0.60):.2f}")
    print(f"   PnL:       {baseline['pnl_pct']:>7.2f}%")
    print(f"   Sharpe:    {baseline['sharpe']:>7.4f}")
    print(f"   Win Rate:  {baseline['win_rate']:>7.2f}%")
    
    op = optimized['parameters']
    print(f"\n✨ Optimized Parameters & Performance:")
    print(f"   RSI:  Oversold={op['oversold']}  Overbought={op['overbought']}")
    print(f"   Risk: SL={op['stop_loss_pct']*100:.0f}%  TP={op['take_profit_pct']*100:.0f}%  MaxHold={'unlimited' if not op['max_holding_bars'] else str(op['max_holding_bars'])+' bars'}")
    print(f"   Size: {op['position_size']:.2f}")
    print(f"   PnL:       {optimized['pnl_pct']:>7.2f}%")
    print(f"   Sharpe:    {optimized['sharpe']:>7.4f}")
    print(f"   Win Rate:  {optimized['win_rate']:>7.2f}%")
    print(f"   Score:     {optimized['composite_score']:>7.4f}")
    
    print(f"\n🚀 Improvement:")
    print(f"   PnL:    {improvement['pnl_pct']:>+7.2f}%")
    print(f"   Sharpe: {improvement['sharpe']:>+7.4f}")
    
    if improvement['pnl_pct'] > 0:
        print(f"\n✅ SUCCESS: Optimization improved the strategy!")
    else:
        print(f"\n⚠️  Best variant didn't beat baseline - keeping original params")
    
    if 'top_5_variants' in results:
        print(f"\n📋 Top 5 Parameter Combinations:")
        for i, variant in enumerate(results['top_5_variants'], 1):
            p = variant['parameters']
            hold_str = 'unlim' if not p['max_holding_bars'] else f"{p['max_holding_bars']}bar"
            print(f"   {i}. OS:{p['oversold']} OB:{p['overbought']} Sz:{p['position_size']:.2f} "
                  f"SL:{p['stop_loss_pct']*100:.0f}% TP:{p['take_profit_pct']*100:.0f}% "
                  f"Hold:{hold_str} → PnL:{variant['pnl_pct']:>7.2f}% Score:{variant['score']:.4f}")
    
    print("\n" + "="*75)


def main():
    """Main overnight optimization routine"""
    logger.info("="*75)
    logger.info("🌙 OVERNIGHT STRATEGY OPTIMIZATION v2 - STARTING")
    logger.info("="*75)
    
    try:
        winner = get_latest_tournament_winner()
        if not winner:
            logger.error("No tournament winner found")
            return False
        
        results = optimize_winner_strategy(winner)
        # Champion/Challenger evaluation
        if _CHAMPION_AVAILABLE and _FEEDBACK_AVAILABLE:
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
