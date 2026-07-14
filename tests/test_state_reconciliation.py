"""FD-8 reconciliation classifier: truth table + DisagreementReport shape."""

from datetime import UTC, datetime

import pytest

from iab_agentic_primitives.primitives.lifecycle import (
    ChangeRequestStatus,
    DealStatus,
    OrderStatus,
)
from iab_agentic_primitives.state import (
    AUTHORITATIVE_SIDE,
    CHANGE_REQUEST_COMPARISON_TABLE,
    DEAL_COMPARISON_TABLE,
    DEAL_LIFECYCLE,
    ORDER_COMPARISON_TABLE,
    AuthoritativeSide,
    ReconciliationOutcome,
    classify,
    reconcile_change_request,
    reconcile_deal,
    reconcile_order,
)

C = ReconciliationOutcome.CONSISTENT
BB = ReconciliationOutcome.BUYER_BEHIND
SB = ReconciliationOutcome.SELLER_BEHIND
D = ReconciliationOutcome.DIVERGENT


class TestClassifierTruthTable:
    @pytest.mark.parametrize(
        ("buyer", "seller", "expected"),
        [
            # Consistent: identical states.
            (DealStatus.ACTIVE, DealStatus.ACTIVE, C),
            (DealStatus.CANCELLED, DealStatus.CANCELLED, C),
            # Buyer behind: seller has progressed; buyer has not seen it yet.
            (DealStatus.ACCEPTED, DealStatus.BOOKED, BB),
            (DealStatus.ACCEPTED, DealStatus.ACTIVE, BB),
            (DealStatus.BOOKED, DealStatus.COMPLETED, BB),
            (DealStatus.ACTIVE, DealStatus.MAKEGOOD_PENDING, BB),
            # Seller behind: buyer knows something the seller has not processed.
            (DealStatus.ACTIVE, DealStatus.ACCEPTED, SB),
            (DealStatus.CANCELLED, DealStatus.ACTIVE, SB),
            (DealStatus.COMPLETED, DealStatus.BOOKED, SB),
            # Precedence uses forward-progress edges only: the rework edge
            # negotiating -> proposed does not make these divergent.
            (DealStatus.PROPOSED, DealStatus.NEGOTIATING, BB),
            (DealStatus.NEGOTIATING, DealStatus.PROPOSED, SB),
            # Divergent: two conflicting terminal outcomes.
            (DealStatus.CANCELLED, DealStatus.COMPLETED, D),
            (DealStatus.FAILED, DealStatus.EXPIRED, D),
            # Divergent: rejected has no forward path to accepted or back.
            (DealStatus.REJECTED, DealStatus.ACCEPTED, D),
        ],
    )
    def test_deal_pairs(self, buyer, seller, expected) -> None:
        assert classify(DEAL_LIFECYCLE, buyer, seller) is expected
        assert DEAL_COMPARISON_TABLE[(buyer.value, seller.value)] is expected

    @pytest.mark.parametrize(
        ("buyer", "seller", "expected"),
        [
            (OrderStatus.BOOKED, OrderStatus.BOOKED, C),
            (OrderStatus.SUBMITTED, OrderStatus.APPROVED, BB),
            (OrderStatus.BOOKED, OrderStatus.COMPLETED, BB),
            (OrderStatus.IN_PROGRESS, OrderStatus.APPROVED, SB),
            (OrderStatus.CANCELLED, OrderStatus.IN_PROGRESS, SB),
            (OrderStatus.CANCELLED, OrderStatus.COMPLETED, D),
            # Divergent: approval gate produced opposite answers.
            (OrderStatus.REJECTED, OrderStatus.APPROVED, D),
            # The failed -> draft recovery loop is a rework edge, so a
            # buyer that still shows draft is simply behind, not divergent.
            (OrderStatus.DRAFT, OrderStatus.FAILED, BB),
        ],
    )
    def test_order_pairs(self, buyer, seller, expected) -> None:
        assert ORDER_COMPARISON_TABLE[(buyer.value, seller.value)] is expected

    @pytest.mark.parametrize(
        ("buyer", "seller", "expected"),
        [
            (ChangeRequestStatus.PENDING, ChangeRequestStatus.PENDING, C),
            (ChangeRequestStatus.PENDING, ChangeRequestStatus.APPLIED, BB),
            (ChangeRequestStatus.VALIDATING, ChangeRequestStatus.PENDING_APPROVAL, BB),
            (ChangeRequestStatus.APPLIED, ChangeRequestStatus.APPROVED, SB),
            (ChangeRequestStatus.REJECTED, ChangeRequestStatus.APPLIED, D),
        ],
    )
    def test_change_request_pairs(self, buyer, seller, expected) -> None:
        assert CHANGE_REQUEST_COMPARISON_TABLE[(buyer.value, seller.value)] is expected

    def test_tables_are_total(self) -> None:
        assert len(DEAL_COMPARISON_TABLE) == 12 * 12
        assert len(ORDER_COMPARISON_TABLE) == 11 * 11
        assert len(CHANGE_REQUEST_COMPARISON_TABLE) == 7 * 7
        # Diagonal is always consistent; classification is exhaustive.
        for (buyer, seller), outcome in DEAL_COMPARISON_TABLE.items():
            if buyer == seller:
                assert outcome is C
            else:
                assert outcome in {BB, SB, D}

    def test_classifier_is_antisymmetric(self) -> None:
        swap = {BB: SB, SB: BB, C: C, D: D}
        for (buyer, seller), outcome in DEAL_COMPARISON_TABLE.items():
            assert DEAL_COMPARISON_TABLE[(seller, buyer)] is swap[outcome]


class TestReconcileHelpers:
    def test_seller_is_authoritative_for_all_shared_lifecycles(self) -> None:
        assert AUTHORITATIVE_SIDE == {
            "deal": AuthoritativeSide.SELLER,
            "order": AuthoritativeSide.SELLER,
            "change_request": AuthoritativeSide.SELLER,
        }

    def test_consistent_result_has_no_disagreement(self) -> None:
        result = reconcile_deal(
            deal_id="deal-1", buyer_status="active", seller_status=DealStatus.ACTIVE
        )
        assert result.outcome is C
        assert result.disagreement is None
        assert result.authoritative_side is AuthoritativeSide.SELLER

    def test_behind_results_have_no_disagreement_report(self) -> None:
        result = reconcile_order(
            order_id="ord-1", buyer_status="submitted", seller_status="approved"
        )
        assert result.outcome is BB
        assert result.disagreement is None

    def test_divergent_produces_structured_report(self) -> None:
        buyer_seen = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
        seller_seen = datetime(2026, 7, 13, 9, 5, tzinfo=UTC)
        result = reconcile_deal(
            deal_id="deal-9",
            buyer_status=DealStatus.CANCELLED,
            seller_status=DealStatus.COMPLETED,
            buyer_observed_at=buyer_seen,
            seller_observed_at=seller_seen,
        )
        assert result.outcome is D
        report = result.disagreement
        assert report is not None
        assert report.lifecycle == "deal"
        assert report.entity_id == "deal-9"
        assert report.buyer_status == "cancelled"
        assert report.seller_status == "completed"
        assert report.buyer_observed_at == buyer_seen
        assert report.seller_observed_at == seller_seen
        assert report.authoritative_side is AuthoritativeSide.SELLER
        assert report.authoritative_status == "completed"
        assert "cancelled" in report.detail and "completed" in report.detail

    def test_change_request_wrapper(self) -> None:
        result = reconcile_change_request(
            change_request_id="CR-7",
            buyer_status="rejected",
            seller_status="applied",
        )
        assert result.outcome is D
        assert result.disagreement is not None
        assert result.disagreement.authoritative_status == "applied"

    def test_naive_timestamps_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            reconcile_deal(
                deal_id="deal-1",
                buyer_status="active",
                seller_status="active",
                buyer_observed_at=datetime(2026, 7, 13),
            )

    def test_unknown_status_string_rejected(self) -> None:
        with pytest.raises(ValueError):
            reconcile_order(order_id="ord-1", buyer_status="syncing", seller_status="booked")
