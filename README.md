# Market Webhook Monitor V3

A Flask dashboard for independent market quotes and HTTP webhook alerts.

## V3 features
- Market symbol search from the biquote catalogue.
- Alert conditions: `Crosses`, `Closes Above`, `Closes Below`.
- `Crosses` uses live mid-price ticks.
- Close conditions use completed OHLC candles only (the still-open candle is ignored).
- Candle intervals: `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `1d`.
- Mid-price is automatic; no bid/ask selector.
- Optional label, edit, test webhook, once/repeat, cooldown and UTC expiry.
- Per-user saved webhook destinations.
- Three-dot alert action menus and background action toast notifications.
- Delivery queue with initial attempt plus up to three retries, five seconds apart.
- PostgreSQL-backed accounts, alerts and delivery logs.
- No database reset code and no automatic schema migration.

## Webhook payload
Alert deliveries include a plain `message` field, for example:

```json
{
  "message": "EURUSD crossed 1.22",
  "event": "price_target_hit",
  "symbol": "EURUSD",
  "condition": "crossed",
  "target": 1.22,
  "price": 1.2201,
  "price_field": "mid",
  "closed_candle": false,
  "candle_interval": null,
  "feed": "biquote",
  "triggered_at": "2026-11-24T12:00:00+00:00"
}
```

Candle close messages use `EURUSD closed above 1.22` or `EURUSD closed below 1.22`.

## Deployment
Use the existing Render web service and existing PostgreSQL `DATABASE_URL`. Keep:
- `ADMIN_PASSWORD` — account registration/invite password
- `SECRET_KEY` — Flask session signing key
- `DATABASE_URL` — PostgreSQL connection string
- `POLL_SECONDS` — default 2
- `QUOTE_MAX_AGE_SECONDS` — default 30

Render start command:

```bash
gunicorn app:app --workers 1 --threads 4 --timeout 120
```

Use one Gunicorn worker because the price monitor and delivery worker run as in-process background threads. UptimeRobot can ping `/health`, but it cannot guarantee a free Render instance never restarts.

## Database compatibility
V3 deliberately contains no reset code and no automatic schema migration. It reuses the existing V2 columns for condition state and candle interval, so it does not require new database columns. Deploy against the existing V2 PostgreSQL database; do not enable any database-reset environment variable.

## Important market-data limitations
- biquote provides indicative MT5-derived quotes and OHLC bars; these may differ from your broker.
- Close conditions depend on the provider's completed candle data. If a symbol has no OHLC data for a timeframe, that alert cannot trigger until data is available.
- Tick polling can miss a very brief crossing between checks.
- This is an alerting tool, not a trade execution or guaranteed-delivery system.

### Favorite assets
In the asset search list, tap the star beside an asset to add or remove it from Favorites. Favorites appear above the full asset catalogue when the search field is opened. Favorites are stored locally in that browser/device.
