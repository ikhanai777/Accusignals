---
name: accusignals
description: Generate, validate and report Binance crypto scalping signals (entry, stop-loss, take-profits, confidence, reasons) from live Binance market data using the Accusignals CLI. Use when asked for trade signals, a market scan, a backtest or a walk-forward validation on Binance pairs.
---

# Accusignals: Binance scalping signals

Accusignals is a Python CLI in the Accusignals project folder, called
`ACCUSIGNALS_DIR` below (the folder that holds `accusignals/`, `windows/`, and
`requirements.txt`). It uses only real data from the Binance public API. No API
key is needed, and it never places orders.

## Running it

| Host | Command prefix |
|---|---|
| Windows (cmd / PowerShell) | `ACCUSIGNALS_DIR\windows\accusignals.bat` |
| Linux / macOS / WSL2 | `ACCUSIGNALS_DIR/.venv/bin/python -m accusignals` |

**Deployment:** to install and run it live on a Windows 10 PC, follow
`ACCUSIGNALS_DIR/docs/DEPLOY_WINDOWS_HERMES.md` step by step.

**Preferred live mode: the dashboard.** When `http://127.0.0.1:8765/healthz`
answers `{"ok": true}`, the dashboard is already running its own scanner. Read
signals from it (`GET /api/signals?limit=20`, `GET /api/status`) and don't
start a second scanner. To start it, run
`start "Accusignals" /min ACCUSIGNALS_DIR\windows\dashboard.bat`. The runbook
lists every endpoint and each `tracking.status` value.

First-time setup: on Windows, run `windows\setup.bat`. On Linux or WSL2, run
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
**Always pass `--json`.** Each result is then one JSON object per line on
stdout, and logs go to stderr.

Exit codes: `0` ok, `2` Binance unreachable (report it and don't invent
signals), `1` other error (read stderr or `logs/accusignals.log`).

## Tasks

`accusignals` below means the command prefix from the table above.

1. **Health check** (run this first each session):
   `accusignals health --json` returns `{"ok": true, "clock_offset_ms": ..., "top_symbols": [...]}`.
2. **One scan of the last closed candle:**
   `accusignals scan --json [--symbols BTCUSDT,ETHUSDT] [--interval 5m --htf 1h] [--market futures|spot]`.
   Each signal line has this shape (the values are illustrative):
   ```json
   {"symbol":"SOLUSDT","market":"futures","interval":"5m","side":"LONG","direction":1,
    "bar_close_time":"2026-09-24T14:05:00+00:00","entry":151.23,"sl":150.71,"tp1":151.75,"tp2":152.27,
    "stop_pct":0.344,"tp1_fraction":0.5,"score":8.0,"confidence":57.1,
    "reasons":["htf_trend","vwap_pullback","delta","sweep"],"footprint":{"delta":812.4,"poc":151.2,"bias":1}}
   ```
   If nothing qualifies, you get `{"signals": 0, ...}`. That's a normal result.
   Report "no setups". Never make one up.
   To scan every candle, run the command once per interval, a few seconds after
   the candle closes (for 5m: :00:05, :05:05 and so on). Repeat alerts are
   suppressed across runs through `signals/signals.jsonl`.
3. **Continuous scanner** (a long-running background process):
   `windows\scan_loop.bat --json`. New signals are appended to
   `ACCUSIGNALS_DIR/signals/signals.jsonl`. Read the new lines from that file
   rather than scraping the console.
4. **Validate before trusting:**
   `accusignals walkforward --json --top 10 --days 90`. This takes several
   minutes. Report `metrics.profit_factor`, `metrics.avg_r`,
   `metrics.win_rate_pct`, `metrics.max_drawdown_pct` and
   `metrics.avg_daily_return_pct`. These are **out-of-sample** figures and the
   only ones to quote as expected performance. A `backtest` result is in-sample
   and optimistic.

## How to present a signal to the user

- Include the symbol, side, entry, SL (with `stop_pct`), TP1 (close
  `tp1_fraction`, then move the SL to breakeven), TP2, confidence and the
  reasons in plain words. For example, "sweep" means a liquidity sweep of a
  prior swing, and "delta" means aggressive buyers or sellers dominated the
  candle.
- A signal is only valid near `entry` shortly after `bar_close_time`. If more
  than one candle interval has passed, or price has already moved more than
  half the way to TP1, call it stale.
- Sizing: risk 0.25–1% of the account per trade (see `risk_per_trade_pct` in
  `config.example.json`). Respect the daily stop: stop after -3% or 4 losses in
  a row for the day, and once the daily target is hit.
- Never promise returns. A 5–10% daily target is a ceiling that stops trading
  on good days, not an expectation. Quote walk-forward numbers, not hopes.

## Config

Pass `--config ACCUSIGNALS_DIR\config.example.json` or a copy of it. Useful
flags are `--min-score` (higher means fewer trades with a higher win rate),
`--interval 1m --htf 15m` for faster scalps, and `--no-footprint` to skip the
aggTrades confirmation (faster, fewer API calls). For Telegram alerts, set the
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` environment variables and add
`--notify`.
