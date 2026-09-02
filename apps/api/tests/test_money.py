"""Money tests.

The spec's hardest reliability requirement is that money is never a float. These
tests assert that the type actively *refuses* float input rather than merely
avoiding it by convention.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.money import Money, MoneyError, SignedMoney


def test_paise_is_the_domain_unit() -> None:
    assert Money(12345).paise == 12345
    assert Money.from_rupees("123.45").paise == 12345
    assert Money.from_rupees(500).paise == 50000


def test_float_construction_is_refused() -> None:
    """0.1 + 0.2 must never be able to reach a recovered-amount total."""
    with pytest.raises(MoneyError, match="float"):
        Money.from_rupees(123.45)  # type: ignore[arg-type]


def test_float_scaling_is_refused() -> None:
    with pytest.raises(MoneyError, match="float"):
        Money(10000).scale(0.5)  # type: ignore[arg-type]


def test_bool_is_not_an_int_amount() -> None:
    """bool is a subclass of int in Python; accepting it would be a silent bug."""
    with pytest.raises(MoneyError):
        Money(True)  # type: ignore[arg-type]


def test_sub_paise_rupee_values_are_rejected() -> None:
    with pytest.raises(MoneyError, match="whole number of paise"):
        Money.from_rupees("10.001")


def test_negative_money_is_rejected() -> None:
    with pytest.raises(MoneyError, match="negative"):
        Money(-1)


def test_subtraction_below_zero_is_refused_with_guidance() -> None:
    with pytest.raises(MoneyError, match="SignedMoney"):
        Money(100) - Money(500)


def test_addition_and_subtraction_are_exact() -> None:
    total = Money.zero()
    for _ in range(1000):
        total = total + Money.from_rupees("0.01")
    # A float accumulation of 0.01 a thousand times does not land on 10.00.
    assert total.paise == 1000
    assert total.rupees == Decimal("10.00")


def test_scale_rounds_half_up_to_the_paisa() -> None:
    # Expected-value arithmetic: 0.5 probability of INR 100.01 at risk.
    assert Money(10001).scale(Decimal("0.5")).paise == 5001


def test_indian_digit_grouping() -> None:
    assert Money(123456789).format_inr() == "INR 12,34,567.89"
    assert Money(100000).format_inr() == "INR 1,000.00"
    assert Money(0).format_inr() == "INR 0.00"
    assert Money(99).format_inr() == "INR 0.99"


def test_signed_money_supports_negative_expected_value() -> None:
    """Negative net EV is a first-class outcome: it is what triggers a stop."""
    expected_gross = SignedMoney.of(Money(5000))
    cost = SignedMoney.of(Money(8000))
    net = expected_gross - cost
    assert net.paise == -3000
    assert not net.is_positive
    assert str(net) == "-INR 30.00"


def test_money_is_ordered_and_hashable() -> None:
    assert Money(100) < Money(200)
    assert len({Money(100), Money(100), Money(200)}) == 2
