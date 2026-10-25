def _ladder_pivots(closed, L):
    out = []
    for i in range(L, len(closed) - L):
        hi, lo = closed[i][2], closed[i][3]
        if all(hi > closed[j][2] for j in range(i - L, i)) and all(hi >= closed[j][2] for j in range(i + 1, i + L + 1)):
            out.append((i, hi, "H"))
        if all(lo < closed[j][3] for j in range(i - L, i)) and all(lo <= closed[j][3] for j in range(i + 1, i + L + 1)):
            out.append((i, lo, "L"))
    return sorted(out)


def swing_ladder(cs, L=3):
    """[(candle index, price, 'H'/'L', tag)] - tag is HH / LH / EH (equal) for highs and HL / LL / EL for lows, each compared with the swing of the same kind before it."""
    closed = cs[:-1]
    z = []
    for p_ in _ladder_pivots(closed, L):
        if z and z[-1][2] == p_[2]:
            if (p_[2] == "H" and p_[1] > z[-1][1]) or (p_[2] == "L" and p_[1] < z[-1][1]):
                z[-1] = p_                                   # two highs (or lows) in a row: keep the more extreme one so the steps alternate
        else:
            z.append(p_)
    lastH = lastL = None
    out = []
    for i, px, t in z:
        eq = px * 0.00012
        if t == "H":
            tag = "H" if lastH is None else ("EH" if abs(px - lastH) <= eq else ("HH" if px > lastH else "LH"))
            lastH = px
        else:
            tag = "L" if lastL is None else ("EL" if abs(px - lastL) <= eq else ("HL" if px > lastL else "LL"))
            lastL = px
        out.append((i, px, t, tag))
    return out


def ladder_verdict(lad):
    hs = [x for x in lad if x[2] == "H"]
    ls = [x for x in lad if x[2] == "L"]
    if len(hs) < 2 or len(ls) < 2:
        return "NOT ENOUGH SWINGS YET", "flat", None, "Not enough swing highs and lows on this chart yet to build a staircase.", 0
    h, l_ = hs[-1][3], ls[-1][3]
    run = 0
    if h == "HH" and l_ == "HL":
        for x in reversed(lad):
            if x[3] in ("HH", "HL"):
                run += 1
            else:
                break
        brk = ls[-1][1]
        return "STAIRS UP", "up", brk, f"Each new high is higher and each new low is higher - {run} steps up in a row. The staircase breaks if price closes below the last higher low.", run
    if h == "LH" and l_ == "LL":
        for x in reversed(lad):
            if x[3] in ("LH", "LL"):
                run += 1
            else:
                break
        brk = hs[-1][1]
        return "STAIRS DOWN", "down", brk, f"Each new high is lower and each new low is lower - {run} steps down in a row. The staircase breaks if price closes above the last lower high.", run
    if h == "HH" and l_ == "LL":
        return "WIDENING", "flat", None, "Highs are getting higher AND lows are getting lower - the swings are getting bigger in both directions (choppy and unclear).", 0
    if h == "LH" and l_ == "HL":
        return "NARROWING", "flat", None, "Highs are getting lower AND lows are getting higher - the swings are squeezing into a point, which often ends in a break either way.", 0
    return "MIXED", "flat", None, "The last swings do not line up as a staircase either way (the latest high is " + h + " and the latest low is " + l_ + ").", 0


def ladder_svg(cs, lad, uid, dec, nshow=90, width=1100, height=300):
    L_, R_, T_, B_ = 56, 64, 18, 22
    pw, ph = width - L_ - R_, height - T_ - B_
    n = len(cs)
    start = max(0, n - nshow)
    vis = cs[start:]
    ext = max(8, len(vis) // 4)                           # blank room on the right so the trend lines can be drawn into the future
    lo = min(x[3] for x in vis); hi = max(x[2] for x in vis)
    pad = (hi - lo) * 0.08 or hi * 0.0005
    lo -= pad; hi += pad
    sx = lambda i: L_ + (i - start + 0.5) / (len(vis) + ext) * pw
    sy = lambda v: T_ + (hi - v) / (hi - lo) * ph
    bw = max(2.0, pw / (len(vis) + ext) * 0.62)
    o = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" class="lc-svg" role="img" aria-label="swing high and low staircase">',
         f'<defs><clipPath id="{uid}c"><rect x="{L_}" y="{T_}" width="{pw}" height="{ph}"/></clipPath></defs>',
         f'<rect x="0" y="0" width="{width}" height="{height}" fill="#0d1117"/>']
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = sy(v)
        o.append(f'<line x1="{L_}" y1="{y:.1f}" x2="{width - R_}" y2="{y:.1f}" stroke="#21262d" stroke-width="1"/>'
                 f'<text x="{L_ - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="#8b949e">{v:,.{dec}f}</text>')
    for j, x in enumerate(vis):
        i = start + j
        col = "#3fb950" if x[4] >= x[1] else "#f85149"
        cx = sx(i)
        o.append(f'<line x1="{cx:.1f}" y1="{sy(x[2]):.1f}" x2="{cx:.1f}" y2="{sy(x[3]):.1f}" stroke="{col}" stroke-width="1.2"/>')
        yt, yb = sy(max(x[1], x[4])), sy(min(x[1], x[4]))
        o.append(f'<rect x="{cx - bw / 2:.1f}" y="{yt:.1f}" width="{bw:.1f}" height="{max(yb - yt, 1):.1f}" fill="{col}" fill-opacity="0.85"/>')
    vl = [x for x in lad if x[0] >= start - 1]
    for i, px, t, tag in vl:                                  # the rung: runs right from each swing until a candle closes through it
        broke = None
        for k in range(i + 1, n):
            if (t == "H" and cs[k][4] > px) or (t == "L" and cs[k][4] < px):
                broke = k
                break
        x2 = sx(broke) if broke is not None else width - R_
        col = "#3fb950" if tag in ("HH", "HL") else ("#f85149" if tag in ("LH", "LL") else "#9da7b3")
        o.append(f'<line x1="{sx(max(i, start)):.1f}" y1="{sy(px):.1f}" x2="{x2:.1f}" y2="{sy(px):.1f}" stroke="{col}" stroke-width="1.3" stroke-dasharray="5 4" stroke-opacity="0.8"/>')
        if broke is not None and broke >= start:
            up_ = (t == "H")                                    # a candle closed through a swing HIGH = break of structure up; through a swing LOW = down
            bc = "#56d364" if up_ else "#ff7b72"
            o.append(f'<text x="{sx(broke):.1f}" y="{(sy(px) + 15) if up_ else (sy(px) - 8):.1f}" text-anchor="middle" font-size="11" font-weight="800" fill="{bc}">BOS {"up" if up_ else "down"}</text>')
            role_up, s0 = up_, broke                              # role_up True = price is above the level, so it acts as a FLOOR; False = price below, a CEILING
            segs, flips = [], []
            for k2 in range(broke + 1, n):
                if (role_up and cs[k2][4] < px) or ((not role_up) and cs[k2][4] > px):
                    segs.append((s0, k2, role_up)); flips.append((k2, role_up)); s0, role_up = k2, (not role_up)
            segs.append((s0, None, role_up))
            for a0, a1, ru in segs:
                active = a1 is None
                sc = "#56d364" if ru else "#ff7b72"
                xa, xb = sx(a0), (width - R_ if active else sx(a1))
                o.append(f'<line x1="{xa:.1f}" y1="{sy(px):.1f}" x2="{xb:.1f}" y2="{sy(px):.1f}" stroke="{sc}" stroke-width="{3 if active else 2}" stroke-opacity="{0.95 if active else 0.4}"/>')
            for fk, fru in flips[-3:]:
                o.append(f'<text x="{sx(fk):.1f}" y="{(sy(px) - 8) if fru else (sy(px) + 15):.1f}" text-anchor="middle" font-size="10" font-weight="800" fill="{"#ff7b72" if fru else "#56d364"}">{"floor broke - now ceiling" if fru else "ceiling broke - now floor"}</text>')
            o.append(f'<text x="{width - R_ + 4}" y="{sy(px) + 4:.1f}" font-size="11" font-weight="700" fill="{"#56d364" if segs[-1][2] else "#ff7b72"}">{"floor" if segs[-1][2] else "ceiling"}</text>')
    if len(vl) >= 2:
        o.append('<polyline points="' + " ".join(f"{sx(i):.1f},{sy(px):.1f}" for i, px, t, tag in vl) + '" fill="none" stroke="#e3b341" stroke-width="2.2" stroke-linejoin="round"/>')
    for i, px, t, tag in vl:
        col = "#56d364" if tag in ("HH", "HL") else ("#ff7b72" if tag in ("LH", "LL") else "#c9d1d9")
        o.append(f'<circle cx="{sx(i):.1f}" cy="{sy(px):.1f}" r="4.5" fill="{col}" stroke="#0d1117" stroke-width="1.5"/>'
                 f'<text x="{sx(i):.1f}" y="{(sy(px) - 9) if t == "H" else (sy(px) + 18):.1f}" text-anchor="middle" font-size="13" font-weight="800" fill="{col}">{tag}</text>')
    # the slanted trend lines you draw: through the last two swing highs (resistance) and the last two swing lows (support), extended into the blank space
    tl = {}
    for kind, nm_ in (("H", "res"), ("L", "sup")):
        pts_ = [x for x in lad if x[2] == kind][-2:]
        if len(pts_) == 2 and pts_[1][0] - pts_[0][0] >= 3:
            tl[nm_] = (pts_[0][0], pts_[0][1], (pts_[1][1] - pts_[0][1]) / (pts_[1][0] - pts_[0][0]))
    xe = n - 1 + ext
    for nm_, (i1, p1, sl) in tl.items():
        va = lambda i, p1=p1, i1=i1, sl=sl: p1 + sl * (i - i1)
        i0 = max(i1, start)
        o.append(f'<line x1="{sx(i0):.1f}" y1="{sy(va(i0)):.1f}" x2="{sx(xe):.1f}" y2="{sy(va(xe)):.1f}" stroke="#ffd54a" stroke-width="2.6" clip-path="url(#{uid}c)"/>')
        wd = ("falling resistance" if sl < 0 else "rising resistance") if nm_ == "res" else ("rising support" if sl > 0 else "falling support")
        yy = min(max(sy(va(xe)), T_ + 10), T_ + ph - 6)
        o.append(f'<text x="{sx(xe) - 4:.1f}" y="{yy - 5:.1f}" text-anchor="end" font-size="12" font-weight="800" fill="#ffd54a">{wd}</text>')
    if len(tl) == 2:
        (ia, pa, sa), (ib, pb, sb) = tl["res"], tl["sup"]
        if abs(sa - sb) > 1e-12:
            xs = (pb - pa + sa * ia - sb * ib) / (sa - sb)
            if n - 1 <= xs <= xe:
                ys = pa + sa * (xs - ia)
                o.append(f'<circle cx="{sx(xs):.1f}" cy="{sy(ys):.1f}" r="7" fill="none" stroke="#ffd54a" stroke-width="2.4" clip-path="url(#{uid}c)"/>'
                         f'<text x="{sx(xs):.1f}" y="{sy(ys) - 12:.1f}" text-anchor="middle" font-size="12" font-weight="800" fill="#ffd54a">squeeze in ~{int(xs - (n - 1))} candles</text>')
    o.append(f'<text x="{L_}" y="{height - 5}" font-size="11" fill="#8b949e">last {len(vis)} candles - thin gold zigzag = the swings - dashed = each step until a candle closes through it - solid green/red = floor/ceiling from the break - bold yellow = slanted trend lines - HH higher high, HL higher low, LH lower high, LL lower low, E = equal</text>')
    o.append("</svg>")
    return "".join(o)


