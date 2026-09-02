"""Feature extraction (spec section 10.2).

**This module is deliberately the only place features are defined.** Training
reads its inputs through :func:`build_feature_frame`, and live inference reads
through :func:`context_to_row`. Both funnel into the same column list and the
same encoding, so a feature cannot be computed one way at training time and a
different way at serving time. That class of bug -- training/serving skew -- is
silent, and it is the reason this module exists rather than the feature logic
living inside ``train.py``.

**Leakage rule:** no field here may be unknown at the moment the decision is
made. ``recovered``, ``recovery_horizon_hours`` and ``true_recovery_probability``
are outcomes and are excluded by construction; :func:`assert_no_leakage` fails
loudly if one ever appears.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd

#: Ground-truth columns. Present in generated data; never model inputs.
LEAKAGE_COLUMNS: frozenset[str] = frozenset(
    {"recovered", "recovery_horizon_hours", "true_recovery_probability"}
)

#: Continuous inputs, standardised before the linear model sees them.
NUMERIC_FEATURES: list[str] = [
    "amount_at_risk_rupees",
    "log_amount",
    "attempt_count",
    "customer_tenure_days",
    "prior_successful_payments",
    "prior_failed_payments",
    "subscription_age_days",
    "hour_of_day",
    "day_of_week",
    "is_overnight",
    "has_payment_history",
    "failure_ratio",
]

#: Low-cardinality inputs, one-hot encoded.
CATEGORICAL_FEATURES: list[str] = [
    "failure_category",
    "payment_method",
    "customer_segment",
    "source_type",
]

ALL_FEATURES: list[str] = NUMERIC_FEATURES + CATEGORICAL_FEATURES

#: Used when a case has no linked customer or a null column. Chosen so a missing
#: value reads as "no evidence" rather than as an unusually good or bad signal.
DEFAULTS: dict[str, Any] = {
    "payment_method": "UNKNOWN",
    "customer_segment": "STANDARD",
    "customer_tenure_days": 0,
    "prior_successful_payments": 0,
    "prior_failed_payments": 0,
    "subscription_age_days": 0,
}


@dataclass(frozen=True, slots=True)
class ScoringContext:
    """Everything the model may look at for one case.

    Mirrors what the database can supply at prediction time. Anything absent
    here is, by definition, not a feature -- which is what keeps the training
    frame and the serving row in step.
    """

    failure_category: str
    source_type: str
    amount_at_risk_paise: int
    attempt_count: int
    hour_of_day: int
    day_of_week: int
    payment_method: str | None = None
    customer_segment: str | None = None
    customer_tenure_days: int = 0
    prior_successful_payments: int = 0
    prior_failed_payments: int = 0
    subscription_age_days: int | None = None


def _derive(row: dict[str, Any]) -> dict[str, Any]:
    """Compute engineered columns from raw ones.

    Shared by both entry points, so an engineered feature is defined once.
    """
    amount_paise = int(row.get("amount_at_risk_paise") or 0)
    rupees = amount_paise / 100.0

    successes = int(row.get("prior_successful_payments") or 0)
    failures = int(row.get("prior_failed_payments") or 0)
    total_history = successes + failures

    hour = int(row.get("hour_of_day") or 0)

    # The generator emits attempt_number (1-based: "this is the Nth attempt"),
    # while the database stores attempt_count (attempts already made). Ingestion
    # maps one to the other, and the identical mapping is applied here so the
    # training frame and the serving row mean the same thing by this column.
    if row.get("attempt_count") is None and row.get("attempt_number") is not None:
        row["attempt_count"] = max(int(row["attempt_number"]) - 1, 0)

    row["amount_at_risk_rupees"] = rupees
    # Ticket sizes are log-normal, so the linear model sees the log. Without
    # this a handful of very large cases dominate the fit.
    row["log_amount"] = math.log1p(max(rupees, 0.0))
    row["has_payment_history"] = 1 if total_history > 0 else 0
    # Share of past attempts that failed. Neutral 0.5 when there is no history,
    # so "unknown" is not confused with "never failed".
    row["failure_ratio"] = (failures / total_history) if total_history > 0 else 0.5
    # Overnight failures wait for the customer to wake up.
    row["is_overnight"] = 1 if hour in (0, 1, 2, 3, 4, 5) else 0

    for column, default in DEFAULTS.items():
        if row.get(column) is None:
            row[column] = default
    return row


def context_to_row(context: ScoringContext) -> dict[str, Any]:
    """One live case -> one feature row. Used by inference only."""
    row: dict[str, Any] = {
        "failure_category": context.failure_category,
        "source_type": context.source_type,
        "amount_at_risk_paise": context.amount_at_risk_paise,
        "attempt_count": context.attempt_count,
        "hour_of_day": context.hour_of_day,
        "day_of_week": context.day_of_week,
        "payment_method": context.payment_method,
        "customer_segment": context.customer_segment,
        "customer_tenure_days": context.customer_tenure_days,
        "prior_successful_payments": context.prior_successful_payments,
        "prior_failed_payments": context.prior_failed_payments,
        "subscription_age_days": context.subscription_age_days,
    }
    return {key: value for key, value in _derive(row).items() if key in ALL_FEATURES}


def build_feature_frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Generated records -> the training design matrix. Used by training only."""
    import pandas as pd

    rows = [_derive(dict(record)) for record in records]
    frame = pd.DataFrame(rows)
    missing = [column for column in ALL_FEATURES if column not in frame.columns]
    if missing:
        raise ValueError(f"Feature frame is missing required columns: {missing}")
    return frame[ALL_FEATURES]


def extract_target(records: list[dict[str, Any]]) -> list[int]:
    """The supervised label: did this case recover within the horizon?"""
    return [int(record["recovered"]) for record in records]


def assert_no_leakage(columns: list[str]) -> None:
    """Fail loudly if an outcome column reached the feature set.

    Called from training. Data leakage inflates every metric and is invisible in
    the results, so it is checked rather than trusted.
    """
    leaked = sorted(set(columns) & LEAKAGE_COLUMNS)
    if leaked:
        raise ValueError(
            f"Outcome columns leaked into the feature set: {leaked}. "
            "These are unknown when the prediction is made and must never be features."
        )
