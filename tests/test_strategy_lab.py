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
