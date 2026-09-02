"""Policy engine tests (spec section 12).

The engine is the component with final authority over what the agent does, so
each rule gets a test that would fail if the rule stopped firing, and the
ordering between rules is tested explicitly -- ordering bugs are the dangerous
kind here (an opted-out customer being contacted because value was checked
first).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.money import Money
from app.domain.enums import CaseState, FailureCategory, InterventionStrategy, Recoverability
from app.domain.policies.config import PolicyConfig
from app.domain.policies.engine import CaseSnapshot, RuleId, decide
from app.domain.scoring.service import DeterministicScorer, score_all

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def snapshot(**overrides: object) -> CaseSnapshot:
    defaults: dict[str, object] = {
        "state": CaseState.SCORED,
        "amount_at_risk": Money.from_rupees("5000.00"),
        "recovered_amount": Money.zero(),
        "recoverability": Recoverability.ACTIONABLE,
        "attempt_count": 0,
        "do_not_contact": False,
        "last_action_at": None,
        "evaluated_at": NOW,
    }
    defaults.update(overrides)
    return CaseSnapshot(**defaults)  # type: ignore[arg-type]


def scored_for(
    recoverability: Recoverability = Recoverability.ACTIONABLE,
    amount: Money | None = None,
    attempt_count: int = 0,
) -> list:
    return score_all(
        DeterministicScorer(),
        failure_category=FailureCategory.INSUFFICIENT_FUNDS,
        amount_at_risk=amount or Money.from_rupees("5000.00"),
        attempt_count=attempt_count,
        recoverability=recoverability,
    )


def config(**overrides: object) -> PolicyConfig:
    return PolicyConfig(**overrides)  # type: ignore[arg-type]


# --- Rule 1: already recovered ---------------------------------------------


def test_recovered_case_is_stopped() -> None:
    decision = decide(
        snapshot(recovered_amount=Money.from_rupees("5000.00")), scored_for(), config()
    )
    assert decision.recommended_strategy is InterventionStrategy.STOP_RECOVERY
    assert decision.next_state is CaseState.STOPPED
    assert decision.decisive_rule == RuleId.ALREADY_RECOVERED


# --- Rule 2: do-not-contact -------------------------------------------------


def test_do_not_contact_stops_recovery() -> None:
    decision = decide(snapshot(do_not_contact=True), scored_for(), config())
    assert decision.next_state is CaseState.STOPPED
    assert decision.decisive_rule == RuleId.DO_NOT_CONTACT
    # Every candidate must be explicitly blocked, with a reason.
    assert len(decision.blocked) == len(scored_for())
    assert all(b.rule_id == RuleId.DO_NOT_CONTACT for b in decision.blocked)


def test_do_not_contact_beats_a_highly_profitable_case() -> None:
    """Ordering test: opt-out is checked before economics.

    A huge, high-confidence, obviously profitable case must still be stopped.
    """
    decision = decide(
        snapshot(
            do_not_contact=True,
            amount_at_risk=Money.from_rupees("500000.00"),
            recoverability=Recoverability.TRANSIENT,
        ),
        scored_for(Recoverability.TRANSIENT, Money.from_rupees("500000.00")),
        config(),
    )
    assert decision.next_state is CaseState.STOPPED
    assert decision.decisive_rule == RuleId.DO_NOT_CONTACT


# --- Rule 4: attempt budget -------------------------------------------------


def test_max_attempts_escalates_to_human() -> None:
    decision = decide(snapshot(attempt_count=3), scored_for(attempt_count=3), config())
    assert decision.requires_human
    assert decision.next_state is CaseState.ESCALATED
    assert decision.decisive_rule == RuleId.MAX_ATTEMPTS


def test_attempts_below_the_cap_still_act() -> None:
    decision = decide(
        snapshot(attempt_count=1, recoverability=Recoverability.TRANSIENT),
        scored_for(Recoverability.TRANSIENT, attempt_count=1),
        config(),
    )
    assert decision.next_state is CaseState.ACTION_SELECTED


def test_max_attempts_is_merchant_configurable() -> None:
    decision = decide(
        snapshot(attempt_count=1), scored_for(attempt_count=1), config(max_automated_attempts=1)
    )
    assert decision.decisive_rule == RuleId.MAX_ATTEMPTS


# --- Rule 5: unrecoverable --------------------------------------------------


def test_unrecoverable_diagnosis_stops_rather_than_spends() -> None:
    decision = decide(
        snapshot(recoverability=Recoverability.UNRECOVERABLE),
        scored_for(Recoverability.UNRECOVERABLE),
        config(),
    )
    assert decision.next_state is CaseState.STOPPED
    assert decision.decisive_rule == RuleId.UNRECOVERABLE


# --- Rule 6: expected value -------------------------------------------------


def test_non_positive_expected_value_stops() -> None:
    """A tiny ticket cannot pay for the intervention that would recover it."""
    tiny = Money.from_rupees("1.00")
    decision = decide(
        snapshot(amount_at_risk=tiny, recoverability=Recoverability.STRUCTURAL),
        scored_for(Recoverability.STRUCTURAL, tiny),
        config(),
    )
    assert decision.next_state is CaseState.STOPPED
    assert decision.decisive_rule == RuleId.NEGATIVE_EXPECTED_VALUE
    assert all(b.rule_id == RuleId.NEGATIVE_EXPECTED_VALUE for b in decision.blocked)


def test_expected_value_bar_is_configurable() -> None:
    """Raising the bar turns a marginal case into a stop."""
    amount = Money.from_rupees("5000.00")
    permissive = decide(snapshot(), scored_for(amount=amount), config())
    assert permissive.next_state is CaseState.ACTION_SELECTED

    strict = decide(
        snapshot(),
        scored_for(amount=amount),
        config(min_expected_net_recovery_paise=100_000_000),
    )
    assert strict.next_state is CaseState.STOPPED
    assert strict.decisive_rule == RuleId.NEGATIVE_EXPECTED_VALUE


# --- Rule 7: confidence and high value --------------------------------------


def test_high_value_low_confidence_goes_to_a_human() -> None:
    big = Money.from_rupees("100000.00")
    decision = decide(
        snapshot(amount_at_risk=big, recoverability=Recoverability.STRUCTURAL),
        scored_for(Recoverability.STRUCTURAL, big),
        config(high_value_threshold_paise=1_000_000, min_auto_action_confidence=0.9),
    )
    assert decision.requires_human
    assert decision.next_state is CaseState.ESCALATED
    assert decision.decisive_rule == RuleId.HIGH_VALUE_LOW_CONFIDENCE


def test_low_confidence_alone_still_escalates() -> None:
    decision = decide(
        snapshot(recoverability=Recoverability.STRUCTURAL),
        scored_for(Recoverability.STRUCTURAL),
        config(min_auto_action_confidence=0.99, high_value_threshold_paise=10**12),
    )
    assert decision.requires_human
    assert decision.decisive_rule == RuleId.LOW_CONFIDENCE


def test_high_value_with_high_confidence_is_automated() -> None:
    """High value alone must not escalate -- only high value *and* low confidence."""
    big = Money.from_rupees("100000.00")
    decision = decide(
        snapshot(amount_at_risk=big, recoverability=Recoverability.TRANSIENT),
        scored_for(Recoverability.TRANSIENT, big),
        config(high_value_threshold_paise=1_000_000, min_auto_action_confidence=0.05),
    )
    assert not decision.requires_human
    assert decision.next_state is CaseState.ACTION_SELECTED


# --- Rule 8: cooldown -------------------------------------------------------


def test_cooldown_defers_instead_of_contacting_again() -> None:
    decision = decide(
        snapshot(last_action_at=NOW - timedelta(hours=2), recoverability=Recoverability.TRANSIENT),
        scored_for(Recoverability.TRANSIENT),
        config(cooldown_hours=24),
    )
    assert decision.is_deferred
    assert decision.next_state is None
    assert decision.recommended_strategy is InterventionStrategy.WAIT_AND_RETRY
    assert decision.decisive_rule == RuleId.COOLDOWN_ACTIVE
    assert decision.retry_after == NOW + timedelta(hours=22)


def test_expired_cooldown_permits_action() -> None:
    decision = decide(
        snapshot(last_action_at=NOW - timedelta(hours=30), recoverability=Recoverability.TRANSIENT),
        scored_for(Recoverability.TRANSIENT),
        config(cooldown_hours=24),
    )
    assert decision.next_state is CaseState.ACTION_SELECTED


# --- Rule 9: merchant-disabled strategies -----------------------------------


def test_disabled_strategy_is_blocked_even_when_it_ranks_first() -> None:
    only_reminders = config(
        enabled_strategies=frozenset({InterventionStrategy.SEND_REMINDER_SIMULATED})
    )
    decision = decide(snapshot(), scored_for(), only_reminders)

    assert decision.recommended_strategy is InterventionStrategy.SEND_REMINDER_SIMULATED
    blocked_names = {b.strategy for b in decision.blocked}
    assert InterventionStrategy.CREATE_PAYMENT_LINK in blocked_names
    assert RuleId.STRATEGY_DISABLED in decision.applied_rules


# --- Happy path and shape ---------------------------------------------------


def test_transient_failure_prefers_retry() -> None:
    decision = decide(
        snapshot(recoverability=Recoverability.TRANSIENT),
        scored_for(Recoverability.TRANSIENT),
        config(),
    )
    assert decision.recommended_strategy is InterventionStrategy.WAIT_AND_RETRY
    assert RuleId.TRANSIENT_RETRY in decision.applied_rules


def test_structural_failure_prefers_alternate_method_over_retry() -> None:
    """A revoked mandate will not fix itself on a retry."""
    big = Money.from_rupees("50000.00")
    decision = decide(
        snapshot(recoverability=Recoverability.STRUCTURAL, amount_at_risk=big),
        scored_for(Recoverability.STRUCTURAL, big),
        config(min_auto_action_confidence=0.0, high_value_threshold_paise=10**12),
    )
    assert decision.recommended_strategy is InterventionStrategy.REQUEST_ALTERNATE_METHOD


def test_decision_always_names_the_rules_it_applied() -> None:
    decision = decide(snapshot(), scored_for(), config())
    assert decision.applied_rules
    assert decision.explanation.strip()


def test_audit_payload_is_json_safe_and_complete() -> None:
    """Spec FR-8 requires policy checks to be reconstructable from the trail."""
    import json

    decision = decide(snapshot(), scored_for(), config())
    payload = decision.to_audit_payload()
    json.dumps(payload)  # must not raise

    assert payload["recommended_strategy"]
    assert payload["applied_rules"]
    assert isinstance(payload["allowed"], list)
    assert payload["allowed"]


def test_engine_never_declines_to_decide() -> None:
    """Every reachable combination yields a decision -- 'do nothing' is explicit."""
    for recoverability in Recoverability:
        for attempts in (0, 1, 3, 9):
            for dnc in (True, False):
                decision = decide(
                    snapshot(
                        recoverability=recoverability, attempt_count=attempts, do_not_contact=dnc
                    ),
                    scored_for(recoverability, attempt_count=attempts),
                    config(),
                )
                assert decision.recommended_strategy is not None
                assert decision.next_state is not None


# --- Configuration ----------------------------------------------------------


def test_backoff_is_exponential_and_capped() -> None:
    cfg = config(backoff_base_hours=6, backoff_cap_hours=72)
    assert cfg.backoff_hours_for_attempt(0) == 0
    assert cfg.backoff_hours_for_attempt(1) == 6
    assert cfg.backoff_hours_for_attempt(2) == 12
    assert cfg.backoff_hours_for_attempt(3) == 24
    assert cfg.backoff_hours_for_attempt(10) == 72  # capped


def test_invalid_config_is_rejected_not_defaulted() -> None:
    """A merchant who set a limit must never silently get a different one."""
    with pytest.raises(ValueError):
        PolicyConfig(max_automated_attempts=-1)
    with pytest.raises(ValueError):
        PolicyConfig(min_auto_action_confidence=1.5)
    with pytest.raises(ValueError, match="backoff_cap_hours"):
        PolicyConfig(backoff_base_hours=48, backoff_cap_hours=6)


def test_unknown_config_key_is_rejected() -> None:
    with pytest.raises(ValueError):
        PolicyConfig(maximum_attempts=3)  # type: ignore[call-arg]


def test_config_from_merchant_injects_threshold() -> None:
    cfg = PolicyConfig.from_merchant({"max_automated_attempts": 2}, 9_999_00)
    assert cfg.max_automated_attempts == 2
    assert cfg.high_value_threshold_paise == 999900


def test_config_accepts_strategies_as_a_list_from_jsonb() -> None:
    cfg = PolicyConfig.from_merchant({"enabled_strategies": ["WAIT_AND_RETRY"]}, 0)
    assert cfg.enabled_strategies == frozenset({InterventionStrategy.WAIT_AND_RETRY})


# --- Scoring ----------------------------------------------------------------


def test_expected_net_is_gross_minus_cost() -> None:
    scored = scored_for()
    for option in scored:
        assert option.expected_net.paise == option.expected_gross.paise - option.cost.paise


def test_probability_decays_with_attempts() -> None:
    scorer = DeterministicScorer()
    first = scorer.probability(
        recoverability=Recoverability.ACTIONABLE,
        strategy=InterventionStrategy.CREATE_PAYMENT_LINK,
        attempt_count=0,
    )
    third = scorer.probability(
        recoverability=Recoverability.ACTIONABLE,
        strategy=InterventionStrategy.CREATE_PAYMENT_LINK,
        attempt_count=2,
    )
    assert third < first


def test_probability_stays_within_bounds() -> None:
    scorer = DeterministicScorer()
    for recoverability in Recoverability:
        for strategy in InterventionStrategy:
            for attempts in range(0, 12):
                p = scorer.probability(
                    recoverability=recoverability, strategy=strategy, attempt_count=attempts
                )
                assert Decimal("0.01") <= p <= Decimal("0.95")


def test_candidates_are_ranked_by_expected_net_descending() -> None:
    scored = scored_for()
    nets = [s.expected_net.paise for s in scored]
    assert nets == sorted(nets, reverse=True)


def test_scoring_is_deterministic() -> None:
    assert [s.expected_net.paise for s in scored_for()] == [
        s.expected_net.paise for s in scored_for()
    ]
