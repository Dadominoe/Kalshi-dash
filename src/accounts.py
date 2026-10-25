"""Individual logins for a HOSTED copy of the dashboard.

Everybody gets their own user name and password. The owner signs in as  admin  with the DASH_PASSWORD set on the host and uses /admin to add, remove or reset people.
Passwords are never stored - only a salted PBKDF2 hash - in data_cache/users.json (the hosted volume). Display / access only: nothing here touches trading.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_VOL = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("DATA_VOLUME")
_DATA = os.path.join(_VOL, "data_cache") if _VOL else os.path.join(ROOT, "data_cache")       # people go straight onto the Volume (no reliance on a folder link)
USERS_FILE = os.path.join(_DATA, "users.json")
SECRET_FILE = os.path.join(_DATA, "session_secret.txt")
REQ_FILE = os.path.join(_DATA, "signup_requests.json")
LOG_FILE = os.path.join(_DATA, "people_log.jsonl")   # every ask / approve / deny / remove, one line each, never erased
SESSION_DAYS = 30
ITER = 200_000
NAME_RE = re.compile(r"^[a-z0-9_.-]{2,24}$")
_mu = threading.Lock()
_cache = {"mtime": None, "users": {}}
_fails = {}                      # ip -> [count, first_fail_time, locked_until]
last_seen = {}                   # user -> unix time of their latest request


def _load():
    try:
        m = os.path.getmtime(USERS_FILE)
    except OSError:
        return {}
    if _cache["mtime"] != m:
        try:
            with open(USERS_FILE, encoding="utf-8") as f:
                _cache["users"] = json.load(f)
        except Exception:
            _cache["users"] = {}
        _cache["mtime"] = m
    return _cache["users"]


def _save(users):
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=1)
    os.replace(tmp, USERS_FILE)
    _cache["mtime"] = None


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), ITER).hex()


def list_users():
    with _mu:
        return {k: dict(v) for k, v in _load().items()}


def valid_name(name):
    return bool(NAME_RE.match(name or "")) and name != "admin"


def add_user(name, pw):
    """(ok, message). Also used to reset a password (the old sessions stop working because the salt changes)."""
    name = (name or "").strip().lower()
    if not valid_name(name):
        return False, "User name: 2-24 letters, numbers, dot, dash or underscore (and not 'admin')."
    if len(pw or "") < 8:
        return False, "Password must be at least 8 characters."
    salt = secrets.token_hex(16)
    with _mu:
        users = dict(_load())
        old = users.get(name, {})
        users[name] = {**old, "salt": salt, "hash": _hash(pw, salt), "created": old.get("created", time.time())}
        _save(users)
    _log("password changed" if old else "added by owner", name, discord=old.get("note"))
    return True, "Saved."


def delete_user(name):
    with _mu:
        users = dict(_load())
        u = users.pop((name or "").lower(), None)
        gone = u is not None
        if gone:
            _save(users)
    if gone:
        _log("removed", (name or "").lower(), discord=u.get("note"), approved_at=u.get("created"))
    last_seen.pop((name or "").lower(), None)
    return gone


def verify(name, pw):
    name = (name or "").strip().lower()
    with _mu:
        u = _load().get(name)
    if not u:
        _hash("x", "00" * 16)                        # same work either way, so a wrong name takes as long as a wrong password
        return False
    return hmac.compare_digest(_hash(pw or "", u["salt"]), u["hash"])


def _secret():
    try:
        with open(SECRET_FILE, encoding="utf-8") as f:
            s = f.read().strip()
            if len(s) >= 32:
                return s.encode()
    except OSError:
        pass
    s = secrets.token_hex(32)
    os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(s)
    return s.encode()


_SECRET = None


def _sig(payload):
    global _SECRET
    if _SECRET is None:
        _SECRET = _secret()
    return hmac.new(_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:40]


def make_session(name, master_fingerprint=""):
    """Cookie value. A user's token stops working when they are removed or their password is reset (their salt is in it); the admin's stops when DASH_PASSWORD changes."""
    if name == "admin":
        tag = hashlib.sha256(master_fingerprint.encode("utf-8")).hexdigest()[:8]
    else:
        tag = (_load().get(name) or {}).get("salt", "")[:8]
    payload = f"{name}|{int(time.time()) + SESSION_DAYS * 86400}|{tag}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode().rstrip("=") + "." + _sig(payload)


def check_session(token, master_fingerprint=""):
    """The user name the cookie belongs to, or None."""
    try:
        b, sig = (token or "").split(".", 1)
        payload = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8")
        if not hmac.compare_digest(sig, _sig(payload)):
            return None
        name, exp, tag = payload.split("|")
        if int(exp) < time.time():
            return None
        if name == "admin":
            return name if tag == hashlib.sha256(master_fingerprint.encode("utf-8")).hexdigest()[:8] else None
        with _mu:
            u = _load().get(name)
        return name if u and u["salt"][:8] == tag else None
    except Exception:
        return None


def throttled(ip):
    """Seconds left on a lock-out for this address (0 if free to try)."""
    f = _fails.get(ip)
    return max(0, int(f[2] - time.time())) if f and f[2] > time.time() else 0


def note_fail(ip):
    now = time.time()
    f = _fails.get(ip)
    if not f or now - f[1] > 900:
        f = [0, now, 0]
    f[0] += 1
    if f[0] >= 6:
        f[2] = now + 300                              # 6 wrong tries in 15 minutes -> 5 minutes locked out
        f[0] = 0
        f[1] = now
    _fails[ip] = f
    if len(_fails) > 2000:
        _fails.clear()


def note_ok(ip):
    _fails.pop(ip, None)


# ---- sign-up requests: a person asks for an account and chooses their own password; nothing works until the owner approves
_signup_hits = {}                # ip -> [unix times of recent requests]


def _load_reqs():
    try:
        with open(REQ_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_reqs(r):
    os.makedirs(os.path.dirname(REQ_FILE), exist_ok=True)
    tmp = REQ_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(r, f, indent=1)
    os.replace(tmp, REQ_FILE)


def list_requests():
    with _mu:
        return {k: dict(v) for k, v in _load_reqs().items()}


def request_signup(name, pw, note, ip, terms=""):
    """(ok, message). At most 3 requests per address per hour and 100 waiting in all."""
    name = (name or "").strip().lower()
    if not valid_name(name):
        return False, "User name: 2-24 letters, numbers, dot, dash or underscore."
    if len(pw or "") < 8:
        return False, "Password must be at least 8 characters."
    now = time.time()
    hits = [t for t in _signup_hits.get(ip, []) if now - t < 3600]
    if len(hits) >= 3:
        return False, "Too many requests from here. Try again in an hour."
    with _mu:
        reqs = _load_reqs()
        if name in _load() or name in reqs:
            return False, "That user name is taken. Pick another."
        if len(reqs) >= 100:
            return False, "Too many requests are waiting right now. Please try later."
        salt = secrets.token_hex(16)
        reqs[name] = {"salt": salt, "hash": _hash(pw, salt), "note": (note or "").strip()[:200], "time": now, "agreed_terms": terms, "agreed_at": now}
        _save_reqs(reqs)
    _log("asked", name, discord=(note or "").strip()[:200], terms=terms)
    _signup_hits[ip] = hits + [now]
    if len(_signup_hits) > 2000:
        _signup_hits.clear()
    return True, "Sent."


def approve_request(name):
    name = (name or "").strip().lower()
    with _mu:
        reqs = _load_reqs()
        r = reqs.pop(name, None)
        if not r or name in _load():
            return False
        users = dict(_load())
        users[name] = {"salt": r["salt"], "hash": r["hash"], "note": r.get("note", ""), "created": time.time(), "asked_at": r.get("time"),
                       "agreed_terms": r.get("agreed_terms", ""), "agreed_at": r.get("agreed_at")}
        _save(users)
        _save_reqs(reqs)
    _log("approved", name, discord=r.get("note"), asked_at=r.get("time"), terms=r.get("agreed_terms"), agreed_at=r.get("agreed_at"))
    return True


def deny_request(name):
    name = (name or "").strip().lower()
    with _mu:
        reqs = _load_reqs()
        r = reqs.pop(name, None)
        gone = r is not None
        if gone:
            _save_reqs(reqs)
    if gone:
        _log("denied", name, discord=r.get("note"), asked_at=r.get("time"))
    return gone


def is_pending(name):
    with _mu:
        return (name or "").strip().lower() in _load_reqs()


# ---- accounts made on the spot + paid-membership fields (only used when the host sets MEMBERSHIP_ON=1; see membership.py)
MEMBER_FIELDS = ("email", "member_until", "trial_used", "stripe_customer", "plan")


def set_fields(name, **kw):
    """Add or change fields on one person's record. Returns False if there is no such person."""
    name = (name or "").strip().lower()
    with _mu:
        users = dict(_load())
        u = users.get(name)
        if u is None:
            return False
        users[name] = {**u, **kw}
        _save(users)
    return True


def create_instant(name, pw, email, note, ip, terms=""):
    """(ok, message). A new person gets an account at once - no waiting for approval - with NO access time yet (member_until = 0): they start the free pass or pay."""
    name = (name or "").strip().lower()
    if not valid_name(name):
        return False, "User name: 2-24 letters, numbers, dot, dash or underscore."
    if len(pw or "") < 8:
        return False, "Password must be at least 8 characters."
    now = time.time()
    hits = [t for t in _signup_hits.get(ip, []) if now - t < 3600]
    if len(hits) >= 3:
        return False, "Too many accounts made from here. Try again in an hour."
    with _mu:
        if name in _load() or name in _load_reqs():
            return False, "That user name is taken. Pick another."
        salt = secrets.token_hex(16)
        users = dict(_load())
        users[name] = {"salt": salt, "hash": _hash(pw, salt), "note": (note or "").strip()[:200], "created": now, "asked_at": now, "agreed_terms": terms, "agreed_at": now,
                       "email": (email or "").strip().lower(), "member_until": 0, "trial_used": False}
        _save(users)
    _signup_hits[ip] = hits + [now]
    if len(_signup_hits) > 2000:
        _signup_hits.clear()
    _log("account created (instant)", name, discord=(note or "").strip()[:200], terms=terms)
    return True, "Created."


# ---- backup / restore (the owner can save a copy of who is approved, and put it back)
def export_all():
    return {"users": list_users(), "requests": list_requests(), "log": read_log(), "saved_at": time.time()}


def import_all(data):
    """Add back anybody in the backup who is not here now (what is here already wins). Returns how many were added."""
    added = 0
    ok_hex = re.compile(r"^[0-9a-f]{32,128}$")
    with _mu:
        users = dict(_load())
        for name, u in (data.get("users") or {}).items():
            if (isinstance(u, dict) and valid_name(name) and name not in users
                    and ok_hex.match(str(u.get("salt", ""))) and ok_hex.match(str(u.get("hash", "")))):
                users[name] = {"salt": u["salt"], "hash": u["hash"], "note": u.get("note", ""), "created": u.get("created", time.time()),
                               "agreed_terms": u.get("agreed_terms", ""), "agreed_at": u.get("agreed_at"), **{k: u[k] for k in MEMBER_FIELDS if k in u}}
                added += 1
        if added:
            _save(users)
        reqs = _load_reqs()
        radd = 0
        for name, u in (data.get("requests") or {}).items():
            if (isinstance(u, dict) and valid_name(name) and name not in users and name not in reqs
                    and ok_hex.match(str(u.get("salt", ""))) and ok_hex.match(str(u.get("hash", "")))):
                reqs[name] = {k: u.get(k) for k in ("salt", "hash", "note", "time", "agreed_terms", "agreed_at")}
                radd += 1
        if radd:
            _save_reqs(reqs)
    _merge_log(data.get("log") or [])
    return added, radd


# ---- the people log: a permanent record of who asked, who was approved / denied / removed, and when.
# Removing someone takes them out of users.json, but their lines here stay. It lives on the Volume and goes inside every backup.
_log_mu = threading.Lock()


def _seed_log_locked():
    """First time only: start the log with everybody already on file, so it is complete from day one."""
    if os.path.exists(LOG_FILE):
        return
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    lines = []
    for name, u in _load().items():
        lines.append({"time": u.get("created"), "event": "approved (before the log started)", "name": name,
                      "discord": u.get("note", ""), "terms": u.get("agreed_terms", ""), "agreed_at": u.get("agreed_at")})
    for name, r in _load_reqs().items():
        lines.append({"time": r.get("time"), "event": "asked (before the log started)", "name": name,
                      "discord": r.get("note", ""), "terms": r.get("agreed_terms", "")})
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        for rec in sorted(lines, key=lambda x: x.get("time") or 0):
            f.write(json.dumps({k: v for k, v in rec.items() if v not in (None, "")}) + "\n")


def _log(event, name, **info):
    rec = {"time": time.time(), "event": event, "name": name, **{k: v for k, v in info.items() if v not in (None, "")}}
    try:
        with _log_mu:
            _seed_log_locked()
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
    except OSError:
        pass                     # record-keeping must never break signing in


def read_log():
    out = []
    try:
        with _log_mu:
            _seed_log_locked()
            with open(LOG_FILE, encoding="utf-8") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def _merge_log(entries):
    """Restore: add back any log lines from a backup that are not here already."""
    current = read_log()
    have = {(e.get("time"), e.get("event"), e.get("name")) for e in current}
    new = [e for e in entries if isinstance(e, dict) and e.get("name") and (e.get("time"), e.get("event"), e.get("name")) not in have]
    if not new:
        return
    with _log_mu:
        tmp = LOG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in sorted(current + new, key=lambda x: x.get("time") or 0):
                f.write(json.dumps(rec) + "\n")
        os.replace(tmp, LOG_FILE)


def people_rows():
    """One row per person ever seen - approved, waiting, denied or removed - for the owner's spreadsheet. Times are unix seconds."""
    users, reqs = list_users(), list_requests()
    rows = {}
    for e in read_log():
        n = e.get("name")
        r = rows.setdefault(n, {"user_name": n})
        for src, dst in (("discord", "discord_name"), ("terms", "terms_agreed"), ("agreed_at", "terms_agreed_on")):
            if e.get(src):
                r[dst] = e[src]
        ev = e.get("event", "")
        if ev.startswith("asked"):
            r["asked_on"] = e.get("time")
        elif ev.startswith("approved") or ev == "added by owner":
            r["approved_on"] = e.get("time")
            r["asked_on"] = r.get("asked_on") or e.get("asked_at")
        elif ev == "denied":
            r["status"], r["denied_on"] = "denied", e.get("time")
        elif ev == "removed":
            r["status"], r["removed_on"] = "removed", e.get("time")
    for n, u in users.items():
        r = rows.setdefault(n, {"user_name": n})
        r.update(status="approved", discord_name=u.get("note") or r.get("discord_name", ""),
                 approved_on=u.get("created"), asked_on=u.get("asked_at") or r.get("asked_on"),
                 terms_agreed=u.get("agreed_terms") or r.get("terms_agreed", ""),
                 terms_agreed_on=u.get("agreed_at") or r.get("terms_agreed_on"), last_seen=last_seen.get(n))
        r.pop("removed_on", None)
    for n, q in reqs.items():
        r = rows.setdefault(n, {"user_name": n})
        r.update(status="waiting for approval", discord_name=q.get("note", ""), asked_on=q.get("time"),
                 terms_agreed=q.get("agreed_terms", ""))
    order = {"approved": 0, "waiting for approval": 1, "removed": 2, "denied": 3}
    return sorted(rows.values(), key=lambda r: (order.get(r.get("status"), 4), r.get("approved_on") or r.get("asked_on") or 0))


try:                             # start the people log as soon as the site boots, from whoever is already on file
    with _log_mu:
        _seed_log_locked()
except OSError:
    pass
