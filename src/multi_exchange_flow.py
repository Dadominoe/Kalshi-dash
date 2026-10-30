"""Live buy-vs-sell pressure from as many public exchange feeds as we can reach (information only - never used for buying).

Two reads per coin:
  * TRADE FLOW  - every trade printed on each venue is classified by who hit whom (the taker / aggressor side) and summed in
                  dollars over rolling windows (10s / 30s / 1m / 5m).  Buyers lifting offers = buy pressure.
  * BOOK DEPTH  - resting bid dollars vs resting ask dollars close to the mid price on each venue (who is stacked up where).

Public, key-less WebSocket / REST endpoints only.  Venues that are geo-blocked or silent simply drop out of the count.
Coinbase and Kraken trades are fed in by the dashboard's own sockets (hub.record), so they are not opened twice.
"""
import csv
import gzip
import json
import os
import threading
import time
import urllib.request

try:
    import websocket
except Exception:  # pragma: no cover - the dashboard already requires it
    websocket = None

ASSETS = ("BTC", "XRP")
WINDOWS = (10, 30, 60, 300)
KEEP_SECONDS = 900
_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PRESSURE_LOG = os.path.join(_ROOT, "bot_state", "pressure_log.csv")
INCOMING_LOG = os.path.join(_ROOT, "bot_state", "incoming_log.csv")

# venue -> (display label, kind)
VENUES = {
    "coinbase": ("Coinbase", "spot"), "kraken": ("Kraken", "spot"), "bitstamp": ("Bitstamp", "spot"),
    "gemini": ("Gemini", "spot"), "bitfinex": ("Bitfinex", "spot"), "cryptocom": ("Crypto.com", "spot"),
    "okx": ("OKX", "spot"), "bybit": ("Bybit", "spot"), "gate": ("Gate.io", "spot"), "bitget": ("Bitget", "spot"),
    "kucoin": ("KuCoin", "spot"), "binance": ("Binance", "spot"), "binanceus": ("Binance.US", "spot"),
    "okx_perp": ("OKX perp", "perp"), "bybit_perp": ("Bybit perp", "perp"), "hyperliquid": ("Hyperliquid perp", "perp"),
    "deribit": ("Deribit perp", "perp"),
    "bitget_perp": ("Bitget perp", "perp"), "gate_perp": ("Gate perp", "perp"), "mexc_perp": ("MEXC perp", "perp"), "kraken_perp": ("Kraken perp", "perp"),
    "dydx": ("dYdX perp", "perp"), "bingx_perp": ("BingX perp", "perp"), "htx": ("HTX", "spot"), "htx_perp": ("HTX perp", "perp"),
    "upbit": ("Upbit (won)", "spot"), "bithumb": ("Bithumb (won)", "spot"),
    "whitebit": ("WhiteBIT", "spot"), "lbank": ("LBank", "spot"), "hashkey": ("HashKey", "spot"), "bitflyer": ("bitFlyer (yen)", "spot"),
}


class FlowHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.buckets = {a: {} for a in ASSETS}      # asset -> {second: {venue: [buy_usd, sell_usd, n]}}
        self.last_seen = {}                         # (venue, asset) -> ts of last trade
        self.books = {}                             # (venue, asset) -> (ts, bid_usd, ask_usd)
        self.basis = {}                             # (venue, asset) -> slowly-learned price offset vs the dollar venues (USDT venues sit a few bps off)
        self.levels = {}                            # (venue, asset) -> (ts, {'mid', 'bids': [(price, usd)], 'asks': [...]}) - for the all-exchange walls
        self.last_price = {}                        # (venue, asset) -> last trade price
        self._pruned = 0
        self._last_incoming = {}
        self.line = {}                              # asset -> {"ticker","strike"}: the Kalshi line of the round in progress
        self.line_vol = {}                          # asset -> {"ticker","above","below"} dollars traded on USD venues above / below the line
        self.on_trade = None                        # optional hook: fn(venue, asset, usd, side, price) for every trade
        self.bars = {a: {} for a in ASSETS}         # asset -> venue -> {minute: [open, close, usd_volume]} (for the composite RSI)
        self.tbars = {a: {} for a in ASSETS}        # asset -> {second: [open, high, low, close, buy_usd, sell_usd]}: price from Coinbase + Kraken trades, dollars from every exchange (the live trades chart)
        self.deriv = {a: {} for a in ASSETS}        # asset -> funding / open interest / premium / liquidations (perp markets)
        self.oi_hist = {a: [] for a in ASSETS}      # asset -> [(ts, total open interest usd)]

    # -------------------------------------------------------------- input
    def record(self, venue, asset, usd, side, price=None):
        if asset not in self.buckets or usd <= 0 or side not in ("buy", "sell"):
            return
        now = time.time()
        sec = int(now)
        with self.lock:
            b = self.buckets[asset]
            row = b.setdefault(sec, {}).setdefault(venue, [0.0, 0.0, 0])
            row[0 if side == "buy" else 1] += usd
            row[2] += 1
            tb = self.tbars[asset].get(sec)
            if tb is None:
                tb = self.tbars[asset][sec] = [None, None, None, None, 0.0, 0.0]
            tb[4 if side == "buy" else 5] += usd
            self.last_seen[(venue, asset)] = now
            ln = self.line.get(asset)
            if ln and price and venue in ("coinbase", "kraken", "bitstamp", "gemini", "bitfinex", "cryptocom"):
                lv = self.line_vol.setdefault(asset, {"ticker": ln["ticker"], "above": 0.0, "below": 0.0})
                lv["above" if price >= ln["strike"] else "below"] += usd
            if price:
                self.last_price[(venue, asset)] = price
                mn = sec // 60
                vb = self.bars[asset].setdefault(venue, {})
                bar = vb.get(mn)
                if bar is None:
                    vb[mn] = [price, price, usd]
                else:
                    bar[1] = price; bar[2] += usd
            if sec - self._pruned >= 30:
                self._pruned = sec
                cutoff = sec - KEEP_SECONDS
                for a in self.buckets:
                    for k in [k for k in self.buckets[a] if k < cutoff]:
                        del self.buckets[a][k]
                    for k in [k for k in self.tbars[a] if k < sec - 7200]:
                        del self.tbars[a][k]
                    for vb in self.bars[a].values():
                        for k in [k for k in vb if k < sec // 60 - 200]:
                            del vb[k]
        cb = self.on_trade
        if cb is not None:
            try:
                cb(venue, asset, usd, side, price)
            except Exception:
                pass

    def dump_tbars(self, path, keep=7200):
        """Save the last `keep` seconds of live-chart bars (atomic), so a restart does not wipe the chart."""
        cut = int(time.time()) - keep
        with self.lock:
            snap = {a: [[s] + list(v) for s, v in self.tbars[a].items() if s >= cut and v[0] is not None] for a in self.tbars}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        os.replace(tmp, path)

    def load_tbars(self, path, keep=7200):
        try:
            with open(path, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except Exception:
            return 0
        cut = int(time.time()) - keep
        n = 0
        with self.lock:
            for a, rows in snap.items():
                if a not in self.tbars:
                    continue
                for r in rows:
                    if r[0] >= cut and r[0] not in self.tbars[a]:
                        self.tbars[a][r[0]] = [r[1], r[2], r[3], r[4], r[5], r[6]]
                        n += 1
        return n

    def tick_price(self, asset, price):
        """The page's blended live price (Coinbase + Kraken), fed in every time it moves: it makes the open / high / low / close of the live trades chart."""
        if asset not in self.tbars or not price:
            return
        sec = int(time.time())
        with self.lock:
            tb = self.tbars[asset].get(sec)
            if tb is None:
                tb = self.tbars[asset][sec] = [None, None, None, None, 0.0, 0.0]
            if tb[0] is None:
                tb[0] = tb[1] = tb[2] = price
            tb[3] = price
            if price > tb[1]:
                tb[1] = price
            if price < tb[2]:
                tb[2] = price

    def tick_bars(self, asset, tf, n, since=None):
        """The live trades chart: [[bar_start, open, high, low, close, buy_usd, sell_usd], ...] oldest first, tf seconds per bar (1, 5 or 15), the bar still forming included. Gaps are flat bars at the last price.
        With since = a bar start, only bars from there on (the page keeps the older ones)."""
        tf = tf if tf in (1, 5, 15) else 5
        now = int(time.time())
        end = now // tf * tf
        start = end - (max(10, min(int(n), 700)) - 1) * tf
        if since is not None:
            start = max(start, int(since) // tf * tf)
        with self.lock:
            items = sorted((s, tuple(v)) for s, v in self.tbars.get(asset, {}).items() if s <= now)
        prev = None
        agg = {}
        for s, v in items:
            k = s // tf * tf
            if v[0] is not None and k < start:
                prev = v[3]
            if k < start:
                continue
            a = agg.get(k)
            if a is None:
                a = agg[k] = [None, None, None, None, 0.0, 0.0]
            if v[0] is not None:
                if a[0] is None:
                    a[0], a[1], a[2] = v[0], v[1], v[2]
                else:
                    a[1] = max(a[1], v[1]); a[2] = min(a[2], v[2])
                a[3] = v[3]
            a[4] += v[4]; a[5] += v[5]
        out = []
        last_real = start
        for k in range(start, end + tf, tf):
            a = agg.get(k)
            if a is not None and a[0] is not None:
                o, h, l, c = a[0], a[1], a[2], a[3]
                bu, se = a[4], a[5]
                last_real = k
            else:
                if prev is None or k - last_real > 60:              # a short quiet spell is a flat bar; a long gap (the bot was off) stays a gap
                    continue
                o = h = l = c = prev
                bu, se = (a[4], a[5]) if a is not None else (0.0, 0.0)
            prev = c
            out.append([k, round(o, 5), round(h, 5), round(l, 5), round(c, 5), int(bu), int(se)])
        return out

    def set_book(self, venue, asset, bid_usd, ask_usd):
        with self.lock:
            self.books[(venue, asset)] = (time.time(), bid_usd, ask_usd)

    def set_levels(self, venue, asset, lv):
        with self.lock:
            now = time.time()
            self.levels[(venue, asset)] = (now, lv)
            if venue not in USD_VENUES:             # learn this venue's offset against the dollar-quoted venues, smoothly (it barely changes)
                mids = sorted(l["mid"] for (v, a), (ts, l) in self.levels.items() if a == asset and v in USD_VENUES and now - ts <= 60)
                if mids:
                    ratio = lv["mid"] / mids[len(mids) // 2] - 1
                    if abs(ratio) < 0.01:           # a 1% 'offset' means a bad snapshot - ignore it
                        old = self.basis.get((venue, asset))
                        self.basis[(venue, asset)] = ratio if old is None else 0.9 * old + 0.1 * ratio

    WALL_ZONE = {"BTC": 10.0, "XRP": 0.0002}        # FIXED zone width (about 1.2 / 1.5 bps) - a grid that never moves with the live price

    def wall_map(self, asset, ref_price, max_age=90):
        """Resting dollars merged into FIXED price zones across every exchange with a recent book. Resting orders do not move when the live price does,
        so nothing here depends on the live price except which side (bid / ask) a zone is on. USDT venues are shifted by their slowly-learned offset.
        -> {"bids": {zone: usd}, "asks": {...}, "by": {(side, zone): {venue: usd}}, "n": venues, "zone": width} or None."""
        now = time.time()
        with self.lock:
            fresh = {v: lv for (v, a), (ts, lv) in self.levels.items() if a == asset and now - ts <= max_age}
            basis = {v: self.basis.get((v, asset), 0.0) for v in fresh}
        if not fresh or not ref_price:
            return None
        zone = self.WALL_ZONE.get(asset, ref_price * 1.5 / 1e4)
        out = {"bids": {}, "asks": {}, "by": {}, "n": len(fresh), "zone": zone}
        for v, lv in fresh.items():
            b = 1.0 + basis[v]
            for side, key in (("bid", "bids"), ("ask", "asks")):
                for p, usd in lv[key]:
                    adj = p / b
                    if (side == "bid" and adj >= ref_price) or (side == "ask" and adj <= ref_price):
                        continue
                    z = round(round(adj / zone) * zone, 8)
                    out[key][z] = out[key].get(z, 0.0) + usd
                    d = out["by"].setdefault((side, z), {})
                    d[v] = d.get(v, 0.0) + usd
        return out

    # -------------------------------------------------------------- output
    def summary(self, asset):
        now = time.time()
        sec_now = int(now)
        with self.lock:
            b = {s: {v: tuple(x) for v, x in vd.items()} for s, vd in self.buckets.get(asset, {}).items()}
            seen = {v: ts for (v, a), ts in self.last_seen.items() if a == asset}
            books = {v: x for (v, a), x in self.books.items() if a == asset}
        per = {}                         # venue -> {win: [buy, sell, n]}
        tot = {w: [0.0, 0.0, 0] for w in WINDOWS}
        for sec, vd in b.items():
            age = sec_now - sec
            for v, (bu, se, n) in vd.items():
                for w in WINDOWS:
                    if age < w:
                        t = tot[w]; t[0] += bu; t[1] += se; t[2] += n
                        p = per.setdefault(v, {}).setdefault(w, [0.0, 0.0, 0]); p[0] += bu; p[1] += se; p[2] += n
        wins = {}
        for w in WINDOWS:
            bu, se, n = tot[w]
            wins[w] = {"buy": bu, "sell": se, "n": n, "pct": (bu / (bu + se)) if (bu + se) > 0 else None}
        venues = []
        for v in set(list(per) + list(books) + list(seen)):
            label, kind = VENUES.get(v, (v, "spot"))
            p60 = per.get(v, {}).get(60, [0.0, 0.0, 0])
            bk = books.get(v)
            book_pct = None
            if bk and now - bk[0] < 60 and (bk[1] + bk[2]) > 0:
                book_pct = bk[1] / (bk[1] + bk[2])
            venues.append({
                "key": v, "label": label, "kind": kind, "buy": p60[0], "sell": p60[1], "n": p60[2],
                "pct": (p60[0] / (p60[0] + p60[1])) if (p60[0] + p60[1]) > 0 else None,
                "live": (now - seen.get(v, 0)) < 120, "book_pct": book_pct,
            })
        venues.sort(key=lambda x: -(x["buy"] + x["sell"]))
        bids = sum(x[1] for x in books.values() if now - x[0] < 60)
        asks = sum(x[2] for x in books.values() if now - x[0] < 60)
        with self.lock:
            deriv = dict(self.deriv.get(asset, {}))
        return {
            "deriv": deriv,
            "wins": wins, "venues": venues,
            "live_n": sum(1 for x in venues if x["live"]),
            "book_pct": (bids / (bids + asks)) if (bids + asks) > 0 else None,
            "book_n": sum(1 for x in books.values() if now - x[0] < 60),
            "book_usd": bids + asks,
        }


    def volume_gauge(self, asset):
        """Live traded volume (dollars, every exchange) over the last 60s split buy / sell, with this market's own 'normal': the median
        and 95th percentile of the rolling 60s totals over the last ~14 minutes. Feeds the vertical volume bar beside the chart."""
        now = int(time.time())
        with self.lock:
            b = {s: [(x[0], x[1]) for x in vd.values()] for s, vd in self.buckets.get(asset, {}).items() if s >= now - 900}
        per = {s: (sum(a for a, _ in v), sum(c for _, c in v)) for s, v in b.items()}

        def win(end, w=60):
            bu = se = 0.0
            for k in range(end - w + 1, end + 1):
                x = per.get(k)
                if x:
                    bu += x[0]; se += x[1]
            return bu, se
        bu, se = win(now)
        first = min(per) if per else now
        # only windows that lie fully inside the data we actually have (right after a start there is little history: no baseline yet)
        samples = sorted(sum(win(e)) for e in range(max(now - 840, first + 59), now - 59, 10))
        med = samples[len(samples) // 2] if len(samples) >= 6 else 0.0
        p95 = samples[int(len(samples) * 0.95)] if len(samples) >= 6 else 0.0
        return {"buy": bu, "sell": se, "total": bu + se, "median": med, "p95": p95, "prev": sum(win(now - 60))}

    INCOMING_FLOOR_USD = {"BTC": 3_000_000, "XRP": 400_000}   # one-sided 10-second push that counts as "huge"
    INCOMING_COOLDOWN = 45

    def incoming(self, asset):
        """A huge one-sided burst of taker flow across ALL exchanges in the last 10 seconds - an early warning that shows up
        before the chart moves. Fires once, then stays quiet for 45s. Needs >= the floor, >= 2.5x this market's own recent
        90th-percentile 10s push, and >= 65% of the 10s volume on one side. Returns an event dict or None."""
        now = int(time.time())
        with self.lock:
            b = {s: {v: tuple(x) for v, x in vd.items()} for s, vd in self.buckets.get(asset, {}).items() if s >= now - 900}
            last = self._last_incoming.get(asset)
        if last and now - last[0] < self.INCOMING_COOLDOWN:
            return None

        def sec(s):
            vd = b.get(s, {})
            return sum(x[0] for x in vd.values()), sum(x[1] for x in vd.values())
        per = {s: sec(s) for s in range(now - 899, now + 1)}

        def win(end):
            bu = sum(per[k][0] for k in range(end - 9, end + 1) if k in per)
            se = sum(per[k][1] for k in range(end - 9, end + 1) if k in per)
            return bu, se
        hist = sorted(abs(win(e)[0] - win(e)[1]) for e in range(now - 840, now - 10, 5))
        p90 = hist[int(len(hist) * 0.9)] if hist else 0.0
        bu, se = win(now)
        net, tot = bu - se, bu + se
        thr = max(self.INCOMING_FLOOR_USD.get(asset, 1e12), 2.5 * p90)
        if tot <= 0 or abs(net) < thr or max(bu, se) / tot < 0.65:
            return None
        side = "buy" if net > 0 else "sell"
        venues = {}
        for s in range(now - 9, now + 1):
            for v, x in b.get(s, {}).items():
                venues[v] = venues.get(v, 0.0) + (x[0] if side == "buy" else x[1])
        top = max(venues, key=venues.get) if venues else ""
        ev = {"side": side, "usd": abs(net), "venues": sum(1 for x in venues.values() if x > 0),
              "top": VENUES.get(top, (top, ""))[0], "ts": time.time(), "price": self.last_price.get(("coinbase", asset))}
        with self.lock:
            self._last_incoming[asset] = (now, side)
        try:
            os.makedirs(os.path.dirname(INCOMING_LOG), exist_ok=True)
            new = not os.path.exists(INCOMING_LOG)
            with open(INCOMING_LOG, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["ts_utc", "asset", "side", "net_usd_10s", "venues", "top_venue", "coinbase_price", "threshold_usd"])
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), asset, side, f"{abs(net):.0f}", ev["venues"], ev["top"],
                            ev["price"], f"{thr:.0f}"])
        except Exception:
            pass
        return ev

    def set_line(self, asset, ticker, strike):
        """Tell the hub which Kalshi line (strike) the current round is about; the above / below tally restarts with each new round."""
        if not ticker or not strike:
            return
        with self.lock:
            cur = self.line.get(asset)
            if not cur or cur["ticker"] != ticker:
                self.line_vol[asset] = {"ticker": ticker, "above": 0.0, "below": 0.0}
            self.line[asset] = {"ticker": ticker, "strike": float(strike)}

    def line_volume(self, asset):
        with self.lock:
            v = self.line_vol.get(asset)
            return dict(v) if v else None

    def window_totals(self, asset, seconds, kind=None):
        """(buy_usd, sell_usd, trades) over the last `seconds` seconds, every exchange (or only 'spot' / only 'perp' venues)."""
        now = int(time.time())
        seconds = max(1, min(int(seconds), 900))
        bu = se = 0.0
        n = 0
        with self.lock:
            for s in range(now - seconds + 1, now + 1):
                vd = self.buckets.get(asset, {}).get(s)
                if vd:
                    for v, x in vd.items():
                        if kind and VENUES.get(v, ("", "spot"))[1] != kind:
                            continue
                        bu += x[0]; se += x[1]; n += x[2]
        return bu, se, n

    def seed_bars(self, asset, venue, candles):
        """candles: {minute_start_ts: (o, h, l, c, usd_volume)} from REST history; live data wins where both exist."""
        with self.lock:
            vb = self.bars[asset].setdefault(venue, {})
            for ts, (o, h, l, cl, v) in candles.items():
                vb.setdefault(int(ts) // 60, [o, cl, v])

    def composite_rsi(self, asset, period=14, want=61):
        """RSI(14) of a basis-neutral composite of every exchange: each minute's composite return is the volume-weighted
        average of each venue's OWN 1-minute return, so a constant USD-vs-USDT price offset cancels out. None until there are
        ~20 completed minutes of history."""
        cur = int(time.time()) // 60
        with self.lock:
            bars = {v: dict(b) for v, b in self.bars.get(asset, {}).items()}
        minutes = sorted({m for b in bars.values() for m in b if m < cur})[-want - 1:]
        closes, level = [], 1.0
        for i in range(1, len(minutes)):
            m, pm = minutes[i], minutes[i - 1]
            num = den = 0.0
            for b in bars.values():
                if m in b and pm in b and b[m][1] > 0 and b[pm][1] > 0:
                    w = max(b[m][2], 1.0)
                    num += w * (b[m][1] / b[pm][1] - 1.0); den += w
            if den > 0 and m - pm <= 3:
                level *= 1.0 + num / den
                closes.append(level)
        if len(closes) <= period + 5:
            return None
        gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, period + 1)]
        losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, period + 1)]
        ag, al = sum(gains) / period, sum(losses) / period
        for i in range(period + 1, len(closes)):
            ch = closes[i] - closes[i - 1]
            ag = (ag * (period - 1) + max(ch, 0.0)) / period
            al = (al * (period - 1) + max(-ch, 0.0)) / period
        return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


HUB = FlowHub()
USD_VENUES = ("coinbase", "kraken", "bitstamp", "gemini", "bitfinex", "cryptocom")   # quoted in real dollars, so 'above / below the line' is fair


def _seed_history():
    """Fill ~90 minutes of 1-minute candles from the exchanges that serve history, so the composite RSI works right away."""
    try:
        from src.account_analysis import venue_vs_brti as vb
    except Exception:
        return
    now = int(time.time())
    for asset in ASSETS:
        for venue, fn in (("coinbase", vb.f_coinbase), ("bitstamp", vb.f_bitstamp), ("binance", vb.f_binance),
                          ("kucoin", vb.f_kucoin), ("okx", vb.f_okx), ("hyperliquid", vb.f_hyperliquid)):
            try:
                HUB.seed_bars(asset, venue, fn(asset, now - 95 * 60, now))
            except Exception:
                pass


# ====================================================================== trade feeds
def _f(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def _base(asset_symbol_map, sym):
    return asset_symbol_map.get(sym)


class _Feed:
    """One reconnecting WebSocket.  parse(msg_obj, ws, state) calls HUB.record(...)."""

    def __init__(self, name, url, subs, parse, ping=None, ping_every=15, url_fn=None, pre_sleep=0.0):
        self.name, self.url, self.subs, self.parse = name, url, subs, parse
        self.ping, self.ping_every, self.url_fn, self.pre_sleep = ping, ping_every, url_fn, pre_sleep
        self.state = {}

    def run_forever(self):
        backoff = 3
        while True:
            try:
                url = self.url_fn() if self.url_fn else self.url
                self.state = {}
                ws = websocket.WebSocketApp(url, on_open=self._open, on_message=self._msg,
                                            on_error=lambda w, e: None)
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(60, backoff * 1.5)

    def _open(self, ws):
        def go():
            try:
                if self.pre_sleep:
                    time.sleep(self.pre_sleep)
                for s in self.subs:
                    ws.send(s if isinstance(s, str) else json.dumps(s))
                while ws.keep_running and self.ping is not None:
                    time.sleep(self.ping_every)
                    ws.send(self.ping if isinstance(self.ping, str) else json.dumps(self.ping))
            except Exception:
                pass
        threading.Thread(target=go, daemon=True).start()

    def _msg(self, ws, m):
        try:
            if isinstance(m, bytes):
                try:
                    m = gzip.decompress(m).decode("utf-8")
                except Exception:
                    m = m.decode("utf-8", "ignore")
            if isinstance(m, str) and m in ("pong", "ping", "Ping", "Pong"):
                if m in ("Ping", "ping") and self.name in ("bingx_perp",):
                    ws.send("Pong")
                return
            self.parse(json.loads(m), ws, self.state)
        except Exception:
            pass


def _bybit(venue):
    sym = {"BTCUSDT": "BTC", "XRPUSDT": "XRP"}

    def parse(d, ws, st):
        if str(d.get("topic", "")).startswith("publicTrade"):
            for t in d.get("data", []):
                a = sym.get(t.get("s"))
                if a:
                    p = _f(t["p"]); HUB.record(venue, a, p * _f(t["v"]), "buy" if t["S"] == "Buy" else "sell", p)
    return parse


def _okx(d, ws, st):
    ct = {"BTC-USDT-SWAP": ("okx_perp", "BTC", 0.01), "XRP-USDT-SWAP": ("okx_perp", "XRP", 100.0),
          "BTC-USDT": ("okx", "BTC", 1.0), "XRP-USDT": ("okx", "XRP", 1.0)}
    if d.get("arg", {}).get("channel") == "trades":
        for t in d.get("data", []):
            m = ct.get(t.get("instId"))
            if m:
                p = _f(t["px"]); HUB.record(m[0], m[1], p * _f(t["sz"]) * m[2], "buy" if t["side"] == "buy" else "sell", p)


def _bitstamp(d, ws, st):
    if d.get("event") == "trade":
        a = {"live_trades_btcusd": "BTC", "live_trades_xrpusd": "XRP"}.get(d.get("channel"))
        t = d.get("data", {})
        if a:
            p = _f(t.get("price")); HUB.record("bitstamp", a, p * _f(t.get("amount")), "buy" if t.get("type") == 0 else "sell", p)


def _gemini(d, ws, st):
    if d.get("type") == "trade":
        a = {"BTCUSD": "BTC", "XRPUSD": "XRP"}.get(d.get("symbol"))
        if a:
            p = _f(d["price"]); HUB.record("gemini", a, p * _f(d["quantity"]), "buy" if d.get("side") == "buy" else "sell", p)


def _bitfinex(d, ws, st):
    if isinstance(d, dict):
        if d.get("event") == "subscribed" and d.get("channel") == "trades":
            st[d["chanId"]] = {"tBTCUSD": "BTC", "tXRPUSD": "XRP"}.get(d.get("symbol"))
        return
    if isinstance(d, list) and len(d) >= 3 and d[1] == "te" and st.get(d[0]):
        amt, p = _f(d[2][2]), _f(d[2][3])
        HUB.record("bitfinex", st[d[0]], abs(amt) * p, "buy" if amt > 0 else "sell", p)


def _cryptocom(d, ws, st):
    if d.get("method") == "public/heartbeat":
        ws.send(json.dumps({"id": d["id"], "method": "public/respond-heartbeat"}))
        return
    r = d.get("result") or {}
    if r.get("channel") == "trade":
        a = {"BTC_USD": "BTC", "XRP_USD": "XRP"}.get(r.get("instrument_name"))
        if a:
            for t in r.get("data", []):
                p = _f(t["p"]); HUB.record("cryptocom", a, p * _f(t["q"]), "buy" if t.get("s") == "BUY" else "sell", p)


def _gate(d, ws, st):
    if d.get("channel") == "spot.trades" and d.get("event") == "update":
        t = d.get("result") or {}
        a = {"BTC_USDT": "BTC", "XRP_USDT": "XRP"}.get(t.get("currency_pair"))
        if a:
            p = _f(t["price"]); HUB.record("gate", a, p * _f(t["amount"]), "buy" if t.get("side") == "buy" else "sell", p)


def _bitget(d, ws, st):
    if d.get("arg", {}).get("channel") == "trade" and d.get("action") != "snapshot":
        a = {"BTCUSDT": "BTC", "XRPUSDT": "XRP"}.get(d["arg"].get("instId"))
        if a:
            for t in d.get("data", []):
                p = _f(t["price"]); HUB.record("bitget", a, p * _f(t["size"]), "buy" if t.get("side") == "buy" else "sell", p)


def _hyperliquid(d, ws, st):
    if d.get("channel") == "trades" and not d.get("data", {}) == {} and isinstance(d.get("data"), list):
        for t in d["data"]:
            a = t.get("coin")
            if a in ASSETS:
                p = _f(t["px"]); HUB.record("hyperliquid", a, p * _f(t["sz"]), "buy" if t.get("side") == "B" else "sell", p)


def _kucoin(d, ws, st):
    if d.get("subject") == "trade.l3match":
        t = d.get("data") or {}
        a = {"BTC-USDT": "BTC", "XRP-USDT": "XRP"}.get(t.get("symbol"))
        if a:
            p = _f(t["price"]); HUB.record("kucoin", a, p * _f(t["size"]), "buy" if t.get("side") == "buy" else "sell", p)


def _deribit(d, ws, st):
    if d.get("method") == "subscription":
        for t in d.get("params", {}).get("data", []):
            if t.get("instrument_name") == "BTC-PERPETUAL":      # amount is in USD for the perpetual
                HUB.record("deribit", "BTC", _f(t["amount"]), "buy" if t.get("direction") == "buy" else "sell", _f(t.get("price")))


def _binance(d, ws, st):
    t = d.get("data", d)
    if t.get("e") == "trade":
        a = {"BTCUSDT": "BTC", "XRPUSDT": "XRP"}.get(t.get("s"))
        if a:
            p = _f(t["p"])      # m = buyer is the maker -> the taker SOLD
            HUB.record("binance", a, p * _f(t["q"]), "sell" if t.get("m") else "buy", p)


# ---------------------------------------------------------------- more venues (2026-10-24): offshore derivatives + Korean spot
def _bitget_perp(d, ws, st):
    if d.get("arg", {}).get("channel") == "trade" and d.get("action") != "snapshot":
        a = {"BTCUSDT": "BTC", "XRPUSDT": "XRP"}.get(d["arg"].get("instId"))
        if a:
            for t in d.get("data", []):
                p = _f(t["price"]); HUB.record("bitget_perp", a, p * _f(t["size"]), "buy" if t.get("side") == "buy" else "sell", p)


_CONTRACT_MULT = {}


def _contract_mult(venue, sym):
    """Contract size (coins per contract) for venues that quote trade size in contracts. Fetched once from the venue's public contract list; None if it cannot be read."""
    k = (venue, sym)
    if k in _CONTRACT_MULT:
        return _CONTRACT_MULT[k]
    v = None
    try:
        if venue == "gate_perp":
            v = _f(_get(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{sym}")["quanto_multiplier"])
        elif venue == "mexc_perp":
            v = _f(_get(f"https://contract.mexc.com/api/v1/contract/detail?symbol={sym}")["data"]["contractSize"])
    except Exception:
        v = None
    if v:
        _CONTRACT_MULT[k] = v
    return v


def _gate_perp(d, ws, st):
    if d.get("channel") == "futures.trades" and d.get("event") == "update":
        for t in d.get("result") or []:
            sym = t.get("contract")
            a = {"BTC_USDT": "BTC", "XRP_USDT": "XRP"}.get(sym)
            m = _contract_mult("gate_perp", sym) if a else None
            if a and m:
                sz = _f(t.get("size")); p = _f(t.get("price"))
                HUB.record("gate_perp", a, abs(sz) * m * p, "buy" if sz > 0 else "sell", p)       # size > 0 = taker BUY: checked on Gate's own trade tape on 2026-10-25 (price rose after positive-size trades 98% of the time, BTC). A wrong flip to the opposite sign was live 2026-10-24 23:16 -> 2026-10-25 02:10.


def _mexc_perp(d, ws, st):
    if d.get("channel") == "push.deal":
        sym = d.get("symbol")
        a = {"BTC_USDT": "BTC", "XRP_USDT": "XRP"}.get(sym)
        m = _contract_mult("mexc_perp", sym) if a else None
        if a and m:
            for t in d.get("data") or []:
                p = _f(t.get("p")); HUB.record("mexc_perp", a, p * _f(t.get("v")) * m, "buy" if t.get("T") == 1 else "sell", p)


def _kraken_perp(d, ws, st):
    if d.get("feed") == "trade":
        a = {"PF_XBTUSD": "BTC", "PF_XRPUSD": "XRP"}.get(d.get("product_id"))
        if a:
            p = _f(d.get("price")); HUB.record("kraken_perp", a, p * _f(d.get("qty")), "buy" if d.get("side") == "buy" else "sell", p)


def _dydx(d, ws, st):
    if d.get("type") == "channel_data" and d.get("channel") == "v4_trades":
        a = {"BTC-USD": "BTC", "XRP-USD": "XRP"}.get(d.get("id"))
        if a:
            for t in (d.get("contents") or {}).get("trades", []):
                p = _f(t.get("price")); HUB.record("dydx", a, p * _f(t.get("size")), "buy" if t.get("side") == "BUY" else "sell", p)


def _bingx_perp(d, ws, st):
    if isinstance(d.get("data"), list) and str(d.get("dataType", "")).endswith("@trade"):
        for t in d["data"]:
            a = {"BTC-USDT": "BTC", "XRP-USDT": "XRP"}.get(t.get("s"))
            if a:
                p = _f(t.get("p")); HUB.record("bingx_perp", a, p * _f(t.get("q")), "sell" if t.get("m") else "buy", p)       # m = buyer is the maker -> the taker SOLD


def _htx(venue, mult_key=None):
    def parse(d, ws, st):
        if "ping" in d:
            ws.send(json.dumps({"pong": d["ping"]}))
            return
        ch = str(d.get("ch", ""))
        if not ch.endswith(".trade.detail"):
            return
        a = {"btcusdt": "BTC", "xrpusdt": "XRP", "BTC-USDT": "BTC", "XRP-USDT": "XRP"}.get(ch.split(".")[1])
        if not a:
            return
        for t in (d.get("tick") or {}).get("data", []):
            p = _f(t.get("price"))
            coins = _f(t.get("quantity")) if "quantity" in t else _f(t.get("amount"))
            HUB.record(venue, a, p * coins, "buy" if t.get("direction") == "buy" else "sell", p)
    return parse


def _won_usd(a, coins):
    """Korean-won venues quote in won: value the coins at the Coinbase dollar price (so the 'Korean premium' does not distort the dollar flow)."""
    p = HUB.last_price.get(("coinbase", a))
    return coins * p if p else None


def _upbit(d, ws, st):
    if d.get("type") == "trade":
        a = {"KRW-BTC": "BTC", "KRW-XRP": "XRP"}.get(d.get("code"))
        usd = _won_usd(a, _f(d.get("trade_volume"))) if a else None
        if usd:
            HUB.record("upbit", a, usd, "buy" if d.get("ask_bid") == "BID" else "sell")


def _bithumb(d, ws, st):
    if d.get("type") == "transaction":
        for t in (d.get("content") or {}).get("list", []):
            a = {"BTC_KRW": "BTC", "XRP_KRW": "XRP"}.get(t.get("symbol"))
            usd = _won_usd(a, _f(t.get("contQty"))) if a else None
            if usd:
                HUB.record("bithumb", a, usd, "buy" if str(t.get("buySellGb")) == "2" else "sell")


def _whitebit(d, ws, st):
    if d.get("method") == "trades_update":
        pair, rows = d["params"][0], d["params"][1]
        a = {"BTC_USDT": "BTC", "XRP_USDT": "XRP"}.get(pair)
        if a:
            now = time.time()
            for t in rows:
                if now - _f(t.get("time")) < 10:                       # the first message replays recent history - skip it
                    p = _f(t.get("price")); HUB.record("whitebit", a, p * _f(t.get("amount")), "buy" if t.get("type") == "buy" else "sell", p)


def _lbank(d, ws, st):
    if d.get("type") == "trade":
        a = {"btc_usdt": "BTC", "xrp_usdt": "XRP"}.get(d.get("pair"))
        t = d.get("trade") or {}
        if a:
            HUB.record("lbank", a, _f(t.get("amount")), "buy" if t.get("direction") == "buy" else "sell", _f(t.get("price")))


def _hashkey(d, ws, st):
    if d.get("topic") == "trade":
        a = {"BTCUSDT": "BTC", "XRPUSDT": "XRP"}.get(d.get("symbol"))
        if a:
            now_ms = time.time() * 1000
            for t in d.get("data", []):
                if now_ms - _f(t.get("t")) < 10000:                    # skip the 24-hour snapshot sent on connect
                    p = _f(t.get("p")); HUB.record("hashkey", a, p * _f(t.get("q")), "sell" if t.get("m") else "buy", p)


def _bitflyer(d, ws, st):
    if d.get("method") == "channelMessage":
        pr = d.get("params") or {}
        if pr.get("channel") == "lightning_executions_BTC_JPY":
            for t in pr.get("message", []):
                usd = _won_usd("BTC", _f(t.get("size")))               # yen venue: value the coins at the Coinbase dollar price
                if usd:
                    HUB.record("bitflyer", "BTC", usd, "buy" if t.get("side") == "BUY" else "sell")


def _binanceus_poll_loop():
    """Binance.US streams are silent from here, so poll its public REST trades (new trades since the last id)."""
    last = {}
    time.sleep(3)
    while True:
        for a in ASSETS:
            try:
                rows = _get(f"https://api.binance.us/api/v3/trades?symbol={a}USDT&limit=200")
                prev = last.get(a)
                for r in rows:
                    if prev is not None and r["id"] > prev:
                        p = _f(r["price"]); HUB.record("binanceus", a, p * _f(r["qty"]), "sell" if r["isBuyerMaker"] else "buy", p)
                if rows:
                    last[a] = max(r["id"] for r in rows)
            except Exception:
                pass
        time.sleep(3.5)


def _kucoin_url():
    req = urllib.request.Request("https://api.kucoin.com/api/v1/bullet-public", data=b"", method="POST")
    j = json.loads(urllib.request.urlopen(req, timeout=10).read())
    s = j["data"]["instanceServers"][0]
    return f'{s["endpoint"]}?token={j["data"]["token"]}'


def _feeds():
    return [
        # Binance's official public market-data-only endpoint (the main api/stream hosts refuse US addresses)
        _Feed("binance", "wss://data-stream.binance.vision/stream?streams=btcusdt@trade/xrpusdt@trade", [], _binance),
        _Feed("bybit", "wss://stream.bybit.com/v5/public/spot",
              [{"op": "subscribe", "args": ["publicTrade.BTCUSDT", "publicTrade.XRPUSDT"]}], _bybit("bybit"), ping={"op": "ping"}),
        _Feed("bybit_perp", "wss://stream.bybit.com/v5/public/linear",
              [{"op": "subscribe", "args": ["publicTrade.BTCUSDT", "publicTrade.XRPUSDT"]}], _bybit("bybit_perp"), ping={"op": "ping"}),
        _Feed("okx", "wss://ws.okx.com:8443/ws/v5/public",
              [{"op": "subscribe", "args": [{"channel": "trades", "instId": i} for i in ("BTC-USDT", "XRP-USDT", "BTC-USDT-SWAP", "XRP-USDT-SWAP")]}],
              _okx, ping="ping", ping_every=20),
        _Feed("bitstamp", "wss://ws.bitstamp.net",
              [{"event": "bts:subscribe", "data": {"channel": c}} for c in ("live_trades_btcusd", "live_trades_xrpusd")], _bitstamp),
        _Feed("gemini", "wss://api.gemini.com/v2/marketdata",
              [{"type": "subscribe", "subscriptions": [{"name": "l2", "symbols": ["BTCUSD", "XRPUSD"]}]}], _gemini),
        _Feed("bitfinex", "wss://api-pub.bitfinex.com/ws/2",
              [{"event": "subscribe", "channel": "trades", "symbol": s} for s in ("tBTCUSD", "tXRPUSD")], _bitfinex),
        _Feed("cryptocom", "wss://stream.crypto.com/exchange/v1/market",
              [{"id": 1, "method": "subscribe", "params": {"channels": ["trade.BTC_USD", "trade.XRP_USD"]}}], _cryptocom, pre_sleep=1.0),
        _Feed("gate", "wss://api.gateio.ws/ws/v4/",
              [{"time": int(time.time()), "channel": "spot.trades", "event": "subscribe", "payload": ["BTC_USDT", "XRP_USDT"]}],
              _gate, ping={"channel": "spot.ping"}),
        _Feed("bitget", "wss://ws.bitget.com/v2/ws/public",
              [{"op": "subscribe", "args": [{"instType": "SPOT", "channel": "trade", "instId": i} for i in ("BTCUSDT", "XRPUSDT")]}],
              _bitget, ping="ping", ping_every=25),
        _Feed("hyperliquid", "wss://api.hyperliquid.xyz/ws",
              [{"method": "subscribe", "subscription": {"type": "trades", "coin": c}} for c in ("BTC", "XRP")],
              _hyperliquid, ping={"method": "ping"}, ping_every=30),
        _Feed("kucoin", None,
              [{"id": 1, "type": "subscribe", "topic": "/market/match:BTC-USDT,XRP-USDT", "response": True}],
              _kucoin, ping={"id": 2, "type": "ping"}, ping_every=15, url_fn=_kucoin_url),
        _Feed("deribit", "wss://www.deribit.com/ws/api/v2",
              [{"jsonrpc": "2.0", "id": 1, "method": "public/subscribe", "params": {"channels": ["trades.BTC-PERPETUAL.100ms"]}}], _deribit),
        _Feed("bitget_perp", "wss://ws.bitget.com/v2/ws/public",
              [{"op": "subscribe", "args": [{"instType": "USDT-FUTURES", "channel": "trade", "instId": i} for i in ("BTCUSDT", "XRPUSDT")]}],
              _bitget_perp, ping="ping", ping_every=25),
        _Feed("gate_perp", "wss://fx-ws.gateio.ws/v4/ws/usdt",
              [{"time": int(time.time()), "channel": "futures.trades", "event": "subscribe", "payload": ["BTC_USDT", "XRP_USDT"]}],
              _gate_perp, ping={"channel": "futures.ping"}),
        _Feed("mexc_perp", "wss://contract.mexc.com/edge",
              [{"method": "sub.deal", "param": {"symbol": s}} for s in ("BTC_USDT", "XRP_USDT")], _mexc_perp, ping={"method": "ping"}, ping_every=15),
        _Feed("kraken_perp", "wss://futures.kraken.com/ws/v1",
              [{"event": "subscribe", "feed": "trade", "product_ids": ["PF_XBTUSD", "PF_XRPUSD"]}], _kraken_perp),
        _Feed("dydx", "wss://indexer.dydx.trade/v4/ws",
              [{"type": "subscribe", "channel": "v4_trades", "id": i} for i in ("BTC-USD", "XRP-USD")], _dydx),
        _Feed("bingx_perp", "wss://open-api-swap.bingx.com/swap-market",
              [{"id": f"s{i}", "reqType": "sub", "dataType": f"{s}@trade"} for i, s in enumerate(("BTC-USDT", "XRP-USDT"))], _bingx_perp),
        _Feed("htx", "wss://api.huobi.pro/ws",
              [{"sub": "market.btcusdt.trade.detail", "id": "a"}, {"sub": "market.xrpusdt.trade.detail", "id": "b"}], _htx("htx")),
        _Feed("htx_perp", "wss://api.hbdm.com/linear-swap-ws",
              [{"sub": "market.BTC-USDT.trade.detail", "id": "a"}, {"sub": "market.XRP-USDT.trade.detail", "id": "b"}], _htx("htx_perp")),
        _Feed("upbit", "wss://api.upbit.com/websocket/v1",
              [[{"ticket": "dash"}, {"type": "trade", "codes": ["KRW-BTC", "KRW-XRP"]}]], _upbit),
        _Feed("bithumb", "wss://pubwss.bithumb.com/pub/ws",
              [{"type": "transaction", "symbols": ["BTC_KRW", "XRP_KRW"]}], _bithumb),
        _Feed("whitebit", "wss://api.whitebit.com/ws", [{"id": 1, "method": "trades_subscribe", "params": ["BTC_USDT", "XRP_USDT"]}], _whitebit,
              ping={"id": 2, "method": "ping", "params": []}, ping_every=30),
        _Feed("lbank", "wss://www.lbkex.net/ws/V2/",
              [{"action": "subscribe", "subscribe": "trade", "pair": p_} for p_ in ("btc_usdt", "xrp_usdt")], _lbank),
        _Feed("hashkey", "wss://stream-glb.hashkey.com/quote/ws/v1", [{"symbol": "BTCUSDT,XRPUSDT", "topic": "trade", "event": "sub"}], _hashkey),
        _Feed("bitflyer", "wss://ws.lightstream.bitflyer.com/json-rpc",
              [{"method": "subscribe", "params": {"channel": "lightning_executions_BTC_JPY"}, "id": 1}], _bitflyer),
    ]


# ====================================================================== perp-market extras (funding, open interest, liquidations)
_OKX_CT = {"BTC": 0.01, "XRP": 100.0}


def _deriv_loop():
    """Every 30s: perp funding rate, total open interest (and its 5-minute change), perp premium to spot, and recent forced
    liquidations - from Hyperliquid + OKX (+ Deribit for BTC).  Positioning context that raw trades do not show."""
    time.sleep(5)
    while True:
        try:
            hl = _get("https://api.hyperliquid.xyz/info", json.dumps({"type": "metaAndAssetCtxs"}).encode(),
                      {"Content-Type": "application/json"})
            names = [u["name"] for u in hl[0]["universe"]]
        except Exception:
            hl, names = None, []
        for a in ASSETS:
            d = {}
            oi_total = 0.0
            try:
                if a in names:
                    x = hl[1][names.index(a)]
                    mark = _f(x["markPx"])
                    d["funding_hl"] = _f(x["funding"])            # per hour
                    d["premium_pct"] = _f(x["premium"]) * 100
                    oi_total += _f(x["openInterest"]) * mark
            except Exception:
                pass
            try:
                inst = f"{a}-USDT-SWAP"
                d["funding_okx"] = _f(_get(f"https://www.okx.com/api/v5/public/funding-rate?instId={inst}")["data"][0]["fundingRate"])   # per 8h
                oi_total += _f(_get(f"https://www.okx.com/api/v5/public/open-interest?instId={inst}")["data"][0]["oiUsd"])
            except Exception:
                pass
            if a == "BTC":
                try:
                    r = _get("https://www.deribit.com/api/v2/public/ticker?instrument_name=BTC-PERPETUAL")["result"]
                    oi_total += _f(r.get("open_interest"))        # already USD
                except Exception:
                    pass
            try:
                liq = _get(f"https://www.okx.com/api/v5/public/liquidation-orders?instType=SWAP&instFamily={a}-USDT&state=filled")["data"]
                now_ms = time.time() * 1000
                longs = shorts = 0.0
                for blk in liq:
                    for t in blk.get("details", []):
                        if now_ms - _f(t.get("ts")) <= 300000:
                            usd = _f(t["sz"]) * _OKX_CT[a] * _f(t["bkPx"])
                            if t.get("posSide") == "long":
                                longs += usd        # longs force-sold = selling pressure
                            else:
                                shorts += usd       # shorts force-bought = buying pressure
                d["liq_long_usd_5m"], d["liq_short_usd_5m"] = longs, shorts
            except Exception:
                pass
            now = time.time()
            if oi_total > 0:
                h = HUB.oi_hist[a]
                h.append((now, oi_total))
                while h and now - h[0][0] > 1800:
                    h.pop(0)
                old = [v for t, v in h if now - t >= 270]
                d["oi_usd"] = oi_total
                d["oi_chg_5m_pct"] = ((oi_total - old[-1]) / old[-1] * 100) if old else None
            with HUB.lock:
                HUB.deriv[a].update(d)
        time.sleep(30)


# ====================================================================== order-book depth (REST, polled)
_tls = threading.local()


def _get(url, data=None, headers=None):
    """JSON GET (or POST when data is given) over a KEEP-ALIVE session, one per thread. A fresh urllib request per call
    re-did the TLS handshake and re-loaded the CA bundle every time (found 2026-10-10: ~5 requests/s from these pollers
    starved the dashboard's own refresh loop); a reused connection costs almost nothing."""
    import requests
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "kalshi-dashboard"})
        _tls.s = s
    try:
        if data is not None:
            r = s.post(url, data=data, headers=headers or {}, timeout=8)
        else:
            r = s.get(url, headers=headers or {}, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        _tls.s = None          # rebuild the session next time
        raise


_tl = threading.local()          # the price levels of the book this thread just parsed (handed to HUB.set_levels by _book_loop)
WALL_LEVEL_RANGE = 0.005         # keep levels within 0.5% of that venue's mid


def _book_usd(bids, asks, mult=1.0, usd_qty=False):
    """bids / asks: [(price, qty)].  Resting dollars within the band both sides actually reach (max 0.25% from mid)."""
    _tl.levels = None
    bids = [(float(p), float(q) * mult) for p, q in bids if float(q) > 0]
    asks = [(float(p), float(q) * mult) for p, q in asks if float(q) > 0]
    if not bids or not asks:
        return None
    bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
    mid = (bb + ba) / 2
    if mid <= 0 or ba < bb:
        return None
    reach = min((mid - min(p for p, _ in bids)) / mid, (max(p for p, _ in asks) - mid) / mid, 0.0025)
    if reach < 0.00015:     # book too shallow to say anything (1.5 bps: drops KuCoin's 20-level book, keeps dense BTC books)
        return None
    lo, hi = mid * (1 - reach), mid * (1 + reach)
    wl, wh = mid * (1 - WALL_LEVEL_RANGE), mid * (1 + WALL_LEVEL_RANGE)
    _tl.levels = {"mid": mid,
                  "bids": [(p, q if usd_qty else p * q) for p, q in bids if p >= wl][:800],
                  "asks": [(p, q if usd_qty else p * q) for p, q in asks if p <= wh][:800]}
    if usd_qty:
        return (sum(q for p, q in bids if p >= lo), sum(q for p, q in asks if p <= hi))
    return (sum(p * q for p, q in bids if p >= lo), sum(p * q for p, q in asks if p <= hi))


def _pairs(rows, i=0, j=1):
    return [(r[i], r[j]) for r in rows]


def _book_coinbase(a):
    j = _get(f"https://api.exchange.coinbase.com/products/{a}-USD/book?level=2")
    return _book_usd(_pairs(j["bids"]), _pairs(j["asks"]))


def _book_binance(a):
    j = _get(f"https://data-api.binance.vision/api/v3/depth?symbol={a}USDT&limit=500")
    return _book_usd(_pairs(j["bids"]), _pairs(j["asks"]))


def _book_binanceus(a):
    j = _get(f"https://api.binance.us/api/v3/depth?symbol={a}USDT&limit=500")
    return _book_usd(_pairs(j["bids"]), _pairs(j["asks"]))


def _book_kraken(a):
    j = _get(f"https://api.kraken.com/0/public/Depth?pair={'XBT' if a == 'BTC' else 'XRP'}USD&count=100")
    r = next(iter(j["result"].values()))
    return _book_usd(_pairs(r["bids"]), _pairs(r["asks"]))


def _book_bitstamp(a):
    j = _get(f"https://www.bitstamp.net/api/v2/order_book/{a.lower()}usd/")
    return _book_usd(_pairs(j["bids"][:200]), _pairs(j["asks"][:200]))


def _book_gemini(a):
    j = _get(f"https://api.gemini.com/v1/book/{a.lower()}usd?limit_bids=100&limit_asks=100")
    return _book_usd([(x["price"], x["amount"]) for x in j["bids"]], [(x["price"], x["amount"]) for x in j["asks"]])


def _book_okx(a):
    d = _get(f"https://www.okx.com/api/v5/market/books?instId={a}-USDT&sz=400")["data"][0]
    return _book_usd(_pairs(d["bids"]), _pairs(d["asks"]))


def _book_okx_perp(a):
    d = _get(f"https://www.okx.com/api/v5/market/books?instId={a}-USDT-SWAP&sz=400")["data"][0]
    return _book_usd(_pairs(d["bids"]), _pairs(d["asks"]), mult=0.01 if a == "BTC" else 100.0)


def _book_bitfinex(a):
    j = _get(f"https://api-pub.bitfinex.com/v2/book/t{a}USD/P0?len=100")
    return _book_usd([(r[0], r[2]) for r in j if r[2] > 0], [(r[0], -r[2]) for r in j if r[2] < 0])


def _book_gate(a):
    j = _get(f"https://api.gateio.ws/api/v4/spot/order_book?currency_pair={a}_USDT&limit=100")
    return _book_usd(_pairs(j["bids"]), _pairs(j["asks"]))


def _book_bitget(a):
    d = _get(f"https://api.bitget.com/api/v2/spot/market/orderbook?symbol={a}USDT&limit=100")["data"]
    return _book_usd(_pairs(d["bids"]), _pairs(d["asks"]))


def _book_kucoin(a):
    d = _get(f"https://api.kucoin.com/api/v1/market/orderbook/level2_20?symbol={a}-USDT")["data"]
    return _book_usd(_pairs(d["bids"]), _pairs(d["asks"]))


def _book_cryptocom(a):
    d = _get(f"https://api.crypto.com/exchange/v1/public/get-book?instrument_name={a}_USD&depth=50")["result"]["data"][0]
    return _book_usd(_pairs(d["bids"]), _pairs(d["asks"]))


def _book_hyperliquid(a):
    j = _get("https://api.hyperliquid.xyz/info", json.dumps({"type": "l2Book", "coin": a}).encode(), {"Content-Type": "application/json"})
    return _book_usd([(x["px"], x["sz"]) for x in j["levels"][0]], [(x["px"], x["sz"]) for x in j["levels"][1]])


def _book_deribit(a):
    if a != "BTC":
        return None
    r = _get("https://www.deribit.com/api/v2/public/get_order_book?instrument_name=BTC-PERPETUAL&depth=50")["result"]
    return _book_usd(_pairs(r["bids"]), _pairs(r["asks"]), usd_qty=True)


BOOKS = {"binance": _book_binance, "binanceus": _book_binanceus, "coinbase": _book_coinbase, "kraken": _book_kraken, "bitstamp": _book_bitstamp, "gemini": _book_gemini,
         "okx": _book_okx, "okx_perp": _book_okx_perp, "bitfinex": _book_bitfinex, "gate": _book_gate,
         "bitget": _book_bitget, "kucoin": _book_kucoin, "cryptocom": _book_cryptocom,
         "hyperliquid": _book_hyperliquid, "deribit": _book_deribit}


def _book_loop(venue, fn):
    time.sleep(2 + (hash(venue) % 5))
    while True:
        for a in ASSETS:
            try:
                _tl.levels = None
                r = fn(a)
                if r:
                    HUB.set_book(venue, a, r[0], r[1])
                    if getattr(_tl, "levels", None):
                        HUB.set_levels(venue, a, _tl.levels)
            except Exception:
                pass
            time.sleep(0.4)
        time.sleep(8)


# ====================================================================== log + start
def _log_loop():
    """Append-only record of the pressure every 5s (kept forever - lets us test later whether it ever predicted anything)."""
    time.sleep(20)
    try:
        os.makedirs(os.path.dirname(PRESSURE_LOG), exist_ok=True)
        new = not os.path.exists(PRESSURE_LOG)
        f = open(PRESSURE_LOG, "a", newline="", encoding="utf-8")
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "asset", "buy_pct_10s", "buy_pct_30s", "buy_pct_60s", "buy_pct_300s", "usd_60s", "venues_live", "book_buy_pct", "book_venues"])
    except Exception:
        return
    while True:
        try:
            for a in ASSETS:
                s = HUB.summary(a)
                wn = s["wins"]
                fmt = lambda x: "" if x is None else f"{x:.4f}"
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), a] + [fmt(wn[k]["pct"]) for k in WINDOWS]
                           + [f'{wn[60]["buy"] + wn[60]["sell"]:.0f}', s["live_n"], fmt(s["book_pct"]), s["book_n"]])
            f.flush()
        except Exception:
            pass
        time.sleep(5)


_started = False


def start():
    """Start every feed in its own daemon thread.  Safe to call twice."""
    global _started
    if _started or websocket is None:
        return
    _started = True
    for feed in _feeds():
        threading.Thread(target=feed.run_forever, daemon=True, name=f"flow-{feed.name}").start()
    for v, fn in BOOKS.items():
        threading.Thread(target=_book_loop, args=(v, fn), daemon=True, name=f"book-{v}").start()
    threading.Thread(target=_binanceus_poll_loop, daemon=True, name="flow-binanceus").start()
    threading.Thread(target=_deriv_loop, daemon=True, name="flow-deriv").start()
    threading.Thread(target=_seed_history, daemon=True, name="flow-seed").start()
    if not os.environ.get("FLOW_NO_LOG"):      # tests set this so they never write into the real log
        threading.Thread(target=_log_loop, daemon=True, name="pressure-log").start()
