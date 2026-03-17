"""Webhook alerter — POSTs JSON payloads to a configurable URL."""
import json
import logging
import urllib.request
import urllib.error
from typing import Dict, Any

logger = logging.getLogger(__name__)


class WebhookAlerter:
    """
    Sends alert notifications as HTTP POST requests (JSON).

    Configuration keys (from alerts config dict):
        webhook_enabled : bool — master switch, default False
        webhook_url     : str  — destination URL
        webhook_timeout : int  — request timeout in seconds (default 10)
        webhook_headers : dict — extra HTTP headers to include
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def _is_configured(self) -> bool:
        """Return True if webhook alerting is enabled and a URL is set."""
        if not self.config.get('webhook_enabled', False):
            return False
        url = self.config.get('webhook_url', '').strip()
        if not url:
            logger.warning("Webhook alerter: webhook_url not set — alerts disabled")
            return False
        if not url.startswith(('http://', 'https://')):
            logger.warning(f"Webhook alerter: invalid webhook_url '{url}' — alerts disabled")
            return False
        return True

    def send(self, payload: Dict[str, Any]) -> bool:
        """
        POST a JSON payload to the configured webhook URL.

        Args:
            payload: Dictionary that will be serialised to JSON

        Returns:
            True if the server responded with 2xx, False otherwise
        """
        if not self._is_configured():
            logger.debug("Webhook alerter not configured — skipping send")
            return False

        url = self.config['webhook_url'].strip()
        timeout = int(self.config.get('webhook_timeout', 10))
        extra_headers: Dict[str, str] = self.config.get('webhook_headers', {}) or {}

        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                url,
                data=data,
                method='POST',
            )
            req.add_header('Content-Type', 'application/json')
            req.add_header('User-Agent', 'TradeSight-Alerts/1.0')
            for header_name, header_val in extra_headers.items():
                req.add_header(header_name, header_val)

            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                if 200 <= status < 300:
                    logger.info(f"Webhook alert sent to {url} — HTTP {status}")
                    return True
                else:
                    logger.warning(f"Webhook alert: unexpected HTTP {status} from {url}")
                    return False

        except urllib.error.HTTPError as e:
            logger.error(f"Webhook alert: HTTP error {e.code} from {url} — {e.reason}")
        except urllib.error.URLError as e:
            logger.error(f"Webhook alert: URL error for {url} — {e.reason}")
        except Exception as e:
            logger.error(f"Webhook alert: unexpected error — {e}")

        return False
