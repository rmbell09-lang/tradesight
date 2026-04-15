#!/usr/bin/env python3
"""
TradeSight Paper Trading Runner

Reads Alpaca keys from Keychain, initializes paper trader with real API,
runs a trading session, outputs report.

Usage:
  python3 run_paper_trader.py              # Run one trading session
  python3 run_paper_trader.py --report     # Just generate report (no trades)
  python3 run_paper_trader.py --status     # Show portfolio status
"""
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from utils.keychain import get_alpaca_api_key, get_alpaca_secret_key
from trading.paper_trader import PaperTrader
from datetime import datetime

# Weekday guard (defense-in-depth): exit immediately on weekends (markets closed)
if datetime.now().weekday() >= 5:
    print(f'Weekday guard: skipping run on {datetime.now().strftime("%A")} (market closed)')
    sys.exit(0)

def main():
    # Get Alpaca keys
    api_key = get_alpaca_api_key()
    secret_key = get_alpaca_secret_key()
    
    if not api_key or not secret_key:
        print("No Alpaca API keys found. Running in DEMO mode.")
        print("Store keys: security add-generic-password -s TradeSight-Alpaca-Key -a api-key -w YOUR_KEY -U")
        api_key = None
        secret_key = None
    else:
        mode_str = "paper trading"
        print(f"Alpaca keys loaded ({mode_str} mode)")
    
    base_dir = os.path.dirname(__file__)
    trader = PaperTrader(base_dir=base_dir, alpaca_api_key=api_key, alpaca_secret=secret_key)
    
    mode = sys.argv[1] if len(sys.argv) > 1 else "--trade"
    
    if mode == "--report":
        report = trader.generate_trading_report()
        print(report)
    elif mode == "--status":
        portfolio = trader.position_manager.get_portfolio_state()
        ts = datetime.now().strftime('%Y-%m-%d %H:%M')
        print(f"Portfolio Status ({ts})")
        print(f"   Total Value:  ${portfolio.total_value:,.2f}")
        print(f"   Cash:         ${portfolio.available_cash:,.2f}")
        print(f"   Positions:    ${portfolio.total_positions_value:,.2f}")
        print(f"   P&L:          ${portfolio.total_pnl:,.2f}")
        print(f"   Open:         {portfolio.position_count}")
        strats = ', '.join(portfolio.strategies_active) if portfolio.strategies_active else 'none'
        print(f"   Strategies:   {strats}")
    else:
        ts = datetime.now().strftime('%Y-%m-%d %H:%M')
        trade_mode = "LIVE PAPER" if api_key else "DEMO"
        print(f"Starting paper trading session ({ts})")
        print(f"   Mode: {trade_mode}")
        print()
        report = trader.run_trading_session()
        print(report)

if __name__ == "__main__":
    main()
