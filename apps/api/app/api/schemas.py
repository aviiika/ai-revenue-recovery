"""API request/response schemas.

Money crosses the API boundary as **integer paise** in ``*_paise`` fields. A
formatted rupee string is included alongside for display, so the frontend never
performs money arithmetic of its own (spec QUALITY rule: no duplicated domain
logic between frontend and backend).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.money import Money
from app.db.models import RecoveryCase
from app.domain.enums import (
    ActorType,
    AuditEventType,
    CaseState,
    FailureCategory,
    InterventionStrategy,
    Recoverability,
    ReviewReason,
    ReviewStatus,
    SourceType,
)


class MoneyOut(BaseModel):
    """Canonical money representation for every API response."""

    paise: int
    formatted: str

    @classmethod
    def of(cls, paise: int) -> MoneyOut:
        return cls(paise=paise, formatted=Money(paise).format_inr())


class CaseSummary(BaseModel):
    """Row shape for the recovery cases table (spec section 16)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_type: SourceType
    source_external_id: str
    amount_at_risk: MoneyOut
    recovered_amount: MoneyOut
    currency: str
    failure_category: FailureCategory
    failure_reason_code: str | None
    recoverability: Recoverability | None
    current_state: CaseState
    recoverability_score: float | None
    priority_score: float | None
    model_version: str | None
    attempt_count: int
    detected_at: datetime
    recovered_at: datetime | None
    last_action_at: datetime | None
    do_not_contact: bool
    is_synthetic: bool
    updated_at: datetime

    @classmethod
    def from_model(cls, case: RecoveryCase) -> CaseSummary:
        data = {
            key: getattr(case, key)
            for key in cls.model_fields
            if key not in {"amount_at_risk", "recovered_amount"}
        }
        data["amount_at_risk"] = MoneyOut.of(case.amount_at_risk_paise)
        data["recovered_amount"] = MoneyOut.of(case.recovered_amount_paise)
        return cls.model_validate(data)


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequence: int
    actor_type: ActorType
    actor_id: str | None
    event_type: AuditEventType
    before_state: CaseState | None
    after_state: CaseState | None
    summary: str
    payload: dict[str, object]
    correlation_id: str | None
    created_at: datetime


class CaseDetail(CaseSummary):
    """Case plus its complete audit trail (spec FR-10 explainability)."""

    audit_trail: list[AuditEventOut]
    allowed_transitions: list[CaseState]


class CaseListResponse(BaseModel):
    items: list[CaseSummary]
    total: int
    limit: int
    offset: int
    #: True when any row in this result set is synthetic. Drives the UI banner.
    contains_synthetic: bool


class NewCaseRequest(BaseModel):
    """One case in a batch ingestion request."""

    source_type: SourceType
    source_external_id: str = Field(min_length=1, max_length=120)
    amount_at_risk_paise: int = Field(gt=0, description="Integer paise. Never a float.")
    detected_at: datetime
    failure_category: FailureCategory = FailureCategory.UNKNOWN
    failure_reason_code: str | None = Field(default=None, max_length=80)
    customer_external_ref: str | None = Field(default=None, max_length=100)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    do_not_contact: bool = False
    is_synthetic: bool = False
    attempt_count: int = Field(default=0, ge=0, description="Attempts already made upstream.")
    payment_method: str | None = Field(default=None, max_length=20)
    subscription_age_days: int | None = Field(default=None, ge=0)

    @field_validator("detected_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        # A naive timestamp silently becomes a wrong timestamp once it crosses
        # a timezone boundary; cooldown windows depend on this being right.
        if value.tzinfo is None:
            raise ValueError("detected_at must be timezone-aware (include an offset, e.g. +05:30)")
        return value


class BatchIngestRequest(BaseModel):
    cases: list[NewCaseRequest] = Field(min_length=1, max_length=5000)


class BatchIngestResponse(BaseModel):
    ingested: int
    #: Source ids already present. Re-sending the same batch is safe and lands
    #: here rather than creating duplicates.
    duplicates: list[str]
    failed: list[str]


class SeedRequest(BaseModel):
    count: int = Field(default=120, ge=1, le=5000)
    seed: int | None = Field(default=None, description="Defaults to SYNTHETIC_SEED from settings.")
    reset: bool = Field(default=False, description="Delete existing synthetic cases first.")


class SeedResponse(BaseModel):
    merchant_id: uuid.UUID
    seed_used: int
    cases_created: int
    customers_created: int
    total_at_risk: MoneyOut
    synthetic: bool = True


class HealthResponse(BaseModel):
    status: str
    app_env: str
    database: str
    razorpay_mode: str
    llm_enabled: bool
    demo_endpoints_enabled: bool


class ScoredStrategyOut(BaseModel):
    """One candidate action with its economics, for the decision card."""

    strategy: InterventionStrategy
    probability: float
    expected_gross: MoneyOut
    cost: MoneyOut
    expected_net_paise: int
    model_version: str


class BlockedStrategyOut(BaseModel):
    strategy: InterventionStrategy
    rule_id: str
    reason: str


class PolicyDecisionOut(BaseModel):
    """Why the agent decided what it decided (spec FR-10 explainability)."""

    recommended_strategy: InterventionStrategy
    #: None when the engine deferred without changing the case (cooldown).
    next_state: CaseState | None
    deferred: bool
    requires_human: bool
    explanation: str
    expected_net_paise: int
    applied_rules: list[str]
    decisive_rule: str | None
    retry_after: datetime | None
    allowed: list[ScoredStrategyOut]
    blocked: list[BlockedStrategyOut]


class EvaluateResponse(BaseModel):
    case: CaseSummary
    decision: PolicyDecisionOut
    model_version: str


class StopRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    reviewer: str | None = Field(default=None, max_length=120)


class EvaluateBatchRequest(BaseModel):
    limit: int = Field(default=200, ge=1, le=2000)


class EvaluateBatchResponse(BaseModel):
    """Aggregate outcome of running the agent over a batch."""

    evaluated: int
    action_selected: int
    escalated: int
    stopped: int
    waiting: int
    total_at_risk: MoneyOut
    total_expected_net_paise: int
    by_rule: dict[str, int]


class RunBatchRequest(BaseModel):
    limit: int = Field(default=500, ge=1, le=5000)
    rounds: int = Field(default=3, ge=1, le=10, description="Act/observe cycles to run.")


class RunBatchResponse(BaseModel):
    """Outcome of one orchestration pass."""

    evaluated: int
    planned: int
    executed: int
    skipped: int
    escalated: int
    stopped: int
    deferred: int
    #: Cases the agent chose to act on but withheld because they are in the
    #: randomised holdout arm. Withholding is the point of the arm.
    holdout_withheld: int
    total_at_risk: MoneyOut
    estimated_spend: MoneyOut
    by_rule: dict[str, int]


class SimulateOutcomesRequest(BaseModel):
    seed: int | None = Field(default=None, description="Defaults to SYNTHETIC_SEED.")
    limit: int = Field(default=2000, ge=1, le=5000)


class SimulateOutcomesResponse(BaseModel):
    cases_observed: int
    recovered: int
    no_response: int
    recovered_amount: MoneyOut
    #: Split by arm, so the incremental comparison is visible in the raw result
    #: rather than only after aggregation.
    treatment_observed: int
    treatment_recovered: int
    holdout_observed: int
    holdout_recovered: int


class ReviewOut(BaseModel):
    """One row of the human review queue."""

    id: uuid.UUID
    recovery_case_id: uuid.UUID
    source_external_id: str
    reason: ReviewReason
    status: ReviewStatus
    proposed_strategy: InterventionStrategy | None
    chosen_strategy: InterventionStrategy | None
    #: Prose rationale. Template-written by default, LLM-written when configured.
    explanation: str | None
    amount_at_risk: MoneyOut
    failure_category: FailureCategory | None
    recoverability_score: float | None
    current_state: CaseState | None
    attempt_count: int
    reviewer: str | None
    decision_notes: str | None
    applied_rules: list[str]
    created_at: datetime
    resolved_at: datetime | None


class ReviewListResponse(BaseModel):
    items: list[ReviewOut]
    total: int
    limit: int
    offset: int
    pending: int
    value_awaiting_review: MoneyOut
    by_reason: dict[str, int]


class ReviewActionRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=120)
    notes: str = Field(min_length=1, max_length=1000)


class ReviewOverrideRequest(ReviewActionRequest):
    #: Must be one the merchant has enabled; the API rejects anything else.
    strategy: InterventionStrategy


class PolicyResponse(BaseModel):
    merchant_id: uuid.UUID
    max_automated_attempts: int
    cooldown_hours: int
    min_auto_action_confidence: float
    high_value_threshold_paise: int
    min_expected_net_recovery_paise: int
    backoff_base_hours: int
    backoff_cap_hours: int
    enabled_strategies: list[InterventionStrategy]
    available_strategies: list[InterventionStrategy]


class PolicyUpdateRequest(BaseModel):
    """Partial update. Only supplied fields change; the rest are preserved."""

    model_config = ConfigDict(extra="forbid")

    max_automated_attempts: int | None = None
    cooldown_hours: int | None = None
    min_auto_action_confidence: float | None = None
    high_value_threshold_paise: int | None = None
    min_expected_net_recovery_paise: int | None = None
    backoff_base_hours: int | None = None
    backoff_cap_hours: int | None = None
    enabled_strategies: list[InterventionStrategy] | None = None
