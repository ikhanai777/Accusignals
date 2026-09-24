"""Command line: ``python -m accusignals <command>``.

  scan         live signals for the most liquid Binance pairs
  download     cache historical klines to CSV
  backtest     backtest on cached/downloaded data
  walkforward  walk-forward optimisation (out-of-sample results)
  demo         offline run on synthetic data (sanity check, no edge expected)
"""
from __future__ import annotations

import argparse
import json
from dataclasses import fields, replace
from pathlib import Path

import pandas as pd

from .backtest import run_backtest
from .data import BinanceClient, load_csv, save_csv, synthetic_ohlcv
from .optimize import walk_forward
from .risk import RiskConfig
from .scanner import run_forever, scan
from .strategy import StrategyConfig


def load_configs(path: str | None, args) -> tuple[StrategyConfig, RiskConfig]:
    scfg, rcfg = StrategyConfig(), RiskConfig()
    if path:
        raw = json.loads(Path(path).read_text())
        scfg = _apply(scfg, raw.get("strategy", {}))
        rcfg = _apply(rcfg, raw.get("risk", {}))
    overrides = {"interval": args.interval, "htf_interval": args.htf, "min_score": args.min_score}
    scfg = replace(scfg, **{k: v for k, v in overrides.items() if v is not None})
    if getattr(args, "risk", None) is not None:
        rcfg = replace(rcfg, risk_per_trade_pct=args.risk)
    if getattr(args, "market", None) == "spot" and not (path and "fee_rate" in raw.get("risk", {})):
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
        return [s.upper() for s in args.symbols.split(",")]
    return client.top_symbols(args.top)


def _load_data(args, client: BinanceClient, scfg: StrategyConfig) -> dict[str, pd.DataFrame]:
    if args.csv:
        return {Path(p).stem.split("_")[0]: load_csv(p) for p in args.csv}
    data = {}
    for sym in _symbols(args, client):
        path = Path(args.data_dir) / args.market / f"{sym}_{scfg.interval}_{args.days}d.csv"
        if path.exists() and not args.refresh:
            df = load_csv(path)
        else:
            print(f"[data] downloading {sym} {scfg.interval} {args.days}d")
            df = client.history(sym, scfg.interval, args.days).drop(columns=["close_time"], errors="ignore")
            save_csv(df, path)
        data[sym] = df
    return data


def _report(title: str, m: dict) -> None:
    print(f"\n=== {title} ===")
    for k, v in m.items():
        print(f"  {k:26s} {v}")


def main(argv: list[str] | None = None) -> None:
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

    def hist(sp):
        sp.add_argument("--days", type=int, default=30)
        sp.add_argument("--csv", nargs="*", help="use these CSV files instead of downloading")
        sp.add_argument("--data-dir", default="data")
        sp.add_argument("--refresh", action="store_true", help="re-download even if cached")
        sp.add_argument("--risk", type=float, help="%% equity risked per trade")
        sp.add_argument("--equity", type=float, default=1000.0)
        sp.add_argument("--out", default="results", help="directory for trades/equity CSVs")

    sp = sub.add_parser("scan", help="live signals")
    common(sp)
    sp.add_argument("--loop", action="store_true", help="keep scanning at every candle close")
    sp.add_argument("--no-footprint", action="store_true", help="skip aggTrades footprint confirmation")
    sp.add_argument("--notify", action="store_true", help="push to Telegram / webhook (see README)")

    sp = sub.add_parser("download", help="cache klines to CSV")
    common(sp)
    hist(sp)

    sp = sub.add_parser("backtest", help="backtest")
    common(sp)
    hist(sp)

    sp = sub.add_parser("walkforward", help="walk-forward optimisation")
    common(sp)
    hist(sp)
    sp.add_argument("--train-days", type=float, default=21)
    sp.add_argument("--test-days", type=float, default=7)

    sp = sub.add_parser("demo", help="offline synthetic-data run")
    common(sp)
    sp.add_argument("--bars", type=int, default=20000)
    sp.add_argument("--risk", type=float)

    args = p.parse_args(argv)
    scfg, rcfg = load_configs(args.config, args)
    client = BinanceClient(args.market, base_url=args.base_url)

    if args.cmd == "scan":
        syms = _symbols(args, client)
        print(f"[scan] {len(syms)} symbols on {args.market} {scfg.interval}/{scfg.htf_interval}: {', '.join(syms)}")
        if args.loop:
            run_forever(client, syms, scfg, not args.no_footprint, args.notify)
        else:
            if not scan(client, syms, scfg, not args.no_footprint, args.notify):
                print("[scan] no qualifying setups on the last closed candle")
        return

    if args.cmd == "demo":
        data = {f"SYN{i}": synthetic_ohlcv(args.bars, scfg.interval, seed=i) for i in range(3)}
        m, _, _ = run_backtest(data, scfg, rcfg)
        _report("synthetic random-walk backtest (expect ~breakeven minus fees: proves no look-ahead)", m)
        return

    data = _load_data(args, client, scfg)
    if args.cmd == "download":
        print(f"[data] cached {len(data)} symbols under {args.data_dir}/{args.market}")
        return

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.cmd == "backtest":
        m, taken, curve = run_backtest(data, scfg, rcfg, args.equity)
        _report(f"backtest {scfg.interval} {len(data)} symbols {args.days}d (in-sample)", m)
    else:
        m, folds, taken, curve = walk_forward(data, scfg, rcfg, train_days=args.train_days, test_days=args.test_days,
                                              start_equity=args.equity)
        folds.to_csv(out / "walkforward_folds.csv", index=False)
        _report(f"walk-forward OUT-OF-SAMPLE {scfg.interval} {len(data)} symbols", m)
    taken.to_csv(out / f"{args.cmd}_trades.csv", index=False)
    curve.rename("equity").to_csv(out / f"{args.cmd}_equity.csv")
    print(f"\n[out] trades and equity curve written to {out}/")


if __name__ == "__main__":
    main()
