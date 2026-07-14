import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from trading.live_readiness import LiveReadinessService


def _policy(root):
    path = root / "ops"
    path.mkdir(parents=True)
    (path / "live_readiness_policy.json").write_text(json.dumps({
        "live_execution_compiled_in": False,
        "requirements": {
            "verified_accounting_sessions": 30,
            "forward_paper_sessions": 60,
            "broker_confirmed_forward_trades": 100,
        },
        "pilot_constraints": {"leverage_allowed": False},
    }))


def _inputs(trades=0):
    accounting = {
        "status": "VERIFIED",
        "observed_at": "2026-07-14T18:00:00+00:00",
        "epoch": {"epoch_id": "paper-test"},
        "live_trading_allowed": False,
        "reconciliation": {"mismatches": []},
        "local": {"trusted_closed_trades": trades, "legacy_unverified_closed_trades": 197},
    }
    strategy = {
        "champion": {"strategy": "RSI Mean Reversion"},
        "candidate": {"auto_promotion": False},
        "latest_optimizer": {
            "quality_gate": {"eligible": False, "reasons": ["OOS failed"]},
            "champion_decision": {"promoted": False, "reason": "candidate rejected"},
        },
    }
    risk = {"mandatory_controls": {
        "paper_only": True,
        "operator_controls_protected": True,
        "csrf_protected": True,
        "audit_chain_valid": True,
        "local_safety_alerts": True,
        "outbound_alert_channel": False,
    }}
    return accounting, strategy, risk


def test_readiness_fails_closed_and_never_enables_live(tmp_path):
    _policy(tmp_path)
    service = LiveReadinessService(tmp_path)
    accounting, strategy, risk = _inputs()
    result = service.snapshot(accounting, strategy, risk)
    assert result["status"] == "NOT_READY"
    assert result["live_trading_allowed"] is False
    assert result["live_execution_compiled_in"] is False
    failed = {gate["gate_id"] for gate in result["gates"] if not gate["passed"]}
    assert {"accounting_streak", "forward_sessions", "forward_trades", "robustness", "outbound_alerts", "failure_drills", "ray_approval"} <= failed


def test_verified_accounting_observation_is_idempotent_per_date(tmp_path):
    _policy(tmp_path)
    service = LiveReadinessService(tmp_path)
    accounting, _, _ = _inputs()
    service.record_accounting_observation(accounting)
    service.record_accounting_observation(accounting)
    assert len((tmp_path / "state" / "accounting-session-history.jsonl").read_text().splitlines()) == 1


def test_mismatch_is_not_recorded(tmp_path):
    _policy(tmp_path)
    service = LiveReadinessService(tmp_path)
    accounting, _, _ = _inputs()
    accounting["reconciliation"]["mismatches"] = [{"kind": "missing_broker"}]
    service.record_accounting_observation(accounting)
    assert not service.history_path.exists()
