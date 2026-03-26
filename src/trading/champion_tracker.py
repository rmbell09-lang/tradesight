#!/usr/bin/env python3
"""
TradeSight Champion/Challenger Tracker

Maintains a "champion" strategy that only gets replaced when a challenger
proves itself superior over multiple sessions — not just one backtest.

Rules:
- Champion is stored in data/champion.json
- A challenger must beat the champion by PROMOTION_THRESHOLD (default 10%)
  on blended score AND have at least MIN_CHALLENGE_SESSIONS data points
- If no champion exists yet, the first optimizer result becomes champion
- Champion params are what the paper trader actually uses
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

PROMOTION_THRESHOLD = 0.10        # challenger must be 10% better to take over
MIN_CHALLENGE_SESSIONS = 3        # must have at least 3 live sessions before challenging
CIRCUIT_BREAKER_PNL = -10.0       # force champion replacement if live avg P&L drops below this
CIRCUIT_BREAKER_SESSIONS = 5      # minimum sessions before circuit breaker can fire
CHAMPION_FILE = 'data/champion.json'


class ChampionTracker:
    """Manages the champion strategy and challenger promotion logic."""

    def __init__(self, base_dir: str = None):
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent.parent
        self.champion_path = self.base_dir / CHAMPION_FILE
        self.champion_path.parent.mkdir(exist_ok=True)

    def get_champion(self) -> Optional[Dict]:
        """Load current champion. Returns None if no champion yet."""
        if not self.champion_path.exists():
            return None
        with open(self.champion_path) as f:
            return json.load(f)

    def _save_champion(self, params: Dict, backtest_score: float,
                       live_avg_pnl: float, sessions: int, reason: str):
        champion = {
            'params': params,
            'backtest_score': round(backtest_score, 6),
            'live_avg_pnl': round(live_avg_pnl, 4),
            'sessions': sessions,
            'promoted_at': datetime.now().isoformat(),
            'promotion_reason': reason
        }
        with open(self.champion_path, 'w') as f:
            json.dump(champion, f, indent=2)
        logger.info(f"Champion saved: {reason}")

    def evaluate_challenger(self, challenger_params: Dict,
                             challenger_backtest_score: float,
                             feedback_tracker) -> Tuple[Dict, str]:
        """
        Compare challenger against champion.
        Returns (params_to_use, decision_reason).

        Decision logic:
          1. No champion → challenger becomes champion immediately
          2. Challenger has < MIN_CHALLENGE_SESSIONS live data → use champion
          3. Challenger blended score > champion score * (1 + THRESHOLD) → promote
          4. Otherwise → keep champion
        """
        champion = self.get_champion()

        # Refresh champion live stats from feedback DB (always, not just when challenger wins)
        if champion is not None and feedback_tracker:
            champ_params = champion['params']
            for entry in (feedback_tracker.get_top_params(n=50) or []):
                ep = entry['params']
                if (ep.get('oversold') == champ_params.get('oversold') and
                        ep.get('overbought') == champ_params.get('overbought') and
                        abs(ep.get('position_size', 0) - champ_params.get('position_size', 0)) < 0.01 and
                        abs(ep.get('stop_loss_pct', 0) - champ_params.get('stop_loss_pct', 0)) < 0.01):
                    if entry['times_used'] > champion.get('sessions', 0):
                        champion['live_avg_pnl'] = entry['avg_pnl']
                        champion['sessions'] = entry['times_used']
                        self._save_champion(
                            params=champ_params,
                            backtest_score=champion.get('backtest_score', 0),
                            live_avg_pnl=entry['avg_pnl'],
                            sessions=entry['times_used'],
                            reason='Live stats refreshed'
                        )
                        logger.info(f"Champion live stats refreshed: avg_pnl={entry['avg_pnl']:.2f}% over {entry['times_used']} sessions")
                    break

        # CIRCUIT BREAKER: if champion is consistently losing, force replacement even without challenger data
        if champion is not None:
            champ_live_pnl = champion.get('live_avg_pnl', 0.0)
            champ_sessions = champion.get('sessions', 0)
            if (champ_sessions >= CIRCUIT_BREAKER_SESSIONS and
                    champ_live_pnl < CIRCUIT_BREAKER_PNL):
                reason = (f"CIRCUIT BREAKER: Champion avg P&L {champ_live_pnl:.2f}% over {champ_sessions} sessions "
                          f"below threshold {CIRCUIT_BREAKER_PNL}% — forcing replacement with challenger")
                logger.warning(reason)
                self._save_champion(
                    params=challenger_params,
                    backtest_score=challenger_backtest_score,
                    live_avg_pnl=0.0,
                    sessions=0,
                    reason=reason
                )
                return challenger_params, reason

        # Case 1: No champion yet
        if champion is None:
            reason = "First run — challenger becomes champion by default"
            self._save_champion(
                params=challenger_params,
                backtest_score=challenger_backtest_score,
                live_avg_pnl=0.0,
                sessions=0,
                reason=reason
            )
            logger.info(f"No champion existed. {reason}")
            return challenger_params, reason

        # Look up challenger's live trading history
        challenger_live = None
        if feedback_tracker:
            top = feedback_tracker.get_top_params(n=50)
            for entry in top:
                ep = entry['params']
                cp = challenger_params
                if (ep.get('oversold') == cp.get('oversold') and
                        ep.get('overbought') == cp.get('overbought') and
                        abs(ep.get('position_size', 0) - cp.get('position_size', 0)) < 0.01 and
                        abs(ep.get('stop_loss_pct', 0) - cp.get('stop_loss_pct', 0)) < 0.01 and
                        abs(ep.get('take_profit_pct', 0) - cp.get('take_profit_pct', 0)) < 0.01):
                    challenger_live = entry
                    break

        challenger_sessions = challenger_live['times_used'] if challenger_live else 0
        challenger_live_pnl = challenger_live['avg_pnl'] if challenger_live else 0.0
        challenger_blended = (challenger_backtest_score * 0.4 +
                              (challenger_live_pnl / 100.0) * 0.6) if challenger_sessions >= MIN_CHALLENGE_SESSIONS else challenger_backtest_score * 0.4

        # Case 2: Challenger has insufficient live data
        if challenger_sessions < MIN_CHALLENGE_SESSIONS:
            reason = (f"Challenger has only {challenger_sessions}/{MIN_CHALLENGE_SESSIONS} "
                      f"live sessions — keeping champion")
            logger.info(reason)
            return champion['params'], reason

        # Champion blended score
        champ_live_pnl = champion.get('live_avg_pnl', 0.0)
        champ_backtest = champion.get('backtest_score', 0.0)
        champ_blended = champ_backtest * 0.4 + (champ_live_pnl / 100.0) * 0.6

        improvement = (challenger_blended - champ_blended) / max(abs(champ_blended), 0.001)

        logger.info(f"Champion blended score: {champ_blended:.4f} (live: {champ_live_pnl:.2f}% over {champion.get('sessions', 0)} sessions)")
        logger.info(f"Challenger blended score: {challenger_blended:.4f} (live: {challenger_live_pnl:.2f}% over {challenger_sessions} sessions)")
        logger.info(f"Improvement: {improvement*100:.1f}% (need {PROMOTION_THRESHOLD*100:.0f}% to promote)")

        # Case 3: Challenger beats champion by enough
        if improvement >= PROMOTION_THRESHOLD:
            reason = (f"Challenger promoted: {improvement*100:.1f}% better than champion "
                      f"(backtest {challenger_backtest_score:.4f} + live {challenger_live_pnl:.2f}% "
                      f"over {challenger_sessions} sessions)")
            self._save_champion(
                params=challenger_params,
                backtest_score=challenger_backtest_score,
                live_avg_pnl=challenger_live_pnl,
                sessions=challenger_sessions,
                reason=reason
            )
            logger.info(f"PROMOTION: {reason}")
            return challenger_params, reason

        # Case 4: Keep champion
        reason = (f"Champion retained: challenger only {improvement*100:.1f}% better "
                  f"(need {PROMOTION_THRESHOLD*100:.0f}%)")
        logger.info(reason)

        # Update champion's live stats if we have more data
        if champion.get('sessions', 0) < challenger_sessions:
            champ_params = champion['params']
            champ_live_entry = None
            if feedback_tracker:
                for entry in (feedback_tracker.get_top_params(n=50) or []):
                    ep = entry['params']
                    if (ep.get('oversold') == champ_params.get('oversold') and
                            ep.get('overbought') == champ_params.get('overbought') and
                            abs(ep.get('position_size', 0) - champ_params.get('position_size', 0)) < 0.01):
                        champ_live_entry = entry
                        break
            if champ_live_entry:
                self._save_champion(
                    params=champ_params,
                    backtest_score=champ_backtest,
                    live_avg_pnl=champ_live_entry['avg_pnl'],
                    sessions=champ_live_entry['times_used'],
                    reason=f"Stats refreshed (champion retained)"
                )

        return champion['params'], reason

    def status(self) -> str:
        """Human-readable champion status."""
        c = self.get_champion()
        if not c:
            return "No champion yet — will be set after first optimizer run."
        p = c['params']
        return (f"Champion: OS={p.get('oversold')} OB={p.get('overbought')} "
                f"size={p.get('position_size')} SL={p.get('stop_loss_pct')} "
                f"TP={p.get('take_profit_pct')} | "
                f"Backtest score: {c.get('backtest_score', 0):.4f} | "
                f"Live avg P&L: {c.get('live_avg_pnl', 0):.2f}% over {c.get('sessions', 0)} sessions | "
                f"Promoted: {c.get('promoted_at', 'N/A')[:10]}")
