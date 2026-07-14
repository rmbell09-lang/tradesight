#!/usr/bin/env python3
"""
Tests for TradeSight Paper Trading System
"""

import pytest
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import sys
import os
from datetime import datetime, timedelta

# Add src to path for testing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading.position_manager import PositionManager, Position, PortfolioState
from trading.paper_trader import PaperTrader
from trading.feedback_tracker import FeedbackTracker


class TestPositionManager:
    """Test suite for PositionManager class"""
    
    def setup_method(self):
        """Setup test environment"""
        self.temp_dir = tempfile.mkdtemp()
        self.pm = PositionManager(base_dir=self.temp_dir)
    
    def teardown_method(self):
        """Cleanup test environment"""
        import shutil
        shutil.rmtree(self.temp_dir)
    
    def test_initialization(self):
        """Test position manager initialization"""
        assert self.pm.base_dir == Path(self.temp_dir)
        assert self.pm.data_dir.exists()
        assert self.pm.config['initial_balance'] > 0
        
        # Verify database was created
        db_path = self.pm.data_dir / 'positions.db'
        assert db_path.exists()
        
        # Check tables exist
        with sqlite3.connect(db_path) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t[0] for t in tables]
            assert 'positions' in table_names
            assert 'portfolio_history' in table_names
    
    def test_custom_initial_balance(self):
        """PositionManager accepts custom initial balance."""
        pm = PositionManager(base_dir=self.temp_dir, initial_balance=1500.0)
        assert pm.config['initial_balance'] == 1500.0

    def test_open_position_success(self):
        """Test successful position opening"""
        success = self.pm.open_position(
            symbol='AAPL',
            strategy='MACD Crossover',
            side='long',
            quantity=100,
            entry_price=150.0
        )
        
        assert success
        
        # Verify position was stored
        db_path = self.pm.data_dir / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            position = conn.execute(
                'SELECT * FROM positions WHERE symbol = ? AND strategy = ?',
                ('AAPL', 'MACD Crossover')
            ).fetchone()
            
            assert position is not None
            assert position[1] == 'AAPL'  # symbol
            assert position[2] == 'MACD Crossover'  # strategy
            assert position[3] == 'long'  # side
            assert position[4] == 100  # quantity
            assert position[5] == 150.0  # entry_price
    
    def test_close_position_success(self):
        """Test successful position closing"""
        # First open a position
        self.pm.open_position('MSFT', 'RSI Mean Reversion', 'long', 50, 300.0)
        
        # Close the position
        success = self.pm.close_position('MSFT', 'RSI Mean Reversion', 310.0)
        assert success
        
        # Verify position was closed
        db_path = self.pm.data_dir / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            position = conn.execute(
                'SELECT status, exit_price, realized_pnl FROM positions WHERE symbol = ? AND strategy = ?',
                ('MSFT', 'RSI Mean Reversion')
            ).fetchone()
            
            assert position[0] == 'closed'  # status
            assert position[1] == 310.0  # exit_price
            assert position[2] == 500.0  # realized_pnl (50 * (310 - 300))
    
    def test_close_position_not_found(self):
        """Test closing non-existent position"""
        success = self.pm.close_position('NONEXISTENT', 'Fake Strategy', 100.0)
        assert not success
    
    def test_update_positions(self):
        """Test position price updates"""
        # Open positions
        self.pm.open_position('AAPL', 'Test Strategy', 'long', 100, 150.0)
        self.pm.open_position('MSFT', 'Test Strategy', 'short', 50, 300.0)
        
        # Update prices
        price_data = {'AAPL': 155.0, 'MSFT': 295.0}
        self.pm.update_positions(price_data)
        
        # Verify updates
        db_path = self.pm.data_dir / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            positions = conn.execute(
                'SELECT symbol, current_price, unrealized_pnl FROM positions WHERE status = "open"'
            ).fetchall()
            
            position_dict = {pos[0]: (pos[1], pos[2]) for pos in positions}
            
            # AAPL long: (155 - 150) * 100 = +500
            assert position_dict['AAPL'][0] == 155.0
            assert position_dict['AAPL'][1] == 500.0
            
            # MSFT short: (300 - 295) * 50 = +250
            assert position_dict['MSFT'][0] == 295.0
            assert position_dict['MSFT'][1] == 250.0
    
    def test_portfolio_state(self):
        """Test portfolio state calculation"""
        # Open some positions
        self.pm.open_position('AAPL', 'Strategy A', 'long', 100, 150.0)
        self.pm.open_position('MSFT', 'Strategy B', 'long', 50, 300.0)
        
        # Update prices
        self.pm.update_positions({'AAPL': 155.0, 'MSFT': 310.0})
        
        # Close one position
        self.pm.close_position('AAPL', 'Strategy A', 155.0)
        
        # Get portfolio state
        state = self.pm.get_portfolio_state()
        
        assert isinstance(state, PortfolioState)
        assert state.position_count == 1  # MSFT still open
        assert state.realized_pnl == 500.0  # AAPL profit
        assert state.unrealized_pnl == 500.0  # MSFT profit
        assert state.total_pnl == 1000.0  # Total profit
        assert state.total_value == self.pm.config['initial_balance'] + 1000.0
        assert 'Strategy B' in state.strategies_active
        assert 'Strategy A' not in state.strategies_active
    
    def test_calculate_position_size(self):
        """Test position sizing calculation"""
        # Test basic position sizing
        size = self.pm.calculate_position_size('AAPL', 'Test Strategy', 150.0)
        
        # Should be limited by max position size (10% of portfolio)
        expected_max_value = self.pm.config['initial_balance'] * self.pm.config['max_position_size']
        expected_max_shares = round(expected_max_value / 150.0, 6)
        
        assert size == expected_max_shares
        
        # Test with existing strategy allocation
        # Open a large position for the same strategy
        large_quantity = int(self.pm.config['initial_balance'] * 0.20 / 150.0)  # 20% of portfolio
        self.pm.open_position('MSFT', 'Test Strategy', 'long', large_quantity, 300.0)
        
        # Update current prices
        self.pm.update_positions({'MSFT': 300.0})
        
        # Now position size should be limited by strategy allocation
        size = self.pm.calculate_position_size('GOOGL', 'Test Strategy', 200.0)
        
        # Available strategy allocation should be reduced
        assert size < expected_max_shares
    
    def test_performance_report(self):
        """Test performance report generation"""
        # Create some trading history
        self.pm.open_position('AAPL', 'Strategy A', 'long', 100, 150.0)
        self.pm.close_position('AAPL', 'Strategy A', 155.0)
        
        self.pm.open_position('MSFT', 'Strategy B', 'short', 50, 300.0)
        self.pm.close_position('MSFT', 'Strategy B', 295.0)
        
        # Generate report
        report = self.pm.get_performance_report(days=30)
        
        assert "Portfolio Performance Report" in report
        assert "Portfolio Value:" in report
        assert "Strategy A" in report or "Strategy B" in report
        assert "AAPL" in report or "MSFT" in report
    
    def test_portfolio_snapshot(self):
        """Test portfolio snapshot saving"""
        # Save a snapshot
        self.pm.save_portfolio_snapshot()
        
        # Verify it was saved
        db_path = self.pm.data_dir / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            snapshots = conn.execute(
                'SELECT * FROM portfolio_history ORDER BY timestamp DESC LIMIT 1'
            ).fetchone()
            
            assert snapshots is not None
            assert snapshots[2] == self.pm.config['initial_balance']  # total_value


class TestPaperTrader:
    """Test suite for PaperTrader class"""
    
    def setup_method(self):
        """Setup test environment"""
        self.temp_dir = tempfile.mkdtemp()
        self.trader = PaperTrader(base_dir=self.temp_dir)
    
    def teardown_method(self):
        """Cleanup test environment"""
        import shutil
        shutil.rmtree(self.temp_dir)
    
    def test_initialization(self):
        """Test paper trader initialization"""
        assert self.trader.base_dir == Path(self.temp_dir).resolve()
        assert self.trader.position_manager is not None
        assert self.trader.automation is not None
        assert self.trader.alpaca is not None  # Should be in demo mode
        assert len(self.trader.config['trading_symbols']) > 0

    def test_initialization_with_custom_initial_balance(self):
        """PaperTrader forwards custom initial balance to PositionManager."""
        trader = PaperTrader(base_dir=self.temp_dir, initial_balance=2000.0)
        assert trader.position_manager.config['initial_balance'] == 2000.0

    def test_initialization_normalizes_src_base_dir(self):
        """Passing project/src should normalize to project root."""
        src_dir = Path(self.temp_dir) / "src"
        (src_dir / "trading").mkdir(parents=True, exist_ok=True)

        trader = PaperTrader(base_dir=str(src_dir))

        assert trader.base_dir == Path(self.temp_dir).resolve()
        assert trader.data_dir == Path(self.temp_dir).resolve() / "data"
    
    @patch('trading.paper_trader.sqlite3.connect')
    def test_get_tournament_winners_empty(self, mock_connect):
        """Test getting tournament winners with no data"""
        # Mock empty database
        mock_conn = Mock()
        mock_connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value.fetchall.return_value = []
        
        winners = self.trader.get_latest_tournament_winners()
        assert winners == []
    
    @patch('trading.paper_trader.sqlite3.connect')
    def test_get_tournament_winners_with_data(self, mock_connect):
        """Test getting tournament winners with tournament data"""
        # Mock database with winners
        mock_conn = Mock()
        mock_connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.return_value.fetchall.return_value = [
            ('MACD Crossover', 0.75, '2026-02-28T10:00:00'),
            ('RSI Mean Reversion', 0.68, '2026-02-28T11:00:00'),
            ('Bollinger Bounce', 0.55, '2026-02-28T12:00:00')  # Above 0.50 threshold
        ]
        
        # Mock path exists
        self.trader.automation.data_dir = Path(self.temp_dir)
        db_path = self.trader.automation.data_dir / 'tournament_history.db'
        db_path.touch()  # Create empty file
        
        winners = self.trader.get_latest_tournament_winners()
        
        # Should return strategies above confidence threshold (0.50)
        assert len(winners) == 3
        assert ('MACD Crossover', 0.75) in winners
        assert ('RSI Mean Reversion', 0.68) in winners
        assert ('Bollinger Bounce', 0.55) in winners
    
    @patch('trading.paper_trader.TechnicalIndicators')
    def test_generate_trading_signals_macd(self, mock_indicators):
        """Test MACD trading signal generation"""
        # Mock market data: long decline then sharp final jump forces MACD bullish cross
        import pandas as pd
        import numpy as np
        prices = list(np.linspace(200, 100, 199)) + [200.0]
        mock_data = pd.DataFrame({
            'open': [p - 0.5 for p in prices],
            'high': [p + 1.0 for p in prices],
            'low': [p - 1.0 for p in prices],
            'close': prices,
            'volume': [1000000] * 200
        }, index=pd.date_range('2026-01-01', periods=200, freq='h'))
        self.trader.alpaca.get_historical_data = Mock(return_value=mock_data)
        mock_indicator_instance = Mock()
        mock_indicators.return_value = mock_indicator_instance
        mock_indicator_instance.calculate_all.return_value = {"signals": {}, "indicators": {}}
        mock_indicator_instance.calculate_all.return_value = {"signals": {}, "indicators": {}}
        
        # Generate signal
        signal = self.trader.generate_trading_signals('AAPL', 'MACD Crossover')
        
        assert signal is not None
        assert signal['symbol'] == 'AAPL'
        assert signal['strategy'] == 'MACD Crossover'
        assert signal['action'] == 'buy'
        assert signal['side'] == 'long'
        assert signal['confidence'] == 0.75
        assert 'MACD bullish crossover' in signal['reason']
    
    @patch('trading.paper_trader.TechnicalIndicators')
    def test_generate_trading_signals_rsi(self, mock_indicators):
        """Test RSI trading signal generation — bullish daily trend with oversold RSI.
        
        Uses ascending prices so daily SMA50 trend filter (Task 14) passes,
        while mocked RSI = 20.0 simulates an oversold pullback within an uptrend.
        """
        import pandas as pd
        # Ascending prices: price rises 100→300 so daily trend = bullish (price > SMA50)
        # This simulates a healthy uptrend with a temporary oversold dip (RSI mocked at 20)
        close_prices = list(range(100, 300))  # 200 bars, ascending
        mock_data = pd.DataFrame({
            'open': [p - 0.5 for p in close_prices],
            'high': [p + 1.0 for p in close_prices],
            'low': [p - 1.0 for p in close_prices],
            'close': close_prices,
            'volume': [1_000_000] * 200
        }, index=pd.date_range('2026-01-01', periods=200, freq='1h'))
        
        self.trader.alpaca.get_historical_data = Mock(return_value=mock_data)
        
        # Mock RSI indicators with proper structure
        mock_indicator_instance = Mock()
        mock_indicators.return_value = mock_indicator_instance
        
        # RSI value of 20.0 is clearly oversold (below any reasonable threshold)
        mock_indicator_instance.calculate_all.return_value = {
            "indicators": {"rsi": 20.0},  # Strongly oversold RSI (well below typical 25-33 threshold)
            "signals": {}
        }
        
        # Generate signal — should pass MTF filter (bullish trend) + RSI filter (oversold)
        signal = self.trader.generate_trading_signals('AAPL', 'RSI Mean Reversion')
        
        assert signal is not None, "Expected buy signal: RSI oversold in bullish daily trend"
        assert signal['action'] == 'buy'
        assert signal['side'] == 'long'
        assert signal['confidence'] > 0.60
        assert 'RSI oversold' in signal['reason']

    @patch('trading.paper_trader.TechnicalIndicators')
    def test_rsi_mtf_filter_blocks_bearish_trend(self, mock_indicators):
        """Task 14: MTF filter must block RSI buy signals when daily trend is bearish.
        
        Uses declining prices so daily SMA50 filter detects a downtrend,
        which should suppress the RSI buy signal even when RSI = 20.
        """
        import pandas as pd
        # Declining prices: 200 → 1 → daily trend = bearish
        close_prices = list(range(200, 0, -1))
        mock_data = pd.DataFrame({
            'open': [p - 0.5 for p in close_prices],
            'high': [p + 1.0 for p in close_prices],
            'low': [p - 1.0 for p in close_prices],
            'close': close_prices,
            'volume': [1_000_000] * 200
        }, index=pd.date_range('2026-01-01', periods=200, freq='1h'))
        
        self.trader.alpaca.get_historical_data = Mock(return_value=mock_data)
        
        mock_indicator_instance = Mock()
        mock_indicators.return_value = mock_indicator_instance
        mock_indicator_instance.calculate_all.return_value = {
            "indicators": {"rsi": 20.0},
            "signals": {}
        }
        
        # Signal should be blocked — RSI oversold but daily trend is bearish
        signal = self.trader.generate_trading_signals('AAPL', 'RSI Mean Reversion')
        assert signal is None, "MTF filter should block RSI buy in bearish daily trend"
    
    @patch('trading.paper_trader.TechnicalIndicators')
    def test_generate_trading_signals_bollinger_uses_current_indicator_format(self, mock_indicators):
        """Bollinger strategy should read indicators['bollinger'] dict produced by TechnicalIndicators."""
        import pandas as pd

        close_prices = [100.0] * 199 + [95.0]  # Last price near mocked lower band
        mock_data = pd.DataFrame({
            'open': [p - 0.5 for p in close_prices],
            'high': [p + 1.0 for p in close_prices],
            'low': [p - 1.0 for p in close_prices],
            'close': close_prices,
            'volume': [1_000_000] * 200
        }, index=pd.date_range('2026-01-01', periods=200, freq='1h'))

        self.trader.alpaca.get_historical_data = Mock(return_value=mock_data)

        mock_indicator_instance = Mock()
        mock_indicators.return_value = mock_indicator_instance
        mock_indicator_instance.calculate_all.return_value = {
            'indicators': {
                'bollinger': {
                    'upper': 110.0,
                    'middle': 100.0,
                    'lower': 94.0,
                    'position': 0.10,
                }
            },
            'signals': {}
        }

        signal = self.trader.generate_trading_signals('AAPL', 'Bollinger Bounce')

        assert signal is not None
        assert signal['action'] == 'buy'
        assert signal['side'] == 'long'
        assert 'Bollinger lower band' in signal['reason']

    def test_generate_trading_signals_insufficient_data(self):
        """Test signal generation with insufficient data"""
        # Mock insufficient data
        self.trader.alpaca.get_historical_data = Mock(return_value=None)
        
        signal = self.trader.generate_trading_signals('AAPL', 'MACD Crossover')
        assert signal is None
    
    def test_check_existing_position(self):
        """Test checking for existing positions"""
        # No existing position initially
        exists = self.trader._check_existing_position('AAPL', 'Test Strategy')
        assert not exists
        
        # Open a position
        self.trader.position_manager.open_position('AAPL', 'Test Strategy', 'long', 100, 150.0)
        
        # Now should find existing position
        exists = self.trader._check_existing_position('AAPL', 'Test Strategy')
        assert exists
    
    @patch('trading.paper_trader.datetime')
    def test_close_aged_positions(self, mock_datetime):
        """Test closing aged positions"""
        # Set current time
        current_time = datetime(2026, 2, 28, 15, 0, 0)
        mock_datetime.now.return_value = current_time
        
        # Open an old position (more than 5 days ago)
        old_time = current_time - timedelta(days=6)
        
        # Manually insert aged position
        db_path = self.trader.position_manager.data_dir / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            conn.execute('''
                INSERT INTO positions 
                (symbol, strategy, side, quantity, entry_price, current_price, entry_time, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', ('AAPL', 'Old Strategy', 'long', 100, 150.0, 155.0, 
                 old_time.isoformat(), 'open'))
            conn.commit()
        
        # Mock Alpaca quote
        mock_quote = Mock(); mock_quote.last = 160.0; self.trader.alpaca.get_quote = Mock(return_value=mock_quote)
        self.trader.alpaca.place_paper_trade = Mock(return_value={
            'status': 'filled', 
            'fill_price': 160.0
        })
        
        # Close aged positions
        self.trader.close_aged_positions()
        
        # Verify position was closed
        with sqlite3.connect(db_path) as conn:
            position = conn.execute(
                'SELECT status, exit_price FROM positions WHERE symbol = ? AND strategy = ?',
                ('AAPL', 'Old Strategy')
            ).fetchone()
            
            assert position[0] == 'closed'
            assert position[1] == 160.0
    
    def test_buy_order_persists_entry_order_fill_metadata(self):
        """Entry order_id + fill status should be written to positions DB."""
        self.trader.alpaca.place_paper_trade = Mock(return_value={
            'status': 'filled',
            'order_id': 'ord_entry_123',
            'fill_price': 151.25,
        })

        ok = self.trader._execute_buy_order('AAPL', 'RSI Mean Reversion', 'long', 1.0, 151.0)
        assert ok is True

        db_path = self.trader.position_manager.data_dir / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT entry_order_id, entry_fill_status, entry_price FROM positions "
                "WHERE symbol=? AND strategy=? AND status='open' ORDER BY id DESC LIMIT 1",
                ('AAPL', 'RSI Mean Reversion')
            ).fetchone()

        assert row is not None
        assert row[0] == 'ord_entry_123'
        assert row[1] == 'filled'
        assert row[2] == 151.25


    def test_trade_logger_empty_analysis_is_not_appended_to_positions_report(self):
        """Do not append contradictory 'No closed trades yet' when positions DB has closes."""
        self.trader.position_manager.open_position('AAPL', 'Confluence', 'long', 1.0, 100.0)
        self.trader.position_manager.close_position('AAPL', 'Confluence', 105.0)

        report = self.trader.generate_trading_report()
        report = self.trader._append_trade_logger_analysis(report, days=30)

        assert 'Recently Closed Trades' in report or 'Closed Trades' in report
        assert 'No closed trades yet' not in report

    def test_unverified_closed_trades_are_labeled_in_report(self):
        """Positions without broker exit fill metadata should be explicit, not blended as verified."""
        self.trader.position_manager.open_position('AAPL', 'Confluence', 'long', 1.0, 100.0)
        self.trader.position_manager.close_position('AAPL', 'Confluence', 105.0)

        report = self.trader.generate_trading_report()

        assert 'Local/Unverified Closed Trades' in report
        assert 'missing broker exit fill' in report

    def test_paper_trader_does_not_block_close_with_min_hold_guard(self):
        """Paper trading should allow protective/discretionary closes without a fake PDT block."""
        self.trader.position_manager.open_position('AAPL', 'RSI Mean Reversion', 'long', 2.0, 100.0)

        self.trader.alpaca.close_full_position = Mock(return_value={
            'status': 'closed',
            'order_id': 'ord_exit_paper',
            'fill_price': 110.0,
        })

        ok = self.trader._execute_sell_order('AAPL', 'RSI Mean Reversion', 109.0)
        assert ok is True

    def test_live_mode_still_respects_min_hold_guard(self):
        """If min-hold enforcement is explicitly enabled, a fresh position should stay open."""
        self.trader.config['enforce_min_hold_hours'] = True
        self.trader.config['min_hold_hours'] = 24
        self.trader.position_manager.open_position('AAPL', 'RSI Mean Reversion', 'long', 2.0, 100.0)

        self.trader.alpaca.close_full_position = Mock(return_value={
            'status': 'closed',
            'order_id': 'ord_exit_blocked',
            'fill_price': 110.0,
        })

        ok = self.trader._execute_sell_order('AAPL', 'RSI Mean Reversion', 109.0)
        assert ok is False
        self.trader.alpaca.close_full_position.assert_not_called()

    def test_sell_order_persists_exit_order_fill_metadata_and_pnl(self):
        """Exit order_id + fill status should be recorded and PnL should use fill price."""
        self.trader.config['min_hold_hours'] = 0
        self.trader.position_manager.open_position('AAPL', 'RSI Mean Reversion', 'long', 2.0, 100.0)

        self.trader.alpaca.close_full_position = Mock(return_value={
            'status': 'closed',
            'order_id': 'ord_exit_456',
            'fill_price': 110.0,
        })

        ok = self.trader._execute_sell_order('AAPL', 'RSI Mean Reversion', 109.0)
        assert ok is True

        db_path = self.trader.position_manager.data_dir / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT status, exit_order_id, exit_fill_status, exit_price, realized_pnl FROM positions "
                "WHERE symbol=? AND strategy=? ORDER BY id DESC LIMIT 1",
                ('AAPL', 'RSI Mean Reversion')
            ).fetchone()

        assert row is not None
        assert row[0] == 'closed'
        assert row[1] == 'ord_exit_456'
        assert row[2] == 'closed'
        assert row[3] == 110.0
        assert row[4] == 20.0

    def test_broker_verified_close_without_order_id_gets_synthetic_exit_reference(self):
        """Alpaca close-position success can lack order id; do not mark it suspicious/local."""
        self.trader.config['min_hold_hours'] = 0
        self.trader.position_manager.open_position('KO', 'RSI Mean Reversion', 'long', 2.0, 80.0)

        self.trader.alpaca.close_full_position = Mock(return_value={
            'status': 'closed',
            'fill_price': 82.0,
            'symbol': 'KO',
            'broker_verified': True,
        })

        ok = self.trader._execute_sell_order('KO', 'RSI Mean Reversion', 81.5)
        assert ok is True

        db_path = self.trader.position_manager.data_dir / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT status, exit_order_id, exit_fill_status, exit_price, realized_pnl FROM positions "
                "WHERE symbol=? AND strategy=? ORDER BY id DESC LIMIT 1",
                ('KO', 'RSI Mean Reversion')
            ).fetchone()

        assert row is not None
        assert row[0] == 'closed'
        assert row[1].startswith('alpaca_close_position:KO:')
        assert row[2] == 'closed'
        assert row[3] == 82.0
        assert row[4] == 4.0

    def test_pending_exit_order_does_not_fake_close_local_position(self):
        """accepted/pending_new broker exits stay open until fill reconciliation."""
        self.trader.config['min_hold_hours'] = 0
        self.trader.position_manager.open_position('COST', 'RSI Mean Reversion', 'long', 1.0, 900.0)

        self.trader.alpaca.close_full_position = Mock(return_value={
            'status': 'pending_new',
            'order_id': 'ord_exit_pending',
            'fill_price': None,
        })

        ok = self.trader._execute_sell_order('COST', 'RSI Mean Reversion', 905.0)
        assert ok is False
        assert self.trader._last_close_blocked == ('COST', 'RSI Mean Reversion', 'EXIT_PENDING')

        db_path = self.trader.position_manager.data_dir / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT status, exit_order_id, exit_fill_status, exit_price FROM positions "
                "WHERE symbol='COST' ORDER BY id DESC LIMIT 1"
            ).fetchone()

        assert row == ('open', 'ord_exit_pending', 'pending_new', None)

    def test_broker_fill_reconcile_closes_position_and_clears_trade_logger_open(self):
        """End-of-run reconciliation should align positions.db and trades.db."""
        self.trader.config['min_hold_hours'] = 0
        self.trader.position_manager.open_position('WMT', 'RSI Mean Reversion', 'long', 2.0, 70.0)
        db_path = self.trader.position_manager.data_dir / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE positions SET exit_order_id='ord_wmt_exit', exit_fill_status='pending_new' "
                "WHERE symbol='WMT'"
            )
            conn.commit()

        self.trader.alpaca.demo_mode = False
        self.trader.alpaca.get_order = Mock(return_value={
            'id': 'ord_wmt_exit',
            'status': 'filled',
            'filled_avg_price': '72.50',
        })

        reconciled = self.trader._reconcile_exit_fills(remote_symbols={'WMT'})
        assert reconciled == 1

        with sqlite3.connect(db_path) as conn:
            pos = conn.execute(
                "SELECT status, exit_price, realized_pnl, exit_fill_status FROM positions WHERE symbol='WMT'"
            ).fetchone()
        assert pos == ('closed', 72.5, 5.0, 'filled')

        trades_db = self.trader.position_manager.trade_logger.db_path
        with sqlite3.connect(trades_db) as conn:
            open_count = conn.execute(
                "SELECT COUNT(*) FROM open_trades WHERE symbol='WMT'"
            ).fetchone()[0]
            closed_count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE symbol='WMT'"
            ).fetchone()[0]
        assert open_count == 0
        assert closed_count == 1

    def test_symbol_cooldown_blocks_recent_close_and_losing_trailing_stop(self):
        """Same-symbol churn should be blocked after recent or losing protective exits."""
        db_path = self.trader.position_manager.data_dir / 'positions.db'
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO positions (symbol, strategy, side, quantity, entry_price, current_price, "
                "entry_time, exit_time, exit_price, realized_pnl, status, exit_reason) "
                "VALUES ('CVX', 'RSI Mean Reversion', 'long', 1, 100, 100, ?, ?, 99, -1, 'closed', 'TRAILING_STOP')",
                (
                    (datetime.now() - timedelta(days=2)).isoformat(),
                    (datetime.now() - timedelta(days=2)).isoformat(),
                )
            )
            conn.commit()

        assert self.trader._is_symbol_in_cooldown('CVX') is True

    def test_accounting_divergence_warning_appears_in_report(self):
        """Report should warn when local P&L and broker equity disagree."""
        self.trader.position_manager.open_position('AAPL', 'Confluence', 'long', 1.0, 100.0)
        self.trader.position_manager.close_position('AAPL', 'Confluence', 120.0)
        self.trader.position_manager.persist_balance_sync(
            buying_power=500.0,
            equity=502.0,
            positions_value=0.0,
        )

        report = self.trader.generate_trading_report()

        assert 'Accounting Reconciliation Warning' in report
        assert 'Treat broker equity as source of truth' in report

    def test_generate_trading_report(self):
        """Test trading report generation"""
        report = self.trader.generate_trading_report()
        
        assert "TradeSight Automated Trading Report" in report
        assert "Portfolio Summary" in report
        assert "Total Value:" in report
        assert "Available Cash:" in report



    # ── Task 890: Daily Loss Circuit Breaker ─────────────────────────────────

    def test_circuit_breaker_flag_initializes_false(self):
        """_daily_loss_limit_reached must start False"""
        assert self.trader._daily_loss_limit_reached is False

    def test_circuit_breaker_config_key_present(self):
        """daily_loss_limit key must exist in config with a positive value"""
        assert 'daily_loss_limit' in self.trader.config
        assert self.trader.config['daily_loss_limit'] > 0

    @patch('sqlite3.connect')
    def test_check_daily_loss_no_loss(self, mock_connect):
        """Returns False when no closed trades today"""
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(0.0,))),
            MagicMock(fetchall=MagicMock(return_value=[])),
        ]
        mock_connect.return_value = mock_conn
        assert self.trader._check_daily_loss_limit() is False
        assert self.trader._daily_loss_limit_reached is False

    @patch('sqlite3.connect')
    def test_check_daily_loss_under_limit(self, mock_connect):
        """Returns False when loss is below the configured limit"""
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(-5.0,))),
            MagicMock(fetchall=MagicMock(return_value=[])),
        ]
        mock_connect.return_value = mock_conn
        assert self.trader._check_daily_loss_limit() is False

    @patch('sqlite3.connect')
    def test_check_daily_loss_excludes_suspicious_placeholder_exits(self, mock_connect):
        """Suspicious demo-like exits must be excluded from circuit-breaker P&L"""
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(-10.0,))),
            MagicMock(fetchall=MagicMock(return_value=[('SPY', 'long', 582.11, 100.0, -58.98)])),
        ]
        mock_connect.return_value = mock_conn
        assert self.trader._check_daily_loss_limit() is False
        assert self.trader._daily_loss_limit_reached is False

    @patch('sqlite3.connect')
    def test_check_daily_loss_at_limit(self, mock_connect):
        """Returns True and sets flag when daily P&L == -limit"""
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(-15.0,))),
            MagicMock(fetchall=MagicMock(return_value=[])),
        ]
        mock_connect.return_value = mock_conn
        result = self.trader._check_daily_loss_limit()
        assert result is True
        assert self.trader._daily_loss_limit_reached is True

    @patch('sqlite3.connect')
    def test_check_daily_loss_over_limit(self, mock_connect):
        """Returns True when daily P&L exceeds limit"""
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(-22.50,))),
            MagicMock(fetchall=MagicMock(return_value=[])),
        ]
        mock_connect.return_value = mock_conn
        assert self.trader._check_daily_loss_limit() is True
        assert self.trader._daily_loss_limit_reached is True

    @patch('sqlite3.connect')
    def test_check_daily_loss_recalculates_and_can_reset_flag(self, mock_connect):
        """Even when flag is set, DB recalculation can reset circuit breaker"""
        self.trader._daily_loss_limit_reached = True
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(0.0,))),
            MagicMock(fetchall=MagicMock(return_value=[])),
        ]
        mock_connect.return_value = mock_conn
        result = self.trader._check_daily_loss_limit()
        assert result is False
        assert self.trader._daily_loss_limit_reached is False

    @patch('sqlite3.connect')
    def test_check_daily_loss_db_error_returns_false(self, mock_connect):
        """DB error must not crash the trader — returns False gracefully"""
        mock_connect.side_effect = Exception("DB locked")
        assert self.trader._check_daily_loss_limit() is False
        assert self.trader._daily_loss_limit_reached is False

    def test_execute_signal_blocked_when_limit_reached(self):
        """execute_signal returns False immediately when circuit breaker is active"""
        self.trader._daily_loss_limit_reached = True
        signal = {
            'symbol': 'AAPL', 'strategy': 'RSI', 'action': 'buy',
            'side': 'long', 'current_price': 150.0, 'confidence': 0.75
        }
        result = self.trader.execute_signal(signal)
        assert result is False

    def test_daily_loss_limit_midnight_reset(self):
        """Flag resets when _cb_date is a past date"""
        from datetime import date, timedelta
        self.trader._daily_loss_limit_reached = True
        self.trader._cb_date = date.today() - timedelta(days=1)
        # Calling with a DB that returns no loss should reset flag and return False
        with patch('sqlite3.connect') as mock_connect:
            mock_conn = MagicMock()
            mock_conn.__enter__ = lambda s: mock_conn
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=(0.0,))),
            MagicMock(fetchall=MagicMock(return_value=[])),
        ]
            mock_connect.return_value = mock_conn
            result = self.trader._check_daily_loss_limit()
        assert result is False
        assert self.trader._daily_loss_limit_reached is False



def run_paper_trading_integration_test():
    """Integration test for paper trading system"""
    print("Running paper trading integration test...")
    
    try:
        # Create temporary directory
        temp_dir = tempfile.mkdtemp()
        
        # Test position manager
        pm = PositionManager(base_dir=temp_dir)
        print("✅ PositionManager initialized")
        
        # Test opening and closing positions
        pm.open_position('AAPL', 'Test Strategy', 'long', 100, 150.0)
        pm.update_positions({'AAPL': 155.0})
        pm.close_position('AAPL', 'Test Strategy', 155.0)
        print("✅ Position lifecycle completed")
        
        # Test portfolio state
        state = pm.get_portfolio_state()
        assert state.realized_pnl == 500.0
        print(f"✅ Portfolio P&L: ${state.total_pnl:.2f}")
        
        # Test paper trader
        trader = PaperTrader(base_dir=temp_dir)
        print("✅ PaperTrader initialized")
        
        # Generate trading report
        report = trader.generate_trading_report()
        assert len(report) > 100
        print("✅ Trading report generated")
        
        # Cleanup
        import shutil
        shutil.rmtree(temp_dir)
        print("✅ Integration test PASSED")
        return True
        
    except Exception as e:
        print(f"❌ Integration test FAILED: {e}")
        return False


if __name__ == '__main__':
    # Run integration test when called directly
    success = run_paper_trading_integration_test()
    sys.exit(0 if success else 1)


def test_tournament_winner_score_threshold_is_separate_from_signal_confidence(tmp_path):
    """Low composite tournament scores should still qualify instead of forcing fallback params."""
    trader = PaperTrader(base_dir=str(tmp_path))
    db_path = trader.automation.data_dir / 'tournament_history.db'
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute('''CREATE TABLE tournament_sessions (
            session_id TEXT, start_time TEXT, status TEXT, winner TEXT, winner_avg_score REAL
        )''')
        conn.execute(
            "INSERT INTO tournament_sessions VALUES (?,?,?,?,?)",
            ('s1', datetime.now().isoformat(), 'completed', 'MACD Crossover', 0.02),
        )
    trader.config['min_strategy_confidence'] = 0.55
    trader.config['min_tournament_winner_score'] = 0.0

    assert trader.get_latest_tournament_winners(days=7) == [('MACD Crossover', 0.02)]


def test_tiny_tournament_winner_score_is_rejected_by_default(tmp_path):
    """Zero/tiny tournament scores should not trade just because they won."""
    trader = PaperTrader(base_dir=str(tmp_path))
    db_path = trader.automation.data_dir / 'tournament_history.db'
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute('''CREATE TABLE tournament_sessions (
            session_id TEXT, start_time TEXT, status TEXT, winner TEXT, winner_avg_score REAL
        )''')
        conn.execute(
            "INSERT INTO tournament_sessions VALUES (?,?,?,?,?)",
            ('s1', datetime.now().isoformat(), 'completed', 'MACD Crossover', 0.001),
        )

    assert trader.config['min_tournament_winner_score'] == 0.01
    assert trader.get_latest_tournament_winners(days=7) == []


def test_gap_up_at_capacity_closes_profitable_position(tmp_path):
    """A large profitable gap-up should trigger a capacity-freeing paper close."""
    import pandas as pd
    trader = PaperTrader(base_dir=str(tmp_path))
    trader.config['max_concurrent_trades'] = 1
    trader.position_manager.open_position('AAPL', 'MACD Crossover', 'long', 1.0, 100.0)

    quote = Mock()
    quote.last = 110.0
    trader.alpaca.get_quote = Mock(return_value=quote)
    trader.alpaca.get_historical_data = Mock(return_value=pd.DataFrame({'close': [100.0, 104.0]}))
    trader._execute_sell_order = Mock(return_value=True)

    trader._check_premarket_gaps()

    trader._execute_sell_order.assert_called_once_with('AAPL', 'MACD Crossover', 110.0, force=True)


def test_trade_level_feedback_blocks_negative_symbol_strategy(tmp_path):
    tracker = FeedbackTracker(base_dir=str(tmp_path))
    params = {'oversold': 30, 'overbought': 65, 'position_size': 0.15}

    for idx in range(5):
        tracker.record_closed_trade(
            params=params,
            symbol='META',
            strategy='Confluence',
            side='long',
            entry_price=100.0,
            exit_price=96.0,
            quantity=1.0,
            pnl_dollars=-4.0,
            exit_reason='STOP_LOSS',
            source='test',
            source_id=str(idx),
        )

    adjustment = tracker.get_execution_adjustment('META', 'Confluence')

    assert adjustment['tradeable'] is False
    assert adjustment['confidence_multiplier'] == 0.0
    assert adjustment['size_multiplier'] == 0.0
    assert adjustment['sample_size'] == 5


def test_paper_trader_applies_learning_size_multiplier_to_buys(tmp_path):
    trader = PaperTrader(base_dir=str(tmp_path))
    trader.feedback = Mock()
    trader.feedback.get_execution_adjustment.return_value = {
        'tradeable': True,
        'confidence_multiplier': 1.0,
        'size_multiplier': 0.5,
        'reason': 'reduced by learned weak edge',
    }
    trader.position_manager.calculate_position_size = Mock(return_value=10.0)
    trader._execute_buy_order = Mock(return_value=True)

    ok = trader.execute_signal({
        'symbol': 'SPY',
        'strategy': 'MACD Crossover',
        'action': 'buy',
        'side': 'long',
        'current_price': 100.0,
        'confidence': 0.8,
    })

    assert ok is True
    trader._execute_buy_order.assert_called_once()
    assert trader._execute_buy_order.call_args.args[3] == 5.0


def test_learning_block_does_not_block_sell_signal(tmp_path):
    trader = PaperTrader(base_dir=str(tmp_path))
    trader.feedback = Mock()
    trader.feedback.get_execution_adjustment.return_value = {
        'tradeable': False,
        'confidence_multiplier': 0.0,
        'size_multiplier': 0.0,
        'reason': 'blocked by learned negative edge',
    }
    trader.position_manager.calculate_position_size = Mock(return_value=1.0)
    trader._execute_sell_order = Mock(return_value=True)

    ok = trader.execute_signal({
        'symbol': 'SPY',
        'strategy': 'MACD Crossover',
        'action': 'sell',
        'side': 'long',
        'current_price': 100.0,
        'confidence': 0.8,
    })

    assert ok is True
    trader._execute_sell_order.assert_called_once_with('SPY', 'MACD Crossover', 100.0)
