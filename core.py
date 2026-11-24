"""Pure helpers for the monitor: no Flask or database imports, so they are easy to test."""
import hashlib, hmac, ipaddress, re, socket
from urllib.parse import urlparse

SYMBOL_RE = re.compile(r"[A-Z0-9._#+/\-]{1,40}")
FORMATS = ("json", "discord", "slack", "telegram")
DEFAULT_MESSAGE = "{symbol} crossed {direction} {target} (now {price})"


def fmt_num(x):
    """2350.50000000 -> '2350.5', None -> '–'."""
    if x is None or x == "":
        return "–"
    return f"{float(x):.8f}".rstrip("0").rstrip(".")


def check_webhook_url(url, dev=False):
    """Require HTTPS and refuse hosts that resolve to private/internal addresses (SSRF guard)."""
    p = urlparse(url)
    if p.scheme != "https" and not (dev and p.scheme == "http"):
        raise ValueError("Webhook URL must be a valid HTTPS URL.")
    if not p.hostname:
        raise ValueError("Webhook URL must include a host name.")
    if not dev:
        try:
            infos = socket.getaddrinfo(p.hostname, p.port or 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            raise ValueError("Webhook host could not be resolved.")
        for info in infos:
            if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
                raise ValueError("Webhook host resolves to a private or reserved address.")
    return url


def step(direction, target, last_price, price, armed, can_rearm=True):
    """One monitoring step for one alert -> (fire, armed).

    The first observation (last_price is None) only sets a baseline and never fires.
    A fired alert is re-armed once price is back on the 'reset' side of the target
    (and the optional cooldown has passed, which the caller reports via can_rearm).
    """
    above = direction == "above"
    reset_side = price < target if above else price > target
    if last_price is None:
        return False, armed or reset_side
    if not armed and reset_side and can_rearm:
        armed = True
    crossed = (last_price < target <= price) if above else (last_price > target >= price)
    if armed and crossed:
        return True, False
    return False, armed


def render_message(template, ctx):
    """Replace {placeholders}; unknown ones are left as-is. No attribute access, so it is safe."""
    return re.sub(r"\{(\w+)\}", lambda m: str(ctx.get(m.group(1), m.group(0))),
                  template or DEFAULT_MESSAGE)


def build_body(fmt, event, template=None):
    """Turn the event dict into the JSON body for the chosen destination format."""
    if fmt not in FORMATS or fmt == "json":
        return event
    ctx = dict(event)
    for k in ("price", "target", "bid", "ask", "mid"):
        ctx[k] = fmt_num(event.get(k))
    msg = ("TEST: " if event.get("event") == "test" else "") + render_message(template, ctx)
    if fmt == "discord":
        return {"content": msg[:1900], "username": "Market Monitor"}
    return {"text": msg}  # Slack, Telegram (chat_id goes in the URL query string)


def sign(secret, raw_body):
    """HMAC-SHA256 of the exact request body, sent as X-Signature-256."""
    return "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def backoff(attempt, base=5, cap=300):
    """Seconds to wait after failed attempt number `attempt` (1-based)."""
    return min(cap, base * 2 ** (attempt - 1))
