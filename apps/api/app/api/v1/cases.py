"""Recovery case endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.schemas import (
    AuditEventOut,
    BatchIngestRequest,
    BatchIngestResponse,
    CaseDetail,
    CaseListResponse,
    CaseSummary,
)
from app.core.money import Money
from app.db.models import Customer, Merchant, RecoveryCase
from app.domain.audit import service as audit
from app.domain.cases import service as cases
from app.domain.cases import state_machine
from app.domain.cases.service import DuplicateCaseError, NewCaseInput
from app.domain.enums import CaseState, FailureCategory, SourceType

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
                ),
            )
        except DuplicateCaseError:
            duplicates.append(item.source_external_id)
            continue
        ingested += 1

    return BatchIngestResponse(ingested=ingested, duplicates=duplicates, failed=[])
