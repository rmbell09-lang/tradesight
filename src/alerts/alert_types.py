"""Alert type definitions for TradeSight notifications."""
from enum import Enum


class AlertType(Enum):
    """Types of alerts that TradeSight can dispatch."""
    SIGNAL_FIRED = "signal_fired"        # Scanner found a trading signal
    TRADE_EXECUTED = "trade_executed"    # Paper trader executed a buy/sell
    DAILY_SUMMARY = "daily_summary"      # End-of-day performance summary
    STRATEGY_EVOLVED = "strategy_evolved"  # Overnight tournament produced a winner
    BROKER_DISCONNECTED = "broker_disconnected"
    STALE_MARKET_DATA = "stale_market_data"
    ACCOUNTING_DRIFT = "accounting_drift"
    UNEXPECTED_POSITION = "unexpected_position"
    DUPLICATE_ORDER = "duplicate_order"
    MISSED_EXIT = "missed_exit"
    OPTIMIZER_FAILURE = "optimizer_failure"
    RISK_LIMIT_BREACH = "risk_limit_breach"
    SERVICE_FAILURE = "service_failure"
    TRADING_SUSPENDED = "trading_suspended"
