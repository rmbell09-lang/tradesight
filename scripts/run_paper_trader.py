#!/usr/bin/env python3
"""TradeSight Paper Trader Runner — with feedback loop"""

import os
import sys
import json
import glob
import logging
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR / 'src'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(BASE_DIR / 'logs' / 'paper_trader.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def get_latest_optimization_params():
    """Load best parameters from latest overnight optimization report."""
    reports = sorted(glob.glob(str(BASE_DIR / 'reports' / 'optimization_*.json')), reverse=True)
    if not reports:
        logger.warning('No optimization reports found — using default params')
        return {
            'oversold': 30, 'overbought': 70, 'position_size': 0.65,
            'stop_loss_pct': 0.08, 'take_profit_pct': 0.09, 'max_holding_bars': 0
        }
    with open(reports[0]) as f:
        report = json.load(f)
    optimized = report.get('optimized', {})
    params = optimized.get('params', {})
    pnl = optimized.get('pnl_pct', 0)
    boost = optimized.get('feedback_boost', 0)
    live_pnl = optimized.get('live_avg_pnl', None)
    logger.info(f'Loaded params from {Path(reports[0]).name}')
    logger.info(f'  Backtest PnL: {pnl:.2f}%')
    if boost:
        logger.info(f'  Feedback boost: +{boost:.4f} (live avg PnL: {live_pnl}%)')
    return params


def main():
    api_key = os.environ.get('ALPACA_API_KEY', '')
    secret_key = os.environ.get('ALPACA_SECRET_KEY', '') or os.environ.get('ALPACA_SECRET', '')

    if not api_key or not secret_key:
        logger.error('Alpaca API keys not set — cannot run paper trader')
        sys.exit(1)

    logger.info('=== TradeSight Paper Trader Starting ===')
    logger.info(f'API key: {api_key[:8]}...')

    from trading.paper_trader import PaperTrader

    trader = PaperTrader(
        base_dir=str(BASE_DIR),
        alpaca_api_key=api_key,
        alpaca_secret=secret_key
    )

    # Prefer champion params (stable, proven) over raw optimizer output
    try:
        from trading.champion_tracker import ChampionTracker
        champion = ChampionTracker(base_dir=str(BASE_DIR))
        champ = champion.get_champion()
        if champ and champ.get('params'):
            params = champ['params']
            logger.info(f"Using CHAMPION params: {params}")
            logger.info(f"Champion status: {champion.status()}")
        else:
            params = get_latest_optimization_params()
            logger.info(f"No champion yet — using latest optimizer params: {params}")
    except Exception as ce:
        logger.warning(f"Champion tracker failed ({ce}), falling back to optimizer params")
        params = get_latest_optimization_params()

    trader.active_params = params
    logger.info(f'Active params: {params}')

    # Run session (will auto-log to feedback DB at end)
    report = trader.run_trading_session()

    print('\n' + '='*60)
    print(report)
    print('='*60)

    # Print feedback summary
    if trader.feedback:
        print('\n' + trader.feedback.summary())

    logger.info('=== Paper Trader Session Complete ===')


if __name__ == '__main__':
    main()
