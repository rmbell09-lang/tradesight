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
        """Zero trades returns -1.0 (strategy useless on this data — intentional penalty)"""
        mock_result = {'metrics': {'total_trades': 0, 'total_pnl_pct': 0, 'win_rate': 0,
                                    'max_drawdown': 0, 'profit_factor': 0}}
        score = self.ai_engine._calculate_performance_score(mock_result)
        assert score == -1.0  # -1.0 signals zero trades; tournament eliminates these


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


# --- Stock Opportunity Scorer Tests ---

from scanners.stock_opportunities import StockOpportunityScorer, OpportunityScore


class TestStockOpportunityScorer:
    
    def setup_method(self):
        self.scorer = StockOpportunityScorer()
        self.test_data = create_test_data('BTC', 250)
    
    def test_initialization(self):
        assert self.scorer.weights is not None
        assert abs(sum(self.scorer.weights.values()) - 1.0) < 0.01
    
    def test_score_opportunity(self):
        score = self.scorer.score_opportunity(self.test_data, 'BTC')
        assert isinstance(score, OpportunityScore)
        assert score.symbol == 'BTC'
        assert 0 <= score.overall_score <= 100
        assert score.confidence in ('high', 'medium', 'low')
        assert score.direction in ('bullish', 'bearish', 'neutral')
    
    def test_score_components_in_range(self):
        score = self.scorer.score_opportunity(self.test_data, 'BTC')
        assert 0 <= score.volume_score <= 100
        assert 0 <= score.volatility_score <= 100
        assert 0 <= score.technical_score <= 100
        assert 0 <= score.momentum_score <= 100
        assert 0 <= score.trend_score <= 100
    
    def test_needs_minimum_data(self):
        small_data = create_test_data('BTC', 50)
        with pytest.raises(ValueError, match="at least 200"):
            self.scorer.score_opportunity(small_data, 'BTC')
    
    def test_rank_opportunities(self):
        datasets = {
            'BTC': create_test_data('BTC', 250),
            'ETH': create_test_data('ETH', 250),
            'SPY': create_test_data('SPY', 250),
        }
        ranked = self.scorer.rank_opportunities(datasets, min_score=0)
        assert len(ranked) > 0
        # Verify sorted descending
        for i in range(len(ranked) - 1):
            assert ranked[i].overall_score >= ranked[i+1].overall_score
    
    def test_custom_weights(self):
        custom_weights = {
            'volume': 0.50,
            'volatility': 0.10,
            'technical': 0.20,
            'momentum': 0.10,
            'trend': 0.10,
        }
        scorer = StockOpportunityScorer(weights=custom_weights)
        score = scorer.score_opportunity(self.test_data, 'BTC')
        assert isinstance(score, OpportunityScore)


# --- Alpaca Integration Tests ---

import sys
sys.path.append('src')
from data.alpaca_client import AlpacaClient, StockQuote, PaperPosition
from scanners.stock_scanner import StockScanner, ScanResult


class TestAlpacaClient:
    
    def setup_method(self):
        self.client = AlpacaClient()  # Demo mode
    
    def test_initialization(self):
        assert self.client.demo_mode is True
        assert self.client.paper is True
        assert len(self.client.SP500_SYMBOLS) >= 50
    
    def test_get_historical_data(self):
        data = self.client.get_historical_data('AAPL', days=50)
        assert len(data) == 50
        assert all(col in data.columns for col in ['open', 'high', 'low', 'close', 'volume'])
        assert data['high'].min() >= data['low'].min()
        assert data['close'].min() > 0
    
    def test_get_quote(self):
        quote = self.client.get_quote('AAPL')
        assert isinstance(quote, StockQuote)
        assert quote.symbol == 'AAPL'
        assert quote.last > 0
        assert quote.bid <= quote.ask
    
    def test_place_paper_trade(self):
        order = self.client.place_paper_trade('AAPL', 10, 'buy')
        assert 'order_id' in order
        assert order['symbol'] == 'AAPL'
        assert order['quantity'] == 10
        assert order['side'] == 'buy'
        assert order['demo_mode'] is True
    
    def test_get_paper_positions(self):
        positions = self.client.get_paper_positions()
        assert isinstance(positions, list)
        if positions:
            pos = positions[0]
            assert isinstance(pos, PaperPosition)
            assert pos.symbol is not None
    
    def test_demo_data_deterministic(self):
        # Same symbol should produce same data
        data1 = self.client._generate_demo_data('AAPL', 10)
        data2 = self.client._generate_demo_data('AAPL', 10)
        assert (data1["close"].values == data2["close"].values).all()


class TestStockScanner:
    
    def setup_method(self):
        self.scanner = StockScanner()
    
    def test_initialization(self):
        assert self.scanner.alpaca is not None
        assert self.scanner.scorer is not None
        assert self.scanner.last_scan_result is None
    
    def test_quick_scan(self):
        result = self.scanner.quick_scan(limit=3)
        assert isinstance(result, ScanResult)
        assert result.total_scanned == 3
        assert result.scan_duration_seconds >= 0
        assert result.scan_parameters['scan_type'] == 'quick'
    
    def test_custom_scan(self):
        symbols = ['AAPL', 'MSFT']
        result = self.scanner.custom_scan(symbols, min_score=0)
        assert result.total_scanned == 2
        assert result.scan_parameters['scan_type'] == 'custom'
    
    def test_get_quote(self):
        quote = self.scanner.get_quote('AAPL')
        assert isinstance(quote, StockQuote)
        assert quote.symbol == 'AAPL'
    
    def test_get_historical_data(self):
        data = self.scanner.get_historical_data('AAPL', days=30)
        assert len(data) == 30
        assert 'close' in data.columns
