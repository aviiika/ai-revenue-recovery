"""Demo control endpoints.

Gated behind ``DEMO_ENDPOINTS_ENABLED`` because they create and destroy data.
Only ``seed`` and ``reset`` exist in Slice 1; ``run-batch`` and
``simulate-outcomes`` arrive with the orchestrator in Milestone 4.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_demo_enabled
from app.api.schemas import (
    MoneyOut,
    RunBatchRequest,
    RunBatchResponse,
    SeedRequest,
    SeedResponse,
    SimulateOutcomesRequest,
    SimulateOutcomesResponse,
)
from app.core.config import Settings
from app.core.money import Money
from app.db.models import Customer, Merchant, RecoveryCase
from app.domain.cases import service as cases
from app.domain.cases.service import DuplicateCaseError, NewCaseInput
from app.domain.enums import FailureCategory, SourceType
from app.domain.orchestrator import service as orchestrator
from app.domain.simulator import service as simulator

# Repo-root-relative import of the shared generator, so the seeded demo data and
# the ML training data come from exactly one implementation.
from ml.src.generate_data import generate

router = APIRouter(prefix="/demo", tags=["demo"])

DEMO_MERCHANT_NAME = "Synthetic Demo Merchant"


def _get_or_create_merchant(session: Session) -> Merchant:
    merchant = session.execute(
        select(Merchant).where(Merchant.name == DEMO_MERCHANT_NAME)
    ).scalar_one_or_none()
    if merchant is None:
        merchant = Merchant(
            id=uuid.uuid4(),
            name=DEMO_MERCHANT_NAME,
            currency="INR",
            timezone="Asia/Kolkata",
            # Cases at or above INR 25,000 go to a human by default.
            high_value_threshold_paise=2_500_000,
            policy_config={
                "max_automated_attempts": 3,
                "cooldown_hours": 24,
                "min_auto_action_confidence": 0.35,
                "min_expected_net_recovery_paise": 0,
            },
        )
        session.add(merchant)
        session.flush()
    return merchant


@router.post("/seed", response_model=SeedResponse, status_code=status.HTTP_201_CREATED)
def seed(
    payload: SeedRequest,
    session: Session = Depends(get_db),
    settings: Settings = Depends(require_demo_enabled),
) -> SeedResponse:
    """Seed a deterministic batch of synthetic cases.

    The same seed always produces the same cases, so the demo is reproducible
    and a judge can re-run it and see identical numbers.
    """
    seed_used = payload.seed if payload.seed is not None else settings.synthetic_seed
    merchant = _get_or_create_merchant(session)

    if payload.reset:
        # Scoped to synthetic rows only, so a reset can never destroy data that
        # came from a real Razorpay test-mode webhook.
        session.execute(
            delete(RecoveryCase).where(
                RecoveryCase.merchant_id == merchant.id,
                RecoveryCase.is_synthetic.is_(True),
            )
        )
        session.flush()

    synthetic_cases = generate(count=payload.count, seed=seed_used)

    existing_refs = {
        ref
        for (ref,) in session.execute(
            select(Customer.external_ref).where(Customer.merchant_id == merchant.id)
        ).all()
    }
    customers_created = 0
    customer_ids: dict[str, uuid.UUID] = {
        ref: cid
        for ref, cid in session.execute(
            select(Customer.external_ref, Customer.id).where(Customer.merchant_id == merchant.id)
        ).all()
    }

    for item in synthetic_cases:
        if item.customer_external_ref in existing_refs:
            continue
        customer = Customer(
            id=uuid.uuid4(),
            merchant_id=merchant.id,
            external_ref=item.customer_external_ref,
            segment=item.customer_segment,
            do_not_contact=item.do_not_contact,
            tenure_days=item.customer_tenure_days,
            prior_successful_payments=item.prior_successful_payments,
            prior_failed_payments=item.prior_failed_payments,
        )
        session.add(customer)
        existing_refs.add(item.customer_external_ref)
        customer_ids[item.customer_external_ref] = customer.id
        customers_created += 1
    session.flush()

    created = 0
    total_at_risk = Money.zero()
    for item in synthetic_cases:
        try:
            cases.ingest(
                session,
                NewCaseInput(
                    merchant_id=merchant.id,
                    source_type=SourceType(item.source_type),
                    source_external_id=item.source_external_id,
                    amount_at_risk=Money(item.amount_at_risk_paise),
                    detected_at=datetime.fromisoformat(item.detected_at),
                    failure_category=FailureCategory(item.failure_category),
                    failure_reason_code=item.failure_reason_code,
                    customer_id=customer_ids.get(item.customer_external_ref),
                    do_not_contact=item.do_not_contact,
                    is_synthetic=True,
                    # attempt_number is 1-based ("this is the Nth attempt"), so
                    # prior attempts is one fewer.
                    attempt_count=max(item.attempt_number - 1, 0),
                    payment_method=item.payment_method,
                    subscription_age_days=item.subscription_age_days,
                ),
            )
        except DuplicateCaseError:
            # Seeding twice with the same seed is a no-op rather than an error.
            continue
        created += 1
        total_at_risk = total_at_risk + Money(item.amount_at_risk_paise)

    return SeedResponse(
        merchant_id=merchant.id,
        seed_used=seed_used,
        cases_created=created,
        customers_created=customers_created,
        total_at_risk=MoneyOut.of(total_at_risk.paise),
    )


@router.post("/reset", status_code=status.HTTP_200_OK)
def reset(
    session: Session = Depends(get_db),
    settings: Settings = Depends(require_demo_enabled),
) -> dict[str, int]:
    """Delete every synthetic case. Non-synthetic data is left untouched."""
    # Counted before the delete rather than read off the cursor: rowcount is not
    # portably typed across dialects, and the count is what the caller wants.
    deleted = session.execute(
        select(func.count()).select_from(RecoveryCase).where(RecoveryCase.is_synthetic.is_(True))
    ).scalar_one()
    session.execute(delete(RecoveryCase).where(RecoveryCase.is_synthetic.is_(True)))
    return {"deleted_cases": deleted}


@router.post("/run-batch", response_model=RunBatchResponse)
def run_batch(
    payload: RunBatchRequest,
    session: Session = Depends(get_db),
    settings: Settings = Depends(require_demo_enabled),
) -> RunBatchResponse:
    """Evaluate pending cases and execute whatever policy authorises.

    Runs inline rather than dispatching to Celery, because the spec requires a
    demo that does not depend on external timing. The worker task calls the
    identical orchestrator function.
    """
    merchant = _get_or_create_merchant(session)
    result = orchestrator.run_batch(session, merchant, now=datetime.now(UTC), limit=payload.limit)
    return RunBatchResponse(
        evaluated=result.evaluated,
        planned=result.planned,
        executed=result.executed,
        skipped=result.skipped,
        escalated=result.escalated,
        stopped=result.stopped,
        deferred=result.deferred,
        holdout_withheld=result.holdout_withheld,
        total_at_risk=MoneyOut.of(result.total_at_risk.paise),
        estimated_spend=MoneyOut.of(result.estimated_spend.paise),
        by_rule=dict(sorted(result.by_rule.items())),
    )


@router.post("/simulate-outcomes", response_model=SimulateOutcomesResponse)
def simulate_outcomes(
    payload: SimulateOutcomesRequest,
    session: Session = Depends(get_db),
    settings: Settings = Depends(require_demo_enabled),
) -> SimulateOutcomesResponse:
    """Reveal deterministic outcomes for cases awaiting observation."""
    merchant = _get_or_create_merchant(session)
    seed = payload.seed if payload.seed is not None else settings.synthetic_seed
    summary = simulator.simulate_outcomes(
        session, merchant.id, seed=seed, now=datetime.now(UTC), limit=payload.limit
    )
    return SimulateOutcomesResponse(
        cases_observed=summary.cases_observed,
        recovered=summary.recovered,
        no_response=summary.no_response,
        recovered_amount=MoneyOut.of(summary.recovered_amount.paise),
        treatment_observed=summary.treatment_observed,
        treatment_recovered=summary.treatment_recovered,
        holdout_observed=summary.holdout_observed,
        holdout_recovered=summary.holdout_recovered,
    )


@router.post("/run-full-cycle")
def run_full_cycle(
    payload: RunBatchRequest,
    session: Session = Depends(get_db),
    settings: Settings = Depends(require_demo_enabled),
) -> dict[str, object]:
    """Drive the whole loop to convergence: act, observe, retry, repeat.

    This is the single call the demo makes: it takes a seeded population all the
    way from ingestion to settled recovered revenue.
    """
    merchant = _get_or_create_merchant(session)
    return orchestrator.run_full_cycle(
        session,
        merchant,
        seed=settings.synthetic_seed,
        now=datetime.now(UTC),
        rounds=payload.rounds,
        limit=payload.limit,
    )
