"""Tests for the canonical event vocabulary and envelope (EP-1.5)."""

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from iab_agentic_primitives.events import LEGACY_ALIASES, Event, EventType, utc_now
from iab_agentic_primitives.events.schema import build_event_schema, build_event_type_values

REPO_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Source vocabularies, hardcoded as fixtures.
#
# The two agent repos are not dependencies of this library, so their enums
# cannot be imported. These lists are verbatim copies of the enum values in:
#   buyer:  buyer-agent/src/ad_buyer/events/models.py   (EventType, 38 values)
#   seller: seller-agent/src/ad_seller/events/models.py (EventType, 22 values)
# ---------------------------------------------------------------------------

BUYER_EVENT_VALUES = [
    "quote.requested",
    "quote.received",
    "deal.booked",
    "deal.cancelled",
    "campaign.created",
    "campaign.brief_validated",
    "campaign.plan_generated",
    "campaign.plan_approved",
    "campaign.booking_started",
    "campaign.booking_completed",
    "campaign.ready",
    "campaign.activated",
    "campaign.completed",
    "campaign.canceled",
    "budget.allocated",
    "booking.submitted",
    "inventory.discovered",
    "negotiation.started",
    "negotiation.round",
    "negotiation.concluded",
    "session.created",
    "session.closed",
    "deal.imported",
    "deal.template_created",
    "portfolio.inspected",
    "deal.manual_action_required",
    "pacing.snapshot_taken",
    "pacing.deviation_detected",
    "pacing.reallocation_recommended",
    "pacing.reallocation_applied",
    "creative.uploaded",
    "creative.validated",
    "creative.matched",
    "creative.rotation_updated",
    "creative.ad_server_pushed",
    "approval.requested",
    "approval.granted",
    "approval.rejected",
]

SELLER_EVENT_VALUES = [
    "proposal.received",
    "proposal.evaluated",
    "proposal.accepted",
    "proposal.rejected",
    "proposal.countered",
    "deal.created",
    "deal.registered",
    "deal.synced",
    "execution.completed",
    "approval.requested",
    "approval.granted",
    "approval.denied",
    "approval.timed_out",
    "session.created",
    "session.resumed",
    "session.closed",
    "package.created",
    "package.updated",
    "package.synced",
    "negotiation.started",
    "negotiation.round",
    "negotiation.concluded",
]


def canonicalize(source_value: str) -> EventType:
    """Map a legacy source value to its canonical EventType member."""
    if source_value in LEGACY_ALIASES:
        return LEGACY_ALIASES[source_value]
    return EventType(source_value)


class TestEnvelope:
    def test_round_trip_full(self) -> None:
        event = Event(
            event_type=EventType.DEAL_BOOKED,
            source_agent="buyer",
            deal_id="deal-1",
            order_id="order-1",
            proposal_id="prop-1",
            campaign_id="camp-1",
            session_id="sess-1",
            metadata={"flow_id": "flow-9", "cpm": 12.5},
        )
        restored = Event.model_validate_json(event.model_dump_json())
        assert restored == event

    def test_round_trip_minimal_defaults(self) -> None:
        event = Event(event_type=EventType.SESSION_CREATED, source_agent="seller")
        restored = Event.model_validate_json(event.model_dump_json())
        assert restored == event
        assert restored.event_id  # UUID default was generated
        assert restored.deal_id is None
        assert restored.metadata == {}

    def test_occurred_at_default_is_aware_utc(self) -> None:
        event = Event(event_type=EventType.QUOTE_REQUESTED, source_agent="buyer")
        assert event.occurred_at.tzinfo is not None
        assert event.occurred_at.utcoffset() == timedelta(0)

    def test_occurred_at_rejects_naive(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            Event(
                event_type=EventType.QUOTE_REQUESTED,
                source_agent="buyer",
                occurred_at=datetime(2026, 7, 13, 12, 0, 0),  # noqa: DTZ001
            )

    def test_occurred_at_normalized_to_utc(self) -> None:
        eastern = timezone(timedelta(hours=-5))
        event = Event(
            event_type=EventType.QUOTE_REQUESTED,
            source_agent="buyer",
            occurred_at=datetime(2026, 7, 13, 7, 0, 0, tzinfo=eastern),
        )
        assert event.occurred_at.tzinfo == UTC
        assert event.occurred_at.hour == 12

    def test_source_agent_restricted(self) -> None:
        with pytest.raises(ValidationError):
            Event(event_type=EventType.QUOTE_REQUESTED, source_agent="broker")

    def test_utc_now_is_aware(self) -> None:
        assert utc_now().tzinfo == UTC


class TestVocabulary:
    def test_every_value_dot_namespaced_lowercase(self) -> None:
        pattern = re.compile(r"^[a-z][a-z_]*\.[a-z][a-z_]*$")
        for member in EventType:
            assert pattern.fullmatch(member.value), (
                f"{member.name}={member.value!r} is not dot-namespaced lowercase"
            )

    def test_values_unique(self) -> None:
        values = [member.value for member in EventType]
        assert len(values) == len(set(values))

    def test_canonical_count(self) -> None:
        # 38 buyer + 22 seller - 8 seller duplicates = 52 canonical values.
        assert len(EventType) == 52


class TestReconciliationCompleteness:
    def test_fixture_counts_match_source_repos(self) -> None:
        assert len(BUYER_EVENT_VALUES) == 38
        assert len(SELLER_EVENT_VALUES) == 22

    @pytest.mark.parametrize("source_value", sorted(set(BUYER_EVENT_VALUES + SELLER_EVENT_VALUES)))
    def test_every_source_value_maps_to_canonical(self, source_value: str) -> None:
        assert isinstance(canonicalize(source_value), EventType)

    def test_aliases_target_canonical_members(self) -> None:
        for legacy, target in LEGACY_ALIASES.items():
            assert isinstance(target, EventType)
            assert legacy not in {member.value for member in EventType}, (
                f"alias key {legacy!r} collides with a canonical value"
            )

    def test_every_canonical_value_comes_from_a_source(self) -> None:
        mapped = {canonicalize(value) for value in BUYER_EVENT_VALUES + SELLER_EVENT_VALUES}
        assert mapped == set(EventType)

    @pytest.mark.parametrize("source_value", sorted(set(BUYER_EVENT_VALUES + SELLER_EVENT_VALUES)))
    def test_reconciliation_doc_covers_every_source_value(self, source_value: str) -> None:
        doc = (REPO_ROOT / "EVENTS_RECONCILIATION.md").read_text()
        assert f"`{source_value}`" in doc, (
            f"{source_value!r} missing from EVENTS_RECONCILIATION.md"
        )


class TestSpecArtifacts:
    """The checked-in JSON (JavaScript Object Notation) Schema files match the models."""

    SPEC_DIR = REPO_ROOT / "spec" / "jsonschema" / "events"

    def test_event_schema_up_to_date(self) -> None:
        checked_in = json.loads((self.SPEC_DIR / "event.schema.json").read_text())
        assert checked_in == build_event_schema(), (
            "stale artifact: run `uv run python spec/jsonschema/events/generate.py`"
        )

    def test_event_type_values_up_to_date(self) -> None:
        checked_in = json.loads((self.SPEC_DIR / "event_type.values.json").read_text())
        assert checked_in == build_event_type_values(), (
            "stale artifact: run `uv run python spec/jsonschema/events/generate.py`"
        )
        assert checked_in["values"] == [member.value for member in EventType]
