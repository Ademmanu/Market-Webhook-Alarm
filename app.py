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


def migrate_database():
    """Add columns to the original Alert table without deleting existing records."""
    db.create_all()
    inspector = inspect(db.engine)
    if 'alert' not in inspector.get_table_names():
        return
    existing = {c['name'] for c in inspector.get_columns('alert')}
    # Nullable user_id keeps legacy records intact until the first user registers.
    additions = {
        'user_id': 'INTEGER', 'label': 'VARCHAR(100)', 'fire_mode': "VARCHAR(10) DEFAULT 'once'",
        'cooldown_minutes': 'INTEGER', 'expires_at': 'TIMESTAMP', 'next_rearm_at': 'TIMESTAMP'
    }
    for name, sql_type in additions.items():
        if name not in existing:
            try:
                db.session.execute(text(f'ALTER TABLE alert ADD COLUMN {name} {sql_type}'))
                db.session.commit()
            except Exception:
                db.session.rollback()
                log.exception('Could not add migration column %s', name)
    # Normalize old records to safe defaults.
    try:
        db.session.execute(text("UPDATE alert SET fire_mode='once' WHERE fire_mode IS NULL OR fire_mode=''") )
        db.session.commit()
    except Exception:
        db.session.rollback()


with app.app_context():
    migrate_database()


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


def generate_username():
    adjectives = ['amber','swift','quiet','bright','silver','rapid','clever','bold','calm','lucky','solar','blue']
    nouns = ['fox','hawk','tiger','otter','wolf','eagle','panda','lynx','bear','raven','koala','whale']
    for _ in range(100):
        name = f"{random.choice(adjectives)}{random.choice(nouns)}{random.randint(10,999)}alarm"
        if not User.query.filter_by(username=name).first():
            return name
    return 'user' + uuid.uuid4().hex[:10] + 'alarm'


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
            flash('Invalid account-creation password.', 'error')
            return render_template('signup.html')
        password = request.form.get('password','')
        if len(password) < 10:
            flash('Choose a password with at least 10 characters.', 'error')
            return render_template('signup.html')
        username = generate_username()
        user = User(username=username, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.flush()
        # Existing alerts from the original single-user version are assigned to first account.
        if Alert.query.filter(Alert.user_id.is_(None)).count():
            db.session.query(Alert).filter(Alert.user_id.is_(None)).update({Alert.user_id:user.id})
        db.session.commit()
        session.clear(); session.permanent=True; session['user_id']=user.id; csrf_token()
        flash('Account created. Save your generated username somewhere safe.', 'success')
        return render_template('signup_success.html', username=username)
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
    direction=form.get('direction','above')
    price_field=form.get('price_field','mid')
    fire_mode=form.get('fire_mode','once')
    target=float(form.get('target',''))
    webhook_url=form.get('webhook_url','').strip()
    secret=form.get('webhook_secret','').strip() or None
    cooldown_raw=form.get('cooldown_minutes','').strip()
    cooldown=int(cooldown_raw) if cooldown_raw else None
    expiry=parse_expiry(form.get('expires_at','').strip())
    if not symbol or len(symbol)>40: raise ValueError('Enter a valid symbol.')
    if direction not in ('above','below'): raise ValueError('Invalid direction.')
    if price_field not in ('bid','ask','mid'): raise ValueError('Invalid price field.')
    if fire_mode not in ('once','repeat'): raise ValueError('Invalid firing mode.')
    if fire_mode=='repeat' and (cooldown is None or cooldown<1 or cooldown>10080):
        raise ValueError('Repeat mode requires a cooldown between 1 and 10080 minutes.')
    ok,msg=valid_webhook_url(webhook_url)
    if not ok: raise ValueError(msg)
    if expiry and expiry <= utcnow(): raise ValueError('Expiry must be a future UTC time.')
    alert.symbol=symbol; alert.direction=direction; alert.target=target; alert.price_field=price_field
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
        a.armed=True; a.last_price=None; a.next_rearm_at=None
    db.session.commit(); return redirect(url_for('index'))


@app.post('/alerts/<int:alert_id>/rearm')
@login_required
def rearm_alert(alert_id):
    a=Alert.query.filter_by(id=alert_id,user_id=current_user().id).first_or_404()
    a.enabled=True; a.armed=True; a.last_price=None; a.next_rearm_at=None
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
             'direction':a.direction,'target':a.target,'price_field':a.price_field,
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
        raw=tick.get(a.price_field,tick.get('mid'))
        try: price=float(raw) if raw is not None else None
        except (TypeError,ValueError): price=None
        distance=(((a.target-price) if a.direction=='above' else (price-a.target))/a.target*100 if price is not None and a.target else None)
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


def queue_delivery(alert,tick,price):
    payload={'event':'price_target_hit','alert_id':alert.id,'label':alert.label,'symbol':alert.symbol,
        'direction':alert.direction,'target':alert.target,'price':price,'price_field':alert.price_field,
        'bid':tick.get('bid'),'ask':tick.get('ask'),'mid':tick.get('mid'),'feed':'biquote',
        'source':tick.get('source'),'market_state':tick.get('marketState'),'timestamp':tick.get('timestamp'),
        'triggered_at':utcnow().isoformat()}
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
                # Expire alerts in UTC.
                expiring=Alert.query.filter(Alert.enabled.is_(True),Alert.expires_at.isnot(None)).all()
                for a in expiring:
                    if as_utc(a.expires_at) <= now: a.enabled=False
                db.session.commit()
                alerts=Alert.query.filter_by(enabled=True).all()
                symbols=sorted({a.symbol for a in alerts})
                if symbols:
                    ticks=get_ticks(symbols)
                    tick_map={str(t.get('symbol','')).upper():t for t in ticks if t.get('symbol')}
                    for a in alerts:
                        tick=tick_map.get(a.symbol)
                        if not tick or tick.get('stale') is True: continue
                        age=tick.get('quoteAgeSeconds')
                        if age is not None and float(age)>QUOTE_MAX_AGE: continue
                        raw=tick.get(a.price_field,tick.get('mid'))
                        try: price=float(raw)
                        except (TypeError,ValueError): continue
                        if a.last_price is None:
                            a.last_price=price
                            # Establish baseline only; never fire immediately on create/resume.
                            db.session.commit(); continue
                        crossed=(a.last_price < a.target <= price) if a.direction=='above' else (a.last_price > a.target >= price)
                        reset_side=(price < a.target) if a.direction=='above' else (price > a.target)
                        if a.armed and crossed:
                            queue_delivery(a,tick,price)
                            a.last_fired_at=now; a.armed=False
                            if a.fire_mode=='once': a.enabled=False
                            elif a.cooldown_minutes:
                                a.next_rearm_at=now+timedelta(minutes=a.cooldown_minutes)
                        elif not a.armed and a.fire_mode=='repeat' and a.enabled and reset_side:
                            if a.next_rearm_at is None or as_utc(a.next_rearm_at)<=now:
                                a.armed=True; a.next_rearm_at=None
                        a.last_price=price
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
