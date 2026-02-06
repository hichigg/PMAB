"""Strategy module — arbitrage engine, matching, signals, sizing."""

from src.strategy.engine import ArbEngine, ArbEventCallback
from src.strategy.exceptions import (
    ExecutionError,
    MatchError,
    SignalError,
    SizingError,
    StrategyError,
)
from src.strategy.matcher import MarketMatcher
from src.strategy.signals import SignalGenerator
from src.strategy.sizer import PositionSizer

__all__ = [
    "ArbEngine",
    "ArbEventCallback",
    "ExecutionError",
    "MarketMatcher",
    "MatchError",
    "PositionSizer",
    "SignalError",
    "SignalGenerator",
    "SizingError",
    "StrategyError",
]
