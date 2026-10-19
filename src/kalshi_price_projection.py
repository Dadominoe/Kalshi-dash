"""Gives real numbers - not a fake single-point prediction, and not the
overestimated theoretical volatility model this used before. Uses the ACTUAL
empirical distribution of how far BTC/ETH have really moved over each
horizon, measured across ~78,000 real 1-min bars (45 days). Validated: the
old sqrt(time)-volatility-scaling formula was measurably too wide (a "68%"
range actually captured 72.1% of real outcomes) - these percentiles are
taken directly from real historical moves, so they're calibrated to what
actually happens, not a theoretical assumption. Read-only - never places
any orders.
"""
import argparse

from src.data import fetch_klines, drop_forming_candle

HORIZONS_MINUTES = [5, 10, 15, 30]

# empirical absolute-move percentiles, measured from real 1-min bars over the
# last 45 days (2026-08-11 pull). Re-measure periodically - these will drift
# as market volatility regimes change over time.
EMPIRICAL_MOVES = {
    "BTC-USD": {
        5: {"p50": 29.62, "p68": 48.02, "p90": 100.41},
        10: {"p50": 42.07, "p68": 67.98, "p90": 141.75},
        15: {"p50": 51.87, "p68": 83.77, "p90": 173.49},
        30: {"p50": 75.28, "p68": 120.61, "p90": 251.35},
    },
    "ETH-USD": {
        5: {"p50": 1.11, "p68": 1.79, "p90": 3.71},
        10: {"p50": 1.55, "p68": 2.48, "p90": 5.14},
        15: {"p50": 1.90, "p68": 3.06, "p90": 6.34},
        30: {"p50": 2.68, "p68": 4.36, "p90": 9.11},
    },
    # measured 2026-08-12 pull, ~36 days of NEAR's Coinbase history (shorter
    # than BTC/ETH's 45+ days since NEAR-USD is a newer listing). NEAR moves
    # ~3x more than BTC as a % of price, so BTC/ETH's numbers would badly
    # understate its real range. Re-measure periodically as more accumulates.
    "NEAR-USD": {
        5: {"p50": 0.0025, "p68": 0.0040, "p90": 0.0070},
        10: {"p50": 0.0037, "p68": 0.0050, "p90": 0.0100},
        15: {"p50": 0.0040, "p68": 0.0069, "p90": 0.0120},
        30: {"p50": 0.0060, "p68": 0.0090, "p90": 0.0170},
    },
    # measured 2026-08-12 pull, ~38 days of BNB's Coinbase history. BNB moves
    # ~1.19x BTC's volatility as a % of price - much closer to BTC than NEAR
    # is, but still its own real number, not borrowed from BTC.
    "BNB-USD": {
        5: {"p50": 0.36, "p68": 0.55, "p90": 1.05},
        10: {"p50": 0.51, "p68": 0.79, "p90": 1.51},
        15: {"p50": 0.62, "p68": 0.96, "p90": 1.85},
        30: {"p50": 0.88, "p68": 1.35, "p90": 2.60},
    },
    # measured 2026-08-13 pull, ~55+ days of XRP's Coinbase history. XRP moves
    # ~1.28x BTC's volatility as a % of price - similar territory to BNB.
    "XRP-USD": {
        5: {"p50": 0.00070, "p68": 0.00110, "p90": 0.00220},
        10: {"p50": 0.00100, "p68": 0.00150, "p90": 0.00300},
        15: {"p50": 0.00120, "p68": 0.00190, "p90": 0.00370},
        30: {"p50": 0.00170, "p68": 0.00270, "p90": 0.00540},
    },
    # measured 2026-09-01 pull, ~19 days of PAXG-USD's Coinbase history (a
    # gold-backed token, used as gold's spot-price proxy since gold itself
    # isn't on Coinbase - Kalshi's real KXGOLD15M market settles off Pyth's
    # gold feed, PAXG tracks it closely: ~$4344 vs. Kalshi's own $4330.88
    # strike at the same moment). Shorter history than BTC/ETH so this is a
    # rougher estimate - re-measure once more data accumulates.
    "PAXG-USD": {
        5: {"p50": 1.96, "p68": 3.36, "p90": 6.70},
        10: {"p50": 2.86, "p68": 4.83, "p90": 9.52},
        15: {"p50": 3.51, "p68": 5.97, "p90": 11.77},
        30: {"p50": 5.02, "p68": 8.52, "p90": 16.53},
    },
    # measured 2026-09-02 from real gold futures data (Yahoo Finance GC=F,
    # ~9200 1-min bars / 8 days) - replaced the PAXG token proxy above per
    # user request ("gold actually has a chart... instead of using a coin").
    # Almost identical to PAXG's numbers, which is reassuring re: how good a
    # proxy PAXG actually was, but this is gold's own real price now.
    "GOLD-FUT": {
        5: {"p50": 2.00, "p68": 3.10, "p90": 5.80},
        10: {"p50": 2.80, "p68": 4.30, "p90": 8.10},
        15: {"p50": 3.50, "p68": 5.30, "p90": 10.00},
        30: {"p50": 5.10, "p68": 7.60, "p90": 14.10},
    },
}


def project_range(current_price, symbol, horizon_minutes, percentile="p68"):
    """Empirical range: current price +/- the real historical absolute move
    at this percentile and horizon. No distributional assumption - this is
    literally what actually happened, that often."""
    moves = EMPIRICAL_MOVES.get(symbol, {}).get(horizon_minutes)
    if moves is None:
        return None, None
    half_width = moves[percentile]
    return current_price - half_width, current_price + half_width


def project_point_for_minutes(current_price, symbol, minutes_left, direction=None, percentile="p50"):
    """Single-number estimate anchored to the actual time left on a specific
    market (not a generic horizon bucket) - interpolated between the nearest
    empirical horizons we have real data for.

    Honest behavior: if `direction` is None (no validated directional lean),
    this just returns current_price - a random walk has no bias, so the only
    honest 'prediction' with no signal is 'about where it is now'. Only when
    `direction` is 'YES' or 'NO' (a call that already cleared the confidence
    floor + strict-direction bar elsewhere) does this offset the number - by
    the real empirical median move, in that direction.
    """
    moves = EMPIRICAL_MOVES.get(symbol)
    if moves is None or current_price is None:
        return None
    horizons = sorted(moves.keys())
    m = max(horizons[0], min(minutes_left, horizons[-1]))
    lo_h = max([h for h in horizons if h <= m], default=horizons[0])
    hi_h = min([h for h in horizons if h >= m], default=horizons[-1])
    if lo_h == hi_h:
        half_width = moves[lo_h][percentile]
    else:
        frac = (m - lo_h) / (hi_h - lo_h)
        half_width = moves[lo_h][percentile] + frac * (moves[hi_h][percentile] - moves[lo_h][percentile])
    if direction == "YES":
        return current_price + half_width
    if direction == "NO":
        return current_price - half_width
    return current_price


def show_projection(symbol):
    df = fetch_klines(symbol=symbol, interval="1m", limit=5)
    df = drop_forming_candle(df, "1m")
    if len(df) < 1:
        print(f"{symbol}: not enough recent data.")
        return
    current_price = float(df["close"].iloc[-1])

    print(f"\n=== {symbol}: current price ${current_price:,.2f} ===")
    print(f"(empirical ranges, measured from real historical price moves - not a theoretical model)\n")
    print(f"{'horizon':>10} | {'68% range (typical)':>26} | {'90% range (wide moves)':>26}")
    for h in HORIZONS_MINUTES:
        low68, high68 = project_range(current_price, symbol, h, "p68")
        low90, high90 = project_range(current_price, symbol, h, "p90")
        if low68 is None:
            continue
        print(f"{h:>7}min | ${low68:>10,.2f} - ${high68:<10,.2f} | ${low90:>10,.2f} - ${high90:<10,.2f}")

    print("\nHonest read: same as before - the center is just the current price, no directional lean is")
    print("implied here. But these ranges are now calibrated to what BTC/ETH actually do, not a formula's")
    print("assumption - median 15min BTC move is only ~$52, so treat any claim of a bigger 'sure thing'")
    print("move with real skepticism.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTC-USD", "ETH-USD"])
    args = parser.parse_args()
    for symbol in args.symbols:
        show_projection(symbol)


if __name__ == "__main__":
    main()
