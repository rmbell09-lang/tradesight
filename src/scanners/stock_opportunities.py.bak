"""
TradeSight Stock Opportunity Scorer

Multi-factor scoring system for ranking stock opportunities.
Combines volume, volatility, technical signals, and other factors
into a single confidence score.
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from datetime import datetime

import sys
sys.path.append('..')
from indicators.technical_indicators import TechnicalIndicators


@dataclass
class OpportunityScore:
    """Scored opportunity for a single asset"""
    symbol: str
    timestamp: datetime
    overall_score: float  # 0-100
    confidence: str  # 'high', 'medium', 'low'
    direction: str  # 'bullish', 'bearish', 'neutral'
    
    # Component scores (0-100 each)
    volume_score: float
    volatility_score: float
    technical_score: float
    momentum_score: float
    trend_score: float
    
    # Key signals
    active_signals: List[str] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)
    
    # Raw data
    current_price: float = 0.0
    avg_volume: float = 0.0
    volatility_pct: float = 0.0


class StockOpportunityScorer:
    """
    Multi-factor opportunity scoring system.
    
    Analyzes stocks/assets across 5 dimensions:
    1. Volume — unusual volume indicates institutional activity
    2. Volatility — higher vol = more opportunity (and risk)
    3. Technical — indicator confluence from TechnicalIndicators
    4. Momentum — price momentum over multiple timeframes
    5. Trend — overall trend strength and direction
    
    Each factor produces a 0-100 score. The overall score
    is a weighted combination with configurable weights.
    """
    
    DEFAULT_WEIGHTS = {
        'volume': 0.15,
        'volatility': 0.10,
        'technical': 0.35,
        'momentum': 0.25,
        'trend': 0.15,
    }
    
    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.weights = weights or self.DEFAULT_WEIGHTS
        self.indicators = TechnicalIndicators()
    
    def score_opportunity(self, 
                          data: pd.DataFrame,
                          symbol: str = "Unknown") -> OpportunityScore:
        """
        Score a single asset based on its OHLCV data.
        
        Args:
            data: DataFrame with columns: open, high, low, close, volume
            symbol: Asset symbol for identification
            
        Returns:
            OpportunityScore with detailed breakdown
        """
        if len(data) < 200:
            raise ValueError(f"Need at least 200 data points, got {len(data)}")
        
        current_price = float(data['close'].iloc[-1])
        signals = []
        risks = []
        
        # 1. Volume Score
        volume_score = self._score_volume(data)
        if volume_score > 70:
            signals.append("unusual_volume")
        
        # 2. Volatility Score
        volatility_score, vol_pct = self._score_volatility(data)
        if vol_pct > 5:
            risks.append("high_volatility")
        
        # 3. Technical Score (uses TechnicalIndicators)
        technical_score, tech_signals = self._score_technical(data)
        signals.extend(tech_signals)
        
        # 4. Momentum Score
        momentum_score, mom_direction = self._score_momentum(data)
        if momentum_score > 70:
            signals.append(f"strong_{mom_direction}_momentum")
        
        # 5. Trend Score
        trend_score, trend_dir = self._score_trend(data)
        if trend_score > 70:
            signals.append(f"strong_{trend_dir}_trend")
        
        # Calculate weighted overall score
        overall = (
            volume_score * self.weights['volume'] +
            volatility_score * self.weights['volatility'] +
            technical_score * self.weights['technical'] +
            momentum_score * self.weights['momentum'] +
            trend_score * self.weights['trend']
        )
        
        # Determine direction based on momentum and trend
        bullish_signals = sum(1 for s in signals if 'bullish' in s or 'buy' in s.lower())
        bearish_signals = sum(1 for s in signals if 'bearish' in s or 'sell' in s.lower())
        
        if bullish_signals > bearish_signals:
            direction = 'bullish'
        elif bearish_signals > bullish_signals:
            direction = 'bearish'
        else:
            direction = 'neutral'
        
        # Confidence level
        if overall > 70 and len(signals) >= 3:
            confidence = 'high'
        elif overall > 50 and len(signals) >= 2:
            confidence = 'medium'
        else:
            confidence = 'low'
        
        return OpportunityScore(
            symbol=symbol,
            timestamp=data.index[-1] if hasattr(data.index[-1], 'isoformat') else datetime.now(),
            overall_score=round(overall, 1),
            confidence=confidence,
            direction=direction,
            volume_score=round(volume_score, 1),
            volatility_score=round(volatility_score, 1),
            technical_score=round(technical_score, 1),
            momentum_score=round(momentum_score, 1),
            trend_score=round(trend_score, 1),
            active_signals=signals,
            risk_factors=risks,
            current_price=current_price,
            avg_volume=float(data['volume'].tail(20).mean()),
            volatility_pct=vol_pct
        )
    
    def rank_opportunities(self,
                           datasets: Dict[str, pd.DataFrame],
                           min_score: float = 40.0) -> List[OpportunityScore]:
        """
        Score and rank multiple assets, returning sorted opportunities.
        
        Args:
            datasets: Dict of symbol -> OHLCV DataFrame
            min_score: Minimum overall score to include
            
        Returns:
            List of OpportunityScore sorted by overall_score (desc)
        """
        scores = []
        
        for symbol, data in datasets.items():
            try:
                score = self.score_opportunity(data, symbol)
                if score.overall_score >= min_score:
                    scores.append(score)
            except Exception as e:
                continue
        
        scores.sort(key=lambda s: s.overall_score, reverse=True)
        return scores
    
    def _score_volume(self, data: pd.DataFrame) -> float:
        """Score based on volume analysis (0-100)"""
        volume = data['volume'].values
        
        # Compare recent volume to average
        recent_vol = np.mean(volume[-5:])
        avg_vol = np.mean(volume[-50:])
        
        if avg_vol == 0:
            return 50.0
        
        vol_ratio = recent_vol / avg_vol
        
        # Score: 1x avg = 50, 2x avg = 80, 3x+ avg = 100
        score = min(100, 50 + (vol_ratio - 1) * 30)
        return max(0, score)
    
    def _score_volatility(self, data: pd.DataFrame) -> Tuple[float, float]:
        """Score based on volatility (0-100) + return vol percentage"""
        close = data['close'].values
        returns = np.diff(close) / close[:-1]
        
        # Recent volatility (annualized)
        recent_vol = np.std(returns[-20:]) * np.sqrt(252) * 100
        
        # Score: moderate volatility is best (not too low, not too high)
        # Sweet spot around 20-40% annualized
        if recent_vol < 10:
            score = 30.0  # Too quiet
        elif recent_vol < 20:
            score = 60.0
        elif recent_vol < 40:
            score = 80.0  # Sweet spot
        elif recent_vol < 60:
            score = 60.0  # Getting risky
        else:
            score = 40.0  # Too volatile
        
        return score, recent_vol
    
    def _score_technical(self, data: pd.DataFrame) -> Tuple[float, List[str]]:
        """Score based on technical indicators (0-100) + signal list"""
        signals = []
        
        # Calculate basic indicators inline
        close = data['close']
        
        # RSI
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        current_rsi = rsi.iloc[-1]
        
        rsi_score = 50.0
        if current_rsi < 30:
            rsi_score = 80.0  # Oversold = buy opportunity
            signals.append("rsi_oversold")
        elif current_rsi > 70:
            rsi_score = 80.0  # Overbought = sell opportunity
            signals.append("rsi_overbought")
        elif 40 < current_rsi < 60:
            rsi_score = 40.0  # Neutral
        
        # MACD
        exp1 = close.ewm(span=12).mean()
        exp2 = close.ewm(span=26).mean()
        macd = exp1 - exp2
        macd_signal = macd.ewm(span=9).mean()
        
        macd_score = 50.0
        if macd.iloc[-1] > macd_signal.iloc[-1] and macd.iloc[-2] <= macd_signal.iloc[-2]:
            macd_score = 85.0
            signals.append("macd_bullish_cross")
        elif macd.iloc[-1] < macd_signal.iloc[-1] and macd.iloc[-2] >= macd_signal.iloc[-2]:
            macd_score = 85.0
            signals.append("macd_bearish_cross")
        
        # Bollinger Bands
        bb_middle = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        bb_upper = bb_middle + 2 * bb_std
        bb_lower = bb_middle - 2 * bb_std
        
        bb_score = 50.0
        if close.iloc[-1] <= bb_lower.iloc[-1]:
            bb_score = 80.0
            signals.append("bollinger_oversold")
        elif close.iloc[-1] >= bb_upper.iloc[-1]:
            bb_score = 80.0
            signals.append("bollinger_overbought")
        
        # Combined technical score
        tech_score = (rsi_score * 0.35 + macd_score * 0.35 + bb_score * 0.30)
        
        return tech_score, signals
    
    def _score_momentum(self, data: pd.DataFrame) -> Tuple[float, str]:
        """Score based on price momentum (0-100) + direction"""
        close = data['close'].values
        
        # Multi-timeframe momentum
        mom_5 = (close[-1] - close[-5]) / close[-5] * 100
        mom_10 = (close[-1] - close[-10]) / close[-10] * 100
        mom_20 = (close[-1] - close[-20]) / close[-20] * 100
        
        # Weighted momentum
        weighted_mom = mom_5 * 0.5 + mom_10 * 0.3 + mom_20 * 0.2
        
        # Direction
        direction = 'bullish' if weighted_mom > 0 else 'bearish'
        
        # Score based on magnitude (capped at ±10%)
        score = min(100, 50 + abs(weighted_mom) * 5)
        
        return score, direction
    
    def _score_trend(self, data: pd.DataFrame) -> Tuple[float, str]:
        """Score based on trend strength (0-100) + direction"""
        close = data['close']
        
        # Moving averages
        sma_20 = close.rolling(20).mean().iloc[-1]
        sma_50 = close.rolling(50).mean().iloc[-1]
        sma_100 = close.rolling(100).mean().iloc[-1]
        current = close.iloc[-1]
        
        # Count alignment
        aligned_bullish = (current > sma_20 > sma_50 > sma_100)
        aligned_bearish = (current < sma_20 < sma_50 < sma_100)
        
        if aligned_bullish:
            direction = 'bullish'
            score = 90.0
        elif aligned_bearish:
            direction = 'bearish'
            score = 90.0
        else:
            # Partial alignment
            bullish_count = sum([
                current > sma_20,
                current > sma_50,
                sma_20 > sma_50,
                sma_50 > sma_100
            ])
            
            direction = 'bullish' if bullish_count >= 3 else 'bearish' if bullish_count <= 1 else 'neutral'
            score = 40 + bullish_count * 10  # 40-80 range
        
        return score, direction
