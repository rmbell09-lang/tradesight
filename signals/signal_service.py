#!/usr/bin/env python3
"""TradeSight Signal Service — generates daily trade signals from RSI Mean Reversion strategy.
Can output to: email (SMTP), webhook (POST), or file (JSON).
Designed to run via launchd cron, checks market data, generates signal, dispatches."""

import json, os, sys, urllib.request, urllib.parse, subprocess, smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta

ALPACA_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"

def get_keychain(service, account):
    try:
        r = subprocess.run(["security","find-generic-password","-s",service,"-a",account,"-w"],
                          capture_output=True, text=True, check=True)
        return r.stdout.strip()
    except: return None

def alpaca_api(path, api_key, api_secret, base=DATA_BASE):
    url = f"{base}{path}"
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    })
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def generate_signals(symbols=None):
    """Generate RSI-based trade signals for given symbols."""
    if symbols is None:
        symbols = ["SPY", "AAPL", "MSFT", "AMZN", "GOOGL"]

    api_key = get_keychain("Alpaca-API-Key", "luckyai")
    api_secret = get_keychain("Alpaca-API-Secret", "luckyai")
    if not api_key or not api_secret:
        return {"error": "Alpaca keys not in Keychain"}

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    signals = []
    for sym in symbols:
        try:
            bars = alpaca_api(
                f"/v2/stocks/{sym}/bars?timeframe=1Day&start={start}&end={end}&limit=30",
                api_key, api_secret
            )
            closes = [b["c"] for b in bars.get("bars", [])]
            if len(closes) < 15:
                continue

            rsi = calculate_rsi(closes)
            price = closes[-1]

            signal = {
                "symbol": sym,
                "price": price,
                "rsi": round(rsi, 2) if rsi else None,
                "signal": "NONE",
                "timestamp": datetime.now().isoformat()
            }

            if rsi and rsi < 30:
                signal["signal"] = "BUY"
                signal["reason"] = f"RSI oversold ({rsi:.1f})"
            elif rsi and rsi > 70:
                signal["signal"] = "SELL"
                signal["reason"] = f"RSI overbought ({rsi:.1f})"
            else:
                signal["reason"] = f"RSI neutral ({rsi:.1f})" if rsi else "Insufficient data"

            signals.append(signal)
        except Exception as e:
            signals.append({"symbol": sym, "error": str(e)})

    return {
        "generated_at": datetime.now().isoformat(),
        "strategy": "RSI Mean Reversion (14-period)",
        "signals": signals,
        "active_signals": [s for s in signals if s.get("signal") not in ("NONE", None)]
    }

def format_email(data):
    """Format signals as plain text email."""
    lines = [
        f"TradeSight Daily Signals — {data[generated_at][:10]}",
        f"Strategy: {data[strategy]}",
        "",
    ]
    for s in data["signals"]:
        if "error" in s:
            lines.append(f"  {s[symbol]}: ERROR — {s[error]}")
        else:
            emoji = {"BUY": "🟢", "SELL": "🔴", "NONE": "⚪"}.get(s["signal"], "⚪")
            lines.append(f"  {emoji} {s[symbol]}: ${s[price]:.2f} | RSI {s[rsi]} | {s[signal]} — {s[reason]}")

    active = data.get("active_signals", [])
    if active:
        lines.append(f"\n⚡ {len(active)} active signal(s) today")
    else:
        lines.append(f"\nNo actionable signals today. Patience is a strategy.")

    return "\n".join(lines)

def dispatch_webhook(data, url):
    """Send signals to a webhook URL."""
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    with urllib.request.urlopen(req) as r:
        return r.status

def main():
    data = generate_signals()

    if "error" in data:
        print(f"Error: {data[error]}")
        sys.exit(1)

    # Always save to file
    out_dir = os.path.join(os.path.dirname(__file__), "output")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, f"signals_{datetime.now().strftime(%Y%m%d)}.json")
    with open(out_file, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved to {out_file}")

    # Print formatted
    print()
    print(format_email(data))

    # Dispatch to webhook if configured
    webhook = os.environ.get("SIGNAL_WEBHOOK_URL")
    if webhook:
        try:
            status = dispatch_webhook(data, webhook)
            print(f"\nWebhook dispatched: {status}")
        except Exception as e:
            print(f"\nWebhook failed: {e}")

if __name__ == "__main__":
    main()
