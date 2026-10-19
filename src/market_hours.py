"""When the world's markets open and close (regular hours; holidays are built in for US stocks only), plus this Kalshi round. Information only."""
import datetime as dt
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc

# NYSE / Nasdaq full-day closures and early closes (13:00 ET). Other exchanges' holidays are NOT included.
NYSE_HOLIDAYS = {dt.date(*d) for d in [
    (2026, 1, 1), (2026, 1, 19), (2026, 2, 16), (2026, 4, 3), (2026, 5, 25), (2026, 6, 19), (2026, 7, 3), (2026, 9, 7), (2026, 11, 26), (2026, 12, 25),
    (2027, 1, 1), (2027, 1, 18), (2027, 2, 15), (2027, 3, 26), (2027, 5, 31), (2027, 6, 18), (2027, 7, 5), (2027, 9, 6), (2027, 11, 25), (2027, 12, 24)]}
NYSE_EARLY = {dt.date(2026, 11, 27): "13:00", dt.date(2026, 12, 24): "13:00", dt.date(2027, 11, 26): "13:00"}

WEEK = (0, 1, 2, 3, 4)
# (group, name, time zone, [(weekdays, open, close, days-after-open the close falls)], holiday calendar, note)
MARKETS = [
    ("Crypto", "Bitcoin / XRP spot trading (Coinbase, Kraken ...)", None, None, None, "open 24 hours a day, 7 days a week"),
    ("US stocks", "New York pre-market", "America/New_York", [(WEEK, "04:00", "09:30", 0)], "NYSE", "early trading - thin volume"),
    ("US stocks", "New York stock market (NYSE / Nasdaq)", "America/New_York", [(WEEK, "09:30", "16:00", 0)], "NYSE", "the big one - crypto often moves around the open and the close"),
    ("US stocks", "New York after-hours", "America/New_York", [(WEEK, "16:00", "20:00", 0)], "NYSE", "late trading - thin volume"),
    ("Futures", "CME futures (Bitcoin, stock indexes)", "America/New_York", [((6, 0, 1, 2, 3), "18:00", "17:00", 1)], None, "closes 5-6 PM ET each day and all weekend (the weekend 'gap' on the CME Bitcoin chart)"),
    ("Europe", "London stock exchange", "Europe/London", [(WEEK, "08:00", "16:30", 0)], None, ""),
    ("Europe", "Frankfurt (Xetra)", "Europe/Berlin", [(WEEK, "09:00", "17:30", 0)], None, ""),
    ("Asia", "Tokyo stock exchange", "Asia/Tokyo", [(WEEK, "09:00", "11:30", 0), (WEEK, "12:30", "15:30", 0)], None, "lunch break 11:30 - 12:30"),
    ("Asia", "Hong Kong stock exchange", "Asia/Hong_Kong", [(WEEK, "09:30", "12:00", 0), (WEEK, "13:00", "16:00", 0)], None, "lunch break 12:00 - 1:00 PM"),
    ("Asia", "Shanghai stock exchange", "Asia/Shanghai", [(WEEK, "09:30", "11:30", 0), (WEEK, "13:00", "15:00", 0)], None, "lunch break 11:30 - 1:00 PM"),
    ("Australia", "Sydney (ASX)", "Australia/Sydney", [(WEEK, "10:00", "16:00", 0)], None, ""),
]


def _sessions(zone, sched, cal, now):
    """[(start_ts, end_ts, early_close)] for the days around now, in order."""
    z = ZoneInfo(zone)
    today = dt.datetime.fromtimestamp(now, z).date()
    out = []
    for k in range(-3, 10):
        d = today + dt.timedelta(days=k)
        if cal == "NYSE" and d in NYSE_HOLIDAYS:
            continue
        for wds, o, c, off in sched:
            if d.weekday() not in wds:
                continue
            if cal == "NYSE" and c == "16:00" and d in NYSE_EARLY:
                c = NYSE_EARLY[d]
            oh, om = map(int, o.split(":"))
            ch, cm = map(int, c.split(":"))
            s = dt.datetime(d.year, d.month, d.day, oh, om, tzinfo=z)
            e = dt.datetime(d.year, d.month, d.day, ch, cm, tzinfo=z) + dt.timedelta(days=off)
            out.append((s.timestamp(), e.timestamp()))
    return sorted(out)


def market_rows(now):
    """One dict per market: group, name, hours (the market's own clock), state OPEN / CLOSED / BREAK, next change time + what it is."""
    rows = []
    for group, name, zone, sched, cal, note in MARKETS:
        if sched is None:
            rows.append({"group": group, "name": name, "hours": "24 / 7", "state": "OPEN", "next_ts": None, "next_what": "", "note": note})
            continue
        ss = _sessions(zone, sched, cal, now)
        cur = next(((s, e) for s, e in ss if s <= now < e), None)
        nxt = next((s for s, e in ss if s > now), None)
        prev_end = max([e for s, e in ss if e <= now], default=None)
        z = ZoneInfo(zone)
        o, c = sched[0][1], sched[-1][2]
        d0 = dt.datetime.fromtimestamp(now, z)
        hours = f"{_h12(o)} - {_h12(c)} {d0.tzname()}"
        if cur:
            rows.append({"group": group, "name": name, "hours": hours, "state": "OPEN", "next_ts": cur[1], "next_what": "closes", "note": note})
        else:
            brk = prev_end is not None and nxt is not None and now - prev_end <= 2 * 3600 and nxt - now <= 2 * 3600
            rows.append({"group": group, "name": name, "hours": hours, "state": "BREAK" if brk else "CLOSED", "next_ts": nxt, "next_what": "opens", "note": note})
    return rows


def _h12(hhmm):
    h, m = map(int, hhmm.split(":"))
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def round_info(now):
    """The Kalshi 15-minute round now: start, end, the 8th-minute lock, and the next round."""
    start = int(now // 900) * 900
    return {"start": start, "end": start + 900, "lock": start + 480, "next_end": start + 1800}


def next_funding(now):
    """Next 8-hour perpetual funding time (00:00 / 08:00 / 16:00 UTC - Binance, Bybit, OKX and most others)."""
    d = dt.datetime.fromtimestamp(now, UTC)
    base = d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    for k in range(0, 5):
        t = base + k * 8 * 3600
        if t > now:
            return t
    return base + 86400


# ---- "about this open": a short plain-English write-up per market. Numbers come from data_cache/market_open_stats.json (a backtest on ~60 days of 1-minute BTC / XRP candles).
# name prefix -> (stats key, [more stats keys], what it is, why crypto cares, what to watch)
INFO = {
    "Bitcoin / XRP spot": (None, [], "Bitcoin and XRP trade on exchanges all over the world, with no opening bell and no closing bell.",
                           "So there is no 'open' to wait for. The busiest hours are when the big markets are awake: the US session (roughly 8:30 AM - 4 PM ET) and the London / New York overlap.",
                           "Weekends and the middle of the night (US time) are usually quieter, so a move can be less reliable. Kalshi's 15-minute markets run all the time."),
    "New York pre-market": ("nyse_pre", [], "Early electronic trading in US stocks before the regular session (4:00 - 9:30 AM ET).",
                            "Volume is thin, but the first reactions to overnight news and the 8:30 AM ET economic reports (CPI, jobs and so on) show up here, and crypto often reacts to the same news.",
                            "Thin trading can make sharp, quick moves that do not always last into the 9:30 AM open."),
    "New York stock market": ("nyse_open", [], "The opening bell of the US stock market (NYSE and Nasdaq) at 9:30 AM ET.",
                              "It is the biggest daily rush of money in US markets. Bitcoin trades alongside stocks more than it used to, and the US spot Bitcoin funds (ETFs) only trade during these hours, so their buying and selling can push crypto too.",
                              "The first 30 minutes are usually the wildest part of the US day. The first move is often partly undone, so a lot of traders wait a few minutes before trusting it."),
    "New York after-hours": ("nyse_close", [], "Trading after the 4:00 PM ET close, until 8:00 PM ET. Most company earnings reports come out here.",
                             "News released after the close can move stock futures and other risk assets overnight, and crypto sometimes follows.",
                             "Volume is thin, so moves can be jumpy. The stats below are for the 30 minutes BEFORE the 4 PM close (the closing rush), not the after-hours session itself."),
    "CME futures": ("cme_daily", ["cme_sunday"], "The Chicago Mercantile Exchange trades Bitcoin and stock-index futures from Sunday 6 PM ET to Friday 5 PM ET, pausing 5 - 6 PM ET every day.",
                    "Big funds and institutions use these. Because the CME closes on weekends, its Bitcoin chart shows a 'gap' between Friday's close and Sunday's open, and traders often watch whether price comes back to fill it.",
                    "The Sunday 6 PM ET reopening is a thin-liquidity moment that can move price (only a small number of Sundays are in the sample, so treat that line gently)."),
    "London stock exchange": ("london", [], "The start (8:00 AM London time) of the European trading day.",
                              "It brings European banks and funds online, and it starts the London / New York overlap, which builds into the busiest hours of the day.",
                              "The open itself has often been ordinary for crypto; the busy part tends to come later as New York wakes up."),
    "Frankfurt": ("frankfurt", [], "The start (9:00 AM German time) of trading on Germany's main exchange, at the same moment as London while both are on summer time.",
                  "A second wave of European money at the start of the European day.",
                  "Looks almost identical to the London open in the numbers, since the two open at the same time for most of the year."),
    "Tokyo": ("tokyo", [], "The start (9:00 AM Japan time) of the Japanese stock market, one of Asia's biggest.",
              "Starts the Asian session. Japanese and Korean traders are a real share of crypto trading, and Asia often sets the tone before Europe wakes up.",
              "Moves in the first minutes have tended to carry on more than at most opens."),
    "Hong Kong": ("hongkong", [], "The start (9:30 AM Hong Kong time) of trading in Hong Kong.",
                  "Chinese-linked money and Asian crypto desks are active; it opens at the same moment as Shanghai.",
                  "Early direction has been less reliable here than at most opens."),
    "Shanghai": ("shanghai", [], "The start (9:30 AM China time) of mainland Chinese stock trading, at the same moment as Hong Kong.",
                 "Chinese markets move global risk appetite, even though crypto trading is restricted on the mainland.",
                 "Looks the same as Hong Kong in the numbers because they open together."),
    "Sydney": ("sydney", [], "The start (10:00 AM Sydney time) of the Australian stock market.",
               "The first Western Pacific market to open each day; a small player for crypto.",
               "Usually a quiet open for crypto."),
}


def info_for(name):
    for k, v in INFO.items():
        if name.startswith(k):
            return v
    return None


def stats_line(asset, key, st, before_close=False):
    """One sentence of numbers for a coin at an event, from the backtest file `st` (None if there is no data)."""
    try:
        s = st[asset][key]
        b = st[asset]["_baseline"]
    except (KeyError, TypeError):
        return ""
    when = "in the 30 minutes before the close" if before_close else "in the 30 minutes after the open"
    return (f"<b>{asset}</b> {when}: moved {s['range_pct']:.2f}% from high to low on average - <b>{s['range_x']:.1f}x</b> a normal 30 minutes ({b['range_pct']:.2f}%) - with "
            f"{s['vol_x']:.1f}x the usual volume. It finished the 30 minutes higher {s['up_pct']}% of the time. The direction of the first 5 minutes was still the direction at 30 minutes "
            f"{s['cont_pct']}% of the time, and price came back to the opening price at least once {s['retest_pct']}% of the time ({s['n']} times in the last {b['days']} days).")
