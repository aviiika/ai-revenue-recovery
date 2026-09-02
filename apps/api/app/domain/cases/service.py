"""RecoveryCase domain service.

Knows nothing about FastAPI or HTTP (spec: "keep domain logic independent of
FastAPI controllers"). Every state change funnels through :func:`transition`,
which validates against the state machine and writes the audit record in the
same transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.money import Money
from app.db.models import RecoveryCase
from app.domain.audit import service as audit
from app.domain.cases import state_machine
from app.domain.cases.state_machine import IllegalTransitionError
from app.domain.enums import (
    CATEGORY_RECOVERABILITY,
    ActorType,
    AuditEventType,
    CaseState,
    FailureCategory,
    SourceType,
)


class CaseNotFoundError(Exception):
    def __init__(self, case_id: uuid.UUID) -> None:
        self.case_id = case_id
        super().__init__(f"Recovery case {case_id} not found")


class DuplicateCaseError(Exception):
    """Raised when a source event has already been ingested for this merchant."""

    def __init__(self, source_external_id: str) -> None:
        self.source_external_id = source_external_id
        super().__init__(f"A case already exists for source event {source_external_id!r}")


@dataclass(frozen=True, slots=True)
class NewCaseInput:
    """Normalised ingestion payload, provider-agnostic by design.

    Razorpay webhooks, CSV uploads and the synthetic generator all funnel into
    this one shape, so the domain never learns a provider's field names.
    """

    merchant_id: uuid.UUID
    source_type: SourceType
    source_external_id: str
    amount_at_risk: Money
    detected_at: datetime
    failure_category: FailureCategory = FailureCategory.UNKNOWN
    failure_reason_code: str | None = None
    customer_id: uuid.UUID | None = None
    currency: str = "INR"
    do_not_contact: bool = False
    is_synthetic: bool = False
    #: Attempts already made before this case reached us (e.g. gateway retries).
    #: Counts against the merchant's automated attempt budget.
    attempt_count: int = 0
    payment_method: str | None = None
    subscription_age_days: int | None = None


def ingest(session: Session, payload: NewCaseInput) -> RecoveryCase:
    """Create a case in state NEW and record the ingestion event.

    Idempotent by ``(merchant_id, source_external_id)``: re-delivering the same
    provider event raises :class:`DuplicateCaseError` rather than creating a
    second case. Callers that expect duplicates (the webhook handler) treat
    that as success.
    """
    existing = session.execute(
        select(RecoveryCase).where(
            RecoveryCase.merchant_id == payload.merchant_id,
            RecoveryCase.source_external_id == payload.source_external_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise DuplicateCaseError(payload.source_external_id)

    case = RecoveryCase(
        id=uuid.uuid4(),
        merchant_id=payload.merchant_id,
        customer_id=payload.customer_id,
        source_type=payload.source_type,
        source_external_id=payload.source_external_id,
        amount_at_risk_paise=payload.amount_at_risk.paise,
        currency=payload.currency,
        failure_category=payload.failure_category,
        failure_reason_code=payload.failure_reason_code,
        payment_method=payload.payment_method,
        subscription_age_days=payload.subscription_age_days,
        # Deterministic prior from the reason code. An LLM may later enrich the
        # human-readable explanation, but never overrides this mapping.
        recoverability=CATEGORY_RECOVERABILITY[payload.failure_category],
        detected_at=payload.detected_at,
        current_state=CaseState.NEW,
        attempt_count=payload.attempt_count,
        do_not_contact=payload.do_not_contact,
        is_synthetic=payload.is_synthetic,
    )
    session.add(case)
    session.flush()

    audit.record(
        session,
        case_id=case.id,
        event_type=AuditEventType.CASE_INGESTED,
        summary=(
            f"Ingested {payload.source_type} case for {payload.amount_at_risk.format_inr()} "
            f"at risk, reason {payload.failure_category}"
        ),
        after_state=CaseState.NEW,
        payload={
            "source_external_id": payload.source_external_id,
            "amount_at_risk_paise": payload.amount_at_risk.paise,
            "failure_category": str(payload.failure_category),
            "failure_reason_code": payload.failure_reason_code,
            "is_synthetic": payload.is_synthetic,
            "attempt_count": payload.attempt_count,
        },
    )
    return case


def transition(
    session: Session,
    case: RecoveryCase,
    target: CaseState,
    reason: str,
    *,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: str | None = None,
) -> RecoveryCase:
    """Move a case to ``target``, or raise :class:`IllegalTransitionError`.

    A rejected transition is itself audited before the exception propagates:
    an attempt to do something illegal is exactly the kind of thing an operator
    needs to be able to see afterwards.
    """
    source = CaseState(case.current_state)
    try:
        validated = state_machine.validate(source, target, reason)
    except IllegalTransitionError as exc:
        audit.record(
            session,
            case_id=case.id,
            event_type=AuditEventType.TRANSITION_REJECTED,
            summary=f"Rejected transition {source} -> {target}: {exc}",
            actor_type=actor_type,
            actor_id=actor_id,
            before_state=source,
            payload={"attempted_target": str(target), "reason": reason},
        )
        raise

    case.current_state = validated.target
    session.add(case)

    audit.record(
        session,
        case_id=case.id,
        event_type=AuditEventType.STATE_TRANSITIONED,
        summary=f"{validated.source} -> {validated.target}: {validated.reason}",
        actor_type=actor_type,
        actor_id=actor_id,
        before_state=validated.source,
        after_state=validated.target,
        payload={"reason": validated.reason},
    )
    session.flush()
    return case


def record_recovery(
    session: Session,
    case: RecoveryCase,
    amount: Money,
    observed_at: datetime,
    *,
    actor_type: ActorType = ActorType.WEBHOOK,
    actor_id: str | None = None,
) -> RecoveryCase:
    """Record recovered money and move the case to RECOVERED.

    Deterministic and idempotent-friendly: recovering an already-recovered case
    is refused, which is what stops a duplicate webhook from double-counting
    revenue in the headline number.
    """
    if case.current_state == CaseState.RECOVERED:
        raise IllegalTransitionError(
            CaseState.RECOVERED,
            CaseState.RECOVERED,
            "case is already recovered; refusing to record recovery twice",
        )
    if amount.paise > case.amount_at_risk_paise:
        raise ValueError(
            f"Recovered {amount.paise} paise exceeds {case.amount_at_risk_paise} at risk"
        )

    case.recovered_amount_paise = amount.paise
    case.recovered_at = observed_at
    session.add(case)

    audit.record(
        session,
        case_id=case.id,
        event_type=AuditEventType.RECOVERY_RECORDED,
        summary=(
            f"Recovered {amount.format_inr()} of {Money(case.amount_at_risk_paise).format_inr()}"
        ),
        actor_type=actor_type,
        actor_id=actor_id,
        payload={"recovered_amount_paise": amount.paise, "observed_at": observed_at.isoformat()},
    )

    return transition(
        session,
        case,
        CaseState.RECOVERED,
        f"Payment recovered: {amount.format_inr()}",
        actor_type=actor_type,
        actor_id=actor_id,
    )


def get(session: Session, case_id: uuid.UUID) -> RecoveryCase:
    case = session.get(RecoveryCase, case_id)
    if case is None:
        raise CaseNotFoundError(case_id)
    return case


def build_list_query(
    *,
    merchant_id: uuid.UUID | None = None,
    states: list[CaseState] | None = None,
    source_types: list[SourceType] | None = None,
    failure_categories: list[FailureCategory] | None = None,
    min_amount_paise: int | None = None,
    max_amount_paise: int | None = None,
) -> Select[tuple[RecoveryCase]]:
    """Filtered case query backing ``GET /api/v1/cases``.

    Returned as a Select so the API layer can apply paging and a count without
    the domain layer knowing what a page is.
    """
    query = select(RecoveryCase)
    if merchant_id is not None:
        query = query.where(RecoveryCase.merchant_id == merchant_id)
    if states:
        query = query.where(RecoveryCase.current_state.in_(states))
    if source_types:
        query = query.where(RecoveryCase.source_type.in_(source_types))
    if failure_categories:
        query = query.where(RecoveryCase.failure_category.in_(failure_categories))
    if min_amount_paise is not None:
        query = query.where(RecoveryCase.amount_at_risk_paise >= min_amount_paise)
    if max_amount_paise is not None:
        query = query.where(RecoveryCase.amount_at_risk_paise <= max_amount_paise)
    return query
