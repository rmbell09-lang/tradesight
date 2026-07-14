"""Tests for the unified dashboard truth layer."""

import os
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'web'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))


def test_unified_dashboard_imports():
    from dashboard import app, get_polymarket_stats, get_stock_stats, get_strategy_lab_stats

    assert app is not None
    assert app.name == 'dashboard'
    assert callable(get_polymarket_stats)
    assert callable(get_stock_stats)
    assert callable(get_strategy_lab_stats)


def test_polymarket_stats_has_explicit_provenance():
    from dashboard import get_polymarket_stats

    stats = get_polymarket_stats()
    for key in ('total_markets', 'last_scan', 'active_markets', 'high_volume_markets'):
        assert key in stats
    assert stats['provenance']['kind'] in {'REAL', 'STALE', 'UNAVAILABLE'}
    assert stats['provenance']['source']


@patch('dashboard.load_alpaca_credentials', return_value=('', '', 'missing'))
def test_stock_stats_fails_closed_without_credentials(_credentials):
    from dashboard import get_stock_stats

    stats = get_stock_stats()
    assert stats['total_scanned'] == 0
    assert stats['provenance']['kind'] == 'UNAVAILABLE'
    assert 'fallback' in stats['provenance']['message'].lower()


@patch('dashboard.load_alpaca_credentials', return_value=('key', 'secret', 'test'))
@patch('dashboard.StockScanner')
def test_stock_stats_marks_only_verified_alpaca_scan_real(scanner_cls, _credentials):
    from dashboard import get_stock_stats

    result = SimpleNamespace(
        total_scanned=5,
        opportunities_found=1,
        scan_duration_seconds=0.5,
        scan_time=datetime.now(),
        top_opportunities=[SimpleNamespace(symbol='AAPL', overall_score=72.5)],
        scan_parameters={
            'verified_real_data_only': True,
            'data_source_counts': {'alpaca_1day': 5},
            'skipped_demo_symbols': [],
        },
    )
    scanner_cls.return_value.quick_scan.return_value = result

    stats = get_stock_stats()
    assert stats['provenance']['kind'] == 'REAL'
    scanner_cls.assert_called_once_with(
        alpaca_api_key='key',
        alpaca_secret='secret',
        paper_trading=True,
        allow_demo_data=False,
    )


def test_production_stock_scanner_rejects_demo_rows():
    from scanners.stock_scanner import StockScanner

    scanner = StockScanner(allow_demo_data=False)
    frame = pd.DataFrame(
        {
            'open': [1.0] * 120,
            'high': [2.0] * 120,
            'low': [0.5] * 120,
            'close': [1.5] * 120,
            'volume': [1000] * 120,
        }
    )
    frame.attrs['data_source'] = 'demo_fallback'
    scanner.alpaca = MagicMock()
    scanner.alpaca.SP500_SYMBOLS = ['AAPL']
    scanner.alpaca.get_historical_data.return_value = frame

    result = scanner.quick_scan(limit=1)
    assert result.total_scanned == 0
    assert result.top_opportunities == []
    assert result.scan_parameters['skipped_demo_symbols'] == ['AAPL']
    assert result.scan_parameters['verified_real_data_only'] is False


def test_strategy_lab_connects_real_evidence_and_refuses_synthetic_run():
    from dashboard import DEMO_TOOLS_ENABLED, app, get_strategy_lab_stats

    assert DEMO_TOOLS_ENABLED is False
    stats = get_strategy_lab_stats()
    if stats['available']:
        assert stats['winner']
        assert stats['champion']['stage'] == 'CHAMPION'
        assert stats['provenance']['kind'] == 'REAL'
    else:
        # A clean checkout intentionally has no ignored runtime optimizer DB or
        # reports. A tracked frozen champion may still be visible, but the app
        # must not invent a current tournament winner or claim full availability.
        assert stats['winner'] is None
        assert stats['latest_tournament'] == {}
        assert stats['latest_optimizer']['report_path'] is None
        assert stats['provenance']['kind'] == 'UNAVAILABLE'
    assert stats['provenance']['message'] == 'Synthetic results are excluded'

    client = app.test_client()
    response = client.post('/api/strategy-lab/start-tournament', json={})
    assert response.status_code == 410
    assert response.get_json()['provenance']['kind'] == 'DEMO'


@patch('dashboard.get_accounting_reconciliation')
def test_paper_status_separates_trusted_and_legacy_records(reconciliation):
    from dashboard import app

    reconciliation.return_value = {
        'status': 'VERIFIED',
        'source': 'Alpaca paper Trading API + local positions.db evidence',
        'observed_at': '2026-07-14T12:00:00+00:00',
        'broker': {'equity': 500.5, 'equity_change_since_epoch': 0.5},
        'local': {
            'open_positions': {'SPY': {'quantity': 1}},
            'verification_summary': {
                'broker_verified': {'count': 2, 'realized_pnl': 3.0},
                'legacy_unverified': {'count': 197, 'realized_pnl': 671.89},
            },
            'trusted_realized_pnl': 3.0,
            'trusted_closed_trades': 2,
            'legacy_unverified_realized_pnl': 671.89,
            'legacy_unverified_closed_trades': 197,
        },
        'reconciliation': {'matched_symbols': ['SPY'], 'mismatches': [], 'blockers': []},
        'live_trading_allowed': False,
    }

    response = app.test_client().get('/api/paper-trading/status')
    assert response.status_code == 200
    data = response.get_json()
    assert data['mode'] == 'paper'
    assert data['provenance']['kind'] == 'VERIFIED'
    assert data['trusted_realized_pnl'] == 3.0
    assert data['legacy_unverified_realized_pnl'] == 671.89
    assert 'broker-verified' in data['pnl_label'].lower()


def test_flask_routes_return_typed_payloads():
    from dashboard import app

    client = app.test_client()
    assert client.get('/').status_code == 200

    poly = client.get('/api/polymarket/opportunities')
    assert poly.status_code == 200
    assert isinstance(poly.get_json()['items'], list)
    assert poly.get_json()['provenance']['kind'] in {'REAL', 'STALE', 'UNAVAILABLE'}

    with patch('dashboard.load_alpaca_credentials', return_value=('', '', 'missing')):
        stocks = client.get('/api/stocks/opportunities')
    assert stocks.status_code == 200
    assert stocks.get_json()['items'] == []
    assert stocks.get_json()['provenance']['kind'] == 'UNAVAILABLE'


def test_health_route_reports_real_service_uptime_and_test_receipt():
    from dashboard import app

    response = app.test_client().get('/health')
    assert response.status_code in (200, 500)
    data = response.get_json()
    assert data['service'] == 'tradesight-dashboard'
    assert data['trading_mode'] == 'PAPER_ONLY'
    assert data['uptime_seconds'] >= 0
    assert data['started_at']
    assert data['test_suite']['provenance']['kind'] in {'VERIFIED', 'STALE', 'UNKNOWN'}


def test_dashboard_template_has_no_fake_live_or_hardcoded_test_truth():
    from dashboard import app

    html = app.test_client().get('/').get_data(as_text=True)
    assert 'PAPER ONLY' in html
    assert '94/96' not in html
    assert 'const startTime' not in html
    assert 'event.target' not in html
    assert "switchTab('system', this)" in html
    assert 'Synthetic tournaments remain disabled' in html
    assert 'Archived Polymarket Data' in html
    assert 'Pre-epoch local rows remain preserved as <strong>LEGACY / UNVERIFIED</strong>' in html
    assert 'Accounting Reconciliation' in html
    assert 'Strategy Lifecycle' in html
