"""Live scanner: pulls closed candles for the most liquid pairs, scores the
latest bar, optionally confirms with a footprint built from aggTrades, and
pushes alerts."""
from __future__ import annotations

import time

import pandas as pd

from .data import INTERVAL_MS, BinanceClient, drop_unclosed, pandas_freq
from .notify import format_signal, send_telegram, send_webhook
from .orderflow import footprint, footprint_bias
from .strategy import StrategyConfig, generate_signals


def bars_needed(cfg: StrategyConfig) -> int:
    """Enough base bars to warm up the slow EMA on the higher timeframe."""
    ratio = INTERVAL_MS[cfg.htf_interval] // INTERVAL_MS[cfg.interval]
    return int(ratio * (cfg.htf_ema_slow + 20))


def tick_for(price: float) -> float:
    """Footprint bucket size: ~2 bp of price rounded to a clean number."""
    raw = price * 0.0002
    mag = 10 ** int(f"{raw:e}".split("e")[1])
    return round(max(mag, round(raw / mag) * mag), 12)


def scan_symbol(client: BinanceClient, symbol: str, cfg: StrategyConfig, use_footprint: bool = True,
                footprint_mode: str = "confirm") -> dict | None:
    days = bars_needed(cfg) * INTERVAL_MS[cfg.interval] / 86_400_000
    df = drop_unclosed(client.history(symbol, cfg.interval, days))
    if len(df) < 300:
        return None
    df = df.drop(columns=["close_time"], errors="ignore")
    sig = generate_signals(df, cfg).iloc[-1]
    if sig["signal"] == 0:
        return None
    side = int(sig["signal"])
    out = {
        "symbol": symbol, "interval": cfg.interval, "time": df.index[-1] + pd.Timedelta(pandas_freq(cfg.interval)),
        "side": side, "score": float(sig["score"]), "confidence": float(sig["confidence"]),
        "entry": float(sig["entry"]), "sl": float(sig["sl"]), "tp1": float(sig["tp1"]), "tp2": float(sig["tp2"]),
        "stop_pct": float(sig["risk"] / sig["entry"] * 100), "tp1_fraction": cfg.tp1_fraction,
        "reasons": sig["reasons"], "footprint": None,
    }
    if use_footprint:
        start = int(df.index[-1].value // 1_000_000)
        trades = client.agg_trades(symbol, start, start + INTERVAL_MS[cfg.interval] - 1)
        bars = footprint(trades, pandas_freq(cfg.interval), tick_for(out["entry"]))
        bias = footprint_bias(bars)
        if bars:
            out["footprint"] = {**bars[-1].summary(), "bias": bias}
        if footprint_mode == "confirm" and bias == -side:
            return None  # the tape disagrees with the setup: skip it
    return out


def scan(client: BinanceClient, symbols: list[str], cfg: StrategyConfig, use_footprint: bool = True,
         notify: bool = False) -> list[dict]:
    found = []
    for sym in symbols:
        try:
            s = scan_symbol(client, sym, cfg, use_footprint)
        except Exception as exc:  # one bad symbol must not kill the scan
            print(f"[scan] {sym}: {exc}")
            continue
        if s:
            found.append(s)
    found.sort(key=lambda s: s["score"], reverse=True)
    for s in found:
        text = format_signal(s)
        print(text + "\n")
        if notify:
            send_telegram(text)
            send_webhook(s)
    return found


def run_forever(client: BinanceClient, symbols: list[str], cfg: StrategyConfig, use_footprint: bool = True,
                notify: bool = False) -> None:
    """Scan a few seconds after every candle close."""
    step = INTERVAL_MS[cfg.interval] / 1000
    while True:
        now = time.time()
        wait = step - (now % step) + 3
        print(f"[scan] next scan in {wait:.0f}s")
        time.sleep(wait)
        print(f"[scan] {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M:%S} scanning {len(symbols)} symbols")
        scan(client, symbols, cfg, use_footprint, notify)
