"""ML scorer adapter (spec section 15: ``apps/api/app/ml/``).

Bridges the domain's :class:`RecoveryScorer` protocol to the trained artifact.
The dependency runs one way only -- this module imports ``ml.src``; nothing in
``ml/`` imports the app -- so the ML pipeline stays runnable on its own.

**Graceful degradation is the point of this file.** If the artifact is missing or
unreadable, :func:`get_scorer` hands back the deterministic baseline instead. The
spec requires the demo to survive a missing model (section 19), so the fallback
is the normal path on a fresh clone, not an error case.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from app.domain.enums import InterventionStrategy
from app.domain.scoring.service import (
    CaseFeatures,
    DeterministicScorer,
    RecoveryScorer,
    strategy_fit,
)
from ml.src.features import ScoringContext
from ml.src.inference import LoadedModel, load_metadata, load_model

logger = logging.getLogger(__name__)


def _to_context(features: CaseFeatures) -> ScoringContext:
    """Domain features -> the ML module's feature contract.

    The only place the two vocabularies meet. Keeping the translation explicit
    means a rename on either side is a compile-time problem, not a silent
    mis-scoring.
    """
    return ScoringContext(
        failure_category=str(features.failure_category),
        source_type=str(features.source_type),
        amount_at_risk_paise=features.amount_at_risk.paise,
        attempt_count=features.attempt_count,
        hour_of_day=features.hour_of_day,
        day_of_week=features.day_of_week,
        payment_method=features.payment_method,
        customer_segment=features.customer_segment,
        customer_tenure_days=features.customer_tenure_days,
        prior_successful_payments=features.prior_successful_payments,
        prior_failed_payments=features.prior_failed_payments,
        subscription_age_days=features.subscription_age_days,
        days_overdue=features.days_overdue,
        checkout_stage=features.checkout_stage,
    )


class MLScorer:
    """Trained classifier, wrapped to satisfy :class:`RecoveryScorer`.

    The model predicts *case-level* recoverability -- will this case recover at
    all. It does not predict per-strategy outcomes, because the synthetic data
    contains no counterfactuals to learn them from. Spec 10.1 sanctions exactly
    this for the MVP: "train a baseline binary classifier and apply
    deterministic intervention rules". So the per-strategy estimate is the
    model's calibrated probability multiplied by the same deterministic
    strategy-fit factor the baseline scorer uses.

    This split is deliberate and is documented in the model card: the learned
    part and the assumed part are kept visibly separate rather than blended into
    one opaque number.
    """

    def __init__(self, model: LoadedModel) -> None:
        self._model = model

    @property
    def model_version(self) -> str:
        return self._model.model_version

    @property
    def threshold(self) -> float:
        """The business-value-optimised threshold chosen at training time."""
        return self._model.threshold

    def probability(self, features: CaseFeatures, strategy: InterventionStrategy) -> Decimal:
        base = Decimal(str(self._model.predict_proba(_to_context(features))))
        fit = strategy_fit(features.recoverability, strategy)
        return min(max(base * fit, Decimal("0.01")), Decimal("0.95"))


def get_scorer(prefer_model: bool = True) -> RecoveryScorer:
    """Return the best available scorer.

    Falls back silently and by design: a fresh clone has no artifact (binaries
    are gitignored) and must still run the full loop.
    """
    if not prefer_model:
        return DeterministicScorer()

    loaded = load_model()
    if loaded is None:
        return DeterministicScorer()
    return MLScorer(loaded)


def model_report() -> dict[str, object] | None:
    """Training metrics for the model page. ``None`` when never trained."""
    return load_metadata()
