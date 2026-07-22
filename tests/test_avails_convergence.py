"""OpenDirect 2.1 dialect convergence for the avails surface.

The canonical avails contract (tests/test_avails.py) pinned the shipped
*simplified profile* (single-product request, camelCase extension
fields). This file pins the CONVERGED dialect: the published OpenDirect
2.1 normative objects — ``ProductAvailsSearch`` (multi-product request),
``Avails`` (per-product response record with ``availsstatus``), and the
``avails`` collection envelope required by the spec's Collection Objects
table for ``POST /products/avails``.

Spec source: OpenDirect v2.1 final (July 8, 2024),
https://github.com/InteractiveAdvertisingBureau/OpenDirect —
"Object: ProductAvailsSearch", "Object: Avails", "Object: AvailsStatus",
"Object: ProductTargeting", and the "Collection Objects" table. The IAB
publishes no machine-readable schemas for OpenDirect 2.1; the shapes here
transcribe the normative attribute tables (all-lowercase wire names,
starred attributes required).

Convergence rules encoded here:

1. Both request dialects are ACCEPTED (legacy single-product
   ``AvailsRequest`` and spec ``ProductAvailsSearch``), discriminated by
   ``productids`` (array, spec) vs ``productid`` (scalar, legacy).
2. The emitted spec request contains ONLY spec-defined top-level fields —
   the legacy extension fields (``requestedImpressions``/``budget``/dict
   ``targeting``) travel inside spec slots: ProductTargeting Investment
   entries and the AdCOM Segment ``targeting`` array.
3. The spec response is the ``avails`` collection envelope of ``Avails``
   records; ``availsstatus`` carries the spec availability semantics
   (Available / Partially Available / Unavailable + reason).
4. Legacy payload round-trips are untouched (pinned in test_avails.py);
   response dialect follows request dialect.
"""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from iab_agentic_primitives.protocol import (
    PROTOCOL_MESSAGES,
    Avails,
    AvailsCollection,
    AvailsRequest,
    AvailsResponse,
    AvailsStatus,
    AvailsStatusReason,
    AvailsStatusValue,
    ProductAvailsSearch,
    ProductTargeting,
    TargetingDimension,
    TargetingUnit,
    avails_from_simplified,
    parse_avails_request,
    parse_avails_response,
)
from iab_agentic_primitives.protocol.avails import (
    EXTENSION_DATASOURCE,
    TARGET_BUDGET,
    TARGET_IMPRESSIONS,
    TARGET_REQUESTED_IMPRESSIONS,
)

START = datetime(2026, 8, 1, tzinfo=UTC)
END = datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC)

# A strictly spec-shaped ProductAvailsSearch, exactly as a conformant
# independent OpenDirect 2.1 client would send it (normative attribute
# table: productids*, accountid*, advertiserbrandid*, startdate*,
# enddate*, optional currency).
SPEC_AVAILS_SEARCH_WIRE = {
    "productids": ["prod-video-001", "prod-video-002"],
    "accountid": "acct-42",
    "advertiserbrandid": "brand-orchard-7",
    "startdate": "2026-08-01T00:00:00Z",
    "enddate": "2026-08-31T23:59:59Z",
    "currency": "USD",
}

# A strictly spec-shaped Avails record (normative attribute table:
# productid*, accountid*, price*, startdate*, enddate*, optional
# availability/currency/availsstatus).
SPEC_AVAILS_WIRE = {
    "productid": "prod-video-001",
    "accountid": "acct-42",
    "availability": 640000,
    "currency": "USD",
    "price": 14.5,
    "startdate": "2026-08-01T00:00:00Z",
    "enddate": "2026-08-31T23:59:59Z",
}

SPEC_REQUEST_FIELDS = {
    "productids",
    "targeting",
    "producttargeting",
    "accountid",
    "currency",
    "advertiserbrandid",
    "availabilityfields",
    "grouping",
    "startdate",
    "enddate",
}

SPEC_AVAILS_FIELDS = {
    "productid",
    "accountid",
    "availability",
    "availsstatus",
    "currency",
    "price",
    "startdate",
    "enddate",
}


def _legacy_request(**overrides) -> AvailsRequest:
    kwargs = dict(
        product_id="prod-video-001",
        start_date=START,
        end_date=END,
        requested_impressions=500_000,
        budget=6000.0,
        targeting={"geo": ["US"], "device": ["mobile", "ctv"]},
    )
    kwargs.update(overrides)
    return AvailsRequest(**kwargs)


# ---------------------------------------------------------------------------
# ProductAvailsSearch: the spec request shape
# ---------------------------------------------------------------------------


class TestProductAvailsSearch:
    def test_accepts_strictly_spec_shaped_payload(self):
        search = ProductAvailsSearch.model_validate(SPEC_AVAILS_SEARCH_WIRE)
        assert search.product_ids == ["prod-video-001", "prod-video-002"]
        assert search.account_id == "acct-42"
        assert search.advertiser_brand_id == "brand-orchard-7"
        assert search.currency == "USD"

    @pytest.mark.parametrize(
        "missing", ["productids", "accountid", "advertiserbrandid", "startdate", "enddate"]
    )
    def test_spec_required_fields_are_required(self, missing):
        payload = {k: v for k, v in SPEC_AVAILS_SEARCH_WIRE.items() if k != missing}
        with pytest.raises(ValidationError):
            ProductAvailsSearch.model_validate(payload)

    def test_productids_must_be_nonempty(self):
        payload = dict(SPEC_AVAILS_SEARCH_WIRE, productids=[])
        with pytest.raises(ValidationError):
            ProductAvailsSearch.model_validate(payload)

    def test_end_must_be_after_start(self):
        payload = dict(
            SPEC_AVAILS_SEARCH_WIRE,
            startdate="2026-08-31T00:00:00Z",
            enddate="2026-08-01T00:00:00Z",
        )
        with pytest.raises(ValidationError, match="enddate must be after startdate"):
            ProductAvailsSearch.model_validate(payload)

    def test_emitted_wire_uses_only_spec_fields(self):
        """The converged request would validate against the published
        ProductAvailsSearch table: spec-lowercase names, no extras."""
        search = ProductAvailsSearch.model_validate(SPEC_AVAILS_SEARCH_WIRE)
        wire = search.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert set(wire) <= SPEC_REQUEST_FIELDS
        assert wire["productids"] == ["prod-video-001", "prod-video-002"]
        assert wire["startdate"] == "2026-08-01T00:00:00Z"

    def test_json_roundtrip(self):
        search = ProductAvailsSearch.model_validate(SPEC_AVAILS_SEARCH_WIRE)
        wire = json.loads(search.model_dump_json(by_alias=True, exclude_none=True))
        assert ProductAvailsSearch.model_validate(wire) == search


# ---------------------------------------------------------------------------
# ProductTargeting / AvailsStatus: the spec sub-objects
# ---------------------------------------------------------------------------


class TestProductTargeting:
    def test_spec_shape(self):
        pt = ProductTargeting.model_validate(
            {
                "name": "Inventory",
                "type": "Audience",
                "datasource": "example-metrics",
                "target": "impressions",
                "targetvalues": ["640000"],
                "selectable": False,
                "count": 640000,
            }
        )
        assert pt.name is TargetingDimension.INVENTORY
        assert pt.type is TargetingUnit.AUDIENCE
        assert pt.target_values == ["640000"]

    @pytest.mark.parametrize(
        "missing",
        ["name", "type", "datasource", "target", "targetvalues", "selectable"],
    )
    def test_required_fields(self, missing):
        payload = {
            "name": "Investment",
            "type": "Investment",
            "datasource": "d",
            "target": "budget",
            "targetvalues": ["6000.0"],
            "selectable": False,
        }
        del payload[missing]
        with pytest.raises(ValidationError):
            ProductTargeting.model_validate(payload)

    def test_name_and_type_are_spec_enums(self):
        with pytest.raises(ValidationError):
            ProductTargeting.model_validate(
                {
                    "name": "NotASpecDimension",
                    "type": "Total",
                    "datasource": "d",
                    "target": "t",
                    "targetvalues": ["v"],
                    "selectable": True,
                }
            )


class TestAvailsStatus:
    def test_spec_shape_and_enums(self):
        status = AvailsStatus.model_validate(
            {
                "status": "Partially Available",
                "reason": "Booked",
                "comment": "Only 400k of 500k requested impressions remain",
                "producttargeting": [
                    {
                        "name": "Inventory",
                        "type": "Audience",
                        "datasource": "example-metrics",
                        "target": "impressions",
                        "targetvalues": ["400000"],
                        "selectable": False,
                    }
                ],
            }
        )
        assert status.status is AvailsStatusValue.PARTIALLY_AVAILABLE
        assert status.reason is AvailsStatusReason.BOOKED
        assert status.product_targeting[0].target == "impressions"

    def test_producttargeting_is_required(self):
        with pytest.raises(ValidationError):
            AvailsStatus.model_validate({"status": "Available"})

    def test_status_enum_is_strict(self):
        with pytest.raises(ValidationError):
            AvailsStatus.model_validate(
                {"status": "SoldOut", "producttargeting": []}
            )

    def test_reason_enum_covers_spec_list(self):
        spec_reasons = {
            "Booked",
            "Optioned",
            "Excluded",
            "OutOfCharge",
            "Prohibited",
            "Manual Trade Only",
            "InvalidPeriodLength",
            "InvalidFrameID",
            "InvalidBudget",
            "InvalidPrice",
            "ClientDuplication",
            "LocationDuplication",
            "LocationJuxta",
        }
        assert {r.value for r in AvailsStatusReason} == spec_reasons


# ---------------------------------------------------------------------------
# Avails + the collection envelope
# ---------------------------------------------------------------------------


class TestAvails:
    def test_accepts_strictly_spec_shaped_payload(self):
        avails = Avails.model_validate(SPEC_AVAILS_WIRE)
        assert avails.product_id == "prod-video-001"
        assert avails.account_id == "acct-42"
        assert avails.price == 14.5
        assert avails.availability == 640000

    @pytest.mark.parametrize(
        "missing", ["productid", "accountid", "price", "startdate", "enddate"]
    )
    def test_spec_required_fields_are_required(self, missing):
        payload = {k: v for k, v in SPEC_AVAILS_WIRE.items() if k != missing}
        with pytest.raises(ValidationError):
            Avails.model_validate(payload)

    def test_emitted_wire_uses_only_spec_fields(self):
        avails = Avails.model_validate(SPEC_AVAILS_WIRE)
        wire = avails.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert set(wire) <= SPEC_AVAILS_FIELDS

    def test_collection_envelope(self):
        """Collection Objects table: POST /products/avails responses wrap
        the records in an object whose array property is named 'avails'."""
        collection = AvailsCollection.model_validate(
            {"avails": [SPEC_AVAILS_WIRE]}
        )
        assert len(collection.avails) == 1
        wire = collection.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert set(wire) == {"avails"}

    def test_empty_collection_is_valid(self):
        assert AvailsCollection.model_validate({"avails": []}).avails == []

    def test_collection_requires_the_avails_array(self):
        with pytest.raises(ValidationError):
            AvailsCollection.model_validate({})


# ---------------------------------------------------------------------------
# Dialect bridge: legacy simplified profile <-> spec shapes
# ---------------------------------------------------------------------------


class TestRequestBridge:
    def test_to_spec_produces_spec_only_top_level_fields(self):
        search = _legacy_request().to_spec(
            account_id="acct-42", advertiser_brand_id="brand-orchard-7"
        )
        wire = search.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert set(wire) <= SPEC_REQUEST_FIELDS
        assert wire["productids"] == ["prod-video-001"]
        assert wire["accountid"] == "acct-42"
        assert wire["advertiserbrandid"] == "brand-orchard-7"

    def test_to_spec_moves_volume_into_investment_producttargeting(self):
        search = _legacy_request().to_spec(
            account_id="acct-42", advertiser_brand_id="brand-orchard-7"
        )
        targets = {pt.target: pt for pt in (search.product_targeting or [])}
        imp = targets[TARGET_REQUESTED_IMPRESSIONS]
        assert imp.name is TargetingDimension.INVESTMENT
        assert imp.target_values == ["500000"]
        assert imp.datasource == EXTENSION_DATASOURCE
        budget = targets[TARGET_BUDGET]
        assert budget.type is TargetingUnit.INVESTMENT
        assert budget.target_values == ["6000.0"]

    def test_to_spec_maps_targeting_dict_to_segment_array(self):
        search = _legacy_request().to_spec(
            account_id="acct-42", advertiser_brand_id="brand-orchard-7"
        )
        # AdCOM Segment objects: {"name": <dimension>, "value": <value>}
        assert search.targeting == [
            {"name": "device", "value": "mobile"},
            {"name": "device", "value": "ctv"},
            {"name": "geo", "value": "US"},
        ]

    def test_to_spec_without_extensions_emits_no_producttargeting(self):
        search = _legacy_request(
            requested_impressions=None, budget=None, targeting=None
        ).to_spec(account_id="acct-42", advertiser_brand_id="brand-orchard-7")
        assert search.product_targeting is None
        assert search.targeting is None

    def test_to_simplified_recovers_legacy_fields(self):
        original = _legacy_request()
        search = original.to_spec(
            account_id="acct-42", advertiser_brand_id="brand-orchard-7"
        )
        [restored] = search.to_simplified()
        assert restored.product_id == original.product_id
        assert restored.requested_impressions == 500_000
        assert restored.budget == 6000.0
        assert restored.targeting == {"device": ["mobile", "ctv"], "geo": ["US"]}
        assert restored.start_date == original.start_date
        assert restored.end_date == original.end_date

    def test_to_simplified_yields_one_query_per_product(self):
        search = ProductAvailsSearch.model_validate(SPEC_AVAILS_SEARCH_WIRE)
        simplified = search.to_simplified()
        assert [req.product_id for req in simplified] == [
            "prod-video-001",
            "prod-video-002",
        ]
        assert all(req.requested_impressions is None for req in simplified)

    def test_parse_avails_request_discriminates_by_productids(self):
        spec = parse_avails_request(SPEC_AVAILS_SEARCH_WIRE)
        assert isinstance(spec, ProductAvailsSearch)
        legacy = parse_avails_request(
            {
                "productid": "prod-video-001",
                "startdate": "2026-08-01T00:00:00Z",
                "enddate": "2026-08-31T23:59:59Z",
            }
        )
        assert isinstance(legacy, AvailsRequest)


class TestResponseBridge:
    def _simplified(self, available: int, guaranteed: int | None = None) -> AvailsResponse:
        return AvailsResponse(
            product_id="prod-video-001",
            available_impressions=available,
            guaranteed_impressions=guaranteed,
            estimated_cpm=14.5,
            total_cost=round(available / 1000 * 14.5, 2),
        )

    def _avails(self, available: int, requested: int | None) -> Avails:
        return avails_from_simplified(
            self._simplified(available),
            account_id="acct-42",
            start_date=START,
            end_date=END,
            currency="USD",
            requested_impressions=requested,
        )

    def test_fully_available(self):
        avails = self._avails(500_000, requested=500_000)
        assert avails.product_id == "prod-video-001"
        assert avails.account_id == "acct-42"
        assert avails.price == 14.5
        assert avails.availability == 500_000
        assert avails.avails_status.status is AvailsStatusValue.AVAILABLE
        assert avails.avails_status.reason is None

    def test_partially_available_reports_reason(self):
        avails = self._avails(400_000, requested=500_000)
        status = avails.avails_status
        assert status.status is AvailsStatusValue.PARTIALLY_AVAILABLE
        assert status.reason is AvailsStatusReason.BOOKED
        [pt] = status.product_targeting
        assert pt.target == TARGET_IMPRESSIONS
        assert pt.target_values == ["400000"]

    def test_unavailable_when_zero(self):
        avails = self._avails(0, requested=500_000)
        assert avails.avails_status.status is AvailsStatusValue.UNAVAILABLE
        assert avails.avails_status.reason is AvailsStatusReason.BOOKED

    def test_no_requested_volume_means_available(self):
        avails = self._avails(250_000, requested=None)
        assert avails.avails_status.status is AvailsStatusValue.AVAILABLE

    def test_spec_wire_shape_of_derived_avails(self):
        wire = self._avails(400_000, requested=500_000).model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        assert set(wire) <= SPEC_AVAILS_FIELDS
        assert wire["startdate"] == "2026-08-01T00:00:00Z"
        assert wire["enddate"] == "2026-08-31T23:59:59Z"

    def test_parse_avails_response_discriminates_by_envelope(self):
        spec = parse_avails_response({"avails": [SPEC_AVAILS_WIRE]})
        assert isinstance(spec, AvailsCollection)
        legacy = parse_avails_response(
            {
                "productid": "prod-video-001",
                "availableImpressions": 750000,
                "estimatedCpm": 12.0,
                "totalCost": 9000.0,
            }
        )
        assert isinstance(legacy, AvailsResponse)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_spec_messages_are_registered():
    assert PROTOCOL_MESSAGES["ProductAvailsSearch"] is ProductAvailsSearch
    assert PROTOCOL_MESSAGES["Avails"] is Avails
    assert PROTOCOL_MESSAGES["AvailsCollection"] is AvailsCollection
