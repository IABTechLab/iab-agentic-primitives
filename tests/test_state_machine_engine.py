"""Engine tests: the generic StateMachine used by all three lifecycles."""

from datetime import UTC, datetime, timedelta, timezone
from enum import Enum

import pytest

from iab_agentic_primitives.state import (
    CHANGE_REQUEST_LIFECYCLE,
    DEAL_LIFECYCLE,
    ORDER_LIFECYCLE,
    AuditEntry,
    InvalidTransitionError,
    StateMachine,
    TransitionRule,
)


class Toy(str, Enum):
    START = "start"
    MIDDLE = "middle"
    DONE = "done"
    ABORTED = "aborted"


def build_toy(guard=None) -> StateMachine[Toy]:
    return StateMachine(
        name="toy",
        states=Toy,
        initial=Toy.START,
        terminal=frozenset({Toy.DONE, Toy.ABORTED}),
        rules=[
            TransitionRule(Toy.START, Toy.MIDDLE, "go", guard=guard),
            TransitionRule(Toy.MIDDLE, Toy.DONE, "finish"),
            TransitionRule(Toy.MIDDLE, Toy.ABORTED, "abort"),
            TransitionRule(Toy.START, Toy.ABORTED, "abort early"),
        ],
    )


class TestEngine:
    def test_transition_is_pure_and_returns_target(self) -> None:
        machine = build_toy()
        assert machine.transition(Toy.START, Toy.MIDDLE) is Toy.MIDDLE
        # No mutable current-state: the same call is repeatable.
        assert machine.transition(Toy.START, Toy.MIDDLE) is Toy.MIDDLE

    def test_invalid_transition_carries_current_attempted_allowed(self) -> None:
        machine = build_toy()
        with pytest.raises(InvalidTransitionError) as exc_info:
            machine.transition(Toy.START, Toy.DONE)
        err = exc_info.value
        assert err.machine == "toy"
        assert err.current is Toy.START
        assert err.attempted is Toy.DONE
        assert set(err.allowed) == {Toy.MIDDLE, Toy.ABORTED}
        payload = err.as_dict()
        assert payload["error"] == "invalid_transition"
        assert payload["current"] == "start"
        assert payload["attempted"] == "done"
        assert set(payload["allowed"]) == {"middle", "aborted"}

    def test_guard_rejection_raises_with_allowed_list(self) -> None:
        machine = build_toy(guard=lambda cur, to, ctx: ctx.get("ok", False))
        assert machine.transition(Toy.START, Toy.MIDDLE, {"ok": True}) is Toy.MIDDLE
        assert machine.can_transition(Toy.START, Toy.MIDDLE, {"ok": True})
        assert not machine.can_transition(Toy.START, Toy.MIDDLE)
        with pytest.raises(InvalidTransitionError) as exc_info:
            machine.transition(Toy.START, Toy.MIDDLE)
        assert exc_info.value.reason == "guard condition failed"
        assert Toy.MIDDLE in exc_info.value.allowed

    def test_entry_builds_audit_record_with_rule_description(self) -> None:
        machine = build_toy()
        entry = machine.entry(Toy.START, Toy.MIDDLE, actor="agent:buyer-1")
        assert entry.machine == "toy"
        assert entry.from_state == "start"
        assert entry.to_state == "middle"
        assert entry.actor == "agent:buyer-1"
        assert entry.reason == "go"  # defaults to the rule description
        assert entry.occurred_at.tzinfo is not None

    def test_entry_rejects_invalid_move(self) -> None:
        machine = build_toy()
        with pytest.raises(InvalidTransitionError):
            machine.entry(Toy.DONE, Toy.START)

    def test_audit_entry_requires_tz_aware_and_normalizes_to_utc(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            AuditEntry(
                machine="toy", from_state="a", to_state="b", occurred_at=datetime(2026, 1, 1)
            )
        aware = datetime(2026, 1, 1, 12, tzinfo=UTC).astimezone(timezone(timedelta(hours=-5)))
        entry = AuditEntry(machine="toy", from_state="a", to_state="b", occurred_at=aware)
        assert entry.occurred_at.tzinfo == UTC
        assert entry.occurred_at == aware

    def test_can_reach(self) -> None:
        machine = build_toy()
        assert machine.can_reach(Toy.START, Toy.DONE)
        assert not machine.can_reach(Toy.DONE, Toy.START)
        assert not machine.can_reach(Toy.DONE, Toy.DONE)  # terminal, no self-loop

    def test_rework_edges_are_legal_moves_but_not_progress(self) -> None:
        machine = StateMachine(
            name="loop",
            states=Toy,
            initial=Toy.START,
            terminal=frozenset({Toy.DONE, Toy.ABORTED}),
            rules=[
                TransitionRule(Toy.START, Toy.MIDDLE, "go"),
                TransitionRule(Toy.MIDDLE, Toy.START, "retry", rework=True),
                TransitionRule(Toy.MIDDLE, Toy.DONE, "finish"),
                TransitionRule(Toy.MIDDLE, Toy.ABORTED, "abort"),
            ],
        )
        # The rework edge is a perfectly legal transition...
        assert machine.transition(Toy.MIDDLE, Toy.START) is Toy.START
        assert machine.can_reach(Toy.MIDDLE, Toy.START)
        # ...but does not count as forward progress.
        assert not machine.can_progress_to(Toy.MIDDLE, Toy.START)
        assert machine.can_progress_to(Toy.START, Toy.DONE)


class TestConstructionValidation:
    def test_terminal_state_with_outgoing_edge_rejected(self) -> None:
        with pytest.raises(ValueError, match="terminal"):
            StateMachine(
                name="bad",
                states=Toy,
                initial=Toy.START,
                terminal=frozenset({Toy.DONE, Toy.ABORTED}),
                rules=[
                    TransitionRule(Toy.START, Toy.MIDDLE),
                    TransitionRule(Toy.MIDDLE, Toy.DONE),
                    TransitionRule(Toy.MIDDLE, Toy.ABORTED),
                    TransitionRule(Toy.DONE, Toy.START),
                ],
            )

    def test_duplicate_rule_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            StateMachine(
                name="bad",
                states=Toy,
                initial=Toy.START,
                terminal=frozenset({Toy.DONE, Toy.ABORTED}),
                rules=[
                    TransitionRule(Toy.START, Toy.MIDDLE),
                    TransitionRule(Toy.START, Toy.MIDDLE),
                    TransitionRule(Toy.MIDDLE, Toy.DONE),
                    TransitionRule(Toy.MIDDLE, Toy.ABORTED),
                ],
            )

    def test_unreachable_state_rejected(self) -> None:
        with pytest.raises(ValueError, match="unreachable"):
            StateMachine(
                name="bad",
                states=Toy,
                initial=Toy.START,
                terminal=frozenset({Toy.DONE, Toy.ABORTED}),
                rules=[
                    TransitionRule(Toy.START, Toy.DONE),
                    TransitionRule(Toy.START, Toy.ABORTED),
                ],  # MIDDLE unreachable
            )

    def test_unflagged_progress_cycle_rejected(self) -> None:
        with pytest.raises(ValueError, match="forward-progress cycle"):
            StateMachine(
                name="bad",
                states=Toy,
                initial=Toy.START,
                terminal=frozenset({Toy.DONE, Toy.ABORTED}),
                rules=[
                    TransitionRule(Toy.START, Toy.MIDDLE),
                    TransitionRule(Toy.MIDDLE, Toy.START),  # cycle, not marked rework
                    TransitionRule(Toy.MIDDLE, Toy.DONE),
                    TransitionRule(Toy.MIDDLE, Toy.ABORTED),
                ],
            )

    def test_initial_cannot_be_terminal(self) -> None:
        with pytest.raises(ValueError, match="initial"):
            StateMachine(
                name="bad",
                states=Toy,
                initial=Toy.DONE,
                terminal=frozenset({Toy.DONE}),
                rules=[],
            )


class TestGenericness:
    """The one engine drives all three canonical lifecycles."""

    @pytest.mark.parametrize(
        "machine", [DEAL_LIFECYCLE, ORDER_LIFECYCLE, CHANGE_REQUEST_LIFECYCLE], ids=lambda m: m.name
    )
    def test_lifecycles_are_engine_instances(self, machine: StateMachine) -> None:
        assert type(machine) is StateMachine
        assert machine.rules  # non-empty explicit table
        # Every lifecycle answers the same query surface.
        assert machine.allowed_from(machine.initial)
        assert not machine.is_terminal(machine.initial)
        spec = machine.to_spec_dict()
        assert spec["initial"] == machine.initial.value
        assert len(spec["transitions"]) == len(machine.rules)
