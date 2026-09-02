"""Append-only audit trail (spec FR-8).

Every meaningful thing that happens to a case is recorded here. The module
exposes exactly one write verb -- :func:`record` -- and no update or delete
verb, so "append-only" is a property of the API surface rather than a
convention someone has to remember.

Spec section 20 requires that for any case we can reconstruct: what happened,
when, which model and version, what probability, what evidence, which policies
fired, what action was chosen, who approved it, what the provider replied, and
whether money was ultimately recovered.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import correlation_id_var, redact
from app.db.models import AuditEvent
from app.domain.enums import ActorType, AuditEventType, CaseState


class AuditError(Exception):
    """Raised when an audit event cannot be recorded."""


def _next_sequence(session: Session, case_id: uuid.UUID) -> int:
    """Next per-case sequence number.

    Read inside the caller's transaction. The ``(recovery_case_id, sequence)``
    unique constraint is the real guard: if two concurrent writers pick the same
    number, one of them fails loudly rather than silently interleaving history.
    """
    current = session.execute(
        select(func.max(AuditEvent.sequence)).where(AuditEvent.recovery_case_id == case_id)
    ).scalar_one_or_none()
    return (current or 0) + 1


def record(
    session: Session,
    *,
    case_id: uuid.UUID,
    event_type: AuditEventType,
    summary: str,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: str | None = None,
    before_state: CaseState | None = None,
    after_state: CaseState | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one event to a case's trail.

    The payload is redacted before it is written -- the audit trail is read by
    humans in the UI and must never become a secret-leak vector.

    Does not commit: the event joins the caller's transaction so that a state
    change and its audit record either both land or neither does.
    """
    if not summary.strip():
        raise AuditError("audit events require a non-empty summary")

    event = AuditEvent(
        id=uuid.uuid4(),
        recovery_case_id=case_id,
        sequence=_next_sequence(session, case_id),
        actor_type=actor_type,
        actor_id=actor_id,
        event_type=event_type,
        before_state=before_state,
        after_state=after_state,
        summary=summary.strip(),
        payload=redact(payload or {}),
        correlation_id=correlation_id_var.get(),
    )
    session.add(event)
    session.flush()
    return event


def get_trail(session: Session, case_id: uuid.UUID) -> list[AuditEvent]:
    """Full ordered history for a case, oldest first."""
    return list(
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.recovery_case_id == case_id)
            .order_by(AuditEvent.sequence)
        )
        .scalars()
        .all()
    )
