"""End-to-end evaluation loop tests.

Proves that diagnose -> score -> policy -> transition runs as one auditable unit,
and that the resulting audit trail is sufficient to reconstruct the decision --
which is the spec's actual bar (section 20), not merely "it ran".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.money import Money
from app.db.models import Merchant
from app.domain.audit import service as audit
from app.domain.cases import evaluation
from app.domain.cases import service as cases
from app.domain.cases.service import NewCaseInput
from app.domain.enums import (
    ActorType,
    AuditEventType,
    CaseState,
    FailureCategory,
    InterventionStrategy,
    Recoverability,
    SourceType,
)
from app.domain.scoring.service import DeterministicScorer

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def make_case(
    session: Session,
    merchant: Merchant,
    *,
    external_id: str = "pay_eval_0001",
    rupees: str = "5000.00",
    category: FailureCategory = FailureCategory.NETWORK_TIMEOUT,
    do_not_contact: bool = False,
):
    return cases.ingest(
        session,
        NewCaseInput(
            merchant_id=merchant.id,
            source_type=SourceType.PAYMENT,
            source_external_id=external_id,
            amount_at_risk=Money.from_rupees(rupees),
            detected_at=NOW,
            failure_category=category,
            do_not_contact=do_not_contact,
            is_synthetic=True,
        ),
    )


def test_evaluation_walks_new_case_to_action_selected(session: Session, merchant: Merchant) -> None:
    case = make_case(session, merchant)
    result = evaluation.evaluate(session, case, merchant, now=NOW)

    assert case.current_state == CaseState.ACTION_SELECTED
    assert result.decision.recommended_strategy is InterventionStrategy.WAIT_AND_RETRY
    assert not result.decision.requires_human


def test_evaluation_records_a_reconstructable_trail(session: Session, merchant: Merchant) -> None:
    """Spec section 20: model version, probability, policies fired and the
    chosen action must all be recoverable from the trail alone."""
    case = make_case(session, merchant)
    evaluation.evaluate(session, case, merchant, now=NOW, scorer=DeterministicScorer())

    trail = audit.get_trail(session, case.id)
    event_types = [e.event_type for e in trail]

    for required in (
        AuditEventType.CASE_INGESTED,
        AuditEventType.CASE_DIAGNOSED,
        AuditEventType.CASE_SCORED,
        AuditEventType.POLICY_EVALUATED,
        AuditEventType.INTERVENTION_SELECTED,
    ):
        assert required in event_types, f"{required} missing from audit trail"

    # Sequence must remain strictly monotonic across the whole loop.
    assert [e.sequence for e in trail] == list(range(1, len(trail) + 1))

    scored_event = next(e for e in trail if e.event_type == AuditEventType.CASE_SCORED)
    assert scored_event.actor_type == ActorType.MODEL
    assert scored_event.actor_id == "deterministic-baseline-v1"
    assert scored_event.payload["candidates"]

    policy_event = next(e for e in trail if e.event_type == AuditEventType.POLICY_EVALUATED)
    assert policy_event.payload["applied_rules"]
    assert policy_event.payload["recommended_strategy"]


def test_diagnosis_sets_recoverability_deterministically(
    session: Session, merchant: Merchant
) -> None:
    case = make_case(session, merchant, category=FailureCategory.MANDATE_REVOKED)
    evaluation.evaluate(session, case, merchant, now=NOW)
    assert case.recoverability == Recoverability.STRUCTURAL


def test_scoring_persists_model_outputs_on_the_case(session: Session, merchant: Merchant) -> None:
    case = make_case(session, merchant)
    evaluation.evaluate(session, case, merchant, now=NOW, scorer=DeterministicScorer())

    assert case.model_version == "deterministic-baseline-v1"
    assert case.recoverability_score is not None
    assert 0.0 < case.recoverability_score < 1.0
    # Priority is expected net value in paise: it ranks the queue by money.
    assert case.priority_score is not None


def test_do_not_contact_case_is_stopped_by_the_loop(session: Session, merchant: Merchant) -> None:
    case = make_case(session, merchant, do_not_contact=True)
    result = evaluation.evaluate(session, case, merchant, now=NOW)

    assert case.current_state == CaseState.STOPPED
    assert result.decision.decisive_rule == "R02_DO_NOT_CONTACT"


def test_tiny_case_is_stopped_on_negative_expected_value(
    session: Session, merchant: Merchant
) -> None:
    case = make_case(session, merchant, rupees="1.00", category=FailureCategory.MANDATE_REVOKED)
    result = evaluation.evaluate(session, case, merchant, now=NOW)

    assert case.current_state == CaseState.STOPPED
    assert result.decision.decisive_rule == "R07_NON_POSITIVE_EXPECTED_VALUE"


def test_high_value_low_confidence_case_escalates(session: Session, merchant: Merchant) -> None:
    # Merchant threshold is INR 25,000; a structural failure scores low.
    case = make_case(session, merchant, rupees="90000.00", category=FailureCategory.MANDATE_REVOKED)
    result = evaluation.evaluate(session, case, merchant, now=NOW)

    assert case.current_state == CaseState.ESCALATED
    assert result.decision.requires_human


def test_terminal_case_cannot_be_evaluated(session: Session, merchant: Merchant) -> None:
    case = make_case(session, merchant)
    evaluation.stop(session, case, "operator halted the campaign")
    assert case.current_state == CaseState.STOPPED

    with pytest.raises(evaluation.NotEvaluableError):
        evaluation.evaluate(session, case, merchant, now=NOW)


def test_manual_stop_is_attributed_in_the_trail(session: Session, merchant: Merchant) -> None:
    case = make_case(session, merchant)
    evaluation.stop(
        session, case, "customer complained", actor_type=ActorType.HUMAN, actor_id="ops@example"
    )

    trail = audit.get_trail(session, case.id)
    decision_event = next(e for e in trail if e.event_type == AuditEventType.HUMAN_DECISION)
    assert decision_event.actor_type == ActorType.HUMAN
    assert decision_event.actor_id == "ops@example"
    assert case.current_state == CaseState.STOPPED


def test_cooldown_defers_a_recently_actioned_case(session: Session, merchant: Merchant) -> None:
    case = make_case(session, merchant)
    case.last_action_at = NOW - timedelta(hours=1)
    session.add(case)
    session.flush()

    result = evaluation.evaluate(session, case, merchant, now=NOW)

    # Deferral changes nothing about the case: it stays scored and will be
    # picked up again once the cooldown window expires.
    assert result.decision.is_deferred
    assert result.decision.next_state is None
    assert case.current_state == CaseState.SCORED
    assert result.decision.retry_after == NOW + timedelta(hours=23)

    # The deferral itself is still auditable.
    policy_event = next(
        e
        for e in reversed(audit.get_trail(session, case.id))
        if e.event_type == AuditEventType.POLICY_EVALUATED
    )
    assert policy_event.payload["deferred"] is True
    assert policy_event.payload["decisive_rule"] == "R08_COOLDOWN_ACTIVE"


def test_attempt_cap_escalates_through_the_loop(session: Session, merchant: Merchant) -> None:
    case = make_case(session, merchant)
    case.attempt_count = 3
    session.add(case)
    session.flush()

    result = evaluation.evaluate(session, case, merchant, now=NOW)
    assert case.current_state == CaseState.ESCALATED
    assert result.decision.decisive_rule == "R04_MAX_ATTEMPTS_REACHED"


def test_evaluation_is_deterministic_for_identical_cases(
    session: Session, merchant: Merchant
) -> None:
    """Same inputs, same decision -- the demo must be reproducible."""
    first = make_case(session, merchant, external_id="pay_det_a")
    second = make_case(session, merchant, external_id="pay_det_b")

    a = evaluation.evaluate(session, first, merchant, now=NOW)
    b = evaluation.evaluate(session, second, merchant, now=NOW)

    assert a.decision.recommended_strategy == b.decision.recommended_strategy
    assert a.decision.expected_net.paise == b.decision.expected_net.paise
    assert a.decision.applied_rules == b.decision.applied_rules


def test_prior_attempts_are_carried_from_ingestion(session: Session, merchant: Merchant) -> None:
    """A case that arrives having already burned its budget must escalate, not act."""
    case = cases.ingest(
        session,
        NewCaseInput(
            merchant_id=merchant.id,
            source_type=SourceType.PAYMENT,
            source_external_id="pay_exhausted_001",
            amount_at_risk=Money.from_rupees("5000.00"),
            detected_at=NOW,
            failure_category=FailureCategory.NETWORK_TIMEOUT,
            attempt_count=3,
            is_synthetic=True,
        ),
    )
    assert case.attempt_count == 3

    result = evaluation.evaluate(session, case, merchant, now=NOW)
    assert case.current_state == CaseState.ESCALATED
    assert result.decision.decisive_rule == "R04_MAX_ATTEMPTS_REACHED"
