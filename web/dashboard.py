#!/usr/bin/env python3
"""
TradeSight Unified Dashboard
Multi-market intelligence platform showing Polymarket, Stocks, and Strategy Lab
"""

from flask import Flask, render_template, jsonify, request
import sqlite3
import json
from datetime import datetime, timedelta
import os
import sys
import pandas as pd

# Add project root to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from scanners.stock_scanner import StockScanner
from strategy_lab.tournament import StrategyTournament
from strategy_lab.ai_engine import create_test_data

app = Flask(__name__)

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
        
        return {
            'total_markets': total_markets,
            'last_scan': last_scan,
            'active_markets': active_markets,
            'high_volume_markets': high_volume_markets
        }
    except Exception as e:
        return {
            'total_markets': 0,
            'last_scan': None,
            'active_markets': 0,
            'high_volume_markets': 0,
            'error': str(e)
        }

def get_stock_stats():
    """Get stock market statistics"""
    try:
        # Create scanner
        scanner = StockScanner()
        
        # Run a quick scan (using the correct method name)
        scan_result = scanner.quick_scan(limit=5)
        
        return {
            'total_scanned': scan_result.total_scanned,
            'opportunities_found': scan_result.opportunities_found,
            'scan_duration': scan_result.scan_duration_seconds,
            'last_scan': scan_result.scan_time.isoformat(),
            'top_opportunity': scan_result.top_opportunities[0].symbol if scan_result.top_opportunities else None,
            'top_score': scan_result.top_opportunities[0].overall_score if scan_result.top_opportunities else 0
        }
    except Exception as e:
        return {
            'total_scanned': 0,
            'opportunities_found': 0,
            'scan_duration': 0,
            'last_scan': None,
            'top_opportunity': None,
            'top_score': 0,
            'error': str(e)
        }

def get_strategy_lab_stats():
    """Get Strategy Lab statistics"""
    try:
        # Create tournament
        tournament = StrategyTournament(
            initial_balance=10000.0,
            elimination_rate=0.3,
            min_survivors=2
        )
        
        # Create test data for tournament
        test_data = create_test_data(days=100)
        round_datasets = [
            ('Test Data', test_data)
        ]
        
        # Run tournament with test data
        results = tournament.run_tournament(round_datasets)
        
        return {
            'strategies_tested': results.total_strategies_entered,
            'winner': results.winner if results.winner != 'None' else 'None',
            'winner_score': results.winner_avg_score,
            'rounds_completed': results.total_rounds,
            'last_run': datetime.now().isoformat()
        }
    except Exception as e:
        return {
            'strategies_tested': 0,
            'winner': 'None',
            'winner_score': 0,
            'rounds_completed': 0,
            'last_run': None,
            'error': str(e)
        }

@app.route('/')
def dashboard():
    """Main dashboard with all market types"""
    return render_template('unified_dashboard.html')

@app.route('/api/polymarket/stats')
def polymarket_stats():
    """API endpoint for Polymarket statistics"""
    return jsonify(get_polymarket_stats())

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
        return jsonify(opportunities)
        
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/stocks/stats')
def stocks_stats():
    """API endpoint for stock statistics"""
    return jsonify(get_stock_stats())

@app.route('/api/stocks/opportunities')
def stocks_opportunities():
    """API endpoint for stock opportunities"""
    try:
        scanner = StockScanner()
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
        
        return jsonify(opportunities)
        
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/strategy-lab/stats')
def strategy_lab_stats():
    """API endpoint for Strategy Lab statistics"""
    return jsonify(get_strategy_lab_stats())

@app.route('/api/strategy-lab/tournament')
def strategy_lab_tournament():
    """API endpoint for tournament results"""
    try:
        tournament = StrategyTournament(
            initial_balance=10000.0,
            elimination_rate=0.3,
            min_survivors=2
        )
        
        # Create test data for tournament
        test_data = create_test_data(days=100)
        round_datasets = [
            ('Test Data', test_data)
        ]
        
        results = tournament.run_tournament(round_datasets)
        
        # Convert results to JSON-serializable format
        participants = []
        for p in results['participants']:
            participants.append({
                'name': p.name,
                'wins': p.wins,
                'losses': p.losses,
                'total_score': p.total_score,
                'avg_score': p.avg_score
            })
        
        winner_data = None
        if results['winner']:
            winner_data = {
                'name': results['winner'].name,
                'avg_score': results['winner'].avg_score,
                'wins': results['winner'].wins
            }
        
        return jsonify({
            'participants': participants,
            'winner': winner_data,
            'rounds_completed': results.total_rounds,
            'eliminations': results['eliminations']
        })
        
    except Exception as e:
        return jsonify({'error': str(e)})

if __name__ == '__main__':
    app.run(debug=False, host="127.0.0.1", port=5000)
# Strategy Lab Management - Global tournament state
current_tournament = None
tournament_in_progress = False
tournament_results_history = []

@app.route('/strategy-lab')
def strategy_lab():
    """Strategy Lab interface for interactive tournament management"""
    return render_template('strategy_lab.html')

@app.route('/api/strategy-lab/start-tournament', methods=['POST'])
def start_tournament():
    """Start a new tournament with custom parameters"""
    global current_tournament, tournament_in_progress
    
    try:
        data = request.get_json() or {}
        
        # Tournament parameters
        initial_balance = data.get('initial_balance', 10000.0)
        elimination_rate = data.get('elimination_rate', 0.3)
        min_survivors = data.get('min_survivors', 2)
        max_rounds = data.get('max_rounds', 3)
        data_days = max(60, data.get('data_days', 100))
        
        if tournament_in_progress:
            return jsonify({'error': 'Tournament already in progress'}), 400
        
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
        
        # Start tournament (blocking call - in production this would be async)
        tournament_in_progress = True
        results = tournament.run_tournament(round_datasets)
        tournament_in_progress = False
        
        # Store results
        current_tournament = results
        tournament_results_history.append({
            'timestamp': datetime.now().isoformat(),
            'results': results,
            'parameters': {
                'initial_balance': initial_balance,
                'elimination_rate': elimination_rate,
                'min_survivors': min_survivors,
                'max_rounds': max_rounds,
                'data_days': data_days
            }
        })
        
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
            'eliminations': results.elimination_log
        })
        
    except Exception as e:
        tournament_in_progress = False
        return jsonify({'error': str(e)}), 500

@app.route('/api/strategy-lab/status')
def tournament_status():
    """Get current tournament status"""
    global tournament_in_progress, current_tournament
    
    return jsonify({
        'in_progress': tournament_in_progress,
        'has_results': current_tournament is not None,
        'history_count': len(tournament_results_history)
    })

@app.route('/api/strategy-lab/results')
def tournament_results():
    """Get latest tournament results"""
    global current_tournament
    
    if not current_tournament:
        return jsonify({'error': 'No tournament results available'}), 404
    
    # Convert results for JSON
    participants = []
    for p in []:
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
    if current_tournament['winner']:
        winner_data = {
            'name': current_tournament['winner'].name,
            'avg_score': current_tournament['winner'].avg_score,
            'wins': current_tournament['winner'].wins,
            'total_score': current_tournament['winner'].total_score
        }
    
    return jsonify({
        'participants': participants,
        'winner': winner_data,
        'rounds_completed': current_tournament['rounds_completed'],
        'eliminations': current_tournament['eliminations']
    })

@app.route('/api/strategy-lab/export-winner')
def export_winner():
    """Export winning strategy details"""
    global current_tournament
    
    if not current_tournament or not current_tournament.get('winner'):
        return jsonify({'error': 'No winning strategy available'}), 404
    
    winner = current_tournament['winner']
    
    # Get strategy function source if possible
    try:
        import inspect
        strategy_source = inspect.getsource(winner.strategy_func)
    except:
        strategy_source = "# " + winner.name + " strategy (source not available)"
    
    export_data = {
        'strategy_name': winner.name,
        'performance': {
            'avg_score': winner.avg_score,
            'total_score': winner.total_score,
            'wins': winner.wins,
            'losses': winner.losses,
            'rounds_survived': winner.rounds_survived
        },
        'strategy_source': strategy_source,
        'export_timestamp': datetime.now().isoformat(),
        'tournament_info': {
            'rounds_completed': current_tournament['rounds_completed'],
            'total_participants': len([])
        }
    }
    
    return jsonify(export_data)

@app.route('/api/strategy-lab/history')
def tournament_history():
    """Get tournament history"""
    global tournament_results_history
    
    # Return simplified history (last 10 tournaments)
    history = []
    for entry in tournament_results_history[-10:]:
        winner_name = None
        if entry['results'].get('winner'):
            winner_name = entry['results']['winner'].name
        
        history.append({
            'timestamp': entry['timestamp'],
            'winner': winner_name,
            'rounds_completed': entry['results']['rounds_completed'],
            'participants_count': len(entry['results']['participants']),
            'parameters': entry['parameters']
        })
    
    return jsonify(history)