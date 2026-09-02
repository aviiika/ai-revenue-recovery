"""Model loading and prediction.

Pure ML concern: loads the artifact and returns a probability. Knows nothing
about cases, policies or the database -- the adapter that bridges the domain to
this module lives in ``apps/api/app/ml/scorer.py``, so the dependency runs one
way only (app -> ml, never ml -> app).

Loading is lazy and failure is non-fatal. A missing or unreadable artifact
returns ``None`` rather than raising, because the spec requires the system to
keep working when the model is unavailable (section 19: "model unavailable ->
deterministic fallback").
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ml.src.features import ScoringContext, context_to_row

logger = logging.getLogger(__name__)

ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "artifacts"
MODEL_PATH = ARTIFACT_DIR / "recovery_model.joblib"
METADATA_PATH = ARTIFACT_DIR / "model_metadata.json"


@dataclass(frozen=True, slots=True)
class LoadedModel:
    """A trained artifact, ready to score."""

    model: Any
    threshold: float
    model_version: str
    selected_model: str
    features: list[str]

    def predict_proba(self, context: ScoringContext) -> float:
        """P(recovery) for one case. Always returns a value in (0, 1)."""
        import pandas as pd

        row = context_to_row(context)
        frame = pd.DataFrame([row])[self.features]
        probability = float(self.model.predict_proba(frame)[0][1])
        # Clamp away from the absolute bounds: a hard 0 or 1 would claim
        # certainty the model has not earned, and downstream expected-value
        # arithmetic reads better with strictly positive probabilities.
        return min(max(probability, 0.001), 0.999)


_cache: LoadedModel | None = None
_load_attempted = False


def load_model(force: bool = False) -> LoadedModel | None:
    """Load the artifact once and cache it. Returns ``None`` if unavailable.

    Deliberately does not raise: an absent model is an expected operating
    condition (a fresh clone has no artifact, since binaries are gitignored),
    and the caller falls back to the deterministic scorer.
    """
    global _cache, _load_attempted

    if force:
        _cache, _load_attempted = None, False
    if _load_attempted:
        return _cache
    _load_attempted = True

    if not MODEL_PATH.exists():
        logger.info(
            "model_artifact_absent",
            extra={"path": str(MODEL_PATH), "action": "falling back to deterministic scorer"},
        )
        return None

    try:
        import joblib

        payload = joblib.load(MODEL_PATH)
        _cache = LoadedModel(
            model=payload["model"],
            threshold=float(payload["threshold"]),
            model_version=str(payload["model_version"]),
            selected_model=str(payload.get("selected_model", "unknown")),
            features=list(payload["features"]),
        )
        logger.info("model_artifact_loaded", extra={"model_version": _cache.model_version})
    except Exception as exc:  # noqa: BLE001 - deliberate, see below
        # Deserialising a binary artifact can fail in essentially unbounded
        # ways: a truncated pickle raises IndexError, a version mismatch raises
        # AttributeError, a wrong file type raises UnpicklingError, and so on.
        # Enumerating them invites the one that was missed to take down the API.
        #
        # This is `except Exception`, not a bare `except:` -- KeyboardInterrupt
        # and SystemExit still propagate. The failure is logged and degrades to
        # the deterministic scorer, which is exactly the behaviour spec section
        # 19 requires of a model that cannot be loaded.
        logger.warning(
            "model_artifact_unreadable",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        _cache = None
    return _cache


def load_metadata() -> dict[str, Any] | None:
    """Training report for the model metrics page. ``None`` if not trained."""
    if not METADATA_PATH.exists():
        return None
    try:
        return dict(json.loads(METADATA_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("model_metadata_unreadable", extra={"error": str(exc)})
        return None
