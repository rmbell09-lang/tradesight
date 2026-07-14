"""Broker-verified accounting truth for TradeSight paper trading.

This module deliberately separates three things that the legacy dashboard mixed:

* current broker account/position truth;
* post-epoch closes with broker-confirmed entry and exit fills; and
* preserved legacy local rows that are useful evidence but not trusted P&L.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional


FINAL_FILL_STATUSES = {"filled", "closed"}
SCHEMA_VERSION = "tradesight_accounting_truth.v1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


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


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def load_epoch(state_dir: Path) -> Optional[Dict[str, Any]]:
    path = Path(state_dir) / "accounting-epoch.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None
    if data.get("schema") != SCHEMA_VERSION or not data.get("started_at"):
        return None
    return data


def establish_epoch(
    state_dir: Path,
    broker_account: Dict[str, Any],
    broker_positions: Iterable[Dict[str, Any]],
    source: str = "Alpaca paper Trading API",
) -> Dict[str, Any]:
    """Create the immutable baseline for trusted future performance.

    Existing epochs are returned unchanged. This makes the operation idempotent and
    prevents a dashboard refresh from resetting performance history.
    """

    state_dir = Path(state_dir)
    existing = load_epoch(state_dir)
    if existing:
        return existing

    equity = broker_account.get("equity") or broker_account.get("portfolio_value")
    if equity in (None, ""):
        raise ValueError("Broker account did not provide equity; epoch was not created")

    account_id = str(broker_account.get("account_number") or broker_account.get("id") or "paper")
    account_ref = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:12]
    started_at = _now()
    positions = []
    for item in broker_positions or []:
        positions.append({
            "symbol": item.get("symbol"),
            "qty": _float(item.get("qty")),
            "avg_entry_price": _float(item.get("avg_entry_price")),
            "market_value": _float(item.get("market_value")),
        })

    payload = {
        "schema": SCHEMA_VERSION,
        "epoch_id": "paper-" + started_at.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8],
        "started_at": _iso(started_at),
        "mode": "paper",
        "source": source,
        "account_ref": account_ref,
        "baseline": {
            "equity": _float(equity),
            "cash": _float(broker_account.get("cash")),
            "buying_power": _float(broker_account.get("buying_power")),
            "long_market_value": _float(broker_account.get("long_market_value")),
            "short_market_value": _float(broker_account.get("short_market_value")),
            "positions": positions,
        },
        "policy": {
            "legacy_rows": "preserved_but_excluded",
            "trusted_realized_pnl": "post_epoch_entry_and_exit_orders_broker_confirmed_filled",
            "live_trading_allowed": False,
        },
    }
    _write_json(state_dir / "accounting-epoch.json", payload)
    return payload


def ensure_schema(db_path: Path, epoch: Dict[str, Any]) -> None:
    """Add non-destructive evidence columns and classify preserved rows."""

    db_path = Path(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
        additions = {
            "accounting_epoch_id": "TEXT",
            "verification_status": "TEXT DEFAULT 'pending_broker_reconciliation'",
            "verification_reason": "TEXT",
            "verified_at": "TEXT",
            "verified_realized_pnl": "REAL",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute("ALTER TABLE positions ADD COLUMN %s %s" % (name, definition))

        epoch_id = epoch["epoch_id"]
        started_at = epoch["started_at"]
        conn.execute(
            """
            UPDATE positions
               SET verification_status='legacy_unverified',
                   verification_reason='Predates broker-verified accounting epoch; preserved but excluded',
                   accounting_epoch_id=NULL,
                   verified_at=NULL
             WHERE status='closed'
               AND (exit_time IS NULL OR exit_time < ?)
               AND COALESCE(verification_status, '') != 'broker_verified'
            """,
            (started_at,),
        )
        conn.execute(
            """
            UPDATE positions
               SET verification_status='post_epoch_unverified',
                   verification_reason='Awaiting broker-confirmed entry and exit fills',
                   accounting_epoch_id=?
             WHERE status='closed'
               AND exit_time >= ?
               AND COALESCE(verification_status, '') != 'broker_verified'
            """,
            (epoch_id, started_at),
        )
        conn.execute(
            """
            UPDATE positions
               SET verification_status='pending_broker_reconciliation',
                   verification_reason='Open local row awaiting current broker-position match',
                   accounting_epoch_id=?
             WHERE status='open'
               AND COALESCE(verification_status, '') NOT IN ('broker_position_confirmed')
            """,
            (epoch_id,),
        )
        conn.commit()


def _remote_position_map(remote_positions: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    result: Dict[str, Dict[str, float]] = {}
    for row in remote_positions or []:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        result[symbol] = {
            "quantity": _float(row.get("qty")),
            "avg_entry_price": _float(row.get("avg_entry_price")),
            "current_price": _float(row.get("current_price")),
            "market_value": _float(row.get("market_value")),
            "unrealized_pnl": _float(row.get("unrealized_pl")),
        }
    return result


def _filled_order(order: Dict[str, Any]) -> bool:
    return bool(
        isinstance(order, dict)
        and str(order.get("status") or "").lower() in FINAL_FILL_STATUSES
        and _float(order.get("filled_qty"), 1.0) > 0
    )


def _order_fill_price(order: Dict[str, Any]) -> Optional[float]:
    for key in ("filled_avg_price", "avg_fill_price", "fill_price"):
        value = order.get(key) if isinstance(order, dict) else None
        if value not in (None, ""):
            price = _float(value)
            if price > 0:
                return price
    return None


def reconcile_accounting(
    project_root: Path,
    broker_account: Dict[str, Any],
    broker_positions: Iterable[Dict[str, Any]],
    order_fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Reconcile local rows against current paper-broker truth and write a receipt."""

    project_root = Path(project_root)
    state_dir = project_root / "state"
    db_path = project_root / "data" / "positions.db"
    epoch = load_epoch(state_dir)
    if not epoch:
        return {
            "schema": SCHEMA_VERSION,
            "status": "EPOCH_REQUIRED",
            "mode": "paper",
            "live_trading_allowed": False,
            "error": "No broker-verified accounting epoch exists",
        }
    if not broker_account or broker_account.get("equity") in (None, ""):
        return {
            "schema": SCHEMA_VERSION,
            "status": "UNAVAILABLE",
            "mode": "paper",
            "epoch": epoch,
            "live_trading_allowed": False,
            "error": "Broker account truth is unavailable",
        }

    ensure_schema(db_path, epoch)
    remote = _remote_position_map(broker_positions)
    epoch_positions = {
        str(item.get("symbol") or "").upper(): item
        for item in (epoch.get("baseline", {}).get("positions") or [])
        if item.get("symbol")
    }
    observed_at = _iso(_now())
    epoch_started = _parse(epoch["started_at"])
    order_cache: Dict[str, Dict[str, Any]] = {}

    def fetch_order(order_id: str) -> Dict[str, Any]:
        if not order_id or order_id.startswith("alpaca_close_position:") or not order_fetcher:
            return {}
        if order_id not in order_cache:
            try:
                order_cache[order_id] = order_fetcher(order_id) or {}
            except Exception as exc:  # fail closed; caller records the reason
                order_cache[order_id] = {"error": str(exc)}
        return order_cache[order_id]

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        local_open_rows = conn.execute(
            "SELECT id,symbol,side,quantity,entry_price,current_price,strategy FROM positions WHERE status='open'"
        ).fetchall()
        local_by_symbol: Dict[str, Dict[str, Any]] = {}
        for row in local_open_rows:
            symbol = str(row["symbol"]).upper()
            signed_qty = _float(row["quantity"]) * (-1.0 if str(row["side"]).lower() == "short" else 1.0)
            item = local_by_symbol.setdefault(symbol, {"quantity": 0.0, "row_ids": [], "strategies": []})
            item["quantity"] += signed_qty
            item["row_ids"].append(int(row["id"]))
            item["strategies"].append(row["strategy"])

        mismatches: List[Dict[str, Any]] = []
        all_symbols = sorted(set(local_by_symbol) | set(remote))
        matched_symbols = []
        for symbol in all_symbols:
            local_qty = _float(local_by_symbol.get(symbol, {}).get("quantity"))
            remote_qty = _float(remote.get(symbol, {}).get("quantity"))
            tolerance = max(0.000001, abs(remote_qty) * 0.001)
            if symbol not in local_by_symbol:
                mismatches.append({"symbol": symbol, "kind": "missing_local", "broker_quantity": remote_qty})
            elif symbol not in remote:
                mismatches.append({"symbol": symbol, "kind": "missing_broker", "local_quantity": local_qty})
            elif abs(local_qty - remote_qty) > tolerance:
                mismatches.append({
                    "symbol": symbol,
                    "kind": "quantity_mismatch",
                    "local_quantity": local_qty,
                    "broker_quantity": remote_qty,
                })
            else:
                matched_symbols.append(symbol)

        for symbol, local in local_by_symbol.items():
            matched = symbol in matched_symbols
            conn.execute(
                """
                UPDATE positions
                   SET verification_status=?, verification_reason=?, accounting_epoch_id=?, verified_at=?
                 WHERE status='open' AND UPPER(symbol)=?
                """,
                (
                    "broker_position_confirmed" if matched else "broker_position_mismatch",
                    "Current quantity matches broker paper position" if matched else "Current local row does not match broker paper position",
                    epoch["epoch_id"],
                    observed_at if matched else None,
                    symbol,
                ),
            )

        closed_rows = conn.execute(
            """
            SELECT id,symbol,side,quantity,entry_price,exit_price,realized_pnl,exit_time,
                   entry_order_id,entry_fill_status,exit_order_id,exit_fill_status
              FROM positions WHERE status='closed'
            """
        ).fetchall()
        for row in closed_rows:
            exit_time = _parse(row["exit_time"])
            if not exit_time or not epoch_started or exit_time < epoch_started:
                continue
            entry_id = str(row["entry_order_id"] or "")
            exit_id = str(row["exit_order_id"] or "")
            entry_status = str(row["entry_fill_status"] or "").lower()
            exit_status = str(row["exit_fill_status"] or "").lower()
            reason = ""
            verified = False
            verified_pnl = None
            if entry_id and exit_id and entry_status in FINAL_FILL_STATUSES and exit_status in FINAL_FILL_STATUSES:
                entry_order = fetch_order(entry_id)
                exit_order = fetch_order(exit_id)
                verified = _filled_order(entry_order) and _filled_order(exit_order)
                reason = (
                    "Entry and exit fills confirmed by broker order API"
                    if verified
                    else "Broker order API did not confirm both entry and exit fills"
                )
                if verified:
                    entry_fill = _order_fill_price(entry_order) or _float(row["entry_price"])
                    exit_fill = _order_fill_price(exit_order) or _float(row["exit_price"])
                    qty = _float(row["quantity"])
                    verified_pnl = (
                        (entry_fill - exit_fill) * qty
                        if str(row["side"]).lower() == "short"
                        else (exit_fill - entry_fill) * qty
                    )
            elif exit_id and exit_status in FINAL_FILL_STATUSES and str(row["symbol"]).upper() in epoch_positions:
                # A position that was already open when the epoch was established has
                # broker-confirmed baseline quantity/average price instead of an entry
                # order id. A later broker-confirmed exit is enough to verify its P&L.
                baseline = epoch_positions[str(row["symbol"]).upper()]
                qty = _float(row["quantity"])
                baseline_qty = abs(_float(baseline.get("qty")))
                tolerance = max(0.000001, baseline_qty * 0.001)
                exit_order = fetch_order(exit_id)
                verified = abs(abs(qty) - baseline_qty) <= tolerance and _filled_order(exit_order)
                if verified:
                    entry_fill = _float(baseline.get("avg_entry_price"))
                    exit_fill = _order_fill_price(exit_order) or _float(row["exit_price"])
                    verified_pnl = (
                        (entry_fill - exit_fill) * abs(qty)
                        if str(row["side"]).lower() == "short"
                        else (exit_fill - entry_fill) * abs(qty)
                    )
                    reason = "Epoch baseline broker position plus broker-confirmed exit fill"
                else:
                    reason = "Epoch baseline quantity or broker exit fill did not verify"
            elif not entry_id or not exit_id:
                reason = "Missing entry or exit broker order identifier"
            else:
                reason = "Local entry or exit status is not final-filled"
            conn.execute(
                """
                UPDATE positions
                   SET verification_status=?, verification_reason=?, accounting_epoch_id=?,
                       verified_at=?, verified_realized_pnl=?
                 WHERE id=?
                """,
                (
                    "broker_verified" if verified else "post_epoch_unverified",
                    reason,
                    epoch["epoch_id"],
                    observed_at if verified else None,
                    round(verified_pnl, 8) if verified_pnl is not None else None,
                    int(row["id"]),
                ),
            )

        summaries = conn.execute(
            """
            SELECT COALESCE(verification_status,'unknown') AS verification_status,
                   COUNT(*) AS count,
                   COALESCE(SUM(CASE WHEN status='closed' THEN realized_pnl ELSE 0 END),0) AS local_realized_pnl,
                   COALESCE(SUM(CASE WHEN status='closed' THEN verified_realized_pnl ELSE 0 END),0) AS verified_realized_pnl
              FROM positions
             GROUP BY COALESCE(verification_status,'unknown')
            """
        ).fetchall()
        recent = conn.execute(
            """
            SELECT id,symbol,strategy,side,quantity,entry_price,exit_price,realized_pnl,
                   verified_realized_pnl,
                   entry_time,exit_time,verification_status,verification_reason
              FROM positions WHERE status='closed'
             ORDER BY COALESCE(exit_time,entry_time) DESC LIMIT 20
            """
        ).fetchall()
        conn.commit()

    summary_map = {
        row["verification_status"]: {
            "count": int(row["count"]),
            "local_realized_pnl": round(_float(row["local_realized_pnl"]), 6),
            "verified_realized_pnl": round(_float(row["verified_realized_pnl"]), 6),
        }
        for row in summaries
    }
    empty_summary = {"count": 0, "local_realized_pnl": 0.0, "verified_realized_pnl": 0.0}
    trusted = summary_map.get("broker_verified", empty_summary)
    legacy = summary_map.get("legacy_unverified", empty_summary)
    post_epoch_unverified = summary_map.get("post_epoch_unverified", empty_summary)

    current_equity = _float(broker_account.get("equity") or broker_account.get("portfolio_value"))
    baseline_equity = _float(epoch.get("baseline", {}).get("equity"))
    status = "VERIFIED"
    blockers = []
    if mismatches:
        blockers.append("Local and broker open positions do not match")
    if post_epoch_unverified["count"]:
        blockers.append("Post-epoch closed rows are missing complete broker fill proof")
    if blockers:
        status = "DEGRADED"

    payload = {
        "schema": SCHEMA_VERSION,
        "snapshot_id": uuid.uuid4().hex,
        "observed_at": observed_at,
        "status": status,
        "mode": "paper",
        "source": "Alpaca paper Trading API + local positions.db evidence",
        "epoch": epoch,
        "broker": {
            "status": broker_account.get("status"),
            "equity": current_equity,
            "cash": _float(broker_account.get("cash")),
            "buying_power": _float(broker_account.get("buying_power")),
            "long_market_value": _float(broker_account.get("long_market_value")),
            "short_market_value": _float(broker_account.get("short_market_value")),
            "equity_change_since_epoch": round(current_equity - baseline_equity, 6),
            "positions": remote,
        },
        "local": {
            "open_positions": local_by_symbol,
            "verification_summary": summary_map,
            "trusted_realized_pnl": trusted["verified_realized_pnl"],
            "trusted_closed_trades": trusted["count"],
            "legacy_unverified_realized_pnl": legacy["local_realized_pnl"],
            "legacy_unverified_closed_trades": legacy["count"],
            "post_epoch_unverified": post_epoch_unverified,
            "recent_closed_records": [dict(row) for row in recent],
        },
        "reconciliation": {
            "matched_symbols": matched_symbols,
            "mismatches": mismatches,
            "blockers": blockers,
        },
        "live_trading_allowed": False,
    }
    _write_json(state_dir / "accounting-reconciliation.json", payload)
    return payload
