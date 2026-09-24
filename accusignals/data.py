"""Market data: Binance public REST (spot + USDⓈ-M futures), CSV cache, and a
synthetic generator used by the tests.

No API key is needed: signals only use public market-data endpoints.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ENDPOINTS = {
    "spot": {
        "base": "https://api.binance.com",
        "klines": "/api/v3/klines",
        "agg": "/api/v3/aggTrades",
        "ticker": "/api/v3/ticker/24hr",
        "max_limit": 1000,
    },
    "futures": {
        "base": "https://fapi.binance.com",
        "klines": "/fapi/v1/klines",
        "agg": "/fapi/v1/aggTrades",
        "ticker": "/fapi/v1/ticker/24hr",
        "max_limit": 1500,
    },
}

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}

KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]

# Stablecoin / pegged bases and leveraged tokens are useless for scalping.
EXCLUDED_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR", "AEUR", "USDE", "EURI", "PAXG", "WBTC", "XUSD", "USD1"}


def pandas_freq(interval: str) -> str:
    return interval[:-1] + {"m": "min", "h": "h", "d": "D"}[interval[-1]]


class BinanceClient:
    def __init__(self, market: str = "futures", base_url: str | None = None, session: requests.Session | None = None,
                 timeout: float = 10.0, max_retries: int = 4):
        if market not in ENDPOINTS:
            raise ValueError(f"market must be one of {list(ENDPOINTS)}")
        self.market = market
        self.cfg = ENDPOINTS[market]
        self.base = base_url or self.cfg["base"]
        self.http = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries

    def _get(self, path: str, params: dict):
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                r = self.http.get(self.base + path, params=params, timeout=self.timeout)
                if r.status_code in (418, 429):  # rate limited: back off
                    time.sleep(float(r.headers.get("Retry-After", delay)))
                    delay *= 2
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException:
                if attempt == self.max_retries:
                    raise
                time.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    def klines(self, symbol: str, interval: str, limit: int = 500, start_ms: int | None = None,
               end_ms: int | None = None) -> pd.DataFrame:
        params = {"symbol": symbol, "interval": interval, "limit": min(limit, self.cfg["max_limit"])}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        return klines_to_frame(self._get(self.cfg["klines"], params))

    def history(self, symbol: str, interval: str, days: float, end_ms: int | None = None) -> pd.DataFrame:
        """Page through klines to get ``days`` of history."""
        step = INTERVAL_MS[interval]
        end_ms = end_ms or int(time.time() * 1000)
        start = end_ms - int(days * 86_400_000)
        frames = []
        while start < end_ms:
            df = self.klines(symbol, interval, self.cfg["max_limit"], start_ms=start, end_ms=end_ms)
            if df.empty:
                break
            frames.append(df)
            start = int(df.index[-1].value // 1_000_000) + step
            time.sleep(0.05)
        if not frames:
            return klines_to_frame([])
        out = pd.concat(frames)
        return out[~out.index.duplicated(keep="last")].sort_index()

    def agg_trades(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        rows, cursor = [], start_ms
        while cursor < end_ms:
            batch = self._get(self.cfg["agg"], {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000})
            if not batch:
                break
            rows.extend(batch)
            last = batch[-1]["T"]
            if len(batch) < 1000 or last <= cursor:
                break
            cursor = last + 1
        if not rows:
            return pd.DataFrame(columns=["price", "qty", "is_buyer_maker"])
        t = pd.DataFrame(rows)
        out = pd.DataFrame(
            {"price": t["p"].astype(float), "qty": t["q"].astype(float), "is_buyer_maker": t["m"].astype(bool)}
        )
        out.index = pd.to_datetime(t["T"], unit="ms", utc=True)
        return out

    def top_symbols(self, n: int = 20, quote: str = "USDT", min_quote_volume: float = 50e6) -> list[str]:
        """Most liquid pairs by 24h quote volume: tight spreads matter more for
        scalping than anything else."""
        rows = self._get(self.cfg["ticker"], {})
        picks = []
        for r in rows:
            sym = r["symbol"]
            if not sym.endswith(quote):
                continue
            base = sym[: -len(quote)]
            if base in EXCLUDED_BASES or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
                continue
            qv = float(r.get("quoteVolume", 0))
            if qv >= min_quote_volume:
                picks.append((qv, sym))
        return [s for _, s in sorted(picks, reverse=True)[:n]]


def klines_to_frame(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=KLINE_COLS)
    if df.empty:
        df.index = pd.DatetimeIndex([], tz="UTC", name="open_time")
        return df[["open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_volume"]].astype(float)
    df.index = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
    df.index.name = "open_time"
    df["close_time"] = pd.to_datetime(df["close_time"].astype("int64"), unit="ms", utc=True)
    cols = ["open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_volume"]
    out = df[cols].astype(float)
    out["close_time"] = df["close_time"]
    return out


def drop_unclosed(df: pd.DataFrame, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Binance returns the still-forming candle last; signals must use closed bars only."""
    if df.empty or "close_time" not in df:
        return df
    now = now or pd.Timestamp.now(tz="UTC")
    return df[df["close_time"] < now]


def save_csv(df: pd.DataFrame, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path)


def load_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "open_time"
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    if "close_time" in df:
        df = df.drop(columns=["close_time"])
    return df


def synthetic_ohlcv(n: int = 5000, interval: str = "5m", seed: int = 7, start: str = "2024-01-01",
                    price: float = 100.0) -> pd.DataFrame:
    """Regime-switching random walk with volatility clustering and a
    taker-buy split correlated with returns. Used for tests and demos only:
    it has no real edge, so results on it say nothing about live markets."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq=pandas_freq(interval), tz="UTC", name="open_time")
    regime_drift = np.repeat(rng.normal(0, 0.0004, n // 200 + 1), 200)[:n]
    vol = np.empty(n)
    v = 0.002
    for i in range(n):
        v = 0.0005 + 0.94 * v + 0.05 * abs(rng.normal(0, 0.002))
        vol[i] = v
    rets = regime_drift + rng.standard_normal(n) * vol * 0.6
    close = price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[price], close[:-1]])
    wick = np.abs(rng.normal(0, 1, (n, 2))) * vol[:, None] * close[:, None] * 0.5
    high = np.maximum(open_, close) + wick[:, 0]
    low = np.minimum(open_, close) - wick[:, 1]
    volume = rng.lognormal(3, 0.5, n) * (1 + 150 * np.abs(rets))
    buy_share = np.clip(0.5 + rets / (vol * 4) + rng.normal(0, 0.05, n), 0.02, 0.98)
    return pd.DataFrame(
        {
            "open": open_, "high": high, "low": low, "close": close, "volume": volume,
            "quote_volume": volume * close, "trades": (volume * 10).round(),
            "taker_buy_volume": volume * buy_share,
        },
        index=idx,
    )
