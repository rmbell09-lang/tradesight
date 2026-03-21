#!/usr/bin/env python3
"""
Integration test: TradeSight Dynamic Balance Sync (task 886)

Tests:
1. balance_cache table is created on DB init
2. portfolio_history has buying_power + balance_synced_at columns
3. persist_balance_sync() writes to balance_cache correctly
4. get_portfolio_state() returns buying_power from cache
5. buying_power survives a second PositionManager instantiation (persistence)
6. Upsert: calling persist_balance_sync() twice updates (no duplicate rows)
"""
import sqlite3
import tempfile
import shutil
import pytest
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading.position_manager import PositionManager


@pytest.fixture
def pm_dir():
    """Fresh temp dir with initialized PositionManager"""
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d)


@pytest.fixture
def pm(pm_dir):
    return PositionManager(base_dir=pm_dir)


def db_path(pm_dir):
    return Path(pm_dir) / 'data' / 'positions.db'


class TestBalanceSyncSchema:
    """Schema migration tests"""

    def test_balance_cache_table_exists(self, pm, pm_dir):
        with sqlite3.connect(db_path(pm_dir)) as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert 'balance_cache' in tables

    def test_portfolio_history_has_buying_power_column(self, pm, pm_dir):
        with sqlite3.connect(db_path(pm_dir)) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(portfolio_history)").fetchall()]
        assert 'buying_power' in cols

    def test_portfolio_history_has_balance_synced_at_column(self, pm, pm_dir):
        with sqlite3.connect(db_path(pm_dir)) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(portfolio_history)").fetchall()]
        assert 'balance_synced_at' in cols


class TestPersistBalanceSync:
    """persist_balance_sync() method tests"""

    def test_persist_returns_true(self, pm):
        assert pm.persist_balance_sync(1234.56) is True

    def test_persist_writes_buying_power(self, pm, pm_dir):
        pm.persist_balance_sync(999.99)
        with sqlite3.connect(db_path(pm_dir)) as conn:
            row = conn.execute("SELECT buying_power FROM balance_cache WHERE id=1").fetchone()
        assert row is not None
        assert abs(row[0] - 999.99) < 0.01

    def test_persist_writes_synced_at(self, pm, pm_dir):
        pm.persist_balance_sync(500.0)
        with sqlite3.connect(db_path(pm_dir)) as conn:
            row = conn.execute("SELECT synced_at FROM balance_cache WHERE id=1").fetchone()
        assert row is not None
        assert row[0] is not None and len(row[0]) > 10  # ISO timestamp

    def test_upsert_updates_existing_row(self, pm, pm_dir):
        pm.persist_balance_sync(100.0)
        pm.persist_balance_sync(200.0)
        with sqlite3.connect(db_path(pm_dir)) as conn:
            rows = conn.execute("SELECT buying_power FROM balance_cache").fetchall()
        # Must remain 1 row (upsert, not insert)
        assert len(rows) == 1
        assert abs(rows[0][0] - 200.0) < 0.01


class TestGetPortfolioStateWithBalance:
    """get_portfolio_state() integration with balance_cache"""

    def test_buying_power_none_before_sync(self, pm):
        state = pm.get_portfolio_state()
        assert state.buying_power is None

    def test_buying_power_populated_after_sync(self, pm):
        pm.persist_balance_sync(750.25)
        state = pm.get_portfolio_state()
        assert state.buying_power is not None
        assert abs(state.buying_power - 750.25) < 0.01

    def test_balance_synced_at_populated_after_sync(self, pm):
        pm.persist_balance_sync(500.0)
        state = pm.get_portfolio_state()
        assert state.balance_synced_at is not None

    def test_buying_power_persists_across_instantiation(self, pm_dir):
        """Balance survives PositionManager restart (DB persistence)"""
        pm1 = PositionManager(base_dir=pm_dir)
        pm1.persist_balance_sync(888.88)

        pm2 = PositionManager(base_dir=pm_dir)
        state = pm2.get_portfolio_state()
        assert state.buying_power is not None
        assert abs(state.buying_power - 888.88) < 0.01

    def test_portfolio_state_available_cash_unaffected(self, pm):
        """available_cash (calculated from positions) still works independently"""
        pm.persist_balance_sync(1000.0)
        state = pm.get_portfolio_state()
        # No positions — available_cash == initial_balance
        assert state.available_cash == pytest.approx(500.0, abs=0.01)
