"""Tests for slippage + spread modeling"""
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.strategy_lab.slippage import SlippageModel, AssetClass, ASSET_DEFAULTS


class TestSlippageModel:
    """Core slippage model tests"""

    def test_default_stock_params(self):
        m = SlippageModel(asset_class='stock')
        assert m.base_spread_bps == ASSET_DEFAULTS[AssetClass.STOCK]['base_spread_bps']

    def test_default_crypto_wider_spread(self):
        stock = SlippageModel(asset_class='stock')
        crypto = SlippageModel(asset_class='crypto')
        assert crypto.base_spread_bps > stock.base_spread_bps

    def test_penny_stock_widest(self):
        penny = SlippageModel(asset_class='penny')
        stock = SlippageModel(asset_class='stock')
        assert penny.base_spread_bps > stock.base_spread_bps * 5

    def test_unknown_asset_defaults_to_stock(self):
        m = SlippageModel(asset_class='unobtainium')
        assert m.base_spread_bps == ASSET_DEFAULTS[AssetClass.STOCK]['base_spread_bps']

    def test_buy_fills_above_market(self):
        m = SlippageModel(asset_class='stock', seed=42)
        fill = m.apply(100.0, 'buy')
        assert fill > 100.0

    def test_sell_fills_below_market(self):
        m = SlippageModel(asset_class='stock', seed=42)
        fill = m.apply(100.0, 'sell')
        assert fill < 100.0

    def test_long_alias_works(self):
        m = SlippageModel(asset_class='stock', seed=42)
        fill = m.apply(100.0, 'long')
        assert fill > 100.0

    def test_short_alias_works(self):
        m = SlippageModel(asset_class='stock', seed=42)
        fill = m.apply(100.0, 'short')
        assert fill < 100.0

    def test_volume_impact_increases_slippage(self):
        m = SlippageModel(asset_class='stock', jitter_bps=0, seed=0)
        low_vol = m.total_slippage_bps(order_shares=100, avg_daily_volume=1_000_000, price=100)
        high_vol = m.total_slippage_bps(order_shares=50_000, avg_daily_volume=1_000_000, price=100)
        assert high_vol > low_vol

    def test_volatility_increases_slippage(self):
        m = SlippageModel(asset_class='stock', jitter_bps=0, seed=0)
        low_atr = m.total_slippage_bps(atr=0.5, price=100)
        high_atr = m.total_slippage_bps(atr=5.0, price=100)
        assert high_atr > low_atr

    def test_round_trip_is_double_one_way(self):
        m = SlippageModel(asset_class='stock', jitter_bps=0, seed=0)
        one_way = m.total_slippage_bps(price=100)
        rt = m.round_trip_cost_bps(price=100)
        assert abs(rt - one_way * 2) < 0.01

    def test_slippage_never_negative(self):
        m = SlippageModel(asset_class='stock', seed=0)
        for _ in range(100):
            slip = m.total_slippage_bps(price=50)
            assert slip >= 0

    def test_custom_params_override_defaults(self):
        m = SlippageModel(asset_class='stock', base_spread_bps=99.0)
        assert m.base_spread_bps == 99.0

    def test_summary_returns_dict(self):
        m = SlippageModel(asset_class='crypto')
        s = m.summary()
        assert isinstance(s, dict)
        assert s['asset_class'] == 'crypto'
        assert 'base_spread_bps' in s

    def test_deterministic_with_seed(self):
        m1 = SlippageModel(seed=123)
        m2 = SlippageModel(seed=123)
        f1 = m1.apply(100, 'buy', order_shares=500, avg_daily_volume=500_000, atr=2.0)
        f2 = m2.apply(100, 'buy', order_shares=500, avg_daily_volume=500_000, atr=2.0)
        assert f1 == f2


class TestBacktestIntegration:
    """Test SlippageModel integrated with BacktestEngine"""

    def test_engine_accepts_slippage_model(self):
        from src.strategy_lab.backtest import BacktestEngine
        model = SlippageModel(asset_class='stock', seed=42)
        engine = BacktestEngine(slippage_model=model)
        assert engine.slippage_model is model

    def test_engine_without_model_uses_flat_pct(self):
        from src.strategy_lab.backtest import BacktestEngine
        engine = BacktestEngine(slippage_pct=0.001)
        assert engine.slippage_model is None
        assert engine.slippage_pct == 0.001

    def test_backtest_with_model_runs(self):
        """Full backtest with slippage model produces valid results"""
        import pandas as pd
        import numpy as np
        from src.strategy_lab.backtest import BacktestEngine, simple_ma_crossover

        np.random.seed(42)
        dates = pd.date_range('2023-01-01', periods=300, freq='D')
        prices = 100 + np.cumsum(np.random.randn(300) * 0.5)
        prices = np.maximum(prices, 10)  # floor
        data = pd.DataFrame({
            'open': prices * 0.999,
            'high': prices * 1.01,
            'low': prices * 0.99,
            'close': prices,
            'volume': np.random.randint(100_000, 2_000_000, 300)
        }, index=dates)

        model = SlippageModel(asset_class='stock', seed=42)
        engine = BacktestEngine(slippage_model=model)
        result = engine.run_backtest(data, simple_ma_crossover, 'TEST')

        assert result['metrics']['total_trades'] >= 0
        assert 'slippage_model' in result
        assert result['slippage_model']['asset_class'] == 'stock'

    def test_model_slippage_more_realistic_than_flat(self):
        """With high ATR, model slippage should exceed the default 0.05% flat"""
        model = SlippageModel(asset_class='stock', jitter_bps=0, seed=0)
        # High ATR scenario: ATR = 5% of price
        slip_bps = model.total_slippage_bps(atr=5.0, price=100.0)
        flat_bps = 5.0  # 0.05% = 5 bps
        assert slip_bps > flat_bps, f"Model slippage {slip_bps} should exceed flat {flat_bps} in volatile market"
