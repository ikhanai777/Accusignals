"""Price-action structure: swings, support/resistance, liquidity sweeps, candles.

Pivots are only "known" ``right`` bars after they print, so every output here
is aligned to the bar on which the information became available.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def confirmed_pivots(df: pd.DataFrame, left: int = 5, right: int = 3):
    """Return (pivot_high, pivot_low) Series holding the pivot price on the bar
    where the pivot is *confirmed* (NaN elsewhere)."""
    high, low = df["high"], df["low"]
    win = left + right + 1
    is_ph = high.shift(right) == high.rolling(win, min_periods=win).max()
    is_pl = low.shift(right) == low.rolling(win, min_periods=win).min()
    ph = high.shift(right).where(is_ph)
    pl = low.shift(right).where(is_pl)
    return ph, pl


def swing_levels(df: pd.DataFrame, left: int = 5, right: int = 3) -> pd.DataFrame:
    """Last and previous confirmed swing high/low plus a market-structure trend
    (+1 higher-highs & higher-lows, -1 lower-highs & lower-lows, 0 mixed)."""
    ph, pl = confirmed_pivots(df, left, right)
    last_h = ph.ffill()
    last_l = pl.ffill()
    prev_h = ph.dropna().shift(1).reindex(df.index).ffill()
    prev_l = pl.dropna().shift(1).reindex(df.index).ffill()
    ms = np.where((last_h > prev_h) & (last_l > prev_l), 1, np.where((last_h < prev_h) & (last_l < prev_l), -1, 0))
    return pd.DataFrame(
        {"swing_high": last_h, "swing_low": last_l, "prev_swing_high": prev_h, "prev_swing_low": prev_l, "ms_trend": ms},
        index=df.index,
    )


def liquidity_sweeps(df: pd.DataFrame, swings: pd.DataFrame):
    """Stop-hunt reversals: price wicks through the prior swing (taking the
    resting stops) and closes back inside it."""
    sl = swings["swing_low"].shift(1)
    sh = swings["swing_high"].shift(1)
    bull = (df["low"] < sl) & (df["close"] > sl)
    bear = (df["high"] > sh) & (df["close"] < sh)
    return bull.fillna(False), bear.fillna(False)


def support_resistance(
    df: pd.DataFrame,
    atr_s: pd.Series,
    left: int = 5,
    right: int = 3,
    lookback: int = 300,
    tol_atr: float = 0.35,
) -> pd.DataFrame:
    """For every bar, the nearest support below / resistance above the close,
    built from clustered confirmed pivots within ``lookback`` bars. ``*_touches``
    is how many pivots sit inside the zone (zone strength)."""
    ph, pl = confirmed_pivots(df, left, right)
    close = df["close"].to_numpy()
    a = atr_s.to_numpy()
    n = len(df)
    lvl_idx = np.concatenate([np.flatnonzero(ph.notna().to_numpy()), np.flatnonzero(pl.notna().to_numpy())])
    lvl_px = np.concatenate([ph.dropna().to_numpy(), pl.dropna().to_numpy()])
    order = np.argsort(lvl_idx, kind="stable")
    lvl_idx, lvl_px = lvl_idx[order], lvl_px[order]

    sup = np.full(n, np.nan)
    res = np.full(n, np.nan)
    sup_t = np.zeros(n, dtype=int)
    res_t = np.zeros(n, dtype=int)
    lo_ptr = hi_ptr = 0
    for i in range(n):
        while hi_ptr < len(lvl_idx) and lvl_idx[hi_ptr] <= i:
            hi_ptr += 1
        while lo_ptr < hi_ptr and lvl_idx[lo_ptr] < i - lookback:
            lo_ptr += 1
        if hi_ptr == lo_ptr or np.isnan(a[i]):
            continue
        px = lvl_px[lo_ptr:hi_ptr]
        tol = tol_atr * a[i]
        below = px[px < close[i]]
        above = px[px > close[i]]
        if below.size:
            s = below.max()
            sup[i] = s
            sup_t[i] = int(np.count_nonzero(np.abs(px - s) <= tol))
        if above.size:
            r = above.min()
            res[i] = r
            res_t[i] = int(np.count_nonzero(np.abs(px - r) <= tol))
    return pd.DataFrame({"support": sup, "resistance": res, "support_touches": sup_t, "resistance_touches": res_t}, index=df.index)


def candle_patterns(df: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    upper_wick = h - pd.concat([o, c], axis=1).max(axis=1)
    lower_wick = pd.concat([o, c], axis=1).min(axis=1) - l
    po, pc = o.shift(1), c.shift(1)
    bull_engulf = (c > o) & (pc < po) & (c >= po) & (o <= pc) & (body > (pc - po).abs())
    bear_engulf = (c < o) & (pc > po) & (c <= po) & (o >= pc) & (body > (pc - po).abs())
    # Pin bars: long rejection wick, small body, close in the favourable third.
    bull_pin = (lower_wick >= 2 * body) & (lower_wick / rng >= 0.55) & ((c - l) / rng >= 0.6)
    bear_pin = (upper_wick >= 2 * body) & (upper_wick / rng >= 0.55) & ((h - c) / rng >= 0.6)
    return pd.DataFrame(
        {
            "bull_engulf": bull_engulf.fillna(False),
            "bear_engulf": bear_engulf.fillna(False),
            "bull_pin": bull_pin.fillna(False),
            "bear_pin": bear_pin.fillna(False),
            "close_pos": ((c - l) / rng).fillna(0.5),  # 0 = closed on low, 1 = on high
        },
        index=df.index,
    )
