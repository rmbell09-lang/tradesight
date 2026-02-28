#!/usr/bin/env python3
"""
Test suite for TradeSight Scanner
"""

import pytest
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

from scanner import PolymarketScanner

def test_scanner_initialization():
    """Test scanner can be initialized"""
    scanner = PolymarketScanner("/tmp/test_tradesight.db")
    assert scanner is not None
    assert scanner.api_base == "https://gamma-api.polymarket.com"

def test_parse_market_data():
    """Test market data parsing"""
    scanner = PolymarketScanner("/tmp/test_tradesight.db")
    
    mock_market = {
        'id': '123',
        'question': 'Test market?',
        'outcomePrices': '["0.6", "0.4"]',
        'volume': '1000.5',
        'active': True,
        'closed': False
    }
    
    result = scanner.parse_market_data(mock_market)
    
    assert result is not None
    assert result['id'] == '123'
    assert result['price_yes'] == 0.6
    assert result['price_no'] == 0.4
    assert result['volume'] == 1000.5

def test_arbitrage_detection():
    """Test arbitrage opportunity detection"""
    scanner = PolymarketScanner("/tmp/test_tradesight.db")
    
    # Test arbitrage case: Yes + No < $1.00
    arb = scanner.detect_arbitrage(0.40, 0.50)  # 0.90 total
    assert arb is not None
    assert arb['type'] == 'arbitrage'
    assert arb['confidence'] == 1.0
    assert abs(arb['expected_profit'] - 0.10) < 0.001  # Float comparison
    
    # Test no arbitrage case
    no_arb = scanner.detect_arbitrage(0.60, 0.45)  # 1.05 total
    assert no_arb is None

if __name__ == "__main__":
    pytest.main([__file__, "-v"])