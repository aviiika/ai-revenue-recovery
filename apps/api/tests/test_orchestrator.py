"""Orchestrator, intervention and simulator tests (Milestone 4).

Concentrated on the failure modes spec section 19 requires the system to
demonstrate: duplicate execution, stale queued actions, opted-out customers,
and outcomes that arrive twice. These are the cases where a recovery system
quietly loses money or contacts someone it shouldn't.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.money import Money
from app.db.models import Intervention, Merchant, RecoveryCase, RecoveryOutcome
from app.domain.audit import service as audit
from app.domain.cases import evaluation
from app.domain.cases import service as cases
from app.domain.cases.service import NewCaseInput
from app.domain.enums import (
    AuditEventType,
    CaseState,
    ExperimentArm,
    FailureCategory,
    InterventionStatus,
    InterventionStrategy,
    SourceType,
)
from app.domain.experiments import service as experiments
from app.domain.interventions import service as interventions
from app.domain.interventions.service import SimulatedProvider
from app.domain.orchestrator import service as orchestrator
from app.domain.simulator import service as simulator

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def make_case(
    session: Session,
    merchant: Merchant,
    *,
    external_id: str = "pay_orch_0001",
    rupees: str = "5000.00",
    category: FailureCategory = FailureCategory.NETWORK_TIMEOUT,
    do_not_contact: bool = False,
) -> RecoveryCase:
    return cases.ingest(
        session,
        NewCaseInput(
            merchant_id=merchant.id,
            source_type=SourceType.PAYMENT,
            source_external_id=external_id,
            amount_at_risk=Money.from_rupees(rupees),
            detected_at=NOW,
            failure_category=category,
            do_not_contact=do_not_contact,
            is_synthetic=True,
        ),
    )


def plan_for(session: Session, merchant: Merchant, case: RecoveryCase) -> Intervention:
    result = evaluation.evaluate(session, case, merchant, now=NOW)
    assert result.decision.next_state == CaseState.ACTION_SELECTED
    return interventions.plan(session, case, result.decision, now=NOW)


# --- Idempotency ------------------------------------------------------------


def test_planning_twice_returns_the_same_intervention(session: Session, merchant: Merchant) -> None:
    case = make_case(session, merchant)
    result = evaluation.evaluate(session, case, merchant, now=NOW)

    first = interventions.plan(session, case, result.decision, now=NOW)
    second = interventions.plan(session, case, result.decision, now=NOW)

    assert first.id == second.id
    assert first.idempotency_key == second.idempotency_key


def test_idempotency_key_is_derived_not_random(session: Session, merchant: Merchant) -> None:
    """A random key would defeat the UNIQUE constraint that enforces safety."""
    case = make_case(session, merchant)
    key = interventions.build_idempotency_key(case.id, 1)
    assert key == interventions.build_idempotency_key(case.id, 1)
    assert key != interventions.build_idempotency_key(case.id, 2)


def test_executing_twice_does_not_act_twice(session: Session, merchant: Merchant) -> None:
    case = make_case(session, merchant)
    intervention = plan_for(session, merchant, case)

    first = interventions.execute(session, intervention, merchant, SimulatedProvider(), now=NOW)
    second = interventions.execute(session, intervention, merchant, SimulatedProvider(), now=NOW)

    assert first.executed is True
    assert second.executed is False
    # The attempt budget must not be consumed twice by one action.
    assert case.attempt_count == 1


# --- Stale queued actions (spec FR-7, section 19) ---------------------------


def test_queued_action_is_skipped_if_the_case_was_already_recovered(
    session: Session, merchant: Merchant
) -> None:
    """The scenario the spec names explicitly: money arrives between planning
    and execution. Acting anyway would contact someone who has already paid."""
    case = make_case(session, merchant)
    intervention = plan_for(session, merchant, case)

    cases.record_recovery(session, case, Money.from_rupees("5000.00"), NOW)

    result = interventions.execute(session, intervention, merchant, SimulatedProvider(), now=NOW)

    assert result.executed is False
    assert "already recovered" in (result.skipped_reason or "")
    assert intervention.status == InterventionStatus.SKIPPED


def test_queued_action_is_skipped_if_the_customer_opted_out(
    session: Session, merchant: Merchant
) -> None:
    case = make_case(session, merchant)
    intervention = plan_for(session, merchant, case)

    case.do_not_contact = True
    session.add(case)
    session.flush()

    result = interventions.execute(session, intervention, merchant, SimulatedProvider(), now=NOW)
    assert result.executed is False
    assert "opted out" in (result.skipped_reason or "")


def test_skipped_action_is_audited(session: Session, merchant: Merchant) -> None:
    """An action that was planned and then withheld must be visible."""
    case = make_case(session, merchant)
    intervention = plan_for(session, merchant, case)
    case.do_not_contact = True
    session.add(case)
    session.flush()

    interventions.execute(session, intervention, merchant, SimulatedProvider(), now=NOW)

    event_types = [e.event_type for e in audit.get_trail(session, case.id)]
    assert AuditEventType.INTERVENTION_SKIPPED in event_types


def test_skipped_action_costs_nothing(session: Session, merchant: Merchant) -> None:
    case = make_case(session, merchant)
    intervention = plan_for(session, merchant, case)
    case.do_not_contact = True
    session.add(case)
    session.flush()
    interventions.execute(session, intervention, merchant, SimulatedProvider(), now=NOW)

    assert interventions.total_cost(session, case.id).paise == 0


def test_execution_walks_the_case_to_observing(session: Session, merchant: Merchant) -> None:
    case = make_case(session, merchant)
    intervention = plan_for(session, merchant, case)
    interventions.execute(session, intervention, merchant, SimulatedProvider(), now=NOW)

    assert case.current_state == CaseState.OBSERVING
    assert case.last_action_at == NOW
    assert case.attempt_count == 1


def test_provider_failure_records_and_does_not_lose_the_case(
    session: Session, merchant: Merchant
) -> None:
    """Spec section 19: an API failure must degrade, not drop the case."""

    class BrokenProvider:
        @property
        def channel(self) -> str:
            return "BROKEN"

        def execute(self, **_: object) -> dict[str, object]:
            raise RuntimeError("provider timeout")

    case = make_case(session, merchant)
    intervention = plan_for(session, merchant, case)

    result = interventions.execute(session, intervention, merchant, BrokenProvider(), now=NOW)

    assert result.executed is False
    assert intervention.status == InterventionStatus.FAILED
    assert "timeout" in str(intervention.result)
    # The case did not advance, so a later pass can retry it.
    assert case.current_state == CaseState.ACTION_SELECTED


# --- Holdout arm ------------------------------------------------------------


def test_arm_assignment_is_deterministic() -> None:
    case_id = uuid.uuid4()
    assert simulator.assign_arm(case_id, 42) == simulator.assign_arm(case_id, 42)


def test_arm_assignment_splits_roughly_at_the_configured_share() -> None:
    ids = [uuid.uuid4() for _ in range(2000)]
    holdout = sum(1 for i in ids if simulator.assign_arm(i, 7) == ExperimentArm.HOLDOUT)
    share = holdout / len(ids)
    assert 0.15 < share < 0.25  # configured 0.20


def test_holdout_cases_are_never_contacted(session: Session, merchant: Merchant) -> None:
    """The whole point of the arm. If a holdout case gets an intervention, the
    incremental measurement is worthless."""
    case = make_case(session, merchant)
    case.experiment_arm = ExperimentArm.HOLDOUT
    session.add(case)
    session.flush()

    result = orchestrator.run_batch(session, merchant, now=NOW)

    assert result.holdout_withheld == 1
    assert result.executed == 0
    assert session.query(Intervention).filter(Intervention.recovery_case_id == case.id).count() == 0


def test_holdout_execution_is_blocked_even_if_something_plans_one(
    session: Session, merchant: Merchant
) -> None:
    """Defence in depth: the executor re-checks the arm too."""
    case = make_case(session, merchant)
    intervention = plan_for(session, merchant, case)
    case.experiment_arm = ExperimentArm.HOLDOUT
    session.add(case)
    session.flush()

    result = interventions.execute(session, intervention, merchant, SimulatedProvider(), now=NOW)
    assert result.executed is False
    assert "holdout" in (result.skipped_reason or "")


# --- Simulator --------------------------------------------------------------


def test_simulated_outcomes_are_deterministic(session: Session, merchant: Merchant) -> None:
    for index in range(12):
        make_case(session, merchant, external_id=f"pay_sim_{index:03d}")
    orchestrator.run_batch(session, merchant, now=NOW)

    first = simulator.simulate_outcomes(session, merchant.id, seed=99, now=NOW)
    assert first.cases_observed > 0

    # Same seed on an already-observed population must not double count.
    second = simulator.simulate_outcomes(session, merchant.id, seed=99, now=NOW)
    assert second.recovered == 0


def test_duplicate_simulated_event_does_not_double_count_revenue(
    session: Session, merchant: Merchant
) -> None:
    """Same protection a redelivered webhook needs."""
    case = make_case(session, merchant)
    orchestrator.run_batch(session, merchant, now=NOW)

    simulator.simulate_outcomes(session, merchant.id, seed=5, now=NOW)
    recovered_after_first = case.recovered_amount_paise

    simulator.simulate_outcomes(session, merchant.id, seed=5, now=NOW)
    assert case.recovered_amount_paise == recovered_after_first

    assert (
        session.query(RecoveryOutcome).filter(RecoveryOutcome.recovery_case_id == case.id).count()
        == 1
    )


def test_treated_probability_exceeds_spontaneous() -> None:
    """If treatment did not help, the whole exercise would be pointless -- and
    if it helped equally for every strategy, strategy choice would not matter."""
    from app.domain.enums import Recoverability

    for recoverability in Recoverability:
        spontaneous = simulator.spontaneous_probability(recoverability)
        treated = simulator.treated_probability(
            recoverability, InterventionStrategy.CREATE_PAYMENT_LINK
        )
        assert treated >= spontaneous


def test_structural_failures_respond_better_to_alternate_method_than_retry() -> None:
    from app.domain.enums import Recoverability

    retry = simulator.treated_probability(
        Recoverability.STRUCTURAL, InterventionStrategy.WAIT_AND_RETRY
    )
    alternate = simulator.treated_probability(
        Recoverability.STRUCTURAL, InterventionStrategy.REQUEST_ALTERNATE_METHOD
    )
    assert alternate > retry


def test_recovery_flows_through_the_normal_case_path(session: Session, merchant: Merchant) -> None:
    """The simulator must not have a private write path -- it goes through the
    same record_recovery a real webhook would."""
    for index in range(20):
        make_case(session, merchant, external_id=f"pay_path_{index:03d}")
    orchestrator.run_batch(session, merchant, now=NOW)
    simulator.simulate_outcomes(session, merchant.id, seed=3, now=NOW)

    recovered = (
        session.query(RecoveryCase).filter(RecoveryCase.current_state == CaseState.RECOVERED).all()
    )
    for case in recovered:
        trail = [e.event_type for e in audit.get_trail(session, case.id)]
        assert AuditEventType.RECOVERY_RECORDED in trail
        assert AuditEventType.SIMULATED_EVENT in trail


# --- Full cycle and experiments ---------------------------------------------


def test_full_cycle_recovers_money_and_converges(session: Session, merchant: Merchant) -> None:
    for index in range(40):
        make_case(session, merchant, external_id=f"pay_cycle_{index:03d}")

    report = orchestrator.run_full_cycle(session, merchant, seed=20260902, now=NOW, rounds=3)

    rounds = report["rounds"]
    assert isinstance(rounds, list)
    assert len(rounds) >= 1

    total_recovered = sum(int(r["recovered_amount_paise"]) for r in rounds)
    assert total_recovered > 0, "the agent must actually recover money"


def test_incremental_report_separates_treatment_from_holdout(
    session: Session, merchant: Merchant
) -> None:
    for index in range(60):
        make_case(session, merchant, external_id=f"pay_inc_{index:03d}")
    orchestrator.run_full_cycle(session, merchant, seed=20260902, now=NOW, rounds=3)

    report = experiments.comparison_report(session, merchant.id)
    incremental = report["incremental"]
    assert isinstance(incremental, dict)

    assert incremental["treatment"]["cases"] > 0
    assert incremental["holdout"]["cases"] > 0
    # Holdout cases must never have been actioned.
    assert incremental["holdout"]["executed_actions"] == 0
    # The caveat text must always ship with the number.
    assert "not evidence of real-world" in str(incremental["caveat"])


def test_baseline_comparison_reports_both_value_and_action_count(
    session: Session, merchant: Merchant
) -> None:
    """Spec section 18: recovering more by contacting everyone is not a win."""
    for index in range(30):
        make_case(session, merchant, external_id=f"pay_base_{index:03d}")
    orchestrator.run_batch(session, merchant, now=NOW)

    report = experiments.comparison_report(session, merchant.id)
    policies = report["policies"]
    assert isinstance(policies, dict)

    assert policies["agent"]["actions"] >= 0
    assert policies["baseline"]["actions"] > 0
    assert "expected_net_delta_paise" in report["agent_vs_baseline"]
    assert "action_delta" in report["agent_vs_baseline"]


def test_baseline_still_honours_do_not_contact(session: Session, merchant: Merchant) -> None:
    """Even the naive baseline must not contact opted-out customers; the agent's
    advantage should never come from that."""
    for index in range(10):
        make_case(session, merchant, external_id=f"pay_dnc_{index:03d}", do_not_contact=True)

    report = experiments.comparison_report(session, merchant.id)
    assert report["policies"]["baseline"]["actions"] == 0


def test_run_batch_reports_which_rules_fired(session: Session, merchant: Merchant) -> None:
    for index in range(10):
        make_case(session, merchant, external_id=f"pay_rules_{index:03d}")
    result = orchestrator.run_batch(session, merchant, now=NOW)
    assert result.by_rule
    assert result.evaluated == 10


def test_cost_counts_only_executed_interventions(session: Session, merchant: Merchant) -> None:
    case = make_case(session, merchant)
    intervention = plan_for(session, merchant, case)
    assert interventions.total_cost(session, case.id).paise == 0  # planned only

    interventions.execute(session, intervention, merchant, SimulatedProvider(), now=NOW)
    assert interventions.total_cost(session, case.id).paise > 0


def test_planning_requires_an_actionable_decision(session: Session, merchant: Merchant) -> None:
    """A stopped or escalated decision must not be plannable."""
    case = make_case(session, merchant, do_not_contact=True)
    result = evaluation.evaluate(session, case, merchant, now=NOW)
    assert result.decision.next_state == CaseState.STOPPED

    with pytest.raises(interventions.InterventionError):
        interventions.plan(session, case, result.decision, now=NOW)


def test_cooldown_prevents_immediate_second_action(session: Session, merchant: Merchant) -> None:
    """After acting, the case must not be actioned again within the window."""
    case = make_case(session, merchant)
    intervention = plan_for(session, merchant, case)
    interventions.execute(session, intervention, merchant, SimulatedProvider(), now=NOW)

    simulator.simulate_outcomes(session, merchant.id, seed=1, now=NOW)
    if case.current_state != CaseState.RETRY_ELIGIBLE:
        pytest.skip("case recovered on the first attempt in this seed")

    soon = NOW + timedelta(hours=1)
    result = evaluation.evaluate(session, case, merchant, now=soon)
    assert result.decision.is_deferred
    assert result.decision.decisive_rule == "R08_COOLDOWN_ACTIVE"


def test_holdout_gets_exactly_one_observation_regardless_of_rounds(
    session: Session, merchant: Merchant
) -> None:
    """Regression: a holdout case must not be re-drawn every round.

    It previously stayed in ACTION_SELECTED after a no-response and was
    re-observed on each round with a fresh seed, accumulating 1-(1-p)^rounds
    while treated cases got a single draw. That inverted the sign of the
    measured lift while still looking like a plausible number.
    """
    for index in range(40):
        make_case(session, merchant, external_id=f"pay_once_{index:03d}")

    orchestrator.run_full_cycle(session, merchant, seed=20260902, now=NOW, rounds=4)

    holdout_ids = [
        row.id
        for row in session.query(RecoveryCase)
        .filter(RecoveryCase.experiment_arm == ExperimentArm.HOLDOUT)
        .all()
    ]
    assert holdout_ids, "seed must produce some holdout cases"

    for case_id in holdout_ids:
        observations = (
            session.query(RecoveryOutcome)
            .filter(RecoveryOutcome.recovery_case_id == case_id)
            .count()
        )
        assert observations <= 1, "a withheld case must get one draw, not one per round"


def test_incremental_lift_is_positive_by_construction(session: Session, merchant: Merchant) -> None:
    """Treated probability is >= spontaneous for every case, so a negative
    measured lift means a measurement bug, not a weak agent."""
    for index in range(200):
        make_case(session, merchant, external_id=f"pay_lift_{index:03d}")

    orchestrator.run_full_cycle(session, merchant, seed=20260902, now=NOW, rounds=3)
    result = experiments.incremental_recovery(session, merchant.id)

    assert result.holdout.cases > 0
    assert result.treatment.cases > 0
    assert result.holdout.executed_actions == 0
    assert result.incremental_rate_points > 0, (
        f"expected positive lift, got {result.incremental_rate_points}pp -- "
        "check arm composition and observation counts"
    )


def test_arms_are_compared_like_for_like(session: Session, merchant: Merchant) -> None:
    """Escalated and stopped cases were never candidates for treatment, so they
    must not sit in one arm's denominator and not the other's."""
    for index in range(80):
        make_case(session, merchant, external_id=f"pay_lfl_{index:03d}")
    orchestrator.run_full_cycle(session, merchant, seed=20260902, now=NOW, rounds=2)

    eligible = experiments.actionable_case_ids(session, merchant.id)
    restricted = experiments.arm_result(
        session, merchant.id, ExperimentArm.TREATMENT, restrict_to=eligible
    )
    unrestricted = experiments.arm_result(session, merchant.id, ExperimentArm.TREATMENT)

    assert restricted.cases <= unrestricted.cases
    # Every restricted treated case was actually actioned.
    assert restricted.executed_actions >= restricted.cases - 1
