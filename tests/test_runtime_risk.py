import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading.runtime_risk import OperationalRiskGate


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_new_entries_require_fresh_verified_accounting_after_epoch():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_json(root / 'state' / 'accounting-epoch.json', {'started_at': '2026-07-14T00:00:00Z'})
        gate = OperationalRiskGate(root)
        decision = gate.evaluate('buy', datetime.now(timezone.utc).isoformat())
        assert decision.allowed is False
        assert 'accounting_reconciliation_missing_or_unreadable' in decision.reasons


def test_fresh_verified_accounting_and_signal_allow_paper_entry():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        now = datetime.now(timezone.utc)
        write_json(root / 'state' / 'accounting-epoch.json', {'started_at': now.isoformat()})
        write_json(root / 'state' / 'accounting-reconciliation.json', {
            'status': 'VERIFIED', 'observed_at': now.isoformat(),
        })
        decision = OperationalRiskGate(root).evaluate('buy', now.isoformat(), now=now)
        assert decision.allowed is True
        assert decision.reasons == []


def test_stale_accounting_and_stale_signal_suspend_new_entries_but_not_exits():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        now = datetime.now(timezone.utc)
        stale = now - timedelta(hours=2)
        write_json(root / 'state' / 'accounting-epoch.json', {'started_at': stale.isoformat()})
        write_json(root / 'state' / 'accounting-reconciliation.json', {
            'status': 'VERIFIED', 'observed_at': stale.isoformat(),
        })
        gate = OperationalRiskGate(root)
        buy = gate.evaluate('buy', stale.isoformat(), now=now)
        sell = gate.evaluate('sell', None, now=now)
        assert buy.allowed is False
        assert 'accounting_reconciliation_stale' in buy.reasons
        assert 'market_signal_stale' in buy.reasons
        assert sell.allowed is True
        assert sell.reasons == ['exit_path_always_allowed']


def test_manual_suspension_blocks_entries():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        now = datetime.now(timezone.utc)
        write_json(root / 'state' / 'accounting-epoch.json', {'started_at': now.isoformat()})
        write_json(root / 'state' / 'accounting-reconciliation.json', {'status': 'VERIFIED', 'observed_at': now.isoformat()})
        write_json(root / 'state' / 'trading-manual-suspension.json', {'reason': 'test'})
        decision = OperationalRiskGate(root).evaluate('buy', now.isoformat(), now=now)
        assert decision.allowed is False
        assert 'manual_trading_suspension_active' in decision.reasons


def test_status_recomputes_current_accounting_instead_of_freezing_old_suspension():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        now = datetime.now(timezone.utc)
        write_json(root / 'state' / 'accounting-epoch.json', {'started_at': now.isoformat()})
        write_json(root / 'state' / 'accounting-reconciliation.json', {'status': 'VERIFIED', 'observed_at': now.isoformat()})
        gate = OperationalRiskGate(root)
        gate.evaluate('buy', (now - timedelta(hours=2)).isoformat(), now=now)
        assert gate.status()['new_entries_suspended'] is False
        assert gate.status()['current_reasons'] == []
