"""OracleMonitor — tracks UMA oracle proposals, disputes, and whale activity."""

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
    Position,
    WhaleActivity,
)
from src.risk.positions import PositionTracker

logger = structlog.stdlib.get_logger()

OracleAlertCallback = Callable[[OracleAlert], Awaitable[None] | None]


class OracleMonitor:
    """Monitors UMA oracle proposals and disputes for active markets.

    Uses ingest methods to accept data from external sources (subgraph,
    RPC, etc.) so it can be unit-tested with synthetic data today and
    plugged into a real feed later.

    Usage::

        monitor = OracleMonitor(config, positions)
        monitor.on_alert(my_callback)

        await monitor.ingest_proposal(proposal)
        await monitor.ingest_dispute("cond1", "0xdisputer", time.time())

        if monitor.is_disputed("cond1"):
            ...  # handle disputed market
    """

    def __init__(
        self,
        config: OracleConfig | None = None,
        positions: PositionTracker | None = None,
    ) -> None:
        self._config = config or OracleConfig()
        self._positions = positions
        self._proposals: dict[str, OracleProposal] = {}
        self._callbacks: list[OracleAlertCallback] = []
        self._whale_addresses: set[str] = {
            addr.lower() for addr in self._config.whale_addresses
        }

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

    def clear(self) -> None:
        """Reset all tracked state."""
        self._proposals.clear()

    def snapshot(self) -> dict[str, object]:
        """Return a snapshot of oracle monitor state."""
        return {
            "tracked_proposals": len(self._proposals),
            "disputed_markets": len(self.disputed_conditions),
            "exposure_at_risk_usd": float(self.exposure_at_risk()),
            "whale_addresses_count": len(self._whale_addresses),
        }

    def _get_held_exposure(self, condition_id: str) -> Decimal:
        """Get exposure for a condition from position tracker."""
        if self._positions is None or not condition_id:
            return Decimal("0")
        return self._positions.exposure_for_condition(condition_id)
