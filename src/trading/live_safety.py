#!/usr/bin/env python3
"""Fail-closed live trading safety and shadow canary scaffolding.

This module does not place orders. It only decides whether a proposed live
order is allowed and records shadow/canary receipts for later review.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Dict, List, Optional


FINAL_EXIT_ACTIONS = {"sell", "close", "exit"}


@dataclass
class LiveSafetyPolicy:
    """Hard limits for live trading promotion."""

    live_intent_required: str = "I_UNDERSTAND_LIVE_RISK"
    kill_switch: bool = True
    approved_symbols: List[str] = field(default_factory=list)
    approved_strategies: List[str] = field(default_factory=list)
    max_notional_per_trade: float = 5.0
    max_daily_loss: float = 5.0
    max_open_positions: int = 1
    dry_run_max_age_minutes: int = 60
    require_secure_credentials: bool = True
    allow_live_orders: bool = False
    allow_tiny_live_canary: bool = False


@dataclass
class AccountSnapshot:
    """Sanitized broker account facts needed by the gate."""

    endpoint: str
    status: str
    trading_blocked: bool
    account_blocked: bool
    transfers_blocked: bool = False
    buying_power: float = 0.0
    equity: float = 0.0
    pattern_day_trader: bool = False
    daytrade_count: int = 0
    shorting_enabled: bool = False


@dataclass
class DryRunReceipt:
    """Fresh checklist proving shadow mode worked immediately before live."""

    passed: bool
    generated_at: str
    checklist_version: str = "live-canary-v1"
    mode: str = "shadow"
    notes: str = ""

    def is_fresh(self, max_age_minutes: int, now: Optional[datetime] = None) -> bool:
        if not self.passed:
            return False
        now = now or datetime.now()
        try:
            generated = datetime.fromisoformat(self.generated_at)
        except (TypeError, ValueError):
            return False
        return now - generated <= timedelta(minutes=max_age_minutes)


@dataclass
class OrderIntent:
    """A proposed order before any broker call."""

    symbol: str
    strategy: str
    action: str
    quantity: float
    price: float
    side: str = "long"
    mode: str = "shadow"
    live_intent: str = ""

    @property
    def notional(self) -> float:
        return max(0.0, float(self.quantity or 0.0) * float(self.price or 0.0))


@dataclass
class LiveSafetyReceipt:
    """Decision artifact for every shadow/canary/live attempt."""

    allowed: bool
    mode: str
    reasons: List[str]
    intent: Dict
    account: Dict
    limits: Dict
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict:
        return asdict(self)


class LiveTradingSafetyGate:
    """Deny live/canary trading unless every required condition is known-good."""

    def __init__(self, policy: Optional[LiveSafetyPolicy] = None):
        self.policy = policy or LiveSafetyPolicy()

    def evaluate(
        self,
        order: OrderIntent,
        account: Optional[AccountSnapshot] = None,
        dry_run: Optional[DryRunReceipt] = None,
        daily_realized_pnl: float = 0.0,
        open_positions: int = 0,
        credential_source: str = "unknown",
        now: Optional[datetime] = None,
    ) -> LiveSafetyReceipt:
        reasons: List[str] = []
        action = (order.action or "").lower()
        mode = (order.mode or "shadow").lower()

        if mode == "shadow":
            return LiveSafetyReceipt(
                allowed=True,
                mode=mode,
                reasons=["shadow_only_no_broker_order_allowed"],
                intent=asdict(order),
                account=asdict(account) if account else {},
                limits=self._limits_dict(daily_realized_pnl, open_positions, credential_source),
            )

        if mode not in {"canary", "live"}:
            reasons.append("unknown_mode")
        if mode == "live" and not self.policy.allow_live_orders:
            reasons.append("live_orders_disabled_by_policy")
        if mode == "canary" and not self.policy.allow_tiny_live_canary:
            reasons.append("tiny_live_canary_disabled_by_policy")
        if order.live_intent != self.policy.live_intent_required:
            reasons.append("explicit_live_intent_missing")
        if self.policy.kill_switch:
            reasons.append("kill_switch_enabled")
        if not account:
            reasons.append("broker_account_snapshot_missing")
        else:
            if account.endpoint != "live":
                reasons.append("broker_endpoint_not_live")
            if str(account.status).upper() != "ACTIVE":
                reasons.append("broker_account_not_active")
            if account.trading_blocked:
                reasons.append("broker_trading_blocked")
            if account.account_blocked:
                reasons.append("broker_account_blocked")
            if account.transfers_blocked:
                reasons.append("broker_transfers_blocked")
            if account.buying_power <= 0:
                reasons.append("broker_buying_power_missing")
        if order.symbol not in set(self.policy.approved_symbols):
            reasons.append("symbol_not_approved")
        if order.strategy not in set(self.policy.approved_strategies):
            reasons.append("strategy_not_approved")
        if order.notional <= 0:
            reasons.append("order_notional_missing")
        if action not in FINAL_EXIT_ACTIONS and order.notional > self.policy.max_notional_per_trade:
            reasons.append("order_notional_exceeds_cap")
        if daily_realized_pnl <= -abs(self.policy.max_daily_loss):
            reasons.append("daily_loss_cap_reached")
        if action not in FINAL_EXIT_ACTIONS and open_positions >= self.policy.max_open_positions:
            reasons.append("open_position_cap_reached")
        if not dry_run or not dry_run.is_fresh(self.policy.dry_run_max_age_minutes, now=now):
            reasons.append("fresh_dry_run_receipt_missing")
        if self.policy.require_secure_credentials and credential_source not in {"keychain", "broker_oauth"}:
            reasons.append("credential_source_not_secure")

        return LiveSafetyReceipt(
            allowed=not reasons,
            mode=mode,
            reasons=reasons,
            intent=asdict(order),
            account=asdict(account) if account else {},
            limits=self._limits_dict(daily_realized_pnl, open_positions, credential_source),
        )

    def _limits_dict(self, daily_realized_pnl: float, open_positions: int, credential_source: str) -> Dict:
        return {
            "max_notional_per_trade": self.policy.max_notional_per_trade,
            "max_daily_loss": self.policy.max_daily_loss,
            "max_open_positions": self.policy.max_open_positions,
            "daily_realized_pnl": daily_realized_pnl,
            "open_positions": open_positions,
            "credential_source": credential_source,
        }


class ShadowCanaryRecorder:
    """Append-only shadow/canary receipt log. It never calls a broker."""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.log_path = self.base_dir / "data" / "live_safety_receipts.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, receipt: LiveSafetyReceipt) -> Path:
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(receipt.to_dict(), sort_keys=True) + "\n")
        return self.log_path
