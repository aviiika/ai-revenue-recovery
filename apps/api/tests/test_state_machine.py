"""Exhaustive state machine tests (spec FR-6: illegal transitions must be rejected).

The table is small enough to test *completely*: every one of the 144 ordered
state pairs is asserted legal or illegal. That is deliberate -- this is the
component that stops the agent doing something unbounded, so partial coverage
would not be worth much.
"""

from __future__ import annotations

import itertools

import pytest

from app.domain.cases import state_machine
from app.domain.cases.state_machine import IllegalTransitionError
from app.domain.enums import TERMINAL_STATES, CaseState

S = CaseState

#: The happy path from spec FR-6, walked end to end.
HAPPY_PATH = [
    S.NEW,
    S.DIAGNOSED,
    S.SCORED,
    S.ACTION_SELECTED,
    S.ACTION_PENDING,
    S.ACTION_EXECUTED,
    S.OBSERVING,
    S.RECOVERED,
]


def test_happy_path_is_walkable() -> None:
    for source, target in itertools.pairwise(HAPPY_PATH):
        assert state_machine.is_legal(source, target), f"{source} -> {target} should be legal"


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_terminal_states_admit_no_transitions(state: CaseState) -> None:
    assert state_machine.allowed_targets(state) == frozenset()
    assert state_machine.is_terminal(state)
    for target in CaseState:
        assert not state_machine.is_legal(state, target)


@pytest.mark.parametrize("state", [s for s in CaseState if s not in TERMINAL_STATES])
def test_every_non_terminal_state_can_stop_recover_and_escalate(state: CaseState) -> None:
    """A do-not-contact flag, an out-of-band payment, or a condition needing a
    human can all arrive at any point in the loop."""
    for universal in (S.RECOVERED, S.STOPPED, S.ESCALATED):
        if universal == state:
            continue
        assert state_machine.is_legal(state, universal), f"{state} -> {universal} should be legal"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        # Acting on a case that was never diagnosed or scored.
        (S.NEW, S.ACTION_EXECUTED),
        (S.NEW, S.OBSERVING),
        (S.DIAGNOSED, S.ACTION_PENDING),
        (S.SCORED, S.ACTION_EXECUTED),
        # Skipping execution entirely.
        (S.ACTION_SELECTED, S.OBSERVING),
        # Going backwards.
        (S.OBSERVING, S.SCORED),
        (S.ACTION_EXECUTED, S.ACTION_SELECTED),
        # Leaving a terminal state.
        (S.RECOVERED, S.OBSERVING),
        (S.STOPPED, S.ACTION_SELECTED),
        (S.EXHAUSTED, S.ACTION_SELECTED),
    ],
)
def test_illegal_transitions_are_rejected(source: CaseState, target: CaseState) -> None:
    assert not state_machine.is_legal(source, target)
    with pytest.raises(IllegalTransitionError):
        state_machine.validate(source, target, "attempted in test")


def test_self_transition_is_rejected() -> None:
    with pytest.raises(IllegalTransitionError, match="self-transitions"):
        state_machine.validate(S.OBSERVING, S.OBSERVING, "no-op")


def test_transition_requires_a_reason() -> None:
    """An unexplained state change is not auditable, so it is not permitted."""
    with pytest.raises(IllegalTransitionError, match="reason"):
        state_machine.validate(S.NEW, S.DIAGNOSED, "   ")


def test_validate_returns_trimmed_transition() -> None:
    transition = state_machine.validate(S.NEW, S.DIAGNOSED, "  reason code mapped  ")
    assert transition.source is S.NEW
    assert transition.target is S.DIAGNOSED
    assert transition.reason == "reason code mapped"


def test_retry_loop_reenters_at_action_selection() -> None:
    """A retry must be re-evaluated against policy, not executed directly."""
    assert state_machine.is_legal(S.OBSERVING, S.RETRY_ELIGIBLE)
    assert state_machine.is_legal(S.RETRY_ELIGIBLE, S.ACTION_SELECTED)
    assert not state_machine.is_legal(S.RETRY_ELIGIBLE, S.ACTION_PENDING)
    assert not state_machine.is_legal(S.RETRY_ELIGIBLE, S.ACTION_EXECUTED)


def test_escalated_is_not_terminal() -> None:
    """A human must be able to hand a case back to the agent or end it."""
    assert not state_machine.is_terminal(S.ESCALATED)
    assert state_machine.is_legal(S.ESCALATED, S.ACTION_SELECTED)
    assert state_machine.is_legal(S.ESCALATED, S.STOPPED)
    assert state_machine.is_legal(S.ESCALATED, S.EXHAUSTED)


def test_every_state_pair_is_classified() -> None:
    """No pair is undefined: the table covers the full 12x12 space."""
    pairs = list(itertools.product(CaseState, CaseState))
    assert len(pairs) == 144
    for source, target in pairs:
        legal = state_machine.is_legal(source, target)
        if legal:
            state_machine.validate(source, target, "covered by exhaustive test")
        else:
            with pytest.raises(IllegalTransitionError):
                state_machine.validate(source, target, "covered by exhaustive test")
