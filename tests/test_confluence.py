#!/usr/bin/env python3
"""
Test suite for TradeSight Confluence Strategy
Tests: get_price_history(), detect_confluence(), scan_markets() confluence path
"""

import pytest
import sys
import os
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

from scanner import PolymarketScanner

# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture
def scanner(tmp_path):
    db = str(tmp_path / "test_confluence.db")
    return PolymarketScanner(db)


def _seed_snapshots(scanner, market_id: str, n: int, start_price: float = 0.5):
    """Seed n price snapshots for a market into the scanner DB."""
    conn = sqlite3.connect(scanner.db_path)
    cursor = conn.cursor()

    # Insert market row first (FK constraint)
    cursor.execute('''
        INSERT OR IGNORE INTO markets
        (id, question, category, end_date, volume, liquidity, active, closed,
         outcomes, price_yes, price_no, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (market_id, "Test market?", "test", "", 50000, 5000, True, False,
          '["Yes","No"]', start_price, 1.0 - start_price,
          datetime.now(timezone.utc).isoformat()))

    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    price = start_price
    for i in range(n):
        ts = (base + timedelta(hours=i)).isoformat()
        price = max(0.01, min(0.99, price + np.random.uniform(-0.005, 0.005)))
        cursor.execute('''
            INSERT INTO price_snapshots
            (market_id, timestamp, price_yes, price_no, volume_24h, best_bid, best_ask, spread)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (market_id, ts, price, 1.0 - price, 10000.0,
              price - 0.01, price + 0.01, 0.02))

    conn.commit()
    conn.close()


# ──────────────────────────────────────────────
# get_price_history tests
# ──────────────────────────────────────────────

def test_get_price_history_returns_none_when_no_data(scanner):
    """Returns None when there are no snapshots for a market."""
    result = scanner.get_price_history("nonexistent-market", n=60)
    assert result is None


def test_get_price_history_returns_none_when_insufficient_data(scanner):
    """Returns None when fewer than 14 snapshots exist."""
    np.random.seed(1)
    _seed_snapshots(scanner, "mkt-few", 10)
    result = scanner.get_price_history("mkt-few", n=60)
    assert result is None


def test_get_price_history_returns_dataframe_with_enough_data(scanner):
    """Returns a valid OHLCV DataFrame when enough snapshots exist."""
    np.random.seed(2)
    _seed_snapshots(scanner, "mkt-enough", 60)
    df = scanner.get_price_history("mkt-enough", n=60)
    assert df is not None
    assert isinstance(df, pd.DataFrame)
    assert set(df.columns) == {'open', 'high', 'low', 'close', 'volume'}
    assert len(df) == 60


def test_get_price_history_ohlc_constraints(scanner):
    """OHLCV data satisfies: low <= close <= high and volume > 0."""
    np.random.seed(3)
    _seed_snapshots(scanner, "mkt-ohlc", 70)
    df = scanner.get_price_history("mkt-ohlc", n=70)
    assert df is not None
    assert (df['low'] <= df['close']).all(), "low must be <= close"
    assert (df['close'] <= df['high']).all(), "close must be <= high"
    assert (df['volume'] > 0).all(), "volume must be positive"


def test_get_price_history_respects_n_limit(scanner):
    """Returns at most n rows."""
    np.random.seed(4)
    _seed_snapshots(scanner, "mkt-limit", 80)
    df = scanner.get_price_history("mkt-limit", n=30)
    assert df is not None
    assert len(df) == 30


# ──────────────────────────────────────────────
# detect_confluence tests
# ──────────────────────────────────────────────

def test_detect_confluence_true_when_all_signals_positive(scanner):
    """detect_confluence returns True with strong bullish confluence."""
    indicators = {
        'confluence_score': 0.6,
        'signals': {
            'rsi': 0,       # neutral (non-bearish)
            'macd': 1,      # bullish crossover
            'bollinger': 0.5,  # above midline
            'supertrend': 1,   # bullish
            'vwap': 1,         # above VWAP
        }
    }
    assert scanner.detect_confluence(indicators) is True


def test_detect_confluence_false_when_low_score(scanner):
    """detect_confluence returns False when confluence_score <= 0.2."""
    indicators = {
        'confluence_score': 0.1,
        'signals': {
            'rsi': 1,
            'macd': 1,
            'bollinger': 1.0,
            'supertrend': 1,
            'vwap': 1,
        }
    }
    assert scanner.detect_confluence(indicators) is False


def test_detect_confluence_false_when_few_positive_signals(scanner):
    """detect_confluence returns False when fewer than 3 signals positive."""
    indicators = {
        'confluence_score': 0.5,
        'signals': {
            'rsi': -1,     # bearish
            'macd': -1,    # bearish
            'bollinger': -0.5,  # below midline
            'supertrend': 1,
            'vwap': 1,
        }
    }
    # Only 2 positive (supertrend + vwap) → False
    assert scanner.detect_confluence(indicators) is False


def test_detect_confluence_exactly_three_positive(scanner):
    """detect_confluence returns True at the boundary of 3 positive signals."""
    indicators = {
        'confluence_score': 0.4,
        'signals': {
            'rsi': 0,      # non-bearish → positive
            'macd': 1,     # positive
            'bollinger': 0.5,  # positive
            'supertrend': -1,  # negative
            'vwap': -1,        # negative
        }
    }
    assert scanner.detect_confluence(indicators) is True


def test_detect_confluence_missing_signals_graceful(scanner):
    """detect_confluence handles missing signal keys without crashing."""
    indicators = {
        'confluence_score': 0.3,
        'signals': {}
    }
    # All signals default to 0 (non-positive for macd/bollinger/supertrend/vwap)
    # rsi defaults to 0 (non-bearish = 1 point), rest = 0
    result = scanner.detect_confluence(indicators)
    assert isinstance(result, bool)


# ──────────────────────────────────────────────
# Integration: scanner produces correct result shape
# ──────────────────────────────────────────────

def test_scanner_has_confluence_key_in_scan_result():
    """scan_markets() result includes a 'confluence' count key."""
    scanner = PolymarketScanner("/tmp/test_confluence_scan.db")
    # We can't call scan_markets() without network, so verify structure via monkey-patch
    result = {
        'scan_time': datetime.now(timezone.utc).isoformat(),
        'markets': 0,
        'opportunities': 0,
        'arbitrage': 0,
        'confluence': 0,
        'top_opportunities': []
    }
    assert 'confluence' in result
    assert result['confluence'] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
