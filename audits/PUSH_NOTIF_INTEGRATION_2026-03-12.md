# TradeSight Phase 5.1 — Push Notification Integration Points
**Audit by Lucky — 2026-03-12**

---

## Summary

No notification infrastructure exists yet. Zero references to push delivery, WebSocket events, or alert dispatching anywhere in the codebase. All results go to log files or report `.txt` files that nobody reads unless they actively check.

The delivery model for Phase 5.1: **write to `/tmp/tradesight-alert.json` → Lucky heartbeat polls it → forwards to WhatsApp.**

---

## Integration Points (Ranked by Priority)

### 1. Overnight Session Complete
**File:** `src/automation/strategy_automation.py`
**Method:** `run_overnight_session()` — line ~310
**Hook location:** After `self.save_daily_report(report)`, before `return results`

```python
# HOOK: fire notification after save_daily_report()
if not results.get('error'):
    notify(
        event='overnight_complete',
        winner=results['winner'],
        score=results['winner_avg_score'],
        report_path=str(report_file)
    )
else:
    notify(event='overnight_failed', error=results['error'])
```

**What to send:** Winner strategy name + score + delta vs. prior day's winner (if available). This is the highest-value alert — fires once per night and tells Ray what the AI found.

---

### 2. Position Closed — Significant P&L
**File:** `src/trading/position_manager.py`
**Method:** `close_position()` — line ~174
**Hook location:** After `conn.commit()`, inside the success path

```python
# HOOK: notify on position close with meaningful P&L
if abs(realized_pnl) > NOTIFY_THRESHOLD:  # e.g. $50
    notify(
        event='position_closed',
        symbol=symbol,
        strategy=strategy,
        pnl=realized_pnl,
        side=side
    )
```

**What to send:** Symbol, strategy, P&L ($). Positive = win, negative = loss. Threshold should be configurable (suggest $50 paper trade default).

---

### 3. Position Opened
**File:** `src/trading/position_manager.py`
**Method:** `open_position()` — line ~139
**Hook location:** After `conn.commit()`, inside the success path

```python
# HOOK: notify on new position entry
notify(
    event='position_opened',
    symbol=symbol,
    strategy=strategy,
    side=side,
    quantity=quantity,
    entry_price=entry_price
)
```

**What to send:** Optional (can get noisy). Suggest making this opt-in via config flag `NOTIFY_ON_OPEN=false` default.

---

### 4. High-Confidence Signal Detected
**File:** `src/scanners/stock_opportunities.py`
**Method:** `rank_opportunities()` — line ~163
**Hook location:** After sorting, before return — filter for `overall_score >= 80`

```python
# HOOK: alert on high-confidence opportunities
high_conf = [s for s in scores if s.overall_score >= 80]
if high_conf:
    notify(
        event='high_conf_signal',
        opportunities=[(s.symbol, s.overall_score, s.active_signals) for s in high_conf[:3]]
    )
return scores
```

**What to send:** Top 3 symbols with scores + active signals (e.g. `AAPL 84.2 [rsi_oversold, unusual_volume]`). Only fires when scanner runs and score threshold is crossed — not spammy.

---

### 5. Nightly Shell Script Completion
**File:** `scripts/nightly_strategy_improvement.sh`
**Hook location:** After the Python script exits 0 (line ~68, after the `tee` pipe), OR on failure (line ~82)

```bash
# HOOK: write alert file after cron completes
ALERT_FILE="/tmp/tradesight-alert.json"
if [ ${PIPESTATUS[0]} -eq 0 ]; then
    echo "{\"event\":\"cron_complete\",\"date\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"status\":\"ok\"}" > "$ALERT_FILE"
else
    echo "{\"event\":\"cron_failed\",\"date\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"status\":\"error\"}" > "$ALERT_FILE"
fi
```

**Note:** This is a fallback safety net — the Python-level hook (#1) is preferred because it carries more data. The shell hook catches cases where Python crashes before writing anything.

---

## Proposed Notifier Module

**New file:** `src/alerts/notifier.py`

Responsibilities:
- Single `notify(event, **kwargs)` function — all callers use this, no direct file I/O
- Writes/appends to `/tmp/tradesight-alert.json` (JSON Lines format — one object per line)
- Lucky's heartbeat reads this file, clears it after processing, forwards to WhatsApp
- Config flag `NOTIFICATIONS_ENABLED=true` in `src/config.py` to kill all alerts without code changes

Schema per alert line:
```json
{
  "ts": "2026-03-12T06:10:00Z",
  "event": "overnight_complete",
  "winner": "RSI_Momentum_v3",
  "score": 0.847,
  "delta": "+0.031 vs yesterday"
}
```

---

## Files That Need No Changes

- `web/dashboard.py` — no server-push needed for Phase 5.1 (heartbeat delivery covers it)
- `web/templates/unified_dashboard.html` — browser notifications are Phase 5.2+ scope
- `src/strategy_lab/tournament.py` — fires within `run_overnight_session()`, already captured by hook #1
- `src/data/alpaca_client.py` — data fetcher, not a trigger point

---

## Estimated Build Order

1. Create `src/alerts/__init__.py` + `src/alerts/notifier.py` — 30 min
2. Wire hook #1 (overnight complete) — 15 min, highest value
3. Wire hook #4 (high-conf signals) — 15 min
4. Wire hooks #2/#3 (position open/close) — 20 min
5. Update Lucky heartbeat to read `/tmp/tradesight-alert.json` — 20 min
6. Test end-to-end (manual trigger → WhatsApp delivery) — 30 min

**Total: ~2.5 hrs.** NEXT_TASKS.md estimated 4-6 hrs; this is achievable on the low end.

