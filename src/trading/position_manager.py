#!/usr/bin/env python3
"""
TradeSight Position Manager

Tracks and manages paper trading positions, P&L, and portfolio state.
Provides position sizing, risk management, and performance analytics.
"""

import os
import sys
import json
import sqlite3

try:
    from trading.trade_logger import TradeLogger
    _TRADE_LOGGER_AVAILABLE = True
except ImportError:
    _TRADE_LOGGER_AVAILABLE = False
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
import pandas as pd

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@dataclass
class Position:
    """Individual position record"""
    symbol: str
    strategy: str
    side: str  # 'long' or 'short'
    quantity: float
    entry_price: float
    current_price: float
    entry_time: datetime
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    status: str = 'open'  # 'open', 'closed', 'partial'


@dataclass
class PortfolioState:
    """Current portfolio state"""
    total_value: float
    available_cash: float
    total_positions_value: float
    unrealized_pnl: float
    realized_pnl: float
    total_pnl: float
    position_count: int
    strategies_active: List[str]


class PositionManager:
    """Manages trading positions and portfolio state"""
    
    def __init__(self, base_dir: str = None):
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).parent.parent
        self.data_dir = self.base_dir / 'data'
        self.data_dir.mkdir(exist_ok=True)
        
        # Setup logging
        self.logger = logging.getLogger('PositionManager')

        # Trade logger for individual trade tracking
        if _TRADE_LOGGER_AVAILABLE:
            self.trade_logger = TradeLogger(base_dir=str(self.base_dir))
        else:
            self.trade_logger = None

        # Initialize database
        self._init_database()
        
        # Portfolio parameters
        self.config = {
            'initial_balance': 50000.0,  # Starting paper money
            'max_position_size': 0.10,   # 10% max per position
            'max_strategy_allocation': 0.25,  # 25% max per strategy
            'stop_loss_percent': 0.08,   # 8% stop loss
            'take_profit_percent': 0.15  # 15% take profit
        }
    
    def _init_database(self):
        """Initialize SQLite database for position tracking"""
        db_path = self.data_dir / 'positions.db'
        
        try:
            with sqlite3.connect(db_path) as conn:
                # Positions table
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS positions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        strategy TEXT NOT NULL,
                        side TEXT NOT NULL,
                        quantity REAL NOT NULL,
                        entry_price REAL NOT NULL,
                        current_price REAL NOT NULL,
                        entry_time TEXT NOT NULL,
                        exit_time TEXT,
                        exit_price REAL,
                        unrealized_pnl REAL DEFAULT 0.0,
                        realized_pnl REAL DEFAULT 0.0,
                        status TEXT DEFAULT 'open',
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Portfolio history table
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS portfolio_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        total_value REAL NOT NULL,
                        available_cash REAL NOT NULL,
                        total_positions_value REAL NOT NULL,
                        unrealized_pnl REAL NOT NULL,
                        realized_pnl REAL NOT NULL,
                        total_pnl REAL NOT NULL,
                        position_count INTEGER NOT NULL,
                        strategies_active TEXT NOT NULL
                    )
                ''')
                
                conn.commit()
                self.logger.info(f"Position database initialized at {db_path}")
                
        except Exception as e:
            self.logger.error(f"Failed to initialize position database: {e}")
            raise
    
    def open_position(self, symbol: str, strategy: str, side: str, 
                     quantity: float, entry_price: float) -> bool:
        """Open a new trading position"""
        try:
            position = Position(
                symbol=symbol,
                strategy=strategy,
                side=side,
                quantity=quantity,
                entry_price=entry_price,
                current_price=entry_price,
                entry_time=datetime.now()
            )
            
            db_path = self.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                conn.execute('''
                    INSERT INTO positions 
                    (symbol, strategy, side, quantity, entry_price, current_price, entry_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (symbol, strategy, side, quantity, entry_price, entry_price, 
                     position.entry_time.isoformat()))
                
                conn.commit()
                
            self.logger.info(f"Opened {side} position: {quantity} {symbol} @ ${entry_price:.2f} (Strategy: {strategy})")
            if self.trade_logger:
                self.trade_logger.log_open(symbol=symbol, strategy=strategy, side=side,
                                           quantity=quantity, entry_price=entry_price)
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to open position: {e}")
            return False
    
    def close_position(self, symbol: str, strategy: str, exit_price: float) -> bool:
        """Close an existing position"""
        try:
            db_path = self.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                # Find open position
                position_data = conn.execute('''
                    SELECT id, side, quantity, entry_price FROM positions 
                    WHERE symbol = ? AND strategy = ? AND status = 'open'
                    ORDER BY entry_time DESC LIMIT 1
                ''', (symbol, strategy)).fetchone()
                
                if not position_data:
                    self.logger.warning(f"No open position found for {symbol} {strategy}")
                    return False
                
                position_id, side, quantity, entry_price = position_data
                
                # Calculate realized P&L
                if side == 'long':
                    realized_pnl = (exit_price - entry_price) * quantity
                else:  # short
                    realized_pnl = (entry_price - exit_price) * quantity
                
                # Update position
                conn.execute('''
                    UPDATE positions 
                    SET exit_time = ?, exit_price = ?, realized_pnl = ?, 
                        status = 'closed', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (datetime.now().isoformat(), exit_price, realized_pnl, position_id))
                
                conn.commit()
                
            self.logger.info(f"Closed {side} position: {quantity} {symbol} @ ${exit_price:.2f}, P&L: ${realized_pnl:.2f}")
            if self.trade_logger:
                self.trade_logger.log_close(symbol=symbol, strategy=strategy,
                                            exit_price=exit_price, exit_reason='signal')
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to close position: {e}")
            return False
    
    def update_positions(self, price_data: Dict[str, float]):
        """Update current prices and unrealized P&L for open positions"""
        try:
            db_path = self.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                # Get all open positions
                positions = conn.execute('''
                    SELECT id, symbol, side, quantity, entry_price FROM positions 
                    WHERE status = 'open'
                ''').fetchall()
                
                for position_id, symbol, side, quantity, entry_price in positions:
                    if symbol in price_data:
                        current_price = price_data[symbol]
                        
                        # Calculate unrealized P&L
                        if side == 'long':
                            unrealized_pnl = (current_price - entry_price) * quantity
                        else:  # short
                            unrealized_pnl = (entry_price - current_price) * quantity
                        
                        # Update position
                        conn.execute('''
                            UPDATE positions 
                            SET current_price = ?, unrealized_pnl = ?, 
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        ''', (current_price, unrealized_pnl, position_id))
                
                conn.commit()
                
        except Exception as e:
            self.logger.error(f"Failed to update positions: {e}")
    
    def get_portfolio_state(self) -> PortfolioState:
        """Get current portfolio state and performance"""
        try:
            db_path = self.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                # Get position summary
                summary = conn.execute('''
                    SELECT 
                        COUNT(CASE WHEN status = "open" THEN 1 END) as position_count,
                        SUM(CASE WHEN status = 'open' THEN ABS(quantity * current_price) ELSE 0 END) as positions_value,
                        SUM(CASE WHEN status = 'open' THEN unrealized_pnl ELSE 0 END) as unrealized_pnl,
                        SUM(realized_pnl) as realized_pnl
                    FROM positions
                ''').fetchone()
                
                position_count, positions_value, unrealized_pnl, realized_pnl = summary or (0, 0, 0, 0)
                
                # Calculate portfolio metrics
                total_pnl = (unrealized_pnl or 0) + (realized_pnl or 0)
                total_value = self.config['initial_balance'] + total_pnl
                available_cash = total_value - (positions_value or 0)
                
                # Get active strategies
                strategies = conn.execute('''
                    SELECT DISTINCT strategy FROM positions 
                    WHERE status = 'open'
                ''').fetchall()
                
                strategies_active = [s[0] for s in strategies]
                
                return PortfolioState(
                    total_value=total_value,
                    available_cash=available_cash,
                    total_positions_value=positions_value or 0,
                    unrealized_pnl=unrealized_pnl or 0,
                    realized_pnl=realized_pnl or 0,
                    total_pnl=total_pnl,
                    position_count=position_count or 0,
                    strategies_active=strategies_active
                )
                
        except Exception as e:
            self.logger.error(f"Failed to get portfolio state: {e}")
            return PortfolioState(
                total_value=self.config['initial_balance'],
                available_cash=self.config['initial_balance'],
                total_positions_value=0,
                unrealized_pnl=0,
                realized_pnl=0,
                total_pnl=0,
                position_count=0,
                strategies_active=[]
            )
    
    def save_portfolio_snapshot(self):
        """Save current portfolio state to history"""
        try:
            state = self.get_portfolio_state()
            
            db_path = self.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                conn.execute('''
                    INSERT INTO portfolio_history 
                    (timestamp, total_value, available_cash, total_positions_value,
                     unrealized_pnl, realized_pnl, total_pnl, position_count, strategies_active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (datetime.now().isoformat(), state.total_value, state.available_cash,
                     state.total_positions_value, state.unrealized_pnl, state.realized_pnl,
                     state.total_pnl, state.position_count, json.dumps(state.strategies_active)))
                
                conn.commit()
                
        except Exception as e:
            self.logger.error(f"Failed to save portfolio snapshot: {e}")
    
    def calculate_position_size(self, symbol: str, strategy: str, price: float) -> float:
        """Calculate appropriate position size based on risk management"""
        try:
            state = self.get_portfolio_state()
            
            # Maximum position value
            max_position_value = state.total_value * self.config['max_position_size']
            
            # Check strategy allocation
            db_path = self.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                strategy_value = conn.execute('''
                    SELECT SUM(ABS(quantity * current_price)) 
                    FROM positions 
                    WHERE strategy = ? AND status = 'open'
                ''', (strategy,)).fetchone()[0] or 0
            
            # Available allocation for this strategy
            max_strategy_value = state.total_value * self.config['max_strategy_allocation']
            available_strategy_allocation = max_strategy_value - strategy_value
            
            # Position size is minimum of available allocation and max position size
            max_value = min(max_position_value, available_strategy_allocation)
            
            if max_value <= 0:
                return 0
            
            # Convert to shares
            max_shares = int(max_value / price)
            
            return max(0, max_shares)
            
        except Exception as e:
            self.logger.error(f"Failed to calculate position size: {e}")
            return 0
    
    def get_performance_report(self, days: int = 30) -> str:
        """Generate performance report"""
        try:
            state = self.get_portfolio_state()
            
            # Get historical performance
            db_path = self.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                # Recent closed positions
                recent_trades = conn.execute('''
                    SELECT symbol, strategy, side, quantity, entry_price, exit_price, 
                           realized_pnl, exit_time
                    FROM positions 
                    WHERE status = 'closed' 
                    AND date(exit_time) >= date('now', '-{} days')
                    ORDER BY exit_time DESC
                    LIMIT 20
                '''.format(days)).fetchall()
                
                # Strategy performance
                strategy_perf = conn.execute('''
                    SELECT strategy, 
                           COUNT(*) as trades,
                           SUM(realized_pnl) as total_pnl,
                           AVG(realized_pnl) as avg_pnl,
                           SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins
                    FROM positions 
                    WHERE status = 'closed'
                    AND date(exit_time) >= date('now', '-{} days')
                    GROUP BY strategy
                    ORDER BY total_pnl DESC
                '''.format(days)).fetchall()
            
            # Build report
            report_lines = []
            report_lines.append(f"📊 TradeSight Portfolio Performance Report")
            report_lines.append("=" * 50)
            report_lines.append(f"Portfolio Value: ${state.total_value:,.2f}")
            report_lines.append(f"Available Cash: ${state.available_cash:,.2f}")
            report_lines.append(f"Total P&L: ${state.total_pnl:,.2f} ({(state.total_pnl/self.config['initial_balance']*100):+.2f}%)")
            report_lines.append(f"Open Positions: {state.position_count}")
            
            if strategy_perf:
                report_lines.append(f"\n💰 Strategy Performance (Last {days} Days)")
                report_lines.append("-" * 40)
                for strategy, trades, total_pnl, avg_pnl, wins in strategy_perf:
                    win_rate = (wins / trades * 100) if trades > 0 else 0
                    report_lines.append(f"{strategy}: ${total_pnl:,.2f} ({trades} trades, {win_rate:.1f}% win rate)")
            
            if recent_trades:
                report_lines.append(f"\n📈 Recent Trades (Last {len(recent_trades)})")
                report_lines.append("-" * 40)
                for symbol, strategy, side, qty, entry, exit, pnl, exit_time in recent_trades[:10]:
                    pnl_str = f"${pnl:+.2f}"
                    report_lines.append(f"{symbol} {side}: {pnl_str} ({strategy})")
            
            return "\n".join(report_lines)
            
        except Exception as e:
            self.logger.error(f"Failed to generate performance report: {e}")
            return "Failed to generate performance report"


def run_position_manager_test():
    """Test position manager functionality"""
    import tempfile
    
    temp_dir = tempfile.mkdtemp()
    pm = PositionManager(base_dir=temp_dir)
    
    # Test opening positions
    pm.open_position('AAPL', 'MACD Crossover', 'long', 100, 150.0)
    pm.open_position('MSFT', 'RSI Mean Reversion', 'short', 50, 300.0)
    
    # Test price updates
    pm.update_positions({'AAPL': 155.0, 'MSFT': 295.0})
    
    # Test portfolio state
    state = pm.get_portfolio_state()
    print(f"Portfolio value: ${state.total_value:.2f}")
    print(f"Unrealized P&L: ${state.unrealized_pnl:.2f}")
    
    # Test closing position
    pm.close_position('AAPL', 'MACD Crossover', 155.0)
    
    # Test performance report
    report = pm.get_performance_report()
    print("\nPerformance Report:")
    print(report)
    
    # Cleanup
    import shutil
    shutil.rmtree(temp_dir)
    
    print("✅ Position manager test completed")


if __name__ == '__main__':
    run_position_manager_test()
