"""SMTP email alerter for TradeSight notifications."""
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class EmailAlerter:
    """
    Sends alert notifications via SMTP email.

    Configuration keys (from alerts config dict):
        email_enabled   : bool  — master switch, default False
        smtp_host       : str   — SMTP server host
        smtp_port       : int   — SMTP server port (default 587)
        smtp_use_tls    : bool  — use STARTTLS (default True)
        smtp_username   : str   — login username (empty = no auth)
        smtp_password   : str   — login password
        email_from      : str   — From address
        email_to        : list  — list of recipient addresses
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def _is_configured(self) -> bool:
        """Return True if email alerting is enabled and minimally configured."""
        if not self.config.get('email_enabled', False):
            return False
        required = ['smtp_host', 'email_from', 'email_to']
        for key in required:
            val = self.config.get(key)
            if not val:
                logger.warning(f"Email alerter: missing config key '{key}' — alerts disabled")
                return False
        recipients = self.config.get('email_to', [])
        if isinstance(recipients, str):
            recipients = [recipients]
        if not recipients:
            logger.warning("Email alerter: email_to is empty — alerts disabled")
            return False
        return True

    def send(self, subject: str, body: str, html_body: Optional[str] = None) -> bool:
        """
        Send an email alert.

        Args:
            subject:   Email subject line
            body:      Plain-text body
            html_body: Optional HTML body (falls back to plain text)

        Returns:
            True if sent successfully, False otherwise
        """
        if not self._is_configured():
            logger.debug("Email alerter not configured — skipping send")
            return False

        smtp_host = self.config['smtp_host']
        smtp_port = int(self.config.get('smtp_port', 587))
        use_tls = self.config.get('smtp_use_tls', True)
        username = self.config.get('smtp_username', '')
        password = self.config.get('smtp_password', '')
        from_addr = self.config['email_from']
        to_addrs = self.config['email_to']
        if isinstance(to_addrs, str):
            to_addrs = [to_addrs]

        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = from_addr
            msg['To'] = ', '.join(to_addrs)

            msg.attach(MIMEText(body, 'plain'))
            if html_body:
                msg.attach(MIMEText(html_body, 'html'))

            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                if use_tls:
                    server.starttls()
                if username and password:
                    server.login(username, password)
                server.sendmail(from_addr, to_addrs, msg.as_string())

            logger.info(f"Email alert sent: '{subject}' → {to_addrs}")
            return True

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"Email alert: SMTP auth failed — {e}")
        except smtplib.SMTPConnectError as e:
            logger.error(f"Email alert: cannot connect to {smtp_host}:{smtp_port} — {e}")
        except Exception as e:
            logger.error(f"Email alert: unexpected error — {e}")

        return False
