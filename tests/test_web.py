"""Dashboard server and live-signal tracker. The Binance HTTP layer is replaced
by FakeSession (Binance-format payloads) so these run offline; the product
itself only talks to the real API."""
import json
import threading
import urllib.error
import urllib.request

import pandas as pd
import pytest

from accusignals.data import BinanceClient
from accusignals.risk import RiskConfig
from accusignals.strategy import StrategyConfig
from accusignals.tracker import summarize, track_signal
from accusignals.web.server import App, Handler
from http.server import ThreadingHTTPServer

from test_core import FakeResp, FakeSession, _frame

SIG = {"symbol": "BTCUSDT", "market": "futures", "interval": "5m", "side": "LONG", "direction": 1,
       "bar_close_time": "2024-01-01T00:05:00+00:00", "entry": 100.0, "sl": 99.0, "tp1": 101.0, "tp2": 102.0,
       "stop_pct": 1.0, "tp1_fraction": 0.5, "score": 7.0, "confidence": 50.0, "reasons": ["sweep"], "footprint": None}


def test_tracker_statuses():
    cfg, r = StrategyConfig(), RiskConfig(fee_rate=0, slippage_bps=0)
    only_signal_bar = _frame([[100, 100, 100, 100]])
    assert track_signal(SIG, only_signal_bar, cfg, r)["status"] == "pending"
    running = _frame([[100, 100, 100, 100], [100, 100.5, 99.5, 100.2]])
    assert track_signal(SIG, running, cfg, r)["status"] == "open"
    tp1 = _frame([[100, 100, 100, 100], [100, 101.2, 100.1, 101], [101, 101.5, 100.5, 101.2]])
    assert track_signal(SIG, tp1, cfg, r)["status"] == "tp1"
    stopped = _frame([[100, 100, 100, 100], [100, 100.2, 98.5, 98.8]])
    res = track_signal(SIG, stopped, cfg, r)
    assert res["status"] == "sl" and res["r"] == pytest.approx(-1)
    s = summarize([res, {"status": "pending"}, {"status": "open"}])
    assert s["closed"] == 1 and s["losses"] == 1 and s["pending"] == 1 and s["open"] == 1


@pytest.fixture
def server(tmp_path):
    (tmp_path / "signals").mkdir()
    (tmp_path / "signals" / "signals.jsonl").write_text(json.dumps(SIG) + "\n", encoding="utf-8")
    kl = [[1704067200000 + i * 300_000, "100", "100.5", "99.5", "100.2", "10", 1704067200000 + i * 300_000 + 299_999,
           "1000", 5, "6", "600", "0"] for i in range(3)]
    sess = FakeSession({"/klines": FakeResp(kl), "/ticker/price": FakeResp([{"symbol": "BTCUSDT", "price": "100.3"}]),
                        "/time": FakeResp({"serverTime": 1704068200000})})
    app = App(tmp_path, StrategyConfig(), RiskConfig(), client=BinanceClient("futures", session=sess))
    handler = type("H", (Handler,), {"app": app, "token": "secret"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req) as r:
        return r.status, r.read(), dict(r.headers)


def test_requires_token(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(server + "/api/status")
    assert e.value.code == 401
    status, body, headers = _get(server + "/?token=secret")
    assert status == 200 and b"Accusignals" in body and "as_token=secret" in headers["Set-Cookie"]
    assert _get(server + "/api/status", {"Cookie": "as_token=secret"})[0] == 200
    with pytest.raises(urllib.error.HTTPError):
        _get(server + "/api/status", {"Authorization": "Bearer wrong"})


def test_api_endpoints(server):
    auth = {"Authorization": "Bearer secret"}
    st = json.loads(_get(server + "/api/status", auth)[1])
    assert st["running"] is False and st["settings"]["interval"] == "5m"
    sig = json.loads(_get(server + "/api/signals", auth)[1])
    assert sig["signals"][0]["symbol"] == "BTCUSDT" and sig["signals"][0]["tracking"]["status"] in ("open", "pending")
    assert json.loads(_get(server + "/api/prices?symbols=BTCUSDT", auth)[1]) == {"BTCUSDT": 100.3}
    c = json.loads(_get(server + "/api/candles?symbol=BTCUSDT", auth)[1])
    assert len(c["candles"]) == 3
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(server + "/api/candles?symbol=../etc", auth)
    assert e.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(server + "/../../etc/passwd", auth)
    assert e.value.code == 404


def test_post_requires_json_and_validates(server):
    auth = {"Authorization": "Bearer secret"}
    form = urllib.request.Request(server + "/api/settings", data=b"min_score=1", method="POST",
                                  headers={**auth, "Content-Type": "application/x-www-form-urlencoded"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(form)
    assert e.value.code == 415  # blocks cross-site form posts
    bad = urllib.request.Request(server + "/api/settings", data=json.dumps({"interval": "1h", "htf_interval": "5m"}).encode(),
                                 method="POST", headers={**auth, "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(bad)
    assert e.value.code == 400
    ok = urllib.request.Request(server + "/api/settings", data=json.dumps({"min_score": 7, "symbols": "btcusdt, ethusdt"}).encode(),
                                method="POST", headers={**auth, "Content-Type": "application/json"})
    with urllib.request.urlopen(ok) as r:
        s = json.loads(r.read())
    assert s["min_score"] == 7.0 and s["symbols"] == ["BTCUSDT", "ETHUSDT"]
