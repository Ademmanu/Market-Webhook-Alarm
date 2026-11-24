# Market Webhook Monitor v2

A multi-user Flask dashboard for price-crossing alerts using biquote's public market-data API.

## v2 features
- User-chosen usernames (3–32 characters); individual password per account.
- `ADMIN_PASSWORD` is the shared invitation/account-creation password, not a dashboard login password.
- 24-hour persistent login session, secure HttpOnly/SameSite cookies, proxy-aware HTTPS handling, CSRF protection, and five-failure/five-minute per-account lockout.
- Existing database schema is not automatically migrated or altered at startup. The application creates missing tables only.
- Optional alert label, edit, test webhook, once/repeat, repeat cooldown, UTC expiry, manual re-arm, pause all/resume all.
- Per-user saved webhook destinations and per-user delivery history.
- Durable webhook queue: initial attempt plus three retries, five seconds apart; queued jobs are stored in the database.
- Dashboard counters and live quotes refreshed every 10 seconds; backend checks quotes every `POLL_SECONDS` (default 2 seconds).
- Cached symbol catalogue and type/description search picker. The picker uses `/api/symbols?quotedWithinDays=7` and filters by both symbol and description.
- Correctly parses biquote `/api/latest` response as a symbol-keyed JSON object (for example `{ "EURUSD": { ... } }`).
- Feed outage backoff, health/database check, and stale-quote filtering.

## Important setup notes
1. Back up your existing database before deploying this version.
2. Deploy to the same Render service and use the same `DATABASE_URL`. This build does not run database migrations or alter existing tables. It assumes the database already has the v2 schema.
3. Ensure `ADMIN_PASSWORD` is set. It is the account invitation password. Users create their own personal dashboard passwords.
4. Set a strong `SECRET_KEY` (at least 32 random characters).
5. Use persistent PostgreSQL for reliable alerts and webhook retry queue. SQLite on Render's ephemeral filesystem can lose data on restart/redeploy. If your old deployment used SQLite, it will not be durable unless you mount persistent storage or migrate the database.
6. Keep exactly one Gunicorn worker (`--workers 1`). The price monitor and delivery worker run as in-process background threads; multiple web workers would create duplicate monitors. Render can restart free instances at any time, so delivery continuity is not guaranteed during downtime.
7. UptimeRobot can ping `https://YOUR-APP.onrender.com/health` every 5 minutes to reduce idle spin-down, but it cannot prevent restarts or guarantee uptime.

## Render settings
Build command:
```
pip install -r requirements.txt
```
Start command:
```
gunicorn app:app --workers 1 --threads 4 --timeout 120
```
Environment:
- `ADMIN_PASSWORD`: strong shared invitation password
- `SECRET_KEY`: strong random secret
- `DATABASE_URL`: persistent PostgreSQL connection string
- `POLL_SECONDS`: optional, default 2
- `QUOTE_MAX_AGE_SECONDS`: optional, default 30
- `FEED_BASE`: optional, default `https://biquote.io`

## Account flow
Open `/signup`, enter the shared `ADMIN_PASSWORD`, choose your own username, and create a password of at least 6 characters. Confirm the password. You can show or hide the admin and personal passwords while typing. Sign in at `/login` using your chosen username and password.

Accounts only see their own alerts, saved webhooks, and delivery logs. Existing alerts already assigned to an account remain with that account.

## Symbol picker
The instrument picker downloads the recent symbol catalogue from biquote and filters locally by ticker, description, and market type. It deliberately uses `quotedWithinDays=7`, not `liveOnly=true`, so the list does not disappear when markets are closed. A symbol must still have a fresh quote when an alert is monitored.

## Alert semantics
- **Once:** fires once and disables itself.
- **Repeat:** requires a cooldown from 1 to 10,080 minutes. After firing, price must return to the non-trigger side and the cooldown must have elapsed before the alert rearms.
- **No cooldown:** use Once mode; repeat mode is rejected without a cooldown.
- **Expiry:** `datetime-local` input is interpreted as UTC. Enter the UTC date/time, not your local time.
- **Resume/re-arm:** clears the previous quote baseline. The first fresh quote is baseline only, preventing immediate false firing from stale prices.

## Webhook delivery
The webhook must use HTTPS and resolve to a public IP address. This blocks localhost/private-network targets. Each alert's optional secret is sent in `X-Webhook-Secret`.

A price trigger creates a durable delivery row. The worker tries once immediately, then retries up to three additional times at 5-second intervals. HTTP 2xx means sent; other responses are retried. The delivery log shows the latest status, attempts, response code, and error/response text.

Example alert payload:
```json
{
  "event": "price_target_hit",
  "alert_id": 12,
  "label": "Gold breakout",
  "symbol": "XAUUSD",
  "direction": "above",
  "target": 4390.5,
  "price": 4390.62,
  "price_field": "mid",
  "bid": 4390.52,
  "ask": 4390.72,
  "mid": 4390.62,
  "feed": "biquote",
  "source": "MetaTrader 5 (Broker 1)",
  "market_state": "open",
  "timestamp": "2026-11-24T09:30:00Z",
  "triggered_at": "2026-11-24T09:30:01+00:00"
}
```

Test webhook payload uses `event: webhook_test` and `test: true`.

## Health / status
- `/health` is public and checks database connectivity. Use it with UptimeRobot.
- `/api/symbols` is public and returns the cached symbol catalogue.
- `/api/dashboard` and `/api/status` require login.

## Limitations
This is a hobby-grade alert system, not an execution engine or guaranteed trading-alert service. The free biquote feed is MT5-derived and can differ from your broker's symbol naming, spread, price, and quote timing. A 2-second REST polling interval can miss a very brief cross between polls. For more precise event capture, upgrade the monitor to the feed's SignalR streaming endpoint and run it on an always-on worker. Render free instances may restart or be unavailable, and UptimeRobot cannot guarantee continuous monitoring. Use persistent PostgreSQL for the queue and alert records.
