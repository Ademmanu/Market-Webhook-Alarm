"""Completed-candle helpers for "Closes Above / Closes Below" alerts.

Deliberately has no Flask or database imports so the logic can be tested on its own.
Alert objects only need these attributes: id, direction, target, armed, enabled,
fire_mode, cooldown_minutes, next_rearm_at, last_fired_at, last_price.
"""
from datetime import datetime, timezone, timedelta

LIST_KEYS = ('bars', 'candles', 'items', 'data', 'ohlc', 'results')
TIME_KEYS = ('openTime', 'open_time', 'time', 't', 'timestamp', 'ts')
CLOSE_KEYS = ('close', 'c', 'Close')
OPEN_FLAG_KEYS = ('isOpen', 'is_open', 'forming', 'incomplete')
DONE_FLAG_KEYS = ('isClosed', 'is_closed', 'closed', 'complete', 'completed', 'final')


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'y')
    return bool(value)


def _as_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def extract_bars(data):
    """Accept a bare list or a dict that wraps the list under a common key."""
    if isinstance(data, list):
        return [b for b in data if isinstance(b, dict)]
    if isinstance(data, dict):
        for key in LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return [b for b in value if isinstance(b, dict)]
    return []


def is_open_bar(bar):
    for key in OPEN_FLAG_KEYS:
        if key in bar:
            return _truthy(bar[key])
    for key in DONE_FLAG_KEYS:
        if key in bar:
            return not _truthy(bar[key])
    return False


def bar_close(bar):
    for key in CLOSE_KEYS:
        if key in bar and bar[key] is not None:
            try:
                return float(bar[key])
            except (TypeError, ValueError):
                return None
    return None


def bar_time(bar):
    for key in TIME_KEYS:
        value = bar.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        try:
            return float(text)
        except ValueError:
            pass
        try:
            return _as_utc(datetime.fromisoformat(text.replace('Z', '+00:00'))).timestamp()
        except ValueError:
            continue
    return None


def completed_bars(data):
    """Return completed candles with a usable close, ordered oldest -> newest.

    Order is decided from the candle timestamps when every candle has one;
    otherwise the feed is assumed to be newest-first (as before).
    """
    bars = [b for b in extract_bars(data) if not is_open_bar(b) and bar_close(b) is not None]
    times = [bar_time(b) for b in bars]
    if bars and all(t is not None for t in times):
        return [b for _, b in sorted(zip(times, bars), key=lambda pair: pair[0])]
    return list(reversed(bars))


def new_completed_bars(bars, seen_time, last_close):
    """Candles (oldest -> newest) that have not been judged yet.

    seen_time is the in-memory time of the last judged candle. It is None after
    a restart; then only the newest candle is judged, and only if its close
    differs from the stored last close (i.e. it is not the candle we already saw).
    """
    if not bars:
        return []
    newest = bars[-1]
    if seen_time is None or bar_time(newest) is None:
        return [newest] if bar_close(newest) != last_close else []
    return [b for b in bars if (bar_time(b) or 0) > seen_time]


def evaluate_close(alert, close, now):
    """Judge one newly completed candle. Mutates alert state; True means fire.

    Closes Above ('ca'): fires when a completed candle closes above the target.
    Closes Below ('cb'): fires when a completed candle closes below the target.
    The candle that was already complete when the alert was created is used as
    the baseline only (see process_candle_alert) and is never judged.
    """
    above = alert.direction.startswith('ca')
    base = 'ca' if above else 'cb'
    fired_state = alert.direction.endswith('_fired')
    beyond = (close > alert.target) if above else (close < alert.target)
    fire = False
    if alert.armed and not fired_state and beyond:
        fire = True
        alert.last_fired_at = now
        alert.armed = False
        alert.direction = base + '_fired'
        if alert.fire_mode == 'once':
            alert.enabled = False
        elif alert.cooldown_minutes:
            alert.next_rearm_at = now + timedelta(minutes=alert.cooldown_minutes)
    elif not alert.armed and alert.fire_mode == 'repeat' and fired_state:
        returned = (close <= alert.target) if above else (close >= alert.target)
        cooldown_done = alert.next_rearm_at is None or _as_utc(alert.next_rearm_at) <= now
        if returned and cooldown_done:
            alert.armed = True
            alert.direction = base
            alert.next_rearm_at = None
    alert.last_price = close
    return fire


def process_candle_alert(alert, bars, now, seen, on_fire):
    """Run one monitor cycle for a close alert.

    bars: completed candles, oldest -> newest.
    seen: dict of alert id -> time of the last judged candle (in memory).
    on_fire(bar, close): called for each candle that triggers the alert.
    """
    if not bars:
        return
    if alert.last_price is None:
        newest = bars[-1]
        alert.last_price = bar_close(newest)
        seen[alert.id] = bar_time(newest)
        return
    for bar in new_completed_bars(bars, seen.get(alert.id), alert.last_price):
        close = bar_close(bar)
        if evaluate_close(alert, close, now):
            on_fire(bar, close)
        seen[alert.id] = bar_time(bar)
        if not alert.enabled:
            break
