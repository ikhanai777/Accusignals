import numpy as np
import pandas as pd
import pytest
import requests

from accusignals import indicators as ind
from accusignals import orderflow as of
from accusignals import structure as st
from accusignals.backtest import metrics, run_backtest, simulate_portfolio, simulate_symbol, trades_frame
from accusignals.data import BinanceClient, drop_unclosed, klines_to_frame, missing_bars
from accusignals.notify import format_signal
from accusignals.optimize import walk_forward
from accusignals.risk import RiskConfig, position_size
from accusignals.scanner import tick_for
from accusignals.strategy import StrategyConfig, generate_signals


@pytest.fixture(scope="module")
def df(binance_5m):
    return binance_5m.iloc[-6000:]


def test_no_lookahead(df):
    """Signals for bar k must not change when later bars are appended."""
    cfg = StrategyConfig()
    full = generate_signals(df, cfg)
    cols = ["signal", "long_score", "short_score", "sl", "tp1", "tp2"]
    for k in (3500, 4200, 5100, 5999):
        part = generate_signals(df.iloc[: k + 1], cfg)
        a, b = full[cols].iloc[k], part[cols].iloc[-1]
        pd.testing.assert_series_equal(a, b, check_names=False, rtol=1e-9)


def test_signals_have_valid_geometry(df):
    s = generate_signals(df, StrategyConfig(min_score=4))
    longs, shorts = s[s.signal == 1], s[s.signal == -1]
    assert len(longs) and len(shorts)
    assert (longs.sl < longs.entry).all() and (longs.tp1 > longs.entry).all() and (longs.tp2 > longs.tp1).all()
    assert (shorts.sl > shorts.entry).all() and (shorts.tp1 < shorts.entry).all() and (shorts.tp2 < shorts.tp1).all()
    assert s.loc[s.signal != 0, "reasons"].str.len().gt(0).all()


def test_indicators_basic(df):
    r = ind.rsi(df.close).dropna()
    assert r.between(0, 100).all()
    assert (ind.atr(df).dropna() > 0).all()
    e = ind.ema(pd.Series([1.0, 2, 3, 4, 5]), 3)
    assert e.iloc[2] == pytest.approx(2.25) and e.iloc[4] == pytest.approx(0.5 * 5 + 0.5 * 3.125)
    vwap, lo, hi = ind.session_vwap(df)
    day = df.index.floor("D")
    assert (vwap >= df.low.groupby(day).cummin() - 1e-9).all()
    assert (vwap <= df.high.groupby(day).cummax() + 1e-9).all()
    assert (lo <= vwap).all() and (hi >= vwap).all()
    assert set(ind.supertrend(df).unique()) <= {1.0, -1.0}


def test_pivots_confirmed_late():
    idx = pd.date_range("2024", periods=11, freq="5min", tz="UTC")
    high = pd.Series([1, 2, 3, 4, 5, 9, 5, 4, 3, 2, 1], index=idx, dtype=float)
    d = pd.DataFrame({"high": high, "low": high - 0.5})
    ph, _ = st.confirmed_pivots(d, left=3, right=3)
    assert ph.dropna().index.tolist() == [idx[8]]  # peak at bar 5, known at bar 8
    assert ph.dropna().iloc[0] == 9


def test_footprint_imbalances():
    t0 = pd.Timestamp("2024-01-01", tz="UTC")
    rows = []
    for i, px in enumerate([100.0, 100.1, 100.2, 100.3]):  # aggressive buying, stacked up the book
        rows.append((t0 + pd.Timedelta(seconds=i), px, 10.0, False))
        rows.append((t0 + pd.Timedelta(seconds=i, milliseconds=1), px, 1.0, True))
    t = pd.DataFrame(rows, columns=["t", "price", "qty", "is_buyer_maker"]).set_index("t")
    bars = of.footprint(t, "5min", tick=0.1)
    assert len(bars) == 1
    b = bars[0]
    assert b.delta == pytest.approx(36.0)
    assert b.stacked_buy_imbalances >= 3
    assert of.footprint_bias(bars) == 1


def test_delta_from_taker_volume():
    d = pd.DataFrame({"volume": [10.0, 10.0], "taker_buy_volume": [8.0, 2.0], "high": [1, 1], "low": [0, 0], "close": [1, 0]})
    assert of.bar_delta(d).tolist() == [6.0, -6.0]


def _frame(rows):
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="5min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def _sig(n, i, side, sl, tp2):
    s = pd.DataFrame({"signal": 0, "sl": np.nan, "tp2": np.nan, "score": np.nan, "reasons": ""}, index=range(n))
    s.loc[i, ["signal", "sl", "tp2", "score"]] = [side, sl, tp2, 7.0]
    return s


def test_backtest_stop_first_when_ambiguous():
    df = _frame([[100, 100, 100, 100], [100, 100, 100, 100], [100, 103, 98, 100], [100, 100, 100, 100]])
    cfg, r = StrategyConfig(tp1_r=1, tp2_r=2), RiskConfig(fee_rate=0, slippage_bps=0)
    tr = simulate_symbol(df, _sig(4, 0, 1, 99.0, 102.0), cfg, r)
    assert tr[0].outcome == "sl" and tr[0].r_multiple == pytest.approx(-1)


def test_backtest_tp1_then_tp2_with_fees():
    df = _frame([[100, 100, 100, 100], [100, 101.2, 100.1, 101], [101, 102.5, 100.5, 102], [102, 102, 102, 102]])
    cfg, r = StrategyConfig(tp1_r=1, tp2_r=2, tp1_fraction=0.5), RiskConfig(fee_rate=0.0004, slippage_bps=0)
    tr = simulate_symbol(df, _sig(4, 0, 1, 99.0, 102.0), cfg, r)
    t = tr[0]
    assert t.outcome == "tp2"
    fees = 0.0004 * (100 + 0.5 * 101 + 0.5 * 102)  # entry + size-weighted exit notional
    assert t.r_multiple == pytest.approx((0.5 * 1 + 0.5 * 2 - fees) / 1.0)


def test_backtest_short_breakeven():
    df = _frame([[100, 100, 100, 100], [100, 100.2, 98.9, 99], [99, 100.5, 99, 100.2], [100, 100, 100, 100]])
    cfg, r = StrategyConfig(tp1_r=1, tp2_r=2, tp1_fraction=0.5), RiskConfig(fee_rate=0, slippage_bps=0)
    tr = simulate_symbol(df, _sig(4, 0, -1, 101.0, 98.0), cfg, r)
    assert tr[0].outcome == "tp1+be" and tr[0].r_multiple == pytest.approx(0.5)


def _cand(times, rets, sym="A"):
    rows = []
    for k, (t, ret) in enumerate(zip(times, rets)):
        t = pd.Timestamp(t, tz="UTC")
        rows.append({"symbol": f"{sym}{k}", "side": 1, "entry_time": t, "exit_time": t + pd.Timedelta("10min"),
                     "entry": 100.0, "sl": 99.0, "ret": ret, "r_multiple": ret * 100, "outcome": "x"})
    return pd.DataFrame(rows)


def test_portfolio_daily_target_and_loss_limit():
    r = RiskConfig(risk_per_trade_pct=1, daily_profit_target_pct=2.5, daily_loss_limit_pct=10, max_consecutive_losses=99)
    c = _cand([f"2024-01-01 0{h}:00" for h in range(6)], [0.01] * 6)  # each trade = +1R = +1%
    taken, curve = simulate_portfolio(c, r)
    assert len(taken) == 3  # stops once +2.5% is banked
    r2 = RiskConfig(risk_per_trade_pct=1, daily_loss_limit_pct=1.5, max_consecutive_losses=99)
    c2 = _cand([f"2024-01-01 0{h}:00" for h in range(6)] + ["2024-01-02 01:00"], [-0.01] * 7)
    taken2, _ = simulate_portfolio(c2, r2)
    assert len(taken2) == 3  # two -1% losses breach -1.5%, then one trade on the next day
    assert taken2["entry_time"].iloc[-1].day == 2  # new day resets the limit


def test_position_size_caps_leverage():
    r = RiskConfig(risk_per_trade_pct=1, max_leverage=5)
    assert position_size(1000, 100, 99, r) == pytest.approx(10.0)
    assert position_size(1000, 100, 99.99, r) == pytest.approx(50.0)  # capped at 5x


class FakeResp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.headers, self.text = payload, status, {}, str(payload)

    def json(self):
        return self.payload


class FakeSession:
    """Replays Binance-format responses by endpoint so client logic can be
    tested offline. The payloads mirror Binance's documented schema."""

    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        for path, resp in self.routes.items():
            if url.endswith(path):
                return resp(params) if callable(resp) else resp
        raise AssertionError(f"unexpected url {url}")


def _kline(t_ms, px=1.0):
    return [t_ms, str(px), str(px + 1), str(px - 0.5), str(px + 0.5), "10", t_ms + 299_999, "15", 5, "6", "9", "0"]


def test_klines_parsing():
    f = klines_to_frame([_kline(1700000000000)])
    assert f["taker_buy_volume"].iloc[0] == 6.0 and f.index[0].year == 2023


def test_top_symbols_filters_non_tradable():
    info = {"symbols": [
        {"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT", "contractType": "PERPETUAL"},
        {"symbol": "ETHUSDT", "status": "TRADING", "quoteAsset": "USDT", "contractType": "PERPETUAL"},
        {"symbol": "USDCUSDT", "status": "TRADING", "quoteAsset": "USDT", "contractType": "PERPETUAL"},
        {"symbol": "OLDUSDT", "status": "SETTLING", "quoteAsset": "USDT", "contractType": "PERPETUAL"},
        {"symbol": "BTCUSDT_250926", "status": "TRADING", "quoteAsset": "USDT", "contractType": "CURRENT_QUARTER"},
    ]}
    tick = [{"symbol": "BTCUSDT", "quoteVolume": "9e9"}, {"symbol": "USDCUSDT", "quoteVolume": "8e9"},
            {"symbol": "ETHUSDT", "quoteVolume": "5e9"}, {"symbol": "OLDUSDT", "quoteVolume": "7e9"},
            {"symbol": "BTCUSDT_250926", "quoteVolume": "9e9"}]
    sess = FakeSession({"/exchangeInfo": FakeResp(info), "/ticker/24hr": FakeResp(tick)})
    assert BinanceClient("futures", session=sess).top_symbols(5) == ["BTCUSDT", "ETHUSDT"]


def test_client_does_not_retry_bad_request():
    sess = FakeSession({"/klines": FakeResp({"code": -1121, "msg": "Invalid symbol."}, status=400)})
    with pytest.raises(requests.HTTPError):
        BinanceClient("futures", session=sess).klines("NOPEUSDT", "5m")
    assert len(sess.calls) == 1


def test_update_appends_and_replaces_forming_bar():
    t0 = 1700000000000
    sess = FakeSession({"/klines": lambda p: FakeResp([_kline(p["startTime"], 2.0), _kline(p["startTime"] + 300_000, 3.0)])})
    c = BinanceClient("futures", session=sess)
    df = klines_to_frame([_kline(t0 - 300_000), _kline(t0, 1.0)])
    out = c.update(df, "BTCUSDT", "5m")
    assert len(out) == 3 and out["open"].iloc[1] == 2.0  # forming bar refreshed
    assert sess.calls[0][1]["startTime"] == t0


def test_drop_unclosed_uses_exchange_time():
    df = klines_to_frame([_kline(1700000000000), _kline(1700000300000)])
    now = pd.Timestamp(1700000300000 + 1000, unit="ms", tz="UTC")  # 1s into the second candle
    assert len(drop_unclosed(df, now)) == 1


def test_format_signal_is_ascii():
    s = {"symbol": "BTCUSDT", "side": "LONG", "market": "futures", "interval": "5m", "confidence": 55.0, "score": 7.5,
         "entry": 65000.0, "sl": 64800.0, "stop_pct": 0.31, "tp1": 65200.0, "tp1_fraction": 0.5, "tp2": 65400.0,
         "reasons": ["htf_trend", "sweep"], "footprint": None, "bar_close_time": pd.Timestamp("2025-01-01", tz="UTC")}
    format_signal(s).encode("cp1252")  # Windows console codepage


def test_tick_for():
    assert tick_for(65000) == pytest.approx(10.0)
    assert tick_for(0.5) == pytest.approx(0.0001)


def test_backtest_on_real_history_is_consistent(binance_5m):
    m, taken, curve = run_backtest({"BTCUSDT": binance_5m})
    if m["trades"] == 0:
        pytest.skip("no trades in this window")
    assert (taken["exit_time"] >= taken["entry_time"]).all()
    assert (taken["entry_time"] > taken["signal_time"]).all()  # filled on the bar after the signal
    assert curve.iloc[-1] == pytest.approx(1000 + taken["pnl"].sum())


def test_walk_forward_runs_on_real_history(binance_5m):
    grid = {"min_score": [5.0, 6.0], "tp2_r": [2.0]}
    m, folds, taken, curve = walk_forward({"BTCUSDT": binance_5m}, grid=grid, train_days=14, test_days=7, min_trades=5)
    assert len(folds) >= 2
    if len(taken):
        assert (taken["entry_time"] >= folds["test_start"].min()).all()  # nothing from the first training window


def test_real_history_is_clean(binance_5m):
    assert binance_5m.index.is_monotonic_increasing and not binance_5m.index.duplicated().any()
    assert (binance_5m["high"] >= binance_5m[["open", "close"]].max(axis=1)).all()
    assert (binance_5m["taker_buy_volume"] <= binance_5m["volume"] + 1e-9).all()
    assert missing_bars(binance_5m, "5m") < 50


def test_scanner_cooldown_survives_restart(tmp_path):
    from accusignals.scanner import Scanner

    f = tmp_path / "signals.jsonl"
    f.write_text('{"symbol": "BTCUSDT", "market": "futures", "interval": "5m", "direction": 1, '
                 '"bar_close_time": "2025-01-01T00:05:00+00:00"}\nnot json\n', encoding="utf-8")
    sc = Scanner(BinanceClient("futures", session=FakeSession({})), ["BTCUSDT"], StrategyConfig(), signals_file=f)
    assert sc.last_alert["BTCUSDT"] == (pd.Timestamp("2025-01-01T00:05:00+00:00"), 1)
