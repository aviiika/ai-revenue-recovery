"""Metrics endpoints (spec section 14)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.ml.scorer import model_report

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/models")
def model_metrics() -> dict[str, Any]:
    """Training report for the Model Metrics screen (spec section 16).

    Returns ``trained: false`` rather than 404 when no artifact exists: "the
    model has not been trained yet" is information the page should render, not
    an error it should hide.
    """
    report = model_report()
    if report is None:
        return {
            "trained": False,
            "synthetic": True,
            "active_scorer": "deterministic-baseline-v1",
            "message": (
                "No trained model artifact found. The system is running on the "
                "deterministic baseline scorer. Train one with: "
                "python -m ml.src.train --count 6000"
            ),
        }
    return {"trained": True, **report}
