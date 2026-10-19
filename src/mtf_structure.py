"""Multi-timeframe structure + bias + pattern memory - INFORMATION ONLY (never used for buying). Added 2026-10-05.

Timeframes: 1m, 5m, 15m, 1h, 3h, 6h, 12h, 1d, 1w, 1M, 1Y, overall. Short ones come from the live tick stream (CandleBook);
15m/1h/6h/1d come from Coinbase REST (cached), the rest are aggregated from those. Each timeframe gets the pattern detector's
labels plus a bullish / neutral / bearish call.

LEARNING: every non-neutral pattern call is stored with its price; after a timeframe-scaled horizon the move is scored
hit / miss / flat. Success rates per (pattern, timeframe, coin) are shown next to the label and also scale how much each
pattern counts toward that timeframe's bias (unproven patterns count half; patterns that work <= 50% count nothing)."""
import csv
import datetime as dt
import json
import math
import os
import threading
import time
import urllib.request
from zoneinfo import ZoneInfo

from src.pattern_detector import CandleBook, _structure, _candle_patterns, _atr, _L, FailTracker, TF_SECONDS
from src.pattern_info import explain

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA = os.path.join(ROOT, "data_cache")
STATS_FILE = os.path.join(ROOT, "bot_state", "pattern_stats.json")
LOG_FILE = os.path.join(ROOT, "bot_state", "pattern_log.csv")
EVENTS_FILE = os.path.join(ROOT, "bot_state", "pattern_events.csv")   # permanent record of every pattern completion / failure / outcome
REVIEWS_FILE = os.path.join(ROOT, "bot_state", "pattern_reviews.csv")  # per event: the pattern's projected target, best price reached, final price
ET = ZoneInfo("America/New_York")
CB = "https://api.exchange.coinbase.com"

TFS = ["1m", "5m", "15m", "1h", "3h", "6h", "12h", "1d", "1w", "1M", "1Y", "overall"]
TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "3h": 180, "6h": 360, "12h": 720, "1d": 1440, "1w": 10080, "1M": 43200, "1Y": 525600}
# how long after a pattern call its result is scored (minutes) - the first one is the main number shown
HORIZONS = {tf: (max(5, TF_MIN[tf]), max(15, 3 * TF_MIN[tf])) for tf in TF_MIN}
HIT_ATR = 0.25
MIN_N = 20   # decisive results needed before a pattern's success rate changes its weight


_tls = threading.local()


def _get_json(url, timeout=15):
    """Keep-alive session per thread - a new TLS handshake + CA-bundle load per candle request was costing real CPU."""
    import requests
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "kalshi-dashboard"})
        _tls.s = s
    try:
        r = s.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        _tls.s = None
        raise


def fetch_candles(product, gran, start=None, end=None):
    q = f"{CB}/products/{product}/candles?granularity={gran}"
    if not (start and end):
        # Coinbase's default (no start/end) answer can be cached and run 2-4 minutes behind (found 2026-10-10: the 1m chart
        # lagged); an explicit recent range is always fresh. 299 candles = just under their 300-per-request cap.
        now = int(time.time())
        fmt = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start, end = fmt(now - 299 * gran), fmt(now + gran)
    q += f"&start={start}&end={end}"
    data = _get_json(q)
    return sorted([[int(t), float(o), float(h), float(l), float(c)] for t, l, h, o, c, _v in data])


def fetch_daily_history(product, max_pages=20):
    """All daily candles Coinbase has (cached on disk, refreshed every 6 hours)."""
    fn = os.path.join(DATA, f"mtf_{product}_1d.json")
    try:
        if os.path.exists(fn) and time.time() - os.path.getmtime(fn) < 6 * 3600:
            return json.load(open(fn))
    except (OSError, ValueError):
        pass
    out = {}
    end = dt.datetime.now(dt.timezone.utc)
    for _ in range(max_pages):
        start = end - dt.timedelta(days=299)
        try:
            chunk = fetch_candles(product, 86400, start.isoformat(), end.isoformat())
        except Exception:
            break
        if not chunk:
            break
        for c in chunk:
            out[c[0]] = c
        end = start
        time.sleep(0.25)
    cs = [out[k] for k in sorted(out)]
    if cs:
        try:
            os.makedirs(DATA, exist_ok=True)
            json.dump(cs, open(fn, "w"))
        except OSError:
            pass
    return cs


def _resample(cs, key_fn):
    out = {}
    for ts, o, h, l, c in cs:
        k = key_fn(ts)
        if k not in out:
            out[k] = [k, o, h, l, c]
        else:
            x = out[k]; x[2] = max(x[2], h); x[3] = min(x[3], l); x[4] = c
    return [out[k] for k in sorted(out)]


def resample_seconds(cs, secs):
    return _resample(cs, lambda ts: int(ts // secs) * secs)


def resample_calendar(cs, kind):
    def key(ts):
        d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
        if kind == "w":
            d = (d - dt.timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        elif kind == "M":
            d = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            d = d.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return int(d.timestamp())
    return _resample(cs, key)


# ------------------------------------------------------------------ pattern memory
def label_key(name):
    e = explain(name)
    return e["key"] if e else name.lower()[:40]


class PatternStats:
    """hist = back-filled from recorded history (walk-forward, no look-ahead); live = scored from calls made by the running dashboard."""

    def __init__(self):
        self.lock = threading.Lock()
        self.live = {}      # "asset|tf|key" -> {"hit","miss","flat"}
        self.hist = {}
        self.pending = []   # [{"id","asset","tf","key","bias","price","ts","atr","h":minutes,"main":bool}]
        self.last_call = {}  # "asset|tf|key" -> ts of the last logged call (cooldown)
        self.dirty = False
        try:
            d = json.load(open(STATS_FILE))
            self.live, self.hist, self.pending = d.get("live", {}), d.get("hist", {}), d.get("pending", [])
        except (OSError, ValueError):
            pass

    @staticmethod
    def _k(asset, tf, key): return f"{asset}|{tf}|{key}"

    def add_call(self, asset, tf, name, bias, price, atr, ts=None):
        if bias not in ("bullish", "bearish") or atr <= 0:
            return False
        ts = ts or time.time()
        key = label_key(name)
        k = self._k(asset, tf, key)
        with self.lock:
            if ts - self.last_call.get(k, 0) < HORIZONS[tf][0] * 60:
                return False
            self.last_call[k] = ts
            for i, h in enumerate(HORIZONS[tf]):
                self.pending.append({"asset": asset, "tf": tf, "key": key, "bias": bias, "price": price, "ts": ts,
                                     "atr": atr, "h": h, "main": i == 0})
            self.dirty = True
        try:
            new = not os.path.exists(LOG_FILE)
            with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["ts_utc", "asset", "tf", "pattern", "bias", "price"])
                w.writerow([dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds"), asset, tf, name, bias, price])
        except OSError:
            pass
        return True

    def resolve(self, prices, now=None):
        """prices: {asset: current price}. Scores every pending call whose horizon has passed."""
        now = now or time.time()
        with self.lock:
            keep = []
            for e in self.pending:
                due = e["ts"] + e["h"] * 60
                p = prices.get(e["asset"])
                if now < due or p is None:
                    if now - e["ts"] < 14 * 86400:
                        keep.append(e)
                    continue
                thr = HIT_ATR * e["atr"] * math.sqrt(max(1.0, e["h"] / TF_MIN[e["tf"]]))
                move = (p - e["price"]) * (1 if e["bias"] == "bullish" else -1)
                res = "hit" if move >= thr else ("miss" if move <= -thr else "flat")
                if e["main"]:
                    d = self.live.setdefault(self._k(e["asset"], e["tf"], e["key"]), {"hit": 0, "miss": 0, "flat": 0})
                    d[res] += 1
                self.dirty = True
            self.pending = keep

    def summary(self, asset, tf, name):
        key = label_key(name); k = self._k(asset, tf, key)
        lv = self.live.get(k, {"hit": 0, "miss": 0, "flat": 0}); hs = self.hist.get(k, {"hit": 0, "miss": 0, "flat": 0})
        hit = lv["hit"] + hs["hit"]; miss = lv["miss"] + hs["miss"]; flat = lv["flat"] + hs["flat"]
        dec = hit + miss
        return {"hit": hit, "miss": miss, "flat": flat, "decisive": dec, "rate": (hit / dec) if dec else None,
                "live_n": sum(lv.values()), "hist_n": sum(hs.values())}

    def weight(self, asset, tf, name):
        s = self.summary(asset, tf, name)
        if s["decisive"] < MIN_N:
            return 0.5
        return max(0.0, min(1.5, 2 * (s["rate"] - 0.5) * 3))

    def save(self):
        if not self.dirty:
            return
        with self.lock:
            snap = {"live": self.live, "hist": self.hist, "pending": self.pending[-3000:]}
            self.dirty = False
        try:
            tmp = STATS_FILE + ".tmp"
            json.dump(snap, open(tmp, "w"))
            os.replace(tmp, STATS_FILE)
        except OSError:
            pass


# ------------------------------------------------------------------ bias
_STATUS_W = {"confirmed": 2.0, "forming": 1.0, "formed": 0.6}
_FAMILY_W = {"chart": 1.0, "smc": 0.8, "structure": 1.0, "candle": 0.4}


def _ema(vals, n):
    k = 2 / (n + 1); e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def _trend_labels(cs, tf):
    out = []
    n = len(cs)
    if n < 4:
        return out
    closes = [c[4] for c in cs]
    run, d0 = 0, 0
    for i in range(n - 1, 0, -1):
        d = 1 if closes[i] > closes[i - 1] else (-1 if closes[i] < closes[i - 1] else 0)
        if d == 0 or (d0 and d != d0):
            break
        d0 = d0 or d; run += 1
    if run >= 3 and d0:
        out.append(_L(f"{run} {'higher' if d0 > 0 else 'lower'} closes in a row", "trend", "bullish" if d0 > 0 else "bearish", "formed", closes[-1], tf))
    if n >= 8:
        m = max(3, min(20, n // 2)); ma = sum(closes[-m:]) / m
        prev = sum(closes[-m - 1:-1]) / m
        above = closes[-1] > ma
        out.append(_L(f"price {'above' if above else 'below'} its {m}-period moving average ({'rising' if ma > prev else 'falling'})",
                      "trend", ("bullish" if above and ma >= prev else "bearish" if (not above) and ma <= prev else "neutral"), "formed", closes[-1], tf))
    if n >= 10:
        hi = max(c[2] for c in cs)
        dd = (hi - closes[-1]) / hi if hi else 0
        if dd >= 0.10:
            out.append(_L(f"drawdown: {dd:.0%} below the highest price in this data", "trend", "bearish" if dd >= 0.4 else "neutral", "formed", closes[-1], tf))
    return out


def assess(cs, tf, asset, stats):
    """-> {"bias","score","n","labels":[...]} for one timeframe's candle list (oldest first, last may be forming)."""
    n = len(cs)
    res = {"bias": "neutral", "score": 0.0, "n": n, "labels": []}
    if n < 6:
        res["note"] = "not enough history"
        return res
    labels = []
    try:
        if n >= 25:
            window = cs[-90:]
            labels += _structure(window, tf) + _candle_patterns(window, tf)
        labels += _trend_labels(cs, tf)
    except Exception:
        pass
    score = 0.0
    closes = [c[4] for c in cs]
    if n >= 25:
        f, s = _ema(closes[-60:], 8), _ema(closes[-60:], 21)
        if closes[-1] > f > s: score += 1.5
        elif closes[-1] < f < s: score -= 1.5
    for l in labels:
        sgn = 1 if l["bias"] == "bullish" else (-1 if l["bias"] == "bearish" else 0)
        if not sgn:
            continue
        w = _STATUS_W.get(l["status"], 0.5) * _FAMILY_W.get(l["family"], 0.6)
        if l["family"] != "trend":
            w *= stats.weight(asset, tf, l["name"]) if stats else 0.5
        score += sgn * w
    res["score"] = round(score, 2)
    res["bias"] = "bullish" if score >= 1.5 else ("bearish" if score <= -1.5 else "neutral")
    order = {"confirmed": 0, "forming": 1, "formed": 2}
    pri = {"chart": 0, "smc": 1, "structure": 2, "trend": 3, "candle": 4}
    seen, uniq = set(), []
    for l in sorted(labels, key=lambda l: (order.get(l["status"], 3), pri.get(l["family"], 5))):
        if l["name"] not in seen:
            seen.add(l["name"]); uniq.append(l)
    res["labels"] = uniq[:6]
    res["all_labels"] = labels        # EVERYTHING detected, not name-deduped and not cut to 6 - the history records all of it
    res["atr"] = _atr(cs[:-1]) if n > 2 else 0.0
    return res


# ------------------------------------------------------------------ pattern history (what completed, when, and how it worked)
def _pkey(name):
    import re
    return re.sub(r"\s+", " ", re.sub(r"[0-9][0-9.,]*%?", "#", name.lower())).strip()


class PatternHistory:
    """Remembers every pattern that COMPLETED (formed / confirmed) with the time it did, every one that fell apart before
    finishing, and - for the bullish / bearish ones - whether it worked once its normal horizon passed (same hit / miss / flat
    rule as the success rates). Saved to bot_state/pattern_events.csv (append-only, kept forever) and reloaded on start, so the
    history survives restarts. Information only."""
    KEEP_SECONDS = 3 * 86400
    SIGNALS = ("price at a bullish fair value gap", "price at a bearish fair value gap", "retesting a ", "pullback at the", "bounce at the")
    GRACE_STEPS = 3          # a label must be missing this many updates in a row before it counts as gone (the table list flickers)

    def __init__(self):
        self.lock = threading.Lock()
        self.events = []         # oldest first
        self.by_id = {}
        self.active = {}         # (asset, tf, pkey) -> {"first","name","completed","carried","pct","family","bias","status","miss"}
        self.last_emit = {}      # (asset, tf, pkey, kind) -> ts   (cooldown against flicker)
        self.pending = []        # directional completions waiting for their horizon
        self.last_by_key = {}    # (asset, tf, pkey) -> the most recent 'complete' event for that pattern
        self.start = time.time()
        self._seq = 0
        self._load()

    # ---------------------------------------------------------------- persistence
    def _append(self, row):
        try:
            os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
            new = not os.path.exists(EVENTS_FILE)
            with open(EVENTS_FILE, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["ts_utc", "event", "asset", "tf", "name", "bias", "status", "family", "price", "atr", "result", "move_atr", "event_id"])
                w.writerow(row)
        except OSError:
            pass

    def _append_review(self, eid, target, best, final, reached):
        try:
            os.makedirs(os.path.dirname(REVIEWS_FILE), exist_ok=True)
            new = not os.path.exists(REVIEWS_FILE)
            with open(REVIEWS_FILE, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["event_id", "target", "best", "final", "reached"])
                w.writerow([eid, "" if target is None else target, "" if best is None else best, "" if final is None else final,
                            "" if reached is None else int(reached)])
        except OSError:
            pass

    def _load(self):
        old_latest = {}          # completions of the daily-and-longer timeframes older than the 3 days kept in memory (restore only)
        try:
            cutoff = time.time() - self.KEEP_SECONDS
            with open(EVENTS_FILE, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        ts = dt.datetime.fromisoformat(r["ts_utc"]).timestamp()
                    except (ValueError, KeyError):
                        continue
                    if r["event"] == "complete" and ts < cutoff and r["tf"] in ("1d", "1w", "1M", "1Y") and ts >= time.time() - 60 * 86400:
                        old_latest[(r["asset"], r["tf"], _pkey(r["name"]))] = {
                            "ts": ts, "name": r["name"], "family": r["family"], "bias": r["bias"], "status": r["status"], "tf": r["tf"], "kind": "complete"}
                        continue
                    if r["event"] in ("complete", "failed") and ts >= cutoff:
                        ev = {"id": r["event_id"], "ts": ts, "asset": r["asset"], "tf": r["tf"], "name": r["name"], "bias": r["bias"],
                              "status": r["status"], "family": r["family"], "kind": r["event"], "price": float(r["price"] or 0),
                              "atr": float(r["atr"] or 0), "result": None, "move": None, "pk": _pkey(r["name"]),
                              "target": None, "best": None, "final": None, "reached": None}
                        self.events.append(ev); self.by_id[ev["id"]] = ev
                        if ev["kind"] == "complete":
                            self.last_by_key[(ev["asset"], ev["tf"], ev["pk"])] = ev
                        if ev["kind"] == "complete" and ev["bias"] in ("bullish", "bearish") and ev["atr"] > 0 and ev["tf"] in HORIZONS:
                            self.pending.append(ev["id"])
                    elif r["event"] == "result" and r["event_id"] in self.by_id:
                        ev = self.by_id[r["event_id"]]
                        ev["result"] = r["result"]; ev["move"] = float(r["move_atr"] or 0)
                        if ev["id"] in self.pending:
                            self.pending.remove(ev["id"])
        except (OSError, ValueError):
            pass
        try:
            with open(REVIEWS_FILE, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    ev = self.by_id.get(r.get("event_id"))
                    if ev is None:
                        continue
                    for k_ in ("target", "best", "final"):
                        if r.get(k_) not in (None, ""):
                            ev[k_] = float(r[k_])
                    if r.get("reached") not in (None, ""):
                        ev["reached"] = bool(int(r["reached"]))
        except (OSError, ValueError):
            pass
        # a pattern still standing after a restart keeps its REAL completion time (the file's latest 'complete' for it), as long as that
        # was recent for its timeframe; older than that we cannot tell whether it is the same pattern, so it shows 'standing since'
        latest = dict(old_latest)
        for ev in self.events:
            if ev["kind"] == "complete":
                latest[(ev["asset"], ev["tf"], _pkey(ev["name"]))] = ev
        now = time.time()
        for key, ev in latest.items():
            if ev["ts"] >= now - max(1800, 6 * TF_MIN.get(ev["tf"], 1) * 60):
                self.active[key] = {"first": ev["ts"], "name": ev["name"], "completed": ev["ts"], "carried": False, "pct": 1.0,
                                    "family": ev["family"], "bias": ev["bias"], "status": ev["status"], "miss": 0}

    # ---------------------------------------------------------------- recording
    def _emit(self, kind, asset, tf, pk, info, price, atr, ts):
        ck = (asset, tf, pk, kind)
        if ts - self.last_emit.get(ck, 0) < max(60, TF_MIN.get(tf, 1) * 60):
            return None
        self.last_emit[ck] = ts
        self._seq += 1
        eid = "%d-%d" % (int(ts * 1000), self._seq)
        ev = {"id": eid, "ts": ts, "asset": asset, "tf": tf, "name": info["name"], "bias": info["bias"], "status": info["status"],
              "family": info["family"], "kind": kind, "price": price or 0.0, "atr": atr or 0.0, "result": None, "move": None, "pk": pk,
              "target": None, "best": None, "final": None, "reached": None}
        self.events.append(ev); self.by_id[eid] = ev
        if kind == "complete":
            self.last_by_key[(asset, tf, pk)] = ev
        if kind == "complete" and ev["bias"] in ("bullish", "bearish") and ev["atr"] > 0 and price and tf in HORIZONS:
            self.pending.append(eid)
        self._append([dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds"), kind, asset, tf, ev["name"], ev["bias"],
                      ev["status"], ev["family"], ev["price"], ev["atr"], "", "", eid])
        return ev

    def update(self, asset, tf, labels, price, atr, now, candle_close=None, cs=None):
        """labels: every label detected for this asset + timeframe right now (not cut down)."""
        warm = now - self.start < 25          # patterns already standing when the dashboard starts are not 'new completions'
        cur = {}
        rank = {"forming": 0, "formed": 1, "confirmed": 2}
        for l in labels:
            if l["family"] == "trend":
                continue
            k_ = _pkey(l["name"])
            if k_ not in cur or rank.get(l["status"], 0) >= rank.get(cur[k_]["status"], 0):
                cur[k_] = l          # the same pattern can be listed as forming (live candle) AND formed (closed candle): keep the completed one
        with self.lock:
            for pk, l in cur.items():
                key = (asset, tf, pk)
                a = self.active.get(key)
                if a is None:
                    a = {"first": now, "name": l["name"], "completed": None, "carried": False, "pct": 0.0, "family": l["family"],
                         "bias": l["bias"], "status": l["status"], "miss": 0}
                    self.active[key] = a
                    if warm and l["status"] in ("formed", "confirmed"):
                        a["completed"], a["carried"] = now, True
                a.update({"name": l["name"], "bias": l["bias"], "status": l["status"], "miss": 0})
                if l.get("pct"):
                    a["pct"] = l["pct"]
                if (l["status"] == "forming" and a["completed"] is None and now - a["first"] >= 10
                        and l["name"].lower().startswith(self.SIGNALS)):
                    # a signal that only ever exists while 'forming' (gap / order-block retest, Fibonacci touch) - log it once it has held 10s
                    a["completed"] = now
                    self._emit("complete", asset, tf, pk, dict(a, status="formed"), price, atr, now)
                if l["status"] in ("formed", "confirmed") and a["completed"] is None:
                    t_done = now
                    if (l["family"] == "candle" or l["name"].startswith("LIQUIDITY SWEEP")) and candle_close and 0 <= now - candle_close < 600:
                        t_done = candle_close                    # a candle shape is complete the moment its candle closes
                    a["completed"] = t_done
                    ev_ = self._emit("complete", asset, tf, pk, a, price, atr, t_done)
                    if ev_ is not None and cs and ev_["family"] in ("chart", "structure", "smc") and ev_["bias"] in ("bullish", "bearish"):
                        try:        # the pattern's own projected target (measured move) - drawn on the chart by the overlay code
                            from src.pattern_overlay import drawings_for
                            for d_ in drawings_for([list(x) for x in cs[-90:]], {"name": ev_["name"], "family": ev_["family"], "bias": ev_["bias"], "status": ev_["status"]}):
                                if d_.get("t") == "proj":
                                    tg_ = float(d_["p"])
                                    ahead = (tg_ > ev_["price"]) if ev_["bias"] == "bullish" else (tg_ < ev_["price"])
                                    if ahead and price:             # a target already behind the price says nothing about this call
                                        ev_["target"] = tg_
                                        self._append_review(ev_["id"], tg_, None, None, None)
                                    break
                        except Exception:
                            pass
            confirmed_fams = {x["family"] for x in cur.values() if x["status"] == "confirmed"}
            for key in [k for k in self.active if k[0] == asset and k[1] == tf and k[2] not in cur]:
                a = self.active[key]
                a["miss"] += 1
                if a["miss"] < self.GRACE_STEPS and a["family"] not in confirmed_fams:
                    continue
                del self.active[key]
                # a 'forming' label that turned into its own CONFIRMED version did not fail - it finished
                if a["completed"] is None and not a["carried"] and a["family"] not in confirmed_fams and now - a["first"] >= 8 and a["pct"] >= 0.35:
                    self._emit("failed", asset, tf, key[2], a, price, atr, now)
            if len(self.events) > 5000 and self.events[0]["ts"] < now - self.KEEP_SECONDS:
                cutoff = now - self.KEEP_SECONDS
                self.events = [e for e in self.events if e["ts"] >= cutoff]
                self.by_id = {e["id"]: e for e in self.events}

    def resolve(self, prices, now=None):
        """prices: {asset: price}. Scores every pending directional completion whose horizon has passed."""
        now = now or time.time()
        with self.lock:
            keep = []
            for eid in self.pending:
                ev = self.by_id.get(eid)
                if ev is None:
                    continue
                h = HORIZONS[ev["tf"]][0]
                p = prices.get(ev["asset"])
                if p is not None and ev["bias"] in ("bullish", "bearish"):
                    b0 = ev["best"] if ev["best"] is not None else ev["price"]
                    ev["best"] = max(b0, p) if ev["bias"] == "bullish" else min(b0, p)
                if now < ev["ts"] + h * 60 or p is None:
                    if now - ev["ts"] < 14 * 86400:
                        keep.append(eid)
                    continue
                thr = HIT_ATR * ev["atr"] * math.sqrt(max(1.0, h / TF_MIN[ev["tf"]]))
                move = (p - ev["price"]) * (1 if ev["bias"] == "bullish" else -1)
                ev["result"] = "hit" if move >= thr else ("miss" if move <= -thr else "flat")
                ev["move"] = move / ev["atr"]
                ev["final"] = p
                if ev["target"] is not None and ev["best"] is not None:
                    ev["reached"] = (ev["best"] >= ev["target"]) if ev["bias"] == "bullish" else (ev["best"] <= ev["target"])
                self._append_review(eid, ev["target"], ev["best"], p, ev["reached"])
                self._append([dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds"), "result", ev["asset"], ev["tf"],
                              ev["name"], ev["bias"], ev["status"], ev["family"], p, ev["atr"], ev["result"], "%.3f" % ev["move"], eid])
            self.pending = keep

    # ---------------------------------------------------------------- queries (used by the page)
    def completed_info(self, asset, tf, name):
        """(timestamp, carried) for the pattern currently standing, or None."""
        with self.lock:
            a = self.active.get((asset, tf, _pkey(name)))
            if a and a["completed"] is not None:
                return a["completed"], a["carried"]
        return None

    def latest_event(self, asset, tf, name):
        """The most recent completion of this pattern on this coin + timeframe (with its result once scored), or None."""
        return self.last_by_key.get((asset, tf, _pkey(name)))

    def recent_completed(self, asset, tf, since):
        """Completed (not failed) events for this coin + timeframe since `since`, newest first."""
        out = []
        with self.lock:
            for e in reversed(self.events):
                if e["ts"] < since:
                    break
                if e["asset"] == asset and e["tf"] == tf and e["kind"] == "complete":
                    out.append(e)
        return out

    def events_between(self, asset, start, end):
        with self.lock:
            out = [e for e in self.events if e["asset"] == asset and start <= e["ts"] < end]
        out.sort(key=lambda e: -e["ts"])
        return out

    def hour_index(self, asset, now=None, hours=24):
        """[{start, end, label, n, hit, miss, flat, pending, failed}] for the last `hours` Eastern clock hours, newest first."""
        now = now or time.time()
        from src import viewtz
        top = dt.datetime.fromtimestamp(now, viewtz.get()).replace(minute=0, second=0, microsecond=0)
        rows = []
        for h in range(hours):
            s = (top - dt.timedelta(hours=h))
            rows.append({"start": s.timestamp(), "end": (s + dt.timedelta(hours=1)).timestamp(), "label": s.strftime("%a %I %p").replace(" 0", " "),
                         "n": 0, "hit": 0, "miss": 0, "flat": 0, "pending": 0, "failed": 0})
        oldest = rows[-1]["start"]
        with self.lock:
            evs = [e for e in self.events if e["asset"] == asset and e["ts"] >= oldest]
        for e in evs:
            for r in rows:
                if r["start"] <= e["ts"] < r["end"]:
                    if e["kind"] == "failed":
                        r["failed"] += 1
                    else:
                        r["n"] += 1
                        if e["bias"] in ("bullish", "bearish"):
                            if e["result"] in ("hit", "miss", "flat"):
                                r[e["result"]] += 1
                            else:
                                r["pending"] += 1
                    break
        return rows


# ------------------------------------------------------------------ the monitor thread
class MTFMonitor:
    def __init__(self, products, tick_getter):
        self.products = list(products)           # e.g. ["BTC-USD", "XRP-USD"]
        self.tick_getter = tick_getter           # fn(symbol) -> [(ts, price), ...]
        self.books = {p: CandleBook() for p in self.products}
        self.raw = {p: {} for p in self.products}
        self.fetched_at = {}
        self.results = {}
        self.stats = PatternStats()
        self.windows = {}                        # symbol -> {tf: last 90 candles} for the live pattern window
        self.price = {}
        self.names = {}                          # symbol -> asset name for stats keys
        self._stop = False
        self.trackers = {}                       # (product, tf) -> FailTracker
        self.history = PatternHistory()          # what completed / failed / worked, with times (permanent log)

    def _refresh_remote(self, p):
        now = time.time()
        for tf, gran, every in (("1m", 60, 2), ("15m", 900, 120), ("1h", 3600, 300), ("6h", 21600, 900)):
            if now - self.fetched_at.get((p, tf), 0) >= every:
                try:
                    self.raw[p][tf] = fetch_candles(p, gran)
                    self.fetched_at[(p, tf)] = now
                except Exception:
                    self.fetched_at[(p, tf)] = now - every + min(30, every)     # retry in 30s (sooner for the 5s feed)
        if now - self.fetched_at.get((p, "1d"), 0) >= 3600:
            try:
                self.raw[p]["1d"] = fetch_daily_history(p)
            finally:
                self.fetched_at[(p, "1d")] = now

    def _series(self, p):
        r = self.raw[p]; b = self.books[p]
        # 1m / 5m come from Coinbase's OWN candles (refreshed every ~5s, forming candle included) so the colors and wicks match
        # what the exchange / TradingView shows. Blended Coinbase+Kraken ticks (the old source) sit $5-15 apart and flipped
        # small candles. The tick book only fills in minutes older than the Coinbase window.
        rest = r.get("1m", [])
        first = rest[0][0] if rest else None
        merged = {cd[0]: cd for cd in b.series(60, 360) if first is None or cd[0] < first}   # tick book only BEFORE Coinbase's window
        for cd in rest:
            merged[cd[0]] = cd
        one = [merged[k] for k in sorted(merged)]
        s = {"1m": one[-120:], "5m": resample_seconds(one, 300)[-90:]}
        s["15m"] = r.get("15m", []); s["1h"] = r.get("1h", []); s["6h"] = r.get("6h", []); s["1d"] = r.get("1d", [])
        s["3h"] = resample_seconds(s["1h"], 10800) if s["1h"] else []
        s["12h"] = resample_seconds(s["6h"], 43200) if s["6h"] else []
        s["1w"] = resample_calendar(s["1d"], "w") if s["1d"] else []
        s["1M"] = resample_calendar(s["1d"], "M") if s["1d"] else []
        s["1Y"] = resample_calendar(s["1d"], "Y") if s["1d"] else []
        s["overall"] = s["1M"]
        return s

    def step(self):
        for p in self.products:
            b = self.books[p]
            if not b.seeded:
                b.seed_coinbase(p)
            b.update_from_ticks(self.tick_getter(p))
            self._refresh_remote(p)
            if b.c:
                self.price[p] = b.c[max(b.c)][4]
            asset = self.names.get(p, p.split("-")[0])
            series = self._series(p)
            out = {}
            for tf in TFS:
                cs = series.get(tf) or []
                a = assess(cs, tf, asset, self.stats)
                try:       # permanent history of completions / failures (all labels, not just the 6 shown in the table)
                    self.history.update(asset, tf, a.get("all_labels", a["labels"]), self.price.get(p), a.get("atr", 0) or 0, time.time(),
                                        candle_close=(cs[-2][0] + TF_SECONDS.get(tf, 0)) if (len(cs) >= 2 and tf in TF_SECONDS) else None, cs=cs)
                except Exception as _he:
                    print(f"[history] update failed: {_he!r}")
                try:       # patterns that died before finishing show as FAILED for a few seconds instead of just vanishing
                    a["labels"] = a["labels"] + self.trackers.setdefault((p, tf), FailTracker()).update(a["labels"])
                except Exception:
                    pass
                if tf == "overall":
                    a["note"] = "whole price history, monthly candles"
                for l in a["labels"]:
                    l["stats"] = self.stats.summary(asset, tf, l["name"])
                    if l["status"] in ("forming", "confirmed", "formed") and l["bias"] in ("bullish", "bearish") and l["family"] != "trend" and TF_MIN.get(tf, 10**9) <= 1440:
                        self.stats.add_call(asset, tf, l["name"], l["bias"], l["price"], a.get("atr", 0) or 0)
                out[tf] = a
            self.results[p] = out
            now_ = time.time(); px_ = self.price.get(p); win = {}
            for tf in ("1m", "5m", "15m", "1h", "3h", "6h", "12h", "1d", "1M"):
                cs_ = list((series.get(tf) or [])[-90:])
                if cs_ and px_ and tf not in ("1m", "5m"):       # slow timeframes: keep the forming candle moving with the live price
                    ts_, o_, h_, l_, c_ = cs_[-1]
                    if 0 <= now_ - ts_ < TF_SECONDS.get(tf, 0) * 1.05 + 5:
                        cs_[-1] = [ts_, o_, max(h_, px_), min(l_, px_), px_]
                win[tf] = cs_
            self.windows[p] = win
        _px = {self.names.get(p, p.split("-")[0]): v for p, v in self.price.items()}
        self.stats.resolve(_px)
        try:
            self.history.resolve(_px)
        except Exception as _he:
            print(f"[history] resolve failed: {_he!r}")
        self.stats.save()

    def run_forever(self):
        while not self._stop:
            try:
                self.step()
            except Exception as e:  # noqa: BLE001 - an information-only feature must never take the dashboard down
                print(f"[MTF] step failed: {e!r}")
            time.sleep(5)

    def snapshot(self, product):
        return self.results.get(product)
