#!/usr/bin/env python3
"""
Tests for TradeSight Strategy Automation System
"""

import pytest
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch
import sys
import os

# Add src to path for testing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from automation.strategy_automation import StrategyAutomation


class TestStrategyAutomation:
    """Test suite for StrategyAutomation class"""
    
    def setup_method(self):
        """Setup test environment"""
        self.temp_dir = tempfile.mkdtemp()
        self.automation = StrategyAutomation(base_dir=self.temp_dir)
    
    def teardown_method(self):
        """Cleanup test environment"""
        import shutil
        shutil.rmtree(self.temp_dir)
    
    def test_initialization(self):
        """Test automation system initialization"""
        assert self.automation.base_dir == Path(self.temp_dir)
        assert self.automation.data_dir.exists()
        assert self.automation.logs_dir.exists()
        assert self.automation.reports_dir.exists()
        assert self.automation.logger is not None
    
    def test_tournament_config(self):
        """Test tournament configuration"""
        config = self.automation.tournament_config
        assert config['initial_balance'] > 0
        assert 0 < config['elimination_rate'] < 1
        assert config['min_survivors'] >= 2
        assert len(config['data_days_per_round']) > 0
    
    def test_create_tournament_datasets(self):
        """Test dataset creation for tournaments"""
        datasets = self.automation.create_tournament_datasets()
        
        assert len(datasets) == len(self.automation.tournament_config['data_days_per_round'])
        
        for name, data in datasets:
            assert isinstance(name, str)
            assert 'Round_' in name
            assert hasattr(data, 'iloc')  # DataFrame-like
            assert len(data) > 0
    
    def test_store_session_results_success(self):
        """Test storing successful session results"""
        results = {
            'session_id': 'test_session_001',
            'start_time': '2026-02-28T10:00:00',
            'end_time': '2026-02-28T10:30:00',
            'duration_seconds': 1800.0,
            'winner': 'MACD Crossover',
            'winner_avg_score': 0.424,
            'total_rounds': 3,
            'total_strategies': 7,
            'final_survivors': 3,
            'participants': [
                {
                    'name': 'MACD Crossover',
                    'avg_score': 0.424,
                    'total_score': 1.272,
                    'wins': 3,
                    'losses': 0,
                    'rounds_survived': 3,
                    'eliminated': False
                },
                {
                    'name': 'RSI Mean Reversion',
                    'avg_score': 0.301,
                    'total_score': 0.903,
                    'wins': 2,
                    'losses': 1,
                    'rounds_survived': 2,
                    'eliminated': True
                }
            ]
        }
        
        self.automation.store_session_results(results)
        
        # Verify data was stored
        db_path = self.automation.data_dir / 'tournament_history.db'
        assert db_path.exists()
        
        with sqlite3.connect(db_path) as conn:
            # Check session was stored
            session = conn.execute(
                'SELECT * FROM tournament_sessions WHERE session_id = ?',
                (results['session_id'],)
            ).fetchone()
            assert session is not None
            assert session[4] == 'MACD Crossover'  # winner
            assert session[5] == 0.424  # winner_avg_score
            
            # Check participants were stored
            participants = conn.execute(
                'SELECT * FROM strategy_performance WHERE session_id = ?',
                (results['session_id'],)
            ).fetchall()
            assert len(participants) == 2
    
    def test_store_session_results_failure(self):
        """Test storing failed session results"""
        failed_results = {
            'session_id': 'test_session_failed',
            'start_time': '2026-02-28T10:00:00',
            'error': 'Test error message',
            'status': 'failed'
        }
        
        self.automation.store_session_results(failed_results)
        
        # Verify failure was stored
        db_path = self.automation.data_dir / 'tournament_history.db'
        with sqlite3.connect(db_path) as conn:
            session = conn.execute(
                'SELECT * FROM tournament_sessions WHERE session_id = ?',
                (failed_results['session_id'],)
            ).fetchone()
            assert session is not None
            assert session[9] == 'failed'  # status column
    
    def test_generate_daily_report_no_data(self):
        """Test report generation with no data"""
        report = self.automation.generate_daily_report()
        assert "No tournament sessions completed today" in report
    
    def test_generate_daily_report_with_data(self):
        """Test report generation with session data"""
        # Store some test session data first
        test_results = {
            'session_id': 'test_daily_report',
            'start_time': '2026-02-28T10:00:00',
            'end_time': '2026-02-28T10:15:00',
            'duration_seconds': 900.0,
            'winner': 'Test Strategy',
            'winner_avg_score': 0.500,
            'total_rounds': 2,
            'total_strategies': 5,
            'final_survivors': 2,
            'participants': [
                {
                    'name': 'Test Strategy',
                    'avg_score': 0.500,
                    'total_score': 1.000,
                    'wins': 2,
                    'losses': 0,
                    'rounds_survived': 2,
                    'eliminated': False
                }
            ]
        }
        
        self.automation.store_session_results(test_results)
        
        # Generate report
        report = self.automation.generate_daily_report()
        
        assert "TradeSight Daily Strategy Report" in report
        assert "Sessions completed: 1" in report
        assert "Test Strategy" in report
        assert "Score: 0.500" in report
    
    def test_save_daily_report(self):
        """Test saving daily report to file"""
        test_report = "Test daily report content"
        self.automation.save_daily_report(test_report)
        
        # Check report file was created
        report_files = list(self.automation.reports_dir.glob("daily_report_*.txt"))
        assert len(report_files) == 1
        
        with open(report_files[0], 'r') as f:
            content = f.read()
        assert content == test_report
    
    @patch('automation.strategy_automation.StrategyTournament')
    @patch('automation.strategy_automation.get_builtin_strategies')
    def test_run_tournament_session_success(self, mock_strategies, mock_tournament):
        """Test successful tournament session run"""
        # Setup mocks
        mock_strategies.return_value = {'Test Strategy': Mock()}
        mock_tournament_instance = Mock()
        mock_tournament.return_value = mock_tournament_instance
        mock_tournament_instance.entries = [Mock(
            name='Test Strategy',
            avg_score=0.400,
            total_score=0.800,
            wins=1,
            losses=0,
            rounds_survived=1,
            eliminated=False
        )]
        
        mock_results = Mock(
            winner='Test Strategy',
            winner_avg_score=0.400,
            total_rounds=1,
            total_strategies_entered=1,
            final_survivors=1,
            top_3=[],
            elimination_log=[]
        )
        mock_tournament_instance.run_tournament.return_value = mock_results
        
        # Run tournament session
        results = self.automation.run_tournament_session('test_session')
        
        # Verify results
        assert results['session_id'] == 'test_session'
        assert results['winner'] == 'Test Strategy'
        assert results['winner_avg_score'] == 0.400
        assert 'error' not in results
        assert len(results['participants']) == 1
    
    @patch('automation.strategy_automation.StrategyTournament')
    def test_run_tournament_session_failure(self, mock_tournament):
        """Test failed tournament session run"""
        # Setup mock to raise exception
        mock_tournament.side_effect = Exception("Test tournament failure")
        
        # Run tournament session
        results = self.automation.run_tournament_session('test_session_fail')
        
        # Verify failure handling
        assert results['session_id'] == 'test_session_fail'
        assert results['status'] == 'failed'
        assert 'Test tournament failure' in results['error']


def run_automation_integration_test():
    """Integration test for full automation workflow"""
    print("Running integration test for Strategy Automation...")
    
    try:
        # Create automation instance with temp directory
        temp_dir = tempfile.mkdtemp()
        automation = StrategyAutomation(base_dir=temp_dir)
        
        print(f"✓ Automation initialized in {temp_dir}")
        
        # Test dataset creation
        datasets = automation.create_tournament_datasets()
        print(f"✓ Created {len(datasets)} datasets")
        
        # Test tournament session (this will actually run strategies)
        print("Running actual tournament session...")
        session_id = "integration_test_session"
        results = automation.run_tournament_session(session_id)
        
        if 'error' in results:
            print(f"✗ Tournament failed: {results['error']}")
            return False
        
        print(f"✓ Tournament completed - Winner: {results['winner']}")
        
        # Test result storage
        automation.store_session_results(results)
        print("✓ Results stored to database")
        
        # Test report generation
        report = automation.generate_daily_report()
        automation.save_daily_report(report)
        print("✓ Daily report generated and saved")
        
        # Verify files created
        db_path = automation.data_dir / 'tournament_history.db'
        report_files = list(automation.reports_dir.glob("*.txt"))
        
        print(f"✓ Database created: {db_path.exists()}")
        print(f"✓ Report files: {len(report_files)} files")
        
        # Cleanup
        import shutil
        shutil.rmtree(temp_dir)
        print("✓ Cleanup completed")
        
        print("🎉 Integration test PASSED")
        return True
        
    except Exception as e:
        print(f"✗ Integration test FAILED: {e}")
        return False


if __name__ == '__main__':
    # Run integration test when called directly
    success = run_automation_integration_test()
    sys.exit(0 if success else 1)