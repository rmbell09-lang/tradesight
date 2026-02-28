"""
TradeSight Stock Scanner

High-level stock scanning interface that combines Alpaca data
with technical analysis and opportunity scoring.
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import json

from data.alpaca_client import AlpacaClient, StockQuote
from scanners.stock_opportunities import StockOpportunityScorer, OpportunityScore


@dataclass
class ScanResult:
    """Result from stock scanning"""
    scan_time: datetime
    total_scanned: int
    opportunities_found: int
    top_opportunities: List[OpportunityScore]
    scan_duration_seconds: float
    scan_parameters: Dict


class StockScanner:
    """
    Complete stock scanning system that combines:
    - Alpaca market data
    - Technical analysis via OpportunityScorer
    - Filtering and ranking
    - Multiple scan modes (quick, deep, custom)
    """
    
    def __init__(self, 
                 alpaca_api_key: str = None,
                 alpaca_secret: str = None,
                 paper_trading: bool = True):
        """
        Initialize stock scanner.
        
        Args:
            alpaca_api_key: Alpaca API key (None = demo mode)
            alpaca_secret: Alpaca secret key
            paper_trading: Use paper trading endpoints
        """
        self.alpaca = AlpacaClient(
            api_key=alpaca_api_key, 
            secret_key=alpaca_secret,
            paper=paper_trading
        )
        self.scorer = StockOpportunityScorer()
        self.last_scan_result = None
    
    def quick_scan(self, limit: int = 10) -> ScanResult:
        """
        Quick scan of top S&P 500 stocks.
        
        Args:
            limit: Max number of stocks to analyze
            
        Returns:
            ScanResult with top opportunities
        """
        start_time = datetime.now()
        
        # Get subset of S&P 500 for quick scan
        symbols = self.alpaca.SP500_SYMBOLS[:limit]
        
        return self._scan_symbols(
            symbols, 
            scan_type="quick",
            min_score=50.0,
            start_time=start_time
        )
    
    def deep_scan(self, min_volume: int = 1000000) -> ScanResult:
        """
        Deep scan of entire S&P 500 with volume filtering.
        
        Args:
            min_volume: Minimum average volume filter
            
        Returns:
            ScanResult with comprehensive analysis
        """
        start_time = datetime.now()
        
        # Use all S&P 500 symbols
        symbols = self.alpaca.SP500_SYMBOLS
        
        return self._scan_symbols(
            symbols,
            scan_type="deep", 
            min_score=40.0,
            min_volume=min_volume,
            start_time=start_time
        )
    
    def custom_scan(self, 
                   symbols: List[str],
                   min_score: float = 30.0,
                   min_volume: int = 500000) -> ScanResult:
        """
        Custom scan of specific symbols.
        
        Args:
            symbols: List of stock symbols to scan
            min_score: Minimum opportunity score
            min_volume: Minimum average volume
            
        Returns:
            ScanResult for custom symbol list
        """
        start_time = datetime.now()
        
        return self._scan_symbols(
            symbols,
            scan_type="custom",
            min_score=min_score,
            min_volume=min_volume, 
            start_time=start_time
        )
    
    def _scan_symbols(self,
                     symbols: List[str],
                     scan_type: str,
                     min_score: float,
                     min_volume: int = 0,
                     start_time: datetime = None) -> ScanResult:
        """Core scanning logic"""
        if start_time is None:
            start_time = datetime.now()
        
        print(f"📊 Starting {scan_type} scan of {len(symbols)} symbols...")
        
        opportunities = []
        
        for i, symbol in enumerate(symbols):
            try:
                print(f"  Analyzing {symbol} ({i+1}/{len(symbols)})...")
                
                # Get historical data
                data = self.alpaca.get_historical_data(symbol, days=200)
                
                if len(data) < 100:
                    print(f"  Skipping {symbol}: insufficient data")
                    continue
                
                # Volume filter
                if min_volume > 0:
                    avg_volume = data['volume'].tail(20).mean()
                    if avg_volume < min_volume:
                        print(f"  Skipping {symbol}: volume {avg_volume:,.0f} < {min_volume:,}")
                        continue
                
                # Score the opportunity
                opportunity = self.scorer.score_opportunity(data, symbol)
                
                # Score filter
                if opportunity.overall_score >= min_score:
                    opportunities.append(opportunity)
                    print(f"  ✅ {symbol}: Score {opportunity.overall_score:.1f} ({opportunity.confidence} confidence)")
                else:
                    print(f"  ❌ {symbol}: Score {opportunity.overall_score:.1f} (below {min_score})")
                
            except Exception as e:
                print(f"  Error analyzing {symbol}: {e}")
                continue
        
        # Sort by score descending
        opportunities.sort(key=lambda o: o.overall_score, reverse=True)
        
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        result = ScanResult(
            scan_time=start_time,
            total_scanned=len(symbols),
            opportunities_found=len(opportunities),
            top_opportunities=opportunities,
            scan_duration_seconds=duration,
            scan_parameters={
                'scan_type': scan_type,
                'min_score': min_score,
                'min_volume': min_volume,
                'symbols_requested': len(symbols)
            }
        )
        
        self.last_scan_result = result
        
        print(f"\n✅ Scan complete!")
        print(f"   Duration: {duration:.1f}s")
        print(f"   Analyzed: {len(symbols)} symbols")
        print(f"   Found: {len(opportunities)} opportunities")
        
        if opportunities:
            print(f"\n🏆 Top 3:")
            for i, opp in enumerate(opportunities[:3], 1):
                print(f"   {i}. {opp.symbol}: {opp.overall_score:.1f} ({opp.direction}, {opp.confidence})")
        
        return result
    
    def get_quote(self, symbol: str) -> Optional[StockQuote]:
        """Get real-time quote for a symbol"""
        return self.alpaca.get_quote(symbol)
    
    def get_historical_data(self, symbol: str, days: int = 100) -> pd.DataFrame:
        """Get historical OHLCV data"""
        return self.alpaca.get_historical_data(symbol, days)
    
    def place_paper_trade(self, symbol: str, quantity: int, side: str) -> Dict:
        """Place paper trade"""
        return self.alpaca.place_paper_trade(symbol, quantity, side)
    
    def get_positions(self):
        """Get current paper trading positions"""
        return self.alpaca.get_paper_positions()
    
    def export_scan_results(self, filepath: str) -> bool:
        """Export last scan results to JSON file"""
        if not self.last_scan_result:
            print("No scan results to export")
            return False
        
        try:
            # Convert opportunities to dicts
            opportunities_data = [asdict(opp) for opp in self.last_scan_result.top_opportunities]
            
            export_data = {
                'scan_time': self.last_scan_result.scan_time.isoformat(),
                'total_scanned': self.last_scan_result.total_scanned,
                'opportunities_found': self.last_scan_result.opportunities_found,
                'scan_duration_seconds': self.last_scan_result.scan_duration_seconds,
                'scan_parameters': self.last_scan_result.scan_parameters,
                'opportunities': opportunities_data
            }
            
            with open(filepath, 'w') as f:
                json.dump(export_data, f, indent=2, default=str)
            
            print(f"✅ Scan results exported to {filepath}")
            return True
            
        except Exception as e:
            print(f"Error exporting results: {e}")
            return False


def example_scan():
    """Example of using the stock scanner"""
    print("🚀 TradeSight Stock Scanner Example")
    print("=" * 50)
    
    # Initialize scanner (demo mode)
    scanner = StockScanner()
    
    # Quick scan
    print("\n1. Quick Scan (Top 10 S&P 500)")
    quick_result = scanner.quick_scan(limit=10)
    
    if quick_result.opportunities_found > 0:
        print(f"\nTop opportunity: {quick_result.top_opportunities[0].symbol}")
        print(f"Score: {quick_result.top_opportunities[0].overall_score}")
        print(f"Signals: {quick_result.top_opportunities[0].active_signals}")
    
    # Demo paper trade on top opportunity
    if quick_result.opportunities_found > 0:
        top_symbol = quick_result.top_opportunities[0].symbol
        print(f"\n2. Demo Paper Trade: {top_symbol}")
        
        trade_result = scanner.place_paper_trade(top_symbol, 10, 'buy')
        print(f"Trade result: {trade_result}")
    
    # Export results
    scanner.export_scan_results('scan_results.json')
    
    return scanner, quick_result


if __name__ == "__main__":
    scanner, result = example_scan()
