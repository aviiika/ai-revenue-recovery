"""Model evaluation (spec sections 10.5, 10.6, 10.8).

Two families of metric, and the spec is explicit that the second matters more:

* **Classification** -- PR-AUC, ROC-AUC, F1, Brier, calibration. These say
  whether the model ranks and calibrates well.
* **Business** -- recovered value, net of intervention cost, cost per rupee
  recovered. These say whether acting on the model makes money.

A model can win on ROC-AUC and lose money. The threshold is therefore chosen by
expected net recovery on the validation split, never left at 0.5
(spec 10.8: "Do not hardcode 0.5 as universal threshold").
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    f1_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)


@dataclass(frozen=True, slots=True)
class ClassificationMetrics:
    """How well the model ranks and how honest its probabilities are."""

    n: int
    positive_rate: float
    precision: float
    recall: float
    f1: float
    roc_auc: float
    pr_auc: float
    log_loss: float
    brier: float
    threshold: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BusinessMetrics:
    """What acting on the model is worth, in paise (spec 10.5)."""

    cases_considered: int
    cases_actioned: int
    total_at_risk_paise: int
    actioned_at_risk_paise: int
    recovered_paise: int
    intervention_cost_paise: int
    net_recovery_paise: int
    recovery_rate_by_count: float
    recovery_rate_by_value: float
    cost_per_rupee_recovered: float
    actions_per_successful_recovery: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_rate: float


@dataclass
class EvaluationReport:
    model_name: str
    classification: ClassificationMetrics
    business: BusinessMetrics
    calibration: list[CalibrationBin] = field(default_factory=list)
    #: Signed contribution per feature. Coefficients for the linear baseline,
    #: permutation importance for the tree. Never presented as causal.
    feature_influence: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "classification": self.classification.to_dict(),
            "business": self.business.to_dict(),
            "calibration": [asdict(bin_) for bin_ in self.calibration],
            "feature_influence": self.feature_influence,
        }


def classification_metrics(
    y_true: np.ndarray, y_prob: np.ndarray, threshold: float
) -> ClassificationMetrics:
    y_pred = (y_prob >= threshold).astype(int)
    return ClassificationMetrics(
        n=len(y_true),
        positive_rate=float(y_true.mean()),
        precision=float(precision_score(y_true, y_pred, zero_division=0)),
        recall=float(recall_score(y_true, y_pred, zero_division=0)),
        f1=float(f1_score(y_true, y_pred, zero_division=0)),
        roc_auc=float(roc_auc_score(y_true, y_prob)),
        # PR-AUC is the headline ranking metric here: recovery positives are the
        # minority class and ROC-AUC flatters imbalanced problems (spec 10.7).
        pr_auc=float(average_precision_score(y_true, y_prob)),
        log_loss=float(log_loss(y_true, y_prob, labels=[0, 1])),
        brier=float(brier_score_loss(y_true, y_prob)),
        threshold=float(threshold),
    )


def business_metrics(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    amounts_paise: np.ndarray,
    threshold: float,
    intervention_cost_paise: int,
) -> BusinessMetrics:
    """Value of acting on every case the model scores at or above ``threshold``.

    Deliberately counts recovery only for cases we actually actioned. A case that
    would have recovered on its own but was never contacted is not revenue the
    agent can claim -- see the causal caveat in the model card.
    """
    actioned = y_prob >= threshold
    recovered = actioned & (y_true == 1)

    total_at_risk = int(amounts_paise.sum())
    actioned_at_risk = int(amounts_paise[actioned].sum())
    recovered_value = int(amounts_paise[recovered].sum())
    cost = int(actioned.sum()) * intervention_cost_paise

    n_actioned = int(actioned.sum())
    n_recovered = int(recovered.sum())

    return BusinessMetrics(
        cases_considered=len(y_true),
        cases_actioned=n_actioned,
        total_at_risk_paise=total_at_risk,
        actioned_at_risk_paise=actioned_at_risk,
        recovered_paise=recovered_value,
        intervention_cost_paise=cost,
        net_recovery_paise=recovered_value - cost,
        recovery_rate_by_count=(n_recovered / n_actioned) if n_actioned else 0.0,
        recovery_rate_by_value=(recovered_value / actioned_at_risk) if actioned_at_risk else 0.0,
        # How many paise we spend to bring in one rupee. Lower is better.
        cost_per_rupee_recovered=(cost / recovered_value) if recovered_value else float("inf"),
        actions_per_successful_recovery=(n_actioned / n_recovered) if n_recovered else float("inf"),
    )


def choose_threshold(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    amounts_paise: np.ndarray,
    intervention_cost_paise: int,
    grid: np.ndarray | None = None,
) -> tuple[float, int]:
    """Pick the threshold maximising expected net recovery on validation data.

    Returns ``(threshold, net_recovery_paise)``. This is the single most
    business-relevant decision in the pipeline: it is where "the model is
    accurate" becomes "acting on the model is profitable".

    Ties break toward the *higher* threshold, which actions fewer customers for
    the same money -- fewer unnecessary contacts is a real benefit the spec asks
    us to report.
    """
    candidates = grid if grid is not None else np.round(np.arange(0.05, 0.96, 0.01), 2)

    best_threshold = float(candidates[0])
    best_net = -(10**18)
    for threshold in candidates:
        actioned = y_prob >= threshold
        recovered_value = int(amounts_paise[actioned & (y_true == 1)].sum())
        cost = int(actioned.sum()) * intervention_cost_paise
        net = recovered_value - cost
        if net >= best_net:
            best_net = net
            best_threshold = float(threshold)
    return best_threshold, int(best_net)


def calibration_bins(
    y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10
) -> list[CalibrationBin]:
    """Reliability curve data (spec 10.6).

    A predicted 0.8 should correspond to roughly 80% observed recovery among
    comparable cases. Empty bins are dropped rather than reported as zero, which
    would misread as "the model is wrong here".
    """
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins: list[CalibrationBin] = []
    for index in range(n_bins):
        lower, upper = edges[index], edges[index + 1]
        mask = (y_prob >= lower) & (y_prob < upper if index < n_bins - 1 else y_prob <= upper)
        count = int(mask.sum())
        if count == 0:
            continue
        bins.append(
            CalibrationBin(
                lower=float(lower),
                upper=float(upper),
                count=count,
                mean_predicted=float(y_prob[mask].mean()),
                observed_rate=float(y_true[mask].mean()),
            )
        )
    return bins


def expected_calibration_error(bins: list[CalibrationBin]) -> float:
    """Weighted mean gap between predicted probability and observed frequency."""
    total = sum(bin_.count for bin_ in bins)
    if total == 0:
        return 0.0
    return sum(bin_.count / total * abs(bin_.mean_predicted - bin_.observed_rate) for bin_ in bins)


def threshold_sweep(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    amounts_paise: np.ndarray,
    intervention_cost_paise: int,
    thresholds: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8),
) -> list[dict[str, Any]]:
    """Precision / recall / net recovery across a range of operating points.

    Reported because a single chosen threshold hides the trade-off. When the
    intervention is cheap relative to the ticket, expected value alone pushes the
    threshold toward zero -- "contact everyone" is genuinely EV-optimal. That is
    a real property of the economics, not a modelling error, but an operator
    needs to see what they give up by acting more selectively (fewer contacts,
    higher precision, less gross recovery).
    """
    rows: list[dict[str, Any]] = []
    for threshold in thresholds:
        actioned = y_prob >= threshold
        n_actioned = int(actioned.sum())
        recovered_mask = actioned & (y_true == 1)
        recovered_value = int(amounts_paise[recovered_mask].sum())
        cost = n_actioned * intervention_cost_paise
        rows.append(
            {
                "threshold": round(float(threshold), 2),
                "cases_actioned": n_actioned,
                "share_actioned": round(n_actioned / len(y_true), 4) if len(y_true) else 0.0,
                "precision": round(
                    float(precision_score(y_true, actioned.astype(int), zero_division=0)), 4
                ),
                "recall": round(
                    float(recall_score(y_true, actioned.astype(int), zero_division=0)), 4
                ),
                "recovered_paise": recovered_value,
                "net_recovery_paise": recovered_value - cost,
            }
        )
    return rows
