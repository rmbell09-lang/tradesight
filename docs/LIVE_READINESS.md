# Live Trading Readiness

TradeSight 1.2 is a paper-only release. Live execution is not compiled into it.
The dashboard's Live Readiness tab measures evidence; it cannot activate trading.

All mandatory gates must pass before a separate live-canary build is even reviewed:

1. Supported isolated Python runtime.
2. Paper-only release lock confirmed.
3. Current broker and local accounting reconcile with zero unexplained differences.
4. Thirty verified accounting sessions.
5. Legacy P&L preserved but excluded from trusted results.
6. Sixty forward-paper sessions.
7. One hundred broker-confirmed forward-paper round trips.
8. One frozen champion with automatic promotion disabled.
9. Out-of-sample and robustness qualification receipt passed.
10. Authenticated, CSRF-protected, audit-chained risk controls.
11. Tested outbound safety alerts.
12. Failure and restart drills passed.
13. The operator's explicit approval of the strategy and hard per-trade, daily, and total loss limits.

Passing all gates does not enable live trading. It only permits a separate review of a
small canary build. That build would use one frozen strategy, liquid stocks or ETFs,
regular market hours, no leverage, no shorting, no options, no automatic strategy
switching, and hard loss limits. Any accounting, safety, or execution discrepancy
must stop it automatically.

Scaling is never automatic. It requires a new review after 20–50 clean canary trades.
