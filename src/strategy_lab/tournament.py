"""
TradeSight Strategy Tournament System

Runs multiple strategies in parallel, ranks them by performance,
eliminates losers, evolves winners over multiple rounds.
Implements Alex Carter's strategy tournament approach.
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Callable, Optional, Tuple
from dataclasses import dataclass, asdict, field
from datetime import datetime
import json
import copy

from .backtest import BacktestEngine, simple_ma_crossover, rsi_mean_reversion
from .ai_engine import AIStrategyEngine, StrategyGeneration, create_test_data


@dataclass
class TournamentEntry:
    """A strategy competing in the tournament"""
    name: str
    strategy_func: Callable
    generation: int = 0
    wins: int = 0
    losses: int = 0
    total_score: float = 0.0
    avg_score: float = 0.0
    rounds_survived: int = 0
    eliminated: bool = False
    elimination_round: Optional[int] = None
    backtest_history: List[Dict] = field(default_factory=list)


@dataclass
class TournamentRound:
    """Results from one tournament round"""
    round_number: int
    asset_name: str
    entries_count: int
    eliminated_count: int
    surviving_entries: List[str]
    eliminated_entries: List[str]
    rankings: List[Dict]  # [{name, score, win_rate, pnl_pct}]
    best_performer: str
    worst_performer: str


@dataclass
class TournamentResults:
    """Final tournament results"""
    total_rounds: int
    total_strategies_entered: int
    final_survivors: int
    rounds: List[TournamentRound]
    winner: str
    winner_avg_score: float
    top_3: List[Dict]  # [{name, avg_score, rounds_survived, total_wins}]
    elimination_log: List[Dict]  # [{name, round, reason, final_score}]


class StrategyTournament:
    """
    Strategy Tournament System
    
    Runs N strategies head-to-head across multiple rounds.
    Each round tests on different data/assets.
    Bottom performers get eliminated each round.
    Winners can optionally evolve between rounds.
    
    Tournament flow:
    1. Register 10+ strategies
    2. Round 1: Test all on Asset A, eliminate bottom 30%
    3. Round 2: Test survivors on Asset B, eliminate bottom 30%
    4. Round 3: Test survivors on Asset C, rank final winners
    5. Report top 3 strategies with full metrics
    """
    
    def __init__(self, 
                 initial_balance: float = 500.0,
                 elimination_rate: float = 0.3,
                 min_survivors: int = 3):
        self.initial_balance = initial_balance
        self.elimination_rate = elimination_rate  # Bottom 30% eliminated each round
        self.min_survivors = min_survivors  # Never go below this many
        self.backtest_engine = BacktestEngine(initial_balance)
        self.ai_engine = AIStrategyEngine(initial_balance)
        self.entries: List[TournamentEntry] = []
        self.rounds: List[TournamentRound] = []
        self.started = False
    
    def register_strategy(self, name: str, strategy_func: Callable) -> bool:
        """Register a strategy for the tournament"""
        if self.started:
            raise RuntimeError("Cannot register after tournament has started")
        
        # Check for duplicate names
        if any(e.name == name for e in self.entries):
            raise ValueError(f"Strategy '{name}' already registered")
        
        entry = TournamentEntry(
            name=name,
            strategy_func=strategy_func
        )
        self.entries.append(entry)
        return True
    
    def run_tournament(self, 
                      round_datasets: List[Tuple[str, pd.DataFrame]],
                      evolve_between_rounds: bool = False) -> TournamentResults:
        """
        Run the full tournament across multiple rounds.
        
        Args:
            round_datasets: List of (asset_name, DataFrame) tuples, one per round
            evolve_between_rounds: If True, run AI evolution on winners between rounds
            
        Returns:
            TournamentResults with full tournament history
        """
        if len(self.entries) < 2:
            raise ValueError("Need at least 2 strategies to run a tournament")
        
        self.started = True
        self.rounds = []
        
        print(f"\n🏆 STRATEGY TOURNAMENT")
        print(f"{'='*60}")
        print(f"📊 Strategies entered: {len(self.entries)}")
        print(f"🔄 Rounds: {len(round_datasets)}")
        print(f"⚔️ Elimination rate: {self.elimination_rate*100:.0f}% per round")
        print(f"🛡️ Minimum survivors: {self.min_survivors}")
        print(f"{'='*60}\n")
        
        for round_num, (asset_name, data) in enumerate(round_datasets, 1):
            active_entries = [e for e in self.entries if not e.eliminated]
            
            if len(active_entries) <= self.min_survivors:
                print(f"⚠️ Only {len(active_entries)} strategies remaining. Ending tournament.")
                break
            
            print(f"\n{'─'*60}")
            print(f"🥊 ROUND {round_num}: Testing on {asset_name}")
            print(f"{'─'*60}")
            print(f"Active strategies: {len(active_entries)}")
            
            # Run backtests for all active strategies
            round_results = []
            for entry in active_entries:
                try:
                    result = self.backtest_engine.run_backtest(
                        data, entry.strategy_func, asset_name
                    )
                    score = self.ai_engine._calculate_performance_score(result)
                    
                    entry.backtest_history.append({
                        'round': round_num,
                        'asset': asset_name,
                        'score': score,
                        'metrics': result['metrics']
                    })
                    entry.total_score += score
                    entry.rounds_survived += 1
                    
                    round_results.append({
                        'entry': entry,
                        'score': score,
                        'metrics': result['metrics']
                    })
                    
                    win_rate = result['metrics']['win_rate']
                    pnl = result['metrics']['total_pnl_pct']
                    trades = result['metrics']['total_trades']
                    print(f"  {entry.name}: Score {score:.3f}, "
                          f"WR {win_rate:.1f}%, PnL {pnl:.1f}%, "
                          f"Trades {trades}")
                    
                except Exception as e:
                    print(f"  {entry.name}: ❌ FAILED - {str(e)[:80]}")
                    round_results.append({
                        'entry': entry,
                        'score': -1.0,
                        'metrics': None
                    })
            
            # Sort by score (best first)
            round_results.sort(key=lambda x: x['score'], reverse=True)
            
            # Determine eliminations
            num_to_eliminate = max(1, int(len(active_entries) * self.elimination_rate))
            # Don't eliminate below minimum
            num_to_eliminate = min(num_to_eliminate, len(active_entries) - self.min_survivors)
            num_to_eliminate = max(0, num_to_eliminate)
            
            eliminated_this_round = []
            surviving_this_round = []
            
            for i, result in enumerate(round_results):
                entry = result['entry']
                if i >= len(round_results) - num_to_eliminate:
                    entry.eliminated = True
                    entry.elimination_round = round_num
                    entry.losses += 1
                    eliminated_this_round.append(entry.name)
                    print(f"  ❌ ELIMINATED: {entry.name} (Score: {result['score']:.3f})")
                else:
                    entry.wins += 1
                    surviving_this_round.append(entry.name)
            
            # Update average scores
            for entry in self.entries:
                if entry.rounds_survived > 0:
                    entry.avg_score = entry.total_score / entry.rounds_survived
            
            # Build rankings
            rankings = []
            for result in round_results:
                entry = result['entry']
                ranking = {
                    'name': entry.name,
                    'score': result['score'],
                    'win_rate': result['metrics']['win_rate'] if result['metrics'] else 0,
                    'pnl_pct': result['metrics']['total_pnl_pct'] if result['metrics'] else 0,
                    'eliminated': entry.eliminated
                }
                rankings.append(ranking)
            
            # Record round
            tournament_round = TournamentRound(
                round_number=round_num,
                asset_name=asset_name,
                entries_count=len(active_entries),
                eliminated_count=num_to_eliminate,
                surviving_entries=surviving_this_round,
                eliminated_entries=eliminated_this_round,
                rankings=rankings,
                best_performer=round_results[0]['entry'].name if round_results else "None",
                worst_performer=round_results[-1]['entry'].name if round_results else "None"
            )
            self.rounds.append(tournament_round)
            
            print(f"\n  🏅 Round {round_num} winner: {tournament_round.best_performer}")
            print(f"  Survivors: {len(surviving_this_round)}, Eliminated: {len(eliminated_this_round)}")
        
        # Build final results
        survivors = [e for e in self.entries if not e.eliminated]
        survivors.sort(key=lambda e: e.avg_score, reverse=True)
        
        top_3 = []
        for entry in survivors[:3]:
            top_3.append({
                'name': entry.name,
                'avg_score': entry.avg_score,
                'rounds_survived': entry.rounds_survived,
                'total_wins': entry.wins,
                'total_losses': entry.losses
            })
        
        elimination_log = []
        for entry in self.entries:
            if entry.eliminated:
                elimination_log.append({
                    'name': entry.name,
                    'round': entry.elimination_round,
                    'final_score': entry.avg_score,
                    'rounds_survived': entry.rounds_survived
                })
        
        winner = survivors[0] if survivors else None
        
        results = TournamentResults(
            total_rounds=len(self.rounds),
            total_strategies_entered=len(self.entries),
            final_survivors=len(survivors),
            rounds=self.rounds,
            winner=winner.name if winner else "None",
            winner_avg_score=winner.avg_score if winner else 0.0,
            top_3=top_3,
            elimination_log=elimination_log
        )
        
        # Print final results
        print(f"\n{'='*60}")
        print(f"🏆 TOURNAMENT RESULTS")
        print(f"{'='*60}")
        print(f"Winner: {results.winner} (avg score: {results.winner_avg_score:.3f})")
        print(f"\nTop 3:")
        for i, entry in enumerate(top_3, 1):
            print(f"  {i}. {entry['name']} — Avg Score: {entry['avg_score']:.3f}, "
                  f"Wins: {entry['total_wins']}, Survived: {entry['rounds_survived']} rounds")
        
        if elimination_log:
            print(f"\nElimination Log:")
            for elim in elimination_log:
                print(f"  ❌ {elim['name']} — Round {elim['round']}, "
                      f"Final Score: {elim['final_score']:.3f}")
        
        return results
    
    def get_surviving_strategies(self) -> List[TournamentEntry]:
        """Get list of strategies that survived the tournament"""
        return [e for e in self.entries if not e.eliminated]
    
    def get_entry_by_name(self, name: str) -> Optional[TournamentEntry]:
        """Find a tournament entry by strategy name"""
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None


# --- Built-in Strategy Library for Tournament Testing ---

def macd_crossover(data: pd.DataFrame, index: int, positions: List) -> Optional[Dict]:
    """MACD crossover strategy"""
    if index < 50:
        return None
    current = data.iloc[index]
    prev = data.iloc[index - 1]
    
    # Buy: MACD crosses above signal
    if (current['macd'] > current['macd_signal'] and 
        prev['macd'] <= prev['macd_signal'] and not positions):
        return {
            'action': 'buy', 'size': 0.7,
            'stop_loss': current['close'] * 0.96,
            'take_profit': current['close'] * 1.08
        }
    
    # Sell: MACD crosses below signal
    if (current['macd'] < current['macd_signal'] and 
        prev['macd'] >= prev['macd_signal'] and positions):
        return {'action': 'close'}
    return None


def bollinger_bounce(data: pd.DataFrame, index: int, positions: List) -> Optional[Dict]:
    """Bollinger Band bounce strategy"""
    if index < 50:
        return None
    current = data.iloc[index]
    
    # Buy: Price touches lower band
    if current['close'] <= current['bb_lower'] and not positions:
        return {
            'action': 'buy', 'size': 0.6,
            'stop_loss': current['close'] * 0.97,
            'take_profit': current['bb_middle']
        }
    
    # Sell: Price touches upper band
    if current['close'] >= current['bb_upper'] and positions:
        return {'action': 'close'}
    return None


def dual_ma_rsi(data: pd.DataFrame, index: int, positions: List) -> Optional[Dict]:
    """Dual MA + RSI confirmation strategy"""
    if index < 50:
        return None
    current = data.iloc[index]
    prev = data.iloc[index - 1]
    
    # Buy: MA crossover + RSI not overbought
    if (current['sma_20'] > current['sma_50'] and 
        prev['sma_20'] <= prev['sma_50'] and 
        current['rsi'] < 65 and not positions):
        return {
            'action': 'buy', 'size': 0.75,
            'stop_loss': current['close'] * 0.94,
            'take_profit': current['close'] * 1.12
        }
    
    # Sell: MA cross down or RSI overbought
    if positions and (current['rsi'] > 75 or 
        (current['sma_20'] < current['sma_50'] and prev['sma_20'] >= prev['sma_50'])):
        return {'action': 'close'}
    return None


def momentum_breakout(data: pd.DataFrame, index: int, positions: List) -> Optional[Dict]:
    """Momentum breakout strategy"""
    if index < 50:
        return None
    current = data.iloc[index]
    
    # Calculate simple momentum (% change over 10 periods)
    momentum = (current['close'] - data.iloc[index - 10]['close']) / data.iloc[index - 10]['close']
    
    # Buy: Strong upward momentum + RSI not extreme
    if momentum > 0.03 and current['rsi'] < 70 and not positions:
        return {
            'action': 'buy', 'size': 0.5,
            'stop_loss': current['close'] * 0.95,
            'take_profit': current['close'] * 1.10
        }
    
    # Sell: Momentum reversal
    if momentum < -0.02 and positions:
        return {'action': 'close'}
    return None


def conservative_trend(data: pd.DataFrame, index: int, positions: List) -> Optional[Dict]:
    """Conservative trend-following strategy"""
    if index < 100:
        return None
    current = data.iloc[index]
    
    # Buy: Price above SMA100 + RSI between 40-60 (trending, not extreme)
    if (current['close'] > current['sma_100'] and 
        40 < current['rsi'] < 60 and not positions):
        return {
            'action': 'buy', 'size': 0.4,
            'stop_loss': current['close'] * 0.97,
            'take_profit': current['close'] * 1.06
        }
    
    # Sell: Price drops below SMA100
    if current['close'] < current['sma_100'] and positions:
        return {'action': 'close'}
    return None


def get_builtin_strategies() -> Dict[str, Callable]:
    """Get all built-in strategies for tournament use"""
    return {
        'MA Crossover': simple_ma_crossover,
        'RSI Mean Reversion': rsi_mean_reversion,
        'MACD Crossover': macd_crossover,
        'Bollinger Bounce': bollinger_bounce,
        'Dual MA + RSI': dual_ma_rsi,
        'Momentum Breakout': momentum_breakout,
        'Conservative Trend': conservative_trend,
    }
