"""Synthetic generator tests.

Determinism is the property that makes the demo reproducible and every other
test meaningful, so it is asserted directly rather than assumed.
"""

from __future__ import annotations

from dataclasses import asdict

from ml.src.generate_data import DEFAULT_SEED, generate, summarise


def test_same_seed_produces_identical_output() -> None:
    first = generate(count=200, seed=DEFAULT_SEED)
    second = generate(count=200, seed=DEFAULT_SEED)
    assert [asdict(c) for c in first] == [asdict(c) for c in second]


def test_different_seeds_produce_different_output() -> None:
    first = generate(count=200, seed=1)
    second = generate(count=200, seed=2)
    assert [asdict(c) for c in first] != [asdict(c) for c in second]


def test_every_record_is_flagged_synthetic() -> None:
    """The spec forbids presenting synthetic results as real, so the flag is
    not optional."""
    assert all(case.is_synthetic for case in generate(count=300))


def test_amounts_are_positive_whole_paise() -> None:
    for case in generate(count=500):
        assert isinstance(case.amount_at_risk_paise, int)
        assert case.amount_at_risk_paise > 0


def test_ground_truth_is_consistent_with_recovery_flag() -> None:
    """A recovery time exists if and only if the case recovered -- otherwise the
    horizon column would leak the label."""
    for case in generate(count=500):
        if case.recovered:
            assert case.recovery_horizon_hours is not None
        else:
            assert case.recovery_horizon_hours is None


def test_probabilities_are_within_bounds() -> None:
    for case in generate(count=500):
        assert 0.0 < case.true_recovery_probability < 1.0


def test_population_contains_a_learnable_signal() -> None:
    """Recovery rates must differ meaningfully across failure categories.

    If they did not, the Milestone 2 model would have nothing to learn and any
    reported metric would be noise.
    """
    cases = generate(count=3000)
    by_category = summarise(cases)["by_failure_category"]
    assert isinstance(by_category, dict)

    rates = {
        name: stats["recovered"] / stats["n"]
        for name, stats in by_category.items()
        if stats["n"] >= 30
    }
    assert rates["NETWORK_TIMEOUT"] > rates["MANDATE_REVOKED"]
    assert max(rates.values()) - min(rates.values()) > 0.25


def test_population_includes_do_not_contact_cases() -> None:
    """The guardrail needs a population to act on, or the demo cannot show it."""
    cases = generate(count=1000)
    opted_out = [c for c in cases if c.do_not_contact]
    assert 10 < len(opted_out) < 150


def test_cases_are_sorted_by_detection_time() -> None:
    """Enables the temporal train/validation/test split in spec 10.4."""
    cases = generate(count=400)
    assert [c.detected_at for c in cases] == sorted(c.detected_at for c in cases)


def test_summary_reports_both_recovery_rates() -> None:
    """Value-weighted and count-weighted rates differ because of the long tail;
    the dashboard is required to show both."""
    summary = summarise(generate(count=2000))
    assert summary["synthetic"] is True
    assert summary["case_count"] == 2000
    assert 0.0 < float(summary["recovery_rate_by_count"]) < 1.0  # type: ignore[arg-type]
    assert 0.0 < float(summary["recovery_rate_by_value"]) < 1.0  # type: ignore[arg-type]
    assert summary["recovery_rate_by_count"] != summary["recovery_rate_by_value"]


def test_generate_rejects_non_positive_count() -> None:
    import pytest

    with pytest.raises(ValueError, match="positive"):
        generate(count=0)
