"""Deterministic synthetic revenue-risk data generator (spec section 10.11).

WHY THIS EXISTS
---------------
We have no real Razorpay merchant data, and we must never claim we do. This
generator produces a controlled population of failed payments and failed
subscription renewals whose *ground-truth recovery propensity is known*, so the
ML baseline in Milestone 2 has a target to learn and the simulator in Milestone
4 has an outcome to reveal.

DETERMINISM
-----------
Everything is driven by a single seed. The same seed produces byte-identical
output on any machine, which is what makes the demo reproducible and the tests
meaningful. Nothing here reads the clock except through ``reference_time``,
which the caller supplies.

HONESTY
-------
Every record carries ``is_synthetic=True``. Model metrics computed on this data
describe *this simulation*, never real-world production performance.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Importable both as `ml.src.generate_data` and as a standalone script.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

DEFAULT_SEED = 20260902

# --- Population parameters -------------------------------------------------
# These encode the domain assumptions the simulation rests on. They are stated
# here explicitly, rather than buried in code, so a reviewer can challenge them.

#: failure category -> (share of failures, base recovery probability)
#:
#: The base rates express a deliberate ordering: a network timeout is mostly a
#: retry problem and recovers often; a revoked mandate needs the customer to
#: re-authorise and rarely recovers without contact.
FAILURE_PROFILE: dict[str, tuple[float, float]] = {
    "INSUFFICIENT_FUNDS": (0.26, 0.42),
    "AUTHENTICATION_FAILED": (0.14, 0.66),
    "CARD_EXPIRED": (0.11, 0.28),
    "ISSUER_DECLINED": (0.15, 0.31),
    "TECHNICAL_ERROR": (0.09, 0.74),
    "NETWORK_TIMEOUT": (0.08, 0.79),
    "LIMIT_EXCEEDED": (0.07, 0.47),
    "MANDATE_REVOKED": (0.06, 0.12),
    "CUSTOMER_ABANDONED": (0.04, 0.22),
}

#: Provider-style reason codes, kept plausible but explicitly synthetic.
REASON_CODES: dict[str, list[str]] = {
    "INSUFFICIENT_FUNDS": ["BAD_REQUEST_PAYMENT_FAILED", "GATEWAY_INSUFFICIENT_BALANCE"],
    "AUTHENTICATION_FAILED": ["BAD_REQUEST_OTP_FAILED", "GATEWAY_3DS_TIMEOUT"],
    "CARD_EXPIRED": ["BAD_REQUEST_CARD_EXPIRED"],
    "ISSUER_DECLINED": ["GATEWAY_ISSUER_DECLINED", "GATEWAY_DO_NOT_HONOUR"],
    "TECHNICAL_ERROR": ["SERVER_ERROR_GATEWAY", "SERVER_ERROR_UPSTREAM"],
    "NETWORK_TIMEOUT": ["GATEWAY_TIMEOUT"],
    "LIMIT_EXCEEDED": ["GATEWAY_LIMIT_EXCEEDED"],
    "MANDATE_REVOKED": ["BAD_REQUEST_MANDATE_REVOKED", "BAD_REQUEST_MANDATE_CANCELLED"],
    "CUSTOMER_ABANDONED": ["CUSTOMER_DROPPED_OFF"],
}

SEGMENTS: list[tuple[str, float]] = [("STANDARD", 0.62), ("PREMIUM", 0.24), ("ENTERPRISE", 0.14)]
PAYMENT_METHODS: list[tuple[str, float]] = [
    ("CARD", 0.46),
    ("UPI", 0.34),
    ("NETBANKING", 0.12),
    ("WALLET", 0.08),
]


@dataclass(frozen=True, slots=True)
class SyntheticCase:
    """One generated revenue-at-risk record.

    ``recovered`` and ``recovery_horizon_hours`` are ground truth: they are what
    Milestone 2 trains against and what the simulator reveals over time. They
    must never be exposed to the model as an input feature -- that would be
    textbook data leakage.
    """

    source_external_id: str
    customer_external_ref: str
    source_type: str
    amount_at_risk_paise: int
    currency: str
    failure_category: str
    failure_reason_code: str
    payment_method: str
    detected_at: str
    customer_segment: str
    customer_tenure_days: int
    prior_successful_payments: int
    prior_failed_payments: int
    subscription_age_days: int | None
    attempt_number: int
    hour_of_day: int
    day_of_week: int
    do_not_contact: bool
    is_synthetic: bool
    recovered: int
    recovery_horizon_hours: float | None
    true_recovery_probability: float


def _weighted_choice(rng: random.Random, options: list[tuple[str, float]]) -> str:
    """Pick one option by weight, using only ``rng`` so results stay seeded."""
    roll = rng.random() * sum(weight for _, weight in options)
    cumulative = 0.0
    for name, weight in options:
        cumulative += weight
        if roll <= cumulative:
            return name
    return options[-1][0]


def _draw_amount_paise(rng: random.Random, segment: str) -> int:
    """Draw a realistic, long-tailed ticket size in whole paise.

    Log-normal shape: many small failures, a few large ones. The large tail is
    what makes value-weighted recovery rate differ from count-weighted recovery
    rate -- a distinction the dashboard is required to show.
    """
    # Calibrated so a 120-case demo batch lands in the spec's stated
    # INR 8-12 lakh at-risk band (spec section 17), while keeping the
    # segment ordering and the long right tail.
    mu, sigma = {
        "STANDARD": (7.95, 0.75),
        "PREMIUM": (8.85, 0.70),
        "ENTERPRISE": (9.95, 0.85),
    }[segment]
    rupees = min(max(rng.lognormvariate(mu, sigma), 49.0), 750_000.0)
    return round(rupees * 100)


def _recovery_probability(
    base: float,
    *,
    segment: str,
    tenure_days: int,
    prior_successes: int,
    prior_failures: int,
    attempt_number: int,
    amount_paise: int,
    hour_of_day: int,
) -> float:
    """Ground-truth recovery probability for one case.

    A transparent, monotone function of a few drivers. It is intentionally
    *learnable but not trivial*: a logistic baseline should recover most of the
    signal, leaving a little headroom for a tree model to justify itself.
    """
    odds_multiplier = 1.0

    # A customer with a long, successful history is a better bet.
    odds_multiplier *= 1.0 + min(prior_successes, 20) * 0.045
    odds_multiplier *= 1.0 + min(tenure_days, 900) / 900.0 * 0.30

    # Repeated recent failures signal a genuinely dead payment method.
    odds_multiplier *= max(0.35, 1.0 - min(prior_failures, 8) * 0.085)

    # Each additional attempt on the same case is less likely to land.
    odds_multiplier *= 0.72 ** (attempt_number - 1)

    # Very large tickets stall more often (manual approval, limits).
    if amount_paise > 5_000_000:
        odds_multiplier *= 0.78
    elif amount_paise > 1_000_000:
        odds_multiplier *= 0.90

    # Overnight failures wait for the customer to wake up.
    if hour_of_day in (0, 1, 2, 3, 4, 5):
        odds_multiplier *= 0.85

    odds_multiplier *= {"STANDARD": 1.0, "PREMIUM": 1.12, "ENTERPRISE": 1.20}[segment]

    odds = (base / (1.0 - base)) * odds_multiplier
    probability = odds / (1.0 + odds)
    return min(max(probability, 0.01), 0.97)


def generate(
    count: int = 1000,
    seed: int = DEFAULT_SEED,
    reference_time: datetime | None = None,
    horizon_hours: int = 72,
) -> list[SyntheticCase]:
    """Generate ``count`` synthetic cases deterministically from ``seed``.

    ``reference_time`` defaults to a fixed instant rather than ``now()`` so that
    two runs on different days remain byte-identical.
    """
    if count <= 0:
        raise ValueError("count must be positive")

    rng = random.Random(seed)
    anchor = reference_time or datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

    category_options = [(name, share) for name, (share, _) in FAILURE_PROFILE.items()]
    cases: list[SyntheticCase] = []

    for index in range(count):
        category = _weighted_choice(rng, category_options)
        base_rate = FAILURE_PROFILE[category][1]
        segment = _weighted_choice(rng, SEGMENTS)
        method = _weighted_choice(rng, PAYMENT_METHODS)

        # Spread detections back over 30 days so temporal splits are meaningful.
        detected_at = anchor - timedelta(minutes=rng.randint(0, 30 * 24 * 60))

        tenure_days = int(rng.triangular(0, 1200, 240))
        prior_successes = rng.randint(0, 3) if tenure_days < 60 else rng.randint(0, 24)
        prior_failures = rng.randint(0, 6)
        attempt_number = rng.choices([1, 2, 3], weights=[0.72, 0.20, 0.08])[0]
        amount = _draw_amount_paise(rng, segment)

        source_type = "SUBSCRIPTION" if rng.random() < 0.38 else "PAYMENT"
        subscription_age = rng.randint(30, 900) if source_type == "SUBSCRIPTION" else None

        probability = _recovery_probability(
            base_rate,
            segment=segment,
            tenure_days=tenure_days,
            prior_successes=prior_successes,
            prior_failures=prior_failures,
            attempt_number=attempt_number,
            amount_paise=amount,
            hour_of_day=detected_at.hour,
        )

        recovered = 1 if rng.random() < probability else 0
        # Recovery time is drawn only for cases that actually recover.
        time_to_recovery = round(rng.uniform(0.4, horizon_hours), 2) if recovered else None

        # A small share of customers have opted out entirely. The policy engine
        # must refuse to contact these no matter how attractive the EV looks --
        # this is the population that proves the guardrail works.
        do_not_contact = rng.random() < 0.05

        cases.append(
            SyntheticCase(
                source_external_id=f"syn_{seed}_{index:06d}",
                customer_external_ref=f"cust_syn_{rng.randint(1, max(2, count // 3)):06d}",
                source_type=source_type,
                amount_at_risk_paise=amount,
                currency="INR",
                failure_category=category,
                failure_reason_code=rng.choice(REASON_CODES[category]),
                payment_method=method,
                detected_at=detected_at.isoformat(),
                customer_segment=segment,
                customer_tenure_days=tenure_days,
                prior_successful_payments=prior_successes,
                prior_failed_payments=prior_failures,
                subscription_age_days=subscription_age,
                attempt_number=attempt_number,
                hour_of_day=detected_at.hour,
                day_of_week=detected_at.weekday(),
                do_not_contact=do_not_contact,
                is_synthetic=True,
                recovered=recovered,
                recovery_horizon_hours=time_to_recovery,
                true_recovery_probability=round(probability, 6),
            )
        )

    # Stable ordering by detection time makes the temporal train/test split in
    # Milestone 2 a simple slice.
    cases.sort(key=lambda c: (c.detected_at, c.source_external_id))
    return cases


def summarise(cases: list[SyntheticCase]) -> dict[str, object]:
    """Population statistics, for the model card and a quick sanity check."""
    total_at_risk = sum(c.amount_at_risk_paise for c in cases)
    recovered_value = sum(c.amount_at_risk_paise for c in cases if c.recovered)
    recovered_count = sum(c.recovered for c in cases)
    by_category: dict[str, dict[str, int]] = {}
    for case in cases:
        bucket = by_category.setdefault(case.failure_category, {"n": 0, "recovered": 0})
        bucket["n"] += 1
        bucket["recovered"] += case.recovered
    return {
        "synthetic": True,
        "case_count": len(cases),
        "total_at_risk_paise": total_at_risk,
        "recovered_count": recovered_count,
        "recovered_value_paise": recovered_value,
        "recovery_rate_by_count": round(recovered_count / len(cases), 4),
        "recovery_rate_by_value": round(recovered_value / total_at_risk, 4),
        "do_not_contact_count": sum(1 for c in cases if c.do_not_contact),
        "by_failure_category": by_category,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=None, help="Write JSON lines here")
    parser.add_argument("--summary", action="store_true", help="Print population stats only")
    args = parser.parse_args()

    cases = generate(count=args.count, seed=args.seed)

    if args.summary:
        print(json.dumps(summarise(cases), indent=2))
        return 0

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as handle:
            for case in cases:
                handle.write(json.dumps(asdict(case)) + "\n")
        print(f"Wrote {len(cases)} synthetic cases to {args.out}")
    else:
        for case in cases:
            print(json.dumps(asdict(case)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
