"""Live 'pattern window': real candles with every detected pattern DRAWN on them (ledge lines, necklines, triangle lines,
shoulders/head, order-block and gap boxes, markers on candle patterns...). INFORMATION ONLY - never used for buying.
Input candles [[ts,o,h,l,c], ...] (last one may be forming) and the labels from pattern_detector; geometry is re-derived
here from the same pivot logic so the drawings line up with what was detected."""
import re
import datetime as dt
from zoneinfo import ZoneInfo

from src.pattern_detector import _pivots, _atr, _fit, _L, _structure, _candle_patterns

try:
    import numpy as np
except ImportError:
    np = None

COL = {"bullish": "#3fb950", "bearish": "#f85149", "neutral": "#ffffff"}
TWO = ("engulfing", "harami", "piercing line", "dark cloud", "tweezer", "inside bar", "outside bar", "window")
THREE = ("morning", "evening", "three white", "three black", "three inside", "three outside")
FIVE = ("three methods",)


def _num(name, pat=r"at ([0-9][0-9.]*)"):
    m = re.search(pat, name)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def drawings_for(cs, label):
    """-> list of primitives for one label. x = candle index into cs; y = price."""
    n = len(cs)
    closed = cs[:-1]
    atr = _atr(closed) or 1e-9
    etol = 0.4 * atr
    pv = _pivots(closed)
    highs = [p for p in pv if p[2] == "H"]
    lows = [p for p in pv if p[2] == "L"]
    name = label["name"]; low = name.lower(); col = COL.get(label["bias"], COL["neutral"])
    out = []

    def hline(price, x0, text):
        out.append({"t": "hline", "p": price, "x0": max(0, x0), "col": col, "text": text})

    def proj(price, text):
        out.append({"t": "proj", "p": price, "col": col, "text": text})

    # ---- candle patterns: highlight the candles involved
    if label["family"] == "candle":
        m = re.match(r"(\d+) (rising|falling) candles in a row", low)
        if m:
            k = int(m.group(1)); out.append({"t": "mark", "i0": n - k, "i1": n - 1, "col": col, "text": f"{k} {m.group(2)}"})
        elif any(k in low for k in FIVE):
            out.append({"t": "mark", "i0": n - 6, "i1": n - 2, "col": col, "text": name.split(" (")[0]})
        elif any(k in low for k in THREE):
            out.append({"t": "mark", "i0": n - 4, "i1": n - 2, "col": col, "text": name})
        elif any(k in low for k in TWO):
            out.append({"t": "mark", "i0": n - 3, "i1": n - 2, "col": col, "text": name})
        else:
            i = n - 1 if label["status"] == "forming" else n - 2 - label.get("age", 0)
            out.append({"t": "mark", "i0": i, "i1": i, "col": col, "text": name.split(" (")[0]})
        return out

    # ---- swing structure
    if "uptrend structure" in low or "downtrend structure" in low:
        pts = sorted(pv)[-5:]
        lab, lastH, lastL = [], None, None
        for (i, p, t) in sorted(pv)[-8:]:
            if t == "H":
                tag = "" if lastH is None else ("HH" if p > lastH else "LH"); lastH = p
            else:
                tag = "" if lastL is None else ("HL" if p > lastL else "LL"); lastL = p
            lab.append((i, p, tag, t))
        out.append({"t": "pts", "pts": [(i, p, tg, t) for (i, p, tg, t) in lab[-5:] if tg], "col": col})
        return out
    if "break of structure" in low or "change of character" in low:
        use_high = ("UP" in name) or ("character up" in low)
        pt = (highs[-1] if use_high else lows[-1]) if (highs if use_high else lows) else None
        if pt:
            hline(pt[1], pt[0], "BOS" if "break of structure" in low else "CHoCH")
            out.append({"t": "pts", "pts": [(pt[0], pt[1], "swing", "H" if use_high else "L")], "col": col})
        return out
    if "premium zone" in low or "discount zone" in low:
        if len(highs) >= 2 and len(lows) >= 2:
            hi_, lo_ = max(highs[-1][1], highs[-2][1]), min(lows[-1][1], lows[-2][1])
            hline((hi_ + lo_) / 2, max(0, n - 40), "50% of swing range")
        return out
    if "fibonacci" in low:
        if highs and lows:
            h_, l_ = highs[-1], lows[-1]
            for lvl in (0.382, 0.5, 0.618, 0.786):
                if h_[0] > l_[0]:   # last leg was down -> bounce levels measured from the high
                    price = h_[1] - (h_[1] - l_[1]) * (1 - lvl)
                else:
                    price = h_[1] - (h_[1] - l_[1]) * lvl
                hline(price, min(h_[0], l_[0]), f"{lvl:.1%}")
            out.append({"t": "pts", "pts": [(h_[0], h_[1], "leg", "H"), (l_[0], l_[1], "leg", "L")], "col": col})
        return out

    # ---- liquidity: equal highs / lows, sweeps
    if "liquidity sweep of the highs" in low and highs:
        hline(highs[-1][1], highs[-1][0], "stops swept")
        out.append({"t": "mark", "i0": n - 1, "i1": n - 1, "col": col, "text": "sweep"})
        out.append(_sweep_star(cs, highs[-1][1], True))
        return out
    if "liquidity sweep of the lows" in low and lows:
        hline(lows[-1][1], lows[-1][0], "stops swept")
        out.append({"t": "mark", "i0": n - 1, "i1": n - 1, "col": col, "text": "sweep"})
        out.append(_sweep_star(cs, lows[-1][1], False))
        return out
    if any(k in low for k in ("equal highs", "equal lows", "ledge", "ceiling broke", "shelf")):
        lvl = _num(name)
        pts = highs if any(k in low for k in ("highs", "resistance", "ceiling")) else lows
        if lvl is not None:
            touches = [p for p in pts if abs(p[1] - lvl) <= max(etol * 1.6, 0.0)]
            x0 = min([p[0] for p in touches], default=max(0, n - 30))
            hline(lvl, x0, "ceiling" if any(k in low for k in ("resistance", "ceiling", "highs")) else "floor")
            out.append({"t": "pts", "pts": [(p[0], p[1], "", "H" if pts is highs else "L") for p in touches], "col": col})
            if "fallen ledge" in low or "ceiling broke" in low:
                seg = closed[x0:] or closed[-10:]
                if "fallen ledge" in low:
                    proj(lvl - max(max(s_[2] for s_ in seg) - lvl, 2 * atr), "target = shelf minus its own height")
                else:
                    proj(lvl + max(lvl - min(s_[3] for s_ in seg), 2 * atr), "target = shelf plus its own height")
        return out

    # ---- gaps and order blocks
    if "fair value gap" in low:
        m = re.search(r"([0-9][0-9.]*)-([0-9][0-9.]*) \((\d+) candles old\)", name)
        if m:
            a, b, age = float(m.group(1)), float(m.group(2)), int(m.group(3))
            out.append({"t": "box", "i0": max(0, n - 1 - age - 1), "i1": n - 1, "lo": min(a, b), "hi": max(a, b), "col": col, "text": "gap"})
        return out
    if "order block" in low:
        m = re.search(r"([0-9][0-9.]*)-([0-9][0-9.]*)", name)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            out.append({"t": "box", "i0": max(0, n - 14), "i1": n - 1, "lo": min(a, b), "hi": max(a, b), "col": col, "text": "order block"})
        return out

    # ---- double / triple tops and bottoms, head and shoulders
    m = re.match(r"(double|triple) (top|bottom)", low)
    if m:
        cnt = 3 if m.group(1) == "triple" else 2
        pts = (highs if m.group(2) == "top" else lows)[-cnt:]
        if len(pts) == cnt:
            seg = closed[pts[0][0]:pts[-1][0] + 1]
            neck = min(c[3] for c in seg) if m.group(2) == "top" else max(c[2] for c in seg)
            out.append({"t": "pts", "pts": [(p[0], p[1], str(k + 1), "H" if m.group(2) == "top" else "L") for k, p in enumerate(pts)], "col": col})
            hline(neck, pts[0][0], "neckline")
            h_ = abs(sum(p_[1] for p_ in pts) / cnt - neck)
            proj(neck - h_ if m.group(2) == "top" else neck + h_, "target = neckline " + ("minus" if m.group(2) == "top" else "plus") + " pattern height")
        return out
    if "head and shoulders" in low:
        inv = low.startswith("inverse")
        pts = (lows if inv else highs)[-3:]
        if len(pts) == 3:
            seg = closed[pts[0][0]:pts[-1][0] + 1]
            neck = max(c[2] for c in seg) if inv else min(c[3] for c in seg)
            t = "L" if inv else "H"
            out.append({"t": "pts", "pts": [(pts[0][0], pts[0][1], "L shoulder", t), (pts[1][0], pts[1][1], "HEAD", t), (pts[2][0], pts[2][1], "R shoulder", t)], "col": col})
            hline(neck, pts[0][0], "neckline")
            h_ = abs(pts[1][1] - neck)
            proj(neck + h_ if inv else neck - h_, "target = neckline " + ("plus" if inv else "minus") + " head height")
        return out

    # ---- trendline patterns (triangle / wedge / channel / rectangle / broadening)
    if any(k in low for k in ("triangle", "wedge", "channel", "rectangle", "broadening")) and len(highs) >= 3 and len(lows) >= 3:
        hp = [(p[0], p[1]) for p in highs[-4:]]; lp = [(p[0], p[1]) for p in lows[-4:]]
        x0 = min(hp[0][0], lp[0][0]); x1 = n - 1
        hs, hb, _ = _fit(hp); ls_, lb, _ = _fit(lp)
        out.append({"t": "line", "x0": x0, "y0": hs * x0 + hb, "x1": x1, "y1": hs * x1 + hb, "col": col, "text": "upper line"})
        out.append({"t": "line", "x0": x0, "y0": ls_ * x0 + lb, "x1": x1, "y1": ls_ * x1 + lb, "col": col, "text": "lower line"})
        out.append({"t": "pts", "pts": [(i, p, "", "H") for i, p in hp] + [(i, p, "", "L") for i, p in lp], "col": col})
        up_x1 = hs * x1 + hb; lo_x1 = ls_ * x1 + lb
        width0 = (hs * x0 + hb) - (ls_ * x0 + lb)
        prior_up = closed[-1][4] >= closed[max(0, x0 - 10)][4]
        if "ascending triangle" in low: proj(up_x1 + width0, "target = breakout + triangle height")
        elif "descending triangle" in low: proj(lo_x1 - width0, "target = breakdown - triangle height")
        elif "symmetrical" in low: proj((up_x1 + width0) if prior_up else (lo_x1 - width0), "target = break in the prior trend's direction + height")
        elif "rising wedge" in low: proj(ls_ * x0 + lb, "typical move = back to where the wedge started")
        elif "falling wedge" in low: proj(hs * x0 + hb, "typical move = back to where the wedge started")
        elif "up-channel" in low: proj(hs * (n + 5) + hb, "channel top, if the trend holds")
        elif "down-channel" in low: proj(ls_ * (n + 5) + lb, "channel bottom, if the trend holds")
        return out

    # ---- flag / pennant: box over the pause + arrow along the impulse
    if "flag" in low or "pennant" in low:
        cons = closed[-10:]
        out.append({"t": "box", "i0": n - 11, "i1": n - 1, "lo": min(c[3] for c in cons), "hi": max(c[2] for c in cons), "col": col, "text": "pause"})
        seg = closed[-24:-10]
        if seg:
            out.append({"t": "line", "x0": n - 24, "y0": seg[0][1], "x1": n - 11, "y1": seg[-1][4], "col": col, "text": "sharp move"})
            imp = seg[-1][4] - seg[0][1]
            proj(closed[-1][4] + imp, "target = pause end + the sharp move again")
        return out

    # ---- rounding bottom / top / cup with handle
    if ("rounding" in low or "cup" in low) and np is not None and n >= 45:
        ys = np.array([c[4] for c in closed[-40:]]); xs = np.arange(40)
        a, b, c0 = np.polyfit(xs, ys, 2)
        pts = [(n - 1 - 40 + k + 0, float(a * k * k + b * k + c0)) for k in range(0, 40, 2)]
        out.append({"t": "curve", "pts": pts, "col": col, "text": "cup" if "cup" in low else "curve"})
        rim, depth = float(max(ys)), float(max(ys) - min(ys))
        if "cup" in low or "bottom" in low:
            proj(rim + depth, "target = rim plus cup depth")
        return out
    return out


_ET = ZoneInfo("America/New_York")


def _ztz():
    """The viewer's time zone for the on-screen clock (Eastern when unknown)."""
    try:
        from src import viewtz
        return viewtz.get()
    except Exception:
        return _ET


def _clock(d):
    return d.strftime("%I:%M %p").lstrip("0")
_NICE = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400]


def _time_ticks(cs, step, fs):
    """Candle indexes to label on the time axis, with their text (Eastern time). Round clock times (:00 :05 :15 ...,
    midnights, month starts), spaced so the labels never touch. -> [(index, text, is_date)]"""
    n = len(cs)
    if n < 3:
        return []
    gaps = sorted(cs[i + 1][0] - cs[i][0] for i in range(n - 1))
    spacing = max(gaps[len(gaps) // 2], 1)
    label_px = 8 * fs * 0.62 + 18
    need = max(1, int(label_px * 1.35 / max(step, 1)) + 1)          # candles between two labels
    want = need * spacing
    out = []
    if spacing >= 20 * 86400:                                         # monthly (or longer) candles
        k = max(1, -(-need // 1))
        for i, cd in enumerate(cs):
            d = dt.datetime.fromtimestamp(cd[0], dt.timezone.utc)
            if (d.year * 12 + d.month) % k == 0:
                out.append((i, d.strftime("%b '%y"), True))
        return out
    if want >= 86400:                                                  # labels at least a day apart: label the first candle of a day
        k = max(1, -(-want // 86400))
        prev = None
        for i, cd in enumerate(cs):
            d = dt.datetime.fromtimestamp(cd[0], _ztz())
            if d.date() != prev and d.date().toordinal() % k == 0:
                out.append((i, d.strftime("%b %d"), True))
            prev = d.date()
        return out
    interval = next((v for v in _NICE if v >= want), 86400)
    prev_day = None
    for i, cd in enumerate(cs):
        d = dt.datetime.fromtimestamp(cd[0], _ztz())
        local = int(cd[0] + d.utcoffset().total_seconds())
        if local % interval == 0:
            new_day = prev_day is not None and d.date() != prev_day
            out.append((i, d.strftime("%b %d") if (d.hour == 0 and d.minute == 0) or new_day else _clock(d),
                        (d.hour == 0 and d.minute == 0) or new_day))
        prev_day = d.date()
    return out


def _sweep_star(cs, lvl, up):
    """Shooting-star marker for a liquidity sweep: sits on the wick tip that ran past the level (the open candle if it did, else the one before)."""
    n = len(cs)
    idx = n - 1
    for i in (n - 1, n - 2):
        if i >= 0 and ((up and cs[i][2] > lvl and cs[i][4] < lvl) or (not up and cs[i][3] < lvl and cs[i][4] > lvl)):
            idx = i
            break
    return {"t": "star", "i": idx, "p": cs[idx][2] if up else cs[idx][3], "up": up, "col": "#ffd33d", "text": "LIQUIDITY SWEEP"}


def _x(i, n, left, right):
    step = (right - left) / (n + 1)
    return left + step * (i + 1), step


def render_window_svg(cs, labels, title="", width=1100, height=880, max_labels=7, events=None, levels=None, sweeps=None):
    """SVG of the candles with the drawings of the most important labels. Returns (svg, shown_labels).
    Text labels are collision-avoided (pushed to a free spot) and outlined so they stay readable over candles."""
    n = len(cs)
    if n < 10:
        return '<div class="flat">waiting for candle history...</div>', []
    cs = cs[-90:]; n = len(cs)
    F = width / 760.0 * 0.95        # text / line scale: the chart is drawn big so it can be read
    L, R, T, B = 10, int(70 * F), int(24 * F), int(28 * F)
    draws = []
    shown = []
    pri = {"confirmed": 0, "forming": 1, "formed": 2}
    fam = {"chart": 0, "smc": 1, "structure": 2, "candle": 3, "trend": 4}
    cand = sorted([l for l in labels if l["family"] != "trend"], key=lambda l: (pri.get(l["status"], 3), fam.get(l["family"], 5)))
    # candle shapes (the newest first) get up to 4 guaranteed places so a long list of chart patterns can no longer hide them
    others = [l for l in cand if l["family"] != "candle"]
    def _generic(l):      # plain context shapes rank behind the named reversal patterns (star, hammer, engulfing, ...)
        # dojis (all kinds) are NOT generic - they rank with the named patterns (the user: "doji candles are very important")
        return (l["name"].startswith(("long upper shadow", "long lower shadow", "inside bar", "outside bar", "spinning top",
                                      "rising window", "falling window")) or " candles in a row" in l["name"], l["bias"] == "neutral" and "doji" not in l["name"])
    def _star_like(l):    # dojis, stars and hammers/hanging men: the single-candle reversal shapes the user wants to keep seeing
        return 0 if ("doji" in l["name"] or l["name"] in ("shooting star", "hammer", "hanging man", "inverted hammer")) else 1
    candles = sorted([l for l in cand if l["family"] == "candle"], key=lambda l: (_star_like(l), l.get("age", 0), _generic(l), pri.get(l["status"], 3)))
    picked = []
    for l in candles:
        if len(picked) >= 4:
            break
        d = drawings_for(cs, l)
        if d:
            picked.append((l, d))
    for l in others:
        if len(shown) >= max_labels - len(picked):
            break
        d = drawings_for(cs, l)
        if not d:
            continue
        shown.append(l); draws += d
    for l, d in picked:
        shown.append(l); draws += d
    star_idx = {d["i"] for d in draws if d["t"] == "star"}
    for sw in (sweeps or []):
        j = next((k for k in range(n) if cs[k][0] == sw["ts"]), None)
        if j is not None and j not in star_idx:
            draws.append({"t": "star", "i": j, "p": cs[j][2] if sw["up"] else cs[j][3], "up": sw["up"], "col": "#ffd33d", "text": sw["text"]})
            star_idx.add(j)
    prices = [c[2] for c in cs] + [c[3] for c in cs]
    for d in draws:
        if d["t"] == "hline": prices.append(d["p"])
        elif d["t"] == "box": prices += [d["lo"], d["hi"]]
        elif d["t"] == "line": prices += [d["y0"], d["y1"]]
        elif d["t"] == "proj": prices.append(d["p"])
    _pmin, _pmax = min(prices), max(prices)
    _prng = (_pmax - _pmin) or 1e-9
    for lv in (levels or []):
        if _pmin - 0.5 * _prng <= lv["p"] <= _pmax + 0.5 * _prng:       # a level only moves the scale when it is NEAR the candles - far ones never squash them
            prices.append(lv["p"])
    hi, lo = max(prices), min(prices)
    pad = (hi - lo) * 0.06 or hi * 0.001
    hi += pad; lo -= pad

    def y(p): return T + (hi - p) / (hi - lo) * (height - T - B)

    def xi(i): return _x(i, n + 8, L, width - R)[0]
    step = _x(0, n + 8, L, width - R)[1]
    bw = max(2.0, step * 0.62)
    fs = 10 * F
    o = [f'<svg viewBox="0 0 {width} {height}" width="100%" style="height:auto;background:#0d1117;border:1px solid #30363d;border-radius:6px" xmlns="http://www.w3.org/2000/svg">']
    labs = []       # overlay text labels, placed after everything else so they can avoid each other

    def lab(x, yy, text, anchor, fill, bold=False, prio=5):
        labs.append({"x": x, "y": yy, "text": text, "anchor": anchor, "fill": fill, "bold": bold, "prio": prio})

    for k in range(5):   # price grid + axis
        p = lo + (hi - lo) * k / 4
        o.append(f'<line x1="{L}" y1="{y(p):.1f}" x2="{width - R}" y2="{y(p):.1f}" stroke="#21262d" stroke-width="{1 * F:.1f}"/>')
        o.append(f'<text fill-opacity="0.85" x="{width - R + 4}" y="{y(p) + 3:.1f}" font-size="{fs:.0f}" fill="#6e7681" font-family="sans-serif">{p:.6g}</text>')
    if title:
        o.append(f'<text fill-opacity="0.85" x="{L + 2}" y="{14 * F:.0f}" font-size="{11 * F:.0f}" fill="#8b949e" font-family="sans-serif">{title}</text>')
    for d in draws:     # boxes first (behind candles)
        if d["t"] == "box":
            x0, x1 = xi(d["i0"]) - step / 2, xi(d["i1"]) + step / 2
            o.append(f'<rect x="{x0:.1f}" y="{y(d["hi"]):.1f}" width="{x1 - x0:.1f}" height="{max(2.0, y(d["lo"]) - y(d["hi"])):.1f}" fill="{d["col"]}" opacity="0.11" stroke="{d["col"]}" stroke-opacity="0.5" stroke-dasharray="3 3"/>')
            lab(x0 + 3, y(d["hi"]) + fs, d["text"], "start", d["col"], prio=4)
    for i, (ts, op, h, l, c) in enumerate(cs):
        col = "#3fb950" if c >= op else "#f85149"
        forming = i == n - 1
        o.append(f'<line x1="{xi(i):.1f}" y1="{y(h):.1f}" x2="{xi(i):.1f}" y2="{y(l):.1f}" stroke="{col}" stroke-width="{1 * F:.1f}"/>')
        top, bot = y(max(op, c)), y(min(op, c))
        o.append(f'<rect x="{xi(i) - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(1.0, bot - top):.1f}" fill="{col if not forming else "none"}" stroke="{col}" stroke-width="{1 * F:.1f}"' + (' stroke-dasharray="2 2"' if forming else '') + '/>')
    for d in draws:
        c = d["col"]
        if d["t"] == "hline":
            yy = y(d["p"])
            o.append(f'<line x1="{xi(d["x0"]):.1f}" y1="{yy:.1f}" x2="{width - R}" y2="{yy:.1f}" stroke="{c}" stroke-opacity="0.5" stroke-width="{1.6 * F:.1f}" stroke-dasharray="6 3"/>')
            lab(width - R - 4, yy - 4, f'{d["text"]} {d["p"]:.6g}', "end", c, prio=1)
        elif d["t"] == "line":
            o.append(f'<line x1="{xi(d["x0"]):.1f}" y1="{y(d["y0"]):.1f}" x2="{xi(d["x1"]):.1f}" y2="{y(d["y1"]):.1f}" stroke="{c}" stroke-opacity="0.55" stroke-width="{1.8 * F:.1f}"/>')
            lab(xi(d["x1"]) - 3, y(d["y1"]) - 5, d["text"], "end", c, prio=2)
        elif d["t"] == "curve":
            pts = " ".join(f"{xi(i):.1f},{y(p):.1f}" for i, p in d["pts"] if 0 <= i < n)
            o.append(f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-opacity="0.55" stroke-width="{2 * F:.1f}" stroke-dasharray="4 3"/>')
        elif d["t"] == "pts":
            for (i, p, tag, t) in d["pts"]:
                if not (0 <= i < n - 1):        # never draw on the candle that is still forming
                    continue
                o.append(f'<circle cx="{xi(i):.1f}" cy="{y(p):.1f}" r="{4 * F:.1f}" fill="none" stroke="{c}" stroke-opacity="0.6" stroke-width="{2 * F:.1f}"/>')
                if tag:
                    lab(xi(i), y(p) + (-fs * 0.9 if t == "H" else fs * 1.5), tag, "middle", c, bold=True, prio=3)
        elif d["t"] == "proj":
            x_a, y_a, x_b, y_b = xi(n - 1) + bw, y(cs[-1][4]), xi(n + 6), y(d["p"])
            o.append(f'<line x1="{x_a:.1f}" y1="{y_a:.1f}" x2="{x_b:.1f}" y2="{y_b:.1f}" stroke="{c}" stroke-opacity="0.55" stroke-width="{2.2 * F:.1f}" stroke-dasharray="2 4"/>')
            o.append(f'<circle cx="{x_b:.1f}" cy="{y_b:.1f}" r="{5 * F:.1f}" fill="{c}" opacity="0.6"/>')
            lab(x_b - 8, y_b + (-fs * 0.9 if y_b < y_a else fs * 1.6), f'if it plays out: {d["p"]:.6g}', "end", c, bold=True, prio=0)
        elif d["t"] == "star":
            import math as _m
            cx = xi(max(0, min(n - 1, d["i"])))
            sg = -1 if d["up"] else 1                      # star sits beyond the wick tip; the streak trails in from the left
            cy = y(d["p"]) + sg * 11 * F
            for (ln, op, wd) in ((52, 0.12, 6), (38, 0.25, 5), (25, 0.45, 3.5), (13, 0.75, 2.2)):
                o.append(f'<line x1="{cx - ln * F:.1f}" y1="{cy + sg * ln * 0.38 * F:.1f}" x2="{cx:.1f}" y2="{cy:.1f}" stroke="{c}" stroke-opacity="{op}" '
                         f'stroke-width="{wd * F:.1f}" stroke-linecap="round"/>')
            o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{13 * F:.1f}" fill="{c}" opacity="0.18"/>')
            pts = " ".join(f"{cx + (10 * F if k % 2 == 0 else 4.2 * F) * _m.cos(-_m.pi / 2 + k * _m.pi / 5):.1f},"
                           f"{cy + (10 * F if k % 2 == 0 else 4.2 * F) * _m.sin(-_m.pi / 2 + k * _m.pi / 5):.1f}" for k in range(10))
            o.append(f'<polygon points="{pts}" fill="{c}" stroke="#0d1117" stroke-width="{1 * F:.1f}" stroke-linejoin="round"/>')
            lab(cx - 58 * F, cy + 4 * F, d["text"], "end", c, bold=True, prio=0)
        elif d["t"] == "mark":
            i0, i1 = max(0, d["i0"]), min(n - 2, d["i1"])      # never box / label the candle that is still forming
            if i1 < i0:
                continue
            sub = cs[i0:i1 + 1]
            x0, x1 = xi(i0) - step * 0.7, xi(i1) + step * 0.7
            yt, yb = y(max(c_[2] for c_ in sub)) - 4, y(min(c_[3] for c_ in sub)) + 4
            o.append(f'<rect x="{x0:.1f}" y="{yt:.1f}" width="{x1 - x0:.1f}" height="{yb - yt:.1f}" fill="none" stroke="{c}" stroke-opacity="0.55" stroke-width="{1.5 * F:.1f}" rx="3"/>')
            lab((x0 + x1) / 2, yb + fs * 1.2, d["text"], "middle", c, prio=4)
    off_up, off_dn = [], []
    for lv in (levels or []):                   # horizontal levels, e.g. the CPI floor / ceiling and the targets if they break
        try:
            if not (lo <= lv["p"] <= hi):          # outside this view: say so at the edge instead of squashing the candles
                (off_up if lv["p"] > hi else off_dn).append(lv)
                continue
            yy = y(lv["p"])
            o.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{width - R}" y2="{yy:.1f}" stroke="{lv["col"]}" stroke-opacity="0.9" '
                     f'stroke-width="{lv.get("w", 1.4) * F:.1f}" stroke-dasharray="{lv.get("dash", "7 4")}"/>')
            lab(L + 6, yy - 4, lv["text"], "start", lv["col"], bold=True, prio=0)
        except Exception:
            pass
    for k_, lv in enumerate(sorted(off_up, key=lambda z: -z["p"])):
        lab(L + 6, T + fs * (2.9 + 1.35 * k_), "^ " + lv["text"] + " (above this view)", "start", lv["col"], bold=True, prio=0)
    for k_, lv in enumerate(sorted(off_dn, key=lambda z: z["p"])):
        lab(L + 6, height - B - fs * (1.2 + 1.35 * k_), "v " + lv["text"] + " (below this view)", "start", lv["col"], bold=True, prio=0)
    for (ets, etxt) in (events or []):          # scheduled event markers, e.g. the CPI release
        try:
            gaps_ = sorted(cs[k + 1][0] - cs[k][0] for k in range(n - 1))
            sp_ = max(gaps_[len(gaps_) // 2], 1)
            if not (cs[0][0] <= ets < cs[-1][0] + sp_):
                continue
            k_ = max(j for j in range(n) if cs[j][0] <= ets)
            ex = xi(k_) - step * 0.5 + (ets - cs[k_][0]) / sp_ * step
            o.append(f'<line x1="{ex:.1f}" y1="{T}" x2="{ex:.1f}" y2="{height - B:.1f}" stroke="#d29922" stroke-opacity="0.85" stroke-width="{1.6 * F:.1f}" stroke-dasharray="7 4"/>')
            lab(ex + 4, T + fs * 1.6, etxt, "start", "#d29922", bold=True, prio=0)
        except Exception:
            pass
    # ---- time axis along the bottom (viewer's local time), with faint vertical guide lines
    try:
        ticks = _time_ticks(cs, step, fs)
        now_x = xi(n - 1)
        now_txt = _clock(dt.datetime.now(_ztz()))
        keep_clear = 8 * fs * 0.62 + 20
        base = height - B
        for (i, txt, is_date) in ticks:
            x = xi(i)
            if abs(x - now_x) < keep_clear or x < L + 14 or x > width - R - 14:
                continue
            o.append(f'<line x1="{x:.1f}" y1="{T}" x2="{x:.1f}" y2="{base:.1f}" stroke="#30363d" stroke-opacity="0.35" stroke-width="{1 * F:.1f}"/>')
            o.append(f'<line x1="{x:.1f}" y1="{base:.1f}" x2="{x:.1f}" y2="{base + 4 * F:.1f}" stroke="#8b949e" stroke-opacity="0.8" stroke-width="{1 * F:.1f}"/>')
            o.append(f'<text x="{x:.1f}" y="{base + fs * 1.25 + 3 * F:.1f}" text-anchor="middle" font-size="{fs:.0f}" fill="{"#c9d1d9" if is_date else "#8b949e"}" '
                     f'fill-opacity="0.9" font-family="sans-serif"' + (' font-weight="bold"' if is_date else '') + f'>{txt}</text>')
        o.append(f'<line x1="{now_x:.1f}" y1="{base:.1f}" x2="{now_x:.1f}" y2="{base + 4 * F:.1f}" stroke="#58a6ff" stroke-width="{1.5 * F:.1f}"/>')
        o.append(f'<text x="{now_x:.1f}" y="{base + fs * 1.25 + 3 * F:.1f}" text-anchor="middle" font-size="{fs:.0f}" fill="#58a6ff" font-weight="bold" '
                 f'font-family="sans-serif">{now_txt}</text>')
    except Exception:
        pass
    last = cs[-1][4]
    o.append(f'<line x1="{L}" y1="{y(last):.1f}" x2="{width - R}" y2="{y(last):.1f}" stroke="#58a6ff" stroke-width="{1 * F:.1f}" stroke-dasharray="1 3"/>')
    o.append(f'<text fill-opacity="0.9" x="{width - R + 4}" y="{y(last) + 3:.1f}" font-size="{fs:.0f}" fill="#58a6ff" font-family="sans-serif" font-weight="bold">{last:.6g}</text>')

    # ---- collision-free label placement: highest priority first, nudge up/down until the box is free
    fh, fl = cs[-1][2], cs[-1][3]
    placed = [(xi(n - 1) - step * 1.2, y(fh) - fs * 0.6, xi(n - 1) + step * 1.2, y(fl) + fs * 0.6)]   # keep text off the forming candle
    lh = fs * 1.35
    for lb in sorted(labs, key=lambda z: z["prio"]):
        w = len(lb["text"]) * fs * (0.62 if lb["bold"] else 0.56)
        x0 = lb["x"] - (w if lb["anchor"] == "end" else (w / 2 if lb["anchor"] == "middle" else 0))
        x0 = max(L, min(x0, width - R - w))
        best = None
        for k in range(0, 15):
            for sgn in ((1,) if k == 0 else (1, -1)):
                yy = lb["y"] + sgn * k * lh
                if yy - lh < T or yy > height - B:
                    continue
                r = (x0 - 2, yy - lh, x0 + w + 2, yy + 3)
                if all(r[2] < q[0] or r[0] > q[2] or r[3] < q[1] or r[1] > q[3] for q in placed):
                    best = (yy, r); break
            if best:
                break
        if best is None:
            best = (lb["y"], (x0 - 2, lb["y"] - lh, x0 + w + 2, lb["y"] + 3))
        placed.append(best[1])
        anchor_x = x0 + (w if lb["anchor"] == "end" else (w / 2 if lb["anchor"] == "middle" else 0))
        o.append(f'<text x="{anchor_x:.1f}" y="{best[0]:.1f}" text-anchor="{lb["anchor"]}" font-size="{fs:.0f}" fill="{lb["fill"]}" fill-opacity="0.38" '
                 f'stroke="#0d1117" stroke-opacity="0.2" stroke-width="{2.4 * F:.1f}" stroke-linejoin="round" paint-order="stroke" font-family="sans-serif"'
                 + (' font-weight="bold"' if lb["bold"] else '') + f'>{lb["text"]}</text>')
    o.append("</svg>")
    return "".join(o), shown
