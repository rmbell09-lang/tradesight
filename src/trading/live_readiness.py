"""Evidence-backed live-trading readiness projection.

This module is intentionally read-mostly. It measures readiness; it does not
enable live execution, accept approval, store credentials, or place orders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, Optional


SCHEMA = "tradesight_live_readiness.v1"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, TypeError, ValueError):
        return {}


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
class ReadinessGate:
    gate_id: str
    label: str
    passed: bool
    current: Any
    required: Any
    evidence: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LiveReadinessService:
    """Build a fail-closed status from current broker, strategy, and risk proof."""

    def __init__(self, project_root: Path):
        self.root = Path(project_root)
        self.state = self.root / "state"
        self.policy = _read_json(self.root / "ops" / "live_readiness_policy.json")
        self.history_path = self.state / "accounting-session-history.jsonl"

    def record_accounting_observation(self, accounting: Dict[str, Any]) -> None:
        """Record at most one verified reconciliation observation per UTC date."""
        if accounting.get("status") != "VERIFIED":
            return
        if (accounting.get("reconciliation") or {}).get("mismatches"):
            return
        observed = _parse(accounting.get("observed_at"))
        if observed is None:
            return
        record = {
            "schema": "tradesight_accounting_session.v1",
            "session_date": observed.date().isoformat(),
            "observed_at": observed.isoformat(),
            "status": "VERIFIED",
            "mismatch_count": 0,
            "epoch_id": (accounting.get("epoch") or {}).get("epoch_id"),
        }
        existing = {item.get("session_date") for item in self._history()}
        if record["session_date"] in existing:
            return
        self.state.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def _history(self) -> list[Dict[str, Any]]:
        if not self.history_path.is_file():
            return []
        rows = []
        for line in self.history_path.read_text().splitlines():
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            if item.get("status") == "VERIFIED" and item.get("mismatch_count") == 0:
                rows.append(item)
        return rows

    def _verified_dates(self) -> list[date]:
        values = set()
        for item in self._history():
            try:
                values.add(date.fromisoformat(item["session_date"]))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(values)

    def snapshot(
        self,
        accounting: Dict[str, Any],
        strategy: Dict[str, Any],
        risk: Dict[str, Any],
    ) -> Dict[str, Any]:
        requirements = self.policy.get("requirements") or {}
        dates = self._verified_dates()
        local = accounting.get("local") or {}
        reconciliation = accounting.get("reconciliation") or {}
        quality = (strategy.get("latest_optimizer") or {}).get("quality_gate") or {}
        decision = (strategy.get("latest_optimizer") or {}).get("champion_decision") or {}
        champion = strategy.get("champion") or {}
        controls = risk.get("mandatory_controls") or {}
        drills = _read_json(self.state / "live-failure-drills.json")
        approval = _read_json(self.state / "live-pilot-approval.json")
        runtime_ok = sys.version_info >= (3, 11)
        accounting_ok = accounting.get("status") == "VERIFIED" and not reconciliation.get("mismatches")
        trusted_trades = int(local.get("trusted_closed_trades") or 0)
        required_accounting = int(requirements.get("verified_accounting_sessions") or 30)
        required_forward = int(requirements.get("forward_paper_sessions") or 60)
        required_trades = int(requirements.get("broker_confirmed_forward_trades") or 100)
        optimizer_passed = quality.get("eligible") is True and decision.get("promoted") is True
        controls_ok = all(bool(controls.get(key)) for key in (
            "paper_only",
            "operator_controls_protected",
            "csrf_protected",
            "audit_chain_valid",
            "local_safety_alerts",
        ))
        approval_ok = bool(
            approval.get("ray_approved")
            and approval.get("approved_strategy")
            and approval.get("max_notional_per_trade")
            and approval.get("max_daily_loss")
            and approval.get("max_total_pilot_loss")
        )
        gates = [
            ReadinessGate("supported_runtime", "Supported isolated runtime", runtime_ok, sys.version.split()[0], ">=3.11", "running Python interpreter"),
            ReadinessGate("release_lock", "Live execution absent from this release", self.policy.get("live_execution_compiled_in") is False, "PAPER_ONLY", "live execution compiled out", "ops/live_readiness_policy.json"),
            ReadinessGate("accounting_current", "Current broker/local accounting reconciles", accounting_ok, accounting.get("status") or "UNAVAILABLE", "VERIFIED with zero mismatches", "accounting reconciliation"),
            ReadinessGate("accounting_streak", "Verified accounting-session streak", len(dates) >= required_accounting, len(dates), required_accounting, "state/accounting-session-history.jsonl"),
            ReadinessGate("legacy_excluded", "Legacy P&L excluded from trusted performance", bool(accounting.get("epoch")) and accounting.get("live_trading_allowed") is False, local.get("legacy_unverified_closed_trades", 0), "preserved but excluded", "accounting epoch policy"),
            ReadinessGate("forward_sessions", "Forward paper sessions", len(dates) >= required_forward, len(dates), required_forward, "verified post-epoch session dates"),
            ReadinessGate("forward_trades", "Broker-confirmed forward paper trades", trusted_trades >= required_trades, trusted_trades, required_trades, "post-epoch entry and exit fills"),
            ReadinessGate("strategy_frozen", "One frozen champion strategy", bool(champion.get("strategy")) and strategy.get("candidate", {}).get("auto_promotion") is False, champion.get("strategy") or "missing", "frozen champion; no auto-promotion", "strategy registry"),
            ReadinessGate("robustness", "Qualification and robustness gates", optimizer_passed, decision.get("reason") or quality.get("reasons") or "no passing receipt", "eligible and promoted qualification receipt", "latest real optimizer report"),
            ReadinessGate("risk_controls", "Protected risk controls", controls_ok, sum(bool(controls.get(key)) for key in controls), "paper lock + auth + CSRF + audit + local alerts", "risk posture"),
            ReadinessGate("outbound_alerts", "Tested outbound safety alert channel", bool(controls.get("outbound_alert_channel")), bool(controls.get("outbound_alert_channel")), True, "risk posture"),
            ReadinessGate("failure_drills", "Failure and restart drills", drills.get("status") == "PASSED" and drills.get("all_required_passed") is True, drills.get("status") or "NO RECEIPT", "PASSED", "state/live-failure-drills.json"),
            ReadinessGate("ray_approval", "Ray approval and hard loss limits", approval_ok, "APPROVED" if approval_ok else "NOT APPROVED", "explicit strategy + per-trade + daily + total loss limits", "state/live-pilot-approval.json"),
        ]
        passed = sum(gate.passed for gate in gates)
        mandatory_ready = passed == len(gates)
        return {
            "schema": SCHEMA,
            "status": "READY_FOR_SEPARATE_CANARY_BUILD_REVIEW" if mandatory_ready else "NOT_READY",
            "mode": "PAPER_ONLY",
            "live_trading_allowed": False,
            "live_execution_compiled_in": False,
            "passed_gates": passed,
            "total_gates": len(gates),
            "gates": [gate.to_dict() for gate in gates],
            "pilot_constraints": self.policy.get("pilot_constraints") or {},
            "scale_review": {
                "minimum_clean_live_trades": requirements.get("minimum_clean_live_canary_trades_before_scale_review", 20),
                "maximum_clean_live_trades": requirements.get("maximum_clean_live_canary_trades_before_scale_review", 50),
                "automatic_scaling": False,
            },
            "next_blockers": [gate.label for gate in gates if not gate.passed],
            "message": (
                "Evidence gates passed; a separate explicitly authorized canary build review is still required"
                if mandatory_ready
                else "Live trading remains technically locked until every evidence gate passes"
            ),
        }


def evaluate_readiness(
    project_root: Path,
    accounting: Dict[str, Any],
    strategy: Dict[str, Any],
    risk: Dict[str, Any],
) -> Dict[str, Any]:
    return LiveReadinessService(project_root).snapshot(accounting, strategy, risk)
