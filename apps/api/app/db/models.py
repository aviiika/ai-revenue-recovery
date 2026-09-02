"""Persistence models (spec section 9).

Slice 1 implements the four entities needed to prove the core loop's spine:
Merchant, Customer, RecoveryCase and AuditEvent. Intervention,
RecoveryOutcome, WebhookEvent, ModelPrediction and HumanReview arrive with the
milestones that give them behaviour, so that no table exists without code that
writes to it.

Money invariant: every monetary column is ``BigInteger`` paise. There is no
Numeric or Float money column anywhere in this schema, by design.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONBType
from app.domain.enums import (
    ActorType,
    AuditEventType,
    CaseState,
    FailureCategory,
    Recoverability,
    SourceType,
)

# SQLAlchemy's dialect-agnostic UUID: native uuid on PostgreSQL, CHAR(32)
# elsewhere. Keeps Python-side values as real uuid.UUID objects on every
# backend, so the SQLite test run exercises the same types as production.
UUIDType = Uuid(as_uuid=True)


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")

    # Cases at or above this amount are routed to a human regardless of model
    # confidence (spec FR-7: escalate high-value cases above merchant threshold).
    high_value_threshold_paise: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=500000
    )
    # Merchant-configurable stopping rules; read by the policy engine (M1).
    policy_config: Mapped[dict[str, Any]] = mapped_column(JSONBType, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    cases: Mapped[list[RecoveryCase]] = relationship(back_populates="merchant")

    __table_args__ = (
        CheckConstraint(
            "high_value_threshold_paise >= 0", name="high_value_threshold_non_negative"
        ),
    )


class Customer(Base):
    """Demo-safe customer record.

    Deliberately holds no card data, no phone number and no email address --
    only an opaque external reference and contactability flags. Real PII is out
    of scope for this project (spec section 2.3).
    """

    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    external_ref: Mapped[str] = mapped_column(String(100), nullable=False)
    segment: Mapped[str] = mapped_column(String(50), nullable=False, default="STANDARD")

    contactable_email: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    contactable_sms: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Hard opt-out. The policy engine treats this as an absolute stop.
    do_not_contact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    preferred_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tenure_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prior_successful_payments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("merchant_id", "external_ref", name="customer_external_ref"),
    )


class RecoveryCase(Base):
    """One unit of revenue at risk, moving through the recovery loop."""

    __tablename__ = "recovery_cases"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )

    source_type: Mapped[SourceType] = mapped_column(String(20), nullable=False)
    # Provider-side identifier (e.g. a Razorpay payment id). Unique per
    # merchant: re-ingesting the same event must not create a second case.
    # This is the first line of webhook idempotency (spec NFR Reliability).
    source_external_id: Mapped[str] = mapped_column(String(120), nullable=False)

    amount_at_risk_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")

    failure_category: Mapped[FailureCategory] = mapped_column(
        String(40), nullable=False, default=FailureCategory.UNKNOWN
    )
    failure_reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    recoverability: Mapped[Recoverability | None] = mapped_column(String(20), nullable=True)

    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_state: Mapped[CaseState] = mapped_column(
        String(20), nullable=False, default=CaseState.NEW
    )

    # Model outputs. Null until the case is scored (Milestone 2).
    recoverability_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(60), nullable=True)

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    recovered_amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    do_not_contact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Every row created by the synthetic generator is flagged. The UI surfaces
    # this as a banner; the spec forbids presenting synthetic results as real.
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    merchant: Mapped[Merchant] = relationship(back_populates="cases")
    customer: Mapped[Customer | None] = relationship()
    audit_events: Mapped[list[AuditEvent]] = relationship(
        back_populates="recovery_case",
        order_by="AuditEvent.sequence",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("merchant_id", "source_external_id", name="case_source_external_id"),
        CheckConstraint("amount_at_risk_paise > 0", name="amount_at_risk_positive"),
        CheckConstraint("recovered_amount_paise >= 0", name="recovered_amount_non_negative"),
        CheckConstraint(
            "recovered_amount_paise <= amount_at_risk_paise",
            name="recovered_not_exceeding_at_risk",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        Index("ix_recovery_cases_merchant_state", "merchant_id", "current_state"),
        Index("ix_recovery_cases_detected_at", "detected_at"),
    )


class AuditEvent(Base):
    """Append-only record of everything that happened to a case (spec FR-8).

    There is no update path and no delete path in :mod:`app.domain.audit`.
    ``sequence`` is a per-case monotonic counter, so the trail can be replayed
    in exact order even when two events share a timestamp.
    """

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    actor_type: Mapped[ActorType] = mapped_column(String(20), nullable=False)
    # Human username, model version string, or webhook event id.
    actor_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    event_type: Mapped[AuditEventType] = mapped_column(String(40), nullable=False)
    before_state: Mapped[CaseState | None] = mapped_column(String(20), nullable=True)
    after_state: Mapped[CaseState | None] = mapped_column(String(20), nullable=True)

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # Redacted before write; never contains secrets or PII.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONBType, nullable=False, default=dict)

    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    recovery_case: Mapped[RecoveryCase] = relationship(back_populates="audit_events")

    __table_args__ = (
        UniqueConstraint("recovery_case_id", "sequence", name="audit_sequence_per_case"),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        Index("ix_audit_events_case_seq", "recovery_case_id", "sequence"),
    )
