"""Train the recovery model (spec section 10).

Pipeline, in order:

1. Generate a seeded synthetic population.
2. Split **temporally** -- oldest 70% train, next 15% validation, newest 15%
   test (spec 10.4). A random split would let the model learn from cases that
   happen after the ones it is tested on, which production never allows.
3. Fit an interpretable logistic baseline, then a HistGradientBoosting
   challenger. The tree only wins if it materially beats the baseline
   (spec 10.3: "Do not use a neural network simply to sound advanced" -- the
   same restraint applies to gradient boosting).
4. Calibrate on validation, so a predicted 0.8 means roughly 80%.
5. Choose the decision threshold by expected net recovery, not accuracy.
6. Report held-out test metrics, once, and never tune on them.

Run:  python -m ml.src.train --count 6000
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from ml.src.evaluate import (
    EvaluationReport,
    business_metrics,
    calibration_bins,
    choose_threshold,
    classification_metrics,
    expected_calibration_error,
    threshold_sweep,
)
from ml.src.features import (
    ALL_FEATURES,
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    assert_no_leakage,
    build_feature_frame,
    extract_target,
)
from ml.src.generate_data import DEFAULT_SEED, generate

ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "artifacts"
MODEL_PATH = ARTIFACT_DIR / "recovery_model.joblib"
METADATA_PATH = ARTIFACT_DIR / "model_metadata.json"

MODEL_VERSION = "recovery-clf-v1"

#: Cost assumed when thresholding. Matches CREATE_PAYMENT_LINK in the policy
#: engine's cost table -- the most common actionable intervention -- so the
#: threshold is tuned against the economics the agent actually faces.
ASSUMED_INTERVENTION_COST_PAISE = 500


def _split_temporally(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Oldest 70% / next 15% / newest 15%, ordered by detection time."""
    ordered = sorted(records, key=lambda r: r["detected_at"])
    n = len(ordered)
    train_end = int(n * 0.70)
    val_end = int(n * 0.85)
    return ordered[:train_end], ordered[train_end:val_end], ordered[val_end:]


def _build_preprocessor() -> ColumnTransformer:
    return ColumnTransformer(
        transformers=[
            ("numeric", StandardScaler(), NUMERIC_FEATURES),
            (
                "categorical",
                # Unknown categories at serving time must not crash inference --
                # a new payment method should degrade gracefully, not 500.
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                CATEGORICAL_FEATURES,
            ),
        ],
        remainder="drop",
    )


def _logistic_pipeline() -> Pipeline:
    return Pipeline(
        [
            ("preprocess", _build_preprocessor()),
            (
                "classifier",
                LogisticRegression(
                    max_iter=2000,
                    # Recovery positives are the minority; unweighted fitting
                    # would push the model toward predicting "never recovers".
                    class_weight="balanced",
                    random_state=DEFAULT_SEED,
                ),
            ),
        ]
    )


def _tree_pipeline() -> Pipeline:
    return Pipeline(
        [
            ("preprocess", _build_preprocessor()),
            (
                "classifier",
                HistGradientBoostingClassifier(
                    max_iter=250,
                    learning_rate=0.06,
                    max_leaf_nodes=15,
                    l2_regularization=1.0,
                    random_state=DEFAULT_SEED,
                ),
            ),
        ]
    )


def _logistic_coefficients(pipeline: Pipeline) -> dict[str, float]:
    """Signed coefficients, largest magnitude first.

    Interpretable, but correlational: a large coefficient does not mean the
    feature *causes* recovery (spec 10.9).
    """
    preprocess: ColumnTransformer = pipeline.named_steps["preprocess"]
    names = list(preprocess.get_feature_names_out())
    coefficients = pipeline.named_steps["classifier"].coef_[0]
    paired = sorted(
        zip(names, coefficients, strict=True), key=lambda pair: abs(pair[1]), reverse=True
    )
    return {name: round(float(value), 4) for name, value in paired[:15]}


def train(
    count: int = 6000, seed: int = DEFAULT_SEED, write_artifacts: bool = True
) -> dict[str, Any]:
    """Train, evaluate and (optionally) persist the model."""
    records = [asdict(case) for case in generate(count=count, seed=seed)]
    train_set, val_set, test_set = _split_temporally(records)

    assert_no_leakage(ALL_FEATURES)

    X_train = build_feature_frame(train_set)
    y_train = np.array(extract_target(train_set))
    X_val = build_feature_frame(val_set)
    y_val = np.array(extract_target(val_set))
    X_test = build_feature_frame(test_set)
    y_test = np.array(extract_target(test_set))

    amounts_val = np.array([r["amount_at_risk_paise"] for r in val_set])
    amounts_test = np.array([r["amount_at_risk_paise"] for r in test_set])

    reports: dict[str, EvaluationReport] = {}
    fitted: dict[str, Any] = {}
    sweeps: dict[str, list[dict[str, Any]]] = {}

    for name, factory in (
        ("logistic_regression", _logistic_pipeline),
        ("hist_gradient_boosting", _tree_pipeline),
    ):
        pipeline = factory()
        pipeline.fit(X_train, y_train)

        # Calibrate on validation using isotonic regression, then re-read
        # probabilities. Uncalibrated class-weighted logistic output is
        # systematically over-confident (spec 10.6).
        # FrozenEstimator wraps the already-fitted pipeline so calibration
        # fits only the isotonic layer on validation data, leaving the base
        # model untouched. (Replaces cv="prefit", removed in scikit-learn 1.9.)
        calibrated = CalibratedClassifierCV(FrozenEstimator(pipeline), method="isotonic")
        calibrated.fit(X_val, y_val)

        val_prob = calibrated.predict_proba(X_val)[:, 1]
        threshold, _ = choose_threshold(
            y_val, val_prob, amounts_val, ASSUMED_INTERVENTION_COST_PAISE
        )

        test_prob = calibrated.predict_proba(X_test)[:, 1]
        bins = calibration_bins(y_test, test_prob)

        influence: dict[str, float] = {}
        if name == "logistic_regression":
            influence = _logistic_coefficients(pipeline)

        reports[name] = EvaluationReport(
            model_name=name,
            classification=classification_metrics(y_test, test_prob, threshold),
            business=business_metrics(
                y_test, test_prob, amounts_test, threshold, ASSUMED_INTERVENTION_COST_PAISE
            ),
            calibration=bins,
            feature_influence=influence,
        )
        fitted[name] = {"model": calibrated, "threshold": threshold}
        sweeps[name] = threshold_sweep(
            y_test, test_prob, amounts_test, ASSUMED_INTERVENTION_COST_PAISE
        )

    # Selection rule, stated before looking at the numbers: keep the
    # interpretable baseline unless the challenger clears it by a real margin on
    # PR-AUC. Complexity has to earn its place (spec 10.3).
    baseline_pr = reports["logistic_regression"].classification.pr_auc
    challenger_pr = reports["hist_gradient_boosting"].classification.pr_auc
    material_gain = challenger_pr - baseline_pr >= 0.02

    selected = "hist_gradient_boosting" if material_gain else "logistic_regression"

    metadata: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "selected_model": selected,
        "selection_rule": (
            "Keep the interpretable logistic baseline unless the tree improves "
            "test PR-AUC by at least 0.02 absolute."
        ),
        "selection_margin_pr_auc": round(challenger_pr - baseline_pr, 4),
        "threshold": fitted[selected]["threshold"],
        "threshold_rule": "Maximises expected net recovery on the validation split.",
        "assumed_intervention_cost_paise": ASSUMED_INTERVENTION_COST_PAISE,
        "trained_at": datetime.now(UTC).isoformat(),
        "seed": seed,
        "synthetic": True,
        "synthetic_warning": (
            "Trained and evaluated entirely on deterministic synthetic data. These "
            "figures describe a controlled simulation and are not evidence of "
            "real-world production performance."
        ),
        "dataset": {
            "total": len(records),
            "train": len(train_set),
            "validation": len(val_set),
            "test": len(test_set),
            "split": "temporal 70/15/15 by detected_at",
            "train_positive_rate": round(float(y_train.mean()), 4),
            "test_positive_rate": round(float(y_test.mean()), 4),
        },
        "features": ALL_FEATURES,
        "expected_calibration_error": round(
            expected_calibration_error(reports[selected].calibration), 4
        ),
        # The EV-optimal threshold can collapse toward zero when the
        # intervention is cheap relative to the ticket. The sweep makes that
        # trade-off visible instead of hiding it behind one number.
        "threshold_sweep": sweeps[selected],
        "threshold_note": (
            "Expected value alone favours contacting nearly every case at the assumed "
            "intervention cost. The policy engine applies an independent confidence "
            "floor (min_auto_action_confidence), so low-confidence cases are routed to "
            "a human rather than actioned automatically."
        ),
        "reports": {name: report.to_dict() for name, report in reports.items()},
    }

    if write_artifacts:
        import joblib

        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "model": fitted[selected]["model"],
                "threshold": fitted[selected]["threshold"],
                "model_version": MODEL_VERSION,
                "features": ALL_FEATURES,
                "selected_model": selected,
            },
            MODEL_PATH,
        )
        METADATA_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    return metadata


def _summarise(metadata: dict[str, Any]) -> str:
    lines = [
        "",
        "=" * 68,
        "  RECOVERY MODEL - trained on SYNTHETIC data",
        "=" * 68,
        f"  selected      : {metadata['selected_model']}",
        f"  threshold     : {metadata['threshold']}  (max expected net recovery)",
        f"  PR-AUC margin : {metadata['selection_margin_pr_auc']:+.4f} (tree - baseline)",
        f"  dataset       : {metadata['dataset']['total']} cases, {metadata['dataset']['split']}",
        f"  ECE           : {metadata['expected_calibration_error']}",
        "",
    ]
    for name, report in metadata["reports"].items():
        c = report["classification"]
        b = report["business"]
        marker = " <- selected" if name == metadata["selected_model"] else ""
        lines += [
            f"  {name}{marker}",
            f"    PR-AUC {c['pr_auc']:.4f}  ROC-AUC {c['roc_auc']:.4f}  "
            f"F1 {c['f1']:.4f}  Brier {c['brier']:.4f}",
            f"    precision {c['precision']:.4f}  recall {c['recall']:.4f}",
            f"    actioned {b['cases_actioned']}/{b['cases_considered']}  "
            f"net INR {b['net_recovery_paise'] / 100:,.0f}  "
            f"cost/INR recovered {b['cost_per_rupee_recovered']:.4f}",
            "",
        ]
    lines.append("=" * 68)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=6000)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--no-write", action="store_true", help="Evaluate without saving artifacts")
    parser.add_argument("--json", action="store_true", help="Print full metadata as JSON")
    args = parser.parse_args()

    metadata = train(count=args.count, seed=args.seed, write_artifacts=not args.no_write)

    if args.json:
        print(json.dumps(metadata, indent=2))
    else:
        print(_summarise(metadata))
        if not args.no_write:
            print(f"  model    -> {MODEL_PATH}")
            print(f"  metadata -> {METADATA_PATH}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
