"""
Live trading bot - close_favorite runs per-asset (BTC, ETH), momentum_jump is
BTC-only. Places REAL orders once armed.

close_favorite: enter the favorite side a few minutes before each 15-min market
closes, then hold to natural settlement (or the defensive exit below). Each
asset uses its own walk-forward validated entry timing / thresholds / position
size - see ASSETS below for exact numbers and where each was validated.

momentum_jump: enter on a >=0.10% one-minute BTC spot jump (the validated lead-lag
signal - spot leads Kalshi's price by ~1 minute), then actively SELL to exit via a
trailing/breakeven stop, or hold to settlement if never triggered. BTC-only -
never validated for other assets.

win-streak scaling: after WIN_STREAK_SCALE_TRIGGER consecutive close_favorite wins
on a given asset, that asset's position size multiplier steps up by
WIN_STREAK_SCALE_PCT. A single loss resets the streak count to 0 but does NOT
undo multipliers already earned. User-requested, not backtested - it compounds
risk on a hot streak, which is a real way to lose more on the eventual loss.

SAFETY: this places REAL orders with REAL money once armed (KALSHI_LIVE_TRADING=true).
Disarmed (dry-run) by default. Every order - entry or exit, real or dry-run - is
logged to bot_state/live_trades.csv.
"""
import argparse
import base64
import csv
import datetime as dt
import json
import os
import time
import uuid

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from src.data import fetch_klines, drop_forming_candle
from src.kalshi_paper_bot import load_thresholds

AUTH_BASE = "https://api.elections.kalshi.com/trade-api/v2"
PUBLIC_BASE = "https://external-api.kalshi.com/trade-api/v2"

STATE_DIR = os.path.join(os.path.dirname(__file__), "..", "bot_state")
LIVE_TRADES_PATH = os.path.join(STATE_DIR, "live_trades.csv")
LIVE_LEDGER_PATH = os.path.join(STATE_DIR, "live_ledger.json")

TRADE_FIELDS = ["ticker", "strategy", "time", "action", "side", "count", "price",
                 "cost_dollars", "order_id", "dry_run", "exit_reason", "result", "payoff_dollars"]

MOMENTUM_MIN_SECONDS_LEFT = 90
POLL_SECONDS = 1
SETTLEMENT_GRACE_SECONDS = 30

# --- safety rails: override via environment variables, all conservative by default ---
DAILY_LOSS_LIMIT_DOLLARS = float(os.environ.get("KALSHI_DAILY_LOSS_LIMIT", "20"))
DAILY_LOSS_LIMIT_ENABLED = DAILY_LOSS_LIMIT_DOLLARS > 0  # set KALSHI_DAILY_LOSS_LIMIT=0 to disable
LIVE_TRADING_ARMED = os.environ.get("KALSHI_LIVE_TRADING", "false").lower() == "true"
ENABLE_MOMENTUM = os.environ.get("KALSHI_ENABLE_MOMENTUM", "false").lower() == "true"

ADAPTIVE_GAP_SCALE_CONFIDENCE = 0.90

# --- per-asset validated params (walk-forward: selected on first 80% of each
# asset's historical markets chronologically, confirmed on held-out last 20%,
# plus a fresh 594-market stress test for BTC's strict-direction combo) ---
# BTC (3.5min entry, floor+strict combo) HOLDOUT: 98.39% -> 100.00% non-neg,
#   avg payoff +$7.37 -> +$8.64/$100. Confirmed again on 594 fresh markets:
#   98.41% -> 100.00% non-neg, +$7.14 -> +$8.20/$100.
# ETH (3min entry, floor+strict combo) HOLDOUT: 98.61% -> 100.00% non-neg,
#   avg payoff roughly flat (+$9.78 -> +$9.52/$100, not improved like BTC's).
#   Weaker result than BTC's - first live run for ETH, sized smaller as a result.
# liquidity sweep filter: validated on both assets' holdout data - skip entry
# if spot just broke a recent swing high/low (where resting stop-loss/limit
# orders cluster) within the last few minutes. Doesn't change win rate (both
# assets already hit 100% non-neg with the combo above) but improves avg
# payoff per trade: BTC +$8.64 -> +$10.14/$100 (+17%), ETH +$9.37 -> +$12.40
# (+32%), at the cost of roughly 30-45% fewer entries.
ASSETS = [
    {"name": "BTC", "series": "KXBTC15M", "symbol": "BTC-USD", "entry_minutes": 3.5,
     "defensive_exit": float(os.environ.get("KALSHI_DEFENSIVE_EXIT_THRESHOLD", "0.00035")),
     "adaptive_gap_min": float(os.environ.get("KALSHI_ADAPTIVE_GAP_MIN", "0.00005")),
     "entry_confidence_floor": float(os.environ.get("KALSHI_ENTRY_CONFIDENCE_FLOOR", "0.58")),
     "strict_direction_margin": float(os.environ.get("KALSHI_STRICT_DIRECTION_MARGIN", "0.0002")),
     "sweep_lookback_minutes": int(os.environ.get("KALSHI_SWEEP_LOOKBACK", "10")),
     "sweep_recent_minutes": int(os.environ.get("KALSHI_SWEEP_RECENT", "2")),
     "sweep_margin": float(os.environ.get("KALSHI_SWEEP_MARGIN", "0.0003")),
     "hard_floor_level": float(os.environ.get("KALSHI_HARD_FLOOR", "0.50")),
     "max_contracts": int(os.environ.get("KALSHI_MAX_CONTRACTS", "5"))},
    {"name": "ETH", "series": "KXETH15M", "symbol": "ETH-USD", "entry_minutes": 3,
     "defensive_exit": float(os.environ.get("KALSHI_ETH_DEFENSIVE_EXIT_THRESHOLD", "0.0005")),
     "adaptive_gap_min": float(os.environ.get("KALSHI_ETH_ADAPTIVE_GAP_MIN", "0.00005")),
     "entry_confidence_floor": float(os.environ.get("KALSHI_ETH_ENTRY_CONFIDENCE_FLOOR", "0.58")),
     "strict_direction_margin": float(os.environ.get("KALSHI_ETH_STRICT_DIRECTION_MARGIN", "0.0002")),
     "sweep_lookback_minutes": int(os.environ.get("KALSHI_ETH_SWEEP_LOOKBACK", "30")),
     "sweep_recent_minutes": int(os.environ.get("KALSHI_ETH_SWEEP_RECENT", "10")),
     "sweep_margin": float(os.environ.get("KALSHI_ETH_SWEEP_MARGIN", "0.001")),
     "hard_floor_level": float(os.environ.get("KALSHI_ETH_HARD_FLOOR", "0.50")),
     "max_contracts": int(os.environ.get("KALSHI_ETH_MAX_CONTRACTS", "5"))},
]

WIN_STREAK_SCALE_TRIGGER = int(os.environ.get("KALSHI_WIN_STREAK_TRIGGER", "20"))
WIN_STREAK_SCALE_PCT = float(os.environ.get("KALSHI_WIN_STREAK_SCALE_PCT", "0.10"))


def adaptive_defensive_threshold(confidence, base_threshold, min_threshold):
    span = confidence - 0.5
    scale_span = ADAPTIVE_GAP_SCALE_CONFIDENCE - 0.5
    ratio = span / scale_span if scale_span else 1.0
    threshold = min_threshold + ratio * (base_threshold - min_threshold)
    return max(min_threshold, threshold)


def _load_private_key():
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_path or not os.path.exists(key_path):
        raise RuntimeError(
            "KALSHI_PRIVATE_KEY_PATH is not set or the file doesn't exist. "
            "See the setup guide - this must point to your RSA private key .pem file."
        )
    with open(key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign(private_key, timestamp_ms, method, path):
    message = f"{timestamp_ms}{method}{path}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


class KalshiClient:
    def __init__(self):
        self.key_id = os.environ.get("KALSHI_API_KEY_ID")
        if not self.key_id:
            raise RuntimeError("KALSHI_API_KEY_ID is not set. See the setup guide.")
        self.private_key = _load_private_key()

    def _headers(self, method, path):
        timestamp_ms = str(int(time.time() * 1000))
        signature = _sign(self.private_key, timestamp_ms, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "Content-Type": "application/json",
        }

    def get_balance_dollars(self):
        path = "/trade-api/v2/portfolio/balance"
        resp = requests.get(AUTH_BASE + path.replace("/trade-api/v2", ""),
                             headers=self._headers("GET", path), timeout=20)
        resp.raise_for_status()
        return float(resp.json().get("balance_dollars", 0))

    def get_balance_for_exchange(self, exchange_index):
        """Spendable cash in ONE of Kalshi's balance pools - added 2026-10-04.
        /portfolio/balance splits the total into balance_breakdown by
        exchange_index and a market can only spend its own pool (the 15-minute
        crypto markets are exchange_index 2): a $305 total with only $84 in
        pool 2 made every $60 order fail with insufficient_balance. Falls back
        to the total when the breakdown or index is unavailable."""
        path = "/trade-api/v2/portfolio/balance"
        resp = requests.get(AUTH_BASE + path.replace("/trade-api/v2", ""),
                             headers=self._headers("GET", path), timeout=20)
        resp.raise_for_status()
        j = resp.json()
        if exchange_index is not None:
            for b in j.get("balance_breakdown") or []:
                if b.get("exchange_index") == exchange_index:
                    return float(b.get("balance", 0))
        return float(j.get("balance_dollars", 0))

    def get_position(self, ticker):
        """Real, ground-truth position for one market - added 2026-09-27 so
        the dashboard can detect a manual sell/close done directly on
        Kalshi's own site/app (which it otherwise has no way to see). Returns
        the market_position dict ({"position_fp": signed string - positive
        = YES contracts held, negative = NO contracts held, "0.00" = flat;
        "realized_pnl_dollars": ...}), or None if Kalshi has no position
        record at all for this ticker (never traded, or omitted while flat)."""
        path = "/trade-api/v2/portfolio/positions"
        resp = requests.get(AUTH_BASE + path.replace("/trade-api/v2", ""),
                             headers=self._headers("GET", path), params={"ticker": ticker}, timeout=20)
        resp.raise_for_status()
        for p in resp.json().get("market_positions", []):
            if p.get("ticker") == ticker:
                return p
        return None

    def get_fills(self, ticker):
        """All fills on one market (read-only) - used to PROVE a manual sale
        really happened before the dashboard believes the position endpoint."""
        path = "/trade-api/v2/portfolio/fills"
        resp = requests.get(AUTH_BASE + path.replace("/trade-api/v2", ""),
                             headers=self._headers("GET", path), params={"ticker": ticker, "limit": 200}, timeout=20)
        resp.raise_for_status()
        return resp.json().get("fills", [])

    def create_order(self, ticker, side, count, price, reduce_only=False):
        path = "/trade-api/v2/portfolio/events/orders"
        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": side,  # "bid" (buy YES) or "ask" (sell/short YES)
            "count": f"{count:.2f}",
            # Kalshi's price ticks are 0.001 near the extremes (below 10c, above 90c)
            # and 0.01 in the middle. Formatting with only 2 decimals rounded prices
            # like 0.999 up to "1.00" - an invalid price - causing every order near a
            # near-certain favorite (exactly where this strategy trades most) to be
            # rejected outright. 3 decimals covers both tick sizes correctly.
            "price": f"{price:.3f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only": reduce_only,
        }
        resp = requests.post(AUTH_BASE + path.replace("/trade-api/v2", ""),
                              headers=self._headers("POST", path),
                              data=json.dumps(body), timeout=20)
        if resp.status_code >= 400:
            print(f"  ORDER REJECTED ({resp.status_code}): request body={body}")
            print(f"  response body={resp.text}")
        resp.raise_for_status()
        return resp.json()


def _get_public(url, params=None, retries=5):
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=20)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.exceptions.HTTPError(str(resp.status_code))
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            time.sleep(2 * (2 ** attempt))
    raise RuntimeError(f"failed after {retries} retries: {url}")


def get_active_market(series):
    data = _get_public(f"{PUBLIC_BASE}/markets", params={"series_ticker": series, "status": "open", "limit": 10})
    markets = data.get("markets", [])
    now = dt.datetime.now(dt.timezone.utc)

    def close_dt(m):
        return dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))

    upcoming = [m for m in markets if close_dt(m) > now]
    if upcoming:
        return min(upcoming, key=close_dt)
    # 2026-10-18: Kalshi began reporting the live market's status as "active", and the status=open filter then returns NOTHING (the dashboard showed "no active
    # market" for a market that was trading). So when the filter finds none, ask for the markets closing in the next ~40 minutes and take the live one.
    t = int(time.time())
    data = _get_public(f"{PUBLIC_BASE}/markets", params={"series_ticker": series, "min_close_ts": t - 60, "max_close_ts": t + 2400, "limit": 20})

    def open_dt(m):
        return dt.datetime.fromisoformat(m["open_time"].replace("Z", "+00:00"))
    live = [m for m in data.get("markets", []) if m.get("status") in ("open", "active") and open_dt(m) <= now < close_dt(m)]
    return min(live, key=close_dt) if live else None


def get_market_snapshot(ticker):
    data = _get_public(f"{PUBLIC_BASE}/markets/{ticker}")
    return data.get("market", data)


def get_kalshi_price(market):
    # Live bid/ask reflects the CURRENT book; last_price_dollars is the last trade,
    # which can go stale for seconds in a fast-moving market if nothing prints -
    # using it preferentially caused a real bug: the entry decision was made off a
    # stale price while order pricing (which used live bid/ask) executed against
    # the real, already-moved market, landing on the wrong side. Bid/ask now wins.
    yes_bid = float(market.get("yes_bid_dollars", 0) or 0)
    yes_ask = float(market.get("yes_ask_dollars", 0) or 0)
    if yes_bid or yes_ask:
        return (yes_bid + yes_ask) / 2
    last_price = float(market.get("last_price_dollars", 0) or 0)
    return last_price if last_price else None


def get_yes_bid_ask(market):
    return float(market.get("yes_bid_dollars", 0) or 0), float(market.get("yes_ask_dollars", 0) or 0)


def get_live_yes_bid_ask(ticker):
    """(yes_bid, yes_ask) straight from the order book, or None if unavailable.
    Added 2026-10-03: the /markets LIST endpoint's yes_bid/yes_ask is a cached
    copy that only refreshes ~every 30s and /markets/{ticker} lags ~5-10s
    (measured side by side), while /orderbook is live. Real losses came from
    this: on 2026-10-02 20:00 XRP the list said YES 66.5c, the dashboard locked
    YES as the 'favorite', and the order actually filled at 38.7c - YES was the
    underdog the whole time (-$100.17). Same on 09-26 23:00 and 09-29 20:15."""
    try:
        data = _get_public(f"{PUBLIC_BASE}/markets/{ticker}/orderbook", retries=1)
    except Exception:
        return None
    book = data.get("orderbook_fp") or {}
    yes_bids = [float(p) for p, size in (book.get("yes_dollars") or []) if float(size) > 0]
    no_bids = [float(p) for p, size in (book.get("no_dollars") or []) if float(size) > 0]
    if not yes_bids or not no_bids:
        return None
    return max(yes_bids), round(1 - max(no_bids), 4)


def marketable_price(order_side, yes_bid, yes_ask, buffer=0.01):
    """A price aggressive enough that an immediate_or_cancel order actually crosses
    the spread and fills, instead of silently getting cancelled with 0 contracts."""
    if order_side == "bid":
        base = yes_ask if yes_ask > 0 else yes_bid
        price = base + buffer
    else:
        base = yes_bid if yes_bid > 0 else yes_ask
        price = base - buffer
    # Kalshi prices range 0.000-1.000; the old 0.01-0.99 clip was cutting off
    # exactly the near-certain favorites (0.99+) this strategy targets most,
    # pricing the order BELOW the real ask and guaranteeing a non-fill.
    price = max(0.001, min(0.999, price))
    # tick size (fixed 2026-10-04 after a take-profit sell was rejected ~10
    # times as "invalid_price" at 0.107, 0.117, 0.127...): 0.001 ticks only
    # exist below 10c and above 90c, the middle range takes whole cents. A
    # quote near 10c + the buffer lands in the middle range with 3 decimals.
    # Round in the AGGRESSIVE direction (up for a bid, down for an ask) so
    # the order stays marketable.
    if 0.10 <= price <= 0.90:
        import math
        price = (math.ceil(price * 100 - 1e-9) if order_side == "bid" else math.floor(price * 100 + 1e-9)) / 100
        price = max(0.10, min(0.90, price))
    return round(price, 3)


# observed in production: near market close the quote can move faster than a flat
# 1-cent buffer keeps up with, causing repeated 0-contract non-fills as the order
# chases a moving price - by the time it does fill several retries later, it's at a
# meaningfully worse price than acting decisively would have gotten. Escalate the
# buffer with each consecutive failed attempt on the same leg.
ENTRY_BUFFER_BASE = 0.01
ENTRY_BUFFER_STEP = 0.01
ENTRY_BUFFER_MAX = 0.05


def escalated_buffer(attempts):
    return min(ENTRY_BUFFER_MAX, ENTRY_BUFFER_BASE + attempts * ENTRY_BUFFER_STEP)


# real production failure (2026-08-02): a defensive exit failed to fill 6
# times in a row while quoting 0.630 -> 0.590, capped by ENTRY_BUFFER_MAX at
# just 5 cents of aggressiveness - nowhere near enough for a market crashing
# this fast. By the time it finally filled, price had crashed to 0.008,
# turning what should have been a small controlled loss (defensive exit's
# entire purpose) into a near-total one. For an ENTRY, a few extra cents of
# slippage barely matters since it's a voluntary choice to trade at all. For
# an EXIT, price is secondary to actually getting out - after a handful of
# failed attempts, force a guaranteed-fill price (crosses essentially the
# entire book) rather than keep chasing a moving target while the loss grows.
EXIT_BUFFER_BASE = 0.01
EXIT_BUFFER_STEP = 0.05
EXIT_BUFFER_MAX = 0.30
EXIT_FORCE_FILL_ATTEMPTS = 3


def escalated_exit_buffer(attempts):
    if attempts >= EXIT_FORCE_FILL_ATTEMPTS:
        return 1.0  # clipped by marketable_price to 0.001/0.999 - crosses any resting liquidity
    return min(EXIT_BUFFER_MAX, EXIT_BUFFER_BASE + attempts * EXIT_BUFFER_STEP)


def get_spot_closes(symbol, minutes_needed):
    """Fetches enough 1-min candles to cover both the latest 1-min return and
    a liquidity-sweep lookback window. Returns a list of closes, oldest first."""
    limit = max(minutes_needed + 5, 10)
    df = fetch_klines(symbol=symbol, interval="1m", limit=limit)
    df = drop_forming_candle(df, "1m")
    if len(df) < 2:
        return None
    return [float(c) for c in df["close"].tolist()]


def spot_return_from_closes(closes):
    if closes is None or len(closes) < 2:
        return None
    return closes[-1] / closes[-2] - 1


def had_recent_sweep(closes, lookback_minutes, recent_minutes, sweep_margin):
    """Validated on historical data: was there a swing high/low over the
    lookback window (excluding the most recent `recent_minutes`) that price
    then broke through within the most recent `recent_minutes`? That means
    resting liquidity at the old extreme just got swept - a precursor to
    exactly the kind of fast reversal that's caused real losses."""
    if closes is None or len(closes) < lookback_minutes + 1:
        return False
    older = closes[-(lookback_minutes + 1):-recent_minutes] if recent_minutes > 0 else closes[-(lookback_minutes + 1):]
    recent = closes[-recent_minutes:] if recent_minutes > 0 else []
    if not older or not recent:
        return False
    recent_high = max(older)
    recent_low = min(older)
    swept_up = max(recent) > recent_high * (1 + sweep_margin)
    swept_down = min(recent) < recent_low * (1 - sweep_margin)
    return swept_up or swept_down


def _ensure_files():
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(LIVE_TRADES_PATH):
        with open(LIVE_TRADES_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=TRADE_FIELDS).writeheader()


def _append_trade(row):
    with open(LIVE_TRADES_PATH, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow(row)


def _migrate_per_asset(value):
    """Older ledger files (pre-multi-asset) stored win_streak/size_multiplier
    as a single number, not a per-asset dict. Discard incompatible old data
    rather than crash on it."""
    return value if isinstance(value, dict) else {}


def _load_ledger():
    today = dt.date.today().isoformat()
    if os.path.exists(LIVE_LEDGER_PATH):
        with open(LIVE_LEDGER_PATH) as f:
            ledger = json.load(f)
        if ledger.get("date") == today:
            ledger["win_streak"] = _migrate_per_asset(ledger.get("win_streak"))
            ledger["size_multiplier"] = _migrate_per_asset(ledger.get("size_multiplier"))
            return ledger
        # new day: reset the daily P&L/halt state, but the win streak and the size
        # multiplier it's earned track ongoing performance, not a single day's
        # numbers - carry those forward across the midnight reset.
        return {"date": today, "realized_pnl": 0.0, "halted": False,
                "win_streak": _migrate_per_asset(ledger.get("win_streak")),
                "size_multiplier": _migrate_per_asset(ledger.get("size_multiplier"))}
    return {"date": today, "realized_pnl": 0.0, "halted": False, "win_streak": {}, "size_multiplier": {}}


def _save_ledger(ledger):
    with open(LIVE_LEDGER_PATH, "w") as f:
        json.dump(ledger, f)


def _size_multiplier(ledger, asset_name):
    return ledger.get("size_multiplier", {}).get(asset_name, 1.0)


def _record_close_favorite_result(ledger, asset_name, payoff_dollars):
    """Updates the win streak / size multiplier (per-asset) after a
    close_favorite position closes (defensive exit or settlement)."""
    streaks = ledger.setdefault("win_streak", {})
    multipliers = ledger.setdefault("size_multiplier", {})
    streak = streaks.get(asset_name, 0)
    mult = multipliers.get(asset_name, 1.0)
    if payoff_dollars >= 0:
        streak += 1
        if streak % WIN_STREAK_SCALE_TRIGGER == 0:
            mult *= (1 + WIN_STREAK_SCALE_PCT)
            print(f"*** WIN STREAK ({asset_name}): {streak} close_favorite wins in a row - "
                  f"position size multiplier now {mult:.3f}x ***")
    else:
        if streak > 0:
            print(f"Win streak ({asset_name}) broken at {streak} (size multiplier stays {mult:.3f}x)")
        streak = 0
    streaks[asset_name] = streak
    multipliers[asset_name] = mult


def _place_or_log(client, ticker, order_side, count, price, reduce_only, label):
    """Places a real order if armed, or logs what would happen if not.
    Returns {"order_id", "filled", "fill_count", "avg_fill_price"} - callers MUST
    check "filled" before treating this as a real position. An immediate_or_cancel
    order that doesn't cross the spread returns a valid order_id with 0 fills."""
    if LIVE_TRADING_ARMED:
        result = client.create_order(ticker, order_side, count, price, reduce_only=reduce_only)
        order_id = result.get("order_id", "")
        fill_count = float(result.get("fill_count", "0") or 0)
        avg_fill_price_raw = result.get("average_fill_price")
        avg_fill_price = float(avg_fill_price_raw) if avg_fill_price_raw not in (None, "") else price
        filled = fill_count > 0
        status = "FILLED" if filled else "NOT FILLED (0 contracts matched - order was cancelled)"
        print(f"[LIVE {label}] {ticker} {order_side} x{count} @ {price:.3f} -> order_id={order_id} "
              f"fill_count={fill_count:.2f} {status}")
        return {"order_id": order_id, "filled": filled, "fill_count": fill_count, "avg_fill_price": avg_fill_price}
    print(f"[DRY RUN {label}] would place {ticker} {order_side} x{count} @ {price:.3f}")
    return {"order_id": "", "filled": True, "fill_count": count, "avg_fill_price": price}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    _ensure_files()
    client = None
    if LIVE_TRADING_ARMED:
        client = KalshiClient()
        loss_limit_desc = f"${DAILY_LOSS_LIMIT_DOLLARS:.2f} daily loss limit" if DAILY_LOSS_LIMIT_ENABLED \
            else "NO daily loss limit (disabled)"
        assets_desc = ", ".join(f"{a['name']} max {a['max_contracts']} contracts/trade "
                                 f"(floor {a['entry_confidence_floor']:.0%}, strict margin "
                                 f"{a['strict_direction_margin']})" for a in ASSETS)
        print(f"*** LIVE TRADING ARMED *** {assets_desc}, {loss_limit_desc}. "
              f"Momentum strategy (BTC only): {'ENABLED' if ENABLE_MOMENTUM else 'disabled'}. "
              f"Win-streak scaling: +{WIN_STREAK_SCALE_PCT:.0%} every {WIN_STREAK_SCALE_TRIGGER} wins in a row.")
    else:
        print("Running in DRY RUN mode - no real orders will be placed. "
              "Set KALSHI_LIVE_TRADING=true to arm.")

    positions = {}

    while True:
        ledger = _load_ledger()
        cfg = load_thresholds()
        now = dt.datetime.now(dt.timezone.utc)
        try:
            for asset in ASSETS:
                active = get_active_market(asset["series"])
                spot_closes = get_spot_closes(asset["symbol"], asset["sweep_lookback_minutes"])
                spot_ret = spot_return_from_closes(spot_closes)  # needed for defensive exit even without momentum

                if active is None:
                    continue

                ticker = active["ticker"]
                close_time = dt.datetime.fromisoformat(active["close_time"].replace("Z", "+00:00"))
                seconds_left = (close_time - now).total_seconds()
                kalshi_price = get_kalshi_price(active)
                strategy_name = "close_favorite" if asset["name"] == "BTC" else f"close_favorite_{asset['name']}"

                if ticker not in positions:
                    positions[ticker] = {
                        "asset": asset,
                        "close_time": close_time,
                        "close_favorite": None,
                        "close_favorite_entry_attempts": 0,
                        "close_favorite_exit_attempts": 0,
                        "close_favorite_exited": False,
                        "momentum": None, "momentum_triggered": False,
                    }
                pos = positions[ticker]

                # --- close_favorite entry ---
                # never re-enter a ticker after a defensive exit fired on it - that
                # exit means spot already moved against the market once this window;
                # only the single initial entry near close is validated, re-entering
                # into a market that just proved volatile is an unvalidated bet.
                if (pos["close_favorite"] is None and not pos["close_favorite_exited"]
                        and not ledger["halted"] and kalshi_price is not None
                        and kalshi_price != 0.5 and 0 < seconds_left <= asset["entry_minutes"] * 60):
                    side_yesno = "yes" if kalshi_price > 0.5 else "no"
                    order_side = "bid" if side_yesno == "yes" else "ask"
                    cost = kalshi_price if side_yesno == "yes" else (1 - kalshi_price)
                    contracts = max(1, round(asset["max_contracts"] * _size_multiplier(ledger, asset["name"])))
                    needed = cost * contracts
                    confidence = max(kalshi_price, 1 - kalshi_price)

                    entry_favorable = spot_ret is not None and (
                        (side_yesno == "yes" and spot_ret >= asset["strict_direction_margin"]) or
                        (side_yesno == "no" and spot_ret <= -asset["strict_direction_margin"])
                    )
                    swept = had_recent_sweep(spot_closes, asset["sweep_lookback_minutes"],
                                              asset["sweep_recent_minutes"], asset["sweep_margin"])

                    if confidence < asset["entry_confidence_floor"]:
                        print(f"[SKIPPED] {strategy_name} {ticker}: confidence {confidence:.1%} below "
                              f"{asset['entry_confidence_floor']:.0%} floor - waiting for it to strengthen "
                              f"before entering.")
                    elif not entry_favorable:
                        print(f"[SKIPPED] {strategy_name} {ticker}: spot not yet confirming {side_yesno} "
                              f"(spot_ret={spot_ret if spot_ret is not None else 'n/a'}) - waiting for favorable "
                              f"momentum before entering.")
                    elif swept:
                        print(f"[SKIPPED] {strategy_name} {ticker}: spot just swept a recent high/low - "
                              f"waiting for this to settle before entering.")
                    elif LIVE_TRADING_ARMED and client.get_balance_dollars() < needed:
                        print(f"[SKIPPED] {strategy_name} {ticker}: need ${needed:.2f}, insufficient balance.")
                    else:
                        yes_bid, yes_ask = get_yes_bid_ask(active)
                        buffer = escalated_buffer(pos["close_favorite_entry_attempts"])
                        order_price = marketable_price(order_side, yes_bid, yes_ask, buffer=buffer)
                        fill = _place_or_log(client, ticker, order_side, contracts,
                                              order_price, False, f"ENTRY:{strategy_name}")
                        if not fill["filled"]:
                            pos["close_favorite_entry_attempts"] += 1
                            print(f"[NO POSITION] {strategy_name} {ticker}: order did not fill, "
                                  f"nothing bought - will retry next poll (attempt "
                                  f"{pos['close_favorite_entry_attempts']}, buffer=${buffer:.3f}).")
                        else:
                            filled_count = fill["fill_count"]
                            actual_cost = fill["avg_fill_price"] if side_yesno == "yes" \
                                else (1 - fill["avg_fill_price"])
                            _append_trade({
                                "ticker": ticker, "strategy": strategy_name, "time": now.isoformat(),
                                "action": "buy", "side": side_yesno, "count": filled_count,
                                "price": fill["avg_fill_price"], "cost_dollars": actual_cost * filled_count,
                                "order_id": fill["order_id"], "dry_run": not LIVE_TRADING_ARMED,
                                "exit_reason": "", "result": "", "payoff_dollars": "",
                            })
                            pos["close_favorite"] = {"side": side_yesno, "cost": actual_cost, "count": filled_count}

                # --- close_favorite defensive exit: adaptive-gap spot trigger, OR a
                # hard floor backstop that fires regardless of the spot signal if the
                # position itself has crashed too far. Validated: even an imperfect
                # (gap-risk-limited) hard floor meaningfully reduces worst-case tail
                # losses (-32.98% -> -27.08% avg in the worst 10% of historical events
                # at this 80% level), at a real but bounded cost to overall profit
                # (-26% total). This is a deliberate "cap the damage" tradeoff, not a
                # profit-maximizing one - the user explicitly prioritized never losing
                # the whole stake over squeezing out maximum long-run profit. ---
                if pos["close_favorite"] is not None and kalshi_price is not None:
                    cf = pos["close_favorite"]
                    confidence = max(kalshi_price, 1 - kalshi_price)
                    price_side_now_check = kalshi_price if cf["side"] == "yes" else (1 - kalshi_price)
                    hard_floor_hit = price_side_now_check <= cf["cost"] * asset["hard_floor_level"]

                    adverse = False
                    if spot_ret is not None:
                        active_threshold = adaptive_defensive_threshold(
                            confidence, asset["defensive_exit"], asset["adaptive_gap_min"])
                        adverse = (cf["side"] == "yes" and spot_ret <= -active_threshold) or \
                                  (cf["side"] == "no" and spot_ret >= active_threshold)

                    if adverse or hard_floor_hit:
                        exit_reason_label = "hard_floor" if hard_floor_hit and not adverse else "defensive_exit"
                        exit_order_side = "ask" if cf["side"] == "yes" else "bid"
                        yes_bid, yes_ask = get_yes_bid_ask(active)
                        buffer = escalated_exit_buffer(pos["close_favorite_exit_attempts"])
                        exit_price = marketable_price(exit_order_side, yes_bid, yes_ask, buffer=buffer)
                        fill = _place_or_log(client, ticker, exit_order_side, cf["count"],
                                              exit_price, True, f"EXIT:{strategy_name}:{exit_reason_label}")
                        if not fill["filled"]:
                            pos["close_favorite_exit_attempts"] += 1
                            print(f"[STILL OPEN] {strategy_name} {ticker}: exit order did not fill, "
                                  f"position remains open - will retry next poll (attempt "
                                  f"{pos['close_favorite_exit_attempts']}, buffer=${buffer:.3f}).")
                        else:
                            price_side_now = fill["avg_fill_price"] if cf["side"] == "yes" \
                                else (1 - fill["avg_fill_price"])
                            payoff_dollars = (price_side_now - cf["cost"]) * fill["fill_count"]
                            ledger["realized_pnl"] += payoff_dollars
                            _record_close_favorite_result(ledger, asset["name"], payoff_dollars)
                            _append_trade({
                                "ticker": ticker, "strategy": strategy_name, "time": now.isoformat(),
                                "action": "sell", "side": cf["side"], "count": fill["fill_count"],
                                "price": fill["avg_fill_price"], "cost_dollars": "", "order_id": fill["order_id"],
                                "dry_run": not LIVE_TRADING_ARMED, "exit_reason": exit_reason_label, "result": "",
                                "payoff_dollars": payoff_dollars,
                            })
                            spot_ret_desc = f"{spot_ret:+.4%}" if spot_ret is not None else "n/a"
                            print(f"[EXIT] {strategy_name} {ticker} reason={exit_reason_label} "
                                  f"spot_ret={spot_ret_desc} payoff=${payoff_dollars:+.2f}")
                            pos["close_favorite"] = None
                            pos["close_favorite_exited"] = True

                # --- momentum entry (BTC only - never validated for other assets) ---
                if (asset["name"] == "BTC" and ENABLE_MOMENTUM and not pos["momentum_triggered"]
                        and not ledger["halted"] and kalshi_price is not None and spot_ret is not None
                        and seconds_left > MOMENTUM_MIN_SECONDS_LEFT
                        and abs(spot_ret) >= cfg["momentum_jump_threshold"]
                        and 0 < kalshi_price < 1):
                    side_yesno = "yes" if spot_ret > 0 else "no"
                    order_side = "bid" if side_yesno == "yes" else "ask"
                    cost = kalshi_price if side_yesno == "yes" else (1 - kalshi_price)
                    needed = cost * asset["max_contracts"]

                    if LIVE_TRADING_ARMED and client.get_balance_dollars() < needed:
                        print(f"[SKIPPED] momentum {ticker}: need ${needed:.2f}, insufficient balance.")
                    else:
                        yes_bid, yes_ask = get_yes_bid_ask(active)
                        order_price = marketable_price(order_side, yes_bid, yes_ask)
                        fill = _place_or_log(client, ticker, order_side, asset["max_contracts"],
                                              order_price, False, "ENTRY:momentum")
                        if not fill["filled"]:
                            print(f"[NO POSITION] momentum {ticker}: order did not fill, nothing bought.")
                        else:
                            filled_count = fill["fill_count"]
                            actual_cost = fill["avg_fill_price"] if side_yesno == "yes" \
                                else (1 - fill["avg_fill_price"])
                            _append_trade({
                                "ticker": ticker, "strategy": "momentum_jump", "time": now.isoformat(),
                                "action": "buy", "side": side_yesno, "count": filled_count,
                                "price": fill["avg_fill_price"], "cost_dollars": actual_cost * filled_count,
                                "order_id": fill["order_id"], "dry_run": not LIVE_TRADING_ARMED,
                                "exit_reason": "", "result": "", "payoff_dollars": "",
                            })
                            pos["momentum"] = {"side": side_yesno, "cost": actual_cost,
                                                "count": filled_count, "peak_payoff": 0.0}
                    pos["momentum_triggered"] = True

                # --- momentum exit (trailing/breakeven stop) ---
                if pos["momentum"] is not None and kalshi_price is not None:
                    m = pos["momentum"]
                    price_side_now = kalshi_price if m["side"] == "yes" else (1 - kalshi_price)
                    payoff_now = price_side_now - m["cost"]
                    m["peak_payoff"] = max(m["peak_payoff"], payoff_now)

                    triggered, reason = False, None
                    if m["peak_payoff"] >= cfg["momentum_lock_in"]:
                        if payoff_now <= m["peak_payoff"] - cfg["momentum_trail"]:
                            triggered, reason = True, "trailing_stop"
                    elif payoff_now <= -cfg["momentum_hard_stop"]:
                        triggered, reason = True, "breakeven_stop"

                    if triggered:
                        # closing an existing position: sell what we hold (opposite of entry side)
                        exit_order_side = "ask" if m["side"] == "yes" else "bid"
                        yes_bid, yes_ask = get_yes_bid_ask(active)
                        exit_price = marketable_price(exit_order_side, yes_bid, yes_ask)
                        fill = _place_or_log(client, ticker, exit_order_side, m["count"],
                                              exit_price, True, f"EXIT:momentum:{reason}")
                        if not fill["filled"]:
                            print(f"[STILL OPEN] momentum {ticker}: exit order did not fill, "
                                  "position remains open - will retry next poll.")
                        else:
                            price_side_now = fill["avg_fill_price"] if m["side"] == "yes" \
                                else (1 - fill["avg_fill_price"])
                            payoff_dollars = (price_side_now - m["cost"]) * fill["fill_count"]
                            ledger["realized_pnl"] += payoff_dollars
                            _append_trade({
                                "ticker": ticker, "strategy": "momentum_jump", "time": now.isoformat(),
                                "action": "sell", "side": m["side"], "count": fill["fill_count"],
                                "price": fill["avg_fill_price"], "cost_dollars": "", "order_id": fill["order_id"],
                                "dry_run": not LIVE_TRADING_ARMED, "exit_reason": reason, "result": "",
                                "payoff_dollars": payoff_dollars,
                            })
                            print(f"[EXIT] momentum {ticker} reason={reason} payoff=${payoff_dollars:+.2f}")
                            pos["momentum"] = None

            # --- settle any tracked market whose window has closed (all assets) ---
            for ticker in list(positions.keys()):
                pos = positions[ticker]
                if (now - pos["close_time"]).total_seconds() < SETTLEMENT_GRACE_SECONDS:
                    continue
                if pos["close_favorite"] is None and pos["momentum"] is None:
                    del positions[ticker]
                    continue

                snap = get_market_snapshot(ticker)
                result = snap.get("result")
                if result not in ("yes", "no"):
                    continue

                pos_asset = pos["asset"]
                cf_strategy_name = "close_favorite" if pos_asset["name"] == "BTC" else f"close_favorite_{pos_asset['name']}"
                for strategy_key, strategy_name in (("close_favorite", cf_strategy_name), ("momentum", "momentum_jump")):
                    leg = pos[strategy_key]
                    if leg is None:
                        continue
                    leg_count = leg.get("count", pos_asset["max_contracts"])  # count = actual filled contracts
                    payoff_per_contract = (1 - leg["cost"]) if leg["side"] == result else -leg["cost"]
                    payoff_dollars = payoff_per_contract * leg_count
                    ledger["realized_pnl"] += payoff_dollars
                    if strategy_key == "close_favorite":
                        _record_close_favorite_result(ledger, pos_asset["name"], payoff_dollars)
                    _append_trade({
                        "ticker": ticker, "strategy": strategy_name, "time": now.isoformat(),
                        "action": "settle", "side": leg["side"], "count": leg_count,
                        "price": 1.0 if result == "yes" else 0.0, "cost_dollars": "", "order_id": "",
                        "dry_run": not LIVE_TRADING_ARMED, "exit_reason": "settlement", "result": result,
                        "payoff_dollars": payoff_dollars,
                    })
                    print(f"[SETTLED] {strategy_name} {ticker} result={result} payoff=${payoff_dollars:+.2f} "
                          f"(day total: ${ledger['realized_pnl']:+.2f})")

                if (DAILY_LOSS_LIMIT_ENABLED and ledger["realized_pnl"] <= -DAILY_LOSS_LIMIT_DOLLARS
                        and not ledger["halted"]):
                    ledger["halted"] = True
                    print(f"*** DAILY LOSS LIMIT HIT (${-ledger['realized_pnl']:.2f}) - "
                          "no new entries until tomorrow. Existing positions unaffected. ***")

                del positions[ticker]

            _save_ledger(ledger)

        except Exception as exc:
            print(f"  poll error: {exc}")

        if args.once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
