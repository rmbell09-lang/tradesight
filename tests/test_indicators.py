#!/usr/bin/env python3
"""
Test suite for Technical Indicators Engine
"""

import pytest
import sys
import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src', 'indicators'))

from technical_indicators import TechnicalIndicators

def create_test_data(periods=250):
    """Create test OHLCV data"""
    dates = pd.date_range('2023-01-01', periods=periods, freq='D')
    np.random.seed(42)
    
    close_prices = 100 + np.cumsum(np.random.randn(periods) * 0.02)
    high_prices = close_prices + np.random.rand(periods) * 2
    low_prices = close_prices - np.random.rand(periods) * 2
    open_prices = close_prices + np.random.randn(periods) * 0.5
    volumes = np.random.randint(100000, 1000000, periods).astype(np.float64)
    
    return pd.DataFrame({
        'open': open_prices,
        'high': high_prices,
        'low': low_prices,
        'close': close_prices,
        'volume': volumes
    }, index=dates)

def test_indicators_engine_initialization():
    """Test indicators engine can be initialized"""
    engine = TechnicalIndicators()
    assert engine is not None
    assert engine.indicators == {}
    assert engine.signals == {}

def test_calculate_all_indicators():
    """Test all indicators calculation"""
    engine = TechnicalIndicators()
    test_data = create_test_data()
    
    results = engine.calculate_all(test_data)
    
    # Check structure
    assert 'indicators' in results
    assert 'signals' in results
    assert 'confluence_score' in results
    assert 'timestamp' in results
    assert 'price' in results
    
    # Check all major indicators present
    indicators = results['indicators']
    assert 'rsi' in indicators
    assert 'macd' in indicators
    assert 'bollinger' in indicators
    assert 'moving_averages' in indicators
    assert 'volume' in indicators
    assert 'vwap' in indicators
    assert 'supertrend' in indicators
    assert 'ichimoku' in indicators

def test_rsi_signal_logic():
    """Test RSI signal interpretation"""
    engine = TechnicalIndicators()
    
    # Test overbought (>70)
    assert engine._rsi_signal(75.0) == 1
    
    # Test oversold (<30)
    assert engine._rsi_signal(25.0) == -1
    
    # Test neutral (30-70)
    assert engine._rsi_signal(50.0) == 0

def test_macd_crossover_signal():
    """Test MACD crossover detection"""
    engine = TechnicalIndicators()
    
    # Bullish crossover: MACD crosses above signal
    macd = np.array([0.5, 1.5])  # MACD increasing
    signal = np.array([1.0, 1.0])  # Signal flat
    assert engine._macd_signal(macd, signal) == 1
    
    # Bearish crossover: MACD crosses below signal
    macd = np.array([1.5, 0.5])  # MACD decreasing
    signal = np.array([1.0, 1.0])  # Signal flat
    assert engine._macd_signal(macd, signal) == -1

def test_bollinger_signal_logic():
    """Test Bollinger Bands signal interpretation"""
    engine = TechnicalIndicators()
    
    # Above upper band - overbought
    assert engine._bollinger_signal(105, 100, 90, 80) == 1
    
    # Below lower band - oversold
    assert engine._bollinger_signal(75, 100, 90, 80) == -1
    
    # Above middle - mild bullish
    assert engine._bollinger_signal(95, 100, 90, 80) == 0.5
    
    # Below middle - mild bearish
    assert engine._bollinger_signal(85, 100, 90, 80) == -0.5

def test_volume_signal_logic():
    """Test volume confirmation signal"""
    engine = TechnicalIndicators()
    
    # High volume (>1.5x average)
    assert engine._volume_signal(160000, 100000) == 1
    
    # Low volume (<0.5x average)
    assert engine._volume_signal(40000, 100000) == -1
    
    # Normal volume
    assert engine._volume_signal(100000, 100000) == 0

def test_confluence_score_calculation():
    """Test confluence score aggregation"""
    engine = TechnicalIndicators()
    
    # All bullish signals
    signals = {
        'rsi': -1,      # Oversold (buy signal)
        'macd': 1,      # Bullish crossover
        'ma_trend': 1,  # Above MAs
        'volume': 1,    # High volume
        'vwap': 1,      # Above VWAP
        'supertrend': 1 # Bullish trend
    }
    
    score = engine._calculate_confluence_score(signals)
    assert score > 0  # Should be positive (bullish)
    assert -1.0 <= score <= 1.0  # Within range

def test_insufficient_data_handling():
    """Test error handling with insufficient data"""
    engine = TechnicalIndicators()
    insufficient_data = create_test_data(periods=50)  # Less than required 200
    
    with pytest.raises(ValueError, match="Need at least 200 periods"):
        engine.calculate_all(insufficient_data)

def test_vwap_calculation():
    """Test VWAP calculation"""
    engine = TechnicalIndicators()
    
    # Simple test data
    test_ohlcv = pd.DataFrame({
        'high': [102, 104, 103],
        'low': [98, 96, 97],
        'close': [100, 100, 100],
        'volume': [1000, 2000, 1500]
    })
    
    vwap = engine._calculate_vwap(test_ohlcv)
    assert isinstance(vwap, float)
    assert vwap > 0

if __name__ == "__main__":
    pytest.main([__file__, "-v"])