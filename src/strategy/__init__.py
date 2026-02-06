"""Strategy module — arbitrage engine, matching, signals, sizing, prioritization."""

from src.strategy.engine import ArbEngine, ArbEventCallback
from src.strategy.exceptions import (
    ExecutionError,
    MatchError,
    PrioritizationError,
    SignalError,
    SizingError,
    StrategyError,
)
from src.strategy.matcher import MarketMatcher
from src.strategy.prioritizer import OpportunityPrioritizer, compute_priority_score
from src.strategy.signals import SignalGenerator
from src.strategy.sizer import PositionSizer

__all__ = [
    "ArbEngine",
    "ArbEventCallback",
    "ExecutionError",
    "MarketMatcher",
    "MatchError",
    "OpportunityPrioritizer",
    "PositionSizer",
    "PrioritizationError",
    "SignalError",
    "SignalGenerator",
    "SizingError",
    "StrategyError",
    "compute_priority_score",
]
