#!/usr/bin/env python3
"""
Tests for Task 887: Trailing Stop Implementation

Verifies _check_stop_loss_take_profit() trailing stop logic:
- HWM is updated when price rises
- Trailing stop activates at +2% gain
- When trailing stop is active, it REPLACES fixed take profit
- Trailing stop fires when price drops trailing_stop_pct below HWM
- Classic scenario: up 8%, drops 3% -> closes at ~+5%
- trailing_stop_pct read from champion params (default 3%)
"""

import sys
import os
import sqlite3
import tempfile
import shutil
from unittest.mock import MagicMock, patch
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading.paper_trader import PaperTrader


def make_trader_with_position(base_dir, symbol, strategy, entry_price,
                               high_water_mark=None, trailing_stop_active=0):
    """Create PaperTrader with one seeded position including HWM state."""
    trader = PaperTrader(base_dir=base_dir)
    db_path = Path(base_dir) / 'data' / 'positions.db'
    hwm = high_water_mark if high_water_mark is not None else entry_price
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO positions
               (symbol, strategy, side, quantity, entry_price, current_price,
                entry_time, status, high_water_mark, trailing_stop_active)
               VALUES (?, ?, 'long', 1.0, ?, ?, datetime('now'), 'open', ?, ?)""",
            (symbol, strategy, entry_price, entry_price, hwm, trailing_stop_active)
        )
        conn.commit()
    return trader


def test_trailing_stop_not_active_below_threshold():
    """Position up 1.5% should NOT activate trailing stop (threshold=2%)."""
    base_dir = tempfile.mkdtemp()
    try:
        trader = make_trader_with_position(base_dir, 'AAPL', 'RSI Mean Reversion', 100.0)
        trader.active_params = {
            'stop_loss_pct': 0.05,
            'take_profit_pct': 0.06,
            'trailing_stop_pct': 0.03,
        }
        mock_quote = MagicMock()
        mock_quote.last = 101.5  # +1.5% — below activation threshold
        trader.alpaca.get_quote = MagicMock(return_value=mock_quote)
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        trader._execute_sell_order.assert_not_called()

        # Verify trailing_stop_active is still 0
        db_path = Path(base_dir) / 'data' / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT trailing_stop_active FROM positions WHERE symbol='AAPL'"
            ).fetchone()
        assert row[0] == 0, f"trailing_stop_active should still be 0, got {row[0]}"
        print('PASS: trailing_stop_not_active_below_threshold')
    finally:
        shutil.rmtree(base_dir)


def test_trailing_stop_activates_at_2pct_gain():
    """Position up 2.5% should activate trailing stop."""
    base_dir = tempfile.mkdtemp()
    try:
        trader = make_trader_with_position(base_dir, 'MSFT', 'RSI Mean Reversion', 200.0)
        trader.active_params = {
            'stop_loss_pct': 0.05,
            'take_profit_pct': 0.06,
            'trailing_stop_pct': 0.03,
        }
        mock_quote = MagicMock()
        mock_quote.last = 205.0  # +2.5% — above activation threshold, below TP (6%)
        trader.alpaca.get_quote = MagicMock(return_value=mock_quote)
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        # Should NOT close (price has not dropped 3% from HWM)
        trader._execute_sell_order.assert_not_called()

        # trailing_stop_active should now be 1
        db_path = Path(base_dir) / 'data' / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT trailing_stop_active, high_water_mark FROM positions WHERE symbol='MSFT'"
            ).fetchone()
        assert row[0] == 1, f"trailing_stop_active should be 1, got {row[0]}"
        print('PASS: trailing_stop_activates_at_2pct_gain')
    finally:
        shutil.rmtree(base_dir)


def test_trailing_stop_fires_on_pullback():
    """Classic: position went up 8%, now down 3% from HWM -> should close at ~+5%."""
    base_dir = tempfile.mkdtemp()
    try:
        # Entry=$100, HWM=$108 (up 8%), current=$104.70 (3% below HWM: 108 * 0.97 = 104.76)
        entry = 100.0
        hwm = 108.0
        current = 104.70  # 104.70 < 108 * 0.97 = 104.76 -> should trigger

        trader = make_trader_with_position(
            base_dir, 'TSLA', 'RSI Mean Reversion',
            entry_price=entry, high_water_mark=hwm, trailing_stop_active=1
        )
        trader.active_params = {
            'stop_loss_pct': 0.05,
            'take_profit_pct': 0.06,
            'trailing_stop_pct': 0.03,
        }
        mock_quote = MagicMock()
        mock_quote.last = current
        trader.alpaca.get_quote = MagicMock(return_value=mock_quote)
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        trader._execute_sell_order.assert_called_once_with('TSLA', 'RSI Mean Reversion', current)
        print(f'PASS: trailing_stop_fires_on_pullback (entry=${entry} HWM=${hwm} exit=${current} => +{(current-entry)/entry*100:.1f}%)')
    finally:
        shutil.rmtree(base_dir)


def test_trailing_stop_does_not_fire_above_floor():
    """Position up 8%, current 2% from HWM (floor is 3%) -> should NOT close."""
    base_dir = tempfile.mkdtemp()
    try:
        entry = 100.0
        hwm = 108.0
        current = 106.0  # 108 * 0.97 = 104.76; 106 > 104.76 -> no trigger

        trader = make_trader_with_position(
            base_dir, 'GOOG', 'RSI Mean Reversion',
            entry_price=entry, high_water_mark=hwm, trailing_stop_active=1
        )
        trader.active_params = {
            'stop_loss_pct': 0.05,
            'take_profit_pct': 0.06,
            'trailing_stop_pct': 0.03,
        }
        mock_quote = MagicMock()
        mock_quote.last = current
        trader.alpaca.get_quote = MagicMock(return_value=mock_quote)
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        trader._execute_sell_order.assert_not_called()
        print('PASS: trailing_stop_does_not_fire_above_floor')
    finally:
        shutil.rmtree(base_dir)


def test_trailing_stop_replaces_fixed_tp():
    """When trailing stop is active, fixed TP should NOT fire even at +7%."""
    base_dir = tempfile.mkdtemp()
    try:
        # Position is at +7% (above 6% fixed TP) but trailing stop is active
        # HWM = entry * 1.07, current just dropped slightly but still above trail floor
        entry = 100.0
        hwm = 107.0
        current = 107.0  # no pullback yet — trailing active but floor not hit

        trader = make_trader_with_position(
            base_dir, 'AMZN', 'RSI Mean Reversion',
            entry_price=entry, high_water_mark=hwm, trailing_stop_active=1
        )
        trader.active_params = {
            'stop_loss_pct': 0.05,
            'take_profit_pct': 0.06,
            'trailing_stop_pct': 0.03,
        }
        mock_quote = MagicMock()
        mock_quote.last = current  # +7% but trailing is active -> no close
        trader.alpaca.get_quote = MagicMock(return_value=mock_quote)
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        # Should NOT close — trailing stop lets it run, floor = 107 * 0.97 = 103.79 > 107? No.
        # floor = 103.79, current = 107 > 103.79 -> no trigger. Let winner run.
        trader._execute_sell_order.assert_not_called()
        print('PASS: trailing_stop_replaces_fixed_tp')
    finally:
        shutil.rmtree(base_dir)


def test_stop_loss_overrides_trailing_stop():
    """Even with trailing stop active, stop loss should still fire if down from entry."""
    base_dir = tempfile.mkdtemp()
    try:
        # Entry=$100, HWM=$105 (trailing active), price crashed to $93 (SL at 5%)
        entry = 100.0
        hwm = 105.0
        current = 93.0  # -7% from entry -> should trigger SL

        trader = make_trader_with_position(
            base_dir, 'AMD', 'RSI Mean Reversion',
            entry_price=entry, high_water_mark=hwm, trailing_stop_active=1
        )
        trader.active_params = {
            'stop_loss_pct': 0.05,
            'take_profit_pct': 0.06,
            'trailing_stop_pct': 0.03,
        }
        mock_quote = MagicMock()
        mock_quote.last = current
        trader.alpaca.get_quote = MagicMock(return_value=mock_quote)
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        trader._execute_sell_order.assert_called_once_with('AMD', 'RSI Mean Reversion', current)
        print('PASS: stop_loss_overrides_trailing_stop')
    finally:
        shutil.rmtree(base_dir)


def test_hwm_updates_when_price_rises():
    """HWM should be updated in DB when price rises above it."""
    base_dir = tempfile.mkdtemp()
    try:
        entry = 100.0
        old_hwm = 103.0
        current = 107.0  # new high

        trader = make_trader_with_position(
            base_dir, 'META', 'RSI Mean Reversion',
            entry_price=entry, high_water_mark=old_hwm, trailing_stop_active=1
        )
        trader.active_params = {
            'stop_loss_pct': 0.05,
            'take_profit_pct': 0.06,
            'trailing_stop_pct': 0.03,
        }
        mock_quote = MagicMock()
        mock_quote.last = current
        trader.alpaca.get_quote = MagicMock(return_value=mock_quote)
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        # HWM should now be 107.0
        db_path = Path(base_dir) / 'data' / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT high_water_mark FROM positions WHERE symbol='META'"
            ).fetchone()
        assert abs(row[0] - 107.0) < 0.01, f"Expected HWM=107.0, got {row[0]}"
        print('PASS: hwm_updates_when_price_rises')
    finally:
        shutil.rmtree(base_dir)


def test_trailing_stop_pct_from_champion_params():
    """trailing_stop_pct should be read from active_params (default 3%)."""
    base_dir = tempfile.mkdtemp()
    try:
        # Use 5% trailing stop instead of default 3%
        entry = 100.0
        hwm = 108.0
        # 3% trail would fire at 108 * 0.97 = 104.76
        # 5% trail fires at 108 * 0.95 = 102.60
        current = 104.0  # between 5% floor (102.60) and 3% floor (104.76)
        # With 5% trail: 104 > 102.60 -> NO trigger
        # With default 3% trail: 104 < 104.76 -> would trigger

        trader = make_trader_with_position(
            base_dir, 'NVDA', 'RSI Mean Reversion',
            entry_price=entry, high_water_mark=hwm, trailing_stop_active=1
        )
        trader.active_params = {
            'stop_loss_pct': 0.05,
            'take_profit_pct': 0.06,
            'trailing_stop_pct': 0.05,  # 5% custom trail
        }
        mock_quote = MagicMock()
        mock_quote.last = current
        trader.alpaca.get_quote = MagicMock(return_value=mock_quote)
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        # 5% trail -> floor = 102.60, current=104 > 102.60 -> no trigger
        trader._execute_sell_order.assert_not_called()
        print('PASS: trailing_stop_pct_from_champion_params')
    finally:
        shutil.rmtree(base_dir)


if __name__ == '__main__':
    test_trailing_stop_not_active_below_threshold()
    test_trailing_stop_activates_at_2pct_gain()
    test_trailing_stop_fires_on_pullback()
    test_trailing_stop_does_not_fire_above_floor()
    test_trailing_stop_replaces_fixed_tp()
    test_stop_loss_overrides_trailing_stop()
    test_hwm_updates_when_price_rises()
    test_trailing_stop_pct_from_champion_params()
    print()
    print("All trailing stop tests passed.")
