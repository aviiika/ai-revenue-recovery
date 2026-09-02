"""Intervention planning and execution (spec FR-5, FR-7).

Two guarantees this module exists to provide, both of which the spec calls out
explicitly and both of which are easy to get subtly wrong:

**Idempotency.** The intervention row is written *before* the provider is
called, and its ``idempotency_key`` carries a UNIQUE constraint. Re-running an
orchestration pass therefore cannot produce a second action — the database
refuses it — rather than relying on the caller to check first.

**Re-checking eligibility at execution time.** Spec FR-7: "Before executing a
queued action, re-check that the case is still eligible." A decision made at
09:00 may be wrong by 09:05 — the customer may have paid, or opted out. The
queued action is therefore re-validated against the policy engine immediately
before execution and skipped (audibly) if the case has moved on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.money import Money
from app.db.models import Intervention, Merchant, RecoveryCase
from app.domain.audit import service as audit
from app.domain.cases import service as cases
from app.domain.enums import (
    ActorType,
    AuditEventType,
    CaseState,
    ExperimentArm,
    InterventionStatus,
    InterventionStrategy,
)
from app.domain.policies.engine import PolicyDecision
from app.domain.scoring.service import intervention_cost


class InterventionError(Exception):
    """Raised when an intervention cannot be planned or executed."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome of attempting one intervention."""

    intervention: Intervention
    executed: bool
    skipped_reason: str | None = None


class PaymentProvider(Protocol):
    """Allow-listed execution surface (spec section 13).

    Deliberately narrow. The agent can only do what this Protocol exposes, which
    is what keeps the action space bounded no matter what a model suggests. The
    Razorpay adapter implements the same Protocol at Milestone 5.
    """

    @property
    def channel(self) -> str: ...

    def execute(
        self,
        *,
        strategy: InterventionStrategy,
        case: RecoveryCase,
        idempotency_key: str,
    ) -> dict[str, object]: ...


class SimulatedProvider:
    """Simulation-first communication adapter (spec FR-5).

    The MVP does not contact real customers. This records what *would* have been
    sent, so the UI can show the channel and message that a live integration
    would have used, without any outbound traffic.
    """

    @property
    def channel(self) -> str:
        return "SIMULATED"

    def execute(
        self,
        *,
        strategy: InterventionStrategy,
        case: RecoveryCase,
        idempotency_key: str,
    ) -> dict[str, object]:
        templates = {
            InterventionStrategy.WAIT_AND_RETRY: (
                "Silent gateway retry scheduled; no customer contact."
            ),
            InterventionStrategy.SEND_REMINDER_SIMULATED: (
                "Reminder that a payment did not complete, with a link to retry."
            ),
            InterventionStrategy.REQUEST_ALTERNATE_METHOD: (
                "Request to add a different payment method; the saved one can no longer be charged."
            ),
            InterventionStrategy.CREATE_PAYMENT_LINK: (
                "A payment link for the outstanding amount."
            ),
        }
        return {
            "provider": "simulator",
            "channel": self.channel,
            "simulated": True,
            "strategy": str(strategy),
            "would_send": templates.get(strategy, "No customer-facing message."),
            "amount_paise": case.amount_at_risk_paise,
            "idempotency_key": idempotency_key,
        }


def build_idempotency_key(case_id: uuid.UUID, attempt_number: int) -> str:
    """Stable key for one (case, attempt) pair.

    Derived rather than random: replaying the same orchestration pass must
    produce the same key, or the UNIQUE constraint would not catch the repeat.
    """
    return f"case:{case_id}:attempt:{attempt_number}"


def plan(
    session: Session,
    case: RecoveryCase,
    decision: PolicyDecision,
    *,
    now: datetime,
) -> Intervention:
    """Create a PLANNED intervention from a policy decision.

    Returns the existing row if this attempt was already planned, so the caller
    can re-run a pass without branching on "did I already do this?".
    """
    if decision.next_state != CaseState.ACTION_SELECTED:
        raise InterventionError(
            f"Cannot plan an intervention for a decision whose next state is "
            f"{decision.next_state}; only ACTION_SELECTED is actionable"
        )

    attempt_number = case.attempt_count + 1
    key = build_idempotency_key(case.id, attempt_number)

    existing = session.execute(
        select(Intervention).where(Intervention.idempotency_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    intervention = Intervention(
        id=uuid.uuid4(),
        recovery_case_id=case.id,
        strategy=decision.recommended_strategy,
        channel="SIMULATED",
        status=InterventionStatus.PLANNED,
        attempt_number=attempt_number,
        idempotency_key=key,
        planned_at=now,
        estimated_cost_paise=intervention_cost(decision.recommended_strategy).paise,
        policy_snapshot={"applied_rules": decision.applied_rules},
        decision_snapshot=decision.to_audit_payload(),
    )
    session.add(intervention)
    session.flush()
    return intervention


def execute(
    session: Session,
    intervention: Intervention,
    merchant: Merchant,
    provider: PaymentProvider,
    *,
    now: datetime,
    recheck: bool = True,
) -> ExecutionResult:
    """Execute a planned intervention, after re-confirming eligibility.

    The re-check is the point of this function. A queued action carries a
    decision that was correct when it was made; between planning and execution
    the case may have been recovered out of band, opted out, or stopped by an
    operator. Executing anyway would spend money on a case that no longer wants
    it, and in the recovered case would contact someone who has already paid.
    """
    if intervention.status is not InterventionStatus.PLANNED:
        # Already executed or skipped: replaying is a no-op, not an error.
        return ExecutionResult(
            intervention=intervention, executed=False, skipped_reason="already processed"
        )

    case = session.get(RecoveryCase, intervention.recovery_case_id)
    if case is None:
        raise InterventionError(f"Intervention {intervention.id} has no case")

    if recheck:
        reason = _ineligible_reason(case)
        if reason is not None:
            intervention.status = InterventionStatus.SKIPPED
            intervention.result = {"skipped": True, "reason": reason}
            session.add(intervention)
            audit.record(
                session,
                case_id=case.id,
                event_type=AuditEventType.INTERVENTION_SKIPPED,
                summary=f"Skipped {intervention.strategy} at execution time: {reason}",
                payload={
                    "strategy": str(intervention.strategy),
                    "reason": reason,
                    "idempotency_key": intervention.idempotency_key,
                },
            )
            session.flush()
            return ExecutionResult(intervention=intervention, executed=False, skipped_reason=reason)

    try:
        result = provider.execute(
            strategy=InterventionStrategy(intervention.strategy),
            case=case,
            idempotency_key=intervention.idempotency_key,
        )
        intervention.status = InterventionStatus.EXECUTED
        intervention.result = dict(result)
    except (OSError, ValueError, RuntimeError) as exc:
        # A provider failure must not lose the case. The row records the
        # failure and the case stays where it is, so a later pass can retry.
        intervention.status = InterventionStatus.FAILED
        intervention.result = {"error": str(exc), "error_type": type(exc).__name__}
        session.add(intervention)
        audit.record(
            session,
            case_id=case.id,
            event_type=AuditEventType.INTERVENTION_EXECUTED,
            summary=f"Provider failed executing {intervention.strategy}: {exc}",
            payload={"status": "FAILED", "error": str(exc)},
        )
        session.flush()
        return ExecutionResult(intervention=intervention, executed=False, skipped_reason=str(exc))

    intervention.executed_at = now
    intervention.channel = provider.channel
    session.add(intervention)

    # The attempt only counts once it actually happened. Counting at planning
    # time would let a skipped action consume the customer's attempt budget.
    case.attempt_count = intervention.attempt_number
    case.last_action_at = now
    session.add(case)

    audit.record(
        session,
        case_id=case.id,
        event_type=AuditEventType.INTERVENTION_EXECUTED,
        summary=(
            f"Executed {intervention.strategy} via {provider.channel} "
            f"(attempt {intervention.attempt_number})"
        ),
        payload={
            "strategy": str(intervention.strategy),
            "channel": provider.channel,
            "attempt_number": intervention.attempt_number,
            "idempotency_key": intervention.idempotency_key,
            "estimated_cost_paise": intervention.estimated_cost_paise,
            "result": intervention.result,
        },
    )

    cases.transition(
        session,
        case,
        CaseState.ACTION_PENDING,
        f"Intervention {intervention.strategy} queued for execution",
    )
    cases.transition(session, case, CaseState.ACTION_EXECUTED, f"{intervention.strategy} executed")
    cases.transition(session, case, CaseState.OBSERVING, "Awaiting customer response")

    session.flush()
    return ExecutionResult(intervention=intervention, executed=True)


def _ineligible_reason(case: RecoveryCase) -> str | None:
    """Why this case must not be actioned right now, or None if it may be.

    Deliberately a short, blunt list of hard conditions. Nuanced economics are
    the policy engine's job; this is the last-moment safety check.
    """
    if case.current_state == CaseState.RECOVERED:
        return "case was already recovered before the action executed"
    if case.current_state in {CaseState.STOPPED, CaseState.EXHAUSTED}:
        return f"case moved to terminal state {case.current_state}"
    if case.do_not_contact:
        return "customer opted out after the action was planned"
    if case.experiment_arm == ExperimentArm.HOLDOUT:
        return "case is in the holdout arm and must not be contacted"
    if case.recovered_amount_paise > 0:
        return "case already has a recorded recovery"
    return None


def total_cost(session: Session, case_id: uuid.UUID) -> Money:
    """Actual spend on a case: executed interventions only.

    Planned-but-skipped actions cost nothing, and counting them would overstate
    the cost side of every net-recovery figure.
    """
    rows = (
        session.execute(
            select(Intervention.estimated_cost_paise).where(
                Intervention.recovery_case_id == case_id,
                Intervention.status == InterventionStatus.EXECUTED,
            )
        )
        .scalars()
        .all()
    )
    return Money(sum(rows))


def record_human_decision(
    session: Session,
    case: RecoveryCase,
    decision: str,
    reason: str,
    reviewer: str,
) -> None:
    """Audit a human's call on a case (spec: every override is audited)."""
    audit.record(
        session,
        case_id=case.id,
        event_type=AuditEventType.HUMAN_DECISION,
        summary=f"{decision}: {reason}",
        actor_type=ActorType.HUMAN,
        actor_id=reviewer,
        payload={"decision": decision, "reason": reason},
    )
