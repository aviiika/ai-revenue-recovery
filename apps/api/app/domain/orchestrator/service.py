"""Batch orchestration (spec section 11, Milestone 4).

Drives the whole loop over a population: evaluate, plan, execute, observe. The
logic is synchronous and pure of infrastructure so it can be tested without a
broker; ``app.workers.tasks`` wraps it as a Celery task for the queued path.

That split is deliberate. The spec requires background workers, but it also
requires that "a reliable demo cannot depend entirely on external event timing".
Keeping the orchestration core callable inline means the demo works whether or
not a worker is running, and the worker executes exactly the same code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.money import Money
from app.db.models import Merchant, RecoveryCase
from app.domain.cases import evaluation
from app.domain.enums import CaseState, ExperimentArm
from app.domain.interventions import service as interventions
from app.domain.interventions.service import PaymentProvider, SimulatedProvider
from app.domain.simulator import service as simulator


def _default_provider() -> PaymentProvider:
    """Use Razorpay test mode when credentials exist, else simulate.

    Falling back rather than failing is deliberate: a fresh clone has no keys
    and must still run the full loop (spec section 19). The import is local so
    the domain layer takes no import-time dependency on httpx or the adapter.
    """
    try:
        from app.core.config import get_settings
        from app.integrations.razorpay.adapter import build_provider

        live = build_provider(get_settings())
        if live is not None:
            return live
    except ImportError:
        pass
    return SimulatedProvider()


#: States from which a case can still be pushed forward.
PENDING_STATES = (
    CaseState.NEW,
    CaseState.DIAGNOSED,
    CaseState.SCORED,
    CaseState.RETRY_ELIGIBLE,
)


@dataclass
class BatchResult:
    """What one orchestration pass did, in money and counts."""

    evaluated: int = 0
    planned: int = 0
    executed: int = 0
    skipped: int = 0
    escalated: int = 0
    stopped: int = 0
    deferred: int = 0
    holdout_withheld: int = 0
    total_at_risk: Money = field(default_factory=Money.zero)
    estimated_spend: Money = field(default_factory=Money.zero)
    by_rule: dict[str, int] = field(default_factory=dict)


def assign_arms(session: Session, merchant_id: uuid.UUID, seed: int) -> int:
    """Assign every unassigned case to treatment or holdout.

    Runs before the first orchestration pass. Assignment is a pure function of
    the case id, so it is idempotent and a case can never drift between arms.
    """
    rows = (
        session.execute(select(RecoveryCase).where(RecoveryCase.merchant_id == merchant_id))
        .scalars()
        .all()
    )
    changed = 0
    for case in rows:
        arm = simulator.assign_arm(case.id, seed)
        if case.experiment_arm != arm:
            case.experiment_arm = arm
            session.add(case)
            changed += 1
    session.flush()
    return changed


def run_batch(
    session: Session,
    merchant: Merchant,
    *,
    now: datetime,
    limit: int = 500,
    provider: PaymentProvider | None = None,
) -> BatchResult:
    """Evaluate pending cases and execute whatever policy authorises.

    Holdout cases are evaluated — we still want their diagnosis and score for
    comparison — but never planned or executed. Withholding treatment is the
    entire point of the arm.
    """
    active_provider = provider if provider is not None else _default_provider()
    result = BatchResult()

    pending = (
        session.execute(
            select(RecoveryCase)
            .where(
                RecoveryCase.merchant_id == merchant.id,
                RecoveryCase.current_state.in_(PENDING_STATES),
            )
            .order_by(RecoveryCase.detected_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )

    for case in pending:
        outcome = evaluation.evaluate(session, case, merchant, now=now)
        decision = outcome.decision
        result.evaluated += 1
        result.total_at_risk = result.total_at_risk + Money(case.amount_at_risk_paise)

        for rule in decision.applied_rules:
            result.by_rule[rule] = result.by_rule.get(rule, 0) + 1

        if decision.is_deferred:
            result.deferred += 1
            continue
        if decision.next_state == CaseState.ESCALATED:
            result.escalated += 1
            continue
        if decision.next_state == CaseState.STOPPED:
            result.stopped += 1
            continue
        if decision.next_state != CaseState.ACTION_SELECTED:
            continue

        if case.experiment_arm == ExperimentArm.HOLDOUT:
            # Deliberately withheld. The case keeps its decision — we know what
            # the agent *would* have done — but nothing is executed.
            result.holdout_withheld += 1
            continue

        intervention = interventions.plan(session, case, decision, now=now)
        result.planned += 1

        execution = interventions.execute(session, intervention, merchant, active_provider, now=now)
        if execution.executed:
            result.executed += 1
            result.estimated_spend = result.estimated_spend + Money(
                intervention.estimated_cost_paise
            )
        else:
            result.skipped += 1

    session.flush()
    return result


def run_full_cycle(
    session: Session,
    merchant: Merchant,
    *,
    seed: int,
    now: datetime,
    rounds: int = 3,
    limit: int = 500,
) -> dict[str, object]:
    """Run the loop to convergence: act, observe, retry, until nothing moves.

    Several rounds are needed because a case that fails on attempt one becomes
    retry-eligible and re-enters the loop. Stops early when a round produces no
    executions, so the demo does not spin.
    """
    assign_arms(session, merchant.id, seed)

    rounds_run: list[dict[str, object]] = []
    for index in range(rounds):
        batch = run_batch(session, merchant, now=now, limit=limit)
        observed = simulator.simulate_outcomes(
            session, merchant.id, seed=seed + index, now=now, limit=limit
        )
        rounds_run.append(
            {
                "round": index + 1,
                "evaluated": batch.evaluated,
                "executed": batch.executed,
                "escalated": batch.escalated,
                "stopped": batch.stopped,
                "holdout_withheld": batch.holdout_withheld,
                "observed": observed.cases_observed,
                "recovered": observed.recovered,
                "recovered_amount_paise": observed.recovered_amount.paise,
            }
        )
        if batch.executed == 0 and observed.cases_observed == 0:
            break

    return {"rounds": rounds_run, "seed": seed}
