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
        assert self.trader.base_dir == Path(self.temp_dir)
        assert self.trader.position_manager is not None
        assert self.trader.automation is not None
        assert self.trader.alpaca is not None  # Should be in demo mode
        assert len(self.trader.config['trading_symbols']) > 0
    
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
        """Test RSI trading signal generation"""
        # Mock market data - include all OHLCV columns
        import pandas as pd
        mock_data = pd.DataFrame({
            'open': list(range(200, 0, -1)),  # Declining open
            'high': list(range(201, 1, -1)),  # Declining high
            'low': list(range(199, -1, -1)),  # Declining low
            'close': list(range(200, 0, -1)),  # Declining close (oversold signal)
            'volume': [1000] * 200
        }, index=pd.date_range('2026-01-01', periods=200))
        
        self.trader.alpaca.get_historical_data = Mock(return_value=mock_data)
        
        # Mock RSI indicators with proper structure
        mock_indicator_instance = Mock()
        mock_indicators.return_value = mock_indicator_instance
        
        # RSI value of 20.0 is clearly oversold (below any reasonable threshold)
        mock_indicator_instance.calculate_all.return_value = {
            "indicators": {"rsi": 20.0},  # Strongly oversold RSI (well below typical 25-33 threshold)
            "signals": {}
        }
        
        # Generate signal
        signal = self.trader.generate_trading_signals('AAPL', 'RSI Mean Reversion')
        
        assert signal is not None
        assert signal['action'] == 'buy'
        assert signal['side'] == 'long'
        assert signal['confidence'] > 0.60
        assert 'RSI oversold' in signal['reason']
    
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
    
    def test_generate_trading_report(self):
        """Test trading report generation"""
        report = self.trader.generate_trading_report()
        
        assert "TradeSight Automated Trading Report" in report
        assert "Portfolio Summary" in report
        assert "Total Value:" in report
        assert "Available Cash:" in report


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
