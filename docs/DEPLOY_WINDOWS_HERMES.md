# Deploy Accusignals live on Windows 10: runbook for Nous Hermes Agent

This runbook is written for an AI agent (Nous Hermes Agent) that operates a
Windows 10 PC for its owner. Follow the steps in order. Each step lists the
exact command, what success looks like, and what to do when it fails.

## Rules for the agent (read first)

1. **Real data only.** Every price, candle, trade and signal comes from the
   Binance public API. Never invent, estimate or "example" a signal, a price or a
   performance number. If a command fails, report the failure.
2. **Signals only.** Accusignals never places orders, and you must not place
   orders on the owner's behalf unless they explicitly ask you to, in a separate
   instruction.
3. **Ask the owner before** installing software, changing the firewall (needs
   admin), enabling autostart, or storing a Telegram token.
4. **Report honestly.** Quote walk-forward (out-of-sample) figures as expected
   performance, never the in-sample backtest. A 5–10% daily return is not an
   expectation. The daily profit target is a stop that ends trading for the day.
5. Exit code `2` from any `accusignals` command means Binance is unreachable.
   Say so and stop. Don't retry in a tight loop.

Paths below assume the project lives in `C:\Accusignals`. Substitute the real
folder if the owner picks another one.

---

## Step 1: Check prerequisites

Run in `cmd.exe`:

```bat
git --version
py -3 --version
```

Success: `git version 2.x` and `Python 3.10` or newer (3.11 or 3.12 are ideal).

If either is missing, **ask the owner**, then install it with winget:

```bat
winget install -e --id Git.Git
winget install -e --id Python.Python.3.12
```

Open a **new** terminal afterwards so PATH updates take effect. Windows 10
1803+ ships `curl.exe`, which the later checks use. Confirm with
`curl.exe --version`.

## Step 2: Get the code

```bat
git clone https://github.com/ikhanai777/accusignals.git C:\Accusignals
cd /d C:\Accusignals
git checkout main
```

- If the repository is private, Git opens a GitHub sign-in window
  (Git Credential Manager). The owner completes it.
- If `main` doesn't exist yet (the pull request isn't merged), use
  `git checkout claude/binance-scalping-signals-aktk7r` instead.

Success: `dir` shows `accusignals\`, `windows\`, `requirements.txt` and `README.md`.

## Step 3: Install and check connectivity

```bat
cd /d C:\Accusignals
windows\setup.bat
```

`setup.bat` creates `.venv`, installs numpy, pandas, requests and pytest, and
then runs `accusignals health`. Success ends with lines like these:

```
ok: True
market: futures
api: https://fapi.binance.com
clock_offset_ms: <number>
top_symbols: ['BTCUSDT', 'ETHUSDT', ...]
```

For a machine-readable check at any time:

```bat
windows\accusignals.bat health --json
```

It should print `{"ok": true, ...}` and exit with code 0. Failures:

| Symptom | Meaning | Action |
|---|---|---|
| exit 2, `451` or "restricted location" | Binance blocks this country or IP | Tell the owner. The tool can't work from here, and this isn't something to work around. |
| exit 2, timeout or proxy error | No internet, or a firewall or antivirus is blocking Python | Ask the owner to check the connection and allow `python.exe`. |
| `clock_offset_ms` beyond ±1000 | The PC clock has drifted | Already corrected automatically. Suggest *Settings → Time → Sync now*. |
| `Python 3.10+ required` | Old Python | Do Step 1 again. |

Optionally, run the tests: `.venv\Scripts\python.exe -m pytest -q`. The first
run downloads real BTCUSDT history into `tests\.cache\`.

## Step 4: Configure

```bat
copy config.example.json config.json
```

Edit `config.json` only as the owner instructs. The main settings are:

- `risk.risk_per_trade_pct`: default 1.0. Suggest 0.25–0.5 while the owner is starting out.
- `risk.daily_profit_target_pct` and `risk.daily_loss_limit_pct`: the daily stop rules.
- `strategy.min_score`: higher means fewer signals with a higher win rate.
- `strategy.interval` / `strategy.htf_interval`: entry and trend timeframes (default 5m/1h; faster scalping uses 1m/15m).

`windows\dashboard.bat` picks up `config.json` automatically.

## Step 5: Validate on real history (don't skip)

```bat
windows\accusignals.bat walkforward --json --top 10 --days 90 --config config.json
```

This takes a few minutes and downloads real Binance history into `data\`. The
last stdout line is JSON. Report these fields from `metrics`: `trades`,
`win_rate_pct`, `profit_factor`, `avg_r`, `max_drawdown_pct`,
`avg_daily_return_pct` and `pct_green_days`.

How to read them:
- `profit_factor` > 1.3 and `avg_r` > 0: there's evidence of an edge. Proceed to paper trading.
- `profit_factor` ≤ 1.0 or `avg_r` ≤ 0: no edge in this period. Tell the owner
  plainly, and suggest raising `min_score`, changing timeframes, or testing
  other pairs. Don't present the signals as profitable.

## Step 6: Launch the dashboard

Start it in its own minimized window so the agent's terminal isn't blocked:

```bat
cd /d C:\Accusignals
start "Accusignals" /min windows\dashboard.bat
```

This starts the web UI and the live scanner, which scans a few seconds after
every candle close. `dashboard.bat` restarts itself if the process exits.

Verify it, allowing about 10 seconds for startup:

```bat
curl.exe -s http://127.0.0.1:8765/healthz
curl.exe -s http://127.0.0.1:8765/api/status
```

Success: `{"ok": true}`, and the status JSON shows `"running": true`, a list of
`symbols`, and `"last_error": null`. After the first candle closes,
`last_scan` is set.

Open it for the owner with `start http://127.0.0.1:8765/`.

The dashboard has five tabs:
- **Signals:** live signal cards (entry, stop, TP1, TP2, confidence, reasons,
  live price position, and a chart on tap).
- **Track record:** every signal replayed against the real candles that
  followed it (win rate, average R, cumulative R).
- **Backtest:** runs walk-forward or backtest jobs.
- **Settings:** market, timeframes, score threshold, pairs, alerts.
- **Logs.**

## Step 7 (optional, ask the owner): open it from a phone

1. The PC and phone must be on the same Wi-Fi, and Windows must treat that
   network as **Private** (*Settings → Network & Internet → Wi-Fi → network →
   Private*).
2. Right-click `windows\allow_phone_access.bat` and choose **Run as
   administrator**. This opens TCP 8765 on private networks only.
3. Close the dashboard window, then start it bound to the network:
   ```bat
   start "Accusignals" /min windows\dashboard.bat --host 0.0.0.0
   ```
4. The dashboard window prints a line like
   `From your phone (same Wi-Fi): http://192.168.x.x:8765/?token=...`.
   The token is also saved in `C:\Accusignals\.dashboard_token`. Give that
   exact URL to the owner. Opening it once stores the token in the phone
   browser. Then use *Add to Home Screen* for an app-like icon.

Never expose port 8765 to the internet (no router port-forwarding).

## Step 8 (optional, ask the owner): start at logon

```bat
windows\install_autostart.bat
REM or, with phone access:
windows\install_autostart.bat --host 0.0.0.0
```

To start it immediately, run `schtasks /Run /TN "Accusignals Dashboard"`. To
remove it, run `schtasks /Delete /TN "Accusignals Dashboard" /F`. The PC must
stay awake. Suggest *Settings → System → Power & sleep → Sleep: Never* while
it's plugged in.

## Step 9 (optional, ask the owner): Telegram alerts

The owner creates a bot with @BotFather and gets their chat id. Then run:

```bat
setx TELEGRAM_BOT_TOKEN "<token from owner>"
setx TELEGRAM_CHAT_ID "<chat id>"
```

Restart the dashboard (the new variables only reach newly started processes).
Then enable **Telegram / webhook alerts** in the Settings tab, or add
`--notify` to the dashboard command.

---

## Operating it day to day

Read signals from the running dashboard. Don't start a second scanner.

```bat
curl.exe -s "http://127.0.0.1:8765/api/signals?limit=20"
```

Each item has `symbol`, `side`, `entry`, `sl`, `tp1`, `tp2`, `stop_pct`,
`confidence`, `reasons`, `bar_close_time` and `tracking.status`:

| `tracking.status` | Meaning |
|---|---|
| `pending` | Just posted. Actionable near `entry` during the next candle. |
| `open` | Filled, in the trade. |
| `tp1` | TP1 banked (half closed), stop moved to breakeven, the rest running. |
| `tp2` | Hit TP2. |
| `tp1+be` | TP1, then the remainder stopped at breakeven. |
| `sl` | Stopped out. |
| `time`, `tp1+time` | Closed by the time stop. |

`summary` gives the live track record: `closed`, `win_rate_pct`, `avg_r` and
`total_r`. Every new signal is also appended to `signals\signals.jsonl`.

When relaying a signal to the owner:
- Include the symbol, side, entry, SL (with `stop_pct`), TP1 (close half, then
  move the stop to breakeven), TP2, confidence, and the reasons in plain words.
- A `pending` signal is stale once one candle interval has passed after
  `bar_close_time`, or if price has already covered half the distance to TP1.
  Say "stale" rather than passing it on as fresh.
- Remind them of the day's rules: stop after the daily loss limit, after 4
  losses in a row, or once the daily target is reached.

A useful daily summary uses `/api/signals?limit=200`. Count today's signals
by `tracking.status` and report `summary.win_rate_pct`, `summary.avg_r` and
`summary.total_r`.

### Other endpoints

| Endpoint | Use |
|---|---|
| `GET /api/status` | Scanner state, settings, last scan, next scan, last error |
| `GET /api/prices?symbols=BTCUSDT,ETHUSDT` | Latest Binance prices |
| `GET /api/candles?symbol=BTCUSDT&limit=120` | Recent candles |
| `POST /api/scan` | Scan now. Body `{}` with header `Content-Type: application/json` |
| `POST /api/scanner/start` / `stop` | Start or stop the scanner |
| `POST /api/settings` | e.g. `{"min_score": 7, "symbols": "BTCUSDT,ETHUSDT"}` |
| `POST /api/jobs` then `GET /api/jobs/<id>` | `{"type": "walkforward", "days": 60, "top": 5}` |
| `GET /api/logs?lines=200` | Recent log lines |

For example, from `cmd`:
`curl.exe -s -X POST -H "Content-Type: application/json" -d "{}" http://127.0.0.1:8765/api/scan`

If the dashboard runs with `--host 0.0.0.0`, add
`-H "Authorization: Bearer <token from .dashboard_token>"` to every call.

## Updating

```bat
cd /d C:\Accusignals
git pull
windows\setup.bat
```

Then close the dashboard window. `dashboard.bat` restarts it automatically, or
run `schtasks /Run /TN "Accusignals Dashboard"`.

## If Hermes Agent runs inside WSL2

Hermes can drive the Windows side through interop. Use Windows paths and
`cmd.exe`:

```bash
cmd.exe /c "cd /d C:\Accusignals && windows\setup.bat"
cmd.exe /c "cd /d C:\Accusignals && start \"Accusignals\" /min windows\dashboard.bat"
curl -s http://127.0.0.1:8765/api/status   # WSL2 reaches Windows services on localhost
```

Keep one installation (the Windows one) so the owner's dashboard, data and
signal history stay in one place.

## Final verification checklist

- [ ] `windows\accusignals.bat health --json` shows `"ok": true`.
- [ ] The walk-forward metrics were reported to the owner verbatim.
- [ ] `curl.exe -s http://127.0.0.1:8765/api/status` shows `"running": true` and `"last_error": null`.
- [ ] After one candle interval, `last_scan` is recent.
- [ ] The dashboard opens at `http://127.0.0.1:8765/` (and on the phone, if Step 7 was done).
- [ ] `logs\accusignals.log` has no repeated `ERROR` lines.
- [ ] The owner knows that signals aren't guaranteed, and the risk rules in the Settings tab.

## Troubleshooting

| Problem | Fix |
|---|---|
| Dashboard shows "Binance error: retrying" | Check `/api/status` → `last_error`. If it's 451, Binance isn't available from this location. Otherwise it's a network issue. The scanner retries every 30 s. |
| Port 8765 already in use | Another dashboard is running. Close it, or add `--port 8766`. |
| Phone can't connect | Check the network is Private, run `allow_phone_access.bat` as admin, confirm the `--host 0.0.0.0` window is running, and use the exact URL including `?token=`. |
| No signals for hours | Normal in chop. The strategy only fires on strong confluence. Lower `min_score` slightly (e.g. 5.5) or add pairs. Check `/api/status` shows scans happening. |
| `401 Unauthorized` | The token is missing or wrong. Use the URL from `.dashboard_token`. |
| `pip` errors in setup | Update pip with `.venv\Scripts\python.exe -m pip install --upgrade pip`, then rerun `windows\setup.bat`. |
