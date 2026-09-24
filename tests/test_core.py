import numpy as np
import pandas as pd
import pytest

from accusignals import indicators as ind
from accusignals import orderflow as of
from accusignals import structure as st
from accusignals.backtest import metrics, run_backtest, simulate_portfolio, simulate_symbol, trades_frame
from accusignals.data import BinanceClient, klines_to_frame, synthetic_ohlcv
from accusignals.optimize import walk_forward
from accusignals.risk import RiskConfig, position_size
from accusignals.scanner import tick_for
from accusignals.strategy import StrategyConfig, generate_signals


@pytest.fixture(scope="module")
def df():
    return synthetic_ohlcv(6000, "5m", seed=3)


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


def test_klines_parsing_and_top_symbols():
    row = [1700000000000, "1", "2", "0.5", "1.5", "10", 1700000299999, "15", 5, "6", "9", "0"]
    f = klines_to_frame([row])
    assert f["taker_buy_volume"].iloc[0] == 6.0 and f.index[0].year == 2023

    class Resp:
        status_code, headers = 200, {}
        def raise_for_status(self): pass
        def json(self):
            return [{"symbol": "BTCUSDT", "quoteVolume": "9e9"}, {"symbol": "USDCUSDT", "quoteVolume": "8e9"},
                    {"symbol": "ETHUSDT", "quoteVolume": "5e9"}, {"symbol": "ETHBTC", "quoteVolume": "9e9"},
                    {"symbol": "TINYUSDT", "quoteVolume": "1e3"}]

    class Sess:
        def get(self, *a, **k): return Resp()

    assert BinanceClient("futures", session=Sess()).top_symbols(5) == ["BTCUSDT", "ETHUSDT"]


def test_tick_for():
    assert tick_for(65000) == pytest.approx(10.0)
    assert tick_for(0.5) == pytest.approx(0.0001)


def test_random_walk_has_no_edge():
    """On data with no edge, a correct backtester must not show one."""
    data = {f"S{i}": synthetic_ohlcv(12000, "5m", seed=10 + i) for i in range(2)}
    m, taken, curve = run_backtest(data)
    assert m["trades"] > 50
    assert m["avg_r"] < 0.15


def test_walk_forward_runs():
    data = {"S0": synthetic_ohlcv(12000, "5m", seed=21)}
    grid = {"min_score": [5.0, 6.0], "tp2_r": [2.0]}
    m, folds, taken, curve = walk_forward(data, grid=grid, train_days=14, test_days=7, min_trades=5, verbose=False)
    assert len(folds) >= 3
    assert (taken["entry_time"] >= folds["test_start"].min()).all()
