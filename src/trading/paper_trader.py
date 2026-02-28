#!/usr/bin/env python3
"""
TradeSight Paper Trading Orchestrator

Takes winning strategies from tournaments and executes them in live paper trading.
Integrates with Alpaca Markets for real market data and paper trading execution.
"""

import os
import sys
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import pandas as pd
import time

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from data.alpaca_client import AlpacaClient
from trading.position_manager import PositionManager, PortfolioState
from automation.strategy_automation import StrategyAutomation
from strategy_lab.tournament import get_builtin_strategies
from indicators.technical_indicators import TechnicalIndicators


class PaperTrader:
    """Orchestrates paper trading with tournament-winning strategies"""
    
    def __init__(self, base_dir: str = None, alpaca_api_key: str = None, 
                 alpaca_secret: str = None):
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).parent.parent
        self.data_dir = self.base_dir / 'data'
        self.logs_dir = self.base_dir / 'logs'
        
        # Ensure directories exist
        for dir_path in [self.data_dir, self.logs_dir]:
            dir_path.mkdir(exist_ok=True)
        
        # Initialize components
        self.position_manager = PositionManager(base_dir=self.base_dir)
        self.automation = StrategyAutomation(base_dir=self.base_dir)
        
        # Initialize Alpaca client (demo mode if no API keys)
        if alpaca_api_key and alpaca_secret:
            self.alpaca = AlpacaClient(api_key=alpaca_api_key, secret_key=alpaca_secret, paper=True)
        else:
            self.alpaca = AlpacaClient()  # Demo mode
        
        # Setup logging
        self._setup_logging()
        
        # Trading parameters
        self.config = {
            'trading_symbols': ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'NVDA', 'META', 'NFLX'],
            'min_strategy_confidence': 0.65,  # Minimum score to trade
            'max_concurrent_trades': 5,       # Max open positions at once
            'trade_frequency_hours': 4,       # Check for signals every 4 hours
            'position_hold_days': 5,          # Hold positions for 5 days max
            'rebalance_frequency_days': 7     # Rebalance weekly
        }
    
    def _setup_logging(self):
        """Setup logging for paper trading"""
        log_file = self.logs_dir / f"paper_trader_{datetime.now().strftime('%Y%m%d')}.log"
        
        self.logger = logging.getLogger('PaperTrader')
        if not self.logger.handlers:  # Avoid duplicate handlers
            handler = logging.FileHandler(log_file)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)
    
    def get_latest_tournament_winners(self, days: int = 7) -> List[Tuple[str, float]]:
        """Get winning strategies from recent tournaments"""
        try:
            db_path = self.automation.data_dir / 'tournament_history.db'
            
            if not db_path.exists():
                self.logger.warning("No tournament history found")
                return []
            
            with sqlite3.connect(db_path) as conn:
                # Get recent tournament winners
                winners = conn.execute('''
                    SELECT winner, winner_avg_score, start_time
                    FROM tournament_sessions 
                    WHERE status = 'completed' 
                    AND date(start_time) >= date('now', '-{} days')
                    ORDER BY winner_avg_score DESC
                    LIMIT 10
                '''.format(days)).fetchall()
                
                if not winners:
                    self.logger.info("No recent tournament winners found")
                    return []
                
                # Filter by confidence threshold and deduplicate
                seen_strategies = set()
                qualified_winners = []
                
                for winner, score, start_time in winners:
                    if score >= self.config['min_strategy_confidence'] and winner not in seen_strategies:
                        qualified_winners.append((winner, score))
                        seen_strategies.add(winner)
                
                self.logger.info(f"Found {len(qualified_winners)} qualified strategies from recent tournaments")
                return qualified_winners
                
        except Exception as e:
            self.logger.error(f"Failed to get tournament winners: {e}")
            return []
    
    def generate_trading_signals(self, symbol: str, strategy_name: str) -> Optional[Dict]:
        """Generate trading signals for a symbol using a specific strategy"""
        try:
            # Get market data
            data = self.alpaca.get_historical_data(symbol, days=30)
            if data is None or len(data) < 200:
                self.logger.warning(f"Insufficient data for {symbol}")
                return None
            
            # Calculate technical indicators
            from indicators.technical_indicators import TechnicalIndicators; indicators = TechnicalIndicators(); indicators.data = data
            
            # Calculate all indicators
            indicators_data = indicators.calculate_all(data)
            
            # Apply strategy-specific logic
            signal = self._apply_strategy_logic(strategy_name, data, indicators_data)
            
            if signal:
                signal['symbol'] = symbol
                signal['strategy'] = strategy_name
                signal['timestamp'] = datetime.now().isoformat()
                signal['current_price'] = float(data.iloc[-1]['close'])
                
            return signal
            
        except Exception as e:
            self.logger.error(f"Failed to generate signal for {symbol} using {strategy_name}: {e}")
            return None
    
    def _apply_strategy_logic(self, strategy_name: str, data: pd.DataFrame, 
                            indicators: TechnicalIndicators) -> Optional[Dict]:
        """Apply specific strategy logic to generate buy/sell signals"""
        
        # Get the latest values
        current_price = float(data.iloc[-1]['close'])
        prev_price = float(data.iloc[-2]['close'])
        
        signal = None
        
        if strategy_name == 'MACD Crossover':
            macd_signal = indicators_data.get("signals", {}).get("macd", 0)
            if len(macd) >= 2:
                current_histogram = macd.iloc[-1]['histogram']
                prev_histogram = macd.iloc[-2]['histogram']
                
                # MACD bullish crossover
                if current_histogram > 0 and prev_histogram <= 0:
                    signal = {
                        'action': 'buy',
                        'side': 'long',
                        'confidence': 0.75,
                        'reason': 'MACD bullish crossover'
                    }
                # MACD bearish crossover
                elif current_histogram < 0 and prev_histogram >= 0:
                    signal = {
                        'action': 'sell',
                        'side': 'short',
                        'confidence': 0.70,
                        'reason': 'MACD bearish crossover'
                    }
        
        elif strategy_name == 'RSI Mean Reversion':
            rsi = indicators.calculate_rsi()
            if len(rsi) >= 1:
                current_rsi = rsi.iloc[-1]['rsi']
                
                # Oversold condition
                if current_rsi < 30:
                    signal = {
                        'action': 'buy',
                        'side': 'long',
                        'confidence': min(0.80, (30 - current_rsi) / 30 + 0.60),
                        'reason': f'RSI oversold: {current_rsi:.1f}'
                    }
                # Overbought condition
                elif current_rsi > 70:
                    signal = {
                        'action': 'sell',
                        'side': 'short',
                        'confidence': min(0.80, (current_rsi - 70) / 30 + 0.60),
                        'reason': f'RSI overbought: {current_rsi:.1f}'
                    }
        
        elif strategy_name == 'Bollinger Bounce':
            bb = indicators.calculate_bollinger_bands()
            if len(bb) >= 1:
                current_bb = bb.iloc[-1]
                
                # Price near lower band (buy signal)
                if current_price <= current_bb['lower_band'] * 1.02:
                    signal = {
                        'action': 'buy',
                        'side': 'long',
                        'confidence': 0.72,
                        'reason': 'Price near Bollinger lower band'
                    }
                # Price near upper band (sell signal)
                elif current_price >= current_bb['upper_band'] * 0.98:
                    signal = {
                        'action': 'sell',
                        'side': 'short',
                        'confidence': 0.68,
                        'reason': 'Price near Bollinger upper band'
                    }
        
        # Add more strategy implementations as needed...
        
        return signal
    
    def execute_signal(self, signal: Dict) -> bool:
        """Execute a trading signal"""
        try:
            symbol = signal['symbol']
            strategy = signal['strategy']
            action = signal['action']
            side = signal['side']
            current_price = signal['current_price']
            confidence = signal['confidence']
            
            # Calculate position size
            quantity = self.position_manager.calculate_position_size(symbol, strategy, current_price)
            
            if quantity <= 0:
                self.logger.info(f"No position size available for {symbol} {strategy}")
                return False
            
            # Execute the trade
            if action == 'buy':
                success = self._execute_buy_order(symbol, strategy, side, quantity, current_price)
            else:  # sell/close
                success = self._execute_sell_order(symbol, strategy, current_price)
            
            if success:
                self.logger.info(f"Executed {action} order: {quantity} {symbol} @ ${current_price:.2f} (Strategy: {strategy}, Confidence: {confidence:.2f})")
            
            return success
            
        except Exception as e:
            self.logger.error(f"Failed to execute signal: {e}")
            return False
    
    def _execute_buy_order(self, symbol: str, strategy: str, side: str, 
                          quantity: float, price: float) -> bool:
        """Execute a buy order"""
        try:
            # Place order with Alpaca (or simulate in demo mode)
            order_result = self.alpaca.place_paper_order(
                symbol=symbol,
                quantity=int(quantity),
                side='buy'
            )
            
            if order_result and order_result.get('status') in ['filled', 'accepted']:
                # Record position
                fill_price = order_result.get('filled_price', price)
                success = self.position_manager.open_position(
                    symbol=symbol,
                    strategy=strategy,
                    side=side,
                    quantity=quantity,
                    entry_price=fill_price
                )
                return success
            
            return False
            
        except Exception as e:
            self.logger.error(f"Failed to execute buy order: {e}")
            return False
    
    def _execute_sell_order(self, symbol: str, strategy: str, price: float) -> bool:
        """Execute a sell order (close position)"""
        try:
            # Get current position
            portfolio_state = self.position_manager.get_portfolio_state()
            
            # Check if we have an open position
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                position = conn.execute('''
                    SELECT quantity FROM positions 
                    WHERE symbol = ? AND strategy = ? AND status = 'open'
                    ORDER BY entry_time DESC LIMIT 1
                ''', (symbol, strategy)).fetchone()
                
                if not position:
                    self.logger.info(f"No open position to close for {symbol} {strategy}")
                    return False
                
                quantity = position[0]
            
            # Place sell order
            order_result = self.alpaca.place_paper_order(
                symbol=symbol,
                quantity=int(abs(quantity)),
                side='sell'
            )
            
            if order_result and order_result.get('status') in ['filled', 'accepted']:
                # Close position
                fill_price = order_result.get('filled_price', price)
                success = self.position_manager.close_position(
                    symbol=symbol,
                    strategy=strategy,
                    exit_price=fill_price
                )
                return success
            
            return False
            
        except Exception as e:
            self.logger.error(f"Failed to execute sell order: {e}")
            return False
    
    def scan_and_trade(self):
        """Main trading loop: scan for signals and execute trades"""
        try:
            self.logger.info("Starting trading scan...")
            
            # Get winning strategies
            winning_strategies = self.get_latest_tournament_winners()
            
            if not winning_strategies:
                self.logger.info("No winning strategies found")
                return
            
            # Check portfolio state
            portfolio_state = self.position_manager.get_portfolio_state()
            self.logger.info(f"Portfolio: ${portfolio_state.total_value:,.2f}, {portfolio_state.position_count} positions")
            
            if portfolio_state.position_count >= self.config['max_concurrent_trades']:
                self.logger.info("Maximum concurrent trades reached")
                return
            
            # Get current market prices
            price_data = {}
            for symbol in self.config['trading_symbols']:
                try:
                    quote = self.alpaca.get_latest_quote(symbol)
                    if quote:
                        price_data[symbol] = quote['price']
                except Exception as e:
                    self.logger.warning(f"Failed to get quote for {symbol}: {e}")
            
            # Update existing positions
            if price_data:
                self.position_manager.update_positions(price_data)
            
            # Generate and execute signals
            signals_executed = 0
            
            for strategy_name, score in winning_strategies:
                if signals_executed >= (self.config['max_concurrent_trades'] - portfolio_state.position_count):
                    break
                    
                for symbol in self.config['trading_symbols']:
                    # Check if we already have a position for this strategy+symbol
                    existing_positions = self._check_existing_position(symbol, strategy_name)
                    if existing_positions:
                        continue
                    
                    # Generate signal
                    signal = self.generate_trading_signals(symbol, strategy_name)
                    
                    if signal and signal.get('confidence', 0) >= self.config['min_strategy_confidence']:
                        success = self.execute_signal(signal)
                        if success:
                            signals_executed += 1
                            break  # Only one trade per strategy per scan
            
            # Save portfolio snapshot
            self.position_manager.save_portfolio_snapshot()
            
            self.logger.info(f"Trading scan completed. Executed {signals_executed} signals.")
            
        except Exception as e:
            self.logger.error(f"Trading scan failed: {e}")
    
    def _check_existing_position(self, symbol: str, strategy: str) -> bool:
        """Check if we already have a position for this symbol+strategy"""
        try:
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                position = conn.execute('''
                    SELECT id FROM positions 
                    WHERE symbol = ? AND strategy = ? AND status = 'open'
                    LIMIT 1
                ''', (symbol, strategy)).fetchone()
                
                return position is not None
                
        except Exception as e:
            self.logger.error(f"Failed to check existing position: {e}")
            return False
    
    def close_aged_positions(self):
        """Close positions that have been held too long"""
        try:
            cutoff_date = (datetime.now() - timedelta(days=self.config['position_hold_days'])).isoformat()
            
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                aged_positions = conn.execute('''
                    SELECT symbol, strategy FROM positions 
                    WHERE status = 'open' AND entry_time < ?
                ''', (cutoff_date,)).fetchall()
                
                for symbol, strategy in aged_positions:
                    try:
                        # Get current price
                        quote = self.alpaca.get_latest_quote(symbol)
                        if quote:
                            current_price = quote['price']
                            success = self._execute_sell_order(symbol, strategy, current_price)
                            if success:
                                self.logger.info(f"Closed aged position: {symbol} {strategy}")
                    except Exception as e:
                        self.logger.error(f"Failed to close aged position {symbol} {strategy}: {e}")
            
        except Exception as e:
            self.logger.error(f"Failed to close aged positions: {e}")
    
    def generate_trading_report(self) -> str:
        """Generate comprehensive trading performance report"""
        try:
            # Get portfolio state
            portfolio_state = self.position_manager.get_portfolio_state()
            
            # Get position manager report
            position_report = self.position_manager.get_performance_report(days=30)
            
            # Get recent tournament data
            winning_strategies = self.get_latest_tournament_winners(days=7)
            
            # Build comprehensive report
            report_lines = []
            report_lines.append("🤖 TradeSight Automated Trading Report")
            report_lines.append("=" * 60)
            report_lines.append(f"Report Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            report_lines.append("")
            
            # Portfolio summary
            report_lines.append("💼 Portfolio Summary")
            report_lines.append("-" * 20)
            report_lines.append(f"Total Value: ${portfolio_state.total_value:,.2f}")
            report_lines.append(f"Available Cash: ${portfolio_state.available_cash:,.2f}")
            report_lines.append(f"Positions Value: ${portfolio_state.total_positions_value:,.2f}")
            report_lines.append(f"Total P&L: ${portfolio_state.total_pnl:,.2f}")
            report_lines.append(f"Active Positions: {portfolio_state.position_count}")
            report_lines.append(f"Active Strategies: {', '.join(portfolio_state.strategies_active)}")
            report_lines.append("")
            
            # Tournament winners
            if winning_strategies:
                report_lines.append("🏆 Active Tournament Winners (Last 7 Days)")
                report_lines.append("-" * 40)
                for strategy, score in winning_strategies:
                    report_lines.append(f"{strategy}: {score:.3f} confidence")
                report_lines.append("")
            
            # Position performance
            report_lines.append(position_report)
            
            return "\n".join(report_lines)
            
        except Exception as e:
            self.logger.error(f"Failed to generate trading report: {e}")
            return "Failed to generate trading report"
    
    def run_trading_session(self):
        """Run a complete trading session"""
        try:
            self.logger.info("=== Starting TradeSight Paper Trading Session ===")
            
            # Main trading logic
            self.scan_and_trade()
            
            # Close aged positions
            self.close_aged_positions()
            
            # Generate report
            report = self.generate_trading_report()
            
            # Save report
            report_file = self.logs_dir / f"trading_report_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
            with open(report_file, 'w') as f:
                f.write(report)
            
            self.logger.info(f"Trading session completed. Report saved to {report_file}")
            return report
            
        except Exception as e:
            self.logger.error(f"Trading session failed: {e}")
            return f"Trading session failed: {e}"


def run_paper_trader_test():
    """Test paper trader functionality"""
    import tempfile
    
    # Create temporary directory
    temp_dir = tempfile.mkdtemp()
    
    try:
        # Initialize paper trader
        trader = PaperTrader(base_dir=temp_dir)
        
        print("✅ Paper trader initialized")
        
        # Test signal generation (demo mode)
        signal = trader.generate_trading_signals('AAPL', 'MACD Crossover')
        if signal:
            print(f"✅ Generated signal: {signal}")
        
        # Test trading session
        report = trader.run_trading_session()
        print("✅ Trading session completed")
        print("\nTrading Report:")
        print(report)
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
    
    finally:
        # Cleanup
        import shutil
        shutil.rmtree(temp_dir)
        print("✅ Test cleanup completed")


if __name__ == '__main__':
    run_paper_trader_test()
