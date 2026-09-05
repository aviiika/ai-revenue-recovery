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
    ExecuteInterventionResponse,
    InterventionOut,
    MoneyOut,
    PolicyDecisionOut,
    ScoredStrategyOut,
    StopRequest,
)
from app.core.money import Money
from app.db.models import Customer, Intervention, Merchant, RecoveryCase
from app.domain.audit import service as audit
from app.domain.cases import evaluation, state_machine
from app.domain.cases import service as cases
from app.domain.cases.service import DuplicateCaseError, NewCaseInput
from app.domain.enums import (
    AuditEventType,
    CaseState,
    FailureCategory,
    InterventionStrategy,
    SourceType,
)
from app.domain.interventions import service as interventions
from app.domain.orchestrator import service as orchestrator
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

    rows = (
        session.execute(
            select(Intervention)
            .where(Intervention.recovery_case_id == case.id)
            .order_by(Intervention.attempt_number)
        )
        .scalars()
        .all()
    )

    # The action policy last selected, read from the audit trail rather than
    # recomputed, so the page shows what was actually decided.
    selected: InterventionStrategy | None = None
    expected_net: int | None = None
    for event in reversed(trail):
        if event.event_type == AuditEventType.INTERVENTION_SELECTED:
            raw = event.payload.get("strategy")
            if raw:
                selected = InterventionStrategy(str(raw))
            net = event.payload.get("expected_net_paise")
            expected_net = int(net) if isinstance(net, int) else None
            break

    return CaseDetail(
        **summary.model_dump(),
        audit_trail=[AuditEventOut.model_validate(event) for event in trail],
        allowed_transitions=sorted(state_machine.allowed_targets(CaseState(case.current_state))),
        interventions=[InterventionOut.model_validate(row) for row in rows],
        selected_strategy=selected,
        expected_net_paise=expected_net,
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
                    days_overdue=item.days_overdue,
                    checkout_stage=item.checkout_stage,
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


def _latest_intervention(session: Session, case_id: uuid.UUID) -> Intervention | None:
    return session.execute(
        select(Intervention)
        .where(Intervention.recovery_case_id == case_id)
        .order_by(Intervention.attempt_number.desc())
        .limit(1)
    ).scalar_one_or_none()


@router.post("/{case_id}/execute", response_model=ExecuteInterventionResponse)
def execute_intervention(
    case_id: uuid.UUID, session: Session = Depends(get_db)
) -> ExecuteInterventionResponse:
    """Execute the action the policy engine has selected for this case.

    Exposes the execution path that already existed inside batch orchestration,
    so an operator can drive one case from the dashboard. It adds no recovery
    logic of its own -- it reuses ``evaluation.evaluate`` to obtain the current
    decision, ``interventions.plan`` to create the attempt, and
    ``interventions.execute`` to run it through the configured provider.

    Three properties inherited rather than reimplemented:

    * **The policy engine still decides.** This endpoint executes whatever
      policy selected; it cannot force an action policy refused.
    * **Idempotent.** ``plan`` returns the existing attempt for the same
      ``(case, attempt)`` and ``execute`` is a no-op once an attempt has run, so
      a double-clicked button cannot create two payment links.
    * **Creating a link is not a recovery.** Execution moves the case to
      OBSERVING. Only a payment event -- real webhook or simulator -- records
      money against it.
    """
    try:
        case = cases.get(session, case_id)
    except cases.CaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    merchant = session.get(Merchant, case.merchant_id)
    if merchant is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Case has no owning merchant"
        )

    if case.current_state == CaseState.RECOVERED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Case is already recovered; no further action will be taken",
        )

    # Ask the policy engine what it wants done now. Re-evaluating an
    # already-selected case is safe: the evaluator skips the transition when the
    # decision matches the current state.
    try:
        outcome = evaluation.evaluate(session, case, merchant, now=datetime.now(UTC))
    except evaluation.NotEvaluableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    decision = outcome.decision
    if decision.next_state != CaseState.ACTION_SELECTED:
        # Policy declined to act -- escalated, stopped, or deferred by cooldown.
        # Surfacing its own words keeps the UI from inventing a reason.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Policy did not select an executable action: {decision.explanation}"
                + (f" (rule {decision.decisive_rule})" if decision.decisive_rule else "")
            ),
        )

    intervention = interventions.plan(session, case, decision, now=datetime.now(UTC))
    execution = interventions.execute(
        session,
        intervention,
        merchant,
        orchestrator.default_provider(),
        now=datetime.now(UTC),
    )

    result = dict(execution.intervention.result or {})
    return ExecuteInterventionResponse(
        case=CaseSummary.from_model(case),
        intervention=InterventionOut.model_validate(execution.intervention),
        executed=execution.executed,
        reason=execution.skipped_reason,
        simulated=bool(result.get("simulated", True)),
        strategy=InterventionStrategy(execution.intervention.strategy),
        payment_link_url=(str(result["short_url"]) if result.get("short_url") else None),
        payment_link_id=(str(result["payment_link_id"]) if result.get("payment_link_id") else None),
    )
