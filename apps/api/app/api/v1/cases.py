"""Recovery case endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.schemas import (
    AuditEventOut,
    BatchIngestRequest,
    BatchIngestResponse,
    BlockedStrategyOut,
    CaseDetail,
    CaseListResponse,
    CaseSummary,
    EvaluateBatchRequest,
    EvaluateBatchResponse,
    EvaluateResponse,
    MoneyOut,
    PolicyDecisionOut,
    ScoredStrategyOut,
    StopRequest,
)
from app.core.money import Money
from app.db.models import Customer, Merchant, RecoveryCase
from app.domain.audit import service as audit
from app.domain.cases import evaluation, state_machine
from app.domain.cases import service as cases
from app.domain.cases.service import DuplicateCaseError, NewCaseInput
from app.domain.enums import CaseState, FailureCategory, SourceType
from app.domain.policies.engine import PolicyDecision

router = APIRouter(prefix="/cases", tags=["cases"])


def _default_merchant_id(session: Session) -> uuid.UUID:
    """Resolve the merchant for single-tenant demo operation.

    Authentication and real multi-tenancy are out of scope for the hackathon
    (spec section 21 requires approval before adding an auth provider), so the
    API operates against the single seeded merchant. The column and every query
    are already merchant-scoped, so adding auth later is a routing change, not
    a schema change.
    """
    merchant_id = (
        session.execute(select(Merchant.id).order_by(Merchant.created_at)).scalars().first()
    )
    if merchant_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No merchant exists yet. Seed the demo data first: POST /api/v1/demo/seed",
        )
    return merchant_id


@router.get("", response_model=CaseListResponse)
def list_cases(
    session: Session = Depends(get_db),
    state: list[CaseState] | None = Query(default=None),
    source_type: list[SourceType] | None = Query(default=None),
    failure_category: list[FailureCategory] | None = Query(default=None),
    min_amount_paise: int | None = Query(default=None, ge=0),
    max_amount_paise: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> CaseListResponse:
    """Filtered, paginated case list backing the Recovery Cases table."""
    if (
        min_amount_paise is not None
        and max_amount_paise is not None
        and min_amount_paise > max_amount_paise
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="min_amount_paise cannot exceed max_amount_paise",
        )

    query = cases.build_list_query(
        states=state,
        source_types=source_type,
        failure_categories=failure_category,
        min_amount_paise=min_amount_paise,
        max_amount_paise=max_amount_paise,
    )

    total = session.execute(select(func.count()).select_from(query.subquery())).scalar_one()

    rows = (
        session.execute(query.order_by(RecoveryCase.detected_at.desc()).limit(limit).offset(offset))
        .scalars()
        .all()
    )

    items = [CaseSummary.from_model(row) for row in rows]
    return CaseListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        contains_synthetic=any(row.is_synthetic for row in rows),
    )


@router.get("/{case_id}", response_model=CaseDetail)
def get_case(case_id: uuid.UUID, session: Session = Depends(get_db)) -> CaseDetail:
    """One case with its full audit trail and its legal next states."""
    try:
        case = cases.get(session, case_id)
    except cases.CaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    trail = audit.get_trail(session, case.id)
    summary = CaseSummary.from_model(case)
    return CaseDetail(
        **summary.model_dump(),
        audit_trail=[AuditEventOut.model_validate(event) for event in trail],
        allowed_transitions=sorted(state_machine.allowed_targets(CaseState(case.current_state))),
    )


@router.post("/batch", response_model=BatchIngestResponse, status_code=status.HTTP_201_CREATED)
def ingest_batch(
    payload: BatchIngestRequest, session: Session = Depends(get_db)
) -> BatchIngestResponse:
    """Ingest a batch of revenue-risk cases.

    Idempotent per ``source_external_id``: re-posting a batch reports the
    repeats as duplicates instead of double-counting revenue at risk.
    """
    merchant_id = _default_merchant_id(session)

    known_customers = {
        ref: cid
        for ref, cid in session.execute(
            select(Customer.external_ref, Customer.id).where(Customer.merchant_id == merchant_id)
        ).all()
    }

    ingested = 0
    duplicates: list[str] = []

    for item in payload.cases:
        customer_id = (
            known_customers.get(item.customer_external_ref) if item.customer_external_ref else None
        )
        try:
            cases.ingest(
                session,
                NewCaseInput(
                    merchant_id=merchant_id,
                    source_type=item.source_type,
                    source_external_id=item.source_external_id,
                    amount_at_risk=Money(item.amount_at_risk_paise),
                    detected_at=item.detected_at,
                    failure_category=item.failure_category,
                    failure_reason_code=item.failure_reason_code,
                    customer_id=customer_id,
                    currency=item.currency,
                    do_not_contact=item.do_not_contact,
                    is_synthetic=item.is_synthetic,
                    attempt_count=item.attempt_count,
                    payment_method=item.payment_method,
                    subscription_age_days=item.subscription_age_days,
                ),
            )
        except DuplicateCaseError:
            duplicates.append(item.source_external_id)
            continue
        ingested += 1

    return BatchIngestResponse(ingested=ingested, duplicates=duplicates, failed=[])


def _decision_out(decision: PolicyDecision) -> PolicyDecisionOut:
    return PolicyDecisionOut(
        recommended_strategy=decision.recommended_strategy,
        next_state=decision.next_state,
        deferred=decision.is_deferred,
        requires_human=decision.requires_human,
        explanation=decision.explanation,
        expected_net_paise=decision.expected_net.paise,
        applied_rules=decision.applied_rules,
        decisive_rule=decision.decisive_rule,
        retry_after=decision.retry_after,
        allowed=[
            ScoredStrategyOut(
                strategy=s.strategy,
                probability=float(s.probability),
                expected_gross=MoneyOut.of(s.expected_gross.paise),
                cost=MoneyOut.of(s.cost.paise),
                expected_net_paise=s.expected_net.paise,
                model_version=s.model_version,
            )
            for s in decision.allowed
        ],
        blocked=[
            BlockedStrategyOut(strategy=b.strategy, rule_id=b.rule_id, reason=b.reason)
            for b in decision.blocked
        ],
    )


@router.post("/{case_id}/evaluate", response_model=EvaluateResponse)
def evaluate_case(case_id: uuid.UUID, session: Session = Depends(get_db)) -> EvaluateResponse:
    """Run diagnose -> score -> policy for one case and apply the transition."""
    try:
        case = cases.get(session, case_id)
    except cases.CaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    merchant = session.get(Merchant, case.merchant_id)
    if merchant is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Case has no owning merchant"
        )

    try:
        result = evaluation.evaluate(session, case, merchant, now=datetime.now(UTC))
    except evaluation.NotEvaluableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return EvaluateResponse(
        case=CaseSummary.from_model(result.case),
        decision=_decision_out(result.decision),
        model_version=result.model_version,
    )


@router.post("/{case_id}/stop", response_model=CaseSummary)
def stop_case(
    case_id: uuid.UUID, payload: StopRequest, session: Session = Depends(get_db)
) -> CaseSummary:
    """Operator kill switch. Always available on a non-terminal case."""
    try:
        case = cases.get(session, case_id)
    except cases.CaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    evaluation.stop(session, case, payload.reason, actor_id=payload.reviewer)
    return CaseSummary.from_model(case)


@router.post("/evaluate-batch", response_model=EvaluateBatchResponse)
def evaluate_batch(
    payload: EvaluateBatchRequest, session: Session = Depends(get_db)
) -> EvaluateBatchResponse:
    """Run the agent across every pending case.

    This is the endpoint the demo drives: it turns a pile of ingested failures
    into a set of policy-compliant decisions, and reports the aggregate.
    """
    merchant_id = _default_merchant_id(session)
    merchant = session.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Merchant missing")

    pending = (
        session.execute(
            select(RecoveryCase)
            .where(
                RecoveryCase.merchant_id == merchant_id,
                RecoveryCase.current_state.in_(
                    [CaseState.NEW, CaseState.DIAGNOSED, CaseState.SCORED]
                ),
            )
            .order_by(RecoveryCase.detected_at)
            .limit(payload.limit)
        )
        .scalars()
        .all()
    )

    now = datetime.now(UTC)
    counters = {"action_selected": 0, "escalated": 0, "stopped": 0, "waiting": 0}
    by_rule: dict[str, int] = {}
    total_at_risk = Money.zero()
    total_expected_net = 0

    for case in pending:
        result = evaluation.evaluate(session, case, merchant, now=now)
        total_at_risk = total_at_risk + Money(case.amount_at_risk_paise)
        total_expected_net += result.decision.expected_net.paise

        for rule in result.decision.applied_rules:
            by_rule[rule] = by_rule.get(rule, 0) + 1

        if result.decision.is_deferred:
            counters["waiting"] += 1
        else:
            match result.decision.next_state:
                case CaseState.ACTION_SELECTED:
                    counters["action_selected"] += 1
                case CaseState.ESCALATED:
                    counters["escalated"] += 1
                case CaseState.STOPPED:
                    counters["stopped"] += 1

    return EvaluateBatchResponse(
        evaluated=len(pending),
        action_selected=counters["action_selected"],
        escalated=counters["escalated"],
        stopped=counters["stopped"],
        waiting=counters["waiting"],
        total_at_risk=MoneyOut.of(total_at_risk.paise),
        total_expected_net_paise=total_expected_net,
        by_rule=dict(sorted(by_rule.items())),
    )
