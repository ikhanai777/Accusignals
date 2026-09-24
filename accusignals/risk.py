"""Account-level risk rules.

Scalping P&L is dominated by risk management, not entries. The defaults are
deliberately conservative: a string of losses at 1% risk is survivable, the
same string at 5% is not.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 1.0  # % of equity lost if the stop is hit
    max_leverage: float = 10.0  # hard cap on position notional / equity
    max_concurrent: int = 3
    max_trades_per_day: int = 12
    daily_profit_target_pct: float = 5.0  # stop trading for the day once reached ("lock the win")
    daily_loss_limit_pct: float = 3.0  # stop trading for the day after this drawdown
    max_consecutive_losses: int = 4  # then pause for the rest of the day (tilt protection)
    fee_rate: float = 0.0004  # per side; Binance USDⓈ-M taker. Spot is 0.001 (0.00075 w/ BNB)
    slippage_bps: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)


def position_size(equity: float, entry: float, stop: float, cfg: RiskConfig) -> float:
    """Quantity such that hitting ``stop`` loses ``risk_per_trade_pct`` of
    equity, capped by ``max_leverage``."""
    dist = abs(entry - stop)
    if dist <= 0 or entry <= 0:
        return 0.0
    qty = equity * cfg.risk_per_trade_pct / 100 / dist
    return min(qty, equity * cfg.max_leverage / entry)
