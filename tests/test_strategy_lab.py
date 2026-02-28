"""Tests for AI Strategy Iteration Engine and Backtesting Engine"""

import sys
import pytest
import pandas as pd
import numpy as np

sys.path.append('src')

from strategy_lab.backtest import BacktestEngine, Trade, BacktestMetrics, simple_ma_crossover, rsi_mean_reversion
from strategy_lab.ai_engine import AIStrategyEngine, StrategyGeneration, MultiAssetResults, create_test_data


# --- Backtest Engine Tests ---

class TestBacktestEngine:
    
    def setup_method(self):
        self.engine = BacktestEngine(initial_balance=10000.0)
        self.test_data = self._create_simple_data(200)
    
    def _create_simple_data(self, periods):
        """Create simple test OHLCV data"""
        np.random.seed(42)
        dates = pd.date_range('2024-01-01', periods=periods)
        prices = 100 + np.cumsum(np.random.randn(periods) * 0.5)
        return pd.DataFrame({
            'open': prices * (1 + np.random.uniform(-0.005, 0.005, periods)),
            'high': prices * (1 + np.random.uniform(0.001, 0.02, periods)),
            'low': prices * (1 - np.random.uniform(0.001, 0.02, periods)),
            'close': prices,
            'volume': np.random.randint(1000, 10000, periods)
        }, index=dates)
    
    def test_engine_initialization(self):
        assert self.engine.initial_balance == 10000.0
        assert self.engine.fee_rate == 0.001
        assert self.engine.balance == 10000.0
    
    def test_engine_reset(self):
        self.engine.balance = 5000.0
        self.engine.reset()
        assert self.engine.balance == 10000.0
        assert self.engine.positions == []
        assert self.engine.closed_trades == []
    
    def test_backtest_requires_minimum_data(self):
        small_data = self._create_simple_data(10)
        with pytest.raises(ValueError, match="at least 50"):
            self.engine.run_backtest(small_data, simple_ma_crossover)
    
    def test_backtest_requires_ohlcv_columns(self):
        bad_data = pd.DataFrame({'price': [100]*100}, index=pd.date_range('2024-01-01', periods=100))
        with pytest.raises(ValueError, match="Missing required columns"):
            self.engine.run_backtest(bad_data, simple_ma_crossover)
    
    def test_backtest_returns_correct_structure(self):
        result = self.engine.run_backtest(self.test_data, simple_ma_crossover, 'TEST')
        assert 'metrics' in result
        assert 'trades' in result
        assert 'equity_curve' in result
        assert 'final_balance' in result
        assert 'asset_name' in result
        assert result['asset_name'] == 'TEST'
    
    def test_backtest_metrics_structure(self):
        result = self.engine.run_backtest(self.test_data, simple_ma_crossover)
        metrics = result['metrics']
        required_keys = ['total_trades', 'winning_trades', 'losing_trades', 'win_rate',
                        'total_pnl', 'total_pnl_pct', 'max_drawdown', 'sharpe_ratio',
                        'profit_factor', 'avg_win', 'avg_loss']
        for key in required_keys:
            assert key in metrics, f"Missing metric: {key}"
    
    def test_indicators_added(self):
        result = self.engine._add_indicators(self.test_data)
        expected_indicators = ['sma_20', 'sma_50', 'sma_100', 'rsi', 'macd', 'macd_signal',
                              'macd_histogram', 'bb_middle', 'bb_upper', 'bb_lower']
        for ind in expected_indicators:
            assert ind in result.columns, f"Missing indicator: {ind}"
    
    def test_rsi_strategy_runs(self):
        result = self.engine.run_backtest(self.test_data, rsi_mean_reversion, 'TEST')
        assert isinstance(result, dict)
        assert result['metrics']['total_trades'] >= 0


# --- AI Strategy Engine Tests ---

class TestAIStrategyEngine:
    
    def setup_method(self):
        self.ai_engine = AIStrategyEngine(initial_balance=10000.0, max_generations=2)
        self.test_data = create_test_data('BTC', 150)
    
    def test_engine_initialization(self):
        assert self.ai_engine.initial_balance == 10000.0
        assert self.ai_engine.max_generations == 2
        assert self.ai_engine.backtest_engine is not None
    
    def test_create_test_data(self):
        data = create_test_data('BTC', 100)
        assert len(data) == 100
        assert all(col in data.columns for col in ['open', 'high', 'low', 'close', 'volume'])
    
    def test_evaluate_strategy(self):
        result = self.ai_engine._evaluate_strategy(simple_ma_crossover, self.test_data, 'BTC')
        assert isinstance(result, dict)
        assert 'metrics' in result
        assert result['metrics']['total_trades'] >= 0
    
    def test_calculate_performance_score(self):
        result = self.ai_engine._evaluate_strategy(simple_ma_crossover, self.test_data, 'BTC')
        score = self.ai_engine._calculate_performance_score(result)
        assert isinstance(score, float)
        assert score >= 0.0
    
    def test_extract_parameters(self):
        params = self.ai_engine._extract_parameters(simple_ma_crossover)
        assert isinstance(params, dict)
        assert 'ma_periods' in params
        assert 'position_sizes' in params
    
    def test_evolution_summary_empty(self):
        summary = self.ai_engine.get_evolution_summary()
        assert summary == {}
    
    def test_multi_asset_datasets(self):
        """Test that we can create datasets for multiple assets"""
        datasets = {
            'BTC': create_test_data('BTC', 100),
            'ETH': create_test_data('ETH', 100),
            'SPY': create_test_data('SPY', 100),
        }
        assert len(datasets) == 3
        for name, data in datasets.items():
            assert len(data) == 100
    
    def test_performance_score_zero_trades(self):
        """Performance score should be 0 for no trades"""
        mock_result = {'metrics': {'total_trades': 0, 'total_pnl_pct': 0, 'win_rate': 0,
                                    'max_drawdown': 0, 'profit_factor': 0}}
        score = self.ai_engine._calculate_performance_score(mock_result)
        assert score == 0.0


# --- Tournament System Tests ---

from strategy_lab.tournament import (
    StrategyTournament, TournamentEntry, TournamentResults,
    get_builtin_strategies, macd_crossover, bollinger_bounce,
    dual_ma_rsi, momentum_breakout, conservative_trend
)


class TestStrategyTournament:
    
    def setup_method(self):
        self.tournament = StrategyTournament(initial_balance=10000, elimination_rate=0.3, min_survivors=3)
        self.test_data = create_test_data('BTC', 150)
    
    def test_tournament_initialization(self):
        assert self.tournament.elimination_rate == 0.3
        assert self.tournament.min_survivors == 3
        assert len(self.tournament.entries) == 0
    
    def test_register_strategy(self):
        result = self.tournament.register_strategy('Test', simple_ma_crossover)
        assert result is True
        assert len(self.tournament.entries) == 1
        assert self.tournament.entries[0].name == 'Test'
    
    def test_register_duplicate_raises(self):
        self.tournament.register_strategy('Test', simple_ma_crossover)
        with pytest.raises(ValueError, match="already registered"):
            self.tournament.register_strategy('Test', simple_ma_crossover)
    
    def test_tournament_needs_minimum_strategies(self):
        self.tournament.register_strategy('Solo', simple_ma_crossover)
        with pytest.raises(ValueError, match="at least 2"):
            self.tournament.run_tournament([('BTC', self.test_data)])
    
    def test_get_builtin_strategies(self):
        strategies = get_builtin_strategies()
        assert len(strategies) >= 5
        assert 'MA Crossover' in strategies
        assert 'RSI Mean Reversion' in strategies
        assert 'MACD Crossover' in strategies
    
    def test_full_tournament_runs(self):
        strategies = get_builtin_strategies()
        for name, func in strategies.items():
            self.tournament.register_strategy(name, func)
        
        round_datasets = [
            ('BTC', create_test_data('BTC', 150)),
            ('ETH', create_test_data('ETH', 150)),
        ]
        
        results = self.tournament.run_tournament(round_datasets)
        
        assert isinstance(results, TournamentResults)
        assert results.total_rounds >= 1
        assert results.winner != "None"
        assert len(results.top_3) <= 3
        assert results.final_survivors >= self.tournament.min_survivors
    
    def test_tournament_eliminates_strategies(self):
        strategies = get_builtin_strategies()
        for name, func in strategies.items():
            self.tournament.register_strategy(name, func)
        
        round_datasets = [
            ('BTC', create_test_data('BTC', 150)),
            ('ETH', create_test_data('ETH', 150)),
            ('SPY', create_test_data('SPY', 150)),
        ]
        
        results = self.tournament.run_tournament(round_datasets)
        assert len(results.elimination_log) > 0
    
    def test_get_surviving_strategies(self):
        strategies = get_builtin_strategies()
        for name, func in strategies.items():
            self.tournament.register_strategy(name, func)
        
        self.tournament.run_tournament([('BTC', self.test_data)])
        survivors = self.tournament.get_surviving_strategies()
        assert len(survivors) >= self.tournament.min_survivors
    
    def test_get_entry_by_name(self):
        self.tournament.register_strategy('Test Strategy', simple_ma_crossover)
        entry = self.tournament.get_entry_by_name('Test Strategy')
        assert entry is not None
        assert entry.name == 'Test Strategy'
    
    def test_get_entry_by_name_not_found(self):
        entry = self.tournament.get_entry_by_name('Nonexistent')
        assert entry is None


# --- Multi-Asset Backtester Tests ---

from strategy_lab.backtester import MultiAssetBacktester, WalkForwardResult, MonteCarloResult, OverfitReport


class TestMultiAssetBacktester:
    
    def setup_method(self):
        self.backtester = MultiAssetBacktester(initial_balance=10000)
        self.test_data = create_test_data('BTC', 500)  # Need more data for walk-forward
    
    def test_initialization(self):
        assert self.backtester.initial_balance == 10000
        assert self.backtester.engine is not None
    
    def test_walk_forward_validation(self):
        results = self.backtester.walk_forward_validation(
            simple_ma_crossover, self.test_data, n_folds=3
        )
        assert len(results) > 0
        for r in results:
            assert isinstance(r, WalkForwardResult)
            assert r.train_start < r.test_start
    
    def test_walk_forward_needs_enough_data(self):
        small_data = create_test_data('BTC', 50)
        with pytest.raises(ValueError, match="Not enough data"):
            self.backtester.walk_forward_validation(simple_ma_crossover, small_data, n_folds=5)
    
    def test_monte_carlo_simulation(self):
        result = self.backtester.monte_carlo_simulation(
            simple_ma_crossover, self.test_data, n_simulations=10
        )
        assert isinstance(result, MonteCarloResult)
        assert result.num_simulations == 10
        assert 0 <= result.probability_profitable <= 100
        assert result.percentile_5 <= result.percentile_95
    
    def test_cross_asset_test(self):
        datasets = {
            'BTC': create_test_data('BTC', 200),
            'ETH': create_test_data('ETH', 200),
        }
        results = self.backtester.cross_asset_test(simple_ma_crossover, datasets)
        assert 'BTC' in results
        assert 'ETH' in results
        assert '_aggregate' in results
        assert 'consistency' in results['_aggregate']
    
    def test_detect_overfitting(self):
        validation_datasets = {
            'ETH': create_test_data('ETH', 200),
            'SPY': create_test_data('SPY', 200),
        }
        report = self.backtester.detect_overfitting(
            simple_ma_crossover, self.test_data, validation_datasets, n_monte_carlo=10
        )
        assert isinstance(report, OverfitReport)
        assert report.recommendation in ('safe', 'caution', 'reject')
        assert 0 <= report.confidence <= 1.0
    
    def test_overfit_report_has_bias_analysis(self):
        validation_datasets = {
            'ETH': create_test_data('ETH', 200),
        }
        report = self.backtester.detect_overfitting(
            simple_ma_crossover, self.test_data, validation_datasets, n_monte_carlo=5
        )
        assert isinstance(report.warnings, list)
        assert isinstance(report.bias_flags, list)
