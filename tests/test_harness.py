"""Golden interop scenarios for the two-agent harness.

Each scenario runs the deterministic reference buyer and seller through the
REAL sandbox registry and asserts the reusable interop checks. These are the
same scenarios the two agent repos will re-run against their real agents
(EP-2.x/EP-3.x) and the rig will drive (EP-11): swap a ReferenceBuyer/Seller
for the real one, keep the scenario and the assertions.

Scenarios (bead ar-f3tr):

  (a) happy path: 1 buyer + 1 seller -> booked deal with a seller deal id
  (b) negotiation with a counter that terminates
  (c) budget-ceiling rejection: buyer walks, no deal, no exception
  (d) trust tiers: public pricing vs approved discount, and a buyer cannot
      self-assert a higher tier than the registry grants
  (e) idempotent double-book: same key -> one deal
  (f) 2 buyers + 2 sellers: independent transactions do not cross-contaminate

Plus a documented version-skew smoke stub (the EP-7.2 matrix entry point).
"""

from __future__ import annotations

import pytest

import iab_agentic_primitives
from iab_agentic_primitives.harness import (
    BuyerParticipant,
    CampaignBrief,
    ReferenceBuyer,
    ReferenceSeller,
    SellerParticipant,
    TransactionResult,
    assert_auth_tier_enforced,
    assert_booking_has_seller_deal_id,
    assert_idempotent_booking,
    assert_negotiation_terminates,
    assert_no_field_dropped,
    assert_quote_roundtrips,
    assert_state_consistent,
    assert_structural_rejection,
    run_scenario_sync,
)
from iab_agentic_primitives.primitives import (
    AccessTier,
    BuyerIdentity,
    CommercialTerms,
    DealType,
    LinearTVParams,
    MediaType,
    Money,
    Product,
    TrustStatus,
)
from iab_agentic_primitives.protocol import ErrorCode

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _brief(**overrides) -> CampaignBrief:
    base = dict(
        campaign_name="Q3 Awareness",
        deal_type=DealType.PROGRAMMATIC_GUARANTEED,
        impressions=1_000_000,
        max_cpm=Money.from_decimal_str("15.00"),
        budget=Money.from_decimal_str("20000.00"),
        ad_format="banner",
        audience_plan={"segments": ["auto-intenders"]},
    )
    base.update(overrides)
    return CampaignBrief(**base)


def _run_one(brief: CampaignBrief, *, buyer_trust=TrustStatus.APPROVED) -> TransactionResult:
    result = run_scenario_sync(
        buyers=[BuyerParticipant(ReferenceBuyer(), brief, buyer_trust)],
        sellers=[SellerParticipant(ReferenceSeller(), TrustStatus.APPROVED)],
    )
    return result.transactions[0]


# ---------------------------------------------------------------------------
# (a) Happy path
# ---------------------------------------------------------------------------


def test_happy_path_books_deal_with_seller_deal_id() -> None:
    tx = _run_one(_brief(buyer_identity=BuyerIdentity(agency_id="omnicom-1")))

    assert tx.booked and not tx.walked_away
    assert tx.deal is not None
    # Seller-minted deal id, not buyer-proposed.
    assert tx.deal.deal_id.startswith("ref-seller-")
    # The seller drove BOTH lifecycle machines (deal + order).
    assert len(tx.state_transitions) == 6

    assert_quote_roundtrips(tx)
    assert_no_field_dropped(tx)
    assert_booking_has_seller_deal_id(tx)
    assert_state_consistent(tx)


# ---------------------------------------------------------------------------
# (b) Negotiation that terminates
# ---------------------------------------------------------------------------


def test_negotiation_with_counter_terminates() -> None:
    tx = _run_one(
        _brief(
            buyer_identity=BuyerIdentity(agency_id="omnicom-1"),
            negotiate=True,
            counter_cpm=Money.from_decimal_str("8.00"),
        )
    )

    assert tx.negotiation is not None
    # A real exchange happened (buyer counter -> seller response), and it ended.
    assert len(tx.negotiation.rounds) >= 1
    assert tx.negotiation.status.value in {"accepted", "rejected", "expired"}
    assert_negotiation_terminates(tx)
    # A terminating, accepted negotiation still books.
    if tx.negotiation.status.value == "accepted":
        assert_booking_has_seller_deal_id(tx)
        assert_state_consistent(tx)


def test_negotiation_below_floor_terminates_without_infinite_active() -> None:
    # Buyer's ceiling is below the seller floor (7.00): the negotiation must
    # terminate (rejected), never loop 'active'.
    tx = _run_one(
        _brief(
            max_cpm=Money.from_decimal_str("5.00"),
            budget=Money.from_decimal_str("99999.00"),
            buyer_identity=BuyerIdentity(agency_id="omnicom-1"),
            negotiate=True,
            counter_cpm=Money.from_decimal_str("4.00"),
        )
    )
    # No affordable quote under a 5.00 ceiling (floor is 7.00) -> buyer walks
    # at discovery; if it did negotiate, it still terminated.
    assert tx.walked_away
    if tx.negotiation is not None:
        assert_negotiation_terminates(tx)


# ---------------------------------------------------------------------------
# (c) Budget-ceiling rejection
# ---------------------------------------------------------------------------


def test_budget_ceiling_rejection_walks_cleanly() -> None:
    # Affordable CPM, but the total blows the hard budget ceiling.
    tx = _run_one(
        _brief(
            budget=Money.from_decimal_str("100.00"),
            buyer_identity=BuyerIdentity(agency_id="omnicom-1"),
        )
    )
    assert tx.walked_away is True
    assert tx.deal is None
    assert tx.booked is False
    assert "budget ceiling" in (tx.walk_reason or "")
    # No exception surfaced, and a quote still round-tripped before the walk.
    assert_quote_roundtrips(tx)


# ---------------------------------------------------------------------------
# (d) Trust tiers
# ---------------------------------------------------------------------------


def test_public_tier_buyer_gets_public_pricing() -> None:
    # No revealed identity -> public tier -> list price (no discount).
    tx = _run_one(_brief(budget=Money.from_decimal_str("99999.00")), buyer_trust=TrustStatus.REGISTERED)
    assert tx.quote.buyer_tier is AccessTier.PUBLIC
    assert tx.quote.pricing.final_cpm == Money.from_decimal_str("10.00")
    assert_auth_tier_enforced(tx, expected_tier=AccessTier.PUBLIC)


def test_approved_tier_buyer_gets_the_discount() -> None:
    # Approved trust (ceiling AGENCY) + agency identity -> 10% discount.
    tx = _run_one(
        _brief(buyer_identity=BuyerIdentity(agency_id="omnicom-1")),
        buyer_trust=TrustStatus.APPROVED,
    )
    assert tx.quote.buyer_tier is AccessTier.AGENCY
    assert tx.quote.pricing.final_cpm == Money.from_decimal_str("9.00")
    assert_auth_tier_enforced(tx, expected_tier=AccessTier.AGENCY)


def test_buyer_cannot_self_assert_higher_tier_than_registry_grants() -> None:
    # Buyer self-asserts advertiser (would be 15% off), but the registry only
    # grants REGISTERED (ceiling SEAT): the seller caps it at SEAT (5% off).
    tx = _run_one(
        _brief(buyer_identity=BuyerIdentity(advertiser_id="coca-cola")),
        buyer_trust=TrustStatus.REGISTERED,
    )
    assert tx.tier_ceiling is AccessTier.SEAT
    assert tx.quote.buyer_tier is AccessTier.SEAT
    assert tx.quote.pricing.final_cpm == Money.from_decimal_str("9.50")  # 5% off, not 15%
    assert_auth_tier_enforced(tx, expected_tier=AccessTier.SEAT)


# ---------------------------------------------------------------------------
# (e) Idempotent double-book
# ---------------------------------------------------------------------------


def test_idempotent_double_book_yields_one_deal() -> None:
    tx = _run_one(
        _brief(
            buyer_identity=BuyerIdentity(agency_id="omnicom-1"),
            idempotent_double_book=True,
        )
    )
    assert tx.deal is not None and tx.replay_deal is not None
    assert tx.deal.deal_id == tx.replay_deal.deal_id
    assert_idempotent_booking(tx)


# ---------------------------------------------------------------------------
# (f) 2 buyers x 2 sellers: no cross-contamination
# ---------------------------------------------------------------------------


def _display_only_seller() -> ReferenceSeller:
    product = Product(
        product_id="disp",
        seller_organization_id="org-disp",
        name="Display",
        base_price=Money.from_decimal_str("10.00"),
        ad_formats=["banner"],
        commercial_terms=CommercialTerms(supported_deal_types=[DealType.PROGRAMMATIC_GUARANTEED]),
    )
    return ReferenceSeller(
        "seller-display", "Display Seller", catalog=[(product, Money.from_decimal_str("7.00").amount_micros)]
    )


def _video_only_seller() -> ReferenceSeller:
    product = Product(
        product_id="vid",
        seller_organization_id="org-vid",
        name="Video",
        base_price=Money.from_decimal_str("12.00"),
        ad_formats=["video"],
        commercial_terms=CommercialTerms(supported_deal_types=[DealType.PROGRAMMATIC_GUARANTEED]),
    )
    return ReferenceSeller(
        "seller-video", "Video Seller", catalog=[(product, Money.from_decimal_str("8.00").amount_micros)]
    )


def test_two_buyers_two_sellers_do_not_cross_contaminate() -> None:
    buyer_a = ReferenceBuyer("buyer-a", "Buyer A", organization_id="org-a")
    buyer_b = ReferenceBuyer("buyer-b", "Buyer B", organization_id="org-b")
    brief_a = _brief(ad_format="banner", buyer_identity=BuyerIdentity(agency_id="agency-a"))
    brief_b = _brief(ad_format="video", buyer_identity=BuyerIdentity(agency_id="agency-b"))

    result = run_scenario_sync(
        buyers=[
            BuyerParticipant(buyer_a, brief_a, TrustStatus.APPROVED),
            BuyerParticipant(buyer_b, brief_b, TrustStatus.APPROVED),
        ],
        sellers=[
            SellerParticipant(_display_only_seller(), TrustStatus.APPROVED),
            SellerParticipant(_video_only_seller(), TrustStatus.APPROVED),
        ],
    )

    tx_a = result.for_buyer("buyer-a")
    tx_b = result.for_buyer("buyer-b")

    # Each buyer routed to the seller that carries its format.
    assert tx_a.seller_id == "seller-display"
    assert tx_b.seller_id == "seller-video"
    # Both booked, with DISTINCT deals — no shared or leaked state.
    assert tx_a.booked and tx_b.booked
    assert tx_a.deal.deal_id != tx_b.deal.deal_id
    assert tx_a.deal.product.product_id == "disp"
    assert tx_b.deal.product.product_id == "vid"
    # Each deal is attributed to the correct minting seller.
    assert tx_a.deal.seller_id == "seller-display"
    assert tx_b.deal.seller_id == "seller-video"
    # Timelines are disjoint (each buyer only spoke to its own seller).
    for ex in tx_a.timeline:
        if ex.surface == "booking" and not ex.rejected:
            assert ex.response.deal.seller_id == "seller-display"

    for tx in (tx_a, tx_b):
        assert_quote_roundtrips(tx)
        assert_booking_has_seller_deal_id(tx)
        assert_state_consistent(tx)


# ---------------------------------------------------------------------------
# FD-6 structural rejection (linear TV): the field is never silently dropped
# ---------------------------------------------------------------------------


def test_linear_tv_rejected_structurally_not_mispriced() -> None:
    tx = _run_one(
        _brief(
            media_type=MediaType.LINEAR_TV,
            linear_tv=LinearTVParams(target_demo="A18-49", grps_requested=100),
            buyer_identity=BuyerIdentity(agency_id="omnicom-1"),
        )
    )
    # The reference seller does not support linear TV: it rejects STRUCTURALLY
    # (unsupported_capability naming linear_tv), never returns a digital quote.
    assert_structural_rejection(tx, "quote", ErrorCode.UNSUPPORTED_CAPABILITY)
    assert_no_field_dropped(tx)
    assert tx.walked_away  # no affordable/valid quote -> buyer walks cleanly


# ---------------------------------------------------------------------------
# Version-skew smoke stub (EP-7.2 entry point)
# ---------------------------------------------------------------------------

# For now both reference agents are built from the SAME installed contract, so
# the only working matrix cell is (N, N). The real buyer@N vs seller@N±1 matrix
# lands in EP-7.2: parametrize this with (buyer_version, seller_version) pairs
# and pin each side to a different installed contract wheel. The parametrize
# list below is the documented extension point — add skew rows there.
_VERSION_SKEW_MATRIX = [
    # (buyer_contract_version, seller_contract_version)
    (iab_agentic_primitives.__version__, iab_agentic_primitives.__version__),
    # TODO(EP-7.2): (N, N-1), (N-1, N), (N, N+1), (N+1, N) with pinned wheels.
]


@pytest.mark.parametrize(("buyer_version", "seller_version"), _VERSION_SKEW_MATRIX)
def test_version_skew_smoke(buyer_version: str, seller_version: str) -> None:
    # Both reference agents must run at the same contract version today; this
    # asserts the invariant and gives EP-7.2 a working (N, N) baseline to grow
    # the real skew matrix from.
    assert buyer_version == seller_version == iab_agentic_primitives.__version__
    tx = _run_one(_brief(buyer_identity=BuyerIdentity(agency_id="omnicom-1")))
    assert tx.booked
    assert_state_consistent(tx)
