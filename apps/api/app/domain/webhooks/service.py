"""Applying a verified provider event to a case.

Kept apart from the HTTP handler so the mapping from provider vocabulary to
domain action is testable without a request, and so the handler stays short
enough to comfortably return inside Razorpay's 5-second budget.

Recovery events funnel into the same ``cases.record_recovery`` the simulator
uses. There is deliberately no separate write path for "real" money.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.money import Money
from app.db.models import Intervention, Merchant, RecoveryCase, RecoveryOutcome, WebhookEvent
from app.domain.audit import service as audit
from app.domain.cases import service as cases
from app.domain.cases.service import DuplicateCaseError, NewCaseInput
from app.domain.enums import (
    ActorType,
    AuditEventType,
    CaseState,
    OutcomeType,
    SourceType,
    WebhookStatus,
)
from app.integrations.razorpay import webhooks as razorpay

logger = logging.getLogger(__name__)


def process(
    session: Session,
    record: WebhookEvent,
    body: dict[str, Any],
    *,
    now: datetime,
) -> str:
    """Interpret one verified event. Returns a short description of what it did.

    Never raises for an unrecognised event: the failure is recorded on the
    webhook row and a 2xx still goes back, because a non-2xx would make
    Razorpay retry for 24 hours something we are never going to handle.
    """
    parsed = razorpay.parse_event(body)

    if not parsed.is_handled:
        record.processing_status = WebhookStatus.IGNORED
        record.processed_at = now
        session.add(record)
        return f"ignored: {parsed.event_type or 'unknown event'}"

    try:
        if parsed.is_recovery:
            action = _apply_recovery(session, record, parsed, now=now)
        else:
            action = _apply_failure(session, record, parsed, now=now)
        record.processing_status = WebhookStatus.PROCESSED
    except (ValueError, LookupError) as exc:
        # Narrow and named. The event stays on record with its error so it can
        # be inspected and replayed rather than vanishing.
        logger.warning(
            "webhook_processing_failed",
            extra={"event_id": record.event_id, "error": str(exc)},
        )
        record.processing_status = WebhookStatus.FAILED
        record.processing_error = str(exc)
        action = f"failed: {exc}"

    record.processed_at = now
    session.add(record)
    session.flush()
    return action


def _find_case(session: Session, parsed: razorpay.ParsedEvent) -> RecoveryCase | None:
    """Locate the case an event belongs to.

    Two routes, in order of reliability:

    1. ``reference_id`` — we set this to the intervention's idempotency key when
       creating a payment link, so a ``payment_link.paid`` ties back to the exact
       attempt that caused it.
    2. ``source_external_id`` — for events about the original payment or
       subscription that created the case.
    """
    if parsed.reference_id:
        intervention = session.execute(
            select(Intervention).where(Intervention.idempotency_key == parsed.reference_id)
        ).scalar_one_or_none()
        if intervention is not None:
            return session.get(RecoveryCase, intervention.recovery_case_id)

    if parsed.external_id:
        return session.execute(
            select(RecoveryCase).where(RecoveryCase.source_external_id == parsed.external_id)
        ).scalar_one_or_none()

    return None


def _apply_recovery(
    session: Session,
    record: WebhookEvent,
    parsed: razorpay.ParsedEvent,
    *,
    now: datetime,
) -> str:
    """Money arrived. Record it against the case, once."""
    case = _find_case(session, parsed)
    if case is None:
        raise LookupError(
            f"No case matches {parsed.event_type} "
            f"(reference_id={parsed.reference_id}, external_id={parsed.external_id})"
        )

    record.recovery_case_id = case.id

    if case.current_state == CaseState.RECOVERED:
        # Already settled. A second recovery event for the same case must not
        # add to the total -- the headline figure has to survive redelivery.
        return f"already recovered; no change to case {case.id}"

    # Razorpay amounts are in paise, matching our storage unit exactly.
    amount = Money(min(parsed.amount_paise or 0, case.amount_at_risk_paise))
    if amount.paise <= 0:
        raise ValueError(f"{parsed.event_type} carried no usable amount")

    intervention = None
    if parsed.reference_id:
        intervention = session.execute(
            select(Intervention).where(Intervention.idempotency_key == parsed.reference_id)
        ).scalar_one_or_none()

    session.add(
        RecoveryOutcome(
            id=uuid.uuid4(),
            recovery_case_id=case.id,
            intervention_id=intervention.id if intervention else None,
            outcome_type=OutcomeType.RECOVERED,
            amount_recovered_paise=amount.paise,
            external_event_id=f"razorpay:{record.event_id}",
            observed_at=now,
            is_simulated=False,
        )
    )

    audit.record(
        session,
        case_id=case.id,
        event_type=AuditEventType.OUTCOME_OBSERVED,
        summary=f"Razorpay {parsed.event_type}: {amount.format_inr()} received",
        actor_type=ActorType.WEBHOOK,
        actor_id=record.event_id,
        payload={
            "event_type": parsed.event_type,
            "amount_paise": amount.paise,
            "reference_id": parsed.reference_id,
            "simulated": False,
        },
    )

    cases.record_recovery(
        session, case, amount, now, actor_type=ActorType.WEBHOOK, actor_id=record.event_id
    )
    return f"recorded {amount.format_inr()} recovered on case {case.id}"


def _apply_failure(
    session: Session,
    record: WebhookEvent,
    parsed: razorpay.ParsedEvent,
    *,
    now: datetime,
) -> str:
    """Revenue is newly at risk. Ingest it as a case if we do not have one."""
    existing = _find_case(session, parsed)
    if existing is not None:
        record.recovery_case_id = existing.id
        audit.record(
            session,
            case_id=existing.id,
            event_type=AuditEventType.OUTCOME_OBSERVED,
            summary=f"Razorpay {parsed.event_type} observed for an existing case",
            actor_type=ActorType.WEBHOOK,
            actor_id=record.event_id,
            payload={"event_type": parsed.event_type},
        )
        return f"noted {parsed.event_type} on existing case {existing.id}"

    if not parsed.external_id or not parsed.amount_paise:
        raise ValueError(f"{parsed.event_type} lacks the id or amount needed to ingest")

    merchant_id = (
        session.execute(select(Merchant.id).order_by(Merchant.created_at)).scalars().first()
    )
    if merchant_id is None:
        raise LookupError("No merchant configured to attach this event to")

    try:
        case = cases.ingest(
            session,
            NewCaseInput(
                merchant_id=merchant_id,
                source_type=parsed.source_type or SourceType.PAYMENT,
                source_external_id=parsed.external_id,
                amount_at_risk=Money(parsed.amount_paise),
                detected_at=now,
                failure_category=parsed.failure_category or razorpay.FailureCategory.UNKNOWN,
                failure_reason_code=parsed.failure_reason_code,
                # Real provider events are not synthetic, and must never be
                # deleted by the demo reset.
                is_synthetic=False,
            ),
        )
    except DuplicateCaseError:
        return f"case already exists for {parsed.external_id}"

    record.recovery_case_id = case.id
    return f"ingested case {case.id} from {parsed.event_type}"
