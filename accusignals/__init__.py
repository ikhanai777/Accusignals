"""Accusignals: confluence-based scalping signals for Binance."""
from .backtest import run_backtest
from .risk import RiskConfig
from .strategy import StrategyConfig, compute_features, generate_signals

__all__ = ["RiskConfig", "StrategyConfig", "compute_features", "generate_signals", "run_backtest"]
__version__ = "0.1.0"
