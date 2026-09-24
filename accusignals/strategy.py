"""Confluence scalping strategy.

The idea is the one most consistently profitable discretionary scalpers use:
trade *with* the higher-timeframe trend, enter on a pullback into value
(VWAP / support / swept liquidity), and only when order flow and volume
confirm that the other side has stopped pushing. No single indicator decides;
each independent piece of evidence adds to a score and a trade is only
signalled when enough of them line up.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from . import indicators as ind
from . import orderflow as of
from . import structure as st
from .data import pandas_freq


@dataclass
class StrategyConfig:
    interval: str = "5m"
    htf_interval: str = "1h"
    # Higher-timeframe trend
    htf_ema_fast: int = 50
    htf_ema_slow: int = 200
    require_htf_trend: bool = True
    # Entry timeframe
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    atr_period: int = 14
    vol_z_period: int = 50
    pivot_left: int = 5
    pivot_right: int = 3
    sr_lookback: int = 300
    min_adx: float = 15.0
    # Scoring
    min_score: float = 6.0
    weights: dict = field(default_factory=lambda: {
        "htf_trend": 2.0,
        "ms_trend": 0.5,
        "ema_stack": 0.5,
        "vwap_side": 1.0,
        "vwap_pullback": 1.0,
        "rsi_reset": 1.0,
        "stoch_cross": 0.5,
        "volume": 1.0,
        "delta": 1.0,
        "cvd_slope": 0.5,
        "sweep": 1.5,
        "sr_bounce": 1.0,
        "candle": 1.0,
        "cvd_div": 0.75,
        "absorption": 0.75,
    })
    no_room_penalty: float = 2.0
    # Risk geometry
    sl_atr_buffer: float = 0.2
    sl_min_atr: float = 0.7
    sl_max_atr: float = 2.5
    min_stop_pct: float = 0.20  # stops tighter than this get eaten by fees/noise
    max_stop_pct: float = 2.5
    tp1_r: float = 1.0
    tp2_r: float = 2.0
    tp1_fraction: float = 0.5  # size closed at TP1, then stop -> breakeven
    use_sr_targets: bool = True
    min_room_r: float = 1.0  # need this much space before the next S/R level
    max_hold_bars: int = 36

    def max_score(self) -> float:
        return float(sum(self.weights.values()))

    def to_dict(self) -> dict:
        return asdict(self)


def htf_trend(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Higher-timeframe EMAs aligned so each base bar only sees HTF candles
    that had fully closed by the base bar's close."""
    htf_freq, base_freq = pandas_freq(cfg.htf_interval), pandas_freq(cfg.interval)
    htf = df.resample(htf_freq, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    out = pd.DataFrame(
        {"htf_ema_fast": ind.ema(htf["close"], cfg.htf_ema_fast), "htf_ema_slow": ind.ema(htf["close"], cfg.htf_ema_slow)}
    )
    # An HTF candle opening at T is final once the base bar opening at
    # T + htf - base has closed.
    out.index = out.index + pd.Timedelta(htf_freq) - pd.Timedelta(base_freq)
    return out.reindex(df.index.union(out.index)).ffill().reindex(df.index)


def compute_features(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    f = pd.DataFrame(index=df.index)
    c = df["close"]
    f["close"] = c
    f["atr"] = ind.atr(df, cfg.atr_period)
    f["ema_fast"] = ind.ema(c, cfg.ema_fast)
    f["ema_slow"] = ind.ema(c, cfg.ema_slow)
    f["rsi"] = ind.rsi(c, cfg.rsi_period)
    f["stoch_k"], f["stoch_d"] = ind.stoch_rsi(c)
    f["adx"], _, _ = ind.adx(df)
    f["vwap"], f["vwap_lo"], f["vwap_hi"] = ind.session_vwap(df)
    f["vol_z"] = ind.zscore(df["volume"], cfg.vol_z_period)
    f["delta_ratio"] = of.delta_ratio(df)
    f["cvd"] = of.cvd(df)
    f["cvd_div_bull"], f["cvd_div_bear"] = of.cvd_divergence(df)
    f["absorb_bull"], f["absorb_bear"] = of.absorption(df, f["vol_z"], f["atr"])
    f = f.join(htf_trend(df, cfg))
    sw = st.swing_levels(df, cfg.pivot_left, cfg.pivot_right)
    f = f.join(sw)
    f["sweep_bull"], f["sweep_bear"] = st.liquidity_sweeps(df, sw)
    f = f.join(st.support_resistance(df, f["atr"], cfg.pivot_left, cfg.pivot_right, cfg.sr_lookback))
    f = f.join(st.candle_patterns(df))
    f["recent_low"] = df["low"].rolling(5).min()
    f["recent_high"] = df["high"].rolling(5).max()
    f["low3"] = df["low"].rolling(3).min()
    f["high3"] = df["high"].rolling(3).max()
    f["green"] = df["close"] > df["open"]
    f["red"] = df["close"] < df["open"]
    return f


def _recent(s: pd.Series, n: int) -> pd.Series:
    """True if ``s`` was true on any of the last ``n`` bars."""
    return s.astype(float).rolling(n, min_periods=1).max().fillna(0).astype(bool)


def _side_components(f: pd.DataFrame, cfg: StrategyConfig, side: int) -> pd.DataFrame:
    """Boolean evidence table for one side (+1 long / -1 short)."""
    L = side == 1
    c, a = f["close"], f["atr"]
    rsi = f["rsi"]
    comp = pd.DataFrame(index=f.index)
    if L:
        comp["htf_trend"] = (f["htf_ema_fast"] > f["htf_ema_slow"]) & (c > f["htf_ema_fast"])
        comp["ms_trend"] = f["ms_trend"] == 1
        comp["ema_stack"] = f["ema_fast"] > f["ema_slow"]
        comp["vwap_side"] = c > f["vwap"]
        comp["vwap_pullback"] = (f["low3"] <= f["vwap"] + 0.1 * a) & (c > f["vwap"])
        comp["rsi_reset"] = (rsi.shift(1).rolling(4).min() < 45) & (rsi > rsi.shift(1)) & rsi.between(40, 70)
        k, d = f["stoch_k"], f["stoch_d"]
        comp["stoch_cross"] = _recent((k > d) & (k.shift(1) <= d.shift(1)) & (k.shift(1) < 30), 2)
        comp["delta"] = f["delta_ratio"] > 0.1
        comp["cvd_slope"] = f["cvd"] > f["cvd"].shift(5)
        comp["sweep"] = _recent(f["sweep_bull"], 3)
        comp["sr_bounce"] = (f["low3"] <= f["support"] + 0.3 * a) & (c > f["support"]) & (f["support_touches"] >= 2)
        comp["candle"] = f["bull_engulf"] | f["bull_pin"]
        comp["cvd_div"] = _recent(f["cvd_div_bull"], 3)
        comp["absorption"] = _recent(f["absorb_bull"], 3)
    else:
        comp["htf_trend"] = (f["htf_ema_fast"] < f["htf_ema_slow"]) & (c < f["htf_ema_fast"])
        comp["ms_trend"] = f["ms_trend"] == -1
        comp["ema_stack"] = f["ema_fast"] < f["ema_slow"]
        comp["vwap_side"] = c < f["vwap"]
        comp["vwap_pullback"] = (f["high3"] >= f["vwap"] - 0.1 * a) & (c < f["vwap"])
        comp["rsi_reset"] = (rsi.shift(1).rolling(4).max() > 55) & (rsi < rsi.shift(1)) & rsi.between(30, 60)
        k, d = f["stoch_k"], f["stoch_d"]
        comp["stoch_cross"] = _recent((k < d) & (k.shift(1) >= d.shift(1)) & (k.shift(1) > 70), 2)
        comp["delta"] = f["delta_ratio"] < -0.1
        comp["cvd_slope"] = f["cvd"] < f["cvd"].shift(5)
        comp["sweep"] = _recent(f["sweep_bear"], 3)
        comp["sr_bounce"] = (f["high3"] >= f["resistance"] - 0.3 * a) & (c < f["resistance"]) & (f["resistance_touches"] >= 2)
        comp["candle"] = f["bear_engulf"] | f["bear_pin"]
        comp["cvd_div"] = _recent(f["cvd_div_bear"], 3)
        comp["absorption"] = _recent(f["absorb_bear"], 3)
    comp["volume"] = f["vol_z"] >= 1.0
    return comp.fillna(False).astype(bool)


def _stops_and_targets(f: pd.DataFrame, cfg: StrategyConfig, side: int) -> pd.DataFrame:
    c, a = f["close"], f["atr"]
    if side == 1:
        structural = pd.concat([f["recent_low"], f["swing_low"].where(f["swing_low"] > c - cfg.sl_max_atr * a)], axis=1).min(axis=1)
        dist = (c - (structural - cfg.sl_atr_buffer * a)).clip(lower=cfg.sl_min_atr * a, upper=cfg.sl_max_atr * a)
        sl = c - dist
        room = f["resistance"] - c
    else:
        structural = pd.concat([f["recent_high"], f["swing_high"].where(f["swing_high"] < c + cfg.sl_max_atr * a)], axis=1).max(axis=1)
        dist = ((structural + cfg.sl_atr_buffer * a) - c).clip(lower=cfg.sl_min_atr * a, upper=cfg.sl_max_atr * a)
        sl = c + dist
        room = c - f["support"]
    tp1 = c + side * cfg.tp1_r * dist
    tp2 = c + side * cfg.tp2_r * dist
    if cfg.use_sr_targets:
        # Take profit just in front of the next S/R zone if it sits between
        # TP1 and TP2 - price often stalls there.
        lvl = f["resistance"] - 0.1 * a if side == 1 else f["support"] + 0.1 * a
        inside = (side * (lvl - tp1) > 0) & (side * (tp2 - lvl) > 0)
        tp2 = tp2.where(~inside, lvl)
    has_room = room.isna() | (room >= cfg.min_room_r * dist)
    return pd.DataFrame({"sl": sl, "tp1": tp1, "tp2": tp2, "risk": dist, "has_room": has_room})


def generate_signals(df: pd.DataFrame, cfg: StrategyConfig | None = None, features: pd.DataFrame | None = None) -> pd.DataFrame:
    """Score every bar. Returns one row per bar with ``signal`` (+1 long,
    -1 short, 0 none), score, confidence, entry reference, SL and TPs, plus the
    list of reasons that fired - so each alert is explainable."""
    cfg = cfg or StrategyConfig()
    f = features if features is not None else compute_features(df, cfg)
    w = pd.Series(cfg.weights)
    out = pd.DataFrame(index=f.index)
    sides = {}
    for side, name in ((1, "long"), (-1, "short")):
        comp = _side_components(f, cfg, side)
        geo = _stops_and_targets(f, cfg, side)
        score = comp[w.index].astype(float).mul(w).sum(axis=1) - (~geo["has_room"]) * cfg.no_room_penalty
        stop_pct = geo["risk"] / f["close"] * 100
        gate = (
            f["atr"].notna() & f["vol_z"].notna() & f["htf_ema_slow"].notna()
            & (f["adx"] >= cfg.min_adx)
            & stop_pct.between(cfg.min_stop_pct, cfg.max_stop_pct)
            & (f["green"] if side == 1 else f["red"])  # the trigger candle must close in our direction
        )
        if cfg.require_htf_trend:
            gate &= comp["htf_trend"]
        sides[name] = (score.where(gate, -np.inf), comp, geo)
    long_s, short_s = sides["long"][0], sides["short"][0]
    sig = np.where((long_s >= cfg.min_score) & (long_s >= short_s), 1, np.where(short_s >= cfg.min_score, -1, 0))
    out["signal"] = sig
    out["long_score"] = long_s.replace(-np.inf, np.nan)
    out["short_score"] = short_s.replace(-np.inf, np.nan)
    out["score"] = np.where(sig == 1, out["long_score"], np.where(sig == -1, out["short_score"], np.nan))
    out["confidence"] = (out["score"] / cfg.max_score() * 100).round(1)
    out["entry"] = f["close"]
    out["atr"] = f["atr"]
    for col in ("sl", "tp1", "tp2", "risk"):
        out[col] = np.where(sig == 1, sides["long"][2][col], np.where(sig == -1, sides["short"][2][col], np.nan))
    reasons = pd.Series("", index=f.index)
    for side, name in ((1, "long"), (-1, "short")):
        comp = sides[name][1]
        mask = sig == side
        if mask.any():
            reasons[mask] = comp[mask].apply(lambda r: ",".join(r.index[r.to_numpy()]), axis=1)
    out["reasons"] = reasons
    return out
