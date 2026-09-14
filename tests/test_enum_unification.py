"""Enum-unification tests: one vocabulary per concept, wire values canonical.

PG = Programmatic Guaranteed, PD = Preferred Deal, PA = Private Auction.
"""

import pytest

from iab_agentic_primitives.primitives import (
    DealStatus,
    DealType,
    LineStatus,
    OrderStatus,
    QuoteStatus,
)

# ---------------------------------------------------------------------------
# DealType: the short wire encoding wins; long-form names are gone
# ---------------------------------------------------------------------------


def test_dealtype_wire_values() -> None:
    assert DealType("PG") is DealType.PROGRAMMATIC_GUARANTEED
    assert DealType("PD") is DealType.PREFERRED_DEAL
    assert DealType("PA") is DealType.PRIVATE_AUCTION
    assert {m.value for m in DealType} == {"PG", "PD", "PA"}


@pytest.mark.parametrize(
    "retired", ["programmaticguaranteed", "preferreddeal", "privateauction", "pg", "pd", "pa"]
)
def test_dealtype_rejects_retired_long_form_values(retired: str) -> None:
    """The seller repo's long-form encodings are NOT valid wire values."""
    with pytest.raises(ValueError):
        DealType(retired)


def test_dealtype_serializes_to_wire_value() -> None:
    from iab_agentic_primitives.primitives import ProductRef, Quote, QuotePricing, QuoteTerms

    quote = Quote(
        quote_id="q-1",
        deal_type=DealType.PROGRAMMATIC_GUARANTEED,
        product=ProductRef(product_id="p-1", name="x"),
        pricing=QuotePricing(),
        terms=QuoteTerms(),
    )
    assert '"deal_type":"PG"' in quote.model_dump_json()


def test_dealtype_docstring_disclaims_curation_as_a_deal_type() -> None:
    """Curated packages are represented by the Curation object, not a DealType
    value; the docstring must say so explicitly and record that CUR/PMP were
    considered and rejected as fourth deal-type wire values."""
    doc = DealType.__doc__ or ""
    assert "not" in doc.lower() and "deal type" in doc.lower()
    assert "Curation" in doc
    assert "CUR" in doc
    assert "PMP" in doc
    assert "orthogonal" in doc.lower()


# ---------------------------------------------------------------------------
# DealStatus: ONE unioned vocabulary; retired aliases absent
# ---------------------------------------------------------------------------


def test_dealstatus_union_values_present() -> None:
    expected = {
        "proposed",
        "negotiating",
        "accepted",
        "booked",
        "active",
        "makegood_pending",
        "partially_cancelled",
        "completed",
        "rejected",
        "failed",
        "cancelled",
        "expired",
    }
    assert {m.value for m in DealStatus} == expected


@pytest.mark.parametrize(
    "retired",
    [
        "delivering",  # alias of "active" (buyer BuyerDealStatus)
        "booking",  # alias of "booked" (buyer BuyerDealStatus)
        "quoted",  # a QuoteStatus concern, not a deal state
        "partially_canceled",  # single-l spelling retired
    ],
)
def test_dealstatus_retired_aliases_absent(retired: str) -> None:
    with pytest.raises(ValueError):
        DealStatus(retired)


# ---------------------------------------------------------------------------
# OrderStatus: ONE unioned vocabulary; retired aliases absent
# ---------------------------------------------------------------------------


def test_orderstatus_union_values_present() -> None:
    expected = {
        "draft",
        "submitted",
        "pending_approval",
        "approved",
        "rejected",
        "in_progress",
        "booked",
        "unbooked",
        "completed",
        "failed",
        "cancelled",
    }
    assert {m.value for m in OrderStatus} == expected


@pytest.mark.parametrize(
    "retired",
    [
        "PENDING",  # buyer OpenDirect uppercase -> pending_approval
        "APPROVED",  # uppercase retired
        "proposed",  # seller ExecutionOrderStatus -> submitted
        "canceled",  # single-l spelling retired
        "syncing",  # seller-internal ad-server state, not a wire state
    ],
)
def test_orderstatus_retired_aliases_absent(retired: str) -> None:
    with pytest.raises(ValueError):
        OrderStatus(retired)


# ---------------------------------------------------------------------------
# QuoteStatus / LineStatus sanity
# ---------------------------------------------------------------------------


def test_quotestatus_values() -> None:
    assert {m.value for m in QuoteStatus} == {"available", "booked", "expired", "declined"}


def test_linestatus_keeps_opendirect_wire_casing() -> None:
    """OpenDirect (the IAB direct-buying API standard) vocabulary wins as-is."""
    assert LineStatus("InFlight") is LineStatus.IN_FLIGHT
    assert LineStatus("PendingReservation") is LineStatus.PENDING_RESERVATION
    with pytest.raises(ValueError):
        LineStatus("in_flight")
