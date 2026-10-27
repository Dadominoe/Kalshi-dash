"""Standalone PREVIEW (touches nothing on the dashboard): swing trend lines from several timeframes on ONE chart (one color per timeframe, no labels), the owner's SMA lines
(green moving average / red / blue), SR channels in the background, and a replay of the 1-minute lines being redrawn every 5 candles.
Trend lines are TOUCH-BASED like a hand-drawn one: start at a swing high (or low), run through a later swing high (or low), keep it only if no candle in between pokes through it,
rank by how many swing points sit on it."""
import datetime as dt, json, math, os, re, sys, time
import requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ladder_core import _ladder_pivots

OUT = r"C:\Users\Natey\Downloads\MTF_Lines_Preview.html"
EMBED = "--embed" in sys.argv
TARGETS = {}                                 # the dashboard runs this with --embed <folder>: one small page per coin, no preview notes, with live self-refresh
OUT_DIR = sys.argv[sys.argv.index("--embed") + 1] if EMBED and len(sys.argv) > sys.argv.index("--embed") + 1 else None
COINS = (("BTC", "BTC-USD", 2), ("XRP", "XRP-USD", 4))
HOLD_SECONDS = 6 * 60                                       # a freshly projected tunnel is held for 6 minutes before it may move on or be replaced (a pinch is shown only after it has stood that long)
BIAS_WEAK = 0.25                                           # below this strength (in candle sizes) the market-bias strip is drawn faded; the Indicators tab uses the measured split
MONEY_P, RED_P, BLUE_P = 15, 34, 111                      # the owner's TradingView MA Ribbon: SMA on the close
# key, minutes, color, width, on by default, lines kept per side
TFS = (("1m", 1, "#ffd54a", 1.7, True, 6), ("5m", 5, "#ff9f43", 1.9, False, 5), ("15m", 15, "#39d0ff", 2.3, True, 5),
       ("30m", 30, "#7ee787", 2.5, False, 5), ("1h", 60, "#ff4dd2", 3.0, True, 5), ("1d", 1440, "#b388ff", 3.6, False, 5))
BASES = (("1 minute candles", "1m"), ("15 minute candles", "15m"), ("1 hour candles", "1h"))
SHOW = {"1m": 100, "15m": 56, "1h": 72}                 # candles on screen
GRAN = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
FRAMES, FRAME_STEP = 12, 5                               # replay: 12 pictures, the lines redrawn every 5 candles


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_SESS = requests.Session()                                  # one kept-open connection: no new TLS handshake (and certificate load) for every request
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data_cache", "mtf_cache")


def _page(sym, gran, e):
    r = _SESS.get(f"https://api.exchange.coinbase.com/products/{sym}/candles", params={"granularity": gran, "start": iso(e - 300 * gran), "end": iso(e)}, timeout=20)
    return {int(x[0]): [int(x[0]), float(x[3]), float(x[2]), float(x[1]), float(x[4])] for x in r.json()}


def fetch(sym, gran, pages=1):
    """Candles (oldest first), `pages` x 300 of them. Closed candles never change, so they are kept in a small file between builds and only the newest page is asked for each time
    (older pages are fetched only when the file does not reach back far enough, or when there is a gap)."""
    import pickle
    now = int(time.time())
    path = os.path.join(_CACHE_DIR, f"live_{sym}_{gran}.pkl")
    try:
        have = pickle.load(open(path, "rb"))
    except Exception:
        have = {}
    need_from = now - pages * 300 * gran
    had_max = max(have) if have else 0
    e, got = now, 0
    while True:                                                 # newest page first, then back until it overlaps what we already have (or until we have enough history)
        pg = _page(sym, gran, e)
        have.update(pg)
        got += 1
        e -= 300 * gran
        if not pg or min(pg) <= had_max or e <= need_from or got >= pages + 2:
            break
        time.sleep(0.2)
    while have and min(have) > need_from + 3 * gran and got < pages + 4:       # the file does not reach back far enough yet: add older pages
        have.update(_page(sym, gran, min(have)))
        got += 1
        time.sleep(0.2)
    keep = {t: r for t, r in have.items() if t >= need_from - 2 * gran}
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        pickle.dump(keep, open(tmp, "wb"))
        os.replace(tmp, path)
    except Exception:
        pass
    return [keep[t] for t in sorted(keep)]


def agg(cs, factor):
    out, cur = [], None
    for c in cs:
        key = c[0] // (900 * factor)
        if cur is None or cur[0] != key:
            if cur is not None:
                out.append(cur[1])
            cur = (key, [key * 900 * factor, c[1], c[2], c[3], c[4]])
        else:
            b = cur[1]; b[2] = max(b[2], c[2]); b[3] = min(b[3], c[3]); b[4] = c[4]
    if cur is not None:
        out.append(cur[1])
    return out


def get_tf(sym, key):
    if key == "30m":
        return agg(fetch(sym, 900), 2)
    return fetch(sym, GRAN[key], pages={"1m": 8, "15m": 3, "1h": 2}.get(key, 1))


def sma(closes, p):
    out, run = [], 0.0
    for i, c in enumerate(closes):
        run += c
        if i >= p:
            run -= closes[i - p]
        out.append(run / min(i + 1, p))
    return out


def ema_list(vals, p):
    a, v, out = 2 / (p + 1), vals[0], []
    for x in vals:
        v = a * x + (1 - a) * v
        out.append(v)
    return out


def market_bias(cs, ha_len=100, ha_len2=100):
    """Signed strength for every candle (+ bullish / - bearish): Heikin-Ashi candles built from 100-period smoothed open / high / low / close, smoothed again by 100; bullish when the smoothed HA close is above its open."""
    o = ema_list([x[1] for x in cs], ha_len); h = ema_list([x[2] for x in cs], ha_len); l_ = ema_list([x[3] for x in cs], ha_len); cl = ema_list([x[4] for x in cs], ha_len)
    hac = [(o[i] + h[i] + l_[i] + cl[i]) / 4 for i in range(len(cs))]
    hao = [(o[0] + cl[0]) / 2]
    for i in range(1, len(cs)):
        hao.append((hao[-1] + hac[i - 1]) / 2)
    o2, c2 = ema_list(hao, ha_len2), ema_list(hac, ha_len2)
    a = atr(cs[-300:]) or 1e-9
    return [(c2[i] - o2[i]) / a for i in range(len(cs))]            # signed strength in units of a typical candle: + bullish, - bearish; small = weak (faded)


def market_bias_candles(cs, ha_len=100, ha_len2=100):
    """The Market Bias indicator's own candles (settings 100 / 100): Heikin-Ashi built from 100-period smoothed open/high/low/close, then smoothed again by 100. -> [(open, high, low, close)] per candle."""
    o = ema_list([x[1] for x in cs], ha_len); h = ema_list([x[2] for x in cs], ha_len); l_ = ema_list([x[3] for x in cs], ha_len); cl = ema_list([x[4] for x in cs], ha_len)
    hac = [(o[i] + h[i] + l_[i] + cl[i]) / 4 for i in range(len(cs))]
    hao = [(o[0] + cl[0]) / 2]
    for i in range(1, len(cs)):
        hao.append((hao[-1] + hac[i - 1]) / 2)
    hah = [max(h[i], hao[i], hac[i]) for i in range(len(cs))]
    hal = [min(l_[i], hao[i], hac[i]) for i in range(len(cs))]
    o2, c2, h2, l2 = ema_list(hao, ha_len2), ema_list(hac, ha_len2), ema_list(hah, ha_len2), ema_list(hal, ha_len2)
    return list(zip(o2, h2, l2, c2))


def atr(cs, n=14):
    trs = [max(c[2] - c[3], abs(c[2] - cs[i - 1][4]), abs(c[3] - cs[i - 1][4])) for i, c in enumerate(cs) if i > 0]
    return sum(trs[-n:]) / max(len(trs[-n:]), 1)


def angle_pct(series, cs, look=5):
    """Tilt as a percent: 0 flat, +100 straight up, -100 straight down (scaled so the steepest 10% of this chart's history reads about 70%)."""
    a = atr(cs[-300:]) or 1e-9
    slopes = [(series[i] - series[i - look]) / look / a for i in range(look, len(series))]
    ref = sorted(abs(s) for s in slopes[-1500:])[int(0.9 * len(slopes[-1500:]))] or 1e-9
    k = math.tan(0.7 * math.pi / 2) / ref
    s = (series[-1] - series[-1 - look]) / look / a
    return math.atan(s * k) / (math.pi / 2) * 100


def touch_lines(cs, keep=2, L=3, maxpiv=40):
    """Hand-drawn style trend lines, per side (resistance through swing highs, support through swing lows - so lower lows and higher lows, lower highs and higher highs all count):
      major   - starts at a swing and runs through LATER swings; kept only if no candle between the two points pokes through; the most-touched lines first
      recent  - the best respected line whose second point is the most recent one (so it reflects where price is NOW)
      project - the line through the LAST TWO swings of that side, drawn even if a spike broke it: 'higher high projects to the next higher high', dashed
    Returns [(start time, start price, slope per second, touches, kind)]."""
    cs = cs[-320:]
    n = len(cs)
    if n < 40:
        return []
    tol = 0.25 * (atr(cs[-200:]) or 1e-9)
    piv = _ladder_pivots(cs[:-1], L)
    dt_ = cs[1][0] - cs[0][0]
    out = []
    for kind in ("H", "L"):
        pts = [(i, p) for i, p, t in piv if t == kind][-maxpiv:]
        cand = []
        for ai in range(len(pts)):
            for bi in range(ai + 1, len(pts)):
                (ia, pa), (ib, pb) = pts[ai], pts[bi]
                if ib - ia < 3:
                    continue
                sl = (pb - pa) / (ib - ia)
                ok = True
                for k in range(ia, ib + 1):                       # nothing between the two points may poke through the line
                    ln = pa + sl * (k - ia)
                    if (kind == "H" and cs[k][2] > ln + tol) or (kind == "L" and cs[k][3] < ln - tol):
                        ok = False
                        break
                if not ok:
                    continue
                touches = sum(1 for (ic, pc) in pts if ic >= ia and abs(pc - (pa + sl * (ic - ia))) <= tol)
                cand.append((touches, ib, ia, pa, sl))
        chosen = []                                               # (ia, slope, kind label, touches, price at ia)

        def same(ia, sl):
            return any(abs(ia - a2) < 6 and abs(sl - s2) <= 0.3 * max(abs(s2), abs(sl), 1e-12) for a2, s2, *_ in chosen)
        for touches, ib, ia, pa, sl in sorted(cand, reverse=True):
            if not same(ia, sl):
                chosen.append((ia, sl, "major", touches, pa))
            if len([c for c in chosen if c[2] == "major"]) >= keep:
                break
        added = 0
        for touches, ib, ia, pa, sl in sorted(cand, key=lambda c: (c[1], c[0]), reverse=True):    # the newest lines (newest second point first) - these are where price has just been
            if not same(ia, sl):
                chosen.append((ia, sl, "recent", touches, pa))
                added += 1
                if added >= 6:
                    break
        if len(pts) >= 2:                                         # the projection: last two swings of this side, no questions asked
            (ia, pa), (ib, pb) = pts[-2], pts[-1]
            if ib - ia >= 2:
                sl = (pb - pa) / (ib - ia)
                if not same(ia, sl):
                    chosen.append((ia, sl, "project", 1, pa))
        for ia, sl, label, touches, pa in chosen:
            out.append((cs[ia][0], pa, sl / dt_, touches, label, kind))
    def hull(pts, upper):
        pts = sorted(pts)
        h = []
        for pt in pts:
            while len(h) >= 2:
                (x1, y1), (x2, y2) = h[-2], h[-1]
                cross = (x2 - x1) * (pt[1] - y1) - (y2 - y1) * (pt[0] - x1)
                if (upper and cross >= 0) or ((not upper) and cross <= 0):
                    h.pop()
                else:
                    break
            h.append(pt)
        return h
    gw = cs[-120:]
    gbase = n - len(gw)
    for upper, side in ((True, "H"), (False, "L")):
        hv = hull([(k, gw[k][2] if upper else gw[k][3]) for k in range(len(gw))], upper)
        for j in range(len(hv) - 1):
            (x1, y1), (x2, y2) = hv[j], hv[j + 1]
            if x2 - x1 < 2:
                continue
            last = (j == len(hv) - 2)
            out.append((gw[x1][0], y1, (y2 - y1) / (x2 - x1) / dt_, 3, "glove", side, None if last else gw[x2][0]))
    for W in (90, 60, 35, 20, 12):                                 # in-time trend lines at five scales, down to the tiny 6-12 candle drops that become support / resistance later                                            # the in-time trend lines at two scales: a longer stretch and a tight, recent one
        w = cs[-W:]
        base = n - len(w)
        ih = max(range(len(w)), key=lambda k: w[k][2])
        il = min(range(len(w)), key=lambda k: w[k][3])
        if ih < len(w) - 4:                                        # line from the highest high, just above every later high (the downtrend line when price is falling)
            s = max((w[k][2] - w[ih][2]) / (k - ih) for k in range(ih + 1, len(w)))
            out.append((w[ih][0], w[ih][2], s / dt_, 3, "live", "H"))
        if il < len(w) - 4:                                        # line from the lowest low, just below every later low (the uptrend line when price is rising)
            s = min((w[k][3] - w[il][3]) / (k - il) for k in range(il + 1, len(w)))
            out.append((w[il][0], w[il][3], s / dt_, 3, "live", "L"))
    return out


def floors(cs, L=3, n_each=8):
    """Horizontal floors (swing lows) and ceilings (swing highs). The owner's rule: a single candle that just pokes out is NOT a line; it counts only when TWO candles sit at the same level
    (side by side, or a later candle back at it). Each such level - the wick edge or the body edge - runs on until a candle CLOSES through it."""
    cs = cs[-320:]
    n = len(cs)
    if n < 40:
        return []
    tol = 0.12 * (atr(cs[-200:]) or 1e-9)
    piv = _ladder_pivots(cs[:-1], L)
    out = []
    for kind in ("L", "H"):
        for i in [i_ for i_, p_, t_ in piv if t_ == kind][-n_each:]:
            cd = cs[i]
            seen = []
            for mode, lv in (("wick", cd[3] if kind == "L" else cd[2]), ("body", min(cd[1], cd[4]) if kind == "L" else max(cd[1], cd[4]))):
                if any(abs(lv - s) <= tol for s in seen):
                    continue                                          # wick and body are the same level here: one line
                touching = [k for k in range(max(0, i - 3), n - 1)
                            if abs(((cs[k][3] if kind == "L" else cs[k][2]) if mode == "wick" else (min(cs[k][1], cs[k][4]) if kind == "L" else max(cs[k][1], cs[k][4]))) - lv) <= tol]
                if len(touching) < 2:
                    continue                                          # only one candle makes it: void
                seen.append(lv)
                end = None
                for k in range(i + 1, n):
                    if (kind == "L" and cs[k][4] < lv) or (kind == "H" and cs[k][4] > lv):
                        end = cs[k][0]
                        break
                out.append((cd[0], lv, 0.0, len(touching), "floor", kind, end))
    return out


def all_lines(cs, keep, L):
    """Every kind of line for one chart: from the wicks, from the candle bodies (dotted), and the horizontal floors / ceilings (fine dots)."""
    wick = touch_lines(cs, keep=keep, L=L)
    body_cs = [[x[0], x[1], max(x[1], x[4]), min(x[1], x[4]), x[4]] for x in cs]
    body = [(t, p_, s_, tc, "body-" + k_, sd, *rest) for (t, p_, s_, tc, k_, sd, *rest) in touch_lines(body_cs, keep=3, L=L)]
    return wick + body + floors(cs, L)


def cross_stats(cs, g, r, horizons=(5, 15)):
    res = {"up": {h: [0, 0] for h in horizons}, "down": {h: [0, 0] for h in horizons}}
    for i in range(1, len(cs) - max(horizons)):
        up = g[i - 1] <= r[i - 1] and g[i] > r[i]
        dn = g[i - 1] >= r[i - 1] and g[i] < r[i]
        if not (up or dn):
            continue
        for h in horizons:
            mv = cs[i + h][4] - cs[i][4]
            b = res["up" if up else "down"][h]
            b[1] += 1
            b[0] += 1 if ((up and mv > 0) or (dn and mv < 0)) else 0
    return res


def sr_channels(cs, prd=10, maxw=5.0, minstr=1, maxnum=6, loopback=290):
    """The owner's SRchannel settings: pivot 10, widest 5% of the last 300 candles' range, strength 1, up to 6 channels, 290 candles back."""
    n = len(cs)
    if n < prd * 2 + 20:
        return []
    piv = []
    for i in range(max(prd, n - loopback - prd), n - prd):
        hi, lo = cs[i][2], cs[i][3]
        if hi == max(x[2] for x in cs[i - prd:i + prd + 1]):
            piv.append(hi)
        if lo == min(x[3] for x in cs[i - prd:i + prd + 1]):
            piv.append(lo)
    if not piv:
        return []
    cw = (max(x[2] for x in cs[-300:]) - min(x[3] for x in cs[-300:])) * maxw / 100
    window = cs[-loopback:]
    cands = []
    for p in piv:
        lo = hi = p
        num = 0
        for q in piv:
            wd = (hi - q) if q <= hi else (q - lo)
            if wd <= cw:
                lo, hi = min(lo, q), max(hi, q)
                num += 20
        cands.append((num + sum(1 for x in window if (lo <= x[2] <= hi) or (lo <= x[3] <= hi)), hi, lo))
    cands.sort(reverse=True)
    chosen = []
    for st, hi, lo in cands:
        if st < minstr * 20 or len(chosen) >= maxnum:
            continue
        if any(not (hi < l2 or lo > h2) for _s, h2, l2 in chosen):
            continue
        chosen.append((st, hi, lo))
    return chosen


def _down_from(w, ih, dt_):
    s = max((w[k][2] - w[ih][2]) / (k - ih) for k in range(ih + 1, len(w)))                    # per candle: the line just above every later high
    low_off = min(w[k][3] - (w[ih][2] + s * (k - ih)) for k in range(ih + 1, len(w)))          # the parallel bottom edge through the lowest low after it
    return (w[ih][0], w[ih][2], s / dt_, w[ih][2] + low_off, s / dt_, "down")


def _up_from(w, il, dt_):
    s = min((w[k][3] - w[il][3]) / (k - il) for k in range(il + 1, len(w)))
    hi_off = max(w[k][2] - (w[il][3] + s * (k - il)) for k in range(il + 1, len(w)))
    return (w[il][0], w[il][3] + hi_off, s / dt_, w[il][3], s / dt_, "up")


def bouncy(cs, tun):
    """True when price has been poking out through the tunnel's edges lately (a third or more of the 12 candles before the newest two), i.e. the tunnel is not solid."""
    if not tun or len(cs) < 16:
        return False
    ta, tp, ts_, bp, bs_ = tun[:5]
    a = atr(cs[-60:]) or 1e-9
    out = 0
    for x in cs[-14:-2]:
        top, bot = tp + ts_ * (x[0] - ta), bp + bs_ * (x[0] - ta)
        if x[2] > top + 0.15 * a or x[3] < bot - 0.15 * a:
            out += 1
    return out >= 4


def tunnel_pair(cs, W=120, prev=None, hold=False):
    """(main, optional). The MAIN tunnel is the one we are in now: it starts at the most recent turning point (the later of the big high and the big low). The OPTIONAL one is the other (older) tunnel.
    If `prev` (the main tunnel found a moment ago) still holds it is KEPT and only re-projected through the new candles. If price CLOSES THROUGH an edge (two closes, clearly outside), that broken edge becomes the new
    floor (broke up) or ceiling (broke down) and the tunnel moves on to the NEXT ceiling / floor - the old tunnel stays as the optional one. A tunnel that has moved on is kept while price stays inside it."""
    w = cs[-W:]
    if len(w) < 30:
        return None, None
    dt_ = w[1][0] - w[0][0]
    n_ = len(w)
    ih = max(range(n_), key=lambda k: w[k][2])
    il = min(range(n_), key=lambda k: w[k][3])
    a = atr(w[-60:]) or 1e-9
    build = lambda kind, idx: _up_from(w, idx, dt_) if kind == "up" else _down_from(w, idx, dt_)
    cands = sorted([(idx, kind) for kind, idx in (("up", il), ("down", ih)) if n_ - 1 - idx >= 6], reverse=True)      # newest turning point first
    topf = lambda t, tt: t[1] + t[2] * (tt - t[0])
    botf = lambda t, tt: t[3] + t[4] * (tt - t[0])
    last2, last3 = w[-2:], w[-3:]
    main = opt = None
    if prev is not None:
        if len(prev) > 6 and prev[6] == "moved":                                   # a tunnel that already moved on
            orig = prev[7]
            oidx = next((k for k in range(n_) if w[k][0] == orig[0]), None)
            if oidx is not None and n_ - 1 - oidx >= 6 and botf(orig, w[-1][0]) <= w[-1][4] <= topf(orig, w[-1][0]):
                main = build(orig[5], oidx)                                         # price is back INSIDE the original tunnel: that break was false, go back to it
            elif all(botf(prev, x[0]) - 0.3 * a <= x[4] <= topf(prev, x[0]) + 0.3 * a for x in last3):
                main = prev                                                         # still inside the tunnel it moved on to: keep it
        else:
            idx = next((k for k in range(n_) if w[k][0] == prev[0]), None)
            if idx is not None and n_ - 1 - idx >= 6:
                kind = prev[5]
                t_now_ = build(kind, idx)                                           # the edges re-fitted through every candle so far
                t = (_up_from(w[:-2], idx, dt_) if kind == "up" else _down_from(w[:-2], idx, dt_)) if n_ - 3 - idx >= 4 else t_now_      # the edges as they stood BEFORE the newest two candles
                beaten = any(x[3] < w[idx][3] - 0.3 * a for x in w[idx + 1:]) if kind == "up" else any(x[2] > w[idx][2] + 0.3 * a for x in w[idx + 1:])
                up_break = all(x[4] > topf(t, x[0]) + 0.3 * a for x in last2)
                dn_break = all(x[4] < botf(t, x[0]) - 0.3 * a for x in last2)
                if not beaten and (up_break or dn_break) and not hold:            # during the first 6 minutes a break is not acted on (it is usually noise)
                    other = next((build(k_, i_) for i_, k_ in cands if i_ != idx), None)           # the other tunnel, to find the next ceiling / floor
                    width = topf(t, w[-1][0]) - botf(t, w[-1][0])
                    px = w[-1][4]
                    if up_break:                                                    # broke UP: the old ceiling is now the floor; the next ceiling is the next line above
                        ceil = (other[1], other[2], other[0]) if (other and topf(other, w[-1][0]) > px) else (t[1] + width, t[2], t[0])
                        floor = (t[1], t[2], t[0])
                    else:                                                           # broke DOWN: the old floor is now the ceiling; the next floor is the next line below
                        floor = (other[3], other[4], other[0]) if (other and botf(other, w[-1][0]) < px) else (t[3] - width, t[4], t[0])
                        ceil = (t[3], t[4], t[0])
                    ta = t[0]
                    tp = ceil[0] + ceil[1] * (ta - ceil[2])
                    bp = floor[0] + floor[1] * (ta - floor[2])
                    main = (ta, tp, ceil[1], bp, floor[1], "up" if up_break else "down", "moved", t)
                    opt = t
                elif not beaten:
                    main = t_now_
    if main is None and cands:
        main = build(cands[0][1], cands[0][0])
    if opt is None:
        for idx, kind in cands:
            t = build(kind, idx)
            if main is None or t[0] != main[0]:
                opt = t
                break
    return main, opt


def wedge(cs, L=3):
    """The pinched tunnel. Tunnels often thin out: the falling top line and the rising bottom line close in on each other and meet at a point (an apex) a little way ahead. Pick the best such pair
    of respected lines (most touches) that price is sitting between right now and that meet within the next 60 candles. -> (start, top p0, top slope/s, bottom p0, bottom slope/s, 'pinch', apex time)"""
    ls = touch_lines(cs[-320:], keep=6, L=L)
    t_now, px = cs[-1][0], cs[-1][4]
    dt_ = cs[1][0] - cs[0][0]
    a = atr(cs[-60:]) or 1e-9
    best = None
    for h in [x for x in ls if x[5] == "H" and x[4] in ("major", "recent", "live")]:
        for l_ in [x for x in ls if x[5] == "L" and x[4] in ("major", "recent", "live")]:
            top, bot = h[1] + h[2] * (t_now - h[0]), l_[1] + l_[2] * (t_now - l_[0])
            if not (bot < px < top):
                continue
            gap = h[2] - l_[2]
            if gap >= 0:
                continue                                              # not closing in
            t_apex = t_now + (top - bot) / (-gap)
            ahead = (t_apex - t_now) / dt_
            wd = top - bot
            if ahead < 1 or ahead > 60 or wd < 0.4 * a or wd > 14 * a:
                continue
            start = max(h[0], l_[0])
            if (t_now - start) / dt_ < 8:
                continue                                              # has to have been forming for a while
            key = (h[3] + l_[3], -ahead)
            if best is None or key > best[0]:
                best = (key, start, h, l_, t_apex)
    if best is None:
        return None
    _k, start, h, l_, t_apex = best
    return (start, h[1] + h[2] * (start - h[0]), h[2], l_[1] + l_[2] * (start - l_[0]), l_[2], "pinch", t_apex)


def tunnel_both(cs, W=120, stride=5, steps=18):
    """Replay the last ~90 candles in steps of 5 so the tunnel found earlier is kept while it holds; returns (main, optional) for the newest candle."""
    n = len(cs)
    prev, main, opt = None, None, None
    dt_ = (cs[1][0] - cs[0][0]) or 60
    hold_n = max(1, int(HOLD_SECONDS // dt_))                         # 6 minutes = 6 candles on the 1-minute chart, 1 candle on bigger charts
    ends = [n - 1 - stride * m for m in range(steps, 1, -1)] + list(range(n - 8, n))      # every 5 candles over the last ~90, then candle by candle for the newest 8
    last_id, born = None, None
    for e in ends:
        if e < 40:
            continue
        hold = born is not None and (e - born) < hold_n and bouncy(cs[:e + 1], prev)          # only a bouncy tunnel is made to wait; a solid one acts at once
        main, opt = tunnel_pair(cs[:e + 1], W, prev, hold)
        ident = None if main is None else (main[0], main[5], len(main) > 6)
        if ident != last_id:
            last_id, born = ident, e
        prev = main
    L_ = 2 if dt_ <= 300 else 3
    wdg = wedge(cs, L_)
    if wdg and len(cs) > hold_n + 40 and bouncy(cs, wdg):
        earlier = wedge(cs[:-hold_n], L_)                             # the pinch must already have been there 6 minutes ago, or it is still just forming
        if not earlier:
            wdg = None
    if wdg:
        return wdg, main                                              # a pinch exists: it is the tunnel to watch; the side-by-side tunnel becomes the optional one
    return main, opt


def _fit_channel(w, dt_, a):
    """One narrow parallel channel through the candles in w: the slope is the straight-line trend of the closes, the top edge sits where the highs pile up (the 90th percentile of the highs, so a few wicks may
    poke out) and the bottom edge where the lows pile up (10th percentile). -> (start, top p0, slope/s, bottom p0, slope/s, kind, width, top touches, bottom touches)"""
    n_ = len(w)
    xs = list(range(n_))
    mx = (n_ - 1) / 2
    my = sum(x[4] for x in w) / n_
    den = sum((k - mx) ** 2 for k in xs) or 1
    s = sum((k - mx) * (w[k][4] - my) for k in xs) / den
    b = my - s * mx
    rh = sorted(w[k][2] - (b + s * k) for k in xs)
    rl = sorted(w[k][3] - (b + s * k) for k in xs)
    top_off = rh[min(n_ - 1, int(0.9 * n_))]
    bot_off = rl[int(0.1 * n_)]
    tt = [k for k in xs if w[k][2] >= b + s * k + top_off - 0.2 * a]
    bt = [k for k in xs if w[k][3] <= b + s * k + bot_off + 0.2 * a]

    def apart(ks):
        out, last = 0, -9
        for k in ks:
            if k - last >= 3:
                out += 1
            last = k
        return out
    return (w[0][0], b + top_off, s / dt_, b + bot_off, s / dt_, "up" if s >= 0 else "down", top_off - bot_off, apart(tt), apart(bt))


def find_narrow(cs, minN=14, maxN=90, relax=False):
    """Look back over 14 to 90 candles for the LONGEST stretch that fits inside one narrow channel (not wider than about 4 average candle ranges), whose top and bottom edges are each touched at least twice
    and that price is still inside right now. If nothing is that neat, take the narrowest one found."""
    dt_ = (cs[1][0] - cs[0][0]) or 60
    a = atr(cs[-60:]) or 1e-9
    best, fallback = None, None
    for N in range(minN, min(maxN, len(cs) - 1) + 1, 2):
        w = cs[-N:]
        f = _fit_channel(w, dt_, a)
        top = f[1] + f[2] * (w[-1][0] - f[0])
        bot = f[3] + f[4] * (w[-1][0] - f[0])
        inside = bot - 0.3 * a <= w[-1][4] <= top + 0.3 * a
        share = sum(1 for x in w if f[3] + f[4] * (x[0] - f[0]) - 0.3 * a <= x[4] <= f[1] + f[2] * (x[0] - f[0]) + 0.3 * a) / N
        if relax:
            inside = bot - 0.8 * a <= w[-1][4] <= top + 0.8 * a
        if not inside or share < (0.75 if relax else 0.9):
            continue
        if fallback is None or f[6] / a < fallback[1]:
            fallback = (f, f[6] / a)
        if f[6] <= 4.0 * a and f[7] >= 2 and f[8] >= 2:
            best = f                                                  # keep going: the longest neat one wins
    pick = best or (fallback[0] if fallback else None)
    return None if pick is None else pick[:6]


TSTATE_PATH = None            # set when the page builder starts: where the locked tunnels are remembered between builds
TSTATE = {}


def tstate_load(path):
    global TSTATE_PATH, TSTATE
    TSTATE_PATH = path
    try:
        TSTATE = json.load(open(path, encoding="utf-8"))
    except Exception:
        TSTATE = {}


def tstate_save():
    if not TSTATE_PATH:
        return
    try:
        tmp = f"{TSTATE_PATH}.{os.getpid()}.tmp"
        json.dump(TSTATE, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, TSTATE_PATH)
    except Exception:
        pass


def narrow_tunnel(cs, key=None):
    """The tunnel we are riding now. Found once, then its two lines are LOCKED and nothing else is used until price breaks it. A break is two closes outside the same edge - or ONE close outside when RSI and the
    moving average both lean the same way (a change of character), which is faster. A bouncy tunnel must also have stood 6 minutes; a solid one breaks at once. After a break the new tunnel is looked for ONLY from
    the turning point that led to the break (the low before an upward break, the high before a downward one), so the old tunnel is not mixed into the new one. With a `key` the locked tunnel is remembered between builds:
    only the candles that arrived since the last build are checked, so it stays put until it really breaks."""
    n = len(cs)
    if n < 40:
        return None
    dt_ = (cs[1][0] - cs[0][0]) or 60
    hold_n = max(1, int(HOLD_SECONDS // dt_))
    a = atr(cs[-60:]) or 1e-9
    closes = [x[4] for x in cs]
    rs = rsi_list(closes)
    g15 = sma(closes, 15)
    by_t = {x[0]: i for i, x in enumerate(cs)}
    cur, born, start = None, None, max(30, n - 60)
    st = TSTATE.get(key) if key else None
    if st and st.get("last_t") in by_t and st.get("born_t") in by_t and n - by_t[st["last_t"]] < 600:
        cur = tuple(st["cur"]) if st.get("cur") else None
        born = by_t[st["born_t"]]
        start = by_t[st["last_t"]] + 1                                    # continue from where the last build stopped
    for e in range(start, n):
        sub = cs[:e + 1]
        brk = None
        if cur is not None:
            ta, tp, ts_, bp, bs_ = cur[:5]
            l2 = sub[-2:]
            up2 = all(x[4] > tp + ts_ * (x[0] - ta) + 0.3 * a for x in l2)
            dn2 = all(x[4] < bp + bs_ * (x[0] - ta) - 0.3 * a for x in l2)
            x = sub[-1]
            up1 = x[4] > tp + ts_ * (x[0] - ta) + 0.3 * a
            dn1 = x[4] < bp + bs_ * (x[0] - ta) - 0.3 * a
            rdir = rs[e] - rs[e - 3]
            tilt = g15[e] - g15[e - 5]
            fast_up = up1 and rdir > 2 and tilt > 0                      # one close out, and RSI + moving average agree
            fast_dn = dn1 and rdir < -2 and tilt < 0
            waiting = (e - born) < hold_n and bouncy(sub, cur)
            if not waiting and (up2 or dn2 or fast_up or fast_dn):
                brk = "up" if (up2 or fast_up) else "down"
        if brk:
            seg = sub[-45:]
            k = min(range(len(seg)), key=lambda i: seg[i][3]) if brk == "up" else max(range(len(seg)), key=lambda i: seg[i][2])
            since = len(seg) - k                                          # candles since that turning point
            nxt = find_narrow(sub, minN=6, maxN=max(8, since + 1))
            if not nxt:
                nxt = find_narrow(sub, minN=4, maxN=max(8, since + 1), relax=True)
            cur, born = nxt, e                                           # no new tunnel found at all = no tunnel (the broken one is not kept on screen)
        elif cur is None:
            cur, born = find_narrow(sub), e
    if key:
        TSTATE[key] = {"cur": list(cur) if cur else None, "last_t": cs[-1][0], "born_t": cs[born if born is not None else n - 1][0]}
    return cur


def tunnel(cs, W=120, key=None):
    return narrow_tunnel(cs, key)


def tunnel_old(cs, W=120):
    return tunnel_both(cs, W)[0]


def tunnel_svg(cs, main, opt, uid, dec, nshow=45, width=1100, height=760, flag=None, frames=None, live=None, target=None):
    """The Tunnel tab's own picture: just candles and the tunnel(s). Main = highlighted (yellow edges, color projected forward); optional = dashed white edges with a faint blue fill."""
    L_, R_, T_, B_ = 84, 14, 14, 46
    pw, ph = width - L_ - R_, height - T_ - B_
    vis = cs[-nshow:]
    step = vis[1][0] - vis[0][0]
    t0, t1 = vis[0][0], vis[-1][0] + step
    t_now = t1 - step / 2
    tmax = max(t1 + (t1 - t0) * 0.35, t_now + 900 + 2 * step)
    core = vis[-30:] if nshow <= 60 else vis
    lo = min(x[3] for x in core); hi = max(x[2] for x in core)
    if target and abs(target - vis[-1][4]) / vis[-1][4] < 0.06:
        lo, hi = min(lo, target), max(hi, target)
    for _t in (main, opt):                                            # keep the 15-minute projection on the page
        if _t and _t[5] != "pinch":
            for _f in ((_t[1], _t[2]), (_t[3], _t[4])):
                _v = _f[0] + _f[1] * (t_now + 900 - _t[0])
                lo, hi = min(lo, _v), max(hi, _v)
    pad = (hi - lo) * 0.12
    lo -= pad; hi += pad
    if nshow <= 60:                                                      # keep the live price in the MIDDLE of the box
        _pxm = vis[-1][4]; _half = max(_pxm - lo, hi - _pxm) * 1.1
        lo, hi = _pxm - _half, _pxm + _half
    sx = lambda t: L_ + (t - t0) / (tmax - t0) * pw
    sy = lambda v: T_ + (hi - v) / (hi - lo) * ph
    frames = frames or []
    xs = ",".join(f"{sx(fe):.1f}" for fe, _t in frames)
    o = [f'<svg id="{uid}" data-own="tun" data-live="{live or ""}" data-dec="{dec}" data-geo="{L_},{T_},{pw},{ph},{t0},{tmax},{lo},{hi},{step}" data-xs="{xs}" data-right="{width - R_}" data-n="{len(frames)}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="tunnel">',
         f'<defs><clipPath id="{uid}c"><rect x="{L_}" y="{T_}" width="{pw}" height="{ph}"/></clipPath></defs>',
         f'<rect width="{width}" height="{height}" fill="#0d1117"/>']
    for k in range(6):
        v = lo + (hi - lo) * k / 5
        o.append(f'<line x1="{L_}" y1="{sy(v):.1f}" x2="{width - R_}" y2="{sy(v):.1f}" stroke="#21262d"/><text x="{L_ - 6}" y="{sy(v) + 4:.1f}" text-anchor="end" font-size="15" fill="#8b949e">{v:,.{dec}f}</text>')
    o.append(time_axis_svg(t0, tmax, step, sx, T_, ph))
    bw = max(2.0, pw / (tmax - t0) * step * 0.62)
    mb = market_bias_candles(cs)[-len(vis):]
    o.append(f'<g class="tn-bias" clip-path="url(#{uid}c)">')
    for x, (bo, bh, bl, bc) in zip(vis, mb):
        cwid = pw / (tmax - t0) * step + 0.4
        o.append(f'<rect x="{sx(x[0]):.1f}" y="{sy(bh):.1f}" width="{cwid:.1f}" height="{max(sy(bl) - sy(bh), 1):.1f}" fill="{"#3fb950" if bc >= bo else "#f85149"}" fill-opacity="0.10"/>'
                 f'<rect x="{sx(x[0]):.1f}" y="{sy(max(bo, bc)):.1f}" width="{cwid:.1f}" height="{max(sy(min(bo, bc)) - sy(max(bo, bc)), 1):.1f}" fill="{"#3fb950" if bc >= bo else "#f85149"}" fill-opacity="0.28"/>')
    o.append("</g>")
    for x in vis:
        col = "#3fb950" if x[4] >= x[1] else "#f85149"
        cx = sx(x[0] + step / 2)
        o.append(f'<g class="cd" data-t="{x[0]}" clip-path="url(#{uid}c)"><line x1="{cx:.1f}" y1="{sy(x[2]):.1f}" x2="{cx:.1f}" y2="{sy(x[3]):.1f}" stroke="{col}" stroke-width="1"/>'
                 f'<rect x="{cx - bw / 2:.1f}" y="{sy(max(x[1], x[4])):.1f}" width="{bw:.1f}" height="{max(sy(min(x[1], x[4])) - sy(max(x[1], x[4])), 1):.1f}" fill="{col}"/></g>')
    o.append(f'<line x1="{sx(t1):.1f}" y1="{T_}" x2="{sx(t1):.1f}" y2="{T_ + ph}" stroke="#484f58" stroke-dasharray="4 4"/>')

    def draw(tun, cls, edge, dash, fill, fa_now, fa_fut, t_now=t_now):
        if not tun:
            return ""
        ta, tp, ts_, bp, bs_, kind, *_m = tun
        ts = max(ta, t0)
        te = min(tmax, _m[0]) if (kind == "pinch" and _m) else min(tmax, t_now + 900)            # the Tunnel tab projects 15 minutes ahead
        top = lambda t: tp + ts_ * (t - ta)
        bot = lambda t: bp + bs_ * (t - ta)
        fc = fill if fill else ("#e3b341" if kind == "pinch" else ("#f85149" if kind == "down" else "#3fb950"))
        g = (f'<g class="{cls}" clip-path="url(#{uid}c)">'
             f'<polygon points="{sx(ts):.1f},{sy(top(ts)):.1f} {sx(t_now):.1f},{sy(top(t_now)):.1f} {sx(t_now):.1f},{sy(bot(t_now)):.1f} {sx(ts):.1f},{sy(bot(ts)):.1f}" fill="{fc}" fill-opacity="{fa_now}"/>')
        if te > t_now:
            g += f'<polygon points="{sx(t_now):.1f},{sy(top(t_now)):.1f} {sx(te):.1f},{sy(top(te)):.1f} {sx(te):.1f},{sy(bot(te)):.1f} {sx(t_now):.1f},{sy(bot(t_now)):.1f}" fill="{fc}" fill-opacity="{fa_fut}"/>'
        for f in (top, bot):
            g += f'<line x1="{sx(ts):.1f}" y1="{sy(f(ts)):.1f}" x2="{sx(te):.1f}" y2="{sy(f(te)):.1f}" stroke="{edge}" stroke-width="1.5"{dash}/>'
        if kind == "pinch" and _m and _m[0] <= tmax:
            g += f'<circle cx="{sx(_m[0]):.1f}" cy="{sy(top(_m[0])):.1f}" r="6" fill="none" stroke="{edge}" stroke-width="1.8"/>'
        elif te >= t_now + 899 and cls == "tn-main":
            t15 = t_now + 900
            g += (f'<line x1="{sx(t15):.1f}" y1="{sy(top(t15)):.1f}" x2="{sx(t15):.1f}" y2="{sy(bot(t15)):.1f}" stroke="#ffd54a" stroke-width="2.5"/>'
                  f'<circle cx="{sx(t15):.1f}" cy="{sy(top(t15)):.1f}" r="4" fill="#ffd54a"/><circle cx="{sx(t15):.1f}" cy="{sy(bot(t15)):.1f}" r="4" fill="#ffd54a"/>'
                  f'<text x="{sx(t15) - 8:.1f}" y="{sy(top(t15)) - 8:.1f}" text-anchor="end" font-size="17" font-weight="800" fill="#ffd54a">+15 min: {top(t15):,.{dec}f}</text>'
                  f'<text x="{sx(t15) - 8:.1f}" y="{sy(bot(t15)) + 18:.1f}" text-anchor="end" font-size="17" font-weight="800" fill="#ffd54a">+15 min: {bot(t15):,.{dec}f}</text>')
        return g + "</g>"
    o.append(draw(opt, "tn-opt", "#e6edf3", ' stroke-dasharray="7 5"', "#58a6ff", 0.07, 0.12))
    o.append(draw(main, "tn-main", "#ffd54a", "", None, 0.16, 0.28))
    for k, (fe, ftun) in enumerate(frames):                              # the tunnel as it stood at each replay step
        o.append(draw(ftun, f"rf rf-{k}", "#ffd54a", "", None, 0.16, 0.28, t_now=fe - step / 2).replace('<g class="rf rf-' + str(k) + '"', '<g class="rf rf-' + str(k) + '" style="display:none"', 1))
    if frames:
        o.append(f'<rect class="mask" x="{width - R_}" y="{T_}" width="0" height="{ph}" fill="#0d1117"/>')
    if flag:                                                          # a ring round the newest candle when it has reached out through an edge
        x = vis[-1]
        cx = sx(x[0] + step / 2)
        cy = sy(x[2] if flag == "up" else x[3])
        o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="11" fill="none" stroke="#ffd54a" stroke-width="2"/>'
                 f'<text x="{cx - 16:.1f}" y="{cy + (-18 if flag == "up" else 28):.1f}" text-anchor="end" font-size="18" font-weight="800" fill="#ffd54a">{"reaching out through the ceiling" if flag == "up" else "reaching out through the floor"}</text>')
    o.append(f'' + WND_SVG.format(T_=T_, y2=T_ + ph, ya=T_ + 2, yb=T_ + 18) + f'')
    o.append(f'<g class="livepx" style="display:none"><line x1="{L_}" y1="0" x2="{width - R_}" y2="0" stroke="#58a6ff" stroke-width="1" stroke-dasharray="3 3"/><rect x="{width - R_ - 112}" y="-12" width="112" height="24" rx="4" fill="#1f6feb"/><text x="{width - R_ - 56}" y="6" text-anchor="middle" font-size="16" font-weight="800" fill="#ffffff"></text></g><g class="tgt" style="display:none"><line x1="{L_}" y1="0" x2="{width - R_}" y2="0" stroke="#ffffff" stroke-width="2.2" stroke-dasharray="12 6"/><rect x="{L_ + 4}" y="-25" width="262" height="23" rx="4" fill="#0d1117" stroke="#ffffff"/><text x="{L_ + 12}" y="-8" font-size="16" font-weight="800" fill="#ffffff"></text></g>')
    o.append("</svg>")
    return "".join(o)


def rsi_list(closes, n=14):
    gains = losses = 0.0
    out = [50.0] * len(closes)
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g, l_ = max(ch, 0), max(-ch, 0)
        if i <= n:
            gains += g / n; losses += l_ / n
        else:
            gains = (gains * (n - 1) + g) / n; losses = (losses * (n - 1) + l_) / n
        out[i] = 100 - 100 / (1 + gains / losses) if losses > 0 else 100.0
    return out


def character_check(cs, tun):
    """Early warning that the tunnel may be changing character: has the newest candle (or the one before it) reached out through the top or bottom line? -> None, or (side 'up'/'down', how it got out
    'wick' (only the wick) / 'close' (a close), closes outside so far (0-2), RSI now, RSI direction over the last 3 candles, moving-average tilt percent). Information only - it never moves the tunnel."""
    if not tun or len(cs) < 40:
        return None
    ta, tp, ts_, bp, bs_ = tun[:5]
    a = atr(cs[-60:]) or 1e-9
    top = lambda t: tp + ts_ * (t - ta)
    bot = lambda t: bp + bs_ * (t - ta)
    side = how = None
    for x in cs[-2:][::-1]:                                           # newest first
        if x[4] > top(x[0]) + 0.05 * a:
            side, how = "up", "close"; break
        if x[4] < bot(x[0]) - 0.05 * a:
            side, how = "down", "close"; break
        if x[2] > top(x[0]) + 0.05 * a:
            side, how = "up", "wick"; break
        if x[3] < bot(x[0]) - 0.05 * a:
            side, how = "down", "wick"; break
    if side is None:
        return None
    closes = [x[4] for x in cs]
    outside = 0
    for x in cs[-2:][::-1]:
        if (side == "up" and x[4] > top(x[0]) + 0.05 * a) or (side == "down" and x[4] < bot(x[0]) - 0.05 * a):
            outside += 1
        else:
            break
    rs = rsi_list(closes)
    rdir = rs[-1] - rs[-4]
    tilt = angle_pct(sma(closes, 15), cs)
    return side, how, outside, rs[-1], rdir, tilt


def character_html(cs, tun):
    r = character_check(cs, tun)
    if not r:
        return ""
    side, how, outside, rsi, rdir, tilt = r
    edge = "ceiling (top line)" if side == "up" else "floor (bottom line)"
    got = (f"the newest candles have CLOSED outside the {edge} ({outside} of the 2 closes it takes to break the tunnel)" if how == "close" else f"a candle has poked its wick through the {edge}, but has not closed outside it")
    rword = "rising" if rdir > 2 else ("falling" if rdir < -2 else "flat")
    tword = "FLAT" if abs(tilt) < 10 else ("UP" if tilt > 0 else "DOWN")
    tshow = tword if abs(tilt) > 50 or tword == "FLAT" else f"{tword} {abs(tilt):.0f}%"
    agree = (side == "up" and rdir > 2 and tilt > 10) or (side == "down" and rdir < -2 and tilt < -10)
    return (f"<p class='chg'><b style='color:#ffd54a'>POSSIBLE CHANGE OF CHARACTER:</b> {got}. RSI is {rsi:.0f} and {rword}; the moving average is tilting {tshow}. "
            + ("RSI and the moving average are both leaning the same way as the move out - which is what a real break usually shows. " if agree else "RSI and the moving average are not both leaning the same way as the move out yet. ")
            + "Information only: the tunnel itself stays where it is until price closes outside twice.</p>")


def tunnel_text(cs, main, opt, dec):
    def one(t, label):
        if not t:
            return f"<p><b>{label}:</b> none right now.</p>"
        ta, tp, ts_, bp, bs_, kind, *_m = t
        now = cs[-1][0]
        top, bot = tp + ts_ * (now - ta), bp + bs_ * (now - ta)
        px = cs[-1][4]
        pos = (px - bot) / (top - bot) * 100 if top > bot else 50
        when = dt.datetime.fromtimestamp(ta).strftime("%I:%M %p").lstrip("0")
        if kind == "pinch":
            ahead = (_m[0] - now) / ((cs[1][0] - cs[0][0]) or 60)
            wd0 = tp + ts_ * (ta - ta) - (bp + bs_ * (ta - ta))
            apex_px = tp + ts_ * (_m[0] - ta)
            return (f"<p><b>{label}:</b> a <b style='color:#e3b341'>PINCHING</b> tunnel since {when}: the falling top line and the rising bottom line are closing in on each other. "
                    f"Right now it is {top - bot:,.{dec}f} wide (it started {wd0:,.{dec}f} wide) - from {bot:,.{dec}f} up to {top:,.{dec}f} - and price is {pos:.0f}% of the way up it. "
                    f"The two lines meet in about {ahead:.0f} candles, around {apex_px:,.{dec}f}. The squeeze usually resolves with a break out of it, either way.</p>")
        t15 = now + 900
        return (f"<p><b>{label}:</b> a <b style='color:{'#3fb950' if kind == 'up' else '#f85149'}'>{'RISING' if kind == 'up' else 'FALLING'}</b> tunnel since {when}. "
                f"Right now it runs from {bot:,.{dec}f} (bottom) to {top:,.{dec}f} (top) and price is {pos:.0f}% of the way up it. "
                f"<b>Projected 15 minutes ahead</b> (only if the lines hold): bottom {bp + bs_ * (t15 - ta):,.{dec}f}, top {tp + ts_ * (t15 - ta):,.{dec}f}."
                + (" Price closed through the old " + ("ceiling, so that line is now the FLOOR and this tunnel reaches up to the next ceiling." if kind == "up" else "floor, so that line is now the CEILING and this tunnel reaches down to the next floor.") if _m else "") + "</p>")
    return one(main, "The tunnel we are riding")


def build_tunnel_tab(coin, tfc, dec):
    tfs = (("1m", "1 minute"), ("5m", "5 minute"), ("15m", "15 minute"), ("1h", "1 hour"))
    parts, btns = [], ""
    for i, (key, nm) in enumerate(tfs):
        cs = tfc[key]
        main, opt = tunnel(cs, key=f"{coin}|{key}"), None
        uid = f"{coin}tun{key}"
        tstep = cs[1][0] - cs[0][0]
        tframes = []
        for k_ in range(FRAMES):                                    # oldest first; the last frame is "now"
            e_ = len(cs) - 1 - 4 * (FRAMES - 1 - k_)
            tframes.append((cs[e_][0] + tstep, tunnel(cs[:e_ + 1])))
        rbar = (f'<div class="bar"><b>Replay:</b> <input type="range" min="0" max="{FRAMES - 1}" value="{FRAMES - 1}" style="width:340px;vertical-align:middle" oninput="rep(\'{uid}\',this.value)"> '
                '<span class="hint">drag left to go back in time - the tunnel as it stood every 4 candles, using only the candles that existed then (far right = now)</span></div>')
        parts.append(f'<div class="tunv tv-{key}" style="display:{"block" if i == 0 else "none"}">' + '<div class="zoomv" style="display:block">'
                     + tunnel_svg(cs, main, opt, uid, dec, flag=(character_check(cs, main) or [None])[0], frames=tframes, live=key, target=TARGETS.get(coin)) + rbar + "</div>" + tunnel_text(cs, main, opt, dec) + character_html(cs, main) + "</div>")
        btns += f'<button class="{"on" if i == 0 else ""}" onclick="setTunTf(\'{coin}\',\'{key}\',this)">{nm}</button>'
    ib = lambda key, first=False: f'<button class="ibtn" {"data-first=1 " if first else ""}title="What is this?" onclick="showInfo(this,\'{key}\')">i</button>'
    chk = lambda on, js: f'<label class="sw"><input type="checkbox" {"checked" if on else ""} onchange="{js}"><span></span></label>'
    rows = (f'<div class="irow wide"><div class="iname"><b>Tunnel</b><span>the one channel price is riding right now</span></div>{ib("tunnel", True)}<div class="ictl bar tunbar">{btns}</div></div>'
            f'<div class="irow"><div class="iname"><b>15-minute projection</b><span>where the lines would be in 15 minutes</span></div><div class="ictl"></div>{ib("proj")}</div>'
            f'<div class="irow"><div class="iname"><b>Change-of-character warning</b><span>gold ring + note when a candle reaches out</span></div><div class="ictl"></div>{ib("cc")}</div>'
            f'<div class="irow"><div class="iname"><b>Bias band</b><span>smoothed Heikin-Ashi band behind the candles</span></div><div class="ictl">{chk(False, "setTunBias(\'" + coin + "\',this.checked,this)")}</div></div>')
    return ('<div class="tun-drop nobias" data-coin="' + coin + '"><div class="pgrid"><div class="pleft">' + "".join(parts) + '</div><div class="pright"><div class="indgrid"><div class="indlist">' + rows + '</div><div class="infopanel"><div class="infoin"><p class="hint">Click the <b>i</b> beside any option to read about it.</p></div></div></div></div></div></div>')


def channel_svg(tun, t_now, sx, sy, t0, tmax):
    """Shade the tunnel from where it starts up to now, then project its colour on into the future: red when it slopes down, green when it slopes up, amber and narrowing when it is a PINCH
    (it stops where the two lines meet)."""
    if not tun:
        return ""
    ta, tp, ts_, bp, bs_, kind, *_m = tun
    ts = max(ta, t0)
    te = min(tmax, _m[0]) if (kind == "pinch" and _m) else min(tmax, t_now + max(t_now - ta, 0.3 * (tmax - t_now)))      # projected ahead only about as far as it has stood
    top = lambda t: tp + ts_ * (t - ta)
    bot = lambda t: bp + bs_ * (t - ta)
    col = "#e3b341" if kind == "pinch" else ("#f85149" if kind == "down" else "#3fb950")
    pts_now = f"{sx(ts):.1f},{sy(top(ts)):.1f} {sx(t_now):.1f},{sy(top(t_now)):.1f} {sx(t_now):.1f},{sy(bot(t_now)):.1f} {sx(ts):.1f},{sy(bot(ts)):.1f}"
    out = f'<polygon points="{pts_now}" fill="#ffffff" fill-opacity="0.10"/>'
    if te > t_now:
        out += f'<polygon points="{sx(t_now):.1f},{sy(top(t_now)):.1f} {sx(te):.1f},{sy(top(te)):.1f} {sx(te):.1f},{sy(bot(te)):.1f} {sx(t_now):.1f},{sy(bot(t_now)):.1f}" fill="#ffffff" fill-opacity="0.16"/>'
    out += (f'<line x1="{sx(ts):.1f}" y1="{sy(top(ts)):.1f}" x2="{sx(te):.1f}" y2="{sy(top(te)):.1f}" stroke="{col}" stroke-width="1.1" stroke-opacity="0.8"/>'
            f'<line x1="{sx(ts):.1f}" y1="{sy(bot(ts)):.1f}" x2="{sx(te):.1f}" y2="{sy(bot(te)):.1f}" stroke="{col}" stroke-width="1.1" stroke-opacity="0.8"/>')
    if kind == "pinch" and _m and _m[0] <= tmax:
        out += f'<circle cx="{sx(_m[0]):.1f}" cy="{sy(top(_m[0])):.1f}" r="5" fill="none" stroke="{col}" stroke-width="1.5"/>'
    return out


BIG_FIRST = ("1d", "1h", "30m", "15m", "5m", "1m")


def line_at(ln, t):
    return ln[1] + ln[2] * (t - ln[0])


def is_dup(ln, bigger, times, tol):
    """The same line already found on a BIGGER chart: same side, and within tol of it at every checked time."""
    return any(b[5] == ln[5] and all(abs(line_at(ln, t) - line_at(b, t)) <= tol for t in times) for b in bigger)


def dedupe(lines_by_tf, vis):
    """Go from the biggest chart down: a line on a smaller chart that sits on top of a bigger chart's line is dropped, so each line is drawn once, in the bigger chart's color."""
    step = vis[1][0] - vis[0][0]
    t0, t1 = vis[0][0], vis[-1][0] + step
    times = (t0, (t0 + t1) / 2, t1, t1 + (t1 - t0) * 0.28)
    tol = 0.35 * (atr(vis) or 1e-9)
    kept_big, out = [], {}
    for key in BIG_FIRST:
        mine = [ln for ln in lines_by_tf.get(key, []) if not is_dup(ln, kept_big, times, tol)]
        out[key] = mine
        kept_big = kept_big + mine
    return out, kept_big, times, tol


def line_svg(lines, color, w, sx, sy, t0, tmax, keep=None, ybounds=None):
    """ybounds = (top, bottom) in chart pixels: a line that lies completely above or below the picture (the plot area is clipped anyway) is not written at all - the page is ~3x lighter and redraws far faster."""
    out = []
    for ln_ in lines:
        (ta, pa, sl, touches, kind, side, *_rest) = ln_
        ts = max(ta, t0)
        t_end = _rest[0] if (_rest and _rest[0] is not None) else tmax              # a glove edge stops at the point it joins; the newest one runs on into the future
        if ybounds is not None:
            _y1, _y2 = sy(pa + sl * (ts - ta)), sy(pa + sl * (t_end - ta))
            if (_y1 < ybounds[0] and _y2 < ybounds[0]) or (_y1 > ybounds[1] and _y2 > ybounds[1]):
                continue
        dash = ' stroke-dasharray="9 6"' if kind == "project" else (' stroke-dasharray="2 5"' if kind == "floor" else (' stroke-dasharray="4 4"' if kind.startswith("body") else ""))
        extra = " ln-body" if kind.startswith("body") else (" ln-floor" if kind == "floor" else "")
        lw = w * 1.5 if kind == "live" else (1.0 if kind == "floor" else w)                    # the in-time lines are the heavy ones
        far = " ln-far" if (keep is not None and id(ln_) not in keep) else ""
        out.append(f'<line class="{"ln-up" if sl > 0 else ("ln-down" if sl < 0 else "ln-flat")}{extra}{far}" x1="{sx(ts):.1f}" y1="{sy(pa + sl * (ts - ta)):.1f}" x2="{sx(t_end):.1f}" y2="{sy(pa + sl * (t_end - ta)):.1f}" stroke="{color}" stroke-width="{lw}" '
                   f'stroke-linecap="round" stroke-opacity="{1.0 if touches >= 3 else 0.62}"{dash}/>')
    return "".join(out)


MACD_SETS = ((12, 26, 9), (5, 13, 4), (8, 17, 9), (19, 39, 9), (24, 52, 18))          # standard, two faster, two slower


def macd_avg(cs):
    """The MACD histogram of five settings, each in units of its own typical size (so they can be averaged), then averaged. One number per candle: above 0 = the fast average is above the slow one."""
    cl = [x[4] for x in cs]
    hs = []
    for f, s, g in MACD_SETS:
        m = [a - b for a, b in zip(ema_list(cl, f), ema_list(cl, s))]
        h = [a - b for a, b in zip(m, ema_list(m, g))]
        sc = ema_list([abs(x) for x in h], 200)
        hs.append([x / (y or 1e-12) for x, y in zip(h, sc)])
    return [sum(h[i] for h in hs) / len(hs) for i in range(len(cs))]


def macd_svg(base_cs, nshow, uid, width=1100, height=130):
    """A strip under the chart, lined up with the candles above it: bars of the averaged MACD histogram (green above zero, red below; solid when it is strong, see-through when weak)."""
    L_, R_, T_, B_ = 84, 14, 22, 8
    pw, ph = width - L_ - R_, height - T_ - B_
    vis = base_cs[-nshow:]
    off = len(base_cs) - len(vis)
    av = macd_avg(base_cs)[off:]
    step = vis[1][0] - vis[0][0]
    t0, t1 = vis[0][0], vis[-1][0] + step
    tmax = t1 + (t1 - t0) * 0.28                                   # the same width the chart above uses
    mx = max(1.0, max(abs(v) for v in av)) * 1.05
    sx = lambda t: L_ + (t - t0) / (tmax - t0) * pw
    zy = T_ + ph / 2
    sy = lambda v: zy - v / mx * (ph / 2)
    o = [f'<svg id="{uid}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="MACD average" style="cursor:default"><rect width="{width}" height="{height}" fill="#0d1117"/>',
         f'<line x1="{L_}" y1="{zy:.1f}" x2="{width - R_}" y2="{zy:.1f}" stroke="#30363d"/>']
    cw = max(1.0, pw / (tmax - t0) * step * 0.78)
    for x, v in zip(vis, av):
        col = "#3fb950" if v >= 0 else "#f85149"
        op = 0.35 + 0.65 * min(1.0, abs(v) / 1.0)
        y1, y2 = sorted((zy, sy(v)))
        o.append(f'<rect x="{sx(x[0] + step / 2) - cw / 2:.1f}" y="{y1:.1f}" width="{cw:.1f}" height="{max(y2 - y1, 0.8):.1f}" fill="{col}" fill-opacity="{op:.2f}"/>')
    o.append(f'<line x1="{sx(t1):.1f}" y1="{T_}" x2="{sx(t1):.1f}" y2="{T_ + ph}" stroke="#484f58" stroke-dasharray="4 4"/>')
    now_v, prev_v = av[-1], av[-4]
    word = "BULLISH side" if now_v >= 0 else "BEARISH side"
    mv = "rising" if now_v - prev_v > 0.15 else ("falling" if now_v - prev_v < -0.15 else "flat")
    stretch = " - stretched, has tended to snap back" if abs(now_v) > 1.0 else ""
    o.append(f'<text x="{L_ + 6}" y="15" font-size="17" font-weight="800" fill="{"#3fb950" if now_v >= 0 else "#f85149"}">MACD (average of 5 settings): {word}, {mv}{stretch}</text>')
    o.append("</svg>")
    return "".join(o)


def _time_marks(t0, tmax, step):
    """Nice tick times along the bottom: about 6 to 8 across the chart. -> [(time, interval_seconds)]"""
    span = tmax - t0
    iv = 172800
    for cand in (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800):
        if span / cand <= 8:
            iv = cand
            break
    first = (int(t0) // iv + 1) * iv
    return [(t, iv) for t in range(first, int(tmax), iv)]


def time_axis_svg(t0, tmax, step, sx, T_, ph, uid_unused=None):
    """Vertical grid + time labels (converted to the viewer's own time zone by the page script), and a faint dashed line at every 15-minute window boundary on the minute charts."""
    out = []
    for t, iv in _time_marks(t0, tmax, step):
        lab = dt.datetime.fromtimestamp(t).strftime("%I:%M %p").lstrip("0") if iv < 86400 else dt.datetime.fromtimestamp(t).strftime("%b %d")
        out.append(f'<line x1="{sx(t):.1f}" y1="{T_}" x2="{sx(t):.1f}" y2="{T_ + ph}" stroke="#1b222b"/><text class="tl" data-t="{t}" data-d="{1 if iv >= 86400 else 0}" x="{sx(t):.1f}" y="{T_ + ph + 22}" text-anchor="middle" font-size="15" fill="#8b949e">{lab}</text>')
    if step <= 300:
        for t in range(((int(t0) // 900) + 1) * 900, int(tmax), 900):
            out.append(f'<line x1="{sx(t):.1f}" y1="{T_}" x2="{sx(t):.1f}" y2="{T_ + ph}" stroke="#8a6d1d" stroke-opacity="0.55" stroke-dasharray="2 7"/>')
    return "".join(out)


WND_SVG = '''<g class="wnd" style="display:none"><line x1="0" y1="{T_}" x2="0" y2="{y2}" stroke="#ffa657" stroke-opacity="0.25" stroke-width="1" stroke-dasharray="3 6"/><line x1="0" y1="{T_}" x2="0" y2="{y2}" stroke="#ffa657" stroke-opacity="0.55" stroke-width="1" stroke-dasharray="6 5"/><rect x="0" y="{ya}" width="150" height="22" rx="4" fill="#3d2a0c" stroke="#ffa657"/><text x="0" y="{yb}" text-anchor="middle" font-size="15" font-weight="800" fill="#ffa657"></text></g>'''


def fvg_zones(cs, minw=0.5):
    """Open fair value gaps (the LuxAlgo three-candle gaps): (time of the middle candle, +1 bullish / -1 bearish, bottom, top) for every gap at least `minw` candle-ranges wide that no later candle has CLOSED through.
    Backtest (research/fvg_backtest.py, 130 days of 1-minute BTC + XRP): price comes back into a gap about as often as it comes back to any level the same distance away, so these are places to watch, not signals. Information only."""
    out = []
    n = len(cs)
    for i in range(32, n):
        h2, l2 = cs[i - 2][2], cs[i - 2][3]
        if cs[i][3] > h2 and cs[i - 1][4] > h2:
            d, bot, top = 1, h2, cs[i][3]
        elif cs[i][2] < l2 and cs[i - 1][4] < l2:
            d, bot, top = -1, cs[i][2], l2
        else:
            continue
        rg = sum(x[2] - x[3] for x in cs[i - 31:i - 1]) / 30 or 1e-9
        if top - bot < minw * rg:
            continue
        if any((x[4] < bot) if d == 1 else (x[4] > top) for x in cs[i + 1:]):
            continue
        out.append((cs[i - 1][0], d, bot, top))
    return out


def build_svg(coin, base_cs, lines_by_tf, emas, srs, frames, uid, dec, nshow, width=1100, height=960, bias=None, own='1m'):
    L_, R_, T_, B_ = 84, 14, 30, 46
    pw, ph = width - L_ - R_, height - T_ - B_
    n = len(base_cs)
    vis = base_cs[-nshow:]
    off = n - len(vis)
    step = vis[1][0] - vis[0][0]
    t0, t1 = vis[0][0], vis[-1][0] + step
    tmax = t1 + (t1 - t0) * 0.28
    core = vis[-30:] if nshow <= 60 else vis                              # the standard view follows the recent action; the zoomed-out views still fit everything
    lo = min(c[3] for c in core); hi = max(c[2] for c in core)
    _tg = TARGETS.get(coin)
    if _tg and abs(_tg - vis[-1][4]) / vis[-1][4] < 0.06:
        lo, hi = min(lo, _tg), max(hi, _tg)                              # the target line is always on the chart
    pad = (hi - lo) * 0.10
    lo -= pad; hi += pad
    if nshow <= 60:                                                      # keep the live price in the MIDDLE of the box, so the chart does not have to be dragged back
        _pxm = vis[-1][4]; _half = max(_pxm - lo, hi - _pxm) * 1.1
        lo, hi = _pxm - _half, _pxm + _half
    sx = lambda t: L_ + (t - t0) / (tmax - t0) * pw
    sy = lambda v: T_ + (hi - v) / (hi - lo) * ph
    xs = ",".join(f"{sx(fe):.1f}" for fe, _px, _fl, _rc in frames) if frames else ""
    o = [f'<svg id="{uid}" data-own="{own}" data-live="{own}" data-dec="{dec}" data-geo="{L_},{T_},{pw},{ph},{t0},{tmax},{lo},{hi},{step}" data-xs="{xs}" data-right="{width - R_}" data-n="{len(frames)}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{coin} candles with trend lines">',
         f'<defs><clipPath id="{uid}c"><rect x="{L_}" y="{T_}" width="{pw}" height="{ph}"/></clipPath>'
         f'<pattern id="{uid}fg" patternUnits="userSpaceOnUse" width="9" height="9" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="9" stroke="#2ee86b" stroke-width="1.3" stroke-opacity="0.75"/></pattern>'
         f'<pattern id="{uid}fr" patternUnits="userSpaceOnUse" width="9" height="9" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="9" stroke="#ff5d5d" stroke-width="1.3" stroke-opacity="0.75"/></pattern></defs>',
         f'<rect width="{width}" height="{height}" fill="#0d1117"/>']
    for k in range(6):
        v = lo + (hi - lo) * k / 5
        o.append(f'<line x1="{L_}" y1="{sy(v):.1f}" x2="{width - R_}" y2="{sy(v):.1f}" stroke="#21262d"/><text x="{L_ - 6}" y="{sy(v) + 4:.1f}" text-anchor="end" font-size="15" fill="#8b949e">{v:,.{dec}f}</text>')
    o.append(time_axis_svg(t0, tmax, step, sx, T_, ph))
    px_now = vis[-1][4]
    o.append(f'<g class="sr" clip-path="url(#{uid}c)" style="display:inline">')
    for st, h_, l_ in srs:
        col = "#3fb950" if px_now > h_ else ("#f85149" if px_now < l_ else "#8b949e")
        yt, yb = sy(h_), sy(l_)
        o.append(f'<rect x="{L_}" y="{yt:.1f}" width="{pw}" height="{max(yb - yt, 2):.1f}" fill="{col}" fill-opacity="{min(0.05 + st / 2000, 0.15):.2f}" stroke="{col}" stroke-opacity="0.3" stroke-width="1"/>')
    o.append("</g>")
    o.append(f'<g class="fvg" clip-path="url(#{uid}c)" style="display:inline">')                  # open fair value gaps: shaded zones reaching to the right edge until a candle closes through them
    try:
        zs = [z for z in fvg_zones(base_cs) if z[3] >= lo and z[2] <= hi]
        zs.sort(key=lambda z: abs((z[2] + z[3]) / 2 - px_now))
        for tz, dz, bz, tpz in zs[:8]:
            colz = "#2ee86b" if dz == 1 else "#ff5d5d"                                                  # a light white box with green (bull) or red (bear) diagonal lines inside - not a solid colour
            xa, ya, yb = max(L_, sx(tz)), sy(tpz), sy(bz)
            o.append(f'<rect x="{xa:.1f}" y="{ya:.1f}" width="{max(width - R_ - xa, 1):.1f}" height="{max(yb - ya, 2):.1f}" fill="#ffffff" fill-opacity="0.07" stroke="#ffffff" stroke-opacity="0.45" stroke-width="1" stroke-dasharray="5 4"/>'
                     f'<rect x="{xa:.1f}" y="{ya:.1f}" width="{max(width - R_ - xa, 1):.1f}" height="{max(yb - ya, 2):.1f}" fill="url(#{uid}{"fg" if dz == 1 else "fr"})"/>'
                     f'<text x="{width - R_ - 6}" y="{ya + 13:.1f}" text-anchor="end" font-size="12" font-weight="800" fill="{colz}" fill-opacity="0.9">{"BULL" if dz == 1 else "BEAR"} GAP</text>')
    except Exception as e_:
        print(f"[chart] fair value gaps skipped: {e_!r}")
    o.append("</g>")
    bw = max(2.0, pw / (tmax - t0) * step * 0.7)
    for c in vis:
        col = "#3fb950" if c[4] >= c[1] else "#f85149"
        x = sx(c[0] + step / 2)
        o.append(f'<g class="cd" data-t="{c[0]}" clip-path="url(#{uid}c)"><line x1="{x:.1f}" y1="{sy(c[2]):.1f}" x2="{x:.1f}" y2="{sy(c[3]):.1f}" stroke="{col}" stroke-width="1.6" stroke-opacity="0.95"/>'
                 f'<rect x="{x - bw / 2:.1f}" y="{sy(max(c[1], c[4])):.1f}" width="{bw:.1f}" height="{max(sy(min(c[1], c[4])) - sy(max(c[1], c[4])), 1):.1f}" fill="{col}" fill-opacity="1"/></g>')
    o.append(f'<line x1="{sx(t1):.1f}" y1="{T_}" x2="{sx(t1):.1f}" y2="{T_ + ph}" stroke="#484f58" stroke-dasharray="4 4"/>')
    if bias:                                                               # the market-bias strip along the bottom: green = bullish bias, red = bearish
        cw = pw / (tmax - t0) * step
        for j, x in enumerate(vis):
            bv_ = bias[off + j]
            o.append(f'<rect x="{sx(x[0]):.1f}" y="{T_ + ph - 6}" class="mbias" width="{cw + 0.3:.1f}" height="6" fill="{"#3fb950" if bv_ >= 0 else "#f85149"}" fill-opacity="{0.22 + 0.7 * min(1.0, abs(bv_) / (2 * BIAS_WEAK)):.2f}"/>')
        run = 0
        for v in reversed(bias):
            if (v >= 0) != (bias[-1] >= 0):
                break
            run += 1
        mins = run * step // 60
        span = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"
        o.append(f'<text class="mbias" x="{width - R_ - 6}" y="{T_ - 10}" text-anchor="end" font-size="18" font-weight="800" fill="{"#3fb950" if bias[-1] >= 0 else "#f85149"}">market bias: {"BULLISH" if bias[-1] >= 0 else "BEARISH"}{" (weak)" if abs(bias[-1]) < BIAS_WEAK else ""} <tspan font-weight="400" fill="#8b949e">(for {span})</tspan></text>')
    o.append(f'<g class="ema" clip-path="url(#{uid}c)">')
    for key, color, w in (("blue", "#4c8dff", 1.1), ("red", "#ff5d5d", 1.3), ("green", "#2ee86b", 1.8)):
        o.append('<polyline points="' + " ".join(f"{sx(c[0] + step / 2):.1f},{sy(emas[key][off + j]):.1f}" for j, c in enumerate(vis)) + f'" fill="none" stroke="{color}" stroke-width="{w}" stroke-linejoin="round"/>')
    # (the little up / down triangles where the green line crossed the red one were taken off at the owner's request)
    o.append("</g>")
    if frames:
        o.append(f'<rect class="mask" x="{width - R_}" y="{T_}" width="0" height="{ph}" fill="#0d1117"/>')       # the replay hides everything after the frame's candle
    for key, mins, color, w, on, keep in TFS:                      # the shaded tunnel of each timeframe, under the lines - only the one picked in the drop-down is visible (1m to start)
        o.append(f'<g class="ch ch-{key}" clip-path="url(#{uid}c)" style="display:{"inline" if key == "1m" else "none"}">' + channel_svg(lines_by_tf.get(key + '_t'), t1 - step / 2, sx, sy, t0, tmax) + "</g>")
    px_now, a_now, t_now_ = base_cs[-1][4], atr(base_cs[-60:]) or 1e-9, vis[-1][0]
    keep_ids = set()
    for key, mins, color, w, on, keep in TFS:                      # clean view: per timeframe, per side, the 3 strongest lines that are within 4 candle-ranges of price right now
        for sd in ("H", "L"):
            cand = []
            for ln_ in lines_by_tf.get(key, []):
                if ln_[5] != sd or ln_[4] == "floor" or str(ln_[4]).startswith("body"):
                    continue
                dist = abs(ln_[1] + ln_[2] * (t_now_ - ln_[0]) - px_now) / a_now
                if dist <= 4.0 and (ln_[3] >= 3 or (ln_[4] == "live" and dist <= 1.5)):
                    cand.append((dist, id(ln_)))
            for _d, i_ in sorted(cand)[:3]:
                keep_ids.add(i_)
    for key, mins, color, w, on, keep in TFS:
        o.append(f'<g class="tf tf-{key}" clip-path="url(#{uid}c)" style="display:{"inline" if on else "none"}">'
                 + line_svg(lines_by_tf.get(key, []), color, max(0.6, w * 0.3), sx, sy, t0, tmax, keep_ids, (T_ - 0.15 * ph, T_ + 1.15 * ph)) + "</g>")
    for k, (fe, fpx, fl, frc) in enumerate(frames):
        o.append(f'<g class="rf rf-{k}" clip-path="url(#{uid}c)" style="display:none">' + channel_svg(frc, fe - step / 2, sx, sy, t0, tmax) + line_svg(fl, next((t_[2] for t_ in TFS if t_[0] == own), "#ffd54a"), 0.7, sx, sy, t0, tmax, None, (T_ - 0.15 * ph, T_ + 1.15 * ph)) + "</g>")
    ang = angle_pct(emas["green"], base_cs)
    word = "FLAT" if abs(ang) < 10 else ("UP" if ang > 0 else "DOWN")
    col = "#9da7b3" if word == "FLAT" else ("#2ee86b" if ang > 0 else "#ff5d5d")
    o.append(f'<text x="{L_ + 8}" y="{T_ - 10}" font-size="20" font-weight="800" fill="{col}">moving average: {word}{"" if word == "FLAT" else f" {abs(ang):.0f}%"}{f" (tilt {ang:+.0f}%)" if word == "FLAT" else ""}</text>')
    o.append(f'' + WND_SVG.format(T_=T_, y2=T_ + ph, ya=T_ + 2, yb=T_ + 18) + f'')
    o.append(f'<g class="livepx" style="display:none"><line x1="{L_}" y1="0" x2="{width - R_}" y2="0" stroke="#58a6ff" stroke-width="1" stroke-dasharray="3 3"/><rect x="{width - R_ - 112}" y="-12" width="112" height="24" rx="4" fill="#1f6feb"/><text x="{width - R_ - 56}" y="6" text-anchor="middle" font-size="16" font-weight="800" fill="#ffffff"></text></g><g class="tgt" style="display:none"><line x1="{L_}" y1="0" x2="{width - R_}" y2="0" stroke="#ffffff" stroke-width="2.2" stroke-dasharray="12 6"/><rect x="{L_ + 4}" y="-25" width="262" height="23" rx="4" fill="#0d1117" stroke="#ffffff"/><text x="{L_ + 12}" y="-8" font-size="16" font-weight="800" fill="#ffffff"></text></g>')
    o.append("</svg>")
    return "".join(o)


def _load_json(name):
    try:
        return json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), encoding="utf-8"))
    except Exception:
        return None


DIST = _load_json("window_distance.json")


def _load_odds():
    try:
        return json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "window_odds.json"), encoding="utf-8"))
    except Exception:
        return None


ODDS = _load_odds()
AGREE_INFO = {}
AGREE_VOTES = {}                                            # coin -> the six indicator readings behind the 'N of 6 lean' chip, for the click-open list
IND_EXPLAIN = {
    "moving-average tilt": "The green line is a 15-candle moving average: the average closing price of the last 15 minutes. This checks how steeply it slopes. Sloping UP by more than a small amount counts as UP, DOWN the same way, and in between it is flat. It shows which way the short-term average is leaning. It does not say where price will go.",
    "green over/under red": "The green line (the 15-candle average) against the red line (the 34-candle average). Green above red means recent prices are higher than the longer average, so it leans UP. Green below red leans DOWN. When the two cross, that is the golden cross (up) or the death cross (down).",
    "price over/under blue": "The blue line is the 111-candle average, the long-term trend on this chart. Price above it leans UP, price below it leans DOWN.",
    "MACD (average of 5)": "MACD compares a fast average with a slow one. Above zero means the fast one is above the slow one (the bullish side), below zero the bearish side. This is the average of five readings, so it is smoother.",
    "RSI": "RSI measures how strong the recent buying or selling has been, on a scale of 0 to 100. Over 55 leans UP, under 45 leans DOWN, and in between there is no lean. Over 70 means stretched up, under 30 stretched down.",
    "tunnel direction": "The tunnel is the narrow channel price is moving inside right now. A rising tunnel leans UP, a falling tunnel leans DOWN, and when no tunnel is found there is no lean.",
}
_BUCKET_NAME = {"0-1": "0 or 1 of 6 agree", "2-3": "2 or 3 of 6 agree", "4": "4 of 6 agree", "5": "5 of 6 agree", "6": "all 6 agree"}


def _agree_votes(closed, cl):
    """The six readings (+1 up / -1 down / 0 none) on whichever candles are given - each chart size reads its OWN candles."""
    px = cl[-1]
    g, r, b = sma(cl, 15), sma(cl, 34), sma(cl, 111)
    tilt = angle_pct(g, closed)
    rs = rsi_list(cl)[-1]
    mc = macd_avg(closed)[-1]
    f = find_narrow(closed[-200:])
    votes = [("moving-average tilt", 1 if tilt > 10 else (-1 if tilt < -10 else 0), f"tilting {'UP' if tilt > 10 else ('DOWN' if tilt < -10 else 'flat')}"),
             ("green over/under red", 1 if g[-1] > r[-1] else -1, "green line above the red" if g[-1] > r[-1] else "green line below the red"),
             ("price over/under blue", 1 if px > b[-1] else -1, "price above the blue line" if px > b[-1] else "price below the blue line"),
             ("MACD (average of 5)", 1 if mc > 0 else -1, "bullish side" if mc > 0 else "bearish side"),
             ("RSI", 1 if rs > 55 else (-1 if rs < 45 else 0), f"{rs:.0f} - {'leaning up' if rs > 55 else ('leaning down' if rs < 45 else 'middle, no lean')}"),
             ("tunnel direction", 0 if f is None else (1 if f[5] == "up" else -1), "no tunnel" if f is None else ("rising tunnel" if f[5] == "up" else "falling tunnel"))]
    return votes


def _agree_other(closed, cl, coin, base):
    """The same six readings for the 15-minute or 1-hour chart, from that chart's own candles. (The 'how did windows like this end' history is only measured from the 1-minute chart, so it is not repeated here.)"""
    nm = {"15m": "15-minute", "1h": "1-hour"}.get(base, base)
    votes = _agree_votes(closed, cl)
    AGREE_VOTES[coin + "|" + base] = [[v[0], v[1], v[2], IND_EXPLAIN.get(v[0], "")] for v in votes]
    up, dn = sum(1 for v in votes if v[1] == 1), sum(1 for v in votes if v[1] == -1)
    dots = "".join(f'<i style="display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:3px;background:{"#3fb950" if v[1] == 1 else ("#f85149" if v[1] == -1 else "#6e7681")}" title="{v[0]}: {v[2]}"></i>' for v in votes)
    lead = "UP" if up > dn else ("DOWN" if dn > up else "split")
    col = "#3fb950" if up > dn else ("#f85149" if dn > up else "#8b949e")
    label = f'{dots} <b style="color:{col}">' + (f"{max(up, dn)} of 6 lean {lead}" if up != dn else f"split - {up} up, {dn} down") + f'</b> <span style="color:#8b949e">- {nm} chart</span>'
    info = (f"<p><b>Right now.</b> On the {nm} chart, {up} of the six indicators lean up, {dn} lean down, {6 - up - dn} are neutral.</p>"
            f"<p><b>What it counts.</b> Six readings from the {nm} candles, each leaning up, down or neutral: the moving-average tilt, whether the green line is above or below the red, whether price is above or below the blue line, "
            "the averaged MACD, RSI (over 55 leans up, under 45 leans down), and which way the tunnel runs. Every chart size works these out from its own candles, so the 1-minute, 15-minute and 1-hour charts each have their own reading. "
            "The dots in the row are those six, in that order (green = up, red = down, grey = neutral); hover a dot to read it.</p>"
            "<p class='hint'>The past-windows history (how often a window ended on the side price was on) was measured from the 1-minute chart only, so it is shown on the 1-minute chart's reading. "
            "Information only - it describes what the chart is doing and never tells anyone to do anything.</p>")
    AGREE_INFO[f"agree-{coin}-{base}"] = (f"Do the indicators agree? ({coin}, {nm} chart)", info)
    return label


def agree_live(cs, coin, base="1m", gran=60):
    """Six indicators, each leaning up (+1), down (-1) or neutral (0), on the 1-minute chart: moving-average tilt, green over/under red, price over/under blue, averaged MACD, RSI, tunnel direction.
    Where are we in the current 15-minute window, which side of the window's start is price on, and how did windows like this end in the past? -> (label html for the list row, explanation html for the 'i' panel)"""
    closed = cs[:-1] if cs[-1][0] + gran > time.time() else cs
    cl = [x[4] for x in closed]
    if base != "1m":
        return _agree_other(closed, cl, coin, base)
    j = len(closed) - 1
    t_last = closed[j][0]
    W = t_last - t_last % 900
    m = (t_last - W) // 60 + 1                                       # closed candles so far in this window
    k0 = j - (m - 1)
    open_px, px = closed[k0][1], cl[j]
    side = 1 if px > open_px else -1
    votes = _agree_votes(closed, cl)
    AGREE_VOTES[coin + "|1m"] = [[v[0], v[1], v[2], IND_EXPLAIN.get(v[0], "")] for v in votes]
    up, dn = sum(1 for v in votes if v[1] == 1), sum(1 for v in votes if v[1] == -1)
    dots ="".join(f'<i style="display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:3px;background:{"#3fb950" if v[1] == 1 else ("#f85149" if v[1] == -1 else "#6e7681")}" title="{v[0]}: {v[2]}"></i>' for v in votes)
    lead = "UP" if up > dn else ("DOWN" if dn > up else "split")
    col = "#3fb950" if up > dn else ("#f85149" if dn > up else "#8b949e")
    label = f'{dots} <b style="color:{col}">' + (f"{max(up, dn)} of 6 lean {lead}" if up != dn else f"split - {up} up, {dn} down") + '</b>'
    label += f' <span style="color:#8b949e">- price {abs(px - open_px) / (sum(x[2] - x[3] for x in closed[-30:]) / 30 or 1e-9):.1f} ranges {"above" if side == 1 else "below"} the window start</span>'
    agree = up if side == 1 else dn
    bucket = "0-1" if agree <= 1 else ("2-3" if agree <= 3 else str(agree))
    mm = min(13, max(2, m))
    left = 15 - m
    # how far price already is from the window's start, in recent candle ranges
    rng_now = sum(x[2] - x[3] for x in closed[-30:]) / 30 or 1e-9
    DIST_NOW = abs(px - open_px) / rng_now
    edges = (0.25, 0.5, 1.0, 1.5, 2.5)
    names = ("under 0.25", "0.25 to 0.5", "0.5 to 1", "1 to 1.5", "1.5 to 2.5", "over 2.5")
    dname = names[sum(1 for e in edges if DIST_NOW >= e)]
    now_line = (f"<p><b>Right now.</b> {m} minutes into the current 15-minute window (about {left}:00 left). Price is {'ABOVE' if side == 1 else 'BELOW'} where the window started, "
                f"<b>{DIST_NOW:.1f} candle-ranges</b> away from it (a candle range = the recent average high-to-low of one 1-minute candle). "
                f"{up} of the six indicators lean up, {dn} lean down, {6 - up - dn} are neutral.</p>")
    dtable = ""
    if DIST:
        cell = DIST["by_minute"].get(str(mm), {}).get(dname)
        if cell:
            now_line += (f"<p><b>What the history says about a spot like this.</b> At about minute {mm} of a window, with price on one side of the window's start and {dname} candle-ranges away from it, "
                         f"the window ended on that same side <b>{cell['p']:.0f}%</b> of the time ({cell['n']:,} past windows).</p>")
        rows_ = ""
        for mn in (3, 5, 8, 11, 13):
            d = DIST["by_minute"].get(str(mn), {})
            rows_ += f"<tr><td>minute {mn} ({15 - mn}:00 left)</td>" + "".join(f"<td>{d[nm]['p']:.0f}%</td>" if nm in d else "<td>-</td>" for nm in names) + "</tr>"
        dtable = ("<table class='stt'><tr><th>How far into the window</th>" + "".join(f"<th>{nm} ranges away</th>" for nm in names) + "</tr>" + rows_ + "</table>"
                  f"<p class='hint'>Each cell: how often the 15-minute window ENDED on the side of its start that price was already on, by how far from the start price was at that minute. {DIST['meta']['windows']:,} past windows of BTC and XRP "
                  f"(about {DIST['meta']['days_each']} days each, quarter-hour windows, one reading per window per minute).</p>")
    info = (now_line + "<p><b>What it counts.</b> Six readings from the 1-minute chart, each leaning up, down or neutral: the moving-average tilt, whether the green line is above or below the red, whether price is above or below the blue line, "
            "the averaged MACD, RSI (over 55 leans up, under 45 leans down), and which way the tunnel runs. The dots in the row are those six, in that order (green = up, red = down, grey = neutral); hover a dot to read it.</p>"
            "<p><b>The honest finding.</b> The 15-minute calls are decided by where price is when the window ENDS compared with where it started, so that is what was measured. When all six indicators agree with the side price is on, "
            "the window did end on that side more often (about 85% at minute 8, against 78% going by which side alone). But when I compared with windows where price was the SAME distance from the start, agreement added nothing "
            "(within about a point). Indicators tend to agree when price has already moved far, and the distance itself is what predicts the end. So this row is a description of how lined up the chart is; the number that carries the information is the distance below.</p>"
            + dtable + "<p class='hint'>Limits: the start price is Coinbase's price at the start of the window, not the exact reference Kalshi uses; about 60 days of one market mood; it says how windows ended, nothing about what a contract cost at that moment. "
            "Information only - it describes what happened before and never tells anyone to do anything.</p>")
    AGREE_INFO[f"agree-{coin}-1m"] = (f"Do the indicators agree? ({coin}, 1-minute chart)", info)
    return label


def ind_panel(coin, chips, agree=""):
    sel = ('<select onchange="setShade(\'' + coin + '\',this.value)" class="isel"><option value="1m" selected>1 minute tunnel</option><option value="5m">5 minute tunnel</option><option value="15m">15 minute tunnel</option>'
           '<option value="30m">30 minute tunnel</option><option value="1h">1 hour tunnel</option><option value="1d">1 day tunnel</option><option value="none">no shading</option></select>')

    def row(name, sub, ctl, key, wide=False):
        info = f'<button class="ibtn" title="What is this?" onclick="showInfo(this,\'{key}\')">i</button>' if key else ""
        if wide:
            return f'<div class="irow wide"><div class="iname"><b>{name}</b><span>{sub}</span></div>{info}<div class="ictl">{ctl}</div></div>'
        return f'<div class="irow"><div class="iname"><b>{name}</b><span>{sub}</span></div><div class="ictl">{ctl}</div>{info}</div>'
    chk = lambda on, js: f'<label class="sw"><input type="checkbox" {"checked" if on else ""} onchange="{js}"><span></span></label>'
    rows = (row("Trend lines", "one color per chart size", chips, "trend", True)
            + row("Floors, ceilings, body lines", "fine dotted levels", chk(True, "toggleFl(this.checked,this)"), "trend")
            + row("Support / resistance bands", "shaded zones price has turned at", chk(True, "toggleTf(\'" + coin + "\',\'sr\',this.checked)"), "sr")
            + row("Fair value gaps", "light box: green lines = bull gap, red lines = bear gap", chk(True, "toggleTf(\'" + coin + "\',\'fvg\',this.checked)"), "fvg")
            + row("Moving averages", "green (15), red (34) and blue (111)", chk(True, "toggleSel(this,\'.ema\')"), "ma")
            + row("Tunnel shading", "which chart size to shade", sel, "tunnel")
            + row("RSI", "strength meter (on the dashboard)", "", "rsi")
            + row("MACD (average of 5)", "strip under the chart", chk(True, "toggleSel(this,\'.macdpane\')"), "macd"))
    return ('<div class="indgrid"><div class="indlist">' + rows + '</div><div class="infopanel"><div class="infoin"><p class="hint">Click the <b>i</b> beside any indicator to read what it is, what its colors mean, and what price did afterwards.</p></div></div></div>')




def _fetch_target(coin_):
    """The current 15-minute round's target (the Kalshi line) from the running dashboard, so the chart's price range can include it. None if the dashboard is not answering."""
    try:
        import urllib.request
        j = json.load(urllib.request.urlopen(f"http://127.0.0.1:{os.environ.get('PORT', '8765')}/mtf_live?coin={coin_}", timeout=3))
        t_ = j.get("target")
        return float(t_) if t_ else None
    except Exception:
        return None


for _c in COINS:
    TARGETS[_c[0]] = _fetch_target(_c[0])
tstate_load(os.path.join(OUT_DIR, "tunnel_state.json") if (EMBED and OUT_DIR) else os.path.join(os.path.dirname(os.path.abspath(__file__)), "tunnel_state_preview.json"))
blocks = []
tfc_by_coin, lines_by_coin = {}, {}
for coin, sym, dec in COINS:
    tfc = {key: get_tf(sym, key) for key, *_ in TFS}
    lines = {t[0]: all_lines(tfc[t[0]], t[5], (2 if t[0] in ('1m', '5m') else 3)) for t in TFS}
    tfc_by_coin[coin] = tfc
    lines_by_coin[coin] = lines
    for t in TFS:
        lines[t[0] + '_t'] = tunnel(tfc[t[0]], key=f"{coin}|{t[0]}")
    views, stat_txt = [], ""
    for bi, (bname, bkey) in enumerate(BASES):
        cs = tfc[bkey]
        cl = [c[4] for c in cs]
        emas = {"green": sma(cl, MONEY_P), "red": sma(cl, RED_P), "blue": sma(cl, BLUE_P)}
        bias = None                                              # market bias removed (it measured as a coin flip)
        lines_v, _kept, _times, _tol = dedupe(lines, cs[-SHOW[bkey]:])
        for t in TFS:
            lines_v[t[0] + '_t'] = lines[t[0] + '_t']
        big_for_1m = [ln for k_ in BIG_FIRST[:-1] for ln in lines_v.get(k_, [])]
        bigger = [ln for k_ in BIG_FIRST[:BIG_FIRST.index(bkey)] for ln in lines_v.get(k_, [])]
        own_keep = next(t_[5] for t_ in TFS if t_[0] == bkey)
        own_L = 2 if bkey in ("1m", "5m") else 3
        frames = []
        step = cs[1][0] - cs[0][0]
        for k in range(FRAMES):                                  # oldest picture first; the last one is "now" (computed once, shared by every zoom level)
            e = len(cs) - 1 - FRAME_STEP * (FRAMES - 1 - k)
            frames.append((cs[e][0] + step, cs[e][4], [ln for ln in all_lines(cs[:e + 1], own_keep, own_L) if ln[4] == 'floor' or not is_dup(ln, bigger, _times, _tol)], tunnel(cs[:e + 1])))
        own_name = {"1m": "1-minute", "15m": "15-minute", "1h": "1-hour"}[bkey]
        own_col = {"1m": "yellow", "15m": "blue", "1h": "pink"}[bkey]

        def replay_bar(uid_):
            return (f'<div class="bar"><b>Replay:</b> <input type="range" min="0" max="{FRAMES - 1}" value="{FRAMES - 1}" style="width:340px;vertical-align:middle" oninput="rep(\'{uid_}\',this.value)"> '
                    f'<span class="hint">drag left to go back in time - the {own_col} {own_name} lines are redrawn every {FRAME_STEP} candles using only the candles that existed then (far right = now)</span></div>')
        srs = sr_channels(cs)
        if bkey == "1m":
            inner = ""
            for zi, nz in enumerate((45, 100, 200)):
                uid = f"{coin}{bkey}z{nz}"
                inner += f'<div class="zoomv zv-{nz}" style="display:{"block" if zi == 0 else "none"}">' + build_svg(coin, cs, lines_v, emas, srs, (frames if nz in (45, 100) else []), uid, dec, nz, bias=bias, own=bkey) + (replay_bar(uid) if nz in (45, 100) else '<div class="bar"><span class="hint">Replay is on the 45 and 100 candle views (keeps this page light and fast).</span></div>') + f'<div class="macdpane">{macd_svg(cs, nz, uid + "m")}</div>' + "</div>"
            zbar = ('<div class="bar zbar"><b>Candles shown:</b> <button class="on" onclick="setCandles(\'' + coin + '\',45,this)">45 (standard)</button>'
                    '<button onclick="setCandles(\'' + coin + '\',100,this)">100</button><button onclick="setCandles(\'' + coin + '\',200,this)">200 (zoomed out)</button></div>')
            body = inner + zbar
        else:
            uid = f"{coin}{bkey}"
            body = ('<div class="zoomv" style="display:block">' + build_svg(coin, cs, lines_v, emas, srs, [], uid, dec, SHOW[bkey], bias=bias, own=bkey) + '<div class="bar"><span class="hint">Replay is on the 1-minute chart (keeps this page light and fast).</span></div>'
                    + f'<div class="macdpane">{macd_svg(cs, SHOW[bkey], f"{coin}{bkey}m")}</div></div>')
        views.append(f'<div class="view v-{coin}-{bkey}" style="display:{"block" if bi == 0 else "none"}">{body}</div>')
        if bkey == "1m":
            st = cross_stats(cs, emas["green"], emas["red"])
            days = (cs[-1][0] - cs[0][0]) / 86400
            pct = lambda b: f"{b[0] / b[1] * 100:.0f}% of {b[1]}" if b[1] else "-"
            stat_txt = (f'<p class="stat"><b>How the green/red cross has done on the 1-minute chart (last {days:.1f} days):</b> after the green line crossed <b style="color:#2ee86b">UP</b> through the red, price was higher '
                        f'5 minutes later {pct(st["up"][5])} of the time and 15 minutes later {pct(st["up"][15])}. After it crossed <b style="color:#ff5d5d">DOWN</b>, price was lower 5 minutes later {pct(st["down"][5])} and 15 minutes later {pct(st["down"][15])}. (50% would be a coin flip.)</p>')
            stat_txt = ""                                    # removed at the owner's request 2026-10-24: a fixed 5/15-minute look-ahead is misleading, the calls are about where price is when the 15-minute window ENDS
    agree_label = "not available right now"
    chips_html = ""
    for _bi, (_bn, _bk) in enumerate(BASES):                    # each chart size reads its OWN six indicators; only the chip for the chart on show is visible
        try:
            _lab = agree_live(tfc[_bk], coin, _bk, {"1m": 60, "15m": 900, "1h": 3600}[_bk])
        except Exception as _ae:
            _lab = "not available right now"
            print("agree row failed:", _bk, repr(_ae))
        if _bi == 0:
            agree_label = _lab
        _v = __import__("html").escape(json.dumps(AGREE_VOTES.get(coin + "|" + _bk, [])), quote=True)
        chips_html += (f'<span class="agreechip" data-base="{_bk}" title="Click to see which indicators are green or red" data-votes="{_v}" style="display:{"inline-flex" if _bi == 0 else "none"}" '
                       f'onclick="showAgree(this,' + chr(39) + f'agree-{coin}-{_bk}' + chr(39) + f')">{_lab}<button class="ibtn" title="What is this?">i</button></span>')
    base_btns = "".join(f'<button class="{"on" if i == 0 else ""}" onclick="setBase(\'{coin}\',\'{b[1]}\',this)">{b[0]}</button>' for i, b in enumerate(BASES))
    tf_boxes = "".join(f'<label class="chip"><input type="checkbox" data-k="{key}" {"checked" if on else ""} onchange="toggleTf(\'{coin}\',\'{key}\',this.checked)"><i style="background:{color}"></i>{key}</label>' for key, mins, color, w, on, keep in TFS)
    lad_btns = "".join(f'<button class="{"on" if m == "both" else ""}" onclick="setLad(\'{coin}\',\'{m}\',this)">{nm}</button>' for m, nm in (("both", "Both ladders"), ("up", "Up ladder (rising lines)"), ("down", "Down ladder (falling lines)")))
    blocks.append(f'<section class="card lad-both"><h2>{coin}</h2><div class="modebar"><button class="on" onclick="setMode(\'{coin}\',\'pat\',this)">Patterns</button><button onclick="setMode(\'{coin}\',\'tun\',this)">Tunnel</button></div><div class="mpat"><div class="bar basebar">{base_btns}{chips_html}</div><div class="pgrid"><div class="pleft">{"".join(views)}<div class="bar2"><b>Show:</b> {lad_btns} <button class="fs" onclick="goFull(this)">Full screen</button> <button class="fs" style="background:#30363d;border-color:#484f58" onclick="toggleAll(this)">Show every line</button> <button class="fs" style="background:#30363d;border-color:#484f58" onclick="resetZoom(this)">Reset zoom</button> <span class="hint">scroll wheel = zoom - drag = move - double-click = reset</span></div>{stat_txt}</div><div class="pright">{ind_panel(coin, tf_boxes, agree_label)}</div></div></div><div class="mtun" style="display:none">{build_tunnel_tab(coin, tfc, dec)}</div></section>')

def _load_stats():
    try:
        return json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "indicator_stats.json"), encoding="utf-8"))
    except Exception:
        return None


STATS = _load_stats()


def _load_json(name):
    try:
        return json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), encoding="utf-8"))
    except Exception:
        return None


WSTATS = _load_json("window_stats.json")
DIST = _load_json("window_distance.json")


def SW(ind, state, m):
    """'85% (+2.5 points)': how often the window ENDED on the side price was on at minute m with this reading, and how far that is above windows the SAME distance from their start."""
    try:
        d = WSTATS["ind"][ind][state][str(m)]
        return f"{d['p']:.0f}% ({d['d']:+.1f} points)"
    except Exception:
        return "n/a"


def S(ind, state, tf, h):
    """'48% (-1.2 points)': how often price was higher h candles later, and how far that is from 'any time'."""
    try:
        d = STATS["ind"][ind][state][tf]
        return f"{d['up' + str(h)]}% ({d['d' + str(h)]:+.1f} points)"
    except Exception:
        return "n/a"


def stat_table(ind, order):
    """What happened at the END of the 15-minute window after each state of this indicator: how often the window ended on the side of its start that price was on at that moment, with the number in brackets showing the points above (+)
    or below (-) what windows the SAME distance from their start did anyway. Tinted only when that is bigger than chance AND the same in both halves of the data."""
    if not WSTATS:
        return ""
    st = WSTATS["ind"].get(ind, {})
    base = WSTATS["base"]
    mt = WSTATS["meta"]

    def cell(d):
        if not d:
            return "<td>-</td>"
        cls = (("sg" if d["d"] > 0 else "sr_") if d.get("sig") else "")
        return f'<td class="{cls}">{d["p"]:.0f}% <small>({d["d"]:+.0f})</small></td>'
    rows = ""
    for name in order:
        d = st.get(name)
        if not d:
            continue
        share = f"{d['share']}%" if d.get("share") is not None else f"{d['n']:,} times"
        rows += f"<tr><td>{name}</td><td>{share}</td>" + cell(d.get("5")) + cell(d.get("8")) + cell(d.get("11")) + "</tr>"
    brow = (f"<tr class='bl'><td><i>price's side only (the baseline)</i></td><td>100%</td><td>{base['5']['p']:.0f}%</td><td>{base['8']['p']:.0f}%</td><td>{base['11']['p']:.0f}%</td></tr>")
    return ("<table class='stt'><tr><th>When the chart showed...</th><th>how often</th><th>5 minutes into the window (10:00 left)</th><th>8 minutes in (7:00 left)</th><th>11 minutes in (4:00 left)</th></tr>" + rows + brow + "</table>"
            f"<p class='hint'>Each cell: with this showing at that point of a 15-minute window, how often the window ENDED on the same side of its start that price was on at that moment. The number in brackets is how many points that is above (+) or below (-) "
            f"windows where price was the same distance from the start but this was not showing, so a big move is not credited to the indicator. Green / red tint = bigger than chance would explain AND the same way in the first and second half of the data; untinted = noise. "
            f"{mt['windows']:,} past 15-minute windows of BTC and XRP (about {mt['days_each']} days each; support/resistance bands about {mt['sr_days_each']} days).</p>")


INFO_LIST = []


def _ind(title, body):
    INFO_LIST.append((title, body))
    return f'<details class="ind"><summary><b>{title}</b></summary><div class="indb">{body}</div></details>'


IND_HTML = ""
if True:
    G, R = "<b style='color:#3fb950'>green</b>", "<b style='color:#f85149'>red</b>"
    IND_HTML = ('<section class="card"><h2>Indicators on these charts</h2><details class="tun-drop"><summary><b>What every line, color and shade means - with the numbers</b> <span class="hint">- click to open (for members)</span></summary>'
        "<div class='note'><b>Read this first.</b> Every table below measures what the 15-minute calls depend on: at minute 5, 8 and 11 of a window, how often did the window END on the same side of its start that price was already on? "
        "The number in brackets is how many points that is above (+) or below (-) windows where price was the same distance from the start. The honest result: <b>how far price already is from the window's start explains almost everything, and none of these indicators adds more than a point or two on top of it.</b> "
        "They describe what price has done; they have not told the end of the window anything it was not already showing. Information only - not advice.</div>"
        if STATS else "") + (
        "<div class='note'><b>What the shading means everywhere.</b> <b>Solid / strong</b> = confirmed, strong, or happening now. <b>Translucent (see-through) / faded</b> = weak, still forming, or only lightly confirmed - for example a line that only two candles touch is faded, a support/resistance band with few touches is lighter. "
        "<b>Green</b> = leaning or sitting up / acting as a floor; <b>red</b> = leaning or sitting down / acting as a ceiling; <b>grey / amber</b> = neutral, flat or undecided. Shading to the right of the dashed 'now' line is a projection of where the current pattern points, not something that has happened.</div>"
        + _ind("2. Moving average (green line) and its tilt",
               "<p><b>What it is.</b> The green line is the average of the last 15 candle closes (a simple moving average, 15). It follows price closely, so it tilts quickly when the market turns.</p>"
               "<p><b>The tilt percent</b> shown at the top of the chart is how steeply the line is pointing: <b>0% = flat</b> (like 3 o'clock on a clock), <b>+100% = straight up</b> (12 o'clock), <b>-100% = straight down</b> (6 o'clock). Between -10% and +10% it is called flat. The scale adjusts to each chart, so 70% means about as steep as it gets on that chart.</p>"
               "<p><b>What the 15-minute window did afterwards:</b></p>" + stat_table("money_tilt", ["tilting steeply UP (over 50%)", "tilting UP (10% to 50%)", "FLAT (within 10%)", "tilting DOWN (10% to 50%)", "tilting steeply DOWN (over 50%)"])
               + f"<p><b>What that tells you.</b> The tilt describes a move that has already happened. The only reading that stood out is a steep tilt UP: at 8 minutes in, windows ended on the side price was on " + SW('money_tilt', 'tilting steeply UP (over 50%)', 8) + " - about 2 points above windows the same distance from the start. A steep tilt DOWN: " + SW('money_tilt', 'tilting steeply DOWN (over 50%)', 8) + ", flat: " + SW('money_tilt', 'FLAT (within 10%)', 8) + ". Small, so treat it as a description, not a signal.</p>")
        + _ind("3. Green vs red line, and the blue line",
               "<p><b>What they are.</b> The <b style='color:#ff5d5d'>red line</b> is the average of the last 34 closes and the <b style='color:#4c8dff'>blue line</b> is the average of the last 111 - slower lines that show the bigger lean. A small triangle on the chart marks each time the green line crossed the red line (green triangle = crossed up, red triangle = crossed down).</p>"
               "<p><b>What the 15-minute window did afterwards:</b></p>" + stat_table("money_vs_red", ["green line ABOVE the red line", "green line BELOW the red line"]) + stat_table("price_vs_blue", ["price ABOVE the blue line", "price BELOW the blue line"])
               + f"<p><b>What that tells you.</b> Nothing beyond where price already is. At 8 minutes in, with the green line above the red the window ended on the side price was on " + SW('money_vs_red', 'green line ABOVE the red line', 8) + "; below it, " + SW('money_vs_red', 'green line BELOW the red line', 8) + ". Price above the blue line: " + SW('price_vs_blue', 'price ABOVE the blue line', 8) + "; below: " + SW('price_vs_blue', 'price BELOW the blue line', 8) + ". The crosses (little triangles) are the same: a picture of a trend already under way.</p>")
        + _ind("4. Support / resistance bands (the shaded horizontal bands)",
               "<p><b>What they are.</b> Bands drawn where price has turned around several times before - swing highs and lows that bunch together. They are not live orders; they are zones the market has respected in the past, where many traders may have placed orders.</p>"
               f"<p><b>The colors.</b> {R} = price is below the band, so it has been acting as a <b>ceiling</b> (resistance). {G} = price is above the band, so it has been acting as a <b>floor</b> (support). <b>Grey</b> = price is inside the band right now. When price closes through a band it often switches roles - a broken ceiling becomes a floor and the other way round. "
               "A <b>darker</b> band has more swing points and touches in it (stronger); a <b>lighter / translucent</b> band has fewer.</p>"
               "<p><b>What the 15-minute window did afterwards:</b></p>" + stat_table("sr", ["price falls INTO a band from above (the band is acting as a floor)", "price rises INTO a band from below (the band is acting as a ceiling)"])
               + f"<p><b>What that tells you.</b> Bands are places where price has paused before, not walls. When price ran up into a band from below, the window ended on the side price was on " + SW('sr', 'price rises INTO a band from below (the band is acting as a ceiling)', 8) + " at 8 minutes in; after it fell into a band from above, " + SW('sr', 'price falls INTO a band from above (the band is acting as a floor)', 8) + ". Neither is different from windows without a band touch, so the bands are context, not a prediction.</p>")
        + _ind("5. Trend lines, floors and ceilings (the thin colored lines)",
               "<p><b>What they are.</b> Lines drawn through swing highs and swing lows, the way you would draw them by hand. They start at a swing and run through the later swings, and are only kept where the candles respect them. <b>One color per chart timeframe</b> (minute, 5 minute, 15 minute, 30 minute, hour, day); a line found on a bigger chart replaces the same line on a smaller one.</p>"
               "<p><b>Line styles.</b> <b>Bold</b> = three or more swing points sit on it. <b>Faded / translucent</b> = only two touch it. <b>Dashed</b> = a projection through the last two swings (where that line would run next). <b>Fine dotted horizontal lines</b> = floors and ceilings: levels where two candles sit at the same price, drawn at the wick and at the body of the candle. They stop where a candle closes through them. "
               "The <b>heavier lines</b> are the in-time trend lines of the last few minutes or hours.</p><p class='hint'>No percentages for these: they are the shapes that the tunnel and the bands are built from.</p>")
        + _ind("6. The tunnel (the shaded section, and the Tunnel screen)",
               f"<p><b>What it is.</b> The tunnel is the narrow channel price is moving in right now: a top line and a bottom line running side by side. It is the longest recent stretch of candles that fits between two parallel lines, with both lines touched at least twice and the gap no wider than about four average candle ranges. "
               f"{R} shading = the tunnel slopes down. {G} shading = it slopes up. The section to the right of the dashed 'now' line is the line pair carried 15 minutes ahead (see the projection note).</p>"
               "<p><b>Locked until it breaks.</b> Once found, the two lines stay exactly where they are. Only a break makes the chart look for a new tunnel. A break is two closes outside the same line - or one close outside when RSI and the moving average both lean the same way as the move out. "
               "A tunnel that has been bouncing around has to stand at least 6 minutes first; a clean one can break at once. After a break the new tunnel is looked for only from the turning point that led to it (the low before a break up, the high before a break down), so the old tunnel is not mixed in.</p>"
               "<p><b>What the 15-minute window did afterwards:</b></p>" + stat_table("tunnel", ["inside a RISING tunnel (green)", "inside a FALLING tunnel (red)"])
               + (f"<p><b>Does it hold?</b> 15 candles later price was still inside the same pair of lines only {STATS['tunnel_stay_inside_15'][0] / STATS['tunnel_stay_inside_15'][1] * 100:.0f}% of the time ({STATS['tunnel_stay_inside_15'][0]:,} of {STATS['tunnel_stay_inside_15'][1]:,} checks over the last 15 days of 1-minute candles). "
                  "So a tunnel is a good picture of where price has been moving, but it usually does not stay put for 15 more minutes - the lines get replaced. Treat the projection as 'where the lines would be if they held', not as a target.</p>" if STATS and STATS.get('tunnel_stay_inside_15') else "")
               + f"<p><b>What that tells you.</b> Inside a rising tunnel, at 8 minutes in, the window ended on the side price was on " + SW('tunnel', 'inside a RISING tunnel (green)', 8) + "; inside a falling one, " + SW('tunnel', 'inside a FALLING tunnel (red)', 8) + ". The tunnel shows where price has been moving; it did not change how the window ended.</p>")
        + _ind("7. RSI (the strength meter in the top bar of the dashboard)",
               "<p><b>What it is.</b> A number from 0 to 100 that measures how strongly price has moved up or down over the last 14 one-minute candles (the same RSI 14 you see on TradingView). About 50 is normal.</p>"
               f"<p><b>The colors and zones.</b> Over 70 = 'stretched up' (it has risen fast). Under 30 = 'stretched down' (it has fallen fast). Between 45 and 55 = the middle, nothing unusual. The dot on the strip shows where it is; the color follows the pressure. Stretched does not mean it must turn around.</p>"
               "<p><b>What the 15-minute window did afterwards:</b></p>" + stat_table("rsi", ["RSI over 70 (stretched up)", "RSI 55 to 70", "RSI 45 to 55 (middle)", "RSI 30 to 45", "RSI under 30 (stretched down)"])
               + f"<p><b>What that tells you.</b> RSI extremes do show up late in a big move: when RSI was over 70 or under 30 the window ended on the side price was on 91% to 95% of the time at 8 minutes in - but windows where price was just as far from its start did about the same, so the RSI itself adds nothing (RSI over 70: " + SW('rsi', 'RSI over 70 (stretched up)', 8) + "; under 30: " + SW('rsi', 'RSI under 30 (stretched down)', 8) + "). It is a sign that price has already moved a long way, which is exactly what the distance from the start already says.</p>")
        + _ind("8. MACD (not on the chart yet - included because people ask)",
               "<p><b>What it is.</b> Two moving averages compared: a fast one (12 candles) and a slow one (26). The MACD line is fast minus slow; the signal line is a 9-candle average of that; the histogram (the bars) is the gap between them. 'Bullish' = the MACD line crosses up through the signal line; 'bearish' = down through it.</p>"
               f"<p><b>The colors.</b> Histogram above zero (usually drawn {G}) = the fast average is above the slow one. Below zero (usually {R}) = the opposite. Lighter bars mean the gap is shrinking.</p>"
               "<p><b>What the 15-minute window did afterwards:</b></p>" + stat_table("macd", ["histogram ABOVE zero (bullish side)", "histogram BELOW zero (bearish side)"])
               + f"<p><b>What that tells you.</b> MACD above zero: the window ended on the side price was on " + SW('macd', 'histogram ABOVE zero (bullish side)', 8) + " at 8 minutes in; below zero: " + SW('macd', 'histogram BELOW zero (bearish side)', 8) + ". No difference - it describes the trend that already happened. Averaging five MACD settings (the strip under the chart) gave the same answer in a separate test.</p>")
        + "<p class='hint'>Information only - these tools describe what the chart has done. They are not advice and they never tell anyone to do anything. Past behavior, from a short stretch of data, is not a promise.</p></details></section>")


import re as _re
_notes = _re.findall(r"<div class='note'>.*?</div>", IND_HTML, _re.S)
_INFO = {}
if len(INFO_LIST) >= 7:
    for _k, _ix in (("ma", (0, 1)), ("sr", (2,)), ("trend", (3,)), ("tunnel", (4,)), ("rsi", (5,)), ("macd", (6,))):
        _t = " and ".join(_re.sub(r"^\d+\.\s*", "", INFO_LIST[i][0]) for i in _ix)
        _INFO[_k] = (_t, "".join(INFO_LIST[i][1] for i in _ix))
_INFO["fvg"] = ("Fair value gaps", "<p><b>What it is.</b> A fair value gap is a hole left when price jumps so fast that the first candle of a three-candle run and the third candle do not overlap. The zone is that hole, drawn as a light white box with lines inside: green lines (BULL GAP) mean it was left by a jump up, red lines (BEAR GAP) by a jump down. It is lightly shaded so it never hides the candles. "
                "A gap stays on the chart, reaching to the right, until a candle CLOSES through its far edge. Only gaps at least half a candle-range tall are drawn, the 8 nearest to price.</p>"
                "<p><b>What it did, tested on 130 days of 1-minute BTC and XRP.</b> Price came back into a gap within 15 minutes about 82% of the time, but it also comes back to any ordinary level the same distance away about 81% of the time, so most of that is just price moving around. "
                "On XRP the bigger gaps held a little better than ordinary levels (a full candle-range bounce 61% of the time against 57%), the same in both halves of the days; on BTC they were no different from ordinary levels. "
                "Open gaps near price did not change how often a 15-minute round ended on the other side of the line.</p><p class='hint'>Treat them as places to watch, not as signals. Information only.</p>")
_INFO["proj"] = ("15-minute projection", "<p><b>What it is.</b> The two tunnel lines are carried forward 15 minutes - the length of one 15-minute window - and the two prices they would reach are written at the end (top and bottom). "
                "The shading to the right of the dashed 'now' line is that projection.</p><p><b>How to read it.</b> It is only where the lines would be <i>if they hold</i>. It is not a forecast and it moves whenever the tunnel is rebuilt after a break. "
                "A young tunnel (only a few candles old) or a very steep one swings a lot, so its projection is the least reliable.</p>")
_INFO["cc"] = ("Change-of-character warning", "<p><b>What it is.</b> A gold ring around the newest candle, with a note above the chart, when a candle reaches out through the top or the bottom line of the tunnel. "
               "It tells you the tunnel might be changing; it does not change the tunnel.</p><p><b>What the note gives you.</b> Whether only the wick poked through or the candle actually closed outside, how many closes outside so far (a break needs two, or one when RSI and the moving average both lean the same way as the move), "
               "the RSI reading and which way it is moving, and which way the moving average is tilting.</p><p class='hint'>Information only - it describes what the chart is doing; it never says to do anything.</p>")
_INFO.update(AGREE_INFO)
INFOSTORE = '<div id="infostore" style="display:none">' + "".join(f'<div data-info="{k}" data-title="{_t.replace(chr(34), "")}">{b}</div>' for k, (_t, b) in _INFO.items()) + '<div data-info="intro">' + "".join(_notes[:2]) + '</div></div>'


now = dt.datetime.now().strftime("%I:%M %p").lstrip("0")
page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Trend lines and moving average - preview</title>
<style>
body{{margin:0;background:#0f1419;color:#e6edf3;font:16px/1.5 Segoe UI,Arial,sans-serif;padding:16px;max-width:1160px;margin:0 auto}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:20px;margin:0 0 8px}} .hint{{color:#8b949e;font-size:13px}} .stat{{font-size:14px;margin:10px 0 0;color:#c9d1d9}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:14px 16px;margin:14px 0}}
svg{{width:100%;height:auto;display:block;margin-top:8px;border-radius:8px;cursor:grab;touch-action:none;user-select:none}} svg line,svg polyline{{vector-effect:non-scaling-stroke}}
.bar{{margin:6px 0}} .bar button{{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:5px 14px;font-size:14px;cursor:pointer;margin-right:6px}}
.bar button.on{{background:#1f6feb;border-color:#58a6ff}}
.agreechip{{cursor:pointer;display:inline-flex;align-items:center;gap:8px;margin-left:12px;padding:4px 10px;border:1px solid #30363d;border-radius:8px;font-size:13px;background:#161b22;vertical-align:middle}} .agreechip .ibtn{{margin:0;padding:1px 8px}}
#agreepop{{position:fixed;left:12px;top:56px;width:min(560px,92vw);max-height:78vh;overflow:auto;z-index:50;background:#0d1117;border:2px solid #58a6ff;border-radius:12px;padding:12px 16px;box-shadow:0 14px 44px rgba(0,0,0,.75);font-size:14px}} #agreepop .agx{{float:right}} #agreepop .vrow{{display:flex;align-items:center;gap:8px;padding:7px 0;border-top:1px solid #21262d}} #agreepop .vdot{{display:inline-block;width:13px;height:13px;border-radius:50%;flex:0 0 13px}} #agreepop .vst{{font-weight:800}} #agreepop .vrow .vi{{margin-left:auto}} #agreepop .vexp{{background:#161b22;border-left:3px solid #58a6ff;padding:8px 12px;margin:2px 0 6px 21px;font-size:13px;color:#c9d1d9}}
.tfb{{display:inline-flex;align-items:center;gap:6px;margin-right:16px;font-size:14px;cursor:pointer}} .tfb i{{display:inline-block;width:34px;border-radius:3px}}
.noshade .rf polygon{{display:none}} .stt{{width:100%;border-collapse:collapse;font-size:13px;margin:6px 0}} .stt th,.stt td{{border-bottom:1px solid #30363d;padding:5px 6px;text-align:left;vertical-align:top}} .stt th{{color:#8b949e;font-weight:600}} .stt td.sg{{color:#3fb950;font-weight:700}} .stt td.sr_{{color:#f85149;font-weight:700}} .stt tr.bl td{{color:#8b949e}}
.ind{{margin:6px 0}} .ind summary{{cursor:pointer;font-size:15px;color:#58a6ff}} .indb{{margin:6px 0 10px 14px;font-size:14px;color:#c9d1d9}} .indb p{{margin:4px 0}}
.chg{{background:#2d2a12;border-left:3px solid #ffd54a;padding:6px 10px;margin:8px 0}}
.tun-drop{{margin-top:12px;border-top:1px solid #30363d;padding-top:8px}} .tun-drop summary{{cursor:pointer;font-size:16px}} .tunbar button,.tunbar2 button{{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:5px 14px;font-size:14px;cursor:pointer;margin-right:6px}} .tunbar button.on,.tunbar2 button.on{{background:#1f6feb;border-color:#58a6ff}}
.modebar{{margin:4px 0 10px}} .modebar button{{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:10px;padding:8px 26px;font-size:16px;font-weight:700;cursor:pointer;margin-right:8px}} .modebar button.on{{background:#1f6feb;border-color:#58a6ff}}
.indgrid{{display:grid;grid-template-columns:minmax(280px,380px) 1fr;gap:14px;margin:10px 0}} .indlist{{background:#0d1117;border:1px solid #30363d;border-radius:10px;padding:4px 10px}}
.irow{{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid #21262d}} .irow:last-child{{border-bottom:0}} .irow.wide{{flex-wrap:wrap}} .irow.wide .ictl{{order:3;flex:1 1 100%;justify-content:flex-start}} .iname{{flex:1;min-width:0}} .iname b{{display:block;font-size:14px}} .iname span{{display:block;font-size:12px;color:#8b949e}}
.ictl{{display:flex;flex-wrap:wrap;gap:4px;justify-content:flex-end}} .chip{{display:inline-flex;align-items:center;gap:4px;background:#161b22;border:1px solid #30363d;border-radius:12px;padding:2px 8px;font-size:12px;cursor:pointer}} .chip input{{display:none}} .chip i{{display:inline-block;width:14px;height:4px;border-radius:2px;opacity:.35}} .chip:has(input:checked){{border-color:#58a6ff;background:#1f2a3a}} .chip:has(input:checked) i{{opacity:1}}
.sw{{position:relative;display:inline-block;width:34px;height:18px}} .sw input{{display:none}} .sw span{{position:absolute;inset:0;background:#30363d;border-radius:18px;cursor:pointer;transition:.15s}} .sw span:before{{content:"";position:absolute;width:14px;height:14px;left:2px;top:2px;background:#e6edf3;border-radius:50%;transition:.15s}} .sw input:checked+span{{background:#238636}} .sw input:checked+span:before{{transform:translateX(16px)}}
.isel{{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:3px 6px;font-size:13px}}
.ibtn{{width:24px;height:24px;border-radius:50%;border:1px solid #58a6ff;background:#0d1117;color:#58a6ff;font:italic 700 14px Georgia,serif;cursor:pointer;flex:none}} .ibtn.on{{background:#1f6feb;color:#fff}}
.infopanel{{background:#0d1117;border:1px solid #30363d;border-radius:10px;padding:10px 14px;max-height:330px;overflow:auto}} .infoin h3{{margin:0 0 6px;font-size:16px;color:#e6edf3}} .infoin p{{margin:5px 0;font-size:14px;color:#c9d1d9}}
@media(max-width:760px){{.indgrid{{grid-template-columns:1fr}}}}
.pgrid{{display:block}} .pleft{{min-width:0}} .pright{{min-width:0;margin-top:12px}} .pright .indgrid{{margin-top:0}} .pright .infopanel{{max-height:520px}}
.nobias .tn-bias{{display:none}} .tm-main .tn-opt{{display:none}} .tm-opt .tn-main{{display:none}}
.card:not(.alllines) .ln-far{{display:none}} .nofl .ln-body, .nofl .ln-floor{{display:none}} .lad-up .ln-down{{display:none}} .lad-down .ln-up{{display:none}} .bar2{{margin:6px 0}} .bar2 button{{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:5px 14px;font-size:14px;cursor:pointer;margin-right:6px}} .bar2 button.on{{background:#238636;border-color:#3fb950}} .bar2 button.fs{{background:#1f6feb;border-color:#58a6ff;float:right}}
.card:fullscreen{{background:#0f1419;padding:18px;overflow:auto}} .card:fullscreen svg{{height:calc(100vh - 260px);width:100%}}
.note{{background:#2a2208;border:1px solid #d29922;border-radius:10px;padding:10px 14px;margin:10px 0;font-size:14px}}
</style></head><body>
<h1>Trend lines + moving average - preview</h1>
<p class="hint">Live Coinbase candles at {now}. The colored slanted lines are hand-drawn style trend lines, one color per chart timeframe (tick the boxes to show or hide each; no labels): each starts at a swing high or swing low and runs through the later swings, and is kept only if the candles respect it.
Bold = three or more swing points sit on it, faded = two. If the same line shows up on a bigger chart, only the bigger chart's line is drawn (in its color) - the smaller chart's copy is dropped. Green line = your moving average, red = your red line, blue = your slow line; a small triangle marks each moving-average cross of the red. Nothing here is on your dashboard or website yet.</p>
<div class="note"><b>SR channels (shaded bands behind the candles)</b> use your SRchannel settings: pivot 10, high/low, widest 5%, strength 1, up to 6, 290 candles back. They bunch together past swing highs and lows - zones price has turned at before. They are NOT a live feed of real orders. Red = price is below it (ceiling), green = price is above it (floor), grey = price is inside it.</div>
<div class="note"><b>The shaded tunnel</b> is the one we are in right now, drawn the way you draw it: it starts at the big recent high (or low), the top edge hugs the candles after it, and the bottom edge runs parallel through the opposite extreme, so every candle since then sits inside. Its color is projected on into the future: red when it slopes down, green when it slopes up. A single lonely candle is never drawn as a floor - a level needs two candles.</div>\n<div class="note"><b>Your MA Ribbon settings:</b> green SMA {MONEY_P}, red SMA {RED_P}, blue SMA {BLUE_P}, on the closing price (TradingView lists them as SMA, simple moving averages).</div>
{INFOSTORE}
{''.join(blocks)}
<p class="hint">Information only. It shows the shape of the market and tells nobody what to do.</p>
<script>
function card(c){{var r=null;[].forEach.call(document.querySelectorAll('.card'),function(x){{if(x.querySelector('h2').textContent===c)r=x}});return r}}
function rep(id,v){{var s=document.getElementById(id),n=+s.getAttribute('data-n'),xs=s.getAttribute('data-xs').split(','),m=s.querySelector('.mask'),right=+s.getAttribute('data-right');v=+v;
 var x=+xs[v];m.setAttribute('x',x);m.setAttribute('width',Math.max(0,right-x));
 var own=s.getAttribute('data-own')||'1m';var cb=s.closest('.card').querySelector('input[data-k="'+own+'"]');var on=cb?cb.checked:true;
 for(var k=0;k<n;k++){{var g=s.querySelector('.rf-'+k);if(g)g.style.display=(on&&k===v&&v<n-1)?'inline':'none'}}
 var st=s.querySelector(own==='tun'?'.tn-main':'.tf-'+own);if(st)st.style.display=(on&&v>=n-1)?'inline':'none';
 var isnow=(v>=n-1);s.setAttribute('data-replay',isnow?'':'1');if(!isnow){{[].forEach.call(s.querySelectorAll('g.livepx,g.tgt,g.wnd'),function(g){{g.style.display='none'}})}}}}
function toggleTf(c,k,on){{var cd=card(c);[].forEach.call(cd.querySelectorAll(k==='sr'?'.sr':(k==='fvg'?'.fvg':'.tf-'+k)),function(g){{g.style.display=on?'inline':'none'}});
 [].forEach.call(cd.querySelectorAll('svg[data-n][data-own="'+k+'"]'),function(s){{var z=s.closest('.zoomv');var sl=z&&z.querySelector('input[type=range]');if(sl)rep(s.id,sl.value)}})}}
function initZoom(svg){{var b=svg.viewBox.baseVal,base={{x:b.x,y:b.y,w:b.width,h:b.height}};svg._vb={{x:base.x,y:base.y,w:base.w,h:base.h}};svg._base=base;
 function setv(v){{svg.setAttribute('viewBox',v.x+' '+v.y+' '+v.w+' '+v.h);svg._vb=v}}
 svg._reset=function(){{setv({{x:base.x,y:base.y,w:base.w,h:base.h}})}};
 svg.addEventListener('wheel',function(e){{e.preventDefault();var pt=svg.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;var p=pt.matrixTransform(svg.getScreenCTM().inverse());var f=e.deltaY<0?0.82:1/0.82,v=svg._vb;
  var nw=Math.min(base.w,Math.max(base.w/16,v.w*f)),nh=nw*base.h/base.w,nx=p.x-(p.x-v.x)*(nw/v.w),ny=p.y-(p.y-v.y)*(nh/v.h);
  nx=Math.min(Math.max(nx,base.x),base.x+base.w-nw);ny=Math.min(Math.max(ny,base.y),base.y+base.h-nh);setv({{x:nx,y:ny,w:nw,h:nh}})}},{{passive:false}});
 var drag=null;
 svg.addEventListener('pointerdown',function(e){{drag={{x:e.clientX,y:e.clientY,k:1/svg.getScreenCTM().a,vb:{{x:svg._vb.x,y:svg._vb.y,w:svg._vb.w,h:svg._vb.h}}}};svg.setPointerCapture(e.pointerId);svg.style.cursor='grabbing'}});
 svg.addEventListener('pointermove',function(e){{if(!drag)return;var v=drag.vb,nx=v.x-(e.clientX-drag.x)*drag.k,ny=v.y-(e.clientY-drag.y)*drag.k;
  nx=Math.min(Math.max(nx,base.x),base.x+base.w-v.w);ny=Math.min(Math.max(ny,base.y),base.y+base.h-v.h);setv({{x:nx,y:ny,w:v.w,h:v.h}})}});
 svg.addEventListener('pointerup',function(){{drag=null;svg.style.cursor='grab'}});
 svg.addEventListener('dblclick',function(){{svg._reset()}})}}
function resetZoom(btn){{[].forEach.call(btn.closest('.card').querySelectorAll('svg'),function(s){{if(s._reset)s._reset()}})}}
function setCandles(c,n,btn){{var cd=card(c);[].forEach.call(cd.querySelectorAll('.zoomv'),function(v){{v.style.display=v.classList.contains('zv-'+n)?'block':'none'}});[].forEach.call(btn.closest('.bar').querySelectorAll('button'),function(x){{x.classList.toggle('on',x===btn)}})}}
function setTunTf(c,k,btn){{var cd=card(c);[].forEach.call(cd.querySelectorAll('.tunv'),function(v){{v.style.display=v.classList.contains('tv-'+k)?'block':'none'}});[].forEach.call(btn.closest('.bar').querySelectorAll('button'),function(x){{x.classList.toggle('on',x===btn)}})}}
function setTunBias(c,on,btn){{var cd=card(c);var d=cd.querySelector('.tun-drop');d.classList.toggle('nobias',!on)}}
function setMode(c,m,btn){{var cd=card(c);cd.querySelector('.mpat').style.display=(m==='pat')?'block':'none';cd.querySelector('.mtun').style.display=(m==='tun')?'block':'none';[].forEach.call(btn.closest('.modebar').querySelectorAll('button'),function(x){{x.classList.toggle('on',x===btn)}})}}
window.addEventListener('load',function(){{[].forEach.call(document.querySelectorAll('.ibtn[data-first]'),function(b){{showInfo(b,'tunnel')}})}});
function setTunShow(c,m,btn){{var cd=card(c);var d=cd.querySelector('.tun-drop');d.classList.remove('tm-main','tm-opt');if(m!=='both')d.classList.add('tm-'+m);[].forEach.call(btn.closest('.bar').querySelectorAll('button'),function(x){{x.classList.toggle('on',x===btn)}})}}
function goFull(btn){{var cd=btn.closest('.card');if(document.fullscreenElement){{document.exitFullscreen()}}else if(cd.requestFullscreen){{cd.requestFullscreen()}}}}
function setShade(c,v){{var cd=card(c);[].forEach.call(cd.querySelectorAll('.ch'),function(g){{g.style.display=(v!=='none'&&g.classList.contains('ch-'+v))?'inline':'none'}});cd.classList.toggle('noshade',v!=='1m')}}
function toggleAll(btn){{var cd=btn.closest('.card');var on=cd.classList.toggle('alllines');btn.textContent=on?'Clean view':'Show every line'}}
function toggleSel(el,sel){{var cd=el.closest('.card');[].forEach.call(cd.querySelectorAll(sel),function(g){{g.style.display=el.checked?'':'none'}})}}
function showAgree(el,key){{var chip=el.closest?el.closest('.agreechip'):el;var s=document.querySelector('#infostore [data-info="'+key+'"]');var old=document.getElementById('agreepop');if(old){{old.remove();if(old.getAttribute('data-k')===key)return}}var votes=[];try{{votes=JSON.parse(chip.getAttribute('data-votes')||'[]')}}catch(e){{}}var h='<button class="agx" onclick="this.parentNode.remove()">close</button><h3>Which indicators lean up or down</h3><p class="hint">Green leans UP, red leans DOWN, grey has no lean. Click the i beside an indicator to read what it is.</p>';votes.forEach(function(v){{var col=v[1]===1?'#3fb950':(v[1]===-1?'#f85149':'#6e7681');h+='<div class="vrow"><i class="vdot" style="background:'+col+'"></i><b>'+v[0]+'</b><span class="vst" style="color:'+col+'">'+(v[1]===1?'UP':(v[1]===-1?'DOWN':'no lean'))+'</span><span class="hint"> '+v[2]+'</span><button class="ibtn vi" title="What is this indicator?">i</button></div><div class="vexp" style="display:none">'+(v[3]||'')+'</div>'}});if(s)h+='<h3 style="margin-top:14px">What the history says</h3>'+s.innerHTML;var d=document.createElement('div');d.id='agreepop';d.setAttribute('data-k',key);d.innerHTML=h;d.addEventListener('click',function(e){{var t=e.target;if(t.classList&&t.classList.contains('vi')){{var n=t.parentNode.nextSibling;n.style.display=n.style.display==='block'?'none':'block'}}}});document.body.appendChild(d)}}
function showInfo(btn,key){{var cd=btn.closest('.indgrid');var s=document.querySelector('#infostore [data-info="'+key+'"]');if(!s)return;cd.querySelector('.infoin').innerHTML='<h3>'+(s.getAttribute('data-title')||'')+'</h3>'+s.innerHTML;cd.querySelector('.infopanel').scrollTop=0;[].forEach.call(cd.querySelectorAll('.ibtn'),function(b){{b.classList.toggle('on',b===btn)}})}}
function toggleFl(on,el){{var cd=el.closest('.card');cd.classList.toggle('nofl',!on)}}
function setLad(c,m,btn){{var cd=card(c);cd.className='card lad-'+m+(cd.classList.contains('nofl')?' nofl':'')+(cd.classList.contains('alllines')?' alllines':'');[].forEach.call(cd.querySelectorAll('.bar2 button'),function(x){{x.classList.toggle('on',x===btn)}})}}
function setBase(c,b,btn){{var cd=card(c);[].forEach.call(cd.querySelectorAll('.view'),function(v){{v.style.display=v.className.indexOf('v-'+c+'-'+b)>=0?'block':'none'}});[].forEach.call(cd.querySelectorAll('.basebar button'),function(x){{x.classList.toggle('on',x===btn)}});[].forEach.call(cd.querySelectorAll('.agreechip'),function(x){{x.style.display=x.getAttribute('data-base')===b?'inline-flex':'none'}});var sel=cd.querySelector('.isel');if(sel){{sel.value=b;}}setShade(c,b)}}
[].forEach.call(document.querySelectorAll('.view svg'),initZoom);
(function(){{var SW='.card .sw input, .card label.chip input';
 function key(x){{var cd=x.closest('.card'),l=[].slice.call(cd.querySelectorAll(SW));return 'mtfchk:'+cd.querySelector('h2').textContent+':'+l.indexOf(x)}}
 [].forEach.call(document.querySelectorAll(SW),function(x){{var v=null;try{{v=localStorage.getItem(key(x))}}catch(e){{}}
  if(v!==null&&(v==='1')!==x.checked){{x.checked=v==='1';x.dispatchEvent(new Event('change'))}}}});
 document.addEventListener('change',function(ev){{var x=ev.target;if(!x||!x.matches||!x.matches(SW))return;try{{localStorage.setItem(key(x),x.checked?'1':'0')}}catch(e){{}}}},true)}})();
</script></body></html>"""
EMBED_CSS = """.zoomv > svg{max-height:var(--fit,none)} body{padding:0 !important;margin:0} .card{background:transparent !important;border:0 !important;padding:0 !important;margin:0 !important} .card > h2{display:none} .modebar{display:none} .mback{float:right;background:#21262d;color:#e6edf3;border:1px solid #58a6ff;border-radius:10px;padding:8px 16px;font-size:14px;font-weight:700;cursor:pointer}"""
tstate_save()


def narr_json(coin_, dec_, cs1, lines_):
    """What the narrator watches on this coin: the floors / ceilings / strong trend lines near price, the average candle range and the number of decimals. -> JSON text"""
    import time as _t
    now_ = _t.time()
    px = cs1[-1][4]
    rng = sum(x[2] - x[3] for x in cs1[-31:-1]) / 30 or 1e-9
    out = []
    for tfk in ("15m", "1h"):                                      # only the MAJOR levels are spoken (owner's request): big floors / ceilings tested several times, plus the tunnel edges below
        for ln in lines_.get(tfk, []):
            ta, pa, sl, touches, kind, side = ln[:6]
            if kind == "floor" and touches >= 4:
                lab = "ceiling" if side == "H" else "floor"
            else:
                continue
            v = pa + sl * (now_ - ta)
            if abs(v - px) / px > 0.012:
                continue
            out.append({"p": round(pa, 6), "s": sl, "t": ta, "k": lab, "tf": tfk, "n": touches, "d": abs(v - px)})
    for tfk in ("1m", "5m", "15m"):                               # the tunnel's two edges come first: they are the "surface" the owner watches
        tun = lines_.get(tfk + "_t")
        if tun:
            ta, tp, ts_, bp, bs_ = tun[:5]
            for lab, pa, sl in (("tunnel top", tp, ts_), ("tunnel bottom", bp, bs_)):
                if abs(pa + sl * (now_ - ta) - px) / px <= 0.012:
                    out.append({"p": round(pa, 6), "s": sl, "t": ta, "k": lab, "tf": tfk, "n": 9, "d": -1})
    out.sort(key=lambda x: x["d"])
    keep, seen = [], []
    for x in out:
        v = x["p"] + x["s"] * (now_ - x["t"])
        if any(abs(v - y) < 0.2 * rng for y in seen):
            continue
        if not x["k"].startswith("tunnel") and (sum(1 for y in keep if not y["k"].startswith("tunnel")) >= 4 or (x["tf"] == "15m" and x["n"] < 6)):
            continue                                             # beyond the tunnels, only the four nearest really major levels (1 hour, or tested 6+ times on 15 minutes)
        seen.append(v)
        keep.append({k: x[k] for k in ("p", "s", "t", "k", "tf", "n")})
        if len(keep) >= 18:
            break
    return json.dumps({"coin": coin_, "dec": dec_, "range": rng, "levels": keep})


EMBED_JS = r"""
(function(){
 var COIN=(document.querySelector('.card h2')||{}).textContent||'';
 function postH(){try{parent.postMessage({mtf:1,coin:COIN,h:Math.ceil(document.body.offsetHeight)},'*')}catch(e){}}
 window.addEventListener('load',postH);setInterval(postH,700);
 var lastPtr=0;['pointerdown','pointermove','wheel','keydown'].forEach(function(t){window.addEventListener(t,function(){lastPtr=Date.now()},{passive:true})});
 var SEL=['.view','.zoomv','.tunv','.mpat','.mtun','.agreechip'];
 function snap(cd){var s={cls:cd.className,chk:[],sel:[],on:[],rng:[],disp:{},info:'',all:null,vb:{}};
  [].forEach.call(cd.querySelectorAll('input[type=checkbox]'),function(x){s.chk.push(x.checked)});
  [].forEach.call(cd.querySelectorAll('select'),function(x){s.sel.push(x.value)});
  [].forEach.call(cd.querySelectorAll('input[type=range]'),function(x){s.rng.push(x.value)});
  [].forEach.call(cd.querySelectorAll('button'),function(x){s.on.push(x.classList.contains('on'))});
  SEL.forEach(function(q){s.disp[q]=[].map.call(cd.querySelectorAll(q),function(x){return x.style.display})});
  [].forEach.call(cd.querySelectorAll('svg[id]'),function(x){s.vb[x.id]=x.getAttribute('viewBox')});
  var ib=cd.querySelector('.infoin');s.info=ib?ib.innerHTML:'';var ta=cd.querySelector('button[onclick*="toggleAll"]');s.all=ta?ta.textContent:null;return s}
 function apply(cd,s){cd.className=s.cls;
  [].forEach.call(cd.querySelectorAll('input[type=checkbox]'),function(x,i){if(s.chk[i]!==undefined&&x.checked!==s.chk[i]){x.checked=s.chk[i];x.dispatchEvent(new Event('change'))}});
  [].forEach.call(cd.querySelectorAll('select'),function(x,i){if(s.sel[i]!==undefined&&x.value!==s.sel[i]){x.value=s.sel[i];x.dispatchEvent(new Event('change'))}});
  [].forEach.call(cd.querySelectorAll('input[type=range]'),function(x,i){if(s.rng[i]!==undefined){x.value=s.rng[i];x.dispatchEvent(new Event('input'))}});
  [].forEach.call(cd.querySelectorAll('button'),function(x,i){if(s.on[i]!==undefined)x.classList.toggle('on',s.on[i])});
  SEL.forEach(function(q){[].forEach.call(cd.querySelectorAll(q),function(x,i){if(s.disp[q][i]!==undefined)x.style.display=s.disp[q][i]})});
  var ib=cd.querySelector('.infoin');if(ib&&s.info)ib.innerHTML=s.info;var ta=cd.querySelector('button[onclick*="toggleAll"]');if(ta&&s.all)ta.textContent=s.all;
  [].forEach.call(cd.querySelectorAll('.view svg'),initZoom);
  Object.keys(s.vb||{}).forEach(function(id){var sv=document.getElementById(id);if(sv&&sv._vb&&s.vb[id]){var q=s.vb[id].split(' ').map(Number);if(q.length===4&&q[2]>0){sv.setAttribute('viewBox',s.vb[id]);sv._vb={x:q[0],y:q[1],w:q[2],h:q[3]}}}})}
 function refresh(){if(document.fullscreenElement||Date.now()-lastPtr<5000||document.hidden)return;
  fetch(location.href,{cache:'no-store'}).then(function(r){return r.text()}).then(function(t){
   var d=new DOMParser().parseFromString(t,'text/html');var nc=d.querySelector('.card');var oc=document.querySelector('.card');if(!nc||!oc||document.fullscreenElement)return;
   var nb=d.querySelector('meta[name=build]'),ob=document.querySelector('meta[name=build]');if(nb&&ob){if(nb.content===ob.content)return;ob.content=nb.content}
   var s=snap(oc);var ni=d.getElementById('infostore'),oi=document.getElementById('infostore');if(ni&&oi)oi.innerHTML=ni.innerHTML;var nn=d.getElementById('narr-data'),on_=document.getElementById('narr-data');if(nn&&on_){on_.textContent=nn.textContent;loadNarr()}
   oc.replaceWith(nc);var cd=document.querySelector('.card');apply(cd,s);if(lastLive)applyLive(lastLive);
   var nh=d.querySelector('p.hint:last-of-type'),oh=document.querySelector('p.hint:last-of-type');if(nh&&oh)oh.innerHTML=nh.innerHTML}).catch(function(){})}
 setInterval(refresh,8000);
 function fixTimes(){[].forEach.call(document.querySelectorAll('text.tl'),function(t){var d=new Date(+t.getAttribute('data-t')*1000);t.textContent=t.getAttribute('data-d')==='1'?d.toLocaleDateString([],{month:'short',day:'numeric'}):d.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'})})}
 window.addEventListener('load',fixTimes);setInterval(fixTimes,15000);
 window.addEventListener('message',function(e){var d=e.data;if(d&&d.mtfFit!==undefined){document.documentElement.style.setProperty('--fit',d.mtfFit>0?d.mtfFit+'px':'none')}});
 window.addEventListener('message',function(e){var d=e.data;if(d&&d.mtfMode){var b=document.querySelector('.modebar button:nth-child('+(d.mtfMode==='tun'?2:1)+')');if(b)b.click()}});
 function liveCandle(s,cd,px,tgt,left){if(s.getAttribute('data-replay')==='1')return;var g=s.getAttribute('data-geo').split(',').map(Number),L=g[0],T=g[1],pw=g[2],ph=g[3],t0=g[4],tmax=g[5],lo=g[6],hi=g[7],step=g[8],dec=+s.getAttribute('data-dec')||2;
  var sx=function(t){return L+(t-t0)/(tmax-t0)*pw},sy=function(v){var y=T+(hi-v)/(hi-lo)*ph;return Math.max(T,Math.min(T+ph,y))};
  var all=s.querySelectorAll('g.cd');if(!all.length)return;var last=all[all.length-1];var tl=+last.getAttribute('data-t');var tg=last;
  if(cd[0]>tl&&cd[0]-tl<=step*1.5){tg=last.cloneNode(true);tg.setAttribute('data-t',cd[0]);last.parentNode.insertBefore(tg,last.nextSibling)}else if(cd[0]<tl){tg=null}
  if(tg){var col=cd[4]>=cd[1]?'#3fb950':'#f85149',x=sx(cd[0]+step/2),ln=tg.querySelector('line'),rc=tg.querySelector('rect'),bw=+rc.getAttribute('width');
   ln.setAttribute('x1',x);ln.setAttribute('x2',x);ln.setAttribute('y1',sy(cd[2]));ln.setAttribute('y2',sy(cd[3]));ln.setAttribute('stroke',col);
   var yt=sy(Math.max(cd[1],cd[4])),yb=sy(Math.min(cd[1],cd[4]));rc.setAttribute('x',x-bw/2);rc.setAttribute('y',yt);rc.setAttribute('height',Math.max(yb-yt,1));rc.setAttribute('fill',col)}
  var tl=s.querySelector('g.tgt');if(tl){if(tgt){var yy=T+(hi-tgt)/(hi-lo)*ph,cl=yy<T?T:(yy>T+ph?T+ph:yy),arrow=yy<T?' ▲':(yy>T+ph?' ▼':'');tl.style.display='inline';tl.setAttribute('transform','translate(0,'+(yy<T?T+22:(yy>T+ph?T+ph-4:cl))+')');tl.querySelector('text').textContent='15-MIN TARGET '+tgt.toLocaleString(undefined,{minimumFractionDigits:dec,maximumFractionDigits:dec})+arrow}else{tl.style.display='none'}}
  var wg=s.querySelector('g.wnd');if(wg){if(left>0&&left<=900){var te=Date.now()/1000+left,tsx=te-900,xe=L+(te-t0)/(tmax-t0)*pw,xs=L+(tsx-t0)/(tmax-t0)*pw,ls=wg.querySelectorAll('line'),rc2=wg.querySelector('rect'),tx=wg.querySelector('text');
    wg.style.display='inline';ls[0].setAttribute('x1',xs);ls[0].setAttribute('x2',xs);ls[0].style.display=(xs>=L&&xs<=L+pw)?'':'none';ls[1].setAttribute('x1',xe);ls[1].setAttribute('x2',xe);ls[1].style.display=(xe>=L&&xe<=L+pw)?'':'none';
    var lx=Math.min(Math.max(xe-75,L+2),L+pw-152);rc2.setAttribute('x',lx);tx.setAttribute('x',lx+75);var mm=Math.floor(left/60),ss=Math.floor(left%60);tx.textContent='ROUND ENDS '+mm+':'+(ss<10?'0':'')+ss;rc2.style.display=tx.style.display=(xe>=L&&xe<=L+pw+60)?'':'none'}else{wg.style.display='none'}}
  var lp=s.querySelector('g.livepx');if(lp&&px){lp.style.display='inline';lp.setAttribute('transform','translate(0,'+sy(px)+')');lp.querySelector('text').textContent=px.toLocaleString(undefined,{minimumFractionDigits:dec,maximumFractionDigits:dec})}}
 var NARR=null,narSide={},narLast={},narRound={};
 function loadNarr(){try{NARR=JSON.parse(document.getElementById('narr-data').textContent)}catch(e){NARR=null}}
 loadNarr();
 function fp(v){return v.toLocaleString(undefined,{minimumFractionDigits:NARR.dec,maximumFractionDigits:NARR.dec})}
 function say(text,key,ttl,clip){try{parent.postMessage({mtfSay:text,key:key,ttl:ttl||60000,coin:COIN,clip:clip||null},'*')}catch(e){}}
 function narrate(px,tgt,left){if(!NARR||!px)return;var now=Date.now()/1000,hy=NARR.range*0.15,nm=NARR.coin;
  NARR.levels.forEach(function(L){var v=L.p+L.s*(now-L.t),id=L.tf+'|'+L.k+'|'+Math.round(L.p*100),side=px>v+hy?1:(px<v-hy?-1:0),prev=narSide[id];
   if(side!==0){if(prev!==undefined&&prev!==side&&(!narLast[id]||now-narLast[id]>300)){narLast[id]=now;var dwn=side<0;
     var lvl=(L.k==='floor'||L.k==='ceiling'||L.k.indexOf('tunnel')===0);var what=lvl?('the '+(L.tf==='1m'?'one minute':L.tf==='5m'?'five minute':L.tf==='15m'?'fifteen minute':'one hour')+' '+L.k):('the '+L.k);
     say(nm+'. Price broke '+(dwn?'below ':'above ')+what+(lvl?' at '+fp(v):'')+'. It is now '+fp(Math.abs(px-v))+(dwn?' under':' over')+' it.'+(L.n>=3?' That level was tested '+L.n+' times.':''),'lvl:'+id+':'+side,300000,dwn?'support_break':'resistance_break')}narSide[id]=side}else if(prev===undefined){narSide[id]=px>v?1:-1}});
  if(tgt){var ts=px>tgt+hy*0.6?1:(px<tgt-hy*0.6?-1:0),pv=narSide.tgt;if(ts!==0){if(pv!==undefined&&pv!==ts&&(!narLast.tgt||now-narLast.tgt>60)){narLast.tgt=now;say(nm+'. Price crossed '+(ts>0?'above':'below')+' the target.','tgt:'+Math.floor((now+left)/900)+':'+ts+':'+Math.floor(now/60),60000,ts>0?'target_cross_up':'target_cross_down')}narSide.tgt=ts}}
  }
 var lastLive=null,LT=0,LTa=0;
 function applyLive(j){[].forEach.call(document.querySelectorAll('svg[data-live]'),function(s){var k=s.getAttribute('data-live');if(!k||!s.getBoundingClientRect().width)return;var cd=j.c[k];if(cd)liveCandle(s,cd,j.px,j.target,j.left)})}
 function liveTick(){if(document.hidden)return;fetch('/mtf_live?coin='+COIN,{cache:'no-store'}).then(function(r){return r.json()}).then(function(j){if(!j||!j.c)return;
   if(j.target){LT=j.target;LTa=Date.now()}else if(LT&&Date.now()-LTa<30000){j.target=LT}
   lastLive=j;
   if(j.px&&!document.querySelector('svg[data-replay="1"]'))narrate(j.px,j.target,j.left);
   applyLive(j)}).catch(function(){})}
 setInterval(liveTick,1000);
})();
"""
if EMBED:
    os.makedirs(OUT_DIR, exist_ok=True)
    s0, e0 = page.index("<h1>"), page.index('<div id="infostore"')
    shell = (page[:s0] + page[e0:]).replace('<p class="hint">Information only. It shows the shape', f'<p class="hint">Updated {now}. Information only. It shows the shape')
    allb = "".join(blocks)
    assert allb in shell
    for (coin_, _s, _d), blk in zip(COINS, blocks):
        mback = ("<button class=\"mback\" onclick=\"parent.postMessage({mtfClose:1,coin:'" + coin_ + "'},'*')\">&#9650; Collapse this chart</button>")
        blk2 = blk.replace('</div><div class="mpat">', mback + '</div><div class="mpat">', 1)
        _narr = f'<script type="application/json" id="narr-data">{narr_json(coin_, next(d_ for c_, s_, d_ in COINS if c_ == coin_), tfc_by_coin[coin_]["1m"], lines_by_coin[coin_])}</script>'
        html = shell.replace(allb, blk2).replace("</style></head>", EMBED_CSS + "</style><meta name=\"build\" content=\"" + str(int(time.time() * 1000)) + "\"></head>", 1).replace("</script></body></html>", EMBED_JS + "</script>" + _narr + "</body></html>")
        tmp = os.path.join(OUT_DIR, f"mtf_{coin_}.{os.getpid()}.tmp")
        open(tmp, "w", encoding="utf-8").write(html)
        os.replace(tmp, os.path.join(OUT_DIR, f"mtf_{coin_}.html"))
    print("embedded pages written to", OUT_DIR, len(shell) // 1024, "KB each")
else:
    open(OUT, "w", encoding="utf-8").write(page)
    print("written", OUT, len(page) // 1024, "KB")
for m in re.findall(r'<p class="stat">(.*?)</p>', page):
    print(re.sub(r"<[^>]+>", "", m))
