"""Live scanner: keeps a rolling window of real closed candles per symbol,
scores the latest bar, optionally confirms with a footprint built from the
candle's aggTrades, and emits alerts (console, JSON lines, Telegram, webhook)."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pandas as pd

from .data import INTERVAL_MS, BinanceClient, drop_unclosed, missing_bars, pandas_freq
from .notify import format_signal, send_telegram, send_webhook
from .orderflow import footprint, footprint_bias
from .strategy import StrategyConfig, generate_signals

log = logging.getLogger("accusignals")


def bars_needed(cfg: StrategyConfig) -> int:
    """Enough base bars to warm up the slow EMA on the higher timeframe."""
    ratio = INTERVAL_MS[cfg.htf_interval] // INTERVAL_MS[cfg.interval]
    return int(ratio * (cfg.htf_ema_slow + 20))


def tick_for(price: float) -> float:
    """Footprint bucket size: ~2 bp of price rounded to a clean number."""
    raw = price * 0.0002
    mag = 10 ** int(f"{raw:e}".split("e")[1])
    return round(max(mag, round(raw / mag) * mag), 12)


def to_json(sig: dict) -> str:
    return json.dumps(sig, default=lambda o: o.isoformat() if hasattr(o, "isoformat") else str(o))


class Scanner:
    def __init__(self, client: BinanceClient, symbols: list[str], cfg: StrategyConfig, use_footprint: bool = True,
                 notify: bool = False, as_json: bool = False, signals_file: str | Path | None = None,
                 cooldown_bars: int = 6):
        self.client, self.symbols, self.cfg = client, symbols, cfg
        self.use_footprint, self.notify, self.as_json = use_footprint, notify, as_json
        self.signals_file = Path(signals_file) if signals_file else None
        self.cooldown = pd.Timedelta(pandas_freq(cfg.interval)) * cooldown_bars
        self.cache: dict[str, pd.DataFrame] = {}
        self.last_alert: dict[str, tuple[pd.Timestamp, int]] = {}
        self.window = bars_needed(cfg) + 50
        self._load_recent_alerts()

    def _load_recent_alerts(self) -> None:
        """Seed the cooldown from the signals file so one-shot ``scan`` runs
        (e.g. an agent calling it every candle) don't repeat alerts."""
        if not self.signals_file or not self.signals_file.exists():
            return
        lines = self.signals_file.read_text(encoding="utf-8").splitlines()[-500:]
        for line in lines:
            try:
                s = json.loads(line)
                if s.get("interval") == self.cfg.interval and s.get("market") == self.client.market:
                    self.last_alert[s["symbol"]] = (pd.Timestamp(s["bar_close_time"]), int(s["direction"]))
            except (ValueError, KeyError):
                continue

    def candles(self, symbol: str) -> pd.DataFrame:
        cfg = self.cfg
        if symbol in self.cache:
            df = self.client.update(self.cache[symbol], symbol, cfg.interval, keep=self.window)
        else:
            days = self.window * INTERVAL_MS[cfg.interval] / 86_400_000
            df = self.client.history(symbol, cfg.interval, days)
        self.cache[symbol] = df
        closed = drop_unclosed(df, self.client.now())
        gaps = missing_bars(closed.iloc[-500:], cfg.interval)
        if gaps:
            log.warning("%s: %d missing candles in the last 500 (exchange gap)", symbol, gaps)
        return closed.drop(columns=["close_time"], errors="ignore")

    def scan_symbol(self, symbol: str) -> dict | None:
        cfg = self.cfg
        df = self.candles(symbol)
        if len(df) < 300:
            log.info("%s: only %d closed candles, skipping (new listing?)", symbol, len(df))
            return None
        sig = generate_signals(df, cfg).iloc[-1]
        if sig["signal"] == 0:
            return None
        side = int(sig["signal"])
        bar_open = df.index[-1]
        out = {
            "symbol": symbol, "market": self.client.market, "interval": cfg.interval,
            "side": "LONG" if side == 1 else "SHORT", "direction": side,
            "bar_close_time": bar_open + pd.Timedelta(pandas_freq(cfg.interval)),
            "entry": float(sig["entry"]), "sl": float(sig["sl"]), "tp1": float(sig["tp1"]), "tp2": float(sig["tp2"]),
            "stop_pct": round(float(sig["risk"] / sig["entry"] * 100), 3), "tp1_fraction": cfg.tp1_fraction,
            "score": float(sig["score"]), "confidence": float(sig["confidence"]),
            "reasons": sig["reasons"].split(",") if sig["reasons"] else [], "footprint": None,
        }
        if self.use_footprint:
            start = int(bar_open.value // 1_000_000)
            trades = self.client.agg_trades(symbol, start, start + INTERVAL_MS[cfg.interval] - 1)
            bars = footprint(trades, pandas_freq(cfg.interval), tick_for(out["entry"]))
            bias = footprint_bias(bars)
            if bars:
                fp = bars[-1].summary()
                out["footprint"] = {"delta": round(fp["delta"], 4), "poc": fp["poc"], "stacked_buy_imb": fp["stacked_buy_imb"],
                                    "stacked_sell_imb": fp["stacked_sell_imb"], "bias": bias}
            if bias == -side:
                log.info("%s: %s setup vetoed by footprint (opposite stacked imbalances)", symbol, out["side"])
                return None
        prev = self.last_alert.get(symbol)
        if prev and prev[1] == side and out["bar_close_time"] - prev[0] < self.cooldown:
            return None  # same setup still firing on consecutive bars
        self.last_alert[symbol] = (out["bar_close_time"], side)
        return out

    def scan_once(self) -> list[dict]:
        found = []
        for sym in self.symbols:
            try:
                s = self.scan_symbol(sym)
            except Exception as exc:  # one bad symbol must not kill the scan
                log.error("%s: %s", sym, exc)
                continue
            if s:
                found.append(s)
        found.sort(key=lambda s: s["score"], reverse=True)
        for s in found:
            self.emit(s)
        return found

    def emit(self, s: dict) -> None:
        line = to_json(s)
        print(line if self.as_json else format_signal(s) + "\n", flush=True)
        log.info("SIGNAL %s", line)
        if self.signals_file:
            self.signals_file.parent.mkdir(parents=True, exist_ok=True)
            with self.signals_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if self.notify:
            try:
                send_telegram(format_signal(s))
                send_webhook(s)
            except Exception as exc:
                log.error("notification failed: %s", exc)

    def seconds_to_next_scan(self) -> float:
        step = INTERVAL_MS[self.cfg.interval]
        return (step - self.client.now_ms() % step) / 1000 + 3

    def run_forever(self, stop: threading.Event | None = None, on_scan=None) -> None:
        """Scan a few seconds after every candle close (exchange time) until
        ``stop`` is set. ``on_scan(found)`` is called after each scan."""
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.client.sync_time()
            except Exception as exc:
                log.warning("time sync failed: %s", exc)
            wait = self.seconds_to_next_scan()
            log.info("next scan in %.0fs", wait)
            if stop.wait(wait):
                break
            log.info("scanning %d symbols", len(self.symbols))
            found = self.scan_once()
            log.info("scan done: %d signal(s)", len(found))
            if on_scan:
                on_scan(found)
