import os
import time
import datetime as dt

import requests
import pandas as pd

CANDLES_URL = "https://api.exchange.coinbase.com/products/{symbol}/candles"
HEADERS = {"User-Agent": "crypto-15m-predictor/1.0"}

# seconds per candle, and Coinbase's supported granularities
GRANULARITY_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "1d": 86400,
}
INTERVAL_MS = {k: v * 1000 for k, v in GRANULARITY_SECONDS.items()}

MAX_CANDLES_PER_REQUEST = 300
MAX_RETRIES = 6
RETRY_BACKOFF_SECONDS = 2  # doubles each attempt: 2,4,8,16,32,64

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data_cache")


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()


def _request_candles(symbol, granularity, start_ts, end_ts):
    params = {"granularity": granularity, "start": _iso(start_ts), "end": _iso(end_ts)}
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(
                CANDLES_URL.format(symbol=symbol), params=params, headers=HEADERS, timeout=20
            )
        except requests.exceptions.RequestException as exc:
            last_error = exc
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = requests.exceptions.HTTPError(
                    f"{resp.status_code} from Coinbase", response=resp
                )
            else:
                resp.raise_for_status()
                return resp.json()  # rows: [time, low, high, open, close, volume], newest first

        sleep_for = RETRY_BACKOFF_SECONDS * (2 ** attempt)
        print(f"  request failed ({last_error}); retrying in {sleep_for}s "
              f"(attempt {attempt + 1}/{MAX_RETRIES})")
        time.sleep(sleep_for)

    raise last_error


def _to_frame(raw, granularity):
    if not raw:
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume", "close_time"])
    df = pd.DataFrame(raw, columns=["time", "low", "high", "open", "close", "volume"])
    df = df.sort_values("time").reset_index(drop=True)
    df["open_time"] = (df["time"] * 1000).astype("int64")
    df["close_time"] = ((df["time"] + granularity) * 1000 - 1).astype("int64")
    df = df[["open_time", "open", "high", "low", "close", "volume", "close_time"]]
    df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df


def fetch_klines(symbol="BTC-USD", interval="15m", limit=300):
    granularity = GRANULARITY_SECONDS[interval]
    now = int(time.time())
    start_ts = now - min(limit, MAX_CANDLES_PER_REQUEST) * granularity
    raw = _request_candles(symbol, granularity, start_ts, now)
    return _to_frame(raw, granularity)


def _cache_path(symbol, interval):
    safe_symbol = symbol.replace("/", "-")
    return os.path.join(CACHE_DIR, f"{safe_symbol}_{interval}.pkl")


def _load_cache(symbol, interval):
    path = _cache_path(symbol, interval)
    if os.path.exists(path):
        return pd.read_pickle(path)
    return None


def _save_cache(df, symbol, interval):
    os.makedirs(CACHE_DIR, exist_ok=True)
    df.to_pickle(_cache_path(symbol, interval))


def fetch_history(symbol="BTC-USD", interval="15m", days=180, use_cache=True):
    if interval not in GRANULARITY_SECONDS:
        raise ValueError(f"unsupported interval: {interval}")

    granularity = GRANULARITY_SECONDS[interval]
    now = int(time.time())
    start_ts = now - days * 86_400
    chunk_seconds = MAX_CANDLES_PER_REQUEST * granularity

    cached = _load_cache(symbol, interval) if use_cache else None
    frames = []
    cursor = start_ts

    if cached is not None and not cached.empty:
        # Trust the cache and only fetch forward from its tail. (This does not
        # backfill further into the past than whatever the cache already has -
        # fine here since the cache already covers the exchange's full history,
        # but note if you ever need MORE history than a smaller earlier cache holds.)
        frames.append(cached)
        cached_max_close_ms = int(cached["close_time"].max())
        cursor = cached_max_close_ms // 1000 + 1
        print(f"Resuming from cache: {len(cached)} candles already on disk "
              f"(up to {dt.datetime.fromtimestamp(cached_max_close_ms / 1000, tz=dt.timezone.utc)}).")

    def _flush():
        merged = pd.concat(frames, ignore_index=True)
        merged = merged.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
        if use_cache:
            _save_cache(merged, symbol, interval)
        return merged

    requests_made = 0
    while cursor < now:
        chunk_end = min(cursor + chunk_seconds, now)
        raw = _request_candles(symbol, granularity, cursor, chunk_end)
        if raw:
            frames.append(_to_frame(raw, granularity))
        cursor = chunk_end
        requests_made += 1

        if requests_made % 25 == 0 and frames:
            _flush()
            print(f"  ...checkpointed at {dt.datetime.fromtimestamp(cursor, tz=dt.timezone.utc)} "
                  f"({requests_made} requests this run)")

        time.sleep(0.3)  # stay well under the public rate limit

    if not frames:
        return _to_frame([], granularity)

    return _flush()


def drop_forming_candle(df, interval):
    """The most recent candle may still be forming; drop it so predictions
    are only made from fully closed candles."""
    if df.empty:
        return df
    now_ms = int(time.time() * 1000)
    if df.iloc[-1]["close_time"] > now_ms:
        return df.iloc[:-1].reset_index(drop=True)
    return df
