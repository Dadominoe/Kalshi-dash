"""A real visual window: a local web page, auto-refreshing, showing BTC/ETH
spot price, Kalshi's target price for the active market, the up/down lean,
and the predicted price range. Open http://localhost:8765 in your browser
after running this. Read-only - never places any order, pure display.

The live price is volume-weighted across Coinbase + Kraken (Binance.com blocks
US connections - tested, got a hard 451 - so Kraken is the second venue
instead) - each exchange's latest real trade (price and size) is blended, so
a single thin print on one exchange can't move the number by itself. The
"Live price" row shows which venues are actually contributing right now.

Honest note on "My call" and "Predicted @ close": these are NOT a second,
independent source of information from Kalshi's own price - nothing tested
so far beats what Kalshi's price already reflects. "My call" is the same
validated bar the live trading bot itself requires before it would act
(confidence floor + strict-direction confirmation + no active sweep) -
reframed as a plain yes/no/no-call instead of a raw percentage. When it
doesn't clear that bar, it honestly says "NO CALL" rather than guessing.
"Predicted @ close" is current price, offset by the real empirical median
move ONLY in the direction of that same validated call - so if there's no
confident call, it's honestly just the current price.

Bias correction ("learning from mistakes"): once we have graded locked calls
(see _history below), we track the average signed error between what we
predicted and what actually happened, and nudge future predictions by a
DAMPENED (30%) and CAPPED fraction of that - so a real, persistent bias gets
gently corrected, but a handful of noisy misses can't yank the number around.
This does NOT touch "My call"'s yes/no logic - that's the same validated bar
the live trading bot itself uses, not something to retune off a few dozen
samples. Only the price number adapts, and the adjustment is always shown
plainly, never hidden.
"""
import csv
import datetime as dt
import http.server
import json
import math
import os
import re
import socketserver
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import websocket

from src.kalshi_live_bot import (
    get_active_market, get_kalshi_price, get_spot_closes, spot_return_from_closes,
    had_recent_sweep, ASSETS, get_market_snapshot,
    KalshiClient, get_yes_bid_ask, marketable_price, escalated_buffer,
    get_live_yes_bid_ask,
)
from src.kalshi_price_projection import project_range, project_point_for_minutes, EMPIRICAL_MOVES, HORIZONS_MINUTES

# narrowed to BTC + XRP per user request 2026-08-15 - this only filters what
# the DASHBOARD displays/polls, doesn't touch ASSETS itself (shared with the
# real live trading bot's config, never mutated here). ETH's history stays
# saved on disk, just not shown/updated while this filter is active.
# XRP_REAL_ASSET (added 2026-08-31, see below) is a DASHBOARD-ONLY config -
# appended here, never merged into the shared ASSETS list, so it can't touch
# what the live trading bot actually trades.
DASHBOARD_ASSETS = [a for a in ASSETS if a["name"] == "BTC"]

PORT = int(os.environ.get("PORT", 8765))  # Railway/most hosts assign this dynamically
_state = {}
_lock = threading.Lock()
_last_sweep_state = {}  # asset name -> bool, so we only flag the MOMENT it flips (not every poll it stays true)
_last_boom_vs_call = {}  # asset name -> "against"/"with"/None, same "flag only the MOMENT it starts" idea -
                          # this had never had a sound at all (visual pulse only), a real gap a user caught
                          # (2026-08-31: "I have yet to hear the reversal thing go off")
_last_certain_flip = {}  # asset name -> bool, same edge-trigger idea, for the big full-screen
                          # "FLIP - SELL - FOR SURE" alert added 2026-09-24 per user request
CERTAIN_FLIP_MIN_CHANCE = 0.55  # the "for sure" bar - the BOOM swing-against-call signal (fast,
                                  # real price move) must ALSO be backed by Kalshi's OWN price now
                                  # actually favoring the other side by this much, not just a fast
                                  # wiggle that could still resolve the original way
# turned back ON 2026-09-25 - was off pending a real backtest (the user
# worried XRP often swings into the "flip" zone and reverts before close),
# but a real loss surfaced a genuine BUG in the underlying math instead of
# just an unvalidated threshold: cur_flip_chance used 1-confidence, which
# measures "chance the CURRENT leaning flips again," not "chance the LOCKED
# call is wrong" - those look the same right up until price actually
# crosses past the locked call, then the old formula inverts and shrinks
# toward 0 exactly as the real risk climbs toward 1. That's why it stayed
# silent through a real collapse (kalshi_price 0.009 against a locked YES -
# ~99% real risk, read as 0.9%). Fixed via flip_chance_vs_locked_call()
# above. The historical intrawindow_price_log.csv "certain_flip" column
# predates this fix and isn't trustworthy for backtesting - going forward
# it will be.
CERTAIN_FLIP_ALERT_ENABLED = True

INTRAWINDOW_LOG = os.path.join(os.path.dirname(__file__), "..", "bot_state", "intrawindow_price_log.csv")
INTRAWINDOW_FIELDS = ["ts", "asset", "ticker", "seconds_left", "kalshi_price", "spot_price", "target",
                       "leaning", "confidence", "locked_call", "dramatic_swing", "dramatic_vs_call",
                       "certain_flip", "yes_bid", "yes_ask", "quote_source", "fair_yes",
                       "fair_sigma_bps_min"]
# rows before 2026-10-03 have no yes_bid/yes_ask/fair_yes and their
# kalshi_price came from the ~30s-stale list endpoint - they're rotated to
# this file the first time the new header is written
INTRAWINDOW_LOG_V1 = os.path.join(os.path.dirname(__file__), "..", "bot_state", "intrawindow_price_log_v1_stale_quotes.csv")


def _log_intrawindow_price(row):
    """Per-poll snapshot of Kalshi's price DURING each window (not just at
    lock and final settle, all that was ever saved before) - added
    2026-09-24 per user request, the only way to actually backtest the "for
    sure" flip alert instead of guessing whether it's accurate. Only
    poll_loop's single thread ever writes this (same assumption
    _live_append_log already relies on), so no lock needed."""
    try:
        d = os.path.dirname(INTRAWINDOW_LOG)
        os.makedirs(d, exist_ok=True)
        if os.path.exists(INTRAWINDOW_LOG) and not os.path.exists(INTRAWINDOW_LOG_V1):
            with open(INTRAWINDOW_LOG, encoding="utf-8") as f:
                header = f.readline().strip().split(",")
            if header != INTRAWINDOW_FIELDS:
                os.replace(INTRAWINDOW_LOG, INTRAWINDOW_LOG_V1)
        is_new = not os.path.exists(INTRAWINDOW_LOG)
        with open(INTRAWINDOW_LOG, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=INTRAWINDOW_FIELDS)
            if is_new:
                w.writeheader()
            w.writerow(row)
    except OSError:
        pass  # logging is a nice-to-have - never let it crash the poll loop

# patience during a downtrend - added 2026-08-12 per real losses (3 of 7
# wrong calls were sharp bounces during a confirmed BEAR regime faking out a
# single-poll confirmation). During BEAR specifically, require the SAME
# directional confirmation to hold across several consecutive polls before
# trusting it, instead of firing the instant the threshold is crossed once.
# BULL/NEUTRAL keep the original single-read behavior - that's not where the
# losses showed up.
_confirm_streak = {}  # asset name -> {"leaning": "YES"/"NO"/None, "streak": int}
BEAR_CONFIRM_STREAK_REQUIRED = 3  # ~6+ seconds of sustained confirmation at a 2s poll interval

# added 2026-08-13 per real $100-bet backtest: BTC's first 22 real-priced
# calls went 0-for-6 specifically in the 65-75% confidence band, losses
# spread across the whole time range (not one bad stretch) while 55-65% and
# 75%+ both won money over the same period. Read: this band is "moved enough
# to trigger a call, not enough to be a real breakout" - a plausible
# overextension/reversal zone. Small sample (n=6), so extra patience here,
# not an outright block - same caution level as the regime veto.
MID_CONFIDENCE_BAND = (0.65, 0.75)
MID_CONFIDENCE_STREAK_REQUIRED = 3

# added 2026-09-02 after a real losing streak (4 losses in a row live) during
# a choppy, range-bound stretch - backtested against ~4000 real settled
# KXBTC15M markets using the DASHBOARD's actual mechanic (lock-and-hold to
# settlement, no defensive exit - a separate test on the live trading bot's
# defensive-exit mechanic found no edge here, because that system already
# cuts a bad chop-driven entry early; the dashboard has no such safety net).
# Vetoes a call when BTC has been bouncing around a lot in the last few
# minutes (regardless of net direction) - validated: 79.7% baseline holdout
# accuracy -> 85.8% with this veto, TRAIN and HOLDOUT nearly identical
# (85.75% / 85.85%) so this isn't overfit noise. Costs coverage (~91% of
# windows called -> ~50%), a real tradeoff: fewer calls, much more reliable
# ones. Dashboard-local (not touching the shared ASSETS the live bot reads).
# GOLD added 2026-09-10: gold was running ~52% live (vs 71-82% backtested) -
# the gap was choppy 15-min action the same as BTC's. Backtested on 703 real
# settled KXGOLD15M markets (kalshi_real_gold_backtest.py + a threshold
# sweep): a 0.0007 choppiness veto gets 86.0% train / 86.1% holdout,
# near-identical so not overfit, at ~40-50% coverage. It IS effectively a
# trend filter - only lets a call through when gold is moving cleanly, not
# whipsawing.
CHOPPINESS_LOOKBACK_MINUTES = 4
CHOPPINESS_VETO_THRESHOLD = {"BTC": 0.0008, "GOLD": 0.0007}

# added 2026-09-12 per user request + backtested data: a call that locks in
# the very first minute of a BTC window is meaningfully worse than one that
# forms later (63.85% vs 87.48% holdout accuracy on real settled markets,
# src/kalshi_first_minute_backtest.py) - there's almost no real momentum/
# regime signal built up yet at that point, just noise. Block locking during
# that window entirely for BTC; "My call" honestly says NO CALL until 60s
# have passed, same as it already does for any other unmet condition.
# per-market confirm % - the price a call must reach before it locks, overriding
# the shared 58% entry_confidence_floor. BTC raised to 66% 2026-10-03 per user
# request after src/per_asset_floor_backtest.py: BTC calls 75% -> 80% right and
# ROI on <=82c buys -3.5%/+4.2% -> +3.5%/+4.9% (both date halves improved).
# XRP left at 58% - no floor helped XRP in both halves.
CALL_CONFIDENCE_FLOOR_OVERRIDE = {"BTC": 0.66}
MIN_LOCK_SECONDS = {"BTC": 60, "XRP": 60}  # XRP added 2026-09-12: same backtest showed a
                                             # smaller but real gap (70.58% first-min vs 81.29% later)


def get_choppiness(closes, lookback_minutes):
    """Sum of absolute 1-minute returns over the lookback window - high means
    price has been bouncing around a lot (regardless of net direction), low
    means it's been calm/trending smoothly. Distinct from momentum_confirms
    (which only looks at NET direction) and from regime (a 4hr net-return
    read) - this is about how MUCH back-and-forth has happened recently."""
    if closes is None or len(closes) < lookback_minutes + 1:
        return None
    recent = closes[-(lookback_minutes + 1):]
    total = 0.0
    for i in range(1, len(recent)):
        if recent[i - 1] != 0:
            total += abs(recent[i] / recent[i - 1] - 1)
    return total

# added 2026-08-13 per user feedback + a live example: the regime veto was
# blocking a 73.5%-confidence Kalshi-backed YES call solely because the
# slower 4hr regime still read BEAR. A 15-minute window can develop its own
# real trend independent of the 4hr backdrop - a lagging, coarse filter
# shouldn't override a call the market itself is already this sure about.
# The veto still applies at normal confidence (where the original 3-loss
# evidence lives); it steps aside once confidence clears this bar.
REGIME_VETO_OVERRIDE_CONFIDENCE = 0.70

# added 2026-08-13 per direct user request: run TWO independent locked-call
# systems side by side on BTC/ETH (the two assets with a real Kalshi price to
# compare against) so we can see, on screen, which actually performs better -
# PRICE (existing system: direction from Kalshi's own price, gated by a
# confidence floor) vs INFO (direction from OUR OWN momentum reading, gated
# by how many multiples of the minimum threshold it cleared - no Kalshi price
# involved in the decision at all). Both grade against the exact same real
# Kalshi settlement target, so it's a fair, apples-to-apples comparison.
INFO_MOMENTUM_MULTIPLIER = 1.5  # require our own momentum to clear 1.5x the bare minimum threshold

# checkpoint snapshots - added 2026-08-31 per user request: capture the
# leaning at fixed elapsed-time marks (not just the first-real-call lock),
# so a flip mid-window is visible instead of only seeing the very first read
# and the live one. Also the basis for the "chance of a flip" meter - for
# BTC/ETH that's literally Kalshi's own price (1 - confidence = the market's
# own implied probability the OTHER side wins instead), a real calibrated
# number, not a guess. For synthetic assets it's the same self-computed
# proxy already used elsewhere, same caveat applies.
CHECKPOINT_MINUTES = [5, 10]

# added 2026-08-31 per user request: a THIRD, clearly-labeled callbox (not a
# buried text row - that's what caused the earlier confusion) that locks the
# first real call once only ~4.5 minutes are LEFT in the window (as opposed
# to "Locked first call", which locks the first real call from window OPEN).
# Includes the confidence and flip-chance percentages AT the moment it locks.
LATE_CALL_SECONDS = 4.5 * 60

# added 2026-08-13 per real data: since the regime veto already blocks nearly
# all against-trend calls, "BEAR performance" is really just "how well do NO
# calls do during BEAR" - and there's a sharp cliff by confidence: below 65%,
# 4/12 correct (33%, worse than a coin flip); at or above 65%, 4/5 (80%). The
# blanket 0.58 floor was letting through a bunch of weak BEAR calls that lose
# more than they win. Small sample (n=17 total), so revisit as more data
# comes in - but a clean cliff like this is worth acting on now.
BEAR_MIN_CONFIDENCE = 0.65


def get_confirm_streak_ok(asset_name, leaning, direction_confirms, regime, confidence=None):
    state = _confirm_streak.get(asset_name, {"leaning": None, "streak": 0})
    if leaning is not None and direction_confirms:
        if state["leaning"] == leaning:
            state = {"leaning": leaning, "streak": state["streak"] + 1}
        else:
            state = {"leaning": leaning, "streak": 1}
    else:
        state = {"leaning": None, "streak": 0}
    _confirm_streak[asset_name] = state
    # fail CAUTIOUS, not open: if the regime read is unavailable (None - a
    # transient data-fetch hiccup on the 240min lookback), that used to mean
    # "regime == 'BEAR'" was False and the veto silently did nothing, letting
    # a call slip through with zero regime protection at exactly the moment
    # we have the LEAST information. Treat unknown the same as BEAR's extra
    # scrutiny - require the same patience, not none.
    required = BEAR_CONFIRM_STREAK_REQUIRED if regime in ("BEAR", None) else 1
    if confidence is not None and MID_CONFIDENCE_BAND[0] <= confidence < MID_CONFIDENCE_BAND[1]:
        required = max(required, MID_CONFIDENCE_STREAK_REQUIRED)
    return state["streak"] >= required


def momentum_confirms(closes, leaning, margin):
    """Was direction_confirms's original bug: it only checked the single
    most-recent 1-minute tick, so price could sit quietly well above/below
    target for a long stretch (already effectively decided) without ever
    firing, just because that one narrow window happened to be flat. Now
    checks the 1-minute AND 2-minute return - either one clearing the margin
    counts, so a real sustained move isn't missed just because the latest
    single tick was quiet."""
    if closes is None or len(closes) < 2 or leaning is None:
        return False
    checks = [closes[-1] / closes[-2] - 1]
    if len(closes) >= 3:
        checks.append(closes[-1] / closes[-3] - 1)
    if leaning == "YES":
        return any(r >= margin for r in checks)
    return any(r <= -margin for r in checks)


RSI_PERIOD = 14

# ---- reuse HTTPS connections for read-only GETs (2026-10-10 audit): requests.get() builds a brand-new connection pool and
# SSL context per call, and loading the CA bundle each time was ~40% of the refresh loop's time (about 25 REST calls per
# pass) - the clock/price only updated every ~5s. A per-thread keep-alive Session gives the same answers much faster.
# GET only: order placement (POST) is deliberately left exactly as it was - never retry an order on a stale connection.
import requests as _rq
_get_tls = threading.local()
_orig_requests_get = _rq.get


def _fast_get(url, params=None, **kwargs):
    s = getattr(_get_tls, "s", None)
    if s is None:
        s = _rq.Session()
        ad = _rq.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
        s.mount("https://", ad)
        s.mount("http://", ad)
        _get_tls.s = s
    try:
        return s.get(url, params=params, **kwargs)
    except _rq.exceptions.ConnectionError:
        _get_tls.s = None                                     # stale keep-alive socket: one clean retry (a GET is safe to repeat)
        return _orig_requests_get(url, params=params, **kwargs)


_rq.get = _fast_get

# tested 2026-08-15: classic RSI (oversold<30 -> expect bounce up, overbought
# >70 -> expect drop) is backwards in these 15-min windows - 5-12% accuracy
# across every config, both train/holdout. Matches an earlier mean-reversion
# test that failed the same way: this market trends through extremes rather
# than reverting. So RSI is shown here as a WATCH indicator only - never
# wired into "My call" - labeled honestly so it's not read as validated.
def rsi_zone_cls(v):
    """colour by how much pressure: red = stretched (30 or below / 70 or above), orange = building (30-40 / 60-70), green = normal."""
    return "rsi-r" if (v <= 30 or v >= 70) else ("rsi-o" if (v <= 40 or v >= 60) else "rsi-g")


def rsi_pressure_note(v):
    if v >= 70:
        return "OVERBOUGHT - stretched up"
    if v <= 30:
        return "OVERSOLD - stretched down"
    if v >= 60:
        return "pressure building up"
    if v <= 40:
        return "pressure building down"
    return "normal"


def rsi_tradingview(asset_name, period=14):
    """TradingView's RSI(14): Wilder smoothing on the 1-minute closes of Coinbase (the same candles their chart shows). The older closes come from the downloaded candles;
    the newest minutes come from the live Coinbase trades (the download can lag a minute or two), so the last close is the latest trade and it ticks like their chart."""
    try:
        if _mtf is None:
            return None
        sym = {a["name"]: a["symbol"] for a in DASHBOARD_ASSETS}.get(asset_name)
        one = (_mtf.raw.get(sym) or {}).get("1m") if sym else None
        if not one or len(one) < period + 60:
            return None
        base = {int(x[0]): float(x[4]) for x in one}
        with _flow.HUB.lock:
            live = {int(mn) * 60: float(b[1]) for mn, b in (_flow.HUB.bars.get(asset_name, {}).get("coinbase") or {}).items()}
        base.update(live)                                  # live trade bars win where both exist
        last_min = int(time.time() // 60 * 60)
        keys = sorted(k for k in base if k <= last_min)
        if len(keys) < period + 60:
            return None
        closes, prev = [], None
        for t in range(keys[0], keys[-1] + 60, 60):        # a minute with no trade is a flat candle at the previous close
            prev = base.get(t, prev)
            closes.append(prev)
        return compute_rsi(closes, period)
    except Exception:
        return None


def compute_rsi(closes, period=RSI_PERIOD):
    """Standard Wilder's RSI, returns just the latest value (or None if not
    enough data)."""
    if closes is None or len(closes) <= period:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


CANDLE_SECONDS = 30
CANDLE_CHART_LOOKBACK_SECONDS = 60 * 60  # send up to 1hr of candles - JS picks how much to actually show


def bucket_ticks_to_candles(ticks, lookback_seconds=CANDLE_CHART_LOOKBACK_SECONDS):
    """Real 30-second OHLC candles from the live tick stream - not a REST
    API call, since 30s isn't a standard kline granularity anyone offers;
    genuinely computed from every trade tick already flowing in for the
    blended price."""
    if not ticks or len(ticks) < 2:
        return []
    now = ticks[-1][0]
    cutoff = now - lookback_seconds
    windowed = [(t, p) for t, p in ticks if t >= cutoff]
    if len(windowed) < 2:
        return []
    buckets = {}
    for ts, price in windowed:
        b = int(ts // CANDLE_SECONDS) * CANDLE_SECONDS
        if b not in buckets:
            buckets[b] = [price, price, price, price]
        else:
            bucket = buckets[b]
            bucket[1] = max(bucket[1], price)
            bucket[2] = min(bucket[2], price)
            bucket[3] = price
    return [[k] + [round(v, 6) for v in buckets[k]] for k in sorted(buckets)]


def render_candle_chart_svg(candles, target, current_price, typical_move, seconds_left,
                             whale_trades, lookback_seconds=None, width=780, height=160):
    """Real 30s OHLC candles + a translucent 'flashlight' cone projecting
    toward the window's close (empirical typical-move envelope) + a row of
    aggr.trade-style whale trade tick-marks underneath (sized by $ notional,
    green=buy/red=sell) - so incoming size shows up as it happens, not just
    as a one-off alert once it crosses the threshold. Server-rendered (not
    client-side JS) since that's the version already proven reliable - the
    client-side rewrite broke and was reverted 2026-09-13."""
    price_area_h = height - 22  # leaves room for the whale marker strip at the bottom
    if not candles or len(candles) < 2:
        return ""

    proj_hi = current_price + typical_move if (current_price is not None and typical_move) else None
    proj_lo = current_price - typical_move if (current_price is not None and typical_move) else None

    lows = [c[3] for c in candles]
    highs = [c[2] for c in candles]
    bounds = lows + highs
    if target is not None:
        bounds.append(target)
    if proj_hi is not None:
        bounds += [proj_hi, proj_lo]
    lo, hi = min(bounds), max(bounds)
    span = (hi - lo) or (hi * 0.001) or 1
    pad = span * 0.1
    lo -= pad
    hi += pad
    span = hi - lo

    def y(price):
        return price_area_h - ((price - lo) / span) * price_area_h

    # t_start anchored to the FULL requested lookback window (not just the
    # earliest candle actually observed) - so when there's genuinely sparse
    # tick history (e.g. right after a restart), the real candles cluster
    # correctly near their actual position on the timeline instead of being
    # stretched to artificially fill the whole chart width.
    t_end = candles[-1][0]
    t_start = (t_end - lookback_seconds) if lookback_seconds else candles[0][0]
    t_span = (t_end - t_start) or 1
    candle_area_w = width * 0.82

    def x_of(ts):
        return ((ts - t_start) / t_span) * candle_area_w

    n = len(candles)
    # capped so sparse data (e.g. right after a restart, before an hour of
    # tick history has actually accumulated) renders as a few honest thin
    # candles instead of being stretched into giant blown-up blocks filling
    # the whole width - found 2026-09-14 via a real user report
    candle_w = min(10.0, max(1.5, candle_area_w / n))

    bars = []
    for i, (ts, o, h, l, c) in enumerate(candles):
        cx = x_of(ts) + candle_w / 2
        color = "#3fb950" if c >= o else "#f85149"
        body_top, body_bot = y(max(o, c)), y(min(o, c))
        body_h = max(1.0, abs(body_bot - body_top))
        bars.append(f'<line x1="{cx:.1f}" y1="{y(h):.1f}" x2="{cx:.1f}" y2="{y(l):.1f}" '
                    f'stroke="{color}" stroke-width="1"/>')
        bars.append(f'<rect x="{cx - candle_w*0.35:.1f}" y="{min(body_top, body_bot):.1f}" '
                    f'width="{candle_w*0.7:.1f}" height="{body_h:.1f}" fill="{color}"/>')

    cone_svg = ""
    if current_price is not None and proj_hi is not None and seconds_left and seconds_left > 0:
        last_x, last_y = candle_area_w, y(current_price)
        tip_x = width
        hi_y, lo_y = y(proj_hi), y(proj_lo)
        cone_svg = (
            f'<polygon points="{last_x:.1f},{last_y:.1f} {tip_x:.1f},{hi_y:.1f} {tip_x:.1f},{lo_y:.1f}" '
            f'fill="#58a6ff" opacity="0.14"/>'
            f'<line x1="{last_x:.1f}" y1="{last_y:.1f}" x2="{tip_x:.1f}" y2="{hi_y:.1f}" '
            f'stroke="#58a6ff" stroke-width="1" stroke-dasharray="3,2" opacity="0.55"/>'
            f'<line x1="{last_x:.1f}" y1="{last_y:.1f}" x2="{tip_x:.1f}" y2="{lo_y:.1f}" '
            f'stroke="#58a6ff" stroke-width="1" stroke-dasharray="3,2" opacity="0.55"/>'
        )

    target_svg = ""
    if target is not None:
        ty = y(target)
        target_svg = (f'<line x1="0" y1="{ty:.1f}" x2="{width}" y2="{ty:.1f}" '
                      f'stroke="#d29922" stroke-width="1" stroke-dasharray="4,3" opacity="0.6"/>')

    # whale tick-marker strip - aggr.trade-style: one small vertical mark per
    # whale trade, positioned at its real timestamp, height/opacity scaled
    # by how big it was relative to the biggest one shown, green=buy/red=sell
    whale_svg = ""
    in_range = [w for w in (whale_trades or []) if t_start <= w[0] <= t_end]
    if in_range:
        max_usd = max(w[1] for w in in_range) or 1
        marks = []
        strip_top = price_area_h + 4
        strip_h = height - strip_top - 2
        for ts, usd, side in in_range:
            wx = x_of(ts)
            frac = min(1.0, usd / max_usd)
            mh = max(2, strip_h * (0.25 + 0.75 * frac))
            color = "#3fb950" if side == "buy" else "#f85149"
            opacity = 0.4 + 0.6 * frac
            marks.append(f'<line x1="{wx:.1f}" y1="{strip_top + strip_h:.1f}" '
                        f'x2="{wx:.1f}" y2="{strip_top + strip_h - mh:.1f}" '
                        f'stroke="{color}" stroke-width="2" opacity="{opacity:.2f}"/>')
        whale_svg = "".join(marks)

    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'preserveAspectRatio="none" style="display:block;margin-top:4px;">'
            f'{target_svg}{"".join(bars)}{cone_svg}{whale_svg}</svg>')


def build_chart_svg(closes, width=780, height=60):
    """A small inline SVG polyline 'moving graph' of recent price - the line
    itself is colored by whether it's net up or down over the window shown."""
    if not closes or len(closes) < 2:
        return ""
    lo, hi = min(closes), max(closes)
    span = (hi - lo) or (hi * 0.001 or 1)
    n = len(closes)
    points = []
    for i, c in enumerate(closes):
        x = (i / (n - 1)) * width
        y = height - ((c - lo) / span) * height
        points.append(f"{x:.1f},{y:.1f}")
    color = "#3fb950" if closes[-1] >= closes[0] else "#f85149"
    poly = " ".join(points)
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'preserveAspectRatio="none" style="display:block;margin-top:6px;">'
            f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.5"/></svg>')


def build_flame_svg(intensity):
    """Real fire, not a vector cartoon outline - a user rejected the drawn-
    path version twice as looking "cheap"/emoji-like. This uses the same
    technique realistic CSS fire effects actually use: several overlapping
    BLURRED, glowing blobs (radial gradients + blur + screen blend), layered
    small-to-big with a bright white-hot core, each flickering at a slightly
    different speed so the shape never looks static or vector-crisp - much
    closer to how real flame/video footage actually reads (soft, glowing,
    irregular) than a clean outlined icon. intensity 1-3 = how many outer
    glow layers (bigger win streak = bigger, wider fire)."""
    intensity = max(1, min(3, intensity))
    # (css class, width, height, color-stop-near, color-stop-far) - listed
    # widest/dimmest FIRST (drawn in back) down to the innermost/hottest
    # (drawn last, on top). intensity 1 = just the hot inner body; higher
    # intensity adds wider glow layers behind it.
    all_layers = [
        ("flame-glow-3", 46, 58, "#ff8f00", "#3d0000"),  # widest, coolest outer glow
        ("flame-glow-2", 34, 48, "#ff5000", "#6b0000"),
        ("flame-glow-1", 24, 38, "#ff9100", "#ff3d00"),  # innermost body, hottest orange
    ]
    layers_cfg = all_layers[3 - intensity:]
    blobs = "".join(
        f'<div class="flame-blob {css}" style="width:{w}px;height:{h}px;'
        f'background:radial-gradient(circle at 50% 75%, {near} 0%, {far} 75%, transparent 100%);"></div>'
        for css, w, h, near, far in layers_cfg
    )
    return (f'<div class="flame-fx">{blobs}'
            f'<div class="flame-core"></div></div>')


def build_flip_gauge_svg(pct):
    """A PSI-style semicircle dial for the flip-chance % - color zones
    (green/yellow/orange/red) matching the 30/40/45 alert tiers, with a
    needle pointing at the current value. Scaled 0-60% for resolution since
    flip-chance rarely goes higher than that in practice."""
    if pct is None:
        return ""
    cx, cy, r = 90, 90, 70
    max_scale = 60.0
    clamped = max(0.0, min(pct, max_scale))

    def angle_xy(angle_deg, radius):
        rad = math.radians(angle_deg)
        return cx + radius * math.cos(rad), cy - radius * math.sin(rad)

    zones = [(0, 30, "#3fb950"), (30, 40, "#d29922"), (40, 45, "#f0883e"), (45, 60, "#f85149")]
    arcs = []
    for zstart, zend, color in zones:
        a1 = 180 - (zstart / max_scale) * 180
        a2 = 180 - (zend / max_scale) * 180
        x1, y1 = angle_xy(a1, r)
        x2, y2 = angle_xy(a2, r)
        arcs.append(f'<path d="M {x1:.1f} {y1:.1f} A {r} {r} 0 0 1 {x2:.1f} {y2:.1f}" '
                    f'stroke="{color}" stroke-width="14" fill="none" stroke-linecap="butt"/>')

    needle_angle = 180 - (clamped / max_scale) * 180
    nx, ny = angle_xy(needle_angle, r - 12)
    needle = (f'<line x1="{cx}" y1="{cy}" x2="{nx:.1f}" y2="{ny:.1f}" '
             f'stroke="#e6edf3" stroke-width="3" stroke-linecap="round"/>'
             f'<circle cx="{cx}" cy="{cy}" r="5" fill="#e6edf3"/>')
    label = f'<text x="{cx}" y="{cy + 26}" text-anchor="middle" fill="#e6edf3" font-size="16" font-weight="700">{pct:.1f}%</text>'
    return (f'<svg viewBox="0 0 180 126" width="140" height="98" style="display:block;">'
            f'{"".join(arcs)}{needle}{label}</svg>')


# Synthetic assets (no Kalshi market) - full parity with BTC/ETH's card, but
# built on a SYNTHETIC window instead of a real contract: same 15-min cadence
# as BTC's actual Kalshi market (reused for timing sync only), with the
# asset's own price at window-open as the settlement reference (mirrors how
# BTC/ETH's real markets settle relative to their own open). Thresholds are
# BTC's, scaled by each asset's OWN measured volatility (real 1-min move data
# pulled per-asset, not guessed) so "confidence floor" etc. mean something
# comparable, not just BTC's numbers pasted onto a noisier coin.
NEAR_VOL_SCALE = 2.57   # measured 2026-08-12: NEAR's avg 1min move / BTC's
BNB_VOL_SCALE = 1.19    # measured 2026-08-12: BNB's avg 1min move / BTC's
XRP_VOL_SCALE = 1.28    # measured 2026-08-13: XRP's avg 1min move / BTC's

NEAR_PSEUDO_ASSET = {
    "name": "NEAR",
    "symbol": "NEAR-USD",
    "entry_confidence_floor": 0.58,
    "strict_direction_margin": 0.0002 * NEAR_VOL_SCALE,
    "sweep_lookback_minutes": 10,
    "sweep_recent_minutes": 2,
    "sweep_margin": 0.0003 * NEAR_VOL_SCALE,
}
BNB_PSEUDO_ASSET = {
    "name": "BNB",
    "symbol": "BNB-USD",
    "entry_confidence_floor": 0.58,
    "strict_direction_margin": 0.0002 * BNB_VOL_SCALE,
    "sweep_lookback_minutes": 10,
    "sweep_recent_minutes": 2,
    "sweep_margin": 0.0003 * BNB_VOL_SCALE,
}
XRP_PSEUDO_ASSET = {
    "name": "XRP",
    "symbol": "XRP-USD",
    "entry_confidence_floor": 0.58,
    "strict_direction_margin": 0.0002 * XRP_VOL_SCALE,
    "sweep_lookback_minutes": 10,
    "sweep_recent_minutes": 2,
    "sweep_margin": 0.0003 * XRP_VOL_SCALE,
}
# found 2026-08-31: Kalshi actually runs a REAL, live, tradeable XRP 15-min
# market (KXXRP15M) - real floor_strike, real yes_bid/yes_ask, same settlement
# rule as BTC. XRP had been running as a synthetic/self-computed asset this
# whole time even though a real market exists. Backtested on ~4000 real
# settled KXXRP15M markets (src/kalshi_real_xrp_backtest.py): the real
# market's own price + the SAME entry filters already validated for BTC gets
# 76.8% holdout accuracy, vs. 65.7% for the synthetic self-computed approach.
# This is dashboard-DISPLAY-ONLY - deliberately NOT added to ASSETS in
# kalshi_live_bot.py (the shared config the real live trading bot reads), so
# this can't affect what the live bot actually trades.
XRP_REAL_ASSET = {
    "name": "XRP",
    "series": "KXXRP15M",
    "symbol": "XRP-USD",
    # XRP trades around $1-2, so 2-decimal rounding (fine for BTC/ETH's
    # thousands-dollar prices) would badly truncate precision here - typical
    # moves are fractions of a cent.
    "price_decimals": 4,
    "entry_confidence_floor": XRP_PSEUDO_ASSET["entry_confidence_floor"],
    "strict_direction_margin": XRP_PSEUDO_ASSET["strict_direction_margin"],
    "sweep_lookback_minutes": XRP_PSEUDO_ASSET["sweep_lookback_minutes"],
    "sweep_recent_minutes": XRP_PSEUDO_ASSET["sweep_recent_minutes"],
    "sweep_margin": XRP_PSEUDO_ASSET["sweep_margin"],
}

# added 2026-09-01 per user request. Real, live KXGOLD15M market confirmed -
# same settlement style as BTC (relative to its own open, via Pyth's gold
# feed). No spot price feed exists for gold itself among this project's
# existing exchange connections (Coinbase/Kraken - crypto only), so this
# first ran on PAXG-USD (a gold-backed Coinbase token) as a proxy. Replaced
# 2026-09-02 per user request ("gold actually has a chart... instead of
# using a coin") with gold's own real price - Yahoo Finance's GC=F (COMEX
# gold futures), fetched separately below (see get_gold_futures_closes) since
# it's not on Coinbase/Kraken and can't reuse the shared get_spot_closes/
# get_market_regime helpers those exchanges use. "GOLD-FUT" is a made-up
# symbol string (not a real exchange pair) used only as an EMPIRICAL_MOVES/
# cache key for this feed.
# UPDATE 2026-09-02: turns out KXGOLD15M actually has ~28 days of settled-
# market history (2247 markets), not ~12 hours - the earlier read was wrong,
# an artifact of a too-small API page size. Backtested for real
# (src/kalshi_real_gold_backtest.py) against 673 of those markets that
# overlap with real gold spot data (Yahoo's free API only gives ~8 days of
# 1-min history, capping how many settled markets can be validated): same
# live-style filters as BTC/XRP get 80.97% train / 81.98% holdout accuracy
# (n=538/135) - consistent, no overfitting sign. Genuinely validated now,
# just on a smaller sample than BTC (~4000 windows) or XRP (~4000 windows) -
# still marked experimental for that reason (fewer market conditions
# covered), not because it's unproven.
GOLD_VOL_SCALE = 0.972  # measured 2026-09-02 from real GC=F futures data: gold's avg 1min % move / BTC's
GOLD_REAL_ASSET = {
    "name": "GOLD",
    "series": "KXGOLD15M",
    "symbol": "GOLD-FUT",
    "experimental": True,  # not backtested - see note above. Shown as a visible warning on the card.
    "entry_confidence_floor": 0.58,
    "strict_direction_margin": 0.0002 * GOLD_VOL_SCALE,
    "sweep_lookback_minutes": 10,
    "sweep_recent_minutes": 2,
    "sweep_margin": 0.0003 * GOLD_VOL_SCALE,
}

SYNTHETIC_ASSETS = []  # XRP moved to the real-market path above 2026-08-31 (see
                          # XRP_REAL_ASSET). NEAR/BNB history stays saved on disk,
                          # just not shown/updated

# GOLD_REAL_ASSET removed from the display 2026-09-13 per user request
# ("take gold down, delete it, it's trash") - config/functions stay defined
# in case it's ever wanted back, just not polled or shown.

DASHBOARD_ASSETS.append(XRP_REAL_ASSET)  # runs XRP through the exact same real-
                                           # market code path as BTC (poll_loop's
                                           # `for asset in DASHBOARD_ASSETS` loop)

_ASSET_SERIES_BY_NAME = {a["name"]: a["series"] for a in DASHBOARD_ASSETS}  # e.g. "XRP" -> "KXXRP15M" -
                                                                              # lets render_html look up a
                                                                              # series prefix by name alone

_synthetic_window_ref = {}  # asset name -> {"ticker": <synced BTC ticker>, "open_price": <price at that window's start>}

# the FIRST call/predicted-price we make for each market ticker, frozen there
# for the rest of that market's life (not re-guessed every poll) - so you can
# see how the very first read holds up against how it actually plays out.
_locked = {}   # asset name -> {"ticker", "my_call", "predicted_price", "target", "locked_seconds_left"}

# --- real-money auto-buy on XRP's "Locked First Call" (added 2026-09-14 per
# user request, with an on-site arm/disarm switch) -------------------------
# OFF by default and ALWAYS starts OFF on every process restart, on purpose -
# this is real money and this dashboard restarts itself all the time
# (keepalive, crashes) - "armed" must never silently survive a restart the
# user didn't personally see happen, so this is never written to disk.
# Fires exactly ONE $100-notional buy-and-hold order per XRP window, at the
# exact moment "Locked first call" becomes real (the same event already on
# this page, the same one the "$100 per locked call" row measures) - never a
# second entry on the same window, never an exit before settlement.
LIVE_ASSETS = ("XRP", "BTC")  # extended to BTC 2026-09-22 per user request - was XRP-only
LIVE_MAX_ENTRY_PRICE_DEFAULT = 0.70  # added 2026-09-22 as a hardcoded 0.80 per user request
                                       # ("not buy in over 80%"), made a CONFIGURABLE setting and
                                       # dropped to 0.70 default 2026-09-28 after a real losing
                                       # morning: 22 real trades, 13 won (59%) but net -$135.19 -
                                       # the win rate wasn't the problem, the PRICE was: average
                                       # entry that day was ~67c, and at that price you need a
                                       # 67%+ win rate just to break even (a win at 67c only pays
                                       # ~$0.49/$1, a loss still costs the full $1) - 59% wasn't
                                       # enough. This was ALSO previously not exposed as a
                                       # setting at all (unlike the flip strategy's own cap) -
                                       # fixed that at the same time.
_live_max_entry_price = LIVE_MAX_ENTRY_PRICE_DEFAULT
# lock strategy only buys the favorite - refuse a fill under the same 58%
# confidence floor the call itself requires (checked on the LIVE order book
# at buy time). Added 2026-10-03: real lock fills under 60c (mostly from the
# stale list-endpoint price) were 14 positions, 5 won, -$715 - more than the
# account's total net loss.
LIVE_MIN_LOCK_ENTRY_PRICE = 0.58
# assets whose CALLS (leaning/confidence/lock) still use the /markets list
# endpoint's ~30s-cached price, the pre-2026-10-03 behavior. XRP reverted
# 2026-10-03 per user request: on live prices its calls locked ~1 min
# earlier and flipped right after locking far more (17% vs ~5%), 23/36
# right. Buys still check the LIVE book + LIVE_MIN_LOCK_ENTRY_PRICE either
# way, so a stale call can't buy the real underdog.
LIST_QUOTE_CALL_ASSETS = ("XRP",)
# take-profit for the main "lock" strategy - added 2026-10-02 per user request
# ("sell at a certain percentage... Kalshi's percent, not money percent"):
# sells an open lock position once the Kalshi price of the side it holds
# reaches this level (0-1). None = off, hold to settlement (the old behavior).
_live_main_sell_price = None
# loss-limit rule - redone 2026-09-24 per user request, replacing a flat
# $300/day number. Dollar mode: disarm at LIVE_LOSS_MULTIPLE x the CURRENT
# bet size (e.g. $300 on $100 bets, $150 on $50 bets - it scales with
# whatever size is actually set, not a fixed dollar figure). Percent mode:
# a dollar threshold doesn't mean the same thing from one trade to the next
# (the $ per loss moves with the account balance), so that mode disarms on
# LIVE_LOSS_STREAK_LIMIT REAL LOSSES IN A ROW instead. Auto-disarms EVERY
# armed asset when tripped, not just the one that lost - one real account
# being protected, not one limit per asset.
LIVE_LOSS_MULTIPLE = 3
LIVE_LOSS_STREAK_LIMIT = 3
_live_consecutive_losses = 0  # resets to 0 on any real win, daily like the other live counters
_live_armed = {a: False for a in LIVE_ASSETS}
_live_lock = threading.Lock()
_live_client = None
# per-ticker real-money BUY retry state (lock strategy only) - added
# 2026-09-26, replacing the old one-shot _live_last_bought_ticker guard. See
# place_live_buy's attempt_num docstring for the full story: the bot now
# tries once per poll instead of a fast burst, and keeps trying across real
# time instead of giving up after a few seconds.
_live_buy_attempt_n = {}     # ticker -> next attempt_num to use (escalated_buffer input)
# how many 1-minute candles to wait AFTER a call locks before the real-money buy starts -
# added 2026-10-04 per user request (XRP first). 0 = buy as soon as the call locks (old behavior).
_live_buy_delay_candles = {a: 0 for a in LIVE_ASSETS}
LIVE_BUY_DELAY_MAX_CANDLES = 10
_live_buy_deadline = {}      # ticker -> wall-clock time.time() after which it stops trying
_live_buy_gave_up = set()    # tickers that are done - either fully filled, or the deadline passed
BUILD_ID = str(int(time.time()))  # changes on every dashboard start; open tabs reload when it changes
HOSTED_PASSWORD = os.environ.get("DASH_PASSWORD", "")          # set on a hosted copy: every page asks for this password (any user name)
HOSTED_READ_ONLY = os.environ.get("READ_ONLY", "") in ("1", "true", "yes")
ACCEPT_SYNC = os.environ.get("ACCEPT_SYNC", "") in ("1", "true", "yes")       # set on the website copy: it takes its streak / records / END CALLS from the owner's bot
_sync_last = [0.0]                                                            # when the owner's bot last sent its state (website copy)
MIRROR_FRESH_SECONDS = 180                                                     # the owner's bot counts as "online" if it sent something within this long
_FOLLOW_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache", "follow_config.json")


def _mirror_active():
    """True when this copy should NOT make its own END CALL because it takes the shared one: the website copy while the owner's bot keeps sending its state, or a
    follower copy (a friend's, with data_cache/follow_config.json) while the website keeps answering."""
    if time.time() - _sync_last[0] >= MIRROR_FRESH_SECONDS:
        return False
    return ACCEPT_SYNC or os.path.exists(_FOLLOW_CONFIG)   # set on a hosted copy: the buy / arm / settings endpoints are refused outright


def _version_tag():
    """' - version 2026-10-18b' on the shared copies (the zip builder writes a VERSION.txt next to src/); nothing on the owner's own copy."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "VERSION.txt"), "r", encoding="utf-8") as f:
            v = f.read().strip()[:40]
        return f" - version {v}" if v and all(ch.isalnum() or ch in "-_. " for ch in v) else ""
    except OSError:
        return ""
_live_buy_paused_logged = set()  # tickers already logged as 'buy paused by flip warning' (log once)
LIVE_BUY_RETRY_WINDOW_SECONDS = 90  # keep trying for up to this long after the first attempt -
                                      # a real miss showed the book can be thin for a few seconds
                                      # right at lock, then have real liquidity moments later
                                      # ("it lingered around... I sat here and waited and waited,
                                      # it had plenty of time to buy")
_live_open_positions = {}          # ticker -> trade row (now with a "strategy" tag: "lock" or
                                     # "flip"), removed once that window settles or (flip only)
                                     # once it's sold early for the profit target

# flip-scalp strategy - added 2026-09-25 per user request, the first time
# this system ever SELLS before settlement. Separate from the main
# buy-and-hold strategy above and its own on/off switch (LIVE_FLIP_ASSETS
# armed state) - a materially different risk profile deserves its own
# switch, not to be silently bundled into the main one. Trigger: a real
# "for sure" flip (the same corrected certain_flip signal, see
# flip_chance_vs_locked_call()) fires AND there's no open real position on
# that ticker yet (never stacks on top of, or double-buys under, an
# existing position - satisfies "only if we don't have a position").
# Buys into the NEW (flipped-to) direction. Exit: sells the instant the
# position is worth _live_flip_profit_target more than it cost; if that
# target is never reached, it just holds to natural settlement like the
# main strategy - no separate stop-loss, same "hold to close" philosophy
# this whole system is built on.
LIVE_FLIP_PROFIT_TARGET_DEFAULT = 0.20
_live_flip_profit_target = LIVE_FLIP_PROFIT_TARGET_DEFAULT  # configurable on-site 2026-09-25
                                                               # per user request ("choose the
                                                               # percentage and what to sell at")
LIVE_FLIP_MAX_ENTRY_PRICE_DEFAULT = 0.80  # same idea as LIVE_MAX_ENTRY_PRICE but separately
                                            # configurable for the flip-scalp strategy only -
                                            # added 2026-09-25 per user request ("choose the
                                            # price on the flip too... don't want it [buying in
                                            # on a flip] at a lot" - i.e. skip flip entries that
                                            # cost too much per contract, tunable on its own)
_live_flip_max_entry_price = LIVE_FLIP_MAX_ENTRY_PRICE_DEFAULT
# flip-scalp sell mode - added 2026-09-25 per user request. "gain" is the
# original behavior (sell at +_live_flip_profit_target as a % of cost).
# "price" is the alternate mode: sell once Kalshi's own market price for our
# side reaches _live_flip_sell_price_target (e.g. 65%) - a flat probability
# LEVEL rather than a %-gain-on-cost, which the user pointed out is awkward
# since flip entries land at very different prices (a 20% gain off a 20-cent
# entry vs. a 20% gain off an 80-cent entry are very different payouts).
_live_flip_sell_mode = "gain"   # "gain" or "price"
_live_flip_sell_price_target = 0.65
# "hold if guaranteed" - added 2026-09-25 per user request: when armed, skip
# the profit-take/trailing-stop sell entirely while this ticker's certain-
# flip signal (the same one behind the big on-screen FLIP alert) is still
# active - "if it thinks it's going to be a guaranteed flip... maybe hold"
# instead of cashing out early for less than the eventual $1 settlement.
# Off by default - the existing sell-early behavior is the safer default.
_live_flip_hold_if_certain = False
_live_flip_armed = {a: False for a in LIVE_ASSETS}
_live_flip_last_ticker = {a: None for a in LIVE_ASSETS}  # per-asset: ticker already used
                                                            # for a flip-scalp entry this window -
                                                            # "stay out" afterward, win/lose/sold,
                                                            # never a second flip entry same window
_live_flip_followup = {}     # ticker -> wall-clock time.time() a follow-up flip-scalp buy
                               # becomes eligible, set by sell_live_position right after a
                               # protective sell clears a lock position - see that function's
                               # note. LIVE_FLIP_FOLLOWUP_DELAY_SECONDS below is the gap.
LIVE_FLIP_FOLLOWUP_DELAY_SECONDS = 1.0  # "let's try one second first" (2026-09-27, down from
                                           # the initial 1.5s) - gives the sell order time to
                                           # actually clear before the new buy

# protective-sell strategy - added 2026-09-26 per user request, a THIRD
# independent switch alongside real-money auto-buy and flip-scalp ("put
# another box for it... so I don't get locked into it"). Different problem
# from flip-scalp: flip-scalp only fires when FLAT (no existing position),
# so once a real "lock" position is open, a flip against it just rides to
# settlement with no automatic response. This sells that EXISTING lock
# position early instead - never buys anything itself. Its own confidence
# bar, deliberately SEPARATE from CERTAIN_FLIP_MIN_CHANCE (the flip-scalp/
# big-alert bar) - a real backtest of 65 fired signals (2026-09-26) found
# the two need different bars: at the 55% bar used for buying the flip,
# 12 of 65 were false alarms (81.5% accuracy) - fine for an early scalp
# entry, too risky for selling a position that might still win. Every false
# alarm in that sample was under 69% confidence; raising the bar to 70%
# would have been 100% accurate (15/15), at the cost of only catching
# ~28% of the real flips - the right tradeoff for a SELL decision, where a
# false alarm costs you a position that would have won.
LIVE_PROTECT_MIN_CONFIDENCE_DEFAULT = 0.70
_live_protect_min_confidence = LIVE_PROTECT_MIN_CONFIDENCE_DEFAULT
_live_protect_armed = {a: False for a in LIVE_ASSETS}
_last_protect_signal = {}  # asset name -> bool, its OWN edge-trigger state (separate from
                             # _last_certain_flip, which is edge-triggered at the LOWER,
                             # flip-scalp/alert bar and would fire too early for this)
_live_protect_selling = set()  # tickers currently being retried for a protective sell - added
                                 # 2026-09-27 per real user report: the OLD version only ever
                                 # tried once, right when the signal edge-fired, and just sat
                                 # un-filled with no retry if that one attempt didn't cross the
                                 # spread ("it was a delay before it actually sold me out").
                                 # Now: once marked, every poll retries via sell_live_position
                                 # (which itself now also retries with an escalating buffer)
                                 # until the position is actually gone.
_live_trades_today = []            # today's trade rows (buys + settles), memory-only, UTC-day resets
_live_last_trade_result = {}       # (asset_name, strategy) -> last completed row (settle or sell) -
                                     # added 2026-09-27 per user request ("if it flips, lock it in
                                     # at whatever... show the ups and downs in the moment, until it
                                     # locks") - keeps the last real outcome visible on the box even
                                     # after the position closes, instead of it just going blank
_live_realized_pnl_today = 0.0
_live_realized_day = None          # UTC date string; when it changes, the three above reset
_live_last_error = None            # last buy/settle exception message, shown on-site so a silent
                                     # failure is never actually silent
# bet sizing - configurable on-site 2026-09-22 per user request. "dollars" is a flat
# amount every trade; "percent" spends that % of the REAL account balance (read fresh
# at the moment of each trade, not cached) - lets sizing scale with the account instead
# of staying fixed.
_live_size_mode = "dollars"   # "dollars" or "percent"
_live_size_dollars = 100.0
_live_size_percent = 10.0
LIVE_TRADES_LOG = os.path.join(os.path.dirname(__file__), "..", "bot_state", "live_xrp_lock_trades.csv")
LIVE_TRADE_FIELDS = ["ts", "event", "asset", "ticker", "strategy", "side", "contracts", "price",
                      "cost_dollars", "payoff_dollars", "order_id", "filled",
                      "entry_flip_chance", "entry_swing_pct", "flip_sell_mode", "flip_hold_if_certain",
                      "sell_reason"]
# sell_reason - added 2026-09-28 after a real "why did this sell?" mystery
# (a loss that turned out NOT to have crossed the protective-sell bar at
# all, and there was no record of what actually triggered it - manual?
# flip-scalp profit-take? protective-sell?). Every "sell" event now records
# in plain text which of those it was; blank for "buy"/"settle" rows.
# the last 4 fields - added 2026-09-25 per user request ("keep gathering this
# information and save it so we can learn from it, over time") - only
# populated on flip-scalp BUY rows (blank on everything else): the signal
# strength at the moment of entry (how confident the certain-flip call was,
# how big the price swing was) and which sell-mode/hold setting was active,
# so wins/losses can later be correlated against entry strength and settings
# instead of just looking at raw win rate in isolation.

# armed state + sizing now SURVIVE a restart (changed 2026-09-24 per user
# request: "so I can walk away, like it should be") - previously armed
# always reset to off on every restart on purpose, specifically so a crash
# could never silently resume spending real money with nobody re-confirming
# it. The user explicitly asked for the opposite trade-off (set it once,
# trust it to keep running) - the daily loss limit auto-disarm stays in
# place regardless as the one backstop that can't be configured away.
LIVE_TRADING_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "bot_state", "live_trading_state.json")


def _load_live_trading_state():
    global _live_size_mode, _live_size_dollars, _live_size_percent, _live_flip_profit_target
    global _live_flip_max_entry_price, _live_flip_sell_mode, _live_flip_sell_price_target
    global _live_flip_hold_if_certain, _live_protect_min_confidence, _live_max_entry_price
    global _live_main_sell_price
    try:
        with open(LIVE_TRADING_STATE_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return  # no saved state yet (first run) or unreadable - start from the safe defaults
    armed = saved.get("armed") or {}
    for a in LIVE_ASSETS:
        if a in armed:
            _live_armed[a] = bool(armed[a])
    flip_armed = saved.get("flip_armed") or {}
    for a in LIVE_ASSETS:
        if a in flip_armed:
            _live_flip_armed[a] = bool(flip_armed[a])
    protect_armed = saved.get("protect_armed") or {}
    for a in LIVE_ASSETS:
        if a in protect_armed:
            _live_protect_armed[a] = bool(protect_armed[a])
    if saved.get("size_mode") in ("dollars", "percent"):
        _live_size_mode = saved["size_mode"]
    if isinstance(saved.get("size_dollars"), (int, float)):
        _live_size_dollars = float(saved["size_dollars"])
    if isinstance(saved.get("size_percent"), (int, float)):
        _live_size_percent = float(saved["size_percent"])
    if isinstance(saved.get("flip_profit_target"), (int, float)) and 0.01 <= saved["flip_profit_target"] <= 0.95:
        _live_flip_profit_target = float(saved["flip_profit_target"])
    if (isinstance(saved.get("flip_max_entry_price"), (int, float))
            and 0.01 <= saved["flip_max_entry_price"] <= 0.99):
        _live_flip_max_entry_price = float(saved["flip_max_entry_price"])
    if saved.get("flip_sell_mode") in ("gain", "price"):
        _live_flip_sell_mode = saved["flip_sell_mode"]
    if (isinstance(saved.get("flip_sell_price_target"), (int, float))
            and 0.01 <= saved["flip_sell_price_target"] <= 0.99):
        _live_flip_sell_price_target = float(saved["flip_sell_price_target"])
    if isinstance(saved.get("flip_hold_if_certain"), bool):
        _live_flip_hold_if_certain = saved["flip_hold_if_certain"]
    if (isinstance(saved.get("protect_min_confidence"), (int, float))
            and 0.50 <= saved["protect_min_confidence"] <= 0.99):
        _live_protect_min_confidence = float(saved["protect_min_confidence"])
    if (isinstance(saved.get("max_entry_price"), (int, float))
            and 0.01 <= saved["max_entry_price"] <= 0.99):
        _live_max_entry_price = float(saved["max_entry_price"])
    if (isinstance(saved.get("main_sell_price"), (int, float))
            and 0.01 <= saved["main_sell_price"] <= 0.99):
        _live_main_sell_price = float(saved["main_sell_price"])
    delays = saved.get("buy_delay_candles") or {}
    for a in LIVE_ASSETS:
        if isinstance(delays.get(a), int) and 0 <= delays[a] <= LIVE_BUY_DELAY_MAX_CANDLES:
            _live_buy_delay_candles[a] = delays[a]
    if any(_live_armed.values()) or any(_live_flip_armed.values()) or any(_live_protect_armed.values()):
        print(f"[LIVE] restored armed state from disk: {_live_armed}, flip: {_live_flip_armed}, "
              f"protect: {_live_protect_armed}")


LIVE_BOUGHT_FILE = os.path.join(os.path.dirname(__file__), "..", "bot_state", "live_bought_tickers.json")


def _load_bought_tickers():
    """Tickers that already got a real buy in - loaded into _live_buy_gave_up at
    startup so a dashboard RESTART can never buy the same round a second time.
    Added 2026-10-04 after a restart in the middle of a round wiped the
    in-memory buy state and the bot bought the same round twice."""
    try:
        with open(LIVE_BOUGHT_FILE, encoding="utf-8") as f:
            for t in json.load(f):
                _live_buy_gave_up.add(t)
    except (OSError, ValueError):
        pass


def _remember_bought_ticker(ticker):
    try:
        try:
            with open(LIVE_BOUGHT_FILE, encoding="utf-8") as f:
                seen = json.load(f)
        except (OSError, ValueError):
            seen = []
        if ticker not in seen:
            seen = (seen + [ticker])[-300:]
            tmp = LIVE_BOUGHT_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(seen, f)
            os.replace(tmp, LIVE_BOUGHT_FILE)
    except OSError:
        pass


def _save_live_trading_state():
    try:
        os.makedirs(os.path.dirname(LIVE_TRADING_STATE_FILE), exist_ok=True)
        tmp = LIVE_TRADING_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"armed": dict(_live_armed), "flip_armed": dict(_live_flip_armed),
                       "protect_armed": dict(_live_protect_armed),
                       "size_mode": _live_size_mode,
                       "size_dollars": _live_size_dollars, "size_percent": _live_size_percent,
                       "flip_profit_target": _live_flip_profit_target,
                       "flip_max_entry_price": _live_flip_max_entry_price,
                       "flip_sell_mode": _live_flip_sell_mode,
                       "flip_sell_price_target": _live_flip_sell_price_target,
                       "flip_hold_if_certain": _live_flip_hold_if_certain,
                       "protect_min_confidence": _live_protect_min_confidence,
                       "max_entry_price": _live_max_entry_price,
                       "main_sell_price": _live_main_sell_price,
                       "buy_delay_candles": dict(_live_buy_delay_candles)}, f)
        os.replace(tmp, LIVE_TRADING_STATE_FILE)
    except OSError:
        pass  # persistence is a nice-to-have - never let it crash the poll loop/request handler


def _live_ensure_log():
    d = os.path.dirname(LIVE_TRADES_LOG)
    os.makedirs(d, exist_ok=True)
    if not os.path.exists(LIVE_TRADES_LOG):
        with open(LIVE_TRADES_LOG, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=LIVE_TRADE_FIELDS).writeheader()


def _live_append_log(row):
    _live_ensure_log()
    with open(LIVE_TRADES_LOG, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=LIVE_TRADE_FIELDS).writerow(row)


def _live_reset_day_if_needed():
    global _live_realized_day, _live_realized_pnl_today, _live_trades_today, _live_consecutive_losses
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    with _live_lock:
        if _live_realized_day != today:
            _live_realized_day = today
            _live_realized_pnl_today = 0.0
            _live_trades_today = []
            _live_consecutive_losses = 0


def _live_loss_limit_status():
    """(hit, description) - see the LIVE_LOSS_MULTIPLE/LIVE_LOSS_STREAK_LIMIT
    comment above for the rule. Must be called while already holding
    _live_lock (both settle_live_position and /live_status already do)."""
    if _live_size_mode == "percent":
        hit = _live_consecutive_losses >= LIVE_LOSS_STREAK_LIMIT
        desc = f"{_live_consecutive_losses} real loss{'es' if _live_consecutive_losses != 1 else ''} in a row (limit {LIVE_LOSS_STREAK_LIMIT})"
    else:
        threshold = LIVE_LOSS_MULTIPLE * _live_size_dollars
        hit = _live_realized_pnl_today <= -threshold
        desc = (f"today's real losses hit ${-_live_realized_pnl_today:.2f} "
                f"(limit ${threshold:.2f} = {LIVE_LOSS_MULTIPLE}x the ${_live_size_dollars:.0f} bet size)")
    return hit, desc


_exchange_index_cache = {}  # series prefix (e.g. KXXRP15M) -> Kalshi exchange_index (balance pool)


def _live_exchange_index(ticker):
    series = ticker.split("-")[0]
    if series not in _exchange_index_cache:
        try:
            _exchange_index_cache[series] = get_market_snapshot(ticker).get("exchange_index")
        except Exception:
            return None
    return _exchange_index_cache.get(series)


def _live_resolve_bet_dollars(client, ticker=None):
    """The dollar amount for the NEXT trade, given the current sizing mode.
    Percent mode reads the REAL account balance fresh (never cached) so it
    always reflects the actual current balance, not a stale snapshot.
    Changed 2026-10-04: both modes now use the spendable balance of the pool
    THIS market trades in (see KalshiClient.get_balance_for_exchange), and a
    bet is never larger than 96% of it (leaves room for the fee) - a 20% bet
    off the $305 total failed with insufficient_balance when only $84 was in
    the 15-minute markets' pool."""
    pool = client.get_balance_for_exchange(_live_exchange_index(ticker)) if ticker else client.get_balance_dollars()
    if _live_size_mode == "percent":
        bet = max(1.0, pool * (_live_size_percent / 100.0))
    else:
        bet = _live_size_dollars
    return max(0.0, min(bet, pool * 0.96))


LIVE_BUY_MAX_ATTEMPTS = 4  # added 2026-09-24 - a flat 1-cent buffer, single attempt, only
                            # filled 3 of 6 real orders (50%). kalshi_live_bot.py already
                            # hit and solved this exact problem for its own entries
                            # (escalated_buffer, see there) - reusing that instead of a
                            # thinner one-shot attempt. Each retry re-quotes the live book
                            # fresh (price may genuinely have moved) and prices more
                            # aggressively than the last (escalated_buffer(attempt)).


def place_live_buy(asset_name, ticker, side_yesno, strategy="lock", entry_signal=None, attempt_num=None):
    """Fires real buy order(s) totalling up to the configured bet size.
    strategy="lock" is the original buy-and-hold-to-settlement entry (the
    instant an asset's Locked First Call becomes real); strategy="flip" is
    the flip-scalp entry (added 2026-09-25, only called when flat on that
    ticker, buying the NEW direction right after a real flip). entry_signal,
    flip buys only - added 2026-09-25 per user request to keep learning from
    this data over time: a dict of {"flip_chance", "swing_pct", "sell_mode",
    "hold_if_certain"} describing the trigger strength and active settings
    at the moment of entry, logged alongside the trade so outcomes can later
    be correlated against them. Only ever called while the relevant switch
    is armed.

    attempt_num=None (flip-scalp's mode, unchanged since 2026-09-26): retries
    up to LIVE_BUY_MAX_ATTEMPTS times INSIDE this one call, a few seconds
    apart, TOPPING UP toward the full bet size each attempt (fixed
    2026-09-26 - "sometimes it buys me in a little amount instead of my
    hundred": a Kalshi immediate_or_cancel order can PARTIALLY fill, and the
    old code logged the originally REQUESTED contracts/cost instead of what
    actually filled). Flip-scalp keeps this fast, single-call burst on
    purpose - it wants a decisive entry right when the flip fires, not a
    lingering chase.

    attempt_num=<int> (the "lock" strategy's mode, added 2026-09-26 per user
    report: the old burst-of-4-in-a-few-seconds gave up too fast - "it
    lingered around, I sat here and waited and waited, it had plenty of time
    to buy" - the user bought it manually later in the SAME window after the
    bot's quick burst had already given up): does exactly ONE order attempt
    at that attempt number's escalated price buffer, tops up any position
    already open on this ticker from an EARLIER poll's attempt (reads
    _live_open_positions fresh, treats its cost as already spent), and
    returns whether the bet is now fully filled. The CALLER (poll_loop) is
    responsible for calling this again on a LATER poll if it isn't - see the
    real-money auto-buy trigger above, which keeps trying for up to
    LIVE_BUY_RETRY_WINDOW_SECONDS instead of one quick burst. Spreads the
    exact same escalating-buffer retry logic across real time instead of a
    few seconds, which is what actually matches how a thin order book
    refills.

    Either mode skips entirely (no order placed) if the price we'd pay is
    over LIVE_MAX_ENTRY_PRICE / the flip cap, checked fresh on every attempt
    - but never discards an already-filled partial position over that."""
    global _live_client, _live_last_error
    if strategy != "lock":
        with _live_lock:
            _live_flip_last_ticker[asset_name] = ticker  # "stay out" for the rest of this window,
                                                            # win lose or sell - never re-enter after
    _live_reset_day_if_needed()
    try:
        if _live_client is None:
            _live_client = KalshiClient()
        order_side = "bid" if side_yesno == "yes" else "ask"
        bet_dollars = _live_resolve_bet_dollars(_live_client, ticker)
        # the flip-scalp strategy has its OWN configurable price cap, separate
        # from the main strategy's - added 2026-09-25 per user request ("choose
        # the price on the flip too... don't want it [buying] at a lot")
        max_entry_price = _live_flip_max_entry_price if strategy == "flip" else _live_max_entry_price

        # start from whatever's ALREADY filled for this ticker (from an
        # earlier poll's single attempt, in attempt_num mode) instead of
        # buying a second full bet size on top of it
        existing_contracts = existing_cost = existing_weighted_price_sum = 0.0
        existing_order_id = ""
        existing_peak = 0.0
        existing_baseline_realized = 0.0  # Kalshi's realized_pnl_dollars for this ticker at
                                            # the moment we started holding it - a fresh market
                                            # should always start at 0, but preserved from an
                                            # existing partial position across top-up rounds so
                                            # it's never reset mid-fill (see
                                            # check_manual_position_change)
        if attempt_num is not None:
            with _live_lock:
                existing_pos = _live_open_positions.get(ticker)
            if existing_pos and existing_pos.get("strategy", "lock") == strategy:
                existing_contracts = float(existing_pos.get("contracts", 0) or 0)
                existing_cost = float(existing_pos.get("cost_dollars", 0) or 0)
                existing_price = existing_pos.get("price")
                existing_weighted_price_sum = (existing_price * existing_contracts) if existing_price else 0.0
                existing_order_id = existing_pos.get("order_id", "")
                existing_peak = existing_pos.get("peak_gain_pct", 0.0)
                existing_baseline_realized = existing_pos.get("_baseline_realized_pnl", 0.0)

        total_contracts, total_cost = existing_contracts, existing_cost
        weighted_price_sum = existing_weighted_price_sum
        last_order_id = existing_order_id
        remaining_dollars = max(0.0, bet_dollars - existing_cost)
        if remaining_dollars < 0.01:
            return True  # already fully filled from an earlier poll's attempt - nothing to do

        attempt_numbers = [attempt_num] if attempt_num is not None else list(range(LIVE_BUY_MAX_ATTEMPTS))
        for a in attempt_numbers:
            if remaining_dollars < 0.01:
                break
            fresh_quote = get_live_yes_bid_ask(ticker)
            if fresh_quote is not None:
                yes_bid, yes_ask = fresh_quote
            else:
                yes_bid, yes_ask = get_yes_bid_ask(get_market_snapshot(ticker))
            price = marketable_price(order_side, yes_bid, yes_ask, buffer=escalated_buffer(a))
            cost_basis = price if side_yesno == "yes" else (1 - price)
            if cost_basis <= 0:
                raise RuntimeError(f"bad price read (cost_basis={cost_basis})")
            side_ask = yes_ask if side_yesno == "yes" else (1 - yes_bid)
            if strategy == "lock" and side_ask < LIVE_MIN_LOCK_ENTRY_PRICE:
                # the lock strategy buys the FAVORITE - if the live book says
                # this side is under the confidence floor, the call was made
                # off a stale/moved price and it's no longer the favorite.
                # Real live data: fills under 60c went 5-9 for -$715.
                with _live_lock:
                    _live_last_error = None
                print(f"[LIVE {asset_name}] {ticker}: skipped (${remaining_dollars:.2f} left), live price "
                      f"{side_ask:.2f} is under the {LIVE_MIN_LOCK_ENTRY_PRICE:.0%} favorite floor")
                if total_contracts == existing_contracts:
                    return False
                break
            if cost_basis > max_entry_price:
                with _live_lock:
                    _live_last_error = None  # not a failure - a deliberate skip, don't flag it red
                print(f"[LIVE {asset_name}] {ticker}: skipped (${remaining_dollars:.2f} left), price "
                      f"{cost_basis:.2f} over the {max_entry_price:.0%} cap")
                if total_contracts == existing_contracts:
                    return False  # nothing NEW filled this call - try again later poll (attempt_num mode)
                                    # or give up entirely, nothing at all yet (burst mode, attempt_num=None)
                break  # keep whatever already filled, just stop trying for the rest this call
            contracts_wanted = max(1, round(remaining_dollars / cost_basis))
            # network call - deliberately OUTSIDE _live_lock, so a slow request
            # never blocks the page's /live_status reads
            result = _live_client.create_order(ticker, order_side, contracts_wanted, price, reduce_only=False)
            order_id = result.get("order_id", "")
            fill_count = float(result.get("fill_count", "0") or 0)
            avg_fill_price_raw = result.get("average_fill_price")
            avg_fill_price = float(avg_fill_price_raw) if avg_fill_price_raw not in (None, "") else price
            print(f"[LIVE {asset_name} {strategy.upper()} BUY attempt {a + 1}] "
                  f"{ticker} {side_yesno} wanted x{contracts_wanted} @ {price:.3f} -> order_id={order_id} "
                  f"filled x{fill_count:.0f} @ {avg_fill_price:.3f}")
            if fill_count > 0:
                fill_cost_basis = avg_fill_price if side_yesno == "yes" else (1 - avg_fill_price)
                last_order_id = order_id
                total_contracts += fill_count
                total_cost += fill_count * fill_cost_basis
                weighted_price_sum += fill_count * avg_fill_price
                remaining_dollars = max(0.0, remaining_dollars - fill_count * fill_cost_basis)

        newly_filled = total_contracts > existing_contracts
        fully_filled = remaining_dollars < 0.01
        filled = total_contracts > 0
        sig = entry_signal or {}
        row = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "event": "buy", "asset": asset_name,
               "ticker": ticker, "strategy": strategy, "side": side_yesno, "contracts": int(total_contracts),
               "price": (round(weighted_price_sum / total_contracts, 3) if filled else None),
               "cost_dollars": round(total_cost, 2),
               "payoff_dollars": None, "order_id": last_order_id, "filled": filled,
               "entry_flip_chance": (round(sig["flip_chance"], 4) if sig.get("flip_chance") is not None else ""),
               "entry_swing_pct": (round(sig["swing_pct"], 6) if sig.get("swing_pct") is not None else ""),
               "flip_sell_mode": sig.get("sell_mode", ""),
               "flip_hold_if_certain": sig.get("hold_if_certain", "")}
        if not fully_filled and filled and attempt_num is None:
            print(f"[LIVE {asset_name}] {ticker}: only partially filled - got ${total_cost:.2f} of the "
                  f"${bet_dollars:.2f} bet (${remaining_dollars:.2f} short) after {LIVE_BUY_MAX_ATTEMPTS} attempts")
        # only log/count a NEW order row when this call actually changed
        # something - in attempt_num mode a zero-fill poll would otherwise
        # spam the CSV with an identical row every ~2s while it keeps trying
        if newly_filled or attempt_num is None:
            _live_append_log(row)
            with _live_lock:
                _live_trades_today.append(row)
        if filled:
            with _live_lock:
                _live_open_positions[ticker] = dict(row, peak_gain_pct=existing_peak,
                                                     _baseline_realized_pnl=existing_baseline_realized)
            _remember_bought_ticker(ticker)
        with _live_lock:
            _live_last_error = None
        return fully_filled
    except Exception as exc:
        with _live_lock:
            _live_last_error = f"BUY FAILED {ticker}: {exc!r}"
        print(f"[LIVE {asset_name} BUY FAILED] {ticker}: {exc!r}")
        return False


LIVE_FLIP_TRAIL_MIN_PEAK = 0.08   # trailing-stop only arms once unrealized gain has reached
                                    # at least this much - added 2026-09-25 per user request
                                    # ("if it's not going to make 20%, just sell and take profit")
LIVE_FLIP_TRAIL_GIVEBACK = 0.50   # sell once gain has fallen to this fraction OR LESS of its peak
                                    # (i.e. given back at least half of what it had built)


def _position_current_gain_and_price(pos):
    """Current unrealized gain on an OPEN position (fraction of cost, 0.20 =
    up 20%) AND the current market price for our held side (0-1) - reads the
    live market fresh every call, one snapshot covers both so the exit check
    below doesn't double the network call per poll. Added 2026-09-25 for the
    flip-scalp's alternate "sell at an absolute Kalshi price level" exit mode
    (per user request - a flat %-gain target is awkward when flip entries
    land at wildly different prices, so the payout multiplier on a 20% gain
    varies a lot entry to entry; a price LEVEL like "sell at 65%" doesn't).
    Returns (None, None) on any read failure so the caller just skips that
    poll's check rather than treating a network hiccup as a loss."""
    try:
        market = get_market_snapshot(pos["ticker"])
        yes_bid, yes_ask = get_yes_bid_ask(market)
        current_price = yes_bid if pos["side"] == "yes" else (1 - yes_ask)
        current_value = pos["contracts"] * current_price
        gain = (current_value - pos["cost_dollars"]) / pos["cost_dollars"]
        return gain, current_price
    except Exception:
        return None, None


SELL_BURST_MAX_ATTEMPTS = 4  # rewritten 2026-09-27 per user report: protective sell fired,
                               # then sat un-filled with no retry until the signal happened to
                               # re-cross its bar - "I said it was going to flip, and then it was
                               # a delay before it actually sold me out." Escalating-price retry
                               # burst, same pattern already validated for buys (escalated_buffer).


def sell_live_position(ticker, reason):
    """Real early exit - added 2026-09-25 for the flip-scalp strategy, the
    first time this system ever sells before settlement. Reuses the exact
    exit-order mapping kalshi_live_bot.py already validated for its own real
    exits: closing a "yes" holding means SELLING yes (order_side "ask");
    closing a "no" holding means BUYING yes back (order_side "bid").

    Rewritten 2026-09-27 to fix two real bugs found from a live report: (1)
    a SINGLE attempt with a flat 1% buffer often didn't cross the spread
    immediately, and the caller (protective-sell) only ever asks once per
    signal, so a miss here could go un-retried for a long time - now retries
    within this call with an escalating buffer, same idea as place_live_buy.
    (2) a PARTIAL fill used to be recorded as if the whole position sold
    (using pos["contracts"] instead of the real fill_count) - the exact same
    class of bug already fixed on the buy side - now tracks the real filled
    amount and shrinks the position by only that much if it's a partial."""
    global _live_client, _live_realized_pnl_today, _live_last_error, _live_consecutive_losses
    with _live_lock:
        pos = _live_open_positions.pop(ticker, None)
    if pos is None:
        return
    _live_reset_day_if_needed()
    asset_name = pos.get("asset", "?")
    # initialized OUTSIDE the try block so the except handler below can
    # always see how much (if anything) actually filled before an
    # exception hit, even if it hit mid-burst after a partial fill already
    # went through for real - restoring the FULL original position in that
    # case would double-count contracts that are genuinely already sold
    total_contracts_target = pos["contracts"]
    filled_contracts = 0.0
    filled_proceeds = 0.0
    weighted_quote_sum = 0.0  # fill-weighted average YES-quote price, for the logged "price" field
    try:
        if _live_client is None:
            _live_client = KalshiClient()
        exit_order_side = "ask" if pos["side"] == "yes" else "bid"
        last_order_id = ""
        remaining = total_contracts_target
        for attempt in range(SELL_BURST_MAX_ATTEMPTS):
            if remaining < 0.005:
                break
            try:
                market = get_market_snapshot(ticker)
                yes_bid, yes_ask = get_yes_bid_ask(market)
                quote_price = marketable_price(exit_order_side, yes_bid, yes_ask, buffer=escalated_buffer(attempt))
                result = _live_client.create_order(ticker, exit_order_side, remaining, quote_price, reduce_only=True)
            except Exception as attempt_exc:
                # a single attempt's network/API hiccup must never lose
                # track of fills earlier attempts in THIS burst already
                # got for real - log it and let the loop either try again
                # or fall through to the normal success/partial handling
                # below with whatever's been filled so far
                print(f"[LIVE {asset_name} SELL attempt {attempt + 1}/{SELL_BURST_MAX_ATTEMPTS}] "
                      f"{ticker} FAILED: {attempt_exc!r}")
                continue
            order_id = result.get("order_id", "")
            fill_count = float(result.get("fill_count", "0") or 0)
            avg_fill_price_raw = result.get("average_fill_price")
            avg_fill_price = float(avg_fill_price_raw) if avg_fill_price_raw not in (None, "") else quote_price
            print(f"[LIVE {asset_name} SELL attempt {attempt + 1}/{SELL_BURST_MAX_ATTEMPTS}] {ticker} "
                  f"{exit_order_side} wanted x{remaining:.2f} @ {quote_price:.3f} -> order_id={order_id} "
                  f"filled x{fill_count:.0f} @ {avg_fill_price:.3f}")
            if fill_count > 0:
                proceeds_per_contract = avg_fill_price if pos["side"] == "yes" else (1 - avg_fill_price)
                last_order_id = order_id
                filled_contracts += fill_count
                filled_proceeds += fill_count * proceeds_per_contract
                weighted_quote_sum += fill_count * avg_fill_price
                remaining = max(0.0, remaining - fill_count)
        filled = filled_contracts > 0
        if not filled:
            # nothing crossed the spread across the whole burst - put it back
            # as still open. The caller (check_flip_position_exit runs every
            # poll unconditionally; protective-sell now also keeps retrying
            # every poll via _live_protect_selling, see poll_loop) will try
            # again shortly rather than silently losing track of it.
            with _live_lock:
                _live_open_positions[ticker] = pos
            print(f"[LIVE {asset_name} SELL DIDN'T FILL] {ticker}: will retry next poll")
            return
        if filled_contracts < total_contracts_target - 0.005:
            # partial fill - keep the remainder open (accurately sized) so
            # the caller's next-poll retry finishes the job, instead of
            # either discarding it or falsely recording a full exit
            remaining_fraction = (total_contracts_target - filled_contracts) / total_contracts_target
            with _live_lock:
                _live_open_positions[ticker] = dict(
                    pos, contracts=round(total_contracts_target - filled_contracts, 2),
                    cost_dollars=round(pos["cost_dollars"] * remaining_fraction, 2))
            print(f"[LIVE {asset_name} SELL PARTIAL] {ticker}: sold {filled_contracts:.2f} of "
                  f"{total_contracts_target:.2f} - {total_contracts_target - filled_contracts:.2f} left open")
        cost_basis_sold = round(pos["cost_dollars"] * (filled_contracts / total_contracts_target), 2)
        payoff = filled_proceeds - cost_basis_sold
        row = dict(pos)
        row["contracts"] = round(filled_contracts, 2)
        row["cost_dollars"] = cost_basis_sold
        avg_exit_quote = weighted_quote_sum / filled_contracts
        row.pop("peak_gain_pct", None)
        row.pop("_baseline_realized_pnl", None)  # in-memory-only field (manual-sell detection) -
                                                    # not part of LIVE_TRADE_FIELDS, must never
                                                    # reach the CSV
        row.setdefault("strategy", "lock")  # defensive - a position dict from before this field
                                              # existed must never write a short/misaligned CSV row
        for _f in ("entry_flip_chance", "entry_swing_pct", "flip_sell_mode", "flip_hold_if_certain"):
            row.setdefault(_f, "")  # same defensive idea, for positions bought before these fields existed
        row.update({"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "event": "sell",
                    "payoff_dollars": round(payoff, 2), "price": round(avg_exit_quote, 3),
                    "order_id": last_order_id, "sell_reason": reason})
        _live_append_log(row)
        disarmed_now = False
        with _live_lock:
            _live_trades_today.append(row)
            _live_last_trade_result[(asset_name, row.get("strategy", "lock"))] = row
            _live_realized_pnl_today += payoff
            pnl_now = _live_realized_pnl_today
            _live_consecutive_losses = 0 if payoff > 0 else _live_consecutive_losses + 1
            hit, desc = _live_loss_limit_status()
            if hit:
                disarmed_now = (any(_live_armed.values()) or any(_live_flip_armed.values())
                                 or any(_live_protect_armed.values()))
                for a in _live_armed:
                    _live_armed[a] = False
                for a in _live_flip_armed:
                    _live_flip_armed[a] = False
                for a in _live_protect_armed:
                    _live_protect_armed[a] = False
                _live_last_error = f"Auto-disarmed: {desc}."
        print(f"[LIVE {asset_name} SELL] {ticker} ({reason}) payoff=${payoff:+.2f} today_total=${pnl_now:+.2f}")
        if disarmed_now:
            _save_live_trading_state()
            print(f"[LIVE] *** DAILY LOSS LIMIT HIT - ALL ASSETS AUTO-DISARMED *** {_live_last_error}")
        # give flip-scalp a real shot right after a protective sell clears
        # the way - added 2026-09-26 per user request ("make sure the buy is
        # a little bit after the sell, so it successfully will go through").
        # Without this, flip-scalp's own edge-triggered signal usually fires
        # (and gets wasted) WHILE the lock position is still open - since
        # protective-sell's bar is stricter and crosses later, by the time
        # the position is actually cleared there's no fresh trigger left to
        # catch. This schedules a one-shot follow-up attempt a few seconds
        # later (after the sell has had time to actually clear) using
        # whatever the CURRENT direction reads at that point, instead of
        # waiting on a brand new signal that might not come.
        if (not disarmed_now and pos.get("strategy", "lock") == "lock" and _live_flip_armed.get(asset_name)
                and not str(reason).startswith("take-profit")):  # a winning exit isn't a flip signal
            _live_flip_followup[ticker] = time.time() + LIVE_FLIP_FOLLOWUP_DELAY_SECONDS
    except Exception as exc:
        # put back whatever's genuinely still unsold - not blindly the full
        # original position, since a partial fill may have gone through for
        # real before this exception hit (e.g. attempt 2 of the burst
        # failing after attempt 1 already filled some contracts). Losing
        # track of a real open position is worse than a stuck error that
        # gets retried, but double-counting already-sold contracts is a
        # real accounting error, not just a display glitch.
        with _live_lock:
            if filled_contracts > 0.005:
                remaining_fraction = max(0.0, total_contracts_target - filled_contracts) / total_contracts_target
                _live_open_positions[ticker] = dict(
                    pos, contracts=round(total_contracts_target - filled_contracts, 2),
                    cost_dollars=round(pos["cost_dollars"] * remaining_fraction, 2))
            else:
                _live_open_positions[ticker] = pos
            _live_last_error = f"SELL FAILED {ticker}: {exc!r}"
        print(f"[LIVE {asset_name} SELL FAILED] {ticker}: {exc!r} (filled {filled_contracts:.2f} of "
              f"{total_contracts_target:.2f} before the error)")


def check_manual_position_change(ticker):
    """Detects a real position closed or reduced OUTSIDE this dashboard -
    the user selling manually on Kalshi's own site/app - added 2026-09-27
    per user request ("keep track of when I actually sell out manually...
    I don't think it knows"). Before this, a manual sell left
    _live_open_positions stuck thinking the position was still open
    indefinitely (a real report: "open_position" stayed True after a
    manual sell until the window naturally settled).

    Compares Kalshi's own ground-truth position (KalshiClient.get_position,
    the position_fp field - positive=YES contracts, negative=NO, signed)
    against what this dashboard THINKS is held. Any real shortfall is
    recorded using KALSHI'S OWN realized_pnl_dollars for that market, not a
    self-computed price - we have no way to know what price a manual sale
    actually filled at, but Kalshi does. The tracked position is shrunk (or
    cleared, if fully closed) to match reality.

    Only ever touches a ticker this dashboard STILL thinks is open - once
    this dashboard's OWN sell_live_position/settle_live_position succeeds,
    it removes the ticker from _live_open_positions in the same call, so by
    the time this function next runs there is nothing left to misattribute:
    any shortfall found here can only have come from outside the dashboard."""
    global _live_client, _live_realized_pnl_today, _live_last_error, _live_consecutive_losses
    with _live_lock:
        pos = _live_open_positions.get(ticker)
    if pos is None:
        return
    try:
        if _live_client is None:
            _live_client = KalshiClient()
        real = _live_client.get_position(ticker)
        real_fp = float(real.get("position_fp", 0)) if real else 0.0
        real_realized = float(real.get("realized_pnl_dollars", 0)) if real else 0.0
        real_same_side = max(0.0, real_fp) if pos["side"] == "yes" else max(0.0, -real_fp)
        if real_same_side >= pos["contracts"] - 0.005:
            return  # matches what this dashboard already expects - nothing manual happened
        closed_contracts = pos["contracts"] - real_same_side
        # FALSE-ALARM GUARD, added 2026-10-04: on 10-04 at 19:31 the position
        # endpoint lagged ~8s behind a fresh buy, this function read that as a
        # "manual sell", cleared the tracked position, and the buy logic then
        # bought a SECOND full bet ($81.89 + $63.44 on a 20% bet, all lost).
        # Fills showed no sale at all. So: (1) ignore the first 90s after the
        # last buy, (2) only believe a shortfall if Kalshi's own fills show real
        # closing fills for this side that cover it.
        try:
            last_buy_age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(pos["ts"])).total_seconds()
        except (KeyError, ValueError):
            last_buy_age = 9999
        if last_buy_age < 90:
            return
        closing_side = "no" if pos["side"] == "yes" else "yes"   # 'sell no' closes YES, 'sell yes' closes NO
        closed_seen = sum(float(f["count_fp"]) for f in _live_client.get_fills(ticker)
                          if f.get("action") == "sell" and f.get("side") == closing_side)
        if closed_seen < closed_contracts - 0.5:
            return
        baseline_realized = pos.get("_baseline_realized_pnl", 0.0)
        realized_delta = round(real_realized - baseline_realized, 2)
        closed_fraction = closed_contracts / pos["contracts"]
        closed_cost = round(pos["cost_dollars"] * closed_fraction, 2)
        asset_name = pos.get("asset", "?")
        row = dict(pos)
        row.pop("peak_gain_pct", None)
        row.pop("_baseline_realized_pnl", None)
        row.setdefault("strategy", "lock")
        for _f in ("entry_flip_chance", "entry_swing_pct", "flip_sell_mode", "flip_hold_if_certain"):
            row.setdefault(_f, "")
        row.update({"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "event": "sell",
                    "contracts": round(closed_contracts, 2), "cost_dollars": closed_cost,
                    "payoff_dollars": realized_delta, "order_id": "",
                    "sell_reason": "manual sell (detected via Kalshi position check - not "
                                    "initiated by this dashboard)"})
        _live_append_log(row)
        # a hand sale is the user's decision for this window - never buy this
        # ticker again. Fixed 2026-10-04: a hand sale 8s after the first buy
        # cleared the tracked position, and the still-running top-up logic saw
        # "no position" and bought a SECOND full bet ($81.89 + $63.44 = ~$149
        # on a 20% bet) in the same window.
        _live_buy_gave_up.add(ticker)
        disarmed_now = False
        with _live_lock:
            _live_trades_today.append(row)
            _live_last_trade_result[(asset_name, row.get("strategy", "lock"))] = row
            _live_realized_pnl_today += realized_delta
            pnl_now = _live_realized_pnl_today
            _live_consecutive_losses = 0 if realized_delta > 0 else _live_consecutive_losses + 1
            hit, desc = _live_loss_limit_status()
            if real_same_side < 0.005:
                _live_open_positions.pop(ticker, None)
            else:
                remaining_fraction = real_same_side / pos["contracts"]
                _live_open_positions[ticker] = dict(
                    pos, contracts=round(real_same_side, 2),
                    cost_dollars=round(pos["cost_dollars"] * remaining_fraction, 2),
                    _baseline_realized_pnl=real_realized)
            if hit:
                disarmed_now = (any(_live_armed.values()) or any(_live_flip_armed.values())
                                 or any(_live_protect_armed.values()))
                for a in _live_armed:
                    _live_armed[a] = False
                for a in _live_flip_armed:
                    _live_flip_armed[a] = False
                for a in _live_protect_armed:
                    _live_protect_armed[a] = False
                _live_last_error = f"Auto-disarmed: {desc}."
        print(f"[LIVE {asset_name} MANUAL SELL DETECTED] {ticker} closed {closed_contracts:.2f} contracts "
              f"outside the dashboard, realized=${realized_delta:+.2f} today_total=${pnl_now:+.2f}")
        if disarmed_now:
            _save_live_trading_state()
            print(f"[LIVE] *** DAILY LOSS LIMIT HIT - ALL ASSETS AUTO-DISARMED *** {_live_last_error}")
    except Exception as exc:
        # a read failure here must never corrupt tracked state - just log
        # and let the next poll's check try again
        print(f"[LIVE] manual-position check failed for {ticker}: {exc!r}")


def check_flip_position_exit(ticker):
    """Checks ONE open flip-scalp position against the profit-take rules
    (added 2026-09-25) - sells immediately once the target is hit (either
    +_live_flip_profit_target gain on cost, or the market price reaching
    _live_flip_sell_price_target, depending on _live_flip_sell_mode), or, if
    it climbed meaningfully and is now giving that back, sells to lock in
    whatever profit remains rather than risk it turning into a loss by close
    ("if it's not going to make 20%, just sell and take profit"). Holds to
    natural settlement otherwise - no separate stop-loss, same "hold to
    close" philosophy the main strategy already uses. Only ever touches
    strategy="flip" positions - the main buy-and-hold strategy is untouched
    by any of this.

    If _live_flip_hold_if_certain is armed and this ticker's certain-flip
    signal is CURRENTLY active (the same one behind the big on-screen FLIP
    alert), every check below is skipped entirely - added 2026-09-25 per
    user request ("if it thinks it's going to be a guaranteed flip... maybe
    hold") so a position that looks like a lock rides to the full $1
    settlement instead of being cashed out early."""
    with _live_lock:
        pos = dict(_live_open_positions[ticker]) if ticker in _live_open_positions else None
    if pos is None or pos.get("strategy") != "flip":
        return
    if _live_flip_hold_if_certain and _last_certain_flip.get(pos.get("asset"), False):
        return
    gain, price = _position_current_gain_and_price(pos)
    if gain is None:
        return
    peak = pos.get("peak_gain_pct", 0.0)
    if gain > peak:
        peak = gain
        with _live_lock:
            if ticker in _live_open_positions:
                _live_open_positions[ticker]["peak_gain_pct"] = peak
    if _live_flip_sell_mode == "price":
        target_hit = price is not None and price >= _live_flip_sell_price_target
        target_reason = f"hit {_live_flip_sell_price_target:.0%} price target (was {price:.0%})"
    else:
        target_hit = gain >= _live_flip_profit_target
        target_reason = f"hit +{_live_flip_profit_target:.0%} target (was +{gain:.1%})"
    if target_hit:
        sell_live_position(ticker, target_reason)
    elif peak >= LIVE_FLIP_TRAIL_MIN_PEAK and gain > 0 and gain <= peak * (1 - LIVE_FLIP_TRAIL_GIVEBACK):
        sell_live_position(ticker, f"giving back gains (peak +{peak:.1%}, now +{gain:.1%})")


def settle_live_position(ticker, actual_side):
    """Called once a window this bot actually bought into has real-settled.
    Records the real payoff and checks the daily loss limit - auto-disarms
    EVERY armed asset (never silently keeps firing) if today's combined real
    losses hit it."""
    global _live_realized_pnl_today, _live_last_error, _live_consecutive_losses
    with _live_lock:
        pos = _live_open_positions.pop(ticker, None)
    if pos is None:
        return
    _live_reset_day_if_needed()
    asset_name = pos.get("asset", "?")
    won = pos["side"].upper() == (actual_side or "").upper()
    payoff = (pos["contracts"] - pos["cost_dollars"]) if won else -pos["cost_dollars"]
    row = dict(pos)
    row.pop("peak_gain_pct", None)  # in-memory-only field (flip-scalp trailing-stop tracking) -
                                      # not part of LIVE_TRADE_FIELDS, must never reach the CSV
    row.pop("_baseline_realized_pnl", None)  # in-memory-only field (manual-sell detection), same idea
    row.setdefault("strategy", "lock")  # defensive - see sell_live_position's identical note.
                                          # A real outage was caused by exactly this: a position
                                          # bought before the "strategy" field existed settling
                                          # after a restart, writing an 11-field row against a
                                          # 12-field header and silently shifting every column
                                          # after it - which then crashed every single page load
                                          # trying to read the corrupted file (2026-09-25).
    for _f in ("entry_flip_chance", "entry_swing_pct", "flip_sell_mode", "flip_hold_if_certain"):
        row.setdefault(_f, "")  # same defensive idea, for positions bought before these fields existed
    row.update({"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "event": "settle",
                "payoff_dollars": round(payoff, 2)})
    _live_append_log(row)
    disarmed_now = False
    with _live_lock:
        _live_trades_today.append(row)
        _live_last_trade_result[(asset_name, row.get("strategy", "lock"))] = row
        _live_realized_pnl_today += payoff
        pnl_now = _live_realized_pnl_today
        _live_consecutive_losses = 0 if won else _live_consecutive_losses + 1
        hit, desc = _live_loss_limit_status()
        if hit:
            disarmed_now = (any(_live_armed.values()) or any(_live_flip_armed.values())
                             or any(_live_protect_armed.values()))
            for a in _live_armed:
                _live_armed[a] = False
            for a in _live_flip_armed:
                _live_flip_armed[a] = False
            for a in _live_protect_armed:
                _live_protect_armed[a] = False
            _live_last_error = f"Auto-disarmed: {desc}."
    print(f"[LIVE {asset_name} SETTLE] {ticker} {'WON' if won else 'LOST'} payoff=${payoff:+.2f} "
          f"today_total=${pnl_now:+.2f}")
    if disarmed_now:
        _save_live_trading_state()  # the disarm must survive a restart too, not just the arm
        print(f"[LIVE] *** DAILY LOSS LIMIT HIT - ALL ASSETS AUTO-DISARMED *** {_live_last_error}")


def live_stats_since(cutoff_utc, asset_name=None, strategy=None):
    """Real-money up/down stat for the on-site switch - added 2026-09-15 per
    user request, filterable per asset added 2026-09-22, "sell" events (the
    flip-scalp early exit) counted too added 2026-09-25, filterable per
    strategy added 2026-09-27 (for the flip-scalp box's own overall stat -
    "put the overall successful flips on the flip-scalp box"). Reads the
    CSV log's "settle"/"sell" rows (not the in-memory today-only counters),
    so this is correct across a restart, unlike
    _live_trades_today/_live_realized_pnl_today.

    Each row is parsed defensively and a bad one is SKIPPED, not fatal - a
    real outage was caused by exactly one malformed row (a missing field
    shifted every column after it) crashing this on every single page load
    (2026-09-25). One bad line must never be able to take the whole
    dashboard down again."""
    total = 0.0
    staked = 0.0
    wins = 0
    n = 0
    try:
        with open(LIVE_TRADES_LOG, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    if row.get("event") not in ("settle", "sell"):
                        continue
                    if asset_name is not None and row.get("asset") != asset_name:
                        continue
                    if strategy is not None and row.get("strategy", "lock") != strategy:
                        continue
                    ts = dt.datetime.fromisoformat(row["ts"])
                    if ts < cutoff_utc:
                        continue
                    payoff = float(row.get("payoff_dollars") or 0)
                    total += payoff
                    staked += float(row.get("cost_dollars") or 0)
                    if payoff > 0:
                        wins += 1
                    n += 1
                except (ValueError, KeyError, TypeError):
                    continue  # malformed row - skip it, don't let it take down the whole read
    except FileNotFoundError:
        pass
    pct = (total / staked * 100) if staked else 0.0
    win_pct = (wins / n * 100) if n else 0.0
    return {"total": total, "n": n, "pct": pct, "volume": staked, "wins": wins, "win_pct": win_pct}


def live_windowed_stats(window_seconds, asset_name=None):
    """Rolling last-window_seconds version (e.g. last 1h/24h)."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=window_seconds)
    return live_stats_since(cutoff, asset_name)


_history = {}  # asset name -> list of resolved past locked calls, oldest first (capped)
# raised 2026-09-12 per user request - the old 200 cap meant every asset's
# track record silently plateaued at 200 graded windows (oldest one dropped
# each time a new one was added) instead of showing the real full history.
# Each entry is tiny (a handful of numbers), so keeping tens of thousands of
# them costs nothing meaningful in file size - this is now just a sane
# upper bound against unbounded growth, not a real day-to-day limit.
HISTORY_CAP = 50_000

# persisted to disk so the track record survives restarts (needed often while
# iterating on this file) instead of silently resetting to zero every time.
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "data_cache", "kalshi_dashboard_history.json")


_hist_save_lock = threading.Lock()
_hist_saved_total = 0          # windows on disk after the last load / save - a save that would shrink this a lot is refused


def _history_backups():
    d = os.path.dirname(HISTORY_FILE)
    base = os.path.basename(HISTORY_FILE) + ".auto_"
    try:
        return sorted((os.path.join(d, n) for n in os.listdir(d) if n.startswith(base)), reverse=True)
    except OSError:
        return []


def _load_history():
    """Added 2026-10-14 after the whole history (~2,800 windows per coin) was wiped: two dashboard copies saved through the same temp file and
    one corrupted the other's write; the next start read garbage and quietly began from empty. Now an unreadable file is kept aside
    (.corrupt_*) and the newest automatic backup is used instead of an empty history."""
    global _history, _hist_saved_total
    try:
        with open(HISTORY_FILE, "r") as f:
            _history = json.load(f)
    except FileNotFoundError:
        _history = {}
    except (ValueError, OSError) as e:
        print(f"[history] main file unreadable ({e!r}) - keeping a copy and trying the newest automatic backup")
        try:
            import shutil
            shutil.copy2(HISTORY_FILE, HISTORY_FILE + f".corrupt_{int(time.time())}")
        except Exception:
            pass
        _history = {}
        for bp in _history_backups():
            try:
                with open(bp, "r") as f:
                    _history = json.load(f)
                print(f"[history] restored from {os.path.basename(bp)}")
                break
            except Exception:
                continue
    _hist_saved_total = sum(len(v) for v in _history.values())


def _rotate_history_backup():
    """A safety copy of the history roughly every 6 hours (the last 24 kept) - copies only, the live file is never touched."""
    try:
        import shutil
        have = _history_backups()
        if os.path.exists(HISTORY_FILE) and os.path.getsize(HISTORY_FILE) > 50_000:
            if not have or time.time() - os.path.getmtime(have[0]) > 6 * 3600:
                shutil.copy2(HISTORY_FILE, HISTORY_FILE + ".auto_" + time.strftime("%Y%m%d_%H%M"))
                for old in _history_backups()[24:]:
                    os.remove(old)
    except Exception:
        pass


def _save_history():
    global _hist_saved_total
    try:
        with _hist_save_lock:
            total = sum(len(v) for v in _history.values())
            if _hist_saved_total >= 200 and total < 0.8 * _hist_saved_total:
                print(f"[history] REFUSING to save: {total} windows in memory vs {_hist_saved_total} already on disk (not overwriting a bigger history)")
                return
            os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
            _rotate_history_backup()
            tmp = f"{HISTORY_FILE}.{os.getpid()}.tmp"          # one temp file per process, never shared
            with open(tmp, "w") as f:
                json.dump(_history, f)
            os.replace(tmp, HISTORY_FILE)
            _hist_saved_total = total
    except Exception:
        pass  # persistence is a nice-to-have - never let it crash the poll loop


# in-flight lock persistence - added 2026-09-13 after a real report: a
# restart mid-window (which happens often while iterating on this file)
# used to silently wipe whatever call was already locked for the CURRENTLY
# OPEN window, since _locked only ever lived in memory. That window would
# either re-lock a possibly-different call from scratch, or go ungraded
# entirely if the poll never formed a new one before close - a real, quiet
# loss of exactly the thing being watched live. Now the in-flight lock is
# saved to disk continuously and restored on startup, but ONLY if it's
# fresh (same window still plausibly open) - a stale saved lock from a much
# older window (e.g. the PC was off overnight) gets discarded instead of
# being graded against today's live price, which would produce a bogus
# history entry.
LOCKED_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data_cache", "kalshi_dashboard_locked_state.json")
LOCKED_STATE_MAX_AGE_SECONDS = 20 * 60  # a bit more than one 15-min window, to allow for restart delay


def _load_locked_state():
    global _locked
    try:
        with open(LOCKED_STATE_FILE, "r") as f:
            saved = json.load(f)
        if time.time() - saved.get("saved_at", 0) <= LOCKED_STATE_MAX_AGE_SECONDS:
            _locked = saved.get("locked", {})
    except (FileNotFoundError, json.JSONDecodeError, TypeError, KeyError):
        pass  # no saved state, or too old/corrupt - start fresh, same as before this feature existed


def _save_locked_state():
    try:
        os.makedirs(os.path.dirname(LOCKED_STATE_FILE), exist_ok=True)
        tmp = LOCKED_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"saved_at": time.time(), "locked": _locked}, f)
        os.replace(tmp, LOCKED_STATE_FILE)
    except Exception:
        pass  # persistence is a nice-to-have - never let it crash the poll loop

BIAS_MIN_SAMPLES = 5     # don't correct anything until we've actually got enough graded calls
BIAS_WINDOW = 20         # only look at the most recent N graded calls - old mistakes shouldn't linger forever
BIAS_WEIGHT = 0.30       # dampened - nudge partway toward the observed bias, don't chase it fully
BIAS_CAP_FRACTION = 0.5  # never correct by more than half a typical 15min move - a sanity ceiling


BEST_HOURS_MIN_SAMPLE = 8  # don't show an hour until it has at least this many graded windows


def compute_hour_stats(asset_history):
    """Win rate by hour-of-day, parsed straight from each ticker (the HHMM
    embedded in a Kalshi ticker, e.g. ...-1815-15, is already in EDT - same
    timezone Kalshi's own close_time converts to). Returns {hour: (wins, n)}."""
    stats = {}
    for h in asset_history:
        if h.get("correct") is None:
            continue
        m = re.search(r"(\d{4})-\d{2}$", h.get("ticker", ""))
        if not m:
            continue
        hour = int(m.group(1)[:2])
        wins, n = stats.get(hour, (0, 0))
        stats[hour] = (wins + (1 if h["correct"] else 0), n + 1)
    return stats


def _fmt_hour_12(h):
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}{suffix}"


def best_hours_html(asset_history):
    """ALL 24 hours, chronological, small-font compact strip - shows the
    whole day at a glance instead of just top 3 (2026-09-12 per user
    request: 'put the overall hours on the chart... smaller font'). Colored
    up/down by win rate; hours with fewer than BEST_HOURS_MIN_SAMPLE graded
    windows are dimmed (still shown - not hidden, just visually flagged as
    too thin a sample to trust, same discipline as everywhere else here)."""
    stats = compute_hour_stats(asset_history)
    if not stats:
        return '<span class="flat">warming up...</span>'
    chips = []
    for hour in range(24):
        if hour not in stats:
            continue
        wins, n = stats[hour]
        pct = wins / n
        # thresholds set 2026-09-12 per user request: <65% red, 66-74% orange, 75%+ green
        css = "up" if pct >= 0.75 else ("warn" if pct >= 0.66 else "down")
        dim = "" if n >= BEST_HOURS_MIN_SAMPLE else "opacity:0.5;"
        chips.append(f'<span style="{dim}">{_fmt_hour_12(hour)} '
                     f'<span class="{css}">{pct:.0%}</span>({n})</span>')
    strip = " &middot; ".join(chips)
    return (f'<div style="font-size:12px; line-height:1.7; color:#8b949e;">{strip}'
            f'<br><span style="opacity:0.6;">dimmed = under {BEST_HOURS_MIN_SAMPLE} graded windows, EDT</span></div>')


def get_streak(asset_history):
    """Current win/loss streak from the most recent graded calls, walking
    backward. A 'no call' window is SKIPPED, not counted as a break - changed
    2026-09-14 per user request: sitting out a window isn't a loss, so it
    shouldn't end a streak either. Only an actual wrong call breaks it.
    Returns ('win'|'loss'|None, count)."""
    streak_correct = None
    count = 0
    for h in reversed(asset_history):
        if h["correct"] is None:
            continue
        if streak_correct is None:
            streak_correct = h["correct"]
            count = 1
        elif h["correct"] == streak_correct:
            count += 1
        else:
            break
    if streak_correct is None:
        return None, 0
    return ("win" if streak_correct else "loss"), count


def compute_cumulative_bet_pnl(asset_history, bet_size=100.0, series_prefix=None):
    """Real $100-per-call P&L using Kalshi's ACTUAL logged contract price at
    lock time (locked_kalshi_price) - only meaningful for real-market assets.
    Never call this on a synthetic asset's history - its 'locked_kalshi_price'
    is a self-computed proxy, not a real cost.

    XRP moved from synthetic (self-computed, ticker recorded as BTC's ticker
    since it was time-synced off BTC's window) to a real market (KXXRP15M,
    2026-08-31) - old and new history entries now share the same "XRP" key
    on disk. series_prefix, when given, excludes any history entry whose
    ticker doesn't start with the asset's OWN real series (i.e. drops
    pre-migration synthetic entries from the real-money P&L math without
    needing to touch/delete the saved history file itself)."""
    bets = [h for h in asset_history if h.get("locked_kalshi_price") is not None
            and (series_prefix is None or h.get("ticker", "").startswith(series_prefix))]
    if not bets:
        return None
    total = 0.0
    wins = 0
    for h in bets:
        kp = h["locked_kalshi_price"]
        if kp <= 0:
            continue
        contracts = bet_size / kp
        if h["correct"]:
            total += contracts - bet_size
            wins += 1
        else:
            total -= bet_size
    return {"total": total, "n": len(bets), "wins": wins, "bet_size": bet_size}


ET_ZONE = ZoneInfo("America/New_York")
from src import viewtz as _viewtz
from src import accounts as _acc, account_pages as _accp


def _parse_ticker_et_uncached(ticker):
    """A ticker's embedded open time as a timezone-aware Eastern datetime -
    Kalshi's own ticker naming is already Eastern (see compute_hour_stats),
    so no UTC conversion is needed, just attaching the right zone (handles
    EST/EDT correctly across the year, unlike a hardcoded -4 offset)."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{4})-", ticker or "")
    if not m:
        return None
    yr, mon, day, hm = m.groups()
    try:
        naive = dt.datetime.strptime(f"20{yr}-{mon}-{day} {hm}", "%Y-%b-%d %H%M")
        return naive.replace(tzinfo=ET_ZONE)
    except ValueError:
        return None


import functools as _functools
parse_ticker_et = _functools.lru_cache(maxsize=60000)(_parse_ticker_et_uncached)      # 2026-10-18: was ~1 second of CPU per page refresh


def compute_bet_pnl_since(asset_history, series_prefix, cutoff, bet_size=100.0):
    """Added 2026-09-15 per user request: same $100-per-call paper model as
    compute_cumulative_bet_pnl, but scoped to only the graded calls whose
    window opened at or after `cutoff` (a timezone-aware datetime) - the
    on-site 1h/24h/today up/down stats, next to the existing all-time total."""
    bets = []
    for h in asset_history:
        if h.get("locked_kalshi_price") is None or h.get("correct") is None:
            continue
        if series_prefix is not None and not h.get("ticker", "").startswith(series_prefix):
            continue
        t = parse_ticker_et(h.get("ticker"))
        if t is None or t < cutoff:
            continue
        bets.append(h)
    if not bets:
        return {"total": 0.0, "n": 0, "wins": 0, "pct": 0.0}
    total = 0.0
    wins = 0
    staked = 0.0
    for h in bets:
        kp = h["locked_kalshi_price"]
        if kp <= 0:
            continue
        contracts = bet_size / kp
        staked += bet_size
        if h["correct"]:
            total += contracts - bet_size
            wins += 1
        else:
            total -= bet_size
    pct = (total / staked * 100) if staked else 0.0
    return {"total": total, "n": len(bets), "wins": wins, "pct": pct}


def compute_windowed_bet_pnl(asset_history, series_prefix, window_seconds, bet_size=100.0):
    """Rolling last-window_seconds version of compute_bet_pnl_since (e.g. the
    last 1h or last 24h, always moving with "now")."""
    cutoff = dt.datetime.now(ET_ZONE) - dt.timedelta(seconds=window_seconds)
    return compute_bet_pnl_since(asset_history, series_prefix, cutoff, bet_size)


def get_bias_correction(asset_name, symbol):
    """Rolling, dampened, capped correction based on how far off our own past
    predictions have actually been (final price - predicted price). Only
    kicks in once there's a real sample to learn from, and can never grow
    large enough to dominate the underlying signal."""
    hist = [h for h in _history.get(asset_name, []) if h["price_error"] is not None]
    if len(hist) < BIAS_MIN_SAMPLES:
        return 0.0, len(hist)
    recent = hist[-BIAS_WINDOW:]
    avg_bias = statistics.mean(h["price_error"] for h in recent)
    correction = avg_bias * BIAS_WEIGHT
    cap = EMPIRICAL_MOVES.get(symbol, {}).get(15, {}).get("p50", 0) * BIAS_CAP_FRACTION
    if cap:
        correction = max(-cap, min(cap, correction))
    return correction, len(recent)

# true tick-by-tick live price, VOLUME-WEIGHTED ACROSS EXCHANGES - not the
# 1-min-candle price used for momentum/sweep math below, and not a single
# exchange's number either. Real tickers (CoinGecko, TradingView's aggregate,
# etc.) don't trust one venue - they blend live trades from multiple
# exchanges, weighted by how much size actually traded, so one thin/wonky
# print on one venue can't move the "true" price by itself. We do the same
# here with Coinbase + Kraken: each venue's most recent trade (price, size)
# is kept, and the displayed price is the size-weighted average of whichever
# venues currently have data.
_exchange_last = {}   # canonical_symbol -> {"coinbase": (price, size, last_update_ts), "kraken": (...)}
_live_ticks = {}       # canonical_symbol -> blended volume-weighted price
_live_ticks_ts = {}    # canonical_symbol -> when _live_ticks was last actually updated
_ticks_lock = threading.Lock()

# a silently-stalled websocket (connection technically open, but no new
# trades arriving - doesn't always trigger a hard error/reconnect) used to
# leave a frozen, increasingly wrong price in the blend forever, with no
# expiry. Found 2026-08-13 via a real grading bug: overnight ETH "final
# price" readings drifted $3-10 off the true price for stretches at a time.
# Two-layer fix: (1) a single venue whose OWN last trade is stale gets
# dropped from the blend so it can't drag down an otherwise-fine feed from
# the other venue, (2) if NEITHER venue has ticked recently, get_live_price
# returns None so the caller falls back to a fresh REST price instead of a
# frozen tick.
VENUE_STALE_SECONDS = 45
LIVE_PRICE_STALE_SECONDS = 30

COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
COINBASE_PRODUCTS = ["BTC-USD", "ETH-USD", "NEAR-USD", "BNB-USD", "XRP-USD"]

# Binance.com blocks US-based connections (tested, got a hard 451) - Kraken is
# the second venue instead: also top-tier USD liquidity, and US-accessible.
KRAKEN_WS_URL = "wss://ws.kraken.com"
KRAKEN_PAIRS = ["XBT/USD", "ETH/USD", "NEAR/USD", "BNB/USD", "XRP/USD"]
KRAKEN_TO_CANONICAL = {"XBT/USD": "BTC-USD", "ETH/USD": "ETH-USD", "NEAR/USD": "NEAR-USD", "BNB/USD": "BNB-USD",
                        "XRP/USD": "XRP-USD"}

_tick_history = {}  # canonical_symbol -> list of (timestamp, blended_price), last TICK_HISTORY_SECONDS kept
# extended 2026-09-13 from 90s to 20min per user request (real 30-second
# candle chart, built from these live ticks - not a REST API, since 30s
# isn't a standard kline granularity) - get_micro_trend etc. still only
# read the most recent slice of this, unaffected by the longer retention.
TICK_HISTORY_SECONDS = 20 * 60


def _update_blended(symbol):
    """Recompute the size-weighted average across whatever exchanges have a
    RECENT trade for this symbol - a venue whose last trade is older than
    VENUE_STALE_SECONDS is excluded, so a silently-stalled feed can't drag
    down an otherwise-healthy blend. Must be called while holding
    _ticks_lock."""
    venues = _exchange_last.get(symbol)
    if not venues:
        return
    now = time.time()
    fresh = {v: (price, size) for v, (price, size, ts) in venues.items() if now - ts <= VENUE_STALE_SECONDS}
    if not fresh:
        return  # nothing recent from any venue - leave _live_ticks as-is; get_live_price's own staleness check handles the rest
    total_size = sum(size for _, size in fresh.values())
    if total_size <= 0:
        return
    blended = sum(price * size for price, size in fresh.values()) / total_size
    _live_ticks[symbol] = blended
    _live_ticks_ts[symbol] = now
    hist = _tick_history.setdefault(symbol, [])
    hist.append((now, blended))
    cutoff = now - TICK_HISTORY_SECONDS
    while hist and hist[0][0] < cutoff:
        hist.pop(0)


# --- whale trade tracking (added 2026-09-10 per user request) -------------
# Watches the SAME live Coinbase/Kraken trade feeds already used for the
# blended price, and keeps just the big ones - individual trades above a
# per-asset USD-notional threshold - in a rolling 5-minute window. Lets the
# dashboard show recent whale buy vs. sell pressure, and fire a distinct
# "whale" sound when a really big single trade lands. This is TRADES THAT
# ACTUALLY EXECUTED (the tape), not resting order-book depth - seeing big
# resting orders "sitting on the line" needs a level-2 book feed this
# project doesn't subscribe to yet.
_whale_recent = {}  # canonical_symbol -> [(ts, usd, side, venue)] last 10 min - for the "who just bought $2M" lines under the whale row
_whale_trades = {}  # canonical_symbol -> list of (ts, usd_notional, aggressor_side "buy"/"sell")
# extended 2026-09-13 from 5min to 1hr so whale-trade markers can be drawn
# on the candle chart across all its timeframes (15m/30m/1h) - the flow
# SUMMARY row still only looks at the most recent 5 minutes of this,
# unaffected by the longer retention.
WHALE_HISTORY_SECONDS = 3600
# "counts as a whale" - shows in the flow row
# lowered 2026-09-30 per user request ("we're definitely getting some whales
# that aren't being called... the cells we're pinging aren't big enough") -
# the real buy/sell volume tug-of-war added the same day showed plenty of
# genuine trade flow (tens of thousands of $ per 30s) while this threshold
# was requiring a SINGLE print of $100k+ on BTC to count as a whale at all,
# which public Coinbase/Kraken trade prints rarely hit in one line - most
# real size arrives sliced into smaller prints. Cut to roughly 1/4 across
# the board so genuinely large prints actually get flagged.
WHALE_MIN_USD = {"BTC-USD": 25_000, "ETH-USD": 12_500, "XRP-USD": 6_000,
                  "NEAR-USD": 4_000, "BNB-USD": 6_000}
DEFAULT_WHALE_MIN_USD = 12_500
# "huge - make a sound" (bigger than the display threshold) - kept at roughly
# the same 5x multiple over the display threshold as before, just scaled
# down with it, so the loud alert still means "rare and big," not "every
# whale row entry."
WHALE_ALERT_USD = {"BTC-USD": 125_000, "ETH-USD": 60_000, "XRP-USD": 30_000,
                    "NEAR-USD": 20_000, "BNB-USD": 30_000}
DEFAULT_WHALE_ALERT_USD = 60_000
# the two dicts above are now only a FALLBACK (see get_adaptive_whale_thresholds
# below) used until there's enough recent trade history to compute a real one.
_last_whale_alert_ts = {}  # canonical_symbol -> ts of the last alert-worthy whale we already flagged

_trade_notionals = {}  # symbol -> list of (ts, usd_notional), ALL trades regardless of size,
                         # long retention - added 2026-09-30 per user request ("the volume
                         # should be different depending on what kind of market we're in") so
                         # the whale bar can be computed RELATIVE to how much is actually
                         # trading in this specific market right now, instead of one fixed
                         # dollar figure for every condition. Deliberately separate from
                         # _recent_volume (short, "right now" window for the tug-of-war/volume
                         # display) and _whale_trades (already whale-filtered, so useless for
                         # measuring what's "typical" here).
WHALE_BASELINE_WINDOW_SECONDS = 900  # 15min - long enough for a stable read of "typical" trade
                                       # size, short enough to actually adapt across a session
                                       # (dead overnight vs. a live news spike)
WHALE_BASELINE_MIN_SAMPLES = 40  # below this, not enough data for a reliable percentile -
                                   # fall back to the static WHALE_MIN_USD/WHALE_ALERT_USD above
WHALE_DISPLAY_PERCENTILE = 99    # "whale" = bigger than 99% of recent trades in THIS market (all exchanges pooled -
                                  # was 95 / 99 when only Coinbase + Kraken fed it; ~330 prints per 5 min on BTC at 95)
WHALE_ALERT_PERCENTILE = 99.9    # "huge, make a sound" = top 0.1% of recent trades (~2 per 5 min on BTC)
WHALE_ALERT_FLOOR_USD = {"BTC-USD": 100_000, "XRP-USD": 25_000}
WHALE_FLOOR_USD = {"BTC-USD": 10_000, "ETH-USD": 2_500, "XRP-USD": 2_500,
                    "NEAR-USD": 750, "BNB-USD": 1_000}
DEFAULT_WHALE_FLOOR_USD = 1_000  # sanity floor - even in a dead market, don't call a $20
                                   # trade a "whale" just because everything else is even smaller


def _record_trade_notional(symbol, notional):
    """Call while holding _ticks_lock, alongside _record_whale_trade/_record_trade_volume."""
    now = time.time()
    lst = _trade_notionals.setdefault(symbol, [])
    lst.append((now, notional))
    cutoff = now - WHALE_BASELINE_WINDOW_SECONDS
    while lst and lst[0][0] < cutoff:
        lst.pop(0)


def _percentile(sorted_values, pct):
    """Linear-interpolation percentile - no numpy dependency. sorted_values must already be sorted."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (pct / 100) * (len(sorted_values) - 1)
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


_whale_thresh_cache = {}


def _adaptive_whale_thresholds_locked(symbol):
    """Same as get_adaptive_whale_thresholds, but assumes the CALLER already
    holds _ticks_lock - _ticks_lock is a plain (non-reentrant) Lock, so this
    version exists specifically for callers already inside a `with
    _ticks_lock:` block (_record_whale_trade, get_whale_flow), to avoid a
    self-deadlock."""
    now = time.time()
    hit = _whale_thresh_cache.get(symbol)
    if hit and now - hit[0] < 2.0:          # sorting ~20k prints on every trade would be wasteful
        return hit[1]
    sizes = sorted(n for ts, n in _trade_notionals.get(symbol, []) if now - ts <= WHALE_BASELINE_WINDOW_SECONDS)
    floor = WHALE_FLOOR_USD.get(symbol, DEFAULT_WHALE_FLOOR_USD)
    if len(sizes) < WHALE_BASELINE_MIN_SAMPLES:
        res = (max(WHALE_MIN_USD.get(symbol, DEFAULT_WHALE_MIN_USD), floor),
               max(WHALE_ALERT_USD.get(symbol, DEFAULT_WHALE_ALERT_USD), floor))
    else:
        display = max(_percentile(sizes, WHALE_DISPLAY_PERCENTILE), floor)
        alert = max(_percentile(sizes, WHALE_ALERT_PERCENTILE), floor, display, WHALE_ALERT_FLOOR_USD.get(symbol, 0))
        res = (display, alert)
    _whale_thresh_cache[symbol] = (now, res)
    return res


def get_adaptive_whale_thresholds(symbol):
    """The (display_threshold, alert_threshold) dollar figures for THIS
    symbol right now, based on the actual distribution of trade sizes over
    the last WHALE_BASELINE_WINDOW_SECONDS - added 2026-09-30 per user
    request ('the volume should be different depending on what kind of
    market we're in'). Falls back to the static WHALE_MIN_USD/WHALE_ALERT_USD
    defaults when there isn't enough recent trade history yet (e.g. right
    after startup). For callers that do NOT already hold _ticks_lock - see
    _adaptive_whale_thresholds_locked for the internal, lock-free version."""
    with _ticks_lock:
        return _adaptive_whale_thresholds_locked(symbol)


_WHALE_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot_state", "whale_log.csv")
_whale_log_lock = threading.Lock()


def _log_whale(symbol, venue, aggressor, usd, price, threshold):
    """Every whale-sized print, all exchanges, kept forever so 'do whale sizes matter' can be back-tested later."""
    try:
        with _whale_log_lock:
            new = not os.path.exists(_WHALE_LOG)
            with open(_WHALE_LOG, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["ts_utc", "symbol", "venue", "side", "usd", "price", "threshold_usd"])
                w.writerow([dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3], symbol, venue, aggressor,
                            f"{usd:.0f}", f"{price:.6g}", f"{threshold:.0f}"])
    except Exception:
        pass


def _flow_to_legacy(venue, asset, usd, side, price):
    """Hook from the multi-exchange hub: every OTHER exchange's trades also feed the whale / volume recorders, so those rows
    and the whale alert see the whole market. (Coinbase + Kraken already record themselves, and their handlers hold the lock.)"""
    if venue in ("coinbase", "kraken") or not price or usd <= 0:
        return
    symbol = f"{asset}-USD"
    size = usd / price
    with _ticks_lock:
        _record_trade_notional(symbol, usd)
        _record_whale_trade(symbol, price, size, side, venue=venue)
        _record_trade_volume(symbol, price, size, side)


def _rsi_all_exchanges(symbol, fallback):
    """(rsi, source) - the composite RSI across every exchange when it has enough history, else the Coinbase one."""
    try:
        v = _flow.HUB.composite_rsi(symbol.split("-")[0])
    except Exception:
        v = None
    return (v, "all exchanges") if v is not None else (fallback, "Coinbase only - warming up")


def _record_whale_trade(symbol, price, size, aggressor, venue=""):
    """Call while holding _ticks_lock. Keeps only whale-sized trades - the
    bar for "whale-sized" is adaptive per symbol/market conditions, see
    get_adaptive_whale_thresholds."""
    notional = price * size
    display_threshold, _ = _adaptive_whale_thresholds_locked(symbol)
    if notional < display_threshold:
        return
    now = time.time()
    _log_whale(symbol, venue or "?", aggressor, notional, price, display_threshold)
    rl = _whale_recent.setdefault(symbol, [])
    rl.append((now, notional, aggressor, venue or "?"))
    while rl and rl[0][0] < now - 600:
        rl.pop(0)
    lst = _whale_trades.setdefault(symbol, [])
    lst.append((now, notional, aggressor))
    cutoff = now - WHALE_HISTORY_SECONDS
    while lst and lst[0][0] < cutoff:
        lst.pop(0)


_recent_volume = {}  # symbol -> list of (ts, usd_notional, aggressor_side "buy"/"sell"), EVERY trade regardless of size
RECENT_VOLUME_WINDOW_SECONDS = 30  # "in the moment" - added 2026-09-26 per user request
                                     # ("the in time volume in the very moment... instead of
                                     # the 24-hour volume", which barely moves poll to poll).
                                     # Short and matches the existing "Right now (last 30s)"
                                     # micro-momentum row's own window, for a consistent sense
                                     # of what "right now" means across this dashboard.


def _record_trade_volume(symbol, price, size, aggressor):
    """Call while holding _ticks_lock, alongside _record_whale_trade. Unlike
    that function, this keeps EVERY trade (not just whale-sized ones) but
    only for a short rolling window - this is what actually answers 'how
    much is trading right now,' where _record_whale_trade answers 'were any
    huge individual prints part of that.' aggressor ("buy"/"sell") added
    2026-09-30 per user request for a live buy-vs-sell "tug of war" read -
    see get_volume_tug_of_war."""
    notional = price * size
    now = time.time()
    lst = _recent_volume.setdefault(symbol, [])
    lst.append((now, notional, aggressor))
    cutoff = now - RECENT_VOLUME_WINDOW_SECONDS
    while lst and lst[0][0] < cutoff:
        lst.pop(0)


def get_recent_volume(symbol, window_seconds=RECENT_VOLUME_WINDOW_SECONDS):
    """$ volume that ACTUALLY traded in the last window_seconds - every
    trade size counted, a genuinely live read of current activity rather
    than a slow-moving 24h aggregate."""
    now = time.time()
    with _ticks_lock:
        trades = [t for t in _recent_volume.get(symbol, []) if now - t[0] <= window_seconds]
    return {"usd": sum(n for _, n, _ in trades), "n": len(trades)}


def get_volume_tug_of_war(symbol, window_seconds=RECENT_VOLUME_WINDOW_SECONDS):
    """Buy vs sell $ volume ('tug of war') over the last window_seconds -
    added 2026-09-30 per user request ('which side is getting favored, right
    now'). Same underlying trade stream as get_recent_volume (every real
    Coinbase+Kraken trade, not just whale-sized), just split by aggressor
    side instead of summed. buy_pct is None when there's no volume at all in
    the window (nothing to show a tug of war between)."""
    now = time.time()
    with _ticks_lock:
        trades = [t for t in _recent_volume.get(symbol, []) if now - t[0] <= window_seconds]
    buy_usd = sum(n for _, n, side in trades if side == "buy")
    sell_usd = sum(n for _, n, side in trades if side == "sell")
    total = buy_usd + sell_usd
    buy_pct = (buy_usd / total) if total > 0 else None
    return {"buy_usd": buy_usd, "sell_usd": sell_usd, "buy_pct": buy_pct}


WHALE_FLOW_SUMMARY_SECONDS = 300  # the "last 5min" summary row window - separate from
                                    # WHALE_HISTORY_SECONDS (how long raw trades are KEPT,
                                    # now 1hr so the chart can draw markers across all its
                                    # timeframes) so extending retention didn't silently
                                    # widen this display's "5min" claim too.


def get_whale_flow(symbol, window_seconds=WHALE_FLOW_SUMMARY_SECONDS):
    """Summary of whale trades in the window + whether a 'huge' one just
    landed (edge-triggered: alert_side is set once per big trade, then None
    on subsequent polls)."""
    now = time.time()
    with _ticks_lock:
        trades = [t for t in _whale_trades.get(symbol, []) if now - t[0] <= window_seconds]
        recent_all = [t for t in _whale_recent.get(symbol, []) if now - t[0] <= window_seconds]
        display_threshold, alert_threshold = _adaptive_whale_thresholds_locked(symbol)
        alert_side = None
        alert_ts = None
        alert_usd = None
        alert_venue = ""
        huge = [t for t in trades if t[1] >= alert_threshold and now - t[0] <= 12]
        if huge:
            newest = max(huge, key=lambda t: t[0])
            if newest[0] > _last_whale_alert_ts.get(symbol, 0):
                _last_whale_alert_ts[symbol] = newest[0]
                alert_side = newest[2]
                alert_usd = newest[1]
                alert_venue = next((r[3] for r in recent_all if abs(r[0] - newest[0]) < 1e-6 and r[1] == newest[1]), "")
                alert_ts = newest[0]  # the actual trade's timestamp - added 2026-09-25 so the
                                        # client can de-dupe by the real EVENT, not by how many
                                        # /data polls happen to land while this snapshot is still
                                        # the latest one (poll_loop updates it every ~2s but the
                                        # client refreshes every ~1s, so without a stable per-event
                                        # key the same trade got announced multiple times - a real
                                        # user report: "repeated himself four times for one buy")
    buy_usd = sum(n for _, n, s in trades if s == "buy")
    sell_usd = sum(n for _, n, s in trades if s == "sell")
    return {
        "buy_usd": buy_usd, "sell_usd": sell_usd,
        "buy_n": sum(1 for _, _, s in trades if s == "buy"),
        "sell_n": sum(1 for _, _, s in trades if s == "sell"),
        "biggest_usd": max((t[1] for t in trades), default=0),
        "net_side": "buy" if buy_usd > sell_usd else ("sell" if sell_usd > buy_usd else "flat"),
        "alert_side": alert_side,
        "alert_ts": alert_ts,
        "alert_usd": alert_usd,
        "alert_venue": alert_venue,
        "recent": sorted(recent_all, key=lambda r: -r[0])[:3],
        "biggest_whale": max(recent_all, key=lambda r: r[1]) if recent_all else None,
        "display_threshold": display_threshold,
    }


def get_whale_trades_raw(symbol, window_seconds=WHALE_HISTORY_SECONDS):
    """Raw (ts, usd_notional, side) whale trades for the chart's tick-marker
    row / live threshold feed - not the summary aggregate get_whale_flow
    returns, the individual events, aggr.trade-style."""
    now = time.time()
    with _ticks_lock:
        return [t for t in _whale_trades.get(symbol, []) if now - t[0] <= window_seconds]


def _coinbase_on_message(ws, message):
    data = json.loads(message)
    if data.get("type") != "match":
        return
    product_id = data.get("product_id")
    price = data.get("price")
    size = data.get("size")
    if product_id and price and size:
        # Coinbase 'side' is the MAKER side: side="sell" means a resting sell
        # was hit -> an aggressive BUY (uptick); side="buy" -> aggressive SELL.
        aggressor = "buy" if data.get("side") == "sell" else "sell"
        with _ticks_lock:
            _exchange_last.setdefault(product_id, {})["coinbase"] = (float(price), float(size), time.time())
            _update_blended(product_id)
            _record_trade_notional(product_id, float(price) * float(size))
            _record_whale_trade(product_id, float(price), float(size), aggressor, venue="coinbase")
            _record_trade_volume(product_id, float(price), float(size), aggressor)
            _flow_record("coinbase", product_id, float(price), float(size), aggressor)


def _kraken_on_message(ws, message):
    try:
        payload = json.loads(message)
        # trade messages are arrays: [channelID, [[price, volume, time, side, ...], ...], "trade", "XBT/USD"]
        if not isinstance(payload, list) or len(payload) < 4 or payload[2] != "trade":
            return
        pair = payload[3]
        canonical = KRAKEN_TO_CANONICAL.get(pair)
        trades = payload[1]
        if not canonical or not trades:
            return
        last_price, last_qty = trades[-1][0], trades[-1][1]
        with _ticks_lock:
            _exchange_last.setdefault(canonical, {})["kraken"] = (float(last_price), float(last_qty), time.time())
            _update_blended(canonical)
            for tr in trades:
                # kraken trade: [price, volume, time, side ("b"/"s"), ...] - side IS the taker/aggressor side
                aggressor = "buy" if len(tr) > 3 and tr[3] == "b" else "sell"
                _record_trade_notional(canonical, float(tr[0]) * float(tr[1]))
                _record_whale_trade(canonical, float(tr[0]), float(tr[1]), aggressor, venue="kraken")
                _record_trade_volume(canonical, float(tr[0]), float(tr[1]), aggressor)
                _flow_record("kraken", canonical, float(tr[0]), float(tr[1]), aggressor)
    except Exception:
        pass


def get_tick_history(symbol):
    """Raw (timestamp, price) tick list for the candle chart - a plain
    thread-safe copy, oldest first."""
    with _ticks_lock:
        return list(_tick_history.get(symbol, []))


def get_micro_trend(product_id, window_seconds):
    """Real-time direction over the last `window_seconds` of actual trades -
    NOT a prediction of where it ends up, just what's happening right now,
    as fast as the tick data allows. Returns ('up'|'down'|'flat', pct_change)."""
    with _ticks_lock:
        hist = list(_tick_history.get(product_id, []))
    if len(hist) < 2:
        return None, None
    now = hist[-1][0]
    cutoff = now - window_seconds
    windowed = [p for t, p in hist if t >= cutoff]
    if len(windowed) < 2:
        return None, None
    start, end = windowed[0], windowed[-1]
    if start == 0:
        return None, None
    pct = (end - start) / start
    if pct > 0.00003:
        return "up", pct
    if pct < -0.00003:
        return "down", pct
    return "flat", pct


FAIR_VALUE_VOL_LOOKBACK_SECONDS = 300
FAIR_VALUE_BUCKET_SECONDS = 5
FAIR_VALUE_SETTLE_WINDOW_SECONDS = 60  # Kalshi settles on the final 60s AVERAGE


def fair_yes_probability(symbol, spot, target, seconds_left):
    """(P(YES), sigma in bps/min) from spot vs strike and recent realized
    volatility - ported 2026-10-03 from the friend's (GiMMiiXx) bot,
    fair_value.py. Independent of Kalshi's own price, so it can catch the
    book being wrong/stale. Treats the final settlement as the 60s average
    (variance horizon remaining - 60 + 60/3). Backtest on our own log: its
    Brier score beat Kalshi's price slightly on both BTC and XRP and a 50/50
    blend beat both. Logged only for now - not a trading rule yet."""
    if not spot or not target or seconds_left is None:
        return None, None
    hist = get_tick_history(symbol)
    if len(hist) < 2:
        return None, None
    now = hist[-1][0]
    closes = {}
    for t, p in hist:
        if t >= now - FAIR_VALUE_VOL_LOOKBACK_SECONDS and p > 0:
            closes[int(t // FAIR_VALUE_BUCKET_SECONDS)] = p
    keys = sorted(closes)
    rets = [math.log(closes[b] / closes[a]) for a, b in zip(keys, keys[1:]) if b == a + 1]
    lo, hi = 1 / 1e4 / math.sqrt(60), 80 / 1e4 / math.sqrt(60)
    if len(rets) < 12:
        sigma = 8 / 1e4 / math.sqrt(60)
    else:
        sigma = min(hi, max(lo, statistics.pstdev(rets) / math.sqrt(FAIR_VALUE_BUCKET_SECONDS)))
    window = FAIR_VALUE_SETTLE_WINDOW_SECONDS
    remaining = max(0.0, float(seconds_left))
    horizon = remaining - window + window / 3 if remaining >= window else remaining ** 3 / (3 * window ** 2)
    sigma_bps_min = sigma * 1e4 * math.sqrt(60)
    if horizon <= 0:
        return (0.995 if spot >= target else 0.005), sigma_bps_min
    z = math.log(spot / target) / (sigma * math.sqrt(horizon))
    return min(0.995, max(0.005, 0.5 * (1 + math.erf(z / math.sqrt(2))))), sigma_bps_min


DRAMATIC_SWING_WINDOW_SECONDS = 60
DRAMATIC_SWING_FRACTION = 0.5  # firing threshold: this window's move covers
                                 # at least half of what's normally a RARE
                                 # (p90) 5-minute move - i.e. genuinely fast
                                 # and unusual, not everyday noise


def flip_chance_vs_locked_call(kalshi_price, locked_call, confidence):
    """Real bug found 2026-09-25: probability the FINAL result ends up
    OPPOSITE the locked call - NOT "chance the CURRENT leaning flips again"
    (the old 1-confidence formula), which quietly INVERTS and becomes
    misleadingly LOW exactly when the market has already moved hard against
    the locked call. Root cause: confidence = max(kalshi_price, 1-kalshi_price)
    always tracks whichever side is CURRENTLY favored, not the locked call -
    once price crosses 50% past the locked call, confidence starts measuring
    the NEW (opposite) side, so 1-confidence shrinks toward 0 exactly as the
    real risk to the locked call climbs toward 100%. Caught from a real XRP
    loss where kalshi_price collapsed to 0.009 against a locked YES call
    (~99% real risk) while the old formula read flip_chance as 0.9% - so the
    "for sure" alert (and this same math feeding the "Chance of a flip"
    gauge) never fired when it mattered most. Falls back to 1-confidence
    before any call has locked yet, since there's no "original call" to
    measure risk against at that point."""
    if locked_call == "YES":
        return (1 - kalshi_price) if kalshi_price is not None else None
    if locked_call == "NO":
        return kalshi_price
    return (1 - confidence) if confidence is not None else None


def get_dramatic_swing(symbol, current_price):
    """A real 'boom' alert, not a soft trend read - fires only when price has
    moved, in just the last 60 seconds, more than half of what typically
    takes a full 5 minutes to happen (the empirical p90/rare-move benchmark
    for that symbol). Returns 'up', 'down', or None."""
    if current_price is None:
        return None
    direction, pct = get_micro_trend(symbol, DRAMATIC_SWING_WINDOW_SECONDS)
    if direction not in ("up", "down") or pct is None:
        return None
    p90_5min = EMPIRICAL_MOVES.get(symbol, {}).get(5, {}).get("p90")
    if not p90_5min or current_price == 0:
        return None
    threshold_pct = (p90_5min / current_price) * DRAMATIC_SWING_FRACTION
    return direction if abs(pct) >= threshold_pct else None


def _current_swing_pct(symbol):
    """Just the magnitude behind get_dramatic_swing's direction - added
    2026-09-25 as a separate lookup (not a shared return value) so it
    doesn't touch get_dramatic_swing's three existing call sites. Only
    called when actually logging a flip-scalp entry, not every poll, so the
    extra get_micro_trend lookup costs nothing that matters."""
    _, pct = get_micro_trend(symbol, DRAMATIC_SWING_WINDOW_SECONDS)
    return pct


def _coinbase_on_open(ws):
    ws.send(json.dumps({"type": "subscribe", "product_ids": COINBASE_PRODUCTS, "channels": ["matches"]}))


def _coinbase_ws_run_forever():
    while True:
        try:
            ws = websocket.WebSocketApp(COINBASE_WS_URL, on_open=_coinbase_on_open, on_message=_coinbase_on_message)
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception:
            pass
        time.sleep(3)


def _kraken_on_open(ws):
    ws.send(json.dumps({"event": "subscribe", "pair": KRAKEN_PAIRS, "subscription": {"name": "trade"}}))


def _kraken_ws_run_forever():
    while True:
        try:
            ws = websocket.WebSocketApp(KRAKEN_WS_URL, on_open=_kraken_on_open, on_message=_kraken_on_message)
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception:
            pass
        time.sleep(3)


# --- live order-book depth / "whale walls" (added 2026-09-10) --------------
# Coinbase's public level2_batch feed (no auth) gives the full resting order
# book: a 'snapshot' then incremental 'l2update' changes. Kept per symbol so
# the dashboard can flag LARGE resting orders ("walls") sitting near the
# price - a big ask wall just above price is a whale lined up to sell (get
# out ahead of it); a big bid wall just below is a whale lined up to buy.
# This is resting DEPTH, distinct from the whale-TRADE tape above (which is
# fills that already happened).
_order_books = {}  # symbol -> {"bids": {price(float): size(float)}, "asks": {...}}
_book_lock = threading.Lock()
BOOK_WALL_RANGE_PCT = 0.004      # only look at levels within +/-0.4% of price
BOOK_WALL_MIN_USD = {"BTC-USD": 400_000, "ETH-USD": 200_000, "XRP-USD": 80_000,
                      "NEAR-USD": 40_000, "BNB-USD": 80_000}
DEFAULT_BOOK_WALL_MIN_USD = 150_000
BOOK_WALL_ALERT_PCT = 0.0012     # a wall THIS close to price (0.12%) is alert-worthy
_last_book_alert = {}            # (symbol, side) -> (price, time of the alert, time last seen) of the wall we last alerted on
BOOK_ALERT_COOLDOWN = 90         # seconds before a DIFFERENT wall on the same side may alert again
BOOK_ALERT_SAME_WALL_PCT = 0.0005   # a wall within 0.05% of the one we already alerted on is the same wall
BOOK_ALERT_FORGET_SECONDS = 120  # a wall that has been gone this long may alert again when it comes back


def _coinbase_l2_on_open(ws):
    ws.send(json.dumps({"type": "subscribe", "product_ids": COINBASE_PRODUCTS, "channels": ["level2_batch"]}))


def _coinbase_l2_on_message(ws, message):
    data = json.loads(message)
    mtype = data.get("type")
    if mtype == "snapshot":
        pid = data.get("product_id")
        if not pid:
            return
        bids = {float(p): float(s) for p, s in data.get("bids", []) if float(s) > 0}
        asks = {float(p): float(s) for p, s in data.get("asks", []) if float(s) > 0}
        with _book_lock:
            _order_books[pid] = {"bids": bids, "asks": asks}
    elif mtype == "l2update":
        pid = data.get("product_id")
        with _book_lock:
            book = _order_books.get(pid)
            if book is None:
                return
            for change in data.get("changes", []):
                if len(change) != 3:
                    continue
                side, price, size = change
                price, size = float(price), float(size)
                side_book = book["bids"] if side == "buy" else book["asks"]
                if size <= 0:
                    side_book.pop(price, None)
                else:
                    side_book[price] = size


def _coinbase_l2_run_forever():
    while True:
        try:
            ws = websocket.WebSocketApp(COINBASE_WS_URL, on_open=_coinbase_l2_on_open,
                                         on_message=_coinbase_l2_on_message)
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception:
            pass
        time.sleep(3)


def _biggest_wall(levels, ref_price, want_above):
    """Biggest single resting level (by USD notional) within the wall range,
    on the requested side of ref_price. Returns (price, usd, dist_pct) or None."""
    threshold_lo = ref_price * (1 - BOOK_WALL_RANGE_PCT)
    threshold_hi = ref_price * (1 + BOOK_WALL_RANGE_PCT)
    best = None
    for price, size in levels.items():
        if want_above and not (ref_price < price <= threshold_hi):
            continue
        if not want_above and not (threshold_lo <= price < ref_price):
            continue
        usd = price * size
        if best is None or usd > best[1]:
            best = (price, usd, abs(price - ref_price) / ref_price)
    return best


BOOK_WALL_ALL_MIN_USD = {"BTC-USD": 2_500_000, "XRP-USD": 600_000}
BOOK_WALL_ALL_ALERT_USD = {"BTC-USD": 5_000_000, "XRP-USD": 1_000_000}   # an all-exchange wall must be at least this big to RING (it still shows in the row)
BOOK_WALL_ALL_CONTRAST = 2.5       # a wall zone must hold at least this many times the average of the zones around it
BOOK_WALL_ALL_NEIGH = 4            # zones on each side that make up 'around it' (zones are 1.5 basis points wide)


def _venue_label(v):
    try:
        return _flow.VENUES.get(v, (v, ""))[0]
    except Exception:
        return v


_wall_hold = {}                    # (symbol, side) -> the wall that is on screen
WALL_HOLD_SECONDS = 20             # a wall that stops qualifying stays on screen this long before it is dropped
WALL_YOUNG_SECONDS = 8             # a wall that has only just appeared is not replaced by a not-clearly-bigger one


def _hold_wall(symbol, side, new, new_src, ref):
    """Keeps the wall on screen steady (2026-10-14: it used to change every second). The same wall (about the same price) keeps its price and only its size
    updates; a brand-new one has to be clearly bigger to replace a young one; one that stops qualifying lingers for a few seconds."""
    now = time.time()
    k = (symbol, side)
    h = _wall_hold.get(k)
    res = None
    if new is not None:
        if h and abs(new[0] - h["p"]) / new[0] <= 0.0006:
            h.update(usd=new[1], src=new_src, seen=now)                      # same wall: pin its price, take the fresh size
        elif h and now - h["since"] < WALL_YOUNG_SECONDS and new[1] < 1.3 * h["usd"] and now - h["seen"] < WALL_YOUNG_SECONDS:
            pass                                                              # a young wall is kept unless the newcomer is clearly bigger
        else:
            h = {"p": new[0], "usd": new[1], "src": new_src, "since": now, "seen": now}
            _wall_hold[k] = h
    elif h and now - h["seen"] > WALL_HOLD_SECONDS:
        _wall_hold.pop(k, None)
        h = None
    if h:
        dist = abs(h["p"] - ref) / ref
        if dist > BOOK_WALL_RANGE_PCT or (side == "bid" and h["p"] >= ref) or (side == "ask" and h["p"] <= ref):
            _wall_hold.pop(k, None)                                           # price moved through it (or it left the window)
        else:
            res = (h["p"], h["usd"], dist)
    return res, (h["src"] if res else [])


def _all_exchange_walls(symbol, ref_price):
    """Big RESTING orders merged across EVERY exchange with a fresh order book (not just Coinbase). Dense depth right at the price is normal, so a zone only
    counts as a wall if it is big in dollars AND stands out from the zones next to it. -> {"n": exchanges used, "bid"/"ask": (price, usd, dist) or None,
    "bid_src"/"ask_src": [(venue, usd), top 3]} or None when too few books are fresh (the caller falls back to Coinbase only)."""
    asset = {"BTC-USD": "BTC", "XRP-USD": "XRP"}.get(symbol)
    if asset is None or asset not in _flow.ASSETS or not ref_price:
        return None
    wm = _flow.HUB.wall_map(asset, ref_price)
    if not wm or wm["n"] < 5:
        return None
    zone = wm["zone"]
    min_usd = BOOK_WALL_ALL_MIN_USD.get(symbol, 1_000_000)
    out = {"n": wm["n"], "bid": None, "ask": None, "bid_src": [], "ask_src": []}
    for side, key in (("bid", "bids"), ("ask", "asks")):
        zones = wm[key]
        best = None
        for z, usd in zones.items():
            dist = abs(z - ref_price) / ref_price
            if dist <= 0 or dist > BOOK_WALL_RANGE_PCT or usd < min_usd:
                continue
            neigh = []
            for k in range(-BOOK_WALL_ALL_NEIGH, BOOK_WALL_ALL_NEIGH + 1):
                if k == 0:
                    continue
                zn = round(round((z + k * zone) / zone) * zone, 8)
                if (side == "bid" and zn >= ref_price) or (side == "ask" and zn <= ref_price):
                    continue                                 # the other side of the price is not 'around it'
                neigh.append(zones.get(zn, 0.0))
            avg = (sum(neigh) / len(neigh)) if neigh else 0.0
            if usd >= BOOK_WALL_ALL_CONTRAST * max(avg, 1.0) and (best is None or usd > best[1]):
                best = (z, usd, dist)
        src = sorted(wm["by"].get((side, best[0]), {}).items(), key=lambda kv: -kv[1])[:3] if best else []
        best, src = _hold_wall(symbol, side, best, src, ref_price)
        out[side] = best
        out[side + "_src"] = src
    return out


# ---- Bookmap bridge (2026-10-14): the little "Kalshi Dashboard Bridge" add-on running INSIDE Bookmap posts a summary of its order book + trades here once a
# second (localhost only). Kept in memory for the heat map panel and logged to bot_state/bookmap_log.csv. Information only - nothing here feeds a call or a trade.
_bm_lock = threading.Lock()
_bookmap = {a: {"last": None, "hist": __import__("collections").deque(maxlen=3700), "n": 0, "log_ts": 0.0, "buy": 0.0, "sell": 0.0} for a in ("BTC", "XRP")}
BM_ZONE = {"BTC": 10.0, "XRP": 0.0002}
BM_BAND = 0.004
_BM_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot_state", "bookmap_log.csv")
BM_HELP = ("In Bookmap: Settings > Configure add-ons > Add... > choose C:/Users/Natey/projects/crypto-15m-predictor/bookmap_addon/KalshiBridge.jar > "
           "make sure 'Kalshi Dashboard Bridge' is loaded / ticked.")


_own_heat = {a: __import__("collections").deque(maxlen=3700) for a in ("BTC", "XRP")}
_OWN_SYMBOL = {"BTC": "BTC-USD", "XRP": "XRP-USD"}


def _own_heat_loop():
    """Every 2 seconds: the resting dollars near the price, merged across every exchange we have a book for, kept for ~2 hours. This is the heat map's
    data when Bookmap's add-on cannot read the feed (BookmapData blocks add-ons)."""
    while True:
        time.sleep(2)
        for a, sym in _OWN_SYMBOL.items():
            try:
                px = get_live_price(sym)
                if not px:
                    continue
                wm = _flow.HUB.wall_map(a, px)
                if not wm or wm["n"] < 3:
                    continue
                zones = {}
                for key in ("bids", "asks"):
                    for z, usd in wm[key].items():
                        if abs(z - px) / px <= BM_BAND:
                            zones[z] = zones.get(z, 0.0) + usd
                with _bm_lock:
                    _own_heat[a].append((time.time(), px, zones, wm["n"]))
            except Exception:
                pass


def bookmap_ingest(pl):
    alias = str(pl.get("alias", ""))
    asset = "BTC" if alias.upper().startswith("BTC") else ("XRP" if alias.upper().startswith("XRP") else None)
    mid = pl.get("mid") or pl.get("last")
    if asset is None or not mid:
        return False
    zone = BM_ZONE[asset]
    zones = {}
    for key in ("bids", "asks"):
        for lv in pl.get(key) or []:
            price, size = lv[0], lv[1]
            if abs(price - mid) / mid > BM_BAND:
                continue
            z = round(round(price / zone) * zone, 8)
            zones[z] = zones.get(z, 0.0) + price * size
    t = float(pl.get("t", time.time() * 1000)) / 1000.0
    meta = {"alias": alias, "t": t, "recv": time.time(), "apiProtected": bool(pl.get("apiProtected")), "fullDepth": bool(pl.get("fullDepth")),
            "depthEvents": pl.get("depthEvents", 0), "tradeEvents": pl.get("tradeEvents", 0), "levels": len(pl.get("bids") or []) + len(pl.get("asks") or []),
            "buyUsd": pl.get("buyUsd", 0.0), "sellUsd": pl.get("sellUsd", 0.0), "mid": mid, "bb": pl.get("bb"), "ba": pl.get("ba")}
    with _bm_lock:
        st = _bookmap[asset]
        st["last"] = meta
        st["hist"].append((t, mid, zones))
        st["n"] += 1
        st["buy"] += float(pl.get("buyUsd") or 0.0)
        st["sell"] += float(pl.get("sellUsd") or 0.0)
        if time.time() - st["log_ts"] >= 5:
            near = lambda side: sum(u for z, u in zones.items() if (z < mid if side == "b" else z > mid) and abs(z - mid) / mid <= 0.001)
            topb = max(((u, z) for z, u in zones.items() if z < mid), default=(0, 0))
            topa = max(((u, z) for z, u in zones.items() if z > mid), default=(0, 0))
            try:
                os.makedirs(os.path.dirname(_BM_LOG), exist_ok=True)
                new = not os.path.exists(_BM_LOG)
                with open(_BM_LOG, "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["ts_utc", "asset", "alias", "mid", "best_bid", "best_ask", "buy_usd_5s", "sell_usd_5s", "bid_depth_0.1pct_usd",
                                    "ask_depth_0.1pct_usd", "top_bid_zone_price", "top_bid_zone_usd", "top_ask_zone_price", "top_ask_zone_usd", "api_protected"])
                    w.writerow([dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), asset, alias, mid, pl.get("bb"), pl.get("ba"), round(st["buy"]),
                                round(st["sell"]), round(near("b")), round(near("a")), topb[1], round(topb[0]), topa[1], round(topa[0]), int(meta["apiProtected"])])
            except OSError:
                pass
            st["log_ts"] = time.time()
            st["buy"] = st["sell"] = 0.0
    return True


def bookmap_heat(asset, since):
    """Heat-map columns newer than `since` (all of the last ~30 min, thinned, when since is 0) + a plain-English status of the bridge."""
    st = _bookmap.get(asset)
    if st is None:
        return {"cols": [], "status": {"connected": False, "text": "unknown coin"}}
    with _bm_lock:
        last = dict(st["last"]) if st["last"] else None
        hist = list(st["hist"])
    now = time.time()
    cols = []
    if hist:
        sel = [h for h in hist if h[0] > since and h[0] > now - 35 * 60]
        step = max(1, len(sel) // 420) if since <= 0 else 1
        for t, mid, zones in sel[::step]:
            cols.append({"t": round(t, 1), "mid": mid, "z": [[z, int(u)] for z, u in zones.items() if u > 0]})
    if not last or now - last["recv"] > 8 or last.get("depthEvents", 0) == 0:
        # Bookmap's add-on is not feeding us (BookmapData blocks add-ons): draw OUR OWN heat map from the order books of every exchange
        with _bm_lock:
            own = list(_own_heat.get(asset, []))
        sel = [h for h in own if h[0] > since and h[0] > now - 35 * 60]
        step = max(1, len(sel) // 420) if since <= 0 else 1
        ocols = [{"t": round(t, 1), "mid": px, "z": [[z, int(u)] for z, u in zones.items() if u > 0]} for t, px, zones, n in sel[::step]]
        n_ex = own[-1][3] if own else 0
        return {"cols": ocols, "zone": BM_ZONE.get(asset), "zone_pct": BM_BAND, "own": True,
                "status": {"connected": bool(own), "text": ("live - built from the order books of %d exchanges (30-minute window fills as it runs)" % n_ex) if own else "collecting order books..."}}
    else:
        bad = ""
        if last["depthEvents"] == 0:
            bad = " - connected, but NO order-book data is arriving (your Bookmap plan may not give add-ons this feed)"
        elif last["apiProtected"]:
            bad = " - note: this feed is marked API-protected"
        status = {"connected": last["depthEvents"] > 0, "text": "connected - %s - %d price levels%s" % (last["alias"], last["levels"], bad)}
    return {"cols": cols, "zone": BM_ZONE.get(asset), "status": status, "zone_pct": BM_BAND}


def get_book_walls(symbol, ref_price):
    """Biggest bid wall (whale lined up to BUY, below price) and ask wall
    (whale lined up to SELL, above price) near the current price, each only
    if it clears that symbol's size threshold. alert_side is set once when a
    fresh alert-worthy wall (very close to price) shows up."""
    if ref_price is None:
        return None
    min_usd = BOOK_WALL_MIN_USD.get(symbol, DEFAULT_BOOK_WALL_MIN_USD)
    with _book_lock:
        book = _order_books.get(symbol)
        if book and book["bids"] and book["asks"]:
            bid_wall = _biggest_wall(book["bids"], ref_price, want_above=False)
            ask_wall = _biggest_wall(book["asks"], ref_price, want_above=True)
            bid_wall = bid_wall if bid_wall and bid_wall[1] >= min_usd else None
            ask_wall = ask_wall if ask_wall and ask_wall[1] >= min_usd else None
        else:
            bid_wall = ask_wall = None                       # no Coinbase live book (yet): the all-exchange books can still fill in below
            if not _flow.HUB.levels:
                return None
        alert_side = None
        alert_ts = None
        now_ = time.time()
        try:
            allw = _all_exchange_walls(symbol, ref_price)
        except Exception:
            allw = None
        a_ask = allw["ask"] if allw else ask_wall           # what the ALERT watches: every exchange when we have the books, else Coinbase
        a_bid = allw["bid"] if allw else bid_wall
        # fixed 2026-10-14: this used ONE remembered wall per coin shared by both sides, so a buy wall and a sell wall near price kept
        # overwriting each other's memory and EVERY poll looked like a new wall (ten identical "incoming buy wall" alerts in a row).
        # Now each side remembers its own wall; the same wall (about the same price) never alerts twice; one alert per poll.
        alert_wall = None
        for side, wall in (("sell", a_ask), ("buy", a_bid)):
            k = (symbol, side)
            prev = _last_book_alert.get(k)
            if prev is not None and now_ - prev[2] > BOOK_ALERT_FORGET_SECONDS:
                del _last_book_alert[k]                                      # gone for a while: if it is back, it is news again
                prev = None
            if wall and wall[2] <= BOOK_WALL_ALERT_PCT and (not allw or wall[1] >= BOOK_WALL_ALL_ALERT_USD.get(symbol, 0)):
                if prev is not None and abs(wall[0] - prev[0]) / wall[0] <= BOOK_ALERT_SAME_WALL_PCT:
                    _last_book_alert[k] = (prev[0], prev[1], now_)          # still the same wall: remember we saw it, no new alert
                elif alert_side is None and (prev is None or now_ - prev[1] >= BOOK_ALERT_COOLDOWN):
                    _last_book_alert[k] = (wall[0], now_, now_)
                    alert_side = side
                    alert_wall = wall
                    alert_ts = now_  # per-event key for the client - see get_whale_flow's alert_ts note above
    return {"bid_wall": bid_wall, "ask_wall": ask_wall, "alert_side": alert_side, "alert_ts": alert_ts, "alert_wall": alert_wall,
            "all_n": allw["n"] if allw else 0, "all_bid_wall": allw["bid"] if allw else None, "all_ask_wall": allw["ask"] if allw else None,
            "all_bid_src": allw["bid_src"] if allw else [], "all_ask_src": allw["ask_src"] if allw else []}


def get_live_price(symbol):
    """Second layer of the staleness fix: even if _update_blended hasn't
    been re-triggered in a while (e.g. BOTH venues went quiet, so nothing
    fired to re-check them), don't hand back an old frozen tick - if it
    hasn't genuinely updated recently, return None so the caller falls back
    to a fresh REST price instead."""
    with _ticks_lock:
        ts = _live_ticks_ts.get(symbol)
        if ts is None or time.time() - ts > LIVE_PRICE_STALE_SECONDS:
            return None
        return _live_ticks.get(symbol)


def get_live_venues(symbol):
    """Which exchanges currently have RECENT (not stale) data feeding the
    blended price - shown on the dashboard so it's clear it's not a
    single-venue number, and so a silently-dead feed doesn't get credited."""
    with _ticks_lock:
        venues = _exchange_last.get(symbol, {})
        now = time.time()
        return sorted(v for v, (_, _, ts) in venues.items() if now - ts <= VENUE_STALE_SECONDS)


_daily_range_cache = {}  # symbol -> (fetched_ts, high, low)
DAILY_RANGE_REFRESH_SECONDS = 60  # a 24h high/low doesn't need sub-minute freshness


def fetch_daily_range(symbol):
    """24h high/low for the underlying crypto asset itself - added
    2026-09-26 per user request. Same Coinbase public stats endpoint as the
    (now-removed) 24h volume feature - it returns high/low alongside volume,
    no auth needed. Returns (None, None) on any failure."""
    url = f"https://api.exchange.coinbase.com/products/{symbol}/stats"
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-15m-predictor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        return float(data["high"]), float(data["low"])
    except (urllib.error.URLError, KeyError, ValueError, TypeError):
        return None, None


def get_daily_range(symbol):
    """Cached wrapper - returns the last known high/low on a transient fetch
    failure rather than blanking out a number that was fine a moment ago."""
    now = time.time()
    cached = _daily_range_cache.get(symbol)
    if cached and now - cached[0] < DAILY_RANGE_REFRESH_SECONDS:
        return cached[1], cached[2]
    high, low = fetch_daily_range(symbol)
    if high is not None:
        _daily_range_cache[symbol] = (now, high, low)
        return high, low
    return (cached[1], cached[2]) if cached else (None, None)


_regime_cache = {}  # asset name -> (last_fetch_time, return_pct, regime)
REGIME_LOOKBACK_MINUTES = 240  # 4 hours - a 15-min market has no "trend" of
                                 # its own, this is about the broader backdrop
                                 # it's happening inside of, not a prediction.
REGIME_REFRESH_SECONDS = 60     # a 4hr trend doesn't need 2-second updates -
                                 # refetching that much history every poll
                                 # would just hammer the exchange for no reason
# measured 2026-08-13 (BTC, 45 days of 4hr returns): the old 0.5% guess put
# 64.5% of ALL windows into NEUTRAL, since the real median 4hr move is only
# 0.336% - the threshold was set ABOVE the typical move, so most periods
# never had a chance to register as trending. 0.20% gives a real near-even
# 3-way split (~33% each) instead of a guessed number that skews neutral.
REGIME_THRESHOLD = 0.002        # +/-0.20% over 4hrs to call BULL/BEAR, else NEUTRAL


def get_market_regime(asset):
    """Longer-window bull/bear/neutral read, separate from the 15-min call -
    just describes the backdrop (has spot been broadly climbing, falling, or
    going nowhere over the last 4 hours), not a prediction of what's next."""
    now = time.time()
    cached = _regime_cache.get(asset["name"])
    if cached and now - cached[0] < REGIME_REFRESH_SECONDS:
        return cached[1], cached[2]
    closes = get_spot_closes(asset["symbol"], REGIME_LOOKBACK_MINUTES)
    ret, regime = None, None
    if closes and len(closes) >= 2:
        ret = closes[-1] / closes[0] - 1
        if ret >= REGIME_THRESHOLD:
            regime = "BULL"
        elif ret <= -REGIME_THRESHOLD:
            regime = "BEAR"
        else:
            regime = "NEUTRAL"
    _regime_cache[asset["name"]] = (now, ret, regime)
    return ret, regime


# gold's real spot feed - separate from the Coinbase/Kraken websocket-based
# get_spot_closes/get_live_price used for BTC/XRP, since gold isn't a crypto
# pair. Uses Yahoo Finance's free, no-key COMEX gold futures endpoint
# (GC=F). Deliberately NOT routed through the shared get_spot_closes/
# get_market_regime helpers in kalshi_live_bot.py - those are the SAME
# functions the real live trading bot's own code calls, and monkeypatching
# them (the only way to redirect a symbol they don't know about) would mean
# touching shared, real-money-adjacent code for a display-only feature. This
# stays fully separate and self-contained instead.
_gold_futures_cache = {"ts": 0, "points": None}  # points: list of (unix_ts, close) oldest-first
GOLD_FUTURES_REFRESH_SECONDS = 20  # don't hammer Yahoo's unofficial endpoint every 2s poll


def fetch_gold_futures_1m():
    """Raw fetch of ~1 day of 1-min COMEX gold futures closes from Yahoo
    Finance's public chart endpoint. Returns None on any failure (network,
    unexpected response shape, rate limit) - callers already handle a None/
    empty closes list gracefully (same as any other data-fetch hiccup)."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1m&range=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        points = [(ts, c) for ts, c in zip(timestamps, closes) if c is not None]
        return points if points else None
    except (urllib.error.URLError, KeyError, IndexError, ValueError, TypeError):
        return None


def get_gold_futures_closes(minutes_needed):
    """Same shape/contract as get_spot_closes: a list of closes, oldest
    first, covering at least the last `minutes_needed` minutes."""
    now = time.time()
    cached = _gold_futures_cache
    if cached["points"] is None or now - cached["ts"] >= GOLD_FUTURES_REFRESH_SECONDS:
        points = fetch_gold_futures_1m()
        if points is not None:
            cached["points"] = points
            cached["ts"] = now
        elif cached["points"] is None:
            return None  # never had good data yet - honest "no data" rather than a stale guess
    points = cached["points"]
    cutoff = now - minutes_needed * 60
    recent = [c for ts, c in points if ts >= cutoff]
    return recent if len(recent) >= 2 else [c for _, c in points[-max(minutes_needed, 2):]]


GOLD_REGIME_ANCHOR_TOLERANCE_SECONDS = 45 * 60  # the "4hr ago" bar must actually be ~4hr ago


def get_gold_market_regime():
    """4hr-return regime for gold. CRITICAL DIFFERENCE from get_market_regime:
    gold futures (Yahoo GC=F) STOP TRADING nightly (~1hr) and all weekend, so
    the feed has real gaps. The old version just did closes[-1]/closes[0]-1
    over 'whatever bars fall in the last 240 wall-clock minutes' - after a
    weekend that anchor could be Friday's close, turning a multi-day move
    into a bogus '4hr return' and slapping a confident BEAR/BULL label on it.
    Found 2026-09-10: gold was clearly up (+1% on the window, Kalshi pricing
    YES at 74%) while this said 'BEAR -0.75%/4hr', which then wrongly raised
    the entry floor and vetoed winning YES calls - live was 52% vs 71%
    backtested. Now: only return a regime if there's an actual bar within
    ~45min of exactly 4 hours ago; otherwise return None (no regime), which
    the downstream logic already handles as 'no BEAR floor bump, no veto'."""
    now_wall = time.time()
    cached = _regime_cache.get("GOLD")
    if cached and now_wall - cached[0] < REGIME_REFRESH_SECONDS:
        return cached[1], cached[2]

    if _gold_futures_cache["points"] is None or now_wall - _gold_futures_cache["ts"] >= GOLD_FUTURES_REFRESH_SECONDS:
        pts = fetch_gold_futures_1m()
        if pts is not None:
            _gold_futures_cache["points"] = pts
            _gold_futures_cache["ts"] = now_wall
    points = _gold_futures_cache["points"]

    ret, regime = None, None
    if points and len(points) >= 2:
        latest_ts, latest_px = points[-1]
        target_ts = latest_ts - REGIME_LOOKBACK_MINUTES * 60
        anchor = min(points, key=lambda p: abs(p[0] - target_ts))
        if abs(anchor[0] - target_ts) <= GOLD_REGIME_ANCHOR_TOLERANCE_SECONDS and anchor[1] > 0:
            ret = latest_px / anchor[1] - 1
            if ret >= REGIME_THRESHOLD:
                regime = "BULL"
            elif ret <= -REGIME_THRESHOLD:
                regime = "BEAR"
            else:
                regime = "NEUTRAL"
    _regime_cache["GOLD"] = (now_wall, ret, regime)
    return ret, regime


def sanity_check_price(price_a, price_b, symbol, multiplier=1.5):
    """Two prices meant to represent roughly the same instant (the price
    used for grading vs. a fresh independent REST read from the SAME poll
    cycle) should be close. If they differ by more than a rare (p90) 30min
    empirical move, that's not a real market difference - it's a data error
    (found 2026-08-13: a stale/bad tick got recorded as a $560-off
    "settlement" price). Refuse to grade on a failure rather than record a
    bogus win/loss."""
    if price_a is None or price_b is None:
        return True  # can't check without both - don't block on missing data
    bound = EMPIRICAL_MOVES.get(symbol, {}).get(30, {}).get("p90")
    if not bound:
        return True
    return abs(price_a - price_b) <= bound * multiplier


def compute_synthetic_asset_snapshot(pseudo_asset, btc_active):
    """Full snapshot dict for a synthetic (no real Kalshi market) asset -
    same shape/keys as a real BTC/ETH snapshot so it flows through the same
    render_html loop unchanged, just flagged "synthetic": True. See the
    SYNTHETIC_ASSETS comment above for what's real vs. self-computed here."""
    name, symbol = pseudo_asset["name"], pseudo_asset["symbol"]

    close_time = dt.datetime.fromisoformat(btc_active["close_time"].replace("Z", "+00:00"))
    now = dt.datetime.now(dt.timezone.utc)
    seconds_left = max(0, (close_time - now).total_seconds())

    spot_closes = get_spot_closes(symbol, pseudo_asset["sweep_lookback_minutes"])
    current_spot = spot_closes[-1] if spot_closes else None
    spot_ret = spot_return_from_closes(spot_closes)
    live_price = get_live_price(symbol) or current_spot
    live_venues = get_live_venues(symbol)
    chart_closes = get_spot_closes(symbol, 60)
    rsi_value = compute_rsi(chart_closes)
    chart_svg = build_chart_svg(chart_closes)

    # capture this asset's own price ONCE at the start of each synced window
    # as the settlement reference - same idea as BTC's floor_strike, just
    # self-referential since no external contract exists for it. Sanity-
    # checked against a fresh REST read (current_spot, from the SAME poll) -
    # found 2026-08-31 via a real user report: an XRP window's reference got
    # locked in ~3% off real price (a stale tick at the exact instant a new
    # window started), and that bad number stuck for the entire window with
    # no way to correct it. If the check fails, skip capturing this poll and
    # retry next poll instead of committing to a bad value.
    ref = _synthetic_window_ref.setdefault(name, {})
    if (ref.get("ticker") != btc_active["ticker"] and live_price is not None
            and sanity_check_price(live_price, current_spot, symbol)):
        ref["ticker"] = btc_active["ticker"]
        ref["open_price"] = live_price
    target = ref.get("open_price") if ref.get("ticker") == btc_active["ticker"] else None

    regime_ret, regime = get_market_regime(pseudo_asset)
    swept = had_recent_sweep(spot_closes, pseudo_asset["sweep_lookback_minutes"],
                              pseudo_asset["sweep_recent_minutes"], pseudo_asset["sweep_margin"])
    prev_swept = _last_sweep_state.get(name, False)
    sweep_just_fired = swept and not prev_swept
    _last_sweep_state[name] = swept

    micro_dir, micro_pct = get_micro_trend(symbol, 30)

    # live, IN-PROGRESS stat for the CURRENT window only - what fraction of
    # the elapsed minutes has this asset been on the opposite side of ITS OWN
    # open vs. where BTC has been relative to ITS OWN open. Different from a
    # historical settlement stat (that's about the final result; this is
    # about time spent so far this window, and resets every window).
    pct_opposite_this_window, pct_opposite_samples = None, 0
    elapsed_min_int = int(max(0, 15 - seconds_left / 60))
    if elapsed_min_int >= 1:
        lookback_needed = elapsed_min_int + 2
        btc_hist = get_spot_closes("BTC-USD", lookback_needed)
        asset_hist = get_spot_closes(symbol, lookback_needed)
        if btc_hist and asset_hist:
            btc_window = btc_hist[-(elapsed_min_int + 1):]
            asset_window = asset_hist[-(elapsed_min_int + 1):]
            n = min(len(btc_window), len(asset_window))
            if n >= 2:
                btc_window, asset_window = btc_window[-n:], asset_window[-n:]
                btc_ref, asset_ref = btc_window[0], asset_window[0]
                opposite_n, total_n = 0, 0
                for bp, ap in zip(btc_window[1:], asset_window[1:]):
                    btc_d, asset_d = bp - btc_ref, ap - asset_ref
                    if btc_d == 0 or asset_d == 0:
                        continue
                    total_n += 1
                    if (btc_d > 0) != (asset_d > 0):
                        opposite_n += 1
                if total_n:
                    pct_opposite_this_window = opposite_n / total_n
                    pct_opposite_samples = total_n

    ranges = {}
    if live_price is not None:
        for h in HORIZONS_MINUTES:
            low, high = project_range(live_price, symbol, h, "p50")
            if low is not None:
                ranges[h] = [round(low, 4), round(high, 4)]

    leaning, confidence = None, None
    if target is not None and live_price is not None and live_price != target:
        leaning = "YES" if live_price > target else "NO"
        elapsed_minutes = max(0.0, 15 - seconds_left / 60)
        typical_move = project_point_for_minutes(0, symbol, elapsed_minutes,
                                                  direction="YES", percentile="p50")
        if typical_move:
            ratio = abs(live_price - target) / typical_move
            confidence = 0.5 + min(ratio, 1.0) * 0.45

    required_floor = pseudo_asset["entry_confidence_floor"]
    if regime == "BEAR":
        required_floor = max(required_floor, BEAR_MIN_CONFIDENCE)
    below_floor = confidence is None or confidence < required_floor
    direction_confirms = momentum_confirms(spot_closes, leaning, pseudo_asset["strict_direction_margin"])
    against_regime = ((regime == "BEAR" and leaning == "YES") or (regime == "BULL" and leaning == "NO")) and (
        confidence is None or confidence < REGIME_VETO_OVERRIDE_CONFIDENCE)
    patience_cleared = get_confirm_streak_ok(name, leaning, direction_confirms, regime, confidence)
    my_call = leaning if (leaning is not None and not below_floor and direction_confirms
                           and not swept and not against_regime and patience_cleared) else None

    bias_correction, bias_samples = get_bias_correction(name, symbol)
    predicted_close_price = None
    if live_price is not None:
        predicted_close_price = project_point_for_minutes(
            live_price, symbol, seconds_left / 60, direction=my_call, percentile="p50")
        predicted_close_price += bias_correction

    # lock the FIRST real call this window forms, freeze it, grade the
    # previous window on rollover - identical mechanics to BTC/ETH
    prev_lock = _locked.get(name)
    if prev_lock is None or prev_lock["ticker"] != btc_active["ticker"]:
        if (prev_lock is not None and live_price is not None and prev_lock["target"] is not None
                and sanity_check_price(live_price, current_spot, symbol)):
            final_price = live_price
            actual_side = "YES" if final_price >= prev_lock["target"] else "NO"
            was_correct = (prev_lock["my_call"] == actual_side) if prev_lock["my_call"] else None
            price_error = (final_price - prev_lock["predicted_price"]
                           if prev_lock["predicted_price"] is not None else None)
            hist = _history.setdefault(name, [])
            hist.append({
                "ticker": prev_lock["ticker"],
                "call": prev_lock["my_call"],
                "predicted_price": prev_lock["predicted_price"],
                "final_price": round(final_price, 4),
                "actual_side": actual_side,
                "correct": was_correct,
                "price_error": round(price_error, 4) if price_error is not None else None,
                "locked_regime": prev_lock.get("locked_regime"),
                "locked_regime_ret": prev_lock.get("locked_regime_ret"),
                "locked_momentum_ratio": prev_lock.get("locked_momentum_ratio"),
            })
            del hist[:-HISTORY_CAP]
            _save_history()
        _locked[name] = {
            "ticker": btc_active["ticker"],
            "my_call": None,
            "predicted_price": None,
            "target": target,
            "locked_seconds_left": None,
            "locked_regime": None,
            "locked_kalshi_price": None,
            "locked_regime_ret": None,
            "locked_momentum_ratio": None,
            "checkpoints": {m: None for m in CHECKPOINT_MINUTES},
            "late_call": None,
            "late_confidence": None,
            "late_flip_chance": None,
            "late_predicted_price": None,
            "late_locked_seconds_left": None,
        }
    locked = _locked[name]
    elapsed_minutes = max(0.0, 15 - seconds_left / 60)
    checkpoints = locked.setdefault("checkpoints", {m: None for m in CHECKPOINT_MINUTES})
    for m in CHECKPOINT_MINUTES:
        if checkpoints.get(m) is None and elapsed_minutes >= m:
            checkpoints[m] = leaning
    if locked["target"] is None and target is not None:
        locked["target"] = target
    if locked["my_call"] is None and my_call is not None:
        locked["my_call"] = my_call
        locked["predicted_price"] = predicted_close_price
        locked["locked_seconds_left"] = round(seconds_left)
        locked["locked_regime"] = regime
        # NOT a real market price - this asset has no Kalshi contract, this
        # is our own self-computed confidence proxy. Logged for completeness
        # but should never be treated as a real cost basis the way BTC/ETH's
        # locked_kalshi_price is.
        locked["locked_kalshi_price"] = (
            (confidence if my_call == "YES" else (1 - confidence)) if confidence is not None else None
        )
        # OUR OWN gathered information, independent of any market price - the
        # numeric 4hr return (not just the BULL/BEAR/NEUTRAL label) and how
        # many multiples of the minimum required threshold our own momentum
        # signal actually cleared. Logged so we can test whether OUR
        # information predicts wins, not just re-slice Kalshi's own price.
        locked["locked_regime_ret"] = regime_ret
        locked["locked_momentum_ratio"] = (
            abs(spot_ret) / pseudo_asset["strict_direction_margin"] if spot_ret is not None else None
        )

    # "Late call" - separate from "Locked first call": locks the first real
    # call that forms once <=4.5min are LEFT in the window, not from window
    # open. Own callbox, own confidence % and flip-chance % at the moment it
    # locks - a second, independent read late in the window.
    if locked.get("late_call") is None and seconds_left <= LATE_CALL_SECONDS and my_call is not None:
        locked["late_call"] = my_call
        locked["late_confidence"] = confidence
        locked["late_flip_chance"] = (1 - confidence) if confidence is not None else None
        locked["late_predicted_price"] = predicted_close_price
        locked["late_locked_seconds_left"] = round(seconds_left)

    dramatic_swing = get_dramatic_swing(symbol, live_price)
    dramatic_vs_call = None
    if dramatic_swing and locked["my_call"]:
        implied_side = "YES" if dramatic_swing == "up" else "NO"
        dramatic_vs_call = "against" if implied_side != locked["my_call"] else "with"

    asset_history = _history.get(name, [])
    graded = [h for h in asset_history if h["correct"] is not None]
    track_record = (sum(1 for h in graded if h["correct"]), len(graded))
    windows_resolved = len(asset_history)
    no_call_n = windows_resolved - len(graded)
    streak_type, streak_count = get_streak(asset_history)

    return {
        "ticker": btc_active["ticker"] + f" ({name}, synced to BTC's window)",
        "seconds_left": round(seconds_left),
        "spot_price": round(live_price, 4) if live_price is not None else None,
        "live_venues": live_venues,
        "target_price": round(target, 4) if target is not None else None,
        "kalshi_price": None,
        "confidence": confidence,
        "leaning": leaning,
        "spot_ret": spot_ret,
        "regime": regime,
        "regime_ret": regime_ret,
        "swept": swept,
        "sweep_just_fired": sweep_just_fired,
        "micro_dir": micro_dir,
        "micro_pct": micro_pct,
        "my_call": my_call,
        "predicted_close_price": round(predicted_close_price, 4) if predicted_close_price is not None else None,
        "locked_call": locked["my_call"],
        "locked_price": locked["predicted_price"],
        "locked_seconds_left": locked["locked_seconds_left"],
        "track_record": track_record,
        "windows_resolved": windows_resolved,
        "no_call_n": no_call_n,
        "streak_type": streak_type,
        "streak_count": streak_count,
        "last_result": asset_history[-1] if asset_history else None,
        "dramatic_swing": dramatic_swing,
        "dramatic_vs_call": dramatic_vs_call,
        "bias_correction": round(bias_correction, 4),
        "bias_samples": bias_samples,
        "ranges": ranges,
        "updated_at": now.isoformat(),
        "synthetic": True,
        "synthetic_name": name,
        "pct_opposite_this_window": pct_opposite_this_window,
        "pct_opposite_samples": pct_opposite_samples,
        "rsi": round(rsi_value, 1) if rsi_value is not None else None,
        "chart_svg": chart_svg,
        "checkpoints": dict(checkpoints),
        # synthetic assets have no real kalshi_price (a 0-1 P(YES) figure) -
        # rebuild an equivalent from leaning+confidence (which ARE already on
        # that same asymmetric scale) so the same locked-call-aware formula
        # applies here too, not just the real-market path
        "flip_chance": (round(flip_chance_vs_locked_call(
                             confidence if leaning == "YES" else (1 - confidence) if leaning == "NO" else None,
                             locked.get("my_call"), confidence), 4)
                         if confidence is not None else None),
        "late_call": locked.get("late_call"),
        "late_confidence": locked.get("late_confidence"),
        "late_flip_chance": locked.get("late_flip_chance"),
        "late_predicted_price": locked.get("late_predicted_price"),
        "late_locked_seconds_left": locked.get("late_locked_seconds_left"),
    }


def compute_info_call_snapshot(asset, active, ticker, seconds_left, target, kalshi_price,
                                spot_closes, spot_ret, live_price, live_venues, regime, regime_ret,
                                swept, sweep_just_fired, micro_dir, micro_pct, ranges, now):
    """The INFO-based call, graded against the SAME real Kalshi target as the
    PRICE-based call for a fair comparison - direction comes from OUR OWN
    momentum reading (not Kalshi's price at all), gated by how many multiples
    of the minimum threshold that momentum cleared. Reuses signals already
    fetched this poll for the real asset - no extra API calls."""
    name = asset["name"] + "-INFO"
    symbol = asset["symbol"]

    momentum_ratio = abs(spot_ret) / asset["strict_direction_margin"] if spot_ret is not None else None
    leaning = None
    if spot_ret is not None and spot_ret != 0:
        leaning = "YES" if spot_ret > 0 else "NO"
    gate_ok = momentum_ratio is not None and momentum_ratio >= INFO_MOMENTUM_MULTIPLIER
    # display-only mapping of momentum_ratio onto the same 0.5-0.99 scale the
    # self-computed synthetic-asset confidence uses, purely so this reuses
    # the existing card template unchanged - NOT a probability.
    confidence = min(0.5 + min(momentum_ratio, 3.0) / 3.0 * 0.49, 0.99) if momentum_ratio is not None else None

    against_regime = (regime == "BEAR" and leaning == "YES") or (regime == "BULL" and leaning == "NO")
    patience_cleared = get_confirm_streak_ok(name, leaning, gate_ok, regime)
    my_call = leaning if (leaning is not None and gate_ok and not swept and not against_regime
                           and patience_cleared) else None

    bias_correction, bias_samples = get_bias_correction(name, symbol)
    predicted_close_price = None
    if live_price is not None:
        predicted_close_price = project_point_for_minutes(
            live_price, symbol, seconds_left / 60, direction=my_call, percentile="p50")
        predicted_close_price += bias_correction

    prev_lock = _locked.get(name)
    if prev_lock is None or prev_lock["ticker"] != ticker:
        current_spot = spot_closes[-1] if spot_closes else None
        if (prev_lock is not None and live_price is not None and prev_lock["target"] is not None
                and sanity_check_price(live_price, current_spot, symbol)):
            final_price = live_price
            actual_side = "YES" if final_price >= prev_lock["target"] else "NO"
            was_correct = (prev_lock["my_call"] == actual_side) if prev_lock["my_call"] else None
            price_error = (final_price - prev_lock["predicted_price"]
                           if prev_lock["predicted_price"] is not None else None)
            hist = _history.setdefault(name, [])
            hist.append({
                "ticker": prev_lock["ticker"],
                "call": prev_lock["my_call"],
                "predicted_price": prev_lock["predicted_price"],
                "final_price": round(final_price, 2),
                "actual_side": actual_side,
                "correct": was_correct,
                "price_error": round(price_error, 2) if price_error is not None else None,
                "locked_regime": prev_lock.get("locked_regime"),
                "locked_momentum_ratio": prev_lock.get("locked_momentum_ratio"),
            })
            del hist[:-HISTORY_CAP]
            _save_history()
        _locked[name] = {
            "ticker": ticker,
            "my_call": None,
            "predicted_price": None,
            "target": target,
            "locked_seconds_left": None,
            "locked_regime": None,
            "locked_momentum_ratio": None,
        }
    locked = _locked[name]
    if locked["my_call"] is None and my_call is not None:
        locked["my_call"] = my_call
        locked["predicted_price"] = predicted_close_price
        locked["locked_seconds_left"] = round(seconds_left)
        locked["locked_regime"] = regime
        locked["locked_momentum_ratio"] = momentum_ratio

    dramatic_swing = get_dramatic_swing(symbol, live_price)
    dramatic_vs_call = None
    if dramatic_swing and locked["my_call"]:
        implied_side = "YES" if dramatic_swing == "up" else "NO"
        dramatic_vs_call = "against" if implied_side != locked["my_call"] else "with"

    asset_history = _history.get(name, [])
    graded = [h for h in asset_history if h["correct"] is not None]
    track_record = (sum(1 for h in graded if h["correct"]), len(graded))
    windows_resolved = len(asset_history)
    no_call_n = windows_resolved - len(graded)
    streak_type, streak_count = get_streak(asset_history)

    return {
        "ticker": ticker + " (INFO call - our own momentum, not Kalshi's price)",
        "seconds_left": round(seconds_left),
        "spot_price": round(live_price, 2) if live_price is not None else None,
        "live_venues": live_venues,
        "target_price": target,
        "kalshi_price": kalshi_price,
        "confidence": confidence,
        "leaning": leaning,
        "spot_ret": spot_ret,
        "regime": regime,
        "regime_ret": regime_ret,
        "swept": swept,
        "sweep_just_fired": sweep_just_fired,
        "micro_dir": micro_dir,
        "micro_pct": micro_pct,
        "my_call": my_call,
        "predicted_close_price": round(predicted_close_price, 2) if predicted_close_price is not None else None,
        "locked_call": locked["my_call"],
        "locked_price": locked["predicted_price"],
        "locked_seconds_left": locked["locked_seconds_left"],
        "track_record": track_record,
        "windows_resolved": windows_resolved,
        "no_call_n": no_call_n,
        "streak_type": streak_type,
        "streak_count": streak_count,
        "last_result": asset_history[-1] if asset_history else None,
        "dramatic_swing": dramatic_swing,
        "dramatic_vs_call": dramatic_vs_call,
        "bias_correction": round(bias_correction, 2),
        "bias_samples": bias_samples,
        "ranges": ranges,
        "updated_at": now.isoformat(),
        "synthetic": False,
        "direction_source": "momentum",
        "momentum_ratio": round(momentum_ratio, 2) if momentum_ratio is not None else None,
    }


# self-healing reconciliation - added 2026-09-12 after a THIRD real mis-
# grade report. The at-rollover check (grade off Kalshi's finalized result
# instead of our own price proxy) helps, but Kalshi doesn't always finalize
# in the exact ~2s poll where a window rolls over, so it can still silently
# fall back to the proxy and get it wrong. Rather than needing the user to
# catch and report every one, queue any window graded via the fallback
# proxy and keep re-checking it until Kalshi actually finalizes (or give up
# after RECONCILE_MAX_AGE_SECONDS) - correcting the saved history in place.
_pending_reconcile = []  # [{"asset": name, "ticker": ticker, "added_ts": float}]
RECONCILE_MAX_AGE_SECONDS = 15 * 60


def _queue_reconcile(asset_name, ticker):
    _pending_reconcile.append({"asset": asset_name, "ticker": ticker, "added_ts": time.time()})


def _reconcile_pending_grades():
    if not _pending_reconcile:
        return
    now = time.time()
    still_pending = []
    changed = False
    for item in _pending_reconcile:
        if now - item["added_ts"] > RECONCILE_MAX_AGE_SECONDS:
            continue  # give up silently - the proxy grade stands, nothing more we can do
        try:
            settled = get_market_snapshot(item["ticker"])
            real_result = (settled.get("result") or "").upper()
            if settled.get("status") == "finalized" and real_result in ("YES", "NO"):
                for h in _history.get(item["asset"], []):
                    if h["ticker"] == item["ticker"]:
                        if h.get("actual_side") != real_result:
                            h["actual_side"] = real_result
                            h["correct"] = (h["call"] == real_result) if h.get("call") else None
                            changed = True
                        break
                # settle a real-money live position that was still open because
                # Kalshi hadn't finalized at the original rollover moment
                settle_live_position(item["ticker"], real_result)
                continue  # resolved (whether it needed correcting or not) - drop from the queue
        except Exception:
            pass
        still_pending.append(item)
    _pending_reconcile[:] = still_pending
    if changed:
        _save_history()


_poll_t = {}      # asset name -> seconds its last poll step took (diagnostics, shown at /debug_poll)
_poll_loop_t = {"last": 0.0, "avg": 0.0, "n": 0}


def poll_loop():
    while True:
        _t_loop = time.time()
        _reconcile_pending_grades()
        snapshot = {}
        for asset in DASHBOARD_ASSETS:
            _t_asset = time.time()
            try:
                active = get_active_market(asset["series"])
                if active is None:
                    snapshot[asset["name"]] = {"error": "no active market"}
                    continue
                # the list endpoint's bid/ask can be ~30s stale - overwrite it
                # with the live order book so leaning/confidence/lock all see
                # the real current price (see get_live_yes_bid_ask)
                fresh_quote = get_live_yes_bid_ask(active["ticker"])
                calls_use_live_quote = (fresh_quote is not None
                                        and asset["name"] not in LIST_QUOTE_CALL_ASSETS)
                if calls_use_live_quote:
                    active["yes_bid_dollars"], active["yes_ask_dollars"] = fresh_quote
                close_time = dt.datetime.fromisoformat(active["close_time"].replace("Z", "+00:00"))
                now = dt.datetime.now(dt.timezone.utc)
                seconds_left = max(0, (close_time - now).total_seconds())
                open_time_dt = dt.datetime.fromisoformat(active["open_time"].replace("Z", "+00:00"))
                elapsed_seconds = max(0, (now - open_time_dt).total_seconds())
                kalshi_price = get_kalshi_price(active)
                target = active.get("floor_strike")
                try:
                    if asset["name"] in _flow.ASSETS:
                        _flow.HUB.set_line(asset["name"], active["ticker"], target)
                        _kalshi_round_flow(asset["name"], active["ticker"], open_time_dt.timestamp())
                except Exception:
                    pass

                is_gold = asset["symbol"] == "GOLD-FUT"
                if is_gold:
                    spot_closes = get_gold_futures_closes(asset["sweep_lookback_minutes"])
                else:
                    spot_closes = get_spot_closes(asset["symbol"], asset["sweep_lookback_minutes"])
                current_spot = spot_closes[-1] if spot_closes else None
                spot_ret = spot_return_from_closes(spot_closes)
                # longer series for RSI (needs 15+ points for a stable read)
                # and the moving chart - separate fetch since sweep_lookback
                # is often too short for either.
                chart_closes = get_gold_futures_closes(60) if is_gold else get_spot_closes(asset["symbol"], 60)
                rsi_value = compute_rsi(chart_closes)
                regime_ret, regime = get_gold_market_regime() if is_gold else get_market_regime(asset)
                swept = had_recent_sweep(spot_closes, asset["sweep_lookback_minutes"],
                                          asset["sweep_recent_minutes"], asset["sweep_margin"])
                # "swept" stays True for the whole recent_minutes window (that's
                # correct - liquidity is still broken until it retraces), but
                # that reads as constant noise on screen. Only flag the MOMENT
                # it flips false->true as the actual alert; otherwise show it
                # as a calmer "still elevated" state.
                prev_swept = _last_sweep_state.get(asset["name"], False)
                sweep_just_fired = swept and not prev_swept
                _last_sweep_state[asset["name"]] = swept

                # true tick-by-tick, volume-weighted across exchanges - falls
                # back to the 1-min candle price only if no websocket tick has
                # arrived yet (e.g. just started)
                # gold has no websocket tick feed (not a crypto pair) - its
                # "live" price is just the latest 1-min close from Yahoo,
                # refreshed every GOLD_FUTURES_REFRESH_SECONDS, not a true
                # continuous tick. Labeled honestly rather than implying the
                # same tick-by-tick freshness BTC/XRP get.
                if is_gold:
                    live_price = current_spot
                    live_venues = ["Yahoo Finance GC=F (1min bars, not tick-by-tick)"]
                else:
                    live_price = get_live_price(asset["symbol"]) or current_spot
                    live_venues = get_live_venues(asset["symbol"])

                # Kalshi removed floor_strike from these markets (found
                # 2026-08-31 via a real user report) - they now settle by
                # comparing a 60s-average price at close vs. at open, not a
                # fixed dollar target anymore ("Target price: TBD" in their
                # own API). Fall back to our own captured window-open price -
                # same self-referential mechanism already used for
                # NEAR/BNB/XRP. Not a perfect match (Kalshi averages 60s of
                # CF Benchmarks BRTI, we use a single blended tick) but the
                # closest honest approximation available without that feed.
                if target is None:
                    ref = _synthetic_window_ref.setdefault(asset["name"], {})
                    if (ref.get("ticker") != active["ticker"] and live_price is not None
                            and sanity_check_price(live_price, current_spot, asset["symbol"])):
                        ref["ticker"] = active["ticker"]
                        ref["open_price"] = live_price
                    target = ref.get("open_price") if ref.get("ticker") == active["ticker"] else None

                # real-time direction over the last 30s of actual trades - what's
                # happening RIGHT NOW, not a prediction of where it ends up
                micro_dir, micro_pct = get_micro_trend(asset["symbol"], 30)

                price_decimals = asset.get("price_decimals", 2)
                # candle chart rebuilt 2026-09-13, SERVER-rendered this time
                # (the client-side JS rewrite broke and was reverted earlier
                # today - going back to the approach already proven to work,
                # same as the original line chart). Default view: 30min.
                ticks = get_tick_history(asset["symbol"])
                default_lookback_seconds = 40 * 60
                default_candles = bucket_ticks_to_candles(ticks, lookback_seconds=default_lookback_seconds)
                chart_typical_move = None
                if seconds_left > 0:
                    chart_typical_move = project_point_for_minutes(
                        0, asset["symbol"], seconds_left / 60, direction="YES", percentile="p68") or None
                whale_trades_for_chart = get_whale_trades_raw(asset["symbol"])
                chart_svg = render_candle_chart_svg(default_candles, target, live_price, chart_typical_move,
                                                     seconds_left, whale_trades_for_chart,
                                                     lookback_seconds=default_lookback_seconds)
                ranges = {}
                if live_price is not None:
                    for h in HORIZONS_MINUTES:
                        low, high = project_range(live_price, asset["symbol"], h, "p50")
                        if low is not None:
                            ranges[h] = [round(low, price_decimals), round(high, price_decimals)]

                # INFO-based call REMOVED 2026-08-15 per user request - real
                # data showed it underperforming the original Price-based
                # system (57.1% vs 65.0% over 21 vs 143 graded windows).
                # compute_info_call_snapshot() is still defined below but no
                # longer called, so it stops locking/grading new windows too,
                # not just stops being displayed.

                confidence = max(kalshi_price, 1 - kalshi_price) if kalshi_price is not None else None
                leaning = ("YES" if kalshi_price > 0.5 else "NO") if kalshi_price is not None else None

                # "my call": honest yes/no - same validated bar the live bot
                # trades on (confidence floor + strict-direction confirmation +
                # no active sweep). If it doesn't clear the bar, the honest
                # answer is "no confident call", not a forced guess.
                required_floor = CALL_CONFIDENCE_FLOOR_OVERRIDE.get(asset["name"], asset["entry_confidence_floor"])
                if regime == "BEAR":
                    required_floor = max(required_floor, BEAR_MIN_CONFIDENCE)
                below_floor = confidence is None or confidence < required_floor
                direction_confirms = momentum_confirms(spot_closes, leaning, asset["strict_direction_margin"])
                # regime veto - added 2026-08-12 after reviewing real losses:
                # 3 of BTC's first 7 wrong calls all called YES during a
                # confirmed 4hr BEAR regime (brief 1-min upticks faking out
                # the entry logic inside a real downtrend). Small sample (n=3)
                # so treat this as a live-evidenced veto, not a fully
                # backtested rule yet - worth validating against the larger
                # historical dataset to confirm it generalizes.
                against_regime = ((regime == "BEAR" and leaning == "YES") or (regime == "BULL" and leaning == "NO")) and (
                    confidence is None or confidence < REGIME_VETO_OVERRIDE_CONFIDENCE)
                patience_cleared = get_confirm_streak_ok(asset["name"], leaning, direction_confirms, regime, confidence)
                chop_threshold = CHOPPINESS_VETO_THRESHOLD.get(asset["name"])
                choppiness = get_choppiness(spot_closes, CHOPPINESS_LOOKBACK_MINUTES) if chop_threshold is not None else None
                chopped = choppiness is not None and choppiness > chop_threshold
                too_early = elapsed_seconds < MIN_LOCK_SECONDS.get(asset["name"], 0)
                my_call = leaning if (leaning is not None and not below_floor and direction_confirms
                                       and not swept and not against_regime and patience_cleared
                                       and not chopped and not too_early) else None

                # predicted number at THIS market's actual close (not a generic
                # horizon) - only offset from current price when my_call is a
                # real validated lean, otherwise honestly just current price
                predicted_close_price = None
                bias_correction, bias_samples = get_bias_correction(asset["name"], asset["symbol"])
                if live_price is not None:
                    predicted_close_price = project_point_for_minutes(
                        live_price, asset["symbol"], seconds_left / 60, direction=my_call, percentile="p50")
                    predicted_close_price += bias_correction

                # lock in the FIRST REAL call for this ticker - not literally
                # the first poll (which is almost always "NO CALL" since
                # there's no momentum data yet in the opening seconds of a
                # window). Instead: stay unlocked until my_call actually
                # becomes YES/NO for the first time this window, then freeze
                # it there for the rest of the window. Same rule every
                # window. When the ticker rolls over, grade the just-finished
                # lock.
                #
                # fixed 2026-09-11 after a SECOND real mis-grade report: this
                # used to grade off our OWN single-tick price capture vs. our
                # OWN captured target - both just proxies for what Kalshi
                # actually settles on (a 60-SECOND AVERAGE, not one tick).
                # Right near the boundary the two can land on opposite sides
                # (confirmed twice now via the real API: recorded 'NO' when
                # Kalshi's real result was 'yes'). Kalshi's own finalized
                # result is the actual source of truth and it's a single free
                # API call at rollover (~4x/hour/asset) - use that directly,
                # falling back to the self-computed proxy only if the API
                # call fails or the market isn't finalized yet.
                ticker = active["ticker"]
                prev_lock = _locked.get(asset["name"])
                if prev_lock is None or prev_lock["ticker"] != ticker:
                    if (prev_lock is not None and live_price is not None and prev_lock["target"] is not None
                            and sanity_check_price(live_price, current_spot, asset["symbol"])):
                        final_price = live_price
                        actual_side = None
                        try:
                            settled = get_market_snapshot(prev_lock["ticker"])
                            real_result = (settled.get("result") or "").upper()
                            if settled.get("status") == "finalized" and real_result in ("YES", "NO"):
                                actual_side = real_result
                        except Exception:
                            pass
                        used_real_result = actual_side is not None
                        if actual_side is None:
                            actual_side = "YES" if final_price >= prev_lock["target"] else "NO"
                            # Kalshi hadn't finalized yet (or the call failed) - grading
                            # off our own proxy for now, queue it to be double-checked
                            # and self-corrected once the real result is available.
                            _queue_reconcile(asset["name"], prev_lock["ticker"])
                        # settle any real-money live position on this ticker - ONLY off
                        # the real Kalshi result, never the proxy (the proxy has been
                        # wrong before - a real-money daily-loss tally can't be built on
                        # it). If Kalshi hasn't finalized yet, this is left open and
                        # settled later by _reconcile_pending_grades() once it resolves.
                        if asset["name"] in LIVE_ASSETS and used_real_result:
                            settle_live_position(prev_lock["ticker"], actual_side)
                        was_correct = (prev_lock["my_call"] == actual_side) if prev_lock["my_call"] else None
                        price_error = (final_price - prev_lock["predicted_price"]
                                       if prev_lock["predicted_price"] is not None else None)
                        hist = _history.setdefault(asset["name"], [])
                        hist.append({
                            "ticker": prev_lock["ticker"],
                            "call": prev_lock["my_call"],
                            "predicted_price": prev_lock["predicted_price"],
                            "final_price": round(final_price, price_decimals),
                            "actual_side": actual_side,
                            "correct": was_correct,
                            "price_error": round(price_error, price_decimals) if price_error is not None else None,
                            "locked_regime": prev_lock.get("locked_regime"),
                            "locked_kalshi_price": prev_lock.get("locked_kalshi_price"),
                            "locked_regime_ret": prev_lock.get("locked_regime_ret"),
                            "locked_momentum_ratio": prev_lock.get("locked_momentum_ratio"),
                            "locked_wall_usd": prev_lock.get("locked_wall_usd"),
                            "locked_wall_dist_pct": prev_lock.get("locked_wall_dist_pct"),
                        })
                        del hist[:-HISTORY_CAP]
                        _save_history()
                    # new window starts UNLOCKED - no call has formed yet
                    _locked[asset["name"]] = {
                        "ticker": ticker,
                        "my_call": None,
                        "predicted_price": None,
                        "target": target,
                        "locked_seconds_left": None,
                        "locked_regime": None,
                        "locked_kalshi_price": None,
                        "locked_regime_ret": None,
                        "locked_momentum_ratio": None,
                        "locked_wall_usd": None,
                        "locked_wall_dist_pct": None,
                        "checkpoints": {m: None for m in CHECKPOINT_MINUTES},
                        "late_call": None,
                        "late_confidence": None,
                        "late_flip_chance": None,
                        "late_predicted_price": None,
                        "late_locked_seconds_left": None,
                    }
                    _save_locked_state()  # keep the saved ticker current even in windows where no call ever forms
                    # reset the whale feed at every new round (2026-09-14 per user
                    # request) - previously it carried whale trades over from the
                    # prior window on a rolling 1hr basis, which looked confusing
                    # right after a fresh lock/call had just started.
                    with _ticks_lock:
                        _whale_trades[asset["symbol"]] = []
                locked = _locked[asset["name"]]
                elapsed_minutes = max(0.0, 15 - seconds_left / 60)
                checkpoints = locked.setdefault("checkpoints", {m: None for m in CHECKPOINT_MINUTES})
                for m in CHECKPOINT_MINUTES:
                    if checkpoints.get(m) is None and elapsed_minutes >= m:
                        checkpoints[m] = leaning
                just_locked = locked["my_call"] is None and my_call is not None  # added 2026-09-28
                                 # per user request ("as soon as it calls the call") - a
                                 # one-poll-only edge-trigger for the voice announcement below,
                                 # separate from the certain-flip/whale/etc alerts
                if just_locked:
                    locked["my_call"] = my_call
                    locked["predicted_price"] = predicted_close_price
                    locked["locked_seconds_left"] = round(seconds_left)
                    locked["locked_regime"] = regime
                    # the REAL cost basis for a $100-bet backtest - Kalshi's
                    # own contract price for our called side, at the exact
                    # moment the call locked (not a generic/current reading)
                    locked["locked_kalshi_price"] = kalshi_price if my_call == "YES" else (1 - kalshi_price)
                    # OUR OWN gathered information, independent of any market
                    # price - the numeric 4hr return (not just the label) and
                    # how many multiples of the minimum required threshold
                    # our own momentum signal actually cleared. Logged so we
                    # can test whether OUR information predicts wins, not
                    # just re-slice Kalshi's own price band.
                    locked["locked_regime_ret"] = regime_ret
                    locked["locked_momentum_ratio"] = (
                        abs(spot_ret) / asset["strict_direction_margin"] if spot_ret is not None else None
                    )
                    # the resting order-book wall standing IN THE WAY of this
                    # call succeeding - added 2026-09-23 per user observation
                    # that price often snaps back hard right at a big wall.
                    # For a YES call that's the sell/ask wall ABOVE price
                    # (resistance blocking the move up); for a NO call it's
                    # the buy/bid wall BELOW price (support blocking the move
                    # down). Logged so "win% away from a major wall" becomes
                    # an answerable question once enough of these accumulate -
                    # there was no history of this before now, only a live
                    # display that got overwritten every poll.
                    walls_at_lock = get_book_walls(asset["symbol"], live_price)
                    opposing_wall = None
                    if walls_at_lock:
                        opposing_wall = walls_at_lock["ask_wall"] if my_call == "YES" else walls_at_lock["bid_wall"]
                    locked["locked_wall_usd"] = round(opposing_wall[1], 2) if opposing_wall else None
                    locked["locked_wall_dist_pct"] = round(opposing_wall[2], 5) if opposing_wall else None
                    _save_locked_state()  # persist the moment a call actually locks - the thing worth protecting

                # "Late call" - separate from "Locked first call": locks the
                # first real call that forms once <=4.5min are LEFT in the
                # window, not from window open. Own callbox, own confidence %
                # and flip-chance % at the moment it locks.
                if locked.get("late_call") is None and seconds_left <= LATE_CALL_SECONDS and my_call is not None:
                    locked["late_call"] = my_call
                    locked["late_confidence"] = confidence
                    locked["late_flip_chance"] = (1 - confidence) if confidence is not None else None
                    locked["late_predicted_price"] = predicted_close_price
                    locked["late_locked_seconds_left"] = round(seconds_left)
                    _save_locked_state()

                # "boom" alert - a fast, unusually large move happening RIGHT
                # NOW (not the soft 30s trend read above). Flags loudly when
                # it's swinging against the locked call specifically, since
                # that's the "not gonna be what you called it" case.
                dramatic_swing = get_dramatic_swing(asset["symbol"], live_price)
                dramatic_vs_call = None
                if dramatic_swing and locked["my_call"]:
                    implied_side = "YES" if dramatic_swing == "up" else "NO"
                    dramatic_vs_call = "against" if implied_side != locked["my_call"] else "with"
                # edge-triggered: only true on the POLL where it first BECOMES
                # "against" (not every poll while it stays that way) - this
                # alert had never had a sound before, only the visual pulse.
                boom_just_fired = (dramatic_vs_call == "against"
                                    and _last_boom_vs_call.get(asset["name"]) != "against")
                _last_boom_vs_call[asset["name"]] = dramatic_vs_call
                # per-event timestamp for the client to de-dupe by, same idea as
                # get_whale_flow's alert_ts - added 2026-09-25, this snapshot dict
                # is served to every client poll until poll_loop's NEXT iteration
                # overwrites it, so without a stable per-event key the same boom
                # got announced once per poll that happened to see it still True
                boom_alert_ts = time.time() if boom_just_fired else None

                # "for sure" flip alert - added 2026-09-24 per user request for
                # something bigger/louder than the BOOM pill. Stricter than
                # BOOM alone: needs the fast-price-move-against-call signal
                # AND Kalshi's own price to have actually swung to favor the
                # other side by CERTAIN_FLIP_MIN_CHANCE, not just a fast
                # wiggle that could still resolve the original way. Edge-
                # triggered the same way BOOM is - fires once per flip, not
                # every poll while it stays true.
                # fixed 2026-09-25 - this used to be plain (1 - confidence),
                # which is "chance the CURRENT leaning flips again," not
                # "chance the LOCKED call is wrong" - those are the same
                # thing only until price actually crosses past the locked
                # call. Once it's crossed, that old formula inverts and
                # shrinks toward 0 exactly as the real risk climbs toward 1 -
                # which is exactly why this alert never fired on a real XRP
                # loss where kalshi_price collapsed to 0.009 against a
                # locked YES call (~99% real risk, but the old math read
                # 0.9%). See flip_chance_vs_locked_call()'s docstring.
                cur_flip_chance = flip_chance_vs_locked_call(kalshi_price, locked["my_call"], confidence)
                certain_flip = (dramatic_vs_call == "against" and locked["my_call"] is not None
                                 and cur_flip_chance is not None and cur_flip_chance >= CERTAIN_FLIP_MIN_CHANCE)
                certain_flip_just_fired = certain_flip and not _last_certain_flip.get(asset["name"], False)
                _last_certain_flip[asset["name"]] = certain_flip
                certain_flip_alert_ts = time.time() if certain_flip_just_fired else None

                # protective-sell signal - added 2026-09-26 per user request,
                # its OWN edge-trigger at its OWN (stricter, configurable)
                # confidence bar - deliberately separate from certain_flip
                # above (see _live_protect_min_confidence note): a real
                # backtest found the flip-scalp/alert bar (55%) lets through
                # too many false alarms for a SELL decision, where being
                # wrong costs a position that would have won.
                protect_signal = (dramatic_vs_call == "against" and locked["my_call"] is not None
                                    and cur_flip_chance is not None
                                    and cur_flip_chance >= _live_protect_min_confidence)
                protect_signal_just_fired = protect_signal and not _last_protect_signal.get(asset["name"], False)
                _last_protect_signal[asset["name"]] = protect_signal

                fair_yes, fair_sigma_bps_min = (None, None) if is_gold else fair_yes_probability(
                    asset["symbol"], live_price, target, seconds_left)
                _log_intrawindow_price({
                    "yes_bid": fresh_quote[0] if fresh_quote is not None else active.get("yes_bid_dollars"),
                    "yes_ask": fresh_quote[1] if fresh_quote is not None else active.get("yes_ask_dollars"),
                    "quote_source": "orderbook" if calls_use_live_quote else "list",
                    "fair_yes": round(fair_yes, 4) if fair_yes is not None else None,
                    "fair_sigma_bps_min": round(fair_sigma_bps_min, 3) if fair_sigma_bps_min is not None else None,
                    "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "asset": asset["name"],
                    "ticker": active["ticker"],
                    "seconds_left": round(seconds_left),
                    "kalshi_price": kalshi_price,
                    "spot_price": round(live_price, price_decimals) if live_price is not None else None,
                    "target": target,
                    "leaning": leaning,
                    "confidence": round(confidence, 4) if confidence is not None else None,
                    "locked_call": locked["my_call"],
                    "dramatic_swing": dramatic_swing,
                    "dramatic_vs_call": dramatic_vs_call,
                    "certain_flip": certain_flip,
                })

                # manual-sell detection - added 2026-09-27 per user request
                # ("keep track of when I actually sell out manually, because
                # I don't think it knows"). Runs BEFORE anything else that
                # reads _live_open_positions this poll, so a manual close
                # picked up just now is already reflected for the flip-scalp/
                # protective-sell checks below, not one poll stale.
                if active["ticker"] in _live_open_positions:
                    check_manual_position_change(active["ticker"])

                # real-money auto-buy - rebuilt 2026-09-26 per user report: the
                # old version fired a burst of 4 rapid attempts in a couple
                # seconds, ONCE, at the exact moment the call locked, then
                # gave up for the rest of the window. A real miss showed the
                # order book can be genuinely thin for just that moment - the
                # user was able to buy manually later in the SAME window
                # ("it lingered around... I sat here and waited and waited,
                # it had plenty of time"). Now: ONE order attempt per POLL
                # (not a burst), topping up toward the full bet size if a
                # prior poll already got a partial fill, keeps trying for up
                # to LIVE_BUY_RETRY_WINDOW_SECONDS after the first attempt
                # instead of a single few-second burst. Same underlying
                # accumulation math as before, just spread across real time.
                # wait N candles after the call before buying (per-asset option,
                # default 0). Elapsed time since the lock = seconds-left at the
                # lock minus seconds-left now. The 90s retry window below only
                # starts once the wait is over.
                _lb_delay_s = _live_buy_delay_candles.get(asset["name"], 0) * 60
                _lb_waited = (locked.get("locked_seconds_left") is None
                              or (locked["locked_seconds_left"] - seconds_left) >= _lb_delay_s)
                if (asset["name"] in LIVE_ASSETS and _live_armed.get(asset["name"])
                        and locked["my_call"] is not None
                        and _lb_waited
                        and active["ticker"] not in _live_buy_gave_up):
                    _lb_ticker = active["ticker"]
                    _lb_now = time.time()
                    # busted-call guard - added 2026-09-30 after a real loss:
                    # this loop used to buy blindly off the stale locked call
                    # every poll with no regard for how the live read had
                    # moved since. On XRP ticker KXXRP15M-26SEP292015-15 the
                    # locked call was YES while the live leaning sat at NO
                    # with 98%+ confidence for the entire window (kalshi_price
                    # for YES collapsed to ~1.5c) - certain_flip had already
                    # fired TWICE before this buy loop even started, yet it
                    # bought anyway and lost the full stake. certain_flip
                    # means the live read has already flagged the locked call
                    # as busted (>=CERTAIN_FLIP_MIN_CHANCE the market flipped
                    # against it) - refuse to open or add to a position on
                    # that read rather than trusting a call that's already
                    # known-wrong. This does not sell an existing fill (that's
                    # protective-sell's job, gated separately) - it only stops
                    # digging the hole deeper while the warning is on.
                    # CHANGED 2026-10-04 per user request: the warning now PAUSES
                    # the buy instead of cancelling it for the window - if the flip
                    # meter comes back down and the call still stands, the buy
                    # goes ahead. Only if the warning lasts until the window is
                    # about to close does it give up (and log that).
                    _lb_flip_hot = certain_flip or (cur_flip_chance is not None
                                                      and cur_flip_chance >= CERTAIN_FLIP_MIN_CHANCE)
                    if _lb_flip_hot:
                        _lb_fully_filled = False
                        if _lb_ticker not in _live_buy_paused_logged:
                            _live_buy_paused_logged.add(_lb_ticker)
                            print(f"[LIVE {asset['name']}] {_lb_ticker}: buy PAUSED - flip warning is on "
                                  f"against the locked call; will buy if it clears while the call stands")
                        with _live_lock:
                            _lb_pos = _live_open_positions.get(_lb_ticker)
                        if seconds_left <= 10 and _lb_pos is None:
                            _live_buy_gave_up.add(_lb_ticker)
                            _live_append_log({
                                "ts": dt.datetime.now(dt.timezone.utc).isoformat(), "event": "buy",
                                "asset": asset["name"], "ticker": _lb_ticker, "strategy": "lock",
                                "side": locked["my_call"].lower(), "contracts": 0, "price": None,
                                "cost_dollars": 0.0, "payoff_dollars": None, "order_id": "", "filled": False,
                                "entry_flip_chance": "", "entry_swing_pct": "",
                                "flip_sell_mode": "", "flip_hold_if_certain": "",
                                "sell_reason": "gave up - flip warning stayed on against the locked call until the window closed",
                            })
                            print(f"[LIVE {asset['name']}] {_lb_ticker}: gave up - flip warning never cleared")
                    else:
                        if _lb_ticker not in _live_buy_deadline:
                            _live_buy_deadline[_lb_ticker] = _lb_now + LIVE_BUY_RETRY_WINDOW_SECONDS
                        _lb_attempt_n = _live_buy_attempt_n.get(_lb_ticker, 0)
                        _lb_fully_filled = place_live_buy(asset["name"], _lb_ticker, locked["my_call"].lower(),
                                                            attempt_num=_lb_attempt_n)
                        _live_buy_attempt_n[_lb_ticker] = _lb_attempt_n + 1
                        # a FAILED order (API error, insufficient balance, timeout) must not
                        # use up the retry window - keep trying every poll until the round
                        # is nearly over (added 2026-10-04 per user request)
                        with _live_lock:
                            _lb_err = _live_last_error
                        if _lb_err and str(_lb_err).startswith("BUY FAILED") and seconds_left > 25:
                            _live_buy_deadline[_lb_ticker] = max(_live_buy_deadline[_lb_ticker], time.time() + 30)
                        if _lb_fully_filled or _lb_now >= _live_buy_deadline[_lb_ticker]:
                            _live_buy_gave_up.add(_lb_ticker)
                            with _live_lock:
                                _lb_pos = _live_open_positions.get(_lb_ticker)
                            if not _lb_fully_filled and _lb_pos is None:
                                # never got ANY fill across the whole retry window -
                                # log one "gave up" row so the CSV still shows the
                                # attempt honestly (same as the old always-log
                                # behavior), instead of silently logging nothing
                                _live_append_log({
                                    "ts": dt.datetime.now(dt.timezone.utc).isoformat(), "event": "buy",
                                    "asset": asset["name"], "ticker": _lb_ticker, "strategy": "lock",
                                    "side": locked["my_call"].lower(), "contracts": 0, "price": None,
                                    "cost_dollars": 0.0, "payoff_dollars": None, "order_id": "", "filled": False,
                                    "entry_flip_chance": "", "entry_swing_pct": "",
                                    "flip_sell_mode": "", "flip_hold_if_certain": "",
                                })
                                print(f"[LIVE {asset['name']}] {_lb_ticker}: gave up after "
                                      f"{LIVE_BUY_RETRY_WINDOW_SECONDS}s - never got any fill")

                # flip-scalp entry - added 2026-09-25 per user request. Only
                # when: armed for this asset, we're NOT already holding a
                # position on this ticker (either strategy - never stacks on
                # top of the main strategy's buy), and we haven't already
                # used a flip entry on this ticker before ("stay out" for the
                # rest of the window once used, win/lose/sold). Buys the NEW
                # (flipped-to) direction, i.e. the current leaning - that's
                # exactly what "against the locked call" already means here.
                #
                # Fires on EITHER of two triggers:
                # 1. certain_flip_just_fired - the normal case, a real flip
                #    just fired while already flat.
                # 2. the follow-up window (added 2026-09-26 per user request)
                #    - right after a protective sell clears a lock position,
                #    scheduled by sell_live_position. Needed because trigger
                #    1's edge-trigger usually already fired (and was wasted,
                #    blocked by the lock position still being open) BEFORE
                #    protective-sell's stricter bar got crossed - without
                #    this, flip-scalp would just miss its shot every time.
                _lf_followup_ready = (active["ticker"] in _live_flip_followup
                                        and time.time() >= _live_flip_followup[active["ticker"]])
                if (asset["name"] in LIVE_ASSETS and _live_flip_armed.get(asset["name"])
                        and (certain_flip_just_fired or _lf_followup_ready) and leaning is not None
                        and active["ticker"] not in _live_open_positions
                        and _live_flip_last_ticker.get(asset["name"]) != active["ticker"]):
                    if _lf_followup_ready:
                        del _live_flip_followup[active["ticker"]]  # one-shot - never re-fire off this same sell
                    entry_signal = {
                        "flip_chance": cur_flip_chance,
                        "swing_pct": _current_swing_pct(asset["symbol"]),
                        "sell_mode": _live_flip_sell_mode,
                        "hold_if_certain": _live_flip_hold_if_certain,
                    }
                    place_live_buy(asset["name"], active["ticker"], leaning.lower(), strategy="flip",
                                    entry_signal=entry_signal)
                elif _lf_followup_ready:
                    _live_flip_followup.pop(active["ticker"], None)  # conditions no longer met
                                                                       # (e.g. already re-filled,
                                                                       # already used) - drop it,
                                                                       # don't keep checking forever

                # flip-scalp exit check - every poll, for whatever's
                # currently open on this window (no-op if nothing's open or
                # it's a "lock"-strategy position, see check_flip_position_exit)
                check_flip_position_exit(active["ticker"])

                # live unrealized gain/price for an open LOCK position -
                # DISPLAY only, added 2026-09-27 per user request ("show it
                # on the boxes... so I can keep track... without having to
                # look back and forth"). Computed here in poll_loop (~every
                # 2s) rather than per-render, so showing it on the page
                # never adds an extra network call per browser refresh.
                main_pos_gain = main_pos_price = None
                flip_pos_gain = flip_pos_price = None
                with _live_lock:
                    _main_pos_for_gain = _live_open_positions.get(active["ticker"])
                if _main_pos_for_gain and _main_pos_for_gain.get("strategy", "lock") == "lock":
                    main_pos_gain, main_pos_price = _position_current_gain_and_price(_main_pos_for_gain)
                    # take-profit at a Kalshi price level - added 2026-10-02 per
                    # user request. main_pos_price is the price we could sell
                    # the held side at right now. Only fires when the target is
                    # ABOVE what we paid - otherwise it would sell instantly at
                    # a loss of the spread. sell_live_position puts an unfilled
                    # position back, so this simply retries on the next poll.
                    _tp = _live_main_sell_price
                    if _tp is not None and main_pos_price is not None and main_pos_price >= _tp:
                        _entry_q = _main_pos_for_gain.get("price")
                        _entry_cost = None
                        if _entry_q is not None:
                            _entry_cost = _entry_q if _main_pos_for_gain.get("side") == "yes" else (1 - _entry_q)
                        if _entry_cost is not None and _tp > _entry_cost:
                            _live_buy_gave_up.add(active["ticker"])  # stop any top-up buying on this window
                            sell_live_position(active["ticker"],
                                               f"take-profit at {main_pos_price:.0%} Kalshi price (target {_tp:.0%})")
                elif _main_pos_for_gain and _main_pos_for_gain.get("strategy") == "flip":
                    flip_pos_gain, flip_pos_price = _position_current_gain_and_price(_main_pos_for_gain)

                # protective-sell - added 2026-09-26 per user request, a
                # THIRD independent switch: sells an EXISTING real "lock"
                # position early when the (own, stricter) protect_signal
                # fires against it - only ever touches strategy="lock"
                # positions, never stacks with or interferes with flip-scalp.
                # Rewritten 2026-09-27 per real user report ("I said it was
                # going to flip, and then it was a delay before it actually
                # sold me out"): the edge-trigger below still marks the
                # ticker, but now every poll after that keeps retrying via
                # sell_live_position until the position is actually gone,
                # instead of trying exactly once.
                if (asset["name"] in LIVE_ASSETS and _live_protect_armed.get(asset["name"])
                        and protect_signal_just_fired):
                    _live_protect_selling.add(active["ticker"])
                if active["ticker"] in _live_protect_selling:
                    with _live_lock:
                        _protect_pos = _live_open_positions.get(active["ticker"])
                    if _protect_pos and _protect_pos.get("strategy", "lock") == "lock":
                        sell_live_position(active["ticker"],
                                            f"protective sell - flip chance hit {cur_flip_chance:.0%} "
                                            f"(bar {_live_protect_min_confidence:.0%})")
                    else:
                        _live_protect_selling.discard(active["ticker"])  # fully sold (or never
                                                                            # was a lock position) -
                                                                            # stop retrying

                whale = get_whale_flow(asset["symbol"])
                book_walls = get_book_walls(asset["symbol"], live_price)
                volume_recent = get_recent_volume(asset["symbol"])
                volume_tug = get_volume_tug_of_war(asset["symbol"])
                daily_high, daily_low = get_daily_range(asset["symbol"])

                asset_history = _history.get(asset["name"], [])
                graded = [h for h in asset_history if h["correct"] is not None]
                # "no call" locks (below our own confidence bar) are counted in
                # windows_resolved but deliberately excluded from track_record -
                # there's no honest way to grade a call we didn't make.
                track_record = (sum(1 for h in graded if h["correct"]), len(graded))
                windows_resolved = len(asset_history)
                no_call_n = windows_resolved - len(graded)
                streak_type, streak_count = get_streak(asset_history)
                bet_pnl = compute_cumulative_bet_pnl(asset_history, series_prefix=asset["series"])

                snapshot[asset["name"]] = {
                    "ticker": active["ticker"],
                    "seconds_left": round(seconds_left),
                    "spot_price": round(live_price, price_decimals) if live_price is not None else None,
                    "live_venues": live_venues,
                    "target_price": target,
                    "kalshi_price": kalshi_price,
                    "confidence": confidence,
                    "leaning": leaning,
                    "spot_ret": spot_ret,
                    "regime": regime,
                    "regime_ret": regime_ret,
                    "swept": swept,
                    "sweep_just_fired": sweep_just_fired,
                    "micro_dir": micro_dir,
                    "micro_pct": micro_pct,
                    "my_call": my_call,
                    "predicted_close_price": round(predicted_close_price, price_decimals) if predicted_close_price is not None else None,
                    "locked_call": locked["my_call"],
                    "locked_price": locked["predicted_price"],
                    "locked_kalshi_price": locked.get("locked_kalshi_price"),
                    "locked_seconds_left": locked["locked_seconds_left"],
                    "fair_yes": fair_yes,
                    "fair_sigma_bps_min": fair_sigma_bps_min,
                    "quote_source": "orderbook" if calls_use_live_quote else "list",
                    "track_record": track_record,
                    "windows_resolved": windows_resolved,
                    "no_call_n": no_call_n,
                    "streak_type": streak_type,
                    "streak_count": streak_count,
                    "last_result": asset_history[-1] if asset_history else None,
                    "dramatic_swing": dramatic_swing,
                    "dramatic_vs_call": dramatic_vs_call,
                    "just_locked": just_locked,
                    "boom_just_fired": boom_just_fired,
                    "boom_alert_ts": boom_alert_ts,
                    "certain_flip_just_fired": certain_flip_just_fired,
                    "certain_flip_alert_ts": certain_flip_alert_ts,
                    "whale": whale,
                    "incoming": (_flow.HUB.incoming(asset["name"]) if asset["name"] in _flow.ASSETS else None),
                    "chart_svg": chart_svg,
                    "book_walls": book_walls,
                    "volume_recent": volume_recent,
                    "volume_tug": volume_tug,
                    "daily_high": daily_high,
                    "daily_low": daily_low,
                    "main_pos_gain": main_pos_gain,
                    "main_pos_price": main_pos_price,
                    "flip_pos_gain": flip_pos_gain,
                    "flip_pos_price": flip_pos_price,
                    "experimental": asset.get("experimental", False),
                    "choppiness": round(choppiness, 6) if choppiness is not None else None,
                    "choppiness_threshold": chop_threshold,
                    "chopped": chopped,
                    "bias_correction": round(bias_correction, price_decimals),
                    "bias_samples": bias_samples,
                    "ranges": ranges,
                    "updated_at": now.isoformat(),
                    "rsi": (lambda r: round(r[0], 1) if r[0] is not None else None)(_rsi_all_exchanges(asset["symbol"], rsi_value)),
                    "rsi_src": _rsi_all_exchanges(asset["symbol"], rsi_value)[1],
                    "bet_pnl": bet_pnl,
                    "checkpoints": dict(checkpoints),
                    "flip_chance": (round(flip_chance_vs_locked_call(kalshi_price, locked.get("my_call"), confidence), 4)
                                     if confidence is not None else None),
                    "price_decimals": price_decimals,
                    "late_call": locked.get("late_call"),
                    "late_confidence": locked.get("late_confidence"),
                    "late_flip_chance": locked.get("late_flip_chance"),
                    "late_predicted_price": locked.get("late_predicted_price"),
                    "late_locked_seconds_left": locked.get("late_locked_seconds_left"),
                }
            except Exception as exc:
                snapshot[asset["name"]] = {"error": str(exc)}

        # synthetic assets (NEAR, BNB, ...) - no real Kalshi market, so these
        # run on a SYNTHETIC window: same 15-min cadence as BTC's real Kalshi
        # market (reused for timing sync only), with the asset's own price
        # at window-open as the settlement reference - mirrors exactly how
        # BTC/ETH's real markets settle relative to their own open. Same
        # validated call logic (confidence floor + strict direction + sweep
        # filter, thresholds scaled per-asset for its own measured
        # volatility), same locking/grading/persistence/learning as BTC and
        # ETH. The one thing that's genuinely different: there's no live
        # market price for these, so "confidence" is a self-computed proxy -
        # NOT sourced from a real market's aggregated view like BTC/ETH's is.
        try:
            btc_active = get_active_market(ASSETS[0]["series"])  # KXBTC15M, timing sync only
        except Exception as exc:
            btc_active = None
            _btc_sync_error = str(exc)
        else:
            _btc_sync_error = "no active BTC market to sync window with" if btc_active is None else None

        for pseudo_asset in SYNTHETIC_ASSETS:
            try:
                if btc_active is None:
                    raise RuntimeError(_btc_sync_error)
                snapshot[pseudo_asset["name"]] = compute_synthetic_asset_snapshot(pseudo_asset, btc_active)
            except Exception as exc:
                snapshot[pseudo_asset["name"]] = {"error": str(exc)}

        with _lock:
            _state.update(snapshot)
        for _n, _dd in snapshot.items():
            if "error" not in _dd:
                try:
                    _tug_log(_n, _dd)
                except Exception:
                    pass
        _el = time.time() - _t_loop
        _poll_loop_t["last"] = round(_el, 2)
        _poll_loop_t["n"] += 1
        _poll_loop_t["avg"] = round(_poll_loop_t["avg"] + (_el - _poll_loop_t["avg"]) / min(_poll_loop_t["n"], 30), 2)
        time.sleep(2)


PAGE_TEMPLATE = r"""<!doctype html>
<html><head>
<script>
/* independent reload watcher (2026-10-06): lives in its own script tag so a bug in the big script below can never
   leave a tab dead - it reloads the page whenever the dashboard restarts with a new build */
(function () {
  let b0 = null;
  setInterval(function () {
    fetch('/build', {cache: 'no-store'}).then(function (r) { return r.text(); }).then(function (b) {
      if (b0 === null) b0 = b; else if (b !== b0) location.reload();
    }).catch(function () {});
  }, 5000);
})();
</script><meta charset="utf-8"><meta name="viewport" content="width=820"><title>The Dominator Live Dashboard</title>
<style>
body { font-family: -apple-system, Segoe UI, Arial, sans-serif; background:#0d1117; color:#e6edf3; margin:0; padding:12px 24px 24px; }
h1 { font-size: 15px; color:#9d9d9d; margin:0 0 8px; }
.view-bar { margin:6px 0 2px; display:flex; gap:6px; align-items:center; }
body[data-view="BTC"] .card[data-asset="XRP"], body[data-view="XRP"] .card[data-asset="BTC"] { display:none; }
body[data-view="BTC"] .card, body[data-view="XRP"] .card { max-width:1100px; }
/* ONE coin on screen + a wide window: use the empty space - chart on the left, the pattern list beside it on the right (no scrolling down to read it) */
@media (min-width:1200px) {
  body[data-view="BTC"] .card, body[data-view="XRP"] .card { max-width:1800px; flex:1 1 100%; }
  body[data-view="BTC"] .pat-win, body[data-view="XRP"] .pat-win { display:grid; grid-template-columns:minmax(0,1.7fr) minmax(360px,1fr); column-gap:16px; align-items:start; }
  body[data-view="BTC"] .pat-win > .pat-win-head, body[data-view="XRP"] .pat-win > .pat-win-head,
  body[data-view="BTC"] .pat-win > .cpi-drop, body[data-view="XRP"] .pat-win > .cpi-drop { grid-column:1 / -1; }
  body[data-view="BTC"] .pat-win > .pat-chart-row, body[data-view="XRP"] .pat-win > .pat-chart-row { grid-column:1; grid-row:3; }
  body[data-view="BTC"] .pat-win > .hint, body[data-view="XRP"] .pat-win > .hint { grid-column:1; grid-row:4; }
  body[data-view="BTC"] .pat-win > .pat-win-legend, body[data-view="XRP"] .pat-win > .pat-win-legend {
    grid-column:2; grid-row:3 / span 2; margin-top:0; max-height:84vh; overflow-y:auto; padding:0 6px 0 2px; border-left:1px solid #30363d; padding-left:12px; }
  body[data-view="BTC"] .pat-win .pat-chart svg, body[data-view="XRP"] .pat-win .pat-chart svg { max-height:84vh; }
}
#content { display:flex; flex-wrap:wrap; gap:20px; align-items:flex-start; margin-top:10px; }
.sound-details { display:inline-block; vertical-align:middle; margin-left:8px; }
.sound-details summary { display:inline-block; cursor:pointer; color:#8b949e; font-size:14px; padding:6px 10px; background:#21262d; border:1px solid #30363d; border-radius:6px; }
.sound-details summary:hover { background:#30363d; }
.sound-details[open] summary { margin-bottom:6px; }
.sound-details-body { padding-top:4px; }
.card { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:20px; margin-bottom:20px; flex:1 1 640px; min-width:640px; max-width:900px; }
.chart-wrap { margin-top:6px; }
.chart-controls { display:flex; justify-content:flex-end; gap:4px; margin-bottom:2px; }
.chart-tf-btn { background:#21262d; color:#8b949e; border:1px solid #30363d; border-radius:5px; padding:2px 9px; font-size:12px; cursor:pointer; }
.chart-tf-btn:hover { background:#30363d; }
.chart-tf-btn.chart-tf-active { background:#0d2818; border-color:#3fb950; color:#3fb950; }
/* whale-cross: position/left are set inline per-instance (JS computes the
   target card's on-screen rect) since the card it flies over is rebuilt via
   innerHTML every 3s poll - a child element would get wiped mid-animation,
   so this lives under <body> instead and is just visually aligned over the
   card. See showWhaleCrossing(). */
.whale-cross { font-size:90px; white-space:nowrap; pointer-events:none; z-index:9999; }
.whale-cross.whale-buy { filter: drop-shadow(0 0 6px #3fb950); }
.whale-cross.whale-sell { filter: drop-shadow(0 0 6px #f85149); }
/* big, screen-center "for sure" flip alert - added 2026-09-24 per user
   request for something bigger/louder than the small BOOM pill. Fixed under
   <body> (not #content, which gets wiped every 1s poll) so it survives the
   whole time it's showing. pointer-events:none so it never blocks clicking
   whatever's underneath. */
.big-flip-alert { position:fixed; top:50%; left:50%; z-index:10000; pointer-events:none;
                   max-width:92vw; text-align:center; padding:20px 28px; border-radius:14px;
                   background:rgba(13,17,23,0.95); border:3px solid; }
/* NOTE: all motion (fade + scale in/out) is driven entirely by the JS
   Web Animations API call in showBigFlipAlert() below, via `transform` -
   deliberately NOT also a CSS `animation` here. A CSS animation on the same
   property fights the JS-driven transform instead of combining with it (the
   same conflict that broke the flame effect earlier in this project) and
   would drop the translate(-50%,-50%) centering entirely. */
.big-flip-alert.flip-down { border-color:#f85149; box-shadow:0 0 40px rgba(248,81,73,0.6); }
.big-flip-alert.flip-up { border-color:#3fb950; box-shadow:0 0 40px rgba(63,185,80,0.6); }
.big-flip-title { font-size:clamp(22px,5vw,40px); font-weight:900; color:#fff; letter-spacing:0.5px; }
.big-flip-sub { font-size:14px; color:#8b949e; margin-top:8px; }
/* "cha-ching" money banner - added 2026-09-27 per user request ("if the bet
   is won, I'd like a cha-ching with a money symbol to go across the
   screen"). Same technique as .big-flip-alert: fixed position, motion
   driven entirely by the JS Web Animations API call in showMoneyBanner()
   below (animating `left` across the viewport), no CSS animation here. */
.money-banner { position:fixed; top:45%; z-index:10001; pointer-events:none;
                 font-size:clamp(20px,4vw,34px); font-weight:900; color:#3fb950;
                 text-shadow:0 0 16px rgba(63,185,80,0.85); white-space:nowrap; }
.money-banner-emoji { font-size:5.5em; vertical-align:middle; }  /* "make sure it's big" per user
                                                                     request 2026-09-27, bumped up
                                                                     from the initial 3em */
/* fireworks burst particles - added 2026-09-27 per user request ("fireworks").
   Motion driven entirely by the JS Web Animations API call in
   spawnFireworks() below, same no-CSS-animation rule as everything else
   fixed-position on this page. */
.firework-particle { position:fixed; z-index:10002; pointer-events:none; font-size:32px; }
/* loss banner - added 2026-09-27 per user request ("when it's wrong...
   fuck you, Kalshi"). Same technique/motion as .money-banner. */
.loss-banner { position:fixed; top:45%; z-index:10001; pointer-events:none;
                font-size:clamp(20px,4vw,34px); font-weight:900; color:#f85149;
                text-shadow:0 0 16px rgba(248,81,73,0.85); white-space:nowrap; }
.loss-banner-emoji { font-size:5.5em; vertical-align:middle; }  /* "make sure it's big" - same
                                                                    bump as the money emoji */
.asset-name { font-size: 22px; font-weight:700; margin-bottom: 0; }
.row { display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid #21262d; }
.label { color:#8b949e; }
.value { font-weight:600; }
.up { color:#3fb950; }
.fire-streak { display:flex; justify-content:flex-end; margin-bottom:2px; }
/* real-fire technique: blurred, glowing, layered blobs (not a vector
   outline) - soft irregular edges + screen blend = reads like actual flame/
   video footage instead of a clean icon. Each layer flickers at its own
   speed/delay so nothing looks static or perfectly symmetric. */
.flame-fx { position:relative; width:46px; height:58px; }
.flame-blob { position:absolute; left:50%; bottom:0; border-radius:50% 50% 45% 45% / 60% 60% 40% 40%;
              filter: blur(4px); mix-blend-mode: screen; transform-origin: 50% 100%; }
.flame-glow-3 { margin-left:-23px; animation: flame-wave-3 1.1s ease-in-out infinite; }
.flame-glow-2 { margin-left:-17px; animation: flame-wave-2 0.8s ease-in-out infinite; animation-delay:0.1s; }
.flame-glow-1 { margin-left:-12px; animation: flame-wave-1 0.55s ease-in-out infinite; animation-delay:0.18s; }
.flame-core { position:absolute; left:50%; bottom:3px; width:9px; height:16px; margin-left:-4.5px;
              border-radius:50%; filter: blur(2px); mix-blend-mode: screen;
              background: radial-gradient(circle, #ffffff 0%, #ffea00 55%, transparent 100%);
              animation: flame-wave-1 0.4s ease-in-out infinite alternate; }
@keyframes flame-wave-3 { 0%,100% { transform:scaleY(1) scaleX(1) translateX(0) skewX(0deg); opacity:0.55; }
                          50% { transform:scaleY(1.1) scaleX(0.88) translateX(-2px) skewX(-3deg); opacity:0.75; } }
@keyframes flame-wave-2 { 0%,100% { transform:scaleY(1) scaleX(1) translateX(0) skewX(0deg); opacity:0.7; }
                          50% { transform:scaleY(1.14) scaleX(0.85) translateX(2px) skewX(4deg); opacity:0.9; } }
@keyframes flame-wave-1 { 0%,100% { transform:scaleY(1) scaleX(1) translateX(0); opacity:0.9; }
                          50% { transform:scaleY(1.16) scaleX(0.82) translateX(-1px); opacity:1; } }
.smoke-streak { font-size:16px; line-height:1; text-align:right; margin-bottom:2px; opacity:0.7; animation: smoke-rise 2.2s ease-in-out infinite; }
@keyframes smoke-rise { 0% { opacity:0; transform:translateY(4px) scale(0.9); } 30% { opacity:0.7; } 100% { opacity:0; transform:translateY(-10px) scale(1.15); } }
.down { color:#f85149; }
.warn { color:#d29922; }
.position-tag { color:#58a6ff; }  /* live position summary label - added 2026-09-27 per user
                                     request ("a different color other than white green or red...
                                     so it stands out") - a distinct cyan-blue, not reused anywhere
                                     else as up/down/warn/locked-call already claim green/red/gold */
.ranges { margin-top:10px; font-size: 14px; color:#8b949e; }
.timer { font-size: 14px; color:#8b949e; }
.flat { color:#8b949e; }
.callbox { box-sizing:border-box; display:flex; flex-direction:column; justify-content:center;
           background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:5px 6px;
           margin-left:6px; text-align:center; width:92px; flex:0 1 92px; }
.live-price-inline { box-sizing:border-box; display:flex; flex-direction:column; justify-content:center;
                      margin-left:6px; text-align:center; min-width:130px; flex:0 1 auto; }
.live-price-inline .big { font-size:26px; font-weight:800; line-height:1.1; }
.tug { margin:8px 0; padding:10px 12px; background:#0d1117; border:1px solid #30363d; border-radius:8px; }
.tug-title { font-size:15px; font-weight:800; letter-spacing:0.4px; margin-bottom:6px; }
.tug-top { display:flex; justify-content:flex-start; align-items:flex-end; gap:10px; }
.tug-big { font-size:34px; font-weight:800; line-height:1.1; font-variant-numeric:tabular-nums; }
.tug-pair { display:flex; flex-wrap:wrap; align-items:flex-end; gap:6px 20px; }
.tug-vs { align-self:center; margin-top:8px; font-size:22px; }
.tug .hint { font-size:14px; color:#a6b0bb; }
.tug-diff { font-size:34px; font-weight:800; line-height:1.1; font-variant-numeric:tabular-nums; }
.tug-gauge { position:relative; height:26px; margin:6px 0 0; display:flex; align-items:center; justify-content:space-between; }
.tug-gline { position:absolute; left:6%; right:6%; top:50%; height:2px; background:#30363d; }
.tug-gauge i { position:absolute; top:3px; width:16px; height:20px; margin-left:-8px; border-radius:50%; background:#3fb950; box-shadow:0 0 8px #3fb950; }
.tug-gl, .tug-gr { position:relative; z-index:1; font-size:14px; }
.tug .tug-gauge .tug-gl.hint { color:#ff7b72 !important; font-size:16px; font-weight:800; }
.tug .tug-gauge .tug-gr.hint { color:#56d364 !important; font-size:16px; font-weight:800; }
.tug-gauge i.tug-coin { top:0; width:26px; height:26px; margin-left:-13px; font-style:normal; font-weight:900; font-size:17px; line-height:20px; text-align:center; color:#fff;
  background:#f7931a; border:3px solid #3fb950; box-shadow:0 0 8px rgba(63,185,80,0.8); font-family:"Segoe UI Symbol","Apple Symbols","Arial",sans-serif; box-sizing:border-box; }
.tug-gauge i.tug-coin.down { border-color:#f85149; box-shadow:0 0 8px rgba(248,81,73,0.8); }
.tug-gauge i.tug-coin-xrp { background:#23292f; color:#e6edf3; }
.tug-cap { text-align:center; font-size:14px; margin-bottom:4px; }
.tug-head { font-size:26px; font-weight:800; margin:4px 0; }
.tug-lean { font-size:17px; margin-top:2px; }
.tug-bar { position:relative; height:12px; border-radius:6px; background:#da3633; margin:4px 0 8px; }
.tug-bar i { position:absolute; left:0; top:0; bottom:0; border-radius:6px 0 0 6px; background:#238636; }
.tug-bar b { position:absolute; top:-3px; width:4px; height:18px; margin-left:-2px; background:#e6edf3; border-radius:2px; }
.tug-row { display:flex; align-items:center; gap:10px; font-size:16px; margin:5px 0; }
.tug-lab { flex:0 0 235px; color:#c9d1d9; }
.tug-track { position:relative; flex:1 1 auto; height:10px; background:#21262d; border-radius:3px; overflow:hidden; }
.tug-fill { position:absolute; top:0; bottom:0; border-radius:3px; }
.tug-center::after { content:""; position:absolute; left:50%; top:-2px; bottom:-2px; width:2px; margin-left:-1px; background:#e6edf3; opacity:0.7; }
.tug-fill.up { background:#3fb950; } .tug-fill.down { background:#f85149; } .tug-fill.flat { background:#8b949e; }
.tug-val { flex:0 0 210px; text-align:right; font-weight:800; }
.tug-src { margin:3px 0 6px; font-size:13px; line-height:1.35; }
.tug-boxes { display:flex; gap:8px; margin:8px 0 6px; }
.tug-box { flex:1 1 0; padding:8px 12px; border-radius:6px; font-size:16px; border:1px solid #30363d; }
.tug-box.up { background:#0f2a1a; border-color:#238636; } .tug-box.down { background:#2a1212; border-color:#da3633; }
.tug-box span { color:#a6b0bb; font-size:14px; }
.tug-agree { font-size:15px; padding:5px 10px; border-radius:6px; margin:4px 0; }
.tug-agree.ok { background:#0f2a1a; border:1px solid #238636; color:#3fb950; }
.tug-agree.warn { background:#2d2606; border:1px solid #d4b106; color:#e3b341; }
.tug-around { margin-top:6px; font-size:14px; }
.line-diff { font-size:13px; font-weight:800; margin-top:2px; white-space:nowrap; }
.live-price-inline .lbl { font-size:12px; color:#8b949e; text-transform:uppercase; }
.callbox .big { font-size:14px; font-weight:700; line-height:1.2; }
.callbox .lbl { font-size:11px; color:#8b949e; text-transform:uppercase; line-height:1.25; }
.asset-label { font-weight:800; letter-spacing:0.5px; }
.asset-BTC { color:#f7931a; } .asset-XRP { color:#4aa8ff; }
.clock { display:flex; align-items:center; gap:5px; margin-top:4px; font-family:Consolas, 'Courier New', monospace; font-size:17px; font-weight:700; color:#e6edf3; }
.clock svg { display:block; }
.clock.clock-warn { color:#d29922; } .clock.clock-hot { color:#f85149; }
.callbox-locked { background:#2d2606; border:2px solid #d4b106; box-shadow:0 0 8px rgba(212,177,6,0.5); }
.callbox-locked .lbl { color:#f0d040; }
.callbox-locked .big { color:#ffe680; }
.head-row { display:flex; align-items:stretch; justify-content:flex-start; flex-wrap:wrap; row-gap:12px; }
.head-row .asset-name { flex:0 0 auto; align-self:center; min-width:70px; }
.head-row .kalshi-arrow { margin-left:6px; flex:1 1 0; min-width:70px; align-items:center; justify-content:flex-start; }
.head-row .kalshi-arrow .knight-svg { width:100%; max-width:262px; height:auto; }
.head-row .kalshi-arrow-lbl { font-size:13px; margin-top:-6px; text-align:center; }
.head-row .kalshi-arrow-pct { font-size:19px; text-align:center; }
.boom { display:inline-block; padding:6px 16px; border-radius:20px; font-weight:800; font-size:14px;
        margin-left:12px; animation: boom-pulse 0.9s infinite; vertical-align:middle; }
.boom-up { background:#0d2818; color:#3fb950; border:2px solid #3fb950; }
.boom-down { background:#2d0d0d; color:#f85149; border:2px solid #f85149; }
@keyframes boom-pulse { 0% { opacity:1; transform:scale(1); } 50% { opacity:0.65; transform:scale(1.08); } 100% { opacity:1; transform:scale(1); } }
.meter-track { display:inline-block; width:90px; height:8px; background:#0d1117; border:1px solid #30363d;
               border-radius:4px; overflow:hidden; vertical-align:middle; margin-left:8px; }
.meter-fill { height:100%; border-radius:3px; }
.meter-fill-up { background:#3fb950; }
.meter-fill-down { background:#f85149; }
.sound-bar { margin-bottom:16px; padding:12px 16px; background:#161b22; border:1px solid #30363d; border-radius:8px; }
.sound-bar button { background:#21262d; color:#e6edf3; border:1px solid #30363d; border-radius:6px; padding:8px 14px; margin-right:8px; margin-bottom:6px; cursor:pointer; font-size:14px; }
.sound-bar button:hover { background:#30363d; }
#sound-enable-btn { font-weight:800; font-size:17px; padding:8px 20px; border:2px solid; border-radius:8px; letter-spacing:0.3px; cursor:pointer; }
#sound-enable-btn.sound-on { background:#0f3a1f; border-color:#3fb950; color:#56d364; }
#sound-enable-btn.sound-off { background:#3a0f0f; border-color:#f85149; color:#ff7b72; animation:soundOffPulse 1.8s ease-in-out infinite; }
@keyframes soundOffPulse { 0%,100% { box-shadow:0 0 0 rgba(248,81,73,0); } 50% { box-shadow:0 0 10px rgba(248,81,73,0.6); } }
.asset-sound-on { background:#0d2818; border-color:#3fb950; color:#3fb950; }
.asset-sound-off { background:#2d0d0d; border-color:#f85149; color:#f85149; }
.sound-bar .hint { color:#8b949e; font-size:13px; margin-top:6px; }
.live-trading-row { align-items:flex-start; background:#161b22; border:1px solid #30363d; border-radius:8px; padding:10px 12px; margin:4px 0 10px; }
.live-trading-row .hint { color:#8b949e; font-size:13px; margin-top:4px; }
/* live position line, under the box's own label/title instead of stacked in
   with the ARMED state/buttons - added 2026-09-27 per user request ("under
   the words real money auto buy flip scalp, in the same box, but where it's
   blank right there... so it stands aside from everything else... a little
   bit bigger font so I can see it better"). .label itself stays a simple
   inline color, this is a block child that wraps onto its own line below it. */
.live-trading-row .label { display:block; }
.box-position-line { display:block; margin-top:8px; font-size:14px; font-weight:400;
                      white-space:normal; }
.live-trading-row button { border-radius:6px; padding:6px 12px; margin-left:10px; cursor:pointer; font-weight:700; font-size:14px; border:1px solid; }
.live-btn-on { background:#2d0d0d; border-color:#f85149; color:#f85149; }
.live-btn-on:hover { background:#3d1111; }
.live-btn-off { background:#0d2818; border-color:#3fb950; color:#3fb950; }
.live-btn-off:hover { background:#123322; }
.size-mode-btn { background:#21262d; border-color:#30363d; color:#8b949e; font-weight:400 !important; }
/* buy/sell volume "tug of war" bar - added 2026-09-30 per user request
   ("a tug of war window to show the tug of war against up and down volume
   going up and down in the moment so I can see which side is getting
   favored"). Split bar: green (buy/up) grows from the left, red (sell/down)
   fills the rest - the split point itself is the "which side is winning"
   read, labeled with both percentages. */
#lock-toasts { position:fixed; top:70px; right:20px; z-index:99999; display:flex; flex-direction:column; gap:10px; pointer-events:none; }
.lock-toast { background:#0d1117; border:2px solid #f2cc60; color:#f2cc60; padding:14px 22px; font-size:22px;
              font-weight:700; border-radius:10px; box-shadow:0 4px 18px rgba(0,0,0,0.6); }
#rsi-alerts { position:fixed; top:12px; left:50%; transform:translateX(-50%); z-index:100000; display:flex; flex-direction:column; gap:8px; align-items:center; max-width:94vw; }
.rsi-pill { background:#0d1117; border:3px solid currentColor; border-radius:12px; padding:12px 26px; text-align:center; cursor:pointer;
            font-size:26px; font-weight:800; letter-spacing:0.5px; animation:rsiPulse 1.4s ease-in-out infinite; }
.rsi-pill small { display:block; font-size:13px; font-weight:400; opacity:0.75; letter-spacing:0; margin-top:2px; }
.rsi-under { color:#00e5ff; } .rsi-over { color:#ff4dd2; }
@keyframes rsiPulse { 0%,100% { box-shadow:0 0 6px 0 currentColor; } 50% { box-shadow:0 0 30px 8px currentColor; } }
.rsi-hot { font-weight:800; animation:rsiText 1.4s ease-in-out infinite; }
.rsi-hot-under { color:#00e5ff; } .rsi-hot-over { color:#ff4dd2; }
@keyframes rsiText { 0%,100% { opacity:1; } 50% { opacity:0.55; } }
#sound-reminder { position:fixed; bottom:16px; left:50%; transform:translateX(-50%); z-index:99999; background:#3d2f00;
                  border:1px solid #f2cc60; color:#f2cc60; padding:10px 18px; border-radius:8px; font-size:14px; }
#incoming-alerts { position:fixed; top:90px; left:50%; transform:translateX(-50%); z-index:100001; display:flex; flex-direction:column; gap:10px; align-items:center; max-width:96vw; }
.incoming-banner { background:#0d1117; border:4px solid currentColor; border-radius:14px; padding:16px 30px; text-align:center; cursor:pointer;
                   animation:rsiPulse 1.1s ease-in-out infinite; max-width:94vw; }
.incoming-head { font-size:42px; font-weight:900; letter-spacing:1px; line-height:1.1; }
.incoming-detail { font-size:17px; margin-top:6px; color:#e6edf3; font-weight:600; }
.incoming-buy { color:#3fb950; } .incoming-sell { color:#f85149; }
.whale-latest { margin-top:3px; font-size:13px; text-align:right; line-height:1.4; font-weight:400; }
.pat-win { display:block; }
.pat-chart { cursor:zoom-in; flex:1 1 auto; min-width:0; }
#pat-overlay .pat-chart svg { max-height:86vh; }
.pat-chart-row { display:flex; gap:8px; align-items:stretch; }
.vol-bar { flex:0 0 76px; display:flex; flex-direction:column; align-items:center; gap:4px; font-size:12px; text-align:center; color:#8b949e; line-height:1.2; }
.vol-bar b { color:#e6edf3; }
.vol-bar { position:relative; }
.vol-help { width:100%; margin-top:2px; }
.vol-help summary { cursor:pointer; font-size:12px; color:#58a6ff; list-style:none; }
.vol-help summary::-webkit-details-marker { display:none; }
.vol-help-body { position:absolute; right:calc(100% + 6px); bottom:0; width:min(270px, 72vw); z-index:30; text-align:left; font-size:13px; line-height:1.45;
  background:#161b22; border:1px solid #30363d; border-radius:8px; padding:10px 12px; box-shadow:0 6px 20px rgba(0,0,0,0.5); color:#c9d1d9; }
.vol-title { font-size:12px; font-weight:800; letter-spacing:0.5px; color:#e6edf3; }
.vol-legend { font-size:12px; text-align:left; line-height:1.4; }
.lg-buy { color:#238636; } .lg-sell { color:#da3633; } .lg-norm { color:#e6edf3; }
.tug-sub .tug-lab { color:#a6b0bb; font-size:14px; }
.vol-track { position:relative; flex:1 1 auto; width:32px; min-height:120px; background:#21262d; border-radius:6px; overflow:hidden; display:flex; align-items:flex-end; }
.vol-fill { width:100%; display:flex; flex-direction:column; transition:height 0.4s ease; }
.vol-buy { background:#238636; } .vol-sell { background:#da3633; }
.vol-normal { position:absolute; left:0; right:0; height:2px; background:#e6edf3; opacity:0.85; }
@media (max-width:560px) { .vol-bar { flex-basis:46px; font-size:12px; } .vol-track { width:24px; } }
#pat-overlay { position:fixed; top:0; left:0; right:0; bottom:0; background:#0d1117; z-index:100000; overflow:auto; padding:10px 16px; }
.pat-ov-bar { margin-bottom:8px; font-size:15px; }
.pat-ov-bar button { margin-left:8px; }
.pat-ov-chart svg { width:100%; max-height:88vh; }
.pat-win-head { margin-bottom:6px; }
.pat-win-tfs { margin-top:6px; }
.pat-win-tfs button { margin-right:6px; }
.pat-win-legend { margin-top:8px; max-height:62vh; overflow-y:auto; padding-right:6px; overscroll-behavior:contain; }
.pat-panel { display:block; }
details.pat-main > summary { cursor:pointer; list-style:none; }
.pat-chips { margin-top:6px; display:flex; flex-wrap:wrap; gap:6px; }
.pat-chip { font-size:13px; padding:3px 8px; border-radius:12px; border:1px solid #30363d; background:#161b22; }
.pat-chip.up { color:#3fb950; border-color:#238636; } .pat-chip.down { color:#f85149; border-color:#da3633; } .pat-chip.flat { color:#8b949e; }
.pat-table { width:100%; border-collapse:collapse; margin-top:10px; font-size:14px; }
.pat-table th { text-align:left; color:#8b949e; font-weight:400; border-bottom:1px solid #30363d; padding:4px 6px; }
.pat-table td { vertical-align:top; padding:6px; border-bottom:1px solid #21262d; }
.pat-tf { white-space:nowrap; font-weight:700; }
.pat-bias { font-weight:700; white-space:nowrap; }
.pat-status { margin-left:8px; font-size:12px; color:#6e7681; text-transform:uppercase; }
.pat-st { font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:0.4px; margin-right:4px; }
.st-forming { color:#58a6ff; } .st-formed { color:#a371f7; } .st-confirmed { color:#39d0d8; } .st-failed { color:#ff7b72; }
.pat-pct { margin-left:8px; font-size:13px; font-weight:700; white-space:nowrap; }
.pat-pbar { display:inline-block; width:46px; height:6px; margin-right:5px; border-radius:3px; background:#30363d; vertical-align:middle; overflow:hidden; }
.pat-pbar i { display:block; height:100%; background:currentColor; transition:width 0.4s ease; }
.pat-failed { text-decoration:line-through; opacity:0.75; }
.pat-rate { font-size:13px; }
details.pat { margin:3px 0 6px 0; }
details.pat > summary { cursor:pointer; }
.pat-info { margin:6px 0 4px 0; padding:10px; background:#0d1117; border:1px solid #30363d; border-radius:6px; font-size:15px; line-height:1.5; color:#c9d1d9; white-space:normal; }
.pat-review { font-size:13px; font-style:italic; }
.pat-res { font-size:20px; font-weight:900; margin-left:8px; vertical-align:middle; }
.pat-res.pending { font-size:12px; font-weight:400; color:#8b949e; }
.kept-head { margin:10px 0 2px 0; padding-top:6px; border-top:1px solid #30363d; }
.pat-done { font-size:12px; color:#8b949e; margin-left:6px; }
.hist-hour, .hist-q { margin:3px 0 3px 10px; }
.hist-q { margin-left:16px; }
.hist-hour > summary, .hist-q > summary { cursor:pointer; }
.hist-body { margin:4px 0 4px 8px; }
.hist-ev { margin:4px 0 4px 8px; }
.hist-time { font-family:Consolas, 'Courier New', monospace; font-size:13px; color:#c9d1d9; margin-right:4px; }
.pat-info svg { display:block; margin-bottom:6px; max-width:100%; height:auto; }
.pat-info-row { display:flex; gap:16px; align-items:flex-start; }
.pat-pic { flex:0 0 250px; max-width:44%; }
.pat-pic svg { width:100%; height:auto; margin-bottom:0; }
.pat-text { flex:1 1 auto; font-size:15px; line-height:1.5; }
.pat-text > div { margin-bottom:6px; }
@media (max-width:560px) { .pat-info-row { flex-wrap:wrap; } .pat-pic { flex:1 1 100%; max-width:100%; } }
.pat-info b { color:#f2cc60; }
.pat-dir { margin-top:8px; font-size:20px; font-weight:800; line-height:1; }
.pat-dir small { font-size:12px; font-weight:400; opacity:0.8; margin-left:4px; }
.pat-dir.up { color:#3fb950; } .pat-dir.down { color:#f85149; } .pat-dir.flat { color:#8b949e; }
.pat-disc { margin-top:8px; font-size:13px; color:#6e7681; font-size:12px; }
.flip-combo { display:grid; grid-template-columns:1fr 1fr auto; align-items:center; column-gap:8px; padding:4px 0; }
.flip-combo .flip-gauge { grid-column:1; grid-row:1; justify-self:center; }
.flip-combo .flip-fair { grid-column:2; grid-row:1; justify-self:center; text-align:center; position:relative; line-height:1.25; }
.cpi-drop { margin:4px 0; }
.cpi-drop > summary { cursor:pointer; font-size:13px; color:#d29922; }
.cpi-chip { background:#f85149; color:#fff; border-radius:8px; padding:0 8px; font-size:12px; font-weight:700; margin-left:6px; }
.cpi-strip { border:1px solid #30363d; border-left:3px solid #d29922; border-radius:6px; padding:6px 10px; margin:6px 0; font-size:13px; line-height:1.55; }
.cpi-strip.cpi-before { background:rgba(210,153,34,0.10); }
.cpi-strip.cpi-live { border-left-color:#f85149; background:rgba(248,81,73,0.10); }
.cpi-flag { font-weight:800; color:#f0b429; font-size:14px; margin-bottom:2px; }
.cpi-live .cpi-flag { color:#ff7b72; }
.bm-scheme, .bm-sens { margin:3px 0; }
.bm-help summary { cursor:pointer; color:#58a6ff; font-size:13px; margin-top:4px; }
.bm-help-body { margin-top:6px; padding:8px 12px; background:#0d1117; border:1px solid #30363d; border-radius:6px; color:#c9d1d9; font-size:14px; line-height:1.5; }
.bm-help-body p { margin:6px 0; }
.cpi-help summary { cursor:pointer; color:#58a6ff; font-size:12px; margin-top:3px; }
.cpi-help-body { margin-top:6px; padding:8px 10px; background:#0d1117; border:1px solid #30363d; border-radius:6px; color:#c9d1d9; font-size:13px; line-height:1.5; }
.live-clock { float:right; display:flex; flex-direction:column; align-items:flex-end; line-height:1.1; margin-left:10px; padding:2px 10px; border:1px solid #30363d; border-radius:8px; background:#0d1117; }
.live-clock .lc-t { font-size:22px; font-weight:800; color:#58a6ff; font-variant-numeric:tabular-nums; letter-spacing:0.5px; }
.live-clock .lc-d { font-size:12px; color:#8b949e; }
.live-clock-big .lc-t { font-size:30px; }
.pat-ov-bar .live-clock { margin-right:8px; }
.topbar { padding:2px 0 6px; border-bottom:1px solid #21262d; margin-bottom:6px; }
.tb-main { display:flex; flex-wrap:wrap; align-items:center; gap:6px 18px; }
.tb-main .asset-name { font-size:20px; display:flex; align-items:center; gap:8px; }
/* top row: coin | market badge | streak + flip meter (right) ; the price line drops to its own row */
.tb-right { order:3; margin-left:auto; display:flex; align-items:center; position:relative; }
.tb-price { order:5; flex:1 1 100%; }
.tb-rsi { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.tb-rsi-in { flex-direction:column; align-items:flex-start; gap:1px; width:178px; flex:0 0 auto; }
.tb-rsi-in .tb-rsi-k { font-size:11px; } .tb-rsi-in .tb-rsi-n { font-size:13px; } .tb-rsi-in .rsi-track { width:170px; margin-top:3px; }
.tb-rsi-in .tb-rsi-v { font-size:30px; }
.tb-rsi-k { font-size:12px; color:#8b949e; text-transform:uppercase; letter-spacing:0.4px; }
.tb-rsi-v { font-size:26px; font-weight:900; font-variant-numeric:tabular-nums; min-width:68px; line-height:1.1; }
.tb-rsi-n { font-size:15px; font-weight:700; }
.rsi-g { color:#56d364; } .rsi-o { color:#f0883e; } .rsi-r { color:#ff7b72; }
.rsi-track { position:relative; display:inline-block; width:190px; height:10px; border-radius:5px; border:1px solid #30363d;
  background:linear-gradient(to right,#6a1a1a 0,#6a1a1a 30%,#8a5a12 30%,#8a5a12 40%,#1a4d2a 40%,#1a4d2a 60%,#8a5a12 60%,#8a5a12 70%,#6a1a1a 70%,#6a1a1a 100%); }
.rsi-dot { position:absolute; top:-4px; width:6px; height:16px; margin-left:-3px; border-radius:3px; background:#fff; box-shadow:0 0 4px rgba(0,0,0,0.8); }
.tb-right .flip-combo { flex-wrap:nowrap; padding:0; border-bottom:0; gap:4px 18px; margin:0; }
.tb-right { flex:0 0 auto; }
.tb-right .flip-gauge { flex:0 0 auto; width:170px; }
.tb-right .flip-gauge svg { width:170px; height:auto; }
.tb-right .flip-cap { display:none; }
.tb-right .streak-pull-wrap { position:absolute; top:100%; right:0; z-index:300; width:min(600px,92vw); }
/* the streak pull-down sits ON TOP of the page, so it needs a solid dark background (it was see-through and hard to read) */
.streak-pull[open] { background:#0d1117; border:2px solid #484f58; border-radius:10px; padding:8px 12px; box-shadow:0 12px 34px rgba(0,0,0,0.9); }
.streak-pull .press-table { background:#0d1117; font-size:14px; width:100%; }
.streak-pull .press-table th, .streak-pull .press-table td { background:#0d1117; padding:4px 8px; }
/* hidden for now (kept in the page so they can be brought back by deleting these two lines): the knight and the fair value panel */
.flip-knight { display:none !important; }
.flip-fair { display:none !important; }
.tb-price { display:flex; flex-wrap:wrap; align-items:baseline; gap:2px 8px; }
.tb-price .lbl { font-size:12px; color:#8b949e; text-transform:uppercase; }
.tb-big { font-size:22px; font-weight:800; line-height:1.1; font-variant-numeric:tabular-nums; }
.tb-price .line-diff { display:inline-block; font-size:14px; margin-left:6px; }
.mk { font-size:14px; font-weight:800; padding:3px 12px; border-radius:14px; border:2px solid; letter-spacing:0.3px; white-space:nowrap; }
.mk small { font-weight:600; opacity:0.85; }
.mk-drop { position:relative; display:inline-block; }
.mk-drop > summary { cursor:pointer; }
.mk-now { font-size:13px; color:#a6b0bb; margin-left:8px; white-space:nowrap; }
.mk-body { position:absolute; top:calc(100% + 6px); left:0; z-index:60; min-width:400px; padding:10px 14px; background:#161b22; border:1px solid #30363d;
  border-radius:8px; box-shadow:0 6px 20px rgba(0,0,0,0.55); font-size:14px; line-height:1.45; }
.mk-tbl { border-collapse:collapse; margin:6px 0; width:100%; }
.mk-row { cursor:pointer; }
.mk-row:hover td { background:#21262d; }
.mk-row.mk-sel td { background:#1f2a3a; border-bottom-color:#58a6ff; }
.mk-tbl th { text-align:left; color:#8b949e; font-weight:600; font-size:12px; border-bottom:1px solid #30363d; padding:2px 10px 2px 0; }
.mk-tbl td { padding:2px 10px 2px 0; border-bottom:1px solid #21262d; }
.mk4 { font-size:13px; color:#a6b0bb; border:1px dashed #484f58; border-radius:12px; padding:2px 10px; white-space:nowrap; }
.mk-bear { color:#ff7b72; border-color:#da3633; background:rgba(218,54,51,0.14); }
.mk-bull { color:#56d364; border-color:#238636; background:rgba(35,134,54,0.16); }
.mk-neutral { color:#c9d1d9; border-color:#6e7681; background:rgba(110,118,129,0.14); }
.tb-chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:6px; }
.tb-chip { font-size:13px; padding:2px 9px; border-radius:10px; border:1px solid #30363d; background:#0d1117; white-space:nowrap; }
.tb-chip .tb-k { color:#8b949e; font-size:12px; text-transform:uppercase; margin-right:2px; }
.top-more { margin:2px 0 4px; }
.top-more > summary { cursor:pointer; font-size:12px; color:#58a6ff; }
.row-tight { justify-content:flex-start; flex-wrap:wrap; align-items:baseline; gap:2px 14px; }
.row-tight .label { flex:0 0 auto; }
.infoknight { display:flex; flex-wrap:nowrap; gap:6px 14px; align-items:flex-start; margin:2px 0; }
.info-left { flex:1 1 0; min-width:0; }
.info-knight { flex:0 0 190px; width:190px; display:flex; justify-content:center; align-items:flex-start; }
@media (max-width:620px) { .infoknight { flex-wrap:wrap; } .info-left { flex-basis:100%; } }
.info-knight .kalshi-arrow { align-items:center; margin:0; }
.info-knight .knight-svg { width:100%; max-width:190px; height:auto; }
.info-knight .kalshi-arrow-lbl { text-align:center; font-size:13px; margin-top:-4px; }
.info-knight .kalshi-arrow-pct { text-align:center; font-size:19px; }
.flip-knight { flex:0 0 auto; width:108px; display:flex; justify-content:center; }
.flip-knight .kalshi-arrow { align-items:center; margin:0; }
.flip-knight .knight-svg { width:98px; height:auto; }
.flip-knight .kalshi-arrow-lbl { font-size:12px; margin-top:-3px; text-align:center; }
.flip-knight .kalshi-arrow-pct { font-size:15px; text-align:center; }
.tugrow { display:flex; gap:10px; align-items:stretch; }
.tugcol { flex:1 1 0; min-width:0; }
.tugvol { flex:0 0 96px; display:flex; margin:8px 0; }
.tugvol .vol-bar { flex:1 1 auto; width:96px; font-size:14px; }
.tugvol .vol-title { font-size:14px; }
.tugvol .vol-label { font-size:13px; }
.tugvol .vol-legend { font-size:13px; line-height:1.5; }
.tugvol .vol-help summary { font-size:13px; }
@media (max-width:560px) { .tugvol { display:none; } }
.lb-box { margin:6px 0; padding:8px 12px; border:2px solid #30363d; border-radius:10px; background:#0d1117; font-size:15px; line-height:1.4; }
.lb-l { font-size:12px; font-weight:800; letter-spacing:0.6px; color:#8b949e; margin-right:6px; }
.lb-box b { font-size:17px; } .lb-t { color:#c9d1d9; }
.lb-flippy { border-color:#f85149; background:#2a1212; } .lb-flippy b { color:#ff7b72; }
.lb-hover { border-color:#e3b341; background:#2a1f05; } .lb-hover b { color:#e3b341; }
.lb-oneway { border-color:#3fb950; background:#0f2a1a; } .lb-oneway b { color:#56d364; }
.lb-held { border-color:#58a6ff; background:#0c2038; } .lb-held b { color:#79c0ff; }
.bp-row { margin:6px 0; } .bp-chip { display:inline-block; font-size:14px; font-weight:800; padding:3px 10px; border-radius:12px; margin:2px 6px 2px 0; border:2px solid #30363d; }
.bp-bullish { background:#0f2a1a; border-color:#3fb950; color:#56d364; } .bp-bearish { background:#2a1212; border-color:#f85149; color:#ff7b72; } .bp-neutral { color:#8b949e; }
.mh-drop { margin:6px 0; }
.mh-drop > summary { cursor:pointer; font-size:15px; color:#58a6ff; padding:5px 0; }
.mh-kal { margin:6px 0; padding:8px 12px; border:2px solid #30363d; border-radius:10px; background:#0d1117; font-size:15px; line-height:1.5; }
.mh-table { width:100%; font-size:14px; margin:6px 0; } .mh-table td, .mh-table th { padding:5px 8px; text-align:left; vertical-align:top; }
.mh-grp td { background:#161b22; color:#8b949e; font-weight:800; letter-spacing:0.6px; text-transform:uppercase; font-size:12px; }
.mh-x { font-weight:800; font-size:14px; color:#c9d1d9; } .mh-hot { color:#ff7b72; } .mh-calm { color:#8b949e; }
.mh-info { margin-top:4px; } .mh-info > summary { cursor:pointer; color:#58a6ff; font-size:13px; }
.mh-info-body { margin-top:4px; padding:8px 12px; background:#0d1117; border:1px solid #30363d; border-radius:6px; font-size:14px; line-height:1.5; color:#c9d1d9; }
.mh-info-body p { margin:5px 0; } .mh-stats { margin-top:6px; padding-top:6px; border-top:1px solid #21262d; }
.mh-chip { display:inline-block; font-size:12px; font-weight:900; padding:2px 9px; border-radius:9px; border:2px solid; letter-spacing:0.4px; }
.mh-open { background:#0f2a1a; border-color:#3fb950; color:#56d364; } .mh-closed { background:#2a1212; border-color:#f85149; color:#ff7b72; } .mh-break { background:#2a1f05; border-color:#e3b341; color:#e3b341; }
.lc-drop { margin:6px 0; }
.lc-drop > summary { cursor:pointer; font-size:15px; color:#58a6ff; padding:5px 0; }
.lc-table { width:100%; font-size:14px; margin:6px 0; } .lc-table td, .lc-table th { padding:4px 8px; text-align:left; }
.lc-hour { margin:10px 0 14px; padding-top:6px; border-top:1px solid #21262d; }
.lc-hour-title { font-size:16px; } .lc-hour-read { font-size:14px; color:#c9d1d9; margin:2px 0 4px; }
.lc-svg { width:100%; height:auto; display:block; border:1px solid #30363d; border-radius:6px; }
.lc-help summary { cursor:pointer; color:#58a6ff; font-size:13px; margin-top:4px; }
.lc-help-body { margin-top:6px; padding:8px 12px; background:#0d1117; border:1px solid #30363d; border-radius:6px; color:#c9d1d9; font-size:14px; line-height:1.5; }
.lc-help-body p { margin:6px 0; }
.more-info { margin:8px 0; }
.more-info > summary { cursor:pointer; font-size:15px; color:#58a6ff; padding:6px 0; }
.bm-drop { margin:6px 0; }
.bm-drop > summary { cursor:pointer; font-size:14px; color:#58a6ff; padding:4px 0; }
.bm-canvas { width:100%; height:min(84vh, 800px); display:block; border:1px solid #30363d; border-radius:6px; margin:6px 0 4px; }
.info-drop > summary { cursor:pointer; font-size:14px; color:#58a6ff; padding:6px 0; }
.alert-dock { flex:3 1 250px; min-width:220px; min-height:150px; max-height:240px; overflow-y:auto; border:2px dashed #30363d; border-radius:10px; padding:5px 7px;
  display:flex; flex-direction:column; gap:4px; justify-content:center; }
.dock-empty { color:#6e7681; font-size:13px; text-align:center; }
.dock-item { padding:4px 9px; border-radius:7px; border:1px solid #30363d; cursor:pointer; line-height:1.3; font-size:13px; }
.dock-item b { display:block; font-size:14px; }
.dock-item span { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; color:#c9d1d9; }
.dock-buy { background:#0f2a1a; border-color:#238636; } .dock-buy b { color:#56d364; }
.dock-sell { background:#2a1212; border-color:#da3633; } .dock-sell b { color:#ff7b72; }
.dock-lock { background:#2d2606; border-color:#d4b106; } .dock-lock b { color:#f2cc60; }
.dock-rsi-under { background:#06222a; border-color:#00a8c0; } .dock-rsi-under b { color:#00e5ff; }
.dock-rsi-over { background:#2a0f26; border-color:#c0359e; } .dock-rsi-over b { color:#ff4dd2; }
.cwrow { display:flex; flex-wrap:wrap; gap:8px; margin:4px 0 6px; }
/* hidden for now per request 2026-10-15 (kept in the page, so deleting the next two lines brings them back): Locked first call / Late call / My call / Kalshi says */
.cwrow .cw { display:none !important; }
.cwrow { display:none; }          /* the call windows are kept in the page but hidden; the alert box now sits in the top bar (moved 2026-10-15) */
/* alerts box: top row, between the market badge and the flip meter, so the chart (and its time axis) moves up on screen */
.tb-main .alert-dock { order:2; flex:1 1 320px; min-width:240px; min-height:104px; max-height:122px; padding:4px 6px; justify-content:flex-start; }
.fly-alert { position:fixed; left:100vw; z-index:9000; padding:10px 24px; border-radius:12px; border:3px solid #30363d; font-size:26px; font-weight:900; white-space:nowrap; pointer-events:none;
  box-shadow:0 6px 22px rgba(0,0,0,0.7); animation:flyAcross 9s linear forwards; }
.fly-alert small { display:block; font-size:16px; font-weight:600; color:#e6edf3; }
.fly-buy { background:#0f2a1a; border-color:#3fb950; color:#56d364; } .fly-sell { background:#2a1212; border-color:#f85149; color:#ff7b72; }
@keyframes flyAcross { from { transform:translateX(0); } to { transform:translateX(calc(-100vw - 100%)); } }
.cw { flex:1 1 150px; max-width:250px; min-width:130px; padding:5px 10px 6px; border:2px solid #30363d; border-radius:10px; background:#0d1117; text-align:center; }
.cw-l { font-size:12px; text-transform:uppercase; letter-spacing:0.3px; color:#8b949e; line-height:1.2; }
.cw-v { font-size:26px; font-weight:800; line-height:1.15; }
.cw-s { font-size:13px; color:#a6b0bb; min-height:15px; }
.cw-locked { background:#2d2606; border-color:#d4b106; box-shadow:0 0 8px rgba(212,177,6,0.45); }
.cw-locked .cw-l { color:#f0d040; }
.cw-up { border-color:#238636; } .cw-down { border-color:#da3633; }
.cw-locked.cw-up { border-color:#3fb950; } .cw-locked.cw-down { border-color:#f85149; }
/* flip gauge + fair value + streak sit together instead of spread across the card */
.flip-combo { display:flex; flex-wrap:wrap; align-items:center; justify-content:flex-start; gap:4px 16px; padding:2px 0; }
.flip-combo .flip-gauge, .flip-combo .flip-fair, .flip-combo .flip-right { grid-column:auto; grid-row:auto; justify-self:auto; margin:0; }
.flip-combo .flip-right { align-items:flex-start; }
.flip-combo .flip-streak { text-align:left; }
.flip-combo .flip-streak .fire-streak, .flip-combo .flip-streak .smoke-streak { justify-content:flex-start; }
.flip-combo .flip-cap { flex:1 1 100%; order:9; text-align:left; justify-self:auto; }
.flip-wait { font-size:14px; line-height:1.3; color:#a6b0bb; max-width:210px; }
.flip-gauge svg { width:112px; height:auto; }
.flip-combo .flip-fair { max-width:215px; }
.flip-combo .flip-fair .hint { white-space:normal; }
.flip-combo .flip-streak { white-space:nowrap; }
.flip-fair-big { font-size:28px; }
.tug-title { font-size:14px; }
.tug-end { margin:4px 0 6px; padding:6px 12px; border-radius:8px; border:2px solid; font-size:22px; line-height:1.2; }
.tug-end b { font-size:26px; font-weight:800; }
.tug-end .tug-end-l { font-size:13px; letter-spacing:0.5px; color:#a6b0bb; font-weight:700; }
.tug-end .hint { font-size:14px; }
.tug-end.tug-end-wait { background:#2a1f05; border:2px dashed #e3b341; color:#f0d98c; font-size:18px; padding:10px 14px; animation:tugWaitPulse 1.6s ease-in-out infinite; }
.tug-wait-badge { display:inline-block; background:#e3b341; color:#1a1300; font-weight:900; font-size:17px; letter-spacing:0.5px; padding:3px 12px; border-radius:8px; margin:0 8px 0 4px; }
.tug-wait-lean { font-size:26px; font-weight:900; margin-right:8px; vertical-align:middle; }
.tug-wait-lean.up { color:#56d364; } .tug-wait-lean.down { color:#ff7b72; }
.tug-wait-note { font-size:14px; color:#d9c27a; }
@keyframes tugWaitPulse { 0%,100% { box-shadow:0 0 0 rgba(227,179,65,0); } 50% { box-shadow:0 0 12px rgba(227,179,65,0.55); } }
.tug-end.tug-end-none { background:#1c2128; border-color:#6e7681; color:#c9d1d9; font-size:17px; }
.tug-end.tug-end-none b { font-size:20px; }
.tug-tier { font-size:12px; font-weight:800; padding:1px 8px; border-radius:9px; vertical-align:middle; letter-spacing:0.4px; }
.tug-tier-strong { background:#238636; color:#fff; } .tug-tier-good { background:#9e6a03; color:#fff; } .tug-tier-weak { background:#6e7681; color:#fff; }
.tug-flip { display:inline-block; font-size:18px; font-weight:900; padding:3px 12px; border-radius:8px; margin:0 6px; vertical-align:middle; letter-spacing:0.3px; border:2px solid; }
.tug-flip-up { background:#0f2f17; color:#56d364; border-color:#3fb950; } .tug-flip-down { background:#3a1212; color:#ff7b72; border-color:#f85149; }
.tug-flip-note { font-size:14px; margin-top:5px; color:#c9d1d9; font-weight:500; }
.tug-end-live { font-size:13px; margin-top:3px; font-weight:600; }
.tug-end-live.ok { color:#56d364; } .tug-end-live.warn { color:#e3b341; }
.tug-end.up { background:#0f2a1a; border-color:#238636; color:#56d364; }
.tug-end.down { background:#2a1212; border-color:#da3633; color:#ff7b72; } .tug-big { font-size:30px; } .tug-diff { font-size:30px; } .tug-vs { font-size:18px; }
.tug .hint { font-size:13px; } .tug-gl, .tug-gr { font-size:13px; } .tug-cap { font-size:13px; }
.tug-head { font-size:22px; } .tug-lean { font-size:15px; }
.tug-row { font-size:14px; margin:4px 0; } .tug-lab { flex:0 0 205px; } .tug-val { flex:0 0 175px; } .tug-track { height:8px; }
.tug-box { font-size:14px; } .tug-box span { font-size:13px; } .tug-agree { font-size:14px; } .tug-around { font-size:13px; }
.tug-sub .tug-lab { font-size:14px; }
.pat-star { color:#ffd33d; font-size:19px; line-height:1; vertical-align:middle; text-shadow:0 0 7px rgba(255,211,61,0.95), 0 0 2px #000; }
.flip-fair-big { font-size:30px; font-weight:800; line-height:1.05; }
.flip-fair-sub { font-size:14px; font-weight:700; }
.fair-help summary { cursor:pointer; font-size:12px; color:#58a6ff; list-style:none; margin-top:2px; }
.fair-help summary::-webkit-details-marker { display:none; }
.fair-tbl { width:48%; display:inline-table; vertical-align:top; font-size:12px; border-collapse:collapse; margin:4px 1% 8px 0; }
.fair-tbl th { text-align:left; font-weight:600; color:#8b949e; border-bottom:1px solid #30363d; padding:2px 3px; }
.fair-tbl td { padding:1px 3px; border-bottom:1px solid #21262d; }
.fair-help-body { position:absolute; left:50%; transform:translateX(-50%); top:100%; z-index:30; width:min(380px, 88vw); text-align:left; font-size:13px; line-height:1.45;
  background:#161b22; border:1px solid #30363d; border-radius:8px; padding:10px 12px; box-shadow:0 6px 20px rgba(0,0,0,0.5); color:#c9d1d9; font-weight:400; }
.flip-combo .flip-right { grid-column:3; grid-row:1; justify-self:end; display:flex; flex-direction:column; align-items:flex-end; }
.flip-combo .flip-cap { grid-column:1 / -1; justify-self:center; }
@media (max-width:560px) { .flip-gauge svg { width:100px; height:70px; } .flip-combo { grid-template-columns:1fr 1fr; } .flip-combo .flip-right { grid-column:1 / -1; grid-row:2; justify-self:center; align-items:center; } .flip-fair-big { font-size:24px; } }
.kalshi-arrow { display:flex; flex-direction:column; align-items:flex-end; }
.kalshi-arrow svg { display:block; overflow:visible; }
.kalshi-arrow-lbl { font-size:12px; font-weight:400; color:#8b949e; margin-top:2px; }
.kalshi-arrow-pct { font-size:14px; font-weight:800; line-height:1.1; }
@keyframes sparkle { 0%, 100% { opacity:0; scale:0.2; } 50% { opacity:1; scale:1; } }
.fx-spark { transform-box:fill-box; transform-origin:center; animation:sparkle 1s ease-in-out infinite; }
@keyframes dripfall { 0% { opacity:0.95; translate:0 0; } 100% { opacity:0; translate:0 26px; } }
.fx-drop { animation:dripfall 1s linear infinite; }
.flip-combo svg { display:block; }
.flip-combo { position:relative; }
.flip-streak { text-align:right; font-weight:700; font-size:14px; line-height:1.3; }
.flip-streak .fire-streak, .flip-streak .smoke-streak { justify-content:flex-end; }
.flip-cap { font-size:12px; color:#8b949e; text-align:center; line-height:1.35; }
.press-row { display:block; }
.press-head { display:flex; justify-content:space-between; gap:8px; flex-wrap:wrap; align-items:baseline; margin-bottom:4px; }
.press-verdict { font-weight:800; font-size:15px; }
.press-bar { position:relative; display:flex; height:44px; border-radius:8px; overflow:hidden; background:#21262d; border:2px solid #484f58; box-shadow:0 0 10px rgba(255,255,255,0.08); margin:4px 0 6px; }
.press-buy { background:#238636; color:#fff; font-size:18px; font-weight:800; line-height:40px; padding-left:12px; white-space:nowrap; overflow:hidden; transition:width 0.4s ease; text-shadow:0 1px 2px rgba(0,0,0,0.6); }
.press-sell { background:#da3633; color:#fff; font-size:18px; font-weight:800; line-height:40px; padding-right:12px; text-align:right; white-space:nowrap; overflow:hidden; transition:width 0.4s ease; text-shadow:0 1px 2px rgba(0,0,0,0.6); }
.press-mid { position:absolute; left:50%; top:0; bottom:0; width:3px; margin-left:-1px; background:#e6edf3; opacity:0.85; }
.press-chips { margin-top:6px; display:flex; flex-wrap:wrap; gap:6px; }
.press-chip { font-size:13px; padding:2px 8px; border-radius:10px; border:1px solid #30363d; background:#161b22; }
.press-chip.up { color:#3fb950; border-color:#238636; } .press-chip.down { color:#f85149; border-color:#da3633; } .press-chip.flat { color:#8b949e; }
.press-more { margin-top:6px; }
.press-more > summary { cursor:pointer; }
.press-table { width:100%; border-collapse:collapse; margin-top:6px; font-size:13px; }
.press-table th { text-align:left; color:#8b949e; font-weight:400; border-bottom:1px solid #30363d; padding:3px 6px; }
.press-table td { padding:3px 6px; border-bottom:1px solid #21262d; }
.press-idle { opacity:0.45; }
.press-mini { width:90px; height:8px; background:#da3633; border-radius:3px; overflow:hidden; }
.press-mini i { display:block; height:100%; background:#238636; }
.tug-row { margin-top:6px; }
.tug-bar { position:relative; height:18px; border-radius:4px; overflow:hidden;
           background:#21262d; display:flex; margin-top:3px; }
.tug-bar-buy { background:#3fb950; height:100%; transition:width 0.3s ease; }
.tug-bar-sell { background:#f85149; height:100%; transition:width 0.3s ease; }
.tug-bar-label { display:flex; justify-content:space-between; font-size:13px; margin-top:2px; }
.size-mode-btn:hover { background:#30363d; }
.size-mode-active { background:#0d2818 !important; border-color:#3fb950 !important; color:#3fb950 !important; }
.live-trading-row input[type="number"] { background:#0d1117; border:1px solid #30363d; border-radius:6px;
                                          color:#e6edf3; padding:5px 8px; margin-left:8px; font-size:14px; }
/* "FLIP" tag next to the calls (added 2026-09-25) - marks this window's
   real position as the flip-scalp entry, not the original locked call */
.flip-badge { display:inline-block; background:#3d1a00; border:1px solid #d29922; color:#d29922;
               font-size:12px; font-weight:800; letter-spacing:0.5px; padding:2px 7px;
               border-radius:5px; vertical-align:middle; margin-left:2px; }
</style>
<script>
let audioCtx = null;
const PATTERN_DATA = __PATTERN_LIB_JSON__;
let soundEnabled = false;
const alertedFlips = new Set();

// master volume - ONE slider controls every sound (flip, building, siren,
// opportunity, reversal, whale, wall) instead of separate per-sound levels.
// Persisted in localStorage. vol(x) scales a sound's own peak gain by this -
// each sound keeps its own relative loudness/character, this just scales
// all of them together.
let masterVolume = 0.8;
try {
  const savedVol = localStorage.getItem('masterVolume');
  if (savedVol !== null) masterVolume = Math.max(0, Math.min(1, parseFloat(savedVol)));
} catch (e) {}

function vol(peak) {
  return peak * masterVolume;
}

function setMasterVolume(v) {
  masterVolume = Math.max(0, Math.min(1, parseFloat(v)));
  try { localStorage.setItem('masterVolume', String(masterVolume)); } catch (e) {}
  const label = document.getElementById('volume-label');
  if (label) label.textContent = Math.round(masterVolume * 100) + '%';
}

// per-asset mute - lets BTC and XRP alerts be turned on/off independently.
// Persisted in localStorage so a toggle survives a page refresh. Only gates
// whether ALERTS play (flip/building/siren) - the "Test X sound" buttons
// always work regardless, so you can still preview a muted asset's sound.
let assetSoundEnabled = { BTC: true, XRP: true };
try {
  const saved = JSON.parse(localStorage.getItem('assetSoundEnabled') || 'null');
  if (saved) Object.assign(assetSoundEnabled, saved);
} catch (e) { /* localStorage unavailable or corrupt - just use defaults */ }

function toggleAssetSound(assetName) {
  assetSoundEnabled[assetName] = !assetSoundEnabled[assetName];
  try { localStorage.setItem('assetSoundEnabled', JSON.stringify(assetSoundEnabled)); } catch (e) {}
  updateAssetToggleButtons();
  if (assetSoundEnabled[assetName]) {
    ensureAudioCtx();
    playAlienSound(assetName); // confirmation beep so you can hear it's back on
  }
}

// real-money auto-buy switch (added 2026-09-14, multi-asset 2026-09-22,
// strategy param added 2026-09-25 for the separate flip-scalp switch).
// Arming requires a second, explicit native confirm() dialog - a single
// click can never spend real money. Disarming is one click, no confirm, on
// purpose - turning it off should never have friction.
async function armLive(assetName, nextState, strategy) {
  strategy = strategy || 'lock';
  const label = strategy === 'flip' ? 'FLIP-SCALP' : strategy === 'protect' ? 'PROTECTIVE SELL' : 'REAL MONEY';
  if (nextState) {
    const msg = strategy === 'flip'
      ? ('Arm FLIP-SCALP trading on ' + assetName + '?\n\n' +
         'Only when flat: buys the NEW direction the instant a real flip fires, then sells ' +
         'automatically at +20% profit, or earlier if it climbs and starts giving that back. ' +
         'Holds to settlement if neither happens. Stays armed until you turn it off or the ' +
         'daily loss limit trips.\n\nAre you sure?')
      : strategy === 'protect'
      ? ('Arm PROTECTIVE SELL on ' + assetName + '?\n\n' +
         'Only when holding a real-money "Locked first call" position: sells it early, before ' +
         'settlement, the instant the flip signal crosses your set confidence bar AGAINST it - ' +
         'cutting the loss instead of riding it to close. Separate from flip-scalp (which BUYS ' +
         'the new direction) - this only SELLS your existing position. Stays armed until you turn ' +
         'it off or the daily loss limit trips.\n\nAre you sure?')
      : ('Arm REAL MONEY trading on ' + assetName + '?\n\n' +
         'This will spend real dollars the instant ' + assetName + '\'s "Locked first call" ' +
         'fires - buy and hold to settlement, no exit before then, skipping any entry priced ' +
         'over 80 cents. It stays armed until you turn it off or it auto-disarms on hitting the ' +
         'daily loss limit.\n\nAre you sure?');
    const ok = confirm(msg);
    if (!ok) return;
    await fetch('/arm_live?asset=' + encodeURIComponent(assetName) + '&strategy=' + strategy + '&state=on&confirm=yes');
  } else {
    await fetch('/arm_live?asset=' + encodeURIComponent(assetName) + '&strategy=' + strategy + '&state=off');
  }
  refresh();
}

// bet-size config (added 2026-09-22 per user request, simplified same day
// to one input + a mode toggle instead of two boxes) - a flat dollar
// amount, or a % of the real account balance read fresh at trade time.
// Shared across every armed asset - reads whichever card's input triggered
// it, but writes one shared server-side setting.
async function setLiveSizeMode(cardName, mode) {
  // tab click - switches which mode is active WITHOUT changing its stored
  // value, so flipping back and forth doesn't lose either number
  await fetch('/set_live_size?mode=' + mode);
  refresh();
}
async function setLiveSizeValue(cardName) {
  // typed a number and hit Set (or Enter) - applies it to whichever mode
  // is CURRENTLY active (stamped on the input as data-mode by the server)
  const el = document.getElementById('livesize-input-' + cardName);
  if (!el || el.value === '') return;
  const mode = el.dataset.mode === 'percent' ? 'percent' : 'dollars';
  const param = mode === 'percent' ? 'percent' : 'dollars';
  await fetch('/set_live_size?mode=' + mode + '&' + param + '=' + encodeURIComponent(el.value));
  refresh();
}

// flip-scalp sell target (added 2026-09-25 per user request) - a percentage
// gain to sell at, shared across every armed asset same as bet sizing.
async function setFlipTarget(cardName) {
  const el = document.getElementById('fliptarget-input-' + cardName);
  if (!el || el.value === '') return;
  await fetch('/set_flip_target?pct=' + encodeURIComponent(el.value));
  refresh();
}

// flip-scalp max entry price cap (added 2026-09-25 per user request, same
// idea as the bet-size/sell-target controls) - skips a flip entry if the
// price to pay is over this, tunable separately from the main strategy's cap.
async function setFlipMaxPrice(cardName) {
  const el = document.getElementById('flipmaxprice-input-' + cardName);
  if (!el || el.value === '') return;
  await fetch('/set_flip_max_price?pct=' + encodeURIComponent(el.value));
  refresh();
}

// main strategy's max entry price cap - added 2026-09-28, was a hardcoded
// 80% before that day with no on-site control at all (unlike the flip
// strategy's own cap, which already had one) - fixed after a real losing
// morning traced partly to expensive, poor-payout entries.
async function setMainMaxPrice(cardName) {
  const el = document.getElementById('mainmaxprice-input-' + cardName);
  if (!el || el.value === '') return;
  await fetch('/set_main_max_price?pct=' + encodeURIComponent(el.value));
  refresh();
}

// take-profit at a Kalshi price level for the main strategy - added 2026-10-02.
// 0 or blank = off (hold to settlement).
async function setBuyDelay(cardName) {
  const el = document.getElementById('buydelay-input-' + cardName);
  if (!el || el.value === '') return;
  await fetch('/set_buy_delay?asset=' + encodeURIComponent(cardName) + '&n=' + encodeURIComponent(el.value));
  refresh();
}

async function setMainSellPrice(cardName) {
  const el = document.getElementById('mainsellprice-input-' + cardName);
  if (!el) return;
  await fetch('/set_main_sell_price?pct=' + encodeURIComponent(el.value === '' ? '0' : el.value));
  refresh();
}

// flip-scalp sell-trigger mode (added 2026-09-25 per user request) - "gain"
// is the original %-gain-on-cost target, "price" sells at an absolute
// Kalshi price level instead (a flat probability level rather than a
// %-gain that varies wildly with how cheap the flip entry was).
async function setFlipSellMode(cardName, mode) {
  await fetch('/set_flip_sell_mode?mode=' + mode);
  refresh();
}
async function setFlipSellValue(cardName) {
  // applies to whichever mode is CURRENTLY active (stamped as data-mode)
  const el = document.getElementById('flipsell-input-' + cardName);
  if (!el || el.value === '') return;
  const mode = el.dataset.mode === 'price' ? 'price' : 'gain';
  if (mode === 'price') {
    await fetch('/set_flip_sell_mode?mode=price&pct=' + encodeURIComponent(el.value));
  } else {
    await fetch('/set_flip_target?pct=' + encodeURIComponent(el.value));
  }
  refresh();
}

// "hold if guaranteed flip" toggle (added 2026-09-25 per user request) -
// when on, a flip position isn't sold early while the certain-flip signal
// is still active for it, riding to full settlement instead.
async function setFlipHoldIfCertain(cardName, nextState) {
  await fetch('/set_flip_hold_if_certain?state=' + (nextState ? 'on' : 'off'));
  refresh();
}

// protective-sell confidence threshold (added 2026-09-26 per user request,
// its own separate box like flip-scalp/real-money) - how confident the flip
// signal needs to be, against your locked call, before it sells your
// existing real position early. Own switch, own threshold, deliberately NOT
// tied to flip-scalp's certain-flip alert bar (real backtest found the two
// need different bars - see place_live_buy/protective-sell comments).
async function setProtectThreshold(cardName) {
  const el = document.getElementById('protectthresh-input-' + cardName);
  if (!el || el.value === '') return;
  await fetch('/set_protect_threshold?pct=' + encodeURIComponent(el.value));
  refresh();
}

// building-toward-a-flip beeps (30/40/45% tiers) - user asked to turn these
// off specifically 2026-09-01, so this defaults OFF (unlike the other alerts)
// and is toggleable via the sound bar, persisted like the asset toggles.
let buildingAlertsEnabled = false;
try {
  const savedBuild = localStorage.getItem('buildingAlertsEnabled');
  if (savedBuild !== null) buildingAlertsEnabled = savedBuild === 'true';
} catch (e) {}

function toggleBuildingAlerts() {
  buildingAlertsEnabled = !buildingAlertsEnabled;
  try { localStorage.setItem('buildingAlertsEnabled', String(buildingAlertsEnabled)); } catch (e) {}
  updateBuildingToggleButton();
}

function updateBuildingToggleButton() {
  const btn = document.getElementById('toggle-building');
  if (!btn) return;
  btn.textContent = 'Building-alert beeps: ' + (buildingAlertsEnabled ? 'ON' : 'OFF');
  btn.classList.toggle('asset-sound-on', buildingAlertsEnabled);
  btn.classList.toggle('asset-sound-off', !buildingAlertsEnabled);
}

// spoken voice alerts (added 2026-09-24 per user request) - reads out what
// just happened ("Swinging down, against your call" etc.) a beat AFTER the
// existing sound, using the browser's built-in text-to-speech (no external
// API/key needed). Defaults ON since the user asked for this directly.
// Respects the same per-asset mute list as sounds - if an asset's sound is
// off, its voice is off too, not a separate mute list to manage.
let voiceAlertsEnabled = true;
try {
  const savedVoice = localStorage.getItem('voiceAlertsEnabled');
  if (savedVoice !== null) voiceAlertsEnabled = savedVoice === 'true';
} catch (e) {}

function toggleVoiceAlerts() {
  voiceAlertsEnabled = !voiceAlertsEnabled;
  try { localStorage.setItem('voiceAlertsEnabled', String(voiceAlertsEnabled)); } catch (e) {}
  updateVoiceToggleButton();
}

function updateVoiceToggleButton() {
  const btn = document.getElementById('toggle-voice');
  if (!btn) return;
  btn.textContent = 'Voice alerts: ' + (voiceAlertsEnabled ? 'ON' : 'OFF');
  btn.classList.toggle('asset-sound-on', voiceAlertsEnabled);
  btn.classList.toggle('asset-sound-off', !voiceAlertsEnabled);
}

function speakRaw(text) {
  // unconditional - no toggle checks. Used by speak() below AND directly by
  // the "Test voice alert" button, so the test proves the actual mechanism
  // works regardless of whether voice/sound happen to be toggled off right
  // now - same "test buttons work regardless" convention every other test
  // button on this page already follows.
  if (!('speechSynthesis' in window)) return;
  try {
    // said once (was twice for a short stretch on 2026-09-25, reverted same
    // day per user request - the extra repeat was adding real delay on top
    // of the alert's own lag, which made it feel out of sync with the chart)
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.05;
    // boosted 2026-09-28 per user request ("make it a little bit louder") -
    // raises the floor and applies a 1.3x multiplier, capped at the Web
    // Speech API's hard ceiling of 1.0
    u.volume = Math.max(0.7, Math.min(1, masterVolume * 1.3));
    window.speechSynthesis.speak(u);
  } catch (e) { /* speech synthesis unavailable/blocked - the sound alert already played, fine */ }
}

function speak(text) {
  if (!voiceAlertsEnabled || !soundEnabled) return;
  speakRaw(text);
}

// speak() a beat AFTER the sound already played, so they don't talk over
// each other - the delay is deliberately shorter than most alert sounds'
// own duration so the voice lands right as the sound tail fades.
// the flip itself outranks everything: drop any queued "building" speech and say it right away
function speakUrgent(text, delayMs) {
  setTimeout(() => {
    try { if ('speechSynthesis' in window) window.speechSynthesis.cancel(); } catch (e) {}
    setTimeout(() => speak(text), 80);
  }, delayMs === undefined ? 300 : delayMs);
}

function speakDelayed(text, delayMs) {
  // shortened from 900ms 2026-09-25 - real lag on top of an already-lagging
  // 60s swing detector made alerts feel stale/out of sync by the time they
  // were heard, especially on a fast-reverting asset. Still enough of a gap
  // to not talk over the alert sound's own tail.
  setTimeout(() => speak(text), delayMs === undefined ? 400 : delayMs);
}

function updateAssetToggleButtons() {
  ['BTC', 'XRP'].forEach(name => {
    const btn = document.getElementById('toggle-' + name);
    if (!btn) return;
    const on = assetSoundEnabled[name];
    btn.textContent = name + ' alerts: ' + (on ? 'ON' : 'OFF');
    btn.classList.toggle('asset-sound-on', on);
    btn.classList.toggle('asset-sound-off', !on);
  });
}

// each asset gets its own distinct sound profile so you can tell which one
// fired just by ear, without looking at the screen. Assigned by name so it
// stays consistent asset-to-asset; anything not listed falls back to default.
const ASSET_SOUND_PROFILES = {
  'BTC':  { type: 'sine',     low: 150,  high: 500,  sweeps: 4, step: 0.18 },  // lowered/slowed - was too close to XRP
  'XRP':  { type: 'square',   low: 1100, high: 2600, sweeps: 8, step: 0.09 },  // raised - dogs didn't like the deep low end
  'ETH':  { type: 'triangle', low: 250,  high: 900,  sweeps: 5, step: 0.14 },
  'NEAR': { type: 'sine',     low: 500,  high: 1600, sweeps: 7, step: 0.10 },
  'BNB':  { type: 'sawtooth', low: 200,  high: 700,  sweeps: 4, step: 0.16 },
  'GOLD': { type: 'triangle', low: 350,  high: 1300, sweeps: 6, step: 0.13 },
};
const DEFAULT_SOUND_PROFILE = { type: 'sawtooth', low: 400, high: 1200, sweeps: 6, step: 0.12 };

function ensureAudioCtx() {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (audioCtx.state === 'suspended') audioCtx.resume();
  return audioCtx;
}

// the main sound button: GREEN = sound on, RED = sound off; click it to switch
function syncSoundButton() {
  const btn = document.getElementById('sound-enable-btn');
  if (!btn) return;
  btn.textContent = soundEnabled ? 'SOUND ON - alerts will play (click to turn off)' : 'SOUND OFF - click here to turn it on';
  btn.classList.toggle('sound-on', !!soundEnabled);
  btn.classList.toggle('sound-off', !soundEnabled);
}
function toggleSound() {
  if (soundEnabled) {
    soundEnabled = false;
    try { localStorage.setItem('soundWanted', '0'); } catch (e) {}
    try { window.speechSynthesis.cancel(); } catch (e) {}
    syncSoundButton();
  } else enableSound();
}
function enableSound(quiet) {
  ensureAudioCtx();
  soundEnabled = true;
  try { localStorage.setItem('soundWanted', '1'); } catch (e) {}
  const btn = document.getElementById('sound-enable-btn');
  syncSoundButton();
  if (quiet === true) {
    // auto re-enable after a page reload: warm up speech silently, no beep
    try { const w = new SpeechSynthesisUtterance(' '); w.volume = 0; window.speechSynthesis.speak(w); } catch (e) {}
    return;
  }
  playAlienSound('BTC'); // quick confirmation beep so you know it worked
  // warm up speech synthesis on this same click gesture - some browsers
  // need the FIRST speak() call to happen inside a user gesture before
  // later programmatic ones (from the 1s refresh loop) are allowed through
  if (voiceAlertsEnabled && 'speechSynthesis' in window) {
    try {
      const warmup = new SpeechSynthesisUtterance('Voice alerts on.');
      warmup.volume = Math.max(0, Math.min(1, masterVolume));
      window.speechSynthesis.speak(warmup);
    } catch (e) {}
  }
}

function playAlienSound(assetName) {
  try {
    const ctx = ensureAudioCtx();
    const p = ASSET_SOUND_PROFILES[assetName] || DEFAULT_SOUND_PROFILE;
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = p.type;
    osc.connect(gain);
    gain.connect(ctx.destination);
    // a warbling, sweeping "alien" tone - frequency oscillates up and down
    // fast a few times, fading out at the end. Shape differs per asset via
    // ASSET_SOUND_PROFILES so each one is audibly distinct.
    osc.frequency.setValueAtTime(p.low, now);
    for (let i = 0; i < p.sweeps; i++) {
      const t = now + i * p.step;
      osc.frequency.linearRampToValueAtTime(i % 2 === 0 ? p.high : p.low, t);
    }
    const dur = p.sweeps * p.step + 0.1;
    gain.gain.setValueAtTime(vol(0.5), now);  // turned up from 0.15
    gain.gain.linearRampToValueAtTime(0.0001, now + dur);
    osc.start(now);
    osc.stop(now + dur + 0.05);
  } catch (e) { /* autoplay may be blocked until the user interacts once - fine, just skip */ }
}

// The alerts below are all about the OLD live "call" (flipped / flip chance building / swinging against or with your call / flip for sure). That call is not the
// real call any more (the END CALL, locked at the 8th minute, is), so they are switched OFF 2026-10-17 per request. Set this to true to bring them all back.
const OLD_CALL_ALERTS_ON = false;
function checkFlipAlerts() {
  if (!OLD_CALL_ALERTS_ON) return;
  if (!soundEnabled) return;
  const flips = document.querySelectorAll('.flip-alert');
  flips.forEach(el => {
    const key = el.getAttribute('data-flip-key');
    if (key && !alertedFlips.has(key)) {
      alertedFlips.add(key);
      const assetName = key.split('::')[0];
      if (soundOn(assetName)) {
        playAlienSound(assetName);
        speakUrgent(assetName + '. Flipped.');
      }
    }
  });
}

// EARLY WARNING tiers - not "it flipped" (that's playAlienSound above), this
// is "the flip-chance % is BUILDING toward one." Fires once per window per
// tier as the percentage climbs through each level, so you get a sense of
// it building rather than a single after-the-fact alert.
const BUILD_TIERS = [30, 40, 45];
const alertedBuildTiers = new Set(); // holds "key::tier" strings already fired

function playBuildingBeep(assetName, tier) {
  try {
    const ctx = ensureAudioCtx();
    const p = ASSET_SOUND_PROFILES[assetName] || DEFAULT_SOUND_PROFILE;
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = p.type;
    osc.connect(gain);
    gain.connect(ctx.destination);
    // a short, single beep (not a sweep like the flip alert) - pitch rises
    // with tier so 30/40 are audibly distinct steps of "getting closer"
    const tierIndex = BUILD_TIERS.indexOf(tier); // 0, 1
    const freq = p.low + (p.high - p.low) * (0.3 + tierIndex * 0.3);
    osc.frequency.setValueAtTime(freq, now);
    gain.gain.setValueAtTime(vol(0.4), now);
    gain.gain.linearRampToValueAtTime(0.0001, now + 0.18);
    osc.start(now);
    osc.stop(now + 0.2);
  } catch (e) { /* autoplay may be blocked until the user interacts once - fine, just skip */ }
}

function playSirenAlert(assetName) {
  // synthesized siren (wailing two-tone sweep) for the 45% tier - the most
  // serious one. Not a real ambulance recording - just an oscillator
  // alternating between two frequencies. Base pitch shifts per-asset (using
  // that asset's own profile) so even the siren is audibly which-coin, not
  // one identical sound for everyone.
  try {
    const ctx = ensureAudioCtx();
    const p = ASSET_SOUND_PROFILES[assetName] || DEFAULT_SOUND_PROFILE;
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.connect(gain);
    gain.connect(ctx.destination);
    const cycles = 3;
    const cycleDur = 0.4;
    const lowF = p.low + (p.high - p.low) * 0.15;
    const highF = p.low + (p.high - p.low) * 0.55;
    osc.frequency.setValueAtTime(lowF, now);
    for (let i = 0; i < cycles; i++) {
      const t0 = now + i * cycleDur;
      const t1 = t0 + cycleDur / 2;
      const t2 = t0 + cycleDur;
      osc.frequency.linearRampToValueAtTime(highF, t1);
      osc.frequency.linearRampToValueAtTime(lowF, t2);
    }
    const dur = cycles * cycleDur;
    gain.gain.setValueAtTime(vol(0.45), now);
    gain.gain.setValueAtTime(vol(0.45), now + dur - 0.1);
    gain.gain.linearRampToValueAtTime(0.0001, now + dur);
    osc.start(now);
    osc.stop(now + dur + 0.05);
  } catch (e) { /* autoplay may be blocked until the user interacts once - fine, just skip */ }
}

function checkBuildingAlerts() {
  if (!OLD_CALL_ALERTS_ON) return;
  if (!soundEnabled || !buildingAlertsEnabled) return;
  // assets whose window has ALREADY flipped - the flip alert announces that; no "building" chatter on top of it
  const flipped = new Set(Array.from(document.querySelectorAll('.flip-alert, .certain-flip-alert')).map(e =>
    (e.getAttribute('data-flip-key') || ((e.getAttribute('data-flip-name') || '') + '::')).split('::')[0]));
  const els = document.querySelectorAll('.flip-chance-value');
  els.forEach(el => {
    const key = el.getAttribute('data-flip-chance-key');
    const pct = parseFloat(el.getAttribute('data-flip-chance-pct'));
    if (!key || isNaN(pct)) return;
    const assetName = key.split('::')[0];
    // if it jumped past several tiers at once (e.g. 20% -> 50%), mark them all done but announce ONLY the highest
    let top = 0;
    BUILD_TIERS.forEach(tier => {
      const tierKey = key + '::' + tier;
      if (pct >= tier && !alertedBuildTiers.has(tierKey)) {
        alertedBuildTiers.add(tierKey);
        top = Math.max(top, tier);
      }
    });
    if (!top || flipped.has(assetName) || !soundOn(assetName)) return;
    if (top === 45) {
      playSirenAlert(assetName);
      speakDelayed(assetName + '. Siren. Flip chance ' + top + ' percent, building fast.');
    } else {
      playBuildingBeep(assetName, top);
      speakDelayed(assetName + '. Beep. Flip chance ' + top + ' percent.');
    }
  });
}

// "favorable regime" alert - added 2026-08-31 per real backtested data: BTC's
// best regime historically is BULL (76.7%), XRP's best is NEUTRAL (76.0%).
// Fires once at the MOMENT each asset enters its own favorable regime (not
// continuously while it stays there) - a positive "conditions look good"
// chime, distinct from the flip/building/siren alarm sounds. Respects the
// same per-asset ON/OFF toggle as the other alerts.
const FAVORABLE_REGIME = { BTC: 'BULL', XRP: 'NEUTRAL' };
let lastRegimeSeen = {}; // asset name -> last regime value seen, so we can detect the transition

function playOpportunityChime(assetName) {
  try {
    const ctx = ensureAudioCtx();
    const p = ASSET_SOUND_PROFILES[assetName] || DEFAULT_SOUND_PROFILE;
    const now = ctx.currentTime;
    // pleasant ascending two-note chime - NOT alarm-like, this is good news
    const notes = [p.low + (p.high - p.low) * 0.35, p.low + (p.high - p.low) * 0.65];
    notes.forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.connect(gain);
      gain.connect(ctx.destination);
      const t0 = now + i * 0.14;
      osc.frequency.setValueAtTime(freq, t0);
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.linearRampToValueAtTime(vol(0.35), t0 + 0.03);
      gain.gain.linearRampToValueAtTime(0.0001, t0 + 0.22);
      osc.start(t0);
      osc.stop(t0 + 0.25);
    });
  } catch (e) { /* autoplay may be blocked until the user interacts once - fine, just skip */ }
}

// "reversal" alarm - added 2026-08-31, this alert had ONLY ever been a
// visual pulsing pill (the "*** BOOM - SWINGING ... AGAINST YOUR LOCKED
// CALL ***" box), no sound at all - a real gap a user caught ("I have yet
// to hear the reversal thing go off"). Fires when a fast, dramatic price
// move happens against your locked call - server-side edge-detected so it
// fires once per event, not every 2s poll while it's ongoing.
function playReversalAlarm(assetName) {
  try {
    const ctx = ensureAudioCtx();
    const p = ASSET_SOUND_PROFILES[assetName] || DEFAULT_SOUND_PROFILE;
    const now = ctx.currentTime;
    // sharp double-buzz - urgent and distinct from the flip sweep, building
    // beep, siren, and opportunity chime
    const freq = p.low + (p.high - p.low) * 0.4;
    [0, 0.22].forEach(offset => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'square';
      osc.connect(gain);
      gain.connect(ctx.destination);
      const t0 = now + offset;
      osc.frequency.setValueAtTime(freq, t0);
      gain.gain.setValueAtTime(vol(0.4), t0);
      gain.gain.linearRampToValueAtTime(0.0001, t0 + 0.18);
      osc.start(t0);
      osc.stop(t0 + 0.2);
    });
  } catch (e) { /* autoplay may be blocked until the user interacts once - fine, just skip */ }
}

// per-event de-dup Sets for the alert types below - added 2026-09-25. The
// server's snapshot dict is served to every /data poll until poll_loop's
// NEXT iteration overwrites it (poll_loop runs every ~2s, the client polls
// every ~1s), so without keying by the actual underlying EVENT (not just
// "is the flag true right now"), the same single event got announced
// multiple times - a real user report ("repeated himself four times for
// one whale buy"). Same pattern checkFlipAlerts/checkBuildingAlerts already
// used for their own alerts, now applied here too.
const alertedBooms = new Set();
const alertedCertainFlips = new Set();
const alertedWhales = new Set();
const alertedWalls = new Set();

function checkReversalAlerts() {
  if (!OLD_CALL_ALERTS_ON) return;
  if (!soundEnabled) return;
  const els = document.querySelectorAll('.boom-alert');
  els.forEach(el => {
    const name = el.getAttribute('data-boom-name');
    const swing = el.getAttribute('data-boom-swing'); // 'up' or 'down'
    const vsCall = el.getAttribute('data-boom-vs-call'); // 'against'/'with'/''
    const key = el.getAttribute('data-boom-key');
    if (name && key && !alertedBooms.has(key) && soundOn(name)) {
      alertedBooms.add(key);
      playReversalAlarm(name);
      const dir = swing === 'up' ? 'Swinging up' : 'Swinging down';
      const tail = vsCall === 'against' ? ', against your call' : vsCall === 'with' ? ', confirming your call' : '';
      speakDelayed(name + '. ' + dir + tail + '.');
    }
  });
}

// big, hard-to-miss "for sure" flip alert (added 2026-09-24 per user
// request) - separate from the small BOOM pill already in each card. Only
// fires when a fast price move against the locked call is ALSO backed by
// Kalshi's own price now favoring the other side (CERTAIN_FLIP_MIN_CHANCE),
// not just a quick wiggle. Appended to document.body, not #content, since
// #content gets wiped by the 1s refresh - same technique as showWhaleCrossing.
// Alerts follow the view (2026-10-14): on "BTC only" the XRP pop-ups / sounds are skipped and vice versa, so an XRP warning never looks like it is about
// the coin on screen. "Both" shows and plays everything as before.
// ---- alert dock (2026-10-14): alerts used to flash over the whole screen; now each coin's alerts show in a small box on its own card
// (the empty space beside "Kalshi says") and clear themselves when they expire. The page re-draws every second, so the items live here and are re-filled.
const dockItems = {};
function dockPush(name, cls, head, detail, ttlMs, key, onDismiss) {
  const list = dockItems[name] = dockItems[name] || [];
  const until = ttlMs ? Date.now() + ttlMs : 0;                 // 0 = stays until it is cleared
  const ex = key ? list.find(x => x.key === key) : null;
  if (ex) { ex.cls = cls; ex.head = head; ex.detail = detail; ex.until = until; ex.onDismiss = onDismiss; }
  else list.unshift({key: key || ('k' + Math.random().toString(36).slice(2)), cls: cls, head: head, detail: detail, until: until, onDismiss: onDismiss});
  if (list.length > 8) list.length = 8;
  renderDock();
}
function dockClear(name, key) {
  const list = dockItems[name];
  if (!list) return;
  const i = list.findIndex(x => x.key === key);
  if (i >= 0) { list.splice(i, 1); renderDock(); }
}
function dockDismiss(name, key) {
  const x = (dockItems[name] || []).find(y => y.key === key);
  if (x && x.onDismiss) x.onDismiss();
  dockClear(name, key);
}
function renderDock() {
  const now = Date.now();
  document.querySelectorAll('.alert-dock[data-dock]').forEach(el => {
    const name = el.getAttribute('data-dock');
    const list = (dockItems[name] || []).filter(x => !x.until || x.until > now);
    dockItems[name] = list;
    const sig = list.map(x => x.key + '|' + x.cls + '|' + x.head + '|' + x.detail).join('~');
    if (el.getAttribute('data-sig') === sig) return;
    el.setAttribute('data-sig', sig);
    el.textContent = '';
    if (!list.length) {
      const e = document.createElement('div'); e.className = 'dock-empty'; e.textContent = 'alerts show here'; el.appendChild(e);
      return;
    }
    list.forEach(x => {
      const d = document.createElement('div'); d.className = 'dock-item ' + x.cls; d.title = x.head + ' - ' + x.detail + ' (click to dismiss)';
      d.onclick = () => dockDismiss(name, x.key);
      const h = document.createElement('b'); h.textContent = x.head;
      const t = document.createElement('span'); t.textContent = x.detail;
      d.appendChild(h); d.appendChild(t); el.appendChild(d);
    });
  });
}
setInterval(renderDock, 500);
// time-critical alerts ("flip for sure", incoming buy / sell, huge prints and walls) fly across the whole screen once instead of sitting in the alert box
let flyCount = 0;
function flyAcross(name, cls, head, detail) {
  const el = document.createElement('div');
  el.className = 'fly-alert ' + cls;
  el.style.top = (96 + (flyCount++ % 3) * 92) + 'px';
  const h = document.createElement('div'); h.textContent = name + ' - ' + head;
  const t = document.createElement('small'); t.textContent = detail || '';
  el.appendChild(h); el.appendChild(t);
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 10500);
}
function coinShown(name) {
  const v = document.body.getAttribute('data-view');
  return !((v === 'BTC' && name !== 'BTC') || (v === 'XRP' && name !== 'XRP'));
}
function soundOn(name) { return assetSoundEnabled[name] !== false && coinShown(name); }
function showBigFlipAlert(assetName, side) {
  if (!coinShown(assetName)) return;
  const head = '\u26A0 FLIP - ' + side + ' FOR SURE? ' + (side === 'YES' ? '\u2B06' : '\u2B07');
  const detail = "Kalshi's own price just swung the other way - check it yourself";
  flyAcross(assetName, side === 'YES' ? 'fly-buy' : 'fly-sell', head, detail);                         // goes across the screen ...
  dockPush(assetName, side === 'YES' ? 'dock-buy' : 'dock-sell', head, detail, 60000);                 // ... AND sticks in the alert box for a full minute
}

function checkCertainFlipAlerts() {
  if (!OLD_CALL_ALERTS_ON) return;
  const els = document.querySelectorAll('.certain-flip-alert');
  els.forEach(el => {
    const name = el.getAttribute('data-flip-name');
    const side = el.getAttribute('data-flip-side');
    const key = el.getAttribute('data-flip-alert-key');
    if (!name || !side || !key || alertedCertainFlips.has(key)) return;
    alertedCertainFlips.add(key);
    showBigFlipAlert(name, side);
    if (soundEnabled && soundOn(name)) {
      playSirenAlert(name);
      speakUrgent(name + '. Siren. Flip, for sure, ' + side + '.');
    }
  });
}

// "cha-ching" win celebration - added 2026-09-27 per user request ("if the
// bet is won, I'd like a cha-ching with a money symbol to go across the
// screen"). Fires on every REAL-MONEY win (either strategy) - a synthesized
// bell (no audio file, same "own oscillators" convention as every other
// sound here) plus a money-bag banner sweeping across the screen.
function playChaChing() {
  try {
    const ctx = ensureAudioCtx();
    const now = ctx.currentTime;
    const notes = [880, 1318.5]; // A5, E6 - a bright two-note "cha-ching" bell
    notes.forEach((f, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'triangle';
      osc.connect(gain); gain.connect(ctx.destination);
      const t0 = now + i * 0.11;
      osc.frequency.setValueAtTime(f, t0);
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.linearRampToValueAtTime(vol(0.5), t0 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.35);
      osc.start(t0); osc.stop(t0 + 0.4);
    });
  } catch (e) { /* autoplay may be blocked until first interaction - fine */ }
}

function spawnFireworks(originX, originY) {
  // a single firework burst - added 2026-09-27 per user request ("Fireworks").
  // Particle emojis as JS unicode escapes, not literal characters - this
  // toolchain silently corrupts raw non-ASCII in source files.
  const particleEmojis = ['\u{2728}', '\u{1F386}', '\u{1F4B0}']; // sparkle, firework, money bag
  const count = 14;
  for (let i = 0; i < count; i++) {
    const el = document.createElement('div');
    el.className = 'firework-particle';
    el.textContent = particleEmojis[Math.floor(Math.random() * particleEmojis.length)];
    el.style.left = originX + 'px';
    el.style.top = originY + 'px';
    document.body.appendChild(el);
    const angle = (Math.PI * 2 * i) / count + (Math.random() * 0.4 - 0.2);
    const dist = 110 + Math.random() * 170;
    const dx = Math.cos(angle) * dist;
    const dy = Math.sin(angle) * dist;
    try {
      const anim = el.animate(
        [{ transform: 'translate(-50%,-50%) scale(0.3)', opacity: 1 },
         { transform: 'translate(calc(-50% + ' + dx + 'px), calc(-50% + ' + dy + 'px)) scale(1.5)', opacity: 1, offset: 0.6 },
         { transform: 'translate(calc(-50% + ' + (dx * 1.15) + 'px), calc(-50% + ' + (dy * 1.15 + 40) + 'px)) scale(0.7)', opacity: 0 }],
        { duration: 1100 + Math.random() * 400, easing: 'cubic-bezier(0.15,0.6,0.3,1)' }
      );
      anim.onfinish = () => el.remove();
    } catch (e) { el.remove(); }
    setTimeout(() => el.remove(), 1700);
  }
}

function showMoneyBanner(assetName, amount) {
  if (!coinShown(assetName)) return;
  // appended to document.body, not #content (which gets wiped by the 1s
  // refresh) - same technique as showWhaleCrossing/showBigFlipAlert. Money
  // bag emoji written as a JS unicode escape, not a literal character - this
  // toolchain silently corrupts raw non-ASCII in source files.
  const bag = '\u{1F4B0}';
  const bigBag = '<span class="money-banner-emoji">' + bag + '</span>';
  const el = document.createElement('div');
  el.className = 'money-banner';
  el.innerHTML = bigBag + ' CHA-CHING! ' + assetName + ' +$' + amount.toFixed(2) + ' ' + bigBag;
  document.body.appendChild(el);
  try {
    const anim = el.animate(
      [{ left: '-45%', opacity: 0 },
       { opacity: 1, offset: 0.15 },
       { opacity: 1, offset: 0.85 },
       { left: '140%', opacity: 0 }],
      { duration: 3500, easing: 'ease-in-out' }
    );
    anim.onfinish = () => el.remove();
  } catch (e) { el.remove(); }
  setTimeout(() => el.remove(), 4000); // safety net in case onfinish never fires
  // a few staggered fireworks bursts across the screen alongside the banner
  const w = window.innerWidth, h = window.innerHeight;
  spawnFireworks(w * 0.25, h * 0.4);
  setTimeout(() => spawnFireworks(w * 0.5, h * 0.32), 250);
  setTimeout(() => spawnFireworks(w * 0.75, h * 0.42), 500);
}

const alertedWins = new Set();
function checkWinAlerts() {
  document.querySelectorAll('.win-alert').forEach(el => {
    const name = el.getAttribute('data-win-name');
    const amount = parseFloat(el.getAttribute('data-win-amount'));
    const key = el.getAttribute('data-win-key');
    if (!name || !key || alertedWins.has(key) || isNaN(amount)) return;
    alertedWins.add(key);
    showMoneyBanner(name, amount);
    if (soundEnabled && soundOn(name)) {
      playChaChing();
      speakDelayed(name + '. Cha-ching! Won $' + amount.toFixed(2) + '.');
    }
  });
}

// loss banner - added 2026-09-27 per user request ("when it's wrong...
// a middle finger... fuck you, Kalshi"). Mirrors the win celebration above.
function playLossBuzz() {
  try {
    const ctx = ensureAudioCtx();
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sawtooth';
    osc.connect(gain); gain.connect(ctx.destination);
    osc.frequency.setValueAtTime(180, now);
    osc.frequency.exponentialRampToValueAtTime(70, now + 0.45);
    gain.gain.setValueAtTime(vol(0.4), now);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.5);
    osc.start(now); osc.stop(now + 0.55);
  } catch (e) { /* autoplay may be blocked until first interaction - fine */ }
}

function showLossBanner(assetName, amount) {
  if (!coinShown(assetName)) return;
  // middle-finger emoji written as a JS unicode escape, not a literal
  // character - this toolchain silently corrupts raw non-ASCII in source files.
  const finger = '\u{1F595}';
  const bigFinger = '<span class="loss-banner-emoji">' + finger + '</span>';
  const el = document.createElement('div');
  el.className = 'loss-banner';
  el.innerHTML = bigFinger + ' FUCK YOU, KALSHI! ' + assetName + ' ' + amount.toFixed(2) + ' ' + bigFinger;
  document.body.appendChild(el);
  try {
    const anim = el.animate(
      [{ left: '-45%', opacity: 0 },
       { opacity: 1, offset: 0.15 },
       { opacity: 1, offset: 0.85 },
       { left: '140%', opacity: 0 }],
      { duration: 3500, easing: 'ease-in-out' }
    );
    anim.onfinish = () => el.remove();
  } catch (e) { el.remove(); }
  setTimeout(() => el.remove(), 4000);
}

const alertedLosses = new Set();
function checkLossAlerts() {
  document.querySelectorAll('.loss-alert').forEach(el => {
    const name = el.getAttribute('data-loss-name');
    const amount = parseFloat(el.getAttribute('data-loss-amount'));
    const key = el.getAttribute('data-loss-key');
    if (!name || !key || alertedLosses.has(key) || isNaN(amount)) return;
    alertedLosses.add(key);
    showLossBanner(name, amount);
    if (soundEnabled && soundOn(name)) {
      playLossBuzz();
      speakDelayed(name + '. Lost $' + Math.abs(amount).toFixed(2) + '. Fuck you, Kalshi!');
    }
  });
}

// voice announcement the moment a call locks - added 2026-09-28 per user
// request ("call out the call as soon as it calls the call"). No banner/sound
// effect, just the narrator - a call lock isn't a win/loss event.
function showLockToast(name, call, kp) {
  if (!coinShown(name)) return;
  dockPush(name, 'dock-lock', 'CALL LOCKED: ' + call, kp ? '@ ' + kp : '', 45000);
}
// RSI extreme alert - added 2026-10-10 per user request: big pulsing banner (no screen flash) + beep + voice when the
// 1-minute RSI(14) reaches 25 or below (oversold) or 75 or above (overbought). Fires once per entry into the zone; it
// leaves the zone at 28 / 72 so a wobble around 25 or 75 does not re-alert. Information only - never used for buying.
const RSI_LOW = 25, RSI_HIGH = 75, RSI_BACK_LOW = 28, RSI_BACK_HIGH = 72;
const rsiZone = {};
const rsiDismissed = {};
try { Object.assign(rsiZone, JSON.parse(sessionStorage.getItem('rsiZone') || '{}')); } catch (e) {}
function playRsiAlert(zone) {
  try {
    const ctx = ensureAudioCtx();
    const now = ctx.currentTime;
    const notes = zone === 'under' ? [660, 440, 330] : [440, 660, 880];     // falling = oversold, rising = overbought
    notes.forEach((f, i) => {
      const osc = ctx.createOscillator(); const gain = ctx.createGain();
      osc.type = 'triangle'; osc.connect(gain); gain.connect(ctx.destination);
      const t0 = now + i * 0.22;
      osc.frequency.setValueAtTime(f, t0);
      gain.gain.setValueAtTime(vol(0.5), t0);
      gain.gain.linearRampToValueAtTime(0.0001, t0 + 0.2);
      osc.start(t0); osc.stop(t0 + 0.22);
    });
  } catch (e) { /* audio may be blocked until the page has been clicked once */ }
}
function checkRsiAlerts() {
  let box = document.getElementById('rsi-alerts');
  if (!box) { box = document.createElement('div'); box.id = 'rsi-alerts'; document.body.appendChild(box); }
  const live = {};
  document.querySelectorAll('.rsi-marker').forEach(el => {
    const name = el.getAttribute('data-rsi-name');
    const v = parseFloat(el.getAttribute('data-rsi'));
    if (!name || isNaN(v)) return;
    const prev = rsiZone[name] || '';
    let z = '';
    if (v <= RSI_LOW) z = 'under';
    else if (v >= RSI_HIGH) z = 'over';
    else if (prev === 'under' && v < RSI_BACK_LOW) z = 'under';
    else if (prev === 'over' && v > RSI_BACK_HIGH) z = 'over';
    if (z !== prev) {
      rsiZone[name] = z; rsiDismissed[name] = false;
      try { sessionStorage.setItem('rsiZone', JSON.stringify(rsiZone)); } catch (e) {}
      if (z && soundEnabled && soundOn(name)) {
        playRsiAlert(z);
        speakDelayed(name + '. Warning. ' + (z === 'under' ? 'Oversold.' : 'Overbought.'), 600);
      }
    }
    if (z && !rsiDismissed[name] && coinShown(name)) live[name] = {z: z, v: v};
  });
  ['BTC', 'XRP'].forEach(name => {
    if (!live[name]) { dockClear(name, 'rsi'); return; }
    const z = live[name].z, v = live[name].v;
    dockPush(name, z === 'under' ? 'dock-rsi-under' : 'dock-rsi-over',
      '\u26A0 ' + (z === 'under' ? 'OVERSOLD' : 'OVERBOUGHT') + ' - RSI ' + v.toFixed(0),
      '1-minute RSI ' + (z === 'under' ? 'at or below 25' : 'at or above 75') + ' - information only',
      0, 'rsi', () => { rsiDismissed[name] = true; });
  });
}
window.testRsiAlert = function(name, zone) {   // console helper: testRsiAlert('BTC','under')
  rsiZone[name || 'BTC'] = ''; const el = document.createElement('span'); el.className = 'rsi-marker';
  el.setAttribute('data-rsi-name', name || 'BTC'); el.setAttribute('data-rsi', zone === 'over' ? '78' : '22'); el.style.display = 'none';
  document.body.appendChild(el); checkRsiAlerts(); el.remove();
};
// "INCOMING" banner - a huge buy / sell building across the exchanges (burst of taker flow, giant print, or a big resting wall)
// - added 2026-10-10 per user request, so it is seen before it shows up on the chart. Big, red / green, stays ~16s, click to dismiss.
function spokenUsd(v) {
  if (v >= 1e6) return (v / 1e6).toFixed(1) + ' million dollars';
  return Math.round(v / 1e3) + ' thousand dollars';
}
function showIncomingBanner(name, side, usd, head, detail, key) {
  if (!coinShown(name)) return;
  if (head.indexOf('LIQUIDITY SWEEP') >= 0)            // sweeps stay a solid minute (whether they end up working or not), newest on top
    dockPush(name, side === 'buy' ? 'dock-buy' : 'dock-sell', '\u26A0 ' + head, detail, 60000, key);
  else {
    flyAcross(name, side === 'buy' ? 'fly-buy' : 'fly-sell', '\u26A0 ' + head, detail);                // across the screen ...
    dockPush(name, side === 'buy' ? 'dock-buy' : 'dock-sell', '\u26A0 ' + head, detail, 45000);      // ... AND it stays in the alert box for 45 seconds
  }
}
const alertedIncoming = new Set();
function checkIncomingAlerts() {
  document.querySelectorAll('.incoming-alert').forEach(el => {
    const name = el.getAttribute('data-name'), side = el.getAttribute('data-side'), key = el.getAttribute('data-key');
    const usd = parseFloat(el.getAttribute('data-usd')) || 0;
    if (!name || !key || alertedIncoming.has(key) || (side !== 'buy' && side !== 'sell')) return;
    alertedIncoming.add(key);
    showIncomingBanner(name, side, usd, 'INCOMING ' + side.toUpperCase(),
      fmtUsdShort(usd) + ' of aggressive ' + (side === 'buy' ? 'BUYING' : 'SELLING') + ' in the last 10 seconds across ' +
      el.getAttribute('data-venues') + ' exchanges (biggest: ' + el.getAttribute('data-top') + ') - ahead of the chart');
    if (soundEnabled && soundOn(name)) {
      playWhaleSound(side); playWallAlert(side);
      speakUrgent(name + '. Incoming ' + side + '. ' + spokenUsd(usd) + '.');
    }
  });
}
// LIQUIDITY SWEEP warning - fires once when a sweep starts to form (the open candle ran past equal highs/lows and price came back inside).
// Information only. Which timeframes alert is saved per browser; 1-minute is off by default (about 160 a day).
let sweepAlertsOn = true;
let sweepTfOn = { '1m': false, '5m': true, '15m': true, '1h': true };
try {
  const sv = localStorage.getItem('sweepAlertsOn'); if (sv !== null) sweepAlertsOn = sv === 'true';
  const st = JSON.parse(localStorage.getItem('sweepTfOn') || 'null'); if (st) Object.assign(sweepTfOn, st);
} catch (e) {}
function syncSweepButtons() {
  const b = document.getElementById('toggle-sweep');
  if (b) { b.textContent = 'Sweep warning: ' + (sweepAlertsOn ? 'ON' : 'OFF'); b.classList.toggle('asset-sound-on', sweepAlertsOn); b.classList.toggle('asset-sound-off', !sweepAlertsOn); }
  ['1m', '5m', '15m', '1h'].forEach(tf => {
    const x = document.getElementById('sweep-tf-' + tf);
    if (x) { x.textContent = tf + (sweepTfOn[tf] ? ' on' : ' off'); x.classList.toggle('asset-sound-on', !!sweepTfOn[tf]); x.classList.toggle('asset-sound-off', !sweepTfOn[tf]); }
  });
}
function toggleSweepAlerts() { sweepAlertsOn = !sweepAlertsOn; try { localStorage.setItem('sweepAlertsOn', String(sweepAlertsOn)); } catch (e) {} syncSweepButtons(); }
function toggleSweepTf(tf) { sweepTfOn[tf] = !sweepTfOn[tf]; try { localStorage.setItem('sweepTfOn', JSON.stringify(sweepTfOn)); } catch (e) {} syncSweepButtons(); }
const SWEEP_TF_SPOKEN = { '1m': '1 minute', '5m': '5 minute', '15m': '15 minute', '1h': '1 hour' };
function fireSweepWarning(name, tf, side, bias, key) {
  const up = bias === 'bullish';
  showIncomingBanner(name, up ? 'buy' : 'sell', 0, '\u2605 LIQUIDITY SWEEP FORMING (' + tf + ') \u2605',
    'Price ran ' + (side === 'highs' ? 'above the equal highs and is back below them' : 'below the equal lows and is back above them') +
    ' - a stop run. Leans ' + (up ? 'UP' : 'DOWN') + '. Still forming: it is only confirmed when the ' + tf + ' candle closes. Information only.', key);
  if (soundEnabled && soundOn(name)) {
    playWallAlert(up ? 'buy' : 'sell');
    speakUrgent(name + '. Liquidity sweep forming on the ' + (SWEEP_TF_SPOKEN[tf] || tf) + '. Leans ' + (up ? 'up' : 'down') + '.');
  }
}
const alertedSweeps = new Set();
let sweepPrimed = false;
function checkSweepAlerts() {
  document.querySelectorAll('.sweep-alert').forEach(el => {
    const key = el.getAttribute('data-key');
    if (!key || alertedSweeps.has(key)) return;
    alertedSweeps.add(key);
    if (!sweepPrimed) return;                       // sweeps that were already forming when the page opened are not announced
    const tf = el.getAttribute('data-tf');
    if (!sweepAlertsOn || !sweepTfOn[tf]) return;
    fireSweepWarning(el.getAttribute('data-name'), tf, el.getAttribute('data-side'), el.getAttribute('data-bias'), key);
  });
  if (document.querySelector('.card[data-asset]')) sweepPrimed = true;
}
window.testSweepAlert = function(name, tf, bias) { fireSweepWarning(name || 'BTC', tf || '5m', bias === 'bullish' ? 'lows' : 'highs', bias || 'bearish'); };
const HUGE_PRINT_USD = { BTC: 1000000, XRP: 150000 };
const alertedLocks = new Set();
function checkLockAlerts() {
  document.querySelectorAll('.lock-alert').forEach(el => {
    const name = el.getAttribute('data-lock-name');
    const call = el.getAttribute('data-lock-call');
    const key = el.getAttribute('data-lock-key');
    if (!name || !call || !key || alertedLocks.has(key)) return;
    alertedLocks.add(key);
    // the OLD "call locked" voice + alert box entry are OFF (2026-10-16): that was the old live call locking early, which is not the real locked call any more
    // (the END CALL locks at the 8th minute and is shown in its own box). Delete the next line's comment marker pair to bring them back:
    // showLockToast(name, call, el.getAttribute('data-lock-kp')); if (soundEnabled && soundOn(name)) { speakDelayed(name + '. Locked ' + call + '.', 100); }
  });
}

function checkRegimeAlerts() {
  const els = document.querySelectorAll('.regime-value');
  els.forEach(el => {
    const name = el.getAttribute('data-regime-name');
    const regime = el.getAttribute('data-regime');
    const target = FAVORABLE_REGIME[name];
    if (!name || !target || !regime) return;
    const prev = lastRegimeSeen[name];
    if (regime === target && prev !== target && soundEnabled && soundOn(name)) {
      playOpportunityChime(name);
      speakDelayed(name + '. Chime. Favorable conditions now.');
    }
    lastRegimeSeen[name] = regime;
  });
}

// whale-song sound - a slow, deep, resonant two-part moan (down-glide then
// up-glide), distinct from every other alert. Rises in pitch a bit for a
// whale BUY, drops for a whale SELL, so you can tell direction by ear.
function playWhaleSound(side) {
  try {
    const ctx = ensureAudioCtx();
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.connect(gain);
    gain.connect(ctx.destination);
    const base = 90;
    const peak = side === 'buy' ? 220 : 150;
    const end = side === 'buy' ? 130 : 70;
    osc.frequency.setValueAtTime(base, now);
    osc.frequency.linearRampToValueAtTime(peak, now + 0.5);
    osc.frequency.linearRampToValueAtTime(end, now + 1.4);
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.linearRampToValueAtTime(vol(0.5), now + 0.15);
    gain.gain.setValueAtTime(vol(0.5), now + 1.1);
    gain.gain.linearRampToValueAtTime(0.0001, now + 1.5);
    osc.start(now);
    osc.stop(now + 1.55);
  } catch (e) { /* autoplay may be blocked until first interaction - fine */ }
}

// whale swims across THAT COIN'S card only (not the whole screen) when a
// huge trade or wall shows up - added 2026-09-13 per user request. Buy = up
// arrow, swims left-to-right, green glow. Sell = down arrow, swims right-
// to-left, red glow. Shown regardless of sound settings (this is the visual
// cue, separate from whether the accompanying sound plays).
function fmtUsdShort(v) {
  if (v >= 1e6) return '$' + (v / 1e6).toFixed(v >= 1e7 ? 0 : 1) + 'M';
  return '$' + Math.round(v / 1e3) + 'k';
}
// where on SCREEN the whale runs: across the part of that coin's card you can actually see - or across the whole window when the card is
// hidden (BTC only / XRP only view) or scrolled away. (Fixed 2026-10-14: it used 32% of the card's whole height, which is far below the screen now.)
function whaleLane(card) {
  const vw = window.innerWidth, vh = window.innerHeight;
  let left = 0, right = vw, top = vh * 0.28;
  if (card) {
    const r = card.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) {
      const vt = Math.max(r.top, 0), vb = Math.min(r.bottom, vh);
      if (vb - vt > 80) { top = vt + (vb - vt) * 0.28; left = Math.max(r.left, 0); right = Math.min(r.right, vw); }
    }
  }
  return { top: top, left: left, right: right };
}
function showWhaleCrossing(assetName, side, usd, venue) {
  if (!coinShown(assetName)) return;
  const card = document.querySelector('.card[data-asset="' + assetName + '"]');
  const lane = whaleLane(card);
  const el = document.createElement('div');
  el.className = 'whale-cross ' + (side === 'buy' ? 'whale-buy' : 'whale-sell');
  // whale + up/down arrow, written as JS unicode escapes (not literal emoji)
  // - this toolchain silently corrupts raw non-ASCII characters in source files
  el.textContent = (side === 'buy' ? '\u{1F40B}\u2B06\uFE0F' : '\u{1F40B}\u2B07\uFE0F') + (usd > 0 ? ' ' + fmtUsdShort(usd) : '');
  el.style.position = 'fixed';
  el.style.top = lane.top + 'px';
  document.body.appendChild(el);
  const startLeft = side === 'buy' ? lane.left - 130 : lane.right - 60;
  const endLeft = side === 'buy' ? lane.right - 60 : lane.left - 130;
  el.style.left = startLeft + 'px';
  try {
    const anim = el.animate(
      [{ left: startLeft + 'px', opacity: 0 },
       { opacity: 1, offset: 0.12 },
       { opacity: 1, offset: 0.88 },
       { left: endLeft + 'px', opacity: 0 }],
      { duration: 3200, easing: 'ease-in-out' }
    );
    anim.onfinish = () => el.remove();
  } catch (e) { /* Web Animations API unavailable - just show it briefly, static */ }
  setTimeout(() => el.remove(), 3500); // safety net in case onfinish never fires
}

function checkWhaleAlerts() {
  const els = document.querySelectorAll('.whale-alert');
  els.forEach(el => {
    const name = el.getAttribute('data-whale-name');
    const wside = el.getAttribute('data-whale-side');
    const key = el.getAttribute('data-whale-key');
    if (name && key && !alertedWhales.has(key) && (wside === 'buy' || wside === 'sell')) {
      alertedWhales.add(key);
      const wusd = parseFloat(el.getAttribute('data-whale-usd')) || 0;
      showWhaleCrossing(name, wside, wusd, el.getAttribute('data-whale-venue') || '');
      if (wusd >= (HUGE_PRINT_USD[name] || 1e9)) {
        showIncomingBanner(name, wside, wusd, 'HUGE ' + wside.toUpperCase() + ' PRINT', fmtUsdShort(wusd) + ' just went through on ' + (el.getAttribute('data-whale-venue') || 'an exchange'));
      }
      if (soundEnabled && soundOn(name)) {
        playWhaleSound(wside);
        speakDelayed(name + '. Whale ' + wside + (wusd >= 1e6 ? '. ' + (wusd / 1e6).toFixed(1) + ' million dollars.' : (wusd >= 1e3 ? '. ' + Math.round(wusd / 1e3) + ' thousand dollars.' : '.')));
      }
    }
  });
}

// order-book wall alert - a big RESTING order just parked itself right next
// to the price. A SELL wall (whale lined up to dump) gets an urgent
// descending three-note warning; a BUY wall gets a calmer ascending one.
function playWallAlert(side) {
  try {
    const ctx = ensureAudioCtx();
    const now = ctx.currentTime;
    const notes = side === 'sell' ? [520, 400, 300] : [300, 400, 520];
    notes.forEach((f, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'triangle';
      osc.connect(gain); gain.connect(ctx.destination);
      const t0 = now + i * 0.13;
      osc.frequency.setValueAtTime(f, t0);
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.linearRampToValueAtTime(vol(0.4), t0 + 0.02);
      gain.gain.linearRampToValueAtTime(0.0001, t0 + 0.16);
      osc.start(t0); osc.stop(t0 + 0.18);
    });
  } catch (e) { /* autoplay may be blocked until first interaction - fine */ }
}

function checkWallAlerts() {
  document.querySelectorAll('.wall-alert').forEach(el => {
    const name = el.getAttribute('data-wall-name');
    const wside = el.getAttribute('data-wall-side');
    const key = el.getAttribute('data-wall-key');
    if (name && key && !alertedWalls.has(key) && (wside === 'buy' || wside === 'sell')) {
      alertedWalls.add(key);
      showWhaleCrossing(name, wside);
      const wu = parseFloat(el.getAttribute('data-wall-usd')) || 0;
      if (wu > 0) {
        showIncomingBanner(name, wside, wu, 'INCOMING ' + wside.toUpperCase() + ' WALL', fmtUsdShort(wu) + ' resting ' + (wside === 'sell' ? 'offer' : 'bid') + ' parked at ' + el.getAttribute('data-wall-price') + ' (' + (el.getAttribute('data-wall-src') || 'Coinbase book') + ') - ahead of the chart');
      }
      if (soundEnabled && soundOn(name)) {
        playWallAlert(wside);
        speakDelayed(name + '. ' + (wside === 'sell' ? 'Sell wall.' : 'Buy wall.'));
      }
    }
  });
}

// candle chart timeframe switch - rebuilt 2026-09-13 SERVER-rendered (the
// client-side JS rewrite broke and was reverted the same day - this is
// simpler and reuses the already-proven server rendering path). Clicking
// 15m/30m/1h just fetches that one chart's fresh SVG and swaps it in;
// doesn't touch anything else on the page. Persisted per-asset so the
// choice survives the next 3s poll (the poll always redraws the DEFAULT
// 15m chart server-side, then this re-applies the saved choice).
let chartTimeframe = {};
try {
  const saved = JSON.parse(localStorage.getItem('chartTimeframe') || 'null');
  if (saved) chartTimeframe = saved;
} catch (e) {}

async function fetchChartTimeframe(btn) {
  const name = btn.getAttribute('data-asset');
  const tf = btn.getAttribute('data-tf');
  chartTimeframe[name] = tf;
  try { localStorage.setItem('chartTimeframe', JSON.stringify(chartTimeframe)); } catch (e) {}
  await applyChartTimeframe(name, tf);
}

async function applyChartTimeframe(name, tf) {
  document.querySelectorAll('.chart-tf-btn[data-asset="' + name + '"]').forEach(b => {
    b.classList.toggle('chart-tf-active', b.getAttribute('data-tf') === String(tf));
  });
  const holder = document.querySelector('.chart-svg-holder[data-asset="' + name + '"]');
  if (!holder) return;
  try {
    const res = await fetch('/chart?asset=' + encodeURIComponent(name) + '&tf=' + encodeURIComponent(tf));
    const data = await res.json();
    if (data.svg) holder.innerHTML = data.svg;
  } catch (e) { /* leave whatever was already showing - not worth erroring over a chart refresh */ }
}

function reapplySavedChartTimeframes() {
  Object.keys(chartTimeframe).forEach(name => {
    if (chartTimeframe[name] && chartTimeframe[name] !== '15') {
      applyChartTimeframe(name, chartTimeframe[name]);
    }
  });
}

// bumped on every refresh() call, checked after the fetch resolves - added
// 2026-09-24, fixing a real race: if any one /data request ever takes
// longer than the 1s interval (a slow poll, a network hiccup), an OLDER
// response could resolve AFTER a newer one and overwrite it, silently
// stomping whatever was just typed into an input a moment before. Only the
// MOST RECENTLY STARTED refresh is ever allowed to actually touch the DOM -
// any response that resolves after a newer refresh has already started is
// just discarded.
let refreshSeq = 0;

// live-trading settings boxes (bet size, flip sell target, flip max entry
// price) - skip the 1s refresh ENTIRELY while one of these is focused,
// added 2026-09-25 per user request (the flip sell-target/max-price boxes
// "won't let me type in it"). The old capture-restore-on-swap approach
// (still below, kept for any other input) had a real race: a keystroke
// typed WHILE a refresh's fetch was in flight landed in the DOM node about
// to be destroyed by the innerHTML swap and was silently dropped - and a
// 15-minute window rollover mid-edit hit that same race. Simplest fix:
// just don't touch the DOM at all while one of these is focused, so there's
// nothing to race against; the page picks back up on the next refresh after
// you blur or hit Set.
const LIVE_SETTINGS_INPUT_PREFIX = /^(livesize-input-|fliptarget-input-|flipmaxprice-input-|flipsell-input-|protectthresh-input-)/;

// fixed 2026-10-04: the screen used to freeze FOREVER while a settings box
// stayed selected (user saw no calls for ~an hour after clicking into the bet
// size box). Now it only pauses for 15s after the last click/keystroke in one;
// after that the refresh resumes and the existing restore logic below keeps
// whatever is being typed.
async function setPatternTf(asset, tf) {
  // light the clicked tab up RIGHT AWAY (the page is only rebuilt from the server about once a second), then tell the server
  document.querySelectorAll('.pat-win[data-asset="' + asset + '"] .pat-win-tfs button').forEach(b => {
    const on = (b.getAttribute('onclick') || '').indexOf("'" + tf + "'") >= 0;
    b.className = on ? 'size-mode-btn size-mode-active' : 'size-mode-btn';
  });
  await fetch('/set_pattern_tf?asset=' + encodeURIComponent(asset) + '&tf=' + encodeURIComponent(tf));
  refreshBusySince = 0; refresh();
}
let expandedAsset = null;
function expandPatternChart(asset) { expandedAsset = asset; renderExpanded(); }
function closeExpanded() { expandedAsset = null; const o = document.getElementById('pat-overlay'); if (o) o.remove(); }
function renderExpanded() {
  if (!expandedAsset) return;
  const src = document.querySelector('.pat-win[data-asset="' + expandedAsset + '"]');
  if (!src) return;
  let o = document.getElementById('pat-overlay');
  if (!o) { o = document.createElement('div'); o.id = 'pat-overlay'; document.body.appendChild(o); }
  const chart = src.querySelector('.pat-chart'); const tfs = src.querySelector('.pat-win-tfs');
  o.innerHTML = '<div class="pat-ov-bar"><span class="live-clock live-clock-big"><span class="lc-t"></span><span class="lc-d"></span></span><b>' + expandedAsset + ' - what is being formed (live)</b> ' + (tfs ? tfs.outerHTML : '') +
    ' <button onclick="closeExpanded()">Close (Esc)</button></div><div class="pat-ov-chart">' + (chart ? chart.innerHTML : '') + '</div>';
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeExpanded(); });
// which pull-downs are open is remembered in this browser, so they are still open after a reload (the heat map starts open the very first time; the per-pattern / per-event ones are not saved)
function loadOpenPats() {
  try { const s = localStorage.getItem('openPats'); if (s !== null) { const a = JSON.parse(s); if (Array.isArray(a)) return a; } } catch (e) {}
  return ['BM|BTC', 'BM|XRP'];
}
function saveOpenPats() {
  try { localStorage.setItem('openPats', JSON.stringify(Array.from(openPatterns).filter(k => !/^(WIN|KEPT|KEPTLIST|EV|HIST)[|]/.test(k)))); } catch (e) {}
}
const openPatterns = new Set(loadOpenPats());
// "Recent completed calls that worked": ONE switch for every coin / timeframe. It is OPEN every time the page loads; if you close it, it STAYS closed (until the next page load).
// (It is not saved with the other pull-downs: the other coin's hidden copy used to re-open it on every refresh.)
let keptListOpen = true;
function fillPatternInfo(d) {
  const box = d.querySelector('.pat-info');
  if (!box || box.dataset.filled === '1') return;
  const e = PATTERN_DATA.lib[d.dataset.patkey];
  box.dataset.filled = '1';
  if (!e) { box.textContent = 'No write-up for this one yet.'; return; }
  box.innerHTML = '<div class="pat-info-row"><div class="pat-pic">' + (e.svg || '') + '</div><div class="pat-text">' +
    '<div><b>What it is:</b> ' + e.what + '</div><div><b>What it usually means:</b> ' +
    e.means + '</div><div><b>Watch for:</b> ' + e.watch + '</div>' + patDirection(d.dataset.bias) +
    '</div></div><div class="pat-disc">' + PATTERN_DATA.disclaimer + '</div>';
}
// which way the pattern points if it plays out - green up arrow (bullish), red down arrow (bearish), grey for neither
function patDirection(bias) {
  if (bias === 'bullish') return '<div class="pat-dir up" title="bullish: if it plays out, price is expected to go UP">&#9650; <small>expects price up</small></div>';
  if (bias === 'bearish') return '<div class="pat-dir down" title="bearish: if it plays out, price is expected to go DOWN">&#9660; <small>expects price down</small></div>';
  return '<div class="pat-dir flat" title="neutral: on its own this does not point either way">&#8596; <small>no direction on its own</small></div>';
}
// the Kalshi arrow lives inside the 1-second re-rendered card, so it is re-created every refresh - start it at last
// refresh's tilt/colour and let CSS glide it to the new one, so it visibly swings up/down instead of snapping
const kalshiArrowPrev = {};
function syncKalshiArrows() {
  document.querySelectorAll('.kalshi-arrow').forEach(el => {
    const n = el.getAttribute('data-name');
    const ang = parseFloat(el.getAttribute('data-angle')); const col = el.getAttribute('data-color');
    const svg = el.querySelector('svg'); const sword = el.querySelector('.sword');
    if (!svg || !sword || isNaN(ang)) return;
    const prev = kalshiArrowPrev[n];
    if (prev) { sword.style.transition = 'none'; svg.style.transition = 'none'; sword.style.transform = 'rotate(' + prev.ang + 'deg)'; svg.style.color = prev.col; void svg.getBoundingClientRect(); }
    sword.style.transition = 'transform 1.1s ease'; svg.style.transition = 'color 1.1s ease';
    sword.style.transform = 'rotate(' + ang + 'deg)'; svg.style.color = col;
    kalshiArrowPrev[n] = {ang: ang, col: col};
  });
}
// pattern history: hour and 15-minute-window bodies are fetched when opened and cached, because the page re-renders every second
const histCache = {};
function loadHist(d) {
  const key = d.getAttribute('data-hist');
  const body = d.querySelector(':scope > .hist-body');
  if (!key || !body) return;
  const c = histCache[key];
  if (body.dataset.filled !== '1' && c && c.html) {
    body.innerHTML = c.html; body.dataset.filled = '1';
    restorePatternTabs(); restoreHist();
  }
  const fresh = c && c.html && (Date.now() - c.t < 20000);
  if (!fresh && !(c && c.loading)) fetchHist(key);
}
async function fetchHist(key) {
  const parts = key.split('|');
  histCache[key] = Object.assign({html: '', t: 0}, histCache[key] || {}, {loading: true});
  try {
    const r = await fetch('/pattern_history?asset=' + encodeURIComponent(parts[0]) + '&start=' + parts[1] + '&end=' + parts[2] + '&level=' + parts[3]);
    const j = await r.json();
    const had = histCache[key] && histCache[key].html;
    histCache[key] = {html: j.html || '', t: Date.now(), loading: false};
    const d = document.querySelector('details[data-hist="' + key + '"]');
    const body = d && d.querySelector(':scope > .hist-body');
    if (body && (body.dataset.filled !== '1' || !had)) {
      body.innerHTML = histCache[key].html; body.dataset.filled = '1';
      restorePatternTabs(); restoreHist();
    }
  } catch (e) { if (histCache[key]) histCache[key].loading = false; }
}
function restoreHist() {
  document.querySelectorAll('details[data-hist]').forEach(d => { if (d.open) loadHist(d); });
}
function syncTz() {
  try {
    document.cookie = 'tz=' + (-new Date().getTimezoneOffset()) + '; path=/; max-age=31536000; SameSite=Lax';
    const zn = Intl.DateTimeFormat().resolvedOptions().timeZone;       // e.g. America/New_York - lets the server handle clock changes
    if (zn) document.cookie = 'tzn=' + encodeURIComponent(zn) + '; path=/; max-age=31536000; SameSite=Lax';
  } catch (e) {}
}
syncTz(); setInterval(syncTz, 3600000);
// ---- Fit to screen (2026-10-14): for streaming - nobody is at the mouse, so the page arranges itself: shrink just enough that everything from the top down to
// the bottom of the chart is in the window (never below 60%); if it still does not fit, scroll so the bottom of the chart is at the bottom edge. Saved per browser.
let fitScreen = (window.screen.width >= 1100 && !('ontouchstart' in window));      // phones / tablets start OFF (it keeps scrolling the page for you)
try { const fv = localStorage.getItem('fitScreen'); if (fv !== null) fitScreen = fv === 'true'; } catch (e) {}
let fitDone = false;                                    // true once the page has been fitted; it is only refitted when you switch coin / resize / click the button
let userScrollAt = 0;                                    // when you last scrolled / touched the page: Fit-to-screen never moves the page while you are using it
['wheel', 'touchstart', 'touchmove', 'keydown', 'mousedown'].forEach(ev => window.addEventListener(ev, () => { userScrollAt = Date.now(); }, {passive: true}));
let lastFit = 1;
function syncFitButton() {
  const b = document.getElementById('fit-btn');
  if (!b) return;
  b.textContent = 'Fit to screen: ' + (fitScreen ? 'ON' : 'OFF');
  b.className = 'size-mode-btn' + (fitScreen ? ' size-mode-active' : '');
}
// ---- market tag: click a period in the drop-down to show it on the tag (saved per coin; default 1 hour)
let mktPick = {};
try { mktPick = JSON.parse(localStorage.getItem('mktPick') || '{}') || {}; } catch (e) { mktPick = {}; }
function applyMarketPick() {
  document.querySelectorAll('.mk-drop[data-coin]').forEach(d => {
    const coin = d.getAttribute('data-coin'), want = mktPick[coin] || '1 hour';
    d.querySelectorAll('.mk-row').forEach(r => r.classList.toggle('mk-sel', r.getAttribute('data-period') === want));
    const row = d.querySelector('.mk-row[data-period="' + want + '"]');
    const badge = d.querySelector('summary .mk');
    if (!row || !badge) return;
    const st = row.getAttribute('data-state'), chg = parseFloat(row.getAttribute('data-chg'));
    badge.className = 'mk mk-' + st.toLowerCase();
    const a = badge.querySelector('.mk-state'), b = badge.querySelector('.mk-per');
    if (a) a.textContent = st;
    if (b) b.textContent = want + ' ' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%';
  });
}
function pickMarket(coin, period) {
  mktPick[coin] = period;
  try { localStorage.setItem('mktPick', JSON.stringify(mktPick)); } catch (e) {}
  applyMarketPick();
}
function toggleFit() {
  fitScreen = !fitScreen;
  try { localStorage.setItem('fitScreen', String(fitScreen)); } catch (e) {}
  if (!fitScreen) document.body.style.zoom = '';
  syncFitButton();
  applyFit(true);
}
function applyFit(force) {
  if (!fitScreen) return;
  if (!force && (fitDone || Date.now() - userScrollAt < 10000)) return;
  const rows = Array.from(document.querySelectorAll('.pat-chart-row')).filter(r => r.offsetParent !== null);
  if (!rows.length) return;
  fitDone = true;
  document.body.style.zoom = '1';                         // measure at 100% (all in one task - nothing is painted in between)
  window.scrollTo(0, 0);
  const H = Math.max.apply(null, rows.map(r => r.getBoundingClientRect().bottom)) + 12;
  const vh = window.innerHeight;
  let z = Math.round(Math.min(1, Math.max(0.6, vh / H)) * 50) / 50;      // steps of 2% so it never jitters
  if (!force && Math.abs(z - lastFit) < 0.021) z = lastFit;
  lastFit = z;
  document.body.style.zoom = String(z);
  const total = H * z;
  if (document.body.getAttribute('data-view') === 'both') window.scrollTo(0, 0);       // dual screen: shrink so both coins sit side by side, and stay at the top
  else window.scrollTo(0, total > vh ? total - vh : 0);
}
window.addEventListener('resize', () => applyFit(true));
function setAssetView(v) {
  fitDone = false;                                      // a different coin on screen = fit again once
  if (v !== 'BTC' && v !== 'XRP') v = 'both';
  document.body.setAttribute('data-view', v);
  try { localStorage.setItem('assetView', v); } catch (e) {}
  document.querySelectorAll('.view-bar button').forEach(b =>
    b.className = (b.getAttribute('data-view') === v) ? 'size-mode-btn size-mode-active' : 'size-mode-btn');
}
// fast chart channel: the 1-second page refresh is too slow for a live candle, so the chart + volume bar are re-drawn ~3x a second on their own
// ---- Bookmap heat map: poll the bridge's data, keep 30 minutes of columns, draw them on the canvas (the canvas is re-made every page refresh, so we re-draw)
const bmData = {};
const bmStatus = {};
function bmColor(v) {
  const s = [[0, [8, 16, 48]], [0.25, [16, 70, 150]], [0.5, [0, 170, 190]], [0.75, [240, 210, 40]], [1, [250, 60, 30]]];
  v = Math.max(0, Math.min(1, v));
  for (let i = 1; i < s.length; i++) {
    if (v <= s[i][0]) {
      const f = (v - s[i - 1][0]) / (s[i][0] - s[i - 1][0]), a = s[i - 1][1], b = s[i][1];
      return 'rgb(' + Math.round(a[0] + (b[0] - a[0]) * f) + ',' + Math.round(a[1] + (b[1] - a[1]) * f) + ',' + Math.round(a[2] + (b[2] - a[2]) * f) + ')';
    }
  }
  return 'rgb(250,60,30)';
}
// colour choice for the heat map: one heat scale for everything, or buys (resting below the price) and sells (resting above it) in two different colours
const BM_SCHEMES = {heat: 'One heat colour', gr: 'Buys green / sells red', bo: 'Buys blue / sells orange'};
const BM_RAMPS = {
  green: [[0, [8, 22, 16]], [0.5, [20, 110, 50]], [1, [80, 255, 120]]],
  red: [[0, [28, 10, 12]], [0.5, [140, 30, 30]], [1, [255, 80, 70]]],
  blue: [[0, [8, 14, 32]], [0.5, [20, 70, 160]], [1, [100, 175, 255]]],
  orange: [[0, [28, 16, 8]], [0.5, [150, 90, 20]], [1, [255, 175, 40]]]
};
const BM_SENS = {big: ['Only the biggest walls', [0.85, 0.96, 0.995]], norm: ['Balanced', [0.70, 0.92, 0.985]], more: ['Show more', [0.40, 0.80, 0.95]]};
let bmSens = 'norm';
try { bmSens = localStorage.getItem('bmSens') || 'norm'; } catch (e) {}
if (!BM_SENS[bmSens]) bmSens = 'norm';
function setBmSens(k) {
  if (!BM_SENS[k]) return;
  bmSens = k;
  try { localStorage.setItem('bmSens', k); } catch (e) {}
  drawAllBm();
}
function bmMoney(u) { return u >= 1e6 ? '$' + (u / 1e6).toFixed(1) + 'M' : '$' + Math.round(u / 1e3) + 'K'; }
let bmScheme = 'gr';
try { bmScheme = localStorage.getItem('bmScheme') || 'gr'; } catch (e) {}
if (!BM_SCHEMES[bmScheme]) bmScheme = 'heat';
function bmRamp(name, v) {
  const s = BM_RAMPS[name];
  v = Math.max(0, Math.min(1, v));
  for (let i = 1; i < s.length; i++) {
    if (v <= s[i][0]) {
      const f = (v - s[i - 1][0]) / (s[i][0] - s[i - 1][0]), a = s[i - 1][1], b = s[i][1];
      return 'rgb(' + Math.round(a[0] + (b[0] - a[0]) * f) + ',' + Math.round(a[1] + (b[1] - a[1]) * f) + ',' + Math.round(a[2] + (b[2] - a[2]) * f) + ')';
    }
  }
  return 'rgb(255,255,255)';
}
function setBmScheme(k) {
  if (!BM_SCHEMES[k]) return;
  bmScheme = k;
  try { localStorage.setItem('bmScheme', k); } catch (e) {}
  drawAllBm();
}
function drawBm(a) {
  document.querySelectorAll('.bm-scheme[data-asset="' + a + '"] button').forEach(b =>
    b.className = (b.getAttribute('data-k') === bmScheme) ? 'size-mode-btn size-mode-active' : 'size-mode-btn');
  document.querySelectorAll('.bm-sens[data-asset="' + a + '"] button').forEach(b =>
    b.className = (b.getAttribute('data-k') === bmSens) ? 'size-mode-btn size-mode-active' : 'size-mode-btn');
  const st = document.querySelector('.bm-status[data-asset="' + a + '"]');
  if (st && bmStatus[a]) { st.textContent = bmStatus[a].text; st.style.color = bmStatus[a].connected ? '#3fb950' : '#d29922'; }
  const cv = document.querySelector('.bm-canvas[data-asset="' + a + '"]');
  const box = cv && cv.closest('details');
  if (!cv || !box || !box.open) return;                                     // only draw while the pull-down is open
  const cw = Math.round(cv.clientWidth), ch = Math.round(cv.clientHeight);          // draw at the size it is shown (same height as the candle chart)
  if (cw > 50 && ch > 50 && (cv.width !== cw || cv.height !== ch)) { cv.width = cw; cv.height = ch; }
  const ctx = cv.getContext('2d'), W = cv.width, H = cv.height;
  ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, W, H);
  const d = bmData[a];
  if (!d || !d.cols.length) { ctx.fillStyle = '#8b949e'; ctx.font = '16px sans-serif'; ctx.fillText('no Bookmap data yet', 20, 34); return; }
  const now = Date.now() / 1000, span = 30 * 60, t0 = now - span;
  const mid = d.cols[d.cols.length - 1].mid, lo = mid * 0.996, hi = mid * 1.004, zone = d.zone || (mid * 0.00012);
  const y = p => H - (p - lo) / (hi - lo) * H, x = t => (t - t0) / span * W, zh = Math.max(2, zone / (hi - lo) * H);
  const vals = [];
  d.cols.forEach(c => c.z.forEach(q => vals.push(q[1])));
  vals.sort((p, q) => p - q);
  const pq = f => vals[Math.min(vals.length - 1, Math.floor(vals.length * f))] || 1;
  const qs = BM_SENS[bmSens][1], t1 = pq(qs[0]), t2 = pq(qs[1]), t3 = pq(qs[2]);
  // brightness by RANK, not by raw size: everything under the first cut-off is left dark, so only the real walls stand out
  const level = u => (u < t1 ? -1 : (u < t2 ? 0.14 + 0.30 * (u - t1) / Math.max(1, t2 - t1) : (u < t3 ? 0.44 + 0.31 * (u - t2) / Math.max(1, t3 - t2) : 0.75 + 0.25 * Math.min(1, (u - t3) / Math.max(1, t3)))));
  for (let i = 0; i < d.cols.length; i++) {
    const c = d.cols[i], xn = i + 1 < d.cols.length ? x(d.cols[i + 1].t) : x(now);
    const x0 = x(c.t), w = Math.max(1.5, xn - x0 + 0.8);
    c.z.forEach(q => {
      if (q[0] >= lo && q[0] <= hi) {
        const v = level(q[1]);
        if (v < 0) return;
        if (bmScheme === 'heat') ctx.fillStyle = bmColor(v);
        else {
          const buy = q[0] < c.mid;                              // resting below the price = buyers waiting, above = sellers waiting
          ctx.fillStyle = bmRamp(bmScheme === 'gr' ? (buy ? 'green' : 'red') : (buy ? 'blue' : 'orange'), v);
        }
        ctx.fillRect(x0, y(q[0]) - zh / 2, w, zh + 0.6);
      }
    });
  }
  ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 1.6; ctx.beginPath();
  d.cols.forEach((c, i) => { const px = x(c.t), py = y(c.mid); if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py); });
  ctx.stroke();
  ctx.fillStyle = '#c9d1d9'; ctx.font = '14px sans-serif'; ctx.textAlign = 'right';
  [0.003, 0, -0.003].forEach(f => { const p = mid * (1 + f); ctx.fillText(p.toFixed(a === 'XRP' ? 4 : 0), W - 4, y(p) + 4); });
  ctx.textAlign = 'left'; ctx.fillText('30 min ago', 6, H - 6);
  if (bmScheme !== 'heat') {
    ctx.font = 'bold 14px sans-serif';
    ctx.fillStyle = bmScheme === 'gr' ? '#56d364' : '#58a6ff'; ctx.fillText('buyers waiting (below price)', 6, 18);
    ctx.fillStyle = bmScheme === 'gr' ? '#ff7b72' : '#ffa657'; ctx.fillText('sellers waiting (above price)', 6, 36);
  }
}
function drawAllBm() { ['BTC', 'XRP'].forEach(drawBm); }
async function bmPoll() {
  for (const a of ['BTC', 'XRP']) {
    try {
      const cur = bmData[a] || (bmData[a] = {cols: [], since: 0, zone: null});
      const r = await fetch('/bookmap_heat?asset=' + a + '&since=' + cur.since, {cache: 'no-store'});
      const j = await r.json();
      bmStatus[a] = j.status;
      if (j.zone) cur.zone = j.zone;
      (j.cols || []).forEach(c => { if (c.t > cur.since) { cur.cols.push(c); cur.since = c.t; } });
      const cut = Date.now() / 1000 - 31 * 60;
      while (cur.cols.length && cur.cols[0].t < cut) cur.cols.shift();
    } catch (e) { /* next poll */ }
  }
  drawAllBm();
}
setInterval(bmPoll, 3000);
const fastBusy = {};
const fastLive = {};                   // coin -> when the fast channel last drew its chart (the page refresh keeps that chart instead of swapping in its own copy)
async function fastChartTick() {
  if (document.hidden) return;
  document.querySelectorAll('.pat-win[data-asset]').forEach(async el => {
    const a = el.getAttribute('data-asset');
    if (fastBusy[a] || el.offsetParent === null) return;
    fastBusy[a] = true;
    try {
      const r = await fetch('/chart_live?asset=' + encodeURIComponent(a), {cache: 'no-store'});
      const j = await r.json();
      if (j && j.svg) {
        const cur = document.querySelector('.pat-win[data-asset="' + a + '"]');
        const ch = cur && cur.querySelector('.pat-chart');
        if (ch) { ch.innerHTML = j.svg; if (j.tf) ch.setAttribute('data-tf', j.tf); fastLive[a] = Date.now(); }
        if (typeof j.rsi === 'number') setRsiLive(a, j.rsi);
        const vb = cur && cur.querySelector('.vol-bar');
        if (vb && j.vol) {
          vb.outerHTML = j.vol;
          const nv = cur.querySelector('details.vol-help');
          if (nv && openPatterns.has(nv.dataset.pat)) nv.open = true;
        }
      }
    } catch (e) { /* next tick will try again */ }
    finally { fastBusy[a] = false; }
  });
}
setInterval(fastChartTick, 350);
function rsiZoneCls(v) { return (v <= 30 || v >= 70) ? 'rsi-r' : ((v <= 40 || v >= 60) ? 'rsi-o' : 'rsi-g'); }
function rsiNoteTxt(v) { return v >= 70 ? 'OVERBOUGHT - stretched up' : (v <= 30 ? 'OVERSOLD - stretched down' : (v >= 60 ? 'pressure building up' : (v <= 40 ? 'pressure building down' : 'normal'))); }
function setRsiLive(a, v) {
  document.querySelectorAll('.tb-rsi[data-asset="' + a + '"]').forEach(box => {
    const val = box.querySelector('.tb-rsi-v'), n = box.querySelector('.tb-rsi-n'), dot = box.querySelector('.rsi-dot');
    if (!val || !n || !dot) return;
    const cls = rsiZoneCls(v), t = v.toFixed(1);
    if (val.textContent !== t) val.textContent = t;
    val.className = 'tb-rsi-v ' + cls; n.className = 'tb-rsi-n ' + cls;
    const nt = rsiNoteTxt(v); if (n.textContent !== nt) n.textContent = nt;
    dot.style.left = Math.max(0, Math.min(100, v)).toFixed(1) + '%';
  });
}
function tickClocks() {
  const d = new Date();
  const t = d.toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit', second: '2-digit'});
  const ds = d.toLocaleDateString('en-US', {weekday: 'short', month: 'short', day: 'numeric'});
  document.querySelectorAll('.live-clock').forEach(el => {
    const a = el.querySelector('.lc-t'), b = el.querySelector('.lc-d');
    if (a && a.textContent !== t) a.textContent = t;
    if (b && b.textContent !== ds) b.textContent = ds;
  });
}
setInterval(tickClocks, 250);
function toggleStreakPull(name) {
  const d = document.querySelector('details.streak-pull[data-pat="STREAK|' + name + '"]');
  if (d) { d.open = !d.open; syncStreakArrows(); }
}
function syncStreakArrows() {
  document.querySelectorAll('.streak-arrow').forEach(a => {
    const d = document.querySelector('details.streak-pull[data-pat="STREAK|' + a.getAttribute('data-name') + '"]');
    a.innerHTML = (d && d.open) ? '&#9652;' : '&#9662;';
  });
}
function restorePatternTabs() {
  document.querySelectorAll('details[data-pat]').forEach(d => {
    if (d.dataset.pat.indexOf('KEPTLIST|') === 0) { d.open = keptListOpen; return; }
    if (openPatterns.has(d.dataset.pat)) { d.open = true; if (d.classList.contains('pat')) fillPatternInfo(d); }
  });
  syncStreakArrows();
  restoreHist();
}
document.addEventListener('toggle', e => {
  const d = e.target;
  if (!d || !d.dataset || !d.dataset.pat) return;
  if (d.dataset.pat.indexOf('KEPTLIST|') === 0) { keptListOpen = d.open; return; }
  if (d.open) { openPatterns.add(d.dataset.pat); if (d.classList.contains('pat')) fillPatternInfo(d); if (d.hasAttribute('data-hist')) loadHist(d); }
  else openPatterns.delete(d.dataset.pat);
  saveOpenPats();
}, true);
let lastInputActivity = 0;
['focusin', 'input', 'keydown'].forEach(ev =>
  document.addEventListener(ev, () => { lastInputActivity = Date.now(); }));
let pageBuild = null;

function scrollKey(el) {
  if (el.classList.contains('alert-dock')) return 'dock|' + el.getAttribute('data-dock');
  const w = el.closest('.pat-win');
  return w ? 'leg|' + w.getAttribute('data-asset') : null;
}
// the pattern list is exactly as tall as the chart beside it and scrolls inside itself, so opening a pattern no longer pushes the heat map down
function syncLegendHeights() {
  document.querySelectorAll('.pat-win').forEach(w => {
    const lg = w.querySelector('.pat-win-legend'), row = w.querySelector('.pat-chart-row');
    if (!lg || !row || row.offsetParent === null) return;
    if (getComputedStyle(w).display === 'grid') {
      const h = row.offsetHeight + 22;
      if (h > 180) lg.style.maxHeight = h + 'px';
    } else lg.style.maxHeight = '';
  });
}
let refreshBusySince = 0;
async function refresh() {
  if (refreshBusySince && Date.now() - refreshBusySince < 20000) return;       // one refresh at a time; the next tick starts another when this one is done
  refreshBusySince = Date.now();
  try { await refreshInner(); } finally { refreshBusySince = 0; }
}
async function refreshInner() {
  const activeNow = document.activeElement;
  const typingNow = activeNow && activeNow.tagName === 'INPUT' &&
      LIVE_SETTINGS_INPUT_PREFIX.test(activeNow.id || '') &&
      (Date.now() - lastInputActivity) < 15000;
  if (typingNow) {
    return;
  }
  const mySeq = ++refreshSeq;
  const res = await fetch('/data');
  if (res.status === 401) { location.href = '/login'; return; }      // signed out (removed, or password changed)
  const data = await res.json();
  if (mySeq !== refreshSeq) return; // a newer refresh already started - this one is stale, drop it
  // auto-reload when the server was restarted with new code, so an open tab
  // never keeps running old scripts against new HTML (stale-tab bug)
  if (data.build) {
    if (pageBuild === null) pageBuild = data.build;
    else if (data.build !== pageBuild) { location.reload(); return; }
  }
  // preserve whatever the user is actively typing into (e.g. some other
  // input not covered by the early-return above) across this 1s re-render -
  // added 2026-09-22, the page swaps #content's whole innerHTML every
  // second, which was silently wiping out in-progress typing before it
  // could be submitted, making inputs look broken. Only restores when the
  // SAME element id still exists post-render and is still an editable
  // input - never restores into some other field.
  const contentEl = document.getElementById('content');
  const active = document.activeElement;
  let savedId = null, savedValue = null, savedSelStart = null, savedSelEnd = null;
  if (active && active.id && contentEl.contains(active) &&
      (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')) {
    savedId = active.id;
    savedValue = active.value;
    // number inputs don't support setSelectionRange at all (throws) - only
    // bother capturing/restoring cursor position for types that support it
    if (active.type !== 'number') {
      try { savedSelStart = active.selectionStart; savedSelEnd = active.selectionEnd; } catch (e) {}
    }
  }
  const keepChart = {};
  contentEl.querySelectorAll('.pat-win[data-asset]').forEach(w => {
    const a = w.getAttribute('data-asset'), ch = w.querySelector('.pat-chart'), vb = w.querySelector('.vol-bar');
    if (ch && ch.querySelector('svg') && fastLive[a] && Date.now() - fastLive[a] < 3000)
      keepChart[a] = {tf: ch.getAttribute('data-tf'), svg: ch.innerHTML, vol: vb ? vb.outerHTML : null};
  });
  const keepScroll = {};                 // scroll position of the pattern list and the alert box (the swap below would snap them back to the top = the "twitch")
  contentEl.querySelectorAll('.pat-win-legend, .alert-dock[data-dock]').forEach(el => { const k = scrollKey(el); if (k && el.scrollTop) keepScroll[k] = el.scrollTop; });
  contentEl.innerHTML = data.html;
  contentEl.querySelectorAll('.pat-win[data-asset]').forEach(w => {      // no blink: keep the live chart + volume bar across the swap
    const k = keepChart[w.getAttribute('data-asset')];
    if (!k) return;
    const ch = w.querySelector('.pat-chart');
    if (ch && ch.getAttribute('data-tf') === k.tf) {
      ch.innerHTML = k.svg;
      const vb = w.querySelector('.vol-bar');
      if (vb && k.vol) vb.outerHTML = k.vol;
    }
  });
  if (savedId) {
    const restored = document.getElementById(savedId);
    if (restored && (restored.tagName === 'INPUT' || restored.tagName === 'TEXTAREA')) {
      restored.value = savedValue;
      restored.focus();
      if (savedSelStart !== null) {
        try { restored.setSelectionRange(savedSelStart, savedSelEnd); } catch (e) {}
      }
    }
  }
  renderDock();
  reapplySavedChartTimeframes();
  restorePatternTabs();
  drawAllBm();                         // after the pull-downs are restored, so an open heat map is drawn
  applyMarketPick();
  applyFit();
  syncLegendHeights();
  contentEl.querySelectorAll('.pat-win-legend, .alert-dock[data-dock]').forEach(el => { const k = scrollKey(el); if (k && keepScroll[k]) el.scrollTop = keepScroll[k]; });
  syncKalshiArrows();
  renderExpanded();
  checkFlipAlerts();
  checkBuildingAlerts();
  checkRegimeAlerts();
  checkReversalAlerts();
  checkCertainFlipAlerts();
  checkWhaleAlerts();
  checkWallAlerts();
  checkWinAlerts();
  checkLossAlerts();
  checkLockAlerts();
  checkRsiAlerts();
  checkIncomingAlerts();
  checkSweepAlerts();
}
setInterval(refresh, 1000);
window.onload = function() {
  updateAssetToggleButtons(); updateBuildingToggleButton(); updateVoiceToggleButton(); syncSweepButtons(); syncFitButton(); syncSoundButton();
  let wantedSound = false;
  try { wantedSound = localStorage.getItem('soundWanted') === '1'; } catch (e) {}
  if (wantedSound && !soundEnabled) {
    const bar = document.createElement('div');
    bar.id = 'sound-reminder';
    bar.textContent = 'Sound is off after the page reloaded - click anywhere to turn it back on';
    document.body.appendChild(bar);
    const reenable = () => { enableSound(true); bar.remove();
      document.removeEventListener('pointerdown', reenable); document.removeEventListener('keydown', reenable); };
    document.addEventListener('pointerdown', reenable); document.addEventListener('keydown', reenable);
  }
  const slider = document.getElementById('volume-slider');
  if (slider) slider.value = masterVolume;
  const label = document.getElementById('volume-label');
  if (label) label.textContent = Math.round(masterVolume * 100) + '%';
  { let v = 'both'; try { v = localStorage.getItem('assetView') || 'both'; } catch (e) {} setAssetView(v); }
  refresh();
};
</script>
</head><body>
__USERBAR__<h1>The Dominator Live Dashboard - read only, never trades - refreshes every 1s__VERSION_TAG__</h1>
<div class="view-bar"><span class="hint">Show:</span>
  <button class="size-mode-btn" data-view="BTC" onclick="setAssetView('BTC')">BTC only</button>
  <button class="size-mode-btn" data-view="XRP" onclick="setAssetView('XRP')">XRP only</button>
  <button class="size-mode-btn" data-view="both" onclick="setAssetView('both')">Both</button>
  <button class="size-mode-btn" id="fit-btn" onclick="toggleFit()" title="Scales the page so everything from the top to the bottom of the chart fits the window, and keeps it that way after every reload - for streaming when nobody is touching the mouse">Fit to screen: ON</button></div>
<div id="content">Loading...</div>
<div class="sound-bar">
  <button id="sound-enable-btn" class="sound-off" onclick="toggleSound()">SOUND OFF - click here to turn it on</button>
  <details class="sound-details">
    <summary>Sound &amp; alert settings</summary>
    <div class="sound-details-body">
      <span style="display:inline-block; vertical-align:middle; margin-left:6px;">
        <label for="volume-slider" style="color:#8b949e; font-size:14px; margin-right:6px;">Volume</label>
        <input type="range" id="volume-slider" min="0" max="1" step="0.05"
               oninput="setMasterVolume(this.value)" style="vertical-align:middle;">
        <span id="volume-label" style="color:#8b949e; font-size:14px; margin-left:4px;">80%</span>
      </span>
      <button id="toggle-BTC" onclick="toggleAssetSound('BTC')">BTC alerts: ON</button>
      <button id="toggle-XRP" onclick="toggleAssetSound('XRP')">XRP alerts: ON</button>
      <button id="toggle-building" onclick="toggleBuildingAlerts()">Building-alert beeps: OFF</button>
      <button id="toggle-voice" onclick="toggleVoiceAlerts()">Voice alerts: ON</button>
      <br><span class="hint">Liquidity sweep warning (when one starts to form):</span>
      <button id="toggle-sweep" onclick="toggleSweepAlerts()">Sweep warning: ON</button>
      <button id="sweep-tf-1m" onclick="toggleSweepTf('1m')">1m off</button>
      <button id="sweep-tf-5m" onclick="toggleSweepTf('5m')">5m on</button>
      <button id="sweep-tf-15m" onclick="toggleSweepTf('15m')">15m on</button>
      <button id="sweep-tf-1h" onclick="toggleSweepTf('1h')">1h on</button>
      <button onclick="testSweepAlert('BTC','5m','bearish')">Test sweep warning</button>
      <br>
      <button onclick="playAlienSound('BTC')">Test BTC sound</button>
      <button onclick="playAlienSound('ETH')">Test ETH sound</button>
      <button onclick="playAlienSound('XRP')">Test XRP sound</button>
      <button onclick="playAlienSound('NEAR')">Test NEAR sound</button>
      <button onclick="playAlienSound('BNB')">Test BNB sound</button>
      <button onclick="playWhaleSound('buy'); showWhaleCrossing('BTC','buy')">Test whale BUY</button>
      <button onclick="playWhaleSound('sell'); showWhaleCrossing('BTC','sell')">Test whale SELL</button>
      <button onclick="playWallAlert('sell'); showWhaleCrossing('XRP','sell')">Test SELL wall</button>
      <button onclick="playWallAlert('buy'); showWhaleCrossing('XRP','buy')">Test BUY wall</button>
      <button onclick="playReversalAlarm('XRP'); speakRaw('XRP. Swinging down, against your call.')">Test voice alert</button>
      <div class="hint">Browsers block audio until you click something on the page first - click "enable" once per visit. Test buttons work regardless, so you can learn each one ahead of time. Whale sound = a big single trade just hit the tape (rises for a buy, drops for a sell). Voice alerts only actually speak during a real BOOM/whale/wall event - the test button here plays one on demand so you can check it works without waiting for one.</div>
    </div>
  </details>
</div>
__LEGAL__
</body></html>
"""


def fmt_price(x, decimals=2):
    return f'${x:,.{decimals}f}' if x is not None else '--'


from src import multi_exchange_flow as _flow


def _flow_record(venue, product_id, price, size, aggressor):
    """Hand a Coinbase / Kraken trade to the multi-exchange pressure hub (information only; never raises)."""
    try:
        _flow.HUB.record(venue, product_id.split("-")[0], price * size, aggressor, price)
    except Exception:
        pass


def _usd_short(v):
    v = abs(v)
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _pct_cls(p):
    return "up" if p >= 55 else ("down" if p <= 45 else "flat")


def build_pressure_bar(asset_name):
    """Live buy-vs-sell pressure ('tug of war') summed over every exchange we can reach - taker trade flow in dollars
    over 10s / 30s / 1m / 5m, resting bid-vs-ask depth, perp positioning, and a per-exchange pull-down. Information only."""
    if asset_name not in _flow.ASSETS:
        return ""
    s = _flow.HUB.summary(asset_name)
    w = s["wins"]
    if w[60]["pct"] is None and w[10]["pct"] is None:
        return ('<div class="row press-row"><span class="label">Buy / sell pressure - all exchanges</span> '
                '<span class="hint">collecting trades from the exchanges...</span></div>')
    main = w[60]["pct"] if w[60]["pct"] is not None else w[10]["pct"]
    buy = main * 100
    sell = 100 - buy
    verdict = ("BUYERS in control" if buy >= 55 else ("SELLERS in control" if buy <= 45 else "balanced"))
    chips = []
    for sec, lab in ((10, "10s"), (30, "30s"), (60, "1m"), (300, "5m")):
        pc = w[sec]["pct"]
        if pc is None:
            continue
        chips.append(f'<span class="press-chip {_pct_cls(pc * 100)}">{lab} <b>{pc * 100:.0f}%</b> buy</span>')
    net = w[60]["buy"] - w[60]["sell"]
    book = ""
    if s["book_pct"] is not None:
        bp = s["book_pct"] * 100
        book = (f'<span class="press-chip {_pct_cls(bp)}" title="resting orders within 0.25% of the price, all exchanges">'
                f'order book <b>{bp:.0f}%</b> bids ({s["book_n"]} exchanges)</span>')
    d = s.get("deriv") or {}
    dv = []
    if d.get("funding_hl") is not None or d.get("funding_okx") is not None:
        f8 = (d["funding_okx"] if d.get("funding_okx") is not None else d["funding_hl"] * 8) * 100
        dv.append(f'funding {f8:+.4f}%/8h ' + ("(longs pay)" if f8 > 0 else "(shorts pay)"))
    if d.get("oi_usd"):
        oc = d.get("oi_chg_5m_pct")
        dv.append(f'open interest {_usd_short(d["oi_usd"])}' + (f' ({oc:+.2f}% in 5m)' if oc is not None else ""))
    if d.get("premium_pct") is not None:
        dv.append(f'perp premium {d["premium_pct"]:+.3f}%')
    ll, ls = d.get("liq_long_usd_5m", 0) or 0, d.get("liq_short_usd_5m", 0) or 0
    if ll or ls:
        dv.append(f'liquidated 5m: longs {_usd_short(ll)} / shorts {_usd_short(ls)}')
    deriv_html = f'<div class="hint">{" &nbsp;|&nbsp; ".join(dv)}</div>' if dv else ""
    rows = []
    for v in s["venues"]:
        if v["pct"] is None and v["book_pct"] is None:
            continue
        bpc = "" if v["pct"] is None else f'{v["pct"] * 100:.0f}%'
        w_ = 0 if v["pct"] is None else v["pct"] * 100
        bk = "" if v["book_pct"] is None else f'{v["book_pct"] * 100:.0f}%'
        rows.append(
            f'<tr class="{"" if v["live"] else "press-idle"}"><td>{v["label"]}</td>'
            f'<td><div class="press-mini"><i style="width:{w_:.0f}%"></i></div></td>'
            f'<td>{bpc}</td><td>{_usd_short(v["buy"] + v["sell"]) if (v["buy"] + v["sell"]) else "-"}</td>'
            f'<td>{v["n"] or "-"}</td><td>{bk}</td></tr>')
    table = ('<table class="press-table"><tr><th>Exchange</th><th>buy vs sell (1m)</th><th>buy %</th><th>$ traded 1m</th>'
             '<th>trades</th><th>book bids %</th></tr>' + "".join(rows) + '</table>')
    return (
        f'<div class="row press-row" data-asset-press="{asset_name}">'
        f'<div class="press-head"><b>Buy / sell pressure</b> <span class="hint">{s["live_n"]} exchanges live &middot; last minute '
        f'&middot; net {("+" if net >= 0 else "-")}{_usd_short(net)}</span> <span class="press-verdict {_pct_cls(buy)}">{verdict}</span></div>'
        f'<div class="press-bar"><div class="press-buy" style="width:{buy:.1f}%">{buy:.0f}% buy</div>'
        f'<div class="press-sell" style="width:{sell:.1f}%">{sell:.0f}% sell</div><span class="press-mid"></span></div>'
        f'<div class="press-chips">{"".join(chips)}{book}</div>{deriv_html}'
        f'<details class="press-more" data-pat="PRESS|{asset_name}"><summary class="hint">by exchange (information only - never used for buying)</summary>'
        f'{table}</details></div>')


from src.mtf_structure import MTFMonitor, TFS as _MTF_TFS
from src.pattern_info import INFO as _PAT_INFO, DISCLAIMER as _PAT_DISCLAIMER, explain as _pat_explain
from src.pattern_diagrams import svg_for as _pat_svg

_mtf = None   # MTFMonitor, started in main(); information only - never used for buying
_MTF_NAMES = {"1m": "1 min", "5m": "5 min", "15m": "15 min", "1h": "1 hour", "3h": "3 hours", "6h": "6 hours",
              "12h": "12 hours", "1d": "1 day", "1w": "1 week", "1M": "1 month", "1Y": "1 year", "overall": "Overall"}
_BIAS_UI = {"bullish": ("up", "&#9650; Bullish"), "bearish": ("down", "&#9660; Bearish"), "neutral": ("flat", "&#9679; Neutral")}


def pattern_lib_json():
    """Explanations + pictures for every pattern, sent to the page ONCE; the pull-down tabs fill from it."""
    lib = {}
    for k, (what, means, watch) in _PAT_INFO.items():
        lib[k] = {"what": what, "means": means, "watch": watch, "svg": _pat_svg(k) or ""}
    return json.dumps({"lib": lib, "disclaimer": _PAT_DISCLAIMER})


def _pat_rate_html(stats, bias="bullish"):
    if bias == "neutral":
        return '<span class="pat-rate flat">context only - no up/down call to score</span>'
    if not stats or not stats.get("decisive"):
        return '<span class="pat-rate flat">no track record yet</span>'
    r = stats["rate"]
    css = "up" if (r >= 0.55 and stats["decisive"] >= 30) else ("down" if (r <= 0.45 and stats["decisive"] >= 30) else "flat")
    live = f", {stats['live_n']} live" if stats.get("live_n") else ""
    return (f'<span class="pat-rate {css}">worked {r:.0%} of the time '
            f'({stats["decisive"]:,} scored calls{live})</span>')


_pat_window_tf = {}   # asset name -> timeframe shown in the live pattern window (default 1m)
HORIZONS_MIN = {tf: max(5, m) for tf, m in {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "3h": 180, "6h": 360, "12h": 720, "1d": 1440, "1w": 10080, "1M": 43200, "1Y": 525600}.items()}
_PAT_WINDOW_TFS = ("1m", "5m", "15m", "1h", "3h", "6h", "12h", "1d", "1M")
_view_tf = threading.local()       # this request's viewer: {asset: timeframe} read from their own cookies, so nobody's choice ever changes anybody else's chart


def _get_pat_tf(asset_name):
    return (getattr(_view_tf, "m", None) or {}).get(asset_name) or _pat_window_tf.get(asset_name, "1m")


_kflow = {}     # asset name -> {"ticker","ts","yes","no","n"}: Kalshi taker dollars on YES vs NO so far this round


def _kalshi_round_flow(asset_name, ticker, open_ts):
    """Dollars Kalshi traders have taken on YES versus NO since this round opened (public trade tape; refreshed at most every 6s)."""
    c = _kflow.get(asset_name)
    now = time.time()
    if c and c["ticker"] == ticker and now - c["ts"] < 6:
        return
    keep = c if (c and c["ticker"] == ticker) else {"yes": 0.0, "no": 0.0, "n": 0}
    try:
        yes = no = 0.0
        n = 0
        cursor = None
        for _ in range(3):
            params = {"ticker": ticker, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            r = _rq.get("https://external-api.kalshi.com/trade-api/v2/markets/trades", params=params, timeout=10).json()
            trades = r.get("trades", [])
            if not trades:
                break
            old = False
            for t in trades:
                ts = dt.datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp()
                if ts < open_ts:
                    old = True
                    continue
                cnt = float(t.get("count_fp", 0) or 0)
                p = float(t["yes_price_dollars"])
                if t.get("taker_side") == "yes":
                    yes += cnt * p
                elif t.get("taker_side") == "no":
                    no += cnt * (1 - p)
                n += 1
            cursor = r.get("cursor")
            if old or not cursor:
                break
        _kflow[asset_name] = {"ticker": ticker, "ts": now, "yes": yes, "no": no, "n": n}
    except Exception:
        _kflow[asset_name] = {"ticker": ticker, "ts": now, "yes": keep["yes"], "no": keep["no"], "n": keep["n"]}


def tug_numbers(name, d):
    """The numbers behind the price-vs-the-line panel (also what gets logged every 30s): the four feeds, the fused lean, flow vs price."""
    if name not in _flow.ASSETS:
        return None
    spot, line = d.get("spot_price"), d.get("target_price")
    left = d.get("seconds_left")
    if spot is None or not line or left is None:
        return None
    dec = 4 if name == "XRP" else 2
    diff = spot - line
    up_side = diff >= 0
    elapsed = max(5, 900 - int(left))
    bu, se, ntr = _flow.HUB.window_totals(name, elapsed)
    feeds = []                                                     # (label, up fraction 0..1 or None, note)
    fair = d.get("fair_yes")
    feeds.append(("Price vs the line", fair, None))
    ex_up = bu / (bu + se) if (bu + se) > 0 else None
    sb, ss_, _n1 = _flow.HUB.window_totals(name, elapsed, "spot")
    pb, ps_, _n2 = _flow.HUB.window_totals(name, elapsed, "perp")
    spot_up = sb / (sb + ss_) if (sb + ss_) > 0 else None
    perp_up = pb / (pb + ps_) if (pb + ps_) > 0 else None
    feeds.append(("Exchange buyers vs sellers", ex_up, None))
    kf = _kflow.get(name)
    k_up = None
    if kf and kf["ticker"] == d.get("ticker") and (kf["yes"] + kf["no"]) > 0:
        k_up = kf["yes"] / (kf["yes"] + kf["no"])
    feeds.append(("Kalshi money", k_up, None))
    lq = (_flow.HUB.summary(name).get("deriv") or {})
    ls, ll = lq.get("liq_short_usd_5m", 0) or 0, lq.get("liq_long_usd_5m", 0) or 0
    liq_min = 200_000 if name == "BTC" else 20_000
    liq_up = (ls / (ls + ll)) if (ls + ll) >= liq_min else None
    feeds.append(("Liquidations", liq_up, "calm - no cascade" if liq_up is None else None))
    used = [f[1] for f in feeds if f[1] is not None]
    flow_used = [x for x in (ex_up, k_up, liq_up) if x is not None]
    flow_up = (sum(flow_used) / len(flow_used)) if flow_used else None
    # price vs the line counts for HALF of the fused lean (back-test of ~420 past rounds per coin, 6 min left: price vs the line alone called the
    # winner ~80%, all-exchange flow alone ~68%; when they disagreed the price side won ~3 rounds in 4) - the flow feeds share the other half
    if fair is not None and flow_up is not None:
        fused = 0.5 * fair + 0.5 * flow_up
    else:
        fused = (sum(used) / len(used)) if used else None

    return {"spot": spot, "line": line, "left": left, "dec": dec, "diff": diff, "up_side": up_side, "elapsed": elapsed, "bu": bu, "se": se,
            "feeds": feeds, "used": used, "fused": fused, "flow_up": flow_up, "ex_up": ex_up, "k_up": k_up, "liq_up": liq_up, "fair": fair,
            "spot_up": spot_up, "perp_up": perp_up, "spot_usd": sb + ss_, "perp_usd": pb + ps_}


_TUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot_state", "tug_log.csv")
_tug_last_log = {}


def _tug_log(name, d):
    """Every ~30s per coin: price vs the line and each pressure feed, so we can later check whether the fused lean predicts the round."""
    now = time.time()
    if now - _tug_last_log.get(name, 0) < 30:
        return
    _tug_last_log[name] = now
    tn = tug_numbers(name, d)
    if tn is None:
        return
    f = lambda x: "" if x is None else f"{x:.4f}"
    try:
        new = not os.path.exists(_TUG_LOG)
        with open(_TUG_LOG, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["ts_utc", "asset", "ticker", "seconds_left", "spot", "line", "diff", "price_vs_line_up", "exchange_up", "kalshi_money_up",
                            "liquidation_up", "fused_up", "flow_up", "kalshi_yes"])
            ky = None
            if d.get("confidence") is not None and d.get("leaning") in ("YES", "NO"):
                ky = d["confidence"] if d["leaning"] == "YES" else 1 - d["confidence"]
            w.writerow([dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), name, d.get("ticker"), int(tn["left"]), tn["spot"], tn["line"],
                        f"{tn['diff']:.6g}", f(tn["fair"]), f(tn["ex_up"]), f(tn["k_up"]), f(tn["liq_up"]), f(tn["fused"]), f(tn["flow_up"]), f(ky)])
    except Exception:
        pass


_PVL = {"data": None}
_PVL_TIMES = [(840, "14 min"), (600, "10 min"), (420, "7 min"), (300, "5 min"), (180, "3 min"), (120, "2 min"), (60, "1 min")]
_PVL_BUCKETS = [(50, 60), (60, 70), (70, 80), (80, 90), (90, 95), (95, 100.01)]


def _pvl_accuracy(name, lean_pct, left):
    """How often the side 'price vs the line' favoured really won, from the backtest of ~1,750 past rounds per coin (data_cache/pvl_backtest.json),
    for a lean like this one with about this much time left. -> (win %, rounds, time label) or None when there is too little data."""
    try:
        if _PVL["data"] is None:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache", "pvl_backtest.json"), encoding="utf-8") as f:
                _PVL["data"] = json.load(f)
        g, lab = min(_PVL_TIMES, key=lambda x: abs(x[0] - max(0, left)))
        for lo, hi in _PVL_BUCKETS:
            if lo <= lean_pct < hi:
                pct, n = _PVL["data"][name][f"{lo}-{min(hi, 100):.0f}|{lab}"]
                return (pct, n, lab) if pct is not None and n >= 15 else None
    except Exception:
        return None
    return None


END_CALL_LOCK_LEFT = 420          # the end call locks when the round reaches the 8th minute (7:00 left) and never changes after that
END_CALL_LATE_GRACE = 45          # if the first look at a round is more than this many seconds past the 8th-minute mark (dashboard just started), there is no lock this round
END_CALL_MIN_LEAN = 60.0          # a lean under this at the lock is a coin flip (backtest: right only 50-56% of the time) -> locked as NO CALL
_END_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache", "end_call_locks.json")
_END_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot_state", "end_call_log.csv")
_end_locks = {}                   # "COIN|ticker" -> the locked call (survives a dashboard restart)
_end_lock_mu = threading.Lock()


def _load_end_locks():
    global _end_locks
    try:
        with open(_END_LOCK_FILE, "r", encoding="utf-8") as f:
            _end_locks = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        _end_locks = {}


def _save_end_locks():
    try:
        os.makedirs(os.path.dirname(_END_LOCK_FILE), exist_ok=True)
        keys = list(_end_locks)
        for k in keys[:-400]:
            _end_locks.pop(k, None)
        tmp = f"{_END_LOCK_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_end_locks, f)
        os.replace(tmp, _END_LOCK_FILE)
    except Exception:
        pass


def end_call_lock(name, ticker, fair, left, spot, line):
    """The locked end call for this round, or None while it is still before the 8th minute. It locks ONCE (first look at/after 7:00 left) and is then
    returned unchanged for the rest of the round, even across a restart. Every lock is logged to bot_state/end_call_log.csv (kept forever)."""
    key = f"{name}|{ticker}"
    with _end_lock_mu:
        lk = _end_locks.get(key)
        if lk:
            return lk
        if fair is None or left is None or left > END_CALL_LOCK_LEFT or left <= 0:
            return None
        if _mirror_active():
            return None                                       # mirror mode: the shared END CALL is made elsewhere; it arrives within a few seconds
        lean = max(fair, 1 - fair) * 100
        side = ("YES" if fair >= 0.5 else "NO") if lean >= END_CALL_MIN_LEAN else "NONE"
        if left < END_CALL_LOCK_LEFT - END_CALL_LATE_GRACE:
            side = "LATE"
        lk = {"side": side, "lean": round(lean, 1), "left": int(left), "lean_side": "YES" if fair >= 0.5 else "NO",
              "ts": time.time(), "spot": spot, "line": line}
        _end_locks[key] = lk
        _save_end_locks()
        try:
            os.makedirs(os.path.dirname(_END_LOG), exist_ok=True)
            new = not os.path.exists(_END_LOG)
            with open(_END_LOG, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["ts_utc", "asset", "ticker", "locked_side", "lean_pct", "seconds_left_at_lock", "spot", "line"])
                w.writerow([dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), name, ticker, lk["side"], lk["lean"], lk["left"], spot, line])
        except OSError:
            pass
        return lk


_END_HIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache", "end_call_history.json")
_end_hist = {}                    # "BTC"/"XRP" -> [{"ticker","call","actual_side","correct","lean"}...] oldest first; correct is None for a NO CALL round
_end_hist_mu = threading.Lock()
_end_log_locks = {}               # locks read back from bot_state/end_call_log.csv at start (so the new streak starts from the very first locked call)


def _load_end_hist():
    global _end_hist
    try:
        with open(_END_HIST_FILE, "r", encoding="utf-8") as f:
            _end_hist = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        _end_hist = {}
    try:
        with open(_END_LOG, "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    if float(r["seconds_left_at_lock"]) < END_CALL_LOCK_LEFT - END_CALL_LATE_GRACE:
                        continue                          # a restart-late stamp, not a real 8th-minute lock
                    _end_log_locks[f'{r["asset"]}|{r["ticker"]}'] = {"side": r["locked_side"], "lean": float(r["lean_pct"])}
                except (KeyError, ValueError):
                    continue
    except (FileNotFoundError, OSError):
        pass


def _save_end_hist():
    try:
        os.makedirs(os.path.dirname(_END_HIST_FILE), exist_ok=True)
        tmp = f"{_END_HIST_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_end_hist, f)
        os.replace(tmp, _END_HIST_FILE)
    except Exception:
        pass


def _end_hist_pass():
    """Score every locked end call whose round has finished, once Kalshi has finalized the real result. Never guesses from price."""
    if _mirror_active():
        return                                                # mirror mode: the shared record book comes from the website; scoring our own copy here made the streak flip back and forth
    now = dt.datetime.now(dt.timezone.utc)
    with _end_lock_mu:
        pending = dict(_end_log_locks)
        pending.update({k: v for k, v in _end_locks.items()})
    with _end_hist_mu:
        known = {(a, h.get("ticker")) for a, lst in _end_hist.items() for h in lst}
    todo = []
    for key, lk in pending.items():
        asset, _, ticker = key.partition("|")
        if asset not in ("BTC", "XRP") or (asset, ticker) in known or lk.get("side") == "LATE":
            continue
        t = _window_close_utc(ticker)
        if t is None or t + dt.timedelta(seconds=20) > now:
            continue
        todo.append((t, asset, ticker, lk))
    todo.sort()
    changed = False
    for t, asset, ticker, lk in todo[:80]:                  # a whole backlog at once, so the streak never shows a half-filled history
        try:
            snap = get_market_snapshot(ticker)
            res = (snap.get("result") or "").upper()
            if snap.get("status") != "finalized" or res not in ("YES", "NO"):
                continue
        except Exception:
            continue
        call = lk["side"] if lk.get("side") in ("YES", "NO") else None
        with _end_hist_mu:
            lst = _end_hist.setdefault(asset, [])
            lst.append({"ticker": ticker, "call": call, "actual_side": res, "correct": (call == res) if call else None, "lean": lk.get("lean")})
            lst.sort(key=lambda h: _window_close_utc(h["ticker"]) or now)
            del lst[:-3000]
        changed = True
    if changed:
        with _end_hist_mu:
            _save_end_hist()


def _end_hist_loop():
    while True:
        try:
            _end_hist_pass()
        except Exception as e:
            print(f"[end-call history] pass failed: {e!r}")
        time.sleep(20)


END_FLIP_MIN_LEAN = 80.0          # a live read this strong, on the other side of the lock-time lean, is shown as a LEGIT FLIP (backtest: 80%+ reads are right ~90% of the time)
_END_FLIP_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot_state", "end_call_flips.csv")


def end_call_flip(name, ticker, fair, left):
    """Information only - the locked call is NEVER changed. Once the round has locked, if the live read firms up to END_FLIP_MIN_LEAN or more on the OTHER side of
    the lock-time lean it is latched as a LEGIT FLIP for the rest of the round (so it cannot flicker). On a NO CALL round a strong read on the same side as the
    weak lean is latched as 'FIRMED UP'. Logged to bot_state/end_call_flips.csv."""
    key = f"{name}|{ticker}"
    with _end_lock_mu:
        lk = _end_locks.get(key)
        if not lk or lk.get("side") == "LATE":
            return None
        if lk.get("flip"):
            return lk["flip"]
        if fair is None or left is None or left < 20 or left > END_CALL_LOCK_LEFT:
            return None
        lean = max(fair, 1 - fair) * 100
        if lean < END_FLIP_MIN_LEAN:
            return None
        now_side = "YES" if fair >= 0.5 else "NO"
        base = lk["side"] if lk.get("side") in ("YES", "NO") else (lk.get("lean_side") or ("YES" if (lk.get("spot") or 0) >= (lk.get("line") or 0) else "NO"))
        if lk.get("side") == "NONE":
            kind = "late"                  # no call was made at the 8th minute, so there is nothing to flip - a strong read that forms later is a LATE CALL
        elif now_side != base:
            kind = "flip"
        else:
            return None
        lk["flip"] = {"kind": kind, "side": now_side, "lean": round(lean, 1), "left": int(left), "ts": time.time()}
        _save_end_locks()
        try:
            os.makedirs(os.path.dirname(_END_FLIP_LOG), exist_ok=True)
            new = not os.path.exists(_END_FLIP_LOG)
            with open(_END_FLIP_LOG, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["ts_utc", "asset", "ticker", "kind", "locked_side", "lock_lean_side", "now_side", "now_lean_pct", "seconds_left"])
                w.writerow([dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), name, ticker, kind, lk.get("side"), base, now_side, lk["flip"]["lean"], int(left)])
        except OSError:
            pass
        return lk["flip"]


def _flip_html(name, fl, none=False):
    """(badge, note) for the end-call box, or ("", "") when there is no latched flip."""
    if not fl:
        return "", ""
    yes = fl["side"] == "YES"
    fm, fs = divmod(int(fl["left"]), 60)
    acc = _pvl_accuracy(name, fl["lean"], fl["left"])
    acc_txt = f' A read this strong with this long left has been right <b>{acc[0]:.0f}%</b> of the time ({acc[1]} past rounds).' if acc else ""
    late = none or fl["kind"] == "late"
    word = "LATE CALL" if late else "LEGIT FLIP"
    badge = f'<span class="tug-flip tug-flip-{"up" if yes else "down"}">{word} &rarr; {"YES / UP" if yes else "NO / DOWN"} {fl["lean"]:.0f}%</span>'
    if late:
        note = (f'<div class="tug-flip-note">No call was made at the 8th minute (the read was too weak), so this is not a flip. A strong read formed later, at {fm}:{fs:02d} left.'
                f'{acc_txt} Information only - it is not a locked call and does not count in the streak.</div>')
    else:
        note = f'<div class="tug-flip-note">The live read flipped to the other side at {fm}:{fs:02d} left.{acc_txt} Information only - the locked call above does not change.</div>'
    return badge, note


def _leans_html(name, fair, left):
    """NO CALL round: which way the live read leans and how much that is worth. Shown live (it can move) and never counted as a call or in the streak."""
    if fair is None:
        return "", ""
    yes = fair >= 0.5
    lean = max(fair, 1 - fair) * 100
    badge = f'<span class="tug-flip tug-flip-{"up" if yes else "down"}">LEANS TOWARD {"YES / UP" if yes else "NO / DOWN"} {lean:.0f}%</span>'
    if lean < 70:
        txt = "A shot in the dark - the round could go either way."
    elif lean < 80:
        txt = "A lean, but not strong enough to call."
    else:
        acc = _pvl_accuracy(name, lean, left) if left else None
        txt = ("A strong read has formed since the 8th minute" + (f" - a read this strong has been right {acc[0]:.0f}% of the time ({acc[1]} past rounds)" if acc else "") + ".")
    note = f'<div class="tug-flip-note">Leaning right now: {txt} Information only - not a call, and it does not count in the streak.</div>'
    return badge, note


def _merge_end_hist(cur, inc):
    """Union of two END CALL record books by (coin, round); the incoming version of a round wins; oldest first."""
    out = {}
    for a in set(cur) | set(inc):
        rows = {h.get("ticker"): h for h in cur.get(a, []) if isinstance(h, dict)}
        for h in inc.get(a, []):
            if isinstance(h, dict) and h.get("ticker"):
                rows[h["ticker"]] = h
        out[a] = sorted(rows.values(), key=lambda h: (_window_close_utc(h.get("ticker")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)))[-3000:]
    return out


def _apply_sync(pl, mode="replace"):
    """Take another copy's END CALL record book, recent locks and results.
    mode "replace": mirror it exactly (a follower copy).  "merge": union, incoming wins (the website taking the owner's push).  "fill": union, but only ADD what is missing,
    never change what we already have (the owner's PC taking back the rounds the website scored while the PC was off)."""
    eh = pl.get("end_hist")
    if isinstance(eh, dict):
        eh = {k: v for k, v in eh.items() if isinstance(v, list)}
        with _end_hist_mu:
            if mode == "replace":
                _end_hist.clear()
                _end_hist.update(eh)
            elif mode == "merge":
                merged = _merge_end_hist(_end_hist, eh)
                _end_hist.clear()
                _end_hist.update(merged)
            else:
                merged = _merge_end_hist(eh, _end_hist)          # our own records win
                _end_hist.clear()
                _end_hist.update(merged)
            _save_end_hist()
    lks = pl.get("end_locks")
    if isinstance(lks, dict) and lks:
        with _end_lock_mu:
            for k, v in lks.items():
                if isinstance(v, dict) and v.get("side") in ("YES", "NO", "NONE", "LATE") and (mode != "fill" or k not in _end_locks):
                    _end_locks[k] = v
            _save_end_locks()
    rr = pl.get("round_results")
    if isinstance(rr, dict):
        _lc_results.update({k: v for k, v in rr.items() if v in ("YES", "NO")})
    hist = pl.get("history")
    if isinstance(hist, dict) and hist and mode != "fill":
        with _lock:
            _history.clear()
            _history.update({k: v for k, v in hist.items() if isinstance(v, list)})
        _save_history()
    if mode != "fill":
        _sync_last[0] = time.time()


_SYNC_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache", "sync_config.json")


def _sync_loop():
    """The owner's PC only (and only if data_cache/sync_config.json exists): every 45 s send the END CALL record book + recent locks to the website copy, and the old history
    every ~15 minutes, so the website shows the same streak, records and calls. Never raises."""
    time.sleep(40)
    n = 0
    last_ok = None
    while True:
        try:
            with open(_SYNC_CONFIG, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            url = cfg["url"].rstrip("/") + "/sync_state"
            import base64 as _b64
            auth = "Basic " + _b64.b64encode(("sync:" + cfg["password"]).encode("utf-8")).decode()
            with _end_hist_mu:
                eh = {k: list(v) for k, v in _end_hist.items()}
            with _end_lock_mu:
                locks = dict(list(_end_locks.items())[-60:])
            pl = {"end_hist": eh, "end_locks": locks, "round_results": dict(list(_lc_results.items())[-800:]), "ts": time.time()}
            if n % 20 == 0:
                pl["history"] = {k: list(v) for k, v in _history.items()}
            body = json.dumps(pl).encode()
            req = urllib.request.Request(url, data=body, method="POST", headers={"Authorization": auth, "Content-Type": "application/json", "User-Agent": "kalshi-sync"})
            with urllib.request.urlopen(req, timeout=40) as r:
                ok = r.status == 200
            try:
                greq = urllib.request.Request(cfg["url"].rstrip("/") + "/sync_state", headers={"Authorization": auth, "User-Agent": "kalshi-sync"})
                with urllib.request.urlopen(greq, timeout=30) as gr:
                    _apply_sync(json.loads(gr.read().decode("utf-8")), mode="fill")           # rounds only the website has (PC was off) are added here; ours are never changed
            except Exception:
                pass
            if ok != last_ok:
                print(f"[sync] website update {'working' if ok else 'FAILED'} ({url})")
                last_ok = ok
            n += 1
        except FileNotFoundError:
            pass                                                   # no sync_config.json = syncing is off
        except Exception as e:                                     # noqa: BLE001
            if last_ok is not False:
                print(f"[sync] could not reach the website copy: {str(e)[:100]}")
            last_ok = False
        time.sleep(45)


def _follow_loop():
    """A friend's copy with data_cache/follow_config.json: every 20 s fetch the shared END CALLs + record book from the website copy, so everyone has the same calls and
    the same streak. Never raises; with no config file it does nothing."""
    time.sleep(35)
    last_ok = None
    while True:
        try:
            with open(_FOLLOW_CONFIG, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            import base64 as _b64
            auth = "Basic " + _b64.b64encode(((cfg.get("user") or "sync") + ":" + cfg["password"]).encode("utf-8")).decode()
            req = urllib.request.Request(cfg["url"].rstrip("/") + "/sync_state", headers={"Authorization": auth, "User-Agent": "kalshi-follow"})
            with urllib.request.urlopen(req, timeout=30) as r:
                _apply_sync(json.loads(r.read().decode("utf-8")), mode="replace")
            if last_ok is not True:
                print("[follow] connected to the shared website - using its END CALLs and streak")
                last_ok = True
        except FileNotFoundError:
            pass
        except Exception as e:                                     # noqa: BLE001
            if last_ok is not False:
                print(f"[follow] could not reach the shared website: {str(e)[:100]} (using this copy's own calls meanwhile)")
            last_ok = False
        time.sleep(20)


def _end_call_html(name, d, tn, fair, left, spot, line):
    ticker = d.get("ticker")
    if fair is None or not ticker:
        return ""
    lk = end_call_lock(name, ticker, fair, left, spot, line)
    if lk is None and _mirror_active() and left is not None and left <= END_CALL_LOCK_LEFT:
        return ('<div class="tug-end tug-end-wait"><span class="tug-end-l">END CALL</span> <span class="tug-wait-badge">WAITING FOR THE MAIN BOT\'S LOCK</span> '
                '<span class="tug-wait-note">every screen shows the same END CALL as the main bot - it appears in a few seconds</span></div>')
    if lk is None:
        yes = fair >= 0.5
        lean = max(fair, 1 - fair) * 100
        mm_, ss_ = divmod(max(0, int(left) - END_CALL_LOCK_LEFT), 60)
        return (f'<div class="tug-end tug-end-wait"><span class="tug-end-l">END CALL</span> '
                f'<span class="tug-wait-badge">NOT LOCKED YET - locks in {mm_}:{ss_:02d}</span> '
                f'<span class="tug-wait-lean {"up" if yes else "down"}">Leaning {"YES / UP" if yes else "NO / DOWN"} {lean:.0f}%</span> '
                f'<span class="tug-wait-note">right now - this can still change until it locks at the 8th minute</span></div>')
    lm, ls = divmod(int(lk["left"]), 60)
    if lk["side"] == "LATE":
        return ('<div class="tug-end tug-end-none"><span class="tug-end-l">END CALL</span> <b>no locked call this round</b> '
                '<span class="hint">- the dashboard was started after the 8th minute, so it could not lock in time. The next round will lock normally.</span></div>')
    _fl = end_call_flip(name, ticker, fair, left)             # still logged (bot_state/end_call_flips.csv) even where it is not shown
    if lk["side"] == "NONE":
        _fb, _fn = _leans_html(name, fair, left)              # NO CALL round: just say which way it leans right now and how little that means
    else:
        _fb, _fn = _flip_html(name, _fl)
    if lk["side"] == "NONE":
        return (f'<div class="tug-end tug-end-none"><span class="tug-end-l">END CALL - LOCKED</span> <b>NO CALL - too close to call</b> {_fb} '
                f'<span class="hint">- at the 8th minute ({lm}:{ls:02d} left) the lean was only {lk["lean"]:.0f}%. Reads that weak have been right only about 50-56% of the time, '
                f'so there is no call this round. It will not change.</span>{_fn}</div>')
    yes = lk["side"] == "YES"
    lean = lk["lean"]
    tier = "STRONG" if lean >= 80 else ("GOOD" if lean >= 70 else "WEAK")
    acc = _pvl_accuracy(name, lean, END_CALL_LOCK_LEFT)
    acc_txt = (f'a call locked at this strength has been right <b>{acc[0]:.0f}%</b> of the time ({acc[1]} past rounds)' if acc else
               'locked calls are right about 78% of the time overall')
    holding = (spot >= line) == yes
    live = ('<div class="tug-end-live ok">Price is still on the %s side of the line - holding.</div>' % ("UP" if yes else "DOWN") if holding else
            '<div class="tug-end-live warn">Price is on the OTHER side of the line right now. The locked call does NOT change.</div>')
    return (f'<div class="tug-end {"up" if yes else "down"}"><span class="tug-end-l">END CALL - LOCKED</span> '
            f'<b>{"YES / UP" if yes else "NO / DOWN"} {lean:.0f}%</b> <span class="tug-tier tug-tier-{tier.lower()}">{tier}</span> {_fb} '
            f'<span class="hint">locked at the 8th minute ({lm}:{ls:02d} left); {acc_txt}. It will not change.</span>{live}{_fn}</div>')


def build_tug_panel(name, d):
    """Price vs the line, and the pressure behind it, for the round in progress: where the live price sits against the Kalshi line, four
    feeds (price vs line model, exchange buyers vs sellers, Kalshi money, liquidations) fused into one lean, whether flow and price
    agree, and the dollars traded above vs below the line. Information only."""
    tn = tug_numbers(name, d)
    if tn is None:
        return ""
    spot, line, left, dec, diff, up_side, bu, se = tn["spot"], tn["line"], tn["left"], tn["dec"], tn["diff"], tn["up_side"], tn["bu"], tn["se"]
    feeds, used, fused, flow_up, ex_up, k_up, liq_up = tn["feeds"], tn["used"], tn["fused"], tn["flow_up"], tn["ex_up"], tn["k_up"], tn["liq_up"]

    def _bar_row(label, up, extra="", sub=False):
        pc = up * 100
        lead_up = pc >= 50
        lead = "UP" if lead_up else "DOWN"
        lp = pc if lead_up else 100 - pc
        cls = ("up" if lead_up else "down") if abs(pc - 50) >= 2 else "flat"
        left = min(pc, 50.0)
        width = abs(pc - 50.0)
        return (f'<div class="tug-row{" tug-sub" if sub else ""}"><span class="tug-lab">{"&nbsp;&nbsp;&rarr; " if sub else ""}{label}</span>'
                f'<span class="tug-track tug-center"><i class="tug-fill {cls}" style="left:{left:.1f}%;width:{width:.1f}%"></i></span>'
                f'<span class="tug-val {cls}">{lead} {lp:.0f}%{extra}</span></div>')
    rows = []
    for label, up, note in feeds:
        if label == "Exchange buyers vs sellers":
            rows.append("@@EXCH@@")
        if up is None:
            rows.append(f'<div class="tug-row"><span class="tug-lab">{label}</span><span class="tug-track"></span>'
                        f'<span class="tug-val hint">{note or "no data yet"}</span></div>')
        else:
            rows.append(_bar_row(label, up))
    def _sub(label, up, usd):
        if up is None:
            return ""
        return _bar_row(label, up, f' <span class="hint">({_usd_short(usd)})</span>', sub=True)
    _split = (_sub("spot exchanges only", tn["spot_up"], tn["spot_usd"]) + _sub("perpetual futures only", tn["perp_up"], tn["perp_usd"]))
    rows = [(_split if r == "@@EXCH@@" else r) for r in rows]
    if fused is not None:
        fl = fused * 100
        lean = "UP" if fl >= 50 else "DOWN"
        lean_pct = fl if fl >= 50 else 100 - fl
        lean_html = (f'<div class="tug-lean {"up" if fl >= 50 else "down"}">pressure leans {lean} <b>{lean_pct:.0f}%</b> across {len(used)} feed'
                     f'{"s" if len(used) != 1 else ""} <span class="hint">(price vs the line counts half, the flow feeds the other half - a lean, not a score)</span></div>'
                     f'<div class="tug-bar"><i style="width:{fl:.1f}%"></i><b style="left:{fl:.1f}%"></b></div>')
    else:
        lean_html = '<div class="tug-lean hint">collecting feeds...</div>'
    # needle gauge: centre = the line, needle = live price
    half = max(abs(diff) * 1.25, line * 0.0006)
    needle = 50 + max(-1.0, min(1.0, diff / half)) * 48
    yes_c = None
    if d.get("confidence") is not None and d.get("leaning") in ("YES", "NO"):
        y = d["confidence"] if d["leaning"] == "YES" else 1 - d["confidence"]
        yes_c = (round(y * 100), round((1 - y) * 100))
    kal = f'Kalshi: UP {yes_c[0]}c &middot; DOWN {yes_c[1]}c' if yes_c else ""
    px = max(spot, 1e-9)
    buy_pct = (bu / (bu + se) * 100) if (bu + se) > 0 else None
    coin = name
    boxes = ""
    if buy_pct is not None:
        boxes = (f'<div class="tug-boxes"><div class="tug-box up"><b>{buy_pct:.0f}% buyers</b><br><span>{_usd_short(bu)} &middot; {bu / px:,.2f} {coin} bought</span></div>'
                 f'<div class="tug-box down"><b>{100 - buy_pct:.0f}% sellers</b><br><span>{_usd_short(se)} &middot; {se / px:,.2f} {coin} sold</span></div></div>')
        try:
            _live_n = _flow.HUB.summary(name).get("live_n", 0)
            _em, _es = divmod(int(tn["elapsed"]), 60)
            _sp = tn.get("spot_up"); _su = tn.get("spot_usd") or 0
            _pp = tn.get("perp_up"); _pu = tn.get("perp_usd") or 0
            boxes += (f'<div class="tug-src hint">Counted since this round started ({_em}:{_es:02d} so far) on {_live_n} exchanges, spot AND perpetual futures, in dollars (coins = dollars / price). '
                      f'Spot only: {("%.0f%% buyers" % (_sp * 100)) if _sp is not None else "-"} ({_usd_short(_su)}) &middot; '
                      f'perps only: {("%.0f%% buyers" % (_pp * 100)) if _pp is not None else "-"} ({_usd_short(_pu)}). '
                      f'Checked against each exchange\'s own volume and trade list - it matches. A bot that counts one exchange, spot only, or just the last minute will show smaller numbers.</div>')
        except Exception:
            pass
    agree = ""
    if flow_up is not None:
        flow_side_up = flow_up >= 0.5
        if flow_side_up == up_side:
            agree = (f'<div class="tug-agree ok">&#10003; flow and price agree - {"buyers pushing AND price above the line" if up_side else "sellers pushing AND price below the line"}</div>')
        else:
            agree = (f'<div class="tug-agree warn">&#9888; flow and price disagree - price is {"above" if up_side else "below"} the line but '
                     f'{"buyers" if flow_side_up else "sellers"} are pushing the other way. In ~420 past rounds the PRICE side won about 3 times out of 4 when they disagreed.</div>')
    lv = _flow.HUB.line_volume(name)
    around = ""
    if lv and lv.get("ticker") == d.get("ticker") and (lv["above"] + lv["below"]) > 0:
        tot = lv["above"] + lv["below"]
        around = (f'<div class="hint tug-around">Around the line ({fmt_price(line, dec)}): <b>{_usd_short(lv["above"])}</b> traded above &middot; '
                  f'<b>{_usd_short(lv["below"])}</b> traded below ({lv["above"] / tot * 100:.0f}% above) - Coinbase, Kraken, Bitstamp, Gemini, Bitfinex, Crypto.com</div>')
    mm, ss = divmod(int(left), 60)
    end_html = _end_call_html(name, d, tn, tn.get("fair"), left, spot, line) + _line_behavior_box(name)
    return (f'<div class="tug"><div class="tug-title">PRICE VS THE LINE &middot; tug of war <span class="hint">(this round &middot; resets in {mm:02d}:{ss:02d})</span></div>'
            f'{end_html}'
            f'<div class="tug-top"><div class="tug-pair"><div><div class="hint">price to beat (Kalshi line)</div><div class="tug-big">{fmt_price(line, dec)}</div><div class="hint">{kal}</div></div>'
            f'<div class="tug-vs hint">vs</div>'
            f'<div><div class="hint">live price</div><div class="tug-big {"up" if up_side else "down"}">{fmt_price(spot, dec)}</div>'
            f'<div class="hint">{"&#9650; above" if up_side else "&#9660; below"} the line</div></div>'
            f'<div class="tug-vs hint">=</div>'
            f'<div><div class="hint">difference</div><div class="tug-diff {"up" if up_side else "down"}">{"+" if up_side else "-"}{fmt_price(abs(diff), dec)}</div>'
            f'<div class="hint">{"above" if up_side else "below"} the line</div></div></div></div>'
            f'<div class="tug-gauge"><span class="tug-gl hint">&#9660; DOWN</span><span class="tug-gline"></span>'
            f'<i class="tug-coin tug-coin-{name.lower()} {"up" if up_side else "down"}" style="left:{needle:.1f}%" title="live price">{"&#8383;" if name == "BTC" else "X"}</i>'
            f'<span class="tug-gr hint">UP &#9650;</span></div>'
            f'<div class="hint tug-cap">centre = the line &middot; needle = live price ({fmt_price(spot, dec)})</div>'
            f'<div class="tug-head {"up" if up_side else "down"}">{"UP" if up_side else "DOWN"} is winning by {fmt_price(abs(diff), dec)}</div>'
            f'{lean_html}{"".join(rows)}{boxes}{agree}{around}</div>')


def _coinbase_last_price(symbol):
    """(price, ts) of the most recent Coinbase trade, straight from the live feed (updates on every match)."""
    with _ticks_lock:
        v = _exchange_last.get(symbol, {}).get("coinbase")
    return (v[0], v[2]) if v else (None, None)


def _live_patched(cs, sym, tf):
    """Copy of the candles with the still-forming one moved to the latest Coinbase trade - so the chart follows the market tick by tick
    instead of waiting for the next candle download. Coinbase's own candle stays the base, so colours / wicks match the exchange."""
    from src.pattern_detector import TF_SECONDS as _TFS
    out = [list(x) for x in cs]
    px, pts = _coinbase_last_price(sym)
    if out and px and pts and time.time() - pts < 30:
        ts_, o_, h_, l_, c_ = out[-1]
        secs = _TFS.get(tf, 0)
        if secs and ts_ <= pts < ts_ + secs * 1.05 + 5 and ts_ <= time.time() < ts_ + secs * 1.05 + 5:
            out[-1] = [ts_, o_, max(h_, px), min(l_, px), px]
    return out


_live_labels = {}      # (asset, tf) -> (ts, labels): pattern detection re-run at most once a second for the fast chart channel


_label_sticky = {}                  # (coin, timeframe) -> {pattern key: [label, last time it was detected]}
_label_sticky_lock = threading.Lock()
CHART_LABEL_HOLD_SECONDS = 8


def _stable_labels(asset_name, tf, labels):
    """The labels the CHART draws: everything detected right now PLUS anything detected in the last few seconds, in a fixed order. Without this a
    label (a Fibonacci level, a swing-range line...) that flickers across its detection threshold made the chart blink every second."""
    from src.mtf_structure import _pkey
    now = time.time()
    with _label_sticky_lock:
        st = _label_sticky.setdefault((asset_name, tf), {})
        seen = set()
        for l in labels:
            k = _pkey(l["name"])
            st[k] = [l, now]
            seen.add(k)
        out = list(labels)
        for k in list(st):
            if k in seen:
                continue
            if now - st[k][1] <= CHART_LABEL_HOLD_SECONDS:
                out.append(st[k][0])
            else:
                del st[k]
    return sorted(out, key=lambda l: _pkey(l["name"]))


_sweep_mem = {}                    # (coin, timeframe) -> {(candle time, up?): {"first": when we first saw it}}


SWEEP_STAR_BARS = 5                 # a CONFIRMED liquidity sweep keeps its star for this many bars of the chart's own timeframe after the swept candle closed
_sweep_scan = {}                    # (coin, timeframe) -> the candle the recent past was last re-scanned at


def _sweep_stars(asset_name, tf, labels, cs):
    """The liquidity sweeps to keep a star on: a CONFIRMED sweep (the swept candle closed back inside) keeps its star on that candle for SWEEP_STAR_BARS bars
    of the timeframe being shown. The recent past is re-scanned from the candles every new bar, so the stars come back after a dashboard restart or a
    reload instead of vanishing. -> [{"ts", "up", "text"}]"""
    from src.pattern_detector import _structure as _st, TF_SECONDS as _TFS
    now = time.time()
    tfs = _TFS.get(tf, 60)
    out = []
    with _label_sticky_lock:
        mem = _sweep_mem.setdefault((asset_name, tf), {})
        if len(cs) >= 3:
            for l in labels:
                if l["name"].startswith("LIQUIDITY SWEEP") and l.get("status") == "formed":      # confirmed: the swept candle closed back inside
                    mem.setdefault((cs[-2][0], "highs" in l["name"]), {"first": now})
            sk = (asset_name, tf)
            if _sweep_scan.get(sk) != cs[-1][0]:
                _sweep_scan[sk] = cs[-1][0]
                for back in range(1, SWEEP_STAR_BARS + 2):
                    sub = cs[:len(cs) - back]
                    if len(sub) < 40:
                        break
                    try:
                        past = _st(sub, tf)
                    except Exception:
                        continue
                    for l in past:
                        if l["name"].startswith("LIQUIDITY SWEEP") and l.get("status") == "formed":
                            mem.setdefault((sub[-2][0], "highs" in l["name"]), {"first": now})
        last_ts = cs[-1][0] if cs else now
        for k, v in list(mem.items()):
            bars_since = int(round((last_ts - k[0]) / tfs)) - 1        # 0 = the bar right after the swept candle (where it was confirmed)
            if bars_since > SWEEP_STAR_BARS or bars_since < 0:
                del mem[k]
                continue
            out.append({"ts": k[0], "up": k[1], "text": "sweep just confirmed" if bars_since <= 0 else ("sweep %d bar%s ago" % (bars_since, "" if bars_since == 1 else "s"))})
    return out


def chart_live_payload(asset_name):
    """Fast channel: just the chart SVG + volume bar for the selected timeframe, re-drawn from the latest trade. Information only."""
    from src.pattern_detector import _structure as _st, _candle_patterns as _cp, TF_SECONDS as _TFS
    from src.pattern_overlay import render_window_svg
    if _mtf is None:
        return None
    sym = {a["name"]: a["symbol"] for a in DASHBOARD_ASSETS}.get(asset_name)
    tf = _get_pat_tf(asset_name)
    cs0 = (_mtf.windows.get(sym) or {}).get(tf) if sym else None
    if not cs0 or len(cs0) < 25:
        return None
    cs = _live_patched(cs0, sym, tf)
    key = (asset_name, tf)
    hit = _live_labels.get(key)
    if hit and time.time() - hit[0] < 1.0 and hit[2] == len(cs):
        labels = hit[1]
    else:
        labels = _st(cs, tf) + _cp(cs, tf, max_age=3)
        _live_labels[key] = (time.time(), labels, len(cs))
    svg, shown = render_window_svg(cs, _stable_labels(asset_name, tf, labels), title=f"{asset_name} - {_MTF_NAMES[tf]} candles (dashed outline = still forming)", events=_cpi_chart_events(cs), levels=_chart_levels(asset_name), sweeps=_sweep_stars(asset_name, tf, labels, cs))
    return {"svg": svg, "vol": build_volume_bar(asset_name), "tf": tf, "rsi": rsi_tradingview(asset_name)}


_VOL_HELP = ('<details class="vol-help" data-pat="VOLHELP|{a}"><summary>? what is this</summary><div class="vol-help-body">'
             '<b>The volume bar</b> shows how much money is trading <i>right now</i> on all exchanges, and who is pushing it.<br><br>'
             '<span class="up"><b>GREEN = buy money</b></span>: dollars where a buyer paid the asking price (they wanted in now).<br>'
             '<span class="down"><b>RED = sell money</b></span>: dollars where a seller took the bid (they wanted out now).<br>'
             'Green sits on top, red on the bottom. 70% green = 70% of the last minute&#39;s money was buying.<br><br>'
             '<b>Height</b> = total dollars traded in the last 60 seconds (the big number on top).<br>'
             '<b>White line</b> = this coin&#39;s normal minute (about the last 14 minutes). Above the line = busier than usual; below = quiet.<br>'
             '<b>x2.0 vs normal</b> = twice as busy as usual. The small arrow shows if volume is rising or falling vs the minute before.<br><br>'
             '<b>How to use it:</b> tall + mostly green = heavy buying; tall + mostly red = heavy selling; short bar = quiet market, so a move is less trustworthy.<br><br>'
             '<span class="hint">Different from the pressure bar, which only shows the buy/sell split. This one also shows how much money is behind it. '
             'Information only - never used for buying.</span></div></details>')


def build_volume_bar(asset_name, tag=""):
    """Vertical volume bar for the side of the chart: bar height = traded volume in the last minute vs this market's normal, the fill is
    split green (buy dollars, top) / red (sell dollars, bottom), a white line marks 'normal'. All exchanges. Information only."""
    try:
        g = _flow.HUB.volume_gauge(asset_name) if asset_name in _flow.ASSETS else None
    except Exception:
        g = None
    if not g or g["total"] <= 0:
        return '<div class="vol-bar"><div class="vol-title">VOLUME</div><div class="vol-track"></div><div class="vol-label">collecting...</div>' + _VOL_HELP.format(a=asset_name + tag) + '</div>'
    top = max(g["p95"], 2.0 * g["median"], g["total"], 1.0) * 1.1
    fill = min(100.0, g["total"] / top * 100)
    normal = min(100.0, g["median"] / top * 100) if g["median"] > 0 else -10
    buy_pct = g["buy"] / g["total"] * 100
    ratio = (g["total"] / g["median"]) if g["median"] > 0 else None
    trend = ("&#9650;" if g["total"] > g["prev"] * 1.15 else ("&#9660;" if g["total"] < g["prev"] * 0.85 else "&#9644;")) if g["prev"] > 0 else ""
    rtxt = f"x{ratio:.1f}" if ratio is not None else "-"
    rcls = "up" if (ratio or 0) >= 1.5 else ("flat" if (ratio or 0) >= 0.7 else "down")
    return (f'<div class="vol-bar" title="Traded volume in the last 60s on all exchanges: {_usd_short(g["total"])} '
            f'(buy {_usd_short(g["buy"])} / sell {_usd_short(g["sell"])}). White line = this market&#39;s normal ({_usd_short(g["median"])}).">'
            f'<div class="vol-title">VOLUME</div><div class="vol-label"><b>{_usd_short(g["total"])}</b><br>last 60s<br>all exchanges</div>'
            f'<div class="vol-track"><div class="vol-fill" style="height:{fill:.1f}%">'
            f'<div class="vol-buy" style="height:{buy_pct:.1f}%"></div><div class="vol-sell" style="height:{100 - buy_pct:.1f}%"></div></div>'
            f'<div class="vol-normal" style="bottom:{normal:.1f}%"></div></div>'
            f'<div class="vol-label"><span class="{rcls}"><b>{rtxt}</b></span> vs normal {trend}</div>'
            f'<div class="vol-legend"><span class="lg-buy">&#9632;</span> buy $<br><span class="lg-sell">&#9632;</span> sell $<br><span class="lg-norm">&#9473;</span> normal</div>' + _VOL_HELP.format(a=asset_name + tag) + '</div>')


_FAIR_ACC = {   # time left (s) -> % of rounds where the side the fair value favoured won
    "BTC": [(840, 59.8), (600, 72.5), (420, 78.4), (300, 81.3), (180, 87.0), (120, 90.1), (60, 95.7), (30, 97.0)],
    "XRP": [(840, 58.4), (600, 72.2), (420, 77.6), (300, 82.3), (180, 88.8), (120, 92.0), (60, 94.3), (30, 95.6)]}
_FAIR_CAL = {   # fair value said YES chance in this 10% band -> % of the time YES really happened (7, 5 and 3 min left pooled)
    "BTC": [5, 14, 23, 35, 41, 53, 70, 79, 84, 96],
    "XRP": [3, 13, 25, 36, 51, 59, 74, 83, 93, 98]}


def _fair_tables_html(name):
    acc = _FAIR_ACC.get(name) or _FAIR_ACC["BTC"]
    cal = _FAIR_CAL.get(name) or _FAIR_CAL["BTC"]
    r1 = "".join(f"<tr><td>{(s // 60) if s >= 60 else s}{' min' if s >= 60 else ' sec'} left</td><td><b>{v:.0f}%</b></td></tr>" for s, v in acc)
    r2 = "".join(f"<tr><td>said {i * 10}-{i * 10 + 10}%</td><td><b>{v}%</b></td></tr>" for i, v in enumerate(cal))
    return (f'<table class="fair-tbl"><tr><th>Right side called ({name})</th><th>of rounds</th></tr>{r1}</table>'
            f'<table class="fair-tbl"><tr><th>When it said YES chance...</th><th>YES really won</th></tr>{r2}</table>')


MARKET_24H, MARKET_3D, MARKET_3D_STRONG = 1.0, 1.5, 3.0     # percent: how far price must have moved to call a bull / bear market


_MKT_HORIZONS = [("5 min", 5, 0.06), ("15 min", 15, 0.10), ("30 min", 30, 0.15), ("1 hour", 60, 0.20), ("4 hours", 240, 0.40),
                 ("12 hours", 720, 0.70), ("24 hours", 1440, 1.0), ("3 days", 4320, 1.5), ("7 days", 10080, 2.5)]


def market_horizons(asset_name):
    """[(period, % change, BULL / BEAR / NEUTRAL, move needed to leave NEUTRAL)] from 15 minutes to 7 days - from the 1-minute and hourly candles the
    monitor keeps. The needed move is a bit bigger for XRP (it moves more). Information only."""
    if _mtf is None:
        return []
    sym = {a["name"]: a["symbol"] for a in DASHBOARD_ASSETS}.get(asset_name)
    raw = _mtf.raw.get(sym) or {} if sym else {}
    one, hr = raw.get("1m") or [], raw.get("1h") or []
    last = one[-1][4] if one else (hr[-1][4] if hr else None)
    if not last:
        return []
    fac = 1.4 if asset_name == "XRP" else 1.0
    rows = []
    for label, mins, thr in _MKT_HORIZONS:
        if mins <= 240 and len(one) > mins:
            base = one[-1 - mins][4]
        elif mins > 240 and len(hr) > mins // 60:
            base = hr[-1 - mins // 60][4]
        else:
            continue
        chg = (last / base - 1) * 100
        t = thr * fac
        rows.append((label, chg, "BULL" if chg >= t else ("BEAR" if chg <= -t else "NEUTRAL"), t))
    return rows


def market_trend(asset_name):
    """What kind of market this coin is in, judged over DAYS (not the last few hours): moves over 4h / 24h / 3d / 7d from the hourly candles.
    BEAR = down >1% in 24h AND >1.5% in 3 days, or down >3% in 3 days (BULL is the mirror image); otherwise NEUTRAL. Information only."""
    if _mtf is None:
        return None
    sym = {a["name"]: a["symbol"] for a in DASHBOARD_ASSETS}.get(asset_name)
    cs = (_mtf.raw.get(sym) or {}).get("1h") if sym else None
    if not cs or len(cs) < 30:
        return None
    last = cs[-1][4]

    def chg(hours):
        if len(cs) <= hours + 1:
            return None
        return (last / cs[-1 - hours][4] - 1) * 100
    r4, r24, r72, r168 = chg(4), chg(24), chg(72), chg(168)
    if r24 is None or r72 is None:
        return None
    if (r24 <= -MARKET_24H and r72 <= -MARKET_3D) or r72 <= -MARKET_3D_STRONG:
        label = "BEAR"
    elif (r24 >= MARKET_24H and r72 >= MARKET_3D) or r72 >= MARKET_3D_STRONG:
        label = "BULL"
    else:
        label = "NEUTRAL"
    return {"label": label, "r4": r4, "r24": r24, "r72": r72, "r168": r168}


def _cpi_chart_events(cs):
    """[(timestamp, label)] for every CPI release that falls inside the candles on screen."""
    try:
        from src import cpi_info as _ci
        if not cs or len(cs) < 3:
            return None
        end = cs[-1][0] + (cs[-1][0] - cs[-2][0])
        out = []
        for rel in _ci.release_times():
            ts = rel.timestamp()
            if cs[0][0] <= ts < end:
                out.append((ts, "CPI " + _viewtz.t12(rel.astimezone(_viewtz.get()))))
        return out or None
    except Exception:
        return None


def _cpi_levels_info(asset_name):
    """The CPI floor / ceiling for this coin from the live 1-minute candles (only while the last release is still inside them)."""
    from src import cpi_info as _ci
    last = _ci.last_release()
    if not last or _mtf is None:
        return None
    rel_ts = last[0].timestamp()
    if time.time() - rel_ts > 5.2 * 3600:
        return None
    sym = {a["name"]: a["symbol"] for a in DASHBOARD_ASSETS}.get(asset_name)
    cs = (_mtf.raw.get(sym) or {}).get("1m") if sym else None      # ~5 hours of 1-minute candles (the chart window keeps only 90)
    if not cs:
        return None
    lv = _ci.levels(cs, rel_ts)
    return lv if lv and lv.get("complete") else None


def _wall_chart_levels(asset_name):
    """The biggest BUY wall (below price) and SELL wall (above price) across all the exchanges, as lines on the candle chart. Same walls as the walls row."""
    try:
        sym = {a["name"]: a["symbol"] for a in DASHBOARD_ASSETS}.get(asset_name)
        px = get_live_price(sym) if sym else None
        w = _all_exchange_walls(sym, px) if px else None
        if not w:
            return []
        dec = 4 if asset_name == "XRP" else 2
        out = []
        for key, lab, col in (("bid", "BUY WALL", "#3fb950"), ("ask", "SELL WALL", "#f85149")):
            b = w.get(key)
            if b:
                usd = b[1]
                money = f"${usd / 1e6:.1f}M" if usd >= 1e6 else f"${usd / 1e3:.0f}K"
                out.append({"p": b[0], "text": f"{lab} {fmt_price(b[0], dec)}  {money}", "col": col, "w": 1.6, "dash": "10 4"})
        return out
    except Exception:
        return []


def _chart_levels(asset_name):
    lv = (_cpi_chart_levels(asset_name) or []) + _wall_chart_levels(asset_name)
    return lv or None


def _cpi_chart_levels(asset_name):
    """Lines for the chart: CPI ceiling and floor, plus where it USUALLY stops if it breaks (the median run of past releases, in range heights)."""
    try:
        from src import cpi_info as _ci
        lv = _cpi_levels_info(asset_name)
        if not lv or lv["height"] <= 0:
            return None
        dec = 4 if asset_name == "XRP" else 2
        st = _ci.history_stats(asset_name) or {}
        med = st.get("median_ext_x") or 0.6
        H, L, h = lv["ceiling"], lv["floor"], lv["height"]
        out = [{"p": H, "text": f"CPI CEILING {fmt_price(H, dec)}", "col": "#d29922", "w": 1.7, "dash": "7 4"},
               {"p": L, "text": f"CPI FLOOR {fmt_price(L, dec)}", "col": "#d29922", "w": 1.7, "dash": "7 4"}]
        if lv["break"] in (None, "up"):
            out.append({"p": H + med * h, "text": f"if it breaks UP: usual stop {fmt_price(H + med * h, dec)}", "col": "#3fb950", "w": 1.2, "dash": "2 4"})
        if lv["break"] in (None, "down"):
            out.append({"p": L - med * h, "text": f"if it breaks DOWN: usual stop {fmt_price(L - med * h, dec)}", "col": "#f85149", "w": 1.2, "dash": "2 4"})
        if lv["break"] == "up":
            out.append({"p": H + h, "text": f"full measured move {fmt_price(H + h, dec)} (rare)", "col": "#3fb950", "w": 1.0, "dash": "1 5"})
        if lv["break"] == "down":
            out.append({"p": L - h, "text": f"full measured move {fmt_price(L - h, dec)} (rare)", "col": "#f85149", "w": 1.0, "dash": "1 5"})
        return out
    except Exception:
        return None


def _cpi_levels_html(asset_name):
    """Readout of the CPI floor / ceiling + the odds from the 10 past 2026 releases. Information only."""
    from src import cpi_info as _ci
    last = _ci.last_release()
    if not last:
        return ""
    age = time.time() - last[0].timestamp()
    if age < 0 or age > 5.2 * 3600:
        return ""
    if age < 300:
        return '<div class="hint">CPI range: the first 5 minutes after the release set the floor and the ceiling - still forming...</div>'
    lv = _cpi_levels_info(asset_name)
    if not lv:
        return ""
    dec = 4 if asset_name == "XRP" else 2
    z = _viewtz.get()
    if lv["break"] is None:
        state = '<span class="flat"><b>INSIDE the range</b></span>'
    else:
        bt = dt.datetime.fromtimestamp(lv["break_ts"], z)
        word, col = ("UP through the ceiling", "up") if lv["break"] == "up" else ("DOWN through the floor", "down")
        if lv.get("now_inside"):
            back = ' - <b>but it is back inside the range now</b> (a fake-out)'
        elif lv.get("reentered_15"):
            back = ' - it dipped back inside once, then broke out again'
        else:
            back = ""
        state = (f'<span class="{col}"><b>BROKE {word}</b></span> at {_viewtz.t12(bt)}{back} <span class="hint">(ran {lv["ext_x"]:.2f}x the range height past it so far)</span>')
    html = (f'<div><span class="hint">CPI range (first 5 min):</span> floor <b>{fmt_price(lv["floor"], dec)}</b> - ceiling <b>{fmt_price(lv["ceiling"], dec)}</b> '
            f'<span class="hint">({lv["height_pct"]:.2f}% wide)</span> &middot; now {state}</div>')
    st = _ci.history_stats(asset_name)
    if st and st.get("n"):
        brk = st.get("broke_up", 0) + st.get("broke_down", 0)
        html += (f'<div class="hint">Last {st["n"]} CPI releases ({asset_name}): broke up {st.get("broke_up", 0)}, broke down {st.get("broke_down", 0)}, '
                 f'stayed inside {st.get("stayed_inside", 0)} &middot; {st.get("fakeouts", 0)} of {brk} breaks were back inside within 15 min &middot; '
                 f'the usual run past the level was {st.get("median_ext_x", 0):.2f}x the range height &middot; a full 1x move happened {st.get("reached_1x", 0)} of {brk} times '
                 f'(2x: {st.get("reached_2x", 0)}).</div>')
    return html


def _cpi_reaction_html(asset_name):
    """What this coin did since the last CPI release (from the live 1-minute candles) - only while the release is still inside them."""
    from src import cpi_info as _ci
    last = _ci.last_release()
    if not last or _mtf is None:
        return ""
    rel_ts = last[0].timestamp()
    age = time.time() - rel_ts
    if age < 0 or age > 5.2 * 3600:
        return ""
    sym = {a["name"]: a["symbol"] for a in DASHBOARD_ASSETS}.get(asset_name)
    cs = (_mtf.raw.get(sym) or {}).get("1m") if sym else None
    if not cs:
        return ""
    idx = max((i for i, x in enumerate(cs) if x[0] <= rel_ts), default=None)
    if idx is None or idx > len(cs) - 2:
        return ""
    p0 = cs[idx][1]
    post = cs[idx:]
    chg = (cs[-1][4] / p0 - 1) * 100
    up, dn = (max(x[2] for x in post) / p0 - 1) * 100, (min(x[3] for x in post) / p0 - 1) * 100
    first = cs[idx: idx + 15]
    extra = ""
    pre = cs[max(0, idx - 240): idx]
    ranges = [(max(x[2] for x in pre[i:i + 15]) - min(x[3] for x in pre[i:i + 15])) / pre[i][1] * 100 for i in range(0, len(pre) - 15, 5)]
    if len(first) >= 5 and len(ranges) >= 6 and sum(ranges) > 0:
        fr = (max(x[2] for x in first) - min(x[3] for x in first)) / p0 * 100
        extra = (f' &middot; first {"15 min" if len(first) >= 15 else str(len(first)) + " min so far"}: swung {fr:.2f}% = '
                 f'<b>x{fr / (sum(ranges) / len(ranges)):.1f}</b> a normal 15 minutes')
    col = "up" if chg > 0.02 else ("down" if chg < -0.02 else "flat")
    return (f'<div><span class="hint">{asset_name} since the release ({int(age // 60)} min ago):</span> <span class="{col}"><b>{chg:+.2f}%</b></span> '
            f'<span class="hint">(low {dn:+.2f}% / high {up:+.2f}%{extra})</span></div>')


def build_cpi_strip(asset_name):
    """CPI readout above the chart: latest numbers vs the forecast, the next release (in the viewer's time), a warning around a release, what the coin
    did since the last one, and a drop-down that explains CPI and how it tends to move price. Information only."""
    from src import cpi_info as _ci
    now = dt.datetime.now(ET_ZONE)
    z = _viewtz.get()
    fmt = lambda d: (lambda x: f"{x:%a %b} {x.day}, {_viewtz.t12(x)}")(d.astimezone(z))
    ph, prel = _ci.phase(now)
    banner = ""
    if ph == "before":
        mins = max(1, int((prel - now).total_seconds() // 60))
        banner = f'<div class="cpi-flag">&#9888; CPI comes out in {mins} min ({fmt(prel)}) - expect big, fast, two-way moves around it</div>'
    elif ph == "live":
        mins = int((now - prel).total_seconds() // 60)
        banner = f'<div class="cpi-flag">&#9888; CPI came out {mins} min ago ({fmt(prel)}) - expect big, fast, two-way moves for a while</div>'
    r = _ci.latest()
    if r and r["yoy"] is not None:
        vtxt = ""
        if r.get("verdict"):
            vcol = {"hotter": "down", "cooler": "up", "in line": "flat"}[r["verdict"]]
            vtxt = (f' <span class="{vcol}"><b>{r["verdict"].upper()}</b></span> <span class="hint">than the {r["expected_yoy"]:.1f}% forecast</span>'
                    if r["verdict"] != "in line" else f' <span class="flat"><b>IN LINE</b></span> <span class="hint">with the {r["expected_yoy"]:.1f}% forecast</span>')
        rel_txt = f' <span class="hint">(released {fmt(r["released"])})</span>' if r.get("released") else ""
        read = (f'<div><b>CPI {r["month"]}:</b> <b>{r["yoy"]:.1f}%</b> a year ({r["mom"]:+.1f}% on the month) &middot; '
                f'core <b>{r["core_yoy"]:.1f}%</b> ({r["core_mom"]:+.1f}% on the month){vtxt}{rel_txt}</div>')
    else:
        read = '<div class="hint">CPI numbers loading...</div>'
    nxt = _ci.next_release(now)
    if nxt:
        secs = int((nxt[0] - now).total_seconds())
        d_, h_, m_ = secs // 86400, (secs % 86400) // 3600, (secs % 3600) // 60
        left = (f"{d_}d {h_}h" if d_ else f"{h_}h {m_}m")
        nxt_txt = f'<div><span class="hint">Next CPI ({nxt[1]}):</span> <b>{fmt(nxt[0])}</b> <span class="hint">- in {left}</span></div>'
    else:
        nxt_txt = ""
    sched = " &middot; ".join(fmt(rel) for rel in [x for x in _ci.release_times() if x > now][:3]) or "none listed"
    help_html = (
        f'<details class="cpi-help" data-pat="CPIHELP|{asset_name}"><summary>? what is CPI and how does it move price</summary><div class="cpi-help-body">'
        '<b>What it is:</b> CPI (Consumer Price Index) is the government&#39;s monthly measure of how much the prices people pay have changed - a basket of '
        'housing, food, gas, cars, medical care and more. It comes out around 8:30 AM Eastern, about once a month.<br><br>'
        '<b>How to read it:</b> <i>headline</i> = everything. <i>Core</i> = without food and energy (steadier - what the Fed watches most). '
        '<i>On the month</i> = change since last month. <i>A year</i> = change since a year ago. Markets react to the <b>surprise</b> - the number versus the '
        'forecast - not the number by itself.<br><br>'
        '<b>How it tends to hit price</b> (a tendency, not a rule):<br>'
        '&bull; <span class="down"><b>HOTTER than forecast</b></span> - inflation is not cooling, so rate cuts look less likely and rates may stay high. '
        'The dollar and yields rise and risk assets like BTC and XRP often drop fast.<br>'
        '&bull; <span class="up"><b>COOLER than forecast</b></span> - rate cuts look more likely, and risk assets often jump.<br>'
        '&bull; <span class="flat"><b>IN LINE</b></span> - usually a smaller move, sometimes a fade.<br>'
        'The first move often reverses within minutes (a whipsaw), and headline and core can disagree - so the first spike is not the answer.<br><br>'
        '<b>The CPI floor and ceiling (the lines on the chart):</b> the first 5 minutes after the release set a high (ceiling) and a low (floor). A break is a '
        '1-minute candle that CLOSES beyond one of them. The dotted lines show where it has USUALLY stopped after a break in the 10 past releases - not where it '
        'will go. Most breaks fake out: price often closes back inside the range within 15 minutes, so a break is a warning, not a signal, and a full "measured move" '
        '(the range height again) is rare.<br><br>'
        '<b>What it means for this dashboard and the 15-minute Kalshi markets:</b> price can cross the line in seconds; order books thin out and spreads widen; '
        'the flow numbers and INCOMING banners spike; and the fair value is built from the last 5 minutes of calm, so it understates the risk right before a '
        'release. Treat the first minutes around 8:30 as noisy for every number on the page.<br><br>'
        f'<b>Coming up</b> (BLS schedule, shown in your time): {sched}.<br>'
        '<span class="hint">Numbers are the government&#39;s own (BLS). Information only - not advice, and nothing here changes a call or a trade.</span>'
        '</div></details>')
    cls = {"before": "cpi-before", "live": "cpi-live"}.get(ph, "")
    chip = ""
    if ph == "before":
        chip = f' <span class="cpi-chip">in {max(1, int((prel - now).total_seconds() // 60))} min</span>'
    elif ph == "live":
        chip = ' <span class="cpi-chip">just released</span>'
    return (f'<details class="cpi-drop" data-pat="CPI|{asset_name}"><summary><b>CPI</b>{chip}</summary>'
            f'<div class="cpi-strip {cls}">{banner}{read}{_cpi_reaction_html(asset_name)}{_cpi_levels_html(asset_name)}{nxt_txt}{help_html}</div></details>')


SWEEP_ALERT_TFS = ("1m", "5m", "15m", "1h")


def sweep_forming_markers(asset_name):
    """Hidden page markers for every LIQUIDITY SWEEP that is forming right now (the open candle poked past equal highs / lows and price is back
    inside). The browser turns a NEW one into a banner + sound + voice. Information only - never used for buying."""
    if _mtf is None:
        return ""
    sym = {a["name"]: a["symbol"] for a in DASHBOARD_ASSETS}.get(asset_name)
    snap = _mtf.snapshot(sym) if sym else None
    if not snap:
        return ""
    out = []
    for tf in SWEEP_ALERT_TFS:
        a = snap.get(tf)
        if not a:
            continue
        cs = (_mtf.windows.get(sym) or {}).get(tf)
        cts = int(cs[-1][0]) if cs else 0
        for l in a.get("all_labels", a.get("labels", [])):
            if l["name"].startswith("LIQUIDITY SWEEP") and l.get("status") == "forming":
                side = "highs" if "highs" in l["name"] else "lows"
                out.append(f'<span class="sweep-alert" data-name="{asset_name}" data-tf="{tf}" data-side="{side}" data-bias="{l["bias"]}" '
                           f'data-key="{asset_name}|{tf}|{side}|{cts}" style="display:none;"></span>')
    return "".join(out)


# ---- line crossings (2026-10-16): how often the live price crosses the Kalshi target line inside each round, per hour, with a picture. Information only.
from src import line_cross as _lc
_LC_RESULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache", "round_results.json")
_LC_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot_state", "line_cross_log.csv")
_lc_rounds = {"BTC": [], "XRP": []}
_lc_results = {}          # ticker -> "YES"/"NO" (Kalshi's real, finalized result)
_lc_logged = set()
_lc_mu = threading.Lock()
_lc_built = [0.0]
_lc_cache = {}


def _lc_load():
    global _lc_results
    try:
        with open(_LC_RESULTS_FILE, "r", encoding="utf-8") as f:
            _lc_results = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        _lc_results = {}
    try:
        with open(_LC_LOG, "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                _lc_logged.add(r.get("ticker"))
    except (FileNotFoundError, OSError):
        pass


def _lc_pass():
    now = time.time()
    new_rounds = {}
    for a in ("BTC", "XRP"):
        new_rounds[a] = _lc.analyze(_lc.read_recent(a, now - 5.3 * 3600))
    asked = 0
    changed = False
    for a, rs in new_rounds.items():
        for r in rs:
            tk = r["ticker"]
            if tk in _lc_results or r["close"] + 25 > now or asked >= 14:
                continue
            asked += 1
            try:
                snap = get_market_snapshot(tk)
                res = (snap.get("result") or "").upper()
                if snap.get("status") == "finalized" and res in ("YES", "NO"):
                    _lc_results[tk] = res
                    changed = True
            except Exception:
                pass
    if changed:
        try:
            os.makedirs(os.path.dirname(_LC_RESULTS_FILE), exist_ok=True)
            tmp = f"{_LC_RESULTS_FILE}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(list(_lc_results.items())[-4000:]), f)
            os.replace(tmp, _LC_RESULTS_FILE)
        except Exception:
            pass
    # every finished + settled round is also written to a permanent log
    try:
        rows = []
        for a, rs in new_rounds.items():
            for r in rs:
                if r["ticker"] in _lc_logged or r["ticker"] not in _lc_results or r["cov"] < 0.85:
                    continue
                _lc_logged.add(r["ticker"])
                rows.append([dt.datetime.fromtimestamp(r["close"], dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), a, r["ticker"], len(r["cross"]),
                             "" if r["pct_above"] is None else round(r["pct_above"], 1), r["start_side"] or "", r["side8"] or "", r["end_side"] or "",
                             _lc_results[r["ticker"]], round(r["cov"], 2)])
        if rows:
            os.makedirs(os.path.dirname(_LC_LOG), exist_ok=True)
            new = not os.path.exists(_LC_LOG)
            with open(_LC_LOG, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["close_utc", "asset", "ticker", "crossings", "pct_time_above", "start_side", "side_at_8th_min", "end_side", "kalshi_result", "coverage"])
                w.writerows(rows)
    except OSError:
        pass
    with _lc_mu:
        _lc_rounds.update(new_rounds)
        _lc_built[0] = time.time()


def _lc_loop():
    while True:
        try:
            _lc_pass()
        except Exception as e:
            print(f"[line cross] pass failed: {e!r}")
        time.sleep(15)


_BP_TFS = (("15m", "15 min"), ("1h", "1 hour"), ("3h", "3 hour"), ("6h", "6 hour"), ("12h", "12 hour"), ("1d", "Day"), ("1w", "Week"))


def _bigger_picture(asset_name):
    """The chart read on the bigger timeframes (hour, day ...) - bias from the multi-timeframe monitor. -> (chips_html, sentence)"""
    try:
        sym = {a["name"]: a["symbol"] for a in DASHBOARD_ASSETS}.get(asset_name)
        snap = _mtf.snapshot(sym) if (_mtf is not None and sym) else None
        if not snap:
            return "", ""
        chips, bias = [], {}
        for tf, nm in _BP_TFS:
            a = snap.get(tf)
            if not a or a.get("note"):
                continue
            b = a.get("bias", "neutral")
            bias[tf] = b
            arrow = {"bullish": "&#9650; UP", "bearish": "&#9660; DOWN"}.get(b, "&#9644; FLAT")
            top = next((l["name"] for l in a.get("labels", []) if l.get("bias") == b and b != "neutral"), "")
            chips.append(f'<span class="bp-chip bp-{b}" title="{str(top).replace('"', '&quot;').replace('<', '&lt;')}">{nm}: {arrow}</span>')
        if not chips:
            return "", ""
        h, d = bias.get("1h"), bias.get("1d")
        if h and d:
            if h == d and h != "neutral":
                sent = f"The hour chart and the day chart both lean {'UP' if h == 'bullish' else 'DOWN'} - the small charts are moving with the bigger story."
            elif "neutral" in (h, d):
                sent = f"Hour chart: {h}. Day chart: {d}. One of them has no clear lean, so the bigger charts are not giving a strong push."
            else:
                sent = f"The hour chart leans {'UP' if h == 'bullish' else 'DOWN'} but the day chart leans {'UP' if d == 'bullish' else 'DOWN'} - expect back-and-forth on the small charts."
        else:
            sent = ""
        return "".join(chips), sent
    except Exception:
        return "", ""


def _line_behavior_box(asset_name):
    """The always-visible box under END CALL: what the price is doing around the line right now + how rounds like that have ended."""
    try:
        with _lc_mu:
            rounds = list(_lc_rounds.get(asset_name, []))
        now = time.time()
        cur = next((r for r in reversed(rounds) if r["close"] > now >= r["start"]), None)
        cl = _lc.classify(cur, now) if cur is not None and cur.get("n", 0) >= 20 else None
        if not cl:
            return ""
        acc = _lc.LB_STATS.get(asset_name, {}).get(cl["code"])
        again = _lc.LB_AGAIN.get(cl["code"])
        extra = ""
        if acc is not None:
            extra = (f' <span class="hint">In past rounds like this, the side the price was on at the 8th minute won <b>{acc:.0f}%</b> of the time'
                     + (f' and it crossed the line again in the last 7 minutes <b>{again}%</b> of the time' if again is not None else "") + '.</span>')
        return (f'<div class="lb-box lb-{cl["code"].lower()}"><span class="lb-l">LINE BEHAVIOR</span> <b>{cl["label"]}</b> '
                f'<span class="lb-t">- {cl["text"]}.</span>{extra}</div>')
    except Exception:
        return ""


def _dur_txt(sec):
    sec = max(0, int(sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {sec % 60:02d}s" if m < 10 else f"{m}m"


_MO_STATS = {"v": None}


def _mo_stats():
    if _MO_STATS["v"] is None:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache", "market_open_stats.json"), "r", encoding="utf-8") as f:
                _MO_STATS["v"] = json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            _MO_STATS["v"] = {}
    return _MO_STATS["v"]


def _funding_about():
    from src import market_hours as _mh
    st_ = _mo_stats()
    lines = "".join(f"<p>{x}</p>" for x in (_mh.stats_line(a, "funding", st_) for a in ("BTC", "XRP")) if x)
    lines = lines.replace("after the open", "after a funding time")
    return ('<details class="mh-info" data-pat="MHI|funding"><summary>about funding times</summary><div class="mh-info-body">'
            '<p><b>What it is:</b> Perpetual futures (the no-expiry futures most crypto exchanges offer) charge a small "funding" payment between the people betting up (longs) and the people betting down (shorts) '
            'every 8 hours - at 00:00, 08:00 and 16:00 UTC on most exchanges (Hyperliquid pays every hour).</p>'
            '<p><b>Why crypto cares:</b> When funding is high, one side is paying a lot to keep its position open, and some traders close or flip positions around the payment time.</p>'
            '<p><b>What to watch:</b> It is a modest effect - the numbers below are close to a normal half hour.</p>'
            + (f'<div class="mh-stats"><b>How it has usually run (last 60 days, 1-minute candles):</b>{lines}</div>' if lines else '') + '</div></details>')


def build_market_hours_panel(asset_name):
    """Pull-down: when each market opens and closes (shown in YOUR time), whether it is open now, and the start / end of this Kalshi round."""
    from src import market_hours as _mh
    now = time.time()
    tz = _viewtz.get()
    today = dt.datetime.fromtimestamp(now, tz).date()

    def when(ts):
        d = dt.datetime.fromtimestamp(ts, tz)
        day = "today" if d.date() == today else ("tomorrow" if d.date() == today + dt.timedelta(days=1) else f"{d:%a %b} {d.day}")
        return f"{_viewtz.t12(d)} {day}"
    rows = _mh.market_rows(now)
    ri = _mh.round_info(now)
    ft = _mh.next_funding(now)
    k_start = dt.datetime.fromtimestamp(ri["start"], tz)
    k_end = dt.datetime.fromtimestamp(ri["end"], tz)
    k_nx = dt.datetime.fromtimestamp(ri["next_end"], tz)
    kal = (f'<div class="mh-kal"><b>This Kalshi market ({asset_name} 15-minute):</b> opened {_viewtz.t12(k_start)}, closes {_viewtz.t12(k_end)} '
           f'(<b>{_dur_txt(ri["end"] - now)}</b> left). The END CALL locks at {_viewtz.t12(dt.datetime.fromtimestamp(ri["lock"], tz))}'
           f'{" (already locked)" if now >= ri["lock"] else " (in " + _dur_txt(ri["lock"] - now) + ")"}. '
           f'The next market opens at {_viewtz.t12(k_end)} and closes at {_viewtz.t12(k_nx)} - a new one opens every 15 minutes, day and night, weekends too.'
           f'<br>Next perpetual-futures funding payment (most crypto exchanges, every 8 hours): {when(ft)} (in {_dur_txt(ft - now)}).'
           + _funding_about() + '</div>')
    body, last_group = [], None
    for r in rows:
        if r["group"] != last_group:
            body.append(f'<tr class="mh-grp"><td colspan="5">{r["group"]}</td></tr>')
            last_group = r["group"]
        st = r["state"]
        chip = {"OPEN": '<span class="mh-chip mh-open">OPEN</span>', "BREAK": '<span class="mh-chip mh-break">BREAK</span>'}.get(st, '<span class="mh-chip mh-closed">CLOSED</span>')
        if r["next_ts"]:
            nxt = f'{r["next_what"]} {when(r["next_ts"])} <span class="hint">(in {_dur_txt(r["next_ts"] - now)})</span>'
        else:
            nxt = '<span class="hint">never closes</span>'
        info = _mh.info_for(r["name"])
        usual, about = "", ""
        if info:
            key, more, what, why, watch = info
            st_ = _mo_stats()
            bx = (st_.get("BTC") or {}).get(key) if key else None
            xx = (st_.get("XRP") or {}).get(key) if key else None
            if bx and xx:
                bcls = "mh-hot" if bx["range_x"] >= 1.5 else ("mh-calm" if bx["range_x"] <= 0.95 else "")
                usual = f'<span class="mh-x {bcls}" title="how much bigger than a normal 30 minutes the first 30 minutes after this open are (BTC / XRP)">{bx["range_x"]:.1f}x / {xx["range_x"]:.1f}x</span>'
            lines = []
            for coin in ("BTC", "XRP"):
                if key:
                    lines.append(_mh.stats_line(coin, key, st_, before_close=(key == "nyse_close")))
                    for mk_ in more:
                        lines.append(_mh.stats_line(coin, mk_, st_).replace("after the open", "after the Sunday 6 PM ET open"))
            lines = [x for x in lines if x]
            stats_html = ('<div class="mh-stats"><b>How it has usually run (last 60 days, 1-minute candles):</b>' + "".join(f"<p>{x}</p>" for x in lines) +
                          '<div class="hint">"Normal" = the average of every 30-minute stretch, day and night. 1.0x = an ordinary half hour. Past behavior, not a promise.</div></div>') if lines else ""
            about = (f'<details class="mh-info" data-pat="MHI|{key or r["name"][:12]}"><summary>about this open</summary><div class="mh-info-body">'
                     f'<p><b>What it is:</b> {what}</p><p><b>Why crypto cares:</b> {why}</p><p><b>What to watch:</b> {watch}</p>{stats_html}</div></details>')
        body.append(f'<tr><td>{chip}</td><td><b>{r["name"]}</b>' + (f'<div class="hint">{r["note"]}</div>' if r["note"] else "") + about + f'</td><td>{r["hours"]}</td><td>{usual}</td><td>{nxt}</td></tr>')
    us = next((r for r in rows if r["name"].startswith("New York stock market")), None)
    cme = next((r for r in rows if r["name"].startswith("CME")), None)
    sm = []
    if us:
        sm.append(f'US stocks {us["state"]}' + (f' (opens {when(us["next_ts"])})' if us["state"] != "OPEN" and us["next_ts"] else f' (closes {when(us["next_ts"])})'))
    if cme:
        sm.append(f'CME futures {cme["state"]}')
    head = " - " + " &middot; ".join(sm) if sm else ""
    help_ = ('<div class="hint">Times are shown in YOUR time zone. Regular trading hours only; US stock holidays and early closes are built in, other countries\' holidays and CME holiday hours are not. '
             'Crypto never closes, but it often moves around the US stock open (9:30 AM ET) and close (4:00 PM ET), when the CME Bitcoin futures reopen after their daily pause, and at the funding times. '
             'Information only.</div>')
    return (f'<details class="mh-drop" data-pat="MH|{asset_name}"><summary><b>Market hours</b> <span class="hint">- when each market opens and closes{head}</span></summary>'
            f'{kal}<table class="press-table mh-table"><tr><th>Now</th><th>Market</th><th>Hours (its own clock)</th><th title="how much busier BTC / XRP usually are in the first 30 minutes after the open than in a normal 30 minutes">Usual swing at the open (BTC / XRP)</th><th>Next change (your time)</th></tr>{"".join(body)}</table>{help_}</details>')


def build_line_cross_panel(asset_name):
    """Pull-down: how many times the price crossed the target line this round and in each of the last 4 hours, with a picture of every hour."""
    with _lc_mu:
        rounds = list(_lc_rounds.get(asset_name, []))
        built = _lc_built[0]
    tz = _viewtz.get()
    ck = (asset_name, str(tz), built, int(time.time() // 5))
    if _lc_cache.get(asset_name, (None,))[0] == ck:
        return _lc_cache[asset_name][1]
    now = time.time()
    if not rounds:
        return (f'<details class="lc-drop" data-pat="LC|{asset_name}"><summary><b>Line crossings</b> <span class="hint">- collecting data...</span></summary></details>')
    hours = _lc.hour_buckets(rounds, tz, now, 4)
    cur = next((r for r in reversed(rounds) if r["close"] > now >= r["start"]), None)
    t12 = lambda ts: _viewtz.t12(dt.datetime.fromtimestamp(ts, tz))
    labels = {r["ticker"]: t12(r["close"]) for r in rounds}
    allrs = [r for h in hours for r in h["rounds"]]
    multi = sum(1 for r in allrs if len(r["cross"]) >= 2)
    head = ""
    _lbk = _lc.classify(cur, now) if cur is not None and cur.get("n", 0) >= 20 else None
    if _lbk:
        head += f' - <b>{_lbk["label"]}</b>'
    if cur is not None:
        side = "ABOVE" if cur["d_last"] > 0 else "BELOW"
        pa = f", {cur['pct_above']:.0f}% of the time above" if cur["pct_above"] is not None else ""
        head = f' - this round: <b>{len(cur["cross"])}</b> crossing{"" if len(cur["cross"]) == 1 else "s"}, now {side} the line{pa}'
    if allrs:
        head += f' &middot; last 4 hours: {multi} of {len(allrs)} rounds crossed 2+ times ({multi / len(allrs) * 100:.0f}%)'
    rows_html, pics = [], []
    for k, h in enumerate(hours):
        st = _lc.hour_stats(h, _lc_results)
        s = h["start"]
        nm = f"{_viewtz.t12(s)} - {_viewtz.t12(s + dt.timedelta(hours=1))}" + (" (this hour so far)" if k == 0 else "")
        if st["rounds"]:
            pa = "-" if st["pct_above"] is None else f'{st["pct_above"]:.0f}%'
            ky = "-" if not st["settled"] else f'{st["yes"]} of {st["settled"]} ({st["yes"] / st["settled"] * 100:.0f}%)'
            fl = "-" if not st["flip8_n"] else f'{st["flip8"]} of {st["flip8_n"]}'
            rows_html.append(f'<tr><td>{nm}</td><td>{st["rounds"]}</td><td><b>{st["cross"]}</b></td><td>{st["per_round"]:.1f}</td>'
                             f'<td>{st["calm"]} / {st["one"]} / {st["multi"]}</td><td>{pa}</td><td>{ky}</td><td>{fl}</td></tr>')
        else:
            rows_html.append(f'<tr><td>{nm}</td><td colspan="7" class="hint">no data</td></tr>')
        if st["rounds"]:
            pics.append(f'<div class="lc-hour"><div class="lc-hour-title"><b>{nm}</b> - {st["cross"]} crossings in {st["rounds"]} rounds</div>'
                        f'<div class="lc-hour-read">{_lc.read_sentence(st)}</div>'
                        + _lc.hour_svg(h, _lc_results, f"lc{asset_name}{k}", labels, now) + '</div>')
    table = ('<table class="press-table lc-table"><tr><th>Hour</th><th>Rounds</th><th title="times the price crossed the target line">Crossings</th><th>Per round</th>'
             '<th title="rounds with 0 crossings / exactly 1 / 2 or more">0 / 1 / 2+</th><th title="share of the time the price was above the line">Time above</th>'
             '<th title="how many of the settled rounds Kalshi ended YES">Kalshi YES</th>'
             '<th title="rounds that ended on the opposite side from where they stood at the 8th minute">Ended opposite of 8th-min side</th></tr>' + "".join(rows_html) + '</table>')
    help_ = ('<details class="lc-help" data-pat="LCHELP|' + asset_name + '"><summary>? how to read this</summary><div class="lc-help-body">'
             '<p><b>Crossing</b> = the live price goes from one side of the Kalshi target line (the "price to beat") to the other. It is about the LINE only, not about our call. '
             'The price has to get a little past the line (0.005%) to count, so hovering right on it is not counted over and over.</p>'
             '<p><b>The picture:</b> the gold line in the middle is the target line. Above it (green) the price is on the YES side; below it (red) the NO side. '
             'Dots mark each crossing, and each 15-minute round shows how it finished on Kalshi (YES or NO).</p>'
             '<p><b>Choppy</b> hours (3+ crossings per round) are where calls flip-flop; <b>trending</b> hours (under 1 per round) stay on one side. '
             '"Ended opposite of 8th-min side" counts rounds whose result was NOT the side the price was on at the 8th minute - the bigger it is, the less a lock at the 8th minute could be trusted that hour.</p>'
             '<p>Information only - it never feeds a call or a trade. Every settled round is saved to bot_state/line_cross_log.csv.</p></div></details>')
    chips, bp_sent = _bigger_picture(asset_name)
    read = _line_behavior_box(asset_name)
    bp = (f'<div class="bp-row"><span class="lb-l">BIGGER PICTURE</span> {chips}<div class="hint">{bp_sent} (chart read from the hour / day / week candles - information only, not a promise)</div></div>'
          if chips else "")
    html = (f'<details class="lc-drop" data-pat="LC|{asset_name}"><summary><b>Line crossings</b> <span class="hint">{head}</span></summary>'
            f'{read}{bp}{table}{"".join(pics)}{help_}</details>')
    _lc_cache[asset_name] = (ck, html)
    return html


def build_bookmap_panel(asset_name):
    """Pull-down under the chart: Bookmap-style heat map of the resting order book over the last 30 minutes (drawn in the browser from /bookmap_heat)."""
    return (f'<details class="bm-drop" data-pat="BM|{asset_name}"><summary><b>Order-book heat map</b> <span class="bm-status hint" data-asset="{asset_name}">checking...</span></summary>'
            f'<div class="bm-scheme" data-asset="{asset_name}"><span class="hint">Colors:</span> '
            f'<button class="size-mode-btn" data-k="heat" onclick="setBmScheme(\'heat\')">One heat color</button> '
            f'<button class="size-mode-btn" data-k="gr" onclick="setBmScheme(\'gr\')">Buys green / sells red</button> '
            f'<button class="size-mode-btn" data-k="bo" onclick="setBmScheme(\'bo\')">Buys blue / sells orange</button></div>'
            f'<div class="bm-sens" data-asset="{asset_name}"><span class="hint">Show:</span> '
            f'<button class="size-mode-btn" data-k="big" onclick="setBmSens(\'big\')">Only the biggest walls</button> '
            f'<button class="size-mode-btn" data-k="norm" onclick="setBmSens(\'norm\')">Balanced</button> '
            f'<button class="size-mode-btn" data-k="more" onclick="setBmSens(\'more\')">Show more</button></div>'
            f'<canvas class="bm-canvas" data-asset="{asset_name}" width="1100" height="340"></canvas>'
            f'<details class="bm-help" data-pat="BMHELP|{asset_name}"><summary>? what is this - how to read the heat map</summary><div class="bm-help-body">'
            f'<p><b>What it shows.</b> The last 30 minutes, left (oldest) to right (now). Up and down is price. The white line is the {asset_name} price. '
            f'Each colored stripe is money sitting in the order books of all the exchanges at that price, waiting to buy or sell.</p>'
            f'<p><b>Colors.</b> Only the biggest orders light up; everything small stays dark. <i>Show</i> picks how much counts: only the biggest walls, balanced, or more. In <i>One heat color</i>, blue is a little and yellow / red is a lot. '
            f'In the two-color modes, stripes below the price are <b>buyers waiting</b> (green or blue) and stripes above it are <b>sellers waiting</b> (red or orange).</p>'
            f'<p><b>How to use it.</b> A bright band that stays for minutes is a big wall. Price often slows or bounces near one, and when it breaks through, price can move fast. '
            f'A band that appears and then vanishes before price reaches it was likely pulled, so do not trust a wall until price has actually tested it.</p>'
            f'<p><b>Limits.</b> It only shows orders waiting in the books, not trades that already happened. It updates every 2 seconds and covers a band of about 0.4% around the price. '
            f'Exchanges quoted in USDT are lined up to the dollar price using their usual gap. The picture fills in over the first 30 minutes after the dashboard starts. '
            f'Bookmap feeds it instead if its add-on is connected.</p>'
            f'<p><b>Information only.</b> It is never used for the calls, the end call or buying.</p></div></details></details>')


def build_pattern_window(asset_name):
    """The live 'what is being formed' window: real candles with every detected pattern drawn on them, a timeframe
    switch, and a pull-down explanation for each pattern being formed. Information only - never used for buying."""
    from html import escape
    from src.pattern_detector import _structure as _st, _candle_patterns as _cp
    from src.pattern_overlay import render_window_svg
    if _mtf is None:
        return ""
    sym = {a["name"]: a["symbol"] for a in DASHBOARD_ASSETS}.get(asset_name)
    tf = _get_pat_tf(asset_name)
    cs = (_mtf.windows.get(sym) or {}).get(tf) if sym else None
    btns = "".join(
        f'<button class="{"size-mode-btn size-mode-active" if t == tf else "size-mode-btn"}" '
        f"onclick=\"setPatternTf('{asset_name}','{t}')\">{_MTF_NAMES[t]}</button>" for t in _PAT_WINDOW_TFS)
    _now_local = dt.datetime.now(_viewtz.get())
    clock = (f'<span class="live-clock" title="the time now (your device clock)"><span class="lc-t">{_viewtz.t12(_now_local, True)}</span>'
             f'<span class="lc-d">{_now_local:%a %b} {_now_local.day}</span></span>')
    head = (f'<div class="pat-win-head">{clock}<b>What is being formed - {asset_name} live pattern window</b> '
            f'<span class="hint">(information only - never used for buying)</span><div class="pat-win-tfs">{btns}</div></div>')
    if not cs or len(cs) < 25:
        return f'<div class="row pat-win">{head}<span class="flat">loading candle history...</span></div>'
    cs = _live_patched(cs, sym, tf)
    labels = _st(cs, tf) + _cp(cs, tf, max_age=3)
    from src.pattern_detector import TF_SECONDS as _TFS
    for _l in labels:
        if _l["family"] == "candle" and _l["status"] == "formed" and len(cs) >= 3 + _l.get("age", 0):
            _l["done_ts"] = cs[len(cs) - 2 - _l.get("age", 0)][0] + _TFS.get(tf, 0)
    from src.pattern_detector import FailTracker
    ghosts = _win_trackers.setdefault((asset_name, tf), FailTracker()).update(labels)
    svg, shown = render_window_svg(cs, _stable_labels(asset_name, tf, labels), title=f"{asset_name} - {_MTF_NAMES[tf]} candles (dashed outline = still forming)", events=_cpi_chart_events(cs), levels=_chart_levels(asset_name), sweeps=_sweep_stars(asset_name, tf, labels, cs))
    items = []
    for l in list(shown) + list(ghosts):
        e = _pat_explain(l["name"])
        key = e["key"] if e else ""
        st = _mtf.stats.summary(asset_name, tf, l["name"])
        col = {"bullish": "up", "bearish": "down"}.get(l["bias"], "flat")
        items.append(
            f'<details class="pat" data-pat="WIN|{asset_name}|{tf}|{escape(l["name"][:50])}|{l.get("age", 0)}" data-patkey="{escape(key)}" data-bias="{l["bias"]}">'
            f'<summary>{_pat_summary_html(l, col, _pat_rate_html(st, l["bias"]), asset_name, tf)}</summary>'
            f'<div class="pat-info"></div></details>')
    kept_html = ""
    try:
        from src.mtf_structure import _pkey as _pk_
        keep_s = min(3 * 86400, max(1800, 6 * HORIZONS_MIN.get(tf, 5) * 60))
        standing_ids = set()        # only the exact calls already on screen are skipped - an OLDER call of the same pattern still gets its row
        for l in shown:
            if l["status"] in ("formed", "confirmed"):
                e0 = _mtf.history.latest_event(asset_name, tf, l["name"])
                info0 = _mtf.history.completed_info(asset_name, tf, l["name"])
                if e0 is not None and info0 is not None and abs(e0["ts"] - info0[0]) <= 90:
                    standing_ids.add(e0["id"])
        kept = [e for e in _mtf.history.recent_completed(asset_name, tf, time.time() - keep_s)
                if e["bias"] in ("bullish", "bearish") and e["id"] not in standing_ids][:70]
        rows_, rows_bad = [], []
        for e in kept:
            ex = _pat_explain(e["name"])
            colk = {"bullish": "up", "bearish": "down"}.get(e["bias"], "flat")
            lab = {"name": e["name"], "bias": e["bias"], "status": e["status"], "pct": 1.0, "done_ts": e["ts"], "family": e["family"]}
            row = (f'<details class="pat" data-pat="KEPT|{e["id"]}" data-patkey="{escape(ex["key"] if ex else "")}" data-bias="{e["bias"]}">'
                   f'<summary>{_pat_summary_html(lab, colk, _pat_rate_html(_mtf.stats.summary(asset_name, tf, e["name"]), e["bias"]), asset_name, tf, ev=e)}</summary>'
                   f'<div class="pat-info"></div></details>')
            # only the calls that WORKED (and ones not scored yet - they still can) stay in the main list; failed / no-real-move ones go in their own pull-down
            (rows_bad if e.get("result") in ("miss", "flat") else rows_).append(row)
        n_hit = sum(1 for e in kept if e.get("result") == "hit")
        n_miss = sum(1 for e in kept if e.get("result") == "miss")
        n_flat = sum(1 for e in kept if e.get("result") == "flat")
        rows_, rows_bad = rows_[:14], rows_bad[:14]
        if rows_:
            kept_html = (f'<details class="kept-drop" data-pat="KEPTLIST|{asset_name}|{tf}"><summary class="hint">Recent completed calls that worked '
                         f'({n_hit}) - click to open</summary>'
                         f'<div class="hint kept-head">kept for {keep_s // 60} min - &#10003; worked (calls not scored yet are listed too)</div>'
                         + "".join(rows_) + '</details>')
        else:
            kept_html = '<div class="hint">No completed calls that worked yet on this timeframe.</div>'
        if rows_bad:
            kept_html += (f'<details class="kept-drop kept-bad" data-pat="KEPTBAD|{asset_name}|{tf}"><summary class="hint">Calls that failed or had no real move '
                          f'(<span class="down">{n_miss} failed</span> &middot; {n_flat} no move) - optional, click to open</summary>'
                          f'<div class="hint kept-head">&#10007; failed &middot; &#8211; no real move</div>' + "".join(rows_bad) + '</details>')
    except Exception as _ke:
        print(f"[history] kept calls failed: {_ke!r}")
    legend = "".join(items) + kept_html or '<span class="flat">no clear pattern forming on this timeframe right now</span>'
    try:
        cpi_strip = build_cpi_strip(asset_name)
    except Exception as _cpe:
        cpi_strip = ""
        print(f"[cpi] strip failed: {_cpe!r}")
    return (f'<div class="row pat-win" data-asset="{asset_name}">{head}{cpi_strip}'
            f"<div class=\"pat-chart-row\"><div class=\"pat-chart\" data-tf=\"{tf}\" onclick=\"expandPatternChart('{asset_name}')\" title=\"click to make it full-screen\">{svg}</div></div>"
            f'<div class="hint">click the chart to make it full-screen (Esc to close)</div>'
            f'<div class="pat-win-legend">{legend}</div></div>')


_PCT_TIP = ("how complete this pattern is. It climbs as the pattern builds and DROPS if price moves against it; "
            "100% = finished. Candle shapes are final when their candle closes.")


def _tstr(ts):
    """Viewer's local clock time with seconds, e.g. 2:05:12 PM"""
    return _viewtz.t12(dt.datetime.fromtimestamp(ts, _viewtz.get()), True)


_LONG_TFS = ("1h", "3h", "6h", "12h", "1d", "1w", "1M", "1Y")


def _tfull(ts, tf=None):
    """Completion time WITH the date where it matters: always for the hour-and-longer timeframes (a time alone is meaningless on
    a 6-hour or daily pattern), and for any pattern that completed on an earlier day. Today's short ones keep the seconds."""
    et = dt.datetime.fromtimestamp(ts, _viewtz.get())
    if tf in _LONG_TFS:
        return f"{et:%a %b} {et.day}, {_viewtz.t12(et)}"
    return f"{et:%a %b} {et.day}, {_viewtz.t12(et, True)}"


def _review_text(ev):
    """One short line once a call has been scored: what it predicted and what actually happened."""
    if not ev or ev.get("bias") not in ("bullish", "bearish") or ev.get("result") not in ("hit", "miss", "flat"):
        return ""
    dec = 4 if ev["asset"] == "XRP" else 2
    h = HORIZONS_MIN.get(ev["tf"], 5)
    word = "UP" if ev["bias"] == "bullish" else "DOWN"
    tgt, best, final, mv = ev.get("target"), ev.get("best"), ev.get("final"), ev.get("move") or 0.0
    call = f"Called {word} from {fmt_price(ev['price'], dec)}" + (f" toward {fmt_price(tgt, dec)}" if tgt else "")
    at = f", to {fmt_price(final, dec)}" if final else ""
    if ev["result"] == "hit":
        if tgt and ev.get("reached"):
            tail, cls = f"price reached the target ({fmt_price(best, dec)}). Successfully reached the prediction.", "up"
        elif tgt:
            tail, cls = f"price moved the right way ({mv:+.1f} ATR{at}) within {h} min; target not reached. Direction was right.", "up"
        else:
            tail, cls = f"price moved the right way ({mv:+.1f} ATR{at}) within {h} min. Successful call.", "up"
    elif ev["result"] == "miss":
        extra = f" It touched the target ({fmt_price(best, dec)}) but did not hold it." if (tgt and ev.get("reached")) else ""
        tail, cls = f"price went the other way ({mv:+.1f} ATR{at}). Prediction missed.{extra}", "down"
    else:
        extra = f" It touched the target ({fmt_price(best, dec)}) but did not hold it." if (tgt and ev.get("reached")) else ""
        tail, cls = f"no real move within {h} min ({mv:+.1f} ATR).{extra}", "flat"
    return f'<br><span class="pat-review {cls}">{call}; {tail}</span>'


def _res_mark(ev):
    """Green check / red X / grey dash beside the 100% bar: did the market go the way the pattern said, scored after its horizon."""
    if not ev or ev.get("bias") not in ("bullish", "bearish"):
        return ""
    h = HORIZONS_MIN.get(ev["tf"], 5)
    r = ev.get("result")
    if r == "hit":
        return f'<span class="pat-res up" title="WORKED: price moved {ev["move"]:+.1f} ATR in the pattern direction within {h} min">&#10003;</span>'
    if r == "miss":
        return f'<span class="pat-res down" title="FAILED: price moved {ev["move"]:+.1f} ATR (against it) within {h} min">&#10007;</span>'
    if r == "flat":
        return f'<span class="pat-res flat" title="no real move within {h} min ({ev["move"]:+.1f} ATR)">&#8211;</span>'
    return f'<span class="pat-res pending" title="result is scored {h} min after it completed">checking...</span>'


def _pat_summary_html(l, name_cls, rate_html, asset=None, tf=None, ev=None):
    """One pattern's headline: coloured status word, name, completion % with a little bar, then its track record."""
    from html import escape
    st = l["status"]
    pct = 1.0 if st in ("formed", "confirmed") else l.get("pct")
    pct_html = ""
    review_html = ""
    if pct is not None:
        pv = int(round(pct * 100))
        pct_html = (f'<span class="pat-pct st-{st}" title="{_PCT_TIP}"><span class="pat-pbar"><i style="width:{pv}%"></i></span>{pv}%</span>')
        if st in ("formed", "confirmed") and _mtf is not None and (ev is not None or (asset and tf)):
            try:
                _e = ev
                if _e is None:
                    _e = _mtf.history.latest_event(asset, tf, l["name"])
                    # only when it is the SAME completion that this line shows (not an older instance of the same pattern)
                    _info = _mtf.history.completed_info(asset, tf, l["name"])
                    if _e is not None and (_info is None or abs(_e["ts"] - _info[0]) > 90):
                        _e = None
                pct_html += _res_mark(_e)
                review_html = _review_text(_e)
            except Exception:
                pass
    nm = "pat-name pat-failed" if st == "failed" else f"pat-name {name_cls}"
    age = l.get("age", 0)
    age_html = f' <span class="hint">({age} candle{"s" if age != 1 else ""} ago)</span>' if age else ""
    done_html = ""
    if st in ("formed", "confirmed"):
        ts_done, carried = l.get("done_ts"), False
        if ts_done is None and asset and tf and _mtf is not None:
            try:
                info = _mtf.history.completed_info(asset, tf, l["name"])
                if info:
                    ts_done, carried = info
            except Exception:
                pass
        if ts_done:
            done_html = f' <span class="pat-done" title="when this pattern completed (your local time)">{"standing since" if carried else "completed"} {_tfull(ts_done, tf)}</span>'
    age_html += done_html
    star = '<span class="pat-star" title="liquidity sweep - a stop run">&#9733;</span> ' if l["name"].startswith("LIQUIDITY SWEEP") else ""
    return f'<span class="pat-st st-{st}">{st}</span> {star}<span class="{nm}">{escape(l["name"])}</span>{age_html} {pct_html}<br>{rate_html}{review_html}'


_win_trackers = {}


def _hist_counts_html(hit, miss, flat, pending, failed):
    bits = [f'<span class="up">{hit} worked</span>', f'<span class="down">{miss} failed</span>']
    if flat:
        bits.append(f"{flat} no move")
    if pending:
        bits.append(f"{pending} still checking")
    if failed:
        bits.append(f"{failed} fell apart")
    return " &middot; ".join(bits)


def _hist_event_html(e):
    from html import escape
    ex = _pat_explain(e["name"])
    key = ex["key"] if ex else ""
    col = {"bullish": "up", "bearish": "down"}.get(e["bias"], "flat")
    arrow = {"bullish": '<span class="up">&#9650;</span>', "bearish": '<span class="down">&#9660;</span>'}.get(e["bias"], "")
    if e["kind"] == "failed":
        out = '<span class="flat">fell apart before it finished</span>'
        stl = "failed"
    else:
        stl = e["status"]
        if e["bias"] not in ("bullish", "bearish"):
            out = '<span class="hint">context only - no up/down call to score</span>'
        elif e["result"] == "hit":
            out = f'<span class="up">&#10003; worked</span> <span class="hint">({e["move"]:+.1f} ATR in its direction after {HORIZONS_MIN.get(e["tf"], 5)} min)</span>'
        elif e["result"] == "miss":
            out = f'<span class="down">&#10007; failed</span> <span class="hint">({e["move"]:+.1f} ATR after {HORIZONS_MIN.get(e["tf"], 5)} min)</span>'
        elif e["result"] == "flat":
            out = f'<span class="flat">&#8211; no real move</span> <span class="hint">({e["move"]:+.1f} ATR after {HORIZONS_MIN.get(e["tf"], 5)} min)</span>'
        else:
            out = f'<span class="hint">checking... result {HORIZONS_MIN.get(e["tf"], 5)} min after it completed</span>'
    nm = "pat-name pat-failed" if e["kind"] == "failed" else f"pat-name {col}"
    return (f'<details class="pat hist-ev" data-pat="EV|{e["id"]}" data-patkey="{escape(key)}" data-bias="{e["bias"]}">'
            f'<summary><span class="hist-time">{_tfull(e["ts"], e["tf"])}</span> <span class="pat-st st-{stl}">{_MTF_NAMES.get(e["tf"], e["tf"])}</span> '
            f'{"<span class=" + chr(34) + "pat-star" + chr(34) + ">&#9733;</span> " if e["name"].startswith("LIQUIDITY SWEEP") else ""}'
            f'<span class="{nm}">{escape(e["name"])}</span> {arrow}<br>{out}{_review_text(e)}</summary><div class="pat-info"></div></details>')


def pattern_history_html(asset, start, end, level):
    """Body for one lazily-opened level of the history: an hour -> its four 15-minute windows; a window -> its patterns."""
    if _mtf is None:
        return ""
    evs = _mtf.history.events_between(asset, start, end)
    if level == "hour":
        parts = []
        for q in range(4):
            qs, qe = start + q * 900, start + (q + 1) * 900
            qe_ = [e for e in evs if qs <= e["ts"] < qe]
            done = [e for e in qe_ if e["kind"] == "complete"]
            hit = sum(1 for e in done if e["result"] == "hit"); miss = sum(1 for e in done if e["result"] == "miss")
            flat = sum(1 for e in done if e["result"] == "flat")
            pend = sum(1 for e in done if e["bias"] in ("bullish", "bearish") and e["result"] is None)
            failed = len(qe_) - len(done)
            lab = (dt.datetime.fromtimestamp(qs, _viewtz.get()).strftime("%I:%M").lstrip("0") + " - " +
                   dt.datetime.fromtimestamp(qe, _viewtz.get()).strftime("%I:%M %p").lstrip("0"))
            sub = (f'{len(done)} completed &middot; {_hist_counts_html(hit, miss, flat, pend, failed)}' if qe_ else "nothing completed")
            parts.append(f'<details class="hist-q" data-pat="HIST|{asset}|{int(qs)}|q" data-hist="{asset}|{int(qs)}|{int(qe)}|q">'
                         f'<summary><b>{lab}</b> <span class="hint">{sub}</span></summary><div class="hist-body"></div></details>')
        return "".join(parts)
    if not evs:
        return '<span class="hint">nothing completed in this window</span>'
    return "".join(_hist_event_html(e) for e in evs[:300])


def build_pattern_history_panel(asset_name):
    """Per coin: the last 24 Eastern hours, each openable into its 15-minute windows and then the patterns that completed in them."""
    if _mtf is None:
        return ""
    items = []
    for r in _mtf.history.hour_index(asset_name):
        if not (r["n"] or r["failed"]):
            continue
        items.append(
            f'<details class="hist-hour" data-pat="HIST|{asset_name}|{int(r["start"])}|h" data-hist="{asset_name}|{int(r["start"])}|{int(r["end"])}|hour">'
            f'<summary><b>{r["label"]}</b> <span class="hint">{r["n"]} completed &middot; '
            f'{_hist_counts_html(r["hit"], r["miss"], r["flat"], r["pending"], r["failed"])}</span></summary><div class="hist-body"></div></details>')
    body = "".join(items) or '<span class="hint">no completed patterns recorded yet - they appear here as they finish</span>'
    return (f'<div class="row pat-panel"><details class="pat-main" data-pat="HISTPANEL|{asset_name}">'
            f'<summary><b>Pattern history</b> <span class="hint">(what completed, when, and whether it worked - click an hour, then a 15-minute window)</span></summary>'
            f'{body}</details></div>')


def build_pattern_panel(asset_name):
    """Collapsible per-timeframe structure panel for one coin. Chips (always visible) show the bullish / neutral / bearish
    call for every timeframe; the table inside lists the patterns forming on each, each with its success rate and a
    pull-down tab (what it is, a picture, what it usually means)."""
    from html import escape
    if _mtf is None:
        return ""
    sym = {a["name"]: a["symbol"] for a in DASHBOARD_ASSETS}.get(asset_name)
    snap = _mtf.snapshot(sym) if sym else None
    if not snap:
        return ('<div class="row pat-panel"><span class="label">Chart structure by timeframe</span>'
                '<span class="value flat">loading candle history...</span></div>')
    chips = []
    rows = []
    for tf in ["1m", "5m", "15m", "1h", "3h", "6h", "12h", "1d", "1w", "1M", "1Y", "overall"]:
        a = snap.get(tf) or {"bias": "neutral", "labels": [], "n": 0}
        css, txt = _BIAS_UI[a["bias"]]
        chips.append(f'<span class="pat-chip {css}" title="{_MTF_NAMES[tf]}: {a["bias"]}">{_MTF_NAMES[tf]} {txt}</span>')
        cells = []
        for l in a["labels"]:
            e = _pat_explain(l["name"])
            key = e["key"] if e else ""
            st = l.get("stats")
            cells.append(
                f'<details class="pat" data-pat="{asset_name}|{tf}|{escape(l["name"][:50])}" data-patkey="{escape(key)}" data-bias="{l["bias"]}">'
                f'<summary>{_pat_summary_html(l, _BIAS_UI[l["bias"]][0], _pat_rate_html(st, l["bias"]), asset_name, tf)}</summary>'
                f'<div class="pat-info"></div></details>')
        note = a.get("note", "")
        body = "".join(cells) or f'<span class="flat">{escape(note) if note else "nothing forming"}</span>'
        rows.append(f'<tr><td class="pat-tf">{_MTF_NAMES[tf]}<br><span class="hint">{a["n"]} candles</span></td>'
                    f'<td><span class="pat-bias {css}">{txt}</span><br><span class="hint">score {a.get("score", 0):+.1f}</span></td>'
                    f'<td>{body}</td></tr>')
    return (f'<div class="row pat-panel"><details class="pat-main" data-pat="PANEL|{asset_name}">'
            f'<summary><b>Chart structure by timeframe</b> <span class="hint">(information only - never used for buying; click to open)</span>'
            f'{chips[-1]}</summary><div class="pat-chips">{"".join(chips)}</div>'
            f'<table class="pat-table"><tr><th>Timeframe</th><th>Call</th><th>Patterns forming (click one for what it is)</th></tr>'
            f'{"".join(rows)}</table></details></div>')


_TICKER_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _window_close_utc(ticker):
    """Close time (UTC) of a 15-minute window from its ticker, e.g. KXXRP15M-26OCT041800-00
    = 18:00 ET on 2026-10-04. None if the ticker doesn't parse."""
    m = re.match(r"[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)-", ticker or "")
    if not m or m.group(2) not in _TICKER_MONTHS:
        return None
    try:
        return dt.datetime(2000 + int(m.group(1)), _TICKER_MONTHS[m.group(2)], int(m.group(3)),
                           int(m.group(4)), int(m.group(5)), tzinfo=ZoneInfo("America/New_York")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


STREAK_DAY_START_HOUR = 0   # Eastern hour the "Today" row starts at; 0 = the first candle after midnight


def streak_windows_html(records, name):
    """Pull-down table under the streak: calls won / lost, win %, longest win and loss run, and real-money P&L for the
    current hour and the current 3 / 6 / 12-hour block (fixed blocks from midnight) and Today (since the start of the day, Eastern). Added 2026-10-10 per user request."""
    now = dt.datetime.now(dt.timezone.utc)
    et = now.astimezone(_viewtz.get())
    day_start = et.replace(hour=STREAK_DAY_START_HOUR, minute=0, second=0, microsecond=0)
    if day_start > et:
        day_start -= dt.timedelta(days=1)
    day_label = day_start.strftime("%I:%M %p").lstrip("0")
    def _block(n):
        """Fixed blocks counted from midnight (3h: 12, 3, 6, 9 ...; 6h: 12, 6 ...; 12h: 12 AM / 12 PM) that reset on each boundary."""
        s = et.replace(hour=(et.hour // n) * n, minute=0, second=0, microsecond=0)
        return s.astimezone(dt.timezone.utc), s.strftime("%I:%M %p").lstrip("0")
    # ROLLING look-backs (changed 2026-10-18 per request): the last hour / 3 / 6 / 12 / 24 hours counted back from RIGHT NOW, so nothing blanks out when an hour,
    # a 3-hour block or the day rolls over. "Today" (since the day start) stays as the last row.
    def _ago(h):
        s = now - dt.timedelta(hours=h)
        return s, _viewtz.t12(s.astimezone(_viewtz.get()))
    specs = [(f"Last hour (since {_ago(1)[1]})", _ago(1)[0]), (f"Last 3 hours (since {_ago(3)[1]})", _ago(3)[0]), (f"Last 6 hours (since {_ago(6)[1]})", _ago(6)[0]),
             (f"Last 12 hours (since {_ago(12)[1]})", _ago(12)[0]), (f"Last 24 hours (since {_ago(24)[1]})", _ago(24)[0])]
    specs.append((f"Today (since {day_label})", day_start.astimezone(dt.timezone.utc)))
    graded = []
    recorded = []          # every window the bot saw (called or not), by close time
    for h in records[-400:]:
        t = _window_close_utc(h.get("ticker"))
        if t is None:
            continue
        recorded.append((t, h.get("correct") is not None))
        if h.get("correct") is not None:
            graded.append((t, bool(h["correct"])))
    graded.sort()
    rows = []
    for label, start in specs:
        sel = [ok for t, ok in graded if start < t <= now]
        # every 15-minute window that has closed in this range, vs the ones the bot actually recorded
        total = int(now.timestamp() // 900) - int(start.timestamp() // 900)
        seen = [called for t, called in recorded if start < t <= now]
        no_call = sum(1 for called in seen if not called)
        offline = max(0, total - len(seen))
        n = len(sel)
        wins = sum(sel)
        best_w = best_l = run = 0
        prev = None
        for ok in sel:
            run = run + 1 if ok == prev else 1
            prev = ok
            if ok:
                best_w = max(best_w, run)
            else:
                best_l = max(best_l, run)
        if n:
            pct = wins / n * 100
            rec = f'<span class="up">{wins}</span> - <span class="{"down" if n - wins else "flat"}">{n - wins}</span>'
            pct_html = f'<span class="{"up" if pct >= 60 else ("down" if pct < 50 else "flat")}">{pct:.0f}%</span>'
        else:
            rec, pct_html = '<span class="flat">-</span>', '<span class="flat">-</span>'
            if total == 0:        # the block has only just started: say when its first round closes instead of showing blanks
                _nc = dt.datetime.fromtimestamp((int(now.timestamp() // 900) + 1) * 900, _viewtz.get())
                rec = f'<span class="hint">first round closes {_viewtz.t12(_nc)}</span>'
        pnl_html = '<span class="flat">-</span>'
        if name in LIVE_ASSETS:
            try:
                s = live_stats_since(start, name)
                if s["n"]:
                    pnl_html = f'<span class="{"up" if s["total"] >= 0 else "down"}">{s["total"]:+,.2f}</span> <span class="hint">({s["n"]})</span>'
            except Exception:
                pass
        rows.append(f'<tr><td>{label}</td><td>{total}</td><td>{rec}</td><td>{pct_html}</td>'
                    f'<td>{no_call or "-"}</td><td>{("<span class=warn>" + str(offline) + "</span>") if offline else "-"}</td>'
                    f'<td>{best_w or "-"}</td><td>{best_l or "-"}</td><td>{pnl_html}</td></tr>')
    return (f'<details class="streak-pull" data-pat="STREAK|{name}"><summary style="display:none;"></summary>'
            f'<table class="press-table"><tr><th>Window</th><th title="15-minute windows that have closed in this range">Windows</th>'
            f'<th title="calls the bot made that won - lost">Won - lost</th><th>Win %</th>'
            f'<th title="windows the bot ran but did not lock a call">No call</th>'
            f'<th title="windows with no record at all - the bot / computer was off">Bot off</th><th>Win run</th><th>Loss run</th>'
            f'<th title="real-money profit / loss (number of trades)">Real $</th></tr>{"".join(rows)}</table></details>')


KNIGHT_FX_PCT = 0.94   # at or above this Kalshi confidence: sparkles on the raised blade (YES) / blood dripping from the lowered one (NO)


def kalshi_arrow_html(name, d):
    """A knight whose SWORD is the arrow for Kalshi's prediction (the same price the flip meter is built from): sword out
    level at 50/50, raised toward straight-up as YES gets more likely, lowered toward straight-down as NO does; the blade and
    plume go red -> amber -> green with it. Drawn as plain SVG (no image files)."""
    conf, lean = d.get("confidence"), d.get("leaning")
    if conf is None or lean not in ("YES", "NO"):
        return ""
    yes = conf if lean == "YES" else 1 - conf
    ang = -(yes - 0.5) / 0.5 * 90.0              # CSS rotate: -90 = sword straight up (100% YES), +90 = straight down (100% NO)
    stops = [(0.0, (248, 81, 73)), (0.5, (227, 179, 65)), (1.0, (63, 185, 80))]
    for (a0, c0), (a1, c1) in zip(stops, stops[1:]):
        if yes <= a1:
            t = (yes - a0) / (a1 - a0)
            rgb = tuple(int(round(x0 + (x1 - x0) * t)) for x0, x1 in zip(c0, c1))
            break
    color = "#%02x%02x%02x" % rgb
    n = name
    fx = ""
    blood_blade = ""
    tipx, tipy = 160 + 80 * math.cos(math.radians(ang)), 94 + 80 * math.sin(math.radians(ang))
    if lean == "YES" and conf >= KNIGHT_FX_PCT:           # sword raised and Kalshi very sure: sparkles on the tip
        offs = [(0, -13), (11, -4), (-9, 7), (15, 11), (-13, -9), (4, 14)]
        fx = '<g transform="translate(%.1f %.1f)" style="color:#fff;">' % (tipx, tipy)
        for k, (ox, oy) in enumerate(offs):
            fx += ('<path class="fx-spark" style="animation-delay:-%.2fs;" transform="translate(%d %d)" '
                   'd="M0 -7 L1.8 -1.8 L7 0 L1.8 1.8 L0 7 L-1.8 1.8 L-7 0 L-1.8 -1.8 Z" fill="#ffffff" '
                   'stroke="currentColor" stroke-width="0.6"/>' % (0.17 * k, ox, oy))
        fx += '</g>'
    elif lean == "NO" and conf >= KNIGHT_FX_PCT:          # sword lowered and Kalshi very sure: blood on the blade, dripping off it
        cs_, sn_ = math.cos(math.radians(ang)), math.sin(math.radians(ang))
        for k, (dist, ox) in enumerate(((80, 0), (80, 6), (62, 3), (44, -3), (26, 2))):
            fx += ('<path class="fx-drop" style="animation-delay:-%.2fs;" transform="translate(%.1f %.1f)" '
                   'd="M0 -5 C4 1 4 6 0 6 C-4 6 -4 1 0 -5 Z" fill="#c1121f" stroke="#7a0b13" stroke-width="0.8"/>'
                   % (0.2 * k, 160 + dist * cs_ + ox, 94 + dist * sn_ + 4))
        blood_blade = (
            '<path d="M196 89 H224 L221 99 H199 Z" fill="#b3121b" fill-opacity="0.88"/>'
            '<path d="M178 89 H192 L190 99 H180 Z" fill="#8f0e16" fill-opacity="0.85"/>'
            '<path d="M226 89 L240 94 L226 99 Z" fill="#6e0a11"/>'
            '<path d="M204 99 Q205 105 203 111 M214 99 Q215 104 214 109 M186 99 Q187 103 186 107" stroke="#b3121b" stroke-width="2.4" stroke-linecap="round" fill="none"/>'
        )
    knight = (
        '<svg class="knight-svg" viewBox="0 0 240 197" width="200" height="164" style="color:' + color + ';">'
        '<defs>'
        '<linearGradient id="st-' + n + '" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#eef2f7"/><stop offset="0.45" stop-color="#9aa5b1"/><stop offset="1" stop-color="#454e5a"/></linearGradient>'
        '<linearGradient id="dk-' + n + '" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#7d8794"/><stop offset="0.5" stop-color="#b8c1cc"/><stop offset="1" stop-color="#4a5360"/></linearGradient>'
        '<linearGradient id="bl-' + n + '" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3b8cff"/><stop offset="1" stop-color="#0a2a5e"/></linearGradient>'
        '<radialGradient id="gl-' + n + '" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="currentColor" stop-opacity="0.55"/><stop offset="1" stop-color="currentColor" stop-opacity="0"/></radialGradient>'
        '</defs>'
        # aura + ground shadow
        '<circle cx="95" cy="104" r="96" fill="url(#gl-' + n + ')"/>'
        '<ellipse cx="95" cy="193" rx="54" ry="6" fill="#000" opacity="0.4"/>'
        # legs (armoured greaves, boots), knee plates
        '<path d="M74 146 H93 L91 186 H70 Z" fill="url(#dk-' + n + ')" stroke="#3b4350" stroke-width="2"/>'
        '<path d="M97 146 H116 L120 186 H99 Z" fill="url(#dk-' + n + ')" stroke="#3b4350" stroke-width="2"/>'
        '<ellipse cx="82" cy="162" rx="10" ry="8" fill="url(#st-' + n + ')" stroke="#3b4350" stroke-width="2"/>'
        '<ellipse cx="108" cy="162" rx="10" ry="8" fill="url(#st-' + n + ')" stroke="#3b4350" stroke-width="2"/>'
        '<path d="M64 186 H94 L97 194 H62 Z" fill="#2b313a" stroke="#14181f" stroke-width="1.5"/>'
        '<path d="M96 186 H126 L128 194 H93 Z" fill="#2b313a" stroke="#14181f" stroke-width="1.5"/>'
        # torso: broad shoulders tapering to the waist, breastplate with ridge and plate lines
        '<path d="M60 76 Q95 60 130 76 L119 130 H71 Z" fill="url(#st-' + n + ')" stroke="#3b4350" stroke-width="2.5"/>'
        '<path d="M95 66 V128" stroke="#4a5360" stroke-width="2"/>'
        '<path d="M76 100 Q95 108 114 100 M78 113 Q95 121 112 113" stroke="#4a5360" stroke-width="2" fill="none"/>'
        '<circle cx="95" cy="86" r="7.5" fill="currentColor" style="filter:drop-shadow(0 0 5px currentColor)"/>'
        # belt + short hip plates
        '<rect x="70" y="127" width="50" height="9" rx="2" fill="#8d6e3f" stroke="#4d3a1c" stroke-width="1.5"/><rect x="89" y="126" width="12" height="11" rx="2" fill="#d29922"/>'
        '<path d="M72 136 H93 L91 148 H74 Z M97 136 H118 L116 148 H99 Z" fill="url(#dk-' + n + ')" stroke="#3b4350" stroke-width="1.5"/>'
        # spiked pauldrons
        '<path d="M44 80 Q44 60 64 62 Q76 66 76 82 Q60 90 44 80 Z" fill="url(#st-' + n + ')" stroke="#3b4350" stroke-width="2.5"/>'
        '<path d="M114 82 Q114 66 126 62 Q146 60 146 80 Q130 90 114 82 Z" fill="url(#st-' + n + ')" stroke="#3b4350" stroke-width="2.5"/>'
        '<path d="M50 64 L55 46 L63 62 Z M127 62 L135 46 L140 64 Z" fill="#c9d1d9" stroke="#3b4350" stroke-width="1.5"/>'
        # shield (his left side): kite shield, rim, emblem in the sword colour
        '<path d="M54 82 L70 108" stroke="#3b4350" stroke-width="17" stroke-linecap="round" fill="none"/>'
        '<path d="M54 82 L70 108" stroke="url(#st-' + n + ')" stroke-width="12" stroke-linecap="round" fill="none"/>'
        '<circle cx="84" cy="112" r="9" fill="url(#st-' + n + ')" stroke="#3b4350" stroke-width="2.5"/>'
        '<path d="M30 80 H84 V120 Q57 156 30 120 Z" fill="url(#bl-' + n + ')" stroke="#d0d7de" stroke-width="3"/>'
        '<path d="M57 88 V138 M38 102 H76" stroke="currentColor" stroke-width="5" stroke-linecap="round"/>'
        '<path d="M87 106 V118 M91 108 V116" stroke="#4a5360" stroke-width="1.6"/>'
        # neck, horned helm with glowing visor and crest
        '<rect x="85" y="60" width="20" height="10" fill="#4a5360"/>'
        '<path d="M77 30 Q58 28 54 6 Q70 14 84 22 Z" fill="#dfe5ec" stroke="#3b4350" stroke-width="1.5"/>'
        '<path d="M113 30 Q132 28 136 6 Q120 14 106 22 Z" fill="#dfe5ec" stroke="#3b4350" stroke-width="1.5"/>'
        '<path d="M95 14 C86 -4 110 -8 120 6 C108 4 100 8 95 14 Z" fill="currentColor"/>'
        '<path d="M74 54 V34 Q74 12 95 12 Q116 12 116 34 V54 Q106 64 95 64 Q84 64 74 54 Z" fill="url(#st-' + n + ')" stroke="#3b4350" stroke-width="2.5"/>'
        '<rect x="78" y="34" width="34" height="9" rx="3" fill="#0b0f14"/>'
        '<rect x="81" y="36" width="10" height="4" rx="1.5" fill="currentColor" style="filter:drop-shadow(0 0 4px currentColor)"/>'
        '<rect x="99" y="36" width="10" height="4" rx="1.5" fill="currentColor" style="filter:drop-shadow(0 0 4px currentColor)"/>'
        '<path d="M95 43 V62" stroke="#4a5360" stroke-width="2"/>'
        # sword arm
        '<path d="M126 82 L148 104 L160 94" stroke="#3b4350" stroke-width="17" stroke-linecap="round" stroke-linejoin="round" fill="none"/>'
        '<path d="M126 82 L148 104 L160 94" stroke="url(#st-' + n + ')" stroke-width="12" stroke-linecap="round" stroke-linejoin="round" fill="none"/>'
        # the sword: pivots about the hand; blade + glow take the prediction colour
        '<g class="sword" style="transform-origin:160px 94px; transform:rotate(' + ("%.1f" % ang) + 'deg);">'
        '<rect x="138" y="91" width="28" height="6" rx="2" fill="#6b4f2a"/><circle cx="137" cy="94" r="5.5" fill="#d29922" stroke="#8d6e3f" stroke-width="1.5"/>'
        '<path d="M168 76 L173 80 V108 L168 112 L163 108 V80 Z" fill="#d29922" stroke="#8d6e3f" stroke-width="1.5"/>'
        '<path d="M174 89 H226 L240 94 L226 99 H174 Z" fill="currentColor" stroke="#f0f6fc" stroke-width="1.2" style="filter:drop-shadow(0 0 6px currentColor)"/>'
        '<path d="M176 94 H232" stroke="#ffffff" stroke-opacity="0.55" stroke-width="1.6"/>'
        + blood_blade +
        '</g>'
        '<circle cx="160" cy="94" r="10" fill="url(#st-' + n + ')" stroke="#3b4350" stroke-width="2.5"/>'
        + fx +
        '</svg>'
    )
    return ('<div class="kalshi-arrow" data-name="' + name + '" data-angle="' + ("%.1f" % ang) + '" data-color="' + color + '" '
            'title="Kalshi says ' + lean + ' at ' + ("%.1f%%" % (conf * 100)) + '">' + knight +
            '<div class="kalshi-arrow-lbl">Kalshi&#39;s prediction</div>'
            '<div class="kalshi-arrow-pct" style="color:' + color + '">' + lean + ' ' + ("%.0f%%" % (conf * 100)) + '</div></div>')


def _hour_label(hours_back):
    """e.g. '6:00 AM' - the top of the current (or an earlier) hour, Eastern, for the flip-count caption."""
    t = dt.datetime.now(_viewtz.get()).replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=hours_back)
    return t.strftime("%I:%M %p").lstrip("0")


def flip_counts(records):
    """(flipped, graded) over finished rounds that closed since the TOP OF THIS HOUR (Eastern, "1h") and since the top of
    the previous hour ("2h") - clock-aligned per user request 2026-10-10, so the count starts fresh at :00.
    'Flipped' = the round settled OPPOSITE the locked call - same definition as the
    'Flipped X/4 of the last 4 windows' line in the flip-scalp box. Added 2026-10-04."""
    now = dt.datetime.now(dt.timezone.utc)
    top = now.astimezone(ZoneInfo("America/New_York")).replace(minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
    out = {}
    for label, start in (("1h", top), ("2h", top - dt.timedelta(hours=1))):
        graded = flipped = 0
        for h in records[-40:]:
            if h.get("correct") is None:
                continue
            t = _window_close_utc(h.get("ticker"))
            if t is None or not (start < t <= now):
                continue
            graded += 1
            if h["correct"] is False:
                flipped += 1
        out[label] = (flipped, graded)
    return out


_render_cache = {}
_render_cache_lock = threading.Lock()


def render_html_cached(max_age=0.9):
    key = str(_viewtz.get()) + "|" + repr(sorted((getattr(_view_tf, "m", None) or {}).items()))      # each viewer's zone AND chart choices
    now = time.time()
    with _render_cache_lock:
        hit = _render_cache.get(key)
        if hit and now - hit[0] < max_age:
            return hit[1]
    html = render_html()
    with _render_cache_lock:
        _render_cache[key] = (time.time(), html)
        if len(_render_cache) > 60:
            for k in sorted(_render_cache, key=lambda k_: _render_cache[k_][0])[:-60]:
                _render_cache.pop(k, None)
    return html


def render_html():
    with _lock:
        snapshot = dict(_state)
    parts = []
    for name, d in snapshot.items():
        if "error" in d:
            parts.append(f'<div class="card"><div class="asset-name">{name}</div><div class="row">'
                         f'<span class="label">status</span><span class="value warn">{d["error"]}</span></div></div>')
            continue

        synthetic = d.get("synthetic", False)
        decimals = d.get("price_decimals", 4 if synthetic else 2)
        # live price colored by whether it's currently above/below THIS
        # window's target - added 2026-09-12 per user request, a quick
        # at-a-glance read of which side of the line price is on right now.
        spot_price_val, target_price_val = d.get("spot_price"), d.get("target_price")
        if spot_price_val is not None and target_price_val is not None:
            spot_vs_target_css = "up" if spot_price_val >= target_price_val else "down"
        else:
            spot_vs_target_css = ""
        direction_source = d.get("direction_source", "self_computed" if synthetic else "kalshi")
        target_label = "Window open (ref, no Kalshi market)" if synthetic else "Kalshi target"
        direction_label = {
            "kalshi": "Direction (Kalshi's price)",
            "self_computed": "Direction (self-computed, no market price)",
            "momentum": "Direction (our own momentum, not Kalshi's price)",
        }[direction_source]

        lean_class = "up" if d["leaning"] == "YES" else ("down" if d["leaning"] == "NO" else "flat")
        if d.get("sweep_just_fired"):
            sweep_html = '<span class="warn">*** SWEEP JUST DETECTED ***</span>'
        elif d["swept"]:
            sweep_html = '<span class="warn">still elevated (recent sweep window)</span>'
        else:
            sweep_html = '<span>clear</span>'

        # choppiness veto - only meaningful for assets in CHOPPINESS_VETO_THRESHOLD
        # (BTC currently). Shows the raw reading + whether it's currently
        # blocking a call, so a "NO CALL" is diagnosable instead of a mystery.
        choppiness_row_html = ""
        if d.get("choppiness_threshold") is not None:
            chop_val = d.get("choppiness")
            chop_thresh = d["choppiness_threshold"]
            if chop_val is None:
                chop_txt = '<span class="flat">warming up...</span>'
            elif d.get("chopped"):
                chop_txt = f'<span class="warn">{chop_val:.4f} (over {chop_thresh:.4f} - VETOING new calls, too choppy)</span>'
            else:
                chop_txt = f'<span>{chop_val:.4f} (under {chop_thresh:.4f} - clear)</span>'
            choppiness_row_html = (f'<div class="row"><span class="label">Choppiness '
                                    f'(last {CHOPPINESS_LOOKBACK_MINUTES}min)</span>'
                                    f'<span class="value">{chop_txt}</span></div>')

        # whale flow - big trades that actually executed on the tape (last 5min)
        whale = d.get("whale") or {}
        whale_row_html = ""
        def _m(x):
            return f'${x/1e6:.2f}M' if x >= 1e6 else f'${x/1e3:.0f}k'
        buy_usd, sell_usd = whale.get("buy_usd", 0), whale.get("sell_usd", 0)
        if buy_usd or sell_usd:
            net = whale.get("net_side")
            net_txt = {"buy": '<span class="up">net BUYING</span>',
                       "sell": '<span class="down">net SELLING</span>'}.get(net, '<span class="flat">balanced</span>')
            whale_val = (f'<span class="up">BUY {_m(buy_usd)}</span> ({whale.get("buy_n",0)}) '
                         f'vs <span class="down">SELL {_m(sell_usd)}</span> ({whale.get("sell_n",0)}) - {net_txt}')
        elif d.get("live_venues") and any("Yahoo" in v for v in d["live_venues"]):
            whale_val = '<span class="flat">n/a (no live trade feed for gold)</span>'
        else:
            whale_val = '<span class="flat">no whale-sized trades in the last 5min</span>'
        _vname = lambda k: _flow.VENUES.get(k, (k, ""))[0]
        whale_latest_html = ""
        if whale.get("recent"):
            _items = []
            for _ts, _usd, _side, _ven in whale["recent"]:
                _age = max(0, int(time.time() - _ts))
                _items.append(f'<span class="{"up" if _side == "buy" else "down"}"><b>{"&#9650; BUY" if _side == "buy" else "&#9660; SELL"} {_m(_usd)}</b></span> '
                              f'<span class="hint">{_age}s ago</span>')
            _big = whale.get("biggest_whale")
            _bigtxt = (f'<br><span class="hint">biggest in 5 min: {"BUY" if _big[2] == "buy" else "SELL"} {_m(_big[1])}</span>'
                       if _big else "")
            whale_latest_html = f'<div class="whale-latest">{" &nbsp;|&nbsp; ".join(_items)}{_bigtxt}</div>'
        _inc = d.get("incoming")
        incoming_marker = (f'<span class="incoming-alert" data-name="{name}" data-side="{_inc["side"]}" data-usd="{_inc["usd"]:.0f}" '
                           f'data-venues="{_inc["venues"]}" data-top="{_inc["top"]}" data-key="{name}::{_inc["ts"]:.0f}" style="display:none;"></span>'
                           if _inc else "")
        whale_alert_side = whale.get("alert_side")
        whale_marker = (f'<span class="whale-alert" data-whale-name="{name}" data-whale-usd="{whale.get("alert_usd") or 0:.0f}" '
                        f'data-whale-venue="{_vname(whale.get("alert_venue") or "")}" '
                        f'data-whale-side="{whale_alert_side}" data-whale-key="{name}::{whale.get("alert_ts")}" '
                        f'style="display:none;"></span>'
                        if whale_alert_side else "")
        # live adaptive threshold shown in the label - added 2026-09-30 per user
        # request ("it should be different depending on what kind of market
        # we're in") so it's visible that "whale" isn't one fixed number,
        # it's moving with how much is actually trading right now.
        whale_thresh = whale.get("display_threshold")
        thresh_txt = f' (currently {_m(whale_thresh)}+)' if whale_thresh else ''
        whale_row_html = (f'<div class="row row-tight"><span class="label">Whale trades - all exchanges (last 5min, executed){thresh_txt}</span>'
                          f'<span class="value">{whale_val}{whale_marker}{incoming_marker}{whale_latest_html}</span></div>')

        # resting order-book walls - big orders sitting near price, waiting
        bw = d.get("book_walls") or {}
        def _mw(x):
            return f'${x/1e6:.2f}M' if x >= 1e6 else f'${x/1e3:.0f}k'
        _all_mode = bool(bw.get("all_n"))
        ask_wall, bid_wall = (bw.get("all_ask_wall"), bw.get("all_bid_wall")) if _all_mode else (bw.get("ask_wall"), bw.get("bid_wall"))
        def _srcs(lst):
            return (' <span class="hint">(' + ', '.join(f'{_venue_label(v)} {_mw(u)}' for v, u in lst) + ')</span>') if lst else ''
        wall_parts = []
        if ask_wall:
            wall_parts.append(f'<span class="down">SELL wall {_mw(ask_wall[1])} @ {fmt_price(ask_wall[0], decimals)} '
                              f'(+{ask_wall[2]*100:.2f}%)</span>')
        if bid_wall:
            wall_parts.append(f'<span class="up">BUY wall {_mw(bid_wall[1])} @ {fmt_price(bid_wall[0], decimals)} '
                              f'(-{bid_wall[2]*100:.2f}%)</span>')
        if wall_parts:
            wall_val = ' &nbsp;|&nbsp; '.join(wall_parts)
        elif d.get("live_venues") and any("Yahoo" in v for v in d["live_venues"]):
            wall_val = '<span class="flat">n/a (no order book for gold)</span>'
        else:
            wall_val = '<span class="flat">no big walls near price right now</span>'
        book_alert_side = bw.get("alert_side")
        _aw = bw.get("alert_wall") or (0, 0, 0)
        _wsrc = f'{bw["all_n"]} exchanges' if _all_mode else 'Coinbase book'
        wall_marker = (f'<span class="wall-alert" data-wall-name="{name}" data-wall-usd="{_aw[1]:.0f}" data-wall-price="{_aw[0]:.6g}" data-wall-src="{_wsrc}" '
                       f'data-wall-side="{book_alert_side}" data-wall-key="{name}::{bw.get("alert_ts")}" '
                       f'style="display:none;"></span>'
                       if book_alert_side else "")
        book_walls_row_html = (f'<div class="row row-tight"><span class="label">Order-book walls '
                               f'(resting, within {BOOK_WALL_RANGE_PCT*100:.1f}%)</span>'
                               f'<span class="value">{wall_val}{wall_marker}</span></div>')

        # the underlying crypto market's OWN volume, RIGHT NOW - added
        # 2026-09-25 as a 24h figure, replaced 2026-09-26 per user request
        # ("the in time volume in the very moment... instead of the 24-hour
        # volume", which barely moves poll to poll). Every real trade
        # (Coinbase + Kraken combined) in the last RECENT_VOLUME_WINDOW_SECONDS,
        # not just whale-sized ones - explicitly the real spot market, never
        # to be confused with Kalshi's own prediction-market volume, which
        # this project doesn't track at all.
        vol_recent = d.get("volume_recent") or {}
        vol_recent_usd, vol_recent_n = vol_recent.get("usd", 0), vol_recent.get("n", 0)
        if vol_recent_n:
            volume_recent_val = f'{_mw(vol_recent_usd)} ({vol_recent_n} trade{"s" if vol_recent_n != 1 else ""})'
        else:
            volume_recent_val = '<span class="flat">quiet - no trades</span>'
        volume_recent_row_html = (f'<div class="row"><span class="label">{name} volume right now '
                                  f'(last {RECENT_VOLUME_WINDOW_SECONDS}s, all exchanges, not Kalshi)</span>'
                                  f'<span class="value">{volume_recent_val}</span></div>')

        # buy vs sell volume tug-of-war bar - added 2026-09-30, see CSS comment
        vol_tug = d.get("volume_tug") or {}
        tug_buy_pct = vol_tug.get("buy_pct")
        if tug_buy_pct is None:
            tug_row_html = (f'<div class="row tug-row"><span class="label">{name} buy vs sell '
                            f'(last {RECENT_VOLUME_WINDOW_SECONDS}s)</span>'
                            f'<span class="value"><span class="flat">quiet - no trades</span></span></div>')
        else:
            buy_w = round(tug_buy_pct * 100, 1)
            sell_w = round(100 - buy_w, 1)
            tug_row_html = (
                f'<div class="row tug-row"><span class="label">{name} buy vs sell '
                f'(last {RECENT_VOLUME_WINDOW_SECONDS}s) - which side is favored</span>'
                f'<div class="tug-bar"><div class="tug-bar-buy" style="width:{buy_w}%"></div>'
                f'<div class="tug-bar-sell" style="width:{sell_w}%"></div></div>'
                f'<div class="tug-bar-label"><span class="up">buy {buy_w:.0f}% '
                f'({_mw(vol_tug.get("buy_usd", 0))})</span>'
                f'<span class="down">sell {sell_w:.0f}% ({_mw(vol_tug.get("sell_usd", 0))})</span></div></div>')

        # 24h high/low for the underlying crypto market - added 2026-09-26
        # per user request. Real spot market range, not a Kalshi figure.
        daily_high, daily_low = d.get("daily_high"), d.get("daily_low")
        if daily_high is not None and daily_low is not None:
            daily_range_val = (f'<span class="up">high {fmt_price(daily_high, decimals)}</span> / '
                               f'<span class="down">low {fmt_price(daily_low, decimals)}</span>')
        else:
            daily_range_val = '<span class="flat">n/a</span>'
        daily_range_row_html = (f'<div class="row row-tight"><span class="label">{name} 24h high / low</span>'
                                f'<span class="value">{daily_range_val}</span></div>')

        micro_dir = d.get("micro_dir")
        if micro_dir == "up":
            micro_html = f'<span class="up">UP (last 30s, {d["micro_pct"]:+.4%})</span>'
        elif micro_dir == "down":
            micro_html = f'<span class="down">DOWN (last 30s, {d["micro_pct"]:+.4%})</span>'
        elif micro_dir == "flat":
            micro_html = f'<span class="flat">flat (last 30s)</span>'
        else:
            micro_html = '<span class="flat">warming up...</span>'

        regime = d.get("regime")
        regime_ret = d.get("regime_ret")
        regime_pct = f'{regime_ret:+.2%}' if regime_ret is not None else ''
        if regime == "BULL":
            regime_html = f'<span class="up">BULL ({regime_pct} / 4hr)</span>'
        elif regime == "BEAR":
            regime_html = f'<span class="down">BEAR ({regime_pct} / 4hr)</span>'
        elif regime == "NEUTRAL":
            regime_html = f'<span class="flat">NEUTRAL ({regime_pct} / 4hr)</span>'
        else:
            regime_html = '<span class="flat">warming up...</span>'

        my_call = d.get("my_call")
        if my_call == "YES":
            call_html = '<div class="big up">YES</div>'
        elif my_call == "NO":
            call_html = '<div class="big down">NO</div>'
        else:
            call_html = '<div class="big flat">NO CALL</div>'

        pred_price = d.get("predicted_close_price")
        pred_html = fmt_price(pred_price, decimals)
        bias = d.get("bias_correction", 0)
        bias_n = d.get("bias_samples", 0)
        bias_html = (f'<div class="lbl" style="margin-top:6px;">learned adj: {bias:+,.{decimals}f} (n={bias_n})</div>'
                     if bias_n >= 5 else '<div class="lbl" style="margin-top:6px;">learning... (n={})</div>'.format(bias_n))

        locked_call = d.get("locked_call")
        locked_price = d.get("locked_price")
        locked_secs = d.get("locked_seconds_left")
        if locked_secs is None:
            locked_when_html = '(waiting for a confident call to form this window)'
        else:
            lmins, lsecs = divmod(locked_secs, 60)
            locked_when_html = f'(locked with {lmins}m {lsecs}s left)'
        locked_call_html = {"YES": '<span class="up">YES</span>', "NO": '<span class="down">NO</span>'}.get(
            locked_call, '<span class="flat">not yet</span>' if locked_secs is None else '<span class="flat">NO CALL</span>')
        locked_price_html = fmt_price(locked_price, decimals)
        # voice announcement the MOMENT a call locks - added 2026-09-28 per
        # user request ("as soon as it calls the call"). Ticker as the key -
        # locking only ever happens once per window, so it's naturally unique.
        lock_alert_html = (f'<span class="lock-alert" data-lock-name="{name}" data-lock-call="{locked_call}" '
                           f'data-lock-key="{name}::{d.get("ticker","")}" '
                           f'data-lock-kp="{(format(d["locked_kalshi_price"], ".0%") if d.get("locked_kalshi_price") is not None else "")}" '
                           f'style="display:none;"></span>'
                           if d.get("just_locked") and locked_call else "")

        late_call = d.get("late_call")
        late_secs = d.get("late_locked_seconds_left")
        if late_secs is None:
            late_when_html = '(waiting for the last ~4.5min to lock this one)'
        else:
            lmins2, lsecs2 = divmod(late_secs, 60)
            late_when_html = f'(locked with {lmins2}m {lsecs2}s left)'
        late_call_html = {"YES": '<span class="up">YES</span>', "NO": '<span class="down">NO</span>'}.get(
            late_call, '<span class="flat">not yet</span>')
        late_price_html = fmt_price(d.get("late_predicted_price"), decimals)
        late_conf = d.get("late_confidence")
        late_flip = d.get("late_flip_chance")
        late_pct_html = (f'{late_conf:.1%} conf / {late_flip:.1%} flip chance'
                         if late_conf is not None and late_flip is not None else '--')

        END_STREAK_COINS = ("BTC", "XRP")     # both coins use the new end-call record book (per request 2026-10-15); the OLD history / streak data is still recorded in the background, just not shown
        if name in END_STREAK_COINS:
            with _end_hist_mu:
                _eh = list(_end_hist.get(name, []))
            _eg = [h for h in _eh if h.get("correct") is not None]
            correct_n, graded_n = sum(1 for h in _eg if h["correct"]), len(_eg)
            windows_resolved = len(_eh)
            no_call_n = len(_eh) - len(_eg)
        else:
            _eh = _history.get(name, [])
            correct_n, graded_n = d.get("track_record", (0, 0))
            windows_resolved = d.get("windows_resolved", 0)
            no_call_n = d.get("no_call_n", 0)
        _track_label = "Track record (end call locked at the 8th minute)" if name in END_STREAK_COINS else "Track record (locked calls)"
        _streak_label = "Current streak (end call)" if name in END_STREAK_COINS else "Current streak"
        no_call_note = f', {no_call_n} had no confident call (not graded)' if no_call_n else ''
        if windows_resolved == 0:
            track_html = 'no windows resolved yet'
        elif graded_n == 0:
            track_html = f'no graded calls yet - {windows_resolved} windows resolved{no_call_note}'
        else:
            track_html = (f'{correct_n}/{graded_n} correct ({correct_n/graded_n:.0%})'
                          f' - {windows_resolved} windows resolved{no_call_note}')

        best_hours_row_html = (f'<div class="row" style="flex-direction:column;align-items:flex-start;gap:4px;">'
                               f'<span class="label">Hourly win% (all hours)</span>'
                               f'{best_hours_html(_history.get(name, []))}</div>')

        if name in END_STREAK_COINS:
            streak_type, streak_count = get_streak(_eh)          # BTC: the streak counts the locked END CALLS only (new record book, started fresh)
        else:
            streak_type, streak_count = d.get("streak_type"), d.get("streak_count", 0)
        if streak_type == "win":
            # tiered streak effects, added 2026-09-13 per user request: smoke
            # starting at 5 in a row, a REAL drawn/animated flame (not an
            # emoji - upgraded same day per direct feedback) at 10+, growing
            # from 1 to 3 flame layers the longer the streak runs.
            smoke = '\U0001F4A8'  # Python \U escape, not a literal emoji character - see build_flame_svg note
            if streak_count >= 10:
                intensity = 1 + min(2, (streak_count - 10) // 10)
                effect_html = f'<div class="fire-streak">{build_flame_svg(intensity)}</div>'
            elif streak_count >= 5:
                effect_html = f'<div class="smoke-streak">{smoke}</div>'
            else:
                effect_html = ''
            streak_html = f'{effect_html}<span class="up">{streak_count} win{"s" if streak_count != 1 else ""} in a row</span>'
        elif streak_type == "loss":
            streak_html = f'<span class="down">{streak_count} loss{"es" if streak_count != 1 else ""} in a row</span>'
        else:
            streak_html = '<span class="flat">no streak yet</span>'

        last = d.get("last_result")
        last_html = ""
        if last:
            verdict = ("<span class=\"up\">RIGHT</span>" if last["correct"] else
                       "<span class=\"down\">WRONG</span>" if last["correct"] is False else "n/a")
            lp = fmt_price(last["predicted_price"], decimals)
            fp = fmt_price(last["final_price"], decimals)
            err = fmt_price(abs(last["price_error"]), decimals) if last["price_error"] is not None else None
            kp = last.get("locked_kalshi_price")
            bet = ""
            if kp and not synthetic and not HOSTED_READ_ONLY:       # hypothetical dollar results are not shown on the shared website
                contracts = 100 / kp
                payoff = contracts - 100 if last["correct"] else -100
                bet = f' | $100 bet @ {kp:.1%}: {payoff:+.2f}'
            last_html = (f'<div class="row"><span class="label">Last locked call ({last["ticker"]})</span>'
                         f'<span class="value">{last["call"] or "NO CALL"} {verdict} - guessed {lp}, '
                         f'settled ~{fp}'
                         f'{f" (off by {err})" if err is not None else ""}{bet}'
                         f'</span></div>')

        vs_btc_window_html = ""
        if synthetic:
            pct_opp = d.get("pct_opposite_this_window")
            pct_opp_n = d.get("pct_opposite_samples", 0)
            if pct_opp is not None:
                pct_opp_txt = f'{pct_opp:.0%} of the time so far this window ({pct_opp_n} min sampled)'
            else:
                pct_opp_txt = 'not enough of this window elapsed yet'
            vs_btc_window_html = (f'<div class="row"><span class="label">Opposite of BTC (this window, live)</span>'
                                  f'<span class="value">{pct_opp_txt}</span></div>')

        if direction_source == "momentum":
            mr = d.get("momentum_ratio")
            spot_price_txt = fmt_price(d.get("spot_price"), decimals)
            if d["leaning"] and mr is not None:
                # visual meter - filled proportionally up to 3x threshold (full bar),
                # same color as the direction. Purely a glance-at-it visual, the raw
                # number next to it is still the real, honest figure.
                meter_pct = min(mr / 3.0, 1.0) * 100
                meter_css = "meter-fill-up" if d["leaning"] == "YES" else "meter-fill-down"
                meter_html = (f'<span class="meter-track"><span class="meter-fill {meter_css}" '
                              f'style="width:{meter_pct:.0f}%"></span></span>')
                leaning_html = (f'{d["leaning"]} ({mr:.1f}x threshold, current price {spot_price_txt})'
                                f'{meter_html}')
            else:
                leaning_html = 'not enough data yet'
        else:
            leaning_html = f'{d["leaning"]} @ {d["confidence"]:.1%}' if d["leaning"] and d["confidence"] is not None else 'not enough data yet'

        # friend's fair-value model vs Kalshi's live price (added 2026-10-03) -
        # info only: shows when spot-vs-strike math disagrees with the book
        fair_html = ""
        if d.get("fair_yes") is not None and d.get("confidence") is not None and d.get("leaning"):
            fair_lean = d["fair_yes"] if d["leaning"] == "YES" else 1 - d["fair_yes"]
            gap = (fair_lean - d["confidence"]) * 100
            fair_color = "#4ade80" if gap >= 0 else ("#f87171" if gap <= -10 else "#fbbf24")
            fair_html = (f'<div class="row"><span class="label">Fair value ({d["leaning"]})</span>'
                         f'<span class="value" style="color:{fair_color}">{fair_lean:.1%} '
                         f'({gap:+.0f}c vs Kalshi, vol {d.get("fair_sigma_bps_min") or 0:.1f} bps/min, '
                         f'{d.get("quote_source", "list")} price)</span></div>')

        _fair_help = (
            '<details class="fair-help" data-pat="FAIRHELP|' + name + '"><summary>? what is this</summary><div class="fair-help-body">'
            '<b>Fair value</b> = our own maths for the chance the price finishes ABOVE the line (YES) or below it (NO), using only the live price, '
            'the line, and how jumpy the coin has been over the last 5 minutes. It does not look at Kalshi&#39;s price, the buyers or the sellers.<br><br>'
            'It also knows Kalshi settles on the AVERAGE price of the final 60 seconds, not the last tick.<br><br>'
            '<b>The number shown</b> is the fair chance of the side we are leaning. <b>+3c vs Kalshi</b> means our maths says that side is worth '
            '3 cents more than Kalshi is charging; a negative number means Kalshi is pricing it higher than our maths does. '
            '<b>vol</b> = how much the coin has been moving per minute (basis points).<br><br>'
            '<b>How accurate? (tested on about 1,700 finished rounds per coin, Sep 24 - Oct 14)</b><br>'
            + _fair_tables_html(name) +
            'A perfect model would show "said 90-100%" winning about 95%, "said 40-50%" about 45%, and so on. '
            'It is about as good as Kalshi&#39;s own price until the last few minutes, then Kalshi&#39;s price is better - so it is not an edge over the market. '
            + ('Weak spot: on XRP toss-ups (said 40-60%) YES wins more than it says - it leans toward NO by roughly 8 points.' if name == "XRP" else
               'BTC is well calibrated.') + '<br><br>'
            '<span class="hint">Information only - it is one half of the &quot;price vs the line&quot; feed and never places a trade.</span></div></details>')
        if d.get("fair_yes") is not None and d.get("confidence") is not None and d.get("leaning"):
            fair_top_html = (f'<div class="flip-fair"><div class="hint">FAIR VALUE ({d["leaning"]})</div>'
                             f'<div class="flip-fair-big" style="color:{fair_color}">{fair_lean:.0%}</div>'
                             f'<div class="flip-fair-sub" style="color:{fair_color}">{gap:+.0f}c vs Kalshi</div>'
                             f'<div class="hint">vol {d.get("fair_sigma_bps_min") or 0:.1f} bps/min &middot; {d.get("quote_source", "list")} price</div>{_fair_help}</div>')
        else:
            fair_top_html = (f'<div class="flip-fair"><div class="hint">FAIR VALUE</div><div class="flip-fair-big flat">--</div>'
                             f'<div class="hint">waiting for a call</div>{_fair_help}</div>')
        fair_html = ""

        boom_html = ""
        swing = d.get("dramatic_swing")
        if swing:
            swing_side = "YES" if swing == "up" else "NO"
            vs_call = d.get("dramatic_vs_call")
            # Color reflects whether the swing helps or hurts the locked call,
            # not raw price direction - "up" isn't always good news (e.g. if
            # you're locked NO). Green = with your call, red = against it.
            if vs_call == "against":
                css = "boom-down"
                text = f'*** BOOM - SWINGING {swing.upper()} ({swing_side}) - AGAINST YOUR LOCKED CALL ***'
            elif vs_call == "with":
                css = "boom-up"
                text = f'*** BOOM - SWINGING {swing.upper()} ({swing_side}) - CONFIRMING YOUR CALL ***'
            else:
                css = "boom-up" if swing == "up" else "boom-down"
                text = f'*** BOOM - SWINGING {swing.upper()} ({swing_side}) FOR SURE ***'
            boom_html = ""        # the "BOOM - SWINGING..." pill was removed 2026-10-14 per request; (css, text) kept above in case it comes back
        if d.get("boom_just_fired"):
            boom_html += (f'<span class="boom-alert" data-boom-name="{name}" data-boom-swing="{swing}" '
                          f'data-boom-vs-call="{d.get("dramatic_vs_call") or ""}" '
                          f'data-boom-key="{name}::{d.get("boom_alert_ts")}" style="display:none;"></span>')
        if CERTAIN_FLIP_ALERT_ENABLED and d.get("certain_flip_just_fired"):
            flip_to_side = "NO" if d.get("locked_call") == "YES" else "YES"
            boom_html += (f'<span class="certain-flip-alert" data-flip-name="{name}" '
                          f'data-flip-side="{flip_to_side}" '
                          f'data-flip-alert-key="{name}::{d.get("certain_flip_alert_ts")}" style="display:none;"></span>')

        experimental_html = ""
        if d.get("experimental"):
            experimental_html = (
                '<div class="row" style="background:#2d2606;border-radius:6px;padding:8px 12px;'
                'margin-bottom:8px;border:1px solid #d4b106;">'
                '<span style="color:#f0d040;font-weight:600;">EXPERIMENTAL - reset 2026-09-10. Ran '
                '~52% live at first; the fix was a choppiness/trend veto (only calls when gold is '
                'moving cleanly). Re-backtested on 703 real settled markets: 86% holdout accuracy '
                'with the veto, but it only calls ~40-50% of windows. History was wiped to start '
                'the track record fresh under the new logic. Still fewer validated windows than '
                'BTC/XRP.</span></div>'
            )

        rsi = rsi_tradingview(name) if name in ("BTC", "XRP") else None
        if rsi is None:
            rsi = d.get("rsi")
        tb_rsi_html = (f'<div class="tb-rsi tb-rsi-in" data-asset="{name}"><span class="tb-rsi-k">RSI 14 - TradingView, 1 min</span> '
                       f'<b class="tb-rsi-v {rsi_zone_cls(rsi)}">{rsi:.1f}</b> <span class="tb-rsi-n {rsi_zone_cls(rsi)}">{rsi_pressure_note(rsi)}</span>'
                       f'<span class="rsi-track"><i class="rsi-dot" style="left:{max(0.0, min(100.0, rsi)):.1f}%"></i></span></div>') if rsi is not None else \
                      f'<div class="tb-rsi tb-rsi-in" data-asset="{name}"><span class="tb-rsi-k">RSI 14 - TradingView, 1 min</span> <span class="hint">warming up...</span></div>'
        if rsi is None:
            rsi_html = '<span class="flat">warming up...</span>'
        else:
            if rsi <= 25:
                rsi_css, rsi_note = "rsi-hot rsi-hot-under", "WARNING - OVERSOLD"
            elif rsi >= 75:
                rsi_css, rsi_note = "rsi-hot rsi-hot-over", "WARNING - OVERBOUGHT"
            elif rsi <= 30:
                rsi_css, rsi_note = "warn", "oversold - watch for a possible flip"
            elif rsi >= 70:
                rsi_css, rsi_note = "warn", "overbought - watch for a possible flip"
            else:
                rsi_css, rsi_note = "flat", "neutral zone"
            rsi_html = (f'<span class="{rsi_css}">{rsi:.1f} ({rsi_note})</span>'
                        f'<span class="rsi-marker" data-rsi-name="{name}" data-rsi="{rsi:.1f}" style="display:none;"></span>')
        # candle chart REBUILT 2026-09-13, server-rendered this time (the
        # client-side JS version broke and was reverted earlier today).
        # Timeframe buttons fetch /chart?asset=X&tf=Y and swap just the
        # holder's innerHTML - no full-page poll needed to switch zoom.
        chart_svg_content = d.get("chart_svg") or ""
        chart_html = (
            f'<div class="chart-wrap">'
            f'<div class="chart-controls">'
            f'<button class="chart-tf-btn chart-tf-active" data-asset="{name}" data-tf="15" onclick="fetchChartTimeframe(this)">15m</button>'
            f'<button class="chart-tf-btn" data-asset="{name}" data-tf="30" onclick="fetchChartTimeframe(this)">30m</button>'
            f'<button class="chart-tf-btn" data-asset="{name}" data-tf="60" onclick="fetchChartTimeframe(this)">1h</button>'
            f'</div>'
            f'<div class="chart-svg-holder" data-asset="{name}">{chart_svg_content}</div>'
            f'</div>'
        )

        bet_pnl = d.get("bet_pnl")
        bet_pnl_row_html = ""
        if bet_pnl and not HOSTED_READ_ONLY:
            pnl_css = "up" if bet_pnl["total"] >= 0 else "down"
            bet_pnl_row_html = (
                '<div class="row"><span class="label">If you bet $100 on every locked call '
                '(real Kalshi price at lock time)</span>'
                f'<span class="value {pnl_css}">{bet_pnl["total"]:+,.2f} total '
                f'({bet_pnl["wins"]}/{bet_pnl["n"]} bets won)</span></div>'
            )
        # 1h / 24h windowed up-down % - added 2026-09-15 per user request,
        # right alongside the all-time paper total above. Computed fresh on
        # every render (cheap - just a filter over already-loaded history),
        # not carried through the snapshot, so it's never a poll-cycle stale.
        # Both are ROLLING windows ending right now (not anchored to
        # midnight) - based on each call's ticker-embedded open time, which
        # is Eastern Time (America/New_York, DST-aware), same as everywhere
        # else on this page that reads times off a ticker (e.g. Hourly win%).
        series_prefix = _ASSET_SERIES_BY_NAME.get(name)
        pnl_1h = compute_windowed_bet_pnl(_history.get(name, []), series_prefix, 3600)
        pnl_24h = compute_windowed_bet_pnl(_history.get(name, []), series_prefix, 24 * 3600)
        def _windowed_pnl_span(w):
            css = "up" if w["total"] >= 0 else "down"
            if w["n"] == 0:
                return '<span class="flat">no graded calls yet</span>'
            return f'<span class="{css}">{w["pct"]:+.1f}% ({w["total"]:+,.2f}, {w["n"]} bets)</span>'
        bet_pnl_windowed_html = "" if HOSTED_READ_ONLY else (        # hypothetical dollar results are not shown on the shared website
            '<div class="row"><span class="label">Paper up/down - last 1h</span>'
            f'<span class="value">{_windowed_pnl_span(pnl_1h)}</span></div>'
            '<div class="row"><span class="label">Paper up/down - last 24h</span>'
            f'<span class="value">{_windowed_pnl_span(pnl_24h)}</span></div>'
        )
        # checkpoint TEXT ROW removed 2026-08-31 per user report - seeing the
        # 5min/10min/live values printed out got misread as "the locked call
        # changed" when it hadn't, and led to a bad real-money sell. "Locked
        # first call" is the only call display now, same as before this
        # feature existed. The underlying checkpoint data collection and the
        # sound-alert-on-flip system both still run (the sound is a deliberate
        # choice, not a wall of numbers on screen) - just this visible row is
        # gone. Hidden marker keeps the flip sound working without printing
        # anything confusing.
        # fixed 2026-08-31: this used to only compare 3 sparse samples (a
        # 5min checkpoint, a 10min checkpoint, and whatever the current poll
        # happened to be) - a real flip that happened between those specific
        # moments could go completely undetected (found via a real user
        # report: "it didn't indicate the flips when they happened"). Now
        # compares the LIVE leaning against the locked call on EVERY poll
        # (every ~2s), so it catches a flip the moment it actually happens
        # instead of only at two fixed checkpoints.
        locked_call_for_flip = d.get("locked_call")
        live_leaning = d.get("leaning")
        flipped = (locked_call_for_flip is not None and live_leaning is not None
                   and live_leaning != locked_call_for_flip)
        flip_key = f'{name}::{d.get("ticker", "")}'
        checkpoint_html = (
            f'<span class="flip-alert" data-flip-key="{flip_key}" style="display:none;"></span>'
            if flipped else ""
        )

        # 2026-10-18: the flip meter now works off the END CALL (locked at the 8th minute), not the old live call: it is the chance that the END CALL ends up WRONG,
        # i.e. the round finishes on the other side. From the price-vs-the-line model's current odds. Before the lock / on a NO CALL round there is nothing to flip.
        flip_chance = None
        _end_state = "wait"
        _ek = f'{name}|{d.get("ticker", "")}'
        with _end_lock_mu:
            _elk = _end_locks.get(_ek)
        _fy = d.get("fair_yes")
        if _elk and _elk.get("side") in ("YES", "NO") and _fy is not None:
            flip_chance = (1 - _fy) if _elk["side"] == "YES" else _fy
            _end_state = "locked"
        elif _elk and _elk.get("side") == "NONE":
            _end_state = "none"
        elif _elk and _elk.get("side") == "LATE":
            _end_state = "late"
        flip_meter_html = ""
        if flip_chance is None:
            _msg = {"wait": "END CALL not locked yet - the flip meter starts at the 8th minute", "none": "no END CALL this round (too close to call) - nothing to flip",
                    "late": "no END CALL this round - nothing to flip", "locked": "flip meter waiting for the price model"}[_end_state]
            flip_meter_html = f'<div class="flip-wait">{_msg}</div>'
        if flip_chance is not None:
            pct = flip_chance * 100
            flip_source = "price-vs-line model"
            flip_window_key = f'{name}::{d.get("ticker", "")}'
            gauge_svg = build_flip_gauge_svg(pct)
            # user feedback 2026-09-02: the % alone was confusing - a flip
            # FROM what TO what? Locked call is the thing that would flip
            # (that's what "before close" means here - will the LOCKED call
            # still hold), so show both sides plainly.
            locked_call_for_flip = _elk["side"]
            if locked_call_for_flip in ("YES", "NO"):
                other_side = "NO" if locked_call_for_flip == "YES" else "YES"
                flip_direction_html = (
                    f'would flip your END CALL '
                    f'<span class="{"up" if locked_call_for_flip == "YES" else "down"}">{locked_call_for_flip}</span> '
                    f'&rarr; <span class="{"up" if other_side == "YES" else "down"}">{other_side}</span>'
                )
            else:
                flip_direction_html = 'no locked call yet this window'
            flip_meter_html = (
                f'<span class="flip-chance-value" data-flip-chance-key="{flip_window_key}" '
                f'data-flip-chance-pct="{pct:.2f}" style="display:none;"></span>'
                f'<div class="flip-gauge">{gauge_svg}</div>'
                f'<div class="flip-cap">Chance the END CALL flips before close ({flip_source}) &middot; {flip_direction_html}</div>'
            )

        try:
            tug_html = build_tug_panel(name, d)
        except Exception as _te:
            tug_html = ''
            print(f"[tug] panel failed: {_te!r}")
        try:
            pattern_panel_html = build_pattern_panel(name)
        except Exception:
            pattern_panel_html = ''
        try:
            pattern_hist_html = build_pattern_history_panel(name)
        except Exception as _he:
            pattern_hist_html = ''
            print(f"[history] panel failed: {_he!r}")
        try:
            pattern_window_html = build_pattern_window(name)
        except Exception as _we:
            pattern_window_html = ''
        _fc = flip_counts(_eh)
        def _fc_span(key):
            f_, g_ = _fc[key]
            css = "flat" if g_ == 0 else ("down" if f_ * 2 >= g_ and f_ > 0 else ("up" if f_ == 0 else "flat"))
            return f'<span class="{css}"><b>{f_}</b> of {g_}</span>'
        flip_counts_html = (f'<div class="flip-cap">Flipped against the call (finished rounds): '
                            f'this hour (since {_hour_label(0)}) {_fc_span("1h")} &nbsp;|&nbsp; '
                            f'since {_hour_label(1)} {_fc_span("2h")}</div>')
        try:
            _streak_pull = streak_windows_html(_eh, name)
        except Exception as _se:
            _streak_pull = ""
            print(f"[streak] history table failed: {_se!r}")
        flip_meter_html = (f'<div class="row flip-combo">{flip_meter_html}{tb_rsi_html}{flip_counts_html}'
                           f'{fair_top_html}<div class="flip-right"><div class="flip-streak"><span class="hint">{_streak_label}</span> '
                           f'<span class="streak-arrow" data-name="{name}" onclick="toggleStreakPull(&#39;{name}&#39;)">&#9662;</span><br>{streak_html}</div></div>'
                           f'<div class="flip-knight">{kalshi_arrow_html(name, d)}</div></div>'
                           f'<div class="streak-pull-wrap">{_streak_pull}</div>')
        flip_counts_html = ""
        try:
            pressure_html = build_pressure_bar(name)
        except Exception as _pe:
            pressure_html = ""
            print(f"[pressure] bar failed: {_pe!r}")

        SYNTHETIC_HISTORICAL_STATS = {
            "NEAR": ("2,250", "68.8%", "31.2%", "~3x"),
            "BNB": ("2,262", "75.8%", "24.2%", "~1.19x"),
            "XRP": ("3,893", "75.4%", "24.6%", "~1.28x"),
        }
        near_footer_html = ""
        if synthetic:
            sname = d.get("synthetic_name", name)
            stat = SYNTHETIC_HISTORICAL_STATS.get(sname)
            stat_line = (f'Historically ({stat[0]} real Kalshi BTC windows): {sname} settles the SAME direction '
                        f'as BTC\'s result {stat[1]} of the time, OPPOSITE {stat[2]} - most likely because '
                        f'{sname} is simply {stat[3]} more volatile than BTC as a % of price, not because it '
                        f'leads or predicts BTC.' if stat else '')
            near_footer_html = (
                f'<div class="ranges" style="margin-top:8px;">{stat_line} '
                f'"My call" above is real (same validated logic as BTC/ETH, scaled for {sname}\'s own measured '
                f"volatility, graded the same way) but is anchored to {sname}'s OWN window-open reference, not a "
                "real Kalshi contract.</div>"
            )

        mins, secs = divmod(d["seconds_left"], 60)
        line_diff_html = ""
        _sp, _tp = d.get("spot_price"), d.get("target_price")
        if _sp is not None and _tp:
            _df = _sp - _tp
            line_diff_html = (f'<div class="line-diff {"up" if _df >= 0 else "down"}" title="live price minus the line (the strike Kalshi settles against)">'
                              f'{"&#9650;" if _df >= 0 else "&#9660;"} {fmt_price(abs(_df), decimals)} {"ABOVE" if _df >= 0 else "BELOW"} the line '
                              f'<span class="hint">({_df / _tp * 100:+.3f}%)</span></div>')
        _left = max(0, int(d["seconds_left"]))
        _frac = min(1.0, _left / 900.0)
        _cl = "clock-hot" if _left <= 30 else ("clock-warn" if _left <= 120 else "")
        _stroke = "#f85149" if _left <= 30 else ("#d29922" if _left <= 120 else "#58a6ff")
        clock_html = (f'<div class="clock {_cl}" title="{d.get("ticker", "")}">'
                      f'<svg width="22" height="22" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="none" stroke="#30363d" stroke-width="3"/>'
                      f'<circle cx="12" cy="12" r="10" fill="none" stroke="{_stroke}" stroke-width="3" stroke-linecap="round" '
                      f'stroke-dasharray="{_frac * 62.83:.1f} 62.83" transform="rotate(-90 12 12)"/></svg>'
                      f'<span>{mins}:{secs:02d}</span></div>')
        ranges_html = "".join(
            f'<div>{h}min: {fmt_price(lo, decimals)} - {fmt_price(hi, decimals)}</div>' for h, (lo, hi) in sorted(d["ranges"].items())
        )

        live_pull_html = ""
        # real-money auto-buy on-site switch (added 2026-09-14, extended to
        # any LIVE_ASSETS member + configurable sizing + an 80% entry cap
        # 2026-09-22 per user request). Reads the live globals fresh on
        # every render so this is never one render-cycle stale. Arming needs
        # a second confirmed click (see armLive() in the page script) -
        # disarming is instant.
        live_trading_html = ""
        flip_badge_html = ""  # "FLIP" tag next to the calls (added 2026-09-25 per user
                                # request) - set below only when this window's real
                                # position (if any) came from the flip-scalp strategy,
                                # not the main buy-and-hold one, so it's never confused
                                # for "the first"/original call
        if name in LIVE_ASSETS:
            _live_reset_day_if_needed()
            with _live_lock:
                armed = _live_armed[name]
                pnl_today = _live_realized_pnl_today
                n_today = len(_live_trades_today)
                has_open = bool(_live_open_positions)
                main_pos = _live_open_positions.get(d.get("ticker"))
                main_pos_open = bool(main_pos and main_pos.get("strategy", "lock") == "lock")
                main_pos_ts = main_pos.get("ts") if main_pos_open else None
                main_last_result = _live_last_trade_result.get((name, "lock"))
                last_err = _live_last_error
                size_mode = _live_size_mode
                size_dollars = _live_size_dollars
                size_percent = _live_size_percent
                consecutive_losses = _live_consecutive_losses
                main_max_price = _live_max_entry_price
                main_sell_price = _live_main_sell_price
                buy_delay = _live_buy_delay_candles.get(name, 0)
            def _hold_time_str(ts_iso):
                # elapsed time since a real position was bought - added
                # 2026-09-25 per user request ("add ... time ... to both bots")
                try:
                    started = dt.datetime.fromisoformat(ts_iso)
                    elapsed = dt.datetime.now(dt.timezone.utc) - started
                    mins, secs = divmod(max(0, int(elapsed.total_seconds())), 60)
                    return f"{mins}m {secs}s"
                except (ValueError, TypeError):
                    return "?"
            # full live position summary, not just a hold-time line - added
            # 2026-09-27 per user request ("show it on the boxes... so I can
            # keep track of what's going on without having to look back and
            # forth" - between here and Kalshi's own site/app). The
            # gain/price figures come from poll_loop's main_pos_gain/
            # main_pos_price above, computed once per poll rather than
            # fetched fresh on every render.
            if main_pos_open:
                # kept deliberately terse - added 2026-09-27 per user request
                # ("you don't need to put all that worded information, just
                # push, but yes, and then my position, and then show how
                # positive or negative it is in the moment")
                _mg = d.get("main_pos_gain")
                _mcost = float(main_pos.get("cost_dollars") or 0)
                _gain_css = "up" if (_mg is not None and _mg >= 0) else ("down" if _mg is not None else "flat")
                _gain_txt = f'{_mg*_mcost:+.2f} ({_mg:+.1%})' if _mg is not None else '?'
                main_hold_html = (
                    f'<div class="box-position-line"><b class="position-tag">Position: '
                    f'{main_pos.get("side","?").upper()}</b> '
                    f'<span class="{_gain_css}">{_gain_txt}</span></div>'
                )
            elif main_last_result:
                # locked-in result of the last real trade, kept visible once
                # the position closes instead of just going blank - added
                # 2026-09-27 per user request ("if it flips, lock it in...
                # until it locks")
                _lp = float(main_last_result.get("payoff_dollars") or 0)
                _lp_css = "up" if _lp >= 0 else "down"
                main_hold_html = (
                    f'<div class="box-position-line"><b class="position-tag">Last position: '
                    f'{main_last_result.get("side","?").upper()}</b> '
                    f'<span class="{_lp_css}">{_lp:+.2f}</span></div>'
                )
            else:
                main_hold_html = ""
            loss_limit_desc = (f"{LIVE_LOSS_STREAK_LIMIT} real losses in a row" if size_mode == "percent"
                                else f"-${LIVE_LOSS_MULTIPLE * size_dollars:.0f} ({LIVE_LOSS_MULTIPLE}x "
                                     f"the ${size_dollars:.0f} bet size)")
            state_css = "down" if armed else "flat"
            state_label = "REAL MONEY: ARMED" if armed else "Real money: off (paper only)"
            pnl_css = "up" if pnl_today >= 0 else "down"
            err_html = f'<div class="hint" style="color:#f85149;">{last_err}</div>' if last_err else ""
            size_label = (f"${size_dollars:.0f}/trade" if size_mode == "dollars"
                          else f"{size_percent:.1f}% of balance/trade")
            # rolling 1h/24h up-down %, off the REAL settled trades log for
            # THIS asset - added 2026-09-15 per user request, per-asset
            # 2026-09-22. Both end "now" (not anchored to midnight) -
            # separate from the "Today: ... auto-disarms" line above, which
            # stays on the in-memory/UTC-day counters (combined across
            # assets) that actually drive the daily-loss-limit safety check
            real_1h = live_windowed_stats(3600, name)
            real_24h = live_windowed_stats(24 * 3600, name)
            def _real_windowed_span(w):
                if w["n"] == 0:
                    return '<span class="flat">no real trades yet</span>'
                css = "up" if w["total"] >= 0 else "down"
                return (f'<span class="{css}">{w["pct"]:+.1f}% ({w["total"]:+,.2f}, {w["n"]} trades, '
                        f'${w["volume"]:,.2f} volume)</span>')
            live_trading_html = f'''
          <div class="row live-trading-row">
            <span class="label">Real-money auto-buy ({size_label}, {f'sells at {main_sell_price:.0%}' if main_sell_price else 'buy &amp; hold'}, skips over {main_max_price:.0%}){main_hold_html}</span>
            <span class="value">
              <span class="{state_css}" id="live-trading-state-{name}">{state_label}</span>
              <button id="live-trading-btn-{name}" onclick="armLive('{name}', {str(not armed).lower()})"
                      class="{'live-btn-off' if armed else 'live-btn-on'}">{'Turn OFF' if armed else 'Arm real money'}</button>
              <div class="hint">Today (all live assets combined): <span class="{pnl_css}">{pnl_today:+.2f}</span> realized
                ({n_today} order{"s" if n_today != 1 else ""}{", position open" if has_open else ""}) -
                auto-disarms everything at {loss_limit_desc}
                {f' (currently {consecutive_losses} in a row)' if size_mode == "percent" and consecutive_losses else ''}</div>
              <div class="hint">Real up/down - last 1h: {_real_windowed_span(real_1h)}</div>
              <div class="hint">Real up/down - last 24h: {_real_windowed_span(real_24h)}</div>
              <div class="hint">Max entry price (skip buys over this):
                <input type="number" id="mainmaxprice-input-{name}"
                       value="{main_max_price * 100:.0f}"
                       min="1" max="99" step="1"
                       onkeydown="if(event.key==='Enter') setMainMaxPrice('{name}')"
                       style="width:60px;">%
                <button onclick="setMainMaxPrice('{name}')">Set</button>
                &nbsp;(currently: {main_max_price:.0%})</div>
              <div class="hint">Wait this many 1-min candles after the call before buying (0 = buy right away):
                <input type="number" id="buydelay-input-{name}" value="{buy_delay}"
                       min="0" max="{LIVE_BUY_DELAY_MAX_CANDLES}" step="1"
                       onkeydown="if(event.key==='Enter') setBuyDelay('{name}')"
                       style="width:60px;">
                <button onclick="setBuyDelay('{name}')">Set</button>
                &nbsp;(currently: {f'waits {buy_delay} candle' + ('s' if buy_delay != 1 else '') if buy_delay else 'buys right away'})</div>
              <div class="hint">Auto-sell when Kalshi price hits (0 = hold to settlement):
                <input type="number" id="mainsellprice-input-{name}"
                       value="{f'{main_sell_price * 100:.0f}' if main_sell_price else '0'}"
                       min="0" max="99" step="1"
                       onkeydown="if(event.key==='Enter') setMainSellPrice('{name}')"
                       style="width:60px;">%
                <button onclick="setMainSellPrice('{name}')">Set</button>
                &nbsp;(currently: {f'sells at {main_sell_price:.0%}' if main_sell_price else 'off - holds to settlement'})</div>
              <div class="hint">Bet size (shared across assets):
                <button onclick="setLiveSizeMode('{name}','dollars')"
                        class="{'size-mode-btn size-mode-active' if size_mode == 'dollars' else 'size-mode-btn'}">$ Amount</button>
                <button onclick="setLiveSizeMode('{name}','percent')"
                        class="{'size-mode-btn size-mode-active' if size_mode == 'percent' else 'size-mode-btn'}">% of Balance</button>
                <input type="number" id="livesize-input-{name}"
                       data-mode="{size_mode}"
                       value="{f'{size_dollars:.0f}' if size_mode == 'dollars' else f'{size_percent:.1f}'}"
                       min="{'1' if size_mode == 'dollars' else '0.1'}"
                       max="{'5000' if size_mode == 'dollars' else '100'}"
                       step="{'1' if size_mode == 'dollars' else '0.1'}"
                       onkeydown="if(event.key==='Enter') setLiveSizeValue('{name}')"
                       style="width:80px;">{'$' if size_mode == 'dollars' else '%'}
                <button onclick="setLiveSizeValue('{name}')">Set</button>
                &nbsp;(currently: {size_label})</div>
              {err_html}
            </span>
          </div>'''
            # flip-scalp strategy switch - added 2026-09-25 per user request,
            # its own separate arm switch (see _live_flip_profit_target note).
            with _live_lock:
                flip_armed = _live_flip_armed[name]
                flip_pos = _live_open_positions.get(d.get("ticker"))
                flip_pos_open = bool(flip_pos and flip_pos.get("strategy") == "flip")
                flip_peak = flip_pos.get("peak_gain_pct") if flip_pos_open else None
                flip_target = _live_flip_profit_target
                flip_max_price = _live_flip_max_entry_price
                flip_sell_mode = _live_flip_sell_mode
                flip_sell_price_target = _live_flip_sell_price_target
                flip_hold_if_certain = _live_flip_hold_if_certain
                flip_last_result = _live_last_trade_result.get((name, "flip"))
            # "cha-ching" win markers - added 2026-09-27 per user request.
            # Per-event key (the trade's own "ts", naturally unique) so the
            # client de-dupes properly - same pattern already fixed for the
            # whale/wall/boom alerts (a stale marker served across multiple
            # /data polls would otherwise celebrate the same win repeatedly).
            # loss markers - added 2026-09-27 per user request ("when it's
            # wrong... a middle finger... fuck you, Kalshi") - same per-event
            # key pattern, mirrored off the same last-trade-result data.
            win_alert_html = ""
            for _wr, _wstrat in ((main_last_result, "lock"), (flip_last_result, "flip")):
                if not _wr:
                    continue
                _payoff = float(_wr.get("payoff_dollars") or 0)
                if _payoff > 0:
                    win_alert_html += (f'<span class="win-alert" data-win-name="{name}" '
                                       f'data-win-amount="{_payoff:.2f}" '
                                       f'data-win-key="{name}::{_wstrat}::{_wr.get("ts")}" '
                                       f'style="display:none;"></span>')
                elif _payoff < 0:
                    win_alert_html += (f'<span class="loss-alert" data-loss-name="{name}" '
                                       f'data-loss-amount="{_payoff:.2f}" '
                                       f'data-loss-key="{name}::{_wstrat}::{_wr.get("ts")}" '
                                       f'style="display:none;"></span>')
            if flip_pos_open:
                flip_badge_html = '<span class="flip-badge" title="This window\'s real position is the flip-scalp entry, not the original call">FLIP</span>'
            flip_state_css = "down" if flip_armed else "flat"
            flip_state_label = "FLIP-SCALP: ARMED" if flip_armed else "Flip-scalp: off"
            # sell-target description used in both the label and the status
            # line - either a %-gain-on-cost target or a flat Kalshi price
            # level, depending on flip_sell_mode (see _live_flip_sell_mode note)
            flip_sell_desc = (f"{flip_sell_price_target:.0%} price level" if flip_sell_mode == "price"
                               else f"+{flip_target:.0%} gain")
            # live position summary - enriched 2026-09-27 per user request,
            # same idea/color as the main strategy's box (see main_hold_html
            # above) - shows what it actually flipped INTO, the live price,
            # and locks in the last result once it closes instead of going
            # blank ("in the flip side, lock in what it flips to... show the
            # ups and downs in the moment there, until it locks").
            # kept deliberately terse - added 2026-09-27 per user request
            # ("you don't need to put all that worded information, just
            # push, but yes, and then my position, and then show how
            # positive or negative it is in the moment")
            flip_status_html = ""
            if flip_pos_open:
                _fg = d.get("flip_pos_gain")
                _fcost = float(flip_pos.get("cost_dollars") or 0)
                _fgain_css = "up" if (_fg is not None and _fg >= 0) else ("down" if _fg is not None else "flat")
                _fgain_txt = f'{_fg*_fcost:+.2f} ({_fg:+.1%})' if _fg is not None else '?'
                flip_status_html = (
                    f'<div class="box-position-line"><b class="position-tag">Flipped to: '
                    f'{flip_pos.get("side","?").upper()}</b> '
                    f'<span class="{_fgain_css}">{_fgain_txt}</span></div>')
            elif flip_last_result:
                _flp = float(flip_last_result.get("payoff_dollars") or 0)
                _flp_css = "up" if _flp >= 0 else "down"
                flip_status_html = (
                    f'<div class="box-position-line"><b class="position-tag">Last flip: '
                    f'{flip_last_result.get("side","?").upper()}</b> '
                    f'<span class="{_flp_css}">{_flp:+.2f}</span></div>')
            # overall flip-scalp performance - added 2026-09-27 per user
            # request ("put the overall successful flips on the flip-scale
            # box"). All-time for this asset, off the real settled/sold CSV
            # log (same source as the main strategy's 1h/24h lines) - not
            # windowed, since "overall" means the whole track record.
            flip_overall = live_stats_since(dt.datetime.min.replace(tzinfo=dt.timezone.utc), name, strategy="flip")
            if flip_overall["n"]:
                _fo_css = "up" if flip_overall["total"] >= 0 else "down"
                flip_overall_html = (f'<div class="hint">Overall: <span class="{_fo_css}">'
                                     f'{flip_overall["wins"]}/{flip_overall["n"]} won '
                                     f'({flip_overall["win_pct"]:.0f}%), {flip_overall["total"]:+,.2f} '
                                     f'({flip_overall["pct"]:+.1f}%)</span></div>')
            else:
                flip_overall_html = '<div class="hint">Overall: <span class="flat">no flip trades settled yet</span></div>'
            # how often the market has actually been flipping lately - added
            # 2026-09-27 per user request ("put how many times it has
            # flipped in the last four windows"). Off the same in-memory
            # graded-history list the track record already uses (no extra
            # file read) - counts a window as "flipped" when the final
            # settled result came in OPPOSITE the locked call, over the last
            # 4 GRADED windows for this asset (skips any with no confident
            # call, same as the track record does).
            _recent_graded = [h for h in _history.get(name, []) if h.get("correct") is not None][-4:]
            _recent_flip_n = len(_recent_graded)
            _recent_flip_count = sum(1 for h in _recent_graded if h.get("correct") is False)
            if _recent_flip_n:
                _rf_css = "down" if _recent_flip_count >= 2 else "flat"
                flip_recent_html = (f'<div class="box-position-line">Flipped '
                                    f'<span class="{_rf_css}">{_recent_flip_count}/{_recent_flip_n}</span> '
                                    f'of the last {_recent_flip_n} windows</div>')
            else:
                flip_recent_html = ""
            live_trading_html += f'''
          <div class="row live-trading-row">
            <span class="label">Flip-scalp (buy the flip when flat, sell at {flip_sell_desc} or trailing profit, skips over {flip_max_price:.0%}){flip_status_html}{flip_recent_html}</span>
            <span class="value">
              <span class="{flip_state_css}" id="live-flip-state-{name}">{flip_state_label}</span>
              <button id="live-flip-btn-{name}" onclick="armLive('{name}', {str(not flip_armed).lower()}, 'flip')"
                      class="{'live-btn-off' if flip_armed else 'live-btn-on'}">{'Turn OFF' if flip_armed else 'Arm flip-scalp'}</button>
              {flip_overall_html}
              <div class="hint">Sell trigger:
                <button onclick="setFlipSellMode('{name}','gain')"
                        class="{'size-mode-btn size-mode-active' if flip_sell_mode == 'gain' else 'size-mode-btn'}">% Gain</button>
                <button onclick="setFlipSellMode('{name}','price')"
                        class="{'size-mode-btn size-mode-active' if flip_sell_mode == 'price' else 'size-mode-btn'}">Price Level</button>
                <input type="number" id="flipsell-input-{name}"
                       data-mode="{flip_sell_mode}"
                       value="{f'{flip_target * 100:.0f}' if flip_sell_mode == 'gain' else f'{flip_sell_price_target * 100:.0f}'}"
                       min="1" max="{'95' if flip_sell_mode == 'gain' else '99'}" step="1"
                       onkeydown="if(event.key==='Enter') setFlipSellValue('{name}')"
                       style="width:60px;">%
                <button onclick="setFlipSellValue('{name}')">Set</button>
                &nbsp;(currently: {flip_sell_desc})</div>
              <div class="hint">Max entry price (skip flip buys over this):
                <input type="number" id="flipmaxprice-input-{name}"
                       value="{flip_max_price * 100:.0f}"
                       min="1" max="99" step="1"
                       onkeydown="if(event.key==='Enter') setFlipMaxPrice('{name}')"
                       style="width:60px;">%
                <button onclick="setFlipMaxPrice('{name}')">Set</button>
                &nbsp;(currently: {flip_max_price:.0%})</div>
              <div class="hint">
                <button id="flip-hold-btn-{name}" onclick="setFlipHoldIfCertain('{name}', {str(not flip_hold_if_certain).lower()})"
                        class="{'size-mode-btn size-mode-active' if flip_hold_if_certain else 'size-mode-btn'}">
                  Hold if guaranteed flip: {'ON' if flip_hold_if_certain else 'OFF'}</button>
                &nbsp;skips the sell entirely while the big FLIP alert is active for this position, rides to full settlement instead</div>
            </span>
          </div>'''
            # protective-sell strategy switch - added 2026-09-26 per user
            # request, a THIRD independent box ("kind of like we have the
            # two... put another box for it, please, so I can turn that one
            # on and off too"). Sells an EXISTING real "lock" position early
            # once the flip signal crosses its OWN (stricter, configurable)
            # confidence bar - see _live_protect_min_confidence note above
            # for why this needs a different bar than flip-scalp's.
            with _live_lock:
                protect_armed = _live_protect_armed[name]
                protect_min_conf = _live_protect_min_confidence
                protect_pos = _live_open_positions.get(d.get("ticker"))
                protect_pos_open = bool(protect_pos and protect_pos.get("strategy", "lock") == "lock")
            protect_state_css = "down" if protect_armed else "flat"
            protect_state_label = "PROTECTIVE SELL: ARMED" if protect_armed else "Protective sell: off"
            # simplified 2026-09-27 per user request ("fix that... just give
            # it the in time... I don't care about the stupid other
            # information") - dropped the backtest-accuracy footnote, and
            # the live flip-chance reading now always shows (not just while
            # a position is open) so it's a genuine live "in the moment" read
            # against the bar, not just a static number.
            cur_chance = d.get("flip_chance")
            chance_css = "flat"
            if cur_chance is not None:
                chance_css = "down" if cur_chance >= protect_min_conf else "up"
            chance_txt = f'{cur_chance:.0%}' if cur_chance is not None else '?'
            live_trading_html += f'''
          <div class="row live-trading-row">
            <span class="label">Protective sell (sells your existing real position early if the flip signal against it hits {protect_min_conf:.0%})</span>
            <span class="value">
              <span class="{protect_state_css}" id="live-protect-state-{name}">{protect_state_label}</span>
              <button id="live-protect-btn-{name}" onclick="armLive('{name}', {str(not protect_armed).lower()}, 'protect')"
                      class="{'live-btn-off' if protect_armed else 'live-btn-on'}">{'Turn OFF' if protect_armed else 'Arm protective sell'}</button>
              <div class="hint">Confidence bar:
                <input type="number" id="protectthresh-input-{name}"
                       value="{protect_min_conf * 100:.0f}"
                       min="50" max="99" step="1"
                       onkeydown="if(event.key==='Enter') setProtectThreshold('{name}')"
                       style="width:60px;">%
                <button onclick="setProtectThreshold('{name}')">Set</button>
                &nbsp;(currently: {protect_min_conf:.0%})</div>
              <div class="box-position-line">Flip chance now: <span class="{chance_css}">{chance_txt}</span></div>
            </span>
          </div>'''
        if live_trading_html:
            _arm_main = _live_armed.get(name)
            _arm_flip = _live_flip_armed.get(name)
            _bits = [f'<span class="{"down" if _arm_main else "flat"}">real money {"ARMED" if _arm_main else "off"}</span>',
                     f'<span class="{"down" if _arm_flip else "flat"}">flip-scalp {"ARMED" if _arm_flip else "off"}</span>']
            with _live_lock:
                if _live_open_positions:
                    _bits.append('<span class="warn">position open</span>')
            live_pull_html = (f'<details class="live-pull" data-pat="LIVE|{name}"><summary><b>Purchasing options</b> '
                              f'<span class="hint">(click to open)</span> &nbsp; {" &nbsp;|&nbsp; ".join(_bits)}</summary>'
                              f'{live_trading_html}</details>')
        _rg = d.get("regime")
        _rgp = f'{d["regime_ret"]:+.2%}' if d.get("regime_ret") is not None else ""
        _mt = None
        try:
            _mt = market_trend(name)
        except Exception as _mte:
            print(f"[market] trend failed: {_mte!r}")
        _fp = lambda v: ("--" if v is None else f"{v:+.1f}%")
        if _mt:
            try:
                _hz = market_horizons(name)
            except Exception:
                _hz = []
            _hzd = {r[0]: r for r in _hz}
            def _now_txt(lbl):
                r = _hzd.get(lbl)
                return "" if not r else f'{lbl.replace(" min", "m").replace("1 hour", "1h")} <span class="{"up" if r[2] == "BULL" else "down" if r[2] == "BEAR" else "flat"}">{r[1]:+.2f}%</span>'
            _now_line = (' <span class="mk-now">right now: ' + ' &middot; '.join(x for x in (_now_txt("30 min"), _now_txt("1 hour")) if x) + '</span>') if _hz else ''
            _tbl = "".join(
                f'<tr class="mk-row" data-period="{lbl}" data-chg="{chg:.2f}" data-state="{st}" onclick="pickMarket(&#39;{name}&#39;, &#39;{lbl}&#39;)" title="click to show this period on the tag">'
                f'<td>{lbl}</td><td class="{"up" if st == "BULL" else "down" if st == "BEAR" else "flat"}"><b>{chg:+.2f}%</b></td>'
                f'<td class="{"up" if st == "BULL" else "down" if st == "BEAR" else "flat"}"><b>{st}</b></td><td class="hint">needs +/-{t:.2f}%</td></tr>'
                for lbl, chg, st, t in _hz)
            _mk_body = (f'<div class="mk-body"><b>What kind of market - from the last 15 minutes up to 7 days</b>'
                        f'<table class="mk-tbl"><tr><th>Period</th><th>Price change</th><th>Market</th><th></th></tr>{_tbl}</table>'
                        f'<div class="hint"><b>Click a row to show that period on the tag.</b> BULL = up more than the move shown, BEAR = down more than it, otherwise NEUTRAL (the needed move grows with the period). '
                        f'The big label counts the days (24h / 3 days); the short rows show what is happening right now. Information only.</div></div>') if _hz else ''
            _dflt = _hzd.get("1 hour") or (_hz[0] if _hz else None)
            if _dflt:
                regime_badge_html = (f'<details class="mk-drop" data-pat="MKT|{name}" data-coin="{name}"><summary><span class="mk mk-{_dflt[2].lower()}" '
                                     f'title="The market over the period you picked (click the tag, then click a period). BULL = up more than the move needed, BEAR = down more than it, '
                                     f'otherwise NEUTRAL. Information only."><span class="mk-state">{_dflt[2]}</span> MARKET '
                                     f'<small class="mk-per">{_dflt[0]} {_dflt[1]:+.2f}%</small></span></summary>{_mk_body}</details>')
            else:
                regime_badge_html = (f'<span class="mk mk-{_mt["label"].lower()}">{_mt["label"]} MARKET <small>24h {_fp(_mt["r24"])}</small></span>')
            # the little "4h: bear / bull ..." chip beside the market tag was REMOVED 2026-10-16 per request (it was the old short-term regime and read wrong)
        else:
            regime_badge_html = '<span class="mk mk-neutral" title="needs a few hours of price history">market: warming up</span>'

        def _tbchip(label, text, cls=""):
            return f'<span class="tb-chip"><span class="tb-k">{label}</span> <b class="{cls}">{text}</b></span>'
        _side = lambda v: ("up" if v == "YES" else "down" if v == "NO" else "flat")
        _chips = [_tbchip("Locked", d.get("locked_call") or "not yet", _side(d.get("locked_call"))),
                  _tbchip("Late call", d.get("late_call") or "not yet", _side(d.get("late_call"))),
                  _tbchip("My call", d.get("my_call") or "NO CALL", _side(d.get("my_call")))]
        if d.get("leaning") and d.get("confidence") is not None:
            _chips.append(_tbchip("Kalshi", f'{d["leaning"]} {d["confidence"]:.0%}', _side(d["leaning"])))
        top_chips_html = "".join(_chips)
        try:
            vol_side_html = build_volume_bar(name, "-side")
        except Exception:
            vol_side_html = ""
        bookmap_panel_html = build_bookmap_panel(name) if name in ("BTC", "XRP") else ""
        try:
            line_cross_html = build_line_cross_panel(name) if name in ("BTC", "XRP") else ""
        except Exception as _lce:
            line_cross_html = ""
            print(f"[line cross] panel failed: {_lce!r}")
        try:
            market_hours_html = build_market_hours_panel(name) if name in ("BTC", "XRP") else ""
        except Exception as _mhe:
            market_hours_html = ""
            print(f"[market hours] panel failed: {_mhe!r}")

        def _cw(cls, label, value_html, sub_html="", tip=""):
            return (f'<div class="cw {cls}" title="{tip}"><div class="cw-l">{label}</div><div class="cw-v">{value_html}</div>'
                    f'<div class="cw-s">{sub_html}</div></div>')
        _cside = lambda v: ("cw-up" if v == "YES" else "cw-down" if v == "NO" else "")
        _lc, _late, _mine = d.get("locked_call"), d.get("late_call"), d.get("my_call")
        _mine_html = {"YES": '<span class="up">YES</span>', "NO": '<span class="down">NO</span>'}.get(_mine, '<span class="flat">NO CALL</span>')
        if d.get("leaning") and d.get("confidence") is not None:
            _kal_html = f'<span class="{"up" if d["leaning"] == "YES" else "down"}">{d["leaning"]} {d["confidence"]:.0%}</span>'
        else:
            _kal_html = '<span class="flat">--</span>'
        call_windows_html = "".join([
            _cw("cw-locked " + _cside(_lc), "Locked first call", locked_call_html, (f'close {locked_price_html}' if _lc else 'waiting for a call'),
                "the first confident call this window, frozen"),
            _cw(_cside(_late), "Late call", late_call_html, (f'{late_conf:.0%} sure' if (_late and late_conf is not None) else 'locks ~4.5 min left'),
                "a second, independent read late in the window"),
            _cw(_cside(_mine), "My call", _mine_html, (f'close {pred_html}' if _mine else 'no confident call'), "the live call right now"),
            _cw("", "Kalshi says", _kal_html, "", "which way Kalshi's own price is leaning")])
        try:
            sweep_marker_html = sweep_forming_markers(name)
        except Exception as _swe:
            sweep_marker_html = ""
            print(f"[sweep alert] markers failed: {_swe!r}")
        parts.append(f'''
        <div class="card" data-asset="{name}">
          {sweep_marker_html}
          {experimental_html}
          <div class="topbar">
            <div class="tb-main">
              <div class="asset-name"><span class="asset-label asset-{name}">{name}</span> {flip_badge_html}{clock_html}{boom_html}{win_alert_html}{lock_alert_html}</div>
              {regime_badge_html}
              <div class="alert-dock" data-dock="{name}"></div>
              <div class="tb-right">{flip_meter_html}{flip_counts_html}</div>
              <div class="tb-price" title="{', '.join(d.get("live_venues") or []) or 'connecting...'}">
                <span class="lbl">Live</span> <span class="tb-big {spot_vs_target_css}">{fmt_price(d["spot_price"], decimals)}</span>
                <span class="lbl">{target_label}</span> <span class="tb-big">{fmt_price(d["target_price"], decimals) if d.get("target_price") is not None else "--"}</span>
                {line_diff_html}
              </div>
            </div>
          </div>
          <div class="cwrow">{call_windows_html}</div>
          <details class="info-drop" data-pat="INFO|{name}"><summary>Whale trades, order-book walls and {name} 24h high / low</summary>
          {whale_row_html}
          {book_walls_row_html}
          {daily_range_row_html}
          </details>
          <div class="tugrow">
            <div class="tugcol">
              {tug_html}
              {pressure_html}
            </div>
            <div class="tugvol">{vol_side_html}</div>
          </div>
          {pattern_window_html}
          {bookmap_panel_html}
          {line_cross_html}
          {market_hours_html}
          <details class="more-info" data-pat="MORE|{name}"><summary><b>More {name} information</b> <span class="hint">- chart structure, pattern history, regime, RSI, track record, predicted range (click to open)</span></summary>
          {pattern_panel_html}
          {pattern_hist_html}
          <div class="row"><span class="label">{direction_label}</span><span class="value {lean_class}">{leaning_html}</span></div>{fair_html}
          <div class="row"><span class="label">Short-term regime (4 hr - what the calls use)</span><span class="value">{regime_html}<span class="regime-value" data-regime-name="{name}" data-regime="{regime or ""}" style="display:none;"></span></span></div>
          <div class="row"><span class="label">Right now (last 30s)</span><span class="value">{micro_html}</span></div>
          {vs_btc_window_html}
          <div class="row"><span class="label">Spot momentum (1min)</span><span class="value">{f'{d["spot_ret"]:+.4%}' if d.get("spot_ret") is not None else '--'}</span></div>
          <div class="row"><span class="label">RSI (14, 1-min, TradingView-style: Coinbase candles, Wilder smoothing) - watch only, NOT used for calls</span><span class="value">{rsi_html}</span></div>
          <div class="row"><span class="label">Sweep status</span><span class="value">{sweep_html}</span></div>
          {choppiness_row_html}
          {volume_recent_row_html}
          <div class="row"><span class="label">{_track_label}</span><span class="value">{track_html}</span></div>
          {best_hours_row_html}
          {bet_pnl_row_html}
          {bet_pnl_windowed_html}
          {checkpoint_html}
          {last_html}
          <div class="ranges"><b>Predicted range (median/typical move, empirical):</b>{ranges_html}</div>
          </details>
          {near_footer_html}
          {"" if HOSTED_READ_ONLY else live_pull_html}
        </div>''')

    return "".join(parts) if parts else "<p>Waiting for first poll...</p>"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence default request logging

    def _ip(self):
        xf = (self.headers.get("X-Forwarded-For", "") or "").split(",")[0].strip()
        return xf or self.client_address[0]

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cookie_flags(self):
        return "; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if (self.headers.get("X-Forwarded-Proto", "") or "").lower() == "https" else "")

    def _identify(self):
        """(user name, is_admin) of whoever is asking, or None. A hosted copy knows people by their sign-in cookie, or by Basic auth for scripts (the owner's sync and followers)."""
        import base64 as _b64, hmac as _hmac
        self._master_ok = False
        if not HOSTED_PASSWORD:
            return ("local", True)
        m = re.search(r"(?:^|;\s*)sess=([^;]+)", self.headers.get("Cookie", "") or "")
        if m:
            who = _acc.check_session(m.group(1), HOSTED_PASSWORD)
            if who:
                return (who, who == "admin")
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            ip = self._ip()
            if _acc.throttled(ip):
                return None
            try:
                user, _, pw = _b64.b64decode(auth[6:]).decode("utf-8", "replace").partition(":")
            except Exception:
                return None
            if _hmac.compare_digest(pw.encode(), HOSTED_PASSWORD.encode()):
                self._master_ok = True
                return ("admin", True)
            if _acc.verify(user, pw):
                return (user.strip().lower(), False)
            _acc.note_fail(ip)
        return None

    def _hosted_gate(self):
        """True if the request may go on. Hosted copies: /healthz is open, /login is open, everything else needs a signed-in person; read-only copies refuse every trading / settings endpoint."""
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            body = b"ok"
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
            return False
        self._who = self._identify()
        if HOSTED_PASSWORD:
            if self._who is None:
                if path in ("/login", "/signup", "/terms"):
                    return True
                if "text/html" in (self.headers.get("Accept", "") or ""):
                    self.send_response(302); self.send_header("Location", "/login"); self.send_header("Content-Length", "0"); self.end_headers()
                else:
                    self._send(401, b'{"error": "sign in"}', "application/json")
                return False
            if self._who[0] != "admin":
                _acc.last_seen[self._who[0]] = time.time()
        if HOSTED_READ_ONLY and (path == "/arm_live" or (path.startswith("/set_") and path != "/set_pattern_tf")) or (HOSTED_READ_ONLY and path == "/bookmap_ingest"):
            body = b'{"error": "this copy is read-only"}'
            self.send_response(403); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
            return False
        return True

    def _read_form(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if 0 < n <= 8192 else b""
        except Exception:
            raw = b""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}

    def _admin_post(self, path):
        """The owner's /admin forms: add a person, reset a password, remove a person."""
        f = self._read_form()
        u, pw = (f.get("u") or "").strip().lower(), f.get("p") or ""
        msg, err = "", False
        if path == "/admin/add":
            if u in _acc.list_users():
                msg, err = "That user name already exists - use Change password.", True
            else:
                ok, m = _acc.add_user(u, pw)
                msg, err = (f"Added {u}. Give them their user name and password, and the website address." if ok else m), (not ok)
        elif path == "/admin/passwd":
            if u not in _acc.list_users():
                msg, err = "No such person.", True
            else:
                ok, m = _acc.add_user(u, pw)
                msg, err = (f"Password changed for {u}. They have been signed out." if ok else m), (not ok)
        elif path == "/admin/approve":
            msg = f"Approved {u}. They can sign in now with the password they chose." if _acc.approve_request(u) else "That request is gone."
            err = msg == "That request is gone."
        elif path == "/admin/deny":
            msg = f"Denied {u}." if _acc.deny_request(u) else "That request is gone."
            err = msg == "That request is gone."
        elif path == "/admin/delete":
            msg = f"Removed {u}. They are signed out." if _acc.delete_user(u) else "No such person."
            err = msg == "No such person."
        self._send(200, _accp.admin_page(_acc.list_users(), _acc.last_seen, msg, err, _acc.list_requests()).encode("utf-8"))

    def do_POST(self):
        """The Bookmap bridge add-on (this computer only) and - on the website copy only - the owner's bot (/sync_state, password required) post here."""
        if not self._hosted_gate():
            return
        _ppath = urllib.parse.urlparse(self.path).path
        if HOSTED_PASSWORD and _ppath == "/login":
            ip = self._ip()
            wait = _acc.throttled(ip)
            if wait:
                self._send(429, _accp.login_page(f"Too many wrong tries. Wait {wait // 60 + 1} minute(s) and try again.").encode("utf-8"))
                return
            f = self._read_form()
            u, pw = (f.get("u") or "").strip().lower(), f.get("p") or ""
            if u == "admin":
                import hmac as _hmac
                ok = _hmac.compare_digest(pw.encode(), HOSTED_PASSWORD.encode())
            else:
                ok = _acc.verify(u, pw)
            if ok:
                _acc.note_ok(ip)
                self.send_response(302)
                self.send_header("Location", "/admin" if u == "admin" else "/")
                self.send_header("Set-Cookie", "sess=" + _acc.make_session(u, HOSTED_PASSWORD) + self._cookie_flags() + f"; Max-Age={_acc.SESSION_DAYS * 86400}")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                _acc.note_fail(ip)
                self._send(401, _accp.login_page("Your request is still waiting for the owner to approve it." if (u and _acc.is_pending(u)) else "Wrong user name or password.").encode("utf-8"))
            return
        if HOSTED_PASSWORD and _ppath == "/signup":
            f = self._read_form()
            if f.get("agree") != "1":
                self._send(200, _accp.signup_page("Tick the box to agree to the terms first.", True).encode("utf-8"))
                return
            ok, m = _acc.request_signup(f.get("u"), f.get("p"), f.get("n"), self._ip(), terms=_accp.TERMS_DATE)
            self._send(200, (_accp.signup_page("Request sent.", False, done=True) if ok else _accp.signup_page(m, True)).encode("utf-8"))
            return
        if HOSTED_PASSWORD and _ppath in ("/admin/add", "/admin/passwd", "/admin/delete", "/admin/approve", "/admin/deny"):
            if self._who and self._who[1]:
                self._admin_post(_ppath)
            else:
                self._send(403, b"owner only", "text/plain")
            return
        if _ppath == "/sync_state":
            code = 404
            if ACCEPT_SYNC and HOSTED_PASSWORD and getattr(self, "_master_ok", False):
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    if n <= 0 or n > 9_000_000:
                        raise ValueError("bad size")
                    _apply_sync(json.loads(self.rfile.read(n)), mode="merge")
                    code = 200
                except Exception as _se:
                    print(f"[sync] bad update: {_se!r}")
                    code = 400
            self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()
            return
        parsed = urllib.parse.urlparse(self.path)
        code = 404
        if parsed.path == "/bookmap_ingest" and self.client_address[0] in ("127.0.0.1", "::1"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n <= 0 or n > 2_000_000:
                    raise ValueError("bad size")
                code = 200 if bookmap_ingest(json.loads(self.rfile.read(n))) else 202
            except Exception:
                code = 400
        self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()

    def do_GET(self):
        global _live_armed, _live_last_error, _live_size_mode, _live_size_dollars, _live_size_percent
        global _live_flip_profit_target, _live_flip_max_entry_price, _live_max_entry_price
        global _live_main_sell_price
        global _live_flip_sell_mode, _live_flip_sell_price_target, _live_flip_hold_if_certain
        global _live_protect_min_confidence
        if not self._hosted_gate():
            return
        _ck = self.headers.get("Cookie", "") or ""
        _m_tz = re.search(r"(?:^|;\s*)tz=(-?\d+)", _ck)
        _m_tzn = re.search(r"(?:^|;\s*)tzn=([^;]+)", _ck)
        _viewtz.set_view(urllib.parse.unquote(_m_tzn.group(1)) if _m_tzn else None, _m_tz.group(1) if _m_tz else None)
        _view_tf.m = {m_.group(1): m_.group(2) for m_ in re.finditer(r"(?:^|;\s*)ptf_([A-Za-z0-9]+)=([0-9A-Za-z]+)", _ck) if m_.group(2) in _PAT_WINDOW_TFS}
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/terms":
            self._send(200, _accp.terms_page().encode("utf-8"))
            return
        if HOSTED_PASSWORD and parsed.path == "/login":
            if self._who:
                self.send_response(302); self.send_header("Location", "/"); self.send_header("Content-Length", "0"); self.end_headers()
            else:
                self._send(200, _accp.login_page("", False).encode("utf-8"))
            return
        if HOSTED_PASSWORD and parsed.path == "/signup":
            self._send(200, _accp.signup_page("", False).encode("utf-8"))
            return
        if HOSTED_PASSWORD and parsed.path == "/logout":
            self.send_response(302); self.send_header("Location", "/login"); self.send_header("Content-Length", "0")
            self.send_header("Set-Cookie", "sess=; Max-Age=0" + self._cookie_flags())
            self.end_headers()
            return
        if HOSTED_PASSWORD and parsed.path == "/admin":
            if self._who and self._who[1]:
                self._send(200, _accp.admin_page(_acc.list_users(), _acc.last_seen, requests=_acc.list_requests()).encode("utf-8"))
            else:
                self._send(403, b"owner only", "text/plain")
            return
        if parsed.path in ("/debug_poll", "/debug_threads", "/flow_debug", "/debug_profile") and self.client_address[0] not in ("127.0.0.1", "::1"):
            self.send_response(404); self.end_headers()      # diagnostics: this PC only, never other devices on the network
            return
        if parsed.path == "/debug_poll":
            body = json.dumps({"loop_seconds_last": _poll_loop_t["last"], "loop_seconds_avg30": _poll_loop_t["avg"], "iterations": _poll_loop_t["n"]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/debug_threads":
            import sys as _sys, traceback as _tb
            names = {t.ident: t.name for t in threading.enumerate()}
            out = []
            for tid, fr in _sys._current_frames().items():
                out.append("=== " + names.get(tid, str(tid)) + "\n" + "".join(_tb.format_stack(fr)[-6:]))
            body = "\n".join(out).encode()
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/debug_profile":
            import cProfile, pstats, io as _io
            pr = cProfile.Profile()
            pr.enable()
            for _ in range(3):
                render_html()
            pr.disable()
            s_ = _io.StringIO()
            pstats.Stats(pr, stream=s_).sort_stats("cumulative").print_stats("kalshi_web_dashboard|line_cross|market_hours|multi_exchange_flow|pattern_|mtf_structure|viewtz", 40)
            body = s_.getvalue().encode()
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/sync_state":
            if not (ACCEPT_SYNC and HOSTED_PASSWORD):
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()
                return
            with _end_hist_mu:
                eh = {k: list(v) for k, v in _end_hist.items()}
            with _end_lock_mu:
                locks = dict(list(_end_locks.items())[-60:])
            body = json.dumps({"end_hist": eh, "end_locks": locks, "round_results": dict(list(_lc_results.items())[-800:]), "ts": time.time()}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store"); self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/flow_debug":
            # audit helper (this PC only): what the hub has recorded per exchange per minute, to check against each exchange's own numbers
            qs_ = urllib.parse.parse_qs(parsed.query)
            a_ = (qs_.get("asset") or ["BTC"])[0]
            per = {}
            with _flow.HUB.lock:
                for sec, vd in _flow.HUB.buckets.get(a_, {}).items():
                    for v, x in vd.items():
                        row = per.setdefault(v, {}).setdefault(sec // 60 * 60, [0.0, 0.0, 0])
                        row[0] += x[0]; row[1] += x[1]; row[2] += x[2]
            body = json.dumps({"asset": a_, "now": time.time(), "per_minute": per}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/bookmap_heat":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                payload = bookmap_heat((qs.get("asset") or [""])[0], float((qs.get("since") or ["0"])[0]))
            except Exception as _be:
                payload = {"cols": [], "status": {"connected": False, "text": "error: %r" % (_be,)}}
            body = json.dumps(payload).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store"); self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/chart_live":
            qs = urllib.parse.parse_qs(parsed.query)
            a_ = (qs.get("asset") or [""])[0]
            try:
                payload = chart_live_payload(a_) if a_ in LIVE_ASSETS else None
            except Exception as _ce:
                payload = None
                print(f"[chart_live] failed: {_ce!r}")
            body = json.dumps(payload or {}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store"); self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/pattern_history":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                a_ = (qs.get("asset") or [""])[0]
                s_, e_ = float((qs.get("start") or ["0"])[0]), float((qs.get("end") or ["0"])[0])
                lv_ = (qs.get("level") or ["q"])[0]
                ok = a_ in LIVE_ASSETS and lv_ in ("hour", "q") and 0 < e_ - s_ <= 3600
                html_ = pattern_history_html(a_, s_, e_, lv_) if ok else ""
            except Exception as _pe:
                html_, ok = "", False
                print(f"[history] endpoint failed: {_pe!r}")
            body = json.dumps({"ok": ok, "html": html_}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/set_pattern_tf":
            qs = urllib.parse.parse_qs(parsed.query)
            a_, t_ = (qs.get("asset") or [""])[0], (qs.get("tf") or [""])[0]
            ok = a_ in LIVE_ASSETS and t_ in _PAT_WINDOW_TFS
            if ok and not HOSTED_PASSWORD:
                _pat_window_tf[a_] = t_            # a private copy (one owner): also the default for a browser that has not chosen yet
            body = json.dumps({"ok": ok, "tf": t_ if ok else None}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if ok:
                self.send_header("Set-Cookie", f"ptf_{a_}={t_}; Path=/; Max-Age=31536000; SameSite=Lax")      # this browser only
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/build":
            body = BUILD_ID.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/data":
            body = json.dumps({"html": render_html_cached(), "build": BUILD_ID}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/locked":
            # added 2026-09-14 - a plain JSON read of just the locked-call
            # state (per asset: ticker, locked_call, locked_kalshi_price,
            # locked_seconds_left), sourced from the exact same _state an
            # external script (e.g. a real-money buy-on-lock script) can poll
            # without scraping the rendered HTML - guarantees it can never
            # see a different "locked" than what's actually on the page.
            with _lock:
                snap = {name: dict(d) for name, d in _state.items()}
            out = {
                name: {
                    "ticker": d.get("ticker"),
                    "locked_call": d.get("locked_call"),
                    "locked_kalshi_price": d.get("locked_kalshi_price"),
                    "locked_seconds_left": d.get("locked_seconds_left"),
                }
                for name, d in snap.items() if "error" not in d
            }
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/live_status":
            # real-money auto-buy status for ALL assets, polled by the
            # on-site switches - added 2026-09-14, multi-asset 2026-09-22.
            # Read-only; arming happens only via /arm_live.
            _live_reset_day_if_needed()
            with _live_lock:
                body = json.dumps({
                    "armed": dict(_live_armed),
                    "flip_armed": dict(_live_flip_armed),
                    "protect_armed": dict(_live_protect_armed),
                    "protect_min_confidence": _live_protect_min_confidence,
                    "flip_profit_target": _live_flip_profit_target,
                    "flip_max_entry_price": _live_flip_max_entry_price,
                    "flip_sell_mode": _live_flip_sell_mode,
                    "flip_sell_price_target": _live_flip_sell_price_target,
                    "flip_hold_if_certain": _live_flip_hold_if_certain,
                    "size_mode": _live_size_mode,
                    "size_dollars": _live_size_dollars,
                    "size_percent": _live_size_percent,
                    "max_entry_price": _live_max_entry_price,
                    "main_sell_price": _live_main_sell_price,
                    "buy_delay_candles": dict(_live_buy_delay_candles),
                    "loss_limit_mode": _live_size_mode,
                    "loss_limit_dollars": (LIVE_LOSS_MULTIPLE * _live_size_dollars
                                            if _live_size_mode == "dollars" else None),
                    "loss_limit_streak": (LIVE_LOSS_STREAK_LIMIT if _live_size_mode == "percent" else None),
                    "consecutive_losses": _live_consecutive_losses,
                    "realized_pnl_today": round(_live_realized_pnl_today, 2),
                    "trades_today": len(_live_trades_today),
                    "open_position": bool(_live_open_positions),
                    "last_error": _live_last_error,
                }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/arm_live":
            # on/off switch for a real-money auto-buy - added 2026-09-14,
            # multi-asset 2026-09-22 (pass asset=XRP|BTC). Arming REQUIRES
            # confirm=yes (the page's JS only ever sends that after a second,
            # explicit click) so a stray/prefetched GET can never arm real
            # trading by accident. Disarming needs no confirm - turning it
            # OFF should always be as fast and frictionless as possible.
            # pass strategy=flip to arm/disarm the flip-scalp switch instead
            # of the main buy-and-hold one - added 2026-09-25, its own
            # switch on purpose (see LIVE_FLIP_PROFIT_TARGET note above).
            # strategy=protect added 2026-09-26, same idea, its own third switch.
            qs = urllib.parse.parse_qs(parsed.query)
            asset_q = (qs.get("asset") or [""])[0]
            state = (qs.get("state") or [""])[0]
            confirm = (qs.get("confirm") or [""])[0] == "yes"
            strategy_q = (qs.get("strategy") or ["lock"])[0]
            target_dict = ({"flip": _live_flip_armed, "protect": _live_protect_armed}
                            .get(strategy_q, _live_armed))
            with _live_lock:
                if asset_q in target_dict:
                    if state == "on" and confirm:
                        target_dict[asset_q] = True
                        _live_last_error = None
                    elif state == "off":
                        target_dict[asset_q] = False
                armed_now = dict(_live_armed)
                flip_armed_now = dict(_live_flip_armed)
                protect_armed_now = dict(_live_protect_armed)
            _save_live_trading_state()  # survives a restart now - see LIVE_TRADING_STATE_FILE note above
            body = json.dumps({"armed": armed_now, "flip_armed": flip_armed_now,
                                "protect_armed": protect_armed_now}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/set_live_size":
            # bet-sizing config for the real-money auto-buy - added 2026-09-22
            # per user request (a flat dollar amount, or a % of the real
            # account balance, read fresh at trade time either way), redone
            # same day for a simpler one-input-plus-mode-toggle UI. Shared
            # across every armed asset, not per-asset - one account, one
            # sizing rule. Bounds-checked so a typo can't set a runaway size.
            # `dollars`/`percent` are OPTIONAL - a bare mode switch (tab
            # click) keeps whatever value was last set for that mode; only
            # typing a new number and hitting Set/Enter changes the value.
            qs = urllib.parse.parse_qs(parsed.query)
            mode = (qs.get("mode") or [""])[0]
            ok = mode in ("dollars", "percent")
            with _live_lock:
                if mode == "dollars":
                    _live_size_mode = "dollars"
                    if "dollars" in qs:
                        try:
                            val = float(qs["dollars"][0])
                        except ValueError:
                            ok = False
                        else:
                            if 1 <= val <= 5000:
                                _live_size_dollars = round(val, 2)
                            else:
                                ok = False
                elif mode == "percent":
                    _live_size_mode = "percent"
                    if "percent" in qs:
                        try:
                            val = float(qs["percent"][0])
                        except ValueError:
                            ok = False
                        else:
                            if 0.1 <= val <= 100:
                                _live_size_percent = round(val, 2)
                            else:
                                ok = False
                resp = {"ok": ok, "size_mode": _live_size_mode,
                        "size_dollars": _live_size_dollars, "size_percent": _live_size_percent}
            if ok:
                _save_live_trading_state()
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/set_flip_target":
            # flip-scalp sell target, configurable on-site - added 2026-09-25
            # per user request ("choose the percentage and what to sell at").
            # Shared across every armed asset, same as bet sizing. Bounds
            # 1-95% so a typo can't set something nonsensical.
            qs = urllib.parse.parse_qs(parsed.query)
            ok = True
            with _live_lock:
                try:
                    val = float((qs.get("pct") or [""])[0])
                except ValueError:
                    ok = False
                else:
                    if 1 <= val <= 95:
                        _live_flip_profit_target = round(val / 100.0, 4)
                    else:
                        ok = False
                resp = {"ok": ok, "flip_profit_target": _live_flip_profit_target}
            if ok:
                _save_live_trading_state()
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/set_flip_max_price":
            # flip-scalp max entry price cap, configurable on-site - added
            # 2026-09-25 per user request ("choose the price on the flip
            # too... don't want it [buying] at a lot"). Separate from the
            # main strategy's fixed LIVE_MAX_ENTRY_PRICE - only affects flip
            # entries. Bounds 1-99% (a price cap of 100% would mean no cap
            # at all, never allowed here).
            qs = urllib.parse.parse_qs(parsed.query)
            ok = True
            with _live_lock:
                try:
                    val = float((qs.get("pct") or [""])[0])
                except ValueError:
                    ok = False
                else:
                    if 1 <= val <= 99:
                        _live_flip_max_entry_price = round(val / 100.0, 4)
                    else:
                        ok = False
                resp = {"ok": ok, "flip_max_entry_price": _live_flip_max_entry_price}
            if ok:
                _save_live_trading_state()
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/set_main_max_price":
            # main strategy's max entry price cap, configurable on-site -
            # added 2026-09-28. Was a hardcoded 0.80 constant with NO
            # on-site control at all before this (unlike the flip
            # strategy's own cap) - fixed after a real losing morning
            # traced partly to expensive, poor-payout entries near that cap.
            # Bounds 1-99%, same as the flip cap.
            qs = urllib.parse.parse_qs(parsed.query)
            ok = True
            with _live_lock:
                try:
                    val = float((qs.get("pct") or [""])[0])
                except ValueError:
                    ok = False
                else:
                    if 1 <= val <= 99:
                        _live_max_entry_price = round(val / 100.0, 4)
                    else:
                        ok = False
                resp = {"ok": ok, "max_entry_price": _live_max_entry_price}
            if ok:
                _save_live_trading_state()
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/set_buy_delay":
            # candles (1-minute) to wait after a call locks before the real-money
            # buy starts, per asset - added 2026-10-04. 0 = buy right away.
            qs = urllib.parse.parse_qs(parsed.query)
            ok = True
            asset_q = (qs.get("asset") or [""])[0]
            with _live_lock:
                try:
                    n = int(float((qs.get("n") or [""])[0]))
                except ValueError:
                    ok = False
                else:
                    if asset_q in _live_buy_delay_candles and 0 <= n <= LIVE_BUY_DELAY_MAX_CANDLES:
                        _live_buy_delay_candles[asset_q] = n
                    else:
                        ok = False
                resp = {"ok": ok, "buy_delay_candles": dict(_live_buy_delay_candles)}
            if ok:
                _save_live_trading_state()
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/set_main_sell_price":
            # take-profit Kalshi price level for the main strategy - added
            # 2026-10-02. pct 1-99 sets it; 0 (or blank) turns it off.
            qs = urllib.parse.parse_qs(parsed.query)
            ok = True
            with _live_lock:
                try:
                    val = float((qs.get("pct") or ["0"])[0] or 0)
                except ValueError:
                    ok = False
                else:
                    if val == 0:
                        _live_main_sell_price = None
                    elif 1 <= val <= 99:
                        _live_main_sell_price = round(val / 100.0, 4)
                    else:
                        ok = False
                resp = {"ok": ok, "main_sell_price": _live_main_sell_price}
            if ok:
                _save_live_trading_state()
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/set_flip_sell_mode":
            # flip-scalp sell-trigger mode, configurable on-site - added
            # 2026-09-25 per user request. "gain" is the original %-gain-on-
            # cost target; "price" sells once Kalshi's own market price for
            # our side reaches pct (e.g. 65%) - a flat probability LEVEL,
            # which behaves consistently regardless of how cheap or
            # expensive the flip entry was (a %-gain target off a 20-cent
            # entry locks in far less of the available multiplier than the
            # same %-gain off an 80-cent entry). `pct` is OPTIONAL - a bare
            # mode switch keeps whatever price target was last set.
            qs = urllib.parse.parse_qs(parsed.query)
            mode = (qs.get("mode") or [""])[0]
            ok = mode in ("gain", "price")
            with _live_lock:
                if ok:
                    _live_flip_sell_mode = mode
                if "pct" in qs:
                    try:
                        val = float(qs["pct"][0])
                    except ValueError:
                        ok = False
                    else:
                        if 1 <= val <= 99:
                            _live_flip_sell_price_target = round(val / 100.0, 4)
                        else:
                            ok = False
                resp = {"ok": ok, "flip_sell_mode": _live_flip_sell_mode,
                        "flip_sell_price_target": _live_flip_sell_price_target}
            if ok:
                _save_live_trading_state()
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/set_flip_hold_if_certain":
            # "hold if guaranteed flip" toggle, added 2026-09-25 per user
            # request - when on, check_flip_position_exit() skips selling
            # entirely while the certain-flip signal is active for that
            # ticker, riding to the full $1 settlement instead of cashing
            # out early. Off by default. No confirm needed either
            # direction - unlike arming real money itself, this only
            # changes an already-armed strategy's exit behavior.
            qs = urllib.parse.parse_qs(parsed.query)
            state = (qs.get("state") or [""])[0]
            with _live_lock:
                if state in ("on", "off"):
                    _live_flip_hold_if_certain = (state == "on")
                resp = {"ok": state in ("on", "off"), "flip_hold_if_certain": _live_flip_hold_if_certain}
            _save_live_trading_state()
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/set_protect_threshold":
            # protective-sell confidence bar, configurable on-site - added
            # 2026-09-26 per user request. Bounds 50-99% - below 50% would be
            # weaker than a coin flip, never useful; 100% would mean it can
            # never fire at all. Shared across assets, same as everything else.
            qs = urllib.parse.parse_qs(parsed.query)
            ok = True
            with _live_lock:
                try:
                    val = float((qs.get("pct") or [""])[0])
                except ValueError:
                    ok = False
                else:
                    if 50 <= val <= 99:
                        _live_protect_min_confidence = round(val / 100.0, 4)
                    else:
                        ok = False
                resp = {"ok": ok, "protect_min_confidence": _live_protect_min_confidence}
            if ok:
                _save_live_trading_state()
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/chart":
            # timeframe-switch endpoint (15m/30m/1h buttons) - server-
            # rendered like the default chart, added 2026-09-13. Re-reads
            # the asset's current target/price/seconds-left from the SAME
            # snapshot the main page already uses, so it can't drift out of
            # sync with what "My call" etc. are showing.
            qs = urllib.parse.parse_qs(parsed.query)
            asset_name = (qs.get("asset") or [""])[0]
            try:
                minutes = max(1, min(180, int((qs.get("tf") or ["30"])[0])))
            except ValueError:
                minutes = 30
            asset_cfg = next((a for a in DASHBOARD_ASSETS if a["name"] == asset_name), None)
            with _lock:
                snap = dict(_state.get(asset_name, {}))
            svg = ""
            if asset_cfg is not None and "error" not in snap:
                symbol = asset_cfg["symbol"]
                ticks = get_tick_history(symbol)
                candles = bucket_ticks_to_candles(ticks, lookback_seconds=minutes * 60)
                seconds_left = snap.get("seconds_left") or 0
                typical_move = None
                if seconds_left > 0:
                    typical_move = project_point_for_minutes(
                        0, symbol, seconds_left / 60, direction="YES", percentile="p68") or None
                whale_trades = get_whale_trades_raw(symbol)
                svg = render_candle_chart_svg(candles, snap.get("target_price"), snap.get("spot_price"),
                                               typical_move, seconds_left, whale_trades,
                                               lookback_seconds=minutes * 60)
            body = json.dumps({"svg": svg}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = PAGE_TEMPLATE.replace('__PATTERN_LIB_JSON__', pattern_lib_json()).replace('__VERSION_TAG__', _version_tag()).replace('__LEGAL__', _accp.legal_footer()).replace('__USERBAR__', _accp.userbar(*self._who) if (HOSTED_PASSWORD and self._who) else '').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            # no-cache - added 2026-09-14 so a phone/browser never serves a
            # stale layout from before a deploy (e.g. the sound bar's
            # position) instead of fetching the current page.
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)


class _TeeToFile:
    """Mirrors stdout/stderr into bot_state/dashboard_console.log - added
    2026-10-02: a real buy that never filled left no trace anywhere (the
    dashboard's console output went nowhere), so the cause couldn't be found."""
    def __init__(self, stream, path):
        self._stream = stream
        self._f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")

    def write(self, s):
        try:
            self._stream.write(s)
        except Exception:
            pass
        try:
            if s.strip():
                self._f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {s.rstrip()}\n")
        except Exception:
            pass

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def main():
    import sys
    # a fresh copy has no bot_state/ or data_cache/ folder yet (fixed 2026-10-08 - it crashed on the first log write)
    _root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    os.makedirs(os.path.join(_root, "bot_state"), exist_ok=True)
    os.makedirs(os.path.join(_root, "data_cache"), exist_ok=True)
    _console_log = os.path.join(_root, "bot_state", "dashboard_console.log")
    sys.stdout = _TeeToFile(sys.stdout, _console_log)
    sys.stderr = _TeeToFile(sys.stderr, _console_log)
    import socket as _socket
    _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        _probe.bind(("0.0.0.0", PORT))          # exclusive on purpose (no SO_REUSEADDR): fails if ANOTHER dashboard already has the port
    except OSError:
        print(f"Port {PORT} is already in use - another dashboard is running. This copy exits so two copies never share the history and logs.")
        os._exit(3)
    finally:
        _probe.close()
    _load_history()
    _load_end_locks()
    _load_end_hist()
    threading.Thread(target=_end_hist_loop, daemon=True, name="end-call-history").start()
    _lc_load()
    threading.Thread(target=_lc_loop, daemon=True, name="line-cross").start()
    threading.Thread(target=_sync_loop, daemon=True, name="website-sync").start()
    threading.Thread(target=_follow_loop, daemon=True, name="website-follow").start()
    _load_locked_state()
    _load_live_trading_state()
    _load_bought_tickers()
    total = sum(len(v) for v in _history.values())
    if total:
        print(f"Loaded {total} past graded windows from disk ({HISTORY_FILE}).")
    threading.Thread(target=_own_heat_loop, daemon=True, name="own-heat").start()
    threading.Thread(target=_coinbase_ws_run_forever, daemon=True).start()
    threading.Thread(target=_kraken_ws_run_forever, daemon=True).start()
    threading.Thread(target=_coinbase_l2_run_forever, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()
    try:
        _flow.HUB.on_trade = _flow_to_legacy
        _flow.start()
    except Exception as _e:
        print(f"[flow] could not start: {_e!r}")
    global _mtf
    try:
        _mtf = MTFMonitor([a["symbol"] for a in DASHBOARD_ASSETS if a["name"] in LIVE_ASSETS], get_tick_history)
        _mtf.names = {a["symbol"]: a["name"] for a in DASHBOARD_ASSETS}
        threading.Thread(target=_mtf.run_forever, daemon=True).start()
    except Exception as _e:
        print(f"[MTF] could not start: {_e!r}")
    # 0.0.0.0 - listens on all network interfaces, not just this machine, so
    # another device on the SAME local network (e.g. a laptop on the same
    # WiFi) can reach it. Still not exposed to the public internet - that
    # would need port forwarding or a tunnel, a separate, bigger step.
    # ThreadingTCPServer, not plain TCPServer (fixed 2026-09-14) - the plain
    # single-threaded server handles ONE request at a time, so any single
    # slow/hung request (e.g. a laggy outbound API call) blocked every other
    # device from connecting at all, including the user's own phone - this
    # is the real cause behind "the link's not working" while the process
    # was still alive and technically LISTENING. Every shared global the
    # handlers touch (_state, _history, _whale_trades, ...) is already
    # guarded by a lock for the existing background threads, so handling
    # requests concurrently too is safe.
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    try:
        _httpd_probe = socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler)
    except OSError as _be:
        print(f"Could not take port {PORT} ({_be!r}) - exiting so no half-started copy lingers.")
        os._exit(3)
    with _httpd_probe as httpd:
        print(f"Dashboard running - open http://localhost:{PORT} in your browser (or from another device on "
              f"the same WiFi, use this machine's LAN IP instead of localhost). Ctrl+C to stop.")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
