#!/usr/bin/env python3
"""
TradeSight Web Dashboard
Simple Flask app to visualize market opportunities
"""

from flask import Flask, render_template, jsonify, request
import sqlite3
import json
from datetime import datetime, timedelta
import os

app = Flask(__name__)

def get_db_connection():
    """Get database connection"""
    db_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'tradesight.db')
    return sqlite3.connect(db_path)

def get_market_stats():
    """Get overall market statistics"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Basic counts
    cursor.execute("SELECT COUNT(*) FROM markets WHERE active = 1")
    active_markets = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM opportunities WHERE signal_time > datetime('now', '-24 hours')")
    opportunities_24h = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM opportunities WHERE opportunity_type = 'arbitrage' AND signal_time > datetime('now', '-24 hours')")
    arbitrage_24h = cursor.fetchone()[0]
    
    # Total volume
    cursor.execute("SELECT SUM(volume) FROM markets WHERE active = 1")
    total_volume = cursor.fetchone()[0] or 0
    
    conn.close()
    return {
        'active_markets': active_markets,
        'opportunities_24h': opportunities_24h,
        'arbitrage_24h': arbitrage_24h,
        'total_volume': total_volume
    }

def get_top_opportunities(limit=10):
    """Get top opportunities from database"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT m.question, m.category, m.volume, m.price_yes, m.price_no, 
               o.opportunity_type, o.confidence_score, o.expected_profit, o.notes
        FROM opportunities o
        JOIN markets m ON o.market_id = m.id
        WHERE o.signal_time > datetime('now', '-24 hours')
        ORDER BY o.confidence_score DESC, m.volume DESC
        LIMIT ?
    ''', (limit,))
    
    opportunities = []
    for row in cursor.fetchall():
        opportunities.append({
            'question': row[0],
            'category': row[1],
            'volume': row[2],
            'price_yes': row[3],
            'price_no': row[4],
            'type': row[5],
            'confidence': row[6],
            'expected_profit': row[7],
            'notes': row[8]
        })
    
    conn.close()
    return opportunities

def get_category_breakdown():
    """Get market breakdown by category"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT category, COUNT(*) as count, SUM(volume) as volume
        FROM markets 
        WHERE active = 1 AND category IS NOT NULL AND category != ''
        GROUP BY category
        ORDER BY volume DESC
        LIMIT 10
    ''')
    
    categories = []
    for row in cursor.fetchall():
        categories.append({
            'category': row[0],
            'count': row[1],
            'volume': row[2]
        })
    
    conn.close()
    return categories

@app.route('/')
def dashboard():
    """Main dashboard page"""
    stats = get_market_stats()
    opportunities = get_top_opportunities(10)
    categories = get_category_breakdown()
    
    return render_template('dashboard.html', 
                         stats=stats, 
                         opportunities=opportunities,
                         categories=categories,
                         last_update=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

@app.route('/api/stats')
def api_stats():
    """API endpoint for market stats"""
    return jsonify(get_market_stats())

@app.route('/api/opportunities')
def api_opportunities():
    """API endpoint for opportunities"""
    limit = request.args.get('limit', 20, type=int)
    return jsonify(get_top_opportunities(limit))

@app.route('/api/categories')
def api_categories():
    """API endpoint for category breakdown"""
    return jsonify(get_category_breakdown())

if __name__ == '__main__':
    # Create templates directory if it doesn't exist
    templates_dir = os.path.join(os.path.dirname(__file__), 'templates')
    if not os.path.exists(templates_dir):
        os.makedirs(templates_dir)
    
    app.run(host='0.0.0.0', port=3002, debug=True)