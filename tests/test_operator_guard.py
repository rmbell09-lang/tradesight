import json
import os
from pathlib import Path
import sys
import tempfile
import threading

from flask import Flask, jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from security.operator_guard import OperatorGuard


def make_app(root):
    app = Flask(__name__)
    guard = OperatorGuard(Path(root), session_ttl_seconds=60)

    @app.post('/login')
    def login():
        session, error = guard.authenticate(request)
        if error:
            return jsonify({'error': error}), 401
        response = jsonify({'csrf': session['csrf']})
        response.set_cookie(guard.COOKIE_NAME, session['id'], httponly=True, samesite='Strict')
        return response

    @app.post('/control')
    def control():
        ok, reason = guard.validate(request)
        return jsonify({'ok': ok, 'reason': reason}), (200 if ok else 403)

    return app, guard


def test_operator_control_requires_token_session_and_csrf():
    with tempfile.TemporaryDirectory() as tmp:
        app, guard = make_app(tmp)
        client = app.test_client()
        assert client.post('/control').status_code == 403
        assert client.post('/login', json={'operator_token': 'wrong'}).status_code == 401
        token = guard.token_path.read_text().strip()
        login = client.post('/login', json={'operator_token': token})
        assert login.status_code == 200
        assert client.post('/control').status_code == 403
        csrf = login.get_json()['csrf']
        allowed = client.post('/control', headers={'X-CSRF-Token': csrf})
        assert allowed.status_code == 200
        assert allowed.get_json()['ok'] is True


def test_operator_token_file_is_private_and_audit_chain_verifies():
    with tempfile.TemporaryDirectory() as tmp:
        _, guard = make_app(tmp)
        assert guard.token_path.stat().st_mode & 0o777 == 0o600
        guard.audit('test_action', 'success', details={'safe': True})
        guard.audit('test_action_2', 'success', details={})
        assert guard.verify_audit_chain()['valid'] is True
        assert guard.verify_audit_chain()['entries'] == 2


def test_cross_origin_login_is_denied():
    with tempfile.TemporaryDirectory() as tmp:
        app, guard = make_app(tmp)
        client = app.test_client()
        token = guard.token_path.read_text().strip()
        response = client.post(
            '/login', json={'operator_token': token},
            headers={'Origin': 'https://evil.example'},
        )
        assert response.status_code == 401


def test_concurrent_audit_writes_preserve_single_hash_chain():
    with tempfile.TemporaryDirectory() as tmp:
        _, guard = make_app(tmp)
        threads = [
            threading.Thread(target=guard.audit, args=(f'action_{i}', 'success'))
            for i in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        verification = guard.verify_audit_chain()
        assert verification['valid'] is True
        assert verification['entries'] == 20
