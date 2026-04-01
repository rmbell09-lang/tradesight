"""
Test for the Unified Dashboard functionality
"""

import sys
import os
import pytest
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'web'))

def test_unified_dashboard_imports():
    """Test that all required modules can be imported"""
    from dashboard import (
        get_polymarket_stats, 
        get_stock_stats, 
        get_strategy_lab_stats,
        app
    )
    
    # Check that Flask app exists
    assert app is not None
    assert app.name == 'dashboard'

def test_polymarket_stats_basic():
    """Test Polymarket stats function returns proper structure"""
    from dashboard import get_polymarket_stats
    
    stats = get_polymarket_stats()
    
    # Should have required keys
    required_keys = ['total_markets', 'last_scan', 'active_markets', 'high_volume_markets']
    for key in required_keys:
        assert key in stats
        
    # Should be numbers or None for dates
    assert isinstance(stats['total_markets'], int)
    assert isinstance(stats['active_markets'], int)
    assert isinstance(stats['high_volume_markets'], int)

def test_stock_stats_basic():
    """Test Stock stats function returns proper structure"""  
    from dashboard import get_stock_stats
    
    stats = get_stock_stats()
    
    # Should have required keys
    required_keys = ['total_scanned', 'opportunities_found', 'scan_duration', 'last_scan', 'top_opportunity', 'top_score', 'market_sentiment_enabled']
    for key in required_keys:
        assert key in stats
        
    # Should be proper types
    assert isinstance(stats['total_scanned'], int)
    assert isinstance(stats['opportunities_found'], int)
    
def test_flask_routes():
    """Test that Flask routes are properly configured"""
    from dashboard import app
    
    client = app.test_client()
    
    # Test main dashboard route
    response = client.get('/')
    assert response.status_code == 200
    
    # Test API endpoints exist (though they may error without full data)
    api_endpoints = [
        '/api/polymarket/stats',
        '/api/stocks/stats', 
        '/api/polymarket/opportunities',
        '/api/stocks/opportunities'
    ]
    
    for endpoint in api_endpoints:
        response = client.get(endpoint)
        assert response.status_code == 200  # Should return JSON even if error


def test_stock_opportunities_exposes_retail_sentiment_fields():
    """The dashboard API should expose scanner sentiment enrichment fields."""
    from dashboard import app

    client = app.test_client()
    response = client.get('/api/stocks/opportunities')
    assert response.status_code == 200
    payload = response.get_json()
    if isinstance(payload, list) and payload:
        required = {
            'retail_sentiment_score',
            'retail_sentiment_label',
            'retail_sentiment_alignment',
            'retail_sentiment_buzz',
            'retail_sentiment_coverage',
        }
        assert required <= set(payload[0].keys())

if __name__ == '__main__':
    test_unified_dashboard_imports()
    test_polymarket_stats_basic()  
    test_stock_stats_basic()
    test_flask_routes()
    print("✅ All unified dashboard tests passed!")
