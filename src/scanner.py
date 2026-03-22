#!/usr/bin/env python3
"""
TradeSight - Polymarket Scanner
Phase 1: Read-only market intelligence platform

Features:
- Fetches all active Polymarket markets
- Identifies arbitrage opportunities (Yes + No < $1.00)
- Tracks volume and price movements
- Stores historical data for backtesting
- Confluence strategy: multi-indicator signal confirmation
"""

import requests
import sqlite3
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
import logging
import pandas as pd

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Try to import TechnicalIndicators (requires talib)
try:
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'indicators'))
    from technical_indicators import TechnicalIndicators
    _INDICATORS_AVAILABLE = True
except ImportError:
    _INDICATORS_AVAILABLE = False
    logger.warning("TechnicalIndicators not available — confluence scoring disabled")


class PolymarketScanner:
    def __init__(self, db_path: str = "data/tradesight.db"):
        self.db_path = db_path
        self.api_base = "https://gamma-api.polymarket.com"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'TradeSight/1.0 (Research Scanner)',
            'Accept': 'application/json'
        })
        self._indicators_engine = TechnicalIndicators() if _INDICATORS_AVAILABLE else None
        self.init_database()

    def init_database(self):
        """Initialize SQLite database with required tables"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Markets table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS markets (
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                category TEXT,
                end_date TEXT,
                volume REAL,
                liquidity REAL,
                active BOOLEAN,
                closed BOOLEAN,
                outcomes TEXT,
                price_yes REAL,
                price_no REAL,
                last_updated TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Price snapshots table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT,
                timestamp TEXT,
                price_yes REAL,
                price_no REAL,
                volume_24h REAL,
                best_bid REAL,
                best_ask REAL,
                spread REAL,
                FOREIGN KEY (market_id) REFERENCES markets (id)
            )
        ''')

        # Opportunities table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT,
                opportunity_type TEXT,
                signal_time TEXT,
                confidence_score REAL,
                expected_profit REAL,
                signal_price_yes REAL,
                signal_price_no REAL,
                volume_signal REAL,
                notes TEXT,
                FOREIGN KEY (market_id) REFERENCES markets (id)
            )
        ''')

        conn.commit()
        conn.close()
        logger.info(f"Database initialized: {self.db_path}")

    def fetch_markets(self, active_only: bool = True, limit: int = 1000) -> List[Dict]:
        """Fetch markets from Polymarket API"""
        params = {
            'limit': limit,
            'active': 'true' if active_only else 'false',
            'closed': 'false' if active_only else None
        }
        # Remove None values
        params = {k: v for k, v in params.items() if v is not None}

        try:
            response = self.session.get(f"{self.api_base}/markets", params=params)
            response.raise_for_status()
            markets = response.json()
            logger.info(f"Fetched {len(markets)} markets from Polymarket")
            return markets
        except requests.RequestException as e:
            logger.error(f"Failed to fetch markets: {e}")
            return []

    def parse_market_data(self, market: Dict) -> Dict:
        """Parse and normalize market data"""
        try:
            # Parse outcome prices
            outcome_prices = json.loads(market.get('outcomePrices', '["0", "0"]'))
            price_yes = float(outcome_prices[0]) if len(outcome_prices) > 0 else 0.0
            price_no = float(outcome_prices[1]) if len(outcome_prices) > 1 else 0.0

            return {
                'id': market['id'],
                'question': market['question'],
                'category': market.get('category', ''),
                'end_date': market.get('endDate', ''),
                'volume': float(market.get('volume', 0)),
                'liquidity': float(market.get('liquidity', 0)),
                'active': market.get('active', False),
                'closed': market.get('closed', True),
                'outcomes': market.get('outcomes', '[]'),
                'price_yes': price_yes,
                'price_no': price_no,
                'last_updated': datetime.now(timezone.utc).isoformat(),
                'volume_24h': float(market.get('volume24hr', 0)),
                'best_bid': float(market.get('bestBid', 0)),
                'best_ask': float(market.get('bestAsk', 1)),
                'spread': float(market.get('spread', 1))
            }
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to parse market {market.get('id', 'unknown')}: {e}")
            return None

    def detect_arbitrage(self, price_yes: float, price_no: float, min_profit: float = 0.02) -> Optional[Dict]:
        """Detect arbitrage opportunities (Yes + No < $1.00)"""
        total_cost = price_yes + price_no
        if total_cost < (1.0 - min_profit):  # 2% minimum profit
            guaranteed_profit = 1.0 - total_cost
            return {
                'type': 'arbitrage',
                'confidence': 1.0,  # Arbitrage is guaranteed profit
                'expected_profit': guaranteed_profit,
                'profit_percent': (guaranteed_profit / total_cost) * 100,
                'notes': f"Buy both: Yes@${price_yes:.3f} + No@${price_no:.3f} = ${total_cost:.3f}, guaranteed profit: ${guaranteed_profit:.3f}"
            }
        return None

    def score_market_opportunity(self, market: Dict) -> float:
        """Score market opportunity based on multiple factors"""
        score = 0.0

        # Volume score (higher volume = better liquidity)
        volume = market['volume']
        if volume > 100000:
            score += 0.3
        elif volume > 10000:
            score += 0.2
        elif volume > 1000:
            score += 0.1

        # Liquidity score
        liquidity = market['liquidity']
        if liquidity > 10000:
            score += 0.2
        elif liquidity > 1000:
            score += 0.1

        # Price efficiency score (closer to 0.5/0.5 = more uncertain = more opportunity)
        price_yes = market['price_yes']
        price_no = market['price_no']
        if price_yes > 0 and price_no > 0:
            balance = 1 - abs(price_yes - price_no)
            score += balance * 0.3

        # Spread score (tighter spread = better)
        spread = market['spread']
        if spread < 0.05:  # 5%
            score += 0.2
        elif spread < 0.1:  # 10%
            score += 0.1

        return min(score, 1.0)

    def get_price_history(self, market_id: str, n: int = 60) -> Optional[pd.DataFrame]:
        """
        Retrieve the last N price snapshots for a market as an OHLCV DataFrame.

        Builds synthetic OHLCV from Polymarket data:
          close  = price_yes (prediction probability)
          open   = previous close (shift by 1 period)
          high   = best_ask (or price_yes if zero)
          low    = best_bid (or price_yes if zero)
          volume = volume_24h

        Returns None if fewer than 14 snapshots exist (not enough for any indicator).
        Requires at least 50 for full TechnicalIndicators.calculate_all().
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, price_yes, best_bid, best_ask, volume_24h
            FROM price_snapshots
            WHERE market_id = ?
            ORDER BY timestamp ASC
            LIMIT ?
        ''', (market_id, n))
        rows = cursor.fetchall()
        conn.close()

        if len(rows) < 14:
            return None  # Not enough history for any indicator

        timestamps, closes, bids, asks, volumes = zip(*rows)

        closes = [float(c) for c in closes]
        # Open = previous close (first open = first close)
        opens = [closes[0]] + closes[:-1]
        # High = max(ask, close) to ensure high >= close
        highs = [max(float(a) if float(a) > 0 else c, c) for a, c in zip(asks, closes)]
        # Low = min(bid, close) to ensure low <= close; fall back to close if bid is zero
        lows = [min(float(b) if float(b) > 0 else c, c) for b, c in zip(bids, closes)]
        # Volume: use 1.0 as floor to avoid zero-division in VWAP
        vols = [float(v) if v and float(v) > 0 else 1.0 for v in volumes]

        try:
            idx = pd.to_datetime(list(timestamps))
        except Exception:
            idx = pd.RangeIndex(len(closes))

        return pd.DataFrame({
            'open':   opens,
            'high':   highs,
            'low':    lows,
            'close':  closes,
            'volume': vols
        }, index=idx)

    def detect_confluence(self, indicators: Dict) -> bool:
        """
        Multi-indicator confluence gate.

        Returns True when multi-indicator signals agree on opportunity:
          - confluence_score > 0.2  (net bullish bias)
          - At least 3 of 5 core signals are positive:
              RSI non-bearish, MACD bullish crossover, Bollinger above midline,
              SuperTrend bullish, price above VWAP

        Design note: for prediction markets we treat 'bullish' as 'opportunity'
        (probability moving toward certainty), not traditional equity direction.
        """
        signals = indicators.get('signals', {})
        conf_score = indicators.get('confluence_score', 0.0)

        # Require net positive confluence score
        if conf_score <= 0.2:
            return False

        # Count positive signals from 5 core indicators
        positive = sum([
            1 if signals.get('rsi', 0) >= 0 else 0,
            1 if signals.get('macd', 0) > 0 else 0,
            1 if float(signals.get('bollinger', 0)) > 0 else 0,
            1 if signals.get('supertrend', 0) > 0 else 0,
            1 if signals.get('vwap', 0) > 0 else 0,
        ])

        return positive >= 3

    def store_market_data(self, markets: List[Dict]):
        """Store market data and price snapshots"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        for market in markets:
            if market is None:
                continue

            # Store/update market data
            cursor.execute('''
                INSERT OR REPLACE INTO markets
                (id, question, category, end_date, volume, liquidity, active, closed,
                 outcomes, price_yes, price_no, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                market['id'], market['question'], market['category'], market['end_date'],
                market['volume'], market['liquidity'], market['active'], market['closed'],
                market['outcomes'], market['price_yes'], market['price_no'], market['last_updated']
            ))

            # Store price snapshot
            cursor.execute('''
                INSERT INTO price_snapshots
                (market_id, timestamp, price_yes, price_no, volume_24h, best_bid, best_ask, spread)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                market['id'], market['last_updated'], market['price_yes'], market['price_no'],
                market['volume_24h'], market['best_bid'], market['best_ask'], market['spread']
            ))

        conn.commit()
        conn.close()
        logger.info(f"Stored {len(markets)} market updates")

    def store_opportunity(self, market_id: str, opportunity: Dict):
        """Store detected opportunity"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO opportunities
            (market_id, opportunity_type, signal_time, confidence_score, expected_profit, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            market_id, opportunity['type'], datetime.now(timezone.utc).isoformat(),
            opportunity['confidence'], opportunity['expected_profit'], opportunity['notes']
        ))

        conn.commit()
        conn.close()

    def scan_markets(self) -> Dict:
        """Main scanning function"""
        logger.info("Starting market scan...")

        # Fetch current markets
        raw_markets = self.fetch_markets(active_only=True)
        if not raw_markets:
            logger.warning("No markets fetched")
            return {'markets': 0, 'opportunities': 0, 'arbitrage': 0}

        # Parse market data
        markets = []
        opportunities = []
        arbitrage_count = 0
        confluence_count = 0

        for raw_market in raw_markets:
            market = self.parse_market_data(raw_market)
            if market is None:
                continue

            markets.append(market)

            # Check for arbitrage (highest priority signal)
            arbitrage = self.detect_arbitrage(market['price_yes'], market['price_no'])
            if arbitrage:
                arbitrage_count += 1
                opportunities.append({
                    'market_id': market['id'],
                    'market_question': market['question'],
                    'opportunity': arbitrage
                })
                self.store_opportunity(market['id'], arbitrage)
                logger.info(f"ARBITRAGE FOUND: {market['question'][:60]}... - {arbitrage['notes']}")
                continue  # Don't double-score arbitrage markets

            # Base opportunity score
            score = self.score_market_opportunity(market)

            # --- Confluence Strategy gate ---
            # For promising markets (score >= 0.5), attempt multi-indicator confirmation
            if score >= 0.5 and self._indicators_engine is not None:
                history = self.get_price_history(market['id'], n=60)
                if history is not None and len(history) >= 50:
                    try:
                        indicators = self._indicators_engine.calculate_all(history)
                        if self.detect_confluence(indicators):
                            conf_score = indicators.get('confluence_score', score)
                            opp = {
                                'type': 'confluence',
                                'confidence': round(min(1.0, score + conf_score * 0.3), 4),
                                'expected_profit': 0.0,
                                'notes': (
                                    f"Confluence confirmed: score={conf_score:.3f}, "
                                    f"base={score:.2f}, "
                                    f"RSI={indicators['indicators']['rsi']:.1f}, "
                                    f"MACD_signal={indicators['signals']['macd']}, "
                                    f"BB_pos={indicators['indicators']['bollinger']['position']:.2f}"
                                )
                            }
                            opportunities.append({
                                'market_id': market['id'],
                                'market_question': market['question'],
                                'opportunity': opp
                            })
                            self.store_opportunity(market['id'], opp)
                            confluence_count += 1
                            logger.info(f"CONFLUENCE: {market['question'][:60]}... score={conf_score:.3f}")
                            continue  # Skip the generic high_opportunity check below
                    except Exception as e:
                        logger.debug(f"Indicator calc failed for {market['id']}: {e}")

            # Fallback: store high-scoring markets without confluence confirmation
            if score > 0.7:
                opp = {
                    'type': 'high_opportunity',
                    'confidence': score,
                    'expected_profit': 0.0,
                    'notes': f"High opportunity score: {score:.2f} - Volume: ${market['volume']:,.0f}"
                }
                opportunities.append({
                    'market_id': market['id'],
                    'market_question': market['question'],
                    'opportunity': opp
                })
                self.store_opportunity(market['id'], opp)

        # Store all market data (including snapshots for future history)
        self.store_market_data(markets)

        result = {
            'scan_time': datetime.now(timezone.utc).isoformat(),
            'markets': len(markets),
            'opportunities': len(opportunities),
            'arbitrage': arbitrage_count,
            'confluence': confluence_count,
            'top_opportunities': opportunities[:10]  # Top 10 for quick review
        }

        logger.info(
            f"Scan complete: {result['markets']} markets, "
            f"{result['opportunities']} opportunities, "
            f"{result['arbitrage']} arbitrage, "
            f"{result['confluence']} confluence"
        )
        return result


def main():
    """Run a single market scan"""
    scanner = PolymarketScanner()
    result = scanner.scan_markets()

    print(f"\n=== TradeSight Market Scan Results ===")
    print(f"Scan Time: {result['scan_time']}")
    print(f"Markets Analyzed: {result['markets']}")
    print(f"Opportunities Found: {result['opportunities']}")
    print(f"Arbitrage Opportunities: {result['arbitrage']}")
    print(f"Confluence Signals: {result['confluence']}")

    if result['top_opportunities']:
        print(f"\nTop Opportunities:")
        for i, opp in enumerate(result['top_opportunities'][:5], 1):
            print(f"{i}. {opp['market_question'][:70]}...")
            print(f"   Type: {opp['opportunity']['type']} | Confidence: {opp['opportunity']['confidence']:.2f}")
            if opp['opportunity']['expected_profit'] > 0:
                print(f"   Expected Profit: ${opp['opportunity']['expected_profit']:.3f}")
            print(f"   Notes: {opp['opportunity']['notes']}")
            print()

    return result


if __name__ == "__main__":
    main()
