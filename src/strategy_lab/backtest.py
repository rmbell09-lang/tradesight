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

from .slippage import SlippageModel


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
    
    def __init__(self, initial_balance: float = 500.0, fee_rate: float = 0.001,
                 slippage_pct: float = 0.0005,
                 slippage_model: SlippageModel = None):
        self.initial_balance = initial_balance
        self.fee_rate = fee_rate  # 0.1% per trade
        self.slippage_pct = slippage_pct  # 0.05% default slippage per trade
        self.slippage_model = slippage_model  # Advanced model (overrides slippage_pct when set)
        self.reset()
    
    def reset(self):
        """Reset backtester state for new test"""
        self.balance = self.initial_balance
        self.positions = []
        self.closed_trades = []
        self.equity_curve = []
        self.peak_equity = self.initial_balance
        self.max_drawdown = 0.0
        self._last_atr = 0.0
        self._last_avg_volume = 0.0
    
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
        # NOTE: signal generated at bar i is executed at bar i+1 open (no lookahead bias)
        pending_signal = None
        for i in range(50, len(data)):  # Start after enough data for indicators
            current_bar = data.iloc[i]
            
            # Execute PREVIOUS bar's signal at today's OPEN (realistic entry)
            if pending_signal is not None:
                entry_bar = current_bar.copy()
                entry_bar['close'] = current_bar['open']  # enter at open, not close
                self._execute_signal(pending_signal, entry_bar, i)
                pending_signal = None
            
            # Capture market context for slippage model
            if self.slippage_model is not None:
                self._last_atr = current_bar.get('atr_14', 0.0) if hasattr(current_bar, 'get') else getattr(current_bar, 'atr_14', 0.0)
                self._last_avg_volume = current_bar.get('volume_sma_20', 0.0) if hasattr(current_bar, 'get') else getattr(current_bar, 'volume_sma_20', 0.0)
                if self._last_atr != self._last_atr:  # NaN check
                    self._last_atr = 0.0
                if self._last_avg_volume != self._last_avg_volume:
                    self._last_avg_volume = 0.0

            # Update open positions with current bar (checks SL/TP at close)
            self._update_positions(current_bar)
            
            # Get strategy signal — will be executed at NEXT bar's open
            try:
                signal = strategy_func(data, i, self.positions)
                if signal and isinstance(signal, dict):
                    pending_signal = signal
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
        
        result = {
            'metrics': asdict(metrics),
            'trades': [asdict(trade) for trade in self.closed_trades],
            'equity_curve': self.equity_curve,
            'final_balance': self.balance,
            'asset_name': asset_name
        }
        if self.slippage_model is not None:
            result['slippage_model'] = self.slippage_model.summary()
        return result
    
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
        
        # ATR (Average True Range) — volatility measure for dynamic stops
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift(1)).abs()
        low_close = (df['low'] - df['close'].shift(1)).abs()
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['atr_14'] = true_range.rolling(14).mean()
        
        # Volume SMA for volume confirmation
        df['volume_sma_20'] = df['volume'].rolling(20).mean()
        
        # VWAP (rolling 20-bar volume-weighted average price) for VWAP Reversion strategy
        typical_price = (df['high'] + df['low'] + df['close']) / 3
        cumvol = df['volume'].rolling(20).sum()
        cumtp = (typical_price * df['volume']).rolling(20).sum()
        df['vwap_20'] = cumtp / cumvol
        
        # Opening range (5-bar high/low) for ORB strategy
        df['range_high_5'] = df['high'].rolling(5).max()
        df['range_low_5'] = df['low'].rolling(5).min()
        
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
        """Open new position with slippage modeling"""
        size = signal.get('size', 1.0)
        raw_price = bar['close']
        # Apply slippage: worse fill for the trader
        if self.slippage_model is not None:
            # Use advanced model with volume + volatility context
            atr = self._last_atr or 0.0
            avg_vol = self._last_avg_volume or 1_000_000.0
            order_shares = (self.initial_balance * size) / raw_price if raw_price > 0 else 0
            entry_price = self.slippage_model.apply(
                raw_price, direction,
                order_shares=order_shares,
                avg_daily_volume=avg_vol,
                atr=atr)
        else:
            if direction == 'long':
                entry_price = raw_price * (1 + self.slippage_pct)
            else:
                entry_price = raw_price * (1 - self.slippage_pct)
        
        # Fixed dollar position sizing based on INITIAL balance, not current balance.
        # Using self.balance * size compounds gains exponentially and inflates backtest PnL.
        # Real traders risk a fixed % of starting capital (or use Kelly criterion), not
        # a reinvested fraction of every prior gain.
        position_value = self.initial_balance * size
        fee = position_value * self.fee_rate
        total_cost = position_value + fee
        
        if total_cost > self.balance:
            return  # Not enough balance (account down, can't afford full position)
        
        # Deduct full position cost (capital tied up) + entry fee
        # On close, position_value is returned plus/minus P&L minus exit fee.
        self.balance -= total_cost
        
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
        """Close position and record trade with slippage"""
        if position not in self.positions:
            return
        
        # Apply slippage to exit: worse fill for the trader
        if self.slippage_model is not None:
            atr = self._last_atr or 0.0
            avg_vol = self._last_avg_volume or 1_000_000.0
            order_shares = position['position_value'] / exit_price if exit_price > 0 else 0
            close_dir = 'sell' if position['direction'] == 'long' else 'buy'
            exit_price = self.slippage_model.apply(
                exit_price, close_dir,
                order_shares=order_shares,
                avg_daily_volume=avg_vol,
                atr=atr)
        else:
            if position['direction'] == 'long':
                exit_price = exit_price * (1 - self.slippage_pct)
            else:
                exit_price = exit_price * (1 + self.slippage_pct)
        
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
                # Infer bar frequency from DatetimeIndex for correct annualization
                if not data.empty and len(data.index) > 1 and hasattr(data.index, 'to_series'):
                    avg_delta = (data.index[-1] - data.index[0]) / (len(data.index) - 1)
                    bars_per_day = pd.Timedelta('1D') / avg_delta if avg_delta.total_seconds() > 0 else 1.0
                    annualize_factor = np.sqrt(bars_per_day * 252)
                else:
                    annualize_factor = np.sqrt(252)
                sharpe_ratio = np.mean(returns) / np.std(returns) * annualize_factor  # Annualized
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
    """RSI mean reversion strategy — default thresholds 30/70"""
    return _rsi_mean_reversion_impl(data, index, positions, oversold=30, overbought=70)


def make_rsi_strategy(oversold: int = 30, overbought: int = 70,
                      position_size: float = 0.6,
                      stop_loss_pct: float = 0.07,
                      take_profit_pct: float = 0.08):
    """
    Factory: create an RSI strategy with specific params.
    Used by tournament so champion params are evaluated consistently
    with the same thresholds the paper trader actually uses.
    """
    def _strategy(data: pd.DataFrame, index: int, positions: List) -> Optional[Dict]:
        return _rsi_mean_reversion_impl(
            data, index, positions,
            oversold=oversold, overbought=overbought,
            position_size=position_size,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct
        )
    _strategy.__name__ = f'rsi_mean_reversion_os{oversold}_ob{overbought}'
    return _strategy


def _rsi_mean_reversion_impl(data: pd.DataFrame, index: int, positions: List,
                              oversold: int = 30, overbought: int = 70,
                              position_size: float = 0.6,
                              stop_loss_pct: float = 0.07,
                              take_profit_pct: float = 0.08) -> Optional[Dict]:
    """Core RSI mean reversion logic — parameterised"""
    if index < 50:
        return None
    
    current = data.iloc[index]
    
    # Buy signal: RSI oversold
    if current['rsi'] < oversold and not positions:
        return {
            'action': 'buy',
            'size': position_size,
            'stop_loss': current['close'] * (1.0 - stop_loss_pct),
            'take_profit': current['close'] * (1.0 + take_profit_pct)
        }
    
    # Sell signal: RSI overbought
    if current['rsi'] > overbought and positions:
        return {'action': 'close'}
    
    return None


def vwap_reversion(data: pd.DataFrame, index: int, positions: List) -> Optional[Dict]:
    """VWAP Reversion strategy — buy below VWAP, sell above"""
    if index < 50:
        return None
    
    current = data.iloc[index]
    vwap = current.get('vwap_20')
    
    if vwap is None or pd.isna(vwap) or vwap <= 0:
        return None
    
    price = current['close']
    deviation = (price - vwap) / vwap
    
    # Buy when price drops 1%+ below VWAP
    if deviation < -0.01 and not positions:
        return {
            'action': 'buy',
            'size': 0.5,
            'stop_loss': price * 0.97,
            'take_profit': vwap  # Target: return to VWAP
        }
    
    # Close when price returns to VWAP
    if positions and abs(deviation) < 0.003:
        return {'action': 'close'}
    
    return None


def opening_range_breakout(data: pd.DataFrame, index: int, positions: List) -> Optional[Dict]:
    """Opening Range Breakout — buy breakout above range, sell breakdown"""
    if index < 50:
        return None
    
    current = data.iloc[index]
    prev = data.iloc[index - 1]
    range_high = current.get('range_high_5')
    range_low = current.get('range_low_5')
    
    if range_high is None or range_low is None or pd.isna(range_high) or pd.isna(range_low):
        return None
    
    price = current['close']
    range_size = range_high - range_low
    
    if range_size <= 0:
        return None
    
    # Breakout above range
    if price > range_high and prev['close'] <= range_high and not positions:
        return {
            'action': 'buy',
            'size': 0.5,
            'stop_loss': range_low,  # Stop at range low
            'take_profit': price + range_size  # Target: range extension
        }
    
    # Breakdown below range
    if price < range_low and prev['close'] >= range_low and not positions:
        return {
            'action': 'sell',
            'size': 0.5,
            'stop_loss': range_high,
            'take_profit': price - range_size
        }
    
    return None
