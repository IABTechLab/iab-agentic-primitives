"""Avails surface: ``POST /products/avails`` (EP — avails wire contract).

The avails query is a protocol message, not a primitive (RECONCILIATION.md
G9 / plan §4.4): a request/response capability with no persisted identity.
This file pins the canonical contract and the three settled policy
decisions:

1. ``availableImpressions`` is REQUIRED — uncapped products report
   requested-as-available rather than omitting the field.
2. ``deliveryConfidence`` is OPTIONAL and is OMITTED entirely when the
   seller has no delivery-forecast data source (never fabricated, never
   ``null``-padded by a conformant emitter; ``null`` is tolerated on input
   for compatibility with pre-contract emitters).
3. ``guaranteedImpressions`` is present ONLY for PG-capable products
   (PG = Programmatic Guaranteed).

Wire dialect: the surface shipped in the seller agent v2.1.0 speaking the
OpenDirect 2.1 spec-lowercase names (``productid``/``startdate``/
``enddate``) with camelCase extension fields — the canonical contract
preserves that dialect byte-for-byte so existing payloads round-trip
identically (see the mirrored constants in both agent repos'
``tests/unit/test_opendirect_wire_conformance.py``).
"""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from iab_agentic_primitives.protocol import (
    PROTOCOL_MESSAGES,
    AvailsRequest,
    AvailsResponse,
)

START = datetime(2026, 8, 1, tzinfo=UTC)
END = datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC)

# Existing wire payloads, byte-for-byte as the agents exchange them today
# (mirrored in buyer-agent and seller-agent
# tests/unit/test_opendirect_wire_conformance.py).
BUYER_AVAILS_REQUEST_WIRE = {
    "productid": "prod-display-001",
    "startdate": "2026-08-01T00:00:00Z",
    "enddate": "2026-08-31T23:59:59Z",
    "requestedImpressions": 500000,
    "budget": 6000.0,
    "targeting": {"geo": ["US"], "device": ["mobile"]},
}

# Canonical PG response (policy 3: guaranteedImpressions present, PG-capable).
SELLER_AVAILS_RESPONSE_WIRE_PG = {
    "productid": "prod-display-001",
    "availableImpressions": 750000,
    "guaranteedImpressions": 500000,
    "estimatedCpm": 12.0,
    "totalCost": 6000.0,
}

# Pre-contract emitters (seller <= v2.1.0) padded absent optionals with
# explicit nulls; a tolerant reader still accepts those payloads.
SELLER_AVAILS_RESPONSE_WIRE_LEGACY_NULLS = {
    **SELLER_AVAILS_RESPONSE_WIRE_PG,
    "deliveryConfidence": None,
    "availableTargeting": None,
}


# ---------------------------------------------------------------------------
# Registration: the avails surface is part of the protocol contract
# ---------------------------------------------------------------------------


def test_avails_messages_are_registered_protocol_messages() -> None:
    assert PROTOCOL_MESSAGES["AvailsRequest"] is AvailsRequest
    assert PROTOCOL_MESSAGES["AvailsResponse"] is AvailsResponse


# ---------------------------------------------------------------------------
# Wire dialect: OpenDirect 2.1 spec-lowercase + camelCase extension fields
# ---------------------------------------------------------------------------


class TestRequestWireDialect:
    def test_existing_request_payload_roundtrips_identically(self) -> None:
        req = AvailsRequest.model_validate(BUYER_AVAILS_REQUEST_WIRE)
        assert req.product_id == "prod-display-001"
        assert req.requested_impressions == 500000
        body = req.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert body == BUYER_AVAILS_REQUEST_WIRE

    def test_snake_case_construction_emits_spec_lowercase(self) -> None:
        req = AvailsRequest(
            product_id="prod-display-001",
            start_date=START,
            end_date=END,
            requested_impressions=500000,
            budget=6000.0,
            targeting={"geo": ["US"], "device": ["mobile"]},
        )
        body = req.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert body == BUYER_AVAILS_REQUEST_WIRE

    def test_legacy_camelcase_request_is_rejected(self) -> None:
        legacy = {
            "productId": "prod-display-001",
            "startDate": "2026-08-01T00:00:00Z",
            "endDate": "2026-08-31T23:59:59Z",
        }
        with pytest.raises(ValidationError):
            AvailsRequest.model_validate(legacy)

    def test_minimal_request_omits_absent_optionals(self) -> None:
        req = AvailsRequest(product_id="p1", start_date=START, end_date=END)
        body = req.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert set(body) == {"productid", "startdate", "enddate"}


class TestRequestValidation:
    def test_enddate_must_be_after_startdate(self) -> None:
        with pytest.raises(ValidationError, match="enddate must be after startdate"):
            AvailsRequest(product_id="p1", start_date=END, end_date=START)

    def test_mixed_naive_aware_dates_compare_without_crashing(self) -> None:
        # naive start, aware end: naive is treated as UTC, no TypeError.
        req = AvailsRequest(
            product_id="p1",
            start_date=datetime(2026, 8, 1),
            end_date=END,
        )
        assert req.end_date > START

    def test_requested_impressions_cannot_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            AvailsRequest(
                product_id="p1",
                start_date=START,
                end_date=END,
                requested_impressions=-1,
            )


# ---------------------------------------------------------------------------
# Response: the three settled policy decisions
# ---------------------------------------------------------------------------


class TestResponsePolicy:
    def test_available_impressions_is_required(self) -> None:
        """Policy 1: uncapped products report requested-as-available;
        the field is never optional."""
        payload = dict(SELLER_AVAILS_RESPONSE_WIRE_PG)
        del payload["availableImpressions"]
        with pytest.raises(ValidationError):
            AvailsResponse.model_validate(payload)
        schema = AvailsResponse.model_json_schema()
        assert "availableImpressions" in schema["required"]

    def test_delivery_confidence_is_optional_and_omitted_when_absent(self) -> None:
        """Policy 2: no data source -> the field is absent, not null."""
        resp = AvailsResponse.model_validate(SELLER_AVAILS_RESPONSE_WIRE_PG)
        assert resp.delivery_confidence is None
        schema = AvailsResponse.model_json_schema()
        assert "deliveryConfidence" not in schema["required"]
        wire = resp.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert "deliveryConfidence" not in wire

    def test_delivery_confidence_bounds_when_present(self) -> None:
        with pytest.raises(ValidationError):
            AvailsResponse.model_validate(
                {**SELLER_AVAILS_RESPONSE_WIRE_PG, "deliveryConfidence": 101.0}
            )
        resp = AvailsResponse.model_validate(
            {**SELLER_AVAILS_RESPONSE_WIRE_PG, "deliveryConfidence": 95.0}
        )
        assert resp.delivery_confidence == 95.0

    def test_guaranteed_impressions_only_for_pg_capable_products(self) -> None:
        """Policy 3: non-PG responses omit guaranteedImpressions."""
        non_pg = {
            "productid": "prod-display-002",
            "availableImpressions": 250000,
            "estimatedCpm": 8.5,
            "totalCost": 2125.0,
        }
        resp = AvailsResponse.model_validate(non_pg)
        assert resp.guaranteed_impressions is None
        schema = AvailsResponse.model_json_schema()
        assert "guaranteedImpressions" not in schema["required"]
        wire = resp.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert wire == non_pg


class TestResponseWireCompatibility:
    def test_pg_response_roundtrips_identically(self) -> None:
        resp = AvailsResponse.model_validate(SELLER_AVAILS_RESPONSE_WIRE_PG)
        wire = resp.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert wire == SELLER_AVAILS_RESPONSE_WIRE_PG

    def test_legacy_null_padded_response_still_parses(self) -> None:
        """Tolerant reader: pre-contract emitters sent explicit nulls."""
        resp = AvailsResponse.model_validate(SELLER_AVAILS_RESPONSE_WIRE_LEGACY_NULLS)
        assert resp.delivery_confidence is None
        assert resp.available_targeting is None
        # Kept verbatim (nulls included) when dumped without exclude_none:
        # existing serialized payloads round-trip identically.
        wire = json.loads(resp.model_dump_json(by_alias=True))
        assert wire == SELLER_AVAILS_RESPONSE_WIRE_LEGACY_NULLS

    def test_legacy_camelcase_response_is_rejected(self) -> None:
        legacy = dict(SELLER_AVAILS_RESPONSE_WIRE_PG)
        legacy["productId"] = legacy.pop("productid")
        with pytest.raises(ValidationError):
            AvailsResponse.model_validate(legacy)

    def test_unknown_fields_are_ignored(self) -> None:
        """FD-13 applies to the avails surface too."""
        resp = AvailsResponse.model_validate(
            {
                **SELLER_AVAILS_RESPONSE_WIRE_PG,
                "x_vendor_hint": "ignore me",
                "brand_new_field": 1,
            }
        )
        assert not hasattr(resp, "x_vendor_hint")


# ---------------------------------------------------------------------------
# Spec artifacts: exported schema + OpenAPI path
# ---------------------------------------------------------------------------


def test_exported_schemas_exist_for_avails_messages() -> None:
    from pathlib import Path

    schema_dir = Path(__file__).parent.parent / "spec" / "jsonschema" / "protocol"
    for name in ("AvailsRequest", "AvailsResponse"):
        assert (schema_dir / f"{name}.json").exists()


def test_openapi_declares_the_avails_path() -> None:
    from pathlib import Path

    text = (
        Path(__file__).parent.parent / "spec" / "openapi" / "iab-agentic-api.yaml"
    ).read_text()
    assert "/products/avails:" in text
    assert "AvailsRequest.json" in text
    assert "AvailsResponse.json" in text
