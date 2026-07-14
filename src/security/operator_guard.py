"""Loopback operator authentication, CSRF defense, and action receipts."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse


class OperatorGuard:
    """Protect mutating control routes with a local token and short session."""

    COOKIE_NAME = "tradesight_operator"

    def __init__(self, project_root: Path, session_ttl_seconds: int = 1200):
        self.project_root = Path(project_root)
        self.state_dir = self.project_root / "state"
        self.token_path = self.state_dir / "operator-control-token"
        self.audit_path = self.state_dir / "security-action-audit.jsonl"
        self.session_ttl_seconds = int(session_ttl_seconds)
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._token = self._load_or_create_token()

    def _load_or_create_token(self) -> str:
        configured = os.environ.get("TRADESIGHT_OPERATOR_TOKEN", "").strip()
        if configured:
            return configured
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.token_path.is_file():
            token = self.token_path.read_text().strip()
            if len(token) >= 32:
                os.chmod(self.token_path, 0o600)
                return token
        token = secrets.token_urlsafe(32)
        temporary = self.token_path.with_suffix(".tmp")
        temporary.write_text(token + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(self.token_path)
        os.chmod(self.token_path, 0o600)
        return token

    def status(self, request=None) -> Dict[str, Any]:
        session = self._session_from_request(request) if request is not None else None
        return {
            "schema": "tradesight_operator_guard.v1",
            "protected": True,
            "token_configured": bool(self._token),
            "session_authenticated": bool(session),
            "session_expires_at": session.get("expires_at") if session else None,
            "csrf_required": True,
            "loopback_only": True,
            "audit_log": str(self.audit_path.relative_to(self.project_root)),
        }

    def authenticate(self, request) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        if not self._is_loopback(request.remote_addr):
            return None, "operator authentication is loopback-only"
        if not self._same_origin(request):
            return None, "cross-origin operator authentication denied"
        body = request.get_json(silent=True) or {}
        supplied = str(body.get("operator_token") or "")
        if not hmac.compare_digest(supplied, self._token):
            self.audit("operator_session", "denied", request, {"reason": "invalid_token"})
            return None, "invalid operator token"
        now = time.time()
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        session = {"id": session_id, "csrf": csrf, "expires_at": now + self.session_ttl_seconds}
        with self._lock:
            self._sessions[session_id] = session
            self._prune(now)
        self.audit("operator_session", "authorized", request, {})
        return session, None

    def validate(self, request) -> Tuple[bool, str]:
        if not self._is_loopback(request.remote_addr):
            return False, "control route is loopback-only"
        if not self._same_origin(request):
            return False, "cross-origin request denied"
        session = self._session_from_request(request)
        if not session:
            return False, "authenticated operator session required"
        supplied = str(request.headers.get("X-CSRF-Token") or "")
        if not supplied or not hmac.compare_digest(supplied, session["csrf"]):
            return False, "valid CSRF token required"
        return True, "authorized"

    def revoke(self, request) -> None:
        session_id = request.cookies.get(self.COOKIE_NAME)
        if session_id:
            with self._lock:
                self._sessions.pop(session_id, None)
        self.audit("operator_session", "revoked", request, {})

    def audit(self, action: str, outcome: str, request=None, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        details = dict(details or {})
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            previous_hash = "GENESIS"
            if self.audit_path.is_file():
                try:
                    last = self.audit_path.read_text().splitlines()[-1]
                    previous_hash = json.loads(last).get("hash") or "GENESIS"
                except (OSError, ValueError, IndexError, TypeError):
                    previous_hash = "UNREADABLE_PREVIOUS"
            payload = {
                "schema": "tradesight_security_action.v1",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "action": action,
                "outcome": outcome,
                "remote_addr": getattr(request, "remote_addr", None) if request is not None else None,
                "details": details,
                "previous_hash": previous_hash,
            }
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            payload["hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
        return payload

    def verify_audit_chain(self) -> Dict[str, Any]:
        if not self.audit_path.is_file():
            return {"valid": True, "entries": 0, "last_hash": "GENESIS"}
        previous = "GENESIS"
        entries = 0
        try:
            for line in self.audit_path.read_text().splitlines():
                row = json.loads(line)
                actual = row.pop("hash")
                if row.get("previous_hash") != previous:
                    return {"valid": False, "entries": entries, "reason": "previous_hash_mismatch"}
                canonical = json.dumps(row, sort_keys=True, separators=(",", ":"))
                expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                if not hmac.compare_digest(actual, expected):
                    return {"valid": False, "entries": entries, "reason": "entry_hash_mismatch"}
                previous = actual
                entries += 1
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return {"valid": False, "entries": entries, "reason": str(exc)}
        return {"valid": True, "entries": entries, "last_hash": previous}

    def _session_from_request(self, request) -> Optional[Dict[str, Any]]:
        if request is None:
            return None
        session_id = request.cookies.get(self.COOKIE_NAME)
        if not session_id:
            return None
        now = time.time()
        with self._lock:
            self._prune(now)
            session = self._sessions.get(session_id)
            if not session or session["expires_at"] <= now:
                return None
            return dict(session)

    def _prune(self, now: float) -> None:
        expired = [key for key, value in self._sessions.items() if value["expires_at"] <= now]
        for key in expired:
            self._sessions.pop(key, None)

    @staticmethod
    def _is_loopback(remote_addr: Optional[str]) -> bool:
        return remote_addr in (None, "127.0.0.1", "::1")

    @staticmethod
    def _same_origin(request) -> bool:
        origin = request.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.netloc == request.host and parsed.scheme in ("http", "https")
