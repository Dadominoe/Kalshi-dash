"""Big, labeled teaching pictures for every pattern (the pull-down info tabs), in the style of a trading-school diagram: the candles, the named swing points (First Peak, Second Peak, Left Shoulder ...),
the neckline / key level, a breakout arrow, and the pattern's name in a black tag. The small plain pictures in pattern_diagrams.py stay for the call banner. Information only."""
from src.pattern_diagrams import CANDLES, PATHS, UP, DOWN, GOLD, ROUND_KEYS, smooth_d, _path_candles

BW, BH = 520, 300
FONT = "Arial, Helvetica, sans-serif"
INK = "#e6edf3"
BRK_UP, BRK_DN = "#3fb950", "#ff5d5d"


def _bx(x): return 30 + x * 4.6
def _by(y): return 220 - y * 1.9


def _t(x, y, text, size=16, color=INK, anchor="middle", weight=700):
    return f'<text x="{x:.0f}" y="{y:.0f}" font-size="{size}" font-weight="{weight}" fill="{color}" text-anchor="{anchor}" font-family="{FONT}">{text}</text>'


def _wrap(inner, name):
    tag_w = max(150, len(name) * 15 + 36)
    tag = (f'<rect x="{(BW - tag_w) / 2:.0f}" y="258" width="{tag_w}" height="34" fill="#000" stroke="#e6edf3" stroke-width="1.5"/>'
           f'<text x="{BW / 2:.0f}" y="283" font-size="22" font-weight="800" fill="#ffffff" text-anchor="middle" font-family="{FONT}">{name.upper()}</text>')
    return (f'<svg viewBox="0 0 {BW} {BH}" width="{BW}" height="{BH}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{name}" '
            f'style="background:#0d1117;border:1px solid #30363d;border-radius:6px">{inner}{tag}</svg>')


# the named swing points: (index of the point in the path, text, 'hi' = label above / 'lo' = label below)
PTS = {
    "double top": [(1, "First Peak", "hi"), (3, "Second Peak", "hi")],
    "double bottom": [(1, "First Low", "lo"), (3, "Second Low", "lo")],
    "triple top": [(1, "Peak 1", "hi"), (3, "Peak 2", "hi"), (5, "Peak 3", "hi")],
    "triple bottom": [(1, "Low 1", "lo"), (3, "Low 2", "lo"), (5, "Low 3", "lo")],
    "head and shoulders": [(1, "Left Shoulder", "hi"), (3, "Head", "hi"), (5, "Right Shoulder", "hi")],
    "inverse head and shoulders": [(1, "Left Shoulder", "lo"), (3, "Head", "lo"), (5, "Right Shoulder", "lo")],
    "ascending triangle": [(1, "Touch 1", "hi"), (3, "Touch 2", "hi"), (5, "Touch 3", "hi")],
    "descending triangle": [(1, "Touch 1", "lo"), (3, "Touch 2", "lo"), (5, "Touch 3", "lo")],
    "cup with handle": [(3, "Cup", "lo"), (7, "Handle", "lo")],
    "bull flag": [(1, "Pole", "hi")],
    "bear flag": [(1, "Pole", "lo")],
    "equal highs": [(1, "High 1", "hi"), (3, "High 2", "hi"), (5, "High 3", "hi")],
    "equal lows": [(1, "Low 1", "lo"), (3, "Low 2", "lo"), (5, "Low 3", "lo")],
    "break of structure": [(1, "Swing High", "hi"), (3, "Lower Swing", "hi")],
    "liquidity sweep of the highs": [(1, "High 1", "hi"), (3, "High 2", "hi"), (5, "The Sweep", "hi")],
    "liquidity sweep of the lows": [(1, "Low 1", "lo"), (3, "Low 2", "lo"), (5, "The Sweep", "lo")],
    "uptrend structure": [(1, "Higher High", "hi"), (2, "Higher Low", "lo"), (3, "Higher High", "hi"), (4, "Higher Low", "lo"), (5, "Higher High", "hi")],
    "downtrend structure": [(1, "Lower High", "hi"), (2, "Lower Low", "lo"), (3, "Lower Low", "lo"), (4, "Lower High", "hi"), (5, "Lower Low", "lo")],
}
# where it breaks out: (direction, how far the arrow runs in picture units) from the last point of the path
BRK = {
    "double top": "down", "double bottom": "up", "triple top": "down", "triple bottom": "up", "head and shoulders": "down", "inverse head and shoulders": "up",
    "ascending triangle": "up", "descending triangle": "down", "rising wedge": "down", "falling wedge": "up", "bull flag": "up", "bear flag": "down",
    "cup with handle": "up", "break of structure": "up", "ceiling broke": "up", "fallen ledge": "down", "liquidity sweep of the highs": "down", "liquidity sweep of the lows": "up",
    "rectangle": "up", "symmetrical triangle": "up", "rounding bottom": "up", "rounding top": "down",
}
BRK_TEXT = {"liquidity sweep of the highs": "Reversal", "liquidity sweep of the lows": "Reversal", "rectangle": "Breakout (either way)", "symmetrical triangle": "Breakout (either way)"}

# a short note under each candle (the common candle patterns)
NOTES = {
    "bullish engulfing": ["small red", "small red", "BIG green swallows it"],
    "bearish engulfing": ["small green", "small green", "BIG red swallows it"],
    "hammer": ["falling", "falling", "long lower wick"],
    "shooting star": ["rising", "rising", "long upper wick"],
    "hanging man": ["rising", "rising", "long lower wick"],
    "inverted hammer": ["falling", "falling", "long upper wick"],
    "morning": ["big red", "small star", "big green"],
    "evening": ["big green", "small star", "big red"],
    "bullish harami": ["BIG red", "small green inside"],
    "bearish harami": ["BIG green", "small red inside"],
    "piercing line": ["big red", "green past the middle"],
    "dark cloud cover": ["big green", "red past the middle"],
    "tweezer top": ["green", "red, same high"],
    "tweezer bottom": ["red", "green, same low"],
    "three white soldiers": ["green", "green", "green"],
    "three black crows": ["red", "red", "red"],
    "doji": ["", "", "open = close"],
    "dragonfly doji": ["", "", "bought back up"],
    "gravestone doji": ["", "", "pushed back down"],
}


def _candle(cx, bw, o, h, l, c):
    col = UP if c >= o else DOWN
    top, bot = max(o, c), min(o, c)
    return (f'<line x1="{cx:.1f}" y1="{_by(h):.1f}" x2="{cx:.1f}" y2="{_by(l):.1f}" stroke="{col}" stroke-width="2"/>'
            f'<rect x="{cx - bw / 2:.1f}" y="{_by(top):.1f}" width="{bw:.1f}" height="{max(2.0, _by(bot) - _by(top)):.1f}" fill="{col}"/>')


def _arrow(x1, y1, x2, y2, col):
    import math
    a = math.atan2(y2 - y1, x2 - x1)
    hx = [(x2, y2), (x2 - 14 * math.cos(a - 0.45), y2 - 14 * math.sin(a - 0.45)), (x2 - 14 * math.cos(a + 0.45), y2 - 14 * math.sin(a + 0.45))]
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{col}" stroke-width="4" stroke-linecap="round"/>'
            f'<polygon points="{" ".join(f"{px:.1f},{py:.1f}" for px, py in hx)}" fill="{col}"/>')


def _label(x, y, text, size=14, color=INK):
    px = _bx(x)
    est = len(text) * size * 0.56
    anchor = "start"
    if px + est > BW - 8:
        px, anchor = BW - 8, "end"
    return _t(px, _by(y), text, size, color, anchor)


def _cross(path, lines):
    """Where the path goes through one of the key lines near its end: the gold line (neckline / level) first, then any other. -> (x, y) in picture units, or None."""
    segs = list(zip(path, path[1:]))[-3:][::-1]                                    # the last three moves, newest first
    ordered = [ln for ln in lines if len(ln) > 2 and ln[2] == GOLD] + [ln for ln in lines if not (len(ln) > 2 and ln[2] == GOLD)]
    for (a, b) in segs:
        for ln in ordered:
            (x1, y1), (x2, y2) = ln[0], ln[1]
            dx, dy = x2 - x1, y2 - y1
            ex, ey = b[0] - a[0], b[1] - a[1]
            den = dx * ey - dy * ex
            if abs(den) < 1e-9:
                continue
            t = ((a[0] - x1) * ey - (a[1] - y1) * ex) / den            # position along the key line
            u = ((a[0] - x1) * dy - (a[1] - y1) * dx) / den            # position along the path move
            if 0 <= u <= 1 and -0.05 <= t <= 1.7:
                return (a[0] + ex * u, a[1] + ey * u)
    return None


def big_svg(key):
    name = key[:1].upper() + key[1:]
    if key in CANDLES:
        cs, rect, labels = CANDLES[key]
        n = len(cs)
        step = 440.0 / (n + 1)
        bw = min(54.0, step * 0.5)
        out = []
        if rect:
            a, b, lo, hi = rect
            x1, x2 = 40 + step * (a + 1) - bw, 40 + step * (b + 1) + bw
            out.append(f'<rect x="{x1:.1f}" y="{_by(hi):.1f}" width="{x2 - x1:.1f}" height="{_by(lo) - _by(hi):.1f}" fill="{GOLD}" opacity="0.2"/>')
        notes = NOTES.get(key, [])
        for i, (o, h, l, c) in enumerate(cs):
            cx = 40 + step * (i + 1)
            out.append(_candle(cx, bw, o, h, l, c))
            out.append(_t(cx, 236, str(i + 1), 15, "#8b949e"))
            if i < len(notes) and notes[i]:
                out.append(_t(cx, 252, notes[i], 12, "#c9d1d9", weight=600))
        for (x, y, text) in labels:
            out.append(_t(BW / 2, 22, text, 16, GOLD) if y >= 100 else _label(x, y, text, 14, GOLD))
        return _wrap("".join(out), name)
    if key in PATHS:
        path, lines, labels = PATHS[key]
        out = []
        for (p1, p2, *rest) in lines:                                              # the key levels: solid, gold ones are the neckline / the level that matters
            col = rest[0] if rest else "#8b949e"
            gold = col == GOLD
            out.append(f'<line x1="{_bx(p1[0]):.1f}" y1="{_by(p1[1]):.1f}" x2="{_bx(p2[0]):.1f}" y2="{_by(p2[1]):.1f}" stroke="{col if gold else "#6e7681"}" stroke-width="{2.4 if gold else 1.6}"{"" if gold else " stroke-dasharray=\"6 4\""}/>')
        cs = _path_candles(path, 30)
        bw = max(5.0, (_bx(path[-1][0]) - _bx(path[0][0])) / (len(cs) - 1) * 0.66)
        if key in ROUND_KEYS:                                                      # the rounded shape itself, dashed, drawn with the candles
            out.append(f'<path d="{smooth_d([(_bx(x), _by(y)) for x, y in path])}" fill="none" stroke="{GOLD}" stroke-width="3" stroke-dasharray="9 5" opacity="0.95"/>')
        for (o, h, l, c, x) in cs:
            out.append(_candle(_bx(x), bw, o, h, l, c))
        for (idx, text, side) in PTS.get(key, []):                                 # the named swing points
            x, y = path[idx]
            cx = _bx(x)
            if side == "hi":
                out.append(f'<circle cx="{cx:.1f}" cy="{_by(y) - 9:.1f}" r="4" fill="none" stroke="{INK}" stroke-width="1.6"/>' + _t(cx, _by(y) - 20, text, 15))
            else:
                out.append(f'<circle cx="{cx:.1f}" cy="{_by(y) + 11:.1f}" r="4" fill="none" stroke="{INK}" stroke-width="1.6"/>' + _t(cx, _by(y) + 30, text, 15))
        pts_xy = [path[i] for (i, _t_, _s_) in PTS.get(key, [])]
        if BRK.get(key):                                                                # the breakout ring, word and arrow take their own spot
            _c = _cross(path, lines)
            pts_xy += [_c] if _c else []
            pts_xy += [path[-1], (path[-1][0] + 6, path[-1][1] + (12 if BRK[key] == "up" else -12))]
        for (x, y, text) in labels:
            if any(abs(x - px_) < 26 and abs(y - py_) < 16 for (px_, py_) in pts_xy):         # the named points already label that spot
                continue
            out.append(_label(x, y, text, 13, GOLD if "neckline" in text else "#c9d1d9"))
        d = BRK.get(key)
        if d:                                                                      # the breakout: it is drawn where price goes THROUGH the neckline / trend line (ringed), with the word beside it and an arrow running on
            col = BRK_UP if d == "up" else BRK_DN
            cross = _cross(path, lines)
            lx, ly = path[-1]
            if cross is not None:
                cx_, cy_ = _bx(cross[0]), _by(cross[1])
                out.append(f'<circle cx="{cx_:.1f}" cy="{cy_:.1f}" r="17" fill="{col}" fill-opacity="0.22" stroke="{col}" stroke-width="2.5"/>')
                wx = cx_ + 22 if cx_ < BW - 130 else cx_ - 22
                out.append(_t(wx, cy_ + (-16 if d == "up" else 30), BRK_TEXT.get(key, "Breakout"), 17, col, "start" if cx_ < BW - 130 else "end", 800))
            x1, y1 = _bx(lx) + 4, _by(ly)
            x2, y2 = min(BW - 18, x1 + 34), max(24, min(214, y1 + (-52 if d == "up" else 52)))
            out.append(_arrow(x1, y1, x2, y2, col))
        return _wrap("".join(out), name)
    return None
