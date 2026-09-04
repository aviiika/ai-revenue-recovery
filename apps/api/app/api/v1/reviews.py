"""Human review queue endpoints (spec section 14)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.schemas import (
    MoneyOut,
    ReviewActionRequest,
    ReviewListResponse,
    ReviewOut,
    ReviewOverrideRequest,
)
from app.db.models import HumanReview, Merchant, RecoveryCase
from app.domain.enums import ReviewStatus
from app.domain.reviews import service as reviews

router = APIRouter(prefix="/reviews", tags=["reviews"])


def _to_out(review: HumanReview, case: RecoveryCase | None) -> ReviewOut:
    return ReviewOut(
        id=review.id,
        recovery_case_id=review.recovery_case_id,
        source_external_id=case.source_external_id if case else "",
        reason=review.reason,
        status=review.status,
        proposed_strategy=review.proposed_strategy,
        chosen_strategy=review.chosen_strategy,
        explanation=review.explanation,
        amount_at_risk=MoneyOut.of(review.amount_at_risk_paise),
        failure_category=case.failure_category if case else None,
        recoverability_score=case.recoverability_score if case else None,
        current_state=case.current_state if case else None,
        attempt_count=case.attempt_count if case else 0,
        reviewer=review.reviewer,
        decision_notes=review.decision_notes,
        applied_rules=list(review.decision_snapshot.get("applied_rules", [])),
        created_at=review.created_at,
        resolved_at=review.resolved_at,
    )


@router.get("", response_model=ReviewListResponse)
def list_reviews(
    session: Session = Depends(get_db),
    review_status: ReviewStatus | None = Query(default=ReviewStatus.PENDING, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ReviewListResponse:
    """The queue, largest exposure first."""
    query = reviews.build_queue_query(status=review_status)
    total = session.execute(select(func.count()).select_from(query.subquery())).scalar_one()

    rows = list(session.execute(query.limit(limit).offset(offset)).scalars().all())
    cases = {
        case.id: case
        for case in session.execute(
            select(RecoveryCase).where(
                RecoveryCase.id.in_([r.recovery_case_id for r in rows] or [uuid.uuid4()])
            )
        )
        .scalars()
        .all()
    }

    summary = reviews.queue_summary(session)
    return ReviewListResponse(
        items=[_to_out(r, cases.get(r.recovery_case_id)) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
        pending=summary.pending,
        value_awaiting_review=MoneyOut.of(summary.value_awaiting_review.paise),
        by_reason=summary.by_reason,
    )


def _merchant(session: Session) -> Merchant:
    merchant = session.execute(select(Merchant).order_by(Merchant.created_at)).scalars().first()
    if merchant is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No merchant configured")
    return merchant


@router.post("/{review_id}/approve", response_model=ReviewOut)
def approve_review(
    review_id: uuid.UUID,
    payload: ReviewActionRequest,
    session: Session = Depends(get_db),
) -> ReviewOut:
    """Let the agent's proposed action stand."""
    try:
        outcome = reviews.approve(
            session,
            review_id,
            reviewer=payload.reviewer,
            notes=payload.notes,
            now=datetime.now(UTC),
        )
    except reviews.ReviewError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _to_out(outcome.review, outcome.case)


@router.post("/{review_id}/override", response_model=ReviewOut)
def override_review(
    review_id: uuid.UUID,
    payload: ReviewOverrideRequest,
    session: Session = Depends(get_db),
) -> ReviewOut:
    """Substitute a different strategy, from those the merchant permits."""
    try:
        outcome = reviews.override(
            session,
            review_id,
            _merchant(session),
            strategy=payload.strategy,
            reviewer=payload.reviewer,
            notes=payload.notes,
            now=datetime.now(UTC),
        )
    except reviews.ReviewError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _to_out(outcome.review, outcome.case)


@router.post("/{review_id}/reject", response_model=ReviewOut)
def reject_review(
    review_id: uuid.UUID,
    payload: ReviewActionRequest,
    session: Session = Depends(get_db),
) -> ReviewOut:
    """Stop the case. The human kill switch; always available."""
    try:
        outcome = reviews.reject(
            session,
            review_id,
            reviewer=payload.reviewer,
            notes=payload.notes,
            now=datetime.now(UTC),
        )
    except reviews.ReviewError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _to_out(outcome.review, outcome.case)


@router.get("/summary")
def review_summary(session: Session = Depends(get_db)) -> dict[str, Any]:
    return reviews.queue_summary(session).to_dict()
