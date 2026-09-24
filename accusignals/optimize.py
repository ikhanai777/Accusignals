"""Walk-forward optimisation.

Tuning parameters on the same data you report results on is the #1 way
backtests lie. Here parameters are chosen on a rolling in-sample window and
then *only* the following unseen window is scored; the reported numbers are
the concatenation of those out-of-sample windows.
"""
from __future__ import annotations

import itertools
from dataclasses import replace

import numpy as np
import pandas as pd

from .backtest import metrics, simulate_portfolio, simulate_symbol, trades_frame
from .risk import RiskConfig
from .strategy import StrategyConfig, compute_features, generate_signals

DEFAULT_GRID = {
    "min_score": [5.0, 6.0, 7.0, 8.0],
    "tp1_r": [0.8, 1.0],
    "tp2_r": [1.5, 2.0, 3.0],
    "sl_min_atr": [0.7, 1.0],
}


def _grid(grid: dict) -> list[dict]:
    keys = list(grid)
    return [dict(zip(keys, vals)) for vals in itertools.product(*grid.values())]


def _candidate_trades(data, feats, scfg, rcfg, start, end) -> pd.DataFrame:
    trades = []
    for sym, df in data.items():
        sl = slice(start, end - pd.Timedelta(1, "ns"))  # .loc is end-inclusive
        # Features were computed on full history (they are causal), so slicing
        # them keeps indicator warm-up without leaking the future.
        sigs = generate_signals(df.loc[sl], scfg, feats[sym].loc[sl])
        trades.extend(simulate_symbol(df.loc[sl], sigs, scfg, rcfg, sym))
    return trades_frame(trades)


def objective(tr: pd.DataFrame, min_trades: int) -> float:
    """Mean R scaled by sqrt(n): rewards edge that is also statistically
    meaningful rather than a handful of lucky trades."""
    if len(tr) < min_trades:
        return -np.inf
    r = tr["r_multiple"]
    return float(r.mean() * np.sqrt(len(r)))


def walk_forward(data: dict[str, pd.DataFrame], base: StrategyConfig | None = None, rcfg: RiskConfig | None = None,
                 grid: dict | None = None, train_days: float = 21, test_days: float = 7, min_trades: int = 20,
                 start_equity: float = 1000.0, verbose: bool = True):
    base = base or StrategyConfig()
    rcfg = rcfg or RiskConfig()
    combos = _grid(grid or DEFAULT_GRID)
    feats = {s: compute_features(df, base) for s, df in data.items()}
    t0 = max(df.index[0] for df in data.values())
    t_end = min(df.index[-1] for df in data.values())
    train, test = pd.Timedelta(days=train_days), pd.Timedelta(days=test_days)
    oos, folds = [], []
    cursor = t0 + train
    while cursor < t_end:
        best, best_score = None, -np.inf
        for p in combos:
            cfg = replace(base, **p)
            s = objective(_candidate_trades(data, feats, cfg, rcfg, cursor - train, cursor), min_trades)
            if s > best_score:
                best, best_score = p, s
        if best is None:  # nothing tradeable in-sample: sit this window out
            folds.append({"test_start": cursor, "params": None, "is_score": None, "oos_trades": 0})
            cursor += test
            continue
        cfg = replace(base, **best)
        tr = _candidate_trades(data, feats, cfg, rcfg, cursor, cursor + test)
        # Simulating on the sliced window also closes trades by its end.
        oos.append(tr)
        folds.append({"test_start": cursor, "params": best, "is_score": round(best_score, 3), "oos_trades": len(tr),
                      "oos_avg_r": round(float(tr["r_multiple"].mean()), 3) if len(tr) else None})
        if verbose:
            print(f"[wf] {cursor:%Y-%m-%d} best={best} is={best_score:.2f} oos_trades={len(tr)}")
        cursor += test
    all_oos = pd.concat(oos, ignore_index=True) if oos else trades_frame([])
    if not all_oos.empty:
        all_oos = all_oos.sort_values("entry_time").reset_index(drop=True)
    taken, curve = simulate_portfolio(all_oos, rcfg, start_equity)
    return metrics(taken, curve, start_equity), pd.DataFrame(folds), taken, curve
