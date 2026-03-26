#!/usr/bin/env python3
"""
TradeSight Automated Strategy Development

Runs overnight strategy tournaments, evolves winners, logs performance.
Designed to run via cron for continuous strategy improvement.
"""

import os
import sys
import json
import sqlite3
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import pandas as pd

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from strategy_lab.tournament import StrategyTournament, get_builtin_strategies
from strategy_lab.backtest import make_rsi_strategy
from strategy_lab.ai_engine import AIStrategyEngine, create_test_data
from data.alpaca_client import AlpacaClient



# AlertManager — optional push notifications for tournament results
try:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..'))
    from alerts.alert_manager import AlertManager as _AlertManager
    from alerts.alert_types import AlertType as _AlertType
    from config import ALERTS_CONFIG as _ALERTS_CONFIG
    _AUTOMATION_ALERTS_AVAILABLE = True
except Exception:
    _AUTOMATION_ALERTS_AVAILABLE = False

# Symbols used for tournament data — liquid, well-behaved for mean reversion
TOURNAMENT_SYMBOLS = [
    'SPY', 'QQQ', 'AAPL', 'MSFT', 'GOOGL',
    'JPM', 'BAC', 'JNJ', 'XOM', 'WMT',
]

class StrategyAutomation:
    """Automated strategy development and tournament runner"""
    
    def __init__(self, base_dir: str = None):
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent.parent
        self.data_dir = self.base_dir / 'data'
        self.logs_dir = self.base_dir / 'logs'
        self.reports_dir = self.base_dir / 'reports'
        
        # Ensure directories exist
        for dir_path in [self.data_dir, self.logs_dir, self.reports_dir]:
            dir_path.mkdir(exist_ok=True)
        
        # Setup logging
        self._setup_logging()
        
        # Tournament parameters
        self.tournament_config = {
            'initial_balance': 500.0,
            'elimination_rate': 0.3,
            'min_survivors': 2,
            'rounds': 3,
            'data_days_per_round': [100, 150, 200],  # Different data sizes per round
            # Walk-forward split: train on first 70%, validate on next 15%, test on last 15%
            'train_ratio': 0.70,
            'val_ratio': 0.15,
            # test_ratio = 1.0 - train_ratio - val_ratio = 0.15 (implicit)
        }
        
        self.logger.info(f"StrategyAutomation initialized at {self.base_dir}")
        # Alert manager (optional)
        self._alert_manager = None
        if _AUTOMATION_ALERTS_AVAILABLE:
            try:
                self._alert_manager = _AlertManager(
                    config=_ALERTS_CONFIG,
                    data_dir=str(self.data_dir)
                )
            except Exception:
                pass
    
    def _setup_logging(self):
        """Setup logging for automation runs"""
        log_file = self.logs_dir / f"strategy_automation_{datetime.now().strftime('%Y%m%d')}.log"
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler(sys.stdout)
            ]
        )
        
        self.logger = logging.getLogger('StrategyAutomation')

    # ------------------------------------------------------------------
    # Real historical data fetch with walk-forward splits
    # ------------------------------------------------------------------

    def _fetch_real_data(self, symbol: str, days: int) -> Optional[pd.DataFrame]:
        """
        Fetch real historical daily bars from Alpaca for a given symbol.
        Returns None if fetch fails (caller falls back to synthetic data).
        """
        try:
            client = AlpacaClient()
            data = client.get_historical_data(symbol, days=days, timeframe='1Day')
            if data is not None and len(data) >= 50:
                self.logger.info(f"Fetched {len(data)} bars for {symbol}")
                return data
            else:
                self.logger.warning(f"Insufficient data for {symbol}: {len(data) if data is not None else 0} bars")
                return None
        except Exception as e:
            self.logger.warning(f"Failed to fetch real data for {symbol}: {e}")
            return None

    def _walk_forward_split(self, data: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """
        Split data into train / validation / test slices using strict chronological order.
        No data from the future leaks into earlier slices.

        Returns: (train_df, val_df, test_df)
        """
        n = len(data)
        train_end = int(n * self.tournament_config['train_ratio'])
        val_end = int(n * (self.tournament_config['train_ratio'] + self.tournament_config['val_ratio']))

        train = data.iloc[:train_end].copy()
        val   = data.iloc[train_end:val_end].copy()
        test  = data.iloc[val_end:].copy()

        self.logger.info(
            f"Walk-forward split: train={len(train)} bars, "
            f"val={len(val)} bars, test={len(test)} bars"
        )
        return train, val, test

    def create_tournament_datasets(self) -> List[Tuple[str, pd.DataFrame]]:
        """
        Create three tournament rounds using REAL historical data with walk-forward splits.

        Round structure (no lookahead bias):
          Round 1 — training slice of symbol A  (strategy selection)
          Round 2 — validation slice of symbol B  (out-of-sample check)
          Round 3 — test slice of symbol C  (final holdout, never seen before)

        Falls back to synthetic data only when Alpaca is unreachable.
        """
        datasets: List[Tuple[str, pd.DataFrame]] = []

        # Pick 3 different symbols so each round sees a different market regime
        # Shuffle so we don't always use the same trio
        pool = TOURNAMENT_SYMBOLS.copy()
        random.shuffle(pool)
        round_symbols = pool[:3]

        # Total days to fetch: enough for a full walk-forward split
        # We need at least `data_days_per_round[-1]` bars in the TEST slice alone,
        # so fetch 2× the largest round size to be safe.
        max_days = self.tournament_config['data_days_per_round'][-1]
        fetch_days = int(max_days / (1.0 - self.tournament_config['train_ratio'] - self.tournament_config['val_ratio']) * 1.1)

        split_labels = ['train', 'val', 'test']

        for i, (symbol, days, split_label) in enumerate(
            zip(round_symbols,
                self.tournament_config['data_days_per_round'],
                split_labels),
            start=1
        ):
            real_data = self._fetch_real_data(symbol, days=fetch_days)

            if real_data is not None and len(real_data) >= 150:
                train_df, val_df, test_df = self._walk_forward_split(real_data)
                slices = {'train': train_df, 'val': val_df, 'test': test_df}
                data_slice = slices[split_label]

                # Make sure the slice has at least 50 bars for backtesting
                if len(data_slice) < 50:
                    self.logger.warning(
                        f"Round {i} {split_label} slice for {symbol} too small "
                        f"({len(data_slice)} bars) — using full real data instead"
                    )
                    data_slice = real_data

                label = f'Round_{i}_{symbol}_{split_label}_{len(data_slice)}bars'
                self.logger.info(
                    f"Round {i}: {symbol} [{split_label}] — {len(data_slice)} bars "
                    f"(real historical data, NO synthetic, NO lookahead)"
                )
            else:
                # Fallback: synthetic data (logs a clear warning)
                self.logger.warning(
                    f"Round {i}: Alpaca unavailable for {symbol}. "
                    f"Falling back to synthetic data — tournament results will be unreliable."
                )
                data_slice = create_test_data(days=days)
                label = f'Round_{i}_SYNTHETIC_{days}d_UNRELIABLE'

            datasets.append((label, data_slice))

        return datasets
    
    def run_tournament_session(self, session_id: str) -> Dict:
        """Run a complete tournament session"""
        self.logger.info(f"Starting tournament session: {session_id}")
        
        try:
            # Create tournament
            tournament = StrategyTournament(
                initial_balance=self.tournament_config['initial_balance'],
                elimination_rate=self.tournament_config['elimination_rate'],
                min_survivors=self.tournament_config['min_survivors']
            )
            
            # Register built-in strategies
            builtin_strategies = get_builtin_strategies()
            for name, strategy_func in builtin_strategies.items():
                tournament.register_strategy(name, strategy_func)
                self.logger.info(f"Registered strategy: {name}")
            
            # Also register a champion-params RSI variant so tournament evaluates
            # the actual thresholds the paper trader is using (not just 30/70 defaults)
            try:
                import json as _json
                from pathlib import Path as _Path
                _champ_path = _Path(__file__).resolve().parent.parent.parent / 'data' / 'champion.json'
                if _champ_path.exists():
                    with open(_champ_path) as _cf:
                        _champ = _json.load(_cf)
                    _p = _champ.get('params', {})
                    if _p.get('oversold') and _p.get('overbought'):
                        _champ_rsi = make_rsi_strategy(
                            oversold=_p['oversold'],
                            overbought=_p['overbought'],
                            position_size=_p.get('position_size', 0.6),
                            stop_loss_pct=_p.get('stop_loss_pct', 0.07),
                            take_profit_pct=_p.get('take_profit_pct', 0.08),
                        )
                        _variant_name = f"RSI_Champion_os{_p['oversold']}_ob{_p['overbought']}"
                        tournament.register_strategy(_variant_name, _champ_rsi)
                        self.logger.info(f"Registered champion RSI variant: {_variant_name}")
            except Exception as _ce:
                self.logger.warning(f"Could not register champion RSI variant: {_ce}")
            
            # Create datasets for multi-round tournament
            datasets = self.create_tournament_datasets()

            # Flag whether any round fell back to synthetic data
            used_synthetic = any('SYNTHETIC' in name for name, _ in datasets)
            
            # Run tournament
            start_time = datetime.now()
            results = tournament.run_tournament(datasets)
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            
            # Package results
            session_results = {
                'session_id': session_id,
                'start_time': start_time.isoformat(),
                'end_time': end_time.isoformat(),
                'duration_seconds': duration,
                'winner': results.winner,
                'winner_avg_score': results.winner_avg_score,
                'total_rounds': results.total_rounds,
                'total_strategies': results.total_strategies_entered,
                'final_survivors': results.final_survivors,
                'top_3': results.top_3,
                'elimination_log': results.elimination_log,
                'data_source': 'SYNTHETIC_FALLBACK' if used_synthetic else 'real_historical_walkforward',
                'participants': []
            }
            
            # Add participant details
            for entry in tournament.entries:
                session_results['participants'].append({
                    'name': entry.name,
                    'avg_score': entry.avg_score,
                    'total_score': entry.total_score,
                    'wins': entry.wins,
                    'losses': entry.losses,
                    'rounds_survived': entry.rounds_survived,
                    'eliminated': entry.eliminated
                })
            
            if used_synthetic:
                self.logger.warning(
                    "Tournament used synthetic data for one or more rounds — "
                    "champion params may not reflect real market behaviour."
                )
            else:
                self.logger.info(
                    "Tournament used real historical data with walk-forward splits — "
                    "champion params are valid."
                )

            self.logger.info(f"Tournament completed in {duration:.1f}s - Winner: {results.winner}")
            # Fire strategy evolved alert
            if self._alert_manager:
                try:
                    self._alert_manager.fire(
                        _AlertType.STRATEGY_EVOLVED,
                        winner=results.winner,
                        score=results.winner_avg_score,
                        rounds=results.total_rounds,
                        session_id=session_id,
                    )
                except Exception:
                    pass
            return session_results
            
        except Exception as e:
            self.logger.error(f"Tournament session failed: {e}")
            return {
                'session_id': session_id,
                'error': str(e),
                'start_time': datetime.now().isoformat(),
                'status': 'failed'
            }
    
    def store_session_results(self, results: Dict):
        """Store tournament session results in SQLite database"""
        db_path = self.data_dir / 'tournament_history.db'
        
        try:
            with sqlite3.connect(db_path) as conn:
                # Create tables if they don't exist
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS tournament_sessions (
                        session_id TEXT PRIMARY KEY,
                        start_time TEXT,
                        end_time TEXT,
                        duration_seconds REAL,
                        winner TEXT,
                        winner_avg_score REAL,
                        total_rounds INTEGER,
                        total_strategies INTEGER,
                        final_survivors INTEGER,
                        status TEXT,
                        results_json TEXT
                    )
                ''')
                
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS strategy_performance (
                        session_id TEXT,
                        strategy_name TEXT,
                        avg_score REAL,
                        total_score REAL,
                        wins INTEGER,
                        losses INTEGER,
                        rounds_survived INTEGER,
                        eliminated BOOLEAN,
                        FOREIGN KEY (session_id) REFERENCES tournament_sessions (session_id)
                    )
                ''')
                
                # Insert session data
                if results.get('error'):
                    conn.execute(
                        'INSERT OR REPLACE INTO tournament_sessions (session_id, start_time, status, results_json) VALUES (?, ?, ?, ?)',
                        (results['session_id'], results['start_time'], 'failed', json.dumps(results))
                    )
                else:
                    conn.execute(
                        '''INSERT OR REPLACE INTO tournament_sessions 
                           (session_id, start_time, end_time, duration_seconds, winner, winner_avg_score, 
                            total_rounds, total_strategies, final_survivors, status, results_json) 
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (results['session_id'], results['start_time'], results['end_time'], 
                         results['duration_seconds'], results['winner'], results['winner_avg_score'],
                         results['total_rounds'], results['total_strategies'], results['final_survivors'],
                         'completed', json.dumps(results))
                    )
                    
                    # Insert strategy performance data
                    for participant in results['participants']:
                        conn.execute(
                            '''INSERT INTO strategy_performance 
                               (session_id, strategy_name, avg_score, total_score, wins, losses, 
                                rounds_survived, eliminated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                            (results['session_id'], participant['name'], participant['avg_score'],
                             participant['total_score'], participant['wins'], participant['losses'],
                             participant['rounds_survived'], participant['eliminated'])
                        )
                
                conn.commit()
                self.logger.info(f"Stored session results: {results['session_id']}")
                
        except Exception as e:
            self.logger.error(f"Failed to store results: {e}")
    
    def generate_daily_report(self) -> str:
        """Generate daily performance report"""
        db_path = self.data_dir / 'tournament_history.db'
        report_lines = []
        
        # If database does not exist, no sessions have been run yet
        if not db_path.exists():
            return "No tournament sessions completed today."

        
        try:
            with sqlite3.connect(db_path) as conn:
                # Get today's sessions
                today = datetime.now().strftime('%Y-%m-%d')
                sessions = conn.execute(
                    'SELECT * FROM tournament_sessions WHERE DATE(start_time) = ? ORDER BY start_time DESC',
                    (today,)
                ).fetchall()
                
                if not sessions:
                    return "No tournament sessions completed today."
                
                report_lines.append(f"🏆 TradeSight Daily Strategy Report - {today}")
                report_lines.append("=" * 60)
                report_lines.append(f"Sessions completed: {len(sessions)}")
                
                # Session summaries
                for session in sessions:
                    session_id, start_time, end_time, duration, winner, winner_score, rounds, strategies, survivors, status, results_json = session
                    
                    if status == 'completed':
                        report_lines.append(f"\n📊 Session: {session_id}")
                        report_lines.append(f"   Winner: {winner} (Score: {winner_score:.3f})")
                        report_lines.append(f"   Duration: {duration:.1f}s | Rounds: {rounds} | Strategies: {strategies}")
                        # Show data source if available
                        try:
                            session_data = json.loads(results_json or '{}')
                            data_source = session_data.get('data_source', 'unknown')
                            if 'SYNTHETIC' in data_source:
                                report_lines.append(f"   ⚠️  Data: SYNTHETIC FALLBACK — results may not reflect real markets")
                            else:
                                report_lines.append(f"   ✅ Data: real historical walk-forward (no lookahead bias)")
                        except Exception:
                            pass
                    else:
                        report_lines.append(f"\n❌ Session: {session_id} - FAILED")
                
                # Top performers over last 7 days
                week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
                top_performers = conn.execute('''
                    SELECT strategy_name, AVG(avg_score) as mean_score, COUNT(*) as appearances, 
                           SUM(CASE WHEN eliminated = 0 THEN 1 ELSE 0 END) as survivals
                    FROM strategy_performance sp 
                    JOIN tournament_sessions ts ON sp.session_id = ts.session_id 
                    WHERE DATE(ts.start_time) >= ? AND ts.status = 'completed'
                    GROUP BY strategy_name 
                    ORDER BY mean_score DESC 
                    LIMIT 5
                ''', (week_ago,)).fetchall()
                
                if top_performers:
                    report_lines.append(f"\n🎯 Top Performers (Last 7 Days)")
                    report_lines.append("-" * 40)
                    for i, (name, mean_score, appearances, survivals) in enumerate(top_performers, 1):
                        survival_rate = (survivals / appearances * 100) if appearances > 0 else 0
                        report_lines.append(f"{i}. {name}: {mean_score:.3f} avg score, {survival_rate:.0f}% survival rate")
                
        except Exception as e:
            self.logger.error(f"Failed to generate report: {e}")
            return f"Report generation failed: {e}"
        
        return "\n".join(report_lines)
    
    def save_daily_report(self, report: str):
        """Save daily report to file"""
        report_file = self.reports_dir / f"daily_report_{datetime.now().strftime('%Y%m%d')}.txt"
        
        try:
            with open(report_file, 'w') as f:
                f.write(report)
            self.logger.info(f"Daily report saved: {report_file}")
        except Exception as e:
            self.logger.error(f"Failed to save report: {e}")
    
    def run_overnight_session(self):
        """Main entry point for overnight automation"""
        session_id = f"overnight_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        self.logger.info("=== Starting Overnight Strategy Development Session ===")
        
        # Run tournament
        results = self.run_tournament_session(session_id)
        
        # Store results
        self.store_session_results(results)
        
        # Generate and save daily report
        report = self.generate_daily_report()
        self.save_daily_report(report)
        
        # Print summary for cron logs
        if results.get('error'):
            print(f"❌ Overnight session FAILED: {results['error']}")
        else:
            data_src = results.get('data_source', 'unknown')
            src_label = "✅ real historical walk-forward" if 'real' in data_src else "⚠️  SYNTHETIC FALLBACK"
            print(f"✅ Overnight session completed - Winner: {results['winner']} (Score: {results['winner_avg_score']:.3f})")
            print(f"   Data source: {src_label}")
        
        print("\n" + report)
        
        self.logger.info("=== Overnight Strategy Development Session Complete ===")
        return results


def main():
    """CLI entry point"""
    if len(sys.argv) > 1 and sys.argv[1] == 'report':
        # Generate report only
        automation = StrategyAutomation()
        report = automation.generate_daily_report()
        print(report)
    else:
        # Run full overnight session
        automation = StrategyAutomation()
        automation.run_overnight_session()


if __name__ == '__main__':
    main()
