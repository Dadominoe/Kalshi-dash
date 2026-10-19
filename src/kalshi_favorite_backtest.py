import time
import datetime as dt

import requests

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = "KXBTC15M"
HEADERS = {"User-Agent": "crypto-15m-predictor/1.0"}
ENTRY_SECONDS_BEFORE_CLOSE = 180  # "last three minutes"
N_MARKETS = 1000


def _get(url, params=None, retries=5):
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.exceptions.HTTPError(f"{resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            wait = 2 * (2 ** attempt)
            print(f"  request failed ({exc}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"failed after {retries} retries: {url}")


def fetch_settled_markets(n=N_MARKETS, series=SERIES):
    markets = []
    cursor = None
    while len(markets) < n:
        params = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _get(f"{BASE}/markets", params=params)
        batch = data.get("markets", [])
        if not batch:
            break
        markets.extend(batch)
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(0.15)
    # markets come back most-recent-first; keep only the newest n
    return markets[:n]


def fetch_entry_price(ticker, open_time_iso, close_time_iso):
    open_ts = int(dt.datetime.fromisoformat(open_time_iso.replace("Z", "+00:00")).timestamp())
    close_ts = int(dt.datetime.fromisoformat(close_time_iso.replace("Z", "+00:00")).timestamp())
    data = _get(
        f"{BASE}/series/{SERIES}/markets/{ticker}/candlesticks",
        params={"period_interval": 1, "start_ts": open_ts, "end_ts": close_ts},
    )
    candles = data.get("candlesticks", [])
    if not candles:
        return None
    target_ts = close_ts - ENTRY_SECONDS_BEFORE_CLOSE
    eligible = [c for c in candles if c["end_period_ts"] <= target_ts]
    chosen = eligible[-1] if eligible else candles[0]
    price = chosen.get("price", {}).get("close_dollars")
    return float(price) if price is not None else None


def run():
    print(f"Fetching last {N_MARKETS} settled {SERIES} markets...")
    markets = fetch_settled_markets(N_MARKETS)
    print(f"Got {len(markets)} settled markets.")

    correct = 0
    ties = 0
    missing = 0
    total = 0
    confidences = []

    for i, m in enumerate(markets):
        ticker = m["ticker"]
        result = m.get("result")  # "yes" or "no"
        if result not in ("yes", "no"):
            missing += 1
            continue

        price = fetch_entry_price(ticker, m["open_time"], m["close_time"])
        if price is None:
            missing += 1
            continue

        if price == 0.5:
            ties += 1
            continue

        favorite = "yes" if price > 0.5 else "no"
        confidence = max(price, 1 - price)
        confidences.append(confidence)
        total += 1
        if favorite == result:
            correct += 1

        if (i + 1) % 100 == 0:
            print(f"  ...{i + 1}/{len(markets)} processed")

        time.sleep(0.1)

    print("\n--- Results: buying the favorite with ~3 minutes left, KXBTC15M ---")
    print(f"Markets evaluated: {total} (missing/no-data: {missing}, exact 50/50 ties: {ties})")
    if total:
        print(f"Win rate: {correct / total:.4%}  ({correct}/{total})")
        print(f"Average favorite price (avg implied probability): {sum(confidences) / len(confidences):.4f}")


if __name__ == "__main__":
    run()
