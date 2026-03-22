# Circuit Breaker Integration Spec
Generated: 2026-03-22
Task: #1130 (Pre-flight for task 890: TradeSight Phase 1 Circuit Breaker)

## 1. INSERTION POINT

Method: scan_and_trade() in src/trading/paper_trader.py
Location: After self._check_stop_loss_take_profit(), before get_latest_tournament_winners()

Insert after _check_stop_loss_take_profit() call:

    if self._check_daily_loss_limit():
        self.logger.warning('[CircuitBreaker] Daily loss limit reached -- no new entries today.')
        return

## 2. RECOMMENDED daily_loss_limit VALUE

Portfolio context:
- Started: ~500 USD, current equity: ~350 USD (down 31%)
- RSI Mean Reversion: 7 trades, 28.6% win rate, total P&L -166.44
- Worst per-day scenario: 2 positions x 5% SL x 175 = ~17.50

Recommendation:
- Fixed dollar: 15.00 USD (conservative), 20.00 USD (standard)
- Percentage: ~4-5% of current equity (~350)
- Add to config: 'daily_loss_limit': self.active_params.get('daily_loss_limit', 15.0)

## 3. POSITION FREEZE LOGIC

(a) Flag: self._daily_loss_limit_reached = False  (in __init__)
    - Instance variable, resets on session restart. No DB/file needed.

(b) Short-circuit these methods:
    - scan_and_trade(): return early after SL/TP check (see insertion point)
    - execute_signal(): guard at top: if self._daily_loss_limit_reached: return False
    - DO NOT block _check_stop_loss_take_profit() -- existing positions need protection

(c) Reset: Session-based (instance var clears on restart).
    For long sessions, check midnight ET in _check_daily_loss_limit():
        today = datetime.now().date()
        if hasattr(self, '_cb_date') and self._cb_date != today:
            self._daily_loss_limit_reached = False
            self._cb_date = today

(d) SL/TP while frozen: YES -- continue running. Freeze only blocks NEW entries.

## IMPLEMENTATION SKETCH: _check_daily_loss_limit()

    def _check_daily_loss_limit(self) -> bool:
        if self._daily_loss_limit_reached:
            return True
        limit = self.config.get('daily_loss_limit', 15.0)
        try:
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            db_path = self.position_manager.data_dir / 'positions.db'
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT SUM(realized_pnl) FROM positions WHERE status='closed' AND exit_time >= ?",
                    (today_start,)
                ).fetchone()
            daily_pnl = float(row[0]) if row and row[0] else 0.0
            if daily_pnl <= -limit:
                self._daily_loss_limit_reached = True
                self.logger.warning(f'[CircuitBreaker] TRIGGERED: daily_pnl={daily_pnl:.2f} <= -{limit:.2f}')
                return True
        except Exception as e:
            self.logger.error(f'[CircuitBreaker] Check failed: {e}')
        return False

Feeds into: [BUILDER] task #890 -- TradeSight Phase 1: Circuit Breaker
