"""Backtest replay engine — feeds historical events through the real strategy pipeline."""

from __future__ import annotations

import time
from decimal import Decimal

import structlog

from src.backtest.sim_client import SimulatedClient
from src.backtest.types import BacktestConfig, BacktestResult, Scenario
from src.core.types import ExecutionResult, FeedEvent
from src.monitor.metrics import MetricsCollector
from src.polymarket.scanner import MarketScanner
from src.risk.monitor import RiskMonitor
from src.strategy.engine import ArbEngine

logger = structlog.get_logger(__name__)


class BacktestEngine:
    """Replays a historical scenario through the real strategy pipeline.

    Wires a :class:`SimulatedClient` in place of the live Polymarket
    client so that orders are filled from recorded orderbook snapshots.

    Usage::

        engine = BacktestEngine(scenario, config)
        result = await engine.run()
        print(result.cumulative_pnl)
        print(engine.collector.summary())
    """

    def __init__(
        self,
        scenario: Scenario,
        config: BacktestConfig | None = None,
    ) -> None:
        self._scenario = scenario
        self._config = config or BacktestConfig()

        # Build simulated client
        self._sim_client = SimulatedClient(
            fill_probability=self._config.fill_probability,
            slippage_bps=self._config.slippage_bps,
        )

        # Build scanner (we inject opportunities directly)
        self._scanner = MarketScanner(
            client=self._sim_client,  # type: ignore[arg-type]
            config=self._config.strategy.prioritizer
            and None,  # use defaults
        )

        # Build risk monitor
        self._risk_monitor = RiskMonitor(config=self._config.risk)

        # Build engine with real strategy components
        self._engine = ArbEngine(
            client=self._sim_client,  # type: ignore[arg-type]
            scanner=self._scanner,
            config=self._config.strategy,
            risk_config=self._config.risk,
            risk_monitor=self._risk_monitor,
        )

        # Metrics collector for dashboard output
        self._collector = MetricsCollector()
        self._engine.on_event(self._collector.on_arb_event)

    @property
    def collector(self) -> MetricsCollector:
        """Access the metrics collector for post-run analysis."""
        return self._collector

    @property
    def sim_client(self) -> SimulatedClient:
        """Access the simulated client for fill analysis."""
        return self._sim_client

    @property
    def risk_snapshot(self) -> dict[str, object]:
        """Current risk monitor state."""
        return self._risk_monitor.snapshot()

    async def run(self) -> BacktestResult:
        """Replay all events in the scenario and return aggregated results.

        Steps for each event:
        1. Update the simulated client with the event's orderbook snapshot.
        2. Inject scenario opportunities into the scanner.
        3. Feed the event through ``ArbEngine.process_event()``.
        4. Collect execution results.
        """
        await self._engine.start()

        # Pre-load scenario opportunities into the scanner
        self._scanner._opportunities = dict(self._scenario.opportunities)

        all_results: list[ExecutionResult] = []

        for hist_event in self._scenario.events:
            # Update the simulated clock and orderbooks
            ts = hist_event.feed_event.released_at or hist_event.feed_event.received_at
            self._sim_client.set_time(ts)
            if hist_event.orderbooks:
                self._sim_client.set_orderbooks(hist_event.orderbooks)

                # Also update opportunity prices from the orderbook snapshot
                for token_id, book in hist_event.orderbooks.items():
                    for opp in self._scanner._opportunities.values():
                        if opp.token_id == token_id:
                            opp.best_bid = book.best_bid
                            opp.best_ask = book.best_ask
                            opp.spread = book.spread
                            opp.depth_usd = book.depth_usd

            # Rebase timestamps to wall-clock so the signal generator's
            # staleness check (which uses time.time()) doesn't reject events.
            rebased = _rebase_event(hist_event.feed_event)

            # Feed through the engine
            results = await self._engine.process_event(rebased)
            all_results.extend(results)

        await self._engine.stop()

        # Build result summary
        summary = self._collector.summary()
        return BacktestResult(
            scenario_name=self._scenario.name,
            total_events=len(self._scenario.events),
            total_trades=summary["total_trades"],  # type: ignore[arg-type]
            successful_trades=summary["successful_trades"],  # type: ignore[arg-type]
            failed_trades=summary["failed_trades"],  # type: ignore[arg-type]
            signals_generated=summary["signals_generated"],  # type: ignore[arg-type]
            trades_skipped=summary["trades_skipped"],  # type: ignore[arg-type]
            risk_rejected=summary["risk_rejected"],  # type: ignore[arg-type]
            cumulative_pnl=summary["cumulative_pnl"],  # type: ignore[arg-type]
            win_rate=summary["win_rate"],  # type: ignore[arg-type]
            execution_results=all_results,
        )


def _rebase_event(event: FeedEvent) -> FeedEvent:
    """Copy a FeedEvent with timestamps shifted to the present.

    The signal generator compares ``time.time() - event.received_at``
    against ``max_staleness_secs``.  Historical events would always
    appear stale, so we shift ``received_at`` (and ``released_at``)
    to be relative to *now* while preserving the original delta
    between them.
    """
    now = time.time()
    delta = event.received_at - event.released_at if event.released_at else 0.0
    return event.model_copy(update={
        "received_at": now,
        "released_at": now - delta,
    })
