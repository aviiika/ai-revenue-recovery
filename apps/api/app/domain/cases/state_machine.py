"""The RecoveryCase state machine (spec FR-6).

This module is pure: no database, no FastAPI, no I/O. It is the single
authority on which state changes are legal, so that "illegal transitions must
be rejected" is enforceable in one place and testable exhaustively.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import TERMINAL_STATES, CaseState

S = CaseState

#: The complete legal transition table.
#:
#: Three cross-cutting edges deserve explanation:
#:
#: * ``-> RECOVERED`` is reachable from every non-terminal state, because a
#:   payment can succeed out-of-band at any moment (customer retries on their
#:   own, an old link is paid). Refusing that edge would force us to either drop
#:   a real recovery or record it in the wrong state.
#: * ``-> STOPPED`` is likewise reachable from every non-terminal state: a
#:   do-not-contact flag or an operator kill switch must always be honourable.
#: * ``-> ESCALATED`` is reachable from every non-terminal state, because any
#:   step can hit a condition a human must adjudicate.
#:
#: What is *not* legal is skipping forward (e.g. NEW -> ACTION_EXECUTED, which
#: would mean acting on a case that was never scored) or leaving a terminal
#: state.
_TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    S.NEW: frozenset({S.DIAGNOSED}),
    S.DIAGNOSED: frozenset({S.SCORED}),
    S.SCORED: frozenset({S.ACTION_SELECTED}),
    S.ACTION_SELECTED: frozenset({S.ACTION_PENDING}),
    S.ACTION_PENDING: frozenset({S.ACTION_EXECUTED}),
    S.ACTION_EXECUTED: frozenset({S.OBSERVING}),
    S.OBSERVING: frozenset({S.RETRY_ELIGIBLE, S.EXHAUSTED}),
    # A retry re-enters the loop at action selection: the case must be
    # re-evaluated against policy before it can be acted on again.
    S.RETRY_ELIGIBLE: frozenset({S.ACTION_SELECTED, S.EXHAUSTED}),
    # A human can hand an escalated case back to the agent, or end it.
    S.ESCALATED: frozenset({S.ACTION_SELECTED, S.EXHAUSTED}),
    S.RECOVERED: frozenset(),
    S.EXHAUSTED: frozenset(),
    S.STOPPED: frozenset(),
}

#: Edges permitted from any non-terminal state. See note above.
_UNIVERSAL_TARGETS: frozenset[CaseState] = frozenset({S.RECOVERED, S.STOPPED, S.ESCALATED})


class IllegalTransitionError(Exception):
    """Raised when a caller attempts a transition outside the table."""

    def __init__(self, source: CaseState, target: CaseState, reason: str) -> None:
        self.source = source
        self.target = target
        super().__init__(f"Illegal transition {source} -> {target}: {reason}")


@dataclass(frozen=True, slots=True)
class Transition:
    """A validated state change, ready to be persisted and audited."""

    source: CaseState
    target: CaseState
    reason: str


def allowed_targets(source: CaseState) -> frozenset[CaseState]:
    """Every state legally reachable in one step from ``source``."""
    if source in TERMINAL_STATES:
        return frozenset()
    return _TRANSITIONS[source] | (_UNIVERSAL_TARGETS - {source})


def is_legal(source: CaseState, target: CaseState) -> bool:
    return target in allowed_targets(source)


def validate(source: CaseState, target: CaseState, reason: str) -> Transition:
    """Validate a transition, returning it, or raise :class:`IllegalTransitionError`.

    ``reason`` is mandatory and is carried into the audit trail: a state change
    with no recorded justification is not auditable, which the spec forbids.
    """
    if not reason or not reason.strip():
        raise IllegalTransitionError(source, target, "a non-empty reason is required")

    if source in TERMINAL_STATES:
        raise IllegalTransitionError(
            source, target, f"{source} is terminal and admits no further transitions"
        )

    if source == target:
        raise IllegalTransitionError(source, target, "self-transitions are not state changes")

    if target not in allowed_targets(source):
        legal = ", ".join(sorted(allowed_targets(source))) or "(none)"
        raise IllegalTransitionError(source, target, f"legal targets are: {legal}")

    return Transition(source=source, target=target, reason=reason.strip())


def is_terminal(state: CaseState) -> bool:
    return state in TERMINAL_STATES
