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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import pandas as pd

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from strategy_lab.tournament import StrategyTournament, get_builtin_strategies
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

class StrategyAutomation:
    """Automated strategy development and tournament runner"""
    
    def __init__(self, base_dir: str = None):
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).parent.parent
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
            'data_days_per_round': [100, 150, 200]  # Different data sizes per round
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
    
    def create_tournament_datasets(self) -> List[Tuple[str, pd.DataFrame]]:
        """Create multiple datasets for multi-round tournaments"""
        datasets = []
        
        for i, days in enumerate(self.tournament_config['data_days_per_round'], 1):
            data = create_test_data(days=days)  # Different seed per round
            datasets.append((f'Round_{i}_Data_{days}d', data))
            self.logger.info(f"Created dataset for round {i}: {days} days")
        
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
            
            # Create datasets for multi-round tournament
            datasets = self.create_tournament_datasets()
            
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
                    session_id, start_time, end_time, duration, winner, winner_score, rounds, strategies, survivors, status, _ = session
                    
                    if status == 'completed':
                        report_lines.append(f"\n📊 Session: {session_id}")
                        report_lines.append(f"   Winner: {winner} (Score: {winner_score:.3f})")
                        report_lines.append(f"   Duration: {duration:.1f}s | Rounds: {rounds} | Strategies: {strategies}")
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
            print(f"✅ Overnight session completed - Winner: {results['winner']} (Score: {results['winner_avg_score']:.3f})")
        
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