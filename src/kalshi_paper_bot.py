import argparse
import csv
import datetime as dt
import json
import os
import time

from src.data import fetch_klines, drop_forming_candle
from src.kalshi_favorite_backtest import BASE, SERIES, _get

STATE_DIR = os.path.join(os.path.dirname(__file__), "..", "bot_state")
TRADES_PATH = os.path.join(STATE_DIR, "trades.csv")
TICKS_PATH = os.path.join(STATE_DIR, "ticks.csv")
THRESHOLDS_PATH = os.path.join(STATE_DIR, "thresholds.json")
WIN_STREAK_PATH = os.path.join(STATE_DIR, "paper_win_streak.json")

# mirrors kalshi_live_bot.py's win-streak scaling, BTC close_favorite only: every
# WIN_STREAK_SCALE_TRIGGER consecutive wins, the position size multiplier steps up
# by WIN_STREAK_SCALE_PCT. A loss resets the streak count but keeps the multiplier
# already earned. Not backtested - a user-requested behavior, mirrored here so paper
# trading reflects the same logic as live for comparison.
WIN_STREAK_SCALE_TRIGGER = int(os.environ.get("KALSHI_WIN_STREAK_TRIGGER", "20"))
WIN_STREAK_SCALE_PCT = float(os.environ.get("KALSHI_WIN_STREAK_SCALE_PCT", "0.10"))

TRADE_FIELDS = [
    "ticker", "strategy", "entry_time", "entry_price", "side", "cost",
    "exit_time", "exit_price", "exit_reason", "realized_payoff", "result",
]
TICK_FIELDS = ["timestamp", "ticker", "spot_price", "spot_ret_1m", "kalshi_price"]

# --- validated via walk-forward test (trained on first 80% of each asset's ~3990+
# historical markets chronologically, confirmed on held-out last 20%) ---
# close_favorite: defensive exit + entry filter, run per-asset with each asset's own
# validated entry timing (NOT shared - 3.5min is only validated for BTC so far).
# BTC (3.5min) holdout: 88.57% -> 96.83% non-neg, avg payoff +$5.01 -> +$10.63/$100
# ETH (3min) holdout:    90.33% -> 98.38% non-neg, avg payoff +$2.48 -> +$9.42/$100
#
# adaptive gap on top of defensive_exit: the exit threshold shrinks toward
# adaptive_gap_min as Kalshi's price nears 50% (close to target, any move matters)
# and widens back to defensive_exit as it nears the extremes. Both assets validated
# on holdout with adaptive_gap_min=0.00005:
# BTC holdout: 96.83% non-neg (same), avg payoff +$10.63 -> +$11.12/$100
# ETH holdout: 98.38% -> 98.61% non-neg, avg payoff +$9.42 -> +$9.78/$100
CLOSE_FAVORITE_ASSETS = [
    # entry_confidence_floor: user-requested, BTC only, not backtested - skip
    # entering until the market's own implied confidence reaches this floor.
    {"name": "BTC", "series": "KXBTC15M", "symbol": "BTC-USD", "entry_minutes": 3.5,
     "defensive_exit": 0.00035, "entry_filter": 0.00005, "adaptive_gap_min": 0.00005,
     "entry_confidence_floor": 0.58, "strict_direction_margin": 0.0002,
     "sweep_lookback_minutes": 10, "sweep_recent_minutes": 2, "sweep_margin": 0.0003,
     "hard_floor_level": 0.50},
    {"name": "ETH", "series": "KXETH15M", "symbol": "ETH-USD", "entry_minutes": 3,
     "defensive_exit": 0.0005, "entry_filter": 0.00005, "adaptive_gap_min": 0.00005,
     "entry_confidence_floor": 0.58, "strict_direction_margin": 0.0002,
     "sweep_lookback_minutes": 30, "sweep_recent_minutes": 10, "sweep_margin": 0.001,
     "hard_floor_level": 0.50},
]

ADAPTIVE_GAP_SCALE_CONFIDENCE = 0.90


def adaptive_defensive_threshold(confidence, base_threshold, min_threshold):
    span = confidence - 0.5
    scale_span = ADAPTIVE_GAP_SCALE_CONFIDENCE - 0.5
    ratio = span / scale_span if scale_span else 1.0
    threshold = min_threshold + ratio * (base_threshold - min_threshold)
    return max(min_threshold, threshold)

CLOSE_5MIN_ENTRY_MINUTES_BEFORE_CLOSE = 5  # BTC-only paper comparison, holdout: 84.51% -> 86.65%
DEFENSIVE_EXIT_THRESHOLD_5MIN = 0.0004

MOMENTUM_JUMP_THRESHOLD = 0.001        # BTC-only. Walk-forward validated: 94.38% non-neg / +$25.55/$100
MOMENTUM_LOCK_IN = 0.02
MOMENTUM_TRAIL = 0.02
MOMENTUM_HARD_STOP = 0.01
MOMENTUM_MIN_SECONDS_LEFT = 90

POLL_SECONDS = 1
SETTLEMENT_GRACE_SECONDS = 30


def _ensure_files():
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(TRADES_PATH):
        with open(TRADES_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=TRADE_FIELDS).writeheader()
    if not os.path.exists(TICKS_PATH):
        with open(TICKS_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=TICK_FIELDS).writeheader()


def _append_row(path, fields, row):
    with open(path, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=fields).writerow(row)


def get_open_markets(series=SERIES, limit=10):
    data = _get(f"{BASE}/markets", params={"series_ticker": series, "status": "open", "limit": limit})
    return data.get("markets", [])


def get_active_market(series):
    markets = get_open_markets(series=series)
    now = dt.datetime.now(dt.timezone.utc)

    def close_dt(m):
        return dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))

    upcoming = [m for m in markets if close_dt(m) > now]
    return min(upcoming, key=close_dt) if upcoming else None


def get_market_snapshot(ticker):
    data = _get(f"{BASE}/markets/{ticker}")
    return data.get("market", data)


def get_kalshi_price(market):
    # bid/ask reflects the current book; last_price can go stale for seconds if
    # nothing has traded recently - see kalshi_live_bot.py for the real bug this caused.
    yes_bid = float(market.get("yes_bid_dollars", 0) or 0)
    yes_ask = float(market.get("yes_ask_dollars", 0) or 0)
    if yes_bid or yes_ask:
        return (yes_bid + yes_ask) / 2
    last_price = float(market.get("last_price_dollars", 0) or 0)
    return last_price if last_price else None


def get_latest_spot_return(symbol="BTC-USD"):
    df = fetch_klines(symbol=symbol, interval="1m", limit=5)
    df = drop_forming_candle(df, "1m")
    if len(df) < 2:
        return None, None
    last_close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])
    return last_close, (last_close / prev_close - 1)


def get_spot_closes(symbol, minutes_needed):
    limit = max(minutes_needed + 5, 10)
    df = fetch_klines(symbol=symbol, interval="1m", limit=limit)
    df = drop_forming_candle(df, "1m")
    if len(df) < 2:
        return None
    return [float(c) for c in df["close"].tolist()]


def had_recent_sweep(closes, lookback_minutes, recent_minutes, sweep_margin):
    """Validated: was there a swing high/low over the lookback window
    (excluding the most recent `recent_minutes`) that price then broke
    through within the most recent `recent_minutes`?"""
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


def log_entry(ticker, strategy, side, price, now):
    cost = price if side == "yes" else (1 - price)
    _append_row(TRADES_PATH, TRADE_FIELDS, {
        "ticker": ticker, "strategy": strategy, "entry_time": now.isoformat(),
        "entry_price": price, "side": side, "cost": cost,
        "exit_time": "", "exit_price": "", "exit_reason": "", "realized_payoff": "", "result": "",
    })
    print(f"[ENTRY:{strategy}] {ticker} side={side} price={price:.3f} cost={cost:.3f}")
    return {"side": side, "entry_price": price, "cost": cost, "entry_time": now, "peak_payoff": 0.0}


def log_exit(ticker, strategy, exit_price, reason, cost, side, now, result="", size_multiplier=1.0):
    exit_side_price = exit_price if side == "yes" else (1 - exit_price)
    # size_multiplier scales the realized payoff to reflect a bigger position (e.g.
    # win-streak scaling) without changing the logged per-unit cost/price - those
    # stay as true market prices for auditing.
    payoff = (exit_side_price - cost) * size_multiplier
    _append_row(TRADES_PATH, TRADE_FIELDS, {
        "ticker": ticker, "strategy": strategy, "entry_time": "", "entry_price": "",
        "side": side, "cost": cost, "exit_time": now.isoformat(), "exit_price": exit_price,
        "exit_reason": reason, "realized_payoff": payoff, "result": result,
    })
    print(f"[EXIT:{strategy}] {ticker} reason={reason} exit_price={exit_price:.3f} payoff={payoff:+.3f}")
    return payoff


def load_win_streak():
    if os.path.exists(WIN_STREAK_PATH):
        with open(WIN_STREAK_PATH) as f:
            state = json.load(f)
        state.setdefault("win_streak", 0)
        state.setdefault("size_multiplier", 1.0)
        return state
    return {"win_streak": 0, "size_multiplier": 1.0}


def save_win_streak(state):
    with open(WIN_STREAK_PATH, "w") as f:
        json.dump(state, f)


def record_close_favorite_result(state, payoff):
    if payoff >= 0:
        state["win_streak"] = state.get("win_streak", 0) + 1
        if state["win_streak"] % WIN_STREAK_SCALE_TRIGGER == 0:
            state["size_multiplier"] = state.get("size_multiplier", 1.0) * (1 + WIN_STREAK_SCALE_PCT)
            print(f"*** WIN STREAK (paper, BTC): {state['win_streak']} close_favorite wins in a row - "
                  f"position size multiplier now {state['size_multiplier']:.3f}x ***")
    else:
        if state.get("win_streak", 0) > 0:
            print(f"Win streak (paper, BTC) broken at {state['win_streak']} "
                  f"(size multiplier stays {state.get('size_multiplier', 1.0):.3f}x)")
        state["win_streak"] = 0


def load_thresholds():
    defaults = {
        "momentum_jump_threshold": MOMENTUM_JUMP_THRESHOLD,
        "momentum_lock_in": MOMENTUM_LOCK_IN,
        "momentum_trail": MOMENTUM_TRAIL,
        "momentum_hard_stop": MOMENTUM_HARD_STOP,
    }
    if os.path.exists(THRESHOLDS_PATH):
        with open(THRESHOLDS_PATH) as f:
            defaults.update(json.load(f))
    return defaults


def process_close_favorite_asset(asset, positions, now, win_streak_state=None):
    """Runs the validated close_favorite design (3min entry, defensive exit, entry
    filter) for one asset (BTC or ETH), using that asset's own validated thresholds."""
    strategy_name = "close_favorite" if asset["name"] == "BTC" else f"close_favorite_{asset['name']}"

    active = get_active_market(asset["series"])
    if active is None:
        return
    ticker = active["ticker"]
    close_time = dt.datetime.fromisoformat(active["close_time"].replace("Z", "+00:00"))
    seconds_left = (close_time - now).total_seconds()
    kalshi_price = get_kalshi_price(active)
    spot_closes = get_spot_closes(asset["symbol"], asset.get("sweep_lookback_minutes", 10))
    spot_ret = spot_closes[-1] / spot_closes[-2] - 1 if spot_closes and len(spot_closes) >= 2 else None

    if ticker not in positions:
        positions[ticker] = {"close_time": close_time, "asset": asset["name"], "leg": None, "exited": False}
    pos = positions[ticker]

    if kalshi_price is not None:
        _append_row(TICKS_PATH, TICK_FIELDS, {
            "timestamp": now.isoformat(), "ticker": ticker,
            "spot_price": None, "spot_ret_1m": spot_ret, "kalshi_price": kalshi_price,
        })

    if pos["leg"] is None and not pos["exited"] and kalshi_price is not None and kalshi_price != 0.5:
        if 0 < seconds_left <= asset["entry_minutes"] * 60:
            side = "yes" if kalshi_price > 0.5 else "no"
            confidence = max(kalshi_price, 1 - kalshi_price)
            strict_margin = asset.get("strict_direction_margin")
            if strict_margin is not None:
                # validated: require spot moving FAVORABLY with our side, not
                # just not-against it - see kalshi_live_bot.py for the numbers.
                entry_ok = spot_ret is not None and (
                    (side == "yes" and spot_ret >= strict_margin) or
                    (side == "no" and spot_ret <= -strict_margin)
                )
            else:
                entry_adverse = spot_ret is not None and (
                    (side == "yes" and spot_ret <= -asset["entry_filter"]) or
                    (side == "no" and spot_ret >= asset["entry_filter"])
                )
                entry_ok = not entry_adverse
            swept = had_recent_sweep(spot_closes, asset.get("sweep_lookback_minutes", 10),
                                      asset.get("sweep_recent_minutes", 2), asset.get("sweep_margin", 0.0003))
            if confidence < asset.get("entry_confidence_floor", 0.0):
                pass  # waiting for confidence to strengthen before entering
            elif swept:
                pass  # spot just swept a recent high/low - wait for it to settle
            elif entry_ok:
                pos["leg"] = log_entry(ticker, strategy_name, side, kalshi_price, now)

    if pos["leg"] is not None and kalshi_price is not None:
        leg = pos["leg"]
        confidence = max(kalshi_price, 1 - kalshi_price)
        price_side_now_check = kalshi_price if leg["side"] == "yes" else (1 - kalshi_price)
        hard_floor_hit = price_side_now_check <= leg["cost"] * asset.get("hard_floor_level", 0.0)

        adverse = False
        if spot_ret is not None:
            active_threshold = adaptive_defensive_threshold(
                confidence, asset["defensive_exit"], asset["adaptive_gap_min"])
            adverse = (leg["side"] == "yes" and spot_ret <= -active_threshold) or \
                      (leg["side"] == "no" and spot_ret >= active_threshold)

        if adverse or hard_floor_hit:
            exit_reason = "hard_floor" if hard_floor_hit and not adverse else "defensive_exit"
            size_multiplier = win_streak_state.get("size_multiplier", 1.0) \
                if (asset["name"] == "BTC" and win_streak_state is not None) else 1.0
            payoff = log_exit(ticker, strategy_name, kalshi_price, exit_reason, leg["cost"], leg["side"], now,
                               size_multiplier=size_multiplier)
            if asset["name"] == "BTC" and win_streak_state is not None:
                record_close_favorite_result(win_streak_state, payoff)
            pos["leg"] = None
            pos["exited"] = True


def settle_close_favorite_positions(positions, now, win_streak_state=None):
    for ticker in list(positions.keys()):
        pos = positions[ticker]
        if (now - pos["close_time"]).total_seconds() < SETTLEMENT_GRACE_SECONDS:
            continue
        if pos["leg"] is None:
            del positions[ticker]
            continue
        snap = get_market_snapshot(ticker)
        result = snap.get("result")
        if result not in ("yes", "no"):
            continue
        yes_settlement_price = 1.0 if result == "yes" else 0.0
        leg = pos["leg"]
        strategy_name = "close_favorite" if pos["asset"] == "BTC" else f"close_favorite_{pos['asset']}"
        size_multiplier = win_streak_state.get("size_multiplier", 1.0) \
            if (pos["asset"] == "BTC" and win_streak_state is not None) else 1.0
        payoff = log_exit(ticker, strategy_name, yes_settlement_price, "settlement", leg["cost"], leg["side"], now,
                           result, size_multiplier=size_multiplier)
        if pos["asset"] == "BTC" and win_streak_state is not None:
            record_close_favorite_result(win_streak_state, payoff)
        del positions[ticker]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    _ensure_files()
    print("Paper-trading bot started. No real orders are ever placed. "
          f"Logging to {TRADES_PATH} and {TICKS_PATH}. "
          f"Tracking close_favorite for: {[a['name'] for a in CLOSE_FAVORITE_ASSETS]}")

    close_favorite_positions = {}
    btc_extra_positions = {}  # close_favorite_5min + momentum, BTC-only

    while True:
        cfg = load_thresholds()
        win_streak_state = load_win_streak()
        try:
            now = dt.datetime.now(dt.timezone.utc)

            for asset in CLOSE_FAVORITE_ASSETS:
                process_close_favorite_asset(asset, close_favorite_positions, now, win_streak_state)
            settle_close_favorite_positions(close_favorite_positions, now, win_streak_state)
            save_win_streak(win_streak_state)

            # --- BTC-only extras: close_favorite_5min and momentum_jump ---
            active = get_active_market("KXBTC15M")
            spot_price, spot_ret = get_latest_spot_return("BTC-USD")

            if active is not None:
                ticker = active["ticker"]
                close_time = dt.datetime.fromisoformat(active["close_time"].replace("Z", "+00:00"))
                seconds_left = (close_time - now).total_seconds()
                kalshi_price = get_kalshi_price(active)

                if ticker not in btc_extra_positions:
                    btc_extra_positions[ticker] = {
                        "close_time": close_time, "close_favorite_5min": None,
                        "momentum": None, "momentum_triggered": False,
                    }
                pos = btc_extra_positions[ticker]

                if pos["close_favorite_5min"] is None and kalshi_price is not None and kalshi_price != 0.5:
                    if 0 < seconds_left <= CLOSE_5MIN_ENTRY_MINUTES_BEFORE_CLOSE * 60:
                        side = "yes" if kalshi_price > 0.5 else "no"
                        pos["close_favorite_5min"] = log_entry(ticker, "close_favorite_5min", side, kalshi_price, now)

                leg5 = pos["close_favorite_5min"]
                if leg5 is not None and kalshi_price is not None and spot_ret is not None:
                    adverse = (leg5["side"] == "yes" and spot_ret <= -DEFENSIVE_EXIT_THRESHOLD_5MIN) or \
                              (leg5["side"] == "no" and spot_ret >= DEFENSIVE_EXIT_THRESHOLD_5MIN)
                    if adverse:
                        log_exit(ticker, "close_favorite_5min", kalshi_price, "defensive_exit",
                                  leg5["cost"], leg5["side"], now)
                        pos["close_favorite_5min"] = None

                if (not pos["momentum_triggered"] and kalshi_price is not None
                        and spot_ret is not None and seconds_left > MOMENTUM_MIN_SECONDS_LEFT
                        and abs(spot_ret) >= cfg["momentum_jump_threshold"] and 0 < kalshi_price < 1):
                    side = "yes" if spot_ret > 0 else "no"
                    pos["momentum"] = log_entry(ticker, "momentum_jump", side, kalshi_price, now)
                    pos["momentum_triggered"] = True

                if pos["momentum"] is not None and kalshi_price is not None:
                    m = pos["momentum"]
                    price_side_now = kalshi_price if m["side"] == "yes" else (1 - kalshi_price)
                    payoff_now = price_side_now - m["cost"]
                    m["peak_payoff"] = max(m["peak_payoff"], payoff_now)

                    triggered, reason = False, None
                    if m["peak_payoff"] >= cfg["momentum_lock_in"]:
                        if payoff_now <= m["peak_payoff"] - cfg["momentum_trail"]:
                            triggered, reason = True, "trailing_stop"
                    else:
                        if payoff_now <= -cfg["momentum_hard_stop"]:
                            triggered, reason = True, "breakeven_stop"

                    if triggered:
                        log_exit(ticker, "momentum_jump", kalshi_price, reason, m["cost"], m["side"], now)
                        pos["momentum"] = None

            for ticker in list(btc_extra_positions.keys()):
                pos = btc_extra_positions[ticker]
                if (now - pos["close_time"]).total_seconds() < SETTLEMENT_GRACE_SECONDS:
                    continue
                if pos["close_favorite_5min"] is None and pos["momentum"] is None:
                    del btc_extra_positions[ticker]
                    continue
                snap = get_market_snapshot(ticker)
                result = snap.get("result")
                if result not in ("yes", "no"):
                    continue
                yes_settlement_price = 1.0 if result == "yes" else 0.0
                if pos["close_favorite_5min"] is not None:
                    c5 = pos["close_favorite_5min"]
                    log_exit(ticker, "close_favorite_5min", yes_settlement_price, "settlement",
                              c5["cost"], c5["side"], now, result)
                if pos["momentum"] is not None:
                    m = pos["momentum"]
                    log_exit(ticker, "momentum_jump", yes_settlement_price, "settlement",
                              m["cost"], m["side"], now, result)
                del btc_extra_positions[ticker]

        except Exception as exc:
            print(f"  poll error: {exc}")

        if args.once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
