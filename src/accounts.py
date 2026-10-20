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
USERS_FILE = os.path.join(ROOT, "data_cache", "users.json")
SECRET_FILE = os.path.join(ROOT, "data_cache", "session_secret.txt")
REQ_FILE = os.path.join(ROOT, "data_cache", "signup_requests.json")
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
        created = users.get(name, {}).get("created", time.time())
        users[name] = {"salt": salt, "hash": _hash(pw, salt), "created": created}
        _save(users)
    return True, "Saved."


def delete_user(name):
    with _mu:
        users = dict(_load())
        gone = users.pop((name or "").lower(), None) is not None
        if gone:
            _save(users)
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
        users[name] = {"salt": r["salt"], "hash": r["hash"], "created": time.time(), "agreed_terms": r.get("agreed_terms", ""), "agreed_at": r.get("agreed_at")}
        _save(users)
        _save_reqs(reqs)
    return True


def deny_request(name):
    name = (name or "").strip().lower()
    with _mu:
        reqs = _load_reqs()
        gone = reqs.pop(name, None) is not None
        if gone:
            _save_reqs(reqs)
    return gone


def is_pending(name):
    with _mu:
        return (name or "").strip().lower() in _load_reqs()
