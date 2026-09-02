"""Money handling for the recovery agent.

Spec (NFR Reliability): *all money amounts stored as integer paise, never
floating point*. This module is the only sanctioned way to construct, combine
and format monetary values, so that no float ever reaches the database or a
recovered-amount calculation.

Rupees are a *presentation* concern. Paise are the domain unit.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# 1 rupee = 100 paise.
PAISE_PER_RUPEE = 100


class MoneyError(ValueError):
    """Raised when a monetary value is invalid or an operation is unsafe."""


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """An exact, immutable amount of Indian paise.

    Always integer-valued. Arithmetic that cannot be represented exactly is
    rejected rather than silently rounded, because a rounding error here is a
    reporting error in the headline "revenue recovered" number.
    """

    paise: int

    def __post_init__(self) -> None:
        if isinstance(self.paise, bool) or not isinstance(self.paise, int):
            raise MoneyError(f"Money.paise must be an int, got {type(self.paise).__name__}")
        if self.paise < 0:
            raise MoneyError(f"Money cannot be negative, got {self.paise} paise")

    # -- constructors -------------------------------------------------------

    @classmethod
    def zero(cls) -> Money:
        return cls(0)

    @classmethod
    def from_rupees(cls, rupees: str | int | Decimal) -> Money:
        """Build from a rupee value.

        Accepts ``str``/``int``/``Decimal`` only. ``float`` is refused
        deliberately: ``0.1 + 0.2`` style error is exactly what this type exists
        to prevent.
        """
        if isinstance(rupees, float):
            raise MoneyError(
                "Refusing to build Money from float; pass a str/int/Decimal of rupees instead"
            )
        try:
            decimal_rupees = Decimal(rupees)
        except (InvalidOperation, TypeError) as exc:
            raise MoneyError(f"Cannot interpret {rupees!r} as a rupee amount") from exc

        paise = decimal_rupees * PAISE_PER_RUPEE
        if paise != paise.to_integral_value():
            raise MoneyError(f"{rupees} rupees is not a whole number of paise")
        return cls(int(paise))

    # -- arithmetic ---------------------------------------------------------

    def __add__(self, other: Money) -> Money:
        return Money(self.paise + other.paise)

    def __sub__(self, other: Money) -> Money:
        """Subtract. Refuses to go negative -- see :class:`SignedMoney`."""
        if other.paise > self.paise:
            raise MoneyError(
                f"Subtracting {other.paise} from {self.paise} paise would be negative; "
                "use SignedMoney for values that may be negative (e.g. net expected value)"
            )
        return Money(self.paise - other.paise)

    def scale(self, factor: Decimal | str | int) -> Money:
        """Multiply by a ratio, rounding half-up to the nearest paisa.

        Used for probability-weighted expectations (``p_recovery x amount``).
        The result is an estimate, so rounding is acceptable here -- unlike in
        :meth:`from_rupees`, where an inexact input signals a caller bug.
        """
        if isinstance(factor, float):
            raise MoneyError("Refusing to scale Money by float; pass Decimal/str/int")
        result = (Decimal(self.paise) * Decimal(factor)).quantize(
            Decimal(1), rounding="ROUND_HALF_UP"
        )
        if result < 0:
            raise MoneyError(f"Scaling by {factor} produced a negative amount")
        return Money(int(result))

    # -- presentation -------------------------------------------------------

    @property
    def rupees(self) -> Decimal:
        """Exact rupee value. For display and API responses only."""
        return Decimal(self.paise) / PAISE_PER_RUPEE

    def format_inr(self) -> str:
        """Render as ``INR 1,23,456.78`` using the Indian digit grouping."""
        whole, frac = divmod(self.paise, PAISE_PER_RUPEE)
        digits = str(whole)
        if len(digits) > 3:
            head, tail = digits[:-3], digits[-3:]
            groups: list[str] = []
            while len(head) > 2:
                groups.insert(0, head[-2:])
                head = head[:-2]
            if head:
                groups.insert(0, head)
            digits = ",".join([*groups, tail])
        return f"INR {digits}.{frac:02d}"

    def __str__(self) -> str:
        return self.format_inr()


@dataclass(frozen=True, slots=True, order=True)
class SignedMoney:
    """A monetary value that may legitimately be negative.

    Exists for expected-value arithmetic, where ``expected_gross - cost`` is
    negative for cases that are not worth pursuing -- a first-class outcome the
    policy engine acts on (spec: "stop when expected net recovery <= 0").
    """

    paise: int

    def __post_init__(self) -> None:
        if isinstance(self.paise, bool) or not isinstance(self.paise, int):
            raise MoneyError(f"SignedMoney.paise must be an int, got {type(self.paise).__name__}")

    @classmethod
    def of(cls, amount: Money) -> SignedMoney:
        return cls(amount.paise)

    def __add__(self, other: SignedMoney) -> SignedMoney:
        return SignedMoney(self.paise + other.paise)

    def __sub__(self, other: SignedMoney) -> SignedMoney:
        return SignedMoney(self.paise - other.paise)

    @property
    def is_positive(self) -> bool:
        return self.paise > 0

    @property
    def rupees(self) -> Decimal:
        return Decimal(self.paise) / PAISE_PER_RUPEE

    def __str__(self) -> str:
        sign = "-" if self.paise < 0 else ""
        return sign + str(Money(abs(self.paise)))
