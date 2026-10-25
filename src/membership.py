"""Paid memberships for the HOSTED website (display / access only - nothing here touches trading).

OFF unless the host sets MEMBERSHIP_ON=1: with it off the site works exactly as before (owner approves each person, nobody expires).
With it ON: anyone can make an account at once, gets ONE free 3-hour pass (one per account, per e-mail address and per network address), then needs a paid plan (Stripe).
People who were already on the site before it was switched on keep access with no expiry (their record simply has no "member_until").

Stripe tells the site about payments through /stripe_webhook (signed with STRIPE_WEBHOOK_SECRET, which only the owner puts on the host).
Plans, payment links and price ids live in data_cache/plans.json when that file exists (so the test links can be swapped for live ones without changing code), else DEFAULT_PLANS below (Stripe TEST mode).
"""
import hashlib
import hmac
import html
import json
import os
import re
import threading
import time

from src import accounts as _acc

MEMBERSHIP_ON = os.environ.get("MEMBERSHIP_ON", "") == "1"
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
TRIAL_HOURS = 3
_DATA = _acc._DATA
TRIALS_FILE = os.path.join(_DATA, "trials.json")
EVENTS_FILE = os.path.join(_DATA, "stripe_events.json")
PLANS_FILE = os.path.join(_DATA, "plans.json")
_mu = threading.Lock()
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,200}\.[^@\s]{2,24}$")

DEFAULT_PLANS = {                    # Stripe TEST mode (made 2026-10-25). Replace with data_cache/plans.json for live mode.
    "plans": [
        {"name": "10 days", "days": 10, "price": 65, "price_id": "price_1UUZSYI713HGFPqTQB8wK6rz", "plink_id": "plink_1UUZcRI713HGFPqTrMR1EHKx", "link": "https://buy.stripe.com/test_5kQdR937T7Yr4Vrdzc8Zq00"},
        {"name": "30 days", "days": 30, "price": 165, "price_id": "price_1UUZShI713HGFPqTdx26WsxC", "plink_id": "plink_1UUZcSI713HGFPqTKUfauP0R", "link": "https://buy.stripe.com/test_dRm14neQB0vZcnT3YC8Zq01"},
        {"name": "3.5 months", "days": 105, "price": 379, "price_id": "price_1UUZTlI713HGFPqTKC0h7zxH", "plink_id": "plink_1UUZcTI713HGFPqTth8Hna2m", "link": "https://buy.stripe.com/test_eVq00j9whguX2Nj66K8Zq02", "best": True},
    ],
    "portal_url": "https://billing.stripe.com/p/login/test_5kQdR937T7Yr4Vrdzc8Zq00",      # where members manage or cancel (Stripe TEST mode)
}


def plans():
    try:
        with open(PLANS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("plans"):
            return d
    except Exception:
        pass
    return DEFAULT_PLANS


def _days_for(plink_id="", price_id=""):
    for p in plans()["plans"]:
        if (plink_id and p.get("plink_id") == plink_id) or (price_id and p.get("price_id") == price_id):
            return int(p["days"]), p["name"]
    return 0, ""


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _h(s):
    return hashlib.sha256((s or "").strip().lower().encode("utf-8")).hexdigest()[:32]


# ---- who has access right now
def status(name):
    """('open', None) for people with no expiry (admin, everybody who was here before), ('member', until) while time is left, ('expired', until) otherwise."""
    if name == "admin":
        return "open", None
    u = _acc.list_users().get(name)
    if u is None:
        return "expired", 0
    if "member_until" not in u:
        return "open", None
    until = float(u.get("member_until") or 0)
    return ("member" if until > time.time() else "expired"), until


def has_access(name):
    return (not MEMBERSHIP_ON) or status(name)[0] in ("open", "member")


def grant_days(name, days, plan="", customer=""):
    """Add days on top of whatever time is left. Returns the new end time."""
    u = _acc.list_users().get(name)
    if u is None:
        return None
    base = max(time.time(), float(u.get("member_until") or 0)) if "member_until" in u else time.time()
    until = base + days * 86400
    _acc.set_fields(name, member_until=until, plan=plan or u.get("plan", ""), stripe_customer=customer or u.get("stripe_customer", ""))
    _acc._log("membership extended", name, plan=plan, days=days, until=until)
    return until


# ---- the one free pass
def trial_available(name, ip):
    u = _acc.list_users().get(name) or {}
    if u.get("trial_used"):
        return False, "You already used your free pass."
    t = _read(TRIALS_FILE, {})
    if u.get("email") and ("email:" + _h(u["email"])) in t:
        return False, "This e-mail address already used its free pass."
    if ("ip:" + _h(ip)) in t and time.time() - t[("ip:" + _h(ip))] < 30 * 86400:
        return False, "A free pass was already used from this network in the last 30 days."
    return True, ""


def start_trial(name, ip):
    with _mu:
        ok, why = trial_available(name, ip)
        if not ok:
            return False, why
        u = _acc.list_users().get(name) or {}
        t = _read(TRIALS_FILE, {})
        now = time.time()
        if u.get("email"):
            t["email:" + _h(u["email"])] = now
        t["ip:" + _h(ip)] = now
        _write(TRIALS_FILE, t)
        _acc.set_fields(name, trial_used=True, member_until=now + TRIAL_HOURS * 3600, plan="free pass")
    _acc._log("free pass started", name, hours=TRIAL_HOURS)
    return True, ""


# ---- accounts made on the spot
def create_account(name, pw, email, note, ip, terms=""):
    name = (name or "").strip().lower()
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        return False, "Please type a real e-mail address."
    if any((u.get("email") or "") == email for u in _acc.list_users().values()):
        return False, "That e-mail address already has an account. Sign in instead."
    ok, msg = _acc.create_instant(name, pw, email, note, ip, terms)
    return ok, msg


# ---- Stripe webhook
def verify_signature(raw, header, secret, tolerance=300):
    try:
        parts = [p.split("=", 1) for p in (header or "").split(",") if "=" in p]
        t = next(v for k, v in parts if k == "t")
        sigs = [v for k, v in parts if k == "v1"]
        expected = hmac.new(secret.encode("utf-8"), t.encode() + b"." + raw, hashlib.sha256).hexdigest()
        return abs(time.time() - int(t)) <= tolerance and any(hmac.compare_digest(expected, s) for s in sigs)
    except Exception:
        return False


def handle_event(ev):
    """Returns a short note about what happened (for the log / tests)."""
    with _mu:
        seen = _read(EVENTS_FILE, [])
        if ev.get("id") in seen:
            return "duplicate"
        _write(EVENTS_FILE, (seen + [ev.get("id")])[-1000:])
    typ, obj = ev.get("type", ""), (ev.get("data") or {}).get("object") or {}
    users = _acc.list_users()
    if typ == "checkout.session.completed":
        if obj.get("mode") != "subscription" or obj.get("payment_status") not in ("paid", "no_payment_required"):
            return "ignored"
        name = (obj.get("client_reference_id") or "").strip().lower()
        if name not in users:                                              # no user name came with the payment: fall back to the e-mail they gave
            em = ((obj.get("customer_details") or {}).get("email") or obj.get("customer_email") or "").strip().lower()
            name = next((n for n, u in users.items() if em and u.get("email") == em), "")
        days, plan = _days_for(plink_id=obj.get("payment_link") or "")
        if not name or not days:
            _acc._log("payment could not be matched", name or "?", session=obj.get("id"), customer=obj.get("customer"), payment_link=obj.get("payment_link"))
            return "unmatched"
        grant_days(name, days, plan, obj.get("customer") or "")
        return f"granted {days} days to {name}"
    if typ == "invoice.paid" and obj.get("billing_reason") == "subscription_cycle":
        cust = obj.get("customer")
        name = next((n for n, u in users.items() if cust and u.get("stripe_customer") == cust), "")
        price = ""
        for ln in ((obj.get("lines") or {}).get("data") or []):
            price = (((ln.get("pricing") or {}).get("price_details") or {}).get("price")) or (ln.get("price") or {}).get("id") or price
        days, plan = _days_for(price_id=price)
        if not name or not days:
            _acc._log("renewal could not be matched", name or "?", customer=cust, price=price)
            return "unmatched"
        grant_days(name, days, plan, cust)
        return f"renewed {days} days for {name}"
    if typ == "invoice.payment_failed":
        _acc._log("renewal payment failed", next((n for n, u in users.items() if u.get("stripe_customer") == obj.get("customer")), "?"), customer=obj.get("customer"))
        return "logged"
    return "ignored"


# ---- pages
def _fmt(ts):
    return time.strftime("%b %d, %Y %I:%M %p UTC", time.gmtime(ts))


def join_page(name, ip, msg="", err=False):
    from src.account_pages import _page, _msg, _tiny
    st, until = status(name)
    u = _acc.list_users().get(name) or {}
    pl = plans()
    cards = []
    for p in pl["plans"]:
        link = p["link"] + ("&" if "?" in p["link"] else "?") + "client_reference_id=" + html.escape(name) + ("&prefilled_email=" + html.escape(u["email"]) if u.get("email") else "")
        cards.append(f'<div style="flex:1 1 150px;border:1px solid {"#d29922" if p.get("best") else "#30363d"};border-radius:10px;padding:14px;background:#0d1117">'
                     f'{"<div style=color:#d29922;font-size:12px;font-weight:700>BEST VALUE</div>" if p.get("best") else ""}'
                     f'<div style="font-size:14px;color:#9da7b3">{html.escape(p["name"].upper())}</div><div style="font-size:30px;font-weight:700">${p["price"]}</div>'
                     f'<div style="font-size:13px;color:#8b949e">${p["price"] / p["days"]:.2f} a day</div>'
                     f'<a href="{link}"><button type="button" style="width:100%;margin-top:10px">Get {html.escape(p["name"])}</button></a></div>')
    ok, why = trial_available(name, ip)
    if st == "member" and u.get("plan") == "free pass":
        head = f'<div class="msg">Your free pass runs until <b>{_fmt(until)}</b>. <a href="/">Back to the dashboard</a></div>'
    elif st == "member":
        head = f'<div class="msg">You are a member until <b>{_fmt(until)}</b>. <a href="/">Back to the dashboard</a></div>'
    elif st == "open":
        head = '<div class="msg">Your access has no end date. <a href="/">Back to the dashboard</a></div>'
    else:
        head = '<div class="msg err">Your access has ended. Pick a plan to keep going.</div>'
    trial = ""
    if st == "expired":
        trial = (f'<h2>Try it first</h2><p>One free {TRIAL_HOURS}-hour pass per person.</p><form method="post" action="/trial"><button type="submit">Start my free {TRIAL_HOURS}-hour pass</button></form>'
                 if ok else f'<h2>Try it first</h2><p style="color:#8b949e">{html.escape(why)}</p>')
    portal = (f'<p style="font-size:14px"><a href="{html.escape(pl["portal_url"])}">Manage or cancel a membership</a></p>' if pl.get("portal_url") else "")
    body = (f'<h1>Memberships</h1>{_msg(msg, err)}{head}<h2>Pick a plan</h2>'
            f'<div style="display:flex;gap:12px;flex-wrap:wrap">{"".join(cards)}</div>'
            '<p style="font-size:14px;color:#9da7b3;margin-top:12px">Every plan renews automatically at the same price when its time is up, until you cancel. Payment is handled by Stripe - we never see or keep your card.</p>'
            f'{portal}{trial}<p style="margin-top:20px"><a href="/logout">Sign out</a> &nbsp; <a href="/terms">Terms</a></p>' + _tiny())
    return _page("Memberships", body, wide=True)
