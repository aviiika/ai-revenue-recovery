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


# --- Checkout abandonment and B2B receivables -------------------------------


def test_all_four_revenue_loss_types_are_generated() -> None:
    """The brief names payment, checkout, subscription and receivables. An enum
    value with no data behind it is a claim the product does not back."""
    cases = generate(count=2000)
    sources = {case.source_type for case in cases}
    assert sources == {"PAYMENT", "SUBSCRIPTION", "CHECKOUT", "INVOICE"}


def test_checkout_cases_have_a_stage_and_no_gateway_error() -> None:
    """An abandoned checkout was never charged, so there is no failure code to
    diagnose -- only how far the customer got."""
    checkouts = [c for c in generate(count=1500) if c.source_type == "CHECKOUT"]
    assert checkouts

    for case in checkouts:
        assert case.failure_category == "CUSTOMER_ABANDONED"
        assert case.checkout_stage in {"CART", "ADDRESS", "PAYMENT_METHOD", "OTP"}
        assert case.days_overdue is None


def test_recovery_rises_with_checkout_progress() -> None:
    """Someone who reached the OTP screen was seconds from paying; someone who
    left at the cart may never have intended to. If the model cannot see that
    difference, checkout recovery is guesswork."""
    checkouts = [c for c in generate(count=6000) if c.source_type == "CHECKOUT"]
    rates = {}
    for stage in ("CART", "ADDRESS", "PAYMENT_METHOD", "OTP"):
        group = [c for c in checkouts if c.checkout_stage == stage]
        assert len(group) >= 30
        rates[stage] = sum(c.recovered for c in group) / len(group)

    assert rates["CART"] < rates["ADDRESS"] < rates["PAYMENT_METHOD"] < rates["OTP"]


def test_invoice_cases_are_overdue_receivables() -> None:
    invoices = [c for c in generate(count=1500) if c.source_type == "INVOICE"]
    assert invoices

    for case in invoices:
        assert case.failure_category == "INVOICE_OVERDUE"
        assert case.days_overdue is not None and case.days_overdue >= 0
        assert case.checkout_stage is None


def test_invoice_collectability_decays_with_age() -> None:
    """The central fact of receivables: the longer it is outstanding, the less
    likely it is ever collected."""
    invoices = [c for c in generate(count=6000) if c.source_type == "INVOICE"]
    fresh = [c for c in invoices if (c.days_overdue or 0) <= 14]
    stale = [c for c in invoices if (c.days_overdue or 0) >= 60]

    assert len(fresh) >= 30 and len(stale) >= 20
    fresh_rate = sum(c.recovered for c in fresh) / len(fresh)
    stale_rate = sum(c.recovered for c in stale) / len(stale)
    assert fresh_rate > stale_rate


def test_invoices_are_materially_larger_than_consumer_payments() -> None:
    """B2B receivables are where a single case can outweigh a day of checkouts,
    which is what makes value-weighted prioritisation matter."""
    cases = generate(count=3000)
    invoices = [c.amount_at_risk_paise for c in cases if c.source_type == "INVOICE"]
    payments = [c.amount_at_risk_paise for c in cases if c.source_type == "PAYMENT"]

    assert sum(invoices) / len(invoices) > 3 * (sum(payments) / len(payments))
