import json
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from strategy_lab.optimizer_registry import OptimizerRegistry


def _write_fixture(root):
    data = root / "data"
    reports = root / "reports"
    data.mkdir(parents=True)
    reports.mkdir()
    (data / "champion.json").write_text(json.dumps({
        "params": {"oversold": 30},
        "backtest_score": 0.5,
        "live_avg_pnl": 1.2,
        "sessions": 10,
        "promoted_at": "2026-01-01T00:00:00+00:00",
    }))
    db = data / "tournament_history.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE tournament_sessions(session_id TEXT,start_time TEXT,end_time TEXT,status TEXT,winner TEXT,winner_avg_score REAL,total_rounds INTEGER,total_strategies INTEGER,final_survivors INTEGER,results_json TEXT)")
        conn.execute("CREATE TABLE strategy_performance(session_id TEXT,strategy_name TEXT,avg_score REAL,total_score REAL,wins INTEGER,losses INTEGER,rounds_survived INTEGER,eliminated BOOLEAN)")
        conn.execute("INSERT INTO tournament_sessions VALUES(?,?,?,?,?,?,?,?,?,?)", (
            "daily-1", "2026-01-02T00:00:00", "2026-01-02T00:01:00", "completed", "Bollinger Bounce", 0.02, 3, 2, 1,
            json.dumps({"data_source": "real_historical_walkforward"}),
        ))
        conn.execute("INSERT INTO strategy_performance VALUES(?,?,?,?,?,?,?,?)", ("daily-1", "Bollinger Bounce", 0.02, 0.06, 3, 0, 3, 0))
        conn.execute("INSERT INTO strategy_performance VALUES(?,?,?,?,?,?,?,?)", ("daily-1", "RSI Mean Reversion", -0.1, -0.3, 1, 2, 2, 1))
    (reports / "optimization_20260102_000100.json").write_text(json.dumps({
        "timestamp": "2026-01-02T00:01:00",
        "data_source": "alpaca_1h",
        "data_quality": {"promotion_data_ok": True},
        "optimized": {"pnl_pct": 1, "sharpe": 0.2},
        "baseline": {"pnl_pct": 0.5, "sharpe": 0.3},
        "optimizer_target": "RSI Mean Reversion",
        "promotion_quality_gate": {"eligible": False, "reasons": ["OOS failed"]},
        "champion_decision": {"promoted": False, "reason": "OOS failed"},
        "daily_evaluation": {"candidate": {"tournament_winner": "Bollinger Bounce", "status": "frozen_for_forward_evidence", "auto_promotion": False}},
    }))


def test_registry_projects_real_evidence_and_lifecycle(tmp_path):
    _write_fixture(tmp_path)
    snapshot = OptimizerRegistry(tmp_path).snapshot()
    assert snapshot["available"] is True
    assert snapshot["provenance"]["kind"] == "REAL"
    assert snapshot["winner"] == "Bollinger Bounce"
    assert snapshot["champion"]["strategy"] == "RSI Mean Reversion"
    assert snapshot["latest_tournament"]["participants"][0]["name"] == "Bollinger Bounce"
    stages = {item["stage"] for item in snapshot["lifecycle"]}
    assert "CHAMPION" in stages
    assert "FROZEN_FOR_FORWARD_EVIDENCE" in stages
    assert "REJECTED" in stages


def test_registry_ignores_demo_optimizer_report(tmp_path):
    _write_fixture(tmp_path)
    report = tmp_path / "reports" / "optimization_20990101_000000.json"
    report.write_text(json.dumps({
        "data_source": "demo_fallback",
        "data_quality": {"promotion_data_ok": True},
        "optimized": {"pnl_pct": 999},
    }))
    snapshot = OptimizerRegistry(tmp_path).snapshot()
    assert snapshot["latest_optimizer"]["data_source"] == "alpaca_1h"
