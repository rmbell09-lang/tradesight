import importlib.util
import os
from pathlib import Path

import pandas as pd

os.environ.setdefault('ALPACA_API_KEY', 'test-key')
os.environ.setdefault('ALPACA_SECRET_KEY', 'test-secret')

ROOT = Path('/Users/luckyai/Projects/TradeSight')
SPEC = importlib.util.spec_from_file_location(
    'overnight_strategy_evolution',
    ROOT / 'scripts' / 'overnight_strategy_evolution.py',
)
optimizer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(optimizer)


def _df(rows=250, source='alpaca_1h'):
    idx = pd.date_range('2024-01-01', periods=rows, freq='h')
    data = pd.DataFrame({
        'open': [100.0] * rows,
        'high': [101.0] * rows,
        'low': [99.0] * rows,
        'close': [100.0] * rows,
        'volume': [1_000_000] * rows,
    }, index=idx)
    data.attrs['data_source'] = source
    return data


def test_demo_or_synthetic_data_cannot_promote():
    quality = optimizer.summarize_data_quality({'SPY': _df(source='demo_fallback')}, 'demo_fallback')
    gate = optimizer.build_promotion_quality_gate({
        'data_quality': quality,
        'optimized': {'total_trades': 50, 'symbols_scored': ['SPY'], 'max_drawdown_pct': 1},
        'improvement': {'pnl_pct': 1, 'sharpe': 1},
        'cross_validation': {'SPY': {'pnl_pct': 1, 'sharpe': 1}},
        'walk_forward': {'stable': True},
    })
    assert gate['eligible'] is False
    assert 'market data quality gate failed' in gate['reasons']


def test_bad_oos_and_walk_forward_cannot_promote():
    quality = optimizer.summarize_data_quality({
        'SPY': _df(), 'QQQ': _df(), 'JPM': _df(), 'AAPL': _df(),
    }, 'alpaca_1h')
    gate = optimizer.build_promotion_quality_gate({
        'data_quality': quality,
        'optimized': {'total_trades': 50, 'symbols_scored': ['SPY', 'QQQ'], 'max_drawdown_pct': 1},
        'improvement': {'pnl_pct': 1, 'sharpe': 1},
        'cross_validation': {
            'SPY': {'pnl_pct': -1, 'sharpe': 0.1},
            'QQQ': {'pnl_pct': -0.5, 'sharpe': 0.1},
        },
        'walk_forward': {'stable': False},
    })
    assert gate['eligible'] is False
    assert 'walk-forward stability gate failed' in gate['reasons']
    assert 'OOS average PnL is not positive' in gate['reasons']
    assert 'OOS average Sharpe below 0.25' in gate['reasons']


class _FakeEngine:
    def run_backtest(self, *args, **kwargs):
        return {'metrics': {
            'total_pnl_pct': 1.0,
            'sharpe_ratio': 1.0,
            'win_rate': 60.0,
            'max_drawdown_pct': 1.0,
            'total_trades': 5,
        }}


def test_optimizer_search_space_stats_are_full_grid_not_survivor_count():
    tuner = optimizer.ParameterTuner(_df(), {
        'oversold': 30,
        'overbought': 65,
        'position_size': 0.15,
        'stop_loss_pct': 0.05,
        'take_profit_pct': 0.12,
        'max_holding_bars': 0,
    })
    tuner.backtest_engine = _FakeEngine()
    tuner.scoring_datasets = {'SPY': _df(), 'QQQ': _df()}
    results = tuner.test_parameter_grid('RSI Mean Reversion')
    assert tuner.grid_stats['total_grid_combinations'] == 144
    assert tuner.grid_stats['loop_iterations'] == 144
    assert tuner.grid_stats['evaluated_after_expectancy_filter'] == 144
    assert tuner.grid_stats['profitable_candidates'] == len(results)
    assert {'use_atr', 'trend_buffer', 'volume_min_ratio'} <= set(results[0])


def test_variant_builder_preserves_full_optimizer_params():
    tuner = optimizer.ParameterTuner(_df(), {})
    captured = {}

    def fake_create(*args, **kwargs):
        captured['args'] = args
        captured['kwargs'] = kwargs
        return 'strategy'

    tuner.create_rsi_variant = fake_create
    params = {
        'oversold': 25,
        'overbought': 75,
        'position_size': 0.22,
        'stop_loss_pct': 0.06,
        'take_profit_pct': 0.13,
        'max_holding_bars': 10,
        'use_atr': False,
        'trend_buffer': 0.95,
        'volume_min_ratio': 0.8,
    }

    assert tuner.create_rsi_variant_from_params(params) == 'strategy'
    assert captured['args'] == (25, 75, 0.22, 0.06, 0.13, 10)
    assert captured['kwargs'] == {
        'use_atr': False,
        'trend_buffer': 0.95,
        'volume_min_ratio': 0.8,
    }


def test_normalize_rsi_params_fills_full_optimizer_schema():
    params = optimizer.normalize_rsi_params({
        'oversold': 28,
        'overbought': 68,
        'position_size': 0.2,
        'stop_loss_pct': 0.06,
        'take_profit_pct': 0.13,
        'max_holding_bars': 10,
    })

    assert set(optimizer.RSI_PARAM_KEYS) <= set(params)
    assert params['use_atr'] is True
    assert params['trend_buffer'] == 0.97
    assert params['volume_min_ratio'] == 0.0


def test_validate_rsi_params_rejects_bad_optimizer_seed():
    params = optimizer.normalize_rsi_params({
        'oversold': 72,
        'overbought': 65,
        'position_size': 0.9,
        'stop_loss_pct': 0.10,
        'take_profit_pct': 0.12,
    })

    reasons = optimizer.validate_rsi_params(params)

    assert any('RSI thresholds' in reason for reason in reasons)
    assert any('position_size' in reason for reason in reasons)
    assert any('1.5x' in reason for reason in reasons)


def test_preflight_blocks_missing_real_data_env_and_warns_schema_gap():
    preflight = optimizer.build_optimizer_preflight(
        {
            'name': 'RSI Mean Reversion',
            'base_params': {
                'oversold': 30,
                'overbought': 65,
                'position_size': 0.15,
                'stop_loss_pct': 0.05,
                'take_profit_pct': 0.12,
                'max_holding_bars': 0,
            },
        },
        env={},
    )

    assert preflight['ok'] is False
    assert 'missing Alpaca credentials for real 1H market data' in preflight['blocking']
    assert any('use_atr' in warning for warning in preflight['warnings'])
    assert set(optimizer.RSI_PARAM_KEYS) <= set(preflight['base_params'])


def test_preflight_passes_with_complete_schema_and_env():
    complete = optimizer.normalize_rsi_params({
        'oversold': 30,
        'overbought': 65,
        'position_size': 0.15,
        'stop_loss_pct': 0.05,
        'take_profit_pct': 0.12,
        'max_holding_bars': 0,
        'use_atr': True,
        'trend_buffer': 0.97,
        'volume_min_ratio': 0.0,
    })
    preflight = optimizer.build_optimizer_preflight(
        {'name': 'RSI Mean Reversion', 'base_params': complete},
        env={'ALPACA_API_KEY': 'key', 'ALPACA_SECRET_KEY': 'secret'},
    )

    assert preflight['ok'] is True
    assert preflight['blocking'] == []


def test_oos_and_walk_forward_keep_full_selected_params():
    class CapturingTuner(optimizer.ParameterTuner):
        def __init__(self):
            super().__init__(_df(), {})
            self.seen = []

        def create_rsi_variant_from_params(self, params):
            self.seen.append(dict(params))

            def strategy(data, index, positions):
                return None

            return strategy

    selected = {
        'oversold': 25,
        'overbought': 75,
        'position_size': 0.22,
        'stop_loss_pct': 0.06,
        'take_profit_pct': 0.13,
        'max_holding_bars': 10,
        'use_atr': False,
        'trend_buffer': 0.95,
        'volume_min_ratio': 0.8,
    }

    tuner = CapturingTuner()
    tuner.cross_validate(selected, {'SPY': _df(300)})
    tuner.walk_forward_validate(_df(500), selected)

    assert tuner.seen
    assert all(seen['use_atr'] is False for seen in tuner.seen)
    assert all(seen['trend_buffer'] == 0.95 for seen in tuner.seen)
    assert all(seen['volume_min_ratio'] == 0.8 for seen in tuner.seen)


def test_scoring_dataset_selection_uses_broader_symbol_basket_by_default():
    datasets = {
        sym: _df(250)
        for sym in ['SPY', 'QQQ', 'JPM', 'AAPL', 'MSFT', 'XOM', 'JNJ', 'WMT', 'KO', 'DIS', 'PFE', 'CVX']
    }

    selected = optimizer.select_optimizer_scoring_datasets(datasets, as_of=optimizer.datetime(2026, 6, 23))

    assert len(selected) == 10
    assert list(selected)[:2] == ['SPY', 'QQQ']


def test_optimizer_targets_actual_supported_tournament_winner_family():
    assert optimizer.optimizer_target_for_winner('MACD Crossover') == 'MACD Crossover'
    assert optimizer.optimizer_target_for_winner('Momentum Breakout') == 'Momentum Breakout'
    assert optimizer.optimizer_target_for_winner('Bollinger Bounce') == 'Bollinger Bounce'
    assert optimizer.optimizer_target_for_winner('Dual MA + RSI') == 'RSI Mean Reversion'


def test_macd_family_grid_does_not_require_rsi_params():
    tuner = optimizer.ParameterTuner(_df(), {}, strategy_name='MACD Crossover')
    tuner.backtest_engine = _FakeEngine()
    tuner.scoring_datasets = {'SPY': _df(), 'QQQ': _df()}

    results = tuner.test_parameter_grid('MACD Crossover')

    assert results
    assert 'histogram_min' in results[0]
    assert 'oversold' not in results[0]
    assert tuner.grid_stats['total_grid_combinations'] > 0


def test_momentum_family_grid_does_not_require_rsi_params():
    tuner = optimizer.ParameterTuner(_df(), {}, strategy_name='Momentum Breakout')
    tuner.backtest_engine = _FakeEngine()
    tuner.scoring_datasets = {'SPY': _df(), 'QQQ': _df()}

    results = tuner.test_parameter_grid('Momentum Breakout')

    assert results
    assert 'lookback_bars' in results[0]
    assert 'entry_momentum' in results[0]
    assert 'oversold' not in results[0]


def test_bollinger_family_grid_tunes_actual_tournament_winner():
    tuner = optimizer.ParameterTuner(_df(), {}, strategy_name='Bollinger Bounce')
    tuner.backtest_engine = _FakeEngine()
    tuner.scoring_datasets = {'SPY': _df(), 'QQQ': _df()}

    results = tuner.test_parameter_grid('Bollinger Bounce')

    assert results
    assert 'exit_band' in results[0]
    assert 'oversold' not in results[0]
    assert tuner.grid_stats['total_grid_combinations'] > 0


def test_overfit_diagnostics_feed_promotion_gate():
    results = {
        'data_quality': {
            'promotion_data_ok': True,
        },
        'baseline': {'sharpe': 0.1},
        'optimized': {
            'pnl_pct': 1.0,
            'sharpe': 0.11,
            'total_trades': 20,
            'symbols_scored': ['SPY', 'QQQ', 'JPM', 'AAPL'],
            'max_drawdown_pct': 1,
        },
        'improvement': {'pnl_pct': 1.0, 'sharpe': 0.01},
        'cross_validation': {
            'SPY': {'pnl_pct': 0.1, 'sharpe': 0.3},
            'QQQ': {'pnl_pct': 0.1, 'sharpe': 0.3},
            'JPM': {'pnl_pct': 0.1, 'sharpe': 0.3},
            'AAPL': {'pnl_pct': 0.1, 'sharpe': 0.3},
        },
        'walk_forward': {'stable': True},
        'search_space': {'total_grid_combinations': 1000},
    }

    gate = optimizer.build_promotion_quality_gate(results)

    assert gate['eligible'] is False
    assert 'deflated Sharpe proxy did not clear multiple-testing penalty' in gate['reasons']


def _latest_report(regime='low_vol'):
    return {
        'data_quality': {
            'symbols': {
                'SPY': {'bars': 1000},
                'QQQ': {'bars': 1000},
                'AAPL': {'bars': 1000},
                'JPM': {'bars': 1000},
            },
        },
        'regime_report': {'regime': regime},
    }


def test_daily_evidence_does_not_open_full_search_on_nearly_identical_data():
    gate = optimizer.evaluate_optimizer_evidence(
        _latest_report(),
        optimizer.datetime(2026, 7, 13, 20, 0),
        {'SPY': 1006, 'QQQ': 1006, 'AAPL': 1006, 'JPM': 1006},
        credible_closed_trades=2,
        current_regime='low_vol',
        now=optimizer.datetime(2026, 7, 14, 20, 0),
    )
    assert gate['run'] is False
    assert gate['growth_symbols'] == 0


def test_real_bar_growth_opens_full_search_without_calendar_timer():
    gate = optimizer.evaluate_optimizer_evidence(
        _latest_report(),
        optimizer.datetime(2026, 7, 10, 20, 0),
        {'SPY': 1022, 'QQQ': 1021, 'AAPL': 1020, 'JPM': 1019},
        credible_closed_trades=3,
        current_regime='low_vol',
        now=optimizer.datetime(2026, 7, 14, 20, 0),
        fresh_symbol_bars={'SPY': 22, 'QQQ': 21, 'AAPL': 20, 'JPM': 19},
    )
    assert gate['run'] is True
    assert gate['growth_symbols'] == 3
    assert any('real 1H bars' in reason for reason in gate['reasons'])


def test_closed_trade_or_regime_evidence_can_open_full_search():
    closed = optimizer.evaluate_optimizer_evidence(
        _latest_report(),
        optimizer.datetime(2026, 7, 13, 20, 0),
        {'SPY': 1006, 'QQQ': 1006, 'AAPL': 1006, 'JPM': 1006},
        credible_closed_trades=20,
        current_regime='low_vol',
        now=optimizer.datetime(2026, 7, 14, 20, 0),
    )
    changed = optimizer.evaluate_optimizer_evidence(
        _latest_report(),
        optimizer.datetime(2026, 7, 13, 20, 0),
        {'SPY': 1006, 'QQQ': 1006, 'AAPL': 1006, 'JPM': 1006},
        credible_closed_trades=0,
        current_regime='high_vol',
        now=optimizer.datetime(2026, 7, 14, 20, 0),
    )
    assert closed['run'] is True
    assert changed['run'] is True
    assert changed['regime_changed'] is True


def test_no_prior_full_report_opens_search():
    gate = optimizer.evaluate_optimizer_evidence(
        {}, None, {}, 0, None, now=optimizer.datetime(2026, 7, 14, 20, 0)
    )
    assert gate['run'] is True


def test_console_summary_cannot_claim_success_when_promotion_gate_rejects(capsys):
    optimizer.print_summary({
        'winner': 'RSI Mean Reversion',
        'version': 'v5-test',
        'timestamp': '2026-07-14T04:00:00',
        'baseline': {'parameters': {}, 'pnl_pct': -1.0, 'sharpe': 0.5, 'win_rate': 50.0},
        'optimized': {'parameters': {}, 'pnl_pct': 1.0, 'sharpe': 0.4, 'win_rate': 60.0, 'composite_score': 0.1},
        'improvement': {'pnl_pct': 2.0, 'sharpe': -0.1},
        'search_space': {'total_grid_combinations': 10, 'parameters_varied': []},
        'promotion_quality_gate': {'eligible': False, 'reasons': ['challenger did not beat baseline Sharpe']},
        'champion_decision': 'NO PROMOTION: challenger did not beat baseline Sharpe',
    })
    output = capsys.readouterr().out
    assert 'NO PROMOTION' in output
    assert 'SUCCESS: Optimization improved' not in output


def test_metric_summary_supports_fair_baseline_challenger_comparison():
    metrics = {
        'SPY': {
            'total_pnl_pct': 2.0,
            'sharpe_ratio': 0.8,
            'win_rate': 60.0,
            'max_drawdown_pct': 5.0,
            'total_trades': 10,
        },
        'QQQ': {
            'total_pnl_pct': -1.0,
            'sharpe_ratio': 0.2,
            'win_rate': 40.0,
            'max_drawdown_pct': 7.0,
            'total_trades': 8,
        },
    }

    summary = optimizer.summarize_backtest_metrics(metrics)
    compact = optimizer.compact_per_symbol_metrics(metrics)

    assert summary['pnl_pct'] == 0.5
    assert summary['sharpe'] == 0.5
    assert summary['win_rate'] == 50.0
    assert summary['max_drawdown_pct'] == 6.0
    assert summary['total_trades'] == 18
    assert summary['profitable_symbols'] == 1
    assert summary['negative_symbols'] == 1
    assert summary['worst_symbol_pnl'] == -1.0
    assert compact['SPY']['trades'] == 10
