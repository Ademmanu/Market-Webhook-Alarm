import os, time, json, uuid, random, string, threading, logging, socket, ipaddress
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from functools import wraps

import requests
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import inspect, text, func

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('market-monitor')

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get('SECRET_KEY', 'CHANGE-ME-USE-A-LONG-RANDOM-SECRET')
app.config.update(
    SQLALCHEMY_DATABASE_URI=os.environ.get('DATABASE_URL', 'sqlite:///alerts.db').replace('postgres://', 'postgresql://', 1),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=24),
    SESSION_REFRESH_EACH_REQUEST=True,
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)
db = SQLAlchemy(app)

FEED_BASE = os.environ.get('FEED_BASE', 'https://biquote.io').rstrip('/')
POLL_SECONDS = max(2, int(os.environ.get('POLL_SECONDS', '2')))
QUOTE_MAX_AGE = max(5, int(os.environ.get('QUOTE_MAX_AGE_SECONDS', '30')))
INVITE_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
_SYMBOL_CACHE = {'expires': 0, 'items': []}
_SYMBOL_CACHE_LOCK = threading.Lock()
MAX_LOGIN_ATTEMPTS = 5
LOCK_MINUTES = 5


def utcnow():
    return datetime.now(timezone.utc)


def as_utc(dt):
    if dt is None: return None
    if dt.tzinfo is None: return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    failed_attempts = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    label = db.Column(db.String(100), nullable=True)
    symbol = db.Column(db.String(40), nullable=False, index=True)
    direction = db.Column(db.String(10), nullable=False)
    target = db.Column(db.Float, nullable=False)
    price_field = db.Column(db.String(10), nullable=False, default='mid')
    webhook_url = db.Column(db.Text, nullable=False)
    webhook_secret = db.Column(db.Text, nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    armed = db.Column(db.Boolean, nullable=False, default=True)
    fire_mode = db.Column(db.String(10), nullable=False, default='once')
    cooldown_minutes = db.Column(db.Integer, nullable=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    next_rearm_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_price = db.Column(db.Float, nullable=True)
    last_fired_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class SavedWebhook(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    url = db.Column(db.Text, nullable=False)
    secret = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class Delivery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    alert_id = db.Column(db.Integer, db.ForeignKey('alert.id'), nullable=True, index=True)
    event_type = db.Column(db.String(20), nullable=False, default='alert')
    symbol = db.Column(db.String(40), nullable=True)
    destination = db.Column(db.Text, nullable=False)
    payload = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    max_attempts = db.Column(db.Integer, nullable=False, default=4)
    next_attempt_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    response_code = db.Column(db.Integer, nullable=True)
    response_text = db.Column(db.Text, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


with app.app_context():
    # Create missing tables only. No reset or automatic schema migration.
    db.create_all()


def reset_alert_condition_state(alert):
    if alert.direction in ('xu','xd'):
        alert.direction='cross'
    elif alert.direction=='ca_fired':
        alert.direction='ca'
    elif alert.direction=='cb_fired':
        alert.direction='cb'


def current_user():
    uid = session.get('user_id')
    return db.session.get(User, uid) if uid else None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for('login', next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def csrf_token():
    token = session.get('_csrf')
    if not token:
        token = uuid.uuid4().hex
        session['_csrf'] = token
    return token

app.jinja_env.globals['csrf_token'] = csrf_token

@app.before_request
def check_csrf():
    if request.method == 'POST':
        supplied = request.form.get('_csrf') or request.headers.get('X-CSRF-Token')
        if not supplied or supplied != session.get('_csrf'):
            abort(400, 'Invalid or missing security token. Refresh the page and try again.')


def valid_username(value):
    value = (value or '').strip()
    if not 3 <= len(value) <= 32:
        return False
    return all(ch.isalnum() or ch in '._-' for ch in value)


def valid_webhook_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            return False, 'Webhook URL must be HTTPS and must not contain embedded credentials.'
        # Block localhost and private/reserved IP destinations to reduce SSRF risk.
        host = parsed.hostname.lower()
        if host in ('localhost', 'localhost.localdomain'):
            return False, 'Localhost webhook destinations are not allowed.'
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                return False, 'Private or reserved IP addresses are not allowed.'
        except ValueError:
            try:
                for result in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM):
                    ip = ipaddress.ip_address(result[4][0])
                    if not ip.is_global:
                        return False, 'Webhook host resolves to a private or reserved IP address.'
            except socket.gaierror:
                return False, 'Webhook hostname could not be resolved.'
        return True, ''
    except Exception:
        return False, 'Invalid webhook URL.'


def parse_expiry(value):
    if not value: return None
    # datetime-local is interpreted as UTC by product design.
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@app.route('/signup', methods=['GET','POST'])
def signup():
    if request.method == 'POST':
        if not INVITE_PASSWORD:
            flash('Account creation is disabled: set ADMIN_PASSWORD in Render environment variables.', 'error')
            return render_template('signup.html')
        if request.form.get('invite_password','') != INVITE_PASSWORD:
            flash('Incorrect admin password.', 'error')
            return render_template('signup.html')
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        confirm_password = request.form.get('confirm_password','')
        if not valid_username(username):
            flash('Username must be 3–32 characters and use only letters, numbers, dots, underscores or hyphens.', 'error')
            return render_template('signup.html')
        if User.query.filter(func.lower(User.username)==username.lower()).first():
            flash('That username is already taken. Choose another.', 'error')
            return render_template('signup.html')
        if len(password) < 6:
            flash('Your password must contain at least 6 characters.', 'error')
            return render_template('signup.html')
        if password != confirm_password:
            flash('The passwords do not match.', 'error')
            return render_template('signup.html')
        user = User(username=username, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.flush()
        # Preserve legacy alerts by assigning unowned alerts to the first account.
        if Alert.query.filter(Alert.user_id.is_(None)).count():
            db.session.query(Alert).filter(Alert.user_id.is_(None)).update({Alert.user_id:user.id})
        db.session.commit()
        session.clear(); session.permanent=True; session['user_id']=user.id; csrf_token()
        flash('Account created successfully.', 'success')
        return redirect(url_for('index'))
    return render_template('signup.html')


@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username','').strip().lower()
        user = User.query.filter(func.lower(User.username)==username).first()
        if user and user.locked_until and as_utc(user.locked_until) > utcnow():
            remaining = int((as_utc(user.locked_until)-utcnow()).total_seconds())
            flash(f'This account is temporarily locked. Try again in about {max(1,(remaining+59)//60)} minute(s).', 'error')
            return render_template('login.html')
        password = request.form.get('password','')
        if user and check_password_hash(user.password_hash, password):
            user.failed_attempts=0; user.locked_until=None; db.session.commit()
            session.clear(); session.permanent=True; session['user_id']=user.id; csrf_token()
            return redirect(request.args.get('next') or url_for('index'))
        if user:
            user.failed_attempts += 1
            if user.failed_attempts >= MAX_LOGIN_ATTEMPTS:
                user.failed_attempts=0
                user.locked_until=utcnow()+timedelta(minutes=LOCK_MINUTES)
            db.session.commit()
        flash('Incorrect username or password.', 'error')
    return render_template('login.html')


@app.post('/logout')
@login_required
def logout():
    session.clear(); return redirect(url_for('login'))


@app.post('/account/delete')
@login_required
def delete_account():
    user=current_user()
    alert_ids=[row.id for row in Alert.query.filter_by(user_id=user.id).with_entities(Alert.id).all()]
    # Remove this account's data, including delivery records linked through its alerts.
    Delivery.query.filter(
        db.or_(Delivery.user_id==user.id, Delivery.alert_id.in_(alert_ids) if alert_ids else db.false())
    ).delete(synchronize_session=False)
    Alert.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    SavedWebhook.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    db.session.delete(user)
    db.session.commit()
    session.clear()
    flash('Your account and its associated data have been deleted.', 'success')
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    user=current_user()
    alerts=Alert.query.filter_by(user_id=user.id).order_by(Alert.id.desc()).all()
    webhooks=SavedWebhook.query.filter_by(user_id=user.id).order_by(SavedWebhook.name).all()
    deliveries=Delivery.query.filter_by(user_id=user.id).order_by(Delivery.id.desc()).limit(30).all()
    return render_template('index.html', user=user, alerts=alerts, webhooks=webhooks, deliveries=deliveries, poll_seconds=POLL_SECONDS)


def alert_from_form(alert, form):
    symbol=form.get('symbol','').strip().upper()
    condition=form.get('condition','crossing')
    target=float(form.get('target',''))
    webhook_url=form.get('webhook_url','').strip()
    secret=form.get('webhook_secret','').strip() or None
    fire_mode=form.get('fire_mode','once')
    cooldown_raw=form.get('cooldown_minutes','').strip()
    cooldown=int(cooldown_raw) if cooldown_raw else None
    expiry=parse_expiry(form.get('expires_at','').strip())
    interval=form.get('candle_interval','1m')
    if not symbol or len(symbol)>40: raise ValueError('Enter a valid symbol.')
    if condition not in ('crossing','closing'): raise ValueError('Invalid alert condition.')
    if interval not in ('1m','5m','15m','30m','1h','4h','1d'): raise ValueError('Invalid candle timeframe.')
    if condition == 'closing':
        quotes=get_ticks([symbol])
        quote=next((q for q in quotes if str(q.get('symbol','')).upper()==symbol), None)
        try:
            current_mid=float(quote.get('mid')) if quote and quote.get('mid') is not None else None
        except (TypeError,ValueError):
            current_mid=None
        if current_mid is None:
            raise ValueError('Could not get the current mid-price for this symbol. Try again when a quote is available.')
        if target == current_mid:
            raise ValueError('Target equals the current price. Change the target slightly above or below the current price.')
        detected_direction='ca' if target > current_mid else 'cb'
    if fire_mode not in ('once','repeat'): raise ValueError('Invalid firing mode.')
    if fire_mode=='repeat' and (cooldown is None or cooldown<1 or cooldown>10080):
        raise ValueError('Repeat mode requires a cooldown between 1 and 10080 minutes.')
    ok,msg=valid_webhook_url(webhook_url)
    if not ok: raise ValueError(msg)
    if expiry and expiry <= utcnow(): raise ValueError('Expiry must be a future UTC time.')
    # Reuse existing columns to avoid a schema migration: direction stores the
    # condition; price_field stores candle interval (or mid for tick crossing).
    alert.symbol=symbol
    alert.direction='cross' if condition=='crossing' else detected_direction
    alert.price_field='mid' if condition=='crossing' else interval
    alert.target=target
    alert.webhook_url=webhook_url; alert.webhook_secret=secret; alert.label=form.get('label','').strip()[:100] or None
    alert.fire_mode=fire_mode; alert.cooldown_minutes=cooldown if fire_mode=='repeat' else None
    alert.expires_at=expiry
    if form.get('save_webhook') == '1':
        name=form.get('webhook_name','').strip()[:80]
        if name:
            existing=SavedWebhook.query.filter_by(user_id=alert.user_id,url=webhook_url).first()
            if existing:
                existing.name=name; existing.secret=secret
            else:
                db.session.add(SavedWebhook(user_id=alert.user_id,name=name,url=webhook_url,secret=secret))


@app.post('/alerts')
@login_required
def create_alert():
    try:
        alert=Alert(user_id=current_user().id, enabled=True, armed=True)
        alert_from_form(alert,request.form)
        # first quote after creation establishes baseline, preventing immediate false firing
        alert.last_price=None; alert.next_rearm_at=None
        db.session.add(alert); db.session.commit(); flash('Alert created.', 'success')
    except Exception as exc:
        db.session.rollback(); flash(f'Could not create alert: {exc}', 'error')
    return redirect(url_for('index'))


@app.route('/alerts/<int:alert_id>/edit', methods=['GET','POST'])
@login_required
def edit_alert(alert_id):
    alert=Alert.query.filter_by(id=alert_id,user_id=current_user().id).first_or_404()
    if request.method=='POST':
        try:
            alert_from_form(alert,request.form)
            # Editing resets the quote baseline; do not fire on a stale previous quote.
            alert.last_price=None; alert.armed=True; alert.next_rearm_at=None
            db.session.commit(); flash('Alert updated. It will establish a fresh price baseline.', 'success')
            return redirect(url_for('index'))
        except Exception as exc:
            db.session.rollback(); flash(f'Could not update alert: {exc}', 'error')
    webhooks=SavedWebhook.query.filter_by(user_id=current_user().id).all()
    return render_template('edit.html',alert=alert,webhooks=webhooks)


@app.post('/alerts/<int:alert_id>/toggle')
@login_required
def toggle_alert(alert_id):
    a=Alert.query.filter_by(id=alert_id,user_id=current_user().id).first_or_404()
    a.enabled=not a.enabled
    if a.enabled:
        reset_alert_condition_state(a)
        a.armed=True; a.last_price=None; a.next_rearm_at=None
    db.session.commit(); return redirect(url_for('index'))


@app.post('/alerts/<int:alert_id>/rearm')
@login_required
def rearm_alert(alert_id):
    a=Alert.query.filter_by(id=alert_id,user_id=current_user().id).first_or_404()
    a.enabled=True; reset_alert_condition_state(a); a.armed=True; a.last_price=None; a.next_rearm_at=None
    db.session.commit(); flash('Alert re-armed. A fresh quote will establish its baseline.', 'success')
    return redirect(url_for('index'))


@app.post('/alerts/<int:alert_id>/delete')
@login_required
def delete_alert(alert_id):
    a=Alert.query.filter_by(id=alert_id,user_id=current_user().id).first_or_404()
    Delivery.query.filter_by(alert_id=a.id).delete()
    db.session.delete(a); db.session.commit(); flash('Alert deleted.', 'success')
    return redirect(url_for('index'))


@app.post('/alerts/bulk/<action>')
@login_required
def bulk_alerts(action):
    if action not in ('pause','resume'): abort(404)
    alerts=Alert.query.filter_by(user_id=current_user().id).all()
    for a in alerts:
        a.enabled=(action=='resume')
        if action=='resume':
            reset_alert_condition_state(a)
            a.armed=True; a.last_price=None; a.next_rearm_at=None
    db.session.commit(); flash('All alerts '+('resumed with fresh price baselines.' if action=='resume' else 'paused.'), 'success')
    return redirect(url_for('index'))


@app.post('/webhooks/save')
@login_required
def save_webhook():
    name=request.form.get('name','').strip()[:80]; url=request.form.get('url','').strip(); secret=request.form.get('secret','').strip() or None
    ok,msg=valid_webhook_url(url)
    if not name or not ok:
        flash(msg or 'Enter a name for this webhook.', 'error'); return redirect(url_for('index'))
    db.session.add(SavedWebhook(user_id=current_user().id,name=name,url=url,secret=secret)); db.session.commit()
    flash('Webhook destination saved to your account.', 'success'); return redirect(url_for('index'))


@app.route('/webhooks/<int:webhook_id>/edit', methods=['GET','POST'])
@login_required
def edit_webhook(webhook_id):
    w=SavedWebhook.query.filter_by(id=webhook_id,user_id=current_user().id).first_or_404()
    if request.method == 'POST':
        name=request.form.get('name','').strip()[:80]
        url=request.form.get('url','').strip()
        secret=request.form.get('secret','').strip() or None
        ok,msg=valid_webhook_url(url)
        if not name or not ok:
            flash(msg or 'Enter a name for this webhook.', 'error')
            return render_template('saved_webhook_edit.html', webhook=w)
        w.name=name; w.url=url; w.secret=secret
        db.session.commit()
        flash('Saved webhook updated.', 'success')
        return redirect(url_for('index'))
    return render_template('saved_webhook_edit.html', webhook=w)


@app.post('/webhooks/<int:webhook_id>/delete')
@login_required
def delete_webhook(webhook_id):
    w=SavedWebhook.query.filter_by(id=webhook_id,user_id=current_user().id).first_or_404()
    db.session.delete(w); db.session.commit(); return redirect(url_for('index'))


@app.post('/alerts/<int:alert_id>/test')
@login_required
def test_alert(alert_id):
    a=Alert.query.filter_by(id=alert_id,user_id=current_user().id).first_or_404()
    payload={'event':'webhook_test','test':True,'alert_id':a.id,'label':a.label,'symbol':a.symbol,
             'message':f"{a.symbol} test alert at {a.target}",'target':a.target,
             'feed':'market-webhook-monitor','triggered_at':utcnow().isoformat()}
    d=Delivery(user_id=current_user().id,alert_id=a.id,event_type='test',symbol=a.symbol,
               destination=a.webhook_url,payload=json.dumps(payload),status='pending',attempts=0,
               max_attempts=4,next_attempt_at=utcnow())
    db.session.add(d); db.session.commit(); flash('Test webhook queued. Check the delivery log.', 'success')
    return redirect(url_for('index'))


@app.get('/api/symbols')
def api_symbols():
    # Cache the full recent-quote catalogue for 30 minutes; don't use liveOnly,
    # because that can return an almost-empty list when markets are closed.
    now=time.time()
    with _SYMBOL_CACHE_LOCK:
        if _SYMBOL_CACHE['items'] and _SYMBOL_CACHE['expires'] > now:
            return jsonify(_SYMBOL_CACHE['items'])
    try:
        r=requests.get(FEED_BASE+'/api/symbols',params={'quotedWithinDays':7},timeout=15)
        r.raise_for_status(); data=r.json()
        if isinstance(data,dict):
            data=data.get('items',data.get('symbols',data.get('data',[])))
        if not isinstance(data,list): data=[]
        with _SYMBOL_CACHE_LOCK:
            _SYMBOL_CACHE['items']=data; _SYMBOL_CACHE['expires']=now+1800
        return jsonify(data)
    except Exception as exc:
        log.exception('Symbol catalogue fetch failed')
        # Return stale cached catalogue if available, rather than an empty picker.
        with _SYMBOL_CACHE_LOCK:
            if _SYMBOL_CACHE['items']:
                return jsonify(_SYMBOL_CACHE['items'])
        return jsonify({'error':str(exc)}),502


@app.get('/api/quote')
@login_required
def api_quote():
    symbol=request.args.get('symbol','').strip().upper()
    if not symbol or len(symbol)>40:
        return jsonify({'ok':False,'error':'Enter a valid symbol.'}),400
    try:
        quotes=get_ticks([symbol])
        quote=next((q for q in quotes if str(q.get('symbol','')).upper()==symbol),None)
        mid=float(quote.get('mid')) if quote and quote.get('mid') is not None else None
        if mid is None: return jsonify({'ok':False,'error':'No current mid-price available.'}),503
        return jsonify({'ok':True,'symbol':symbol,'mid':mid})
    except Exception:
        return jsonify({'ok':False,'error':'Live quote unavailable.'}),503


@app.get('/api/dashboard')
@login_required
def api_dashboard():
    user=current_user(); alerts=Alert.query.filter_by(user_id=user.id).all()
    symbols=sorted({a.symbol for a in alerts if a.enabled})
    prices={}
    if symbols:
        try:
            params=[('symbols',s) for s in symbols]
            r=requests.get(FEED_BASE+'/api/latest',params=params,timeout=10); r.raise_for_status()
            body=r.json()
            if isinstance(body,dict) and 'symbol' in body:
                ticks=[body]
            elif isinstance(body,dict) and isinstance(body.get('items',body.get('ticks',body.get('data'))),list):
                ticks=body.get('items',body.get('ticks',body.get('data')))
            elif isinstance(body,dict):
                ticks=[]
                for key,value in body.items():
                    if isinstance(value,dict):
                        value.setdefault('symbol',key); ticks.append(value)
            elif isinstance(body,list): ticks=body
            else: ticks=[]
            for t in ticks:
                sym=str(t.get('symbol','')).upper()
                if sym: prices[sym]=t
        except Exception as exc:
            return jsonify({'feed_ok':False,'feed_error':str(exc),'prices':{},'active':len([a for a in alerts if a.enabled]),
                            'fired':Delivery.query.filter_by(user_id=user.id,status='sent',event_type='alert').count(),
                            'failed':Delivery.query.filter_by(user_id=user.id,status='failed').count()}),200
    rows=[]
    for a in alerts:
        tick=prices.get(a.symbol,{})
        raw=tick.get('mid')
        try: price=float(raw) if raw is not None else None
        except (TypeError,ValueError): price=None
        distance=(abs(a.target-price)/abs(a.target)*100 if price is not None and a.target else None)
        rows.append({'id':a.id,'symbol':a.symbol,'price':price,'distance_pct':distance,'stale':tick.get('stale',False),
                     'quote_age':tick.get('quoteAgeSeconds'),'source':tick.get('source'),'market_state':tick.get('marketState')})
    return jsonify({'feed_ok':bool(prices) if symbols else True,'prices':rows,
        'active':sum(1 for a in alerts if a.enabled),
        'fired':Delivery.query.filter_by(user_id=user.id,status='sent',event_type='alert').count(),
        'failed':Delivery.query.filter_by(user_id=user.id,status='failed').count(),
        'pending':Delivery.query.filter_by(user_id=user.id,status='pending').count(),
        'updated_at':utcnow().isoformat()})


@app.get('/health')
def health():
    try: db.session.execute(text('SELECT 1'))
    except Exception as exc: return jsonify({'ok':False,'database_error':str(exc)}),503
    return jsonify({'ok':True,'service':'market-webhook-monitor','utc':utcnow().isoformat(),'poll_seconds':POLL_SECONDS}),200


@app.get('/api/status')
@login_required
def api_status():
    u=current_user()
    return jsonify({'ok':True,'active_alerts':Alert.query.filter_by(user_id=u.id,enabled=True).count(),
                    'pending_deliveries':Delivery.query.filter_by(user_id=u.id,status='pending').count(),'poll_seconds':POLL_SECONDS})


def queue_delivery(alert,tick,price,closed=False):
    condition = 'crossed' if alert.direction=='cross' else ('closed above' if alert.direction=='ca' else 'closed below')
    message=f"{alert.symbol} {condition} {alert.target:.8f}".rstrip('0').rstrip('.')
    payload={'message':message,'event':'price_target_hit','alert_id':alert.id,'label':alert.label,
        'symbol':alert.symbol,'condition':condition,'target':alert.target,'price':price,
        'price_field':'mid','closed_candle':closed,'candle_interval':alert.price_field if closed else None,
        'timestamp':tick.get('timestamp') or tick.get('openTime'),'feed':'biquote',
        'source':tick.get('source'),'triggered_at':utcnow().isoformat()}
    d=Delivery(user_id=alert.user_id,alert_id=alert.id,event_type='alert',symbol=alert.symbol,
        destination=alert.webhook_url,payload=json.dumps(payload),status='pending',attempts=0,max_attempts=4,next_attempt_at=utcnow())
    db.session.add(d)



def get_ticks(symbols):
    params=[('symbols',s) for s in symbols]
    r=requests.get(FEED_BASE+'/api/latest',params=params,timeout=12); r.raise_for_status()
    body=r.json()
    if isinstance(body,list): return body
    if isinstance(body,dict) and 'symbol' in body: return [body]
    if isinstance(body,dict) and isinstance(body.get('items',body.get('ticks',body.get('data'))),list):
        return body.get('items',body.get('ticks',body.get('data')))
    if isinstance(body,dict):
        result=[]
        for key,value in body.items():
            if isinstance(value,dict):
                value.setdefault('symbol',key); result.append(value)
        return result
    return []


def monitor_loop():
    outage_delay=5
    log.info('Monitor started: poll=%ss feed=%s',POLL_SECONDS,FEED_BASE)
    while True:
        try:
            with app.app_context():
                now=utcnow()
                expiring=Alert.query.filter(Alert.enabled.is_(True),Alert.expires_at.isnot(None)).all()
                for a in expiring:
                    if as_utc(a.expires_at) <= now: a.enabled=False
                db.session.commit()
                alerts=Alert.query.filter_by(enabled=True).all()
                tick_symbols=sorted({a.symbol for a in alerts if a.direction in ('cross','xu','xd')})
                tick_map={}
                if tick_symbols:
                    tick_map={str(t.get('symbol','')).upper():t for t in get_ticks(tick_symbols) if t.get('symbol')}
                candle_cache={}
                for a in alerts:
                    if a.direction in ('cross','xu','xd'):
                        tick=tick_map.get(a.symbol)
                        if not tick or tick.get('stale') is True: continue
                        age=tick.get('quoteAgeSeconds')
                        if age is not None and float(age)>QUOTE_MAX_AGE: continue
                        try: price=float(tick.get('mid'))
                        except (TypeError,ValueError): continue
                        if a.last_price is None:
                            a.last_price=price; db.session.commit(); continue
                        crossed=(a.last_price < a.target <= price) or (a.last_price > a.target >= price)
                        if a.armed and a.direction=='cross' and crossed:
                            fired_up = price > a.last_price
                            queue_delivery(a,tick,price,False)
                            a.last_fired_at=now; a.armed=False; a.direction='xu' if fired_up else 'xd'
                            if a.fire_mode=='once': a.enabled=False
                            elif a.cooldown_minutes: a.next_rearm_at=now+timedelta(minutes=a.cooldown_minutes)
                        elif not a.armed and a.fire_mode=='repeat':
                            returned = (price <= a.target) if a.direction=='xu' else (price >= a.target)
                            cooldown_done = a.next_rearm_at is None or as_utc(a.next_rearm_at)<=now
                            if returned and cooldown_done:
                                a.armed=True; a.direction='cross'; a.next_rearm_at=None
                        a.last_price=price
                    else:
                        interval=a.price_field
                        key=(a.symbol,interval)
                        if key not in candle_cache:
                            try:
                                resp=requests.get(f'{FEED_BASE}/api/{a.symbol}/ohlc',params={'interval':interval,'limit':5},timeout=12)
                                resp.raise_for_status(); data=resp.json()
                                bars=data.get('bars',[]) if isinstance(data,dict) else []
                                candle_cache[key]=[b for b in bars if not b.get('isOpen')]
                            except Exception:
                                log.exception('Candle fetch failed for %s %s',a.symbol,interval); candle_cache[key]=[]
                        bars=candle_cache[key]
                        if not bars: continue
                        # Feed returns newest first. Only completed candles are eligible.
                        bar=bars[0]
                        try: close=float(bar['close'])
                        except (KeyError,TypeError,ValueError): continue
                        if a.last_price is None:
                            a.last_price=close; db.session.commit(); continue
                        base_condition='ca' if a.direction.startswith('ca') else 'cb'
                        fired_state=a.direction.endswith('_fired')
                        crossed=(a.last_price <= a.target < close) if base_condition=='ca' else (a.last_price >= a.target > close)
                        if a.armed and not fired_state and crossed:
                            queue_delivery(a,bar,close,True)
                            a.last_fired_at=now; a.armed=False; a.direction=base_condition+'_fired'
                            if a.fire_mode=='once': a.enabled=False
                            elif a.cooldown_minutes: a.next_rearm_at=now+timedelta(minutes=a.cooldown_minutes)
                        elif not a.armed and a.fire_mode=='repeat' and fired_state:
                            returned=(close <= a.target) if base_condition=='ca' else (close >= a.target)
                            cooldown_done=a.next_rearm_at is None or as_utc(a.next_rearm_at)<=now
                            if returned and cooldown_done:
                                a.armed=True; a.direction=base_condition; a.next_rearm_at=None
                        a.last_price=close
                    db.session.commit()
                outage_delay=5
        except Exception:
            log.exception('Price monitor cycle failed')
            time.sleep(outage_delay); outage_delay=min(outage_delay*2,120)
        time.sleep(POLL_SECONDS)



def delivery_loop():
    log.info('Delivery worker started')
    while True:
        try:
            with app.app_context():
                now=utcnow()
                item=Delivery.query.filter(Delivery.status=='pending',Delivery.next_attempt_at<=now).order_by(Delivery.id.asc()).first()
                if item:
                    # Claim this attempt before network I/O to avoid duplicate work in one process.
                    item.attempts += 1; item.next_attempt_at=now+timedelta(seconds=30); db.session.commit()
                    try:
                        alert=db.session.get(Alert,item.alert_id) if item.alert_id else None
                        headers={'Content-Type':'application/json','User-Agent':'MarketWebhookMonitor/2.0'}
                        if alert and alert.webhook_secret: headers['X-Webhook-Secret']=alert.webhook_secret
                        response=requests.post(item.destination,data=item.payload,headers=headers,timeout=12,allow_redirects=False)
                        item.response_code=response.status_code; item.response_text=(response.text or '')[:1500]
                        if 200<=response.status_code<300:
                            item.status='sent'; item.last_error=None
                        elif item.attempts>=item.max_attempts:
                            item.status='failed'; item.last_error=f'HTTP {response.status_code}'
                        else:
                            item.next_attempt_at=utcnow()+timedelta(seconds=5)
                            item.last_error=f'HTTP {response.status_code}'
                    except Exception as exc:
                        item.last_error=str(exc)[:1000]
                        if item.attempts>=item.max_attempts: item.status='failed'
                        else: item.next_attempt_at=utcnow()+timedelta(seconds=5)
                    item.updated_at=utcnow(); db.session.commit()
                else:
                    time.sleep(1)
        except Exception:
            log.exception('Delivery worker cycle failed'); time.sleep(3)


# One worker process is configured in Render; guard avoids duplicate threads on accidental repeated imports.
_started=False
_start_lock=threading.Lock()
def start_workers():
    global _started
    with _start_lock:
        if _started or os.environ.get('DISABLE_WORKERS','').lower() in ('1','true','yes'): return
        _started=True
        threading.Thread(target=monitor_loop,daemon=True,name='price-monitor').start()
        threading.Thread(target=delivery_loop,daemon=True,name='delivery-worker').start()

start_workers()

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT','5000')))
