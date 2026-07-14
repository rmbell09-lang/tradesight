import os
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading.position_manager import PositionManager
from trading.trade_evidence import TradeEvidenceService


class FakeBroker:
    def get_remote_positions(self):
        return [{
            'symbol': 'SPY', 'qty': '1.5', 'side': 'long',
            'avg_entry_price': '500', 'current_price': '505',
            'market_value': '757.5', 'unrealized_pl': '7.5', 'unrealized_plpc': '0.01',
        }]

    def get_orders(self, status='all', limit=100):
        return [{
            'id': 'order-entry-1', 'symbol': 'SPY', 'side': 'buy', 'type': 'market',
            'qty': '1.5', 'filled_qty': '1.5', 'filled_avg_price': '500',
            'status': 'filled', 'submitted_at': '2026-07-14T13:00:00Z',
        }]

    def get_account_activities(self, activity_types='FILL', page_size=100):
        return [{
            'id': 'fill-1', 'order_id': 'order-entry-1', 'symbol': 'SPY',
            'side': 'buy', 'qty': '1.5', 'price': '500',
            'transaction_time': '2026-07-14T13:00:01Z', 'activity_type': 'FILL',
        }]


def test_snapshot_separates_broker_facts_and_local_context():
    with tempfile.TemporaryDirectory() as tmp:
        pm = PositionManager(base_dir=tmp)
        assert pm.open_position(
            'SPY', 'RSI Mean Reversion', 'long', 1.5, 500,
            entry_order_id='order-entry-1', entry_fill_status='filled',
            entry_reason='RSI crossed below 30', entry_confidence=0.78,
            market_regime='normal', signal_observed_at='2026-07-14T13:00:00Z',
            stop_loss_price=475, take_profit_price=530,
            sizing_reason='15% portfolio cap',
            signal_snapshot={'rsi': 28.1}, risk_snapshot={'notional': 750},
        )
        db = Path(tmp) / 'data' / 'positions.db'
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE positions SET verification_status='broker_position_confirmed' WHERE symbol='SPY'"
            )
            conn.commit()

        snapshot = TradeEvidenceService(Path(tmp), FakeBroker()).snapshot()

        assert snapshot['status'] == 'VERIFIED'
        assert snapshot['counts']['broker_positions'] == 1
        assert snapshot['counts']['broker_orders'] == 1
        assert snapshot['counts']['broker_fills'] == 1
        assert snapshot['positions'][0]['strategy'] == 'RSI Mean Reversion'
        assert snapshot['positions'][0]['evidence'] == 'BROKER_POSITION_CONFIRMED'
        assert snapshot['orders'][0]['evidence'] == 'BROKER_ORDER_FINAL'
        assert snapshot['fills'][0]['evidence'] == 'BROKER_FILL_ACTIVITY'


def test_why_trade_returns_recorded_reason_and_never_infers_missing_data():
    with tempfile.TemporaryDirectory() as tmp:
        pm = PositionManager(base_dir=tmp)
        pm.open_position('AAPL', 'Confluence', 'long', 1, 200, entry_reason='3 of 5 signals positive')
        with sqlite3.connect(Path(tmp) / 'data' / 'positions.db') as conn:
            position_id = conn.execute("SELECT id FROM positions WHERE symbol='AAPL'").fetchone()[0]

        why = TradeEvidenceService(Path(tmp)).why_trade(position_id)

        assert why['decision']['reasons'] == ['3 of 5 signals positive']
        assert why['broker_evidence']['entry_order_id'] is None
        assert why['provenance'] == 'UNVERIFIED'
        assert why['live_trading_allowed'] is False


def test_broker_unavailable_fails_closed_without_generated_rows():
    with tempfile.TemporaryDirectory() as tmp:
        PositionManager(base_dir=tmp)
        snapshot = TradeEvidenceService(Path(tmp), None).snapshot()
        assert snapshot['status'] == 'UNAVAILABLE'
        assert snapshot['positions'] == []
        assert snapshot['orders'] == []
        assert snapshot['fills'] == []
