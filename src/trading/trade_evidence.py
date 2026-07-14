"""Broker-evidenced paper-trading projection for the TradeSight dashboard.

The projection never upgrades a local row to broker truth by inference. Broker
positions, orders, and fill activities remain separate from preserved local
records, and every row carries an explicit evidence label.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


FINAL_ORDER_STATUSES = {"filled", "closed"}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_trade_evidence_schema(db_path: Path) -> None:
    """Add evidence fields without changing or deleting historical rows."""

    additions = {
        "entry_reason": "TEXT DEFAULT ''",
        "exit_reason": "TEXT DEFAULT ''",
        "entry_confidence": "REAL",
        "market_regime": "TEXT",
        "signal_observed_at": "TEXT",
        "stop_loss_price": "REAL",
        "take_profit_price": "REAL",
        "sizing_reason": "TEXT",
        "signal_snapshot_json": "TEXT",
        "risk_snapshot_json": "TEXT",
    }
    with closing(sqlite3.connect(str(db_path))) as connection, connection as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
        for name, definition in additions.items():
            if name not in columns:
                conn.execute("ALTER TABLE positions ADD COLUMN %s %s" % (name, definition))
        conn.commit()


class TradeEvidenceService:
    """Build a read-only paper-trading view from broker and local evidence."""

    def __init__(self, project_root: Path, broker_client: Any = None):
        self.project_root = Path(project_root)
        self.db_path = self.project_root / "data" / "positions.db"
        self.broker = broker_client

    def snapshot(self, limit: int = 100) -> Dict[str, Any]:
        limit = max(1, min(int(limit or 100), 500))
        ensure_trade_evidence_schema(self.db_path)
        local_rows = self._local_rows(limit)
        broker_positions, position_error = self._broker_call("get_remote_positions", [])
        broker_orders, order_error = self._broker_call("get_orders", [], status="all", limit=limit)
        broker_fills, fill_error = self._broker_call("get_account_activities", [], activity_types="FILL", page_size=limit)

        local_open = [row for row in local_rows if row["status"] == "open"]
        local_closed = [row for row in local_rows if row["status"] == "closed"]
        local_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        for row in local_open:
            local_by_symbol.setdefault(row["symbol"], []).append(row)

        positions = []
        broker_symbols = set()
        for raw in broker_positions or []:
            symbol = str(raw.get("symbol") or "").upper()
            if not symbol:
                continue
            broker_symbols.add(symbol)
            candidates = local_by_symbol.get(symbol) or []
            local = candidates[0] if len(candidates) == 1 else None
            positions.append({
                "symbol": symbol,
                "side": raw.get("side") or ("short" if _float(raw.get("qty")) < 0 else "long"),
                "quantity": abs(_float(raw.get("qty"))),
                "avg_entry_price": _float(raw.get("avg_entry_price")),
                "current_price": _float(raw.get("current_price")),
                "market_value": _float(raw.get("market_value")),
                "unrealized_pnl": _float(raw.get("unrealized_pl")),
                "unrealized_pnl_pct": _float(raw.get("unrealized_plpc")) * 100.0,
                "strategy": local.get("strategy") if local else "UNATTRIBUTED",
                "local_position_id": local.get("id") if local else None,
                "local_match": bool(local),
                "local_match_count": len(candidates),
                "evidence": "BROKER_POSITION_CONFIRMED" if local else "BROKER_ONLY_UNATTRIBUTED",
                "why_available": bool(local),
            })

        local_only = [row for row in local_open if row["symbol"] not in broker_symbols]
        orders = [self._normalize_order(row) for row in (broker_orders or []) if isinstance(row, dict)]
        fills = [self._normalize_fill(row) for row in (broker_fills or []) if isinstance(row, dict)]

        errors = [message for message in (position_error, order_error, fill_error) if message]
        status = "VERIFIED" if not errors and not local_only else "DEGRADED"
        if self.broker is None:
            status = "UNAVAILABLE"
        return {
            "schema": "tradesight_trade_evidence.v1",
            "observed_at": _now_iso(),
            "mode": "paper",
            "status": status,
            "positions": positions,
            "local_only_positions": local_only,
            "orders": orders,
            "fills": fills,
            "closed_trades": local_closed,
            "counts": {
                "broker_positions": len(positions),
                "local_only_positions": len(local_only),
                "broker_orders": len(orders),
                "broker_fills": len(fills),
                "closed_local_records": len(local_closed),
                "broker_verified_closed": sum(1 for row in local_closed if row["evidence"] == "BROKER_VERIFIED"),
            },
            "errors": errors,
            "live_trading_allowed": False,
            "provenance": {
                "kind": status,
                "source": "Alpaca paper positions/orders/fills + local positions.db",
                "observed_at": _now_iso(),
                "message": "Broker facts and local strategy context remain explicitly separated",
            },
        }

    def why_trade(self, position_id: int) -> Optional[Dict[str, Any]]:
        ensure_trade_evidence_schema(self.db_path)
        rows = self._local_rows(500, position_id=position_id)
        if not rows:
            return None
        row = rows[0]
        reasons = []
        if row.get("entry_reason"):
            reasons.append(row["entry_reason"])
        else:
            reasons.append("Original signal detail predates the evidence schema; no reason was inferred")
        return {
            "schema": "tradesight_trade_explanation.v1",
            "position_id": row["id"],
            "symbol": row["symbol"],
            "strategy": row["strategy"],
            "side": row["side"],
            "status": row["status"],
            "decision": {
                "reasons": reasons,
                "confidence": row.get("entry_confidence"),
                "market_regime": row.get("market_regime") or "UNKNOWN",
                "signal_observed_at": row.get("signal_observed_at"),
                "signal_snapshot": row.get("signal_snapshot") or {},
            },
            "risk": {
                "quantity": row["quantity"],
                "entry_price": row["entry_price"],
                "stop_loss_price": row.get("stop_loss_price"),
                "take_profit_price": row.get("take_profit_price"),
                "sizing_reason": row.get("sizing_reason") or "Not recorded",
                "snapshot": row.get("risk_snapshot") or {},
            },
            "broker_evidence": {
                "entry_order_id": row.get("entry_order_id"),
                "entry_fill_status": row.get("entry_fill_status"),
                "exit_order_id": row.get("exit_order_id"),
                "exit_fill_status": row.get("exit_fill_status"),
                "verification_status": row.get("verification_status"),
                "verification_reason": row.get("verification_reason"),
                "verified_at": row.get("verified_at"),
            },
            "outcome": {
                "exit_reason": row.get("exit_reason"),
                "exit_price": row.get("exit_price"),
                "local_realized_pnl": row.get("realized_pnl"),
                "broker_verified_realized_pnl": row.get("verified_realized_pnl"),
            },
            "provenance": row["evidence"],
            "live_trading_allowed": False,
        }

    def _broker_call(self, method_name: str, default: Any, **kwargs):
        if self.broker is None:
            return default, "Authenticated Alpaca paper client unavailable"
        method = getattr(self.broker, method_name, None)
        if method is None:
            return default, "Broker client does not support %s" % method_name
        try:
            result = method(**kwargs)
        except Exception as exc:
            return default, "%s failed: %s" % (method_name, exc)
        if isinstance(result, dict) and result.get("error"):
            return default, "%s failed: %s" % (method_name, result.get("error"))
        return result or default, None

    def _local_rows(self, limit: int, position_id: Optional[int] = None) -> List[Dict[str, Any]]:
        where = "WHERE id=?" if position_id is not None else ""
        params: List[Any] = [int(position_id)] if position_id is not None else []
        params.append(int(limit))
        query = """
            SELECT id,symbol,strategy,side,quantity,entry_price,current_price,entry_time,
                   exit_time,exit_price,unrealized_pnl,realized_pnl,status,
                   entry_order_id,entry_fill_status,exit_order_id,exit_fill_status,
                   verification_status,verification_reason,verified_at,verified_realized_pnl,
                   entry_reason,exit_reason,entry_confidence,market_regime,signal_observed_at,
                   stop_loss_price,take_profit_price,sizing_reason,signal_snapshot_json,risk_snapshot_json
              FROM positions %s
             ORDER BY COALESCE(exit_time,entry_time) DESC LIMIT ?
        """ % where
        with closing(sqlite3.connect(str(self.db_path))) as connection, connection as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        result = []
        for raw in rows:
            row = dict(raw)
            verification = str(row.get("verification_status") or "unknown")
            row["symbol"] = str(row.get("symbol") or "").upper()
            row["evidence"] = {
                "broker_verified": "BROKER_VERIFIED",
                "broker_position_confirmed": "BROKER_POSITION_CONFIRMED",
                "legacy_unverified": "LEGACY_UNVERIFIED",
                "post_epoch_unverified": "POST_EPOCH_UNVERIFIED",
                "broker_position_mismatch": "BROKER_POSITION_MISMATCH",
            }.get(verification, "UNVERIFIED")
            row["signal_snapshot"] = _json(row.pop("signal_snapshot_json", None))
            row["risk_snapshot"] = _json(row.pop("risk_snapshot_json", None))
            row["why_available"] = True
            result.append(row)
        return result

    @staticmethod
    def _normalize_order(row: Dict[str, Any]) -> Dict[str, Any]:
        status = str(row.get("status") or "unknown").lower()
        return {
            "id": row.get("id") or row.get("order_id"),
            "client_order_id": row.get("client_order_id"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "type": row.get("type"),
            "time_in_force": row.get("time_in_force"),
            "quantity": _float(row.get("qty")),
            "filled_quantity": _float(row.get("filled_qty")),
            "filled_avg_price": _float(row.get("filled_avg_price")) or None,
            "status": status,
            "submitted_at": row.get("submitted_at") or row.get("created_at"),
            "filled_at": row.get("filled_at"),
            "canceled_at": row.get("canceled_at"),
            "rejected_at": row.get("failed_at") or row.get("expired_at"),
            "evidence": "BROKER_ORDER_FINAL" if status in FINAL_ORDER_STATUSES else "BROKER_ORDER_NONFINAL",
        }

    @staticmethod
    def _normalize_fill(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": row.get("id") or row.get("transaction_id"),
            "order_id": row.get("order_id"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "quantity": _float(row.get("qty")),
            "price": _float(row.get("price")),
            "transaction_time": row.get("transaction_time") or row.get("date"),
            "activity_type": row.get("activity_type") or "FILL",
            "evidence": "BROKER_FILL_ACTIVITY",
        }
