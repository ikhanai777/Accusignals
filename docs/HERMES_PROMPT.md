# Paste-ready task for Nous Hermes Agent

Copy everything below the line into Hermes Agent on the Windows 10 PC. It's
self-contained. For the full details (every endpoint, troubleshooting,
WSL2 notes), the agent should read `C:\Accusignals\docs\DEPLOY_WINDOWS_HERMES.md`
once the code is cloned.

---

You are deploying **Accusignals**, a Binance crypto scalping **signal**
provider, on this Windows 10 PC. It runs locally, uses **real live Binance
market data**, and serves a web dashboard at http://127.0.0.1:8765.

## Ground rules (follow them at every step)
1. **Real data only.** Never invent, estimate or give "example" signals,
   prices or performance numbers. If something fails, report the exact error.
2. **Signals only.** Never place trades or orders, and never ask for Binance
   API keys. None are needed.
3. **Ask me first** before you install software, change the firewall, enable
   start at logon, or store any token.
4. **Be honest about performance.** Quote only the walk-forward
   (out-of-sample) results as expected performance. Never promise a daily
   return. The 5% daily figure is a stop that ends trading for the day, not a
   target.
5. Exit code **2** from any `accusignals` command means Binance is
   unreachable. Tell me and stop. Don't retry in a loop.
6. Run commands in `cmd.exe`. If you run inside WSL2, wrap them as
   `cmd.exe /c "..."` and use the Windows paths shown.

## Step 1: Prerequisites
Run `git --version`, `py -3 --version` and `curl.exe --version`.
- You need Git, Python **3.10 or newer**, and curl (built into Windows 10 1803+).
- If Git or Python is missing, ask me, then run
  `winget install -e --id Git.Git` and `winget install -e --id Python.Python.3.12`.
- Open a new terminal afterwards so PATH updates take effect.

## Step 2: Get the code
```bat
git clone https://github.com/ikhanai777/accusignals.git C:\Accusignals
cd /d C:\Accusignals
git checkout main
dir windows
```
- If `dir windows` doesn't list `dashboard.bat`, the pull request isn't merged
  yet. Run `git checkout claude/binance-scalping-signals-aktk7r` instead.
- If Git asks me to sign in to GitHub, wait for me to finish.

## Step 3: Install and check the Binance connection
```bat
cd /d C:\Accusignals
windows\setup.bat
windows\accusignals.bat health --json
```
- **Success:** the JSON has `"ok": true` and a `top_symbols` list, and the exit
  code is 0. Report `clock_offset_ms` and `top_symbols` to me.
- **Exit code 2 with HTTP 451 or "restricted location":** Binance isn't
  available from this location. Tell me and stop. Don't try to work around it.
- **Exit code 2 with a timeout or proxy error:** a network, firewall or
  antivirus problem. Tell me.

## Step 4: Configure
```bat
copy /Y config.example.json config.json
```
Then show me the `risk` section and ask whether to change
`risk_per_trade_pct`. Suggest 0.25–0.5 while I'm starting out.

## Step 5: Validate on real history (this takes a few minutes)
```bat
windows\accusignals.bat walkforward --json --top 10 --days 90 --config config.json
```
From the last JSON line, report `metrics`: `trades`, `win_rate_pct`,
`profit_factor`, `avg_r`, `max_drawdown_pct`, `avg_daily_return_pct` and
`pct_green_days`.
- If `profit_factor` > 1.3 and `avg_r` > 0, say there's out-of-sample evidence
  of an edge.
- Otherwise, say plainly that there's no edge in this period, and suggest
  raising `min_score` or changing timeframes.

## Step 6: Launch the live dashboard and scanner
```bat
cd /d C:\Accusignals
start "Accusignals" /min windows\dashboard.bat
```
Wait about 10 seconds, then run:
```bat
curl.exe -s http://127.0.0.1:8765/healthz
curl.exe -s http://127.0.0.1:8765/api/status
```
- **Success:** `{"ok": true}`, and the status JSON shows `"running": true`, a
  `symbols` list and `"last_error": null`.
- Run `start http://127.0.0.1:8765/` to open the dashboard for me.
- After one candle interval (5 minutes by default), check `/api/status` again.
  `last_scan` should now be set.

## Step 7: Optional extras. Ask me for each one.
- **Phone access (same Wi-Fi):**
  1. The Wi-Fi network must be set to Private in Windows.
  2. Run `windows\allow_phone_access.bat` as administrator.
  3. Close the dashboard window and restart it with
     `start "Accusignals" /min windows\dashboard.bat --host 0.0.0.0`.
  4. Give me the `http://<pc-ip>:8765/?token=...` link it prints. The token is
     also in `C:\Accusignals\.dashboard_token`.
  5. Never port-forward 8765 on the router.
- **Start at logon:** run `windows\install_autostart.bat`, adding
  `--host 0.0.0.0` if phone access is on. Then run
  `schtasks /Run /TN "Accusignals Dashboard"`.
- **Telegram alerts:**
  1. Run `setx TELEGRAM_BOT_TOKEN "<token I give you>"` and
     `setx TELEGRAM_CHAT_ID "<chat id>"`.
  2. Restart the dashboard.
  3. Turn on **Telegram / webhook alerts** in the Settings tab.

## Step 8: Report back to me
Give me one message with:
- the health result
- the walk-forward metrics, labelled "out-of-sample"
- the dashboard URL(s)
- which optional extras are enabled
- any warnings from `logs\accusignals.log`

## Afterwards: reading and relaying signals
- Get signals with `curl.exe -s "http://127.0.0.1:8765/api/signals?limit=20"`.
  Don't start a second scanner.
- For each signal with `tracking.status` = `pending`, tell me:
  - symbol and side (LONG/SHORT)
  - entry
  - stop (with `stop_pct`)
  - TP1 (close half, then move the stop to breakeven)
  - TP2
  - confidence
  - the reasons in plain words
- Call a signal **stale** once more than one candle interval has passed after
  `bar_close_time`, or once price has covered half the distance to TP1.
- Live results are in the same JSON under `summary`: `win_rate_pct`, `avg_r`
  and `total_r`.
- Remind me of the daily stop rules: stop for the day at the loss limit, after
  4 losses in a row, or once the profit lock is reached.

## Updating later
```bat
cd /d C:\Accusignals
git pull
windows\setup.bat
```
Then close the dashboard window (it restarts itself), or run
`schtasks /Run /TN "Accusignals Dashboard"`.
