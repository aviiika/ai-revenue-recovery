"""Case service and audit trail tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.money import Money
from app.db.models import Merchant
from app.domain.audit import service as audit
from app.domain.cases import service as cases
from app.domain.cases.service import DuplicateCaseError, NewCaseInput
from app.domain.cases.state_machine import IllegalTransitionError
from app.domain.enums import (
    ActorType,
    AuditEventType,
    CaseState,
    FailureCategory,
    Recoverability,
    SourceType,
)


def make_input(merchant: Merchant, now: datetime, **overrides: object) -> NewCaseInput:
    defaults: dict[str, object] = {
        "merchant_id": merchant.id,
        "source_type": SourceType.PAYMENT,
        "source_external_id": "pay_test_0001",
        "amount_at_risk": Money.from_rupees("2500.00"),
        "detected_at": now,
        "failure_category": FailureCategory.NETWORK_TIMEOUT,
        "failure_reason_code": "GATEWAY_TIMEOUT",
        "is_synthetic": True,
    }
    defaults.update(overrides)
    return NewCaseInput(**defaults)  # type: ignore[arg-type]


def test_ingest_creates_case_in_new_state(
    session: Session, merchant: Merchant, now: datetime
) -> None:
    case = cases.ingest(session, make_input(merchant, now))
    assert case.current_state == CaseState.NEW
    assert case.amount_at_risk_paise == 250000
    assert case.recovered_amount_paise == 0
    assert case.is_synthetic is True


def test_ingest_applies_deterministic_recoverability(
    session: Session, merchant: Merchant, now: datetime
) -> None:
    """A known reason code maps deterministically -- no model, no LLM."""
    case = cases.ingest(
        session, make_input(merchant, now, failure_category=FailureCategory.MANDATE_REVOKED)
    )
    assert case.recoverability == Recoverability.STRUCTURAL

    transient = cases.ingest(
        session,
        make_input(
            merchant,
            now,
            source_external_id="pay_test_0002",
            failure_category=FailureCategory.NETWORK_TIMEOUT,
        ),
    )
    assert transient.recoverability == Recoverability.TRANSIENT


def test_ingest_writes_an_audit_event(session: Session, merchant: Merchant, now: datetime) -> None:
    case = cases.ingest(session, make_input(merchant, now))
    trail = audit.get_trail(session, case.id)
    assert len(trail) == 1
    assert trail[0].event_type == AuditEventType.CASE_INGESTED
    assert trail[0].sequence == 1
    assert trail[0].after_state == CaseState.NEW


def test_duplicate_source_event_is_refused(
    session: Session, merchant: Merchant, now: datetime
) -> None:
    """Webhook idempotency: re-delivering an event must not create a 2nd case."""
    cases.ingest(session, make_input(merchant, now))
    with pytest.raises(DuplicateCaseError):
        cases.ingest(session, make_input(merchant, now))


def test_transition_records_before_and_after_state(
    session: Session, merchant: Merchant, now: datetime
) -> None:
    case = cases.ingest(session, make_input(merchant, now))
    cases.transition(session, case, CaseState.DIAGNOSED, "reason code mapped to TRANSIENT")

    trail = audit.get_trail(session, case.id)
    assert case.current_state == CaseState.DIAGNOSED
    assert trail[-1].event_type == AuditEventType.STATE_TRANSITIONED
    assert trail[-1].before_state == CaseState.NEW
    assert trail[-1].after_state == CaseState.DIAGNOSED


def test_illegal_transition_is_rejected_and_audited(
    session: Session, merchant: Merchant, now: datetime
) -> None:
    """The *attempt* is recorded even though it is refused -- an operator needs
    to be able to see that something tried to skip the loop."""
    case = cases.ingest(session, make_input(merchant, now))

    with pytest.raises(IllegalTransitionError):
        cases.transition(session, case, CaseState.ACTION_EXECUTED, "skipping the queue")

    assert case.current_state == CaseState.NEW
    trail = audit.get_trail(session, case.id)
    assert trail[-1].event_type == AuditEventType.TRANSITION_REJECTED


def test_audit_sequence_is_monotonic_per_case(
    session: Session, merchant: Merchant, now: datetime
) -> None:
    case = cases.ingest(session, make_input(merchant, now))
    cases.transition(session, case, CaseState.DIAGNOSED, "diagnosed")
    cases.transition(session, case, CaseState.SCORED, "scored")
    cases.transition(session, case, CaseState.ACTION_SELECTED, "action chosen")

    sequences = [event.sequence for event in audit.get_trail(session, case.id)]
    assert sequences == [1, 2, 3, 4]


def test_audit_payload_is_redacted(session: Session, merchant: Merchant, now: datetime) -> None:
    """Secrets and PII must never enter the trail, which humans read in the UI."""
    case = cases.ingest(session, make_input(merchant, now))
    event = audit.record(
        session,
        case_id=case.id,
        event_type=AuditEventType.INTERVENTION_EXECUTED,
        summary="Created payment link",
        payload={
            "razorpay_key_secret": "should-not-persist",
            "email": "someone@example.com",
            "link_id": "plink_synthetic_001",
            "nested": {"authorization": "Bearer abc"},
        },
    )
    assert event.payload["razorpay_key_secret"] == "[REDACTED]"
    assert event.payload["email"] == "[REDACTED]"
    assert event.payload["nested"]["authorization"] == "[REDACTED]"
    # Non-sensitive fields survive, or the trail would be useless.
    assert event.payload["link_id"] == "plink_synthetic_001"


def test_record_recovery_sets_amount_and_terminal_state(
    session: Session, merchant: Merchant, now: datetime
) -> None:
    case = cases.ingest(session, make_input(merchant, now))
    recovered_at = now + timedelta(hours=6)

    cases.record_recovery(session, case, Money.from_rupees("2500.00"), recovered_at)

    assert case.current_state == CaseState.RECOVERED
    assert case.recovered_amount_paise == 250000
    assert case.recovered_at == recovered_at

    event_types = [event.event_type for event in audit.get_trail(session, case.id)]
    assert AuditEventType.RECOVERY_RECORDED in event_types


def test_double_recovery_is_refused(session: Session, merchant: Merchant, now: datetime) -> None:
    """A duplicate webhook must not double-count revenue in the headline figure."""
    case = cases.ingest(session, make_input(merchant, now))
    cases.record_recovery(session, case, Money.from_rupees("2500.00"), now)

    with pytest.raises(IllegalTransitionError, match="already recovered"):
        cases.record_recovery(session, case, Money.from_rupees("2500.00"), now)

    assert case.recovered_amount_paise == 250000


def test_recovery_exceeding_amount_at_risk_is_refused(
    session: Session, merchant: Merchant, now: datetime
) -> None:
    case = cases.ingest(session, make_input(merchant, now))
    with pytest.raises(ValueError, match="exceeds"):
        cases.record_recovery(session, case, Money.from_rupees("9999.00"), now)


def test_human_actor_is_attributed_in_the_trail(
    session: Session, merchant: Merchant, now: datetime
) -> None:
    """Spec FR-8: every human override is recorded with who made it."""
    case = cases.ingest(session, make_input(merchant, now))
    cases.transition(
        session,
        case,
        CaseState.ESCALATED,
        "high value, low confidence",
        actor_type=ActorType.HUMAN,
        actor_id="reviewer@example-ops",
    )
    last = audit.get_trail(session, case.id)[-1]
    assert last.actor_type == ActorType.HUMAN
    assert last.actor_id == "reviewer@example-ops"


def test_get_missing_case_raises(session: Session) -> None:
    with pytest.raises(cases.CaseNotFoundError):
        cases.get(session, uuid.uuid4())
