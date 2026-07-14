import sqlite3
import os
import sys
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from trading.accounting_truth import establish_epoch, reconcile_accounting


def _create_positions_db(root):
    data = root / "data"
    data.mkdir(parents=True)
    db = data / "positions.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                entry_price REAL NOT NULL,
                current_price REAL NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT,
                exit_price REAL,
                realized_pnl REAL DEFAULT 0,
                status TEXT DEFAULT 'open',
                entry_order_id TEXT,
                entry_fill_status TEXT,
                exit_order_id TEXT,
                exit_fill_status TEXT
            )
            """
        )
    return db


def _account(equity="500.00"):
    return {
        "id": "paper-account-test",
        "status": "ACTIVE",
        "equity": equity,
        "cash": "400.00",
        "buying_power": "800.00",
        "long_market_value": "100.00",
        "short_market_value": "0.00",
    }


def test_epoch_preserves_and_excludes_legacy_rows(tmp_path):
    db = _create_positions_db(tmp_path)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO positions(symbol,strategy,side,quantity,entry_price,current_price,entry_time,exit_time,exit_price,realized_pnl,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("OLD", "Legacy", "long", 1, 1, 2, "2020-01-01", "2020-01-02", 2, 666.94, "closed"),
        )
        conn.execute(
            "INSERT INTO positions(symbol,strategy,side,quantity,entry_price,current_price,entry_time,status) VALUES(?,?,?,?,?,?,?,?)",
            ("AAPL", "Champion", "long", 1, 100, 101, "2026-01-01", "open"),
        )

    positions = [{"symbol": "AAPL", "qty": "1", "avg_entry_price": "100", "market_value": "101"}]
    epoch = establish_epoch(tmp_path / "state", _account(), positions)
    assert establish_epoch(tmp_path / "state", _account("999"), []) == epoch

    snapshot = reconcile_accounting(tmp_path, _account(), positions, order_fetcher=lambda _: {})
    assert snapshot["status"] == "VERIFIED"
    assert snapshot["local"]["legacy_unverified_closed_trades"] == 1
    assert snapshot["local"]["legacy_unverified_realized_pnl"] == 666.94
    assert snapshot["local"]["trusted_realized_pnl"] == 0
    assert snapshot["reconciliation"]["matched_symbols"] == ["AAPL"]
    assert snapshot["live_trading_allowed"] is False


def test_post_epoch_trade_requires_two_broker_fills(tmp_path):
    db = _create_positions_db(tmp_path)
    epoch = establish_epoch(tmp_path / "state", _account(), [])
    exit_time = (datetime.fromisoformat(epoch["started_at"]) + timedelta(seconds=1)).isoformat()
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO positions(
                symbol,strategy,side,quantity,entry_price,current_price,entry_time,
                exit_time,exit_price,realized_pnl,status,entry_order_id,
                entry_fill_status,exit_order_id,exit_fill_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("SPY", "Champion", "long", 1, 100, 105, epoch["started_at"], exit_time, 105, 5, "closed", "entry-1", "filled", "exit-1", "filled"),
        )

    snapshot = reconcile_accounting(
        tmp_path,
        _account("505.00"),
        [],
        order_fetcher=lambda order_id: {"id": order_id, "status": "filled", "filled_qty": "1"},
    )
    assert snapshot["status"] == "VERIFIED"
    assert snapshot["local"]["trusted_closed_trades"] == 1
    assert snapshot["local"]["trusted_realized_pnl"] == 5
    assert snapshot["local"]["legacy_unverified_closed_trades"] == 0


def test_open_position_difference_fails_closed(tmp_path):
    db = _create_positions_db(tmp_path)
    establish_epoch(tmp_path / "state", _account(), [])
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO positions(symbol,strategy,side,quantity,entry_price,current_price,entry_time,status) VALUES(?,?,?,?,?,?,?,?)",
            ("AAPL", "Champion", "long", 1, 100, 101, datetime.now(timezone.utc).isoformat(), "open"),
        )
    snapshot = reconcile_accounting(tmp_path, _account(), [], order_fetcher=lambda _: {})
    assert snapshot["status"] == "DEGRADED"
    assert snapshot["reconciliation"]["mismatches"][0]["kind"] == "missing_broker"


def test_epoch_baseline_position_uses_broker_average_and_exit_fill(tmp_path):
    db = _create_positions_db(tmp_path)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO positions(symbol,strategy,side,quantity,entry_price,current_price,entry_time,status,entry_fill_status) VALUES(?,?,?,?,?,?,?,?,?)",
            ("BAC", "Champion", "long", 2, 55, 60, "2026-01-01", "open", "accepted"),
        )
    epoch = establish_epoch(tmp_path / "state", _account(), [
        {"symbol": "BAC", "qty": "2", "avg_entry_price": "60", "market_value": "120"}
    ])
    exit_time = (datetime.fromisoformat(epoch["started_at"]) + timedelta(seconds=1)).isoformat()
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE positions SET status='closed',exit_time=?,exit_price=65,realized_pnl=20,exit_order_id='exit-bac',exit_fill_status='filled' WHERE symbol='BAC'",
            (exit_time,),
        )
    snapshot = reconcile_accounting(
        tmp_path,
        _account("510"),
        [],
        order_fetcher=lambda _: {"status": "filled", "filled_qty": "2", "filled_avg_price": "65"},
    )
    assert snapshot["status"] == "VERIFIED"
    assert snapshot["local"]["trusted_realized_pnl"] == 10
    assert snapshot["local"]["verification_summary"]["broker_verified"]["local_realized_pnl"] == 20
