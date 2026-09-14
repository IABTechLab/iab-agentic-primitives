"""Tests for the Curation object (IAB Deals API v1.0, deal-api spec, deal1.0.md).

Curated packages ride on a Deal via the optional ``curation`` field, not via
a dedicated DealType value — see DealType's docstring and
tests/test_enum_unification.py::test_dealtype_docstring_disclaims_curation_as_a_deal_type
for the disambiguation this repo settled on (CUR and "PMP" were both
considered and rejected as DealType values).
"""

import pytest
from pydantic import ValidationError

from iab_agentic_primitives.primitives import (
    Curation,
    CurationFeeType,
    Deal,
    DealType,
    ProductRef,
    QuotePricing,
    QuoteTerms,
)


def _deal(**kwargs) -> Deal:
    return Deal(
        deal_id="deal-1",
        deal_type=DealType.PRIVATE_AUCTION,
        product=ProductRef(product_id="p-1", name="x"),
        pricing=QuotePricing(),
        terms=QuoteTerms(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Optional-absent default: a Deal with no curation info still validates
# ---------------------------------------------------------------------------


def test_deal_curation_defaults_to_none() -> None:
    deal = _deal()
    assert deal.curation is None
    assert '"curation":null' in deal.model_dump_json()


# ---------------------------------------------------------------------------
# Round-trip: a populated Curation object survives serialize/deserialize
# ---------------------------------------------------------------------------


def test_curation_roundtrips_on_a_deal() -> None:
    curation = Curation(
        curator="curator.example.com",
        curator_deal_id="curator-deal-42",
        curation_fee_type=CurationFeeType.PERCENT_OF_SPEND,
    )
    deal = _deal(curation=curation)

    dumped = deal.model_dump_json()
    assert '"curator":"curator.example.com"' in dumped
    assert '"curator_deal_id":"curator-deal-42"' in dumped
    assert '"curation_fee_type":1' in dumped

    reparsed = Deal.model_validate_json(dumped)
    assert reparsed.curation == curation
    assert reparsed.curation.curator == "curator.example.com"
    assert reparsed.curation.curator_deal_id == "curator-deal-42"
    assert reparsed.curation.curation_fee_type is CurationFeeType.PERCENT_OF_SPEND


def test_curation_fields_are_individually_optional() -> None:
    """Every Curation field is optional per the published spec (all fields
    in the Deals API v1.0 Curation object table are marked Optional)."""
    curation = Curation()
    assert curation.curator is None
    assert curation.curator_deal_id is None
    assert curation.curation_fee_type is None
    assert curation.ext is None


def test_curation_can_accompany_any_deal_type() -> None:
    """Curation is orthogonal to DealType: it can ride with PG, not just PA."""
    curation = Curation(curator="curator.example.com", curator_deal_id="cd-1")
    deal = Deal(
        deal_id="deal-2",
        deal_type=DealType.PROGRAMMATIC_GUARANTEED,
        product=ProductRef(product_id="p-1", name="x"),
        pricing=QuotePricing(),
        terms=QuoteTerms(),
        curation=curation,
    )
    assert deal.deal_type is DealType.PROGRAMMATIC_GUARANTEED
    assert deal.curation == curation


# ---------------------------------------------------------------------------
# curation_fee_type bounds: only the spec's 0-4 enum values are valid
# ---------------------------------------------------------------------------


def test_curation_fee_type_wire_values() -> None:
    assert CurationFeeType.UNDISCLOSED == 0
    assert CurationFeeType.PERCENT_OF_SPEND == 1
    assert CurationFeeType.FLAT_FEE == 2
    assert CurationFeeType.CPM == 3
    assert CurationFeeType.NO_FEE == 4
    assert {m.value for m in CurationFeeType} == {0, 1, 2, 3, 4}


@pytest.mark.parametrize("out_of_bounds", [-1, 5, 100])
def test_curation_fee_type_rejects_out_of_bounds_values(out_of_bounds: int) -> None:
    with pytest.raises(ValidationError):
        Curation(curation_fee_type=out_of_bounds)
