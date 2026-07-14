"""Read-only registry for the real TradeSight optimizer evidence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = "tradesight_strategy_registry.v1"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return {}


class OptimizerRegistry:
    """Projector over champion.json, real tournament DB, and optimizer reports."""

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        self.data_dir = self.project_root / "data"
        self.reports_dir = self.project_root / "reports"

    def champion(self) -> Dict[str, Any]:
        raw = _read_json(self.data_dir / "champion.json")
        if not raw:
            return {}
        return {
            "strategy": raw.get("strategy") or raw.get("strategy_name") or "RSI Mean Reversion",
            "stage": "CHAMPION",
            "parameters": raw.get("params") or {},
            "backtest_score": raw.get("backtest_score"),
            "forward_avg_pnl": raw.get("live_avg_pnl"),
            "forward_sessions": raw.get("sessions"),
            "promoted_at": raw.get("promoted_at"),
            "promotion_reason": raw.get("promotion_reason"),
            "source": "data/champion.json",
        }

    def latest_full_report(self) -> Dict[str, Any]:
        if not self.reports_dir.is_dir():
            return {}
        for path in sorted(self.reports_dir.glob("optimization_*.json"), reverse=True):
            report = _read_json(path)
            source = str(report.get("data_source") or "")
            quality = report.get("data_quality") or {}
            if report.get("optimized") and source and "demo" not in source.lower() and quality.get("promotion_data_ok"):
                report["_path"] = str(path.relative_to(self.project_root))
                return report
        return {}

    def latest_evaluation_report(self) -> Dict[str, Any]:
        """Return the newest real daily evaluation, including no-search days."""
        if not self.reports_dir.is_dir():
            return {}
        for path in sorted(self.reports_dir.glob("optimization_*.json"), reverse=True):
            report = _read_json(path)
            daily = report.get("daily_evaluation") or {}
            market = daily.get("market_evidence") or {}
            tournament = daily.get("tournament") or {}
            if (
                isinstance(daily, dict)
                and _safe_int(market.get("symbols_verified")) > 0
                and "demo" not in str(tournament.get("data_source") or "").lower()
            ):
                report["_path"] = str(path.relative_to(self.project_root))
                return report
        return {}

    def _connect(self):
        path = self.data_dir / "tournament_history.db"
        if not path.is_file():
            return None
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        return conn

    def latest_tournament(self) -> Dict[str, Any]:
        conn = self._connect()
        if conn is None:
            return {}
        try:
            row = conn.execute(
                """
                SELECT session_id,start_time,end_time,status,winner,winner_avg_score,
                       total_rounds,total_strategies,final_survivors,results_json
                  FROM tournament_sessions
                 WHERE status='completed' AND session_id IS NOT NULL
                 ORDER BY start_time DESC LIMIT 1
                """
            ).fetchone()
            if not row:
                return {}
            participants = [dict(item) for item in conn.execute(
                """
                SELECT strategy_name AS name,avg_score,total_score,wins,losses,
                       rounds_survived,eliminated
                  FROM strategy_performance WHERE session_id=?
                 ORDER BY avg_score DESC
                """,
                (row["session_id"],),
            ).fetchall()]
            result_json = {}
            try:
                result_json = json.loads(row["results_json"] or "{}")
            except (ValueError, TypeError):
                pass
            return {
                "session_id": row["session_id"],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "status": row["status"],
                "winner": row["winner"],
                "winner_avg_score": row["winner_avg_score"],
                "rounds_completed": row["total_rounds"],
                "strategies_tested": row["total_strategies"],
                "final_survivors": row["final_survivors"],
                "participants": participants,
                "data_source": result_json.get("data_source") or "real_historical_walkforward",
            }
        finally:
            conn.close()

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                """
                SELECT session_id,start_time,status,winner,winner_avg_score,
                       total_rounds,total_strategies,final_survivors,results_json
                  FROM tournament_sessions
                 WHERE status='completed' AND session_id IS NOT NULL
                 ORDER BY start_time DESC LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
            history = []
            for row in rows:
                data_source = "real_historical_walkforward"
                try:
                    data_source = json.loads(row["results_json"] or "{}").get("data_source") or data_source
                except (ValueError, TypeError):
                    pass
                history.append({
                    "session_id": row["session_id"],
                    "timestamp": row["start_time"],
                    "status": row["status"],
                    "winner": row["winner"],
                    "winner_avg_score": row["winner_avg_score"],
                    "rounds_completed": row["total_rounds"],
                    "participants_count": row["total_strategies"],
                    "final_survivors": row["final_survivors"],
                    "data_source": data_source,
                })
            return history
        finally:
            conn.close()

    def snapshot(self) -> Dict[str, Any]:
        champion = self.champion()
        tournament = self.latest_tournament()
        report = self.latest_full_report()
        evaluation = self.latest_evaluation_report()
        daily = evaluation.get("daily_evaluation") or report.get("daily_evaluation") or {}
        candidate_data = daily.get("candidate") or {}
        quality_gate = report.get("promotion_quality_gate") or {}
        decision = report.get("champion_decision") or {}
        if not isinstance(candidate_data, dict):
            candidate_data = {}
        if not isinstance(quality_gate, dict):
            quality_gate = {}
        if not isinstance(decision, dict):
            decision = {"promoted": False, "reason": str(decision)}

        candidate_name = candidate_data.get("tournament_winner") or tournament.get("winner")
        candidate_stage = str(candidate_data.get("status") or "CANDIDATE").upper()
        lifecycle: List[Dict[str, Any]] = []
        if champion:
            lifecycle.append({
                "strategy": champion["strategy"],
                "stage": "CHAMPION",
                "evidence": "Active frozen parameters from champion.json",
                "observed_at": champion.get("promoted_at"),
            })
        if candidate_name and candidate_name != champion.get("strategy"):
            lifecycle.append({
                "strategy": candidate_name,
                "stage": candidate_stage,
                "evidence": "Latest real-data strategy-family tournament",
                "observed_at": daily.get("timestamp") or tournament.get("end_time"),
            })
        optimizer_target = report.get("optimizer_target")
        if optimizer_target:
            promoted = bool(decision.get("promoted"))
            stage = "CHAMPION" if promoted else ("REJECTED" if quality_gate.get("eligible") is False else "BACKTEST")
            lifecycle.append({
                "strategy": optimizer_target,
                "stage": stage,
                "evidence": decision.get("reason") or "; ".join(quality_gate.get("reasons") or []) or "Optimizer quality gate",
                "observed_at": report.get("timestamp"),
            })

        available = bool(champion and tournament and report)
        source_kind = "REAL" if available else "UNAVAILABLE"
        return {
            "schema": SCHEMA_VERSION,
            "available": available,
            "strategies_tested": tournament.get("strategies_tested", 0),
            "winner": tournament.get("winner"),
            "winner_score": tournament.get("winner_avg_score"),
            "rounds_completed": tournament.get("rounds_completed", 0),
            "last_run": tournament.get("end_time"),
            "champion": champion,
            "candidate": {
                "strategy": candidate_name,
                "stage": candidate_stage,
                "auto_promotion": bool(candidate_data.get("auto_promotion", False)),
            },
            "latest_tournament": tournament,
            "history": self.history(),
            "latest_optimizer": {
                "report_path": report.get("_path"),
                "timestamp": report.get("timestamp"),
                "data_source": report.get("data_source"),
                "symbols_tested": report.get("symbols_tested") or [],
                "optimized": report.get("optimized") or {},
                "baseline": report.get("baseline") or {},
                "quality_gate": quality_gate,
                "champion_decision": decision,
                "walk_forward": report.get("walk_forward") or {},
                "regime": report.get("regime_report") or {},
            },
            "latest_evaluation": {
                "report_path": evaluation.get("_path"),
                "timestamp": evaluation.get("timestamp") or daily.get("timestamp"),
                "market_evidence": daily.get("market_evidence") or {},
                "full_search_gate": daily.get("full_search_gate") or {},
                "champion_decision": evaluation.get("champion_decision"),
            },
            "lifecycle": lifecycle,
            "message": (
                "Real optimizer, tournament, and champion evidence connected"
                if available
                else "One or more real optimizer evidence sources are unavailable"
            ),
            "provenance": {
                "kind": source_kind,
                "source": "tournament_history.db + champion.json + real optimization report",
                "observed_at": evaluation.get("timestamp") or report.get("timestamp") or tournament.get("end_time"),
                "message": "Synthetic results are excluded",
            },
            "live_trading_allowed": False,
        }


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
