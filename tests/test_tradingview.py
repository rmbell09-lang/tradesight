"""
TradeSight TradingView Webhook Integration Tests

Test suite for TradingView alert parsing, validation, and webhook endpoint.
"""

import pytest
import sys
import os
from unittest.mock import Mock, patch
from datetime import datetime
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestTradingViewAdapter:
    """Pure unit tests for TradingView signal parsing and validation."""

    def test_parse_valid_buy_signal(self):
        from integrations.tradingview_adapter import parse_tradingview_payload

        payload = {
            "symbol": "aapl",
            "action": "buy",
            "confidence": 0.70,
            "price": 185.50,
            "reason": "RSI oversold",
            "strategy": "RSI_SMA"
        }

        signal = parse_tradingview_payload(payload)

        assert signal.symbol == "AAPL"
        assert signal.action == "buy"
        assert signal.confidence == 0.70
        assert signal.price == 185.50
        assert signal.reason == "RSI oversold"
        assert signal.strategy == "RSI_SMA"

    def test_parse_valid_sell_signal(self):
        from integrations.tradingview_adapter import parse_tradingview_payload

        payload = {
            "symbol": "MSFT",
            "action": "sell",
            "confidence": 0.75,
            "price": 340.25,
            "reason": "Taking profit",
            "strategy": "Confluence"
        }

        signal = parse_tradingview_payload(payload)

        assert signal.symbol == "MSFT"
        assert signal.action == "sell"
        assert signal.confidence == 0.75

    def test_parse_missing_required_symbol(self):
        from integrations.tradingview_adapter import parse_tradingview_payload

        with pytest.raises(ValueError, match="symbol"):
            parse_tradingview_payload({"action": "buy", "confidence": 0.70})

    def test_parse_missing_required_action(self):
        from integrations.tradingview_adapter import parse_tradingview_payload

        with pytest.raises(ValueError, match="action"):
            parse_tradingview_payload({"symbol": "AAPL", "confidence": 0.70})

    def test_parse_missing_required_confidence(self):
        from integrations.tradingview_adapter import parse_tradingview_payload

        with pytest.raises(ValueError, match="confidence"):
            parse_tradingview_payload({"symbol": "AAPL", "action": "buy"})

    def test_parse_invalid_action(self):
        from integrations.tradingview_adapter import parse_tradingview_payload

        with pytest.raises(ValueError, match="action"):
            parse_tradingview_payload({
                "symbol": "AAPL", "action": "hold", "confidence": 0.70
            })

    def test_parse_action_case_insensitive(self):
        from integrations.tradingview_adapter import parse_tradingview_payload

        signal = parse_tradingview_payload({
            "symbol": "AAPL", "action": "BUY", "confidence": 0.70
        })
        assert signal.action == "buy"

    def test_confidence_clamped_below_minimum(self):
        from integrations.tradingview_adapter import parse_tradingview_payload

        signal = parse_tradingview_payload({
            "symbol": "AAPL", "action": "buy", "confidence": 0.30
        })
        assert signal.confidence == 0.55

    def test_confidence_clamped_above_maximum(self):
        from integrations.tradingview_adapter import parse_tradingview_payload

        signal = parse_tradingview_payload({
            "symbol": "AAPL", "action": "buy", "confidence": 0.95
        })
        assert signal.confidence == 0.85

    def test_confidence_within_range_unchanged(self):
        from integrations.tradingview_adapter import parse_tradingview_payload

        signal = parse_tradingview_payload({
            "symbol": "AAPL", "action": "buy", "confidence": 0.70
        })
        assert signal.confidence == 0.70

    def test_symbol_uppercased(self):
        from integrations.tradingview_adapter import parse_tradingview_payload

        signal = parse_tradingview_payload({
            "symbol": "aapl", "action": "buy", "confidence": 0.70
        })
        assert signal.symbol == "AAPL"

    def test_parse_optional_price_none(self):
        from integrations.tradingview_adapter import parse_tradingview_payload

        signal = parse_tradingview_payload({
            "symbol": "AAPL", "action": "buy", "confidence": 0.70
        })
        assert signal.price is None

    def test_parse_optional_defaults(self):
        from integrations.tradingview_adapter import parse_tradingview_payload

        signal = parse_tradingview_payload({
            "symbol": "AAPL", "action": "buy", "confidence": 0.70
        })
        assert len(signal.reason) > 0
        assert len(signal.strategy) > 0

    def test_to_internal_signal_buy_sets_long_side(self):
        from integrations.tradingview_adapter import parse_tradingview_payload, to_internal_signal

        tv = parse_tradingview_payload({
            "symbol": "AAPL", "action": "buy", "confidence": 0.70
        })
        internal = to_internal_signal(tv, 185.50)

        assert internal["side"] == "long"
        assert internal["action"] == "buy"

    def test_to_internal_signal_sell_sets_short_side(self):
        from integrations.tradingview_adapter import parse_tradingview_payload, to_internal_signal

        tv = parse_tradingview_payload({
            "symbol": "AAPL", "action": "sell", "confidence": 0.70
        })
        internal = to_internal_signal(tv, 185.50)

        assert internal["side"] == "short"
        assert internal["action"] == "sell"

    def test_to_internal_signal_has_required_fields(self):
        from integrations.tradingview_adapter import parse_tradingview_payload, to_internal_signal

        tv = parse_tradingview_payload({
            "symbol": "AAPL", "action": "buy", "confidence": 0.70,
            "price": 185.50, "reason": "Test", "strategy": "TestStrat"
        })
        internal = to_internal_signal(tv, 185.50)

        required = {"symbol", "action", "side", "confidence", "reason",
                     "strategy", "timestamp", "current_price"}
        assert required.issubset(internal.keys())
        assert internal["symbol"] == "AAPL"
        assert internal["current_price"] == 185.50

    def test_to_internal_signal_timestamp_iso(self):
        from integrations.tradingview_adapter import parse_tradingview_payload, to_internal_signal

        tv = parse_tradingview_payload({
            "symbol": "AAPL", "action": "buy", "confidence": 0.70
        })
        internal = to_internal_signal(tv, 185.50)

        datetime.fromisoformat(internal["timestamp"].replace('Z', '+00:00'))

    def test_verify_token_valid(self):
        from integrations.tradingview_adapter import verify_token
        assert verify_token({"token": "secret123"}, "secret123") is True

    def test_verify_token_invalid(self):
        from integrations.tradingview_adapter import verify_token
        assert verify_token({"token": "secret123"}, "wrong") is False

    def test_verify_token_missing_key(self):
        from integrations.tradingview_adapter import verify_token
        assert verify_token({"symbol": "AAPL"}, "secret123") is False


class TestTradingViewWebhook:
    """Flask integration tests for /api/tradingview/webhook endpoint."""

    @pytest.fixture(autouse=True)
    def setup(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        # Mock talib (C library) if not installed
        if 'talib' not in sys.modules:
            sys.modules['talib'] = Mock()
        from web.dashboard import app
        self.app = app
        self.client = app.test_client()
        yield

    def test_webhook_returns_400_invalid_json(self):
        response = self.client.post(
            '/api/tradingview/webhook',
            data='not json',
            content_type='application/json'
        )
        assert response.status_code == 400
        assert json.loads(response.data)['success'] is False

    def test_webhook_returns_401_wrong_token(self):
        with patch('web.dashboard.TRADINGVIEW_SECRET', 'expected'), \
             patch('web.dashboard.TRADINGVIEW_ENABLED', True):
            response = self.client.post(
                '/api/tradingview/webhook',
                json={"token": "wrong", "symbol": "AAPL",
                      "action": "buy", "confidence": 0.70}
            )
            assert response.status_code == 401

    def test_webhook_returns_422_missing_symbol(self):
        with patch('web.dashboard.TRADINGVIEW_SECRET', ''), \
             patch('web.dashboard.TRADINGVIEW_ENABLED', True):
            response = self.client.post(
                '/api/tradingview/webhook',
                json={"action": "buy", "confidence": 0.70}
            )
            assert response.status_code == 422

    @patch('web.dashboard.get_paper_trader')
    def test_webhook_valid_buy(self, mock_get_trader):
        mock_trader = Mock()
        mock_trader.execute_signal.return_value = True
        mock_get_trader.return_value = mock_trader

        with patch('web.dashboard.TRADINGVIEW_SECRET', ''), \
             patch('web.dashboard.TRADINGVIEW_ENABLED', True), \
             patch('web.dashboard._get_live_price', return_value=185.50):
            response = self.client.post(
                '/api/tradingview/webhook',
                json={"symbol": "AAPL", "action": "buy",
                      "confidence": 0.70, "price": 185.50}
            )
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['success'] is True
            assert data['executed'] is True
            assert data['symbol'] == "AAPL"
            mock_trader.execute_signal.assert_called_once()

    @patch('web.dashboard.get_paper_trader')
    def test_webhook_valid_sell(self, mock_get_trader):
        mock_trader = Mock()
        mock_trader.execute_signal.return_value = True
        mock_get_trader.return_value = mock_trader

        with patch('web.dashboard.TRADINGVIEW_SECRET', ''), \
             patch('web.dashboard.TRADINGVIEW_ENABLED', True), \
             patch('web.dashboard._get_live_price', return_value=340.25):
            response = self.client.post(
                '/api/tradingview/webhook',
                json={"symbol": "MSFT", "action": "sell",
                      "confidence": 0.75, "price": 340.25}
            )
            assert response.status_code == 200
            assert json.loads(response.data)['success'] is True

    def test_webhook_returns_503_disabled(self):
        with patch('web.dashboard.TRADINGVIEW_ENABLED', False):
            response = self.client.post(
                '/api/tradingview/webhook',
                json={"symbol": "AAPL", "action": "buy", "confidence": 0.70}
            )
            assert response.status_code == 503

    @patch('web.dashboard.get_paper_trader')
    def test_webhook_returns_500_execution_error(self, mock_get_trader):
        mock_trader = Mock()
        mock_trader.execute_signal.side_effect = RuntimeError("Connection failed")
        mock_get_trader.return_value = mock_trader

        with patch('web.dashboard.TRADINGVIEW_SECRET', ''), \
             patch('web.dashboard.TRADINGVIEW_ENABLED', True), \
             patch('web.dashboard._get_live_price', return_value=185.50):
            response = self.client.post(
                '/api/tradingview/webhook',
                json={"symbol": "AAPL", "action": "buy", "confidence": 0.70}
            )
            assert response.status_code == 500

    @patch('web.dashboard.get_paper_trader')
    def test_webhook_503_price_unavailable(self, mock_get_trader):
        with patch('web.dashboard.TRADINGVIEW_SECRET', ''), \
             patch('web.dashboard.TRADINGVIEW_ENABLED', True), \
             patch('web.dashboard._get_live_price', side_effect=RuntimeError("No data")):
            response = self.client.post(
                '/api/tradingview/webhook',
                json={"symbol": "AAPL", "action": "buy", "confidence": 0.70}
            )
            assert response.status_code == 503

    @patch('web.dashboard.get_paper_trader')
    def test_webhook_execute_not_called_on_parse_error(self, mock_get_trader):
        mock_trader = Mock()
        mock_get_trader.return_value = mock_trader

        with patch('web.dashboard.TRADINGVIEW_SECRET', ''), \
             patch('web.dashboard.TRADINGVIEW_ENABLED', True):
            self.client.post(
                '/api/tradingview/webhook',
                json={"symbol": "AAPL", "action": "invalid", "confidence": 0.70}
            )
            mock_trader.execute_signal.assert_not_called()

    @patch('web.dashboard.get_paper_trader')
    def test_webhook_uses_provided_price(self, mock_get_trader):
        mock_trader = Mock()
        mock_trader.execute_signal.return_value = True
        mock_get_trader.return_value = mock_trader
        mock_live = Mock()

        with patch('web.dashboard.TRADINGVIEW_SECRET', ''), \
             patch('web.dashboard.TRADINGVIEW_ENABLED', True), \
             patch('web.dashboard._get_live_price', mock_live):
            self.client.post(
                '/api/tradingview/webhook',
                json={"symbol": "AAPL", "action": "buy",
                      "confidence": 0.70, "price": 185.50}
            )
            mock_live.assert_not_called()

    @patch('web.dashboard.get_paper_trader')
    def test_webhook_fetches_price_if_missing(self, mock_get_trader):
        mock_trader = Mock()
        mock_trader.execute_signal.return_value = True
        mock_get_trader.return_value = mock_trader

        with patch('web.dashboard.TRADINGVIEW_SECRET', ''), \
             patch('web.dashboard.TRADINGVIEW_ENABLED', True), \
             patch('web.dashboard._get_live_price', return_value=187.25) as mock_live:
            response = self.client.post(
                '/api/tradingview/webhook',
                json={"symbol": "AAPL", "action": "buy", "confidence": 0.70}
            )
            mock_live.assert_called_once_with("AAPL")
            assert json.loads(response.data)['price'] == 187.25


if __name__ == "__main__":
    pytest.main([__file__, "-v"])