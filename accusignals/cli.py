"""Command line: ``python -m accusignals <command>``.

  health       check Binance connectivity, clock offset and symbol list
  scan         live signals for the most liquid Binance pairs
  download     cache historical klines to CSV (real Binance data)
  backtest     backtest on downloaded Binance history
  walkforward  walk-forward optimisation (out-of-sample results)

Add --json for machine-readable output (one JSON object per line on stdout;
logs go to stderr and logs/accusignals.log). Exit codes: 0 ok, 2 Binance
unreachable, 1 other error.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import fields, replace
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pandas as pd
import requests

from .backtest import run_backtest
from .data import BinanceClient, load_csv, missing_bars, pandas_freq, save_csv
from .optimize import walk_forward
from .risk import RiskConfig
from .scanner import Scanner
from .strategy import StrategyConfig

HOME = Path(__file__).resolve().parent.parent  # project folder: data/, results/, logs/, signals/ live here
log = logging.getLogger("accusignals")


def setup_logging(verbose: bool) -> None:
    (HOME / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(HOME / "logs" / "accusignals.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)  # keep stdout clean for --json consumers
    sh.setFormatter(fmt)
    log.handlers[:] = [fh, sh]
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.propagate = False


def load_configs(path: str | None, args) -> tuple[StrategyConfig, RiskConfig]:
    scfg, rcfg = StrategyConfig(), RiskConfig()
    raw = {}
    if path:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        scfg = _apply(scfg, raw.get("strategy", {}))
        rcfg = _apply(rcfg, raw.get("risk", {}))
    overrides = {"interval": args.interval, "htf_interval": args.htf, "min_score": args.min_score}
    scfg = replace(scfg, **{k: v for k, v in overrides.items() if v is not None})
    if getattr(args, "risk", None) is not None:
        rcfg = replace(rcfg, risk_per_trade_pct=args.risk)
    if args.market == "spot" and "fee_rate" not in raw.get("risk", {}):
        rcfg = replace(rcfg, fee_rate=0.001)
    return scfg, rcfg


def _apply(cfg, values: dict):
    known = {f.name for f in fields(cfg)}
    unknown = set(values) - known
    if unknown:
        raise SystemExit(f"unknown config keys: {sorted(unknown)}")
    return replace(cfg, **values)


def _symbols(args, client: BinanceClient) -> list[str]:
    if args.symbols:
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    return client.top_symbols(args.top)


def _load_data(args, client: BinanceClient, scfg: StrategyConfig) -> dict[str, pd.DataFrame]:
    """Download Binance history, or top up the local cache with the newest
    candles so a cached file is never stale."""
    data = {}
    now = client.now()
    for sym in _symbols(args, client):
        path = Path(args.data_dir) / args.market / f"{sym}_{scfg.interval}.csv"
        if path.exists() and not args.refresh:
            df = load_csv(path)
            if df.index[0] > now - pd.Timedelta(days=args.days):  # cache too short: fetch the full window
                df = client.history(sym, scfg.interval, args.days)
            else:
                df = client.update(df, sym, scfg.interval)
        else:
            log.info("downloading %s %s %dd from Binance %s", sym, scfg.interval, args.days, args.market)
            df = client.history(sym, scfg.interval, args.days)
        df = df.drop(columns=["close_time"], errors="ignore")
        df = df[df.index + pd.Timedelta(pandas_freq(scfg.interval)) <= now]  # closed candles only
        save_csv(df, path)
        df = df[df.index >= now - pd.Timedelta(days=args.days)]
        gaps = missing_bars(df, scfg.interval)
        if gaps:
            log.warning("%s: %d missing candles in history (exchange downtime)", sym, gaps)
        if len(df) < 500:
            log.warning("%s: only %d candles, skipping", sym, len(df))
            continue
        data[sym] = df
    return data


def _report(title: str, m: dict, as_json: bool, extra: dict | None = None) -> None:
    if as_json:
        print(json.dumps({"report": title, **(extra or {}), "metrics": m}, default=str))
        return
    print(f"\n=== {title} ===")
    for k, v in m.items():
        print(f"  {k:26s} {v}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="accusignals", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--market", choices=["futures", "spot"], default="futures")
        sp.add_argument("--interval", help="entry timeframe, e.g. 1m, 3m, 5m (default 5m)")
        sp.add_argument("--htf", help="trend timeframe, e.g. 15m, 1h (default 1h)")
        sp.add_argument("--min-score", type=float, help="confluence threshold (default 6)")
        sp.add_argument("--symbols", help="comma separated, e.g. BTCUSDT,ETHUSDT")
        sp.add_argument("--top", type=int, default=15, help="use the N most liquid USDT pairs")
        sp.add_argument("--config", help="JSON file with {'strategy': {...}, 'risk': {...}}")
        sp.add_argument("--base-url", help="override API host (e.g. https://data-api.binance.vision for spot)")
        sp.add_argument("--json", action="store_true", help="machine-readable JSON lines on stdout")
        sp.add_argument("-v", "--verbose", action="store_true")

    def hist(sp):
        sp.add_argument("--days", type=int, default=30)
        sp.add_argument("--data-dir", default=str(HOME / "data"))
        sp.add_argument("--refresh", action="store_true", help="re-download instead of topping up the cache")
        sp.add_argument("--risk", type=float, help="%% equity risked per trade")
        sp.add_argument("--equity", type=float, default=1000.0)
        sp.add_argument("--out", default=str(HOME / "results"), help="directory for trades/equity CSVs")

    sp = sub.add_parser("health", help="connectivity check")
    common(sp)

    sp = sub.add_parser("scan", help="live signals")
    common(sp)
    sp.add_argument("--loop", action="store_true", help="keep scanning at every candle close")
    sp.add_argument("--no-footprint", action="store_true", help="skip aggTrades footprint confirmation")
    sp.add_argument("--notify", action="store_true", help="push to Telegram / webhook (see README)")
    sp.add_argument("--signals-file", default=str(HOME / "signals" / "signals.jsonl"),
                    help="append every signal here as JSON lines ('' to disable)")
    sp.add_argument("--cooldown-bars", type=int, default=6, help="suppress repeat alerts for the same symbol/side")

    for name, help_ in (("download", "cache klines to CSV"), ("backtest", "backtest"),
                        ("walkforward", "walk-forward optimisation")):
        sp = sub.add_parser(name, help=help_)
        common(sp)
        hist(sp)
        if name == "walkforward":
            sp.add_argument("--train-days", type=float, default=21)
            sp.add_argument("--test-days", type=float, default=7)
    return p


def run(args) -> None:
    scfg, rcfg = load_configs(args.config, args)
    client = BinanceClient(args.market, base_url=args.base_url)
    offset = client.sync_time()
    if abs(offset) > 1000:
        log.warning("local clock is %.1fs off Binance time; using exchange time (consider syncing Windows time)",
                    offset / 1000)

    if args.cmd == "health":
        syms = client.top_symbols(args.top)
        res = {"ok": True, "market": args.market, "api": client.base, "server_time": str(client.now()),
               "clock_offset_ms": offset, "top_symbols": syms}
        print(json.dumps(res) if args.json else "\n".join(f"{k}: {v}" for k, v in res.items()))
        return

    if args.cmd == "scan":
        syms = _symbols(args, client)
        log.info("scanning %d symbols on %s %s/%s: %s", len(syms), args.market, scfg.interval, scfg.htf_interval,
                 ",".join(syms))
        sc = Scanner(client, syms, scfg, not args.no_footprint, args.notify, args.json, args.signals_file or None,
                     args.cooldown_bars)
        if args.loop:
            sc.run_forever()
        elif not sc.scan_once():
            if args.json:
                print(json.dumps({"signals": 0, "symbols": syms, "time": str(client.now())}))
            else:
                print("No qualifying setups on the last closed candle.")
        return

    data = _load_data(args, client, scfg)
    if args.cmd == "download":
        res = {sym: {"candles": len(df), "from": str(df.index[0]), "to": str(df.index[-1])} for sym, df in data.items()}
        print(json.dumps(res) if args.json else "\n".join(f"{k}: {v}" for k, v in res.items()))
        return
    if not data:
        raise SystemExit("no symbols with enough history")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    extra = {"symbols": list(data), "interval": scfg.interval, "days": args.days, "market": args.market}
    if args.cmd == "backtest":
        m, taken, curve = run_backtest(data, scfg, rcfg, args.equity)
        _report("backtest (in-sample)", m, args.json, extra)
    else:
        m, folds, taken, curve = walk_forward(data, scfg, rcfg, train_days=args.train_days, test_days=args.test_days,
                                              start_equity=args.equity)
        folds.to_csv(out / "walkforward_folds.csv", index=False)
        _report("walk-forward OUT-OF-SAMPLE", m, args.json, extra)
    taken.to_csv(out / f"{args.cmd}_trades.csv", index=False)
    curve.rename("equity").to_csv(out / f"{args.cmd}_equity.csv")
    log.info("trades and equity curve written to %s", out)


def main(argv: list[str] | None = None) -> None:
    # Windows consoles default to cp1252; never crash on a stray character.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        run(args)
    except KeyboardInterrupt:
        log.info("stopped by user")
    except requests.RequestException as exc:
        log.error("Binance API unreachable: %s", exc)
        if args.json:
            print(json.dumps({"ok": False, "error": f"binance_unreachable: {exc}"}))
        sys.exit(2)


if __name__ == "__main__":
    main()
