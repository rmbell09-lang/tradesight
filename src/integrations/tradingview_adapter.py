"""
TradeSight TradingView Webhook Adapter

Parses, validates, and converts TradingView alert webhooks
into the internal signal format consumed by PaperTrader.execute_signal().
"""

import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

REQUIRED_FIELDS = {"symbol", "action", "confidence"}
VALID_ACTIONS = {"buy", "sell"}
CONFIDENCE_MIN = 0.55
CONFIDENCE_MAX = 0.85


@dataclass(frozen=True)
class TradingViewSignal:
    """Immutable parsed TradingView alert signal."""
    symbol: str
    action: str
    confidence: float
    price: Optional[float]
    reason: str
    strategy: str


def parse_tradingview_payload(raw: dict) -> TradingViewSignal:
    """Parse and validate incoming TradingView webhook payload.

    Args:
        raw: JSON payload from TradingView webhook.

    Returns:
        Validated TradingViewSignal.

    Raises:
        ValueError: If required fields are missing or action is invalid.
    """
    missing = REQUIRED_FIELDS - raw.keys()
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(sorted(missing))}")

    action = str(raw["action"]).lower()
    if action not in VALID_ACTIONS:
        raise ValueError(f"Invalid action: '{action}'. Must be one of: {', '.join(VALID_ACTIONS)}")

    confidence = float(raw["confidence"])
    confidence = max(CONFIDENCE_MIN, min(CONFIDENCE_MAX, confidence))

    price_raw = raw.get("price")
    price = float(price_raw) if price_raw is not None else None

    return TradingViewSignal(
        symbol=str(raw["symbol"]).upper(),
        action=action,
        confidence=confidence,
        price=price,
        reason=raw.get("reason", f"TradingView {action} signal"),
        strategy=raw.get("strategy", "TradingView"),
    )


def verify_token(payload: dict, expected_token: str) -> bool:
    """Verify webhook authentication token using constant-time comparison.

    Args:
        payload: Webhook JSON payload (expects a "token" key).
        expected_token: The expected secret token.

    Returns:
        True if token matches, False otherwise.
    """
    token = payload.get("token")
    if token is None:
        return False
    return hmac.compare_digest(str(token), expected_token)


def to_internal_signal(tv_signal: TradingViewSignal, current_price: float) -> dict:
    """Convert TradingViewSignal to PaperTrader internal signal format.

    Args:
        tv_signal: Parsed TradingView signal.
        current_price: Current market price for the symbol.

    Returns:
        Dict matching PaperTrader.execute_signal() expected format.
    """
    return {
        "symbol": tv_signal.symbol,
        "action": tv_signal.action,
        "side": "long" if tv_signal.action == "buy" else "short",
        "confidence": tv_signal.confidence,
        "reason": tv_signal.reason,
        "strategy": tv_signal.strategy,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "current_price": current_price,
    }
