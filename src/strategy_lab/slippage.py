"""
TradeSight Slippage & Spread Modeling

Realistic transaction cost modeling for backtests:
- Bid/ask spread (configurable per asset class)
- Volume-dependent slippage (market impact)
- Volatility-dependent slippage (wider fills in volatile markets)

Usage:
    model = SlippageModel(asset_class='stock')
    fill_price = model.apply(price, direction='buy', volume=1000, atr=2.5, avg_volume=1_000_000)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import numpy as np


class AssetClass(Enum):
    STOCK = "stock"           # US equities
    CRYPTO = "crypto"         # Cryptocurrency
    FOREX = "forex"           # Foreign exchange
    PENNY_STOCK = "penny"     # Low-float / penny stocks


# Default spread + slippage parameters by asset class
ASSET_DEFAULTS = {
    AssetClass.STOCK: {
        'base_spread_bps': 2.0,       # ~0.02% half-spread for liquid stocks
        'volume_impact_bps': 1.0,     # per 1% of avg daily volume
        'volatility_scale': 0.5,      # multiplier on ATR-based component
    },
    AssetClass.CRYPTO: {
        'base_spread_bps': 5.0,       # wider for crypto
        'volume_impact_bps': 3.0,
        'volatility_scale': 0.8,
    },
    AssetClass.FOREX: {
        'base_spread_bps': 1.0,       # tight for major FX pairs
        'volume_impact_bps': 0.5,
        'volatility_scale': 0.3,
    },
    AssetClass.PENNY_STOCK: {
        'base_spread_bps': 20.0,      # very wide for illiquid names
        'volume_impact_bps': 10.0,
        'volatility_scale': 1.5,
    },
}


@dataclass
class SlippageModel:
    """
    Realistic slippage + spread model.

    Components:
    1. Half-spread: fixed cost of crossing the bid/ask
    2. Volume impact: larger orders move the market more
    3. Volatility component: slippage scales with recent ATR

    All measured in basis points (bps). 1 bp = 0.01%.
    """
    asset_class: str = "stock"
    base_spread_bps: Optional[float] = None
    volume_impact_bps: Optional[float] = None
    volatility_scale: Optional[float] = None
    # Random jitter adds noise so backtests don't produce deterministic fills
    jitter_bps: float = 0.5
    seed: Optional[int] = None

    def __post_init__(self):
        try:
            ac = AssetClass(self.asset_class)
        except ValueError:
            ac = AssetClass.STOCK

        defaults = ASSET_DEFAULTS[ac]
        if self.base_spread_bps is None:
            self.base_spread_bps = defaults['base_spread_bps']
        if self.volume_impact_bps is None:
            self.volume_impact_bps = defaults['volume_impact_bps']
        if self.volatility_scale is None:
            self.volatility_scale = defaults['volatility_scale']

        self._rng = np.random.default_rng(self.seed)

    def total_slippage_bps(self,
                           order_shares: float = 0.0,
                           avg_daily_volume: float = 1_000_000.0,
                           atr: float = 0.0,
                           price: float = 1.0) -> float:
        """
        Compute total one-way slippage in basis points.

        Args:
            order_shares: Number of shares in the order
            avg_daily_volume: 20-day average volume
            atr: 14-period ATR (dollar value)
            price: Current price (used to normalise ATR)

        Returns:
            Total slippage in bps (always >= 0)
        """
        # 1. Half-spread (always paid)
        spread = self.base_spread_bps

        # 2. Volume impact: linear in participation rate
        vol_impact = 0.0
        if avg_daily_volume > 0 and order_shares > 0:
            participation_pct = (order_shares / avg_daily_volume) * 100.0
            vol_impact = self.volume_impact_bps * participation_pct

        # 3. Volatility: ATR as % of price, scaled
        vol_slip = 0.0
        if price > 0 and atr > 0:
            atr_pct = (atr / price) * 100.0  # ATR as % of price
            vol_slip = self.volatility_scale * atr_pct * 100.0  # convert % → bps

        # 4. Random jitter
        jitter = self._rng.uniform(0, self.jitter_bps)

        return max(0.0, spread + vol_impact + vol_slip + jitter)

    def apply(self,
              price: float,
              direction: str,
              order_shares: float = 0.0,
              avg_daily_volume: float = 1_000_000.0,
              atr: float = 0.0) -> float:
        """
        Return the filled price after slippage.

        For buys: fill is ABOVE market price (worse).
        For sells: fill is BELOW market price (worse).

        Args:
            price: Market price at signal time
            direction: 'long'/'buy' or 'short'/'sell'
            order_shares: Number of shares
            avg_daily_volume: Recent average volume
            atr: 14-period ATR in dollar terms

        Returns:
            Adjusted fill price
        """
        slip_bps = self.total_slippage_bps(order_shares, avg_daily_volume, atr, price)
        slip_frac = slip_bps / 10_000.0  # bps → fraction

        if direction in ('long', 'buy'):
            return price * (1.0 + slip_frac)
        else:
            return price * (1.0 - slip_frac)

    def round_trip_cost_bps(self,
                            order_shares: float = 0.0,
                            avg_daily_volume: float = 1_000_000.0,
                            atr: float = 0.0,
                            price: float = 1.0) -> float:
        """Estimate total round-trip transaction cost in bps (entry + exit)."""
        one_way = self.total_slippage_bps(order_shares, avg_daily_volume, atr, price)
        return one_way * 2.0

    def summary(self) -> dict:
        """Return model parameters for logging."""
        return {
            'asset_class': self.asset_class,
            'base_spread_bps': self.base_spread_bps,
            'volume_impact_bps': self.volume_impact_bps,
            'volatility_scale': self.volatility_scale,
            'jitter_bps': self.jitter_bps,
        }
