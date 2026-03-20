#!/usr/bin/env python3
"""
Tests for Task 1.1: Active Stop Loss + Take Profit Execution

Verifies _check_stop_loss_take_profit() correctly:
- Closes positions that hit stop loss threshold
- Closes positions that hit take profit threshold
- Leaves positions within thresholds untouched
- Uses champion params (not hardcoded values)
"""

import sys
import os
import sqlite3
import tempfile
import shutil
from unittest.mock import MagicMock, patch
from pathlib import Path

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading.paper_trader import PaperTrader


def make_trader_with_positions(base_dir, positions):
    """Helper: create a PaperTrader with seeded positions in its DB."""
    trader = PaperTrader(base_dir=base_dir)
    
    # Seed positions into DB
    db_path = Path(base_dir) / 'data' / 'positions.db'
    with sqlite3.connect(db_path) as conn:
        for pos in positions:
            conn.execute(
                """INSERT INTO positions
                   (symbol, strategy, side, quantity, entry_price, current_price,
                    entry_time, status)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 'open')""",
                (pos['symbol'], pos['strategy'], pos['side'],
                 pos['quantity'], pos['entry_price'], pos['entry_price'])
            )
    return trader


def test_stop_loss_triggers():
    """Position down 6% should trigger stop loss (SL=5%)."""
    base_dir = tempfile.mkdtemp()
    try:
        trader = make_trader_with_positions(base_dir, [
            {'symbol': 'AAPL', 'strategy': 'RSI Mean Reversion',
             'side': 'long', 'quantity': 1.0, 'entry_price': 100.0}
        ])
        # active_params: SL=5%
        trader.active_params = {'stop_loss_pct': 0.05, 'take_profit_pct': 0.06}

        # Mock quote: price dropped 6%
        mock_quote = MagicMock()
        mock_quote.last = 94.0  # -6%
        trader.alpaca.get_quote = MagicMock(return_value=mock_quote)

        # Mock _execute_sell_order to capture call and return True
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        trader._execute_sell_order.assert_called_once_with('AAPL', 'RSI Mean Reversion', 94.0)
        print('PASS: stop_loss_triggers')
    finally:
        shutil.rmtree(base_dir)


def test_take_profit_triggers():
    """Position up 7%: trailing stop activates and replaces fixed TP.
    
    Updated for Task 887: when trailing stop activates (>= +2%), it replaces
    the fixed take profit. At +7% with no pullback, position stays open
    (trailing stop lets winners run until they drop trail_pct from HWM).
    """
    base_dir = tempfile.mkdtemp()
    try:
        trader = make_trader_with_positions(base_dir, [
            {'symbol': 'MSFT', 'strategy': 'MACD Crossover',
             'side': 'long', 'quantity': 0.5, 'entry_price': 200.0}
        ])
        trader.active_params = {
            'stop_loss_pct': 0.05, 'take_profit_pct': 0.06, 'trailing_stop_pct': 0.03
        }

        mock_quote = MagicMock()
        mock_quote.last = 214.0  # +7% — trailing stop activates; no pullback yet
        trader.alpaca.get_quote = MagicMock(return_value=mock_quote)
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        # Trailing stop is active; fixed TP bypassed.
        # Floor = 214 * 0.97 = 207.58; current (214) > floor -> no close yet.
        trader._execute_sell_order.assert_not_called()

        # Verify trailing_stop_active=1
        db_path = Path(base_dir) / 'data' / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT trailing_stop_active FROM positions WHERE symbol='MSFT'"
            ).fetchone()
        assert row[0] == 1, f"Expected trailing_stop_active=1, got {row[0]}"
        print('PASS: take_profit_triggers_replaced_by_trailing_stop')
    finally:
        shutil.rmtree(base_dir)
def test_no_trigger_within_thresholds():
    """Position at +3% should NOT trigger (SL=5%, TP=6%)."""
    base_dir = tempfile.mkdtemp()
    try:
        trader = make_trader_with_positions(base_dir, [
            {'symbol': 'TSLA', 'strategy': 'Bollinger Bounce',
             'side': 'long', 'quantity': 2.0, 'entry_price': 150.0}
        ])
        trader.active_params = {'stop_loss_pct': 0.05, 'take_profit_pct': 0.06}

        mock_quote = MagicMock()
        mock_quote.last = 154.5  # +3%
        trader.alpaca.get_quote = MagicMock(return_value=mock_quote)
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        trader._execute_sell_order.assert_not_called()
        print('PASS: no_trigger_within_thresholds')
    finally:
        shutil.rmtree(base_dir)


def test_short_position_stop_loss():
    """Short position: price up 6% should trigger stop loss."""
    base_dir = tempfile.mkdtemp()
    try:
        trader = make_trader_with_positions(base_dir, [
            {'symbol': 'AMD', 'strategy': 'MACD Crossover',
             'side': 'short', 'quantity': 1.0, 'entry_price': 100.0}
        ])
        trader.active_params = {'stop_loss_pct': 0.05, 'take_profit_pct': 0.06}

        mock_quote = MagicMock()
        mock_quote.last = 107.0  # entry was short, price went UP 7% — loss
        trader.alpaca.get_quote = MagicMock(return_value=mock_quote)
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        trader._execute_sell_order.assert_called_once_with('AMD', 'MACD Crossover', 107.0)
        print('PASS: short_position_stop_loss')
    finally:
        shutil.rmtree(base_dir)


def test_uses_champion_params_not_hardcoded():
    """SL/TP should use active_params, not hardcoded 5%/6%."""
    base_dir = tempfile.mkdtemp()
    try:
        trader = make_trader_with_positions(base_dir, [
            {'symbol': 'AAPL', 'strategy': 'RSI Mean Reversion',
             'side': 'long', 'quantity': 1.0, 'entry_price': 100.0}
        ])
        # Custom params: SL=10%, TP=15%
        trader.active_params = {'stop_loss_pct': 0.10, 'take_profit_pct': 0.15}

        mock_quote = MagicMock()
        mock_quote.last = 94.0  # -6% — would trigger default SL but NOT 10% SL
        trader.alpaca.get_quote = MagicMock(return_value=mock_quote)
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        # Should NOT close — 6% < 10% SL
        trader._execute_sell_order.assert_not_called()
        print('PASS: uses_champion_params_not_hardcoded')
    finally:
        shutil.rmtree(base_dir)


def test_multiple_positions_independent():
    """Multiple positions: only those hitting thresholds get closed."""
    base_dir = tempfile.mkdtemp()
    try:
        trader = make_trader_with_positions(base_dir, [
            {'symbol': 'AAPL', 'strategy': 'RSI Mean Reversion',
             'side': 'long', 'quantity': 1.0, 'entry_price': 100.0},  # will hit SL
            {'symbol': 'MSFT', 'strategy': 'MACD Crossover',
             'side': 'long', 'quantity': 1.0, 'entry_price': 200.0},  # safe
        ])
        trader.active_params = {'stop_loss_pct': 0.05, 'take_profit_pct': 0.06}

        def mock_quote_fn(symbol):
            m = MagicMock()
            m.last = 93.0 if symbol == 'AAPL' else 202.0  # AAPL -7%, MSFT +1%
            return m

        trader.alpaca.get_quote = mock_quote_fn
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        # Only AAPL should be closed
        assert trader._execute_sell_order.call_count == 1
        args = trader._execute_sell_order.call_args[0]
        assert args[0] == 'AAPL'
        print('PASS: multiple_positions_independent')
    finally:
        shutil.rmtree(base_dir)


def test_no_positions_no_crash():
    """Empty positions DB — should not crash."""
    base_dir = tempfile.mkdtemp()
    try:
        trader = PaperTrader(base_dir=base_dir)
        trader.active_params = {'stop_loss_pct': 0.05, 'take_profit_pct': 0.06}
        trader.alpaca.get_quote = MagicMock(return_value=None)
        trader._execute_sell_order = MagicMock(return_value=True)
        trader._check_stop_loss_take_profit()  # should not raise
        trader._execute_sell_order.assert_not_called()
        print('PASS: no_positions_no_crash')
    finally:
        shutil.rmtree(base_dir)


def test_sl_tp_called_before_signals_in_scan():
    """scan_and_trade() must call _check_stop_loss_take_profit before get_latest_tournament_winners."""
    base_dir = tempfile.mkdtemp()
    try:
        trader = PaperTrader(base_dir=base_dir)
        trader.active_params = {'stop_loss_pct': 0.05, 'take_profit_pct': 0.06}
        call_order = []
        trader._check_stop_loss_take_profit = MagicMock(side_effect=lambda: call_order.append('sl_tp'))
        trader.get_latest_tournament_winners = MagicMock(side_effect=lambda: call_order.append('winners') or [])
        trader.position_manager.get_portfolio_state = MagicMock(return_value=MagicMock(
            total_value=500, position_count=2, strategies_active=[]
        ))
        trader.position_manager.save_portfolio_snapshot = MagicMock()
        trader.scan_and_trade()
        # sl_tp must come before winners
        assert call_order[0] == 'sl_tp', f"Expected sl_tp first, got: {call_order}"
        print('PASS: sl_tp_called_before_signals_in_scan')
    finally:
        shutil.rmtree(base_dir)



def test_fraction_params_match_champion_format():
    """Champion stores stop_loss_pct=0.05 (fraction). Verify 0.05 is treated as 5% SL."""
    base_dir = tempfile.mkdtemp()
    try:
        trader = make_trader_with_positions(base_dir, [
            {'symbol': 'AAPL', 'strategy': 'RSI Mean Reversion',
             'side': 'long', 'quantity': 1.0, 'entry_price': 100.0}
        ])
        # Real champion format: fractions
        trader.active_params = {'stop_loss_pct': 0.05, 'take_profit_pct': 0.06}

        mock_quote = MagicMock()
        mock_quote.last = 94.0  # -6% — should trigger 5% SL
        trader.alpaca.get_quote = MagicMock(return_value=mock_quote)
        trader._execute_sell_order = MagicMock(return_value=True)

        trader._check_stop_loss_take_profit()

        trader._execute_sell_order.assert_called_once_with('AAPL', 'RSI Mean Reversion', 94.0)
        print('PASS: fraction_params_match_champion_format')
    finally:
        shutil.rmtree(base_dir)


if __name__ == '__main__':
    test_stop_loss_triggers()
    test_take_profit_triggers()
    test_no_trigger_within_thresholds()
    test_short_position_stop_loss()
    test_uses_champion_params_not_hardcoded()
    test_multiple_positions_independent()
    test_no_positions_no_crash()
    test_sl_tp_called_before_signals_in_scan()
    test_fraction_params_match_champion_format()
    print()
    print('All SL/TP tests passed.')
