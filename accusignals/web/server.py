"""Local web dashboard: ``python -m accusignals dashboard``.

Standard library only (``http.server``), so nothing extra to install on
Windows. It runs the live scanner in a background thread, serves the signal
feed and its real outcome tracking, runs backtests / walk-forwards as jobs, and
serves a mobile-friendly single-page UI from ``static/``.

Security: binds to 127.0.0.1 by default. Binding to another interface (e.g. to
open it from your phone on the same Wi-Fi) requires an access token.
POST endpoints require ``Content-Type: application/json``, which browsers
cannot send cross-site without a CORS preflight that this server never
approves, so other websites can't drive it.
"""
from __future__ import annotations

import hmac
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, replace
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

from .. import __version__
from ..backtest import run_backtest
from ..data import INTERVAL_MS, BinanceClient, drop_unclosed, load_history
from ..optimize import walk_forward
from ..risk import RiskConfig
from ..scanner import Scanner, to_json
from ..strategy import StrategyConfig
from ..tracker import FINAL, summarize, track_signal

log = logging.getLogger("accusignals")
STATIC = Path(__file__).parent / "static"
FILES = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
         "/app.css": ("app.css", "text/css"), "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
         "/icon.svg": ("icon.svg", "image/svg+xml")}
EDITABLE = {"market", "interval", "htf_interval", "min_score", "symbols", "top", "use_footprint", "notify"}
INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h"}


def _clean(o):
    """JSON-safe conversion for numpy / pandas values."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    return o


class App:
    def __init__(self, home: Path, scfg: StrategyConfig, rcfg: RiskConfig, market: str = "futures",
                 symbols: list[str] | None = None, top: int = 15, use_footprint: bool = True, notify: bool = False,
                 base_url: str | None = None, client: BinanceClient | None = None):
        self.home = Path(home)
        self.signals_file = self.home / "signals" / "signals.jsonl"
        self.log_file = self.home / "logs" / "accusignals.log"
        self.data_dir = self.home / "data"
        self.results_dir = self.home / "results"
        self.base_url = base_url
        self.settings = {"market": market, "interval": scfg.interval, "htf_interval": scfg.htf_interval,
                         "min_score": scfg.min_score, "symbols": symbols or [], "top": top,
                         "use_footprint": use_footprint, "notify": notify}
        self.scfg, self.rcfg = scfg, rcfg
        self.client = client or BinanceClient(market, base_url=base_url)
        self.scan_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.scanner: Scanner | None = None
        self.status = {"last_scan": None, "last_scan_signals": 0, "last_error": None, "symbols": []}
        self.jobs: dict[str, dict] = {}
        self.track_cache: dict[str, tuple[float, dict]] = {}

    # ---------- scanner ----------
    def _make_scanner(self) -> Scanner:
        st = self.settings
        if st["market"] != self.client.market:
            self.client = BinanceClient(st["market"], base_url=self.base_url)
        self.scfg = replace(self.scfg, interval=st["interval"], htf_interval=st["htf_interval"],
                            min_score=float(st["min_score"]))
        self.client.sync_time()
        syms = st["symbols"] or self.client.top_symbols(int(st["top"]))
        self.status["symbols"] = syms
        return Scanner(self.client, syms, self.scfg, st["use_footprint"], st["notify"], as_json=True,
                       signals_file=self.signals_file)

    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        if self.running():
            return
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="scanner", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=60)  # let an in-flight scan finish

    def _loop(self) -> None:
        stop = self.stop_event
        while not stop.is_set():
            try:
                with self.scan_lock:
                    self.scanner = self._make_scanner()
                self.status["last_error"] = None
                log.info("dashboard scanner started: %s %s/%s, %d symbols", self.client.market, self.scfg.interval,
                         self.scfg.htf_interval, len(self.scanner.symbols))
                while not stop.is_set():
                    if stop.wait(self.scanner.seconds_to_next_scan()):
                        break
                    self.scan_now()
                    try:
                        self.client.sync_time()
                    except Exception as exc:
                        log.warning("time sync failed: %s", exc)
            except Exception as exc:  # network down etc.: report, wait, retry
                self.status["last_error"] = str(exc)
                log.error("scanner error: %s (retrying in 30s)", exc)
                stop.wait(30)

    def scan_now(self) -> list[dict]:
        with self.scan_lock:
            if self.scanner is None:
                self.scanner = self._make_scanner()
            found = self.scanner.scan_once()
        self.status["last_scan"] = self.client.now().isoformat()
        self.status["last_scan_signals"] = len(found)
        return found

    def update_settings(self, new: dict) -> dict:
        bad = set(new) - EDITABLE
        if bad:
            raise ValueError(f"unknown settings: {sorted(bad)}")
        s = dict(self.settings, **new)
        if s["market"] not in ("futures", "spot"):
            raise ValueError("market must be futures or spot")
        if s["interval"] not in INTERVALS or s["htf_interval"] not in INTERVALS | {"2h", "4h"}:
            raise ValueError("unsupported interval")
        if INTERVAL_MS[s["htf_interval"]] <= INTERVAL_MS[s["interval"]]:
            raise ValueError("trend timeframe must be higher than the entry timeframe")
        if isinstance(s["symbols"], str):
            s["symbols"] = [x.strip().upper() for x in s["symbols"].split(",") if x.strip()]
        s["min_score"] = float(s["min_score"])
        s["top"] = max(1, min(int(s["top"]), 50))
        s["use_footprint"], s["notify"] = bool(s["use_footprint"]), bool(s["notify"])
        was_running = self.running()
        if was_running:
            self.stop()
        self.settings = s
        with self.scan_lock:
            self.scanner = None
        if was_running:
            self.start()
        return s

    # ---------- signals & tracking ----------
    def read_signals(self, limit: int = 100) -> list[dict]:
        if not self.signals_file.exists():
            return []
        out = []
        for line in self.signals_file.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out[::-1]  # newest first

    def track(self, sig: dict) -> dict:
        key = f"{sig['market']}|{sig['symbol']}|{sig['interval']}|{sig['bar_close_time']}|{sig['direction']}"
        cached = self.track_cache.get(key)
        if cached and (cached[1]["status"] in FINAL or time.time() - cached[0] < 30):
            return cached[1]
        client = self.client if sig["market"] == self.client.market else BinanceClient(sig["market"], base_url=self.base_url)
        step = INTERVAL_MS[sig["interval"]]
        start = int(pd.Timestamp(sig["bar_close_time"]).value // 1_000_000) - step
        candles = client.klines(sig["symbol"], sig["interval"], self.scfg.max_hold_bars + 3, start_ms=start)
        candles = drop_unclosed(candles, client.now()).drop(columns=["close_time"], errors="ignore")
        res = track_signal(sig, candles, self.scfg, self.rcfg)
        self.track_cache[key] = (time.time(), res)
        return res

    def signals_with_tracking(self, limit: int = 50, track: int = 30) -> list[dict]:
        sigs = self.read_signals(limit)
        for i, s in enumerate(sigs):
            if i < track:
                try:
                    s["tracking"] = self.track(s)
                except Exception as exc:
                    s["tracking"] = {"status": "unknown", "error": str(exc)}
        return sigs

    # ---------- jobs ----------
    def start_job(self, kind: str, params: dict) -> str:
        if kind not in ("backtest", "walkforward"):
            raise ValueError("type must be backtest or walkforward")
        if any(j["state"] == "running" for j in self.jobs.values()):
            raise ValueError("a job is already running")
        days = max(7, min(int(params.get("days", 30)), 365))
        top = max(1, min(int(params.get("top", 5)), 30))
        syms = [x.strip().upper() for x in str(params.get("symbols", "")).split(",") if x.strip()]
        job_id = uuid.uuid4().hex[:8]
        job = {"id": job_id, "type": kind, "state": "running", "started": time.time(), "params":
               {"days": days, "top": top, "symbols": syms, "interval": self.scfg.interval, "market": self.client.market},
               "progress": "downloading Binance history"}
        self.jobs[job_id] = job
        threading.Thread(target=self._run_job, args=(job, kind, days, top, syms), daemon=True).start()
        return job_id

    def _run_job(self, job: dict, kind: str, days: int, top: int, syms: list[str]) -> None:
        try:
            client = BinanceClient(self.client.market, base_url=self.base_url)
            client.sync_time()
            symbols = syms or client.top_symbols(top)
            data = load_history(client, symbols, self.scfg.interval, days, self.data_dir)
            if not data:
                raise ValueError("no symbols with enough history")
            job["progress"] = f"running {kind} on {len(data)} symbols"
            if kind == "backtest":
                m, taken, curve = run_backtest(data, self.scfg, self.rcfg)
                folds = None
            else:
                m, folds, taken, curve = walk_forward(data, self.scfg, self.rcfg)
            self.results_dir.mkdir(parents=True, exist_ok=True)
            taken.to_csv(self.results_dir / f"{kind}_trades.csv", index=False)
            step = max(1, len(curve) // 400)
            job.update({
                "state": "done", "progress": "done", "metrics": m, "symbols": list(data),
                "equity": [[t.isoformat(), round(float(v), 2)] for t, v in curve.iloc[::step].items()],
                "folds": folds.assign(test_start=folds["test_start"].astype(str)).to_dict("records") if folds is not None else None,
            })
        except Exception as exc:
            log.exception("job %s failed", job["id"])
            job.update({"state": "error", "progress": str(exc)})

    # ---------- misc ----------
    def status_payload(self) -> dict:
        nxt = self.scanner.seconds_to_next_scan() if (self.scanner and self.running()) else None
        return {
            "version": __version__, "running": self.running(), "settings": self.settings,
            "server_time": self.client.now().isoformat(), "clock_offset_ms": self.client.clock_offset_ms,
            "next_scan_in_s": round(nxt) if nxt is not None else None, **self.status,
            "risk": asdict(self.rcfg), "strategy": {"tp1_r": self.scfg.tp1_r, "tp2_r": self.scfg.tp2_r,
                                                    "max_hold_bars": self.scfg.max_hold_bars,
                                                    "max_score": self.scfg.max_score()},
        }

    def tail_log(self, lines: int = 200) -> list[str]:
        if not self.log_file.exists():
            return []
        with self.log_file.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-lines:]


class Handler(BaseHTTPRequestHandler):
    app: App
    token: str | None = None
    server_version = "Accusignals"

    def log_message(self, fmt, *args):  # route access logs to our logger at debug level
        log.debug("http %s - %s", self.address_string(), fmt % args)

    # ---------- helpers ----------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(_clean(obj), default=str).encode("utf-8"), "application/json")

    def _authorized(self, query: dict) -> tuple[bool, dict]:
        if not self.token:
            return True, {}
        supplied = None
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[7:]
        if not supplied and "token" in query:
            supplied = query["token"][0]
            if hmac.compare_digest(supplied, self.token):  # remember it so the phone doesn't need ?token every time
                return True, {"Set-Cookie": f"as_token={self.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"}
        if not supplied:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            supplied = c["as_token"].value if "as_token" in c else None
        return bool(supplied) and hmac.compare_digest(supplied, self.token), {}

    def _body(self) -> dict:
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise PermissionError("POST requires Content-Type: application/json")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    # ---------- routes ----------
    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        ok, extra = self._authorized(q)
        if not ok:
            return self._send(401, b"Unauthorized: open the URL with ?token=YOUR_TOKEN", "text/plain")
        if url.path in FILES:
            name, ctype = FILES[url.path]
            return self._send(200, (STATIC / name).read_bytes(), ctype, extra)
        app = self.app
        try:
            if url.path == "/api/status":
                return self._json(app.status_payload())
            if url.path == "/api/signals":
                limit = min(int(q.get("limit", ["50"])[0]), 500)
                sigs = app.signals_with_tracking(limit)
                return self._json({"signals": sigs, "summary": summarize([s.get("tracking", {}) for s in sigs])})
            if url.path == "/api/prices":
                syms = [s for s in q.get("symbols", [""])[0].split(",") if s]
                return self._json(app.client.prices(syms or None))
            if url.path == "/api/candles":
                sym = q.get("symbol", [""])[0].upper()
                if not sym.isalnum():
                    return self._json({"error": "bad symbol"}, 400)
                interval = q.get("interval", [app.scfg.interval])[0]
                if interval not in INTERVAL_MS:
                    return self._json({"error": "bad interval"}, 400)
                df = app.client.klines(sym, interval, min(int(q.get("limit", ["120"])[0]), 500))
                rows = [[t.isoformat(), r.open, r.high, r.low, r.close, r.volume] for t, r in df.iterrows()]
                return self._json({"symbol": sym, "interval": interval, "candles": rows})
            if url.path == "/api/jobs":
                return self._json(sorted(({k: v for k, v in j.items() if k not in ("equity", "folds")}
                                          for j in app.jobs.values()), key=lambda j: -j["started"]))
            if url.path.startswith("/api/jobs/"):
                job = app.jobs.get(url.path.rsplit("/", 1)[-1])
                return self._json(job) if job else self._json({"error": "no such job"}, 404)
            if url.path == "/api/logs":
                return self._json({"lines": app.tail_log(min(int(q.get("lines", ["200"])[0]), 2000))})
            if url.path == "/healthz":
                return self._json({"ok": True})
        except Exception as exc:
            log.error("GET %s failed: %s", url.path, exc)
            return self._json({"error": str(exc)}, 502)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        url = urlparse(self.path)
        ok, _ = self._authorized(parse_qs(url.query))
        if not ok:
            return self._json({"error": "unauthorized"}, 401)
        app = self.app
        try:
            body = self._body()
            if url.path == "/api/scanner/start":
                app.start()
                return self._json({"running": True})
            if url.path == "/api/scanner/stop":
                app.stop()
                return self._json({"running": False})
            if url.path == "/api/scan":
                return self._json({"signals": app.scan_now()})
            if url.path == "/api/settings":
                return self._json(app.update_settings(body))
            if url.path == "/api/jobs":
                return self._json({"id": app.start_job(body.get("type", "backtest"), body)})
        except PermissionError as exc:
            return self._json({"error": str(exc)}, 415)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            log.error("POST %s failed: %s", url.path, exc)
            return self._json({"error": str(exc)}, 502)
        self._json({"error": "not found"}, 404)


def serve(app: App, host: str = "127.0.0.1", port: int = 8765, token: str | None = None) -> None:
    handler = type("BoundHandler", (Handler,), {"app": app, "token": token})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    log.info("dashboard listening on http://%s:%d/%s", host, port, f"?token={token}" if token else "")
    try:
        httpd.serve_forever()
    finally:
        app.stop()
        httpd.server_close()


__all__ = ["App", "serve", "to_json"]
