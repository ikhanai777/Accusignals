"""Shared fixtures.

Strategy-level tests run on REAL Binance candles: they are downloaded once
into tests/.cache (or read from ACCUSIGNALS_TEST_DATA, a CSV written by
``python -m accusignals download``). If Binance is unreachable those tests are
skipped, never faked.
"""
import os
from pathlib import Path

import pytest
import requests

from accusignals.data import BinanceClient, load_csv, save_csv

CACHE = Path(__file__).parent / ".cache" / "BTCUSDT_5m_futures.csv"


@pytest.fixture(scope="session")
def binance_5m():
    env = os.getenv("ACCUSIGNALS_TEST_DATA")
    if env:
        return load_csv(env)
    if CACHE.exists():
        return load_csv(CACHE)
    try:
        client = BinanceClient("futures", max_retries=1)
        client.sync_time()
        df = client.history("BTCUSDT", "5m", 45).drop(columns=["close_time"])
    except requests.RequestException as exc:
        pytest.skip(f"Binance API unreachable ({exc}); real-data tests skipped")
    save_csv(df.iloc[:-1], CACHE)  # drop the still-forming candle
    return load_csv(CACHE)
