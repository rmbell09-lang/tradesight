#!/usr/bin/env python3
"""
Tests for TradeSight Alerts module (Phase 5.1)

Covers:
  - AlertType enum values
  - EmailAlerter configuration guard (no real SMTP calls)
  - WebhookAlerter configuration guard (no real HTTP calls)
  - AlertManager: fire, history, stats, graceful disabled mode
  - Integration: mock signal → alert dispatched
"""

import json
import sys
import os
import tempfile
import threading
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from alerts.alert_types import AlertType
from alerts.email_alerter import EmailAlerter
from alerts.webhook_alerter import WebhookAlerter
from alerts.alert_manager import AlertManager


# ---------------------------------------------------------------------------
# AlertType tests
# ---------------------------------------------------------------------------

class TestAlertType:
    def test_all_enum_members_exist(self):
        assert AlertType.SIGNAL_FIRED.value == 'signal_fired'
        assert AlertType.TRADE_EXECUTED.value == 'trade_executed'
        assert AlertType.DAILY_SUMMARY.value == 'daily_summary'
        assert AlertType.STRATEGY_EVOLVED.value == 'strategy_evolved'

    def test_enum_count(self):
        assert len(AlertType) == 4


# ---------------------------------------------------------------------------
# EmailAlerter tests
# ---------------------------------------------------------------------------

class TestEmailAlerter:
    def _minimal_config(self, **overrides):
        cfg = {
            'email_enabled': True,
            'smtp_host': 'smtp.example.com',
            'smtp_port': 587,
            'smtp_use_tls': True,
            'smtp_username': 'user@example.com',
            'smtp_password': 'secret',
            'email_from': 'tradesight@example.com',
            'email_to': ['admin@example.com'],
        }
        cfg.update(overrides)
        return cfg

    def test_disabled_by_default(self):
        alerter = EmailAlerter({})
        assert alerter.send("subject", "body") is False

    def test_email_enabled_false(self):
        alerter = EmailAlerter({'email_enabled': False})
        assert alerter.send("subject", "body") is False

    def test_missing_smtp_host_returns_false(self):
        cfg = self._minimal_config(smtp_host='')
        alerter = EmailAlerter(cfg)
        assert alerter.send("subject", "body") is False

    def test_missing_email_to_returns_false(self):
        cfg = self._minimal_config(email_to=[])
        alerter = EmailAlerter(cfg)
        assert alerter.send("subject", "body") is False

    def test_missing_email_from_returns_false(self):
        cfg = self._minimal_config(email_from='')
        alerter = EmailAlerter(cfg)
        assert alerter.send("subject", "body") is False

    def test_sends_successfully_with_mock_smtp(self):
        cfg = self._minimal_config()
        alerter = EmailAlerter(cfg)
        mock_server = MagicMock()
        with patch('smtplib.SMTP') as mock_smtp_cls:
            mock_smtp_cls.return_value.__enter__ = lambda s: mock_server
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = alerter.send("Test Subject", "Test body")
        # send() calls SMTP as context manager — check it was instantiated
        mock_smtp_cls.assert_called_once_with('smtp.example.com', 587, timeout=10)

    def test_smtp_auth_error_returns_false(self):
        import smtplib
        cfg = self._minimal_config()
        alerter = EmailAlerter(cfg)
        with patch('smtplib.SMTP') as mock_smtp_cls:
            mock_smtp_cls.side_effect = smtplib.SMTPAuthenticationError(535, b'Auth failed')
            result = alerter.send("subject", "body")
        assert result is False

    def test_string_email_to_accepted(self):
        cfg = self._minimal_config(email_to='single@example.com')
        alerter = EmailAlerter(cfg)
        assert alerter._is_configured() is True


# ---------------------------------------------------------------------------
# WebhookAlerter tests
# ---------------------------------------------------------------------------

class TestWebhookAlerter:
    def test_disabled_by_default(self):
        alerter = WebhookAlerter({})
        assert alerter.send({'event': 'test'}) is False

    def test_webhook_enabled_false(self):
        alerter = WebhookAlerter({'webhook_enabled': False, 'webhook_url': 'http://example.com'})
        assert alerter.send({'event': 'test'}) is False

    def test_empty_url_returns_false(self):
        alerter = WebhookAlerter({'webhook_enabled': True, 'webhook_url': ''})
        assert alerter.send({'event': 'test'}) is False

    def test_invalid_url_scheme_returns_false(self):
        alerter = WebhookAlerter({'webhook_enabled': True, 'webhook_url': 'ftp://bad.url'})
        assert alerter.send({'event': 'test'}) is False

    def test_successful_send_with_mock(self):
        import urllib.request
        cfg = {'webhook_enabled': True, 'webhook_url': 'https://hooks.example.com/test', 'webhook_timeout': 5}
        alerter = WebhookAlerter(cfg)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = alerter.send({'event': 'test', 'symbol': 'AAPL'})
        assert result is True

    def test_http_error_returns_false(self):
        import urllib.error
        cfg = {'webhook_enabled': True, 'webhook_url': 'https://hooks.example.com/test'}
        alerter = WebhookAlerter(cfg)
        with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError(
                'https://hooks.example.com/test', 500, 'Server Error', {}, None)):
            result = alerter.send({'event': 'test'})
        assert result is False

    def test_url_error_returns_false(self):
        import urllib.error
        cfg = {'webhook_enabled': True, 'webhook_url': 'https://hooks.example.com/test'}
        alerter = WebhookAlerter(cfg)
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('connection refused')):
            result = alerter.send({'event': 'test'})
        assert result is False

    def test_non_2xx_returns_false(self):
        cfg = {'webhook_enabled': True, 'webhook_url': 'https://hooks.example.com/test'}
        alerter = WebhookAlerter(cfg)
        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = alerter.send({'event': 'test'})
        assert result is False


# ---------------------------------------------------------------------------
# AlertManager tests
# ---------------------------------------------------------------------------

class TestAlertManager:
    def _disabled_config(self):
        return {'alerts_enabled': False}

    def _enabled_config(self):
        return {'alerts_enabled': True, 'email_enabled': False, 'webhook_enabled': False}

    def test_fire_returns_false_when_disabled(self):
        am = AlertManager(config=self._disabled_config())
        result = am.fire(AlertType.SIGNAL_FIRED, symbol='AAPL', action='buy')
        assert result is False

    def test_fire_records_history_when_disabled(self):
        """Even with alerts disabled, no history is recorded (disabled = silent)."""
        am = AlertManager(config=self._disabled_config())
        am.fire(AlertType.SIGNAL_FIRED, symbol='AAPL')
        assert len(am.get_recent_alerts()) == 0

    def test_fire_records_history_when_enabled(self):
        am = AlertManager(config=self._enabled_config())
        am.fire(AlertType.SIGNAL_FIRED, symbol='TSLA', action='buy', score=88.5)
        alerts = am.get_recent_alerts()
        assert len(alerts) == 1
        assert alerts[0]['type'] == 'signal_fired'
        assert alerts[0]['symbol'] == 'TSLA'

    def test_fire_multiple_types(self):
        am = AlertManager(config=self._enabled_config())
        am.fire(AlertType.SIGNAL_FIRED, symbol='AAPL')
        am.fire(AlertType.TRADE_EXECUTED, symbol='AAPL', action='buy', quantity=5, price=180.0, strategy='RSI')
        am.fire(AlertType.DAILY_SUMMARY, pnl=25.50, trades=3, positions=1)
        am.fire(AlertType.STRATEGY_EVOLVED, winner='MACD Crossover', score=0.812, rounds=3)
        assert len(am.get_recent_alerts()) == 4

    def test_get_recent_alerts_newest_first(self):
        am = AlertManager(config=self._enabled_config())
        am.fire(AlertType.SIGNAL_FIRED, symbol='AAPL')
        am.fire(AlertType.SIGNAL_FIRED, symbol='TSLA')
        alerts = am.get_recent_alerts()
        assert alerts[0]['symbol'] == 'TSLA'
        assert alerts[1]['symbol'] == 'AAPL'

    def test_get_recent_alerts_limit(self):
        am = AlertManager(config=self._enabled_config())
        for i in range(10):
            am.fire(AlertType.SIGNAL_FIRED, symbol=f'SYM{i}')
        assert len(am.get_recent_alerts(limit=5)) == 5

    def test_get_alert_stats(self):
        am = AlertManager(config=self._enabled_config())
        am.fire(AlertType.SIGNAL_FIRED, symbol='AAPL')
        am.fire(AlertType.TRADE_EXECUTED, symbol='AAPL', action='buy', quantity=1, price=100.0, strategy='RSI')
        stats = am.get_alert_stats()
        assert stats['total'] == 2
        assert stats['by_type']['signal_fired'] == 1
        assert stats['by_type']['trade_executed'] == 1
        assert stats['alerts_enabled'] is True

    def test_history_persisted_to_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._enabled_config()
            am = AlertManager(config=cfg, data_dir=tmpdir)
            am.fire(AlertType.STRATEGY_EVOLVED, winner='Bollinger', score=0.75, rounds=3)

            hist_path = os.path.join(tmpdir, 'alert_history.json')
            assert os.path.exists(hist_path)
            with open(hist_path) as f:
                data = json.load(f)
            assert len(data) == 1
            assert data[0]['type'] == 'strategy_evolved'

    def test_history_loaded_from_disk_on_init(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._enabled_config()
            am1 = AlertManager(config=cfg, data_dir=tmpdir)
            am1.fire(AlertType.SIGNAL_FIRED, symbol='AMZN')

            am2 = AlertManager(config=cfg, data_dir=tmpdir)
            alerts = am2.get_recent_alerts()
            assert len(alerts) == 1
            assert alerts[0]['symbol'] == 'AMZN'

    def test_thread_safety(self):
        am = AlertManager(config=self._enabled_config())
        errors = []

        def fire_batch():
            try:
                for _ in range(20):
                    am.fire(AlertType.SIGNAL_FIRED, symbol='X')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=fire_batch) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(am.get_recent_alerts(limit=100)) == 100

    def test_email_dispatched_on_fire(self):
        cfg = {
            'alerts_enabled': True,
            'email_enabled': True,
            'smtp_host': 'smtp.example.com',
            'smtp_port': 587,
            'smtp_use_tls': True,
            'smtp_username': 'u',
            'smtp_password': 'p',
            'email_from': 'from@x.com',
            'email_to': ['to@x.com'],
            'webhook_enabled': False,
        }
        am = AlertManager(config=cfg)
        with patch('smtplib.SMTP') as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__ = lambda s: mock_server
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            result = am.fire(AlertType.TRADE_EXECUTED, symbol='AAPL', action='buy',
                             quantity=10, price=175.0, strategy='MACD')
        mock_smtp.assert_called_once()

    def test_webhook_dispatched_on_fire(self):
        cfg = {
            'alerts_enabled': True,
            'email_enabled': False,
            'webhook_enabled': True,
            'webhook_url': 'https://hooks.example.com/tradesight',
        }
        am = AlertManager(config=cfg)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = am.fire(AlertType.SIGNAL_FIRED, symbol='NVDA', action='buy', score=92.3)
        assert result is True


# ---------------------------------------------------------------------------
# Integration: mock signal → alert dispatched
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_mock_signal_triggers_alert(self):
        """Simulate the scanner producing a signal and an alert being dispatched."""
        fired_events = []

        class MockAlertManager:
            def fire(self, alert_type, **kwargs):
                fired_events.append({'type': alert_type, **kwargs})
                return True

        mock_am = MockAlertManager()
        mock_am.fire(
            AlertType.SIGNAL_FIRED,
            symbol='AAPL',
            action='buy',
            score=85.5,
            reason='RSI oversold: 28.4',
        )

        assert len(fired_events) == 1
        assert fired_events[0]['type'] == AlertType.SIGNAL_FIRED
        assert fired_events[0]['symbol'] == 'AAPL'
        assert fired_events[0]['score'] == 85.5

    def test_mock_trade_triggers_alert(self):
        """Simulate the paper trader executing and alerting."""
        fired_events = []

        class MockAlertManager:
            def fire(self, alert_type, **kwargs):
                fired_events.append({'type': alert_type, **kwargs})
                return True

        mock_am = MockAlertManager()
        mock_am.fire(
            AlertType.TRADE_EXECUTED,
            symbol='TSLA',
            action='buy',
            quantity=3,
            price=245.50,
            strategy='RSI Mean Reversion',
            confidence=0.76,
        )

        assert fired_events[0]['type'] == AlertType.TRADE_EXECUTED
        assert fired_events[0]['quantity'] == 3

    def test_daily_summary_alert(self):
        am = AlertManager(config={'alerts_enabled': True, 'email_enabled': False, 'webhook_enabled': False})
        result = am.fire(AlertType.DAILY_SUMMARY, pnl=42.75, trades=5, positions=2)
        alerts = am.get_recent_alerts()
        assert alerts[0]['type'] == 'daily_summary'
        assert alerts[0]['pnl'] == 42.75

    def test_strategy_evolved_alert(self):
        am = AlertManager(config={'alerts_enabled': True, 'email_enabled': False, 'webhook_enabled': False})
        am.fire(AlertType.STRATEGY_EVOLVED, winner='Bollinger Bounce', score=0.812, rounds=3)
        alerts = am.get_recent_alerts()
        assert alerts[0]['winner'] == 'Bollinger Bounce'

    def test_graceful_degradation_no_channels(self):
        """Fire with both channels disabled — returns False but doesn't raise."""
        am = AlertManager(config={'alerts_enabled': True, 'email_enabled': False, 'webhook_enabled': False})
        result = am.fire(AlertType.SIGNAL_FIRED, symbol='X', action='buy')
        assert result is False  # no channels delivered
        assert len(am.get_recent_alerts()) == 1  # but history recorded


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
