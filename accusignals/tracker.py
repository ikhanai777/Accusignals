"""Live track record: replays each emitted signal against the real candles
that followed it, with the same fill/exit rules as the backtester (next-bar
open entry, stop-first on ambiguous bars, TP1 partial then breakeven, fees)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .backtest import simulate_symbol
from .data import pandas_freq
from .risk import RiskConfig
from .strategy import StrategyConfig

FINAL = {"sl", "tp2", "tp1+be", "time", "tp1+time"}


def track_signal(sig: dict, candles: pd.DataFrame, scfg: StrategyConfig, rcfg: RiskConfig) -> dict:
    """``candles``: closed candles starting at the signal bar (its open time
    is ``bar_close_time - interval``). Returns status in
    pending / open / tp1 (open, partial banked) / tp2 / sl / tp1+be / time / tp1+time."""
    step = pd.Timedelta(pandas_freq(sig["interval"]))
    bar_open = pd.Timestamp(sig["bar_close_time"]) - step
    c = candles[candles.index >= bar_open]
    if len(c) < 2 or c.index[0] != bar_open:
        return {"status": "pending", "r": None, "exit_time": None, "entry_fill": None}
    side = int(sig["direction"])
    table = pd.DataFrame({"signal": 0, "sl": np.nan, "tp2": np.nan, "score": np.nan, "reasons": ""}, index=c.index)
    table.iloc[0, table.columns.get_loc("signal")] = side
    table.iloc[0, table.columns.get_loc("sl")] = sig["sl"]
    table.iloc[0, table.columns.get_loc("tp2")] = sig["tp2"]
    trades = simulate_symbol(c, table, scfg, rcfg, sig["symbol"])
    if not trades:  # opened beyond the stop: never filled
        return {"status": "skipped", "r": None, "exit_time": None, "entry_fill": None}
    t = trades[0]
    held = int((t.exit_time - t.entry_time) / step) + 1
    still_open = t.outcome in ("time", "tp1+time") and held < scfg.max_hold_bars and t.exit_time == c.index[-1]
    if still_open:
        status = "tp1" if t.outcome == "tp1+time" else "open"
        return {"status": status, "r": None, "exit_time": None, "entry_fill": t.entry, "bars_held": held}
    return {"status": t.outcome, "r": round(t.r_multiple, 3), "exit_time": t.exit_time, "entry_fill": t.entry,
            "bars_held": held}


def summarize(tracked: list[dict]) -> dict:
    closed = [t for t in tracked if t.get("status") in FINAL and t.get("r") is not None]
    rs = np.array([t["r"] for t in closed], dtype=float)
    return {
        "signals": len(tracked),
        "closed": len(closed),
        "open": sum(t.get("status") in ("open", "tp1") for t in tracked),
        "pending": sum(t.get("status") == "pending" for t in tracked),
        "wins": int((rs > 0).sum()),
        "losses": int((rs <= 0).sum()),
        "win_rate_pct": round(float((rs > 0).mean() * 100), 1) if len(rs) else None,
        "avg_r": round(float(rs.mean()), 3) if len(rs) else None,
        "total_r": round(float(rs.sum()), 2) if len(rs) else 0.0,
    }
