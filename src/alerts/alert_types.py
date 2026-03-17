"""Alert type definitions for TradeSight notifications."""
from enum import Enum


class AlertType(Enum):
    """Types of alerts that TradeSight can dispatch."""
    SIGNAL_FIRED = "signal_fired"        # Scanner found a trading signal
    TRADE_EXECUTED = "trade_executed"    # Paper trader executed a buy/sell
    DAILY_SUMMARY = "daily_summary"      # End-of-day performance summary
    STRATEGY_EVOLVED = "strategy_evolved"  # Overnight tournament produced a winner
