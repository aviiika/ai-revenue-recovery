"""Metrics endpoints (spec section 14)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.schemas import MoneyOut
from app.domain.metrics import service as metrics
from app.ml.scorer import model_report

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/overview")
def overview(session: Session = Depends(get_db)) -> dict[str, Any]:
    """Headline KPIs plus the four dashboard charts (spec section 16).

    Every figure is computed from stored rows. ``synthetic`` drives the banner
    the UI must show whenever generated data is in the result.
    """
    summary = metrics.overview(session)
    return {
        "synthetic": summary.contains_synthetic,
        "kpis": {
            "total_cases": summary.total_cases,
            "revenue_at_risk": MoneyOut.of(summary.revenue_at_risk.paise),
            "revenue_recovered": MoneyOut.of(summary.revenue_recovered.paise),
            "estimated_intervention_cost": MoneyOut.of(summary.estimated_intervention_cost.paise),
            "net_recovery_paise": summary.net_recovery.paise,
            "recovery_rate_by_count": round(summary.recovery_rate_by_count, 4),
            "recovery_rate_by_value": round(summary.recovery_rate_by_value, 4),
            "automated_resolution_rate": round(summary.automated_resolution_rate, 4),
            "cases_recovered": summary.cases_recovered,
            "cases_escalated": summary.cases_escalated,
            "cases_stopped": summary.cases_stopped,
            "cases_in_progress": summary.cases_in_progress,
        },
        "funnel": metrics.recovery_funnel(session),
        "by_state": metrics.state_distribution(session),
        "by_failure_reason": metrics.failure_reason_distribution(session),
        "over_time": metrics.revenue_at_risk_over_time(session),
    }


@router.get("/interventions")
def interventions(session: Session = Depends(get_db)) -> dict[str, Any]:
    """Per-strategy performance, reconstructed from the audit trail."""
    return {"interventions": metrics.intervention_performance(session)}


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
