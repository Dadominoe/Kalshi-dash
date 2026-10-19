"""Small schematic SVG pictures for every pattern key in pattern_info.INFO (shown in the dashboard's pull-down tabs).
Candle patterns: lists of (open, high, low, close) on a 0-100 scale (higher = higher price).
Chart patterns: a price path [(x, y)...] on 0-100 x 0-100 plus dashed guide lines and text labels."""

W, H = 240, 120
UP, DOWN, GUIDE, MUTED, GOLD = "#3fb950", "#f85149", "#8b949e", "#6e7681", "#f2cc60"


def _px(x): return 10 + x * 2.2
def _py(y): return 108 - y * 0.96


def _wrap(inner):
    return (f'<svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg" '
            f'style="background:#0d1117;border:1px solid #30363d;border-radius:6px">{inner}</svg>')


def _label(x, y, t, color=MUTED):
    return f'<text x="{_px(x):.0f}" y="{_py(y):.0f}" font-size="9" fill="{color}" font-family="sans-serif">{t}</text>'


def _line(p1, p2, color=GUIDE, dash="4 3"):
    return (f'<line x1="{_px(p1[0]):.1f}" y1="{_py(p1[1]):.1f}" x2="{_px(p2[0]):.1f}" y2="{_py(p2[1]):.1f}" '
            f'stroke="{color}" stroke-width="1.2" stroke-dasharray="{dash}"/>')


def candles_svg(cs, rect=None, labels=()):
    n = len(cs)
    step = 200.0 / (n + 1)
    bw = min(24.0, step * 0.55)
    out = []
    if rect:   # (candle_idx_from, candle_idx_to, y_low, y_high)
        a, b, lo, hi = rect
        x1 = 20 + step * (a + 1) - bw; x2 = 20 + step * (b + 1) + bw
        out.append(f'<rect x="{x1:.1f}" y="{_py(hi):.1f}" width="{x2 - x1:.1f}" height="{_py(lo) - _py(hi):.1f}" fill="{GOLD}" opacity="0.18"/>')
    for i, (o, h, l, c) in enumerate(cs):
        cx = 20 + step * (i + 1)
        col = UP if c >= o else DOWN
        top, bot = max(o, c), min(o, c)
        bh = max(1.5, _py(bot) - _py(top))
        out.append(f'<line x1="{cx:.1f}" y1="{_py(h):.1f}" x2="{cx:.1f}" y2="{_py(l):.1f}" stroke="{col}" stroke-width="1.5"/>')
        out.append(f'<rect x="{cx - bw / 2:.1f}" y="{_py(top):.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{col}"/>')
    for (x, y, t) in labels:
        out.append(_label(x, y, t))
    return _wrap("".join(out))


def path_svg(path, lines=(), labels=(), shade=None, fill_between=None):
    out = []
    if shade:   # (x1, x2, y_low, y_high)
        x1, x2, lo, hi = shade
        out.append(f'<rect x="{_px(x1):.1f}" y="{_py(hi):.1f}" width="{_px(x2) - _px(x1):.1f}" height="{_py(lo) - _py(hi):.1f}" fill="{GOLD}" opacity="0.12"/>')
    for (p1, p2, *rest) in lines:
        out.append(_line(p1, p2, rest[0] if rest else GUIDE))
    pts = " ".join(f"{_px(x):.1f},{_py(y):.1f}" for x, y in path)
    out.append(f'<polyline points="{pts}" fill="none" stroke="#58a6ff" stroke-width="2" stroke-linejoin="round"/>')
    ex, ey = path[-1]
    out.append(f'<circle cx="{_px(ex):.1f}" cy="{_py(ey):.1f}" r="3" fill="#58a6ff"/>')
    for (x, y, t) in labels:
        out.append(_label(x, y, t))
    return _wrap("".join(out))


# ---------------------------------------------------------------- candle specs
_DN = [(70, 72, 64, 66), (66, 68, 58, 60)]       # a short run down before the pattern
_UPR = [(30, 38, 28, 36), (36, 46, 34, 44)]      # a short run up before the pattern

CANDLES = {
    "white marubozu": ([(60, 62, 55, 56), (56, 58, 50, 51), (50, 90, 50, 90)], None, [(40, 100, "no wicks")]),
    "black marubozu": ([(40, 45, 38, 44), (44, 50, 42, 49), (50, 50, 10, 10)], None, [(40, 100, "no wicks")]),
    "big white candle": ([(50, 52, 45, 46), (46, 48, 40, 42), (42, 85, 40, 82)], None, []),
    "big black candle": ([(50, 60, 48, 58), (58, 66, 56, 64), (64, 66, 22, 25)], None, []),
    "gravestone doji": (_UPR + [(48, 92, 48, 48)], None, [(55, 100, "rejected high")]),
    "dragonfly doji": (_DN + [(58, 58, 8, 58)], None, [(55, 100, "bought back up")]),
    "long-legged doji": ([(45, 50, 43, 48), (48, 54, 46, 52), (50, 88, 14, 50)], None, []),
    "doji": ([(45, 50, 43, 48), (48, 54, 46, 52), (52, 62, 42, 52.5)], None, []),
    "spinning top": ([(45, 50, 43, 48), (48, 54, 46, 52), (52, 74, 30, 56)], None, []),
    "shooting star": (_UPR + [(48, 92, 46, 50)], None, [(55, 100, "long upper wick")]),
    "inverted hammer": (_DN + [(40, 84, 38, 44)], None, [(55, 100, "after a fall")]),
    "hanging man": (_UPR + [(60, 62, 20, 64)], None, [(55, 100, "after a rise")]),
    "hammer": (_DN + [(50, 54, 12, 53)], None, [(55, 100, "long lower wick")]),
    "long upper shadow": ([(45, 50, 43, 48), (48, 54, 46, 52), (50, 96, 48, 54)], None, []),
    "long lower shadow": ([(55, 58, 52, 54), (54, 56, 50, 52), (50, 54, 4, 52)], None, []),
    "bullish engulfing": ([(60, 62, 52, 56), (58, 59, 50, 52), (49, 78, 47, 76)], None, []),
    "bearish engulfing": ([(40, 48, 38, 46), (44, 52, 42, 50), (53, 55, 26, 30)], None, []),
    "bullish harami": ([(72, 74, 40, 42), (50, 58, 48, 56)], None, []),
    "bearish harami": ([(30, 64, 28, 62), (54, 58, 46, 48)], None, []),
    "piercing line": ([(72, 74, 40, 42), (34, 70, 32, 62)], None, [(65, 100, "closes past the middle")]),
    "dark cloud cover": ([(30, 64, 28, 62), (68, 70, 38, 42)], None, [(65, 100, "closes past the middle")]),
    "tweezer top": ([(40, 72, 38, 66), (66, 72, 44, 48)], None, [(70, 100, "same high")]),
    "tweezer bottom": ([(66, 68, 30, 34), (34, 66, 30, 62)], None, [(30, 100, "same low")]),
    "inside bar": ([(40, 84, 20, 70), (52, 66, 38, 58)], None, []),
    "outside bar": ([(50, 58, 44, 54), (46, 74, 26, 66)], None, []),
    "rising window": ([(40, 50, 38, 48), (62, 78, 60, 74)], None, [(52, 100, "gap")]),
    "falling window": ([(70, 74, 62, 64), (50, 54, 34, 38)], None, [(56, 100, "gap")]),
    "morning": ([(82, 84, 50, 52), (46, 50, 40, 44), (48, 90, 46, 86)], None, []),
    "evening": ([(30, 62, 28, 60), (66, 72, 62, 68), (62, 64, 24, 28)], None, []),
    "three white soldiers": ([(30, 52, 28, 50), (46, 68, 44, 66), (62, 86, 60, 84)], None, []),
    "three black crows": ([(80, 82, 58, 60), (64, 66, 42, 44), (48, 50, 24, 26)], None, []),
    "three inside up": ([(72, 74, 40, 42), (48, 62, 46, 60), (58, 82, 56, 80)], None, []),
    "three inside down": ([(30, 64, 28, 62), (54, 58, 40, 42), (46, 48, 18, 20)], None, []),
    "three outside up": ([(58, 60, 50, 52), (50, 72, 48, 68), (64, 84, 62, 82)], None, []),
    "three outside down": ([(42, 50, 40, 48), (50, 52, 28, 34), (38, 40, 16, 18)], None, []),
    "rising three methods": ([(30, 64, 28, 62), (60, 62, 52, 54), (54, 58, 48, 50), (50, 56, 46, 48), (52, 92, 50, 90)], None, []),
    "falling three methods": ([(80, 82, 46, 48), (52, 60, 50, 58), (58, 62, 54, 60), (60, 64, 56, 62), (58, 60, 18, 20)], None, []),
    "candles in a row": ([(30, 40, 28, 38), (38, 50, 36, 48), (48, 60, 46, 58), (58, 70, 56, 68)], None, []),
    "bullish fair value gap": ([(40, 52, 38, 50), (52, 82, 50, 80), (78, 90, 64, 88)], (0, 2, 52, 64), [(60, 100, "gap")]),
    "bearish fair value gap": ([(80, 82, 66, 68), (68, 70, 38, 40), (42, 56, 30, 32)], (0, 2, 56, 66), [(60, 100, "gap")]),
    "bullish order block": ([(60, 62, 52, 54), (54, 56, 44, 46), (46, 84, 44, 82)], (1, 1, 44, 56), [(60, 100, "last down candle")]),
    "bearish order block": ([(40, 48, 38, 46), (46, 56, 44, 54), (54, 56, 16, 18)], (1, 1, 44, 56), [(60, 100, "last up candle")]),
}

# ---------------------------------------------------------------- path specs: (path, lines, labels)
_ZZ_UP = [(0, 15), (12, 38), (22, 28), (40, 62), (50, 50), (72, 82)]
_ZZ_DN = [(0, 85), (12, 62), (22, 72), (40, 38), (50, 50), (72, 18)]
PATHS = {
    "uptrend structure": (_ZZ_UP, [], [(10, 44, "HH"), (20, 20, "HL"), (38, 68, "HH"), (48, 38, "HL"), (70, 88, "HH")]),
    "downtrend structure": (_ZZ_DN, [], [(10, 56, "LH"), (20, 80, "LL?"), (38, 30, "LL"), (48, 56, "LH"), (70, 12, "LL")]),
    "break of structure": ([(0, 20), (14, 55), (26, 38), (40, 60), (52, 45), (70, 82)], [((0, 60), (100, 60), GOLD)], [(70, 66, "closes above the last high"), (2, 64, "last swing high")]),
    "change of character": (_ZZ_UP[:5] + [(62, 30), (72, 24)], [((20, 28), (100, 28), GOLD)], [(52, 18, "breaks the last higher low"), (2, 32, "last higher low")]),
    "premium zone": ([(0, 20), (20, 80), (40, 40), (70, 78)], [((0, 50), (100, 50), GUIDE)], [(2, 54, "midpoint"), (72, 84, "premium (expensive)")]),
    "discount zone": ([(0, 80), (20, 20), (40, 60), (70, 24)], [((0, 50), (100, 50), GUIDE)], [(2, 54, "midpoint"), (72, 14, "discount (cheap)")]),
    "fibonacci": ([(0, 10), (35, 90), (60, 50)], [((35, 66), (100, 66), GUIDE), ((35, 52), (100, 52), GUIDE), ((35, 43), (100, 43), GOLD)],
                  [(72, 68, "38.2%"), (72, 54, "50%"), (72, 36, "61.8%")]),
    "equal highs": ([(0, 20), (14, 70), (26, 40), (44, 70), (56, 42), (74, 70)], [((0, 70), (100, 70), GOLD)], [(60, 76, "stops sit above")]),
    "equal lows": ([(0, 80), (14, 30), (26, 60), (44, 30), (56, 58), (74, 30)], [((0, 30), (100, 30), GOLD)], [(60, 18, "stops sit below")]),
    "liquidity sweep of the highs": ([(0, 20), (14, 68), (26, 40), (44, 68), (56, 42), (70, 84), (80, 52)], [((0, 68), (100, 68), GOLD)], [(52, 90, "poke above, close back below")]),
    "liquidity sweep of the lows": ([(0, 80), (14, 32), (26, 60), (44, 32), (56, 58), (70, 16), (80, 48)], [((0, 32), (100, 32), GOLD)], [(40, 6, "poke below, close back above")]),
    "fallen ledge": ([(0, 60), (12, 32), (24, 48), (36, 32), (48, 46), (60, 31), (72, 12), (84, 24)], [((0, 32), (100, 32), GOLD)], [(4, 38, "support shelf"), (62, 4, "floor broke")]),
    "ceiling broke": ([(0, 40), (12, 68), (24, 52), (36, 68), (48, 54), (60, 69), (72, 88), (84, 76)], [((0, 68), (100, 68), GOLD)], [(4, 74, "resistance shelf"), (62, 94, "ceiling broke")]),
    "support ledge": ([(0, 60), (12, 32), (24, 48), (36, 32), (48, 46), (60, 33)], [((0, 32), (100, 32), GOLD)], [(64, 22, "floor holding")]),
    "resistance ledge": ([(0, 40), (12, 68), (24, 52), (36, 68), (48, 54), (60, 67)], [((0, 68), (100, 68), GOLD)], [(64, 78, "ceiling holding")]),
    "double top": ([(0, 20), (20, 80), (40, 42), (60, 80), (80, 30)], [((0, 80), (100, 80), GUIDE), ((10, 42), (100, 42), GOLD)], [(44, 34, "neckline"), (8, 86, "two equal peaks")]),
    "double bottom": ([(0, 80), (20, 20), (40, 58), (60, 20), (80, 70)], [((0, 20), (100, 20), GUIDE), ((10, 58), (100, 58), GOLD)], [(44, 64, "neckline"), (8, 10, "two equal troughs")]),
    "triple top": ([(0, 15), (16, 80), (30, 45), (46, 80), (60, 45), (76, 80), (92, 30)], [((0, 80), (100, 80), GUIDE), ((10, 45), (100, 45), GOLD)], [(40, 36, "neckline")]),
    "triple bottom": ([(0, 85), (16, 20), (30, 55), (46, 20), (60, 55), (76, 20), (92, 70)], [((0, 20), (100, 20), GUIDE), ((10, 55), (100, 55), GOLD)], [(40, 62, "neckline")]),
    "inverse head and shoulders": ([(0, 70), (14, 40), (26, 56), (44, 14), (62, 56), (76, 40), (92, 72)], [((14, 56), (100, 56), GOLD)], [(44, 4, "head"), (6, 32, "shoulder"), (70, 32, "shoulder"), (80, 60, "neckline")]),
    "head and shoulders": ([(0, 30), (14, 60), (26, 44), (44, 86), (62, 44), (76, 60), (92, 28)], [((14, 44), (100, 44), GOLD)], [(44, 92, "head"), (6, 68, "shoulder"), (70, 68, "shoulder"), (80, 38, "neckline")]),
    "ascending triangle": ([(0, 30), (14, 70), (28, 42), (44, 70), (58, 52), (72, 70), (84, 60), (96, 88)], [((0, 70), (100, 70), GOLD), ((0, 30), (84, 62), GUIDE)], [(60, 78, "flat ceiling"), (6, 20, "rising lows")]),
    "descending triangle": ([(0, 70), (14, 30), (28, 58), (44, 30), (58, 48), (72, 30), (84, 40), (96, 12)], [((0, 30), (100, 30), GOLD), ((0, 70), (84, 38), GUIDE)], [(60, 20, "flat floor"), (6, 76, "falling highs")]),
    "symmetrical triangle": ([(0, 50), (12, 80), (26, 26), (40, 72), (54, 36), (66, 62), (78, 46), (90, 60)], [((0, 86), (100, 54), GUIDE), ((0, 18), (100, 52), GUIDE)], [(4, 92, "falling highs"), (4, 8, "rising lows")]),
    "rising wedge": ([(0, 20), (14, 44), (26, 32), (44, 58), (56, 50), (74, 70), (84, 62), (96, 40)], [((0, 28), (84, 74), GUIDE), ((0, 12), (84, 58), GUIDE)], [(60, 82, "narrowing up"), (80, 30, "breaks down")]),
    "falling wedge": ([(0, 80), (14, 60), (26, 70), (44, 42), (56, 52), (74, 30), (84, 38), (96, 62)], [((0, 88), (84, 40), GUIDE), ((0, 70), (84, 24), GUIDE)], [(40, 12, "narrowing down"), (80, 70, "breaks up")]),
    "broadening": ([(0, 50), (12, 60), (24, 40), (40, 72), (54, 28), (70, 84), (84, 14)], [((0, 56), (100, 92), GUIDE), ((0, 44), (100, 8), GUIDE)], [(40, 94, "swings widen")]),
    "rectangle": ([(0, 30), (12, 70), (26, 32), (40, 70), (54, 32), (68, 70), (82, 34), (96, 80)], [((0, 70), (100, 70), GUIDE), ((0, 32), (100, 32), GUIDE)], [(60, 76, "ceiling"), (60, 20, "floor")]),
    "up-channel": ([(0, 20), (12, 40), (24, 28), (38, 52), (50, 40), (64, 64), (76, 52), (90, 76)], [((0, 32), (100, 84), GUIDE), ((0, 12), (100, 64), GUIDE)], [(4, 60, "parallel, rising")]),
    "down-channel": ([(0, 80), (12, 60), (24, 72), (38, 48), (50, 60), (64, 36), (76, 48), (90, 24)], [((0, 88), (100, 36), GUIDE), ((0, 68), (100, 16), GUIDE)], [(4, 40, "parallel, falling")]),
    "bull flag": ([(0, 10), (30, 70), (36, 62), (44, 66), (52, 58), (60, 62), (68, 54), (84, 90)], [((30, 72), (66, 62), GUIDE), ((30, 60), (66, 48), GUIDE)], [(4, 40, "sharp rise"), (40, 76, "tight pause"), (70, 94, "continues up")]),
    "bear flag": ([(0, 90), (30, 30), (36, 38), (44, 34), (52, 42), (60, 38), (68, 46), (84, 10)], [((30, 40), (66, 52), GUIDE), ((30, 28), (66, 40), GUIDE)], [(4, 60, "sharp drop"), (40, 22, "tight pause"), (70, 6, "continues down")]),
    "cup with handle": ([(0, 80), (12, 52), (26, 32), (40, 28), (54, 36), (68, 62), (78, 78), (86, 68), (94, 92)], [((0, 80), (80, 80), GUIDE)], [(30, 16, "rounded cup"), (74, 56, "handle"), (86, 96, "breakout")]),
    "rounding bottom": ([(0, 80), (14, 56), (28, 38), (42, 32), (56, 38), (70, 56), (84, 78), (96, 88)], [], [(30, 20, "slow U-shape")]),
    "rounding top": ([(0, 20), (14, 44), (28, 62), (42, 68), (56, 62), (70, 44), (84, 22), (96, 12)], [], [(30, 80, "slow upside-down U")]),
    "moving average": ([(0, 30), (14, 40), (28, 34), (42, 56), (56, 50), (72, 74), (90, 80)], [((0, 28), (100, 70), GOLD)], [(60, 40, "average"), (4, 14, "price above a rising average = healthy")]),
    "drawdown": ([(0, 20), (20, 90), (40, 50), (60, 30), (80, 38)], [((0, 90), (100, 90), GUIDE)], [(60, 96, "highest point"), (66, 44, "drawdown")]),
    "higher closes": ([(0, 20), (16, 30), (32, 44), (48, 56), (64, 70), (80, 84)], [], [(10, 90, "each close above the last")]),
    "lower closes": ([(0, 84), (16, 70), (32, 56), (48, 44), (64, 30), (80, 16)], [], [(10, 6, "each close below the last")]),
}


def svg_for(key):
    if key in CANDLES:
        cs, rect, labels = CANDLES[key]
        return candles_svg(cs, rect, labels)
    if key in PATHS:
        path, lines, labels = PATHS[key]
        return path_svg(path, lines, labels)
    return None
