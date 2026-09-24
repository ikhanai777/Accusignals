"""Market data from the Binance public REST API (spot and USD-M futures).

Only real exchange data is used. No API key is needed because signals rely on
public market-data endpoints; if ``BINANCE_API_KEY`` is set it is sent as a
header (harmless, and some network setups prefer authenticated traffic).
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pandas as pd
import requests

ENDPOINTS = {
    "spot": {
        "base": "https://api.binance.com",
        "klines": "/api/v3/klines",
        "agg": "/api/v3/aggTrades",
        "ticker": "/api/v3/ticker/24hr",
        "info": "/api/v3/exchangeInfo",
        "price": "/api/v3/ticker/price",
        "time": "/api/v3/time",
        "max_limit": 1000,
    },
    "futures": {
        "base": "https://fapi.binance.com",
        "klines": "/fapi/v1/klines",
        "agg": "/fapi/v1/aggTrades",
        "ticker": "/fapi/v1/ticker/24hr",
        "info": "/fapi/v1/exchangeInfo",
        "price": "/fapi/v2/ticker/price",
        "time": "/fapi/v1/time",
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
log = logging.getLogger("accusignals")

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
        key = os.getenv("BINANCE_API_KEY")
        if key and hasattr(self.http, "headers"):
            self.http.headers["X-MBX-APIKEY"] = key
        self.timeout = timeout
        self.max_retries = max_retries
        self.clock_offset_ms = 0  # server time - local time

    def _get(self, path: str, params: dict):
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                r = self.http.get(self.base + path, params=params, timeout=self.timeout)
            except requests.RequestException as exc:  # network trouble: retry with backoff
                if attempt == self.max_retries:
                    raise
                log.warning("GET %s failed (%s), retrying in %.0fs", path, exc, delay)
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code in (418, 429) or r.status_code >= 500:  # rate limit / exchange hiccup
                if attempt == self.max_retries:
                    r.raise_for_status()
                wait = float(r.headers.get("Retry-After", delay))
                log.warning("GET %s -> HTTP %s, backing off %.0fs", path, r.status_code, wait)
                time.sleep(wait)
                delay *= 2
                continue
            if r.status_code >= 400:  # bad symbol/params: retrying won't help
                raise requests.HTTPError(f"{r.status_code} {r.text[:200]}", response=r)
            return r.json()
        raise RuntimeError("unreachable")

    def sync_time(self) -> int:
        """Measure the local clock's offset from Binance. Windows clocks often
        drift by seconds, which would make us read a still-forming candle as
        closed (or wait too long)."""
        t0 = time.time() * 1000
        server = self._get(self.cfg["time"], {})["serverTime"]
        t1 = time.time() * 1000
        self.clock_offset_ms = int(server - (t0 + t1) / 2)
        return self.clock_offset_ms

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.clock_offset_ms

    def now(self) -> pd.Timestamp:
        return pd.Timestamp(self.now_ms(), unit="ms", tz="UTC")

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
        end_ms = end_ms or self.now_ms()
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

    def update(self, df: pd.DataFrame, symbol: str, interval: str, keep: int | None = None) -> pd.DataFrame:
        """Append candles newer than ``df`` (one small request in steady state)
        instead of re-downloading the whole window every scan."""
        if df.empty:
            raise ValueError("update() needs an existing frame; use history() first")
        start = int(df.index[-1].value // 1_000_000)  # re-fetch the last bar: it may have been forming
        new = self.klines(symbol, interval, self.cfg["max_limit"], start_ms=start)
        out = pd.concat([df, new])
        out = out[~out.index.duplicated(keep="last")].sort_index()
        if len(new) >= self.cfg["max_limit"]:  # fell far behind: page the rest
            out = self.history(symbol, interval, (self.now_ms() - start) / 86_400_000 + 0.01).combine_first(out)
        return out.iloc[-keep:] if keep else out

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

    def prices(self, symbols: list[str] | None = None) -> dict[str, float]:
        """Latest traded price per symbol (one request for all symbols)."""
        rows = self._get(self.cfg["price"], {})
        want = set(symbols) if symbols else None
        return {r["symbol"]: float(r["price"]) for r in rows if want is None or r["symbol"] in want}

    def tradable_symbols(self, quote: str = "USDT") -> set[str]:
        """Symbols currently trading (futures: perpetuals only). The 24h ticker
        still lists delisted/settling contracts, so filter against this."""
        info = self._get(self.cfg["info"], {})
        out = set()
        for s in info["symbols"]:
            if s.get("status") != "TRADING" or s.get("quoteAsset") != quote:
                continue
            if self.market == "futures" and s.get("contractType") != "PERPETUAL":
                continue
            out.add(s["symbol"])
        return out

    def top_symbols(self, n: int = 20, quote: str = "USDT", min_quote_volume: float = 50e6) -> list[str]:
        """Most liquid pairs by 24h quote volume: tight spreads matter more for
        scalping than anything else."""
        tradable = self.tradable_symbols(quote)
        rows = self._get(self.cfg["ticker"], {})
        picks = []
        for r in rows:
            sym = r["symbol"]
            if sym not in tradable or not sym.endswith(quote):
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


def drop_unclosed(df: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    """Binance returns the still-forming candle last; signals must use closed
    bars only. Pass exchange time (``BinanceClient.now()``), not local time."""
    if df.empty or "close_time" not in df:
        return df
    return df[df["close_time"] < now]


def missing_bars(df: pd.DataFrame, interval: str) -> int:
    """Count gaps in the candle sequence (exchange maintenance, delistings)."""
    if len(df) < 2:
        return 0
    expected = (df.index[-1] - df.index[0]) / pd.Timedelta(pandas_freq(interval)) + 1
    return int(round(expected)) - len(df)


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


def load_history(client: BinanceClient, symbols: list[str], interval: str, days: int, data_dir: str | Path,
                 refresh: bool = False, min_bars: int = 500) -> dict[str, pd.DataFrame]:
    """Real Binance history per symbol. A local CSV cache is topped up with the
    newest closed candles on every call, so it is never stale."""
    data = {}
    now = client.now()
    for sym in symbols:
        path = Path(data_dir) / client.market / f"{sym}_{interval}.csv"
        if path.exists() and not refresh:
            df = load_csv(path)
            if df.index[0] > now - pd.Timedelta(days=days):  # cache too short: fetch the full window
                df = client.history(sym, interval, days)
            else:
                df = client.update(df, sym, interval)
        else:
            log.info("downloading %s %s %dd from Binance %s", sym, interval, days, client.market)
            df = client.history(sym, interval, days)
        df = df.drop(columns=["close_time"], errors="ignore")
        df = df[df.index + pd.Timedelta(pandas_freq(interval)) <= now]  # closed candles only
        save_csv(df, path)
        df = df[df.index >= now - pd.Timedelta(days=days)]
        gaps = missing_bars(df, interval)
        if gaps:
            log.warning("%s: %d missing candles in history (exchange downtime)", sym, gaps)
        if len(df) < min_bars:
            log.warning("%s: only %d candles, skipping", sym, len(df))
            continue
        data[sym] = df
    return data
