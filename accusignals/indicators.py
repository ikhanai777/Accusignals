"""Vectorised technical indicators.

Every function takes pandas Series/DataFrames indexed by candle open time and
returns values that only use information available at the *close* of each
bar, so they are safe to use in a bar-by-bar backtest without look-ahead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period, min_periods=period).mean()


def rma(s: pd.Series, period: int) -> pd.Series:
    """Wilder's moving average (used by RSI, ATR, ADX)."""
    return s.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    diff = close.diff()
    gain = rma(diff.clip(lower=0), period)
    loss = rma(-diff.clip(upper=0), period)
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0).where(loss.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return rma(true_range(df), period)


def bollinger(close: pd.Series, period: int = 20, mult: float = 2.0):
    mid = sma(close, period)
    std = close.rolling(period, min_periods=period).std(ddof=0)
    return mid - mult * std, mid, mid + mult * std


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def stoch_rsi(close: pd.Series, period: int = 14, k: int = 3, d: int = 3):
    r = rsi(close, period)
    lo = r.rolling(period, min_periods=period).min()
    hi = r.rolling(period, min_periods=period).max()
    st = (r - lo) / (hi - lo).replace(0, np.nan) * 100
    k_line = sma(st.fillna(50), k)
    return k_line, sma(k_line, d)


def adx(df: pd.DataFrame, period: int = 14):
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = rma(true_range(df), period)
    plus_di = 100 * rma(plus_dm, period) / tr
    minus_di = 100 * rma(minus_dm, period) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return rma(dx, period), plus_di, minus_di


def session_vwap(df: pd.DataFrame, band_mult: float = 1.0):
    """VWAP anchored at each UTC day, with standard-deviation bands.

    Crypto trades 24/7, so the UTC day boundary is the de-facto session reset
    that most desks (and TradingView's default) use.
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    day = df.index.floor("D")
    pv = (tp * df["volume"]).groupby(day).cumsum()
    vol = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    vwap = pv / vol
    pv2 = (tp * tp * df["volume"]).groupby(day).cumsum()
    var = (pv2 / vol - vwap * vwap).clip(lower=0)
    std = np.sqrt(var)
    return vwap, vwap - band_mult * std, vwap + band_mult * std


def zscore(s: pd.Series, period: int = 50) -> pd.Series:
    mean = s.rolling(period, min_periods=period).mean()
    std = s.rolling(period, min_periods=period).std(ddof=0)
    return (s - mean) / std.replace(0, np.nan)


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.Series:
    """Returns +1 when in an up-trend, -1 when in a down-trend."""
    a = atr(df, period).to_numpy()
    hl2 = ((df["high"] + df["low"]) / 2).to_numpy()
    close = df["close"].to_numpy()
    upper = hl2 + mult * a
    lower = hl2 - mult * a
    direction = np.ones(len(df))
    fu, fl = upper.copy(), lower.copy()
    for i in range(1, len(df)):
        if np.isnan(a[i]):
            continue
        fu[i] = upper[i] if (np.isnan(fu[i - 1]) or upper[i] < fu[i - 1] or close[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lower[i] if (np.isnan(fl[i - 1]) or lower[i] > fl[i - 1] or close[i - 1] < fl[i - 1]) else fl[i - 1]
        if direction[i - 1] == -1 and close[i] > fu[i - 1]:
            direction[i] = 1
        elif direction[i - 1] == 1 and close[i] < fl[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
    return pd.Series(direction, index=df.index)
