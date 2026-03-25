# TradeSight WebSocket Code Review (Task 1191)
Generated: 2026-03-23 — Prereq for Task 1008 (alpaca_trade_monitor.py)

## Summary
paper_trader.py is **REST-only** — no WebSocket code exists. Task 1008 is a standalone script, correct.

## Area Review

| Area | Status | Notes |
|------|--------|-------|
| WebSocket connection | ❌ FAIL | None exists in codebase |
| REST auth headers | ✅ PASS | APCA-API-KEY-ID + APCA-API-SECRET-KEY correct |
| Reconnect/backoff | ❌ FAIL | REST-only (3 retry, 5s/10s). No WS reconnect. |
| Fill event parsing | ❌ FAIL | Polling-only. No trade_updates, no fill/partial_fill handler. |
| Integration point | ✅ PASS | Separate standalone script design is correct |

## Top 2 Risks for Task 1008 Builder
1. **WebSocket Connection Handling** — builder must implement connect + reconnect loop with exponential backoff. No existing pattern to copy from.
2. **Fill Event Parsing** — must handle both  and  correctly. Accumulate partial fills before posting to MC.

## Alpaca Paper WS Auth Sequence
```json
// Step 1: After connect
{action: auth, key: APCA_API_KEY_ID, secret: APCA_API_SECRET_KEY}

// Step 2: Subscribe
{action: listen, data: {streams: [trade_updates]}}

// Fill event shape
{stream: trade_updates, data: {event: fill, order: {symbol: AAPL, filled_qty: 1, filled_avg_price: 150.00, ...}}}
// partial_fill uses event=partial_fill
```

## Builder Notes for Task 1008
- Endpoint: wss://paper-api.alpaca.markets/stream
- Auth: same keys as REST (ALPACA_API_KEY + ALPACA_SECRET_KEY env vars)
- Post fills to: MC activity endpoint (localhost:3000/api/activity) + WhatsApp alert
- Handle both fill AND partial_fill events
- Implement exponential backoff reconnect (1s, 2s, 4s, 8s... cap at 60s)
