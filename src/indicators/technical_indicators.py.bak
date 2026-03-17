#!/usr/bin/env python3
"""
TradeSight Technical Indicators Engine
All major indicators with confluence scoring
"""

import pandas as pd
import numpy as np
import talib
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

class TechnicalIndicators:
    """
    Technical indicators engine implementing all major indicators
    from TradeSight trading skill research
    """
    
    def __init__(self):
        self.indicators = {}
        self.signals = {}
    
    def calculate_all(self, ohlcv: pd.DataFrame) -> Dict:
        """
        Calculate all technical indicators for given OHLCV data
        
        Args:
            ohlcv: DataFrame with columns ['open', 'high', 'low', 'close', 'volume']
                   Index should be datetime
        
        Returns:
            Dict with all indicator values and signals
        """
        if len(ohlcv) < 200:  # Need enough data for 200-day MA
            raise ValueError(f"Need at least 200 periods, got {len(ohlcv)}")
        
        high = ohlcv['high'].astype(np.float64).values
        low = ohlcv['low'].astype(np.float64).values
        close = ohlcv['close'].astype(np.float64).values
        volume = ohlcv['volume'].astype(np.float64).values
        
        results = {
            'timestamp': ohlcv.index[-1],
            'price': close[-1],
            'indicators': {},
            'signals': {},
            'confluence_score': 0.0
        }
        
        # RSI (14-period)
        rsi = talib.RSI(close, timeperiod=14)
        results['indicators']['rsi'] = rsi[-1]
        results['signals']['rsi'] = self._rsi_signal(rsi[-1])
        
        # MACD (12, 26, 9)
        macd, macdsignal, macdhist = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
        results['indicators']['macd'] = {
            'macd': macd[-1],
            'signal': macdsignal[-1],
            'histogram': macdhist[-1]
        }
        results['signals']['macd'] = self._macd_signal(macd[-2:], macdsignal[-2:])
        
        # Bollinger Bands (20-period, 2 std)
        bb_upper, bb_middle, bb_lower = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)
        results['indicators']['bollinger'] = {
            'upper': bb_upper[-1],
            'middle': bb_middle[-1],
            'lower': bb_lower[-1],
            'position': (close[-1] - bb_lower[-1]) / (bb_upper[-1] - bb_lower[-1])
        }
        results['signals']['bollinger'] = self._bollinger_signal(close[-1], bb_upper[-1], bb_middle[-1], bb_lower[-1])
        
        # Moving Averages (Key periods)
        mas = {}
        ma_signals = []
        for period in [5, 9, 20, 50, 100, 200]:
            if len(close) >= period:
                sma = talib.SMA(close, timeperiod=period)
                ema = talib.EMA(close, timeperiod=period)
                mas[f'sma_{period}'] = sma[-1]
                mas[f'ema_{period}'] = ema[-1]
                
                # Price vs MA signal
                if close[-1] > sma[-1]:
                    ma_signals.append(1)  # Bullish
                elif close[-1] < sma[-1]:
                    ma_signals.append(-1)  # Bearish
                else:
                    ma_signals.append(0)  # Neutral
        
        results['indicators']['moving_averages'] = mas
        
        # Golden/Death Cross (50-day vs 200-day)
        if len(close) >= 200:
            sma50 = talib.SMA(close, timeperiod=50)
            sma200 = talib.SMA(close, timeperiod=200)
            if sma50[-2] <= sma200[-2] and sma50[-1] > sma200[-1]:
                results['signals']['golden_cross'] = 1  # Golden Cross
            elif sma50[-2] >= sma200[-2] and sma50[-1] < sma200[-1]:
                results['signals']['golden_cross'] = -1  # Death Cross
            else:
                results['signals']['golden_cross'] = 0  # No cross
                
            results['signals']['ma_trend'] = 1 if sum(ma_signals) > 0 else (-1 if sum(ma_signals) < 0 else 0)
        
        # Volume Analysis
        volume_sma = talib.SMA(volume, timeperiod=20)
        results['indicators']['volume'] = {
            'current': volume[-1],
            'sma_20': volume_sma[-1],
            'relative': volume[-1] / volume_sma[-1] if volume_sma[-1] > 0 else 1.0
        }
        results['signals']['volume'] = self._volume_signal(volume[-1], volume_sma[-1])
        
        # VWAP (simplified - needs intraday data for true VWAP)
        results['indicators']['vwap'] = self._calculate_vwap(ohlcv.tail(20))
        results['signals']['vwap'] = 1 if close[-1] > results['indicators']['vwap'] else -1
        
        # Super Trend (ATR-based)
        atr = talib.ATR(high, low, close, timeperiod=14)
        supertrend, supertrend_signal = self._calculate_supertrend(high, low, close, atr, multiplier=3.0)
        results['indicators']['supertrend'] = supertrend[-1]
        results['signals']['supertrend'] = supertrend_signal[-1]
        
        # Ichimoku Cloud (simplified)
        ichimoku = self._calculate_ichimoku(high, low, close)
        results['indicators']['ichimoku'] = ichimoku
        results['signals']['ichimoku'] = self._ichimoku_signal(close[-1], ichimoku)
        
        # Confluence Score (combine all signals)
        results['confluence_score'] = self._calculate_confluence_score(results['signals'])
        
        return results
    
    def _rsi_signal(self, rsi: float) -> int:
        """RSI signal: -1 (oversold), 0 (neutral), 1 (overbought)"""
        if rsi > 70:
            return 1  # Overbought (potential sell)
        elif rsi < 30:
            return -1  # Oversold (potential buy)
        else:
            return 0  # Neutral
    
    def _macd_signal(self, macd: np.ndarray, signal: np.ndarray) -> int:
        """MACD crossover signal"""
        if len(macd) < 2 or len(signal) < 2:
            return 0
        
        # Bullish crossover: MACD crosses above signal
        if macd[-2] <= signal[-2] and macd[-1] > signal[-1]:
            return 1
        # Bearish crossover: MACD crosses below signal
        elif macd[-2] >= signal[-2] and macd[-1] < signal[-1]:
            return -1
        else:
            return 0
    
    def _bollinger_signal(self, price: float, upper: float, middle: float, lower: float) -> int:
        """Bollinger Bands signal"""
        if price > upper:
            return 1  # Above upper band - overbought
        elif price < lower:
            return -1  # Below lower band - oversold
        elif price > middle:
            return 0.5  # Above middle - mild bullish
        else:
            return -0.5  # Below middle - mild bearish
    
    def _volume_signal(self, current_volume: float, avg_volume: float) -> int:
        """Volume confirmation signal"""
        if current_volume > avg_volume * 1.5:
            return 1  # High volume - strong signal
        elif current_volume < avg_volume * 0.5:
            return -1  # Low volume - weak signal
        else:
            return 0  # Normal volume
    
    def _calculate_vwap(self, ohlcv: pd.DataFrame) -> float:
        """Calculate Volume-Weighted Average Price"""
        typical_price = (ohlcv['high'] + ohlcv['low'] + ohlcv['close']) / 3
        vwap = (typical_price * ohlcv['volume']).sum() / ohlcv['volume'].sum()
        return vwap
    
    def _calculate_supertrend(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, 
                            atr: np.ndarray, multiplier: float = 3.0) -> Tuple[np.ndarray, np.ndarray]:
        """Calculate Super Trend indicator"""
        hl2 = (high + low) / 2
        upper_band = hl2 + (multiplier * atr)
        lower_band = hl2 - (multiplier * atr)
        
        supertrend = np.zeros_like(close)
        signal = np.zeros_like(close)
        
        for i in range(1, len(close)):
            # Initialize first value
            if i == 1:
                supertrend[i] = lower_band[i] if close[i] > hl2[i] else upper_band[i]
                continue
            
            # Calculate Super Trend
            if close[i] > supertrend[i-1]:
                supertrend[i] = lower_band[i]
                signal[i] = 1  # Bullish
            else:
                supertrend[i] = upper_band[i]
                signal[i] = -1  # Bearish
        
        return supertrend, signal
    
    def _calculate_ichimoku(self, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> Dict:
        """Calculate Ichimoku Cloud components"""
        def highest_high_lowest_low(high, low, period):
            """Helper function for Ichimoku calculations"""
            hh = pd.Series(high).rolling(window=period).max().values
            ll = pd.Series(low).rolling(window=period).min().values
            return hh, ll
        
        # Tenkan-sen (Conversion Line): (9-period high + 9-period low) / 2
        hh9, ll9 = highest_high_lowest_low(high, low, 9)
        tenkan = (hh9 + ll9) / 2
        
        # Kijun-sen (Base Line): (26-period high + 26-period low) / 2
        hh26, ll26 = highest_high_lowest_low(high, low, 26)
        kijun = (hh26 + ll26) / 2
        
        # Senkou Span A: (Tenkan + Kijun) / 2, plotted 26 periods ahead
        senkou_a = (tenkan + kijun) / 2
        
        # Senkou Span B: (52-period high + 52-period low) / 2, plotted 26 periods ahead
        hh52, ll52 = highest_high_lowest_low(high, low, 52)
        senkou_b = (hh52 + ll52) / 2
        
        # Chikou Span: Current close plotted 26 periods back
        chikou = close
        
        return {
            'tenkan': tenkan[-1] if len(tenkan) > 0 and not np.isnan(tenkan[-1]) else 0,
            'kijun': kijun[-1] if len(kijun) > 0 and not np.isnan(kijun[-1]) else 0,
            'senkou_a': senkou_a[-1] if len(senkou_a) > 0 and not np.isnan(senkou_a[-1]) else 0,
            'senkou_b': senkou_b[-1] if len(senkou_b) > 0 and not np.isnan(senkou_b[-1]) else 0,
            'chikou': chikou[-1] if len(chikou) > 0 else 0
        }
    
    def _ichimoku_signal(self, price: float, ichimoku: Dict) -> int:
        """Ichimoku signal interpretation"""
        cloud_top = max(ichimoku['senkou_a'], ichimoku['senkou_b'])
        cloud_bottom = min(ichimoku['senkou_a'], ichimoku['senkou_b'])
        
        if price > cloud_top:
            return 1  # Above cloud - bullish
        elif price < cloud_bottom:
            return -1  # Below cloud - bearish
        else:
            return 0  # Inside cloud - neutral
    
    def _calculate_confluence_score(self, signals: Dict) -> float:
        """
        Calculate confluence score from all signals
        Range: -1.0 (strong bearish) to +1.0 (strong bullish)
        """
        signal_values = []
        weights = {
            'rsi': 0.15,
            'macd': 0.20,
            'bollinger': 0.15,
            'ma_trend': 0.20,
            'volume': 0.10,
            'vwap': 0.10,
            'supertrend': 0.10
        }
        
        total_weight = 0
        weighted_sum = 0
        
        for signal_name, weight in weights.items():
            if signal_name in signals:
                signal_value = signals[signal_name]
                weighted_sum += signal_value * weight
                total_weight += weight
        
        # Normalize to -1.0 to +1.0 range
        if total_weight > 0:
            confluence = weighted_sum / total_weight
            return max(-1.0, min(1.0, confluence))
        
        return 0.0

def main():
    """Test the technical indicators engine"""
    # Generate sample OHLCV data for testing
    dates = pd.date_range('2023-01-01', periods=250, freq='D')
    np.random.seed(42)
    
    # Simulate price data
    close_prices = 100 + np.cumsum(np.random.randn(250) * 0.02)
    high_prices = close_prices + np.random.rand(250) * 2
    low_prices = close_prices - np.random.rand(250) * 2
    open_prices = close_prices + np.random.randn(250) * 0.5
    volumes = np.random.randint(100000, 1000000, 250).astype(np.float64)
    
    test_data = pd.DataFrame({
        'open': open_prices,
        'high': high_prices,
        'low': low_prices,
        'close': close_prices,
        'volume': volumes
    }, index=dates)
    
    # Test indicators engine
    engine = TechnicalIndicators()
    results = engine.calculate_all(test_data)
    
    print("=== Technical Indicators Test Results ===")
    print(f"Price: ${results['price']:.2f}")
    print(f"RSI: {results['indicators']['rsi']:.2f} (Signal: {results['signals']['rsi']})")
    print(f"MACD: {results['indicators']['macd']['macd']:.3f} (Signal: {results['signals']['macd']})")
    print(f"Bollinger Position: {results['indicators']['bollinger']['position']:.2f} (Signal: {results['signals']['bollinger']})")
    print(f"Volume Relative: {results['indicators']['volume']['relative']:.2f} (Signal: {results['signals']['volume']})")
    print(f"Super Trend: ${results['indicators']['supertrend']:.2f} (Signal: {results['signals']['supertrend']})")
    print(f"\nCONFLUENCE SCORE: {results['confluence_score']:.3f}")
    
    if results['confluence_score'] > 0.5:
        print("🟢 STRONG BUY SIGNAL")
    elif results['confluence_score'] > 0.2:
        print("🔵 MILD BUY SIGNAL")
    elif results['confluence_score'] < -0.5:
        print("🔴 STRONG SELL SIGNAL")
    elif results['confluence_score'] < -0.2:
        print("🟡 MILD SELL SIGNAL")
    else:
        print("⚪ NEUTRAL")

if __name__ == "__main__":
    main()