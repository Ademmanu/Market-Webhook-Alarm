# Market Webhook Monitor v2

A small Flask dashboard that watches symbols from biquote and sends an HTTP POST when a price crosses a target.

## What's new in v2
**Alert options**
- Label, edit-in-place, and a **Test** button that sends a sample webhook
- **Fire once, then pause** or **repeat** (re-arm when price returns to the other side)
- **Cooldown** before re-arming, and an optional **expiry** (UTC)
- Manual **Re-arm**, **Pause all / Resume all**, JSON **Export / Import** (secrets are never exported)

**Delivery**
- Send as **generic JSON**, **Discord**, **Slack/Mattermost** or **Telegram** messages, with an optional message template (`{symbol} {price} {target} {direction} {bid} {ask} {mid} {label}`)
- Failed deliveries are **retried with exponential backoff** (default 5 attempts: 5s, 10s, 20s, 40s). A crossing waiting for delivery is stored in the database, so it survives restarts
- **Delivery log** on the dashboard (kept 30 days) with status codes and errors
- Optional secret is sent as `X-Webhook-Secret` **and** as an HMAC signature, `X-Signature-256`

**Dashboard**
- Live prices with distance to target, feed health (OK / idle / error), counts of active/fired/failed

**Security and reliability**
- CSRF protection, constant-time password check, login lockout (5 failures / 5 min), secure cookies
- Webhook URLs must be HTTPS and may not point at private/internal addresses; redirects are not followed
- Secret URL parts (Discord/Telegram tokens) are redacted from error messages and logs
- Fixed: resuming a long-paused alert could fire instantly from a stale price. It now re-baselines first
- Feed outages back off automatically; symbol catalogue is cached; database connections are health-checked
- **Existing v1 databases are upgraded automatically** on startup (new columns are added, no data lost)

The JSON payload keeps every v1 field, plus `label` and `attempt`, so existing receivers keep working.

## Environment variables
| Variable | Default | Purpose |
|---|---|---|
| `ADMIN_PASSWORD` | – | Dashboard password (required) |
| `SECRET_KEY` | – | Strong random value for sessions |
| `DATABASE_URL` | SQLite | Use external PostgreSQL when deployed |
| `POLL_SECONDS` | 2 | Feed polling interval (minimum 2) |
| `QUOTE_MAX_AGE_SECONDS` | 30 | Ignore quotes older than this |
| `WEBHOOK_MAX_ATTEMPTS` | 5 | Delivery attempts before giving up |
| `WEBHOOK_RETRY_BASE_SECONDS` | 5 | First retry delay (doubles, max 5 min) |
| `DELIVERY_LOG_DAYS` | 30 | How long delivery history is kept |
| `DEV_MODE` | off | Local testing only: allows `http://` and private hosts |
| `TRUST_PROXY` | 1 | Set 0 if not behind a reverse proxy |

## Verifying signatures on your receiver
```python
import hmac, hashlib
expected = "sha256=" + hmac.new(SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
ok = hmac.compare_digest(expected, request.headers["X-Signature-256"])
```

## Local run
1. Python 3.11+ and `pip install -r requirements.txt`
2. Set `ADMIN_PASSWORD` and `SECRET_KEY` (and `DEV_MODE=1` if testing against a local receiver)
3. `python app.py`, then open `http://127.0.0.1:5000`
4. Tests: `python -m unittest discover -s tests`

## Render deployment
1. Push to GitHub, create a Blueprint (or a Web Service) with build `pip install -r requirements.txt` and start `gunicorn app:app --workers 1 --threads 4 --timeout 120`
2. Set `ADMIN_PASSWORD`, `SECRET_KEY` and an external PostgreSQL `DATABASE_URL`
3. Deploy, open `/health`, and add it to UptimeRobot (HTTP(s), 5 minutes)

Keep `--workers 1`: the price monitor runs as a background thread inside the web process.

## Limitations
- Hobby-grade, not an execution or guaranteed-delivery system. Free Render services can restart or sleep.
- Third-party MT5-derived quotes can differ from your broker. 2-second polling can miss a brief spike between polls.
- Webhooks are sent one after another; many slow endpoints can delay the next price check.
- Always use an external database on Render: its filesystem is ephemeral.

## Ideas for next steps
Percent-move and range alerts, quiet hours, email/Telegram-bot delivery, per-alert history charts, multiple users or API tokens, a streaming feed instead of polling, Prometheus metrics.
