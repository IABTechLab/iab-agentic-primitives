"""Avails surface: ``POST /products/avails`` (availability + pricing query).

An avails query is a capability, not a persisted object — it is a protocol
message, not a primitive (RECONCILIATION.md G9 / plan §4.4). The surface
shipped in the seller agent v2.1.0 and is documented on its public API
reference, so the canonical contract preserves the served wire dialect
byte-for-byte: OpenDirect 2.1 spec-lowercase names for spec-defined fields
(``productid``/``startdate``/``enddate``) and camelCase for the extension
fields, exactly as both agents already exchange them.

Settled policy decisions encoded here:

1. ``availableImpressions`` is REQUIRED. A product with no capacity cap
   (no ``maximum_impressions``) reports the requested volume as available;
   the field is never omitted.
2. ``deliveryConfidence`` is OPTIONAL and is OMITTED ENTIRELY when the
   seller has no delivery-forecast data source. Conformant emitters never
   fabricate a value and never pad with ``null``; readers tolerate an
   explicit ``null`` from pre-contract emitters (seller <= v2.1.0).
3. ``guaranteedImpressions`` is present ONLY for PG-capable products
   (PG = Programmatic Guaranteed in ``supported_deal_types``); it is
   omitted otherwise.

Money dialect (flagged): ``budget``/``estimatedCpm``/``totalCost`` are
floats, NOT the shared :class:`~iab_agentic_primitives.primitives.Money`
micros type (FD-11). This is a deliberate, documented exception: the
surface is live on the OpenDirect 2.1 float dialect and existing payloads
must round-trip identically. Migrating avails money fields to ``Money``
is a breaking change reserved for the next major version.

OpenDirect = the IAB direct-buying API standard; CPM = cost per mille
(cost per thousand impressions).
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import ConfigDict, Field, model_validator

from ..primitives import WireModel


class AvailsWireModel(WireModel):
    """Base for avails messages: aliased wire names, either spelling in."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AvailsRequest(AvailsWireModel):
    """Request body for ``POST /products/avails``.

    Spec-named fields use the OpenDirect 2.1 all-lowercase wire names;
    the extension fields (``requestedImpressions``/``budget``/
    ``targeting``) keep their camelCase names. When neither
    ``requestedImpressions`` nor ``budget`` is sent, the seller falls back
    to the product's minimum impressions.
    """

    product_id: str = Field(alias="productid", description="Product to check.")
    start_date: datetime = Field(
        alias="startdate", description="Flight start (ISO-8601)."
    )
    end_date: datetime = Field(
        alias="enddate",
        description="Flight end (ISO-8601); must be after startdate.",
    )
    requested_impressions: int | None = Field(
        default=None,
        alias="requestedImpressions",
        ge=0,
        description="Requested volume; when omitted the seller derives it "
        "from budget at the product CPM, else the product minimum.",
    )
    budget: float | None = Field(
        default=None,
        description="Budget in currency units (OpenDirect 2.1 float "
        "dialect — see the module docstring for the FD-11 exception).",
    )
    targeting: dict[str, Any] | None = Field(
        default=None,
        description="Requested targeting slices; sellers without per-slice "
        "availability data accept but do not filter on it.",
    )

    @model_validator(mode="after")
    def _end_after_start(self) -> "AvailsRequest":
        start, end = self.start_date, self.end_date
        # Normalize mixed naive/aware datetimes (treat naive as UTC) so the
        # comparison never raises.
        if (start.tzinfo is None) != (end.tzinfo is None):
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            if end.tzinfo is None:
                end = end.replace(tzinfo=UTC)
        if end <= start:
            raise ValueError("enddate must be after startdate")
        return self


class AvailsResponse(AvailsWireModel):
    """Response body for ``POST /products/avails``.

    Honest-availability policy: every number is derived from catalog
    data — nothing is fabricated. Optional fields with no value are
    OMITTED from the wire, not sent as ``null`` (readers tolerate ``null``
    from pre-contract emitters).
    """

    product_id: str = Field(alias="productid", description="Echo of the product.")
    available_impressions: int = Field(
        alias="availableImpressions",
        ge=0,
        description="REQUIRED (policy 1). Products without a capacity cap "
        "report the requested volume as available.",
    )
    guaranteed_impressions: int | None = Field(
        default=None,
        alias="guaranteedImpressions",
        ge=0,
        description="Present ONLY for PG-capable products (policy 3); "
        "omitted otherwise.",
    )
    estimated_cpm: float = Field(
        alias="estimatedCpm",
        description="CPM the availability is priced at (base CPM, falling "
        "back to floor CPM). OpenDirect 2.1 float dialect (FD-11 exception).",
    )
    total_cost: float = Field(
        alias="totalCost",
        description="availableImpressions / 1000 * estimatedCpm, rounded "
        "to 2 decimals. OpenDirect 2.1 float dialect (FD-11 exception).",
    )
    delivery_confidence: float | None = Field(
        default=None,
        alias="deliveryConfidence",
        ge=0,
        le=100,
        description="Forecast confidence percentage. OPTIONAL — OMITTED "
        "entirely when the seller has no forecast data source (policy 2); "
        "never fabricated.",
    )
    available_targeting: list[str] | None = Field(
        default=None,
        alias="availableTargeting",
        description="Targeting dimensions the product supports; omitted "
        "when the product declares none.",
    )


__all__ = ["AvailsRequest", "AvailsResponse", "AvailsWireModel"]
