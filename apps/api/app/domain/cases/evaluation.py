"""Case evaluation: the agent loop from ingestion to a selected action.

Implements the pseudo-flow in spec section 11, with the guardrails wired in:

    diagnose -> score -> policy_engine.decide -> transition

Each step transitions the case and writes an audit event, inside one
transaction, so a case can never end up scored-but-unaudited.

The LLM is absent from this path by design. When it arrives (Milestone 6) it will
enrich :attr:`PolicyDecision.explanation` for human readers -- it will not choose
the action, because the action is already chosen by the time an explanation is
needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.money import Money
from app.db.models import Merchant, RecoveryCase
from app.domain.audit import service as audit
from app.domain.cases import service as cases
from app.domain.enums import (
    CATEGORY_RECOVERABILITY,
    ActorType,
    AuditEventType,
    CaseState,
    FailureCategory,
    Recoverability,
)
from app.domain.policies.config import PolicyConfig
from app.domain.policies.engine import CaseSnapshot, PolicyDecision, decide
from app.domain.scoring.service import DeterministicScorer, RecoveryScorer, score_all


class NotEvaluableError(Exception):
    """Raised when a case is in a state the evaluator cannot act on."""


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    case: RecoveryCase
    decision: PolicyDecision
    model_version: str


def diagnose(session: Session, case: RecoveryCase) -> RecoveryCase:
    """Attach a deterministic recoverability class and move to DIAGNOSED.

    Spec FR-2 prefers deterministic mappings where the reason code is known, and
    it is known for every case we ingest. An LLM would add narrative here, not
    a different answer.
    """
    category = FailureCategory(case.failure_category)
    recoverability = CATEGORY_RECOVERABILITY[category]
    case.recoverability = recoverability
    session.add(case)

    audit.record(
        session,
        case_id=case.id,
        event_type=AuditEventType.CASE_DIAGNOSED,
        summary=f"Diagnosed {category} as {recoverability} (deterministic reason-code mapping)",
        payload={
            "failure_category": str(category),
            "failure_reason_code": case.failure_reason_code,
            "recoverability": str(recoverability),
            "method": "deterministic_reason_code_mapping",
        },
    )
    return cases.transition(
        session, case, CaseState.DIAGNOSED, f"Reason code maps to {recoverability}"
    )


def evaluate(
    session: Session,
    case: RecoveryCase,
    merchant: Merchant,
    *,
    now: datetime,
    scorer: RecoveryScorer | None = None,
) -> EvaluationResult:
    """Run the full loop for one case and apply the resulting transition.

    ``now`` is injected rather than read from the clock so that cooldown and
    backoff behaviour is deterministically testable.
    """
    if case.current_state in {CaseState.STOPPED, CaseState.EXHAUSTED, CaseState.RECOVERED}:
        raise NotEvaluableError(
            f"Case {case.id} is in terminal state {case.current_state} and cannot be evaluated"
        )

    active_scorer = scorer or DeterministicScorer()
    config = PolicyConfig.from_merchant(merchant.policy_config, merchant.high_value_threshold_paise)

    # --- Diagnose -------------------------------------------------------
    if case.current_state == CaseState.NEW:
        diagnose(session, case)

    # --- Score ----------------------------------------------------------
    recoverability = Recoverability(
        case.recoverability or CATEGORY_RECOVERABILITY[FailureCategory(case.failure_category)]
    )
    scored = score_all(
        active_scorer,
        failure_category=FailureCategory(case.failure_category),
        amount_at_risk=Money(case.amount_at_risk_paise),
        attempt_count=case.attempt_count,
        recoverability=recoverability,
    )

    best = scored[0] if scored else None
    case.recoverability_score = float(best.probability) if best else None
    case.model_version = active_scorer.model_version
    # Priority ranks the queue by money, not by probability: a 30% chance on a
    # large ticket outranks a 90% chance on a trivial one.
    case.priority_score = float(best.expected_net.paise) if best else 0.0
    session.add(case)

    audit.record(
        session,
        case_id=case.id,
        event_type=AuditEventType.CASE_SCORED,
        summary=(
            f"Scored {len(scored)} candidate strategies; best is {best.strategy} "
            f"at {best.probability:.2f}"
            if best
            else "No candidate strategies to score"
        ),
        actor_type=ActorType.MODEL,
        actor_id=active_scorer.model_version,
        payload={
            "model_version": active_scorer.model_version,
            "recoverability": str(recoverability),
            "attempt_count": case.attempt_count,
            "candidates": [
                {
                    "strategy": str(s.strategy),
                    "probability": str(s.probability),
                    "expected_gross_paise": s.expected_gross.paise,
                    "cost_paise": s.cost.paise,
                    "expected_net_paise": s.expected_net.paise,
                }
                for s in scored
            ],
        },
    )

    if case.current_state == CaseState.DIAGNOSED:
        cases.transition(session, case, CaseState.SCORED, "Candidate strategies scored")

    # --- Policy ---------------------------------------------------------
    snapshot = CaseSnapshot(
        state=CaseState(case.current_state),
        amount_at_risk=Money(case.amount_at_risk_paise),
        recovered_amount=Money(case.recovered_amount_paise),
        recoverability=recoverability,
        attempt_count=case.attempt_count,
        do_not_contact=case.do_not_contact,
        last_action_at=case.last_action_at,
        evaluated_at=now,
    )
    decision = decide(snapshot, scored, config)

    audit.record(
        session,
        case_id=case.id,
        event_type=AuditEventType.POLICY_EVALUATED,
        summary=(f"Policy selected {decision.recommended_strategy}: {decision.explanation}"),
        payload=decision.to_audit_payload(),
    )

    # --- Apply ----------------------------------------------------------
    # A deferred decision (cooldown) deliberately changes nothing: the case stays
    # exactly where it is and will be picked up again once the window expires.
    # The POLICY_EVALUATED event above is still recorded, so the deferral itself
    # is auditable.
    if decision.next_state is not None:
        cases.transition(
            session,
            case,
            decision.next_state,
            decision.explanation,
            actor_type=ActorType.SYSTEM,
        )

    if decision.next_state == CaseState.ACTION_SELECTED:
        audit.record(
            session,
            case_id=case.id,
            event_type=AuditEventType.INTERVENTION_SELECTED,
            summary=f"Selected {decision.recommended_strategy}",
            payload={
                "strategy": str(decision.recommended_strategy),
                "expected_net_paise": decision.expected_net.paise,
                "applied_rules": decision.applied_rules,
            },
        )

    session.flush()
    return EvaluationResult(case=case, decision=decision, model_version=active_scorer.model_version)


def stop(
    session: Session,
    case: RecoveryCase,
    reason: str,
    *,
    actor_type: ActorType = ActorType.HUMAN,
    actor_id: str | None = None,
) -> RecoveryCase:
    """Stop a case manually. The operator kill switch (spec section 'Human control')."""
    audit.record(
        session,
        case_id=case.id,
        event_type=AuditEventType.HUMAN_DECISION,
        summary=f"Manual stop requested: {reason}",
        actor_type=actor_type,
        actor_id=actor_id,
        payload={"decision": "STOP", "reason": reason},
    )
    return cases.transition(
        session, case, CaseState.STOPPED, reason, actor_type=actor_type, actor_id=actor_id
    )
