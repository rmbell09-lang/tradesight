#!/usr/bin/env python3
"""
TradeSight Market Regime Detector (Task 17)

Classifies current market conditions to adjust strategy weights.
LOW_VOL: Mean reversion works well (RSI, Bollinger)
HIGH_VOL: Trend following works better (MACD, ORB)
TRANSITION: Reduce position sizes, tighten stops
"""

import logging
import numpy as np
import pandas as pd
from typing import Dict, Optional, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class MarketRegime(Enum):
    LOW_VOL = "low_vol"          # VIX < 15 or realized vol < 12%
    NORMAL = "normal"            # VIX 15-25 or realized vol 12-20%
    HIGH_VOL = "high_vol"        # VIX 25-35 or realized vol 20-30%
    EXTREME = "extreme"          # VIX > 35 or realized vol > 30%
    UNKNOWN = "unknown"


# Strategy weight adjustments per regime
REGIME_STRATEGY_WEIGHTS = {
    MarketRegime.LOW_VOL: {
        'RSI Mean Reversion': 1.3,    # Mean reversion thrives in calm markets
        'Bollinger Bounce': 1.2,
        'MACD Crossover': 0.8,
        'VWAP Reversion': 1.2,
        'Opening Range Breakout': 0.7,
    },
    MarketRegime.NORMAL: {
        'RSI Mean Reversion': 1.0,
        'Bollinger Bounce': 1.0,
        'MACD Crossover': 1.0,
        'VWAP Reversion': 1.0,
        'Opening Range Breakout': 1.0,
    },
    MarketRegime.HIGH_VOL: {
        'RSI Mean Reversion': 0.6,    # Mean reversion fails in volatile markets
        'Bollinger Bounce': 0.5,
        'MACD Crossover': 1.3,        # Trend following works better
        'VWAP Reversion': 0.7,
        'Opening Range Breakout': 1.2,
    },
    MarketRegime.EXTREME: {
        'RSI Mean Reversion': 0.3,    # Almost all mean reversion fails
        'Bollinger Bounce': 0.3,
        'MACD Crossover': 1.0,
        'VWAP Reversion': 0.4,
        'Opening Range Breakout': 0.8,
    },
}

# Position size multiplier per regime
REGIME_POSITION_MULTIPLIER = {
    MarketRegime.LOW_VOL: 1.0,
    MarketRegime.NORMAL: 1.0,
    MarketRegime.HIGH_VOL: 0.7,     # Reduce size in volatile markets
    MarketRegime.EXTREME: 0.4,     # Significantly reduce in extreme conditions
    MarketRegime.UNKNOWN: 0.8,
}


class RegimeDetector:
    """Detects current market regime using SPY volatility and optional VIX."""

    def __init__(self):
        self._cached_regime = None
        self._cached_at = None
        self._cache_ttl_seconds = 3600  # Re-check hourly

    def detect_regime(self, spy_data: pd.DataFrame = None,
                      vix_value: float = None) -> Tuple[MarketRegime, Dict]:
        """
        Detect current market regime.

        Args:
            spy_data: SPY OHLCV DataFrame (at least 30 bars)
            vix_value: Current VIX value (optional, used if available)

        Returns:
            Tuple of (regime, details_dict)
        """
        details = {
            'realized_vol_20d': None,
            'realized_vol_5d': None,
            'vix': vix_value,
            'method': 'realized_vol',
        }

        # Method 1: VIX-based (if provided)
        if vix_value is not None and vix_value > 0:
            details['method'] = 'vix'
            if vix_value < 15:
                regime = MarketRegime.LOW_VOL
            elif vix_value < 25:
                regime = MarketRegime.NORMAL
            elif vix_value < 35:
                regime = MarketRegime.HIGH_VOL
            else:
                regime = MarketRegime.EXTREME
            logger.info(f"[Regime] VIX={vix_value:.1f} → {regime.value}")
            return regime, details

        # Method 2: Realized volatility from SPY data
        if spy_data is not None and len(spy_data) >= 30:
            try:
                returns = spy_data['close'].pct_change().dropna()

                # 20-day realized vol (annualized)
                vol_20d = float(returns.tail(20).std() * np.sqrt(252) * 100)
                details['realized_vol_20d'] = round(vol_20d, 2)

                # 5-day realized vol for regime change detection
                vol_5d = float(returns.tail(5).std() * np.sqrt(252) * 100)
                details['realized_vol_5d'] = round(vol_5d, 2)

                if vol_20d < 12:
                    regime = MarketRegime.LOW_VOL
                elif vol_20d < 20:
                    regime = MarketRegime.NORMAL
                elif vol_20d < 30:
                    regime = MarketRegime.HIGH_VOL
                else:
                    regime = MarketRegime.EXTREME

                # Check for regime transition (5d vol diverging from 20d)
                if vol_5d > vol_20d * 1.5:
                    details['transition'] = 'vol_expanding'
                    # If vol is expanding rapidly, treat as one level higher
                    if regime == MarketRegime.LOW_VOL:
                        regime = MarketRegime.NORMAL
                    elif regime == MarketRegime.NORMAL:
                        regime = MarketRegime.HIGH_VOL
                elif vol_5d < vol_20d * 0.6:
                    details['transition'] = 'vol_contracting'

                logger.info(
                    f"[Regime] Realized vol: 20d={vol_20d:.1f}%, 5d={vol_5d:.1f}% "
                    f"→ {regime.value} (transition={details.get('transition', 'none')})")
                return regime, details

            except Exception as e:
                logger.warning(f"[Regime] Vol calculation failed: {e}")

        logger.warning("[Regime] Insufficient data — returning UNKNOWN")
        return MarketRegime.UNKNOWN, details

    def get_strategy_weight(self, regime: MarketRegime, strategy_name: str) -> float:
        """Get weight multiplier for a strategy in the current regime."""
        weights = REGIME_STRATEGY_WEIGHTS.get(regime, REGIME_STRATEGY_WEIGHTS[MarketRegime.NORMAL])
        return weights.get(strategy_name, 1.0)

    def get_position_multiplier(self, regime: MarketRegime) -> float:
        """Get position size multiplier for the current regime."""
        return REGIME_POSITION_MULTIPLIER.get(regime, 1.0)


# Convenience function for VIX fetching via yfinance
def fetch_vix() -> Optional[float]:
    """Fetch current VIX value via yfinance. Returns None on failure."""
    try:
        import yfinance as yf
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            vix = yf.download("^VIX", period="5d", interval="1d",
                              progress=False, auto_adjust=True)
        if vix is not None and len(vix) > 0:
            # Handle MultiIndex columns
            if isinstance(vix.columns, pd.MultiIndex):
                close_col = [c for c in vix.columns if c[0].lower() == 'close']
                if close_col:
                    return float(vix[close_col[0]].iloc[-1])
            else:
                return float(vix['Close'].iloc[-1]) if 'Close' in vix.columns else float(vix['close'].iloc[-1])
    except Exception as e:
        logger.debug(f"VIX fetch failed: {e}")
    return None
