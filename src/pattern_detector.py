"""Chart-pattern / market-structure labels - INFORMATION ONLY, never used for buying (user request 2026-10-05:
"just for information to go off of"). Catalog researched from Wikipedia's candlestick-pattern list, StockCharts
ChartSchool's chart-pattern list and the common Smart-Money-Concepts vocabulary (BOS, CHoCH, equal highs/lows,
liquidity sweeps, fair value gaps, order blocks). Not included (too subjective to code honestly): harmonic patterns
(Gartley/Bat/Crab...), diamond, bump-and-run, measured moves, island reversals.

Candles are [ts, o, h, l, c], oldest first, the LAST one may still be forming. Every rule is relative to ATR, so the
same code works for BTC and XRP. Output: list of {"name", "family", "bias", "status", "price", "tf", "pct"}; pct = how complete the pattern is (0..1)."""
import json
import re
import threading
import time
import urllib.request

try:
    import numpy as np
except ImportError:  # the fits below degrade to "no pattern" without numpy
    np = None

PIVOT_L = 2
MAX_LOOKBACK = 90


# ------------------------------------------------------------------ candle store
class CandleBook:
    """Rolling 1-minute candles per symbol, seeded from Coinbase REST at start and kept current from live ticks."""

    def __init__(self, keep_minutes=360):
        self.c = {}
        self.last_tick_ts = 0.0
        self.keep = keep_minutes * 60
        self.seeded = False

    def seed_coinbase(self, product):
        self.seeded = True
        try:
            req = urllib.request.Request(f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=60",
                                         headers={"User-Agent": "kalshi-dashboard"})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            for t, lo, hi, op, cl, _v in data:       # Coinbase: [time, low, high, open, close, volume]
                self.c[int(t)] = [int(t), float(op), float(hi), float(lo), float(cl)]
        except Exception:
            pass

    def update_from_ticks(self, ticks):
        for ts, p in ticks:
            if ts <= self.last_tick_ts:
                continue
            m = int(ts // 60) * 60
            b = self.c.get(m)
            if b is None:
                self.c[m] = [m, p, p, p, p]
            else:
                b[2] = max(b[2], p); b[3] = min(b[3], p); b[4] = p
            self.last_tick_ts = ts
        cutoff = (self.last_tick_ts or time.time()) - self.keep
        for k in [k for k in self.c if k < cutoff]:
            del self.c[k]

    def series(self, seconds=60, max_candles=MAX_LOOKBACK):
        keys = sorted(self.c)
        if seconds == 60:
            return [self.c[k] for k in keys][-max_candles:]
        out = {}
        for k in keys:
            b = int(k // seconds) * seconds
            o, h, l, cl = self.c[k][1:]
            if b not in out:
                out[b] = [b, o, h, l, cl]
            else:
                x = out[b]; x[2] = max(x[2], h); x[3] = min(x[3], l); x[4] = cl
        return [out[k] for k in sorted(out)][-max_candles:]


# ------------------------------------------------------------------ helpers
def _atr(cs, n=14):
    rs = [c[2] - c[3] for c in cs[-n:]]
    return (sum(rs) / len(rs)) if rs else 0.0


def _body(c): return abs(c[4] - c[1])
def _rng(c): return c[2] - c[3]
def _up(c): return c[4] > c[1]
def _down(c): return c[4] < c[1]
def _upper(c): return c[2] - max(c[1], c[4])
def _lower(c): return min(c[1], c[4]) - c[3]


def _pivots(closed):
    out = []
    for i in range(PIVOT_L, len(closed) - PIVOT_L):
        hi, lo = closed[i][2], closed[i][3]
        if all(hi > closed[j][2] for j in range(i - PIVOT_L, i)) and all(hi >= closed[j][2] for j in range(i + 1, i + PIVOT_L + 1)):
            out.append((i, hi, "H"))
        if all(lo < closed[j][3] for j in range(i - PIVOT_L, i)) and all(lo <= closed[j][3] for j in range(i + 1, i + PIVOT_L + 1)):
            out.append((i, lo, "L"))
    return sorted(out)


def _fit(points):
    """Least-squares line through [(x, y)...] -> (slope, intercept, mean_abs_error)."""
    n = len(points)
    mx = sum(p[0] for p in points) / n; my = sum(p[1] for p in points) / n
    den = sum((p[0] - mx) ** 2 for p in points)
    if den == 0:
        return 0.0, my, 0.0
    s = sum((p[0] - mx) * (p[1] - my) for p in points) / den
    b = my - s * mx
    return s, b, sum(abs(p[1] - (s * p[0] + b)) for p in points) / n


def _L(name, family, bias, status, price, tf, pct=None):
    """pct = how complete the pattern is, 0..1 (formed / confirmed are always 1.0; forming ones are measured by the detector)."""
    if status in ("formed", "confirmed"):
        pct = 1.0
    return {"name": name, "family": family, "bias": bias, "status": status, "price": price, "tf": tf,
            "pct": None if pct is None else max(0.0, min(1.0, float(pct)))}


TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "3h": 10800, "6h": 21600, "12h": 43200, "1d": 86400,
              "1w": 604800, "1M": 2592000, "1Y": 31536000}


def _elapsed(cs, tf):
    """How far through the CURRENT (still forming) candle we are, 0..1 - a candle-based pattern is only final when it closes."""
    secs = TF_SECONDS.get(tf)
    if not secs or not cs:
        return 0.5
    return max(0.0, min(1.0, (time.time() - cs[-1][0]) / secs))


def _fill_progress(labels, cs, tf):
    """Any forming label the detector did not measure specifically falls back to the candle-completion fraction."""
    e = None
    for l in labels:
        if l["status"] == "forming" and l.get("pct") is None:
            if e is None:
                e = _elapsed(cs, tf)
            l["pct"] = e
    return labels


def _pkey(name):
    return re.sub(r"\s+", " ", re.sub(r"[0-9][0-9.,]*%?", "#", name.lower())).strip()


class FailTracker:
    """Remembers the patterns that were forming so one that quietly dies (price moved against it before it finished)
    can be shown for a few seconds as FAILED instead of just vanishing.  One tracker per coin + timeframe."""
    GHOST_SECONDS = 25
    MIN_PCT = 0.35
    MIN_AGE = 8

    def __init__(self):
        self.seen = {}
        self.ghosts = {}
        self.last_update = 0.0
        self.lock = threading.Lock()

    def update(self, labels, now=None):
        now = now or time.time()
        with self.lock:
            if now - self.last_update > 10:         # we were not being fed (tab hidden / timeframe switched) - start clean
                self.seen.clear(); self.ghosts.clear()
            self.last_update = now
            if not labels:
                return list(l for _, l in self.ghosts.values())
            cur = {_pkey(l["name"]): l for l in labels}
            for k, l in cur.items():
                if l["status"] == "forming":
                    e = self.seen.setdefault(k, {"first": now, "l": l, "pct": 0.0})
                    e["l"] = l; e["pct"] = l.get("pct") or 0.0
            for k in list(self.seen):
                if k in cur and cur[k]["status"] == "forming":
                    continue
                e = self.seen.pop(k)
                done = k in cur or any(x["family"] == e["l"]["family"] and x["status"] == "confirmed" for x in labels)
                if not done and e["pct"] >= self.MIN_PCT and now - e["first"] >= self.MIN_AGE:
                    self.ghosts[k] = (now, dict(e["l"], status="failed", pct=0.0))
            out = []
            for k, (t, l) in list(self.ghosts.items()):
                if now - t > self.GHOST_SECONDS or (k in cur and cur[k]["status"] == "forming"):
                    del self.ghosts[k]
                else:
                    out.append(l)
            return out


# ------------------------------------------------------------------ candlestick patterns
def _single(c, up, down, st, tf, atr):
    r = _rng(c)
    if r <= 0:
        return []
    b, u, d = _body(c), _upper(c), _lower(c)
    out = []
    cl = c[4]
    if b >= 0.9 * r and b >= 0.8 * atr:
        out.append(_L("white marubozu (all buyers)" if _up(c) else "black marubozu (all sellers)", "candle", "bullish" if _up(c) else "bearish", st, cl, tf)); return out
    if b >= 1.5 * atr and b >= 0.6 * r:
        out.append(_L("big white candle" if _up(c) else "big black candle", "candle", "bullish" if _up(c) else "bearish", st, cl, tf))
    if b <= 0.1 * r:
        if u >= 0.6 * r and d <= 0.1 * r:
            # a tiny-bodied star at the top of a rise is BOTH: the textbook calls the shape a gravestone doji, traders call it a shooting star
            out.append(_L("gravestone doji", "candle", "bearish", st, cl, tf))
            if up:
                out.append(_L("shooting star", "candle", "bearish", st, cl, tf))
        elif d >= 0.6 * r and u <= 0.1 * r:
            out.append(_L("dragonfly doji", "candle", "bullish", st, cl, tf))
            if down:
                out.append(_L("hammer", "candle", "bullish", st, cl, tf))
        elif u >= 0.3 * r and d >= 0.3 * r:
            out.append(_L("long-legged doji", "candle", "neutral", st, cl, tf))
        else:
            out.append(_L("doji (indecision)", "candle", "neutral", st, cl, tf))
    elif b <= 0.4 * r:
        if u >= 2 * b and u >= 0.5 * r and d <= max(b, 0.15 * r):
            if up:
                out.append(_L("shooting star", "candle", "bearish", st, cl, tf))
            elif down:
                out.append(_L("inverted hammer", "candle", "bullish", st, cl, tf))
        elif d >= 2 * b and d >= 0.5 * r and u <= max(b, 0.15 * r):
            if down:
                out.append(_L("hammer", "candle", "bullish", st, cl, tf))
            elif up:
                out.append(_L("hanging man", "candle", "bearish", st, cl, tf))
        elif b <= 0.3 * r and u >= 0.25 * r and d >= 0.25 * r:
            out.append(_L("spinning top", "candle", "neutral", st, cl, tf))
    if not out:
        if u >= (2 / 3) * r:
            out.append(_L("long upper shadow (sellers rejected the high)", "candle", "bearish", st, cl, tf))
        elif d >= (2 / 3) * r:
            out.append(_L("long lower shadow (buyers rejected the low)", "candle", "bullish", st, cl, tf))
    return out


def _candle_patterns(cs, tf, max_age=1):
    out = []
    atr = _atr(cs[:-1]) or 1e-9
    n = len(cs)
    if n < 8:
        return out
    closed = cs[:-1]

    def drift(idx):
        a, z = cs[idx - 3][4], cs[idx - 1][4]
        return (z - a) > 0.8 * atr, (a - z) > 0.8 * atr
    def sdrift(idx):
        """Trend context for the single-candle shapes (star / hammer): price rose (fell) ~0.5 ATR over the 3 candles before it, OR
        this candle poked to a new 5-candle high (low). The old 'rose 0.8 ATR over 2 candles' almost never held on 1-minute data."""
        if idx < 5:
            return drift(idx)
        a, z = cs[idx - 4][4], cs[idx - 1][4]
        hi5 = max(x[2] for x in cs[idx - 5:idx]); lo5 = min(x[3] for x in cs[idx - 5:idx])
        up_, dn_ = ((z - a) > 0.5 * atr or cs[idx][2] >= hi5), ((a - z) > 0.5 * atr or cs[idx][3] <= lo5)
        return (False, False) if (up_ and dn_) else (up_, dn_)
    out += _single(cs[-1], *sdrift(n - 1), "forming", tf, atr)
    el = _elapsed(cs, tf)
    for l in out:
        l["pct"] = el
    out += _single(cs[-2], *sdrift(n - 2), "formed", tf, atr)
    for age in range(1, max_age):          # the pattern window keeps the last few candles' shapes in the list, tagged with their age
        idx = n - 2 - age
        if idx >= 5:
            for lab in _single(cs[idx], *sdrift(idx), "formed", tf, atr):
                lab["age"] = age
                out.append(lab)

    p, c = closed[-2], closed[-1]
    pb, cb = _body(p), _body(c)
    up_tr, down_tr = drift(n - 2)
    # two-candle
    if _down(p) and _up(c) and c[1] <= p[4] and c[4] >= p[1] and cb > pb:
        out.append(_L("bullish engulfing", "candle", "bullish", "formed", c[4], tf))
    if _up(p) and _down(c) and c[1] >= p[4] and c[4] <= p[1] and cb > pb:
        out.append(_L("bearish engulfing", "candle", "bearish", "formed", c[4], tf))
    if pb >= 0.9 * atr and cb <= 0.6 * pb and max(c[1], c[4]) <= max(p[1], p[4]) and min(c[1], c[4]) >= min(p[1], p[4]):
        cross = " cross" if cb <= 0.1 * _rng(c) else ""
        if _down(p) and down_tr:
            out.append(_L(f"bullish harami{cross}", "candle", "bullish", "formed", c[4], tf))
        elif _up(p) and up_tr:
            out.append(_L(f"bearish harami{cross}", "candle", "bearish", "formed", c[4], tf))
    if _down(p) and pb >= 0.9 * atr and _up(c) and c[1] <= p[4] and p[4] + 0.5 * pb <= c[4] < p[1]:
        out.append(_L("piercing line", "candle", "bullish", "formed", c[4], tf))
    if _up(p) and pb >= 0.9 * atr and _down(c) and c[1] >= p[4] and p[4] - 0.5 * pb >= c[4] > p[1]:
        out.append(_L("dark cloud cover", "candle", "bearish", "formed", c[4], tf))
    if abs(p[2] - c[2]) <= 0.1 * atr and _up(p) and _down(c):
        out.append(_L("tweezer top", "candle", "bearish", "formed", c[4], tf))
    if abs(p[3] - c[3]) <= 0.1 * atr and _down(p) and _up(c):
        out.append(_L("tweezer bottom", "candle", "bullish", "formed", c[4], tf))
    if c[2] < p[2] and c[3] > p[3]:
        out.append(_L("inside bar (compression)", "candle", "neutral", "formed", c[4], tf))
    if c[2] > p[2] and c[3] < p[3]:
        out.append(_L("outside bar (expansion)", "candle", "neutral", "formed", c[4], tf))
    if c[3] > p[2]:
        out.append(_L("rising window (gap up)", "candle", "bullish", "formed", c[4], tf))
    if c[2] < p[3]:
        out.append(_L("falling window (gap down)", "candle", "bearish", "formed", c[4], tf))
    # three-candle
    a, b_, c3 = closed[-3], closed[-2], closed[-1]
    mid_a = (a[1] + a[4]) / 2
    if _down(a) and _body(a) >= 0.9 * atr and _body(b_) <= 0.35 * _body(a) and b_[4] < a[4] + 0.2 * _body(a) and _up(c3) and c3[4] > mid_a:
        out.append(_L("morning " + ("doji " if _body(b_) <= 0.1 * _rng(b_) else "") + "star", "candle", "bullish", "formed", c3[4], tf))
    if _up(a) and _body(a) >= 0.9 * atr and _body(b_) <= 0.35 * _body(a) and b_[4] > a[4] - 0.2 * _body(a) and _down(c3) and c3[4] < mid_a:
        out.append(_L("evening " + ("doji " if _body(b_) <= 0.1 * _rng(b_) else "") + "star", "candle", "bearish", "formed", c3[4], tf))
    if all(_up(x) and _body(x) >= 0.5 * atr for x in (a, b_, c3)) and a[4] < b_[4] < c3[4] and a[1] <= b_[1] <= a[4] and b_[1] <= c3[1] <= b_[4]:
        out.append(_L("three white soldiers", "candle", "bullish", "formed", c3[4], tf))
    if all(_down(x) and _body(x) >= 0.5 * atr for x in (a, b_, c3)) and a[4] > b_[4] > c3[4] and a[4] <= b_[1] <= a[1] and c3[4] <= c3[1] <= b_[1]:
        out.append(_L("three black crows", "candle", "bearish", "formed", c3[4], tf))
    if _down(a) and _up(b_) and b_[1] >= a[4] and b_[4] <= a[1] and _body(b_) < _body(a) and _up(c3) and c3[4] > a[1]:
        out.append(_L("three inside up", "candle", "bullish", "formed", c3[4], tf))
    if _up(a) and _down(b_) and b_[1] <= a[4] and b_[4] >= a[1] and _body(b_) < _body(a) and _down(c3) and c3[4] < a[1]:
        out.append(_L("three inside down", "candle", "bearish", "formed", c3[4], tf))
    if _down(a) and _up(b_) and b_[1] <= a[4] and b_[4] >= a[1] and _up(c3) and c3[4] > b_[4]:
        out.append(_L("three outside up", "candle", "bullish", "formed", c3[4], tf))
    if _up(a) and _down(b_) and b_[1] >= a[4] and b_[4] <= a[1] and _down(c3) and c3[4] < b_[4]:
        out.append(_L("three outside down", "candle", "bearish", "formed", c3[4], tf))
    # five-candle continuation: rising / falling three methods
    if n >= 8:
        f5 = closed[-5:]
        if _up(f5[0]) and _body(f5[0]) >= 1.2 * atr and all(_down(x) or _body(x) <= 0.4 * _body(f5[0]) for x in f5[1:4]) \
                and all(f5[0][3] <= x[3] and x[2] <= f5[0][2] for x in f5[1:4]) and _up(f5[4]) and f5[4][4] > f5[0][4]:
            out.append(_L("rising three methods (pause in an uptrend)", "candle", "bullish", "formed", f5[4][4], tf))
        if _down(f5[0]) and _body(f5[0]) >= 1.2 * atr and all(_up(x) or _body(x) <= 0.4 * _body(f5[0]) for x in f5[1:4]) \
                and all(f5[0][3] <= x[3] and x[2] <= f5[0][2] for x in f5[1:4]) and _down(f5[4]) and f5[4][4] < f5[0][4]:
            out.append(_L("falling three methods (pause in a downtrend)", "candle", "bearish", "formed", f5[4][4], tf))
    # runs
    run, direction = 0, 0
    for cd in reversed(cs):
        d = 1 if _up(cd) else (-1 if _down(cd) else 0)
        if d == 0: break
        if direction == 0: direction = d
        if d != direction: break
        run += 1
    if run >= 3:
        # done when the latest candle closes; it drops if that candle's body shrinks toward flipping the other way
        health = max(0.35, min(1.0, 0.35 + _body(cs[-1]) / (0.6 * atr)))
        out.append(_L(f"{run} {'rising' if direction > 0 else 'falling'} candles in a row", "candle",
                      "bullish" if direction > 0 else "bearish", "forming", cs[-1][4], tf, pct=el * health))
    return _fill_progress(out, cs, tf)


# ------------------------------------------------------------------ structure / SMC / chart patterns
def _structure(cs, tf):
    out = []
    if len(cs) < 25:
        return out
    closed = cs[:-1]
    atr = _atr(closed) or 1e-9
    tol = 0.8 * atr
    etol = 0.4 * atr      # 'same price' tolerance for equal highs/lows, ledges, double/triple tops
    price = cs[-1][4]
    last = closed[-1]
    pv = _pivots(closed)
    highs = [p for p in pv if p[2] == "H"]
    lows = [p for p in pv if p[2] == "L"]
    n = len(closed)

    trend = None
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1] + 0.3 * tol; hl = lows[-1][1] > lows[-2][1] + 0.3 * tol
        lh = highs[-1][1] < highs[-2][1] - 0.3 * tol; ll = lows[-1][1] < lows[-2][1] - 0.3 * tol
        if hh and hl:
            trend = "up"; out.append(_L("uptrend structure (higher highs + higher lows)", "structure", "bullish", "formed", price, tf))
        elif lh and ll:
            trend = "down"; out.append(_L("downtrend structure (lower highs + lower lows)", "structure", "bearish", "formed", price, tf))
        if trend == "up" and last[4] < lows[-1][1] - 0.2 * tol:
            out.append(_L("CHANGE OF CHARACTER down (uptrend broke its last higher low)", "structure", "bearish", "confirmed", price, tf))
        elif trend == "down" and last[4] > highs[-1][1] + 0.2 * tol:
            out.append(_L("CHANGE OF CHARACTER up (downtrend broke its last lower high)", "structure", "bullish", "confirmed", price, tf))
        elif trend == "down" and last[4] < lows[-1][1] - 0.2 * tol:
            out.append(_L("break of structure DOWN (trend continuing)", "structure", "bearish", "confirmed", price, tf))
        elif trend == "up" and last[4] > highs[-1][1] + 0.2 * tol:
            out.append(_L("break of structure UP (trend continuing)", "structure", "bullish", "confirmed", price, tf))
        # premium / discount of the last swing range
        hi_, lo_ = max(highs[-1][1], highs[-2][1]), min(lows[-1][1], lows[-2][1])
        if hi_ > lo_:
            pos = (price - lo_) / (hi_ - lo_)
            if pos > 0.62:
                out.append(_L(f"price in PREMIUM zone ({pos:.0%} of the swing range)", "structure", "neutral", "formed", price, tf))
            elif pos < 0.38:
                out.append(_L(f"price in DISCOUNT zone ({pos:.0%} of the swing range)", "structure", "neutral", "formed", price, tf))
        # fibonacci pullback of the latest leg
        legs = sorted([highs[-1], lows[-1]])
        if len(legs) == 2:
            (i1, p1, t1), (i2, p2, t2) = legs
            if t1 == "L" and t2 == "H" and p2 - p1 >= 2 * atr:
                ret = (p2 - price) / (p2 - p1)
                for lvl in (0.382, 0.5, 0.618, 0.786):
                    if abs(ret - lvl) <= 0.04:
                        out.append(_L(f"pullback at the {lvl:.1%} Fibonacci level of the last up-leg", "structure", "neutral", "forming", price, tf, pct=1 - abs(ret - lvl) / 0.04))
            if t1 == "H" and t2 == "L" and p1 - p2 >= 2 * atr:
                ret = (price - p2) / (p1 - p2)
                for lvl in (0.382, 0.5, 0.618, 0.786):
                    if abs(ret - lvl) <= 0.04:
                        out.append(_L(f"bounce at the {lvl:.1%} Fibonacci level of the last down-leg", "structure", "neutral", "forming", price, tf, pct=1 - abs(ret - lvl) / 0.04))

    # liquidity: equal highs/lows and sweeps (a.k.a. upthrust / spring)
    def equal_level(points):
        pts = points[-6:]
        best = None
        for q in pts:
            cl = [z for z in pts if abs(z[1] - q[1]) <= etol]
            if len(cl) >= 2 and (best is None or len(cl) > len(best)):
                best = cl
        return best
    eh, el = equal_level(highs), equal_level(lows)
    f = cs[-1]
    if eh:
        lvl = sum(q[1] for q in eh) / len(eh)
        out.append(_L(f"equal highs at {lvl:.6g} ({len(eh)} touches) - stops sit above", "smc", "neutral", "formed", price, tf))
        if f[2] > lvl + 0.1 * atr and f[4] < lvl:
            out.append(_L("LIQUIDITY SWEEP of the highs, closed back below (upthrust / stop run)", "smc", "bearish", "forming", price, tf))
        g = cs[-2]          # the same sweep on the candle that JUST CLOSED = completed (this is what gets logged to the history)
        if g[2] > lvl + 0.1 * atr and g[4] < lvl:
            out.append(_L("LIQUIDITY SWEEP of the highs, closed back below (upthrust / stop run)", "smc", "bearish", "formed", g[4], tf))
    if el:
        lvl = sum(q[1] for q in el) / len(el)
        out.append(_L(f"equal lows at {lvl:.6g} ({len(el)} touches) - stops sit below", "smc", "neutral", "formed", price, tf))
        if f[3] < lvl - 0.1 * atr and f[4] > lvl:
            out.append(_L("LIQUIDITY SWEEP of the lows, closed back above (spring / stop run)", "smc", "bullish", "forming", price, tf))
        g = cs[-2]
        if g[3] < lvl - 0.1 * atr and g[4] > lvl:
            out.append(_L("LIQUIDITY SWEEP of the lows, closed back above (spring / stop run)", "smc", "bullish", "formed", g[4], tf))

    # fair value gaps near price (3-candle imbalance, not yet fully filled)
    for k in range(max(1, n - 20), n - 1):
        c1, c2, c3 = closed[k - 1], closed[k], closed[k + 1]
        if c3[3] > c1[2] and _up(c2) and _body(c2) >= 1.2 * atr:
            lo_, hi_ = c1[2], c3[3]
            if min(x[3] for x in closed[k + 2:] + [cs[-1]]) > lo_ and lo_ - 1.0 * atr <= price <= hi_ + 0.3 * atr:
                out.append(_L(f"price at a bullish fair value gap {lo_:.6g}-{hi_:.6g} ({n - k} candles old)", "smc", "bullish", "forming", price, tf)); break
        if c3[2] < c1[3] and _down(c2) and _body(c2) >= 1.2 * atr:
            lo_, hi_ = c3[2], c1[3]
            if max(x[2] for x in closed[k + 2:] + [cs[-1]]) < hi_ and lo_ - 0.3 * atr <= price <= hi_ + 1.0 * atr:
                out.append(_L(f"price at a bearish fair value gap {lo_:.6g}-{hi_:.6g} ({n - k} candles old)", "smc", "bearish", "forming", price, tf)); break
    # order blocks: last opposite candle before an impulsive move
    for k in range(n - 2, max(3, n - 25), -1):
        if _up(closed[k]) and _body(closed[k]) >= 1.8 * atr:
            j = k - 1
            while j > 0 and not _down(closed[j]): j -= 1
            if _down(closed[j]):
                lo_, hi_ = closed[j][3], closed[j][2]
                if lo_ - 0.2 * atr <= price <= hi_ + 0.2 * atr:
                    out.append(_L(f"retesting a bullish order block {lo_:.6g}-{hi_:.6g}", "smc", "bullish", "forming", price, tf))
            break
        if _down(closed[k]) and _body(closed[k]) >= 1.8 * atr:
            j = k - 1
            while j > 0 and not _up(closed[j]): j -= 1
            if _up(closed[j]):
                lo_, hi_ = closed[j][3], closed[j][2]
                if lo_ - 0.2 * atr <= price <= hi_ + 0.2 * atr:
                    out.append(_L(f"retesting a bearish order block {lo_:.6g}-{hi_:.6g}", "smc", "bearish", "forming", price, tf))
            break

    # ledges (support / resistance shelves)
    for pts, kind in ((lows, "L"), (highs, "H")):
        cl = equal_level(pts)
        if not cl:
            continue
        lvl = sum(q[1] for q in cl) / len(cl); touches = len(cl)
        if kind == "L":
            if (last[4] < lvl - tol or price < lvl - tol) and (n - 1 - cl[-1][0]) <= 25:
                out.append(_L(f"FALLEN LEDGE - support shelf at {lvl:.6g} broke ({touches} touches)", "chart", "bearish", "confirmed", price, tf))
            elif price <= lvl + 3 * etol and price >= lvl - tol:
                # more touches = more formed; fades as price drifts away above it or sinks toward breaking it
                health = 1 - 0.5 * min(1.0, (price - lvl) / (3 * etol)) if price >= lvl else 1 - min(1.0, (lvl - price) / tol)
                out.append(_L(f"creating a support ledge at {lvl:.6g} ({touches} touches)", "chart", "neutral", "forming", price, tf,
                              pct=min(1.0, (touches + 1) / 4) * max(0.0, health)))
        else:
            if (last[4] > lvl + tol or price > lvl + tol) and (n - 1 - cl[-1][0]) <= 25:
                out.append(_L(f"ceiling broke UP - resistance shelf at {lvl:.6g} ({touches} touches)", "chart", "bullish", "confirmed", price, tf))
            elif price >= lvl - 3 * etol and price <= lvl + tol:
                health = 1 - 0.5 * min(1.0, (lvl - price) / (3 * etol)) if price <= lvl else 1 - min(1.0, (price - lvl) / tol)
                out.append(_L(f"creating a resistance ledge at {lvl:.6g} ({touches} touches)", "chart", "neutral", "forming", price, tf,
                              pct=min(1.0, (touches + 1) / 4) * max(0.0, health)))

    # double / triple tops and bottoms
    def multi(points, top):
        for cnt in (3, 2):
            if len(points) >= cnt:
                grp = points[-cnt:]
                lv = [q[1] for q in grp]
                if max(lv) - min(lv) <= 1.25 * etol and grp[-1][0] - grp[0][0] >= 4 * (cnt - 1):
                    seg = closed[grp[0][0]:grp[-1][0] + 1]
                    neck = min(x[3] for x in seg) if top else max(x[2] for x in seg)
                    depth = (sum(lv) / cnt - neck) if top else (neck - sum(lv) / cnt)
                    if depth >= 2.0 * atr:
                        done = (price < neck - 0.3 * tol) if top else (price > neck + 0.3 * tol)
                        nm = ("triple " if cnt == 3 else "double ") + ("top" if top else "bottom")
                        pk = sum(lv) / cnt
                        prog = ((pk - price) / (pk - neck)) if top else ((price - pk) / (neck - pk))
                        beyond = (price - pk) if top else (pk - price)
                        pc = 0.5 + 0.5 * max(0.0, min(1.0, prog)) if beyond <= 0 else 0.5 * max(0.0, 1 - beyond / (2 * tol))
                        return _L(f"{nm} " + ("CONFIRMED (neckline broke)" if done else "forming"), "chart",
                                  "bearish" if top else "bullish", "confirmed" if done else "forming", price, tf, pct=pc)
        return None
    for lab in (multi(highs, True), multi(lows, False)):
        if lab: out.append(lab)

    # head and shoulders / inverse
    if len(highs) >= 3:
        l_, h_, r_ = highs[-3], highs[-2], highs[-1]
        if h_[1] > l_[1] + tol and h_[1] > r_[1] + tol and abs(l_[1] - r_[1]) <= 1.6 * tol and r_[0] - l_[0] >= 8:
            neck = min(x[3] for x in closed[l_[0]:r_[0] + 1]); done = price < neck - 0.3 * tol
            prog = (r_[1] - price) / (r_[1] - neck) if r_[1] > neck else 0.0
            over = price - r_[1]
            pc = 0.5 + 0.5 * max(0.0, min(1.0, prog)) if over <= 0 else 0.5 * max(0.0, 1 - over / max(h_[1] - r_[1], tol))
            out.append(_L("head and shoulders " + ("CONFIRMED (neckline broke)" if done else "forming"), "chart", "bearish", "confirmed" if done else "forming", price, tf, pct=pc))
    if len(lows) >= 3:
        l_, h_, r_ = lows[-3], lows[-2], lows[-1]
        if h_[1] < l_[1] - tol and h_[1] < r_[1] - tol and abs(l_[1] - r_[1]) <= 1.6 * tol and r_[0] - l_[0] >= 8:
            neck = max(x[2] for x in closed[l_[0]:r_[0] + 1]); done = price > neck + 0.3 * tol
            prog = (price - r_[1]) / (neck - r_[1]) if neck > r_[1] else 0.0
            under = r_[1] - price
            pc = 0.5 + 0.5 * max(0.0, min(1.0, prog)) if under <= 0 else 0.5 * max(0.0, 1 - under / max(r_[1] - h_[1], tol))
            out.append(_L("inverse head and shoulders " + ("CONFIRMED (neckline broke)" if done else "forming"), "chart", "bullish", "confirmed" if done else "forming", price, tf, pct=pc))

    # triangles / wedges / channels / rectangle / broadening from the last 3-4 pivots on each side
    if len(highs) >= 3 and len(lows) >= 3:
        hp = [(p[0], p[1]) for p in highs[-4:]]; lp = [(p[0], p[1]) for p in lows[-4:]]
        x0 = min(hp[0][0], lp[0][0]); x1 = n - 1
        span = x1 - x0
        if span >= 12:
            hs, hb, he = _fit(hp); ls_, lb, le = _fit(lp)
            if he <= 0.7 * atr and le <= 0.7 * atr:
                hd = hs * span / atr; ld = ls_ * span / atr        # drift over the span in ATRs
                w0 = (hs * x0 + hb) - (ls_ * x0 + lb); w1 = (hs * x1 + hb) - (ls_ * x1 + lb)
                up_line = hs * x1 + hb; lo_line = ls_ * x1 + lb
                flat = lambda d: abs(d) <= 0.7
                rising = lambda d: d > 0.7
                falling = lambda d: d < -0.7
                name = bias = None
                if w0 > 0 and w1 > 0:
                    ratio = w1 / w0
                    if ratio <= 0.65:
                        if flat(hd) and rising(ld): name, bias = "ascending triangle", "bullish"
                        elif falling(hd) and flat(ld): name, bias = "descending triangle", "bearish"
                        elif falling(hd) and rising(ld): name, bias = "symmetrical triangle (coiling)", "neutral"
                        elif rising(hd) and rising(ld): name, bias = "rising wedge", "bearish"
                        elif falling(hd) and falling(ld): name, bias = "falling wedge", "bullish"
                    elif ratio >= 1.45 and rising(hd) and falling(ld):
                        name, bias = "broadening formation (widening swings)", "neutral"
                    elif 0.8 <= ratio <= 1.25:
                        if flat(hd) and flat(ld): name, bias = "rectangle / range", "neutral"
                        elif rising(hd) and rising(ld): name, bias = "up-channel", "bullish"
                        elif falling(hd) and falling(ld): name, bias = "down-channel", "bearish"
                if name:
                    st = "forming"
                    if price > up_line + 0.3 * tol: st, name = "confirmed", name + " - BROKE UP"
                    elif price < lo_line - 0.3 * tol: st, name = "confirmed", name + " - BROKE DOWN"
                    else: name = name + " forming"
                    width_now = max(up_line - lo_line, 1e-9)
                    edge = min(price - lo_line, up_line - price) / width_now          # 0.5 = mid-channel, 0 = at a line
                    pc = 0.5 * min(1.0, (len(hp) + len(lp)) / 8) + 0.5 * max(0.0, min(1.0, 1 - 2 * edge))
                    out.append(_L(name, "chart", bias, st, price, tf, pct=pc))

    # flag / pennant: sharp impulse, then a tight pause
    if n >= 22:
        for gap in range(4, 14):
            seg = closed[n - gap - 8: n - gap]
            if len(seg) < 8: continue
            imp = seg[-1][4] - seg[0][1]
            if abs(imp) >= 3 * atr:
                cons = closed[n - gap:]
                chi, clo = max(x[2] for x in cons), min(x[3] for x in cons)
                if (chi - clo) <= 0.45 * abs(imp):
                    drift_ = cons[-1][4] - cons[0][1]
                    if imp > 0 and drift_ <= 0.2 * atr:
                        out.append(_L("bull flag / pennant forming (pause after a sharp rise)", "chart", "bullish", "forming", price, tf, pct=gap / 10)); break
                    if imp < 0 and drift_ >= -0.2 * atr:
                        out.append(_L("bear flag / pennant forming (pause after a sharp drop)", "chart", "bearish", "forming", price, tf, pct=gap / 10)); break

    # rounding bottom / top and cup-with-handle (quadratic fit of the last 40 closes)
    if np is not None and n >= 45:
        ys = np.array([x[4] for x in closed[-40:]]); xs = np.arange(40)
        a, b, c0 = np.polyfit(xs, ys, 2)
        vx = -b / (2 * a) if a != 0 else None
        if vx is not None and 12 <= vx <= 28:
            fit = a * xs ** 2 + b * xs + c0
            if np.mean(np.abs(ys - fit)) <= 0.8 * atr:
                rim = (ys[:3].mean() + ys[-3:].mean()) / 2
                if a > 0 and (rim - ys.min()) >= 2.5 * atr and ys[-3:].mean() >= ys[:3].mean() - 0.8 * (ys[:3].mean() - ys.min()):
                    handle = (ys[-12:].max() - price) <= 0.4 * (ys[-12:].max() - ys.min()) and price < ys[-12:].max() - 0.3 * atr
                    rp = (ys[-3:].mean() - ys.min()) / max(rim - ys.min(), 1e-9)
                    # 2026-10-14: these used to stay 'forming' even at 100%, so they never got a completion time. Once the right side has recovered
                    # (95%+) the cup is FORMED (the name stays the same so it does not flicker between two identities).
                    out.append(_L("cup with handle (bullish)" if handle else "rounding bottom / cup (bullish)", "chart", "bullish",
                                  "formed" if rp >= 0.95 else "forming", price, tf, pct=rp))
                if a < 0 and (ys.max() - rim) >= 2.5 * atr and ys[-3:].mean() <= ys[:3].mean() + 0.8 * (ys.max() - ys[:3].mean()):
                    rp = (ys.max() - ys[-3:].mean()) / max(ys.max() - rim, 1e-9)
                    out.append(_L("rounding top (bearish)", "chart", "bearish", "formed" if rp >= 0.95 else "forming", price, tf, pct=rp))
    return _fill_progress(out, cs, tf)


_PRIORITY = {"chart": 0, "smc": 1, "structure": 2, "candle": 3}
_STATUS = {"confirmed": 0, "forming": 1, "formed": 2}


def detect_all(book, max_labels=8):
    """Run every detector on the 1-minute and 5-minute candles; return labels sorted by importance."""
    labels = []
    for seconds, tf, lookback in ((60, "1m", MAX_LOOKBACK), (300, "5m", 48)):
        cs = book.series(seconds, lookback)
        if len(cs) < 25:
            continue
        try:
            labels += _structure(cs, tf)
            labels += _candle_patterns(cs, tf)
        except Exception:
            continue
    seen, uniq = set(), []
    for l in labels:
        k = (l["name"], l["tf"])
        if k not in seen:
            seen.add(k); uniq.append(l)
    uniq.sort(key=lambda l: (_STATUS.get(l["status"], 3), _PRIORITY.get(l["family"], 4), l["tf"] != "5m"))
    return uniq[:max_labels]
