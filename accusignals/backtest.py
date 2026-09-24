"""Bar-by-bar backtester.

Design choices that keep results honest:
* signals are computed on the *closed* bar and filled at the next bar's open;
* if a bar touches both the stop and a target, the stop is assumed first;
* after TP1 the remainder's stop moves to breakeven, and if the same bar also
  trades back to breakeven we assume it was stopped;
* taker fees and slippage are charged on every fill.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .risk import RiskConfig, position_size
from .strategy import StrategyConfig, generate_signals


@dataclass
class Trade:
    symbol: str
    side: int
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    sl: float
    tp1: float
    tp2: float
    exit_price: float  # size-weighted average exit
    r_multiple: float  # net of fees
    ret: float  # net return on notional
    outcome: str  # "tp2", "tp1+be", "tp1+time", "sl", "time"
    score: float
    reasons: str


def simulate_symbol(df: pd.DataFrame, signals: pd.DataFrame, scfg: StrategyConfig, rcfg: RiskConfig,
                    symbol: str = "") -> list[Trade]:
    """Walk the signal table through price, one position at a time."""
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    idx = df.index
    sig = signals["signal"].to_numpy()
    sl_a, tp2_a = signals["sl"].to_numpy(), signals["tp2"].to_numpy()
    score, reasons = signals["score"].to_numpy(), signals["reasons"].to_numpy()
    slip = rcfg.slippage_bps / 1e4
    fee = rcfg.fee_rate
    trades: list[Trade] = []
    n = len(df)
    i = 0
    while i < n - 1:
        side = int(sig[i])
        if side == 0:
            i += 1
            continue
        e = i + 1
        entry = o[e] * (1 + side * slip)
        sl = sl_a[i]
        risk = side * (entry - sl)
        if not np.isfinite(risk) or risk <= 0:
            i += 1
            continue
        tp1 = entry + side * scfg.tp1_r * risk
        tp2 = tp2_a[i]
        if not np.isfinite(tp2) or side * (tp2 - tp1) <= 0:
            tp2 = entry + side * scfg.tp2_r * risk
        stop = sl
        frac_left, realised = 1.0, 0.0  # realised = sum(frac * side * (exit - entry))
        exit_notional = 0.0
        outcome = None
        j = e
        last = min(n - 1, e + scfg.max_hold_bars - 1)
        while j <= last:
            hi, lo = h[j], l[j]
            adverse = lo if side == 1 else hi
            favour = hi if side == 1 else lo
            if frac_left == 1.0:
                if side * (adverse - stop) <= 0:
                    px = (min(stop, o[j]) if side == 1 else max(stop, o[j])) * (1 - side * slip)
                    realised += side * (px - entry)
                    exit_notional += px
                    frac_left, outcome = 0.0, "sl"
                    break
                if side * (favour - tp1) >= 0:
                    f1 = scfg.tp1_fraction
                    realised += f1 * side * (tp1 - entry)
                    exit_notional += f1 * tp1
                    frac_left = 1.0 - f1
                    stop = entry + side * 2 * fee * entry  # breakeven incl. fees
                    if side * (favour - tp2) >= 0:
                        realised += frac_left * side * (tp2 - entry)
                        exit_notional += frac_left * tp2
                        frac_left, outcome = 0.0, "tp2"
                        break
                    if side * (adverse - stop) <= 0:
                        realised += frac_left * side * (stop - entry)
                        exit_notional += frac_left * stop
                        frac_left, outcome = 0.0, "tp1+be"
                        break
            else:
                if side * (adverse - stop) <= 0:
                    px = min(stop, o[j]) if side == 1 else max(stop, o[j])
                    realised += frac_left * side * (px - entry)
                    exit_notional += frac_left * px
                    frac_left, outcome = 0.0, "tp1+be"
                    break
                if side * (favour - tp2) >= 0:
                    realised += frac_left * side * (tp2 - entry)
                    exit_notional += frac_left * tp2
                    frac_left, outcome = 0.0, "tp2"
                    break
            j += 1
        if outcome is None:  # time stop
            j = min(j, n - 1)
            px = c[j] * (1 - side * slip)
            realised += frac_left * side * (px - entry)
            exit_notional += frac_left * px
            outcome = "time" if frac_left == 1.0 else "tp1+time"
        fees = fee * (entry + exit_notional)
        net = realised - fees
        trades.append(Trade(
            symbol=symbol, side=side, signal_time=idx[i], entry_time=idx[e], exit_time=idx[j],
            entry=entry, sl=sl, tp1=tp1, tp2=tp2, exit_price=exit_notional,
            r_multiple=net / risk, ret=net / entry, outcome=outcome,
            score=float(score[i]), reasons=str(reasons[i]),
        ))
        i = j + 1  # flat again after the exit bar
    return trades


def trades_frame(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=list(Trade.__dataclass_fields__))
    return pd.DataFrame([t.__dict__ for t in trades]).sort_values("entry_time").reset_index(drop=True)


def simulate_portfolio(trades: pd.DataFrame, rcfg: RiskConfig, start_equity: float = 1000.0) -> tuple[pd.DataFrame, pd.Series]:
    """Apply account rules (sizing, concurrency, daily target/loss stop,
    tilt stop) to candidate trades from any number of symbols.
    Returns (taken trades with P&L, equity curve indexed by exit time)."""
    if trades.empty:
        return trades.assign(pnl=[], qty=[]), pd.Series([start_equity], dtype=float)
    events = []
    for k, t in trades.iterrows():
        events.append((t["entry_time"], 1, k))
        # Exits sort before other trades' entries at the same timestamp, but a
        # trade that opens and closes within one bar must open first.
        events.append((t["exit_time"], 0 if t["exit_time"] > t["entry_time"] else 2, k))
    events.sort(key=lambda x: (x[0], x[1]))
    equity = start_equity
    open_pos: dict[int, float] = {}
    taken: dict[int, tuple[float, float]] = {}
    day, day_start_eq, day_trades, consec = None, equity, 0, 0
    curve = {trades["entry_time"].min(): equity}
    for ts, kind, k in events:
        d = ts.floor("D")
        if d != day:
            day, day_start_eq, day_trades, consec = d, equity, 0, 0
        t = trades.loc[k]
        if kind != 1:
            if k in open_pos:
                qty = open_pos.pop(k)
                pnl = qty * t["entry"] * t["ret"]
                equity += pnl
                taken[k] = (qty, pnl)
                consec = consec + 1 if pnl < 0 else 0
                curve[ts] = equity
            continue
        day_ret = (equity / day_start_eq - 1) * 100
        if (
            equity <= 0
            or len(open_pos) >= rcfg.max_concurrent
            or day_trades >= rcfg.max_trades_per_day
            or day_ret >= rcfg.daily_profit_target_pct
            or day_ret <= -rcfg.daily_loss_limit_pct
            or consec >= rcfg.max_consecutive_losses
            or any(trades.loc[x, "symbol"] == t["symbol"] for x in open_pos)
        ):
            continue
        qty = position_size(equity, t["entry"], t["sl"], rcfg)
        if qty > 0:
            open_pos[k] = qty
            day_trades += 1
    out = trades.loc[list(taken)].copy()
    out["qty"] = [taken[k][0] for k in out.index]
    out["pnl"] = [taken[k][1] for k in out.index]
    return out.sort_values("entry_time"), pd.Series(curve).sort_index()


def metrics(taken: pd.DataFrame, curve: pd.Series, start_equity: float = 1000.0) -> dict:
    if taken.empty:
        return {"trades": 0}
    wins = taken["pnl"] > 0
    gross_win = taken.loc[wins, "pnl"].sum()
    gross_loss = -taken.loc[~wins, "pnl"].sum()
    dd = (curve / curve.cummax() - 1).min() * 100
    daily = curve.resample("D").last().ffill()
    daily = pd.concat([pd.Series([start_equity], index=[daily.index[0] - pd.Timedelta("1D")]), daily])
    daily_ret = daily.pct_change().dropna() * 100
    days = max(len(daily_ret), 1)
    out = {
        "trades": int(len(taken)),
        "win_rate_pct": round(wins.mean() * 100, 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else float("inf"),
        "avg_r": round(taken["r_multiple"].mean(), 3),
        "expectancy_pct_equity": round(taken["pnl"].mean() / start_equity * 100, 3),
        "total_return_pct": round((curve.iloc[-1] / start_equity - 1) * 100, 2),
        "max_drawdown_pct": round(dd, 2),
        "days": days,
        "trades_per_day": round(len(taken) / days, 2),
        "avg_daily_return_pct": round(daily_ret.mean(), 3),
        "median_daily_return_pct": round(daily_ret.median(), 3),
        "pct_green_days": round((daily_ret > 0).mean() * 100, 1),
        "sharpe_daily_ann": round(daily_ret.mean() / daily_ret.std() * np.sqrt(365), 2) if daily_ret.std() > 0 else 0.0,
        "outcomes": taken["outcome"].value_counts().to_dict(),
        "long_win_rate_pct": round(wins[taken["side"] == 1].mean() * 100, 1) if (taken["side"] == 1).any() else None,
        "short_win_rate_pct": round(wins[taken["side"] == -1].mean() * 100, 1) if (taken["side"] == -1).any() else None,
    }
    return {k: float(v) if isinstance(v, np.floating) else v for k, v in out.items()}


def run_backtest(data: dict[str, pd.DataFrame], scfg: StrategyConfig | None = None, rcfg: RiskConfig | None = None,
                 start_equity: float = 1000.0):
    """Backtest a strategy over several symbols with shared account rules.
    Returns (metrics dict, taken trades, equity curve)."""
    scfg = scfg or StrategyConfig()
    rcfg = rcfg or RiskConfig()
    all_trades = []
    for sym, df in data.items():
        sigs = generate_signals(df, scfg)
        all_trades.extend(simulate_symbol(df, sigs, scfg, rcfg, sym))
    cand = trades_frame(all_trades)
    taken, curve = simulate_portfolio(cand, rcfg, start_equity)
    return metrics(taken, curve, start_equity), taken, curve
