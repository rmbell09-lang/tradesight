#!/usr/bin/env python3
"""Focused live safety and learning tests."""

from datetime import datetime, timedelta
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading.feedback_tracker import FeedbackTracker
from trading.live_safety import (
    AccountSnapshot,
    DryRunReceipt,
    LiveSafetyPolicy,
    LiveTradingSafetyGate,
    OrderIntent,
    ShadowCanaryRecorder,
)
from trading.paper_trader import PaperTrader


def test_live_safety_gate_denies_live_by_default():
    gate = LiveTradingSafetyGate()
    receipt = gate.evaluate(
        OrderIntent(
            symbol='QQQ',
            strategy='VWAP Reversion',
            action='buy',
            quantity=0.01,
            price=500.0,
            mode='live',
        )
    )

    assert receipt.allowed is False
    assert 'live_orders_disabled_by_policy' in receipt.reasons
    assert 'kill_switch_enabled' in receipt.reasons
    assert 'broker_account_snapshot_missing' in receipt.reasons


def test_shadow_canary_records_receipt_without_broker_order():
    broker = Mock()
    with tempfile.TemporaryDirectory() as tmp:
        gate = LiveTradingSafetyGate()
        receipt = gate.evaluate(
            OrderIntent(
                symbol='SPY',
                strategy='Confluence',
                action='buy',
                quantity=0.01,
                price=500.0,
                mode='shadow',
            )
        )
        path = ShadowCanaryRecorder(tmp).record(receipt)

        assert receipt.allowed is True
        assert receipt.reasons == ['shadow_only_no_broker_order_allowed']
        assert path.exists()
        broker.submit_order.assert_not_called()
        broker.place_paper_trade.assert_not_called()


def test_canary_requires_every_live_safety_condition():
    policy = LiveSafetyPolicy(
        kill_switch=False,
        approved_symbols=['QQQ'],
        approved_strategies=['VWAP Reversion'],
        allow_tiny_live_canary=True,
        max_notional_per_trade=5.0,
    )
    gate = LiveTradingSafetyGate(policy)
    account = AccountSnapshot(
        endpoint='live',
        status='ACTIVE',
        trading_blocked=False,
        account_blocked=False,
        buying_power=100.0,
        equity=100.0,
    )
    dry_run = DryRunReceipt(
        passed=True,
        generated_at=(datetime.now() - timedelta(minutes=5)).isoformat(),
    )

    receipt = gate.evaluate(
        OrderIntent(
            symbol='QQQ',
            strategy='VWAP Reversion',
            action='buy',
            quantity=0.005,
            price=500.0,
            mode='canary',
            live_intent='I_UNDERSTAND_LIVE_RISK',
        ),
        account=account,
        dry_run=dry_run,
        daily_realized_pnl=0.0,
        open_positions=0,
        credential_source='keychain',
    )

    assert receipt.allowed is True
    assert receipt.reasons == []


def test_feedback_normalizes_blank_exit_reason_and_r_multiple():
    with tempfile.TemporaryDirectory() as tmp:
        feedback = FeedbackTracker(base_dir=tmp)
        ok = feedback.record_closed_trade(
            params={'oversold': 30},
            symbol='AAPL',
            strategy='VWAP Reversion',
            side='long',
            entry_price=100.0,
            exit_price=103.0,
            quantity=2.0,
            pnl_dollars=6.0,
            exit_reason='',
            risk_dollars=3.0,
            market_regime='bullish_trend',
            regime_source='test',
            source='test',
            source_id='1',
        )
        assert ok is True
        with sqlite3.connect(Path(tmp) / 'data' / 'feedback.db') as conn:
            row = conn.execute(
                "SELECT exit_reason, missing_exit_reason, notional, risk_dollars, r_multiple, market_regime "
                "FROM trade_feedback WHERE source='test' AND source_id='1'"
            ).fetchone()

        assert row == ('unlabeled_profit_exit', 1, 200.0, 3.0, 2.0, 'bullish_trend')


def test_feedback_does_not_boost_tiny_perfect_sample():
    with tempfile.TemporaryDirectory() as tmp:
        feedback = FeedbackTracker(base_dir=tmp)
        for i in range(3):
            feedback.record_closed_trade(
                params={'p': i},
                symbol='QQQ',
                strategy='Confluence',
                side='long',
                entry_price=100.0,
                exit_price=102.0,
                quantity=1.0,
                pnl_dollars=2.0,
                source='test',
                source_id=str(i),
            )

        adjustment = feedback.get_execution_adjustment('QQQ', 'Confluence', min_trades=3)

        assert adjustment['tradeable'] is True
        assert adjustment['confidence_multiplier'] == 1.0
        assert adjustment['size_multiplier'] == 1.0
        assert adjustment['sample_weight'] == 0.3
        assert 'sample too small for boost' in adjustment['reason']


def test_learned_buy_block_does_not_block_sell_close_path():
    with tempfile.TemporaryDirectory() as tmp:
        trader = PaperTrader(base_dir=tmp)
        trader._daily_loss_limit_reached = False
        trader.position_manager.open_position('META', 'Confluence', 'long', 1.0, 100.0)
        trader._get_learning_adjustment = Mock(return_value={
            'tradeable': False,
            'reason': 'blocked test buy edge',
            'size_multiplier': 0.0,
            'confidence_multiplier': 0.0,
        })
        trader.alpaca.close_full_position = Mock(return_value={
            'status': 'closed',
            'order_id': 'exit_1',
            'fill_price': 101.0,
            'broker_verified': True,
        })

        ok = trader.execute_signal({
            'symbol': 'META',
            'strategy': 'Confluence',
            'action': 'sell',
            'side': 'long',
            'current_price': 101.0,
            'confidence': 0.2,
        })

        assert ok is True
        trader.alpaca.close_full_position.assert_called_once_with('META')
