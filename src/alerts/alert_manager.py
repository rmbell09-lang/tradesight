"""
AlertManager — central dispatcher for TradeSight notifications.

Receives structured alert events and routes them to configured channels
(email, webhook). Falls back gracefully if nothing is configured.
"""
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .alert_types import AlertType
from .email_alerter import EmailAlerter
from .webhook_alerter import WebhookAlerter

logger = logging.getLogger(__name__)

# Max alerts kept in the in-memory ring buffer
_HISTORY_LIMIT = 200


class AlertManager:
    """
    Central alert dispatcher.

    Usage:
        am = AlertManager(config=alerts_config, data_dir='/path/to/data')
        am.fire(AlertType.SIGNAL_FIRED, symbol='AAPL', rsi=28.4, action='buy')
        am.fire(AlertType.TRADE_EXECUTED, symbol='TSLA', action='buy',
                quantity=5, price=250.0, strategy='RSI Mean Reversion')
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None, data_dir: Optional[str] = None):
        """
        Args:
            config:   Alerts config dict (see src/config.py ALERTS_CONFIG).
                      If None, alerting is disabled (safe default).
            data_dir: Directory to persist alert history JSON.
        """
        self.config = config or {}
        self.data_dir = Path(data_dir) if data_dir else None

        self._email = EmailAlerter(self.config)
        self._webhook = WebhookAlerter(self.config)

        # Thread-safe ring buffer for recent alerts
        self._lock = threading.Lock()
        self._history: List[Dict[str, Any]] = []

        # Load persisted history (non-critical — ignore errors)
        if self.data_dir:
            self._load_history()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fire(self, alert_type: AlertType, **kwargs) -> bool:
        """
        Dispatch an alert.

        Args:
            alert_type: One of the AlertType enum values
            **kwargs:   Contextual data included in the payload and message

        Returns:
            True if at least one channel successfully delivered the alert
        """
        if not self.config.get('alerts_enabled', False):
            logger.debug(f"Alerts disabled — skipping {alert_type.value}")
            return False

        payload = self._build_payload(alert_type, kwargs)
        subject, body = self._format_message(alert_type, payload)

        # Record in history regardless of delivery outcome
        self._record(payload)

        # Dispatch to channels
        results = []
        results.append(self._email.send(subject, body))
        results.append(self._webhook.send(payload))

        success = any(results)
        if not success:
            logger.debug(f"Alert {alert_type.value} recorded locally (no channels delivered)")
        return success

    def get_recent_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the most-recent alerts (newest first)."""
        with self._lock:
            return list(reversed(self._history[-_HISTORY_LIMIT:]))[:limit]

    def get_alert_stats(self) -> Dict[str, Any]:
        """Return summary counts by alert type."""
        with self._lock:
            counts: Dict[str, int] = {}
            for entry in self._history:
                t = entry.get('type', 'unknown')
                counts[t] = counts.get(t, 0) + 1
            return {
                'total': len(self._history),
                'by_type': counts,
                'alerts_enabled': self.config.get('alerts_enabled', False),
                'email_enabled': self.config.get('email_enabled', False),
                'webhook_enabled': self.config.get('webhook_enabled', False),
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_payload(self, alert_type: AlertType, data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'source': 'TradeSight',
            'type': alert_type.value,
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            **data,
        }

    def _format_message(self, alert_type: AlertType, payload: Dict[str, Any]):
        """Build human-readable subject + body from payload."""
        ts = payload.get('timestamp', '')

        if alert_type == AlertType.SIGNAL_FIRED:
            symbol = payload.get('symbol', '?')
            action = payload.get('action', '?').upper()
            rsi = payload.get('rsi')
            score = payload.get('score')
            reason = payload.get('reason', '')
            subject = f"[TradeSight] 📡 Signal: {action} {symbol}"
            lines = [
                f"TradeSight Signal Alert",
                f"{'='*40}",
                f"Symbol    : {symbol}",
                f"Action    : {action}",
            ]
            if rsi is not None:
                lines.append(f"RSI       : {rsi:.1f}")
            if score is not None:
                lines.append(f"Score     : {score:.1f}")
            if reason:
                lines.append(f"Reason    : {reason}")
            lines += [f"Time      : {ts}", ""]
            body = "\n".join(lines)

        elif alert_type == AlertType.TRADE_EXECUTED:
            symbol = payload.get('symbol', '?')
            action = payload.get('action', '?').upper()
            qty = payload.get('quantity', '?')
            price = payload.get('price', '?')
            strategy = payload.get('strategy', 'unknown')
            subject = f"[TradeSight] 💰 Trade: {action} {qty}x {symbol} @ ${price}"
            lines = [
                f"TradeSight Trade Alert",
                f"{'='*40}",
                f"Symbol    : {symbol}",
                f"Action    : {action}",
                f"Quantity  : {qty}",
                f"Price     : ${price}",
                f"Strategy  : {strategy}",
                f"Time      : {ts}", "",
            ]
            body = "\n".join(lines)

        elif alert_type == AlertType.DAILY_SUMMARY:
            pnl = payload.get('pnl', 0)
            trades = payload.get('trades', 0)
            positions = payload.get('positions', 0)
            subject = f"[TradeSight] 📊 Daily Summary — P&L: ${pnl:+.2f}"
            lines = [
                f"TradeSight Daily Summary",
                f"{'='*40}",
                f"Date      : {ts[:10]}",
                f"P&L       : ${pnl:+.2f}",
                f"Trades    : {trades}",
                f"Positions : {positions}", "",
            ]
            body = "\n".join(lines)

        elif alert_type == AlertType.STRATEGY_EVOLVED:
            winner = payload.get('winner', 'unknown')
            score = payload.get('score', 0)
            rounds = payload.get('rounds', '?')
            subject = f"[TradeSight] 🏆 Strategy Evolved: {winner} (score {score:.3f})"
            lines = [
                f"TradeSight Strategy Evolution Alert",
                f"{'='*40}",
                f"Winner    : {winner}",
                f"Score     : {score:.3f}",
                f"Rounds    : {rounds}",
                f"Time      : {ts}", "",
            ]
            body = "\n".join(lines)

        else:
            subject = f"[TradeSight] Alert: {alert_type.value}"
            body = json.dumps(payload, indent=2)

        return subject, body

    def _record(self, payload: Dict[str, Any]):
        """Add payload to in-memory history and persist."""
        with self._lock:
            self._history.append(payload)
            # Trim to limit
            if len(self._history) > _HISTORY_LIMIT:
                self._history = self._history[-_HISTORY_LIMIT:]

        if self.data_dir:
            self._save_history()

    def _history_path(self) -> Optional[Path]:
        if not self.data_dir:
            return None
        return self.data_dir / 'alert_history.json'

    def _load_history(self):
        path = self._history_path()
        if path and path.exists():
            try:
                with open(path) as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    with self._lock:
                        self._history = loaded[-_HISTORY_LIMIT:]
                    logger.debug(f"Loaded {len(self._history)} alerts from {path}")
            except Exception as e:
                logger.warning(f"Could not load alert history: {e}")

    def _save_history(self):
        path = self._history_path()
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = list(self._history)
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save alert history: {e}")
