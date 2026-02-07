"""Tests for OracleMonitor — proposal tracking, disputes, whale alerts."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.config import OracleConfig
from src.core.types import (
    OracleAlert,
    OracleEventType,
    OracleProposal,
    OracleProposalState,
    Position,
    Side,
    WhaleActivity,
)
from src.risk.oracle_monitor import OracleMonitor
from src.risk.positions import PositionTracker

# ── Helpers ─────────────────────────────────────────────────────


def _monitor(
    whale_addresses: list[str] | None = None,
    positions: PositionTracker | None = None,
) -> OracleMonitor:
    cfg = OracleConfig(
        enabled=True,
        whale_addresses=whale_addresses or [],
    )
    return OracleMonitor(config=cfg, positions=positions)


def _tracker_with_position(
    condition_id: str = "cond1",
    token_id: str = "0xyes",
    price: Decimal = Decimal("0.50"),
    size: Decimal = Decimal("200"),
) -> PositionTracker:
    tracker = PositionTracker()
    tracker._positions[token_id] = Position(
        token_id=token_id,
        condition_id=condition_id,
        side=Side.BUY,
        entry_price=price,
        size=size,
    )
    return tracker


def _proposal(
    condition_id: str = "cond1",
    state: OracleProposalState = OracleProposalState.PROPOSED,
) -> OracleProposal:
    return OracleProposal(
        condition_id=condition_id,
        proposal_hash="0xabc",
        proposer="0xproposer",
        proposed_outcome="YES",
        state=state,
        proposed_at=1000.0,
    )


# ── Init ──────────────────────────────────────────────────────


class TestOracleMonitorInit:
    def test_default_state_empty(self) -> None:
        mon = _monitor()
        assert mon.proposals == {}
        assert mon.disputed_conditions == set()
        assert mon.exposure_at_risk() == Decimal("0")

    def test_whale_addresses_lowercased(self) -> None:
        mon = _monitor(whale_addresses=["0xABC", "0xDEF"])
        assert mon.whale_addresses == {"0xabc", "0xdef"}


# ── Ingest Proposal ──────────────────────────────────────────


class TestIngestProposal:
    @pytest.mark.asyncio
    async def test_stores_proposal(self) -> None:
        mon = _monitor()
        await mon.ingest_proposal(_proposal("cond1"))
        assert "cond1" in mon.proposals
        assert mon.proposals["cond1"].proposer == "0xproposer"

    @pytest.mark.asyncio
    async def test_updates_existing(self) -> None:
        mon = _monitor()
        await mon.ingest_proposal(_proposal("cond1"))
        updated = _proposal("cond1", state=OracleProposalState.DISPUTED)
        await mon.ingest_proposal(updated)
        assert mon.proposals["cond1"].state == OracleProposalState.DISPUTED

    @pytest.mark.asyncio
    async def test_emits_alert_if_position_held(self) -> None:
        tracker = _tracker_with_position("cond1")
        mon = _monitor(positions=tracker)
        alerts: list[OracleAlert] = []
        mon.on_alert(lambda a: alerts.append(a))
        await mon.ingest_proposal(_proposal("cond1"))
        assert len(alerts) == 1
        assert alerts[0].event_type == OracleEventType.PROPOSAL_DETECTED
        assert alerts[0].held_position_exposure > 0

    @pytest.mark.asyncio
    async def test_no_alert_without_position(self) -> None:
        mon = _monitor()
        alerts: list[OracleAlert] = []
        mon.on_alert(lambda a: alerts.append(a))
        await mon.ingest_proposal(_proposal("cond1"))
        assert len(alerts) == 0


# ── Ingest Dispute ────────────────────────────────────────────


class TestIngestDispute:
    @pytest.mark.asyncio
    async def test_marks_state_disputed(self) -> None:
        mon = _monitor()
        await mon.ingest_proposal(_proposal("cond1"))
        await mon.ingest_dispute("cond1", "0xdisputer", 2000.0)
        assert mon.proposals["cond1"].state == OracleProposalState.DISPUTED

    @pytest.mark.asyncio
    async def test_creates_minimal_proposal_if_unknown(self) -> None:
        mon = _monitor()
        await mon.ingest_dispute("cond_new", "0xdisputer", 2000.0)
        assert "cond_new" in mon.proposals
        assert mon.proposals["cond_new"].state == OracleProposalState.DISPUTED

    @pytest.mark.asyncio
    async def test_is_disputed_returns_true(self) -> None:
        mon = _monitor()
        await mon.ingest_dispute("cond1", "0xdisputer")
        assert mon.is_disputed("cond1") is True

    @pytest.mark.asyncio
    async def test_is_disputed_returns_false_when_undisputed(self) -> None:
        mon = _monitor()
        await mon.ingest_proposal(_proposal("cond1"))
        assert mon.is_disputed("cond1") is False

    @pytest.mark.asyncio
    async def test_alert_with_exposure_when_position_held(self) -> None:
        tracker = _tracker_with_position("cond1")
        mon = _monitor(positions=tracker)
        alerts: list[OracleAlert] = []
        mon.on_alert(lambda a: alerts.append(a))
        await mon.ingest_dispute("cond1", "0xdisputer", 2000.0)
        assert len(alerts) == 1
        assert alerts[0].event_type == OracleEventType.DISPUTE_DETECTED
        assert alerts[0].held_position_exposure == Decimal("100")  # 0.50*200


# ── Ingest Settlement ─────────────────────────────────────────


class TestIngestSettlement:
    @pytest.mark.asyncio
    async def test_marks_state_settled(self) -> None:
        mon = _monitor()
        await mon.ingest_proposal(
            _proposal("cond1", state=OracleProposalState.DISPUTED),
        )
        await mon.ingest_settlement("cond1", "YES", 3000.0)
        assert mon.proposals["cond1"].state == OracleProposalState.SETTLED

    @pytest.mark.asyncio
    async def test_clears_dispute(self) -> None:
        mon = _monitor()
        await mon.ingest_dispute("cond1", "0xdisputer")
        assert mon.is_disputed("cond1") is True
        await mon.ingest_settlement("cond1", "YES")
        assert mon.is_disputed("cond1") is False

    @pytest.mark.asyncio
    async def test_emits_settlement_alert(self) -> None:
        mon = _monitor()
        alerts: list[OracleAlert] = []
        mon.on_alert(lambda a: alerts.append(a))
        await mon.ingest_settlement("cond1", "YES", 3000.0)
        assert len(alerts) == 1
        assert alerts[0].event_type == OracleEventType.SETTLEMENT_DETECTED


# ── Ingest Whale Activity ────────────────────────────────────


class TestIngestWhaleActivity:
    @pytest.mark.asyncio
    async def test_known_whale_emits_alert(self) -> None:
        mon = _monitor(whale_addresses=["0xWhale1"])
        alerts: list[OracleAlert] = []
        mon.on_alert(lambda a: alerts.append(a))
        await mon.ingest_whale_activity(WhaleActivity(
            address="0xwhale1",
            action="DISPUTE",
            condition_id="cond1",
            timestamp=1000.0,
        ))
        assert len(alerts) == 1
        assert alerts[0].event_type == OracleEventType.WHALE_ACTIVITY_DETECTED

    @pytest.mark.asyncio
    async def test_unknown_whale_no_alert(self) -> None:
        mon = _monitor(whale_addresses=["0xWhale1"])
        alerts: list[OracleAlert] = []
        mon.on_alert(lambda a: alerts.append(a))
        await mon.ingest_whale_activity(WhaleActivity(
            address="0xUnknown",
            action="VOTE",
        ))
        assert len(alerts) == 0

    @pytest.mark.asyncio
    async def test_case_insensitive_address_matching(self) -> None:
        mon = _monitor(whale_addresses=["0xABCDEF"])
        alerts: list[OracleAlert] = []
        mon.on_alert(lambda a: alerts.append(a))
        await mon.ingest_whale_activity(WhaleActivity(
            address="0xabcdef",
            action="BOND",
        ))
        assert len(alerts) == 1


# ── Query Methods ─────────────────────────────────────────────


class TestQueryMethods:
    @pytest.mark.asyncio
    async def test_exposure_at_risk_with_disputes(self) -> None:
        tracker = _tracker_with_position("cond1")
        mon = _monitor(positions=tracker)
        await mon.ingest_dispute("cond1", "0xdisputer")
        # Position: 0.50 * 200 = 100
        assert mon.exposure_at_risk() == Decimal("100")

    @pytest.mark.asyncio
    async def test_no_disputes_returns_zero(self) -> None:
        tracker = _tracker_with_position("cond1")
        mon = _monitor(positions=tracker)
        assert mon.exposure_at_risk() == Decimal("0")

    @pytest.mark.asyncio
    async def test_disputed_positions_returns_tuples(self) -> None:
        tracker = _tracker_with_position("cond1")
        mon = _monitor(positions=tracker)
        await mon.ingest_dispute("cond1", "0xdisputer")
        results = mon.disputed_positions()
        assert len(results) == 1
        cid, pos, proposal = results[0]
        assert cid == "cond1"
        assert pos.token_id == "0xyes"
        assert proposal.state == OracleProposalState.DISPUTED


# ── Callbacks and State ───────────────────────────────────────


class TestCallbacksAndState:
    @pytest.mark.asyncio
    async def test_async_callback_support(self) -> None:
        mon = _monitor()
        alerts: list[OracleAlert] = []

        async def async_cb(a: OracleAlert) -> None:
            alerts.append(a)

        mon.on_alert(async_cb)
        await mon.ingest_settlement("cond1", "YES")
        assert len(alerts) == 1

    @pytest.mark.asyncio
    async def test_clear_resets_state(self) -> None:
        mon = _monitor()
        await mon.ingest_proposal(_proposal("cond1"))
        await mon.ingest_dispute("cond2", "0xdisputer")
        mon.clear()
        assert mon.proposals == {}
        assert mon.disputed_conditions == set()

    def test_snapshot_has_key_fields(self) -> None:
        mon = _monitor()
        snap = mon.snapshot()
        assert "tracked_proposals" in snap
        assert "disputed_markets" in snap
        assert "exposure_at_risk_usd" in snap
        assert "whale_addresses_count" in snap
