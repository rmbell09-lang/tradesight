"""
TradeSight Alpaca Integration

Client for Alpaca Markets API - stock market data and paper trading.
Provides real-time and historical OHLCV data for stocks.
"""

import os
import sys
import requests
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import time

# Ensure src/ is on path regardless of working directory
_src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

import config
from indicators.technical_indicators import TechnicalIndicators


@dataclass
class StockQuote:
    """Real-time stock quote"""
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    last: float
    volume: int
    change: float
    change_pct: float


@dataclass
class PaperPosition:
    """Paper trading position"""
    symbol: str
    quantity: int
    side: str  # 'long' or 'short'
    avg_entry_price: float
    current_price: float
    unrealized_pnl: float
    market_value: float


class AlpacaClient:
    """
    Alpaca Markets API client for stock data and paper trading.
    
    Features:
    - Historical OHLCV data for any stock
    - Real-time quotes and market data
    - Paper trading (simulated trades with real prices)
    - S&P 500 universe scanning
    - Integration with TechnicalIndicators
    """
    
    # S&P 500 symbols (subset for demo - in production would load from file/API)
    SP500_SYMBOLS = [
        'AAPL', 'MSFT', 'AMZN', 'GOOGL', 'GOOG', 'TSLA', 'BRK.B', 'UNH', 'JNJ', 'XOM',
        'JPM', 'V', 'PG', 'CVX', 'HD', 'MA', 'BAC', 'ABBV', 'PFE', 'KO',
        'PEP', 'AVGO', 'COST', 'DIS', 'WMT', 'TMO', 'VZ', 'ADBE', 'MRK', 'NFLX',
        'ABT', 'CRM', 'ACN', 'NKE', 'TXN', 'LIN', 'MDT', 'UPS', 'AMD', 'PM',
        'BMY', 'QCOM', 'HON', 'RTX', 'LLY', 'ORCL', 'IBM', 'BA', 'GE', 'MMM'
    ]
    
    def __init__(self, api_key: str = None, secret_key: str = None, paper: bool = True):
        """
        Initialize Alpaca client.
        
        Args:
            api_key: Alpaca API key (if None, uses demo mode)
            secret_key: Alpaca secret key
            paper: If True, uses paper trading endpoints
        """
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = paper
        self.demo_mode = api_key is None
        
        # API endpoints
        if self.paper:
            self.base_url = "https://paper-api.alpaca.markets"
            self.data_url = "https://data.alpaca.markets"
        else:
            self.base_url = "https://api.alpaca.markets"
            self.data_url = "https://data.alpaca.markets"
        
        self.headers = {}
        if not self.demo_mode:
            self.headers = {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
                "Content-Type": "application/json"
            }
        
        self.indicators = TechnicalIndicators()
        self._demo_fallback_count = 0
        
        # Pre-warm DNS for Alpaca endpoints to catch resolution issues early
        if not self.demo_mode:
            import socket
            for host in ['data.alpaca.markets', 'paper-api.alpaca.markets']:
                try:
                    socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
                except socket.gaierror as e:
                    import logging
                    logging.getLogger('AlpacaClient').warning(
                        f"DNS pre-warm failed for {host}: {e} — will retry on each request"
                    )
    
    def get_historical_data(self, 
                          symbol: str,
                          days: int = 100,
                          timeframe: str = '1Day') -> pd.DataFrame:
        """
        Get historical OHLCV data for a symbol.
        
        Args:
            symbol: Stock symbol (e.g., 'AAPL')
            days: Number of days of history
            timeframe: '1Min', '5Min', '15Min', '30Min', '1Hour', '1Day'
            
        Returns:
            DataFrame with OHLCV data
        """
        if self.demo_mode:
            df = self._generate_demo_data(symbol, days)
            df.attrs['data_source'] = 'demo_mode'
            return df
        
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        url = f"{self.data_url}/v2/stocks/{symbol}/bars"
        params = {
            'start': start_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'end': end_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'timeframe': timeframe,
            # For intraday bars, limit must account for bars-per-day (not days)
            'limit': min({
                '1Min': (days + 10) * 390,
                '5Min': (days + 10) * 78,
                '15Min': (days + 10) * 26,
                '30Min': (days + 10) * 13,
                '1Hour': (days + 10) * 7,
                '1Day': days + 50,
            }.get(timeframe, days + 50), 10000)  # Cap at Alpaca max
        }
        
        # Retry with backoff for transient DNS/network failures
        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=self.headers, params=params, timeout=15)
                break  # Success
            except requests.exceptions.ConnectionError as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait = (attempt + 1) * 5  # 5s, 10s backoff
                    import logging as _log
                    _log.getLogger('AlpacaClient').warning(
                        f"Connection error for {symbol} (attempt {attempt+1}/{max_retries}), retrying in {wait}s: {e}"
                    )
                    import time as _time
                    _time.sleep(wait)
                    continue
                else:
                    raise
        try:
            response  # Check we have a response
            
            if response.status_code == 200:
                data = response.json()
                bars = data.get('bars', [])
                
                if not bars:
                    raise ValueError(f"No data returned for {symbol}")
                
                df_data = []
                for bar in bars:
                    df_data.append({
                        'timestamp': pd.to_datetime(bar['t']),
                        'open': float(bar['o']),
                        'high': float(bar['h']),
                        'low': float(bar['l']),
                        'close': float(bar['c']),
                        'volume': int(bar['v'])
                    })
                
                df = pd.DataFrame(df_data)
                df.set_index('timestamp', inplace=True)
                df.columns = ['open', 'high', 'low', 'close', 'volume']
                
                return df.tail(days)  # Return exactly what was requested
                
            else:
                self._demo_fallback_count += 1
                import logging
                logging.getLogger('AlpacaClient').warning(
                    f"API Error {response.status_code} for {symbol} - falling back to DEMO data (fallback #{self._demo_fallback_count})"
                )
                df = self._generate_demo_data(symbol, days)
                df.attrs['data_source'] = 'demo_fallback'
                df.attrs['fallback_reason'] = f'API {response.status_code}'
                return df
                
        except Exception as e:
            self._demo_fallback_count += 1
            import logging
            logging.getLogger('AlpacaClient').warning(
                f"Error fetching {symbol}: {e} - falling back to DEMO data (fallback #{self._demo_fallback_count})"
            )
            df = self._generate_demo_data(symbol, days)
            df.attrs['data_source'] = 'demo_fallback'
            df.attrs['fallback_reason'] = str(e)
            return df
        except NameError:
            # response wasn't assigned (all retries failed)
            self._demo_fallback_count += 1
            import logging
            logging.getLogger('AlpacaClient').warning(
                f"All retries failed for {symbol}: {last_error} - falling back to DEMO data"
            )
            df = self._generate_demo_data(symbol, days)
            df.attrs['data_source'] = 'demo_fallback'
            df.attrs['fallback_reason'] = str(last_error)
            return df
    
    def get_quote(self, symbol: str) -> Optional[StockQuote]:
        """Get real-time quote for a symbol"""
        if self.demo_mode:
            return self._generate_demo_quote(symbol)
        
        url = f"{self.data_url}/v2/stocks/{symbol}/quotes/latest"
        
        try:
            response = requests.get(url, headers=self.headers, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                quote_data = data.get('quote', {})
                
                return StockQuote(
                    symbol=symbol,
                    timestamp=pd.to_datetime(quote_data.get('t')),
                    bid=float(quote_data.get('bp', 0)),
                    ask=float(quote_data.get('ap', 0)),
                    last=float(quote_data.get('ap', 0)),  # Use ask as last for demo
                    volume=int(quote_data.get('as', 0)),
                    change=0.0,  # Would calculate from previous close
                    change_pct=0.0
                )
            else:
                return self._generate_demo_quote(symbol)
                
        except Exception as e:
            print(f"Error getting quote for {symbol}: {e}")
            return self._generate_demo_quote(symbol)
    
    def scan_sp500(self, min_volume: int = 1000000) -> List[Dict]:
        """
        Scan S&P 500 stocks for opportunities using technical indicators.
        
        Args:
            min_volume: Minimum average volume filter
            
        Returns:
            List of opportunity dictionaries with scores
        """
        opportunities = []
        
        print(f"📊 Scanning S&P 500 stocks...")
        
        for i, symbol in enumerate(self.SP500_SYMBOLS):
            try:
                print(f"  Analyzing {symbol} ({i+1}/{len(self.SP500_SYMBOLS)})...")
                
                # Get historical data
                data = self.get_historical_data(symbol, days=100)
                
                if len(data) < 50:
                    continue
                
                # Check volume filter
                avg_volume = data['volume'].tail(20).mean()
                if avg_volume < min_volume:
                    continue
                
                # Calculate indicators
                indicators = self.indicators.calculate_all(data)
                
                # Simple scoring based on confluence
                confluence = indicators.get('confluence_score', 0)
                rsi = indicators['indicators'].get('rsi', 50)
                macd = indicators['indicators'].get('macd', 0)
                
                # Score calculation (simplified)
                score = confluence * 100
                
                if rsi < 30:  # Oversold
                    score += 20
                elif rsi > 70:  # Overbought (short opportunity)
                    score += 15
                
                if abs(macd) > 1:  # Strong MACD signal
                    score += 10
                
                opportunities.append({
                    'symbol': symbol,
                    'score': min(100, score),
                    'current_price': float(data['close'].iloc[-1]),
                    'volume': int(avg_volume),
                    'rsi': float(rsi),
                    'confluence': float(confluence),
                    'signals': self._extract_signals(indicators)
                })
                
            except Exception as e:
                print(f"  Error analyzing {symbol}: {e}")
                continue
        
        # Sort by score descending
        opportunities.sort(key=lambda x: x['score'], reverse=True)
        
        print(f"✅ Found {len(opportunities)} opportunities")
        return opportunities
    
    def place_paper_trade(self, 
                          symbol: str, 
                          quantity: int, 
                          side: str,
                          order_type: str = 'market') -> Dict:
        """
        Place a paper trade (simulated).
        
        Args:
            symbol: Stock symbol
            quantity: Number of shares
            side: 'buy' or 'sell'
            order_type: 'market' or 'limit'
            
        Returns:
            Order confirmation dictionary
        """
        if self.demo_mode:
            # Simulate paper trade
            quote = self.get_quote(symbol)
            fill_price = quote.last if quote else 100.0
            
            order_id = f"demo_{int(time.time())}"
            
            return {
                'order_id': order_id,
                'symbol': symbol,
                'quantity': quantity,
                'side': side,
                'status': 'filled',
                'fill_price': fill_price,
                'fill_time': datetime.now().isoformat(),
                'demo_mode': True
            }
        
        # Real Alpaca paper trading API
        url = f"{self.base_url}/v2/orders"
        
        order_data = {
            'symbol': symbol,
            'qty': quantity,
            'side': side,
            'type': order_type,
            'time_in_force': 'day'
        }
        
        try:
            response = requests.post(url, headers=self.headers, json=order_data, timeout=10)
            
            if response.status_code == 201:
                return response.json()
            else:
                print(f"Order failed: {response.status_code} - {response.text}")
                return {'error': response.text}
                
        except Exception as e:
            print(f"Error placing order: {e}")
            return {'error': str(e)}
    
    def get_paper_positions(self) -> List[PaperPosition]:
        """Get current paper trading positions"""
        if self.demo_mode:
            # Return demo positions
            return [
                PaperPosition(
                    symbol='AAPL',
                    quantity=10,
                    side='long',
                    avg_entry_price=150.0,
                    current_price=155.0,
                    unrealized_pnl=50.0,
                    market_value=1550.0
                )
            ]
        
        url = f"{self.base_url}/v2/positions"
        
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            
            if response.status_code == 200:
                positions_data = response.json()
                positions = []
                
                for pos in positions_data:
                    positions.append(PaperPosition(
                        symbol=pos['symbol'],
                        quantity=int(pos['qty']),
                        side='long' if float(pos['qty']) > 0 else 'short',
                        avg_entry_price=float(pos['avg_entry_price']),
                        current_price=float(pos['market_value']) / abs(float(pos['qty'])),
                        unrealized_pnl=float(pos['unrealized_pnl']),
                        market_value=float(pos['market_value'])
                    ))
                
                return positions
                
        except Exception as e:
            print(f"Error getting positions: {e}")
            return []
    
    def _generate_demo_data(self, symbol: str, days: int) -> pd.DataFrame:
        """Generate realistic demo OHLCV data"""
        np.random.seed((hash(symbol) + int(datetime.now().strftime("%Y%m%d"))) % 2147483647)  # Varies daily per symbol
        
        dates = pd.date_range(end=datetime.now(), periods=days, freq='D')
        
        # Base price varies by symbol
        base_prices = {
            'AAPL': 150, 'MSFT': 300, 'AMZN': 3000, 'GOOGL': 2500, 'TSLA': 200
        }
        base_price = base_prices.get(symbol, 100)
        
        # Generate price series with some trend + noise
        returns = np.random.normal(0.001, 0.02, days)
        prices = [base_price]
        for ret in returns[1:]:
            prices.append(prices[-1] * (1 + ret))
        
        # Create OHLCV
        data = []
        for i, (date, close) in enumerate(zip(dates, prices)):
            open_price = close + np.random.normal(0, close * 0.005)
            high = max(open_price, close) + np.random.uniform(0, close * 0.01)
            low = min(open_price, close) - np.random.uniform(0, close * 0.01)
            volume = int(np.random.uniform(1000000, 10000000))
            
            data.append({
                'open': round(open_price, 2),
                'high': round(high, 2),
                'low': round(low, 2),
                'close': round(close, 2),
                'volume': volume
            })
        
        df = pd.DataFrame(data, index=dates)
        return df
    
    def _generate_demo_quote(self, symbol: str) -> StockQuote:
        """Generate realistic demo quote"""
        # Get last price from demo data
        data = self._generate_demo_data(symbol, 1)
        last_price = float(data['close'].iloc[-1])
        
        return StockQuote(
            symbol=symbol,
            timestamp=datetime.now(),
            bid=round(last_price - 0.01, 2),
            ask=round(last_price + 0.01, 2),
            last=last_price,
            volume=int(np.random.uniform(100000, 1000000)),
            change=round(np.random.uniform(-5, 5), 2),
            change_pct=round(np.random.uniform(-3, 3), 2)
        )
    
    def _extract_signals(self, indicators: Dict) -> List[str]:
        """Extract trading signals from indicator data"""
        signals = []
        
        # Extract signals from indicators
        ind_data = indicators.get('indicators', {})
        
        rsi = ind_data.get('rsi', 50)
        if rsi < 30:
            signals.append('RSI_OVERSOLD')
        elif rsi > 70:
            signals.append('RSI_OVERBOUGHT')
        
        confluence = indicators.get('confluence_score', 0)
        if confluence > 0.7:
            signals.append('HIGH_CONFLUENCE')
        
        return signals
