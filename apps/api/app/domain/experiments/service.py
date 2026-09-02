"""Experiment evaluation (spec section 18, and the 10.12 causal caveat).

Answers two different questions that are easy to conflate:

1. **Did the agent beat a naive baseline?** Compared on the same seeded
   population, counting actions as well as money — a policy that recovers
   slightly more by contacting everyone twice is not obviously better.
2. **Did the agent cause the recovery at all?** Answered by the randomised
   holdout, not by assumption. This is the number that survives a sceptical
   question.

Every figure is computed from stored rows. Nothing here is hardcoded.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.money import Money, SignedMoney
from app.db.models import AuditEvent, Intervention, RecoveryCase
from app.domain.enums import (
    CATEGORY_RECOVERABILITY,
    AuditEventType,
    CaseState,
    ExperimentArm,
    FailureCategory,
    InterventionStatus,
    InterventionStrategy,
    Recoverability,
)
from app.domain.scoring.service import intervention_cost
from app.domain.simulator.service import spontaneous_probability, treated_probability


@dataclass(frozen=True, slots=True)
class ArmResult:
    """Observed performance of one experimental arm."""

    arm: str
    cases: int
    recovered: int
    at_risk: Money
    recovered_amount: Money
    executed_actions: int
    spend: Money

    @property
    def recovery_rate_by_count(self) -> float:
        return (self.recovered / self.cases) if self.cases else 0.0

    @property
    def recovery_rate_by_value(self) -> float:
        return self.recovered_amount.paise / self.at_risk.paise if self.at_risk.paise else 0.0


@dataclass(frozen=True, slots=True)
class IncrementalResult:
    """The causal comparison the holdout makes possible."""

    treatment: ArmResult
    holdout: ArmResult
    #: Percentage-point difference in recovery rate between arms.
    incremental_rate_points: float
    #: That difference applied to the treated population's value at risk.
    incremental_value: SignedMoney
    net_incremental_value: SignedMoney
    holdout_share: float
    caveat: str


def actionable_case_ids(session: Session, merchant_id: uuid.UUID) -> set[uuid.UUID]:
    """Cases the agent decided to act on, in either arm.

    Identified by the ``INTERVENTION_SELECTED`` audit event, which the evaluator
    writes whenever policy chose an action -- *before* the orchestrator checks
    which arm the case belongs to. So it marks intent to treat identically on
    both sides of the experiment.
    """
    rows = (
        session.execute(
            select(AuditEvent.recovery_case_id)
            .join(RecoveryCase, AuditEvent.recovery_case_id == RecoveryCase.id)
            .where(
                RecoveryCase.merchant_id == merchant_id,
                AuditEvent.event_type == AuditEventType.INTERVENTION_SELECTED,
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


def arm_result(
    session: Session,
    merchant_id: uuid.UUID,
    arm: ExperimentArm,
    *,
    restrict_to: set[uuid.UUID] | None = None,
) -> ArmResult:
    """Aggregate one arm from stored rows.

    ``restrict_to`` narrows to cases the agent intended to act on. Without it
    the arms are not comparable: the treated arm accumulates escalated and
    stopped cases that were never actioned and so recover at zero, dragging its
    rate below a holdout that happened to contain fewer of them. That is a
    composition artefact, not a treatment effect -- and it is large enough to
    invert the sign of the measured lift.
    """
    rows = list(
        session.execute(
            select(RecoveryCase).where(
                RecoveryCase.merchant_id == merchant_id,
                RecoveryCase.experiment_arm == arm,
            )
        )
        .scalars()
        .all()
    )
    if restrict_to is not None:
        rows = [row for row in rows if row.id in restrict_to]
    case_ids = [row.id for row in rows]

    executed = 0
    spend = 0
    if case_ids:
        executed_rows = session.execute(
            select(
                func.count(Intervention.id),
                func.coalesce(func.sum(Intervention.estimated_cost_paise), 0),
            ).where(
                Intervention.recovery_case_id.in_(case_ids),
                Intervention.status == InterventionStatus.EXECUTED,
            )
        ).one()
        executed, spend = int(executed_rows[0]), int(executed_rows[1])

    return ArmResult(
        arm=str(arm),
        cases=len(rows),
        recovered=sum(1 for r in rows if r.current_state == CaseState.RECOVERED),
        at_risk=Money(sum(r.amount_at_risk_paise for r in rows)),
        recovered_amount=Money(sum(r.recovered_amount_paise for r in rows)),
        executed_actions=executed,
        spend=Money(spend),
    )


def incremental_recovery(session: Session, merchant_id: uuid.UUID) -> IncrementalResult:
    """Measure what the agent actually *caused*.

    The treated arm's recovery rate minus the holdout's is the incremental
    effect. Applying that difference — rather than the gross recovered total —
    to the treated value at risk gives a figure that survives the question
    "wouldn't they have paid anyway?".
    """
    # Like-for-like: only cases the agent intended to act on. In the treated
    # arm it acted; in the holdout it was withheld. Everything else (escalated,
    # stopped) was never a candidate for treatment in either arm.
    eligible = actionable_case_ids(session, merchant_id)
    treatment = arm_result(session, merchant_id, ExperimentArm.TREATMENT, restrict_to=eligible)
    holdout = arm_result(session, merchant_id, ExperimentArm.HOLDOUT, restrict_to=eligible)

    lift = treatment.recovery_rate_by_value - holdout.recovery_rate_by_value
    incremental_paise = round(treatment.at_risk.paise * lift)
    incremental = SignedMoney(incremental_paise)
    net = incremental - SignedMoney.of(treatment.spend)

    total_cases = treatment.cases + holdout.cases
    caveat = (
        "Incremental recovery is the difference in recovery rate between the "
        "randomised treatment and holdout arms, restricted on both sides to "
        "cases the agent intended to act on. It is measured within a synthetic "
        "simulation, on a small sample, and carries no confidence interval; it "
        "is not evidence of real-world production uplift."
    )
    if holdout.cases < 20:
        caveat = (
            f"Holdout arm has only {holdout.cases} cases -- too few to read the "
            "difference as meaningful. Seed a larger batch before quoting this. "
        ) + caveat

    return IncrementalResult(
        treatment=treatment,
        holdout=holdout,
        incremental_rate_points=round(lift * 100, 2),
        incremental_value=incremental,
        net_incremental_value=net,
        holdout_share=round(holdout.cases / total_cases, 4) if total_cases else 0.0,
        caveat=caveat,
    )


# ---------------------------------------------------------------------------
# Baseline policy comparison (spec section 18)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicySimulation:
    """Expected performance of one policy over a population.

    Analytic rather than executed: for each case we compute the probability the
    policy's chosen action would succeed, using the same ground-truth model the
    simulator draws from. This compares policies on identical cases without the
    sampling noise of running each one, which matters at n=120.
    """

    policy: str
    cases: int
    actions: int
    expected_recovered: Money
    spend: Money
    expected_net: SignedMoney
    contacts: int

    @property
    def actions_per_case(self) -> float:
        return (self.actions / self.cases) if self.cases else 0.0


#: The baseline from spec section 18: retry transient failures, send one generic
#: reminder to everything else, then stop. No scoring, no economics, no
#: guardrails beyond do-not-contact.
def _baseline_action(recoverability: Recoverability) -> InterventionStrategy:
    if recoverability == Recoverability.TRANSIENT:
        return InterventionStrategy.WAIT_AND_RETRY
    return InterventionStrategy.SEND_REMINDER_SIMULATED


def simulate_policies(session: Session, merchant_id: uuid.UUID) -> dict[str, PolicySimulation]:
    """Compare the agent's realised choices against the naive baseline.

    The agent column reflects what the policy engine actually decided for each
    case (read from stored interventions). The baseline column applies the
    section 18 rule to the same cases.
    """
    rows = (
        session.execute(select(RecoveryCase).where(RecoveryCase.merchant_id == merchant_id))
        .scalars()
        .all()
    )

    chosen: dict[uuid.UUID, InterventionStrategy] = {}
    for case_id, strategy in session.execute(
        select(Intervention.recovery_case_id, Intervention.strategy).where(
            Intervention.status == InterventionStatus.EXECUTED
        )
    ).all():
        chosen[case_id] = InterventionStrategy(strategy)

    agent = _simulate_one("agent", rows, chosen, baseline=False)
    baseline = _simulate_one("baseline", rows, chosen, baseline=True)
    return {"agent": agent, "baseline": baseline}


def _simulate_one(
    name: str,
    rows: Sequence[RecoveryCase],
    chosen: dict[uuid.UUID, InterventionStrategy],
    *,
    baseline: bool,
) -> PolicySimulation:
    actions = 0
    contacts = 0
    expected = 0
    spend = 0

    for case in rows:
        recoverability = Recoverability(
            case.recoverability or CATEGORY_RECOVERABILITY[FailureCategory(case.failure_category)]
        )

        if case.do_not_contact:
            # Both policies honour an opt-out. The difference between them is
            # never about contacting people who said no.
            continue

        if baseline:
            strategy = _baseline_action(recoverability)
        else:
            selected = chosen.get(case.id)
            if selected is None:
                # The agent deliberately did nothing here -- stopped, escalated
                # or held out. That restraint is the thing being measured.
                continue
            strategy = selected

        actions += 1
        if strategy != InterventionStrategy.WAIT_AND_RETRY:
            contacts += 1
        cost = intervention_cost(strategy)
        spend += cost.paise

        probability = treated_probability(recoverability, strategy)
        expected += round(Decimal(case.amount_at_risk_paise) * probability)

    return PolicySimulation(
        policy=name,
        cases=len(rows),
        actions=actions,
        expected_recovered=Money(expected),
        spend=Money(spend),
        expected_net=SignedMoney(expected) - SignedMoney(spend),
        contacts=contacts,
    )


def comparison_report(session: Session, merchant_id: uuid.UUID) -> dict[str, object]:
    """The full experiment view for the API and the demo."""
    policies = simulate_policies(session, merchant_id)
    incremental = incremental_recovery(session, merchant_id)

    agent, baseline = policies["agent"], policies["baseline"]
    value_delta = agent.expected_net.paise - baseline.expected_net.paise
    action_delta = agent.actions - baseline.actions

    return {
        "synthetic": True,
        "policies": {
            name: {
                "policy": p.policy,
                "cases": p.cases,
                "actions": p.actions,
                "contacts": p.contacts,
                "expected_recovered_paise": p.expected_recovered.paise,
                "spend_paise": p.spend.paise,
                "expected_net_paise": p.expected_net.paise,
                "actions_per_case": round(p.actions_per_case, 4),
            }
            for name, p in policies.items()
        },
        "agent_vs_baseline": {
            "expected_net_delta_paise": value_delta,
            "action_delta": action_delta,
            "net_uplift_pct": (
                round(value_delta / baseline.expected_net.paise * 100, 2)
                if baseline.expected_net.paise
                else 0.0
            ),
            "fewer_actions_pct": (
                round(-action_delta / baseline.actions * 100, 2) if baseline.actions else 0.0
            ),
        },
        "incremental": {
            "treatment": _arm_dict(incremental.treatment),
            "holdout": _arm_dict(incremental.holdout),
            "incremental_rate_points": incremental.incremental_rate_points,
            "incremental_value_paise": incremental.incremental_value.paise,
            "net_incremental_value_paise": incremental.net_incremental_value.paise,
            "holdout_share": incremental.holdout_share,
            "caveat": incremental.caveat,
        },
        "spontaneous_recovery_note": (
            "The simulator models a spontaneous recovery rate -- customers who "
            "would have paid without any contact. Gross recovered totals include "
            "those; only the incremental figures net them out."
        ),
        "assumed_spontaneous_rate": {
            str(r): str(round(spontaneous_probability(r), 4)) for r in Recoverability
        },
    }


def _arm_dict(arm: ArmResult) -> dict[str, object]:
    return {
        "arm": arm.arm,
        "cases": arm.cases,
        "recovered": arm.recovered,
        "at_risk_paise": arm.at_risk.paise,
        "recovered_paise": arm.recovered_amount.paise,
        "executed_actions": arm.executed_actions,
        "spend_paise": arm.spend.paise,
        "recovery_rate_by_count": round(arm.recovery_rate_by_count, 4),
        "recovery_rate_by_value": round(arm.recovery_rate_by_value, 4),
    }
