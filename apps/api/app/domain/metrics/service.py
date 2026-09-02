"""Aggregate recovery metrics (spec FR-9, section 16).

Every figure here is computed from stored rows in integer paise. Nothing is
hardcoded and nothing is estimated -- the spec is explicit that the UI must
never display fabricated performance numbers.

Two recovery rates are reported and they are genuinely different: ticket sizes
are long-tailed, so recovering a few large cases moves the value rate far more
than the count rate. Showing only one would flatter or understate the result
depending on which you picked.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.money import Money, SignedMoney
from app.db.models import AuditEvent, RecoveryCase
from app.domain.enums import TERMINAL_STATES, CaseState, InterventionStrategy
from app.domain.scoring.service import INTERVENTION_COST_PAISE

#: States a case passes through on the way to being acted on. Used for the
#: funnel, in order.
FUNNEL_STAGES: tuple[tuple[str, tuple[CaseState, ...]], ...] = (
    ("Detected", tuple(CaseState)),
    (
        "Diagnosed",
        tuple(s for s in CaseState if s not in {CaseState.NEW}),
    ),
    (
        "Scored",
        tuple(s for s in CaseState if s not in {CaseState.NEW, CaseState.DIAGNOSED}),
    ),
    (
        "Action selected",
        (
            CaseState.ACTION_SELECTED,
            CaseState.ACTION_PENDING,
            CaseState.ACTION_EXECUTED,
            CaseState.OBSERVING,
            CaseState.RETRY_ELIGIBLE,
            CaseState.RECOVERED,
        ),
    ),
    ("Recovered", (CaseState.RECOVERED,)),
)


@dataclass(frozen=True, slots=True)
class OverviewMetrics:
    """The headline numbers on the dashboard (spec section 16)."""

    total_cases: int
    revenue_at_risk: Money
    revenue_recovered: Money
    #: Cases the agent resolved without a human, as a share of resolved cases.
    automated_resolution_rate: float
    recovery_rate_by_count: float
    recovery_rate_by_value: float
    estimated_intervention_cost: Money
    net_recovery: SignedMoney
    cases_recovered: int
    cases_escalated: int
    cases_stopped: int
    cases_in_progress: int
    contains_synthetic: bool


def _base_query(merchant_id: uuid.UUID | None) -> Select[tuple[RecoveryCase]]:
    query = select(RecoveryCase)
    if merchant_id is not None:
        query = query.where(RecoveryCase.merchant_id == merchant_id)
    return query


def overview(session: Session, merchant_id: uuid.UUID | None = None) -> OverviewMetrics:
    """Portfolio-level KPIs.

    Intervention cost is *estimated* from attempts made and the policy engine's
    cost table, not measured -- no intervention has actually been executed until
    Milestone 4. The field name says ``estimated`` so the UI cannot present it
    as a measured spend.
    """
    rows = list(session.execute(_base_query(merchant_id)).scalars().all())

    total_at_risk = Money(sum(row.amount_at_risk_paise for row in rows))
    total_recovered = Money(sum(row.recovered_amount_paise for row in rows))

    recovered_rows = [r for r in rows if r.current_state == CaseState.RECOVERED]
    escalated = sum(1 for r in rows if r.current_state == CaseState.ESCALATED)
    stopped = sum(1 for r in rows if r.current_state == CaseState.STOPPED)
    exhausted = sum(1 for r in rows if r.current_state == CaseState.EXHAUSTED)
    in_progress = sum(
        1
        for r in rows
        if r.current_state not in TERMINAL_STATES and r.current_state != CaseState.ESCALATED
    )

    # Estimated spend: one payment-link-equivalent per attempt made.
    attempts = sum(r.attempt_count for r in rows)
    estimated_cost = Money(
        attempts * INTERVENTION_COST_PAISE[InterventionStrategy.CREATE_PAYMENT_LINK]
    )

    resolved = len(recovered_rows) + stopped + exhausted + escalated
    automated_resolved = len(recovered_rows) + stopped + exhausted

    return OverviewMetrics(
        total_cases=len(rows),
        revenue_at_risk=total_at_risk,
        revenue_recovered=total_recovered,
        automated_resolution_rate=(automated_resolved / resolved) if resolved else 0.0,
        recovery_rate_by_count=(len(recovered_rows) / len(rows)) if rows else 0.0,
        recovery_rate_by_value=(
            total_recovered.paise / total_at_risk.paise if total_at_risk.paise else 0.0
        ),
        estimated_intervention_cost=estimated_cost,
        net_recovery=SignedMoney.of(total_recovered) - SignedMoney.of(estimated_cost),
        cases_recovered=len(recovered_rows),
        cases_escalated=escalated,
        cases_stopped=stopped + exhausted,
        cases_in_progress=in_progress,
        contains_synthetic=any(r.is_synthetic for r in rows),
    )


def state_distribution(
    session: Session, merchant_id: uuid.UUID | None = None
) -> list[dict[str, object]]:
    """Case count and value by current state."""
    query = select(
        RecoveryCase.current_state,
        func.count().label("count"),
        func.coalesce(func.sum(RecoveryCase.amount_at_risk_paise), 0).label("at_risk"),
    ).group_by(RecoveryCase.current_state)
    if merchant_id is not None:
        query = query.where(RecoveryCase.merchant_id == merchant_id)

    return [
        {"state": str(state), "count": int(count), "at_risk_paise": int(at_risk)}
        for state, count, at_risk in session.execute(query).all()
    ]


def failure_reason_distribution(
    session: Session, merchant_id: uuid.UUID | None = None
) -> list[dict[str, object]]:
    """Where the money is being lost, by diagnosed cause (spec FR-9)."""
    query = select(
        RecoveryCase.failure_category,
        func.count().label("count"),
        func.coalesce(func.sum(RecoveryCase.amount_at_risk_paise), 0).label("at_risk"),
        func.coalesce(func.sum(RecoveryCase.recovered_amount_paise), 0).label("recovered"),
    ).group_by(RecoveryCase.failure_category)
    if merchant_id is not None:
        query = query.where(RecoveryCase.merchant_id == merchant_id)

    rows = sorted(session.execute(query).all(), key=lambda row: int(row[2]), reverse=True)
    return [
        {
            "failure_category": str(category),
            "count": int(count),
            "at_risk_paise": int(at_risk),
            "recovered_paise": int(recovered),
        }
        for category, count, at_risk, recovered in rows
    ]


def recovery_funnel(
    session: Session, merchant_id: uuid.UUID | None = None
) -> list[dict[str, object]]:
    """How many cases, and how much value, reached each stage of the loop.

    Counts cases that reached *at least* a stage, so the funnel is monotonically
    non-increasing and reads correctly as a funnel rather than a state histogram.
    """
    rows = list(session.execute(_base_query(merchant_id)).scalars().all())

    funnel: list[dict[str, object]] = []
    for label, states in FUNNEL_STAGES:
        matching = [r for r in rows if CaseState(r.current_state) in states]
        funnel.append(
            {
                "stage": label,
                "count": len(matching),
                "at_risk_paise": sum(r.amount_at_risk_paise for r in matching),
            }
        )
    return funnel


def intervention_performance(
    session: Session, merchant_id: uuid.UUID | None = None
) -> list[dict[str, object]]:
    """How each strategy performed, read from the audit trail.

    Sourced from ``INTERVENTION_SELECTED`` events rather than a summary column,
    so the numbers are reconstructable from the immutable trail -- which is the
    spec's auditability requirement applied to reporting, not just to cases.
    """
    query = (
        select(
            AuditEvent.payload,
            RecoveryCase.current_state,
            RecoveryCase.recovered_amount_paise,
            RecoveryCase.amount_at_risk_paise,
        )
        .join(RecoveryCase, AuditEvent.recovery_case_id == RecoveryCase.id)
        .where(AuditEvent.event_type == "INTERVENTION_SELECTED")
    )
    if merchant_id is not None:
        query = query.where(RecoveryCase.merchant_id == merchant_id)

    buckets: dict[str, dict[str, int]] = {}
    for payload, state, recovered_paise, at_risk_paise in session.execute(query).all():
        strategy = str((payload or {}).get("strategy", "UNKNOWN"))
        bucket = buckets.setdefault(
            strategy,
            {"selected": 0, "recovered": 0, "recovered_paise": 0, "at_risk_paise": 0},
        )
        bucket["selected"] += 1
        bucket["at_risk_paise"] += int(at_risk_paise)
        if state == CaseState.RECOVERED:
            bucket["recovered"] += 1
            bucket["recovered_paise"] += int(recovered_paise)

    ordered = sorted(buckets.items(), key=lambda item: item[1]["selected"], reverse=True)
    return [
        {
            "strategy": strategy,
            "selected": stats["selected"],
            "recovered": stats["recovered"],
            "at_risk_paise": stats["at_risk_paise"],
            "recovered_paise": stats["recovered_paise"],
            "success_rate": (stats["recovered"] / stats["selected"] if stats["selected"] else 0.0),
            "estimated_cost_paise": stats["selected"]
            * INTERVENTION_COST_PAISE[_strategy_or_default(strategy)],
        }
        for strategy, stats in ordered
    ]


def _strategy_or_default(name: str) -> InterventionStrategy:
    """Map an audited strategy name back to the enum.

    Falls back rather than raising: an audit event written by an older version
    of the code must not be able to break the metrics page.
    """
    try:
        return InterventionStrategy(name)
    except ValueError:
        return InterventionStrategy.CREATE_PAYMENT_LINK


def revenue_at_risk_over_time(
    session: Session, merchant_id: uuid.UUID | None = None
) -> list[dict[str, object]]:
    """Daily detected vs recovered value, oldest first (spec FR-9)."""
    query = select(
        func.date(RecoveryCase.detected_at).label("day"),
        func.count().label("count"),
        func.coalesce(func.sum(RecoveryCase.amount_at_risk_paise), 0).label("at_risk"),
        func.coalesce(func.sum(RecoveryCase.recovered_amount_paise), 0).label("recovered"),
    ).group_by(func.date(RecoveryCase.detected_at))
    if merchant_id is not None:
        query = query.where(RecoveryCase.merchant_id == merchant_id)

    rows = session.execute(query).all()
    series = [
        {
            "date": day.isoformat() if isinstance(day, date) else str(day),
            "count": int(count),
            "at_risk_paise": int(at_risk),
            "recovered_paise": int(recovered),
        }
        for day, count, at_risk, recovered in rows
    ]
    return sorted(series, key=lambda row: str(row["date"]))
