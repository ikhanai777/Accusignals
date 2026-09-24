# Accusignals

Scalping signals for Binance (USDⓈ-M futures or spot). Each signal is scored on
**confluence**: higher-timeframe trend, VWAP, market structure, support and
resistance, liquidity sweeps, volume, real order-flow delta and CVD, footprint
imbalances, and candle patterns. The project comes with an honest backtester and
**walk-forward validation**, so you can measure whether it works before you risk
money.

Alert format (illustrative numbers):

```
BTCUSDT LONG 🟢  [5m]  confidence 61%  (score 8.5)
  Entry  64231.50
  SL     64012.30  (0.34%)
  TP1    64450.70  (close 50%, move SL to breakeven)
  TP2    64669.90
  Why:   htf_trend,ms_trend,ema_stack,vwap_side,vwap_pullback,rsi_reset,delta,cvd_slope,sweep
  Footprint: {'delta': 41.2, 'poc': 64220.0, 'stacked_buy_imb': 3, 'bias': 1}
```

## Read this first: about "5–10% per day"

A steady 5–10% a day isn't a realistic goal, and a system built to chase it will
blow up the account. For scale, 5% compounded daily turns $1,000 into about $54
billion in a year. The best scalping desks target a few percent a *month*, with
tight risk control.

What this project does instead:

* **A daily profit target** (`daily_profit_target_pct`, default 5%). Once a
  good day reaches it, the bot stops trading so the gains aren't given back.
  It's a ceiling, not a promise.
* **A daily loss limit and tilt stop.** Trading stops for the day after a -3%
  day or 4 losses in a row.
* **Fixed-fractional sizing.** Each stop-out costs 1% of equity by default.
* **A high win rate by design.** Half the position closes at 1R and the stop
  moves to breakeven (fees included), so many trades end as small wins instead
  of losses. The cost is a lower average win, which is always the trade-off.
  Judge the system on **expectancy and profit factor**, not win rate alone.

Treat any number you see as unproven until `walkforward` shows it
**out of sample** on recent data, followed by weeks of paper trading.

## How a signal is built

| Evidence | Why it matters for scalping |
|---|---|
| HTF trend (1h EMA 50/200, price on the right side) | Trading with the dominant flow is the single biggest win-rate filter. It's a hard gate by default. |
| Market structure (HH/HL or LH/LL from confirmed pivots) | Confirms the trend on the entry timeframe. |
| EMA 9/21 stack | Short-term momentum alignment. |
| Session VWAP side and pullback to VWAP | VWAP is the institutional fair-value anchor. Pullbacks into it in a trend are the classic scalp entry. |
| RSI reset (dipped under 45, turning up) / Stoch-RSI cross | Enter after a pullback, not at a stretched extreme. |
| Volume z-score ≥ 1 | Real participation behind the move. |
| **Delta** from Binance `taker_buy_volume` | Exact aggressive buy minus sell volume per candle, not an estimate. |
| CVD slope and **CVD divergence** | A new low with no new selling means sellers are exhausted. |
| **Absorption** | Heavy volume with no price progress means passive orders are soaking up the aggression. |
| **Liquidity sweep** | Price wicks through a prior swing (stop hunt) and closes back inside. It's one of the highest-quality reversal triggers. |
| S/R zone bounce (clustered pivots, ≥2 touches) | Entry at a defended level. |
| Engulfing / pin bar | Rejection candle on the trigger bar. |
| **Room to target** | Penalises setups with S/R closer than 1R: no point buying into a wall. |
| **Footprint** (live, from aggTrades) | Stacked diagonal bid/ask imbalances and bar delta. A footprint that disagrees with the setup vetoes the signal. |

Gates: ADX ≥ 15 (no dead chop), the trigger candle must close in the trade's
direction, and the stop distance must be between 0.2% and 2.5%. Anything
tighter gets eaten by fees and noise.

**Stops and targets.** The stop goes beyond the recent structure low/high plus
0.2 ATR, clamped to 0.7–2.5 ATR. TP1 is 1R (50% of the position). TP2 is 2R, or
just in front of the next S/R zone if that comes first. A time stop exits after
36 bars.

## Install on Windows 10

1. Install Python 3.10 or newer from python.org, and tick **"Add python.exe to
   PATH"** during setup.
2. Double-click `windows\setup.bat`, or run it from cmd. It creates `.venv`,
   installs numpy, pandas, requests and pytest, then runs a Binance
   connectivity check.
3. Use `windows\accusignals.bat <command>` for everything after that.

Linux, macOS or WSL2: `python3 -m venv .venv && .venv/bin/pip install -r
requirements.txt`, then `.venv/bin/python -m accusignals <command>`.

No API key is needed. Everything uses Binance public market data, and there's
no sample or synthetic data anywhere in the tool. Keep the Windows clock
synced, although the tool measures its offset from Binance server time on every
scan anyway.

## Usage (Windows)

```bat
REM Connectivity, clock offset and the current most-liquid pairs
windows\accusignals.bat health

REM Score the last closed 5m candle on the 15 most liquid USDT-M perpetuals
windows\accusignals.bat scan

REM Run forever, scanning at each candle close; auto-restarts after network drops
set TELEGRAM_BOT_TOKEN=...
set TELEGRAM_CHAT_ID=...
windows\scan_loop.bat

REM Faster scalping: 1m entries with the 15m trend
windows\accusignals.bat scan --interval 1m --htf 15m --symbols BTCUSDT,ETHUSDT,SOLUSDT

REM Backtest 60 days of real history (cached in data\ and topped up on each run)
windows\accusignals.bat backtest --top 10 --days 60

REM THE number that matters: walk-forward, out of sample
windows\accusignals.bat walkforward --top 10 --days 90 --train-days 21 --test-days 7
```

Outputs:
- `signals\signals.jsonl` gets every signal, one JSON object per line.
- `logs\accusignals.log` is the rotating log.
- `results\` holds the trade lists, equity curves and walk-forward folds.

## Dashboard (desktop and mobile)

```bat
windows\dashboard.bat                  REM http://127.0.0.1:8765, with the live scanner running
windows\dashboard.bat --host 0.0.0.0   REM also reachable from your phone on the same Wi-Fi (token protected)
```

The dashboard is a single-page web app served by Python's standard library, so
there's nothing extra to install and it makes no external requests. It has
five tabs:
- **Signals:** live cards with entry, stop, TP1 and TP2, confidence, the
  reasons that fired, the live price position between stop and target, and a
  candle chart with the levels on tap.
- **Track record:** each emitted signal replayed against the real Binance
  candles that followed it: win rate, average R and cumulative R.
- **Backtest:** runs walk-forward or backtest jobs on real history, with KPIs
  and an equity curve.
- **Settings:** market, timeframes, score threshold, pairs, footprint and
  alerts.
- **Logs.**

It works in light and dark mode, can be added to a phone's home screen, and
shows browser notifications for new signals.

For start at logon, phone access and firewall setup, see
[`docs/DEPLOY_WINDOWS_HERMES.md`](docs/DEPLOY_WINDOWS_HERMES.md). To have Nous
Hermes Agent deploy it for you, paste [`docs/HERMES_PROMPT.md`](docs/HERMES_PROMPT.md) into Hermes.

## Running it from an agent (Hermes Agent)

Add `--json` to any command. Results then go to stdout as JSON lines, and logs
go to stderr. The exit code is `0` on success, `2` if Binance is unreachable,
and `1` on any other error. Full agent instructions are in
[`skills/accusignals/SKILL.md`](skills/accusignals/SKILL.md), a standard
`SKILL.md` skill. Copy that folder into the Hermes skills directory. There's
also a short summary in [`AGENTS.md`](AGENTS.md). The agent can either:

- call `windows\accusignals.bat scan --json` once per candle close (repeat
  alerts are suppressed across runs), or
- keep `windows\scan_loop.bat --json` running in the background and read new
  lines from `signals\signals.jsonl`.

Every setting can be overridden with `--config config.example.json`. Useful
flags are `--market spot`, `--min-score 7`, and `--risk 0.5`. Spot fees switch
to 0.1% automatically.

If `api.binance.com` is geo-blocked for you, spot data is also served at
`--base-url https://data-api.binance.vision`.

## Backtest honesty

* Signals use closed candles only and fill at the **next bar's open**.
  `tests/test_core.py::test_no_lookahead` checks that appending future bars
  never changes a past signal.
* The higher-timeframe trend only uses HTF candles that had already closed.
* Pivots and S/R levels exist only from the bar on which they were confirmed.
* If a bar touches both the stop and a target, the **stop is assumed first**.
* Taker fees (0.04% futures / 0.1% spot) and slippage are charged on every fill.
* Cached history is topped up with the newest closed candles before every run,
  and gaps from exchange downtime are logged.
* `walkforward` picks parameters on a rolling 21-day window and reports only the
  following unseen 7 days, stitched together.

Known limits: the backtest is candle-based, so the footprint filter is live-only
(historical aggTrades are too large to replay here). Funding costs aren't
modelled. Each symbol is simulated one position at a time before the portfolio
rules are applied.

## Suggested workflow to maximise wins

1. Run `walkforward --days 90` on 10–15 liquid pairs. Look for an out-of-sample
   profit factor above 1.3, positive average R, and a max drawdown you can live
   with. Drop pairs that consistently lose.
2. Raise `min_score` until expectancy peaks. Higher scores mean fewer trades but
   a higher win rate.
3. Paper trade with `windows\scan_loop.bat` for 2–4 weeks and compare live fills with the
   backtest.
4. Go live with small size (`--risk 0.25`) and scale up only after it matches.

## Layout

```
accusignals/
  data.py        Binance REST client (klines, aggTrades, top symbols), server-time sync, CSV cache
  indicators.py  EMA/RSI/ATR/ADX/MACD/Stoch-RSI/Bollinger/VWAP bands/Supertrend/z-score
  structure.py   confirmed pivots, market structure, liquidity sweeps, S/R zones, candle patterns
  orderflow.py   delta, CVD, CVD divergence, absorption, footprint bars, volume profile
  strategy.py    feature pipeline and confluence scoring, stops/targets, explainable reasons
  risk.py        sizing and daily risk rules
  backtest.py    bar-by-bar simulator, portfolio rules, metrics
  optimize.py    walk-forward optimisation
  scanner.py     live scanner with footprint confirmation
  notify.py      console / Telegram / webhook output
  cli.py         command line (--json for agents)
  tracker.py     live track record: replays emitted signals against later candles
  web/           dashboard server (stdlib) and static mobile-first UI
windows/         setup, accusignals, scan_loop, dashboard, install_autostart, allow_phone_access (.bat)
docs/            DEPLOY_WINDOWS_HERMES.md runbook
skills/          SKILL.md for Hermes Agent
```

*Not financial advice. Leveraged crypto trading can lose more than your stake.*
