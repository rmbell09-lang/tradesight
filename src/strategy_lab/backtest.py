"""
TradeSight Backtesting Engine

Core backtesting engine that can test any strategy against historical data
and provide comprehensive performance metrics.
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Callable, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import json


@dataclass
class Trade:
    """Represents a single trade"""
    entry_time: datetime
    exit_time: Optional[datetime]
    entry_price: float
    exit_price: Optional[float]
    direction: str  # 'long' or 'short'
    size: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    status: str  # 'open', 'closed', 'stopped'
    pnl: float = 0.0
    pnl_pct: float = 0.0


@dataclass
class BacktestMetrics:
    """Comprehensive backtest performance metrics"""
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe_ratio: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    total_fees: float
    start_date: datetime
    end_date: datetime
    duration_days: int
    roi_annualized: float


class BacktestEngine:
    """
    Backtesting engine that can test any strategy against historical data.
    
    Strategy functions should accept (data, current_index, positions) and return signals:
    {'action': 'buy'/'sell'/'hold', 'size': 1.0, 'stop_loss': price, 'take_profit': price}
    """
    
    def __init__(self, initial_balance: float = 10000.0, fee_rate: float = 0.001):
        self.initial_balance = initial_balance
        self.fee_rate = fee_rate  # 0.1% per trade
        self.reset()
    
    def reset(self):
        """Reset backtester state for new test"""
        self.balance = self.initial_balance
        self.positions = []
        self.closed_trades = []
        self.equity_curve = []
        self.peak_equity = self.initial_balance
        self.max_drawdown = 0.0
    
    def run_backtest(self, 
                     data: pd.DataFrame, 
                     strategy_func: Callable,
                     asset_name: str = "Unknown") -> Dict[str, Any]:
        """
        Run backtest on historical data using provided strategy function.
        
        Args:
            data: DataFrame with OHLCV data (columns: open, high, low, close, volume)
            strategy_func: Function that generates trading signals
            asset_name: Name of the asset being tested
            
        Returns:
            Dict with metrics and trade history
        """
        self.reset()
        
        if len(data) < 50:
            raise ValueError(f"Need at least 50 data points for backtesting, got {len(data)}")
        
        # Ensure data has required columns
        required_cols = ['open', 'high', 'low', 'close', 'volume']
        missing_cols = [col for col in required_cols if col not in data.columns]
        if missing_cols:
            raise ValueError(f"Missing required columns: {missing_cols}")
        
        # Add technical indicators to data for strategy use
        data = self._add_indicators(data)
        
        # Run through each bar
        for i in range(50, len(data)):  # Start after enough data for indicators
            current_bar = data.iloc[i]
            
            # Update open positions first
            self._update_positions(current_bar)
            
            # Get strategy signal
            try:
                signal = strategy_func(data, i, self.positions)
                if signal and isinstance(signal, dict):
                    self._execute_signal(signal, current_bar, i)
            except Exception as e:
                # Strategy function failed, skip this bar
                continue
            
            # Track equity curve
            current_equity = self.balance + sum(pos.get('unrealized_pnl', 0) for pos in self.positions)
            self.equity_curve.append({
                'timestamp': current_bar.name,
                'equity': current_equity,
                'balance': self.balance,
                'drawdown': (self.peak_equity - current_equity) / self.peak_equity if self.peak_equity > 0 else 0
            })
            
            # Update peak and drawdown
            if current_equity > self.peak_equity:
                self.peak_equity = current_equity
            
            current_drawdown = (self.peak_equity - current_equity) / self.peak_equity
            if current_drawdown > self.max_drawdown:
                self.max_drawdown = current_drawdown
        
        # Close any remaining open positions
        if self.positions:
            final_bar = data.iloc[-1]
            for pos in self.positions[:]:  # Copy list to avoid modification during iteration
                self._close_position(pos, final_bar['close'], len(data) - 1, "end_of_data")
        
        # Calculate metrics
        metrics = self._calculate_metrics(data, asset_name)
        
        return {
            'metrics': asdict(metrics),
            'trades': [asdict(trade) for trade in self.closed_trades],
            'equity_curve': self.equity_curve,
            'final_balance': self.balance,
            'asset_name': asset_name
        }
    
    def _add_indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        """Add basic technical indicators for strategy use"""
        df = data.copy()
        
        # Simple Moving Averages
        df['sma_20'] = df['close'].rolling(20).mean()
        df['sma_50'] = df['close'].rolling(50).mean()
        df['sma_100'] = df['close'].rolling(100).mean()
        
        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # MACD
        exp1 = df['close'].ewm(span=12).mean()
        exp2 = df['close'].ewm(span=26).mean()
        df['macd'] = exp1 - exp2
        df['macd_signal'] = df['macd'].ewm(span=9).mean()
        df['macd_histogram'] = df['macd'] - df['macd_signal']
        
        # Bollinger Bands
        df['bb_middle'] = df['close'].rolling(20).mean()
        bb_std = df['close'].rolling(20).std()
        df['bb_upper'] = df['bb_middle'] + (2 * bb_std)
        df['bb_lower'] = df['bb_middle'] - (2 * bb_std)
        
        return df
    
    def _execute_signal(self, signal: Dict, bar: pd.Series, bar_index: int):
        """Execute trading signal"""
        action = signal.get('action', 'hold').lower()
        
        if action == 'buy' and len(self.positions) == 0:  # Only allow one position
            self._open_position('long', signal, bar, bar_index)
        elif action == 'sell' and len(self.positions) == 0:
            self._open_position('short', signal, bar, bar_index)
        elif action in ['close', 'exit'] and self.positions:
            for pos in self.positions[:]:
                self._close_position(pos, bar['close'], bar_index, "signal")
    
    def _open_position(self, direction: str, signal: Dict, bar: pd.Series, bar_index: int):
        """Open new position"""
        size = signal.get('size', 1.0)
        entry_price = bar['close']
        
        # Calculate position value
        position_value = self.balance * size
        fee = position_value * self.fee_rate
        
        if position_value + fee > self.balance:
            return  # Not enough balance
        
        # Deduct fee
        self.balance -= fee
        
        position = {
            'direction': direction,
            'entry_price': entry_price,
            'entry_time': bar.name,
            'entry_index': bar_index,
            'size': size,
            'position_value': position_value,
            'stop_loss': signal.get('stop_loss'),
            'take_profit': signal.get('take_profit'),
            'unrealized_pnl': 0.0
        }
        
        self.positions.append(position)
    
    def _update_positions(self, bar: pd.Series):
        """Update open positions and check for stop loss/take profit"""
        current_price = bar['close']
        
        for pos in self.positions[:]:  # Copy list to avoid modification during iteration
            # Calculate unrealized PnL
            if pos['direction'] == 'long':
                pnl = (current_price - pos['entry_price']) * pos['position_value'] / pos['entry_price']
            else:  # short
                pnl = (pos['entry_price'] - current_price) * pos['position_value'] / pos['entry_price']
            
            pos['unrealized_pnl'] = pnl
            
            # Check stop loss
            if pos['stop_loss'] is not None:
                if ((pos['direction'] == 'long' and current_price <= pos['stop_loss']) or
                    (pos['direction'] == 'short' and current_price >= pos['stop_loss'])):
                    self._close_position(pos, pos['stop_loss'], -1, "stop_loss")
                    continue
            
            # Check take profit
            if pos['take_profit'] is not None:
                if ((pos['direction'] == 'long' and current_price >= pos['take_profit']) or
                    (pos['direction'] == 'short' and current_price <= pos['take_profit'])):
                    self._close_position(pos, pos['take_profit'], -1, "take_profit")
                    continue
    
    def _close_position(self, position: Dict, exit_price: float, bar_index: int, reason: str):
        """Close position and record trade"""
        if position not in self.positions:
            return
        
        # Calculate final PnL
        if position['direction'] == 'long':
            pnl = (exit_price - position['entry_price']) * position['position_value'] / position['entry_price']
        else:  # short
            pnl = (position['entry_price'] - exit_price) * position['position_value'] / position['entry_price']
        
        # Deduct exit fee
        exit_fee = position['position_value'] * self.fee_rate
        pnl -= exit_fee
        
        # Update balance
        self.balance += position['position_value'] + pnl
        
        # Create trade record
        trade = Trade(
            entry_time=position['entry_time'],
            exit_time=datetime.now() if bar_index == -1 else None,  # Will be set properly in actual implementation
            entry_price=position['entry_price'],
            exit_price=exit_price,
            direction=position['direction'],
            size=position['size'],
            stop_loss=position.get('stop_loss'),
            take_profit=position.get('take_profit'),
            status='closed',
            pnl=pnl,
            pnl_pct=(pnl / position['position_value']) * 100
        )
        
        self.closed_trades.append(trade)
        self.positions.remove(position)
    
    def _calculate_metrics(self, data: pd.DataFrame, asset_name: str) -> BacktestMetrics:
        """Calculate comprehensive backtest metrics"""
        if not self.closed_trades:
            # No trades, return zero metrics
            return BacktestMetrics(
                total_trades=0, winning_trades=0, losing_trades=0, win_rate=0.0,
                total_pnl=0.0, total_pnl_pct=0.0, max_drawdown=0.0, max_drawdown_pct=0.0,
                sharpe_ratio=0.0, profit_factor=0.0, avg_win=0.0, avg_loss=0.0,
                max_consecutive_wins=0, max_consecutive_losses=0, total_fees=0.0,
                start_date=data.index[0], end_date=data.index[-1],
                duration_days=(data.index[-1] - data.index[0]).days,
                roi_annualized=0.0
            )
        
        # Basic metrics
        total_trades = len(self.closed_trades)
        winning_trades = sum(1 for t in self.closed_trades if t.pnl > 0)
        losing_trades = total_trades - winning_trades
        win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0
        
        # PnL metrics
        total_pnl = sum(t.pnl for t in self.closed_trades)
        total_pnl_pct = ((self.balance - self.initial_balance) / self.initial_balance) * 100
        
        # Win/Loss metrics
        wins = [t.pnl for t in self.closed_trades if t.pnl > 0]
        losses = [t.pnl for t in self.closed_trades if t.pnl < 0]
        
        avg_win = np.mean(wins) if wins else 0
        avg_loss = np.mean(losses) if losses else 0
        profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf')
        
        # Consecutive wins/losses
        consecutive_wins = 0
        consecutive_losses = 0
        max_consecutive_wins = 0
        max_consecutive_losses = 0
        
        for trade in self.closed_trades:
            if trade.pnl > 0:
                consecutive_wins += 1
                consecutive_losses = 0
                max_consecutive_wins = max(max_consecutive_wins, consecutive_wins)
            else:
                consecutive_losses += 1
                consecutive_wins = 0
                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
        
        # Sharpe ratio (simplified)
        if self.equity_curve:
            returns = []
            for i in range(1, len(self.equity_curve)):
                prev_equity = self.equity_curve[i-1]['equity']
                curr_equity = self.equity_curve[i]['equity']
                returns.append((curr_equity - prev_equity) / prev_equity if prev_equity > 0 else 0)
            
            if returns and np.std(returns) > 0:
                sharpe_ratio = np.mean(returns) / np.std(returns) * np.sqrt(252)  # Annualized
            else:
                sharpe_ratio = 0.0
        else:
            sharpe_ratio = 0.0
        
        # Time metrics
        start_date = data.index[0] if not data.empty else datetime.now()
        end_date = data.index[-1] if not data.empty else datetime.now()
        duration_days = max((end_date - start_date).days, 1)
        
        # Annualized ROI
        roi_annualized = (total_pnl_pct / 100) * (365.0 / duration_days) if duration_days > 0 else 0.0
        
        # Fees (approximate)
        total_fees = total_trades * 2 * self.initial_balance * self.fee_rate  # Entry + exit fees
        
        return BacktestMetrics(
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            total_pnl=total_pnl,
            total_pnl_pct=total_pnl_pct,
            max_drawdown=self.max_drawdown * 100,  # Convert to percentage
            max_drawdown_pct=self.max_drawdown * 100,
            sharpe_ratio=sharpe_ratio,
            profit_factor=profit_factor,
            avg_win=avg_win,
            avg_loss=avg_loss,
            max_consecutive_wins=max_consecutive_wins,
            max_consecutive_losses=max_consecutive_losses,
            total_fees=total_fees,
            start_date=start_date,
            end_date=end_date,
            duration_days=duration_days,
            roi_annualized=roi_annualized
        )


# Example strategy functions for testing
def simple_ma_crossover(data: pd.DataFrame, index: int, positions: List) -> Optional[Dict]:
    """Simple moving average crossover strategy"""
    if index < 50:  # Not enough data
        return None
    
    current = data.iloc[index]
    prev = data.iloc[index - 1]
    
    # Buy signal: SMA20 crosses above SMA50
    if (current['sma_20'] > current['sma_50'] and 
        prev['sma_20'] <= prev['sma_50'] and 
        not positions):
        return {
            'action': 'buy',
            'size': 0.8,  # 80% of balance
            'stop_loss': current['close'] * 0.95,  # 5% stop loss
            'take_profit': current['close'] * 1.10   # 10% take profit
        }
    
    # Sell signal: SMA20 crosses below SMA50
    if (current['sma_20'] < current['sma_50'] and 
        prev['sma_20'] >= prev['sma_50'] and 
        positions):
        return {'action': 'close'}
    
    return None


def rsi_mean_reversion(data: pd.DataFrame, index: int, positions: List) -> Optional[Dict]:
    """RSI mean reversion strategy"""
    if index < 50:
        return None
    
    current = data.iloc[index]
    
    # Buy signal: RSI oversold
    if current['rsi'] < 30 and not positions:
        return {
            'action': 'buy',
            'size': 0.6,
            'stop_loss': current['close'] * 0.93,
            'take_profit': current['close'] * 1.08
        }
    
    # Sell signal: RSI overbought
    if current['rsi'] > 70 and positions:
        return {'action': 'close'}
    
    return None
