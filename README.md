# Market Webhook Monitor

A small Flask dashboard that monitors symbols from biquote and sends HTTP POST requests when a price crosses a target.

## Features
- Symbol catalogue from `https://biquote.io/api/symbols`
- Batched quote polling from `https://biquote.io/api/latest`
- Forex, metals, crypto, stocks and index CFDs, where biquote has quotes
- Above/below crossing alerts
- Bid, ask or mid price selection
- Custom HTTPS webhook destination and optional `X-Webhook-Secret`
- Duplicate prevention and automatic re-arming after price returns across the target
- Health endpoint for UptimeRobot

## Important limitations
- This is a hobby-grade monitor, not an execution or guaranteed trading-alert system.
- Free Render services can restart at any time. UptimeRobot pings can reduce idle spin-down, but cannot prevent restarts or guarantee continuous delivery.
- Render's filesystem is ephemeral. Set `DATABASE_URL` to a persistent external PostgreSQL database before relying on saved alerts. Do not use SQLite for deployed long-term storage.
- The feed is a third-party MT5-derived quote feed. It may differ from your broker's prices, spreads, symbol suffixes, and execution prices.
- The initial monitor uses batched REST polling every 2 seconds. It can miss a brief cross between polls, and delivery depends on feed availability and your webhook endpoint.
- The monitor sends one event per crossing. If webhook delivery fails, it logs the error; it does not currently provide a durable retry queue.

## Local run
1. Python 3.11+ recommended.
2. `pip install -r requirements.txt`
3. Set environment variables:
   - `ADMIN_PASSWORD` (required)
   - `SECRET_KEY` (strong random value)
   - optionally `DATABASE_URL`
4. Run `python app.py`
5. Open `http://127.0.0.1:5000`

## Render deployment
1. Push this folder to a GitHub repository.
2. In Render, create a new Blueprint and select the repository, or create a Web Service manually.
3. Use:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --workers 1 --threads 4 --timeout 120`
4. Set `ADMIN_PASSWORD` and `SECRET_KEY`.
5. Set `DATABASE_URL` to an external PostgreSQL connection string (recommended). Render's free web service filesystem is temporary.
6. Deploy and open `/health`.
7. Add `https://YOUR-SERVICE.onrender.com/health` to UptimeRobot as an HTTP(s) monitor with a 5-minute interval.

## Webhook payload example
```json
{
  "event": "price_target_hit",
  "alert_id": 12,
  "symbol": "XAUUSD",
  "direction": "above",
  "target": 2350.5,
  "price": 2350.62,
  "price_field": "mid",
  "bid": 2350.52,
  "ask": 2350.72,
  "mid": 2350.62,
  "feed": "biquote",
  "source": "MetaTrader 5 (Broker 1)",
  "market_state": "open",
  "timestamp": "2026-11-24T09:30:00Z",
  "triggered_at": "2026-11-24T09:30:01+00:00"
}
```

## UptimeRobot
Monitor type: HTTP(s)
URL: `https://YOUR-SERVICE.onrender.com/health`
Interval: 5 minutes (or the shortest interval available on your plan).

A successful health response looks like:
```json
{"ok":true,"service":"market-webhook-monitor","poll_seconds":2}
```
