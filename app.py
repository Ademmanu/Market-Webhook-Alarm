import os, json, time, math, hmac, secrets, threading, logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import urlparse

import requests
from flask import (Flask, render_template, request, redirect, url_for, flash,
                   session, abort, Response)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text, or_
from werkzeug.middleware.proxy_fix import ProxyFix

import core

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("market-monitor")

def env_int(name, default, minimum=0):
    try: return max(minimum, int(os.environ.get(name, default)))
    except ValueError: return default

FEED_BASE = os.environ.get("FEED_BASE", "https://biquote.io").rstrip("/")
POLL_SECONDS = env_int("POLL_SECONDS", 2, 2)
QUOTE_MAX_AGE = env_int("QUOTE_MAX_AGE_SECONDS", 30)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MAX_ATTEMPTS = env_int("WEBHOOK_MAX_ATTEMPTS", 5, 1)
RETRY_BASE = env_int("WEBHOOK_RETRY_BASE_SECONDS", 5, 1)
LOG_DAYS = env_int("DELIVERY_LOG_DAYS", 30, 1)
DEV_MODE = os.environ.get("DEV_MODE", "").lower() in ("1", "true", "yes")  # allows http:// and private hosts

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-before-deploying")
if app.secret_key == "change-this-secret-before-deploying":
    log.warning("SECRET_KEY is the default value. Set a strong random SECRET_KEY before deploying.")
if env_int("TRUST_PROXY", 1):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # Render sits behind one proxy
app.config.update(
    SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:///alerts.db").replace("postgres://", "postgresql://", 1),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},  # survive dropped connections to free-tier Postgres
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")) and not DEV_MODE,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7))
db = SQLAlchemy(app)

def utcnow():  # naive UTC, matches what SQLite/Postgres hand back
    return datetime.now(timezone.utc).replace(tzinfo=None)

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
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    # --- added in v2 (see NEW_COLUMNS: existing databases are upgraded automatically) ---
    label = db.Column(db.String(80))
    repeat_mode = db.Column(db.String(10), default="rearm")   # rearm = fire on every crossing, once = then pause
    cooldown_seconds = db.Column(db.Integer, default=0)
    expires_at = db.Column(db.DateTime)                       # UTC
    payload_format = db.Column(db.String(10), default="json")  # json / discord / slack / telegram
    message_template = db.Column(db.Text)
    fire_count = db.Column(db.Integer, default=0)
    pending_json = db.Column(db.Text)                          # crossing waiting for delivery (survives restarts)
    retry_count = db.Column(db.Integer, default=0)
    next_retry_at = db.Column(db.DateTime)

    @property
    def state(self):
        if not self.enabled:
            if self.repeat_mode == "once" and self.fire_count: return "Done"
            return "Expired" if self.expires_at and self.expires_at <= utcnow() else "Paused"
        if self.pending_json: return f"Retrying ({self.retry_count})" if self.retry_count else "Sending"
        return "Armed" if self.armed else "Triggered"

    @property
    def state_class(self):
        return {"Paused": "off", "Expired": "off", "Done": "off", "Sending": "warn"}.get(self.state, "warn" if self.state.startswith("Retry") else "")

class Delivery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    alert_id = db.Column(db.Integer, index=True)
    symbol = db.Column(db.String(40))
    kind = db.Column(db.String(10), default="alert")  # alert / test
    ok = db.Column(db.Boolean, nullable=False, default=False)
    status_code = db.Column(db.Integer)
    error = db.Column(db.String(300))
    attempt = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

NEW_COLUMNS = {"label": "VARCHAR(80)", "repeat_mode": "VARCHAR(10) DEFAULT 'rearm'",
               "cooldown_seconds": "INTEGER DEFAULT 0", "expires_at": "TIMESTAMP",
               "payload_format": "VARCHAR(10) DEFAULT 'json'", "message_template": "TEXT",
               "fire_count": "INTEGER DEFAULT 0", "pending_json": "TEXT",
               "retry_count": "INTEGER DEFAULT 0", "next_retry_at": "TIMESTAMP"}

def migrate():
    """Create tables and add any v2 columns missing from an existing v1 database."""
    db.create_all()
    have = {c["name"] for c in inspect(db.engine).get_columns("alert")}
    with db.engine.begin() as conn:
        for name, ddl in NEW_COLUMNS.items():
            if name not in have:
                conn.execute(text(f'ALTER TABLE "alert" ADD COLUMN {name} {ddl}'))
                log.info("Migrated: added alert.%s", name)

# ---------------------------------------------------------------- security
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("authenticated") is not True:
            if request.path.startswith("/api/"): return {"error": "unauthorized"}, 401
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def csrf_token():
    if "_csrf" not in session: session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]
app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.filters["num"] = core.fmt_num
app.jinja_env.filters["dt"] = lambda v: v.strftime("%Y-%m-%d %H:%M:%S") if v else "—"

@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
            abort(400, "Missing or invalid CSRF token. Reload the page and try again.")

FAILED_LOGINS = defaultdict(list)  # ip -> timestamps of recent failures

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip, now = request.remote_addr or "?", time.time()
        FAILED_LOGINS[ip] = [t for t in FAILED_LOGINS[ip] if now - t < 300]
        if len(FAILED_LOGINS[ip]) >= 5:
            flash("Too many failed attempts. Try again in a few minutes.", "error")
        elif not ADMIN_PASSWORD:
            flash("Set ADMIN_PASSWORD in Render environment variables first.", "error")
        elif hmac.compare_digest(request.form.get("password", "").encode(), ADMIN_PASSWORD.encode()):
            FAILED_LOGINS.pop(ip, None)
            session.clear(); session["authenticated"] = True; session.permanent = True
            return redirect(url_for("index"))
        else:
            FAILED_LOGINS[ip].append(now)
            flash("Incorrect password.", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------------------------------------------------------------- shared state
STATE = {"last_ok": None, "error": None, "latency_ms": None, "idle": False, "latest": {}}
FEED = requests.Session()  # used by the monitor thread only

def feed_state():
    age = round(time.time() - STATE["last_ok"], 1) if STATE["last_ok"] else None
    if STATE["error"]: status = "error"
    elif STATE["idle"]: status = "idle"
    elif age is not None and age < max(30, POLL_SECONDS * 5): status = "ok"
    else: status = "starting"
    return {"status": status, "age": age, "error": STATE["error"], "latency_ms": STATE["latency_ms"]}

# ---------------------------------------------------------------- alert forms
def apply_form(alert, f, creating):
    """Validate form/JSON data `f` (anything with .get) and copy it onto `alert`."""
    symbol = str(f.get("symbol", "")).strip().upper()
    if not core.SYMBOL_RE.fullmatch(symbol): raise ValueError("Invalid symbol.")
    direction = f.get("direction")
    if direction not in ("above", "below"): raise ValueError("Invalid direction.")
    price_field = f.get("price_field", "mid")
    if price_field not in ("bid", "ask", "mid"): raise ValueError("Invalid price field.")
    target = float(f.get("target"))
    if not math.isfinite(target): raise ValueError("Invalid target price.")
    url = core.check_webhook_url(str(f.get("webhook_url", "")).strip(), DEV_MODE)
    fmt = f.get("payload_format") or "json"
    if fmt not in core.FORMATS: raise ValueError("Invalid payload format.")
    repeat = f.get("repeat_mode") or "rearm"
    if repeat not in ("rearm", "once"): raise ValueError("Invalid repeat mode.")
    cooldown = min(7 * 86400, max(0, int(float(f.get("cooldown_minutes") or 0) * 60)))
    raw_exp = str(f.get("expires_at") or "").strip()
    expires = datetime.strptime(raw_exp, "%Y-%m-%dT%H:%M") if raw_exp else None
    if expires and expires <= utcnow(): raise ValueError("Expiry must be in the future (UTC).")

    changed = creating or (alert.symbol, alert.direction, alert.target, alert.price_field) != (symbol, direction, target, price_field)
    alert.symbol, alert.direction, alert.target, alert.price_field = symbol, direction, target, price_field
    alert.webhook_url, alert.payload_format, alert.repeat_mode = url, fmt, repeat
    alert.cooldown_seconds, alert.expires_at = cooldown, expires
    alert.label = str(f.get("label") or "").strip()[:80] or None
    alert.message_template = str(f.get("message_template") or "").strip()[:500] or None
    secret = str(f.get("webhook_secret") or "").strip()
    if secret: alert.webhook_secret = secret
    elif f.get("clear_secret"): alert.webhook_secret = None
    if creating:
        alert.enabled, alert.fire_count = True, 0
    if changed:  # new target/symbol => start fresh so a stale baseline can't cause a false crossing
        alert.last_price, alert.armed = None, True
        alert.pending_json, alert.retry_count, alert.next_retry_at = None, 0, None

def alert_to_dict(a):  # same keys as the form, so exports can be re-imported (secrets are never exported)
    return {"symbol": a.symbol, "direction": a.direction, "target": a.target, "price_field": a.price_field,
            "label": a.label or "", "webhook_url": a.webhook_url, "payload_format": a.payload_format or "json",
            "message_template": a.message_template or "", "repeat_mode": a.repeat_mode or "rearm",
            "cooldown_minutes": (a.cooldown_seconds or 0) / 60,
            "expires_at": a.expires_at.strftime("%Y-%m-%dT%H:%M") if a.expires_at else ""}

def reset_state(a, enable):
    a.pending_json, a.retry_count, a.next_retry_at = None, 0, None
    if enable: a.enabled, a.armed, a.last_price = True, True, None  # re-baseline: no false cross after a pause

# ---------------------------------------------------------------- pages
@app.get("/")
@login_required
def index():
    alerts = Alert.query.order_by(Alert.id.desc()).all()
    deliveries = Delivery.query.order_by(Delivery.id.desc()).limit(25).all()
    stats = {"active": sum(1 for a in alerts if a.enabled), "fired": sum(a.fire_count or 0 for a in alerts),
             "failed": Delivery.query.filter(Delivery.ok.is_(False), Delivery.created_at >= utcnow() - timedelta(days=1)).count()}
    return render_template("index.html", alerts=alerts, deliveries=deliveries, stats=stats,
                           feed=feed_state(), poll_seconds=POLL_SECONDS)

@app.post("/alerts")
@login_required
def create_alert():
    try:
        alert = Alert()
        apply_form(alert, request.form, True)
        db.session.add(alert); db.session.commit()
        flash("Alert created.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Could not create alert: {exc}", "error")
    return redirect(url_for("index"))

@app.route("/alerts/<int:alert_id>/edit", methods=["GET", "POST"])
@login_required
def edit_alert(alert_id):
    alert = db.get_or_404(Alert, alert_id)
    if request.method == "POST":
        try:
            apply_form(alert, request.form, False)
            db.session.commit()
            flash("Alert updated.", "success")
            return redirect(url_for("index"))
        except Exception as exc:
            db.session.rollback()
            flash(f"Could not save: {exc}", "error")
    return render_template("edit.html", a=alert)

@app.post("/alerts/<int:alert_id>/toggle")
@login_required
def toggle_alert(alert_id):
    alert = db.get_or_404(Alert, alert_id)
    if alert.enabled:
        alert.enabled = False; reset_state(alert, False)
    elif alert.expires_at and alert.expires_at <= utcnow():
        flash("This alert has expired. Edit it to set a new expiry first.", "error")
        return redirect(url_for("index"))
    else:
        reset_state(alert, True)
    db.session.commit()
    return redirect(url_for("index"))

@app.post("/alerts/<int:alert_id>/rearm")
@login_required
def rearm_alert(alert_id):
    alert = db.get_or_404(Alert, alert_id)
    alert.armed = True; reset_state(alert, False)
    db.session.commit()
    flash("Alert re-armed.", "success")
    return redirect(url_for("index"))

@app.post("/alerts/<int:alert_id>/delete")
@login_required
def delete_alert(alert_id):
    db.session.delete(db.get_or_404(Alert, alert_id))
    db.session.commit()
    flash("Alert deleted.", "success")
    return redirect(url_for("index"))

@app.post("/alerts/<int:alert_id>/test")
@login_required
def test_alert(alert_id):
    alert = db.get_or_404(Alert, alert_id)
    tick = STATE["latest"].get(alert.symbol, {})
    event = make_event(alert, tick, tick.get(alert.price_field) or alert.target, "test")
    ok, code, err = send_webhook(alert, event)
    record_delivery(alert, ok, code, err, 1, "test"); db.session.commit()
    flash(f"Test webhook delivered (HTTP {code})." if ok else f"Test webhook failed: {err}", "success" if ok else "error")
    return redirect(url_for("index"))

@app.post("/alerts/bulk/<action>")
@login_required
def bulk(action):
    if action not in ("pause", "resume"): abort(404)
    now, n = utcnow(), 0
    for a in Alert.query.all():
        if action == "pause" and a.enabled:
            a.enabled = False; reset_state(a, False); n += 1
        elif action == "resume" and not a.enabled and not (a.expires_at and a.expires_at <= now):
            reset_state(a, True); n += 1
    db.session.commit()
    flash(f"{'Paused' if action == 'pause' else 'Resumed'} {n} alert(s).", "success")
    return redirect(url_for("index"))

@app.get("/export.json")
@login_required
def export_alerts():
    body = json.dumps({"alerts": [alert_to_dict(a) for a in Alert.query.order_by(Alert.id)]}, indent=2)
    return Response(body, mimetype="application/json", headers={"Content-Disposition": "attachment; filename=alerts.json"})

@app.post("/import")
@login_required
def import_alerts():
    try:
        data = json.load(request.files["file"].stream)
        items = data["alerts"] if isinstance(data, dict) else data
        if not isinstance(items, list) or len(items) > 100: raise ValueError("Expected a list of at most 100 alerts.")
        new = []
        for item in items:
            alert = Alert(); apply_form(alert, item, True); new.append(alert)  # validate everything first
        db.session.add_all(new); db.session.commit()
        flash(f"Imported {len(new)} alert(s). Webhook secrets are not exported, so re-add them where needed.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Import failed: {exc}", "error")
    return redirect(url_for("index"))

# ---------------------------------------------------------------- JSON endpoints
SYMBOLS_CACHE = {"at": 0, "data": None}

@app.get("/api/symbols")
@login_required
def symbols():
    if SYMBOLS_CACHE["data"] is not None and time.time() - SYMBOLS_CACHE["at"] < 600:
        return SYMBOLS_CACHE["data"]
    try:
        r = requests.get(FEED_BASE + "/api/symbols", params={"quotedWithinDays": 7}, timeout=12)
        r.raise_for_status()
        SYMBOLS_CACHE.update(at=time.time(), data=r.json())
        return SYMBOLS_CACHE["data"]
    except Exception as exc:
        return {"error": str(exc)}, 502

@app.get("/api/prices")
@login_required
def api_prices():
    keys = ("bid", "ask", "mid", "stale", "quoteAgeSeconds", "marketState")
    return {"feed": feed_state(),
            "prices": {s: {k: t.get(k) for k in keys} for s, t in STATE["latest"].items()}}

@app.get("/api/status")
@login_required
def api_status():
    return {"ok": True, "active_alerts": Alert.query.filter_by(enabled=True).count(),
            "feed": FEED_BASE, "feed_state": feed_state(), "poll_seconds": POLL_SECONDS}

@app.get("/health")
def health():
    return {"ok": True, "service": "market-webhook-monitor", "utc": datetime.now(timezone.utc).isoformat(),
            "poll_seconds": POLL_SECONDS, "feed": feed_state()["status"]}, 200

# ---------------------------------------------------------------- webhook delivery
def make_event(alert, tick, price, name="price_target_hit"):
    return {"event": name, "alert_id": alert.id, "label": alert.label or "", "symbol": alert.symbol,
            "direction": alert.direction, "target": alert.target, "price": price,
            "price_field": alert.price_field, "bid": tick.get("bid"), "ask": tick.get("ask"),
            "mid": tick.get("mid"), "feed": "biquote", "source": tick.get("source"),
            "market_state": tick.get("marketState"), "timestamp": tick.get("timestamp"),
            "triggered_at": datetime.now(timezone.utc).isoformat()}

def send_webhook(alert, event):
    """POST the event. Returns (ok, status_code, error). Redirects are not followed (SSRF safety)."""
    try:
        core.check_webhook_url(alert.webhook_url, DEV_MODE)
        raw = json.dumps(core.build_body(alert.payload_format or "json", event, alert.message_template),
                         separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "MarketWebhookMonitor/2.0",
                   "X-Alert-Id": str(alert.id)}
        if alert.webhook_secret:
            headers["X-Webhook-Secret"] = alert.webhook_secret
            headers["X-Signature-256"] = core.sign(alert.webhook_secret, raw)
        r = requests.post(alert.webhook_url, data=raw, headers=headers, timeout=10, allow_redirects=False)
        if 200 <= r.status_code < 300: return True, r.status_code, None
        return False, r.status_code, f"HTTP {r.status_code}"
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}".replace(alert.webhook_url, "<webhook url>")
        p = urlparse(alert.webhook_url)  # requests puts the path/query in errors; Discord/Telegram tokens live there
        for secret_part in (p.path + ("?" + p.query if p.query else ""), p.path):
            if len(secret_part) > 1: msg = msg.replace(secret_part, "/<redacted>")
        return False, None, msg[:300]

def record_delivery(alert, ok, code, err, attempt, kind):
    db.session.add(Delivery(alert_id=alert.id, symbol=alert.symbol, ok=ok, status_code=code,
                            error=err, attempt=attempt, kind=kind))

def deliver_pending():
    """Send crossings that are waiting (new ones and retries). Failures back off, then give up."""
    now = utcnow()
    due = Alert.query.filter(Alert.enabled.is_(True), Alert.pending_json.isnot(None),
                             or_(Alert.next_retry_at.is_(None), Alert.next_retry_at <= now)).all()
    for a in due:
        event = json.loads(a.pending_json)
        attempt = (a.retry_count or 0) + 1
        event["attempt"] = attempt
        ok, code, err = send_webhook(a, event)
        record_delivery(a, ok, code, err, attempt, "alert")
        if ok:
            a.pending_json, a.retry_count, a.next_retry_at = None, 0, None
            a.last_fired_at, a.fire_count = now, (a.fire_count or 0) + 1
            if a.repeat_mode == "once": a.enabled = False
            log.info("Webhook delivered for alert %s (%s)", a.id, a.symbol)
        elif attempt >= MAX_ATTEMPTS:
            a.pending_json, a.retry_count, a.next_retry_at = None, 0, None  # stays disarmed until it re-arms
            log.error("Giving up on alert %s (%s) after %s attempts: %s", a.id, a.symbol, attempt, err)
        else:
            a.retry_count = attempt
            a.next_retry_at = now + timedelta(seconds=core.backoff(attempt, RETRY_BASE))
            log.warning("Webhook failed for alert %s (attempt %s): %s", a.id, attempt, err)
        db.session.commit()

# ---------------------------------------------------------------- monitor
def poll_quotes():
    now = utcnow()
    live = []
    for a in Alert.query.filter_by(enabled=True).all():
        if a.expires_at and a.expires_at <= now and not a.pending_json:
            a.enabled = False
            log.info("Alert %s (%s) expired", a.id, a.symbol)
        else:
            live.append(a)
    db.session.commit()
    symbols_to_watch = sorted({a.symbol for a in live})
    if not symbols_to_watch:
        STATE.update(idle=True, error=None, latest={})
        return
    t0 = time.time()
    response = FEED.get(FEED_BASE + "/api/latest", params=[("symbols", s) for s in symbols_to_watch], timeout=12)
    response.raise_for_status()
    body = response.json()
    # API may return a list or a wrapper object; accept both.
    ticks = body if isinstance(body, list) else body.get("items", body.get("ticks", []))
    tick_map = {str(t.get("symbol", "")).upper(): t for t in ticks if t.get("symbol")}
    STATE.update(last_ok=time.time(), error=None, idle=False, latest=tick_map, latency_ms=int((time.time() - t0) * 1000))
    for a in live:
        tick = tick_map.get(a.symbol)
        if not tick or tick.get("stale") is True: continue
        age = tick.get("quoteAgeSeconds")
        if age is not None and float(age) > QUOTE_MAX_AGE: continue
        price = tick.get(a.price_field)
        if price is None: price = tick.get("mid")
        try: price = float(price)
        except (TypeError, ValueError): continue
        cooled = (not a.cooldown_seconds or not a.last_fired_at
                  or (now - a.last_fired_at).total_seconds() >= a.cooldown_seconds)
        fire, a.armed = core.step(a.direction, a.target, a.last_price, price, a.armed,
                                  can_rearm=cooled and not a.pending_json)
        if fire and not a.pending_json:
            a.pending_json = json.dumps(make_event(a, tick, price))
            a.retry_count, a.next_retry_at = 0, None
        a.last_price = price
    db.session.commit()

def monitor_loop():
    log.info("Price monitor started; polling every %ss", POLL_SECONDS)
    fails, last_prune = 0, 0
    while True:
        delay = POLL_SECONDS
        with app.app_context():
            try:
                poll_quotes(); fails = 0
            except Exception as exc:
                db.session.rollback()
                fails += 1
                STATE["error"] = f"{type(exc).__name__}: {exc}"[:200]
                delay = min(60, POLL_SECONDS * 2 ** min(fails, 5))  # back off while the feed is down
                log.exception("Monitor cycle failed (%s in a row)", fails)
            try:
                deliver_pending()
                if time.time() - last_prune > 3600:
                    last_prune = time.time()
                    Delivery.query.filter(Delivery.created_at < utcnow() - timedelta(days=LOG_DAYS)).delete()
                    db.session.commit()
            except Exception:
                db.session.rollback()
                log.exception("Delivery cycle failed")
        time.sleep(delay)

def start_monitor():
    if os.environ.get("DISABLE_MONITOR", "").lower() in ("1", "true", "yes"):
        return
    threading.Thread(target=monitor_loop, daemon=True, name="price-monitor").start()

with app.app_context():
    migrate()

start_monitor()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
