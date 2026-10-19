"""Line crossings: how often the live price crosses the Kalshi target (the "line") inside each 15-minute round, per clock hour, plus a picture of it.
Information only - nothing here feeds a call or a trade. Reads the intrawindow price log the dashboard already keeps (nothing new is deleted)."""
import csv
import datetime as dt
import math
import os
import re
import statistics

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot_state", "intrawindow_price_log.csv")
HYST_BPS = 0.5            # price must get this far (0.005%) past the line to count as being on that side - keeps a price hovering ON the line from counting hundreds of "crossings"
_MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
_NY = None


def close_utc(ticker):
    """Close time (UTC) of a round from its ticker, e.g. KXBTC15M-26OCT151630-30 = 16:30 ET on 2026-10-15."""
    global _NY
    from zoneinfo import ZoneInfo
    if _NY is None:
        _NY = ZoneInfo("America/New_York")
    m = re.match(r"[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)-", ticker or "")
    if not m or m.group(2) not in _MON:
        return None
    try:
        return dt.datetime(2000 + int(m.group(1)), _MON[m.group(2)], int(m.group(3)), int(m.group(4)), int(m.group(5)), tzinfo=_NY).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def read_recent(asset, since_ts, path=LOG, max_bytes=6_000_000):
    """[(ts, ticker, seconds_left, spot, target)] for one coin since `since_ts`, read from the END of the log only."""
    out = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.readline()
            start = max(len(head), size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return out
    cols = head.decode("utf-8", "replace").strip().split(",")
    try:
        i_ts, i_a, i_t, i_s, i_p, i_g = (cols.index(k) for k in ("ts", "asset", "ticker", "seconds_left", "spot_price", "target"))
    except ValueError:
        return out
    lines = data.decode("utf-8", "replace").split("\n")
    if start > len(head):
        lines = lines[1:]                       # the first line is cut in half
    for row in csv.reader(lines):
        if len(row) <= max(i_ts, i_a, i_t, i_s, i_p, i_g) or row[i_a] != asset:
            continue
        try:
            ts = dt.datetime.fromisoformat(row[i_ts]).timestamp()
            if ts < since_ts:
                continue
            out.append((ts, row[i_t], float(row[i_s]), float(row[i_p]), float(row[i_g])))
        except ValueError:
            continue
    return out


def analyze(rows):
    """One summary per round from the samples of one coin (rows from read_recent), oldest first."""
    by = {}
    for r in rows:
        by.setdefault(r[1], []).append(r)
    rounds = []
    for tk, rs in by.items():
        close = close_utc(tk)
        if close is None:
            continue
        rs.sort()
        line = statistics.median(r[4] for r in rs)
        if line <= 0:
            continue
        start_ts = close.timestamp() - 900
        state = None
        crosses = []
        pts = []
        w_above = w_total = 0.0
        side8 = None
        for k, (ts, _t, left, spot, tg) in enumerate(rs):
            d = (spot - line) / line * 1e4
            new = state
            if d > HYST_BPS:
                new = 1
            elif d < -HYST_BPS:
                new = -1
            if new is not None and state is not None and new != state:
                crosses.append((ts, new))
            state = new
            if side8 is None and left <= 420 and state is not None:
                side8 = "YES" if state > 0 else "NO"
            nxt = rs[k + 1][0] if k + 1 < len(rs) else ts
            wdt = min(max(nxt - ts, 0.0), 5.0)
            if state is not None:
                w_total += wdt
                if state > 0:
                    w_above += wdt
            pts.append((ts - start_ts, d))
        cov = (rs[-1][0] - rs[0][0]) / 900.0
        step = max(1, len(pts) // 160)
        first = next((1 if p[1] > HYST_BPS else -1 for p in pts if abs(p[1]) > HYST_BPS), None)
        rounds.append({"ticker": tk, "close": close.timestamp(), "start": start_ts, "line": line, "n": len(rs), "cross": crosses,
                       "pct_above": (w_above / w_total * 100) if w_total else None, "start_side": None if first is None else ("YES" if first > 0 else "NO"),
                       "end_side": None if state is None else ("YES" if state > 0 else "NO"), "side8": side8, "cov": cov,
                       "pts": pts[::step], "last_left": rs[-1][2], "d_last": pts[-1][1]})
    rounds.sort(key=lambda r: r["start"])
    return rounds


def hour_buckets(rounds, tz, now_ts, nhours=4):
    """The current clock hour and the previous ones (viewer's time zone), newest first, each with its rounds."""
    now_l = dt.datetime.fromtimestamp(now_ts, tz)
    top = now_l.replace(minute=0, second=0, microsecond=0)
    hours = []
    for k in range(nhours):
        s = top - dt.timedelta(hours=k)
        s_ts, e_ts = s.timestamp(), (s + dt.timedelta(hours=1)).timestamp()
        rs = [r for r in rounds if s_ts <= r["start"] < e_ts and (r["cov"] >= 0.5 or r["close"] > now_ts)]
        hours.append({"start": s, "start_ts": s_ts, "rounds": rs})
    return hours


def hour_stats(h, results):
    rs = h["rounds"]
    n = len(rs)
    tot = sum(len(r["cross"]) for r in rs)
    done = [r for r in rs if results.get(r["ticker"]) in ("YES", "NO")]
    yes = sum(1 for r in done if results[r["ticker"]] == "YES")
    flip8 = [r for r in done if r["side8"] in ("YES", "NO")]
    flipped = sum(1 for r in flip8 if r["side8"] != results[r["ticker"]])
    pa = [r["pct_above"] for r in rs if r["pct_above"] is not None]
    st = {"rounds": n, "cross": tot, "per_round": (tot / n) if n else None, "calm": sum(1 for r in rs if not r["cross"]),
          "one": sum(1 for r in rs if len(r["cross"]) == 1), "multi": sum(1 for r in rs if len(r["cross"]) >= 2),
          "pct_above": statistics.mean(pa) if pa else None, "settled": len(done), "yes": yes, "flip8_n": len(flip8), "flip8": flipped}
    return st


def read_sentence(st):
    """One plain sentence about how the hour behaved."""
    if not st["rounds"]:
        return "no data for this hour (the bot was off or it has not started yet)."
    bits = []
    pr = st["per_round"]
    if pr >= 2.5:
        bits.append("CHOPPY - the price kept crossing the line (%.1f times per round), so calls flip-flopped easily" % pr)
    elif pr <= 0.6:
        bits.append("TRENDING - the price mostly stayed on one side of the line (%.1f crossings per round)" % pr)
    else:
        bits.append("MIXED - %.1f crossings per round" % pr)
    pa = st["pct_above"]
    if pa is not None:
        if pa >= 65:
            bits.append("it spent %.0f%% of the time ABOVE the line (YES side)" % pa)
        elif pa <= 35:
            bits.append("it spent %.0f%% of the time BELOW the line (NO side)" % (100 - pa))
        else:
            bits.append("it was split about evenly above and below (%.0f%% above)" % pa)
    if st["flip8_n"]:
        bits.append("%d of %d rounds finished on the opposite side from where they stood at the 8th minute" % (st["flip8"], st["flip8_n"]))
    return "; ".join(bits) + "."


def hour_svg(h, results, uid, labels, now_ts, width=1100, height=210):
    """Picture of one hour: the price's distance from the line (in basis points, 1 bp = 0.01%) for each of the rounds, the target line in the middle,
    green above / red below, a dot wherever it crossed, and each round's Kalshi result."""
    L, R, T, B = 52, 10, 34, 24
    pw, ph = width - L - R, height - T - B
    zero = T + ph / 2
    rs = h["rounds"]
    dmax = max([4.0] + [abs(p[1]) for r in rs for p in r["pts"]]) * 1.12
    sx = lambda off, r: L + ((r["start"] - h["start_ts"]) + off) / 3600.0 * pw
    sy = lambda d: zero - max(-dmax, min(dmax, d)) / dmax * (ph / 2)
    o = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" class="lc-svg" role="img">',
         f'<defs><clipPath id="{uid}a"><rect x="{L}" y="{T}" width="{pw}" height="{ph / 2:.1f}"/></clipPath>'
         f'<clipPath id="{uid}b"><rect x="{L}" y="{zero:.1f}" width="{pw}" height="{ph / 2:.1f}"/></clipPath></defs>',
         f'<rect x="0" y="0" width="{width}" height="{height}" fill="#0d1117"/>',
         f'<rect x="{L}" y="{T}" width="{pw}" height="{ph / 2:.1f}" fill="#3fb950" fill-opacity="0.06"/>',
         f'<rect x="{L}" y="{zero:.1f}" width="{pw}" height="{ph / 2:.1f}" fill="#f85149" fill-opacity="0.06"/>']
    for q in range(5):                                   # round boundaries every 15 minutes
        x = L + q * pw / 4
        o.append(f'<line x1="{x:.1f}" y1="{T}" x2="{x:.1f}" y2="{T + ph}" stroke="#30363d" stroke-width="1" stroke-dasharray="5 4"/>')
    o.append(f'<text x="{L - 6}" y="{T + 12}" text-anchor="end" font-size="13" fill="#56d364">above</text>')
    o.append(f'<text x="{L - 6}" y="{T + ph - 4}" text-anchor="end" font-size="13" fill="#ff7b72">below</text>')
    for r in rs:
        pts = [(sx(p[0], r), sy(p[1])) for p in r["pts"]]
        if len(pts) < 2:
            continue
        path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        poly = path + f" L {pts[-1][0]:.1f},{zero:.1f} L {pts[0][0]:.1f},{zero:.1f} Z"
        o.append(f'<path d="{poly}" fill="#3fb950" fill-opacity="0.35" clip-path="url(#{uid}a)"/>')
        o.append(f'<path d="{poly}" fill="#f85149" fill-opacity="0.35" clip-path="url(#{uid}b)"/>')
        o.append(f'<path d="{path}" fill="none" stroke="#e6edf3" stroke-width="1.8" stroke-linejoin="round"/>')
        for ts, direction in r["cross"]:
            cx = L + (ts - h["start_ts"]) / 3600.0 * pw
            col = "#56d364" if direction > 0 else "#ff7b72"
            o.append(f'<circle cx="{cx:.1f}" cy="{zero:.1f}" r="6" fill="{col}" stroke="#0d1117" stroke-width="1.5"/>')
        x_mid = L + ((r["start"] - h["start_ts"]) + 450) / 3600.0 * pw
        res = results.get(r["ticker"])
        chip = {"YES": ("#3fb950", "YES"), "NO": ("#f85149", "NO")}.get(res)
        lab = labels.get(r["ticker"], "")
        o.append(f'<text x="{x_mid:.1f}" y="{T - 18}" text-anchor="middle" font-size="14" font-weight="700" fill="#c9d1d9">{lab} - {len(r["cross"])} cross{"" if len(r["cross"]) == 1 else "es"}</text>')
        if chip:
            o.append(f'<rect x="{x_mid - 52:.1f}" y="{T - 13}" width="104" height="17" rx="5" fill="{chip[0]}" fill-opacity="0.25" stroke="{chip[0]}"/>'
                     f'<text x="{x_mid:.1f}" y="{T - 0.5}" text-anchor="middle" font-size="13" font-weight="800" fill="{chip[0]}">Kalshi: {chip[1]}</text>')
        else:
            o.append(f'<text x="{x_mid:.1f}" y="{T - 1}" text-anchor="middle" font-size="13" fill="#8b949e">{"settling..." if r["close"] <= now_ts else "in progress"}</text>')
    o.append(f'<line x1="{L}" y1="{zero:.1f}" x2="{width - R}" y2="{zero:.1f}" stroke="#d29922" stroke-width="2.2"/>')
    o.append(f'<text x="{L + 4}" y="{zero - 5:.1f}" font-size="13" font-weight="700" fill="#e3b341">TARGET LINE</text>')
    o.append(f'<text x="{width - R}" y="{height - 6}" text-anchor="end" font-size="12" fill="#8b949e">distance from the line, up to +/- {dmax / 1.12:.0f} basis points (1 bp = 0.01%) - dots = crossings</text>')
    o.append("</svg>")
    return "".join(o)


# What the price is doing around the line RIGHT NOW. The "right" figures come from a backtest of ~1,950 settled rounds (BTC + XRP) of how often the side the
# price was on at the 8th minute (7:00 left) matched Kalshi's real result - see data_cache/line_behavior_stats.json. Information only.
LB_STATS = {
    "BTC": {"FLIPPY": 69.3, "HOVER": 62.2, "ONEWAY": 94.6, "HELD": 81.2, "LEAN": 70.7, "MIXED": 78.4, "CALM0": 87.2},
    "XRP": {"FLIPPY": 71.3, "HOVER": 48.0, "ONEWAY": 89.2, "HELD": 91.3, "LEAN": 63.9, "MIXED": 78.6, "CALM0": 87.2},
}
LB_AGAIN = {"FLIPPY": 51, "HOVER": 70, "ONEWAY": 11, "HELD": 31, "LEAN": 46, "MIXED": 37}      # % that crossed AGAIN in the last 7 minutes (BTC, rounded; XRP is similar)


def classify(rd, now_ts):
    """-> {"code","label","text","c","d","mins_since"} for one round (a dict from analyze), or None."""
    if rd is None:
        return None
    c_ = len(rd["cross"])
    d = rd["d_last"]
    since = (now_ts - rd["cross"][-1][0]) / 60.0 if rd["cross"] else None
    side = "ABOVE" if d > 0 else "BELOW"
    if c_ >= 3:
        code, label, text = "FLIPPY", "FLIPPY", f"the price has crossed the line {c_} times this round - it keeps flipping"
    elif abs(d) < 2:
        code, label, text = "HOVER", "SITTING ON THE LINE", "the price is right on the line (within 0.02%) - it can go either way"
    elif c_ == 0 and abs(d) >= 5:
        code, label, text = "ONEWAY", "ONE-WAY MARKET", f"it went one way and kept going - {abs(d):.0f} bps {side.lower()} the line, no crossings yet"
    elif c_ == 1 and since is not None and since >= 3:
        code, label, text = "HELD", "CROSSED ONCE AND RAN", f"it went through the line {since:.0f} min ago and kept going the other way ({abs(d):.0f} bps {side.lower()})"
    elif c_ == 0:
        code, label, text = "LEAN", "LEANING, NOT FAR YET", f"no crossings yet, but only {abs(d):.0f} bps {side.lower()} the line"
    else:
        code, label, text = "MIXED", "MIXED", f"{c_} crossing{'s' if c_ != 1 else ''} so far, now {abs(d):.0f} bps {side.lower()} the line"
    return {"code": code, "label": label, "text": text, "c": c_, "d": d, "mins_since": since, "side": side}
