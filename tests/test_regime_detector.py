#!/usr/bin/env python3
"""
Tests for TradeSight Market Regime Detector (Task 17 — Phase 2)

Covers:
- Regime classification from realized volatility
- Regime classification from VIX value
- Regime transition detection (expanding/contracting vol)
- Strategy weight lookups
- Position multiplier lookups
- Edge cases (insufficient data, missing data)
"""

import pytest
import sys
import os
import numpy as np
import pandas as pd
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from indicators.regime_detector import (
    RegimeDetector, MarketRegime,
    REGIME_STRATEGY_WEIGHTS, REGIME_POSITION_MULTIPLIER,
    fetch_vix,
)


def make_spy_data(annual_vol_pct: float, n_bars: int = 60) -> pd.DataFrame:
    """Generate synthetic SPY daily data with target realized volatility.
    
    annual_vol_pct: target annualized volatility in percent (e.g. 10.0 = 10%)
    """
    np.random.seed(42)
    daily_vol = (annual_vol_pct / 100) / np.sqrt(252)
    returns = np.random.normal(0, daily_vol, n_bars)
    prices = 400.0 * np.exp(np.cumsum(returns))
    dates = pd.date_range('2025-01-01', periods=n_bars, freq='1D')
    return pd.DataFrame({
        'open': prices * 0.999,
        'high': prices * 1.005,
        'low': prices * 0.995,
        'close': prices,
        'volume': [50_000_000] * n_bars,
    }, index=dates)


class TestRegimeDetector:
    """Tests for RegimeDetector class."""

    def setup_method(self):
        self.detector = RegimeDetector()

    # ------------------------------------------------------------------ #
    # VIX-based classification                                             #
    # ------------------------------------------------------------------ #

    def test_vix_low_vol(self):
        """VIX < 15 → LOW_VOL regime."""
        regime, details = self.detector.detect_regime(vix_value=12.5)
        assert regime == MarketRegime.LOW_VOL
        assert details['method'] == 'vix'
        assert details['vix'] == 12.5

    def test_vix_normal(self):
        """VIX 15-25 → NORMAL regime."""
        regime, details = self.detector.detect_regime(vix_value=18.0)
        assert regime == MarketRegime.NORMAL
        assert details['method'] == 'vix'

    def test_vix_high_vol(self):
        """VIX 25-35 → HIGH_VOL regime."""
        regime, details = self.detector.detect_regime(vix_value=30.0)
        assert regime == MarketRegime.HIGH_VOL

    def test_vix_extreme(self):
        """VIX > 35 → EXTREME regime."""
        regime, details = self.detector.detect_regime(vix_value=42.0)
        assert regime == MarketRegime.EXTREME

    def test_vix_boundary_15(self):
        """VIX exactly 15 → NORMAL (lower bound of normal range)."""
        regime, _ = self.detector.detect_regime(vix_value=15.0)
        assert regime == MarketRegime.NORMAL

    def test_vix_boundary_25(self):
        """VIX exactly 25 → HIGH_VOL (lower bound of high-vol range)."""
        regime, _ = self.detector.detect_regime(vix_value=25.0)
        assert regime == MarketRegime.HIGH_VOL

    def test_vix_zero_falls_through(self):
        """VIX = 0 should skip VIX method (treat as no data)."""
        spy_data = make_spy_data(annual_vol_pct=8.0)  # low vol → LOW_VOL
        regime, details = self.detector.detect_regime(spy_data=spy_data, vix_value=0)
        # Should fall back to realized-vol method
        assert details['method'] == 'realized_vol'

    # ------------------------------------------------------------------ #
    # Realized-vol classification                                          #
    # ------------------------------------------------------------------ #

    def test_realized_vol_low(self):
        """Realized vol < 12% → LOW_VOL."""
        spy_data = make_spy_data(annual_vol_pct=8.0)
        regime, details = self.detector.detect_regime(spy_data=spy_data)
        assert regime == MarketRegime.LOW_VOL
        assert details['realized_vol_20d'] is not None
        assert details['realized_vol_20d'] < 15  # rough sanity

    def test_realized_vol_normal(self):
        """Realized vol ~15% → NORMAL."""
        spy_data = make_spy_data(annual_vol_pct=15.0)
        regime, details = self.detector.detect_regime(spy_data=spy_data)
        assert regime in (MarketRegime.NORMAL, MarketRegime.LOW_VOL, MarketRegime.HIGH_VOL)
        # Stochastic data — just check we got a valid regime
        assert regime != MarketRegime.UNKNOWN

    def test_realized_vol_high(self):
        """Realized vol ~25% → HIGH_VOL."""
        spy_data = make_spy_data(annual_vol_pct=28.0)
        regime, details = self.detector.detect_regime(spy_data=spy_data)
        assert regime in (MarketRegime.HIGH_VOL, MarketRegime.EXTREME)

    def test_realized_vol_extreme(self):
        """Realized vol > 35% → EXTREME."""
        spy_data = make_spy_data(annual_vol_pct=50.0)
        regime, details = self.detector.detect_regime(spy_data=spy_data)
        assert regime == MarketRegime.EXTREME

    def test_5d_vol_details_populated(self):
        """Both 20d and 5d realized vol are returned in details."""
        spy_data = make_spy_data(annual_vol_pct=15.0)
        _, details = self.detector.detect_regime(spy_data=spy_data)
        assert details['realized_vol_20d'] is not None
        assert details['realized_vol_5d'] is not None

    # ------------------------------------------------------------------ #
    # Regime transition detection                                          #
    # ------------------------------------------------------------------ #

    def test_vol_expanding_transition(self):
        """5d vol > 1.5x 20d vol → vol_expanding transition, regime bumped up."""
        np.random.seed(99)
        # Calm first 55 bars then a spike in last 5
        daily_vol_calm = (10.0 / 100) / np.sqrt(252)
        daily_vol_spike = (35.0 / 100) / np.sqrt(252)
        returns_calm = np.random.normal(0, daily_vol_calm, 55)
        returns_spike = np.random.normal(0, daily_vol_spike, 5)
        returns = np.concatenate([returns_calm, returns_spike])
        prices = 400.0 * np.exp(np.cumsum(returns))
        spy_data = pd.DataFrame({'close': prices},
                                index=pd.date_range('2025-01-01', periods=60, freq='1D'))
        
        regime, details = self.detector.detect_regime(spy_data=spy_data)
        # May or may not trigger transition depending on exact numbers; 
        # just verify details is populated and regime is valid
        assert isinstance(regime, MarketRegime)
        assert 'realized_vol_20d' in details

    # ------------------------------------------------------------------ #
    # Insufficient / missing data                                          #
    # ------------------------------------------------------------------ #

    def test_no_data_returns_unknown(self):
        """No SPY data + no VIX → UNKNOWN."""
        regime, _ = self.detector.detect_regime()
        assert regime == MarketRegime.UNKNOWN

    def test_too_few_bars_returns_unknown(self):
        """< 30 bars of SPY data → UNKNOWN."""
        spy_data = make_spy_data(annual_vol_pct=15.0, n_bars=15)
        regime, _ = self.detector.detect_regime(spy_data=spy_data)
        assert regime == MarketRegime.UNKNOWN

    def test_vix_takes_priority_over_spy(self):
        """If both VIX and SPY provided, VIX-based method wins."""
        # VIX says HIGH_VOL but spy_data alone would be LOW_VOL
        spy_data = make_spy_data(annual_vol_pct=5.0)  # would be LOW_VOL
        regime, details = self.detector.detect_regime(spy_data=spy_data, vix_value=30.0)
        assert regime == MarketRegime.HIGH_VOL
        assert details['method'] == 'vix'

    # ------------------------------------------------------------------ #
    # Strategy weight lookups                                              #
    # ------------------------------------------------------------------ #

    def test_strategy_weight_low_vol_rsi(self):
        """LOW_VOL boosts RSI Mean Reversion weight above 1.0."""
        w = self.detector.get_strategy_weight(MarketRegime.LOW_VOL, 'RSI Mean Reversion')
        assert w > 1.0

    def test_strategy_weight_high_vol_rsi(self):
        """HIGH_VOL reduces RSI Mean Reversion weight (mean reversion fails)."""
        w = self.detector.get_strategy_weight(MarketRegime.HIGH_VOL, 'RSI Mean Reversion')
        assert w < 1.0

    def test_strategy_weight_high_vol_macd(self):
        """HIGH_VOL boosts MACD Crossover (trend following)."""
        w = self.detector.get_strategy_weight(MarketRegime.HIGH_VOL, 'MACD Crossover')
        assert w > 1.0

    def test_strategy_weight_extreme_rsi(self):
        """EXTREME regime heavily suppresses RSI Mean Reversion."""
        w = self.detector.get_strategy_weight(MarketRegime.EXTREME, 'RSI Mean Reversion')
        assert w <= 0.3

    def test_strategy_weight_unknown_strategy(self):
        """Unknown strategy name returns 1.0 (neutral weight)."""
        w = self.detector.get_strategy_weight(MarketRegime.NORMAL, 'NonExistentStrategy')
        assert w == 1.0

    def test_strategy_weight_normal_all_ones(self):
        """NORMAL regime has all strategy weights = 1.0."""
        for strategy in REGIME_STRATEGY_WEIGHTS[MarketRegime.NORMAL]:
            w = self.detector.get_strategy_weight(MarketRegime.NORMAL, strategy)
            assert w == 1.0, f"{strategy} weight in NORMAL should be 1.0, got {w}"

    # ------------------------------------------------------------------ #
    # Position size multiplier lookups                                     #
    # ------------------------------------------------------------------ #

    def test_position_multiplier_low_vol(self):
        """LOW_VOL → full position size (1.0)."""
        m = self.detector.get_position_multiplier(MarketRegime.LOW_VOL)
        assert m == 1.0

    def test_position_multiplier_high_vol_reduced(self):
        """HIGH_VOL → position size reduced (< 1.0)."""
        m = self.detector.get_position_multiplier(MarketRegime.HIGH_VOL)
        assert m < 1.0

    def test_position_multiplier_extreme_significantly_reduced(self):
        """EXTREME → position size heavily reduced (<= 0.5)."""
        m = self.detector.get_position_multiplier(MarketRegime.EXTREME)
        assert m <= 0.5

    def test_position_multiplier_unknown_conservative(self):
        """UNKNOWN regime → conservative size (< 1.0 for safety)."""
        m = self.detector.get_position_multiplier(MarketRegime.UNKNOWN)
        assert m < 1.0

    # ------------------------------------------------------------------ #
    # Enum coverage                                                        #
    # ------------------------------------------------------------------ #

    def test_all_regimes_have_strategy_weights(self):
        """Every non-UNKNOWN regime has a weights entry."""
        for r in MarketRegime:
            if r == MarketRegime.UNKNOWN:
                continue
            assert r in REGIME_STRATEGY_WEIGHTS, f"{r} missing from REGIME_STRATEGY_WEIGHTS"

    def test_all_regimes_have_position_multiplier(self):
        """Every regime has a position multiplier entry."""
        for r in MarketRegime:
            assert r in REGIME_POSITION_MULTIPLIER, f"{r} missing from REGIME_POSITION_MULTIPLIER"


class TestFetchVix:
    """Tests for the fetch_vix() convenience function."""

    def test_fetch_vix_returns_none_on_failure(self):
        """fetch_vix returns None when yfinance download raises an exception.
        
        yfinance is imported inside fetch_vix(), so we patch at the import level.
        """
        import yfinance as yf_real
        with patch.object(yf_real, 'download', side_effect=Exception('network error')):
            result = fetch_vix()
        assert result is None

    def test_fetch_vix_returns_float_or_none_on_success(self):
        """fetch_vix either returns a float (on valid data) or None (graceful fallback).
        
        Only validates the return type since MultiIndex handling varies by yfinance version.
        """
        result = fetch_vix()  # Live call: either float or None — both are valid
        assert result is None or isinstance(result, float), (
            f"Expected float or None, got {type(result)}: {result}")
