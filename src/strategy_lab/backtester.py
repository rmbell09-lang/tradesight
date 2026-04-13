"""
TradeSight Multi-Asset Backtesting with Anti-Overfitting Framework

Walk-forward validation, Monte Carlo simulation, cross-asset verification,
overfitting detection, and bias warnings.
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Callable, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

from .backtest import BacktestEngine


@dataclass
class WalkForwardResult:
    """Result from one walk-forward fold"""
    fold: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    train_score: float
    test_score: float
    train_trades: int
    test_trades: int
    train_win_rate: float
    test_win_rate: float
    degradation_pct: float  # How much worse test is vs train


@dataclass
class MonteCarloResult:
    """Result from Monte Carlo simulation"""
    num_simulations: int
    mean_pnl: float
    median_pnl: float
    std_pnl: float
    percentile_5: float   # 5th percentile (worst case)
    percentile_25: float
    percentile_75: float
    percentile_95: float  # 95th percentile (best case)
    probability_profitable: float  # % of sims that are profitable
    max_drawdown_mean: float
    max_drawdown_worst: float


@dataclass 
class OverfitReport:
    """Overfitting detection report"""
    is_overfit: bool
    confidence: float  # 0-1, how confident we are it's overfit
    warnings: List[str]
    walk_forward_degradation: float  # Average train→test degradation
    monte_carlo_risk: float  # Probability of loss
    cross_asset_consistency: float  # How consistent across assets
    bias_flags: List[str]  # Specific biases detected
    recommendation: str  # "safe", "caution", "reject"


class MultiAssetBacktester:
    """
    Advanced backtesting system with anti-overfitting protections.
    
    Implements:
    - Walk-forward validation (train/test splits)
    - Monte Carlo simulation (randomize trade order)
    - Cross-asset verification
    - Overfitting detection with bias warnings
    """
    
    def __init__(self, initial_balance: float = 500.0):
        self.initial_balance = initial_balance
        self.engine = BacktestEngine(initial_balance)
    
    def walk_forward_validation(self,
                                strategy_func: Callable,
                                data: pd.DataFrame,
                                n_folds: int = 5,
                                train_ratio: float = 0.7) -> List[WalkForwardResult]:
        """
        Walk-forward validation: split data into sequential train/test folds.
        
        This is the gold standard for testing trading strategies because:
        1. Train period always comes before test period (no future leak)
        2. Multiple folds show consistency
        3. Average degradation reveals overfitting
        
        Args:
            strategy_func: Strategy to test
            data: Full OHLCV dataset
            n_folds: Number of walk-forward folds
            train_ratio: What fraction of each fold is training
            
        Returns:
            List of WalkForwardResult for each fold
        """
        total_bars = len(data)
        fold_size = total_bars // n_folds
        
        if fold_size < 100:
            raise ValueError(f"Not enough data for {n_folds} folds. Need at least {n_folds * 100} bars.")
        
        results = []
        
        for fold in range(n_folds):
            start_idx = fold * fold_size
            end_idx = min(start_idx + fold_size, total_bars)
            
            fold_data = data.iloc[start_idx:end_idx]
            split_point = int(len(fold_data) * train_ratio)
            
            train_data = fold_data.iloc[:split_point]
            test_data = fold_data.iloc[split_point:]
            
            if len(train_data) < 50 or len(test_data) < 50:
                continue
            
            # Backtest on training data
            train_result = self.engine.run_backtest(train_data, strategy_func, f"train_fold_{fold}")
            train_score = train_result['metrics']['total_pnl_pct']
            
            # Backtest on test data
            test_result = self.engine.run_backtest(test_data, strategy_func, f"test_fold_{fold}")
            test_score = test_result['metrics']['total_pnl_pct']
            
            # Calculate degradation
            if abs(train_score) > 0.001:
                degradation = ((train_score - test_score) / abs(train_score)) * 100
            else:
                degradation = 0.0
            
            wf_result = WalkForwardResult(
                fold=fold,
                train_start=train_data.index[0],
                train_end=train_data.index[-1],
                test_start=test_data.index[0],
                test_end=test_data.index[-1],
                train_score=train_score,
                test_score=test_score,
                train_trades=train_result['metrics']['total_trades'],
                test_trades=test_result['metrics']['total_trades'],
                train_win_rate=train_result['metrics']['win_rate'],
                test_win_rate=test_result['metrics']['win_rate'],
                degradation_pct=degradation
            )
            results.append(wf_result)
        
        return results
    
    def monte_carlo_simulation(self,
                               strategy_func: Callable,
                               data: pd.DataFrame,
                               n_simulations: int = 100) -> MonteCarloResult:
        """
        Monte Carlo simulation over realized trade outcomes.

        IMPORTANT: We randomize the *trade sequence*, not OHLCV bars.
        Shuffling bars destroys indicator continuity and creates synthetic
        market structure. This simulation instead bootstraps closed-trade
        returns from the base backtest and replays randomized trade sequences
        to estimate tail risk and sequence dependence.
        """
        pnl_results = []
        drawdown_results = []

        try:
            base_result = self.engine.run_backtest(data, strategy_func, "mc_base")
            trades = base_result.get('trades', [])
            trade_returns = [float(t.get('pnl_pct', 0.0)) / 100.0 for t in trades]
        except Exception:
            trade_returns = []

        if not trade_returns:
            pnl_array = np.zeros(n_simulations)
            dd_array = np.zeros(n_simulations)
        else:
            for sim in range(n_simulations):
                rng = np.random.default_rng(seed=sim)
                sampled = rng.choice(trade_returns, size=len(trade_returns), replace=True)

                equity = 1.0
                peak = 1.0
                max_dd = 0.0

                for trade_r in sampled:
                    equity *= (1.0 + trade_r)
                    if equity > peak:
                        peak = equity
                    drawdown = (peak - equity) / peak if peak > 0 else 0.0
                    if drawdown > max_dd:
                        max_dd = drawdown

                pnl_results.append((equity - 1.0) * 100.0)
                drawdown_results.append(max_dd * 100.0)

            pnl_array = np.array(pnl_results)
            dd_array = np.array(drawdown_results)

        return MonteCarloResult(
            num_simulations=n_simulations,
            mean_pnl=float(np.mean(pnl_array)),
            median_pnl=float(np.median(pnl_array)),
            std_pnl=float(np.std(pnl_array)),
            percentile_5=float(np.percentile(pnl_array, 5)),
            percentile_25=float(np.percentile(pnl_array, 25)),
            percentile_75=float(np.percentile(pnl_array, 75)),
            percentile_95=float(np.percentile(pnl_array, 95)),
            probability_profitable=float(np.mean(pnl_array > 0) * 100),
            max_drawdown_mean=float(np.mean(dd_array)),
            max_drawdown_worst=float(np.max(dd_array))
        )

    def cross_asset_test(self,
                         strategy_func: Callable,
                         asset_datasets: Dict[str, pd.DataFrame]) -> Dict[str, Dict]:
        """
        Test strategy across multiple assets and measure consistency.
        
        Returns per-asset results plus aggregate consistency metrics.
        """
        results = {}
        scores = []
        
        for asset_name, data in asset_datasets.items():
            try:
                result = self.engine.run_backtest(data, strategy_func, asset_name)
                results[asset_name] = result['metrics']
                scores.append(result['metrics']['total_pnl_pct'])
            except Exception as e:
                results[asset_name] = {'error': str(e)}
                scores.append(0.0)
        
        # Calculate consistency
        valid_scores = [s for s in scores if s != 0]
        if valid_scores and np.mean(valid_scores) != 0:
            consistency = 1.0 - (np.std(valid_scores) / abs(np.mean(valid_scores)))
        else:
            consistency = 0.0
        
        results['_aggregate'] = {
            'mean_pnl': float(np.mean(scores)),
            'std_pnl': float(np.std(scores)),
            'consistency': max(0.0, min(1.0, consistency)),
            'profitable_assets': sum(1 for s in scores if s > 0),
            'total_assets': len(scores)
        }
        
        return results
    
    def detect_overfitting(self,
                           strategy_func: Callable,
                           primary_data: pd.DataFrame,
                           validation_datasets: Dict[str, pd.DataFrame],
                           n_monte_carlo: int = 50) -> OverfitReport:
        """
        Comprehensive overfitting detection combining multiple methods.
        
        Checks:
        1. Walk-forward degradation (does performance hold out-of-sample?)
        2. Monte Carlo risk (is the strategy robust to data randomization?)
        3. Cross-asset consistency (does it work on different assets?)
        4. Specific bias detection (survivorship, look-ahead, selection)
        
        Returns:
            OverfitReport with detailed analysis and recommendation
        """
        warnings = []
        bias_flags = []
        
        # 1. Walk-forward validation
        wf_results = self.walk_forward_validation(strategy_func, primary_data, n_folds=3)
        
        avg_degradation = 0.0
        if wf_results:
            avg_degradation = np.mean([r.degradation_pct for r in wf_results])
            
            if avg_degradation > 50:
                warnings.append(f"HIGH degradation ({avg_degradation:.0f}%): performance drops significantly out-of-sample")
                bias_flags.append("overfitting_likely")
            elif avg_degradation > 25:
                warnings.append(f"MODERATE degradation ({avg_degradation:.0f}%): some overfitting signals")
            
            # Check for inconsistent fold performance
            fold_scores = [r.test_score for r in wf_results]
            if len(fold_scores) > 1 and np.std(fold_scores) > abs(np.mean(fold_scores)):
                warnings.append("Inconsistent performance across folds — strategy may be curve-fitted")
                bias_flags.append("curve_fitting")
        
        # 2. Monte Carlo simulation  
        mc_result = self.monte_carlo_simulation(strategy_func, primary_data, n_monte_carlo)
        mc_risk = 1.0 - (mc_result.probability_profitable / 100.0)
        
        if mc_result.probability_profitable < 50:
            warnings.append(f"Monte Carlo: only {mc_result.probability_profitable:.0f}% profitable — strategy relies on specific data ordering")
            bias_flags.append("sequence_dependent")
        
        if mc_result.max_drawdown_worst > 30:
            warnings.append(f"Monte Carlo worst drawdown: {mc_result.max_drawdown_worst:.0f}% — tail risk is high")
        
        # 3. Cross-asset verification
        cross_results = self.cross_asset_test(strategy_func, validation_datasets)
        consistency = cross_results.get('_aggregate', {}).get('consistency', 0.0)
        profitable_ratio = cross_results.get('_aggregate', {}).get('profitable_assets', 0) / max(1, cross_results.get('_aggregate', {}).get('total_assets', 1))
        
        if consistency < 0.3:
            warnings.append(f"Low cross-asset consistency ({consistency:.2f}): strategy may be asset-specific")
            bias_flags.append("asset_specific")
        
        if profitable_ratio < 0.5:
            warnings.append(f"Only profitable on {profitable_ratio*100:.0f}% of assets — limited generalizability")
        
        # 4. Specific bias detection
        # Look-ahead bias check (basic)
        primary_result = self.engine.run_backtest(primary_data, strategy_func, "bias_check")
        if primary_result['metrics']['win_rate'] > 80:
            warnings.append(f"Suspiciously high win rate ({primary_result['metrics']['win_rate']:.0f}%) — possible look-ahead bias")
            bias_flags.append("look_ahead_suspected")
        
        # Low trade count bias
        if primary_result['metrics']['total_trades'] < 10:
            warnings.append(f"Very few trades ({primary_result['metrics']['total_trades']}) — insufficient statistical significance")
            bias_flags.append("insufficient_trades")
        
        # Calculate overall overfitting confidence
        overfit_signals = len(bias_flags)
        overfit_confidence = min(1.0, overfit_signals * 0.25)  # Each flag adds 25% confidence
        
        # Add degradation and MC risk
        overfit_confidence = min(1.0, overfit_confidence + (avg_degradation / 200) + (mc_risk * 0.3))
        
        is_overfit = overfit_confidence > 0.5
        
        # Recommendation
        if overfit_confidence < 0.25:
            recommendation = "safe"
        elif overfit_confidence < 0.5:
            recommendation = "caution"
        else:
            recommendation = "reject"
        
        return OverfitReport(
            is_overfit=is_overfit,
            confidence=overfit_confidence,
            warnings=warnings,
            walk_forward_degradation=avg_degradation,
            monte_carlo_risk=mc_risk,
            cross_asset_consistency=consistency,
            bias_flags=bias_flags,
            recommendation=recommendation
        )
