"""Tests for OracleMonitor — proposal tracking, disputes, whale alerts,
active polling, condition tracking, and oracle risk assessment."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import OracleConfig
from src.core.types import (
    OracleAlert,
    OracleEventType,
    OracleProposal,
    OracleProposalState,
    OracleRiskAssessment,
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
    http_session: object | None = None,
    **config_overrides: object,
) -> OracleMonitor:
    cfg = OracleConfig(
        enabled=True,
        whale_addresses=whale_addresses or [],
        **config_overrides,  # type: ignore[arg-type]
    )
    return OracleMonitor(config=cfg, positions=positions, http_session=http_session)


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

    def test_initial_polling_state(self) -> None:
        mon = _monitor()
        assert mon.running is False
        assert mon.poll_count == 0
        assert mon.tracked_conditions == set()


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

    def test_snapshot_includes_new_fields(self) -> None:
        mon = _monitor()
        mon.track_condition("cond1")
        snap = mon.snapshot()
        assert snap["tracked_conditions"] == 1
        assert snap["running"] is False
        assert snap["poll_count"] == 0
        assert "last_poll_at" in snap


# ── Condition Tracking ───────────────────────────────────────


class TestConditionTracking:
    def test_track_condition(self) -> None:
        mon = _monitor()
        mon.track_condition("cond1")
        assert "cond1" in mon.tracked_conditions

    def test_track_multiple(self) -> None:
        mon = _monitor()
        mon.track_condition("cond1")
        mon.track_condition("cond2")
        assert mon.tracked_conditions == {"cond1", "cond2"}

    def test_track_duplicate_idempotent(self) -> None:
        mon = _monitor()
        mon.track_condition("cond1")
        mon.track_condition("cond1")
        assert len(mon.tracked_conditions) == 1

    def test_untrack_condition(self) -> None:
        mon = _monitor()
        mon.track_condition("cond1")
        mon.untrack_condition("cond1")
        assert "cond1" not in mon.tracked_conditions

    def test_untrack_nonexistent_safe(self) -> None:
        mon = _monitor()
        mon.untrack_condition("cond_nonexistent")
        assert mon.tracked_conditions == set()

    def test_sync_tracked_from_positions(self) -> None:
        tracker = PositionTracker()
        tracker._positions["0xyes"] = Position(
            token_id="0xyes",
            condition_id="cond1",
            side=Side.BUY,
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
        )
        tracker._positions["0xno"] = Position(
            token_id="0xno",
            condition_id="cond2",
            side=Side.BUY,
            entry_price=Decimal("0.40"),
            size=Decimal("50"),
        )
        mon = _monitor(positions=tracker)
        mon.sync_tracked_from_positions()
        assert mon.tracked_conditions == {"cond1", "cond2"}

    def test_sync_tracked_without_positions_safe(self) -> None:
        mon = _monitor()
        mon.sync_tracked_from_positions()
        assert mon.tracked_conditions == set()

    def test_sync_skips_empty_condition_id(self) -> None:
        tracker = PositionTracker()
        tracker._positions["0xyes"] = Position(
            token_id="0xyes",
            condition_id="",
            side=Side.BUY,
            entry_price=Decimal("0.50"),
            size=Decimal("100"),
        )
        mon = _monitor(positions=tracker)
        mon.sync_tracked_from_positions()
        assert mon.tracked_conditions == set()


# ── Polling Lifecycle ─────────────────────────────────────────


class TestPollingLifecycle:
    @pytest.mark.asyncio
    async def test_start_sets_running(self) -> None:
        mon = _monitor()
        await mon.start()
        assert mon.running is True
        await mon.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_running(self) -> None:
        mon = _monitor()
        await mon.start()
        await mon.stop()
        assert mon.running is False

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        mon = _monitor()
        await mon.start()
        await mon.start()  # second call should be safe
        assert mon.running is True
        await mon.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start_safe(self) -> None:
        mon = _monitor()
        await mon.stop()
        assert mon.running is False

    @pytest.mark.asyncio
    async def test_poll_once_increments_counter(self) -> None:
        mon = _monitor()
        assert mon.poll_count == 0
        await mon.poll_once()
        assert mon.poll_count == 1
        await mon.poll_once()
        assert mon.poll_count == 2

    @pytest.mark.asyncio
    async def test_poll_once_no_tracked_returns_empty(self) -> None:
        mon = _monitor()
        result = await mon.poll_once()
        assert result == []

    @pytest.mark.asyncio
    async def test_poll_once_no_session_returns_empty(self) -> None:
        mon = _monitor()
        mon.track_condition("cond1")
        result = await mon.poll_once()
        # No HTTP session → fetch returns [], but poll completes
        assert mon.poll_count == 1

    @pytest.mark.asyncio
    async def test_poll_updates_last_poll_at(self) -> None:
        mon = _monitor()
        assert mon._last_poll_at == 0.0
        await mon.poll_once()
        assert mon._last_poll_at > 0.0


# ── Subgraph Processing ─────────────────────────────────────


class TestSubgraphProcessing:
    @pytest.mark.asyncio
    async def test_process_new_proposal(self) -> None:
        mon = _monitor()
        mon.track_condition("cond1")
        raw = {
            "id": "proposal_1",
            "ancillaryData": "some data containing cond1 info",
            "proposer": "0xproposer",
            "proposedPrice": "1",
            "requestTimestamp": 1000,
            "disputeTimestamp": 0,
            "settleTimestamp": 0,
            "disputer": "",
            "settled": False,
            "bond": "1000000",
        }
        await mon._process_subgraph_proposal(raw)
        assert "cond1" in mon.proposals
        assert mon.proposals["cond1"].proposer == "0xproposer"
        assert mon.proposals["cond1"].state == OracleProposalState.PROPOSED

    @pytest.mark.asyncio
    async def test_process_dispute(self) -> None:
        mon = _monitor()
        mon.track_condition("cond1")
        # First ingest a proposal
        await mon.ingest_proposal(_proposal("cond1"))

        raw = {
            "id": "proposal_1",
            "ancillaryData": "cond1",
            "proposer": "0xproposer",
            "proposedPrice": "1",
            "requestTimestamp": 1000,
            "disputeTimestamp": 2000,
            "settleTimestamp": 0,
            "disputer": "0xdisputer",
            "settled": False,
            "bond": "1000000",
        }
        await mon._process_subgraph_proposal(raw)
        assert mon.is_disputed("cond1")
        assert mon.proposals["cond1"].disputer == "0xdisputer"

    @pytest.mark.asyncio
    async def test_process_settlement(self) -> None:
        mon = _monitor()
        mon.track_condition("cond1")
        await mon.ingest_proposal(_proposal("cond1"))

        raw = {
            "id": "proposal_1",
            "ancillaryData": "cond1",
            "proposer": "0xproposer",
            "proposedPrice": "1",
            "requestTimestamp": 1000,
            "disputeTimestamp": 0,
            "settleTimestamp": 3000,
            "settlementPrice": "1",
            "disputer": "",
            "settled": True,
            "bond": "1000000",
        }
        await mon._process_subgraph_proposal(raw)
        assert mon.proposals["cond1"].state == OracleProposalState.SETTLED

    @pytest.mark.asyncio
    async def test_process_unmatched_condition_ignored(self) -> None:
        mon = _monitor()
        mon.track_condition("cond1")
        raw = {
            "id": "proposal_1",
            "ancillaryData": "totally_different_data",
            "proposer": "0xproposer",
            "proposedPrice": "1",
            "requestTimestamp": 1000,
            "disputeTimestamp": 0,
            "settleTimestamp": 0,
            "disputer": "",
            "settled": False,
        }
        await mon._process_subgraph_proposal(raw)
        assert mon.proposals == {}

    @pytest.mark.asyncio
    async def test_process_dispute_by_whale_emits_whale_alert(self) -> None:
        mon = _monitor(whale_addresses=["0xWhaleDisputer"])
        mon.track_condition("cond1")
        alerts: list[OracleAlert] = []
        mon.on_alert(lambda a: alerts.append(a))

        raw = {
            "id": "proposal_1",
            "ancillaryData": "cond1",
            "proposer": "0xproposer",
            "proposedPrice": "1",
            "requestTimestamp": 1000,
            "disputeTimestamp": 2000,
            "settleTimestamp": 0,
            "disputer": "0xwhaledisputer",
            "settled": False,
        }
        await mon._process_subgraph_proposal(raw)

        # Should emit both DISPUTE_DETECTED and WHALE_ACTIVITY_DETECTED
        event_types = {a.event_type for a in alerts}
        assert OracleEventType.DISPUTE_DETECTED in event_types
        assert OracleEventType.WHALE_ACTIVITY_DETECTED in event_types

    @pytest.mark.asyncio
    async def test_process_already_disputed_no_duplicate(self) -> None:
        mon = _monitor()
        mon.track_condition("cond1")
        await mon.ingest_dispute("cond1", "0xfirst_disputer", 1500.0)

        alerts: list[OracleAlert] = []
        mon.on_alert(lambda a: alerts.append(a))

        raw = {
            "id": "proposal_1",
            "ancillaryData": "cond1",
            "proposer": "0xproposer",
            "proposedPrice": "1",
            "requestTimestamp": 1000,
            "disputeTimestamp": 2000,
            "settleTimestamp": 0,
            "disputer": "0xdisputer",
            "settled": False,
        }
        await mon._process_subgraph_proposal(raw)
        # Already disputed, should not emit another dispute alert
        dispute_alerts = [
            a for a in alerts if a.event_type == OracleEventType.DISPUTE_DETECTED
        ]
        assert len(dispute_alerts) == 0

    def test_match_condition_case_insensitive(self) -> None:
        mon = _monitor()
        mon.track_condition("CondABC")
        result = mon._match_condition("data with condabc inside")
        assert result == "CondABC"

    def test_match_condition_no_match(self) -> None:
        mon = _monitor()
        mon.track_condition("cond1")
        result = mon._match_condition("unrelated data")
        assert result == ""


# ── Subgraph HTTP Fetch ──────────────────────────────────────


class TestSubgraphFetch:
    @pytest.mark.asyncio
    async def test_fetch_no_session_returns_empty(self) -> None:
        mon = _monitor()
        result = await mon._fetch_subgraph_proposals(["cond1"])
        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_with_mock_session(self) -> None:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "data": {
                "optimisticPriceRequests": [
                    {
                        "id": "req_1",
                        "proposer": "0xabc",
                        "proposedPrice": "1",
                        "requestTimestamp": 1000,
                        "disputeTimestamp": 0,
                        "settleTimestamp": 0,
                        "disputer": "",
                        "settled": False,
                        "ancillaryData": "cond1",
                        "bond": "1000000",
                    },
                ],
            },
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)

        mon = _monitor(http_session=mock_session)
        result = await mon._fetch_subgraph_proposals(["cond1"])
        assert len(result) == 1
        assert result[0]["id"] == "req_1"

    @pytest.mark.asyncio
    async def test_fetch_http_error_returns_empty(self) -> None:
        mock_response = AsyncMock()
        mock_response.status = 500
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)

        mon = _monitor(http_session=mock_session)
        result = await mon._fetch_subgraph_proposals(["cond1"])
        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_exception_returns_empty(self) -> None:
        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=ConnectionError("timeout"))

        mon = _monitor(http_session=mock_session)
        result = await mon._fetch_subgraph_proposals(["cond1"])
        assert result == []


# ── Oracle Risk Assessment ───────────────────────────────────


class TestOracleRiskAssessment:
    def test_no_positions_returns_empty(self) -> None:
        mon = _monitor()
        assert mon.assess_oracle_risk() == []

    def test_low_price_position_skipped(self) -> None:
        """Positions below oracle_risk_price_threshold are not assessed."""
        tracker = _tracker_with_position(
            price=Decimal("0.50"), size=Decimal("100"),
        )
        mon = _monitor(
            positions=tracker,
            oracle_risk_price_threshold=0.95,
        )
        assessments = mon.assess_oracle_risk()
        assert len(assessments) == 0

    def test_high_price_position_assessed(self) -> None:
        """Positions at/above threshold get risk assessment."""
        tracker = _tracker_with_position(
            price=Decimal("0.98"), size=Decimal("100"),
        )
        mon = _monitor(
            positions=tracker,
            oracle_risk_price_threshold=0.95,
        )
        assessments = mon.assess_oracle_risk()
        assert len(assessments) == 1
        a = assessments[0]
        assert a.entry_price == Decimal("0.98")
        assert a.oracle_risk_premium == Decimal("0.02")
        assert a.exposure_usd == Decimal("98.00")

    def test_exact_threshold_included(self) -> None:
        """Position exactly at threshold IS assessed (not skipped)."""
        tracker = _tracker_with_position(
            price=Decimal("0.95"), size=Decimal("100"),
        )
        mon = _monitor(
            positions=tracker,
            oracle_risk_price_threshold=0.95,
        )
        assessments = mon.assess_oracle_risk()
        # Price 0.95 is NOT < 0.95, so it is assessed
        assert len(assessments) == 1
        assert assessments[0].oracle_risk_premium == Decimal("0.05")

    def test_risk_premium_calculation(self) -> None:
        tracker = _tracker_with_position(
            price=Decimal("0.97"), size=Decimal("500"),
        )
        mon = _monitor(
            positions=tracker,
            oracle_risk_price_threshold=0.90,
        )
        assessments = mon.assess_oracle_risk()
        assert len(assessments) == 1
        a = assessments[0]
        assert a.oracle_risk_premium == Decimal("0.03")
        assert a.exposure_usd == Decimal("485.00")

    def test_disputed_position_has_hedge_recommendation(self) -> None:
        tracker = _tracker_with_position(
            condition_id="cond1",
            price=Decimal("0.98"),
            size=Decimal("100"),
        )
        mon = _monitor(
            positions=tracker,
            oracle_risk_price_threshold=0.95,
        )
        # Manually add a dispute
        mon._proposals["cond1"] = OracleProposal(
            condition_id="cond1",
            state=OracleProposalState.DISPUTED,
        )
        assessments = mon.assess_oracle_risk()
        assert len(assessments) == 1
        a = assessments[0]
        assert a.has_active_dispute is True
        assert "HEDGE" in a.recommendation

    def test_undisputed_low_premium_has_monitor_recommendation(self) -> None:
        tracker = _tracker_with_position(
            condition_id="cond1",
            price=Decimal("0.99"),
            size=Decimal("100"),
        )
        mon = _monitor(
            positions=tracker,
            oracle_risk_price_threshold=0.95,
            hedge_risk_threshold=0.02,
        )
        assessments = mon.assess_oracle_risk()
        assert len(assessments) == 1
        a = assessments[0]
        assert a.oracle_risk_premium == Decimal("0.01")
        assert "MONITOR" in a.recommendation

    def test_undisputed_acceptable_premium_has_ok_recommendation(self) -> None:
        tracker = _tracker_with_position(
            condition_id="cond1",
            price=Decimal("0.96"),
            size=Decimal("100"),
        )
        mon = _monitor(
            positions=tracker,
            oracle_risk_price_threshold=0.95,
            hedge_risk_threshold=0.02,
        )
        assessments = mon.assess_oracle_risk()
        assert len(assessments) == 1
        a = assessments[0]
        assert a.oracle_risk_premium == Decimal("0.04")
        assert "OK" in a.recommendation

    def test_multiple_positions_assessed(self) -> None:
        tracker = PositionTracker()
        tracker._positions["0xyes1"] = Position(
            token_id="0xyes1",
            condition_id="cond1",
            side=Side.BUY,
            entry_price=Decimal("0.98"),
            size=Decimal("100"),
        )
        tracker._positions["0xyes2"] = Position(
            token_id="0xyes2",
            condition_id="cond2",
            side=Side.BUY,
            entry_price=Decimal("0.50"),  # below threshold
            size=Decimal("200"),
        )
        tracker._positions["0xyes3"] = Position(
            token_id="0xyes3",
            condition_id="cond3",
            side=Side.BUY,
            entry_price=Decimal("0.97"),
            size=Decimal("300"),
        )
        mon = _monitor(
            positions=tracker,
            oracle_risk_price_threshold=0.95,
        )
        assessments = mon.assess_oracle_risk()
        # Only positions above 0.95 threshold
        assert len(assessments) == 2
        token_ids = {a.token_id for a in assessments}
        assert token_ids == {"0xyes1", "0xyes3"}


# ── Poll Once with Risk Alerts ───────────────────────────────


class TestPollOnceRiskAlerts:
    @pytest.mark.asyncio
    async def test_poll_emits_high_risk_alert(self) -> None:
        """poll_once should emit HIGH_ORACLE_RISK for high-priced positions."""
        tracker = _tracker_with_position(
            condition_id="cond1",
            price=Decimal("0.99"),
            size=Decimal("100"),
        )
        mon = _monitor(
            positions=tracker,
            oracle_risk_price_threshold=0.95,
            hedge_risk_threshold=0.02,
        )
        alerts: list[OracleAlert] = []
        mon.on_alert(lambda a: alerts.append(a))

        await mon.poll_once()

        risk_alerts = [
            a for a in alerts if a.event_type == OracleEventType.HIGH_ORACLE_RISK
        ]
        assert len(risk_alerts) == 1
        assert "risk premium" in risk_alerts[0].reason.lower()

    @pytest.mark.asyncio
    async def test_poll_no_alert_when_risk_acceptable(self) -> None:
        """No HIGH_ORACLE_RISK alert when premium is above threshold."""
        tracker = _tracker_with_position(
            condition_id="cond1",
            price=Decimal("0.96"),
            size=Decimal("100"),
        )
        mon = _monitor(
            positions=tracker,
            oracle_risk_price_threshold=0.95,
            hedge_risk_threshold=0.02,
        )
        alerts: list[OracleAlert] = []
        mon.on_alert(lambda a: alerts.append(a))

        await mon.poll_once()

        risk_alerts = [
            a for a in alerts if a.event_type == OracleEventType.HIGH_ORACLE_RISK
        ]
        assert len(risk_alerts) == 0

    @pytest.mark.asyncio
    async def test_poll_syncs_conditions_from_positions(self) -> None:
        """poll_once auto-syncs tracked conditions from positions."""
        tracker = _tracker_with_position(condition_id="cond_auto")
        mon = _monitor(positions=tracker)
        assert "cond_auto" not in mon.tracked_conditions
        await mon.poll_once()
        assert "cond_auto" in mon.tracked_conditions
