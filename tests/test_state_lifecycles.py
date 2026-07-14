"""Canonical lifecycle tables: every allowed move accepted, invalid moves
rejected with correct allowed-lists, terminal states immutable."""

import pytest

from iab_agentic_primitives.primitives.lifecycle import (
    ChangeRequestStatus,
    DealStatus,
    OrderStatus,
)
from iab_agentic_primitives.state import (
    CHANGE_REQUEST_LIFECYCLE,
    CHANGE_REQUEST_TERMINAL_STATES,
    DEAL_LIFECYCLE,
    DEAL_TERMINAL_STATES,
    ORDER_LIFECYCLE,
    ORDER_TERMINAL_STATES,
    InvalidTransitionError,
    StateMachine,
)

MACHINES = {
    "deal": DEAL_LIFECYCLE,
    "order": ORDER_LIFECYCLE,
    "change_request": CHANGE_REQUEST_LIFECYCLE,
}

ALL_RULES = [
    pytest.param(machine, rule, id=f"{name}:{rule.from_state.value}->{rule.to_state.value}")
    for name, machine in MACHINES.items()
    for rule in machine.rules
]

ALL_TERMINALS = [
    pytest.param(machine, state, id=f"{name}:{state.value}")
    for name, machine in MACHINES.items()
    for state in machine.terminal
]


@pytest.mark.parametrize(("machine", "rule"), ALL_RULES)
def test_every_canonical_transition_accepted(machine: StateMachine, rule) -> None:
    assert machine.transition(rule.from_state, rule.to_state) is rule.to_state
    assert machine.can_transition(rule.from_state, rule.to_state)
    entry = machine.entry(rule.from_state, rule.to_state)
    assert entry.reason == rule.description


@pytest.mark.parametrize(("machine", "state"), ALL_TERMINALS)
def test_terminal_states_are_immutable(machine: StateMachine, state) -> None:
    assert machine.is_terminal(state)
    assert machine.allowed_from(state) == ()
    for other in machine.states:
        if other is state:
            continue
        with pytest.raises(InvalidTransitionError) as exc_info:
            machine.transition(state, other)
        assert exc_info.value.allowed == ()


class TestVocabularyShape:
    """State/transition counts pinned to the reconciliation audit trail."""

    def test_deal_counts(self) -> None:
        assert len(list(DealStatus)) == 12
        assert len(DEAL_LIFECYCLE.rules) == 27
        assert DEAL_TERMINAL_STATES == {
            DealStatus.COMPLETED,
            DealStatus.REJECTED,
            DealStatus.FAILED,
            DealStatus.CANCELLED,
            DealStatus.EXPIRED,
        }
        assert DEAL_LIFECYCLE.initial is DealStatus.PROPOSED

    def test_order_counts(self) -> None:
        assert len(list(OrderStatus)) == 11
        assert len(ORDER_LIFECYCLE.rules) == 19
        # rejected/failed/unbooked are recoverable, hence NOT terminal.
        assert ORDER_TERMINAL_STATES == {OrderStatus.COMPLETED, OrderStatus.CANCELLED}
        assert ORDER_LIFECYCLE.initial is OrderStatus.DRAFT

    def test_change_request_counts(self) -> None:
        assert len(list(ChangeRequestStatus)) == 7
        assert len(CHANGE_REQUEST_LIFECYCLE.rules) == 8
        assert CHANGE_REQUEST_TERMINAL_STATES == {
            ChangeRequestStatus.APPLIED,
            ChangeRequestStatus.REJECTED,
            ChangeRequestStatus.FAILED,
        }
        assert CHANGE_REQUEST_LIFECYCLE.initial is ChangeRequestStatus.PENDING

    def test_syncing_is_not_a_wire_state(self) -> None:
        # The seller-internal in-flight ad-server sync state was dropped:
        # it is an audit note on in_progress, never a status value.
        assert "syncing" not in {status.value for status in OrderStatus}
        assert OrderStatus.BOOKED in ORDER_LIFECYCLE.allowed_from(OrderStatus.IN_PROGRESS)
        assert OrderStatus.FAILED in ORDER_LIFECYCLE.allowed_from(OrderStatus.IN_PROGRESS)


class TestRepresentativeInvalidTransitions:
    """A representative set of illegal moves, each with the correct allowed[]."""

    @pytest.mark.parametrize(
        ("machine", "current", "attempted", "expected_allowed"),
        [
            # Deal: cannot activate straight from acceptance (must book first).
            (
                DEAL_LIFECYCLE,
                DealStatus.ACCEPTED,
                DealStatus.ACTIVE,
                {DealStatus.BOOKED, DealStatus.FAILED, DealStatus.CANCELLED},
            ),
            # Deal: a booked deal cannot silently reopen negotiation.
            (
                DEAL_LIFECYCLE,
                DealStatus.BOOKED,
                DealStatus.NEGOTIATING,
                {DealStatus.ACTIVE, DealStatus.PARTIALLY_CANCELLED, DealStatus.CANCELLED},
            ),
            # Deal: expiry only applies pre-acceptance.
            (
                DEAL_LIFECYCLE,
                DealStatus.ACTIVE,
                DealStatus.EXPIRED,
                {
                    DealStatus.COMPLETED,
                    DealStatus.MAKEGOOD_PENDING,
                    DealStatus.FAILED,
                    DealStatus.CANCELLED,
                },
            ),
            # Order: cannot skip the submission/approval gate.
            (
                ORDER_LIFECYCLE,
                OrderStatus.DRAFT,
                OrderStatus.BOOKED,
                {OrderStatus.SUBMITTED, OrderStatus.CANCELLED},
            ),
            # Order: cannot book without going through execution.
            (
                ORDER_LIFECYCLE,
                OrderStatus.APPROVED,
                OrderStatus.BOOKED,
                {OrderStatus.IN_PROGRESS, OrderStatus.CANCELLED},
            ),
            # Order: rejected recovers only to draft.
            (
                ORDER_LIFECYCLE,
                OrderStatus.REJECTED,
                OrderStatus.APPROVED,
                {OrderStatus.DRAFT},
            ),
            # ChangeRequest: no approval before validation.
            (
                CHANGE_REQUEST_LIFECYCLE,
                ChangeRequestStatus.PENDING,
                ChangeRequestStatus.APPROVED,
                {ChangeRequestStatus.VALIDATING},
            ),
            # ChangeRequest: cannot apply without approval.
            (
                CHANGE_REQUEST_LIFECYCLE,
                ChangeRequestStatus.PENDING_APPROVAL,
                ChangeRequestStatus.APPLIED,
                {ChangeRequestStatus.APPROVED, ChangeRequestStatus.REJECTED},
            ),
        ],
        ids=lambda value: value.value if hasattr(value, "value") else None,
    )
    def test_invalid_move_rejected_with_allowed_list(
        self, machine, current, attempted, expected_allowed
    ) -> None:
        with pytest.raises(InvalidTransitionError) as exc_info:
            machine.transition(current, attempted)
        err = exc_info.value
        assert err.current is current
        assert err.attempted is attempted
        assert set(err.allowed) == expected_allowed
        assert set(machine.allowed_from(current)) == expected_allowed
