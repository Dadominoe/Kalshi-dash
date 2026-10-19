"""Consumer Price Index (CPI) readout for the dashboard - information only, never used for calls or trades.

Numbers come from the U.S. Bureau of Labor Statistics public data service (no key needed, small daily limit - results are cached on disk and
refreshed in a background thread, never from the page render). Release dates are the BLS schedule (all at 8:30 AM Eastern); update RELEASES
when the BLS publishes next year's schedule. 'expected' = the forecast (consensus) going into that release, where we have it.
"""
import datetime as dt
import json
import os
import threading
import time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache", "cpi_cache.json")
_BLS = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
_SERIES = {"sa": "CUSR0000SA0", "nsa": "CUUR0000SA0", "core_sa": "CUSR0000SA0L1E", "core_nsa": "CUUR0000SA0L1E"}

# (release date, reference month "YYYY-MM", forecast yoy %, forecast mom %)
RELEASES = [
    (dt.datetime(2026, 10, 14, 8, 30, tzinfo=ET), "2026-09", 3.6, 0.6),
    (dt.datetime(2026, 11, 10, 8, 30, tzinfo=ET), "2026-10", None, None),
    (dt.datetime(2026, 12, 10, 8, 30, tzinfo=ET), "2026-11", None, None),
    (dt.datetime(2027, 1, 13, 8, 30, tzinfo=ET), "2026-12", None, None),
]

_lock = threading.Lock()
_state = {"data": None, "fetched": 0.0, "thread": False}


def _month_name(ym):
    y, m = ym.split("-")
    return dt.date(int(y), int(m), 1).strftime("%B %Y")


def _load_disk():
    try:
        with open(_CACHE, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("series"), float(d.get("fetched", 0))
    except Exception:
        return None, 0.0


def _fetch():
    import requests
    yr = dt.datetime.now(ET).year
    r = requests.post(_BLS, json={"seriesid": list(_SERIES.values()), "startyear": str(yr - 1), "endyear": str(yr)}, timeout=25,
                      headers={"User-Agent": "Mozilla/5.0"})
    j = r.json()
    if j.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS said {j.get('status')}: {j.get('message')}")
    out = {}
    for s in j["Results"]["series"]:
        key = next(k for k, v in _SERIES.items() if v == s["seriesID"])
        out[key] = {}
        for x in s["data"]:
            try:                                   # some months are published as "-" (no data): skip them
                if x["period"].startswith("M") and x["period"] != "M13":
                    out[key][f"{x['year']}-{x['period'][1:]}"] = float(x["value"])
            except ValueError:
                pass
    return out


def _stale(series, fetched):
    """Refresh every 6 hours; every 5 minutes right after a release until its month shows up (the BLS limit is 25 requests a day)."""
    if not series:
        return time.time() - fetched > 300
    now = dt.datetime.now(ET)
    newest = max(series.get("sa", {}) or {"0": 0})
    for rel, ym, _a, _b in RELEASES:
        if rel <= now < rel + dt.timedelta(hours=6) and newest < ym:
            return time.time() - fetched > 300
    return time.time() - fetched > 6 * 3600


def _worker():
    while True:
        try:
            with _lock:
                series, fetched = _state["data"], _state["fetched"]
            if _stale(series, fetched):
                new = _fetch()
                with _lock:
                    _state["data"], _state["fetched"] = new, time.time()
                try:
                    os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
                    with open(_CACHE, "w", encoding="utf-8") as f:
                        json.dump({"series": new, "fetched": time.time()}, f)
                except Exception:
                    pass
        except Exception as e:                      # offline / limit hit: keep what we have, try again later
            with _lock:
                _state["fetched"] = max(_state["fetched"], time.time() - 6 * 3600 + 600)
            print(f"[cpi] refresh failed: {e!r}")
        time.sleep(60)


def _ensure_started():
    with _lock:
        if _state["thread"]:
            return
        _state["thread"] = True
        series, fetched = _load_disk()
        _state["data"], _state["fetched"] = series, fetched
    threading.Thread(target=_worker, daemon=True, name="cpi-refresh").start()


def _pct(a, b):
    return (a / b - 1) * 100 if a and b else None


def latest():
    """{'month','ym','yoy','mom','core_yoy','core_mom','expected_yoy','expected_mom','verdict','released'} or None (not loaded yet)."""
    _ensure_started()
    with _lock:
        s = _state["data"]
    if not s or not s.get("sa"):
        return None
    ym = max(s["sa"])
    y, m = int(ym[:4]), int(ym[5:])
    prev = f"{y - 1 if m == 1 else y}-{12 if m == 1 else m - 1:02d}"
    ago = f"{y - 1}-{m:02d}"
    g = lambda k, key: (s.get(k) or {}).get(key)
    out = {"ym": ym, "month": _month_name(ym),
           "yoy": _pct(g("nsa", ym), g("nsa", ago)), "mom": _pct(g("sa", ym), g("sa", prev)),
           "core_yoy": _pct(g("core_nsa", ym), g("core_nsa", ago)), "core_mom": _pct(g("core_sa", ym), g("core_sa", prev)),
           "expected_yoy": None, "expected_mom": None, "verdict": None, "released": None}
    for rel, rym, ey, em in RELEASES:
        if rym == ym:
            out["released"], out["expected_yoy"], out["expected_mom"] = rel, ey, em
            if ey is not None and out["yoy"] is not None:
                d = round(out["yoy"], 1) - ey
                out["verdict"] = "hotter" if d > 0.05 else ("cooler" if d < -0.05 else "in line")
    return out


def next_release(now=None):
    """(release datetime, reference month name) of the next release that has not happened yet, or None."""
    now = now or dt.datetime.now(ET)
    for rel, ym, _a, _b in RELEASES:
        if rel > now:
            return rel, _month_name(ym)
    return None


def last_release(now=None):
    now = now or dt.datetime.now(ET)
    best = None
    for rel, ym, _a, _b in RELEASES:
        if rel <= now:
            best = (rel, _month_name(ym))
    return best


def phase(now=None):
    """'before' (inside the 90 minutes ahead of a release), 'live' (first 45 minutes after one), or None."""
    now = now or dt.datetime.now(ET)
    for rel, ym, _a, _b in RELEASES:
        if rel - dt.timedelta(minutes=90) <= now < rel:
            return "before", rel
        if rel <= now < rel + dt.timedelta(minutes=45):
            return "live", rel
    return None, None


def release_times():
    return [r[0] for r in RELEASES]


_HIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache", "cpi_history_stats.json")
_hist_cache = {"mtime": 0, "data": None}


def history_stats(asset):
    """Summary of how `asset` behaved after past CPI releases (written by src.cpi_backtest), or None."""
    try:
        mt = os.path.getmtime(_HIST)
        if mt != _hist_cache["mtime"]:
            with open(_HIST, encoding="utf-8") as f:
                _hist_cache["data"], _hist_cache["mtime"] = json.load(f), mt
        return (_hist_cache["data"] or {}).get("summary", {}).get(asset)
    except Exception:
        return None


def levels(cs, release_ts):
    """The 'CPI range': high (ceiling) and low (floor) of the first 5 minutes after the release, then whether price has broken out of it.
    cs = 1-minute candles [ts, open, high, low, close], newest last (the last one may still be forming and never counts as a break)."""
    first = [c for c in cs if release_ts <= c[0] < release_ts + 300]
    if len(first) < 5 or cs[-1][0] < release_ts + 300:
        return {"complete": False}
    pre = first[0][1]
    H, L = max(c[2] for c in first), min(c[3] for c in first)
    h = H - L
    out = {"complete": True, "pre": pre, "ceiling": H, "floor": L, "height": h, "height_pct": h / pre * 100 if pre else 0,
           "break": None, "break_ts": None, "ext_x": 0.0, "back_inside": False, "now": cs[-1][4]}
    for c in cs[:-1]:
        if c[0] < release_ts + 300:
            continue
        side = "up" if c[4] > H else ("down" if c[4] < L else None)
        if not side:
            continue
        level = H if side == "up" else L
        after = [x for x in cs if x[0] >= c[0]]
        ext = (max(x[2] for x in after) - level) if side == "up" else (level - min(x[3] for x in after))
        out.update({"break": side, "break_ts": c[0], "ext_x": max(ext, 0) / h if h > 0 else 0.0,
                    "back_inside": any(x[0] > c[0] and ((x[4] <= H) if side == "up" else (x[4] >= L)) for x in cs[:-1]),
                    "reentered_15": any(c[0] < x[0] <= c[0] + 900 and ((x[4] <= H) if side == "up" else (x[4] >= L)) for x in cs[:-1])})
        break
    out["now_inside"] = L <= cs[-1][4] <= H
    return out
