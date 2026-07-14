#!/usr/bin/env python3
"""Tests for websocket reconnect supervisor (Task 1199)."""

import threading
import logging
import json
import types

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading.paper_trader import ExponentialBackoffWebSocketSupervisor, PaperTrader


def test_exponential_backoff_progression():
    attempts = {'count': 0}
    sleeps = []

    def fake_connect_once(stop_event):
        attempts['count'] += 1
        if attempts['count'] >= 5:
            stop_event.set()
            return
        raise ConnectionError('drop')

    def fake_sleep(delay):
        sleeps.append(delay)

    sup = ExponentialBackoffWebSocketSupervisor(
        connect_once=fake_connect_once,
        logger=logging.getLogger('test_ws_backoff'),
        initial_backoff=1,
        max_backoff=60,
        sleeper=fake_sleep,
    )

    sup.run(threading.Event())
    assert sleeps == [1, 2, 4, 8]


def test_backoff_caps_at_max():
    attempts = {'count': 0}
    sleeps = []

    def fake_connect_once(stop_event):
        attempts['count'] += 1
        if attempts['count'] >= 6:
            stop_event.set()
            return
        raise RuntimeError('still down')

    sup = ExponentialBackoffWebSocketSupervisor(
        connect_once=fake_connect_once,
        logger=logging.getLogger('test_ws_cap'),
        initial_backoff=1,
        max_backoff=4,
        sleeper=sleeps.append,
    )

    sup.run(threading.Event())
    assert sleeps == [1, 2, 4, 4, 4]


def test_trade_update_idle_timeout_is_not_treated_as_drop(monkeypatch):
    """Idle trade_updates reads should continue, not force reconnect churn."""
    class WebSocketTimeoutException(Exception):
        pass

    class FakeWS:
        def __init__(self):
            self.recv_calls = 0
            self.closed = False

        def settimeout(self, timeout):
            self.timeout = timeout

        def send(self, payload):
            pass

        def recv(self):
            self.recv_calls += 1
            if self.recv_calls == 1:
                return json.dumps([{"stream": "authorization", "data": {"status": "authorized"}}])
            if self.recv_calls == 2:
                return json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}})
            if self.recv_calls == 3:
                raise WebSocketTimeoutException("The read operation timed out")
            return None

        def close(self):
            self.closed = True

    fake_ws = FakeWS()
    fake_module = types.SimpleNamespace(
        create_connection=lambda url, timeout: fake_ws,
        WebSocketTimeoutException=WebSocketTimeoutException,
    )
    monkeypatch.setitem(sys.modules, "websocket", fake_module)

    trader = PaperTrader.__new__(PaperTrader)
    trader.alpaca = types.SimpleNamespace(demo_mode=False, api_key="key", secret_key="secret")
    trader.logger = logging.getLogger("test_trade_update_idle_timeout")

    stop_event = threading.Event()
    try:
        trader._trade_updates_connect_once(stop_event)
    except ConnectionError as exc:
        assert "empty websocket frame" in str(exc)
    else:
        raise AssertionError("expected empty frame after idle timeout continuation")

    assert fake_ws.recv_calls == 4
    assert fake_ws.closed is True
