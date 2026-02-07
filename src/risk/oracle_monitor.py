"""OracleMonitor — tracks UMA oracle proposals, disputes, and whale activity.

Provides both reactive ingest methods and an active polling loop that queries
the UMA Optimistic Oracle subgraph for proposals/disputes on tracked markets.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal

import structlog

from src.core.config import OracleConfig
from src.core.types import (
    OracleAlert,
    OracleEventType,
    OracleProposal,
    OracleProposalState,
    OracleRiskAssessment,
    Position,
    WhaleActivity,
)
from src.risk.positions import PositionTracker

logger = structlog.stdlib.get_logger()

OracleAlertCallback = Callable[[OracleAlert], Awaitable[None] | None]

# GraphQL query for UMA Optimistic Oracle proposals by condition IDs
_PROPOSALS_QUERY = """
query OracleProposals($conditionIds: [String!]!) {
  optimisticPriceRequests(
    where: { ancillaryData_contains_nocase_in: $conditionIds }
    orderBy: requestTimestamp
    orderDirection: desc
    first: 100
  ) {
    id
    proposer
    proposedPrice
    requestTimestamp
    disputeTimestamp
    settleTimestamp
    disputer
    settlementPrice
    settled
    ancillaryData
    bond
  }
}
"""


class OracleMonitor:
    """Monitors UMA oracle proposals and disputes for active markets.

    Supports two modes of operation:

    1. **Reactive (ingest)** — Accept data via ``ingest_proposal``,
       ``ingest_dispute``, etc. for unit testing or external feed integration.

    2. **Active (polling)** — Call ``start()`` to run a background loop that
       queries the UMA subgraph for proposals on tracked condition IDs.

    Usage::

        monitor = OracleMonitor(config, positions)
        monitor.on_alert(my_callback)

        # Track markets the bot is watching
        monitor.track_condition("0xcond1")
        monitor.track_condition("0xcond2")

        # Start active polling
        await monitor.start()

        # Or use reactive ingest
        await monitor.ingest_proposal(proposal)
        await monitor.ingest_dispute("cond1", "0xdisputer", time.time())

        # Assess oracle risk on held positions
        assessments = monitor.assess_oracle_risk()

        await monitor.stop()
    """

    def __init__(
        self,
        config: OracleConfig | None = None,
        positions: PositionTracker | None = None,
        http_session: object | None = None,
    ) -> None:
        self._config = config or OracleConfig()
        self._positions = positions
        self._proposals: dict[str, OracleProposal] = {}
        self._callbacks: list[OracleAlertCallback] = []
        self._whale_addresses: set[str] = {
            addr.lower() for addr in self._config.whale_addresses
        }

        # Active polling state
        self._tracked_conditions: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._http_session = http_session
        self._poll_count = 0
        self._last_poll_at: float = 0.0

    # ── Properties ───────────────────────────────────────────────

    @property
    def proposals(self) -> dict[str, OracleProposal]:
        """Read-only copy of tracked proposals."""
        return dict(self._proposals)

    @property
    def disputed_conditions(self) -> set[str]:
        """Condition IDs with active disputes."""
        return {
            cid
            for cid, p in self._proposals.items()
            if p.state == OracleProposalState.DISPUTED
        }

    @property
    def whale_addresses(self) -> set[str]:
        """Configured whale addresses (lowercased)."""
        return set(self._whale_addresses)

    @property
    def tracked_conditions(self) -> set[str]:
        """Currently tracked condition IDs."""
        return set(self._tracked_conditions)

    @property
    def running(self) -> bool:
        """Whether the polling loop is active."""
        return self._running

    @property
    def poll_count(self) -> int:
        """Number of completed poll cycles."""
        return self._poll_count

    # ── Condition Tracking ───────────────────────────────────────

    def track_condition(self, condition_id: str) -> None:
        """Register a condition ID for active monitoring."""
        self._tracked_conditions.add(condition_id)
        logger.debug("oracle_condition_tracked", condition_id=condition_id)

    def untrack_condition(self, condition_id: str) -> None:
        """Remove a condition ID from active monitoring."""
        self._tracked_conditions.discard(condition_id)
        logger.debug("oracle_condition_untracked", condition_id=condition_id)

    def sync_tracked_from_positions(self) -> None:
        """Sync tracked conditions from the position tracker.

        Adds all condition IDs with open positions to the tracking set.
        """
        if self._positions is None:
            return
        for pos in self._positions.positions.values():
            if pos.condition_id:
                self._tracked_conditions.add(pos.condition_id)

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "oracle_monitor_started",
            poll_interval=self._config.poll_interval_secs,
            tracked_conditions=len(self._tracked_conditions),
        )

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info(
            "oracle_monitor_stopped",
            poll_count=self._poll_count,
        )

    async def _poll_loop(self) -> None:
        """Background loop that calls poll_once() on an interval."""
        while self._running:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("oracle_poll_error")

            try:
                await asyncio.sleep(self._config.poll_interval_secs)
            except asyncio.CancelledError:
                break

    async def poll_once(self) -> list[OracleProposal]:
        """Run a single poll cycle against the UMA subgraph.

        Fetches proposals for all tracked conditions, detects new proposals
        and state changes (disputes, settlements), and emits alerts.

        Returns the list of proposals found.
        """
        self.sync_tracked_from_positions()

        if not self._tracked_conditions:
            self._poll_count += 1
            self._last_poll_at = time.time()
            return []

        raw_proposals = await self._fetch_subgraph_proposals(
            list(self._tracked_conditions),
        )

        for raw in raw_proposals:
            await self._process_subgraph_proposal(raw)

        # Check oracle risk on held positions after each poll
        assessments = self.assess_oracle_risk()
        for assessment in assessments:
            if assessment.oracle_risk_premium <= Decimal(
                str(self._config.hedge_risk_threshold)
            ):
                await self._emit(OracleAlert(
                    event_type=OracleEventType.HIGH_ORACLE_RISK,
                    condition_id=assessment.condition_id,
                    held_position_exposure=assessment.exposure_usd,
                    reason=(
                        f"High oracle risk on {assessment.condition_id}: "
                        f"price ${assessment.current_price}, "
                        f"risk premium {assessment.oracle_risk_premium:.4f} "
                        f"<= {self._config.hedge_risk_threshold:.4f} threshold. "
                        f"{assessment.recommendation}"
                    ),
                    timestamp=time.time(),
                ))

        self._poll_count += 1
        self._last_poll_at = time.time()
        return list(self._proposals.values())

    async def _fetch_subgraph_proposals(
        self, condition_ids: list[str],
    ) -> list[dict[str, object]]:
        """Query the UMA Optimistic Oracle subgraph for proposals.

        Returns raw proposal dicts from the subgraph response.
        Falls back gracefully if the HTTP session is unavailable.
        """
        if self._http_session is None:
            return []

        try:
            import aiohttp

            session: aiohttp.ClientSession = self._http_session  # type: ignore[assignment]
            payload = {
                "query": _PROPOSALS_QUERY,
                "variables": {"conditionIds": condition_ids},
            }
            async with session.post(
                self._config.subgraph_url, json=payload,
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "oracle_subgraph_http_error",
                        status=resp.status,
                    )
                    return []
                data = await resp.json()
                requests = (
                    data.get("data", {}).get("optimisticPriceRequests", [])
                )
                return requests  # type: ignore[no-any-return]
        except Exception:
            logger.exception("oracle_subgraph_fetch_error")
            return []

    async def _process_subgraph_proposal(
        self, raw: dict[str, object],
    ) -> None:
        """Process a single raw proposal from the subgraph response.

        Detects state transitions (new proposal, dispute, settlement) and
        triggers the appropriate ingest methods.
        """
        proposal_id = str(raw.get("id", ""))
        ancillary = str(raw.get("ancillaryData", ""))
        proposer = str(raw.get("proposer", ""))
        disputer = str(raw.get("disputer", ""))
        proposed_price = str(raw.get("proposedPrice", ""))
        request_ts = float(raw.get("requestTimestamp", 0))
        dispute_ts = float(raw.get("disputeTimestamp", 0))
        settle_ts = float(raw.get("settleTimestamp", 0))
        settled = bool(raw.get("settled", False))
        bond_str = str(raw.get("bond", "0"))

        # Match ancillary data to a tracked condition
        condition_id = self._match_condition(ancillary)
        if not condition_id:
            return

        existing = self._proposals.get(condition_id)

        # Determine state
        if settled and settle_ts > 0:
            if existing is None or existing.state != OracleProposalState.SETTLED:
                settlement_price = str(raw.get("settlementPrice", ""))
                outcome = "YES" if settlement_price == "1" else "NO"
                await self.ingest_settlement(condition_id, outcome, settle_ts)
        elif disputer and dispute_ts > 0:
            if existing is None or existing.state != OracleProposalState.DISPUTED:
                await self.ingest_dispute(condition_id, disputer, dispute_ts)
                # Check if disputer is a whale
                if disputer.lower() in self._whale_addresses:
                    await self.ingest_whale_activity(WhaleActivity(
                        address=disputer,
                        condition_id=condition_id,
                        action="DISPUTE",
                        timestamp=dispute_ts,
                    ))
        else:
            if existing is None:
                try:
                    bond = Decimal(bond_str)
                except Exception:
                    bond = Decimal("0")
                proposal = OracleProposal(
                    condition_id=condition_id,
                    proposal_hash=proposal_id,
                    proposer=proposer,
                    proposed_outcome=proposed_price,
                    state=OracleProposalState.PROPOSED,
                    proposed_at=request_ts,
                    bond_amount=bond,
                    metadata={"raw": raw},
                )
                await self.ingest_proposal(proposal)

    def _match_condition(self, ancillary_data: str) -> str:
        """Match subgraph ancillary data to a tracked condition ID."""
        ancillary_lower = ancillary_data.lower()
        for cid in self._tracked_conditions:
            if cid.lower() in ancillary_lower:
                return cid
        return ""

    # ── Oracle Risk Assessment ───────────────────────────────────

    def assess_oracle_risk(self) -> list[OracleRiskAssessment]:
        """Assess oracle risk for all held positions.

        For each position, computes the oracle risk premium — the gap
        between the current price and $1.00 payout. A position at $0.98
        has a 2% oracle risk premium: the market is pricing in 2% chance
        of incorrect resolution.

        Returns assessments only for positions exceeding the configured
        oracle_risk_price_threshold (positions priced near $1.00 where
        oracle risk is the dominant remaining risk).
        """
        if self._positions is None:
            return []

        threshold = Decimal(str(self._config.oracle_risk_price_threshold))
        assessments: list[OracleRiskAssessment] = []

        for pos in self._positions.positions.values():
            current_price = pos.entry_price
            if current_price < threshold:
                continue

            oracle_risk_premium = Decimal("1.00") - current_price
            exposure = current_price * pos.size
            has_dispute = self.is_disputed(pos.condition_id)

            if has_dispute:
                recommendation = (
                    f"HEDGE: Position has active dispute. "
                    f"Consider selling {pos.size} shares to reduce exposure."
                )
            elif oracle_risk_premium <= Decimal(
                str(self._config.hedge_risk_threshold)
            ):
                recommendation = (
                    f"MONITOR: Oracle risk premium "
                    f"({oracle_risk_premium:.4f}) is below "
                    f"threshold ({self._config.hedge_risk_threshold}). "
                    f"Risk/reward may not justify position."
                )
            else:
                recommendation = "OK: Oracle risk within acceptable range."

            assessments.append(OracleRiskAssessment(
                token_id=pos.token_id,
                condition_id=pos.condition_id,
                entry_price=pos.entry_price,
                current_price=current_price,
                size=pos.size,
                oracle_risk_premium=oracle_risk_premium,
                exposure_usd=exposure,
                has_active_dispute=has_dispute,
                recommendation=recommendation,
            ))

        return assessments

    # ── Query Methods ────────────────────────────────────────────

    def is_disputed(self, condition_id: str) -> bool:
        """Check if a condition has an active dispute."""
        proposal = self._proposals.get(condition_id)
        if proposal is None:
            return False
        return proposal.state == OracleProposalState.DISPUTED

    def get_proposal(self, condition_id: str) -> OracleProposal | None:
        """Get the proposal for a condition, if tracked."""
        return self._proposals.get(condition_id)

    def exposure_at_risk(self) -> Decimal:
        """Total exposure in disputed markets (cross-references positions)."""
        if self._positions is None:
            return Decimal("0")
        total = Decimal("0")
        for cid in self.disputed_conditions:
            total += self._positions.exposure_for_condition(cid)
        return total

    def disputed_positions(
        self,
    ) -> list[tuple[str, Position, OracleProposal]]:
        """Return (condition_id, position, proposal) for disputed positions."""
        if self._positions is None:
            return []
        result: list[tuple[str, Position, OracleProposal]] = []
        for cid in self.disputed_conditions:
            proposal = self._proposals[cid]
            for pos in self._positions.positions.values():
                if pos.condition_id == cid:
                    result.append((cid, pos, proposal))
        return result

    # ── Callbacks ────────────────────────────────────────────────

    def on_alert(self, callback: OracleAlertCallback) -> None:
        """Register a callback for oracle alerts."""
        self._callbacks.append(callback)

    async def _emit(self, alert: OracleAlert) -> None:
        """Dispatch an oracle alert to all registered callbacks."""
        for cb in self._callbacks:
            try:
                result = cb(alert)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "oracle_alert_callback_error",
                    event_type=alert.event_type,
                )

    # ── Ingest Methods ───────────────────────────────────────────

    async def ingest_proposal(self, proposal: OracleProposal) -> None:
        """Track a new or updated proposal."""
        cid = proposal.condition_id
        self._proposals[cid] = proposal
        logger.info(
            "oracle_proposal_ingested",
            condition_id=cid,
            state=proposal.state.value,
        )

        exposure = self._get_held_exposure(cid)
        if exposure > 0:
            await self._emit(OracleAlert(
                event_type=OracleEventType.PROPOSAL_DETECTED,
                condition_id=cid,
                proposal=proposal,
                held_position_exposure=exposure,
                reason=f"Proposal detected on held position (${exposure})",
                timestamp=time.time(),
            ))

    async def ingest_dispute(
        self,
        condition_id: str,
        disputer: str = "",
        timestamp: float = 0.0,
    ) -> None:
        """Mark a condition as disputed."""
        ts = timestamp or time.time()
        proposal = self._proposals.get(condition_id)
        if proposal is None:
            proposal = OracleProposal(
                condition_id=condition_id,
                state=OracleProposalState.DISPUTED,
                disputed_at=ts,
                disputer=disputer,
            )
            self._proposals[condition_id] = proposal
        else:
            proposal.state = OracleProposalState.DISPUTED
            proposal.disputed_at = ts
            proposal.disputer = disputer

        logger.warning(
            "oracle_dispute_detected",
            condition_id=condition_id,
            disputer=disputer,
        )

        exposure = self._get_held_exposure(condition_id)
        await self._emit(OracleAlert(
            event_type=OracleEventType.DISPUTE_DETECTED,
            condition_id=condition_id,
            proposal=proposal,
            held_position_exposure=exposure,
            reason=(
                f"Dispute filed on {condition_id}"
                + (f" (${exposure} at risk)" if exposure > 0 else "")
            ),
            timestamp=ts,
        ))

    async def ingest_settlement(
        self,
        condition_id: str,
        outcome: str = "",
        timestamp: float = 0.0,
    ) -> None:
        """Mark a condition as settled."""
        ts = timestamp or time.time()
        proposal = self._proposals.get(condition_id)
        if proposal is None:
            proposal = OracleProposal(
                condition_id=condition_id,
                state=OracleProposalState.SETTLED,
                settled_at=ts,
                proposed_outcome=outcome,
            )
            self._proposals[condition_id] = proposal
        else:
            proposal.state = OracleProposalState.SETTLED
            proposal.settled_at = ts
            if outcome:
                proposal.proposed_outcome = outcome

        logger.info(
            "oracle_settlement_detected",
            condition_id=condition_id,
            outcome=outcome,
        )

        await self._emit(OracleAlert(
            event_type=OracleEventType.SETTLEMENT_DETECTED,
            condition_id=condition_id,
            proposal=proposal,
            reason=f"Settlement on {condition_id}: {outcome}",
            timestamp=ts,
        ))

    async def ingest_whale_activity(
        self, activity: WhaleActivity,
    ) -> None:
        """Process whale activity — alerts only if from a known whale."""
        if activity.address.lower() not in self._whale_addresses:
            return

        logger.warning(
            "oracle_whale_activity",
            address=activity.address,
            action=activity.action,
            condition_id=activity.condition_id,
        )

        exposure = self._get_held_exposure(activity.condition_id)
        await self._emit(OracleAlert(
            event_type=OracleEventType.WHALE_ACTIVITY_DETECTED,
            condition_id=activity.condition_id,
            whale_activity=activity,
            held_position_exposure=exposure,
            reason=(
                f"Whale {activity.address[:10]}... {activity.action}"
                f" on {activity.condition_id}"
            ),
            timestamp=activity.timestamp or time.time(),
        ))

    # ── State Management ─────────────────────────────────────────

    def clear(self) -> None:
        """Reset all tracked state."""
        self._proposals.clear()

    def snapshot(self) -> dict[str, object]:
        """Return a snapshot of oracle monitor state."""
        return {
            "tracked_proposals": len(self._proposals),
            "tracked_conditions": len(self._tracked_conditions),
            "disputed_markets": len(self.disputed_conditions),
            "exposure_at_risk_usd": float(self.exposure_at_risk()),
            "whale_addresses_count": len(self._whale_addresses),
            "running": self._running,
            "poll_count": self._poll_count,
            "last_poll_at": self._last_poll_at,
        }

    def _get_held_exposure(self, condition_id: str) -> Decimal:
        """Get exposure for a condition from position tracker."""
        if self._positions is None or not condition_id:
            return Decimal("0")
        return self._positions.exposure_for_condition(condition_id)
