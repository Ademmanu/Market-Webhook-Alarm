import os, time, threading, logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("market-monitor")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-before-deploying")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///alerts.db").replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

FEED_BASE = "https://biquote.io"
POLL_SECONDS = max(2, int(os.environ.get("POLL_SECONDS", "2")))
QUOTE_MAX_AGE = int(os.environ.get("QUOTE_MAX_AGE_SECONDS", "30"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(40), nullable=False, index=True)
    direction = db.Column(db.String(10), nullable=False)  # above / below
    target = db.Column(db.Float, nullable=False)
    price_field = db.Column(db.String(10), nullable=False, default="mid")  # bid / ask / mid
    webhook_url = db.Column(db.Text, nullable=False)
    webhook_secret = db.Column(db.Text, nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    armed = db.Column(db.Boolean, nullable=False, default=True)
    last_price = db.Column(db.Float, nullable=True)
    last_fired_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

def logged_in():
    return session.get("authenticated") is True

def login_required():
    if not logged_in():
        return redirect(url_for("login"))
    return None

@app.before_request
def ensure_tables():
    # Safe for this small single-instance service. For production migrations, use Alembic.
    db.create_all()

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if not ADMIN_PASSWORD:
            flash("Set ADMIN_PASSWORD in Render environment variables first.", "error")
        elif request.form.get("password", "") == ADMIN_PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("index"))
        else:
            flash("Incorrect password.", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
def index():
    guard = login_required()
    if guard: return guard
    alerts = Alert.query.order_by(Alert.id.desc()).all()
    return render_template("index.html", alerts=alerts, poll_seconds=POLL_SECONDS)

@app.route("/alerts", methods=["POST"])
def create_alert():
    guard = login_required()
    if guard: return guard
    try:
        symbol = request.form["symbol"].strip().upper()
        direction = request.form["direction"]
        target = float(request.form["target"])
        price_field = request.form["price_field"]
        webhook_url = request.form["webhook_url"].strip()
        webhook_secret = request.form.get("webhook_secret", "").strip() or None
        if direction not in ("above", "below"): raise ValueError("Invalid direction")
        if price_field not in ("bid", "ask", "mid"): raise ValueError("Invalid price field")
        parsed = urlparse(webhook_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Webhook URL must be a valid HTTPS URL.")
        if not symbol or len(symbol) > 40: raise ValueError("Invalid symbol.")
        alert = Alert(symbol=symbol, direction=direction, target=target,
                      price_field=price_field, webhook_url=webhook_url,
                      webhook_secret=webhook_secret)
        db.session.add(alert)
        db.session.commit()
        flash("Alert created.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Could not create alert: {exc}", "error")
    return redirect(url_for("index"))

@app.post("/alerts/<int:alert_id>/toggle")
def toggle_alert(alert_id):
    guard = login_required()
    if guard: return guard
    alert = db.get_or_404(Alert, alert_id)
    alert.enabled = not alert.enabled
    if alert.enabled:
        alert.armed = True
    db.session.commit()
    return redirect(url_for("index"))

@app.post("/alerts/<int:alert_id>/delete")
def delete_alert(alert_id):
    guard = login_required()
    if guard: return guard
    alert = db.get_or_404(Alert, alert_id)
    db.session.delete(alert)
    db.session.commit()
    flash("Alert deleted.", "success")
    return redirect(url_for("index"))

@app.get("/api/symbols")
def symbols():
    try:
        r = requests.get(FEED_BASE + "/api/symbols",
                         params={"quotedWithinDays": 7}, timeout=12)
        r.raise_for_status()
        data = r.json()
        return data
    except Exception as exc:
        return {"error": str(exc)}, 502

@app.get("/health")
def health():
    return {"ok": True, "service": "market-webhook-monitor",
            "utc": datetime.now(timezone.utc).isoformat(),
            "poll_seconds": POLL_SECONDS}, 200

def post_webhook(alert, tick, price):
    payload = {
        "event": "price_target_hit",
        "alert_id": alert.id,
        "symbol": alert.symbol,
        "direction": alert.direction,
        "target": alert.target,
        "price": price,
        "price_field": alert.price_field,
        "bid": tick.get("bid"),
        "ask": tick.get("ask"),
        "mid": tick.get("mid"),
        "feed": "biquote",
        "source": tick.get("source"),
        "market_state": tick.get("marketState"),
        "timestamp": tick.get("timestamp"),
        "triggered_at": datetime.now(timezone.utc).isoformat()
    }
    headers = {"Content-Type": "application/json", "User-Agent": "MarketWebhookMonitor/1.0"}
    if alert.webhook_secret:
        headers["X-Webhook-Secret"] = alert.webhook_secret
    response = requests.post(alert.webhook_url, json=payload, headers=headers, timeout=10)
    response.raise_for_status()
    log.info("Webhook delivered for alert %s (%s)", alert.id, alert.symbol)

def monitor_loop():
    log.info("Price monitor started; polling every %ss", POLL_SECONDS)
    while True:
        try:
            with app.app_context():
                alerts = Alert.query.filter_by(enabled=True).all()
                symbols_to_watch = sorted(set(a.symbol for a in alerts))
                if not symbols_to_watch:
                    time.sleep(POLL_SECONDS)
                    continue
                params = [("symbols", s) for s in symbols_to_watch]
                response = requests.get(FEED_BASE + "/api/latest", params=params, timeout=12)
                response.raise_for_status()
                body = response.json()
                # API may return a list or a wrapper object; accept both.
                ticks = body if isinstance(body, list) else body.get("items", body.get("ticks", []))
                tick_map = {str(t.get("symbol", "")).upper(): t for t in ticks if t.get("symbol")}
                for alert in alerts:
                    tick = tick_map.get(alert.symbol)
                    if not tick:
                        continue
                    if tick.get("stale") is True:
                        continue
                    age = tick.get("quoteAgeSeconds")
                    if age is not None and float(age) > QUOTE_MAX_AGE:
                        continue
                    price = tick.get(alert.price_field)
                    if price is None:
                        price = tick.get("mid")
                    try: price = float(price)
                    except (TypeError, ValueError): continue

                    hit = (price >= alert.target) if alert.direction == "above" else (price <= alert.target)
                    reset_side = (price < alert.target) if alert.direction == "above" else (price > alert.target)

                    # First observation establishes state; it does not fire immediately
                    # just because the price is already beyond the target.
                    if alert.last_price is None:
                        alert.last_price = price
                        if reset_side:
                            alert.armed = True
                        db.session.commit()
                        continue

                    if not alert.armed and reset_side:
                        alert.armed = True

                    crossed = ((alert.last_price < alert.target <= price) if alert.direction == "above"
                               else (alert.last_price > alert.target >= price))
                    update_last_price = True
                    if alert.armed and crossed:
                        try:
                            post_webhook(alert, tick, price)
                            alert.armed = False
                            alert.last_fired_at = datetime.now(timezone.utc)
                        except Exception:
                            # Keep the previous price so the crossing remains detectable
                            # and the delivery is retried on the next polling cycle.
                            update_last_price = False
                            log.exception("Webhook failed for alert %s; retrying while price remains across target", alert.id)
                    if update_last_price:
                        alert.last_price = price
                    db.session.commit()
        except Exception:
            log.exception("Monitor cycle failed")
        time.sleep(POLL_SECONDS)

@app.get("/api/status")
def api_status():
    return {"ok": True, "active_alerts": Alert.query.filter_by(enabled=True).count(),
            "feed": FEED_BASE, "poll_seconds": POLL_SECONDS}

def start_monitor():
    if os.environ.get("DISABLE_MONITOR", "").lower() in ("1", "true", "yes"):
        return
    thread = threading.Thread(target=monitor_loop, daemon=True, name="price-monitor")
    thread.start()

with app.app_context():
    db.create_all()

start_monitor()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
