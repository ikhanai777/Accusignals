"""Order-flow analytics: delta, CVD, divergences and footprint charts.

Binance klines carry ``taker_buy_volume`` (aggressive buys), so bar delta is
exact rather than estimated: ``delta = taker_buy - taker_sell``.
Footprints need tick data and are built from aggTrades (see ``data.py``).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def bar_delta(df: pd.DataFrame) -> pd.Series:
    if "taker_buy_volume" in df:
        return 2 * df["taker_buy_volume"] - df["volume"]
    # Fallback approximation when taker volume is unavailable (e.g. CSV from
    # another venue): allocate volume by where the close sits in the range.
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    return (((df["close"] - df["low"]) - (df["high"] - df["close"])) / rng).fillna(0) * df["volume"]


def delta_ratio(df: pd.DataFrame) -> pd.Series:
    """Delta as a fraction of volume in [-1, 1]; > 0.2 is strong buying."""
    return (bar_delta(df) / df["volume"].replace(0, np.nan)).fillna(0)


def cvd(df: pd.DataFrame, reset_daily: bool = True) -> pd.Series:
    d = bar_delta(df)
    if reset_daily:
        return d.groupby(df.index.floor("D")).cumsum()
    return d.cumsum()


def cvd_divergence(df: pd.DataFrame, lookback: int = 20):
    """Bullish: price prints a new ``lookback`` low while CVD does not (sellers
    are exhausted / absorbed). Bearish is the mirror image."""
    c = bar_delta(df).cumsum()
    low_prev = df["low"].shift(1).rolling(lookback).min()
    high_prev = df["high"].shift(1).rolling(lookback).max()
    cvd_low_prev = c.shift(1).rolling(lookback).min()
    cvd_high_prev = c.shift(1).rolling(lookback).max()
    bull = (df["low"] < low_prev) & (c > cvd_low_prev)
    bear = (df["high"] > high_prev) & (c < cvd_high_prev)
    return bull.fillna(False), bear.fillna(False)


def absorption(df: pd.DataFrame, vol_z: pd.Series, atr_s: pd.Series, z: float = 1.5, max_range_atr: float = 0.6):
    """Heavy volume that fails to move price: passive limit orders absorbing
    aggressive flow. Bullish when sellers were the aggressors (delta < 0) yet
    the bar closed in its upper half, and vice versa."""
    small = (df["high"] - df["low"]) <= max_range_atr * atr_s
    heavy = vol_z >= z
    d = bar_delta(df)
    mid = (df["high"] + df["low"]) / 2
    bull = heavy & small & (d < 0) & (df["close"] >= mid)
    bear = heavy & small & (d > 0) & (df["close"] <= mid)
    return bull.fillna(False), bear.fillna(False)


@dataclass
class FootprintBar:
    open_time: pd.Timestamp
    levels: pd.DataFrame  # index=price bucket, columns=bid_vol (sells), ask_vol (buys)
    delta: float
    poc: float  # price with the highest traded volume
    stacked_buy_imbalances: int
    stacked_sell_imbalances: int

    def summary(self) -> dict:
        return {
            "open_time": self.open_time,
            "delta": self.delta,
            "poc": self.poc,
            "stacked_buy_imb": self.stacked_buy_imbalances,
            "stacked_sell_imb": self.stacked_sell_imbalances,
        }


def _max_run(mask: np.ndarray) -> int:
    best = run = 0
    for v in mask:
        run = run + 1 if v else 0
        best = max(best, run)
    return best


def footprint(trades: pd.DataFrame, interval: str, tick: float, imbalance_ratio: float = 3.0) -> list[FootprintBar]:
    """Build footprint bars from trades.

    ``trades`` needs columns ``price``, ``qty``, ``is_buyer_maker`` and a
    DatetimeIndex. ``is_buyer_maker`` True means the taker *sold* (hit the bid).
    Diagonal imbalances compare ask volume at price P against bid volume at
    P - tick, the standard footprint convention.
    """
    if trades.empty:
        return []
    t = trades.copy()
    # Round before flooring so 100.1 / 0.1 = 1000.999... lands in bucket 1001.
    t["bucket"] = (np.floor((t["price"] / tick).round(6)) * tick).round(12)
    t["ask_vol"] = np.where(t["is_buyer_maker"], 0.0, t["qty"])
    t["bid_vol"] = np.where(t["is_buyer_maker"], t["qty"], 0.0)
    bars: list[FootprintBar] = []
    for open_time, g in t.groupby(t.index.floor(interval)):
        lv = g.groupby("bucket")[["bid_vol", "ask_vol"]].sum().sort_index()
        full = np.round(np.arange(lv.index.min(), lv.index.max() + tick / 2, tick), 12)
        lv = lv.reindex(full, fill_value=0.0)
        ask = lv["ask_vol"].to_numpy()
        bid = lv["bid_vol"].to_numpy()
        bid_below = np.concatenate([[0.0], bid[:-1]])
        ask_above = np.concatenate([ask[1:], [0.0]])
        buy_imb = ask >= imbalance_ratio * np.maximum(bid_below, 1e-12)
        sell_imb = bid >= imbalance_ratio * np.maximum(ask_above, 1e-12)
        buy_imb &= ask > 0
        sell_imb &= bid > 0
        total = ask + bid
        bars.append(
            FootprintBar(
                open_time=open_time,
                levels=lv,
                delta=float(ask.sum() - bid.sum()),
                poc=float(lv.index[int(np.argmax(total))]),
                stacked_buy_imbalances=_max_run(buy_imb),
                stacked_sell_imbalances=_max_run(sell_imb),
            )
        )
    return bars


def footprint_bias(bars: list[FootprintBar], min_stack: int = 3) -> int:
    """+1 / -1 / 0 read of the most recent footprint bar: stacked imbalances
    in one direction with agreeing delta."""
    if not bars:
        return 0
    b = bars[-1]
    if b.stacked_buy_imbalances >= min_stack and b.delta > 0:
        return 1
    if b.stacked_sell_imbalances >= min_stack and b.delta < 0:
        return -1
    return 0


def volume_profile(df: pd.DataFrame, bins: int = 50, value_area: float = 0.7) -> dict:
    """Volume-at-price over a window of candles (volume spread evenly across
    each candle's range). Returns POC and value-area high/low."""
    lo, hi = df["low"].min(), df["high"].max()
    if not np.isfinite(lo) or hi <= lo:
        return {"poc": np.nan, "vah": np.nan, "val": np.nan}
    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    hist = np.zeros(bins)
    for l, h, v in zip(df["low"].to_numpy(), df["high"].to_numpy(), df["volume"].to_numpy()):
        mask = (centers >= l) & (centers <= h)
        k = mask.sum()
        if k:
            hist[mask] += v / k
    poc_i = int(np.argmax(hist))
    target = hist.sum() * value_area
    lo_i = hi_i = poc_i
    acc = hist[poc_i]
    while acc < target and (lo_i > 0 or hi_i < bins - 1):
        down = hist[lo_i - 1] if lo_i > 0 else -1
        up = hist[hi_i + 1] if hi_i < bins - 1 else -1
        if up >= down:
            hi_i += 1
            acc += hist[hi_i]
        else:
            lo_i -= 1
            acc += hist[lo_i]
    return {"poc": centers[poc_i], "vah": edges[hi_i + 1], "val": edges[lo_i]}
