"""Fail-closed operational gate for new paper entries.

Exits are always allowed. Once a broker accounting epoch exists, new entries
require fresh verified reconciliation, a fresh signal, and no manual suspension.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


EXIT_ACTIONS = {"sell", "close", "exit"}


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class RuntimeRiskDecision:
    allowed: bool
    action: str
    reasons: List[str] = field(default_factory=list)
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    accounting_status: str = "UNKNOWN"
    signal_observed_at: Optional[str] = None
    enforcement_active: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class OperationalRiskGate:
    """Suspend new entries when current operational truth is not trustworthy."""

    def __init__(self, project_root: Path, accounting_max_age_minutes: int = 30, signal_max_age_minutes: int = 15):
        self.project_root = Path(project_root)
        self.state_dir = self.project_root / "state"
        self.accounting_max_age = timedelta(minutes=accounting_max_age_minutes)
        self.signal_max_age = timedelta(minutes=signal_max_age_minutes)
        self.epoch_path = self.state_dir / "accounting-epoch.json"
        self.reconciliation_path = self.state_dir / "accounting-reconciliation.json"
        self.manual_path = self.state_dir / "trading-manual-suspension.json"
        self.receipt_path = self.state_dir / "trading-suspension.json"

    @property
    def enforcement_active(self) -> bool:
        return self.epoch_path.is_file()

    def evaluate(self, action: str, signal_observed_at: Any = None, now: Optional[datetime] = None) -> RuntimeRiskDecision:
        now = now or datetime.now(timezone.utc)
        action = str(action or "").lower()
        if action in EXIT_ACTIONS:
            return RuntimeRiskDecision(True, action, ["exit_path_always_allowed"], enforcement_active=self.enforcement_active)
        if not self.enforcement_active:
            return RuntimeRiskDecision(True, action, ["accounting_epoch_not_established_paper_only"], enforcement_active=False)

        reasons: List[str] = []
        status = "UNKNOWN"
        if self.manual_path.is_file():
            reasons.append("manual_trading_suspension_active")
        receipt: Dict[str, Any] = {}
        try:
            receipt = json.loads(self.reconciliation_path.read_text())
        except (OSError, ValueError, TypeError):
            reasons.append("accounting_reconciliation_missing_or_unreadable")
        if receipt:
            status = str(receipt.get("status") or "UNKNOWN")
            observed = _parse(receipt.get("observed_at"))
            if status != "VERIFIED":
                reasons.append("accounting_not_verified")
            if observed is None or now - observed > self.accounting_max_age:
                reasons.append("accounting_reconciliation_stale")

        signal_time = _parse(signal_observed_at)
        if signal_time is None:
            reasons.append("signal_timestamp_missing")
        elif now - signal_time > self.signal_max_age:
            reasons.append("market_signal_stale")
        elif signal_time - now > timedelta(minutes=2):
            reasons.append("market_signal_timestamp_in_future")

        decision = RuntimeRiskDecision(
            allowed=not reasons,
            action=action,
            reasons=reasons,
            accounting_status=status,
            signal_observed_at=str(signal_observed_at) if signal_observed_at else None,
            enforcement_active=True,
        )
        self._write_suspension(decision)
        return decision

    def status(self) -> Dict[str, Any]:
        receipt = {}
        if self.receipt_path.is_file():
            try:
                receipt = json.loads(self.receipt_path.read_text())
            except (OSError, ValueError, TypeError):
                receipt = {"error": "unreadable suspension receipt"}
        current_reasons: List[str] = []
        if self.manual_path.is_file():
            current_reasons.append("manual_trading_suspension_active")
        if self.enforcement_active:
            reconciliation = {}
            try:
                reconciliation = json.loads(self.reconciliation_path.read_text())
            except (OSError, ValueError, TypeError):
                current_reasons.append("accounting_reconciliation_missing_or_unreadable")
            if reconciliation:
                if str(reconciliation.get("status") or "UNKNOWN") != "VERIFIED":
                    current_reasons.append("accounting_not_verified")
                observed = _parse(reconciliation.get("observed_at"))
                if observed is None or datetime.now(timezone.utc) - observed > self.accounting_max_age:
                    current_reasons.append("accounting_reconciliation_stale")
        return {
            "schema": "tradesight_runtime_risk.v1",
            "enforcement_active": self.enforcement_active,
            "manual_suspension": self.manual_path.is_file(),
            "new_entries_suspended": bool(current_reasons),
            "current_reasons": current_reasons,
            "latest_suspension": receipt or None,
            "exits_always_allowed": True,
            "live_trading_allowed": False,
        }

    def _write_suspension(self, decision: RuntimeRiskDecision) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        current = None
        if self.receipt_path.is_file():
            try:
                current = json.loads(self.receipt_path.read_text())
            except (OSError, ValueError, TypeError):
                current = None
        payload = decision.to_dict()
        payload["changed"] = not current or current.get("reasons") != payload.get("reasons")
        temporary = self.receipt_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.receipt_path)
