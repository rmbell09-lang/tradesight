#!/usr/bin/env python3
"""Tests for websocket reconnect supervisor (Task 1199)."""

import threading
import logging

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading.paper_trader import ExponentialBackoffWebSocketSupervisor


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
