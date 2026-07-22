"""Tests for the wire-protocol layer (EP-1.3).

Covers: JSON round-trips for every protocol message; the negotiation
action enum making the historical 422 impossible by construction;
required idempotency keys on every money-mutating request (FD-12); the
FD-6 structured rejection; and the A2A (Agent-to-Agent protocol)
JSON-RPC envelope pinned to ``message/send``.
"""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from iab_agentic_primitives.primitives import (
    Agent,
    BuyerIdentity,
    ChangeType,
    DealType,
    LinearTVParams,
    MakegoodDetails,
    MediaType,
    Money,
    NegotiationAction,
    NegotiationRound,
    NegotiationStatus,
)
from iab_agentic_primitives.protocol import (
    A2A_METHOD_MESSAGE_SEND,
    PRICED_ACTIONS,
    PROTOCOL_MESSAGES,
    TERMINAL_ACTIONS,
    A2AMessage,
    A2APart,
    A2AResult,
    AgentCard,
    AgentDiscoveryRequest,
    AgentTrustVerification,
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
    ChangeRequestCreate,
    ChangeRequestResponse,
    DealBookingRequest,
    DealBookingResponse,
    ErrorCode,
    ErrorDetail,
    ErrorEnvelope,
    IdempotentRequest,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    MessageSendParams,
    NegotiationMessage,
    NegotiationRoundResponse,
    ProductListRequest,
    ProductListResponse,
    QuoteRequest,
    QuoteResponse,
)

from conftest import PRIMITIVE_INSTANCES

# ---------------------------------------------------------------------------
# One representative instance of every protocol message
# ---------------------------------------------------------------------------


def build_negotiation_message() -> NegotiationMessage:
    return NegotiationMessage(
        idempotency_key="idem-neg-1",
        action=NegotiationAction.COUNTER,
        negotiation_id="neg-001",
        round_number=2,
        buyer_price=Money.from_decimal_str("21.00"),
        rationale="meeting you halfway",
    )


PROTOCOL_INSTANCES: dict[str, object] = {
    "ProductListRequest": ProductListRequest(limit=25, offset=50),
    "ProductListResponse": ProductListResponse(
        products=[PRIMITIVE_INSTANCES["Product"]],
        total_count=1,
        limit=25,
        offset=0,
    ),
    "AvailsRequest": AvailsRequest(
        product_id="prod-001",
        start_date=datetime(2026, 8, 1, tzinfo=UTC),
        end_date=datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC),
        requested_impressions=500_000,
        budget=6000.0,
        targeting={"geo": ["US"], "device": ["mobile"]},
    ),
    "AvailsResponse": AvailsResponse(
        product_id="prod-001",
        available_impressions=750_000,
        guaranteed_impressions=500_000,
        estimated_cpm=12.0,
        total_cost=9000.0,
        available_targeting=["device", "geo"],
    ),
    "ProductAvailsSearch": ProductAvailsSearch(
        product_ids=["prod-001", "prod-002"],
        account_id="acct-42",
        advertiser_brand_id="brand-7",
        currency="USD",
        start_date=datetime(2026, 8, 1, tzinfo=UTC),
        end_date=datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC),
        product_targeting=[
            ProductTargeting(
                name=TargetingDimension.INVESTMENT,
                type=TargetingUnit.AUDIENCE,
                datasource="iab-agentic-primitives",
                target="requestedimpressions",
                target_values=["500000"],
                selectable=False,
            )
        ],
    ),
    "Avails": Avails(
        product_id="prod-001",
        account_id="acct-42",
        availability=400_000,
        avails_status=AvailsStatus(
            status=AvailsStatusValue.PARTIALLY_AVAILABLE,
            reason=AvailsStatusReason.BOOKED,
            product_targeting=[
                ProductTargeting(
                    name=TargetingDimension.INVENTORY,
                    type=TargetingUnit.AUDIENCE,
                    datasource="iab-agentic-primitives",
                    target="impressions",
                    target_values=["400000"],
                    selectable=False,
                    count=400_000,
                )
            ],
        ),
        currency="USD",
        price=12.0,
        start_date=datetime(2026, 8, 1, tzinfo=UTC),
        end_date=datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC),
    ),
    "AvailsCollection": AvailsCollection(
        avails=[
            Avails(
                product_id="prod-001",
                account_id="acct-42",
                availability=500_000,
                price=12.0,
                currency="USD",
                start_date=datetime(2026, 8, 1, tzinfo=UTC),
                end_date=datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC),
            )
        ]
    ),
    "QuoteRequest": QuoteRequest(
        idempotency_key="idem-quote-1",
        product_id="prod-001",
        deal_type=DealType.PROGRAMMATIC_GUARANTEED,
        impressions=1_000_000,
        flight_start=date(2026, 8, 1),
        flight_end=date(2026, 8, 31),
        target_cpm=Money.from_decimal_str("20.00"),
        buyer_identity=BuyerIdentity(seat_id="ttd-seat-123"),
        agent_url="https://buyer.example.com/a2a",
        rate_card_id="rc-001",
        media_type=MediaType.LINEAR_TV,
        linear_tv=LinearTVParams(target_demo="A18-49", grps_requested=120),
        audience_plan={"audience_plan_id": "ap-1", "primary": {"type": "standard"}},
    ),
    "QuoteResponse": QuoteResponse(quote=PRIMITIVE_INSTANCES["Quote"]),
    "DealBookingRequest": DealBookingRequest(
        idempotency_key="idem-deal-1",
        quote_id="q-123",
        buyer_identity=BuyerIdentity(seat_id="ttd-seat-123"),
        notes="please confirm by Friday",
        audience_plan={"audience_plan_id": "ap-1"},
    ),
    "DealBookingResponse": DealBookingResponse(
        deal=PRIMITIVE_INSTANCES["Deal"],
        audience_plan_snapshot={"audience_plan_id": "ap-1"},
        audience_match_summary={"primary": {"match": "STRONG", "score": 0.92}},
    ),
    "ChangeRequestCreate": ChangeRequestCreate(
        idempotency_key="idem-cr-1",
        deal_id="deal-001",
        change_type=ChangeType.MAKEGOOD,
        reason="GRP shortfall in week 2",
        makegood=MakegoodDetails(
            shortfall_grps=8.5,
            original_daypart="primetime",
            target_demo="A18-49",
        ),
    ),
    "ChangeRequestResponse": ChangeRequestResponse(
        change_request=PRIMITIVE_INSTANCES["ChangeRequest"]
    ),
    "NegotiationMessage": build_negotiation_message(),
    "NegotiationRoundResponse": NegotiationRoundResponse(
        negotiation_id="neg-001",
        status=NegotiationStatus.ACTIVE,
        round=NegotiationRound(
            round_number=2,
            buyer_price=Money.from_decimal_str("21.00"),
            seller_price=Money.from_decimal_str("22.00"),
            action=NegotiationAction.COUNTER,
            concession_pct=0.04,
            cumulative_concession_pct=0.08,
            rationale="split the gap",
        ),
        rounds_remaining=3,
    ),
    "JsonRpcRequest": JsonRpcRequest(
        id="req-1",
        params=MessageSendParams(
            message=A2AMessage(
                message_id="msg-1",
                role="user",
                parts=[A2APart(kind="text", text="List all available products")],
            ),
            context_id="ctx-1",
        ),
    ),
    "JsonRpcResponse": JsonRpcResponse(
        id="req-1",
        result=A2AResult(
            task_id="task-1",
            context_id="ctx-1",
            parts=[A2APart(kind="data", data={"products": []})],
        ),
    ),
    "AgentDiscoveryRequest": AgentDiscoveryRequest(
        agent_url="https://seller.example.com"
    ),
    "AgentTrustVerification": AgentTrustVerification(
        agent_url="https://seller.example.com",
        agent_id="agent-seller-1",
        trust_status="approved",
        registry_id="iab_aamp",
        verified_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
    ),
    "ErrorEnvelope": ErrorEnvelope(
        detail=ErrorDetail(
            error=ErrorCode.UNSUPPORTED_CAPABILITY,
            message="This seller does not transact linear TV.",
            unsupported=["linear_tv"],
        )
    ),
}


# ---------------------------------------------------------------------------
# Round-trips
# ---------------------------------------------------------------------------


def test_every_protocol_message_has_a_fixture() -> None:
    assert set(PROTOCOL_INSTANCES) == set(PROTOCOL_MESSAGES)


@pytest.mark.parametrize("name", sorted(PROTOCOL_MESSAGES))
def test_json_roundtrip(name: str) -> None:
    instance = PROTOCOL_INSTANCES[name]
    model_cls = PROTOCOL_MESSAGES[name]
    # by_alias=True is required for the A2A envelope (camelCase wire names)
    # and a no-op for every other message.
    payload = instance.model_dump_json(by_alias=True)
    restored = model_cls.model_validate_json(payload)
    assert restored == instance
    assert restored.model_dump_json(by_alias=True) == payload


# ---------------------------------------------------------------------------
# Negotiation: the historical 422 is impossible by construction (FD-5)
# ---------------------------------------------------------------------------


def test_buyer_counter_validates_against_the_sellers_model() -> None:
    """The buyer constructs a counter; the seller validates the SAME model."""
    sent = build_negotiation_message()
    received = NegotiationMessage.model_validate_json(sent.model_dump_json())
    assert received == sent
    assert received.action is NegotiationAction.COUNTER
    assert received.buyer_price == Money.from_decimal_str("21.00")


def test_legacy_bare_price_payload_is_rejected() -> None:
    """The buyer's historical ``{"price": 22.0}`` body cannot validate:
    no action, no typed buyer_price, no negotiation context."""
    with pytest.raises(ValidationError):
        NegotiationMessage.model_validate({"price": 22.0})


def test_action_is_required_no_default() -> None:
    with pytest.raises(ValidationError):
        NegotiationMessage.model_validate(
            {
                "idempotency_key": "k",
                "negotiation_id": "neg-001",
                "buyer_price": {"amount_micros": 21_000_000},
            }
        )
    assert "action" in NegotiationMessage.model_json_schema()["required"]


def test_counter_requires_buyer_price() -> None:
    for action in PRICED_ACTIONS:
        with pytest.raises(ValidationError, match="requires buyer_price"):
            NegotiationMessage(
                idempotency_key="k", action=action, negotiation_id="neg-001"
            )


def test_reject_must_not_carry_a_price() -> None:
    with pytest.raises(ValidationError, match="must not carry buyer_price"):
        NegotiationMessage(
            idempotency_key="k",
            action=NegotiationAction.REJECT,
            negotiation_id="neg-001",
            buyer_price=Money.from_decimal_str("1.00"),
        )


def test_accept_and_reject_are_the_terminal_actions() -> None:
    assert TERMINAL_ACTIONS == {NegotiationAction.ACCEPT, NegotiationAction.REJECT}
    assert NegotiationAction.FINAL_OFFER not in TERMINAL_ACTIONS
    # Walk-away is recordable: a priceless reject is a valid message.
    walk_away = NegotiationMessage(
        idempotency_key="k", action=NegotiationAction.REJECT, negotiation_id="neg-001"
    )
    assert walk_away.action in TERMINAL_ACTIONS


def test_message_requires_a_negotiation_context() -> None:
    with pytest.raises(ValidationError, match="negotiation_id, proposal_id, or quote_id"):
        NegotiationMessage(
            idempotency_key="k",
            action=NegotiationAction.COUNTER,
            buyer_price=Money.from_decimal_str("21.00"),
        )


def test_money_not_float_on_the_wire() -> None:
    """FD-11: a float price does not validate; exact micros do."""
    with pytest.raises(ValidationError):
        NegotiationMessage.model_validate(
            {
                "idempotency_key": "k",
                "action": "counter",
                "negotiation_id": "neg-001",
                "buyer_price": 21.0,
            }
        )


# ---------------------------------------------------------------------------
# Idempotency (FD-12)
# ---------------------------------------------------------------------------

MONEY_MUTATING = [QuoteRequest, DealBookingRequest, NegotiationMessage, ChangeRequestCreate]


@pytest.mark.parametrize("model", MONEY_MUTATING, ids=lambda m: m.__name__)
def test_money_mutating_requests_require_idempotency_key(model: type) -> None:
    assert issubclass(model, IdempotentRequest)
    assert "idempotency_key" in model.model_json_schema()["required"]


def test_idempotency_key_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        DealBookingRequest(idempotency_key="", quote_id="q-123")


def test_omitted_idempotency_key_fails() -> None:
    with pytest.raises(ValidationError):
        DealBookingRequest.model_validate({"quote_id": "q-123"})


# ---------------------------------------------------------------------------
# Quotes: linear TV consistency + FD-6 structured rejection
# ---------------------------------------------------------------------------


def _quote_request(**overrides: object) -> dict:
    body: dict = {
        "idempotency_key": "k",
        "product_id": "prod-001",
        "deal_type": "PD",
    }
    body.update(overrides)
    return body


def test_linear_tv_media_type_requires_params() -> None:
    with pytest.raises(ValidationError, match="requires linear_tv params"):
        QuoteRequest.model_validate(_quote_request(media_type="linear_tv"))


def test_linear_tv_params_require_linear_tv_media_type() -> None:
    with pytest.raises(ValidationError, match="require media_type"):
        QuoteRequest.model_validate(
            _quote_request(linear_tv={"target_demo": "A18-49"})
        )


def test_fd6_rejection_envelope_shape() -> None:
    """The FD-6 example ``unsupported: ["linear_tv"]`` normalizes to the
    typed item list inside the one wrapped ``detail`` shape."""
    envelope = ErrorEnvelope.model_validate(
        {
            "detail": {
                "error": "unsupported_capability",
                "message": "linear TV not supported",
                "unsupported": ["linear_tv"],
            }
        }
    )
    assert envelope.detail.error is ErrorCode.UNSUPPORTED_CAPABILITY
    assert envelope.detail.unsupported[0].capability == "linear_tv"
    # Wire shape is the symmetric quote->book encoding: {"detail": {...}}.
    dumped = envelope.model_dump()
    assert set(dumped) == {"detail"}
    assert dumped["detail"]["error"] == "unsupported_capability"


def test_legacy_audience_plan_unsupported_items_normalize() -> None:
    """The seller's historical ``{"path", "reason"}`` entries stay parseable."""
    detail = ErrorDetail.model_validate(
        {
            "error": "unsupported_capability",
            "unsupported": [
                {"path": "audience_plan.extensions[0]", "reason": "no agentic match"}
            ],
        }
    )
    item = detail.unsupported[0]
    assert item.capability == "audience_plan.extensions[0]"
    assert item.path == "audience_plan.extensions[0]"
    assert item.reason == "no agentic match"


def test_retired_seller_error_codes_do_not_validate() -> None:
    for retired in ("invalid_deal_type", "quote_already_booked", "audience_plan_unsupported"):
        with pytest.raises(ValidationError):
            ErrorDetail.model_validate({"error": retired})


# ---------------------------------------------------------------------------
# A2A: one dialect — message/send
# ---------------------------------------------------------------------------


def test_buyer_spec_dialect_validates() -> None:
    """The buyer's A2A-spec payload (camelCase, message/send) is canonical."""
    request = JsonRpcRequest.model_validate(
        {
            "jsonrpc": "2.0",
            "method": "message/send",
            "id": "req-42",
            "params": {
                "message": {
                    "messageId": "msg-42",
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hello"}],
                },
                "contextId": "ctx-42",
            },
        }
    )
    assert request.method == A2A_METHOD_MESSAGE_SEND
    assert request.params.message.message_id == "msg-42"
    assert request.params.context_id == "ctx-42"


def test_seller_call_dialect_is_rejected_by_construction() -> None:
    """The seller's homegrown ``method: "call"`` dialect cannot validate."""
    with pytest.raises(ValidationError):
        JsonRpcRequest.model_validate(
            {
                "jsonrpc": "2.0",
                "id": "req-1",
                "method": "call",
                "params": {"request": "list products"},
            }
        )


def test_a2a_serializes_camelcase_on_the_wire() -> None:
    payload = PROTOCOL_INSTANCES["JsonRpcRequest"].model_dump(by_alias=True)
    assert payload["params"]["message"]["messageId"] == "msg-1"
    assert payload["params"]["contextId"] == "ctx-1"
    assert "message_id" not in payload["params"]["message"]


def test_jsonrpc_response_requires_exactly_one_of_result_error() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        JsonRpcResponse(id="req-1")
    with pytest.raises(ValidationError, match="exactly one"):
        JsonRpcResponse(
            id="req-1",
            result=A2AResult(),
            error=JsonRpcError(code=-32601, message="Method not found"),
        )


def test_a2a_part_kind_consistency() -> None:
    with pytest.raises(ValidationError, match="requires text"):
        A2APart(kind="text")
    with pytest.raises(ValidationError, match="requires data"):
        A2APart(kind="data")


# ---------------------------------------------------------------------------
# Agent card & change requests
# ---------------------------------------------------------------------------


def test_agent_card_is_the_agent_primitive() -> None:
    assert AgentCard is Agent


def test_makegood_change_type_requires_details() -> None:
    with pytest.raises(ValidationError, match="requires makegood details"):
        ChangeRequestCreate(
            idempotency_key="k", deal_id="deal-001", change_type=ChangeType.MAKEGOOD
        )


def test_change_request_requires_a_target() -> None:
    with pytest.raises(ValidationError, match="order_id or deal_id"):
        ChangeRequestCreate(
            idempotency_key="k", change_type=ChangeType.CANCELLATION
        )


def test_cancellation_rides_change_requests_not_a_deal_subroute() -> None:
    """The retired POST /api/v1/deals/{id}/cancel body maps onto a typed
    ChangeRequestCreate with change_type='cancellation' (FD-6)."""
    cancel = ChangeRequestCreate(
        idempotency_key="k",
        deal_id="deal-001",
        change_type=ChangeType.CANCELLATION,
        reason="campaign paused",
        proposed_values={"cancel_pct": 0.3, "effective_date": "2026-08-15"},
    )
    restored = ChangeRequestCreate.model_validate_json(cancel.model_dump_json())
    assert restored == cancel


# ---------------------------------------------------------------------------
# Unknown-field policy (FD-13) applies to protocol messages too
# ---------------------------------------------------------------------------


def test_unknown_fields_are_ignored() -> None:
    request = ProductListRequest.model_validate(
        {"limit": 10, "offset": 0, "x_vendor_hint": "ignore me", "brand_new_field": 1}
    )
    assert request.limit == 10
    assert not hasattr(request, "x_vendor_hint")
