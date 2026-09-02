"""ML pipeline tests.

Covers the three things that actually matter for a model in a money system:
no leakage, no training/serving skew, and graceful degradation when the model
is unavailable. Raw predictive accuracy is checked in the training report, not
here -- these are contract tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.money import Money
from app.domain.enums import FailureCategory, InterventionStrategy, Recoverability
from app.domain.scoring.service import CaseFeatures, DeterministicScorer, score_all
from ml.src.features import (
    ALL_FEATURES,
    LEAKAGE_COLUMNS,
    ScoringContext,
    assert_no_leakage,
    build_feature_frame,
    context_to_row,
)
from ml.src.generate_data import generate


def sample_features(**overrides: object) -> CaseFeatures:
    defaults: dict[str, object] = {
        "recoverability": Recoverability.ACTIONABLE,
        "failure_category": FailureCategory.INSUFFICIENT_FUNDS,
        "amount_at_risk": Money.from_rupees("5000.00"),
        "attempt_count": 0,
        "detected_at": datetime(2026, 9, 1, 14, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return CaseFeatures(**defaults)  # type: ignore[arg-type]


# --- Leakage ---------------------------------------------------------------


def test_outcome_columns_are_never_features() -> None:
    """The single most damaging ML bug in a system like this."""
    assert not (set(ALL_FEATURES) & LEAKAGE_COLUMNS)
    assert_no_leakage(ALL_FEATURES)


def test_leakage_guard_actually_fires() -> None:
    """A guard that never fails is not a guard."""
    with pytest.raises(ValueError, match="leaked"):
        assert_no_leakage([*ALL_FEATURES, "recovered"])


def test_generated_frame_excludes_the_label() -> None:
    from dataclasses import asdict

    records = [asdict(case) for case in generate(count=50)]
    frame = build_feature_frame(records)
    assert set(frame.columns) == set(ALL_FEATURES)
    for leaked in LEAKAGE_COLUMNS:
        assert leaked not in frame.columns


# --- Training / serving consistency ----------------------------------------


def test_serving_row_matches_training_columns_exactly() -> None:
    """Skew check: a live row must have the same shape as a training row.

    If these drift, the model is silently fed different features at serving time
    than it learned on, and nothing errors -- it just predicts badly.
    """
    row = context_to_row(
        ScoringContext(
            failure_category="INSUFFICIENT_FUNDS",
            source_type="PAYMENT",
            amount_at_risk_paise=500000,
            attempt_count=1,
            hour_of_day=14,
            day_of_week=2,
        )
    )
    assert set(row.keys()) == set(ALL_FEATURES)


def test_missing_customer_data_falls_back_to_neutral_defaults() -> None:
    """A case with no linked customer must score, not crash."""
    row = context_to_row(
        ScoringContext(
            failure_category="CARD_EXPIRED",
            source_type="SUBSCRIPTION",
            amount_at_risk_paise=100000,
            attempt_count=0,
            hour_of_day=3,
            day_of_week=0,
        )
    )
    assert row["payment_method"] == "UNKNOWN"
    assert row["customer_segment"] == "STANDARD"
    # No history is 0.5, not 0: "unknown" must not read as "never failed".
    assert row["failure_ratio"] == 0.5
    assert row["has_payment_history"] == 0
    assert row["is_overnight"] == 1


def test_attempt_number_maps_to_attempt_count() -> None:
    """The generator is 1-based; the database stores prior attempts."""
    from dataclasses import asdict

    records = [asdict(case) for case in generate(count=100)]
    frame = build_feature_frame(records)
    assert frame["attempt_count"].min() >= 0
    for record, value in zip(records, frame["attempt_count"], strict=True):
        assert value == max(record["attempt_number"] - 1, 0)


# --- Graceful degradation ---------------------------------------------------


def test_scorer_falls_back_when_model_is_disabled() -> None:
    """Spec section 19: model unavailable -> deterministic fallback."""
    from app.ml.scorer import get_scorer

    scorer = get_scorer(prefer_model=False)
    assert scorer.model_version == "deterministic-baseline-v1"


def test_missing_artifact_returns_none_rather_than_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    import ml.src.inference as inference

    monkeypatch.setattr(inference, "MODEL_PATH", Path("does-not-exist.joblib"))
    assert inference.load_model(force=True) is None

    # And the adapter turns that into a working scorer, not an exception.
    from app.ml.scorer import get_scorer

    assert get_scorer().model_version == "deterministic-baseline-v1"


def test_corrupt_artifact_degrades_instead_of_crashing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ml.src.inference as inference

    broken = tmp_path / "broken.joblib"
    broken.write_bytes(b"this is not a joblib file")
    monkeypatch.setattr(inference, "MODEL_PATH", broken)

    assert inference.load_model(force=True) is None


# --- Scoring contract -------------------------------------------------------


def test_deterministic_scorer_satisfies_the_protocol() -> None:
    scorer = DeterministicScorer()
    probability = scorer.probability(sample_features(), InterventionStrategy.CREATE_PAYMENT_LINK)
    assert isinstance(probability, Decimal)
    assert Decimal("0.01") <= probability <= Decimal("0.95")


def test_score_all_returns_every_actionable_strategy() -> None:
    scored = score_all(DeterministicScorer(), sample_features())
    assert len(scored) == 4
    assert all(s.model_version == "deterministic-baseline-v1" for s in scored)


def test_case_features_derive_time_fields() -> None:
    features = sample_features(detected_at=datetime(2026, 9, 5, 3, 30, tzinfo=UTC))
    assert features.hour_of_day == 3
    assert features.day_of_week == 5  # Saturday


def test_case_features_without_timestamp_use_safe_defaults() -> None:
    features = sample_features(detected_at=None)
    assert features.hour_of_day == 12
    assert features.day_of_week == 0


def test_expected_value_never_uses_float_money() -> None:
    """Probabilities are Decimal so money arithmetic stays exact."""
    scored = score_all(DeterministicScorer(), sample_features())
    for option in scored:
        assert isinstance(option.probability, Decimal)
        assert isinstance(option.expected_gross.paise, int)
        assert option.expected_net.paise == option.expected_gross.paise - option.cost.paise
