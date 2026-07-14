#!/usr/bin/env python3
"""
TradeSight Unified Dashboard
Paper-trading control surface with explicit data provenance.
"""

from flask import Flask, render_template, jsonify, request, abort, make_response
from functools import wraps
import sqlite3
from contextlib import closing
import json
from datetime import datetime, timedelta, timezone
import os
import sys
import pandas as pd
import threading
import time
from pathlib import Path

def sanitize_for_json(obj):
    """Recursively convert numpy types to native Python types."""
    import numpy as np
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [sanitize_for_json(v) for v in obj]
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, np.bool_):
        return bool(obj)
    return obj


import numpy as np

class NumpySafeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


# Add project root to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from scanners.stock_scanner import StockScanner
from strategy_lab.tournament import StrategyTournament
from strategy_lab.ai_engine import create_test_data
from strategy_lab.optimizer_registry import OptimizerRegistry
from trading.accounting_truth import (
    establish_epoch,
    load_epoch,
    reconcile_accounting,
)
from trading.trade_evidence import TradeEvidenceService
from trading.runtime_risk import OperationalRiskGate
from trading.live_readiness import LiveReadinessService
from security.operator_guard import OperatorGuard
from data.alpaca_client import AlpacaClient

app = Flask(__name__, static_folder='static', static_url_path='/static')
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_operator_guard = OperatorGuard(PROJECT_ROOT)
_runtime_risk_gate = OperationalRiskGate(PROJECT_ROOT)
_live_readiness = LiveReadinessService(PROJECT_ROOT)
APP_STARTED_AT = datetime.now().astimezone()
APP_STARTED_MONOTONIC = time.monotonic()
POLYMARKET_STALE_AFTER = timedelta(hours=24)
DEMO_TOOLS_ENABLED = os.environ.get("TRADESIGHT_ENABLE_DEMO_TOOLS", "").lower() in {
    "1", "true", "yes"
}
ACCOUNTING_CACHE_SECONDS = 15
_accounting_cache = {"loaded_at": 0.0, "payload": None}
_accounting_lock = threading.Lock()
# AlertManager — for dashboard alerts tab
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
try:
    from alerts.alert_manager import AlertManager as _AlertManager
    from alerts.alert_types import AlertType as _AlertType
    from config import ALERTS_CONFIG as _ALERTS_CONFIG, save_alerts_config as _save_alerts_config, reload_alerts_config as _reload_alerts_config, DATA_DIR as _DATA_DIR
    _dashboard_alert_manager = _AlertManager(config=_ALERTS_CONFIG, data_dir=str(_DATA_DIR))
    _DASHBOARD_ALERTS_AVAILABLE = True
except Exception as _e:
    _dashboard_alert_manager = None
    _DASHBOARD_ALERTS_AVAILABLE = False

app.json_encoder = NumpySafeEncoder

def safe_jsonify(data):
    """Convert numpy types before jsonifying."""
    return json.loads(json.dumps(data, cls=NumpySafeEncoder))


def parse_observed_at(value):
    """Parse a stored timestamp without inventing freshness."""
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def provenance(kind, source, observed_at=None, message=None, **extra):
    """Create the dashboard's typed truth label."""
    payload = {
        "kind": kind,
        "source": source,
        "observed_at": (
            observed_at.astimezone(timezone.utc).isoformat()
            if isinstance(observed_at, datetime)
            else observed_at
        ),
        "message": message,
    }
    payload.update(extra)
    return payload


def load_test_suite_receipt():
    """Return the latest real pytest receipt, or UNKNOWN/STALE without guessing."""
    path = Path(__file__).resolve().parents[1] / "state" / "test-suite-status.json"
    if not path.is_file():
        return {
            "passed": None,
            "failed": None,
            "total": None,
            "provenance": provenance(
                "UNKNOWN", "state/test-suite-status.json", message="No verified test receipt"
            ),
        }
    try:
        receipt = json.loads(path.read_text())
        observed_at = parse_observed_at(receipt.get("finished_at"))
        age = datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc) if observed_at else None
        kind = "VERIFIED" if age is not None and age <= timedelta(days=7) else "STALE"
        return {
            "passed": receipt.get("passed"),
            "failed": receipt.get("failed"),
            "total": receipt.get("total"),
            "exit_code": receipt.get("exit_code"),
            "provenance": provenance(
                kind,
                "pytest receipt",
                observed_at,
                message="Latest complete local test-suite run",
                age_seconds=int(age.total_seconds()) if age is not None else None,
            ),
        }
    except (OSError, ValueError, TypeError) as exc:
        return {
            "passed": None,
            "failed": None,
            "total": None,
            "provenance": provenance(
                "UNKNOWN", str(path), message=f"Unreadable test receipt: {exc}"
            ),
        }


def load_alpaca_credentials():
    """Load Alpaca credentials through TradeSight config/Keychain, with env fallback."""
    try:
        from config import ALPACA_API_KEY, ALPACA_SECRET_KEY
        api_key = ALPACA_API_KEY or ''
        secret_key = ALPACA_SECRET_KEY or ''
        if api_key and secret_key:
            return api_key, secret_key, 'TradeSight config'
    except Exception:
        pass

    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "") or os.environ.get("ALPACA_SECRET", "")
    if api_key and secret_key:
        return api_key, secret_key, 'environment'
    return '', '', 'missing'


def _paper_broker_client():
    """Return an authenticated paper client or a typed unavailable result."""
    api_key, secret_key, credential_source = load_alpaca_credentials()
    if not api_key or not secret_key:
        return None, credential_source, "Alpaca paper credentials are unavailable"
    client = AlpacaClient(api_key=api_key, secret_key=secret_key, paper=True)
    if getattr(client, "demo_mode", False):
        return None, credential_source, "Alpaca client entered demo mode; broker truth was rejected"
    return client, credential_source, None


def get_accounting_reconciliation(force=False):
    """Return a short-lived projection of current broker/accounting truth."""
    now = time.monotonic()
    with _accounting_lock:
        cached = _accounting_cache.get("payload")
        if not force and cached is not None and now - _accounting_cache.get("loaded_at", 0) < ACCOUNTING_CACHE_SECONDS:
            return cached

        client, credential_source, error = _paper_broker_client()
        if error:
            payload = {
                "schema": "tradesight_accounting_truth.v1",
                "status": "UNAVAILABLE",
                "mode": "paper",
                "error": error,
                "credential_source": credential_source,
                "live_trading_allowed": False,
            }
        else:
            account = client.get_account() or {}
            positions = client.get_remote_positions() or []
            payload = reconcile_accounting(
                PROJECT_ROOT,
                account,
                positions,
                order_fetcher=client.get_order,
            )
            payload["credential_source"] = credential_source

        _live_readiness.record_accounting_observation(payload)

        _accounting_cache["loaded_at"] = now
        _accounting_cache["payload"] = payload
        return payload


def get_strategy_registry():
    """Read the real optimizer/tournament/champion evidence projection."""
    return OptimizerRegistry(PROJECT_ROOT).snapshot()


def operator_control(action_name):
    """Require loopback token authentication and CSRF for control mutations."""
    def decorate(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            allowed, reason = _operator_guard.validate(request)
            if not allowed:
                _operator_guard.audit(action_name, 'denied', request, {'reason': reason})
                status = 401 if 'session' in reason else 403
                return jsonify({'error': reason, 'operator_auth_required': True}), status
            return func(*args, **kwargs)
        return wrapped
    return decorate


def get_db_connection():
    """Get database connection"""
    db_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'tradesight.db')
    return sqlite3.connect(db_path)

def get_polymarket_stats():
    """Get Polymarket statistics"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Total markets and recent scans
        cursor.execute('SELECT COUNT(*) FROM markets')
        total_markets = cursor.fetchone()[0]
        
        cursor.execute('SELECT MAX(last_updated) FROM markets')
        last_scan = cursor.fetchone()[0]
        
        # High volume markets (using volume instead of volume_24h)
        cursor.execute('SELECT COUNT(*) FROM markets WHERE volume > 10000')
        high_volume_markets = cursor.fetchone()[0]
        
        # Active markets
        cursor.execute('SELECT COUNT(*) FROM markets WHERE active = 1')
        active_markets = cursor.fetchone()[0]
        
        conn.close()
        
        observed_at = parse_observed_at(last_scan)
        age = datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc) if observed_at else None
        kind = "STALE" if age is None or age > POLYMARKET_STALE_AFTER else "REAL"

        return {
            'total_markets': total_markets,
            'last_scan': last_scan,
            'active_markets': active_markets,
            'high_volume_markets': high_volume_markets,
            'provenance': provenance(
                kind,
                'local tradesight.db markets table',
                observed_at,
                message=(
                    'Archived prediction-market data; scanner is not currently maintained'
                    if kind == 'STALE'
                    else 'Locally stored prediction-market scan'
                ),
                stale_after_seconds=int(POLYMARKET_STALE_AFTER.total_seconds()),
                age_seconds=int(age.total_seconds()) if age is not None else None,
            ),
        }
    except Exception as e:
        return {
            'total_markets': 0,
            'last_scan': None,
            'active_markets': 0,
            'high_volume_markets': 0,
            'error': str(e),
            'provenance': provenance(
                'UNAVAILABLE', 'local tradesight.db markets table', message=str(e)
            ),
        }

def get_stock_stats():
    """Get stock statistics from verified Alpaca data only."""
    try:
        api_key, secret_key, credential_source = load_alpaca_credentials()
        if not api_key or not secret_key:
            return {
                'total_scanned': 0,
                'opportunities_found': 0,
                'scan_duration': 0,
                'last_scan': None,
                'top_opportunity': None,
                'top_score': 0,
                'error': 'Alpaca market-data credentials are unavailable',
                'provenance': provenance(
                    'UNAVAILABLE', 'Alpaca market data', message='No generated fallback was used'
                ),
            }

        scanner = StockScanner(
            alpaca_api_key=api_key,
            alpaca_secret=secret_key,
            paper_trading=True,
            allow_demo_data=False,
        )
        
        # Run a quick scan (using the correct method name)
        scan_result = scanner.quick_scan(limit=5)
        
        params = scan_result.scan_parameters or {}
        is_verified = bool(params.get('verified_real_data_only'))
        kind = 'REAL' if is_verified else 'UNAVAILABLE'
        result = {
            'total_scanned': scan_result.total_scanned,
            'opportunities_found': scan_result.opportunities_found,
            'scan_duration': scan_result.scan_duration_seconds,
            'last_scan': scan_result.scan_time.isoformat(),
            'top_opportunity': scan_result.top_opportunities[0].symbol if scan_result.top_opportunities else None,
            'top_score': scan_result.top_opportunities[0].overall_score if scan_result.top_opportunities else 0,
            'provenance': provenance(
                kind,
                f'Alpaca IEX via {credential_source}',
                scan_result.scan_time,
                message=(
                    'Verified Alpaca historical bars'
                    if is_verified
                    else 'No verified results; generated fallback was rejected'
                ),
                data_source_counts=params.get('data_source_counts', {}),
                skipped_demo_symbols=params.get('skipped_demo_symbols', []),
            ),
        }
        if not is_verified:
            result['error'] = 'Verified Alpaca data was unavailable; no demo signal is shown'
        return result
    except Exception as e:
        return {
            'total_scanned': 0,
            'opportunities_found': 0,
            'scan_duration': 0,
            'last_scan': None,
            'top_opportunity': None,
            'top_score': 0,
            'error': str(e),
            'provenance': provenance(
                'UNAVAILABLE', 'Alpaca market data', message='No generated fallback was shown'
            ),
        }

def get_strategy_lab_stats():
    """Return only real optimizer, tournament, and champion evidence."""
    payload = get_strategy_registry()
    payload['demo_tools_enabled'] = DEMO_TOOLS_ENABLED
    return payload


@app.route('/health')
def health():
    """Lightweight health check for LaunchAgent/browser/service monitors."""
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    data_dir = os.path.join(project_root, 'data')
    logs_dir = os.path.join(project_root, 'logs')
    reports_dir = os.path.join(project_root, 'reports')
    positions_db = os.path.join(data_dir, 'positions.db')

    payload = {
        'ok': True,
        'service': 'tradesight-dashboard',
        'timestamp': datetime.now().isoformat(),
        'started_at': APP_STARTED_AT.isoformat(),
        'uptime_seconds': int(time.monotonic() - APP_STARTED_MONOTONIC),
        'trading_mode': 'PAPER_ONLY',
        'runtime': {
            'python_version': sys.version.split()[0],
            'python_executable': sys.executable,
            'minimum_supported': '3.11',
            'supported': sys.version_info >= (3, 11),
        },
        'test_suite': load_test_suite_receipt(),
        'checks': {
            'positions_db_exists': os.path.exists(positions_db),
            'logs_dir_exists': os.path.isdir(logs_dir),
            'reports_dir_exists': os.path.isdir(reports_dir),
        },
    }

    try:
        if os.path.exists(positions_db):
            with closing(sqlite3.connect(positions_db)) as connection, connection as conn:
                rows = conn.execute("SELECT status, COUNT(*) FROM positions GROUP BY status").fetchall()
            payload['position_counts'] = {str(status): int(count) for status, count in rows}
            receipt_path = PROJECT_ROOT / 'state' / 'accounting-reconciliation.json'
            accounting_kind = 'UNKNOWN'
            accounting_observed = None
            accounting_message = 'No accounting reconciliation receipt'
            if receipt_path.is_file():
                receipt = json.loads(receipt_path.read_text())
                accounting_observed = parse_observed_at(receipt.get('observed_at'))
                age = datetime.now(timezone.utc) - accounting_observed if accounting_observed else None
                accounting_kind = receipt.get('status') or 'UNKNOWN'
                if age is not None and age > timedelta(minutes=30):
                    accounting_kind = 'STALE'
                    accounting_message = 'Last broker reconciliation is older than 30 minutes'
                else:
                    accounting_message = 'Current counts were broker-reconciled at the recorded time'
            payload['checks']['accounting_reconciliation'] = accounting_kind
            payload['position_counts_provenance'] = provenance(
                accounting_kind,
                'state/accounting-reconciliation.json',
                accounting_observed,
                message=accounting_message,
            )
    except Exception as e:
        payload['ok'] = False
        payload['checks']['positions_db_read_error'] = str(e)

    for label, folder, prefix in (
        ('latest_trading_report', logs_dir, 'trading_report_'),
        ('latest_optimization_report', reports_dir, 'optimization_'),
    ):
        try:
            files = [os.path.join(folder, f) for f in os.listdir(folder) if f.startswith(prefix)]
            if files:
                latest = max(files, key=os.path.getmtime)
                payload[label] = {
                    'path': latest,
                    'mtime': datetime.fromtimestamp(os.path.getmtime(latest)).isoformat(),
                }
        except Exception as e:
            payload.setdefault('warnings', {})[label] = str(e)

    status = 200 if payload.get('ok') else 500
    return jsonify(sanitize_for_json(payload)), status

@app.route('/')
def dashboard():
    """Main dashboard with all market types"""
    return render_template('unified_dashboard.html')

@app.route('/api/polymarket/stats')
def polymarket_stats():
    """API endpoint for Polymarket statistics"""
    return jsonify(sanitize_for_json(get_polymarket_stats()))

@app.route('/api/polymarket/opportunities')
def polymarket_opportunities():
    """API endpoint for Polymarket opportunities"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get top opportunities by volume
        cursor.execute('''
            SELECT question, category, volume, price_yes, price_no, last_updated
            FROM markets 
            WHERE volume > 1000
            ORDER BY volume DESC 
            LIMIT 20
        ''')
        
        opportunities = []
        for row in cursor.fetchall():
            opportunities.append({
                'question': row[0],
                'category': row[1] or 'Unknown',
                'volume': row[2],
                'yes_price': row[3],
                'no_price': row[4],
                'last_updated': row[5]
            })
        
        conn.close()
        stats = get_polymarket_stats()
        source_truth = stats.get('provenance', {})
        visible_items = opportunities if source_truth.get('kind') == 'REAL' else []
        return jsonify(sanitize_for_json({
            'items': visible_items,
            'archived_item_count': len(opportunities),
            'provenance': source_truth,
        }))
        
    except Exception as e:
        return jsonify({
            'items': [],
            'error': str(e),
            'provenance': provenance(
                'UNAVAILABLE', 'local tradesight.db markets table', message=str(e)
            ),
        })

@app.route('/api/stocks/stats')
def stocks_stats():
    """API endpoint for stock statistics"""
    return jsonify(sanitize_for_json(get_stock_stats()))

@app.route('/api/stocks/opportunities')
def stocks_opportunities():
    """API endpoint for stock opportunities"""
    try:
        api_key, secret_key, credential_source = load_alpaca_credentials()
        if not api_key or not secret_key:
            return jsonify({
                'items': [],
                'error': 'Alpaca market-data credentials are unavailable',
                'provenance': provenance(
                    'UNAVAILABLE', 'Alpaca market data', message='No generated fallback was used'
                ),
            })
        scanner = StockScanner(
            alpaca_api_key=api_key,
            alpaca_secret=secret_key,
            paper_trading=True,
            allow_demo_data=False,
        )
        scan_result = scanner.quick_scan(limit=7)
        
        opportunities = []
        for opp in scan_result.top_opportunities:
            opportunities.append({
                'symbol': opp.symbol,
                'overall_score': opp.overall_score,
                'volume_score': opp.volume_score,
                'volatility_score': opp.volatility_score,
                'technical_score': opp.technical_score,
                'momentum_score': opp.momentum_score,
                'trend_score': opp.trend_score,
                'confidence': opp.confidence,
                'direction': opp.direction,
                'current_price': getattr(opp, 'current_price', 0),
                'volume': getattr(opp, 'volume', 0),
                'market_cap': getattr(opp, 'market_cap', 0)
            })
        
        params = scan_result.scan_parameters or {}
        is_verified = bool(params.get('verified_real_data_only'))
        truth = provenance(
            'REAL' if is_verified else 'UNAVAILABLE',
            f'Alpaca IEX via {credential_source}',
            scan_result.scan_time,
            message=(
                'Verified Alpaca historical bars'
                if is_verified
                else 'Generated fallback was rejected; signals are hidden'
            ),
            data_source_counts=params.get('data_source_counts', {}),
            skipped_demo_symbols=params.get('skipped_demo_symbols', []),
        )
        return jsonify(sanitize_for_json({
            'items': opportunities if is_verified else [],
            'provenance': truth,
            'error': None if is_verified else 'Verified Alpaca data unavailable',
        }))
        
    except Exception as e:
        return jsonify({
            'items': [],
            'error': str(e),
            'provenance': provenance(
                'UNAVAILABLE', 'Alpaca market data', message='No generated fallback was shown'
            ),
        })


@app.route('/api/paper-trading/status')
def paper_trading_status():
    """Return paper status with broker truth separated from legacy local history."""
    api_key, secret_key, credential_source = load_alpaca_credentials()
    accounting = get_accounting_reconciliation()
    local = accounting.get('local') or {}
    summary = local.get('verification_summary') or {}
    closed_positions = sum(
        int(item.get('count') or 0)
        for key, item in summary.items()
        if key not in ('broker_position_confirmed', 'broker_position_mismatch', 'pending_broker_reconciliation')
    )
    open_positions = len(local.get('open_positions') or {})
    kind = accounting.get('status') or 'UNAVAILABLE'
    status = {
        'mode': 'paper',
        'credential_source': credential_source,
        'credentials_configured': bool(api_key and secret_key),
        'open_positions': open_positions,
        'closed_positions': closed_positions,
        'broker_equity': (accounting.get('broker') or {}).get('equity'),
        'broker_equity_change_since_epoch': (accounting.get('broker') or {}).get('equity_change_since_epoch'),
        'trusted_realized_pnl': local.get('trusted_realized_pnl', 0.0),
        'trusted_closed_trades': local.get('trusted_closed_trades', 0),
        'legacy_unverified_realized_pnl': local.get('legacy_unverified_realized_pnl', 0.0),
        'legacy_unverified_closed_trades': local.get('legacy_unverified_closed_trades', 0),
        'accounting': accounting,
        'provenance': provenance(
            kind,
            accounting.get('source') or 'accounting truth unavailable',
            accounting.get('observed_at'),
            message='Legacy local P&L is preserved but excluded from trusted performance',
        ),
        'pnl_label': 'Broker-verified realized P&L since accounting epoch',
        'live_trading_allowed': False,
    }
    return jsonify(sanitize_for_json(status))


@app.route('/api/paper-trading/evidence')
def paper_trading_evidence():
    """Broker positions, order lifecycle, fill activity, and local trade proof."""
    client, credential_source, error = _paper_broker_client()
    service = TradeEvidenceService(PROJECT_ROOT, broker_client=client)
    payload = service.snapshot(limit=request.args.get('limit', 100))
    payload['credential_source'] = credential_source
    if error:
        payload.setdefault('errors', []).append(error)
        payload['status'] = 'UNAVAILABLE'
        payload['provenance']['kind'] = 'UNAVAILABLE'
    return jsonify(sanitize_for_json(payload))


@app.route('/api/paper-trading/trades/<int:position_id>/why')
def paper_trade_explanation(position_id):
    """Explain a recorded position without inventing missing historical context."""
    explanation = TradeEvidenceService(PROJECT_ROOT).why_trade(position_id)
    if not explanation:
        return jsonify({'error': 'Trade record not found'}), 404
    return jsonify(sanitize_for_json(explanation))


@app.route('/api/accounting/reconciliation')
def accounting_reconciliation():
    """Current read-only broker/local accounting comparison."""
    return jsonify(sanitize_for_json(get_accounting_reconciliation(force=True)))


@app.route('/api/accounting/epoch', methods=['POST'])
@operator_control('accounting_epoch_establish')
def accounting_epoch():
    """Establish the one-time paper accounting baseline from current broker truth."""
    if request.remote_addr not in ('127.0.0.1', '::1', None):
        return jsonify({'error': 'Accounting epoch can only be established locally'}), 403
    body = request.get_json(silent=True) or {}
    if body.get('confirm') != 'ESTABLISH_PAPER_EPOCH':
        return jsonify({'error': 'Explicit ESTABLISH_PAPER_EPOCH confirmation required'}), 400
    existing = load_epoch(PROJECT_ROOT / 'state')
    if existing:
        return jsonify({'status': 'existing', 'epoch': existing})
    client, credential_source, error = _paper_broker_client()
    if error:
        return jsonify({'error': error}), 503
    account = client.get_account() or {}
    positions = client.get_remote_positions() or []
    try:
        epoch = establish_epoch(
            PROJECT_ROOT / 'state', account, positions,
            source='Alpaca paper Trading API via %s' % credential_source,
        )
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 503
    _accounting_cache['payload'] = None
    reconciliation = get_accounting_reconciliation(force=True)
    return jsonify(sanitize_for_json({
        'status': 'created',
        'epoch': epoch,
        'reconciliation': reconciliation,
    })), 201

@app.route('/api/strategy-lab/stats')
def strategy_lab_stats():
    """API endpoint for Strategy Lab statistics"""
    return jsonify(sanitize_for_json(get_strategy_lab_stats()))

@app.route('/api/strategy-lab/tournament')
def strategy_lab_tournament():
    """Return the latest persisted real-data tournament; never run synthetic work."""
    snapshot = get_strategy_registry()
    latest = snapshot.get('latest_tournament') or {}
    if not latest:
        return jsonify({
            'error': 'No real tournament evidence is available',
            'provenance': snapshot.get('provenance'),
        }), 404
    winner = None
    for participant in latest.get('participants') or []:
        if participant.get('name') == latest.get('winner'):
            winner = participant
            break
    return jsonify(sanitize_for_json({
        'session_id': latest.get('session_id'),
        'participants': latest.get('participants') or [],
        'winner': winner or {
            'name': latest.get('winner'),
            'avg_score': latest.get('winner_avg_score'),
        },
        'rounds_completed': latest.get('rounds_completed'),
        'data_source': latest.get('data_source'),
        'provenance': snapshot.get('provenance'),
    }))


@app.route('/api/strategy-lab/demo-tournament')
def strategy_lab_demo_tournament():
    """Developer-only synthetic tournament retained behind the explicit demo flag."""
    if not DEMO_TOOLS_ENABLED:
        return jsonify({
            'error': 'Synthetic tournament tools are disabled in the production dashboard',
            'provenance': provenance('DEMO', 'generated strategy test data'),
        }), 410
    try:
        from strategy_lab.tournament import get_builtin_strategies
        
        tournament = StrategyTournament(
            initial_balance=10000.0,
            elimination_rate=0.3,
            min_survivors=2
        )
        
        # Register built-in strategies
        builtin_strategies = get_builtin_strategies()
        for name, strategy_func in builtin_strategies.items():
            tournament.register_strategy(name, strategy_func)
        
        # Create test data for tournament
        test_data = create_test_data(days=100)
        round_datasets = [
            ('Test Data', test_data)
        ]
        
        results = tournament.run_tournament(round_datasets)
        
        # Convert results to JSON-serializable format
        participants = []
        for p in tournament.entries:
            participants.append({
                'name': p.name,
                'wins': p.wins,
                'losses': p.losses,
                'total_score': p.total_score,
                'avg_score': p.avg_score,
                'eliminated': p.eliminated,
                'rounds_survived': p.rounds_survived
            })
        
        winner_data = None
        if results.winner and results.winner != 'None':
            winner_entry = next((p for p in tournament.entries if p.name == results.winner), None)
            if winner_entry:
                winner_data = {
                    'name': winner_entry.name,
                    'avg_score': winner_entry.avg_score,
                    'wins': winner_entry.wins,
                    'total_score': winner_entry.total_score
                }
        
        return jsonify(sanitize_for_json({
            'participants': participants,
            'winner': winner_data,
            'rounds_completed': results.total_rounds,
            'eliminations': results.elimination_log,
            'provenance': provenance('DEMO', 'generated strategy test data'),
        }))
        
    except Exception as e:
        return jsonify({'error': str(e)})


# Strategy Lab Management - Thread-safe tournament state
current_tournament = None
tournament_in_progress = False
tournament_results_history = []
MAX_TOURNAMENT_HISTORY = 20
tournament_lock = threading.Lock()

@app.route('/strategy-lab')
def strategy_lab():
    """Strategy Lab interface for interactive tournament management"""
    if not DEMO_TOOLS_ENABLED:
        abort(404)
    return render_template('strategy_lab.html')

@app.route('/api/strategy-lab/start-tournament', methods=['POST'])
def start_tournament():
    """Start a new tournament with custom parameters"""
    global current_tournament, tournament_in_progress

    if not DEMO_TOOLS_ENABLED:
        return jsonify({
            'error': 'Synthetic tournament tools are disabled in the production dashboard',
            'provenance': provenance('DEMO', 'generated strategy test data'),
        }), 410
    
    try:
        data = request.get_json() or {}
        
        # Tournament parameters
        initial_balance = data.get('initial_balance', 10000.0)
        elimination_rate = data.get('elimination_rate', 0.3)
        min_survivors = data.get('min_survivors', 2)
        max_rounds = data.get('max_rounds', 3)
        data_days = max(60, data.get('data_days', 100))
        
        with tournament_lock:
            if tournament_in_progress:
                return jsonify({'error': 'Tournament already in progress'}), 400
            tournament_in_progress = True
        
        # Create tournament
        tournament = StrategyTournament(
            initial_balance=initial_balance,
            elimination_rate=elimination_rate,
            min_survivors=min_survivors
        )
        
        # Register built-in strategies
        from strategy_lab.tournament import get_builtin_strategies
        builtin_strategies = get_builtin_strategies()
        for name, strategy_func in builtin_strategies.items():
            tournament.register_strategy(name, strategy_func)
        
        # Create test data
        test_data = create_test_data(days=data_days)
        round_datasets = [('Test Data', test_data)]
        
        # Run tournament (blocking but protected by lock)
        results = tournament.run_tournament(round_datasets)
        tournament_in_progress = False
        
        # Store results (TournamentResults dataclass)
        current_tournament = results
        # Trim history to prevent unbounded memory growth
        if len(tournament_results_history) >= MAX_TOURNAMENT_HISTORY:
            tournament_results_history.pop(0)
        tournament_results_history.append({
            'timestamp': datetime.now().isoformat(),
            'results': results,
            'tournament_ref': tournament,
            'parameters': {
                'initial_balance': initial_balance,
                'elimination_rate': elimination_rate,
                'min_survivors': min_survivors,
                'max_rounds': max_rounds,
                'data_days': data_days
            }
        })
        # Cap history to prevent unbounded memory growth
        while len(tournament_results_history) > 20:
            tournament_results_history.pop(0)
        
        # Convert results for JSON (results is TournamentResults dataclass)
        participants = []
        for p in tournament.entries:  # Get participants from tournament entries
            participants.append({
                'name': p.name,
                'wins': p.wins,
                'losses': p.losses,
                'total_score': p.total_score,
                'avg_score': p.avg_score,
                'eliminated': p.eliminated,
                'rounds_survived': p.rounds_survived
            })
        
        winner_data = None
        if results.winner != 'None':
            winner_entry = next((p for p in tournament.entries if p.name == results.winner), None)
            if winner_entry:
                winner_data = {
                    'name': winner_entry.name,
                    'avg_score': winner_entry.avg_score,
                    'wins': winner_entry.wins,
                    'total_score': winner_entry.total_score
                }
        
        return jsonify({
            'status': 'completed',
            'participants': participants,
            'winner': winner_data,
            'rounds_completed': results.total_rounds,
            'eliminations': results.elimination_log,
            'provenance': provenance('DEMO', 'generated strategy test data'),
        })
        
    except Exception as e:
        tournament_in_progress = False
        return jsonify({'error': str(e)}), 500

@app.route('/api/strategy-lab/status')
def tournament_status():
    """Return persisted real optimizer status; never imply an in-memory demo run."""
    snapshot = get_strategy_registry()
    return jsonify(sanitize_for_json({
        'in_progress': False,
        'has_results': bool(snapshot.get('latest_tournament')),
        'history_count': len(snapshot.get('history') or []),
        'candidate': snapshot.get('candidate'),
        'champion': snapshot.get('champion'),
        'demo_tools_enabled': DEMO_TOOLS_ENABLED,
        'provenance': snapshot.get('provenance'),
        'live_trading_allowed': False,
    }))

@app.route('/api/strategy-lab/results')
def tournament_results():
    """Return latest real tournament plus optimizer qualification evidence."""
    snapshot = get_strategy_registry()
    latest = snapshot.get('latest_tournament') or {}
    if not latest:
        return jsonify({'error': 'No real tournament results available'}), 404
    return jsonify(sanitize_for_json({
        'latest_tournament': latest,
        'champion': snapshot.get('champion'),
        'candidate': snapshot.get('candidate'),
        'latest_optimizer': snapshot.get('latest_optimizer'),
        'lifecycle': snapshot.get('lifecycle'),
        'provenance': snapshot.get('provenance'),
        'live_trading_allowed': False,
    }))

@app.route('/api/strategy-lab/export-winner')
def export_winner():
    """Export the real frozen champion record, not a transient synthetic winner."""
    snapshot = get_strategy_registry()
    champion = snapshot.get('champion') or {}
    if not champion:
        return jsonify({'error': 'No champion record available'}), 404
    return jsonify(sanitize_for_json({
        'strategy_name': champion.get('strategy'),
        'stage': 'CHAMPION',
        'parameters': champion.get('parameters'),
        'performance': {
            'backtest_score': champion.get('backtest_score'),
            'forward_avg_pnl': champion.get('forward_avg_pnl'),
            'forward_sessions': champion.get('forward_sessions'),
        },
        'promoted_at': champion.get('promoted_at'),
        'promotion_reason': champion.get('promotion_reason'),
        'provenance': snapshot.get('provenance'),
        'live_trading_allowed': False,
    }))

@app.route('/api/strategy-lab/history')
def tournament_history():
    """Get persisted real-data tournament history."""
    snapshot = get_strategy_registry()
    return jsonify(sanitize_for_json(snapshot.get('history') or []))

# ===========================================================================
# Alerts API routes (Phase 5.1)
# ===========================================================================

@app.route('/api/security/operator-session', methods=['GET'])
def operator_session_status():
    return jsonify(_operator_guard.status(request))


@app.route('/api/security/operator-session', methods=['POST'])
def operator_session_create():
    session, error = _operator_guard.authenticate(request)
    if error:
        return jsonify({'error': error}), 401
    response = make_response(jsonify({
        'authenticated': True,
        'csrf_token': session['csrf'],
        'expires_at': session['expires_at'],
    }))
    response.set_cookie(
        _operator_guard.COOKIE_NAME,
        session['id'],
        httponly=True,
        samesite='Strict',
        secure=False,
        max_age=_operator_guard.session_ttl_seconds,
    )
    return response


@app.route('/api/security/operator-session', methods=['DELETE'])
@operator_control('operator_session_revoke')
def operator_session_delete():
    _operator_guard.revoke(request)
    response = make_response(jsonify({'authenticated': False}))
    response.delete_cookie(_operator_guard.COOKIE_NAME)
    return response


@app.route('/api/security/risk-status')
def security_risk_status():
    return jsonify(sanitize_for_json(get_risk_posture()))


def get_risk_posture(accounting=None):
    """Return the current protected paper-risk posture without enabling live trading."""
    accounting = accounting or get_accounting_reconciliation()
    alert_stats = (
        _dashboard_alert_manager.get_alert_stats()
        if _DASHBOARD_ALERTS_AVAILABLE and _dashboard_alert_manager
        else {'alerts_enabled': False, 'local_safety_recording': False}
    )
    runtime = _runtime_risk_gate.status()
    accounting_status = accounting.get('status') or 'UNAVAILABLE'
    outbound_ready = bool(
        alert_stats.get('alerts_enabled')
        and (alert_stats.get('email_enabled') or alert_stats.get('webhook_enabled'))
    )
    mandatory = {
        'paper_only': True,
        'operator_controls_protected': True,
        'csrf_protected': True,
        'audit_chain_valid': _operator_guard.verify_audit_chain().get('valid', False),
        'local_safety_alerts': bool(alert_stats.get('local_safety_recording')),
        'accounting_verified': accounting_status == 'VERIFIED',
        'outbound_alert_channel': outbound_ready,
    }
    return {
        'schema': 'tradesight_risk_posture.v1',
        'status': 'READY' if all(mandatory.values()) else 'DEGRADED',
        'mandatory_controls': mandatory,
        'runtime': runtime,
        'operator': _operator_guard.status(request),
        'audit_chain': _operator_guard.verify_audit_chain(),
        'alerts': alert_stats,
        'accounting_status': accounting_status,
        'new_entries_allowed': not runtime.get('new_entries_suspended', True),
        'exits_always_allowed': True,
        'live_trading_allowed': False,
    }


@app.route('/api/live-readiness')
def live_readiness():
    """Evidence-backed readiness checklist; this endpoint cannot unlock trading."""
    accounting = get_accounting_reconciliation()
    strategy = get_strategy_registry()
    risk = get_risk_posture(accounting=accounting)
    return jsonify(sanitize_for_json(_live_readiness.snapshot(accounting, strategy, risk)))

@app.route('/api/alerts/recent')
def alerts_recent():
    """Return recent alert history."""
    if not _DASHBOARD_ALERTS_AVAILABLE or not _dashboard_alert_manager:
        return jsonify({'alerts': [], 'error': 'Alerts module not available'})
    limit = min(int(request.args.get('limit', 50)), 200)
    alerts = _dashboard_alert_manager.get_recent_alerts(limit=limit)
    return jsonify({'alerts': alerts})


@app.route('/api/alerts/stats')
def alerts_stats():
    """Return alert summary statistics."""
    if not _DASHBOARD_ALERTS_AVAILABLE or not _dashboard_alert_manager:
        return jsonify({'total': 0, 'alerts_enabled': False, 'error': 'Alerts module not available'})
    return jsonify(_dashboard_alert_manager.get_alert_stats())


@app.route('/api/alerts/config', methods=['GET'])
def alerts_config_get():
    """Return current alerts configuration (no credentials)."""
    if not _DASHBOARD_ALERTS_AVAILABLE:
        return jsonify({'error': 'Alerts module not available'}), 500
    safe_config = {k: v for k, v in _ALERTS_CONFIG.items()
                   if k not in ('smtp_password', 'smtp_username')}
    safe_config['smtp_password'] = '***' if _ALERTS_CONFIG.get('smtp_password') else ''
    safe_config['smtp_username'] = _ALERTS_CONFIG.get('smtp_username', '')
    return jsonify(safe_config)


@app.route('/api/alerts/config', methods=['POST'])
@operator_control('alerts_config_save')
def alerts_config_save():
    """Save alerts configuration."""
    if not _DASHBOARD_ALERTS_AVAILABLE:
        return jsonify({'error': 'Alerts module not available'}), 500
    data = request.get_json() or {}
    # Protect — don't overwrite password if placeholder sent
    if data.get('smtp_password') == '***':
        data.pop('smtp_password', None)
    ok = _save_alerts_config(data)
    if ok:
        _reload_alerts_config()
        # Refresh the in-process alert manager config
        if _dashboard_alert_manager:
            _dashboard_alert_manager.config.update(_ALERTS_CONFIG)
        _operator_guard.audit('alerts_config_save', 'success', request, {
            'alerts_enabled': bool(data.get('alerts_enabled')),
            'email_enabled': bool(data.get('email_enabled')),
            'webhook_enabled': bool(data.get('webhook_enabled')),
        })
        return jsonify({'status': 'saved'})
    return jsonify({'error': 'Failed to save config'}), 500


@app.route('/api/alerts/test', methods=['POST'])
@operator_control('alerts_test')
def alerts_test():
    """Send a test alert through all configured channels."""
    if not _DASHBOARD_ALERTS_AVAILABLE or not _dashboard_alert_manager:
        return jsonify({'error': 'Alerts module not available'}), 500
    try:
        fired = _dashboard_alert_manager.fire(
            _AlertType.SIGNAL_FIRED,
            symbol='TEST',
            action='buy',
            score=99.9,
            reason='Dashboard test alert',
        )
        _operator_guard.audit('alerts_test', 'success', request, {'delivered': bool(fired)})
        return jsonify({'sent': fired})
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/api/security/manual-suspension', methods=['POST'])
@operator_control('manual_suspension_enable')
def manual_suspension_enable():
    body = request.get_json(silent=True) or {}
    if body.get('confirm') != 'SUSPEND_PAPER_ENTRIES':
        return jsonify({'error': 'Explicit SUSPEND_PAPER_ENTRIES confirmation required'}), 400
    payload = {
        'schema': 'tradesight_manual_suspension.v1',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'reason': body.get('reason') or 'Operator initiated safety suspension',
        'new_entries_suspended': True,
        'exits_allowed': True,
    }
    path = PROJECT_ROOT / 'state' / 'trading-manual-suspension.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)
    _operator_guard.audit('manual_suspension_enable', 'success', request, {'reason': payload['reason']})
    if _dashboard_alert_manager:
        _dashboard_alert_manager.fire_safety(_AlertType.TRADING_SUSPENDED, reasons=[payload['reason']])
    return jsonify(payload), 201


@app.route('/api/security/manual-suspension', methods=['DELETE'])
@operator_control('manual_suspension_clear')
def manual_suspension_clear():
    body = request.get_json(silent=True) or {}
    if body.get('confirm') != 'RESUME_PAPER_ENTRIES':
        return jsonify({'error': 'Explicit RESUME_PAPER_ENTRIES confirmation required'}), 400
    path = PROJECT_ROOT / 'state' / 'trading-manual-suspension.json'
    if path.exists():
        path.unlink()
    _operator_guard.audit('manual_suspension_clear', 'success', request, {})
    return jsonify({'new_entries_suspended': False, 'exits_allowed': True})


@app.route('/api/emergency/close-all-positions', methods=['POST'])
@operator_control('emergency_close_all_paper_positions')
def emergency_close_all_positions():
    """Submit paper-broker closes without fabricating local fill completion."""
    try:
        from trading.position_manager import PositionManager

        body = request.get_json(silent=True) or {}
        if body.get('confirm') != 'CLOSE_ALL_PAPER_POSITIONS':
            return jsonify({'error': 'Explicit CLOSE_ALL_PAPER_POSITIONS confirmation required'}), 400

        client, credential_source, error = _paper_broker_client()
        if error:
            return jsonify({'error': error}), 503
        alpaca_positions = client.get_remote_positions()
        if isinstance(alpaca_positions, dict) and alpaca_positions.get('error'):
            return jsonify({'error': alpaca_positions['error']}), 503
        closed = []
        errors = []

        for pos in alpaca_positions:
            symbol = pos.get('symbol')
            qty = float(pos.get('qty', 0))
            result = client.close_full_position(symbol)
            if 'error' not in result:
                closed.append({
                    'symbol': symbol,
                    'qty': qty,
                    'order_id': result.get('order_id'),
                    'broker_status': result.get('status'),
                    'fill_price': result.get('fill_price'),
                })
            else:
                errors.append({'symbol': symbol, 'error': result.get('error')})

        # Preserve local rows as open until broker fill reconciliation confirms
        # the exit. Marking them closed here would fabricate fills and P&L.
        pm = PositionManager(base_dir=PROJECT_ROOT)
        db_path = pm.data_dir / 'positions.db'
        with closing(sqlite3.connect(db_path)) as connection, connection as conn:
            for item in closed:
                conn.execute(
                    "UPDATE positions SET exit_order_id=COALESCE(?,exit_order_id), "
                    "exit_fill_status=?, exit_reason='emergency_operator_close', "
                    "updated_at=CURRENT_TIMESTAMP WHERE symbol=? AND status='open'",
                    (item.get('order_id'), item.get('broker_status') or 'pending_emergency_close', item['symbol'])
                )
            conn.commit()

        suspension_path = PROJECT_ROOT / 'state' / 'trading-manual-suspension.json'
        suspension_path.write_text(json.dumps({
            'schema': 'tradesight_manual_suspension.v1',
            'created_at': datetime.now(timezone.utc).isoformat(),
            'reason': 'Emergency close-all invoked; review required before new entries',
            'new_entries_suspended': True,
            'exits_allowed': True,
        }, indent=2, sort_keys=True) + '\n')
        _operator_guard.audit('emergency_close_all_paper_positions', 'partial' if errors else 'success', request, {
            'symbols_submitted': [item['symbol'] for item in closed],
            'error_count': len(errors),
            'local_rows_fabricated_closed': 0,
        })
        if _dashboard_alert_manager:
            _dashboard_alert_manager.fire_safety(
                _AlertType.TRADING_SUSPENDED,
                reasons=['Emergency close-all invoked'],
                symbols=[item['symbol'] for item in closed],
            )
        payload = {
            'broker_close_submissions': closed,
            'errors': errors,
            'local_rows_fabricated_closed': 0,
            'new_entries_suspended': True,
            'credential_source': credential_source,
        }
        return jsonify(payload), (207 if errors else 200)
    except Exception as e:
        _operator_guard.audit('emergency_close_all_paper_positions', 'error', request, {'error': str(e)})
        return jsonify({'error': str(e)}), 500



@app.route('/api/emergency/restore-positions', methods=['POST'])
@operator_control('emergency_restore_paper_positions')
def emergency_restore_positions():
    """Fetch open Alpaca positions and restore them to local DB for SL/TP tracking."""
    try:
        from trading.position_manager import PositionManager

        body = request.get_json(silent=True) or {}
        if body.get('confirm') != 'RESTORE_PAPER_POSITIONS':
            return jsonify({'error': 'Explicit RESTORE_PAPER_POSITIONS confirmation required'}), 400
        client, credential_source, error = _paper_broker_client()
        if error:
            return jsonify({'error': error}), 503
        alpaca_positions = client.get_remote_positions()
        pm = PositionManager(base_dir=PROJECT_ROOT)
        db_path = pm.data_dir / 'positions.db'
        restored = []

        with closing(sqlite3.connect(db_path)) as connection, connection as conn:
            for pos in alpaca_positions:
                symbol = pos.get('symbol')
                qty = float(pos.get('qty', 0))
                entry_price = float(pos.get('avg_entry_price', 0))
                side = 'long' if float(pos.get('qty', 0)) > 0 else 'short'

                # Check if already in DB
                existing = conn.execute(
                    "SELECT id FROM positions WHERE symbol=? AND status='open'",
                    (symbol,)
                ).fetchone()

                if not existing:
                    conn.execute(
                        "INSERT INTO positions (symbol, strategy, side, quantity, entry_price, current_price, "
                        "status, entry_time, entry_fill_status, verification_status, verification_reason, "
                        "entry_reason, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                        (symbol, 'Broker Recovered / Unattributed', side, abs(qty), entry_price, entry_price,
                         datetime.now(timezone.utc).isoformat(), 'broker_position_confirmed',
                         'broker_position_confirmed', 'Restored directly from current Alpaca paper position',
                         'Emergency broker restore; original strategy attribution unavailable')
                    )
                    restored.append({'symbol': symbol, 'qty': abs(qty), 'entry_price': entry_price,
                                     'strategy': 'Broker Recovered / Unattributed'})
            conn.commit()

        _operator_guard.audit('emergency_restore_paper_positions', 'success', request, {
            'restored_symbols': [item['symbol'] for item in restored],
            'broker_position_count': len(alpaca_positions),
        })
        if restored and _dashboard_alert_manager:
            _dashboard_alert_manager.fire_safety(
                _AlertType.UNEXPECTED_POSITION,
                symbols=[item['symbol'] for item in restored],
                action='broker_restore',
            )
        return jsonify({
            'restored': restored,
            'alpaca_positions': len(alpaca_positions),
            'credential_source': credential_source,
        })
    except Exception as e:
        _operator_guard.audit('emergency_restore_paper_positions', 'error', request, {'error': str(e)})
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(
        debug=False,
        host=os.environ.get("TRADESIGHT_HOST", "127.0.0.1"),
        port=int(os.environ.get("TRADESIGHT_PORT", "5001")),
    )
