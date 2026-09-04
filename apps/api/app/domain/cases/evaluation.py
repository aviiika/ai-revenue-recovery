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
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.money import Money
from app.db.models import Customer, Merchant, RecoveryCase
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
from app.domain.scoring.service import (
    CaseFeatures,
    DeterministicScorer,
    RecoveryScorer,
    score_all,
)


class NotEvaluableError(Exception):
    """Raised when a case is in a state the evaluator cannot act on."""


def _as_aware(value: datetime | None) -> datetime | None:
    """Force a stored timestamp to be timezone-aware, assuming UTC.

    SQLite has no timezone type, so ``DateTime(timezone=True)`` round-trips as a
    naive datetime there while PostgreSQL returns an aware one. Comparing the
    two raises ``TypeError``, which would surface as a cooldown crash on one
    backend and not the other.

    Everything is written in UTC, so attaching UTC to a naive value recovers the
    original instant rather than guessing.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _default_scorer() -> RecoveryScorer:
    """Resolve the scorer lazily.

    Imported inside the function so the domain package does not take a hard
    import-time dependency on the ML stack; a deployment without scikit-learn
    installed still imports and runs on the deterministic baseline.
    """
    try:
        from app.ml.scorer import get_scorer

        return get_scorer()
    except ImportError:
        return DeterministicScorer()


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

    # Uses the trained model when its artifact is present, and the
    # deterministic baseline otherwise (spec section 19: model unavailable ->
    # deterministic fallback).
    active_scorer = scorer if scorer is not None else _default_scorer()
    config = PolicyConfig.from_merchant(merchant.policy_config, merchant.high_value_threshold_paise)

    # --- Diagnose -------------------------------------------------------
    if case.current_state == CaseState.NEW:
        diagnose(session, case)

    # --- Score ----------------------------------------------------------
    recoverability = Recoverability(
        case.recoverability or CATEGORY_RECOVERABILITY[FailureCategory(case.failure_category)]
    )
    # Customer history is a model input. A case with no linked customer scores
    # on defaults rather than failing -- an unknown customer is a normal case,
    # not an error.
    customer = session.get(Customer, case.customer_id) if case.customer_id else None
    features = CaseFeatures(
        recoverability=recoverability,
        failure_category=FailureCategory(case.failure_category),
        amount_at_risk=Money(case.amount_at_risk_paise),
        attempt_count=case.attempt_count,
        source_type=str(case.source_type),
        payment_method=case.payment_method,
        subscription_age_days=case.subscription_age_days,
        detected_at=case.detected_at,
        customer_segment=customer.segment if customer else None,
        customer_tenure_days=customer.tenure_days if customer else 0,
        prior_successful_payments=customer.prior_successful_payments if customer else 0,
        prior_failed_payments=customer.prior_failed_payments if customer else 0,
    )
    scored = score_all(active_scorer, features)

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
        last_action_at=_as_aware(case.last_action_at),
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
    # A re-evaluated case can land on the state it is already in (an escalated
    # case that escalates again). That is not a state change, and the state
    # machine rightly refuses a self-transition -- so we simply do not ask.
    if decision.next_state is not None and decision.next_state != case.current_state:
        cases.transition(
            session,
            case,
            decision.next_state,
            decision.explanation,
            actor_type=ActorType.SYSTEM,
        )

    if decision.next_state == CaseState.ESCALATED:
        # An escalation with no queue entry is a case that silently disappears.
        # The explanation is generated here so a reviewer sees prose, not a
        # rule id -- and it degrades to a template when no LLM is configured.
        from app.domain.reviews import service as reviews
        from app.integrations.llm import provider as llm

        written = llm.explain(
            llm.ExplanationContext(
                case_state=CaseState(case.current_state),
                failure_category=FailureCategory(case.failure_category),
                recoverability=recoverability,
                amount_at_risk=Money(case.amount_at_risk_paise),
                attempt_count=case.attempt_count,
                probability=case.recoverability_score,
                recommended_strategy=decision.recommended_strategy,
                applied_rules=decision.applied_rules,
                decisive_rule=decision.decisive_rule,
                policy_explanation=decision.explanation,
                provider_description=case.failure_reason_code,
            ),
            get_settings(),
        )
        reviews.open_review(session, case, decision, now=now, explanation=written.summary)
        audit.record(
            session,
            case_id=case.id,
            event_type=AuditEventType.EXPLANATION_GENERATED,
            summary=f"Explanation written by {written.source}",
            actor_type=ActorType.MODEL,
            actor_id=written.source,
            payload={
                "source": written.source,
                "summary": written.summary,
                "evidence": written.evidence,
                "uncertainty": written.uncertainty,
            },
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
